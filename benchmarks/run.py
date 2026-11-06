"""Benchmarks: compile once, then time the VM on fib(27) and tight loops.

Usage:  python benchmarks/run.py            (from the repository root, after `uv sync`)

For context each benchmark is also timed as native CPython code doing the same work, so the
numbers show the real cost of running on a VM that is itself written in Python.
"""

from __future__ import annotations

import io
import pathlib
import platform
import sys
import time

from vex.api import compile_file
from vex.vm import VM

HERE = pathlib.Path(__file__).parent


def native_fib(n: int) -> int:
    return n if n < 2 else native_fib(n - 1) + native_fib(n - 2)


def native_loops() -> tuple[int, int]:
    total = 0
    i = 0
    while i < 1_000_000:
        total += (i * i) % 7
        i += 1
    count = 0
    for a in range(600):
        for b in range(1000):
            if (a + b) % 3 == 0:
                count += 1
    return total, count


def best_of(fn: object, runs: int) -> float:
    import typing

    call = typing.cast("typing.Callable[[], object]", fn)
    best = float("inf")
    for _ in range(runs):
        t0 = time.perf_counter()
        call()
        best = min(best, time.perf_counter() - t0)
    return best


def vex_runner(name: str) -> tuple[object, str]:
    prog = compile_file(str(HERE / name))
    assert prog.main is not None
    main = prog.main
    out = io.StringIO()

    def go() -> object:
        out.truncate(0)
        out.seek(0)
        return VM(out).run_function(main)

    go()
    return go, out.getvalue().strip().replace("\n", " ")


def main() -> int:
    sys.setrecursionlimit(10_000)
    print(f"Python {platform.python_version()} on {platform.system()} {platform.machine()}")
    rows: list[tuple[str, float, float, str]] = []
    sys.setrecursionlimit(10_000)
    go, out = vex_runner("fib.vx")
    rows.append(("fib(27)", best_of(go, 3), best_of(lambda: native_fib(27), 3), out))
    go, out = vex_runner("loops.vx")
    rows.append(("loops (1M while + 600k nested for)", best_of(go, 3), best_of(native_loops, 3), out))
    print(f"{'benchmark':<38}{'Vex VM (s)':>12}{'CPython (s)':>13}{'slowdown':>10}   result")
    for name, vex_s, py_s, out in rows:
        print(f"{name:<38}{vex_s:>12.3f}{py_s:>13.3f}{vex_s / py_s:>9.1f}x   {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
