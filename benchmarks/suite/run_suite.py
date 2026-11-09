"""The Vex benchmark suite and its CI guard. See BENCHMARKS.md.

    uv run python benchmarks/suite/run_suite.py            measure, write benchmarks/results/latest.json
    uv run python benchmarks/suite/run_suite.py --guard    the CI regression guard

Each benchmark is a Vex program and a CPython program doing the same work. A wrong result is always a
failure: a broken optimisation must not be able to look fast.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import dataclasses
import io
import json
import os
import pathlib
import platform
import runpy
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

HERE = pathlib.Path(__file__).resolve().parent
RESULTS = HERE.parent / "results" / "latest.json"

# fuel_limit is about 1.5x the work counted when the guard was written (see BENCHMARKS.md).
# max_ratio is a Vex/CPython time ratio at least 3x above the worst seen on the development machine.
FUEL_BUDGET = 10**15


@dataclasses.dataclass(frozen=True)
class Benchmark:
    name: str
    full: int
    quick: int
    full_expected: str
    quick_expected: str
    fuel_limit: int
    max_ratio: float


BENCHMARKS = [
    Benchmark("fib", 30, 24, "832040", "46368", 450_000, 200.0),
    Benchmark("nbody", 5000, 1000, "-169075163 -169020000", "-169075163 -169087605", 95_000, 350.0),
    Benchmark("spectral", 100, 30, "1274219991", "1274097380", 332_000, 200.0),
    Benchmark("strings", 20000, 5000, "1734390175 1198 401905", "803276372 224 91191", 1_150_000, 150.0),
    Benchmark("hashmap", 100000, 10000, "355582556 56829 21580", "25123396 5679 2166", 195_000, 150.0),
]


class BenchmarkFailure(Exception):
    """A benchmark produced the wrong result or broke its limit."""


def normalise(output: str) -> str:
    return " ".join(output.split())


def check_output(name: str, who: str, size: int, got: str, expected: str) -> None:
    if normalise(got) != expected:
        raise BenchmarkFailure(f"{name}({size}) on {who}: expected {expected!r}, got {normalise(got)!r}")


def source(name: str, extension: str) -> str:
    return str(HERE / f"{name}{extension}")


# ---- whole-process wall time, used for the published numbers ------------------------------------


def _timed_process(argv: list[str], env: dict[str, str]) -> tuple[float, str]:
    start = time.perf_counter()
    done = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
    elapsed = time.perf_counter() - start
    if done.returncode != 0:
        raise BenchmarkFailure(f"{' '.join(argv)} exited {done.returncode}: {done.stderr.strip()}")
    return elapsed, done.stdout


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HERE.parents[1] / "src")
    return env


def vex_process(name: str, size: int) -> tuple[float, str]:
    return _timed_process([sys.executable, "-m", "vex", "run", source(name, ".vx"), str(size)], _child_env())


def cpython_process(name: str, size: int) -> tuple[float, str]:
    return _timed_process([sys.executable, source(name, ".py"), str(size)], _child_env())


def spread(samples: list[float]) -> dict[str, Any]:
    return {
        "median_s": statistics.median(samples),
        "min_s": min(samples),
        "max_s": max(samples),
        "samples_s": samples,
    }


def startup_cost(runs: int) -> dict[str, float]:
    """How long a do-nothing program takes, so the bias from process startup can be read off."""
    with tempfile.TemporaryDirectory() as folder:
        empty = pathlib.Path(folder) / "empty.vx"
        empty.write_text("fn main() {}\n", encoding="utf-8")
        vex = [
            _timed_process([sys.executable, "-m", "vex", "run", str(empty)], _child_env())[0] for _ in range(runs)
        ]
        cpython = [_timed_process([sys.executable, "-c", "pass"], _child_env())[0] for _ in range(runs)]
    return {"vex_median_s": statistics.median(vex), "cpython_median_s": statistics.median(cpython)}


def hardware() -> dict[str, Any]:
    info: dict[str, Any] = {
        "os": platform.platform(),
        "machine": platform.machine(),
        "logical_cpus": os.cpu_count(),
        "cpu": platform.processor(),
    }
    if sys.platform == "win32":
        name = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
            capture_output=True,
            text=True,
            check=True,
        )
        info["cpu"] = name.stdout.strip()

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("load", ctypes.c_ulong),
                ("total", ctypes.c_ulonglong),
                ("rest", ctypes.c_ulonglong * 6),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            raise OSError("GlobalMemoryStatusEx failed")
        info["ram_gb"] = round(status.total / 1024**3, 1)
    else:
        info["ram_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
    return info


def tool_versions() -> dict[str, str]:
    commit = subprocess.run(
        ["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    )
    return {"cpython": platform.python_version(), "vex_commit": commit.stdout.strip()}


def measure(runs: int, note: str) -> dict[str, Any]:
    rows = []
    for bench in BENCHMARKS:
        vex_samples: list[float] = []
        cpython_samples: list[float] = []
        for _ in range(runs):
            # Alternate, so a burst of load on the machine lands on both sides.
            elapsed, out = vex_process(bench.name, bench.full)
            check_output(bench.name, "vex", bench.full, out, bench.full_expected)
            vex_samples.append(elapsed)
            elapsed, out = cpython_process(bench.name, bench.full)
            check_output(bench.name, "cpython", bench.full, out, bench.full_expected)
            cpython_samples.append(elapsed)
        vex = spread(vex_samples)
        cpython = spread(cpython_samples)
        row = {
            "name": bench.name,
            "size": bench.full,
            "result": bench.full_expected,
            "vex": vex,
            "cpython": cpython,
            "ratio_median": vex["median_s"] / cpython["median_s"],
            "ratio_low": vex["min_s"] / cpython["max_s"],
            "ratio_high": vex["max_s"] / cpython["min_s"],
        }
        print(
            f"{bench.name:<10} vex {vex['median_s']:7.3f}s [{vex['min_s']:.3f}-{vex['max_s']:.3f}]  "
            f"cpython {cpython['median_s']:6.3f}s [{cpython['min_s']:.3f}-{cpython['max_s']:.3f}]  "
            f"{row['ratio_median']:6.1f}x  {bench.full_expected}",
            flush=True,
        )
        rows.append(row)
    return {
        "runs": runs,
        "note": note,
        "date": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "hardware": hardware(),
        "tools": tool_versions(),
        "startup": startup_cost(runs),
        "benchmarks": rows,
    }


# ---- the guard: in-process, on the quick sizes ----------------------------------------------------


def run_vex(name: str, size: int) -> tuple[float, str, int]:
    """Run one benchmark on the VM in this process. Returns (seconds, output, fuel used).

    Fuel is the VM's own deterministic count of work: calls, loop back-edges and built-in costs.
    """
    from vex.api import compile_file
    from vex.vm import VM

    program = compile_file(source(name, ".vx"))
    if program.main is None:
        raise BenchmarkFailure(f"{name}.vx has no main")
    out = io.StringIO()
    vm = VM(out, [str(size)])
    vm.fuel = FUEL_BUDGET
    start = time.perf_counter()
    vm.run_function(program.main)
    elapsed = time.perf_counter() - start
    return elapsed, out.getvalue(), int(FUEL_BUDGET - vm.fuel)


def run_cpython(name: str, size: int) -> tuple[float, str]:
    """Run one reference program in this process. Returns (seconds, output)."""
    out = io.StringIO()
    saved = sys.argv
    sys.argv = [source(name, ".py"), str(size)]
    try:
        with contextlib.redirect_stdout(out):
            start = time.perf_counter()
            runpy.run_path(source(name, ".py"), run_name="__benchmark__")
            elapsed = time.perf_counter() - start
    finally:
        sys.argv = saved
    return elapsed, out.getvalue()


def verdict(bench: Benchmark, vex_seconds: float, cpython_seconds: float, vex_out: str, cpython_out: str) -> str | None:
    """The guard's decision for one benchmark: None if it passes, else the reason it fails."""
    for who, out in (("vex", vex_out), ("cpython", cpython_out)):
        try:
            check_output(bench.name, who, bench.quick, out, bench.quick_expected)
        except BenchmarkFailure as failure:
            return str(failure)
    ratio = vex_seconds / cpython_seconds
    if ratio > bench.max_ratio:
        return f"{bench.name}: Vex is {ratio:.1f}x CPython, limit {bench.max_ratio:.0f}x"
    return None


