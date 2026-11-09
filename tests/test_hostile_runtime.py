"""Hostile arguments at runtime: a located Vex error, an `Err`, or a defined value. Never a Python exception."""

from __future__ import annotations

import io
import os
import random
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from helpers import run_body, run_source

from vex import api, cli, repl, runtime, stdlib
from vex import vm as vm_module
from vex.api import compile_text
from vex.checker import Checker
from vex.diagnostics import Source, VexError, VexRuntimeError, out_of_memory_error
from vex.vm import VM

MIN = "(0 - 9223372036854775807 - 1)"
MAX = "9223372036854775807"


@pytest.fixture(autouse=True)
def default_recursion_limit() -> Iterator[None]:
    """cli.main raises the limit and thread stack for the process; these tests depend on the ordinary ones."""
    limit, stack = sys.getrecursionlimit(), threading.stack_size()
    sys.setrecursionlimit(1000)
    yield
    sys.setrecursionlimit(limit)
    threading.stack_size(stack)


def panics(body: str, fragment: str, prelude: str = "") -> VexRuntimeError:
    """``body`` is one line of ``main``; the program must stop with a Vex error located on that line."""
    with pytest.raises(VexRuntimeError) as info:
        run_source(prelude + "fn main() {\n" + body + "\n}\n")
    error = info.value
    assert fragment in error.message
    assert error.trace, "no stack trace"
    span = error.trace[0][1]
    assert span is not None, "no source location"
    assert span.line == prelude.count("\n") + 2
    assert "-->" in error.render()
    return error


# ---------------------------------------------------------------- the 64-bit contract
@pytest.mark.parametrize(
    "call", [f"math.gcd({MIN}, 0)", f"math.gcd(0, {MIN})", f"math.gcd({MIN}, {MIN})"], ids=["left", "right", "both"]
)
def test_gcd_of_int_min_overflows(call: str) -> None:
    panics(f"print({call});", "integer overflow in `gcd`")


@pytest.mark.parametrize(
    ("call", "expected"),
    [("math.gcd(-4, 6)", "2"), (f"math.gcd({MIN}, 2)", "2"), (f"math.gcd({MAX}, {MAX})", MAX), ("math.gcd(0, 0)", "0")],
)
def test_gcd_in_range_is_unchanged(call: str, expected: str) -> None:
    assert run_body(f"print({call});") == expected + "\n"


@pytest.mark.parametrize("rng", [f"{MIN}..{MAX}", f"0..={MAX}", f"{MIN}..=0", f"{MIN}..={MAX}"])
def test_range_length_outside_64_bits_overflows(rng: str) -> None:
    panics(f"print(({rng}).len());", "integer overflow in `len`")


@pytest.mark.parametrize(
    ("rng", "expected"),
    [(f"0..{MAX}", MAX), (f"{MIN}..-1", MAX), ("5..1", "0"), ("3..3", "0"), ("0..=0", "1"), ("-2..=2", "5")],
)
def test_range_length_in_range(rng: str, expected: str) -> None:
    assert run_body(f"print(({rng}).len());") == expected + "\n"


def test_displayed_range_stays_inside_64_bits() -> None:
    assert run_body(f"print(0..={MAX});") == f"0..={MAX}\n"
    assert run_body(f'print("{{0..={MAX}}}");') == f"0..={MAX}\n"


# ---------------------------------------------------------------- Str holds Unicode scalar values only
@pytest.mark.parametrize("code", [0xD800, 0xDBFF, 0xDC00, 0xDFFF, 0x110000, -1], ids=hex)
def test_to_char_rejects_what_is_not_a_scalar_value(code: int) -> None:
    panics(f"print(({code}).to_char());", "not a valid code point")


@pytest.mark.parametrize("code", [0xD7FF, 0xE000, 0x10FFFF], ids=hex)
def test_to_char_accepts_the_neighbouring_scalars(code: int) -> None:
    assert run_body(f"print({code}.to_char());") == chr(code) + "\n"


def run_with_args(body: str, args: list[str]) -> str:
    prog = compile_text("fn main() {\n" + body + "\n}\n", "<t>")
    out = io.StringIO()
    assert prog.main is not None
    VM(out, args).run_function(prog.main)
    return out.getvalue()


def test_undecodable_program_argument_is_a_located_error() -> None:
    with pytest.raises(VexRuntimeError) as info:
        run_with_args("print(env.args());", ["ok", "\udc80"])
    assert "program argument 1 is not valid Unicode" in info.value.message
    assert info.value.trace[0][1] is not None


def test_valid_program_arguments_are_unchanged() -> None:
    assert run_with_args("print(env.args());", ["a", "é"]) == '["a", "é"]\n'


def test_undecodable_environment_variable_is_a_located_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", {"VEX_BAD": "\udc80", "VEX_FINE": "é"})
    assert run_body('print(env.get("VEX_FINE"));') == 'Some("é")\n'
    assert run_body('print(env.get("VEX_MISSING"));') == "None\n"
    panics('print(env.get("VEX_BAD"));', "environment variable VEX_BAD is not valid Unicode")


