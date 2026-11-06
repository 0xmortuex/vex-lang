"""Runtime errors: checked integers, bounds, unwraps, and source-mapped stack traces."""

from __future__ import annotations

import pytest
from helpers import run_body, run_source

from vex.diagnostics import VexRuntimeError
from vex.runtime import VexExit

# (id, body, expected message fragment)
CASES = [
    ("add_overflow", "let x = 9223372036854775807; print(x + 1);", "integer overflow in `+`"),
    ("sub_overflow", "let x = -9223372036854775807; print(x - 2);", "integer overflow in `-`"),
    ("mul_overflow", "let x = 4611686018427387904; print(x * 2);", "integer overflow in `*`"),
    ("neg_overflow", "let x = -9223372036854775808; print(-x);", "integer overflow in unary `-`"),
    ("div_overflow", "let x = -9223372036854775808; print(x / -1);", "integer overflow in `/`"),
    ("addk_overflow", "let mut x = 9223372036854775807; x += 1;", "integer overflow in `+`"),
    ("pow_overflow", "print(10.pow(19));", "integer overflow in `pow`"),
    ("pow_negative", "print(2.pow(-1));", "negative exponent"),
    ("abs_overflow", "print((-9223372036854775808).abs());", "integer overflow in `abs`"),
    ("sum_overflow", "print([9223372036854775807, 1].sum());", "integer overflow in `sum`"),
    ("div_zero", "let z = 0; print(1 / z);", "division by zero"),
    ("mod_zero", "let z = 0; print(1 % z);", "division by zero in `%`"),
    ("float_div_zero", "let z = 0.0; print(1.0 / z);", "division by zero"),
    ("list_oob", "let xs = [1, 2]; print(xs[2]);", "index 2 out of bounds for list of length 2"),
    ("list_negative_index", "let xs = [1, 2]; print(xs[-1]);", "index -1 out of bounds"),
    ("list_set_oob", "let mut xs = [1]; xs[5] = 1;", "index 5 out of bounds"),
    ("map_missing_key", 'let m = #{"a": 1}; print(m["b"]);', 'key "b" not found in map'),
    ("unwrap_none", "let o: Option[Int] = None; print(o.unwrap());", "unwrap()` on a `None`"),
    ("unwrap_err", 'let r: Result[Int, Str] = Err("boom"); print(r.unwrap());', 'Err` value: "boom"'),
    ("expect_none", 'let o: Option[Int] = None; print(o.expect("needed a value"));', "needed a value"),
    ("unwrap_err_on_ok", "let r: Result[Int, Str] = Ok(1); print(r.unwrap_err());", "unwrap_err()"),
    ("assert_fails", "assert(1 > 2);", "assertion failed"),
    ("assert_eq_fails", "assert_eq(1, 2);", "left = 1, right = 2"),
    ("panic_msg", 'panic("custom message");', "custom message"),
    ("slice_oob", "print([1, 2].slice(0, 5));", "out of range for list of length 2"),
    ("str_slice_oob", 'print("abc".slice(2, 9));', "out of range for string of length 3"),
    ("remove_at_oob", "let mut xs = [1]; xs.remove_at(3);", "remove_at index 3"),
    ("insert_oob", "let mut xs = [1]; xs.insert(5, 1);", "insert index 5"),
    ("to_int_nan", "print((0.0 / 1.0 - math.sqrt(-1.0)).to_int());", "cannot convert NaN to Int"),
    ("to_char_invalid", "print((-1).to_char());", "not a valid code point"),
    ("empty_replace_pattern", 'print("abc".replace("", "x"));', "must not be empty"),
    ("negative_repeat", 'print("a".repeat(-1));', "negative repeat"),
    ("code_of_empty", 'print("".code());', "empty string"),
]


@pytest.mark.parametrize(("name", "body", "fragment"), CASES, ids=[c[0] for c in CASES])
def test_runtime_error_message(name: str, body: str, fragment: str) -> None:
    with pytest.raises(VexRuntimeError) as info:
        run_body(body)
    assert fragment in info.value.message


def test_stack_trace_names_functions_and_lines() -> None:
    src = """fn inner(n: Int) -> Int {
    10 / n
}
fn middle(n: Int) -> Int {
    inner(n) + 1
}
fn main() {
    print(middle(0));
}
"""
    with pytest.raises(VexRuntimeError) as info:
        run_source(src)
    names = [name for name, _ in info.value.trace]
    assert names == ["inner", "middle", "main"]
    lines = [sp.line for _, sp in info.value.trace if sp is not None]
    assert lines == [2, 5, 8]
    text = info.value.render()
    assert "runtime error: division by zero" in text
    assert "10 / n" in text
    assert "in middle at <test>:5" in text


def test_trace_through_lambda_and_builtin() -> None:
    src = """fn main() {
    let r = [1, 0].map(|x| 4 / x);
    print(r);
}
"""
    with pytest.raises(VexRuntimeError) as info:
        run_source(src)
    assert [n for n, _ in info.value.trace][:2] == ["<lambda>", "main"]


def test_stack_overflow_is_reported_not_crashed() -> None:
    src = "fn f(n: Int) -> Int { f(n + 1) + 1 }\nfn main() { print(f(0)); }\n"
    with pytest.raises(VexRuntimeError) as info:
        run_source(src)
    assert "stack overflow" in info.value.message
    assert "more frames" in info.value.render()


def test_error_state_does_not_leak_between_runs() -> None:
    with pytest.raises(VexRuntimeError):
        run_body("let z = 0; print(1 / z);")
    assert run_body("print(1);") == "1\n"


def test_exit_function_raises_exit() -> None:
    with pytest.raises(VexExit) as info:
        run_body("exit(7);")
    assert info.value.code == 7


def test_output_before_error_is_kept() -> None:
    import io

    from vex.api import compile_text
    from vex.vm import VM

    prog = compile_text('fn main() { print("before"); panic("after"); }')
    out = io.StringIO()
    assert prog.main is not None
    with pytest.raises(VexRuntimeError):
        VM(out).run_function(prog.main)
    assert out.getvalue() == "before\n"


def test_deep_recursion_within_limit_works() -> None:
    src = "fn sum(n: Int) -> Int { if n == 0 { 0 } else { n + sum(n - 1) } }\nfn main() { print(sum(20000)); }\n"
    assert run_source(src) == "200010000\n"


def test_checked_arithmetic_boundaries_are_exact() -> None:
    body = """
print(9223372036854775806 + 1);
print(-9223372036854775807 - 1);
print(4611686018427387903 * 2);
print(3037000499 * 3037000499);
"""
    assert run_body(body) == "9223372036854775807\n-9223372036854775808\n9223372036854775806\n9223372030926249001\n"