def best(run: Callable[[], tuple[Any, ...]], attempts: int) -> tuple[Any, ...]:
    """The fastest of several attempts: other load on the machine can only make a run slower."""
    results = [run() for _ in range(attempts)]
    return min(results, key=lambda result: result[0])


def guard(attempts: int) -> int:
    sys.setrecursionlimit(10_000)
    failures = []
    for bench in BENCHMARKS:
        vex = best(lambda: run_vex(bench.name, bench.quick), attempts)
        cpython = best(lambda: run_cpython(bench.name, bench.quick), attempts)
        failure = verdict(bench, vex[0], cpython[0], vex[1], cpython[1])
        print(
            f"{bench.name:<10} vex {vex[0]:.4f}s  cpython {cpython[0]:.4f}s  "
            f"ratio {vex[0] / cpython[0]:6.1f}x (limit {bench.max_ratio:.0f}x)  "
            f"result {'ok' if failure is None else 'FAIL'}",
            flush=True,
        )
        if failure is not None:
            failures.append(failure)
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--guard", action="store_true", help="run the CI regression guard")
    parser.add_argument("--runs", type=int, default=5, help="runs per program (default: 5)")
    parser.add_argument("--attempts", type=int, default=3, help="guard: attempts per program (default: 3)")
    parser.add_argument("--note", default="", help="free text recorded with the results, such as machine load")
    args = parser.parse_args()
    if args.guard:
        return guard(args.attempts)
    results = measure(args.runs, args.note)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
