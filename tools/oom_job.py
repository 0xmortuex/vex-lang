"""Measure what `vex` does when memory really runs out (Windows only; not part of the test suite).

Each case runs the CLI in a Job Object with a process memory limit, so allocations genuinely fail, which an
in-process test cannot arrange. The limits are small, so this is safe on a machine short of memory.

    uv run python tools/oom_job.py

The tests in tests/test_hostile_runtime.py inject MemoryError; this script checks the same paths for real.
"""

from __future__ import annotations

import ctypes
import pathlib
import subprocess
import sys
import tempfile
from ctypes import wintypes
from dataclasses import dataclass


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time", ctypes.c_longlong),
        ("per_job_user_time", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io_counters", ctypes.c_ulonglong * 6),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
MB = 1024 * 1024


def launch_limited(args: list[str], stdin: bytes, megabytes: int) -> subprocess.CompletedProcess[bytes]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.basic.limit_flags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
    limits.process_memory_limit = megabytes * MB
    if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined,unused-ignore]
    child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not kernel32.AssignProcessToJobObject(job, int(child._handle)):  # type: ignore[attr-defined,unused-ignore]
        child.kill()
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined,unused-ignore]
    out, err = child.communicate(stdin, timeout=300)
    return subprocess.CompletedProcess(args, child.returncode, out, err)


@dataclass
class Case:
    name: str
    megabytes: int
    source: str = ""  # run as a file when set
    repl_input: str = ""  # otherwise fed to the REPL
    exit_code: int = 0
    expected: bytes = b""
    also_ok: tuple[tuple[int, bytes], ...] = ()  # other (exit code, output) results; these limits land on several paths


FIVE_MB_LIST = "[" + "1," * 2_500_000 + "1]"
CASES = [
    Case("runtime: linked list", 240, exit_code=2, expected=b"out of memory",
         source="enum L { Nil, Cons(Int, L) }\nfn main() { let mut l = Nil; let mut i = 0; while true { l = Cons(i, l); i += 1; } }\n"),
    Case("runtime: list push", 240, exit_code=2, expected=b"out of memory",
         source="fn main() { let mut xs = [0]; while true { xs.push(xs.len()); } }\n"),
    Case("runtime: map insert", 240, exit_code=2, expected=b"out of memory",
         source="fn main() { let mut m = #{0: 0}; let mut i = 0; while true { m.insert(i, i); i += 1; } }\n"),
    Case("runtime: deep recursion", 240, exit_code=2, expected=b"out of memory",
         source="fn f(n: Int, s: Str) -> Int { f(n + 1, s + s.slice(0, 1)) + 1 }\nfn main() { print(f(0, \"x\".repeat(4000))); }\n"),
    Case("compile: one 5 MB line", 210, exit_code=1, expected=b"out-of-memory",
         source=f"fn main() {{ print({FIVE_MB_LIST}); }}\n"),
    Case("compile: one 5 MB line, 150 MB", 150, exit_code=1, expected=b"out-of-memory", also_ok=((1, b"too large to read"),),
         source=f"fn main() {{ print({FIVE_MB_LIST}); }}\n"),
    Case("repl: keeps the session", 240, repl_input="let mut xs = [0];\nwhile true { xs.push(xs.len()); }\n1 + 1\n", expected=b"2 : Int"),
    Case("repl: one 5 MB line, 150 MB", 150, repl_input=f"{FIVE_MB_LIST}\n1 + 1\n", expected=b"2 : Int", also_ok=((2, b"out of memory"),)),
    Case("repl: one 5 MB line, 190 MB", 190, repl_input=f"{FIVE_MB_LIST}\n1 + 1\n", expected=b"2 : Int", also_ok=((2, b"out of memory"),)),
    Case("repl: one 5 MB line, 210 MB", 210, repl_input=f"{FIVE_MB_LIST}\n1 + 1\n", expected=b"2 : Int", also_ok=((2, b"out of memory"),)),
]  # fmt: skip


def main() -> int:
    if sys.platform != "win32":
        print("Windows only: this uses a Job Object")
        return 0
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for case in CASES:
            if case.source:
                path = pathlib.Path(tmp) / "case.vx"
                path.write_text(case.source, encoding="utf-8")
                args = [sys.executable, "-m", "vex", "run", str(path)]
            else:
                args = [sys.executable, "-m", "vex", "repl"]
            done = launch_limited(args, case.repl_input.encode("utf-8"), case.megabytes)
            output = done.stdout + done.stderr
            passed = any(done.returncode == code and text in output for code, text in ((case.exit_code, case.expected), *case.also_ok))
            failures += not passed
            print(f"{'ok  ' if passed else 'FAIL'} {case.name}: exit {done.returncode}, want {case.exit_code}; {done.stderr[-160:]!r}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
