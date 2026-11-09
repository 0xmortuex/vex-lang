"""The benchmark suite's deterministic guard: right answers and a bounded amount of counted work.

The time ratio against CPython lives in `benchmarks/suite/run_suite.py --guard`, run as its own CI step;
nothing here depends on how fast the machine is.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import Any

import pytest

PATH = pathlib.Path(__file__).resolve().parent.parent / "benchmarks" / "suite" / "run_suite.py"
SPEC = importlib.util.spec_from_file_location("vex_benchmark_suite", PATH)
assert SPEC is not None and SPEC.loader is not None
suite: Any = importlib.util.module_from_spec(SPEC)
sys.modules["vex_benchmark_suite"] = suite
SPEC.loader.exec_module(suite)

NAMES = [bench.name for bench in suite.BENCHMARKS]
BY_NAME = {bench.name: bench for bench in suite.BENCHMARKS}


@pytest.mark.parametrize("name", NAMES)
def test_vex_gets_the_right_answer_within_its_work_budget(name: str) -> None:
    bench = BY_NAME[name]
    _, output, fuel = suite.run_vex(name, bench.quick)
    suite.check_output(name, "vex", bench.quick, output, bench.quick_expected)
    assert 0 < fuel <= bench.fuel_limit, f"{name} used {fuel} fuel, limit {bench.fuel_limit}"


@pytest.mark.parametrize("name", NAMES)
def test_the_cpython_reference_gets_the_same_answer(name: str) -> None:
    bench = BY_NAME[name]
    _, output = suite.run_cpython(name, bench.quick)
    suite.check_output(name, "cpython", bench.quick, output, bench.quick_expected)


def test_published_constants_pin_the_float_benchmarks() -> None:
    # Benchmarks Game reference output: -0.169075164 / -0.169087605 after 1000 steps, 1.274219991 for n=100.
    assert BY_NAME["nbody"].quick == 1000
    assert BY_NAME["nbody"].quick_expected == "-169075163 -169087605"
    assert BY_NAME["spectral"].full == 100
    assert BY_NAME["spectral"].full_expected == "1274219991"


def test_a_wrong_result_fails_the_guard_even_when_fast() -> None:
    bench = BY_NAME["fib"]
    reason = suite.verdict(bench, 0.001, 1.0, "12345", bench.quick_expected)
    assert reason is not None and "expected" in reason


def test_a_wrong_reference_result_fails_the_guard() -> None:
    bench = BY_NAME["fib"]
    assert suite.verdict(bench, 0.1, 1.0, bench.quick_expected, "1") is not None


def test_a_ratio_over_the_limit_fails_the_guard() -> None:
    bench = BY_NAME["fib"]
    reason = suite.verdict(bench, bench.max_ratio + 1.0, 1.0, bench.quick_expected, bench.quick_expected)
    assert reason is not None and "limit" in reason


def test_a_right_result_within_the_limit_passes() -> None:
    bench = BY_NAME["fib"]
    assert suite.verdict(bench, 1.0, 1.0, bench.quick_expected, bench.quick_expected) is None
