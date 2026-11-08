"""Diagnostic rendering, suggestions, error counts and unification unit tests."""

from __future__ import annotations

import pytest

from vex.api import compile_text
from vex.diagnostics import (
    MAX_SNIPPET_WIDTH,
    Diagnostic,
    DiagSink,
    Source,
    Span,
    VexError,
    VexRuntimeError,
    out_of_memory_error,
    suggest,
)
from vex.vtypes import (
    BOOL,
    ERROR,
    INT,
    NEVER,
    STR,
    UNIT,
    Mismatch,
    TFn,
    TParam,
    TTuple,
    TVar,
    list_of,
    map_of,
    option_of,
    prune,
    show,
    subst,
    unify,
    zonk,
)


def errors_of(src: str) -> VexError:
    with pytest.raises(VexError) as info:
        compile_text(src, "d.vx")
    return info.value


# ---------------------------------------------------------------- rendering
def test_render_has_location_snippet_and_caret() -> None:
    src = "fn main() {\n    let x: Int = \"s\";\n}\n"
    text = str(errors_of(src))
    assert "error[type-mismatch]: mismatched types" in text
    assert "--> d.vx:2:18" in text
    assert '2 |     let x: Int = "s";' in text
    caret_line = [ln for ln in text.splitlines() if "^^^" in ln][0]
    src_line = [ln for ln in text.splitlines() if ln.lstrip().startswith("2 |")][0]
    assert caret_line.index("^") == src_line.index('"s"')
    assert "expected `Int`, found `Str`" in caret_line


def test_caret_width_matches_span() -> None:
    text = str(errors_of("fn main() {\n    print(undefined_name);\n}\n"))
    caret = [ln for ln in text.splitlines() if "^" in ln][0]
    assert caret.count("^") == len("undefined_name")


def test_render_secondary_span_and_help() -> None:
    text = str(errors_of("fn main() {\n    let x = 1;\n    x = 2;\n}\n"))
    assert "declared here" in text
    assert "= help: declare it with `let mut x`" in text


def test_render_tabs_keep_caret_aligned() -> None:
    src = Source("t.vx", "fn main() {\n\tlet x = y;\n}\n")
    d = Diagnostic("undefined-name", "oops", Span(src, src.text.index("y;"), src.text.index("y;") + 1), "here")
    lines = d.render().splitlines()
    code_line = [ln for ln in lines if "let x" in ln][0]
    caret_line = [ln for ln in lines if "^" in ln][0]
    assert code_line.split("|", 1)[1].startswith(" \t")
    assert caret_line.split("|", 1)[1].startswith(" \t")


def test_render_without_span() -> None:
    d = Diagnostic("no-main", "this program has no `main` function", None, notes=["add one"])
    assert d.render().startswith("error[no-main]")
    assert "note: add one" in d.render()


def test_render_warning_severity() -> None:
    d = Diagnostic("w", "careful", None, severity="warning")
    assert d.render().startswith("warning[w]")


def test_multiple_errors_in_one_run_are_all_reported() -> None:
    src = """
fn a() -> Int { "x" }
fn b() -> Str { 1 }
fn c() { undefined(); }
fn main() { let x: Int = true; }
"""
    err = errors_of(src)
    assert len(err.diagnostics) == 4
    lines = [d.span.line for d in err.diagnostics if d.span is not None]
    assert lines == sorted(lines)


def test_errors_are_sorted_by_position_across_stages() -> None:
    src = "fn main() {\n    let a: Int = \"s\";\n    print(zzz);\n}\n"
    err = errors_of(src)
    assert [d.code for d in err.diagnostics] == ["type-mismatch", "undefined-name"]


def test_error_cascades_are_suppressed() -> None:
    err = errors_of("fn main() {\n    let x = nope;\n    let y = x + 1;\n    print(y);\n}\n")
    assert len(err.diagnostics) == 1


def test_unknown_function_does_not_also_report_cannot_infer() -> None:
    err = errors_of("fn main() {\n    print(missing(1));\n}\n")
    assert [d.code for d in err.diagnostics] == ["undefined-name"]


def test_parse_errors_stop_before_type_checking() -> None:
    err = errors_of("fn main() { let x = ; let y: Int = \"s\"; }")
    assert [d.code for d in err.diagnostics] == ["expected-expression"]


def test_too_many_errors_are_capped() -> None:
    src = "fn main() {\n" + "    let x = ;\n" * 200 + "}\n"
    err = errors_of(src)
    assert len(err.diagnostics) < 120


def test_diagnostic_dedup() -> None:
    sink = DiagSink()
    src = Source("x", "abc")
    sp = Span(src, 0, 1)
    sink.error("c", "m", sp)
    sink.error("c", "m", sp)
    assert len(sink.items) == 1


# ---------------------------------------------------------------- suggestions
@pytest.mark.parametrize(
    ("name", "pool", "expected"),
    [
        ("totl", ["total", "count"], "total"),
        ("Total", ["total"], "total"),
        ("lenght", ["len", "length", "push"], "length"),
        ("pritn", ["print", "eprint"], "print"),
        ("zzzz", ["total", "count"], None),
        ("x", [], None),
        ("same", ["same"], None),
        ("widht", ["width", "height"], "width"),
    ],
)
def test_suggest(name: str, pool: list[str], expected: str | None) -> None:
    assert suggest(name, pool) == expected


# ---------------------------------------------------------------- unification
def test_unify_primitives() -> None:
    unify(INT, INT)
    with pytest.raises(Mismatch):
        unify(INT, STR)


