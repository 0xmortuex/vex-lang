"""Build the Mort order-pricing demo once, then run it against every Vex rule script.

    uv run python examples/embed/run.py

Needs a Mort checkout (``MORT_ROOT``, or a ``Mort`` directory beside a parent of this repository), ``uv`` on
PATH to supply ``ziglang``, and CPython's import library for the interpreter running this script.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
VEX_MX = ROOT / "embed" / "mort" / "vex.mx"
DEMO_SOURCE = HERE / "pricing.mx"
RULES = [
    "rules/1_standard.vx",
    "rules/2_holiday.vx",
    "rules/3_overflow.vx",
    "rules/4_runaway.vx",
    "rules/4_forbidden.vx",
]


class SetupError(Exception):
    """The toolchain this demo needs is not available."""


def find_mort() -> Path:
    env = os.environ.get("MORT_ROOT")
    if env:
        path = Path(env)
        if not (path / "mortc.py").is_file():
            raise SetupError(f"MORT_ROOT={env} does not contain mortc.py")
        return path
    for parent in ROOT.parents:
        candidate = parent / "Mort"
        if (candidate / "mortc.py").is_file():
            return candidate
    raise SetupError("no Mort checkout found: set MORT_ROOT to the directory containing mortc.py")


def python_library() -> Path:
    if os.name != "nt":
        raise SetupError("this demo's link step has only been written and tested on Windows")
    library = Path(sys.base_prefix) / "libs" / f"python{sys.version_info.major}{sys.version_info.minor}.lib"
    if not library.is_file():
        raise SetupError(f"CPython import library not found: {library}")
    return library


def toolchain_problem() -> str | None:
    """Why the demo cannot be built here, or None."""
    try:
        find_mort()
        python_library()
    except SetupError as e:
        return str(e)
    if shutil.which("uv") is None:
        return "uv is not on PATH; it supplies the ziglang C backend"
    return None


def build(source: Path, directory: Path) -> Path:
    """Compile a Mort host once. Raises SetupError if the toolchain fails."""
    exe = directory / (source.stem + ".exe")
    command = [
        "uv", "run", "--no-project", "--with", "ziglang", "--with", "cryptography",
        "python", str(find_mort() / "mortc.py"), str(source),
        "--package", f"vex={VEX_MX}", "--link", str(python_library()), "-o", str(exe),
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True, text=True, cwd=directory)
    if result.returncode != 0:
        raise SetupError(f"mortc failed:\n{result.stdout}{result.stderr}")
    return exe


def host_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONHOME"] = sys.base_prefix
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PATH"] = sys.base_prefix + os.pathsep + env.get("PATH", "")
    return env


def run_demo(exe: Path) -> str:
    env = host_env()
    env["VEX_DEMO_RULES"] = ";".join(RULES)
    result = subprocess.run([str(exe)], env=env, cwd=HERE, capture_output=True, encoding="utf-8", timeout=120)
    if result.returncode != 0:
        raise SetupError(f"the demo exited with {result.returncode}:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def main() -> int:
    problem = toolchain_problem()
    if problem is not None:
        print(f"cannot run the demo: {problem}", file=sys.stderr)
        return 1
    directory = HERE / "build"
    directory.mkdir(exist_ok=True)
    exe = build(DEMO_SOURCE, directory)
    before = hashlib.sha256(exe.read_bytes()).hexdigest()
    print(f"built {exe.name} once: sha256 {before[:16]}")
    print(run_demo(exe), end="")
    after = hashlib.sha256(exe.read_bytes()).hexdigest()
    print(f"same binary ran all {len(RULES)} rule files (sha256 {after[:16]}), no rebuild")
    return 0 if before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
