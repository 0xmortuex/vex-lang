"""Every ```vex block in the documentation must compile (and run, if it has a main)."""

from __future__ import annotations

import io
import pathlib
import re

import pytest

from vex.api import compile_text
from vex.vm import VM

ROOT = pathlib.Path(__file__).parent.parent
DOC_FILES = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]
BLOCK = re.compile(r"```vex\n(.*?)```", re.S)


def blocks() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for i, m in enumerate(BLOCK.finditer(text)):
            out.append((f"{path.name}#{i}", m.group(1)))
    return out


BLOCKS = blocks()


def test_docs_contain_code() -> None:
    assert len(BLOCKS) >= 12


@pytest.mark.parametrize(("name", "src"), BLOCKS, ids=[b[0] for b in BLOCKS])
def test_doc_snippet_compiles_and_runs(name: str, src: str) -> None:
    has_main = "fn main" in src
    prog = compile_text(src, name, require_main=has_main)
    if has_main:
        assert prog.main is not None
        VM(io.StringIO()).run_function(prog.main)
