"""Hostile arguments at runtime: a located Vex error, an `Err`, or a defined value. Never a Python exception."""

from __future__ import annotations

import io
import subprocess
import sys

import pytest
from helpers import run_body

from vex import stdlib
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


@pytest.mark.parametrize(("code", "text"), [(0xD7FF, chr(0xD7FF)), (0xE000, chr(0xE000)), (0x10FFFF, chr(0x10FFFF))], ids=hex)
def test_to_char_accepts_the_neighbouring_scalars(code: int, text: str) -> None:
    assert run_body(f"print({code}.to_char());") == text + "\n"


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
