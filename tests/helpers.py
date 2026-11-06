"""Shared test helpers: compile and run Vex source in-process."""

from __future__ import annotations

import io

from vex.api import compile_text
from vex.diagnostics import VexError
from vex.vm import VM


def run_source(src: str, args: list[str] | None = None) -> str:
    """Compile and run ``src`` (a complete program); return everything it printed."""
    prog = compile_text(src, "<test>")
    out = io.StringIO()
    vm = VM(out, args)
    assert prog.main is not None
    vm.run_function(prog.main)
    return out.getvalue()


def run_body(body: str) -> str:
    """Run statements wrapped in ``fn main() { ... }``."""
    return run_source("fn main() {\n" + body + "\n}\n")


def error_codes(src: str) -> list[str]:
    """The diagnostic codes produced by a program that fails to compile."""
    try:
        compile_text(src, "<test>")
    except VexError as e:
        return [d.code for d in e.diagnostics]
    return []