def test_cli_diagnostics_stream_cannot_be_broken_by_an_undecodable_name() -> None:
    script = "import sys; from vex.cli import _configure_streams; _configure_streams(); print(chr(0xDC80), file=sys.stderr)"
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert done.stderr.strip() == b"\\udc80"


# ---------------------------------------------------------------- numeric and text conversions
INF = "(1e308 * 10.0)"


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        (f"{INF} % 2.0", "NaN"),
        (f"(0.0 - {INF}) % 2.0", "NaN"),
        (f"{INF} % {INF}", "NaN"),
        (f"1.5 % {INF}", "1.5"),
        (f"({INF} - {INF}) % 2.0", "NaN"),
        ("7.5 % 2.0", "1.5"),
        ("(0.0 - 7.5) % 2.0", "-1.5"),
    ],
)
def test_float_remainder_follows_ieee(expr: str, expected: str) -> None:
    assert run_body(f"print({expr});") == expected + "\n"


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ("[1e308, 1e308]", "inf"),
        (f"[{INF}, 0.0 - {INF}]", "NaN"),
        ("[1e308, 1e308, 0.0 - 1e308]", "inf"),
        (f"[{INF}, 1.0]", "inf"),
        ("[0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]", "1.0"),
    ],
)
def test_sum_float_follows_ieee(items: str, expected: str) -> None:
    assert run_body(f"print({items}.sum_float());") == expected + "\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"9".repeat(5000)', "None"),
        ('"-" + "9".repeat(5000)', "None"),
        ('"0".repeat(5000) + "7"', "Some(7)"),
        ('"-" + "0".repeat(5000) + "7"', "Some(-7)"),
        ('"+" + "0".repeat(5000)', "Some(0)"),
        ('"9223372036854775807"', "Some(9223372036854775807)"),
        ('"9223372036854775808"', "None"),
        ('"-9223372036854775808"', "Some(-9223372036854775808)"),
        ('"-9223372036854775809"', "None"),
        ('" 12 "', "Some(12)"),
        ('"1x"', "None"),
    ],
)
def test_str_to_int_never_reaches_the_bignum_digit_limit(text: str, expected: str) -> None:
    assert run_body(f"print(({text}).to_int());") == expected + "\n"


@pytest.mark.parametrize(
    "text", ['"9".repeat(200000) + "x"', '"9".repeat(200000) + ".5x"', '"." + "9".repeat(200000) + "x"'],
    ids=["digits", "fraction", "leading-dot"],
)  # fmt: skip
def test_str_to_float_rejects_long_input_in_linear_time(text: str) -> None:
    started = time.monotonic()
    assert run_body(f"print(({text}).to_float());") == "None\n"
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [('"1.5"', "Some(1.5)"), ('"1."', "Some(1.0)"), ('".5"', "Some(0.5)"), ('"1e3"', "Some(1000.0)"), ('"1.5e-1"', "Some(0.15)"),
     ('"+2"', "Some(2.0)"), ('"."', "None"), ('"e5"', "None"), ('"1e"', "None"), ('"1.2.3"', "None")],
)  # fmt: skip
def test_str_to_float_grammar_is_unchanged(text: str, expected: str) -> None:
    assert run_body(f"print(({text}).to_float());") == expected + "\n"


# ---------------------------------------------------------------- the file system and the standard streams
NUL_PATHS = ['"a\\0b"', '"\\0"', '"a.txt\\0.exe"']


@pytest.mark.parametrize("path", NUL_PATHS)
def test_read_file_of_an_impossible_path_is_err(path: str) -> None:
    assert run_body(f"print(io.read_file({path}).is_err());") == "true\n"


@pytest.mark.parametrize("path", NUL_PATHS)
def test_write_file_of_an_impossible_path_is_err(path: str) -> None:
    assert run_body(f'print(io.write_file({path}, "x").is_err());') == "true\n"


@pytest.mark.parametrize("path", ['""', '"."', '"a".repeat(70000)'], ids=["empty", "directory", "too-long"])
def test_file_builtins_report_os_errors_as_err(path: str) -> None:
    assert run_body(f'print(io.read_file({path}).is_err()); print(io.write_file({path}, "x").is_err());') == "true\ntrue\n"


def test_err_from_an_impossible_path_names_the_problem() -> None:
    out = run_body('match io.read_file("a\\0b") { Ok(_) => print("ok"), Err(e) => print(e) }')
    assert "null" in out


