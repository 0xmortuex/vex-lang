"""Hostile arguments at runtime: a located Vex error, an `Err`, or a defined value. Never a Python exception."""

from __future__ import annotations

import io
import subprocess
import sys
import time
from typing import Any

import pytest
from helpers import run_body

from vex import cli, stdlib
from vex.api import compile_text
from vex.diagnostics import VexRuntimeError
from vex.vm import VM

MIN = "(0 - 9223372036854775807 - 1)"
MAX = "9223372036854775807"


def panics(body: str, fragment: str) -> VexRuntimeError:
    """``body`` is one line (line 2 of the program); it must stop with a Vex error located there."""
    with pytest.raises(VexRuntimeError) as info:
        run_body(body)
    error = info.value
    assert fragment in error.message
    assert error.trace, "no stack trace"
    span = error.trace[0][1]
    assert span is not None, "no source location"
    assert span.line == 2
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
    monkeypatch.setattr(stdlib.os, "environ", {"VEX_BAD": "\udc80", "VEX_FINE": "é"})
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
