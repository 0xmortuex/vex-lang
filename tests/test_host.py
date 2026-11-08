"""The per-instance Python host API: bindings, capabilities, budget and error reporting."""

from __future__ import annotations

import io
import math
from typing import Any

import pytest
from helpers import error_codes

from vex import stdlib
from vex.diagnostics import VexError, VexRuntimeError
from vex.host import (
    CAP_ENV,
    CAP_PRINT,
    CAP_READ,
    CAP_WRITE,
    DEFAULT_BUDGET,
    MAX_HOST_DEPTH,
    Host,
    VexBindingError,
    VexBudgetError,
)


def codes(host: Host, src: str) -> list[str]:
    with pytest.raises(VexError) as e:
        host.load("rule.vx", src)
    return [d.code for d in e.value.diagnostics]


# ------------------------------------------------------------------ binding
def test_script_calls_bound_function_and_host_calls_script() -> None:
    h = Host()
    h.bind("tax", "(Int) -> Int", lambda n: n // 10)
    h.load("rule.vx", "fn total(n: Int) { n + host.tax(n) }")
    assert h.call("total", [100]) == 110


def test_all_scalar_types_cross_a_binding() -> None:
    h = Host()
    h.bind("f", "(Int, Float, Bool, Str) -> Str", lambda a, b, c, d: f"{a}|{b}|{c}|{d}")
    h.bind("nothing", "()", lambda: None)
    h.load("r.vx", 'fn go() { host.nothing(); host.f(1, 2.5, true, "é") }')
    assert h.call("go", []) == "1|2.5|True|é"


def test_bindings_are_per_instance_and_leave_stdlib_alone() -> None:
    before = (dict(stdlib.GLOBALS), {k: dict(v) for k, v in stdlib.NAMESPACES.items()})
    a = Host()
    b = Host()
    a.bind("x", "() -> Int", lambda: 1)
    b.bind("x", "() -> Int", lambda: 2)
    a.load("a.vx", "fn go() { host.x() }")
    b.load("b.vx", "fn go() { host.x() }")
    assert (a.call("go", []), b.call("go", [])) == (1, 2)
    assert (dict(stdlib.GLOBALS), {k: dict(v) for k, v in stdlib.NAMESPACES.items()}) == before


def test_unbound_host_name_is_a_compile_error_with_did_you_mean() -> None:
    h = Host()
    h.bind("tax_cents", "(Int) -> Int", lambda n: n)
    with pytest.raises(VexError) as e:
        h.load("r.vx", "fn go() { host.tax_cent(1) }")
    d = e.value.diagnostics[0]
    assert d.code == "unknown-member"
    assert d.help == "did you mean `tax_cents`?"
    assert d.span is not None and (d.span.source.path, d.span.line) == ("r.vx", 1)


def test_bound_call_is_type_checked() -> None:
    h = Host()
    h.bind("tax", "(Int) -> Int", lambda n: n)
    assert codes(h, 'fn go() { host.tax("x") }') == ["type-mismatch"]


def test_host_namespace_does_not_exist_outside_an_embedding() -> None:
    assert error_codes("fn main() { host.x(); }") == ["undefined-name"]


@pytest.mark.parametrize("src", ["fn host() { }\nfn main() { }", "struct host { x: Int }\nfn main() { }"])
def test_host_is_a_reserved_name_everywhere(src: str) -> None:
    assert error_codes(src) == ["reserved-name"]


def test_a_local_named_host_shadows_the_namespace() -> None:
    h = Host()
    h.load("r.vx", "fn go() { let host = 3; host + 1 }")
    assert h.call("go", []) == 4


@pytest.mark.parametrize(
    "sig",
    [
        "(List[Int]) -> Int",
        "() -> Map[Str, Int]",
        "((Int, Int)) -> Int",
        "(Point) -> Int",
        "(Option[Int])",
        "(fn(Int) -> Int)",
        "[T](T) -> T",
        "() -> Never",
        "(Int",
        "Int",
        "",
        "(Int) -> Int trailing",
    ],
)
def test_bad_signatures_are_rejected(sig: str) -> None:
    h = Host()
    with pytest.raises(VexBindingError):
        h.bind("f", sig, lambda *a: None)


@pytest.mark.parametrize("name", ["", "1x", "a b", "a.b", "fn", "match", "é x"])
def test_bad_names_are_rejected(name: str) -> None:
    with pytest.raises(VexBindingError):
        Host().bind(name, "()", lambda: None)


def test_duplicate_binding_is_rejected() -> None:
    h = Host()
    h.bind("f", "()", lambda: None)
    with pytest.raises(VexBindingError):
        h.bind("f", "()", lambda: None)


def test_a_binding_made_after_load_applies_to_the_next_load() -> None:
    h = Host()
    h.load("r.vx", "fn go() { 1 }")
    h.bind("f", "() -> Int", lambda: 2)
    h.load("r.vx", "fn go() { host.f() }")
    assert h.call("go", []) == 2


# ------------------------------------------------------------------ capabilities
DENIED = [
    (0, 'fn go() { print("x"); }', "print"),
    (0, 'fn go() { eprint("x"); }', "eprint"),
    (0, 'fn go() { io.read_file("x"); }', "io.read_file"),
    (0, "fn go() { io.read_line(); }", "io.read_line"),
    (0, 'fn go() { io.write_file("x", "y"); }', "io.write_file"),
    (0, 'fn go() { env.get("x"); }', "env.get"),
    (0, "fn go() { env.args(); }", "env.args"),
    (CAP_PRINT | CAP_READ | CAP_ENV, 'fn go() { io.write_file("x", "y"); }', "io.write_file"),
    (CAP_WRITE, 'fn go() { io.read_file("x"); }', "io.read_file"),
    (15, "fn go() { exit(7); }", "exit"),
]


@pytest.mark.parametrize(("caps", "src", "name"), DENIED)
def test_ungranted_names_are_compile_errors(caps: int, src: str, name: str) -> None:
    h = Host(caps)
    with pytest.raises(VexError) as e:
        h.load("r.vx", src)
    d = e.value.diagnostics[0]
    assert d.code == "capability-denied"
    assert f"`{name}`" in d.message
    assert d.span is not None and d.span.source.path == "r.vx"


def test_write_is_really_blocked(tmp_path: Any) -> None:
    target = tmp_path / "out.txt"
    h = Host(CAP_PRINT)
    with pytest.raises(VexError):
        h.load("r.vx", f'fn go() {{ io.write_file("{target.as_posix()}", "x"); }}')
    assert not target.exists()


def test_granted_capabilities_work(tmp_path: Any) -> None:
    target = (tmp_path / "out.txt").as_posix()
    out = io.StringIO()
    h = Host(CAP_PRINT | CAP_READ | CAP_WRITE | CAP_ENV, out=out)
    h.load("r.vx", f"""
        fn go() {{
            io.write_file("{target}", "hi").unwrap();
            print(io.read_file("{target}").unwrap());
            env.get("PATH").is_some()
        }}""")
    assert h.call("go", []) is True
    assert out.getvalue() == "hi\n"


def test_pure_builtins_need_no_capability() -> None:
    h = Host()
    h.load("r.vx", 'fn go() { assert(true); assert_eq(1, 1); math.sqrt(4.0) + "ab".len().to_float() }')
    assert h.call("go", []) == 4.0


def test_unknown_capability_bits_are_rejected() -> None:
    with pytest.raises(VexBindingError):
        Host(16)
    with pytest.raises(VexBindingError):
        Host(-1)


def test_a_script_cannot_redefine_an_ungranted_name() -> None:
    assert codes(Host(), 'fn print(x: Int) { x }') == ["reserved-name"]


def test_exit_stays_available_to_programs_outside_an_embedding() -> None:
    assert error_codes("fn main() { exit(0); }") == []


def test_embedded_scripts_cannot_import(tmp_path: Any) -> None:
    (tmp_path / "util.vx").write_text("fn f() { 1 }")
    path = tmp_path / "main.vx"
    src = "import util;\nfn go() { f() }"
    path.write_text(src)
    assert codes(Host(), src) == ["import-denied"]
    with pytest.raises(VexError) as e:
        Host().load(str(path), src)
    assert e.value.diagnostics[0].code == "import-denied"


# ------------------------------------------------------------------ errors from the script
def test_compile_error_carries_location_and_host_can_load_again() -> None:
    h = Host()
    with pytest.raises(VexError) as e:
        h.load("rules/bad.vx", "fn go() {\n  let x: Int = true;\n}")
    assert "rules/bad.vx:2:" in str(e.value)
    h.load("rules/good.vx", "fn go() { 5 }")
    assert h.call("go", []) == 5


def test_a_failed_load_leaves_the_previous_script_running() -> None:
    h = Host()
    h.load("a.vx", "fn go() { 1 }")
    with pytest.raises(VexError):
        h.load("b.vx", "fn go() { oops }")
    assert h.call("go", []) == 1


def test_overflow_is_a_located_runtime_error_and_host_carries_on() -> None:
    h = Host()
    h.load("rules/o.vx", "fn go(x: Int) {\n  x * 2\n}\nfn ok() { 9 }")
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [math.floor(2**62)])
    assert e.value.message == "integer overflow in `*`"
    rendered = e.value.render()
    assert "rules/o.vx:2:3" in rendered
    assert h.call("ok", []) == 9


