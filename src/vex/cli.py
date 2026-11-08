"""Command line interface: vex run | check | repl | disasm."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from collections.abc import Callable

from vex import __version__
from vex.api import compile_file, read_source
from vex.diagnostics import VexError, VexRuntimeError, render_all
from vex.runtime import VexExit
from vex.vm import VM

EXIT_COMPILE = 1
EXIT_RUNTIME = 2
EXIT_INTERNAL = 70


def _configure_streams() -> None:
    for stream, errors in ((sys.stdout, "strict"), (sys.stderr, "backslashreplace")):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is not None:
            reconf(encoding="utf-8", errors=errors, newline="\n")
    reconf_in = getattr(sys.stdin, "reconfigure", None)
    if reconf_in is not None:
        reconf_in(encoding="utf-8", errors="surrogateescape")  # readers report invalid UTF-8 themselves


def _run_big_stack(call: Callable[[], int]) -> int:
    """Run ``call`` on a thread with a large stack.

    Compiling a very long expression chain and printing/comparing deeply nested values recurse
    in Python; a big stack and a high recursion limit keep that a Vex diagnostic or a result
    instead of a crash.
    """
    result: list[int] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            result.append(call())
        except BaseException as e:  # re-raised on the main thread below
            errors.append(e)

    sys.setrecursionlimit(60_000)
    try:
        threading.stack_size(128 * 1024 * 1024)
    except (ValueError, RuntimeError):
        pass  # keep the platform default stack; only very deep structures are affected
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if errors:
        raise errors[0]
    return result[0]


def cmd_run(args: argparse.Namespace) -> int:
    try:
        prog = compile_file(args.file)
    except VexError as e:
        print(render_all(e.diagnostics), file=sys.stderr)
        n = sum(1 for d in e.diagnostics if d.severity == "error")
        print(f"\naborting: {n} error{'s' if n != 1 else ''}", file=sys.stderr)
        return EXIT_COMPILE
    except OSError as e:
        print(f"error: cannot read {args.file}: {e}", file=sys.stderr)
        return EXIT_COMPILE
    if prog.warnings:
        print(render_all(prog.warnings), file=sys.stderr)
    assert prog.main is not None
    vm = VM(args=args.args)
    try:
        result = vm.run_function(prog.main)
    except VexRuntimeError as e:
        sys.stdout.flush()
        print(e.render(), file=sys.stderr)
        return EXIT_RUNTIME
    except VexExit as e:
        return e.code
    return result if isinstance(result, int) and not isinstance(result, bool) else 0


def cmd_check(args: argparse.Namespace) -> int:
    from vex.api import check_source

    status = 0
    for path in args.files:
        try:
            src = read_source(path)
        except OSError as e:
            print(f"error: cannot read {path}: {e}", file=sys.stderr)
            return EXIT_COMPILE
        diags = check_source(src, require_main=args.main)
        errors = [d for d in diags if d.severity == "error"]
        if diags:
            print(render_all(diags), file=sys.stderr)
        if errors:
            print(f"\n{path}: {len(errors)} error{'s' if len(errors) != 1 else ''}", file=sys.stderr)
            status = EXIT_COMPILE
        else:
            print(f"{path}: ok")
    return status


def cmd_disasm(args: argparse.Namespace) -> int:
    try:
        prog = compile_file(args.file)
    except VexError as e:
        print(render_all(e.diagnostics), file=sys.stderr)
        return EXIT_COMPILE
    print(prog.disassemble())
    return 0


def cmd_repl(args: argparse.Namespace) -> int:
    from vex.repl import run_repl

    return run_repl(sys.stdin, sys.stdout)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vex", description="Vex: a friendly, statically typed language.")
    p.add_argument("--version", action="version", version=f"vex {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="compile and run a .vx program")
    r.add_argument("file")
    r.add_argument("args", nargs=argparse.REMAINDER, help="arguments visible through env.args()")
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("check", help="type-check files without running them")
    c.add_argument("files", nargs="+")
    c.add_argument("--no-main", dest="main", action="store_false", help="do not require a main function")
    c.set_defaults(fn=cmd_check)
    d = sub.add_parser("disasm", help="print the bytecode of a program")
    d.add_argument("file")
    d.set_defaults(fn=cmd_disasm)
    rp = sub.add_parser("repl", help="start an interactive session")
    rp.set_defaults(fn=cmd_repl)
    return p


def main(argv: list[str] | None = None) -> int:
    _configure_streams()
    args = build_parser().parse_args(argv)
    try:
        return _run_big_stack(lambda: int(args.fn(args)))
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # an internal bug: say so plainly instead of a bare traceback
        if os.environ.get("VEX_DEBUG"):
            raise
        print(f"internal compiler error: {type(e).__name__}: {e}\n"
              "this is a bug in Vex; set VEX_DEBUG=1 for a traceback", file=sys.stderr)  # fmt: skip
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
