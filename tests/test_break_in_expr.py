"""``break`` / ``continue`` in the middle of an expression (they have type ``Never``).

Every program nests the jump inside an outer ``for``: a temporary left behind sits on top of that
loop's iterator, so a leak becomes a crash, a wrong result or a hang instead of going unseen.
"""

from __future__ import annotations

import _thread
import io
import threading
from typing import Callable

import pytest
from helpers import error_codes

from vex.api import compile_text
from vex.diagnostics import VexRuntimeError
from vex.vm import VM

PRELUDE = """
fn add(a: Int, b: Int) -> Int { a + b }
struct P { x: Int, y: Int }
enum Two { Both(Int, Int) }
"""

TIME_LIMIT_S = 3
VALUE = "if i == 1 { %s } else { 7 }"
LOOPS = {
    "for": "for i in 0..3 {",
    "while": "let mut i = -1; while i < 2 { i += 1;",
}


def run(src: str) -> str:
    """Run a program; a corrupt stack can loop forever, so that is turned into a test failure."""
    prog = compile_text(PRELUDE + src, "<test>")
    out = io.StringIO()
    assert prog.main is not None
    watchdog = threading.Timer(TIME_LIMIT_S, _thread.interrupt_main)
    watchdog.start()
    try:
        VM(out).run_function(prog.main)
    except KeyboardInterrupt:
        raise AssertionError(f"did not terminate within {TIME_LIMIT_S} s") from None
    finally:
        watchdog.cancel()
    return out.getvalue()


def main(body: str) -> str:
    return run("fn main() {\n" + body + "\n}\n")


# @V is a value that jumps when i == 1 and is otherwise 7; @X is the bare jump.
EXPRESSIONS: list[tuple[str, str, Callable[[int], str]]] = [
    ("call_second_arg", "add(i, @V)", lambda i: str(i + 7)),
    ("call_first_arg", "add(@V, i)", lambda i: str(i + 7)),
    ("call_nested", "add(add(i, @V), add(@V, i))", lambda i: str(2 * i + 14)),
    ("call_method_receiver_held", "[1, 7].contains(@V)", lambda i: "true"),
    ("call_closure_callee_held", "(|a: Int, b: Int| a + b)(i, @V)", lambda i: str(i + 7)),
    ("call_variant_payload", "match Both(i, @V) { Both(a, b) => a + b }", lambda i: str(i + 7)),
    ("binary_left", "@V + i", lambda i: str(i + 7)),
    ("binary_right", "i + @V", lambda i: str(i + 7)),
    ("binary_deep", "1 + (2 * (3 + @V))", lambda i: "21"),
    ("binary_compare", "i < @V", lambda i: "true"),
    ("binary_and_right", "true && (i < @V)", lambda i: "true"),
    ("binary_or_left", "(i > @V) || true", lambda i: "true"),
    ("unary_under_binary", "1 + (-@V)", lambda i: "-6"),
    ("list_literal", "[i, @V]", lambda i: f"[{i}, 7]"),
    ("list_nested", "[[i], [@V]]", lambda i: f"[[{i}], [7]]"),
    ("tuple_literal", "(i, @V)", lambda i: f"({i}, 7)"),
    ("map_value", "#{i: @V}[i]", lambda i: "7"),
    ("map_key", "#{@V: i}[7]", lambda i: str(i)),
    ("struct_in_order", "P { x: i, y: @V }.y", lambda i: "7"),
    ("struct_out_of_order", "add(i, P { y: @V, x: i }.y)", lambda i: str(i + 7)),
    ("interpolation", '"a{i}b{@V}"', lambda i: f"a{i}b7"),
    ("index_held_object", "[10, 11, 12, 13, 14, 15, 16, 17][@V]", lambda i: "17"),
    ("range_held_low", "{ let mut s = 0; for k in i..@V { s += k; } s }", lambda i: str(sum(range(i, 7)))),
    ("if_condition", "if i < @V { 1 } else { 0 }", lambda i: "1"),
    ("if_branch_in_call", "add(i, if i < 9 { @V } else { 0 })", lambda i: str(i + 7)),
    ("match_scrutinee", "i + match @V { 7 => 1, _ => 0 }", lambda i: str(i + 1)),
    ("match_arm", "i + match i { 1 => @X, n => n }", lambda i: str(2 * i)),
    ("match_guard", "i + match i { n if @V > 0 => n, _ => 0 }", lambda i: str(2 * i)),
    ("block_tail", "i + { let y = @V; y }", lambda i: str(i + 7)),
]


def jump_cases() -> list[tuple[str, str, list[int]]]:
    return [("break", "break", [0]), ("continue", "continue", [0, 2])]