def test_panic_in_script_reports_every_frame_once() -> None:
    h = Host()
    h.load("r.vx", 'fn inner() -> Int { panic("bad") }\nfn go() { inner(); }')
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [])
    assert [f[0] for f in e.value.trace] == ["inner", "go"]


def test_panic_in_a_builtin_that_uses_the_vm_reports_every_frame_once() -> None:
    h = Host()
    h.load("r.vx", 'fn go() { "a".repeat(-1) }')
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [])
    assert [f[0] for f in e.value.trace] == ["go"]


def test_internal_python_errors_become_runtime_errors() -> None:
    h = Host()
    h.load("r.vx", "fn go() { (math.INT_MIN..math.INT_MAX).len() }")
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [])
    assert "internal error" in e.value.message


def test_stack_overflow_is_a_runtime_error() -> None:
    h = Host()
    h.budget(10**9)
    h.load("r.vx", "fn f(n: Int) -> Int { f(n + 1) }")
    with pytest.raises(VexRuntimeError) as e:
        h.call("f", [0])
    assert "stack overflow" in e.value.message
    assert not isinstance(e.value, VexBudgetError)


# ------------------------------------------------------------------ calling the script
def test_unknown_function_with_suggestion() -> None:
    h = Host()
    h.load("r.vx", "fn price() { 1 }")
    with pytest.raises(VexBindingError, match="did you mean `price`"):
        h.call("prices", [])