class Broken:
    """A stream whose every operation fails the way a dead pipe, closed file or strict codec does."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def write(self, text: str) -> int:
        raise self.error

    def readline(self, limit: int = -1) -> str:
        raise self.error


WRITE_FAILURES = [OSError(22, "Invalid argument"), BrokenPipeError(32, "Broken pipe"), ValueError("I/O operation on closed file"),
                  UnicodeEncodeError("ascii", "é", 0, 1, "ordinal not in range")]  # fmt: skip


def run_on(out: Any, body: str) -> None:
    prog = compile_text("fn main() {\n" + body + "\n}\n", "<t>")
    assert prog.main is not None
    VM(out).run_function(prog.main)


@pytest.mark.parametrize("failure", WRITE_FAILURES, ids=lambda e: type(e).__name__)
def test_print_to_a_failing_stream_is_a_located_error(failure: Exception) -> None:
    with pytest.raises(VexRuntimeError) as info:
        run_on(Broken(failure), "print(1);")
    assert "cannot write to standard output" in info.value.message
    assert info.value.trace[0][1] is not None


@pytest.mark.parametrize("failure", WRITE_FAILURES, ids=lambda e: type(e).__name__)
def test_eprint_to_a_failing_stream_is_a_located_error(failure: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", Broken(failure))
    panics("eprint(1);", "cannot write to standard error")


def test_print_without_a_stdout_is_a_located_error(monkeypatch: pytest.MonkeyPatch) -> None:
    prog = compile_text("fn main() {\nprint(1);\n}\n", "<t>")
    assert prog.main is not None
    monkeypatch.setattr(sys, "stdout", None)
    with pytest.raises(VexRuntimeError) as info:
        VM().run_function(prog.main)
    assert "standard output is not available" in info.value.message


def test_eprint_without_a_stderr_is_a_located_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", None)
    panics("eprint(1);", "standard error is not available")


@pytest.mark.parametrize("failure", [OSError(9, "Bad file descriptor"), ValueError("I/O operation on closed file")], ids=["os", "closed"])
def test_read_line_from_a_failing_stream_is_a_located_error(failure: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", Broken(failure))
    panics("print(io.read_line());", "cannot read standard input")


def test_read_line_of_undecodable_bytes_from_a_strict_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", Broken(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")))
    panics("print(io.read_line());", "standard input is not valid UTF-8")


def test_read_line_without_a_stdin_is_the_end_of_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", None)
    assert run_body("print(io.read_line());") == "None\n"


def launch(*args: str, **kwargs: Any) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-m", "vex", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def test_cli_program_writing_to_a_closed_pipe_is_a_located_error(tmp_path: Any) -> None:
    src = tmp_path / "p.vx"
    src.write_text('fn main() {\n  for i in 0..200000 { print("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"); }\n}\n', encoding="utf-8")
    child = launch("run", str(src))
    assert child.stdout is not None and child.stderr is not None
    child.stdout.close()
    err = child.stderr.read()
    assert child.wait(timeout=60) == cli.EXIT_RUNTIME, err
    assert b"cannot write to standard output" in err and b"p.vx:2:" in err
    assert b"internal compiler error" not in err and b"Exception ignored" not in err


def test_cli_output_lost_at_exit_is_reported_once(tmp_path: Any) -> None:
    src = tmp_path / "p.vx"
    src.write_text('fn main() { print("x"); }\n', encoding="utf-8")
    child = launch("run", str(src))
    assert child.stdout is not None and child.stderr is not None
    child.stdout.close()
    err = child.stderr.read()
    assert child.wait(timeout=60) == cli.EXIT_RUNTIME, err
    assert b"internal compiler error" not in err and b"Exception ignored" not in err


def test_cli_repl_writing_to_a_closed_pipe_exits_cleanly() -> None:
    child = launch("repl", stdin=subprocess.PIPE)
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None
    child.stdout.close()
    try:
        child.stdin.write(b"1\n" * 50_000)
        child.stdin.close()
    except OSError:  # the REPL has already stopped reading, which is what this test is about
        pass
    err = child.stderr.read()
    assert child.wait(timeout=60) == cli.EXIT_RUNTIME, err
    assert b"internal compiler error" not in err and b"Exception ignored" not in err


@pytest.mark.parametrize("stream", ["stdin", "stdout"])
def test_cli_repl_without_a_standard_stream_exits_cleanly(stream: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, stream, None)
    assert cli.main(["repl"]) == cli.EXIT_RUNTIME
    assert "needs standard input and output" in capsys.readouterr().err


def test_cli_disasm_of_an_unreadable_file_is_a_diagnostic(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["disasm", "no-such-file.vx"]) == cli.EXIT_COMPILE
    assert "cannot read no-such-file.vx" in capsys.readouterr().err


# ---------------------------------------------------------------- nothing builds a value of unbounded size in one step
@pytest.fixture
def small_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "MAX_STR_LEN", 10)
    monkeypatch.setattr(runtime, "MAX_LIST_LEN", 5)


@pytest.mark.parametrize("rng", [f"0..{MAX}", f"{MIN}..{MAX}", f"0..={MAX}"])
def test_to_list_of_a_huge_range_never_allocates(rng: str) -> None:
    started = time.monotonic()
    panics(f"print(({rng}).to_list());", "list too large")
    assert time.monotonic() - started < 2.0


def test_chars_of_a_long_string_is_refused_before_allocating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "MAX_LIST_LEN", 1000)
    panics('print("x".repeat(1001).chars());', "list too large")
    panics('print("x".repeat(1001).split(""));', "list too large")


def test_a_replacement_that_multiplies_is_refused_before_allocating() -> None:
    started = time.monotonic()
    panics('print("a".repeat(1000000).replace("a", "b".repeat(1000000)));', "string too large")
    assert time.monotonic() - started < 2.0


@pytest.mark.usefixtures("small_limits")
@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("print((0..6).to_list());", "list too large"),
        ("print((0..=5).to_list());", "list too large"),
        ('print("abcdef".chars());', "list too large"),
        ('print("abcdef".split(""));', "list too large"),
        ('print("a,b,c,d,e,f".split(","));', "list too large"),
        ("print([1, 2, 3].concat([4, 5, 6]));", "list too large"),
        ("let mut xs = [1, 2, 3]; xs.extend(xs); print(xs);", "list too large"),
        ('print("a".repeat(11));', "string too large"),
        ('print("aaaa".replace("a", "bbb"));', "string too large"),
        ('print(["aaaa", "bbbb"].join("ccc"));', "string too large"),
        ('let a = "aaaaaa"; print(a + a);', "string too large"),
        ('let a = "aaaaaa"; let mut b = a; b += a; print(b);', "string too large"),
        ('let a = "aaaaaa"; print("{a}{a}");', "string too large"),
        ("print([11, 22, 33, 44, 55]);", "too large to display"),
        ('print("{[11, 22, 33, 44, 55]}");', "too large to display"),
        ("assert_eq([11, 22, 33, 44, 55], [11, 22, 33, 44, 56]);", "too large to display"),
    ],
)
def test_limits_are_enforced_at_the_operation(body: str, fragment: str) -> None:
    panics(body, fragment)


@pytest.mark.usefixtures("small_limits")
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("print((0..5).to_list().len());", "5"),
        ("print((0..=4).len());", "5"),
        ('print("abcde".chars().len());', "5"),
        ('print("a,b,c,d,e".split(",").len());', "5"),
        ("print([1, 2].concat([3, 4, 5]).len());", "5"),
        ('print("ab".repeat(5));', "ababababab"),
        ('print("aaa".replace("a", "bbb"));', "bbbbbbbbb"),
        ('print("aaaaaaaaaaaaaaaaaaaa".len());', "20"),
        ('print("abcdefghijkl".replace("abc", ""));', "defghijkl"),
        ('print(["aaaa", "bbb"].join("ccc"));', "aaaacccbbb"),
        ('let a = "aaaaa"; print(a + a);', "aaaaaaaaaa"),
        ('let a = "aaaaa"; print("{a}{a}");', "aaaaaaaaaa"),
    ],
)
def test_limits_allow_exactly_the_limit(body: str, expected: str) -> None:
    assert run_body(body) == expected + "\n"


def test_exponential_sharing_stops_at_the_display_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "MAX_STR_LEN", 1000)
    prelude = "struct N { kids: List[N] }\n"
    started = time.monotonic()
    panics("let mut c = N { kids: [] }; for i in 0..60 { c = N { kids: [c, c] }; } print(c);", "too large to display", prelude)
    assert time.monotonic() - started < 2.0


def test_display_limit_is_charged_for_contents_not_nesting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "MAX_STR_LEN", 100)
    deep = "[" * 40 + "1" + "]" * 40
    assert run_body(f"print({deep});") == deep + "\n"


@pytest.mark.usefixtures("small_limits")
def test_read_file_over_the_limit_is_err(tmp_path: Any) -> None:
    fits, over = tmp_path / "fits.txt", tmp_path / "over.txt"
    fits.write_text("x" * 10, encoding="utf-8")
    over.write_text("x" * 11, encoding="utf-8")
    assert run_body(f'print(io.read_file("{fits.as_posix()}").unwrap().len());') == "10\n"
    assert run_body(f'match io.read_file("{over.as_posix()}") {{ Ok(_) => print("ok"), Err(e) => print(e) }}').endswith("file too large\n")


@pytest.mark.usefixtures("small_limits")
def test_read_line_over_the_limit_is_a_located_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * 11 + "\n"))
    panics("print(io.read_line());", "input line too long")
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * 9 + "\n"))
    assert run_body("print(io.read_line().unwrap().len());") == "9\n"


# ---------------------------------------------------------------- exhausting the Python stack or the heap
DEEP = "struct N { kids: List[N] }\nfn deep(n: Int) -> N { let mut c = N { kids: [] }; for i in 0..n { c = N { kids: [c] }; } c }\n"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("assert_eq(deep(5000), deep(5001));", "assertion failed"),
        ("let mut n = N { kids: [] }; n.kids.push(n); print(n);", "contains itself"),
        ('let mut n = N { kids: [] }; n.kids.push(n); print("{n}");', "contains itself"),
        ("let mut a = N { kids: [] }; let mut b = N { kids: [] }; a.kids.push(b); b.kids.push(a); print(a == b);", "contain themselves"),
    ],
    ids=["assert_eq", "print-cycle", "interpolate-cycle", "equal-cycle"],
)
def test_values_that_cannot_be_handled_are_located_errors(body: str, fragment: str) -> None:
    panics(body, fragment, DEEP)


def test_deep_values_print_and_compare_without_using_the_python_stack() -> None:
    assert run_source(DEEP + "fn main() {\nprint(deep(5000));\n}\n").count("N { kids:") == 5001
    assert run_source(DEEP + "fn main() {\nprint(deep(5000) == deep(5000));\n}\n") == "true\n"
    assert run_source(DEEP + 'fn main() {\nprint("{deep(5000)}".len());\n}\n') == f"{len(_deep_text(5000))}\n"


def _deep_text(depth: int) -> str:
    return "N { kids: [" * depth + "N { kids: [] }" + "] }" * depth


def fail_with(error: type[BaseException]) -> Callable[..., object]:
    def go(*args: object, **kwargs: object) -> object:
        raise error

    return go


def test_python_recursion_in_a_builtin_is_a_located_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdlib.METHODS["Str"]["upper"], "impl", fail_with(RecursionError))
    panics('print("a".upper());', "recursion too deep")


def test_python_recursion_while_echoing_is_a_located_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vex.vm.display", fail_with(RecursionError))
    out = io.StringIO()
    repl.Session(out).handle("1 + 1")
    assert "runtime error: recursion too deep" in out.getvalue() and "<repl:1>" in out.getvalue()


@pytest.mark.parametrize(
    "callback",
    ["[n].map(|x| r(x + 1))[0]", "[n].fold(0, |a, x| r(x + 1))", "[n].filter(|x| r(x + 1) > 0).len()"],
    ids=["map", "fold", "filter"],
)
def test_recursion_through_builtins_is_a_located_error(callback: str) -> None:
    with pytest.raises(VexRuntimeError) as info:
        run_source(f"fn r(n: Int) -> Int {{ {callback} }}\nfn main() {{\nprint(r(0));\n}}\n")
    assert "recursion too deep" in info.value.message
    assert info.value.trace[0][1] is not None
    assert info.value.trace[-1][1] is not None and info.value.trace[-1][1].line == 3


def out_of_memory(*args: object, **kwargs: object) -> object:
    raise MemoryError


def test_running_out_of_memory_in_a_builtin_is_a_located_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdlib.METHODS["Str"]["upper"], "impl", out_of_memory)
    panics('print("a".upper());', "out of memory")


def test_running_out_of_memory_in_a_callback_is_located_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdlib.METHODS["Str"]["upper"], "impl", out_of_memory)
    with pytest.raises(VexRuntimeError) as info:
        run_source('fn main() {\nprint(["a"].map(|s| s.upper()));\n}\n')
    assert info.value.message == "out of memory"
    assert [name for name, _ in info.value.trace] == ["<lambda>", "main"]


def test_vm_is_reusable_after_running_out_of_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdlib.METHODS["Str"]["upper"], "impl", out_of_memory)
    session = repl.Session(io.StringIO())
    with pytest.raises(VexRuntimeError):
        session.eval('"a".upper()')
    assert session.vm.frames == []
    assert session.eval("1 + 1") == "2 : Int"


def test_running_out_of_memory_while_compiling_is_a_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Checker, "check_all", out_of_memory)
    with pytest.raises(VexError) as info:
        compile_text("fn main() {}", "<t>")
    assert [d.code for d in info.value.diagnostics] == ["out-of-memory"]
    assert info.value.diagnostics[0].span is not None


@pytest.mark.parametrize("stage", ["parse_source", "Checker.check_all"])
def test_running_out_of_memory_while_compiling_in_the_repl_keeps_the_session(stage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.StringIO()
    session = repl.Session(out)
    session.handle("let keep = 41;")
    if stage == "parse_source":
        monkeypatch.setattr(repl, "parse_source", out_of_memory)
    else:
        monkeypatch.setattr(Checker, "check_all", out_of_memory)
    session.handle("keep")
    monkeypatch.undo()
    session.handle("keep + 1")
    assert "error[out-of-memory]" in out.getvalue()
    assert "42 : Int" in out.getvalue()


def test_source_file_too_large_to_read_is_an_os_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    class Huge:
        def __enter__(self) -> Huge:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            raise MemoryError

    monkeypatch.setattr(api, "open", lambda *a, **k: Huge(), raising=False)
    with pytest.raises(OSError, match="too large"):
        api.read_source(str(tmp_path / "x.vx"))


# ---------------------------------------------------------------- what only the REPL can reach
def in_repl(*inputs: str) -> str:
    out = io.StringIO()
    session = repl.Session(out)
    for text in inputs:
        session.handle(text)
    return out.getvalue()


@pytest.mark.parametrize(
    "use",
    ["a", "a + 1", "fn g() -> Int { a }\ng()"],
    ids=["read", "operand", "through-a-function"],
)
def test_a_global_whose_let_panicked_is_a_located_error(use: str) -> None:
    text = in_repl('let a: Int = panic("first");', use, "1 + 1")
    assert "first" in text
    assert "`a` has no value" in text
    assert "<repl:2>" in text
    assert text.rstrip().endswith("2 : Int")


def test_compound_assignment_to_a_global_whose_let_panicked() -> None:
    text = in_repl('let mut a: Int = panic("first");', "a += 1;", "a = 5;", "a")
    assert "`a` has no value" in text
    assert text.rstrip().endswith("5 : Int")


def test_earlier_statements_of_a_failed_input_keep_their_values() -> None:
    text = in_repl('let a = 1; let b: Int = panic("first");', "a", "b")
    assert "1 : Int" in text and "`b` has no value" in text


def test_echoing_a_deep_value_works_and_keeps_the_session() -> None:
    prelude = "struct N { kids: List[N] }"
    build = "let mut c = N { kids: [] };\nfor i in 0..5000 { c = N { kids: [c] }; }\nc"
    text = in_repl(prelude, build, "1 + 1")
    assert text.count("N { kids:") == 5001
    assert text.rstrip().endswith("2 : Int")


def test_echoing_a_value_too_large_to_display_is_a_located_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "MAX_STR_LEN", 10)
    text = in_repl("[1, 2, 3, 4, 5, 6]")
    assert "runtime error: value is too large to display" in text and "<repl:1>" in text


def test_running_out_of_memory_while_echoing_is_a_located_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vex.vm.display", out_of_memory)
    text = in_repl("1 + 1")
    assert "runtime error: out of memory" in text and "<repl:1>" in text


# ---------------------------------------------------------------- Unit is Ord (docs/types.md)
@pytest.mark.parametrize(
    ("expr", "expected"), [("() < ()", "false"), ("() <= ()", "true"), ("() > ()", "false"), ("() >= ()", "true")]
)
def test_unit_ordering_is_defined(expr: str, expected: str) -> None:
    assert run_body(f"print({expr});") == expected + "\n"


@pytest.mark.parametrize(
    ("expr", "expected"), [("lt((), ())", "false"), ("le((), ())", "true"), ("lt(1, 2)", "true"), ('le("b", "a")', "false")]
)
def test_unit_ordering_through_a_generic_function(expr: str, expected: str) -> None:
    prelude = "fn lt[T: Ord](a: T, b: T) -> Bool { a < b }\nfn le[T: Ord](a: T, b: T) -> Bool { a <= b }\n"
    assert run_source(prelude + f"fn main() {{ print({expr}); }}\n") == expected + "\n"


@pytest.mark.parametrize(
    "body", ["let mut x = [(), ()]; x.sort(); print(x);", "print([(), ()].sorted());"], ids=["sort", "sorted"]
)
def test_sorting_units_is_defined(body: str) -> None:
    assert run_body(body) == "[(), ()]\n"


def test_sorting_other_values_is_unchanged() -> None:
    assert run_body("let mut x = [3, 1, 2]; x.sort(); print(x); print([2, 1].sorted());") == "[1, 2, 3]\n[1, 2]\n"


def test_sorted_returns_a_new_list_of_units() -> None:
    assert run_body("let x = [()]; let mut y = x.sorted(); y.push(()); print(x.len());") == "1\n"


# ---------------------------------------------------------------- reporting when memory has really run out
# tools/oom_job.py measures these paths under a real memory limit; here MemoryError is injected.
class Held:
    """A runtime value that a weak reference can watch."""


def test_program_values_are_released_before_the_error_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdlib.METHODS["Str"]["upper"], "impl", out_of_memory)
    prog = compile_text('fn inner(y: Int) { "a".upper(); }\nfn outer(x: Int) { inner(x); }\nfn main() {}\n', "<t>")
    code = prog.env.fns["outer"].code
    assert code is not None
    vm = VM(io.StringIO())
    refs: list[weakref.ref[Held]] = []
    released: list[bool] = []
    real_error = VexRuntimeError

    def held_arguments() -> list[Any]:
        held = Held()
        refs.append(weakref.ref(held))
        return [held]

    def spy(message: str, trace: Any = None) -> VexRuntimeError:
        released.append(refs[0]() is None)
        return real_error(message, trace)

    monkeypatch.setattr(vm_module, "VexRuntimeError", spy)
    with pytest.raises(real_error) as info:
        vm.run_function(code, held_arguments())
    assert released == [True]
    assert [name for name, _ in info.value.trace] == ["inner", "outer"]


@pytest.mark.parametrize("chained", [False, True], ids=["single", "chained"])
def test_compile_values_are_released_before_the_error_is_built(monkeypatch: pytest.MonkeyPatch, chained: bool) -> None:
    # `chained`: unwinding with a full heap cannot allocate traceback entries, and CPython reports that by raising
    # another MemoryError whose __context__ is the first, so the frames are pinned from inside the chain.
    refs: list[weakref.ref[Held]] = []
    released: list[bool] = []
    real_error = out_of_memory_error

    def failing_compile(*args: object) -> None:
        held = Held()
        refs.append(weakref.ref(held))
        try:
            raise MemoryError
        except MemoryError:
            if chained:
                raise MemoryError  # noqa: B904 - the implicit __context__ is what this test is about
            raise

    def spy(source: Source) -> VexError:
        released.append(refs[0]() is None)
        return real_error(source)

    monkeypatch.setattr(api, "_compile", failing_compile)
    monkeypatch.setattr(api, "out_of_memory_error", spy)
    with pytest.raises(VexError) as info:
        api.compile_source(Source("<t>", "fn main() {}\n"))
    assert released == [True]
    assert info.value.diagnostics[0].code == "out-of-memory"


def add_two_code() -> Any:
    prog = compile_text("fn add_two(mut x: Int) -> Int { x += 2; let y = x; y }\nfn main() {}\n", "<t>")
    return prog.env.fns["add_two"].code


def test_run_function_leaves_a_reused_argument_list_alone() -> None:
    code = add_two_code()
    vm = VM(io.StringIO())
    args = [2]
    assert vm.run_function(code, args) == 4
    assert args == [2]
    assert vm.run_function(code, args) == 4
    assert args == [2]


def test_run_function_accepts_a_tuple_of_arguments() -> None:
    code = add_two_code()
    vm = VM(io.StringIO())
    args = (2,)
    assert vm.run_function(code, args) == 4
    assert vm.run_function(code, args) == 4
    assert args == (2,)


def test_running_out_of_memory_does_not_empty_a_list_the_caller_can_still_see(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdlib.METHODS["Str"]["upper"], "impl", out_of_memory)
    prog = compile_text('fn f(x: Int) { "a".upper(); }\nfn main() {}\n', "<t>")
    code = prog.env.fns["f"].code
    assert code is not None
    vm = VM(io.StringIO())
    named = [1]
    in_a_container = {"args": [2]}

    def passed_along(args: list[Any]) -> None:
        vm.run_function(code, args)

    for call in (lambda: vm.run_function(code, named), lambda: vm.run_function(code, in_a_container["args"]),
                 lambda: passed_along(named)):  # fmt: skip
        with pytest.raises(VexRuntimeError):
            call()
    assert named == [1]
    assert in_a_container == {"args": [2]}


def test_out_of_memory_is_reported_even_if_the_trace_cannot_be_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdlib.METHODS["Str"]["upper"], "impl", out_of_memory)
    monkeypatch.setattr(VM, "make_trace", out_of_memory)
    with pytest.raises(VexRuntimeError) as info:
        run_body('"a".upper();')
    assert info.value.message == "out of memory"


def test_the_reserve_is_spent_by_a_failure_and_taken_again(monkeypatch: pytest.MonkeyPatch) -> None:
    session = repl.Session(io.StringIO())
    assert session.vm.reserve is not None
    monkeypatch.setattr(stdlib.METHODS["Str"]["upper"], "impl", out_of_memory)
    with pytest.raises(VexRuntimeError):
        session.eval('"a".upper()')
    assert session.vm.reserve is None
    assert session.eval("1 + 1") == "2 : Int"
    assert session.vm.reserve is not None


def test_a_vm_that_cannot_retake_its_reserve_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    session = repl.Session(io.StringIO())
    session.vm.reserve = None
    monkeypatch.setattr(vm_module, "bytearray", out_of_memory, raising=False)
    assert session.eval("1 + 1") == "2 : Int"
    assert session.vm.reserve is None


def test_global_values_survive_a_failure_in_the_repl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdlib.METHODS["Str"]["upper"], "impl", out_of_memory)
    session = repl.Session(io.StringIO())
    session.eval("let keep = [1, 2];")
    with pytest.raises(VexRuntimeError):
        session.eval('"a".upper()')
    assert session.eval("keep") == "[1, 2] : List[Int]"


def test_out_of_memory_position_does_not_leak_into_the_next_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdlib.METHODS["Str"]["upper"], "impl", out_of_memory)
    session = repl.Session(io.StringIO())
    with pytest.raises(VexRuntimeError) as first:
        session.eval('"a".upper()')
    with pytest.raises(VexRuntimeError) as second:
        session.eval('\n"b".upper()')
    assert first.value.trace[0][1] is not None and first.value.trace[0][1].line == 1
    assert second.value.trace[0][1] is not None and second.value.trace[0][1].line == 2


def spy_on_compile_report(monkeypatch: pytest.MonkeyPatch, target: str) -> list[bool]:
    released: list[bool] = []
    real = out_of_memory_error

    def spy(source: Source) -> VexError:
        current = sys.exc_info()[1]
        assert current is not None
        released.append(current.__traceback__ is None)
        return real(source)

    monkeypatch.setattr(target, spy)
    return released


def test_compile_releases_its_stack_before_reporting_out_of_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Checker, "check_all", out_of_memory)
    released = spy_on_compile_report(monkeypatch, "vex.api.out_of_memory_error")
    with pytest.raises(VexError):
        compile_text("fn main() {}", "<t>")
    assert released == [True]


def test_repl_releases_its_stack_before_reporting_out_of_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Checker, "check_all", out_of_memory)
    released = spy_on_compile_report(monkeypatch, "vex.repl.out_of_memory_error")
    with pytest.raises(VexError):
        repl.Session(io.StringIO()).eval("1")
    assert released == [True]


# ---------------------------------------------------------------- the caps hold for every builtin that builds a List or Str
SIX = "six"
GROWN = "let mut six = [1, 2, 3, 4, 5]; six.push(6); let mut m = #{1: 1, 2: 2, 3: 3, 4: 4, 5: 5}; m.insert(6, 6);"  # past the caps


@pytest.mark.usefixtures("small_limits")
@pytest.mark.parametrize(
    "call",
    [
        f"{SIX}.copy()",
        f"{SIX}.map(|x| x)",
        f"{SIX}.filter(|x| true)",
        f"{SIX}.sorted()",
        f"{SIX}.slice(0, 6)",
        f"{SIX}.enumerate()",
        f"{SIX}.zip({SIX})",
        "m.keys()",
        "m.values()",
        "m.entries()",
        r'"\n\n\n\n\n\n".lines()',
        r'"a\nb\nc\nd\ne\nf".lines()',
        r'"a\r\nb\u{2028}c\u{85}d\u{C}e\u{1C}f".lines()',
    ],
    ids=["copy", "map", "filter", "sorted", "slice", "enumerate", "zip", "keys", "values", "entries",
         "lines-empty", "lines-text", "lines-every-break"],
)  # fmt: skip
def test_every_list_builder_enforces_the_cap(call: str) -> None:
    panics(f"{GROWN} let r = {call};", "list too large")


@pytest.mark.usefixtures("small_limits")
@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("[1, 2, 3, 4, 5].copy().len()", "5"),
        ("[1, 2, 3, 4, 5].filter(|x| x > 3).len()", "2"),
        (f"{SIX}.slice(1, 6).len()", "5"),
        (f"{SIX}.zip([1, 2]).len()", "2"),
        (r'"\n\n\n\n\n".lines().len()', "5"),
        (r'"a\nb\nc\nd\ne\n".lines().len()', "5"),
        (r'"a\nb\nc\nd\ne".lines().len()', "5"),
        (r'"\r\n\r\n\r\n\r\n\r\n\r\n".len()', "12"),
    ],
)
def test_list_cap_allows_exactly_the_limit(call: str, expected: str) -> None:
    assert run_body(f"{GROWN} print({call});") == expected + "\n"


@pytest.mark.usefixtures("small_limits")
def test_windows_line_endings_count_once_against_the_cap() -> None:
    assert run_body(r'print("\r\n\r\n\r\n\r\n\r\n".lines().len());') == "5\n"


def test_counting_lines_stops_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "MAX_LIST_LEN", 1000)
    panics(r'print("\n".repeat(1001).lines().len());', "list too large")
    assert run_body(r'print("\n".repeat(1000).lines().len());') == "1000\n"
    assert run_body(r'print("a".repeat(5000).lines().len());') == "1\n"


@pytest.mark.parametrize("seed", range(200))
def test_line_break_count_agrees_with_splitlines(seed: int) -> None:
    rng = random.Random(f"lines/{seed}")
    text = "".join(rng.choice(["a", "\r\n", "\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " ", "\t", " "])
                   for _ in range(rng.randint(1, 30)))  # fmt: skip
    breaks = sum(1 for _ in stdlib._LINE_BREAK.finditer(text))
    assert (breaks if text[-1] in stdlib._LINE_BREAK_CHARS else breaks + 1) == len(text.splitlines())


@pytest.mark.usefixtures("small_limits")
@pytest.mark.parametrize(
    "call", ['"ßßßßßß".upper()', '"İİİİİİ".lower()'], ids=["upper", "lower"]
)
def test_case_conversion_that_grows_enforces_the_cap(call: str) -> None:
    panics(f"let r = {call};", "string too large")


@pytest.mark.usefixtures("small_limits")
@pytest.mark.parametrize(
    ("call", "expected"), [('"ßßßßß".upper()', "SSSSSSSSSS"), ('"abcdefghij".upper()', "ABCDEFGHIJ"), ('"ABCDEFGHIJ".lower()', "abcdefghij")]
)
def test_case_conversion_allows_exactly_the_limit(call: str, expected: str) -> None:
    assert run_body(f"print({call});") == expected + "\n"


@pytest.mark.usefixtures("small_limits")
def test_program_arguments_over_the_cap_are_a_located_error() -> None:
    with pytest.raises(VexRuntimeError) as info:
        run_with_args("print(env.args().len());", ["a"] * 6)
    assert "list too large" in info.value.message and info.value.trace[0][1] is not None
    assert run_with_args("print(env.args().len());", ["a"] * 5) == "5\n"


@pytest.mark.usefixtures("small_limits")
def test_environment_value_over_the_cap_is_a_located_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", {"VEX_LONG": "x" * 11, "VEX_FITS": "x" * 10})
    panics('print(env.get("VEX_LONG").is_some());', "string too large")
    assert run_body('print(env.get("VEX_FITS").unwrap().len());') == "10\n"


# ---------------------------------------------------------------- literals are held to the same caps
@pytest.mark.parametrize(
    ("literal", "fragment"),
    [("[1, 2, 3, 4]", "list too large"), ("#{1: 1, 2: 2, 3: 3, 4: 4}", "map too large")],
    ids=["list", "map"],
)
def test_a_literal_over_the_cap_is_a_located_error(literal: str, fragment: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "MAX_LIST_LEN", 3)
    panics(f"let r = {literal};", fragment)


@pytest.mark.parametrize(
    ("body", "expected"),
    [("print([1, 2, 3].len());", "3"), ("print(#{1: 1, 2: 2, 3: 3}.len());", "3"), ("print([[1, 2, 3], [4, 5, 6]].len());", "2")],
    ids=["list", "map", "each-literal-counts-alone"],
)
def test_literals_at_the_cap_are_built(body: str, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "MAX_LIST_LEN", 3)
    assert run_body(body) == expected + "\n"