@pytest.mark.parametrize("name,expr,show", EXPRESSIONS, ids=[c[0] for c in EXPRESSIONS])
@pytest.mark.parametrize("loop", list(LOOPS))
@pytest.mark.parametrize("jump,kw,seen", jump_cases(), ids=["break", "continue"])
def test_jump_inside_expression(
    name: str, expr: str, show: Callable[[int], str], loop: str, jump: str, kw: str, seen: list[int]
) -> None:
    filled = expr.replace("@V", VALUE % kw).replace("@X", kw)
    body = f'for o in 0..2 {{ {LOOPS[loop]} let v = {filled}; print(v); }} print("end"); }}'
    assert main(body) == ("".join(show(i) + "\n" for i in seen) + "end\n") * 2


ASSIGNMENTS = [
    ("name", "let mut s = 0;", "s = @V;", "s", 7, 7),
    ("name_compound", "let mut s = 0;", "s += @V;", "s", 14, 28),
    ("field", "let mut p = P { x: 0, y: 0 };", "p.x = @V;", "p.x", 7, 7),
    ("field_compound", "let mut p = P { x: 0, y: 0 };", "p.x += @V;", "p.x", 14, 28),
    ("index", "let mut xs = [0];", "xs[0] = @V;", "xs[0]", 7, 7),
    ("index_compound", "let mut xs = [0];", "xs[0] += @V;", "xs[0]", 14, 28),
    ("index_key", "let mut xs = [9];", "xs[@V - 7] = i;", "xs[0]", 0, 2),
]


@pytest.mark.parametrize("name,setup,stmt,result,after_break,after_continue", ASSIGNMENTS, ids=[a[0] for a in ASSIGNMENTS])
@pytest.mark.parametrize("loop", list(LOOPS))
@pytest.mark.parametrize("kw", ["break", "continue"])
def test_jump_inside_assignment(
    name: str, setup: str, stmt: str, result: str, after_break: int, after_continue: int, loop: str, kw: str
) -> None:
    filled = stmt.replace("@V", VALUE % kw)
    body = f"{setup} for o in 0..2 {{ {LOOPS[loop]} {filled} }} }} print({result});"
    assert main(body) == f"{after_break if kw == 'break' else after_continue}\n"


# ---- nested loops: a jump unwinds to its own loop's depth, no further
def test_loop_inside_expression_keeps_the_outer_temporary() -> None:
    src = "for o in 0..2 { print(o + { for j in 0..3 { if j == 1 { break; } print(j); } 5 }); }"
    assert main(src) == "0\n5\n0\n6\n"


def test_jump_in_loop_nested_in_call_argument() -> None:
    src = """
    for o in 0..2 {
        print(add(o, { let mut s = 0; for j in 0..3 { s += 1 + if j == 1 { break } else { j }; } s }));
    }
    """
    assert main(src) == "1\n2\n"


def test_break_leaves_only_the_innermost_loop() -> None:
    src = """
    for a in 0..2 {
        for b in 0..2 {
            for c in 0..3 { print(add(a * 100 + b * 10, if c == 1 { break } else { c })); }
        }
        print("a{a}");
    }
    """
    assert main(src) == "0\n10\na0\n100\n110\na1\n"


def test_while_nested_in_for_with_jump_in_expression() -> None:
    src = """
    for a in 0..2 {
        let mut n = 0;
        while true { n += 1; print(add(a, if n == 2 { break } else { n })); }
        print("a{a}");
    }
    """
    assert main(src) == "1\na0\n2\na1\n"


def test_jump_in_for_iterable_targets_the_enclosing_loop() -> None:
    src = """
    for i in 0..3 {
        for j in [1, if i == 1 { continue } else { 2 }] { print(add(i, j)); }
        print("i{i}");
    }
    """
    assert main(src) == "1\n2\ni0\n3\n4\ni2\n"


def test_break_in_while_condition_targets_the_enclosing_loop() -> None:
    src = 'for i in 0..2 { while if i == 0 { break } else { false } { } print(i); } print("end");'
    assert main(src) == "end\n"


def test_continue_in_while_condition_targets_the_enclosing_loop() -> None:
    src = "for i in 0..2 { while 0 < if i == 0 { continue } else { 0 } { } print(i); }"
    assert main(src) == "1\n"


@pytest.mark.parametrize("kw", ["break", "continue"])
def test_jump_in_while_condition_outside_any_loop_is_rejected(kw: str) -> None:
    assert error_codes(f"fn main() {{ while 1 < if true {{ {kw} }} else {{ 2 }} {{ }} }}") == ["outside-loop"]


# ---- closures
def test_jump_inside_a_closure_body_uses_the_closures_own_stack() -> None:
    src = """
    for o in 0..2 {
        let r = [1].map(|x| { let mut s = 0; for j in 0..3 { s += x + if j == 1 { continue } else { j }; } s });
        print(add(o, r[0]));
    }
    """
    assert main(src) == "4\n5\n"