def test_call_before_load() -> None:
    with pytest.raises(VexBindingError, match="no script"):
        Host().call("go", [])


def test_wrong_arity() -> None:
    h = Host()
    h.load("r.vx", "fn go(x: Int) { x }")
    with pytest.raises(VexBindingError, match="expects 1 argument"):
        h.call("go", [])


@pytest.mark.parametrize(
    ("param", "value"),
    [
        ("Int", True),
        ("Int", 1.0),
        ("Int", "1"),
        ("Int", 2**63),
        ("Int", -(2**63) - 1),
        ("Float", 1),
        ("Bool", 1),
        ("Str", 1),
        ("Str", b"x"),
        ("Str", "\ud800"),
        ("Unit", 0),
    ],
)
def test_wrong_argument_values_are_binding_errors(param: str, value: Any) -> None:
    h = Host()
    h.load("r.vx", f"fn go(x: {param}) {{ }}")
    with pytest.raises(VexBindingError):
        h.call("go", [value])


@pytest.mark.parametrize(
    "src",
    [
        "fn go(x: List[Int]) { 0 }",
        "fn go() { [1] }",
        "fn go() { (1, 2) }",
        "fn go[T](x: T) { 0 }",
        "fn go() { Some(1) }",
        "struct P { x: Int }\nfn go() { P { x: 1 } }",
        "fn go() { |x: Int| x }",
    ],
)
def test_aggregates_do_not_cross(src: str) -> None:
    h = Host()
    h.load("r.vx", src)
    with pytest.raises(VexBindingError, match="cross"):
        h.call("go", [[1]] if "List" in src else [1] if "[T]" in src else [])


def test_int_at_the_edges_crosses_exactly() -> None:
    h = Host()
    h.bind("id", "(Int) -> Int", lambda n: n)
    h.load("r.vx", "fn go(x: Int) { host.id(x) }")
    assert h.call("go", [2**63 - 1]) == 2**63 - 1
    assert h.call("go", [-(2**63)]) == -(2**63)


def test_nan_crosses() -> None:
    h = Host()
    h.load("r.vx", "fn go(x: Float) { x }")
    assert math.isnan(h.call("go", [math.nan]))


def test_out_of_range_int_result_is_an_error() -> None:
    h = Host()
    h.load("r.vx", "fn go() { math.gcd(math.INT_MIN, 0) }")
    with pytest.raises(VexRuntimeError, match="64-bit"):
        h.call("go", [])