def test_unify_binds_variables_both_ways() -> None:
    a, b = TVar(), TVar()
    unify(a, INT)
    unify(STR, b)
    assert show(a) == "Int" and show(b) == "Str"


def test_unify_chain_of_variables() -> None:
    a, b, c = TVar(), TVar(), TVar()
    unify(a, b)
    unify(b, c)
    unify(c, BOOL)
    assert prune(a) is BOOL


def test_unify_nested_structures() -> None:
    v = TVar()
    unify(list_of(option_of(v)), list_of(option_of(INT)))
    assert show(v) == "Int"
    w = TVar()
    unify(map_of(STR, w), map_of(STR, list_of(INT)))
    assert show(w) == "List[Int]"


def test_occurs_check() -> None:
    v = TVar()
    with pytest.raises(Mismatch) as info:
        unify(v, list_of(v))
    assert info.value.infinite


def test_unify_functions_and_tuples() -> None:
    a = TVar()
    unify(TFn((INT, a), BOOL), TFn((INT, STR), BOOL))
    assert show(a) == "Str"
    with pytest.raises(Mismatch):
        unify(TFn((INT,), INT), TFn((INT, INT), INT))
    with pytest.raises(Mismatch):
        unify(TTuple((INT, INT)), TTuple((INT,)))


def test_never_and_error_unify_with_everything() -> None:
    unify(NEVER, INT)
    unify(STR, NEVER)
    unify(ERROR, list_of(INT))
    v = TVar()
    unify(v, NEVER)
    assert isinstance(prune(v), TVar)  # Never does not constrain
    unify(v, ERROR)
    assert show(v) == "<error>"


def test_rigid_type_params_only_match_themselves() -> None:
    t, u = TParam("T"), TParam("U")
    unify(t, t)
    with pytest.raises(Mismatch):
        unify(t, u)
    with pytest.raises(Mismatch):
        unify(t, INT)


def test_subst_and_zonk() -> None:
    t = TParam("T")
    sig = TFn((list_of(t),), option_of(t))
    out = subst(sig, {t: INT})
    assert show(out) == "fn(List[Int]) -> Option[Int]"
    v = TVar()
    wrapped = list_of(v)
    unify(v, STR)
    assert show(zonk(wrapped)) == "List[Str]"


@pytest.mark.parametrize(
    ("ty", "text"),
    [
        (INT, "Int"),
        (UNIT, "Unit"),
        (TTuple((INT, STR)), "(Int, Str)"),
        (TTuple((INT,)), "(Int,)"),
        (TFn((), UNIT), "fn() -> Unit"),
        (map_of(STR, list_of(INT)), "Map[Str, List[Int]]"),
        (TVar(), "_"),
        (NEVER, "Never"),
    ],
)
def test_show(ty: object, text: str) -> None:
    from vex.vtypes import Type

    assert isinstance(ty, Type)
    assert show(ty) == text


# ---------------------------------------------------------------- rendering cannot grow with the source line
def caret_points_at(rendered: str) -> str:
    """The source character above the first caret of a rendered snippet."""
    lines = rendered.splitlines()
    index = next(i for i, ln in enumerate(lines) if "^" in ln)
    return lines[index - 1][lines[index].index("^")]


def test_snippet_of_a_huge_line_is_bounded_and_still_points_at_the_error() -> None:
    src = Source("<t>", "a" * 100_000 + "@" + "b" * 100_000)
    rendered = Diagnostic("c", "m", Span(src, 100_000, 100_001), "here").render()
    assert len(rendered) < 1000
    assert caret_points_at(rendered) == "@"
    assert "<t>:1:100001" in rendered
    assert "...a" in rendered and "b..." in rendered


def test_snippet_at_the_ends_of_a_huge_line() -> None:
    src = Source("<t>", "@" + "a" * 100_000 + "#")
    start = Diagnostic("c", "m", Span(src, 0, 1)).render()
    end = Diagnostic("c", "m", Span(src, 100_001, 100_002)).render()
    assert len(start) < 1000 and caret_points_at(start) == "@" and "| ...@" not in start
    assert len(end) < 1000 and caret_points_at(end) == "#"


def test_caret_run_of_a_span_across_a_huge_line_is_bounded() -> None:
    src = Source("<t>", "a" * 100_000)
    rendered = Diagnostic("c", "m", Span(src, 10, 99_990)).render()
    assert len(rendered) < 1000


def test_only_the_line_with_the_error_is_read_from_a_long_file() -> None:
    src = Source("<t>", "short\n" + "a" * 100_000 + "\nlast")
    assert "| last" in Diagnostic("c", "m", Span(src, 100_007, 100_008)).render()


def test_a_line_at_the_width_limit_is_shown_whole() -> None:
    text = "a" * MAX_SNIPPET_WIDTH
    rendered = Diagnostic("c", "m", Span(Source("<t>", text), 5, 7)).render()
    assert f"1 | {text}\n" in rendered and "..." not in rendered


def test_a_line_just_over_the_width_limit_is_clipped() -> None:
    text = "a" * (MAX_SNIPPET_WIDTH + 1)
    assert "..." in Diagnostic("c", "m", Span(Source("<t>", text), 5, 7)).render()


def test_out_of_memory_diagnostic_of_a_one_line_file_is_small() -> None:
    error = out_of_memory_error(Source("<t>", "1," * 2_500_000))
    assert len(str(error)) < 1000


def test_runtime_error_on_a_huge_line_is_small() -> None:
    src = Source("<t>", "a" * 100_000)
    assert len(VexRuntimeError("boom", [("main", Span(src, 50_000, 50_001))]).render()) < 1000