def test_jump_after_a_closure_call_in_the_same_expression() -> None:
    src = """
    for o in 0..2 {
        let f = |a: Int| a + o;
        for i in 0..3 { print(add(f(i), if i == 1 { continue } else { 7 })); }
    }
    """
    assert main(src) == "7\n9\n8\n10\n"


def test_jump_keeps_shared_captured_state() -> None:
    src = """
    let mut total = 0;
    let bump = |n: Int| { total += n; total };
    for o in 0..2 {
        for i in 0..3 { print(add(bump(1), if i == 1 { continue } else { 0 })); }
    }
    print(total);
    """
    assert main(src) == "1\n3\n4\n6\n6\n"


def test_break_in_a_closure_is_still_rejected() -> None:
    assert error_codes("fn main() { for i in 0..1 { let f = || add(1, break); f(); } }\n" + PRELUDE) == ["outside-loop"]


# ---- `?`
TRY_FN = """
fn scan(o: Option[Int]) -> Option[Int] {
    for k in 0..2 {
        for i in 0..3 { print(add(i, if i == 1 { continue } else { o? })); }
    }
    Some(1)
}
"""


def test_continue_next_to_try_that_succeeds() -> None:
    assert run(TRY_FN + "fn main() { print(scan(Some(5))); }") == "5\n7\n5\n7\nSome(1)\n"


def test_try_returning_with_temporaries_after_a_jump_elsewhere() -> None:
    src = TRY_FN + 'fn main() { for t in 0..2 { print(add(t, match scan(None) { Some(_) => 1, None => 2 })); } print("end"); }'
    assert run(src) == "2\n3\nend\n"


def test_jump_and_try_in_the_same_operand_list() -> None:
    src = """
    fn f(o: Option[Int]) -> Option[Int] {
        for k in 0..2 {
            for i in 0..3 { print([i, o?, if i == 1 { break } else { 7 }]); }
            print("k{k}");
        }
        Some(0)
    }
    fn main() { print(f(Some(5))); print(f(None)); }
    """
    assert run(src) == "[0, 5, 7]\nk0\n[0, 5, 7]\nk1\nSome(0)\nNone\n"


# ---- operators whose operand has type Never: the operator is unreachable but must still compile
NEVER_FORMS = {
    "break": "break",
    "continue": "continue",
    "block": "{ break }",
    "if_both": "(if i == 0 { break } else { continue })",
    "match": "match i { _ => break }",
}
BINARY_OPERANDS = {
    **{op: "1" for op in ("+", "-", "*", "/", "%", "==", "!=", "<", "<=", ">", ">=")},
    "&&": "true",
    "||": "false",
}
NEVER_INITS = [
    (f"{form}_{op}_{side}", init)
    for form, never in NEVER_FORMS.items()
    for op, other in BINARY_OPERANDS.items()
    for side, init in (
        ("left", f"{never} {op} {other}"),
        ("right", f"{other} {op} {never}"),
        ("both", f"{never} {op} {never}"),
    )
] + [
    (f"{form}_{op}_unary", f"{op}{never}") for form, never in NEVER_FORMS.items() for op in ("-", "!")
] + [
    ("float_operand", "break * 2.0"),
    ("str_operand", 'break + "s"'),
]


@pytest.mark.parametrize("init", [i for _, i in NEVER_INITS], ids=[n for n, _ in NEVER_INITS])
def test_operator_with_a_never_operand(init: str) -> None:
    assert main(f'for i in 0..2 {{ let x = {init}; }} print("end");') == "end\n"


NEVER_COMPOUNDS = [
    (f"{form}_{op}_{side}", stmts)
    for form, never in NEVER_FORMS.items()
    for op in ("+=", "-=", "*=", "/=", "%=")
    for side, stmts in (
        ("target", f"let mut x = {never}; x {op} 1;"),
        ("value", f"let mut x = 1; x {op} {never};"),
        ("both", f"let mut x = {never}; x {op} {never};"),
    )
]


@pytest.mark.parametrize("stmts", [s for _, s in NEVER_COMPOUNDS], ids=[n for n, _ in NEVER_COMPOUNDS])
def test_compound_assignment_with_a_never_operand(stmts: str) -> None:
    assert main(f'for i in 0..2 {{ {stmts} }} print("end");') == "end\n"


def test_never_left_operand_skips_the_right_operand() -> None:
    src = 'fn noisy() -> Int { print("ran"); 1 }\nfn main() { for i in 0..2 { let x = break + noisy(); } print("end"); }'
    assert run(src) == "end\n"


def test_never_right_operand_still_runs_the_left_operand() -> None:
    src = 'fn noisy() -> Int { print("ran"); 1 }\nfn main() { for i in 0..2 { let x = noisy() + break; } print("end"); }'
    assert run(src) == "ran\nend\n"


def test_panic_as_left_operand_panics() -> None:
    with pytest.raises(VexRuntimeError, match="boom"):
        main('let x = panic("boom") + 1;')
