"""Hostile arguments at runtime: a located Vex error, an `Err`, or a defined value. Never a Python exception."""

from __future__ import annotations

import pytest
from helpers import run_body

from vex.diagnostics import VexRuntimeError

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
