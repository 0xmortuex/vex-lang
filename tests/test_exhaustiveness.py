"""Exhaustiveness and reachability analysis of `match` (including witnesses)."""

from __future__ import annotations

import pytest

from vex.api import compile_text
from vex.diagnostics import VexError

ENUMS = """
enum Shape { Circle(Int), Rect(Int, Int), Dot }
enum Light { Red, Yellow, Green }
struct P { x: Int, y: Bool }
fn main() {}
"""


def check(match_src: str) -> VexError | None:
    src = ENUMS.replace("fn main() {}", f"fn probe(s: Shape, l: Light, o: Option[Option[Int]], p: P, n: Int, b: Bool) {{ {match_src}; }}\nfn main() {{}}")
    try:
        compile_text(src, "<ex>")
    except VexError as e:
        return e
    return None


EXHAUSTIVE = [
    "match l { Light.Red => 1, Light.Yellow => 2, Light.Green => 3 }",
    "match l { Red => 1, _ => 2 }",
    "match s { Shape.Circle(r) => r, Shape.Rect(w, h) => w * h, Shape.Dot => 0 }",
    "match s { Circle(_) | Rect(_, _) => 1, Dot => 2 }",
    "match b { true => 1, false => 2 }",
    "match b { true => 1, _ => 2 }",
    "match n { 0 => 1, _ => 2 }",
    "match n { x => x }",
    "match o { Some(Some(x)) => x, Some(None) => 0, None => 1 }",
    "match o { Some(_) => 1, None => 2 }",
    "match (b, l) { (true, _) => 1, (false, Red) => 2, (false, Yellow) => 3, (false, Green) => 4 }",
    "match p { P { x, y: true } => x, P { x, y: false } => x + 1 }",
    "match p { P { .. } => 1 }",
    "match (n, b) { (_, true) => 1, (_, false) => 2 }",
    "match s { Circle(r) if r > 0 => 1, Circle(_) => 2, Rect(_, _) => 3, Dot => 4 }",
    "match (l, l) { (Red, _) => 1, (_, Red) => 2, (Yellow, _) => 3, (Green, Yellow) => 4, (Green, Green) => 5 }",
    "match () { () => 1 }",
    "match (b,) { (true,) => 1, (false,) => 2 }",
    "match o { Some(x) => 1, None => 2 }",
]


@pytest.mark.parametrize("src", EXHAUSTIVE)
def test_exhaustive_matches_are_accepted(src: str) -> None:
    err = check(src)
    assert err is None or "non-exhaustive" not in str(err), str(err)


NON_EXHAUSTIVE = [
    ("match l { Light.Red => 1, Light.Yellow => 2 }", "Light.Green"),
    ("match s { Shape.Circle(_) => 1 }", "Shape.Rect(_, _)"),
    ("match s { Circle(_) => 1, Dot => 2 }", "Shape.Rect(_, _)"),
    ("match b { true => 1 }", "false"),
    ("match b { false => 1 }", "true"),
    ("match n { 1 => 1, 2 => 2 }", "_"),
    ("match o { Some(Some(x)) => x, None => 1 }", "Some(None)"),
    ("match o { Some(None) => 0, None => 1 }", "Some(Some(_))"),
    ("match (b, l) { (true, _) => 1, (false, Red) => 2 }", "(false, Light.Yellow)"),
    ("match p { P { y: true, .. } => 1 }", "P { .. }"),
    ("match s { Circle(r) if r > 0 => 1, Rect(_, _) => 2, Dot => 3 }", "Shape.Circle(_)"),
    ("match o { Some(Some(_)) => 1 }", "None"),
]


@pytest.mark.parametrize(("src", "witness"), NON_EXHAUSTIVE)
def test_non_exhaustive_matches_report_a_witness(src: str, witness: str) -> None:
    err = check(src)
    assert err is not None
    codes = [d.code for d in err.diagnostics]
    assert "non-exhaustive-match" in codes
    assert witness in str(err), str(err)


def warnings_for(match_src: str) -> list[str]:
    from vex.api import _compile
    from vex.diagnostics import Source

    src = ENUMS.replace("fn main() {}", f"fn probe(s: Shape, l: Light, n: Int) {{ {match_src}; }}\nfn main() {{}}")
    prog = _compile(Source("<ex>", src), True)
    return [w.code for w in prog.warnings]


@pytest.mark.parametrize(
    "src",
    [
        "match l { Red => 1, Red => 2, _ => 3 }",
        "match n { _ => 1, 5 => 2 }",
        "match l { _ => 1, Red => 2 }",
        "match s { Circle(_) => 1, Circle(1) => 2, _ => 3 }",
        "match n { 1 | 2 => 1, 2 => 2, _ => 3 }",
    ],
)
def test_unreachable_arms_warn(src: str) -> None:
    assert "unreachable-pattern" in warnings_for(src)


@pytest.mark.parametrize(
    "src",
    [
        "match l { Red => 1, Yellow => 2, Green => 3 }",
        "match n { 1 => 1, 2 => 2, _ => 3 }",
        "match n { x if x > 1 => 1, _ => 2 }",
        "match s { Circle(1) => 1, Circle(_) => 2, _ => 3 }",
    ],
)
def test_reachable_arms_do_not_warn(src: str) -> None:
    assert warnings_for(src) == []


def test_guarded_arm_does_not_make_later_arm_unreachable() -> None:
    assert warnings_for("match n { x if x > 0 => 1, y => 2 }") == []


def test_warning_is_not_an_error_and_program_runs() -> None:
    from helpers import run_source

    out = run_source("fn main() { print(match 1 { _ => 1, 2 => 2 }); }")
    assert out == "1\n"


def test_exhaustive_witness_api_directly() -> None:
    from vex import syntax as ast
    from vex.diagnostics import DiagSink, Source
    from vex.exhaust import missing_pattern, show, to_spat
    from vex.parser import parse_source

    sink = DiagSink()
    mod = parse_source(Source("<w>", "fn main() { match x { (true, _) => 1 } }"), sink)
    fn = mod.items[0]
    assert isinstance(fn, ast.FnDecl) and isinstance(fn.body.tail, ast.Match)
    w = missing_pattern([to_spat(fn.body.tail.arms[0].pat)])
    assert w is not None and show(w) == "(false, _)"


def test_irrefutable_let_patterns_ok_and_refutable_rejected() -> None:
    compile_text("fn main() { let (a, b) = (1, 2); let (c, (d, e)) = (1, (2, 3)); print(a + b + c + d + e); }")
    with pytest.raises(VexError) as info:
        compile_text("fn main() { let Some(x) = Some(1); }")
    assert info.value.diagnostics[0].code == "refutable-pattern"