def test_lone_surrogate_result_is_an_error() -> None:
    h = Host()
    h.load("r.vx", "fn go() { 55296.to_char() }")
    with pytest.raises(VexRuntimeError, match="UTF-8"):
        h.call("go", [])


# ------------------------------------------------------------------ host function failures
def test_host_panic_is_a_located_runtime_error() -> None:
    h = Host()
    h.bind("boom", "()", lambda: h.fail("out of stock"))
    h.load("r.vx", "fn go() {\n  host.boom();\n}")
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [])
    assert e.value.message == "out of stock"
    assert "r.vx:2:" in e.value.render()


def test_host_exception_is_a_runtime_error() -> None:
    def bad() -> None:
        raise KeyError("nope")

    h = Host()
    h.bind("bad", "()", bad)
    h.load("r.vx", "fn go() { host.bad(); }")
    with pytest.raises(VexRuntimeError, match="host function `bad` raised KeyError"):
        h.call("go", [])


@pytest.mark.parametrize(
    ("sig", "value"),
    [
        ("() -> Int", 2**70),
        ("() -> Int", True),
        ("() -> Int", "x"),
        ("() -> Float", 1),
        ("() -> Bool", 1),
        ("() -> Str", None),
        ("() -> Str", "\ud800"),
        ("()", 5),
    ],
)
def test_host_return_value_is_checked_against_its_signature(sig: str, value: Any) -> None:
    h = Host()
    h.bind("f", sig, lambda: value)
    h.load("r.vx", "fn go() { host.f(); }")
    with pytest.raises(VexBindingError, match="host function `f`") as e:
        h.call("go", [])
    assert "r.vx:1:" in str(e.value)


def test_bound_function_used_as_a_value() -> None:
    h = Host()
    h.bind("twice", "(Int) -> Int", lambda n: n * 2)
    h.load("r.vx", "fn go() { [1, 2].map(host.twice).sum() }")
    assert h.call("go", []) == 6


# ------------------------------------------------------------------ budget
def test_infinite_loop_stops_at_the_budget_with_a_location() -> None:
    h = Host()
    h.budget(1000)
    h.load("r.vx", "fn go() {\n  while true {\n  }\n}")
    with pytest.raises(VexBudgetError) as e:
        h.call("go", [])
    assert isinstance(e.value, VexRuntimeError)
    assert "budget" in e.value.message
    assert "r.vx:2:" in e.value.render() or "r.vx:3:" in e.value.render()


def test_default_budget() -> None:
    assert DEFAULT_BUDGET == 10_000_000
    assert Host()._budget == DEFAULT_BUDGET


@pytest.mark.parametrize("units", [0, -1])
def test_budget_must_be_positive(units: int) -> None:
    with pytest.raises(VexBindingError):
        Host().budget(units)


def spent(src: str, name: str = "go", args: list[Any] | None = None, budget: int = 10_000) -> int:
    """How many units one call consumes."""
    h = Host()
    h.budget(budget)
    h.load("r.vx", src)
    h.call(name, args or [])
    return budget - h.fuel_left()


def test_one_unit_for_the_outer_call() -> None:
    assert spent("fn go() { 1 }") == 1


def test_one_unit_per_function_entry() -> None:
    assert spent("fn a() { 1 }\nfn go() { a(); a(); a() }") == 4


def test_one_unit_per_loop_back_edge() -> None:
    assert spent("fn go() { for _ in 0..5 { } }") == 6
    assert spent("fn go() { let mut i = 0; while i < 5 { i += 1; } }") == 6


def test_closures_entered_by_builtins_are_charged() -> None:
    assert spent("fn go() { [1, 2, 3].map(|x| x + 1).len() }") >= 4


def test_budget_exactly_spent_is_not_exhausted() -> None:
    h = Host()
    h.budget(4)
    h.load("r.vx", "fn a() { 1 }\nfn go() { a(); a(); a() }")
    assert h.call("go", []) == 1
    h.budget(3)
    with pytest.raises(VexBudgetError):
        h.call("go", [])


def test_budget_resets_on_each_outermost_call() -> None:
    h = Host()
    h.budget(50)
    h.load("r.vx", "fn go() { for _ in 0..30 { } }")
    for _ in range(5):
        h.call("go", [])


def test_a_budget_stop_leaves_the_instance_usable() -> None:
    h = Host()
    h.budget(20)
    h.load("r.vx", "fn spin() { while true { } }\nfn ok() { 3 }")
    with pytest.raises(VexBudgetError):
        h.call("spin", [])
    assert h.call("ok", []) == 3


