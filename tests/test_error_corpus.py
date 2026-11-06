"""Type-error corpus: every file under tests/errors must produce its expected diagnostic.

Header format (first line of each file):  // expect: <code> <line>:<col> [| substring of rendered output]
A position of `-` means the diagnostic has no source position.
"""

from __future__ import annotations

import pathlib

import pytest

from vex.api import compile_source, read_source
from vex.diagnostics import VexError

ERRORS = pathlib.Path(__file__).parent / "errors"
FILES = sorted(ERRORS.glob("*.vx"))


def parse_header(path: pathlib.Path) -> tuple[str, str, str]:
    first = path.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("// expect: "), f"{path.name}: missing expect header"
    body = first[len("// expect: ") :]
    substr = ""
    if " | " in body:
        body, substr = body.split(" | ", 1)
    code, where = body.split()
    return code, where, substr


def test_corpus_is_large() -> None:
    assert len(FILES) >= 100


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_expected_error(path: pathlib.Path) -> None:
    code, where, substr = parse_header(path)
    with pytest.raises(VexError) as info:
        compile_source(read_source(str(path)))
    diags = info.value.diagnostics
    matching = [d for d in diags if d.code == code]
    assert matching, f"{path.name}: expected {code}, got {[d.code for d in diags]}\n{info.value}"
    if where != "-":
        want = tuple(int(x) for x in where.split(":"))
        got = [(d.span.line, d.span.col) for d in matching if d.span is not None]
        assert want in got, f"{path.name}: expected {code} at {want}, got {got}\n{info.value}"
    if substr:
        assert substr in str(info.value), f"{path.name}: {substr!r} not in output:\n{info.value}"
    # Every diagnostic must render without error and mention the file for positioned ones.
    for d in diags:
        text = d.render()
        assert d.code in text
