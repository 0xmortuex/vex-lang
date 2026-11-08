"""Every code block in docs/tour.md is compiled and run, and its output is compared with the tour.

Fence tags in the tour:

    vex         a program; the next block must be `output` with exactly what it prints
    vex-error   a program that fails; the next `output` block is exactly its standard error
    vex-file    a module (first line `// name.vx`), written beside the next `vex-import` program
    vex-import  a program that imports the preceding `vex-file` blocks; followed by `output`
    python      a host program using vex.host; followed by `output`
    repl        a session: `vex> ` and `...> ` lines are typed, the others are expected output
    output      the expected output of the block before it

Any other tag fails the suite, so a block cannot be added to the tour without being run.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field

import pytest

ROOT = pathlib.Path(__file__).parent.parent
TOUR = ROOT / "docs" / "tour.md"
FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```$", re.S | re.M)
PROGRAM_TAGS = {"vex", "vex-error", "vex-import", "python"}
PROMPTS = ("vex> ", "...> ")


@dataclass
class Case:
    name: str
    tag: str
    source: str
    expected: str = ""
    files: dict[str, str] = field(default_factory=dict)


def parse() -> tuple[list[Case], list[str]]:
    """The runnable cases in the tour, and everything about its blocks that breaks the rules above."""
    blocks = [(m.group(1).strip(), m.group(2)) for m in FENCE.finditer(TOUR.read_text(encoding="utf-8"))]
    cases: list[Case] = []
    problems: list[str] = []
    pending: dict[str, str] = {}
    i = 0
    while i < len(blocks):
        tag, body = blocks[i]
        name = f"{i}-{tag}"
        i += 1
        if tag == "vex-file":
            first = body.split("\n", 1)[0]
            if not re.fullmatch(r"// \w+\.vx", first):
                problems.append(f"block {name}: must start with `// name.vx`")
            pending[first.removeprefix("// ")] = body
        elif tag == "repl":
            cases.append(Case(name, tag, body))
        elif tag in PROGRAM_TAGS:
            if tag != "python" and "fn main" not in body:
                problems.append(f"block {name}: has no `fn main`, so it could not run")
            if i >= len(blocks) or blocks[i][0] != "output":
                problems.append(f"block {name}: is not followed by an `output` block")
                continue
            files = {}
            if tag == "vex-import":
                files, pending = pending, {}
            cases.append(Case(name, tag, body, blocks[i][1], files))
            i += 1
        else:
            problems.append(f"block {name}: tag `{tag}` is not one the tour test can run")
    if pending:
        problems.append(f"modules never imported: {sorted(pending)}")
    return cases, problems


CASES, PROBLEMS = parse()


def vex(args: list[str], cwd: pathlib.Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "vex", *args],
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        cwd=cwd,
        timeout=60,
    )


def test_every_block_in_the_tour_can_be_run() -> None:
    assert PROBLEMS == []


def test_the_tour_covers_each_kind_of_block() -> None:
    assert {c.tag for c in CASES} == {*PROGRAM_TAGS, "repl"}
    assert len(CASES) >= 24


def fenced_output(res: subprocess.CompletedProcess[str]) -> str:
    return f"exit {res.returncode}\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"


@pytest.mark.parametrize("case", [c for c in CASES if c.tag in ("vex", "vex-import")], ids=lambda c: c.name)
def test_program_prints_what_the_tour_says(case: Case, tmp_path: pathlib.Path) -> None:
    for name, text in case.files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "tour.vx").write_text(case.source, encoding="utf-8")
    res = vex(["run", "tour.vx"], tmp_path)
    assert res.returncode == 0, fenced_output(res)
    assert res.stdout == case.expected
    assert res.stderr == ""


@pytest.mark.parametrize("case", [c for c in CASES if c.tag == "vex-error"], ids=lambda c: c.name)
def test_failing_program_reports_what_the_tour_says(case: Case, tmp_path: pathlib.Path) -> None:
    (tmp_path / "tour.vx").write_text(case.source, encoding="utf-8")
    res = vex(["run", "tour.vx"], tmp_path)
    assert res.returncode == (2 if case.expected.startswith("runtime error") else 1), fenced_output(res)
    assert res.stderr == case.expected
    assert res.stdout == ""


@pytest.mark.parametrize("case", [c for c in CASES if c.tag == "python"], ids=lambda c: c.name)
def test_host_program_prints_what_the_tour_says(case: Case, tmp_path: pathlib.Path) -> None:
    script = tmp_path / "tour_host.py"
    script.write_text(case.source, encoding="utf-8")
    res = subprocess.run(
        [sys.executable, str(script)], capture_output=True, encoding="utf-8", cwd=tmp_path, timeout=60
    )
    assert res.returncode == 0, fenced_output(res)
    assert res.stdout == case.expected
    assert res.stderr == ""


@pytest.mark.parametrize("case", [c for c in CASES if c.tag == "repl"], ids=lambda c: c.name)
def test_repl_session_matches_the_tour(case: Case, tmp_path: pathlib.Path) -> None:
    lines = case.source.splitlines()
    typed = [line[len(PROMPTS[0]) :] for line in lines if line.startswith(PROMPTS)]
    shown = [line for line in lines if not line.startswith(PROMPTS)]
    res = vex(["repl"], tmp_path, stdin="\n".join(typed) + "\n")
    assert res.returncode == 0, fenced_output(res)
    assert res.stdout.splitlines() == shown