@pytest.mark.parametrize(
    "body",
    [
        '"x".repeat(1000000)',
        "(0..1000000).to_list().len()",
        '"xxxxxxxxxx".repeat(10).replace("x", "yyyyyyyyyyyyyyyyyyyy").repeat(100)',
    ],
)
def test_size_amplifying_builtins_charge_by_size(body: str) -> None:
    h = Host()
    h.budget(1000)
    h.load("r.vx", "fn go() { " + body + "; }")
    with pytest.raises(VexBudgetError):
        h.call("go", [])


@pytest.mark.parametrize(
    "stmt",
    [
        "s = s + s;",
        's = "#{s}#{s}";',
        's = [s, s].join("");',
        's = s.replace("x", "xx");',
    ],
)
def test_string_doubling_is_stopped_by_the_budget(stmt: str) -> None:
    h = Host()
    h.budget(1_000_000)
    h.load("r.vx", 'fn go() { let mut s = "x"; while true { ' + stmt + " } }")
    with pytest.raises(VexBudgetError):
        h.call("go", [])


@pytest.mark.parametrize("stmt", ["xs = xs.concat(xs);", "xs.extend(xs.copy());"])
def test_list_doubling_is_stopped_by_the_budget(stmt: str) -> None:
    h = Host()
    h.budget(1_000_000)
    h.load("r.vx", "fn go() { let mut xs = [1]; while true { " + stmt + " } }")
    with pytest.raises(VexBudgetError):
        h.call("go", [])


def test_programs_outside_an_embedding_have_no_budget() -> None:
    from helpers import run_body

    assert run_body('let mut i = 0; while i < 20000 { i += 1; } print("x".repeat(20000).len());') == "20000\n"


# ------------------------------------------------------------------ re-entry
def test_reentry_works_and_keeps_outer_frames_intact() -> None:
    h = Host()
    h.bind("again", "(Int) -> Int", lambda n: h.call("inner", [n]) * 2)
    h.load("r.vx", "fn inner(n: Int) { n + 1 }\nfn go(n: Int) { 1 + host.again(n) + 1 }")
    assert h.call("go", [1]) == 6


def test_reentrant_runtime_error_reaches_the_outer_call() -> None:
    h = Host()
    h.bind("again", "()", lambda: h.call("bad", []))
    h.load("r.vx", 'fn bad() { panic("inner") }\nfn go() {\n  host.again();\n}')
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [])
    assert "inner" in e.value.message
    assert "r.vx:3:" in e.value.render()


def test_reentry_shares_the_outer_budget() -> None:
    h = Host()
    h.budget(40)
    h.bind("again", "()", lambda: h.call("spin", []))
    h.load("r.vx", "fn spin() { for _ in 0..30 { } }\nfn go() { host.again(); host.again(); }")
    with pytest.raises(VexBudgetError):
        h.call("go", [])


def test_reentry_after_a_budget_stop_still_reports_budget() -> None:
    h = Host()
    h.budget(20)

    def swallow() -> None:
        try:
            h.call("spin", [])
        except VexBudgetError:
            pass

    h.bind("again", "()", swallow)
    h.load("r.vx", "fn spin() { while true { } }\nfn go() { host.again(); }")
    with pytest.raises(VexBudgetError):
        h.call("go", [])


def test_runaway_reentry_is_stopped_before_the_native_stack_is() -> None:
    h = Host()
    seen = [0]

    def again(n: int) -> int:
        seen[0] = n
        r: int = h.call("go", [n + 1])
        return r

    h.bind("again", "(Int) -> Int", again)
    h.load("r.vx", "fn go(n: Int) { host.again(n) }")
    with pytest.raises(VexRuntimeError):
        h.call("go", [0])
    assert seen[0] <= MAX_HOST_DEPTH


def test_depth_counter_recovers_after_failed_reentry() -> None:
    h = Host()
    h.bind("again", "()", lambda: h.call("go", []))
    h.load("r.vx", "fn go() { host.again(); }\nfn ok() { 1 }")
    with pytest.raises(VexRuntimeError):
        h.call("go", [])
    assert h.call("ok", []) == 1


# ------------------------------------------------------------------ output
def test_print_goes_to_the_instance_output() -> None:
    out = io.StringIO()
    h = Host(CAP_PRINT, out=out)
    h.load("r.vx", 'fn go() { print("hi"); }')
    h.call("go", [])
    assert out.getvalue() == "hi\n"
