"""The interactive REPL: a persistent environment, globals and VM across inputs."""

from __future__ import annotations

from typing import TextIO

from vex.checker import Checker
from vex.compiler import Compiler
from vex.diagnostics import (
    DiagSink,
    Source,
    VexError,
    VexRuntimeError,
    first_undecodable,
    render_all,
    too_deep_error,
)
from vex.lexer import Lexer
from vex.parser import parse_source
from vex.resolver import Resolver, make_env
from vex.runtime import VexExit, display
from vex.symbols import Env
from vex.vm import VM
from vex.vtypes import is_unit, show, zonk

HELP = """\
Enter expressions, `let` statements, or definitions (fn / struct / enum / impl).
  :type EXPR   show the type of an expression without running it
  :reset       forget everything
  :help        this text
  :quit        leave (also Ctrl-D)
Unfinished input (open brackets or strings) continues on the next line."""


def is_incomplete(text: str) -> bool:
    """True when ``text`` ends inside a bracket, string or comment (more lines are needed)."""
    sink = DiagSink()
    toks = Lexer(Source("<repl>", text), sink).lex()
    if any(d.code.startswith("unterminated") for d in sink.items):
        return True
    depth = 0
    for t in toks:
        if t.kind in ("(", "[", "{", "#{"):
            depth += 1
        elif t.kind in (")", "]", "}"):
            depth -= 1
    return depth > 0


def _source_of_input(path: str, text: str) -> Source:
    """A Source for one input; bytes stdin could not decode become a positioned diagnostic in parse_source."""
    bad = first_undecodable(text)
    if bad is None:
        return Source(path, text)
    clean = text.translate({c: 0xFFFD for c in range(0xDC80, 0xDD00)})
    return Source(path, clean, decode_error=(bad, ord(text[bad]) - 0xDC00))


def _snapshot(e: Env) -> tuple[object, ...]:
    return (dict(e.structs), dict(e.enums), dict(e.fns), dict(e.methods),
            {k: list(v) for k, v in e.variants.items()}, list(e.fn_list), dict(e.globals),
            dict(e.decl_spans))  # fmt: skip


def _restore(e: Env, snap: tuple[object, ...]) -> None:
    (e.structs, e.enums, e.fns, e.methods, e.variants, e.fn_list, e.globals, e.decl_spans) = snap  # type: ignore[assignment]


class Session:
    """One REPL session. ``eval`` is the whole pipeline for a single input."""

    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.exit_code: int | None = None
        self.reset()

    def reset(self) -> None:
        self.env = make_env()
        self.vm = VM(self.out)
        self.compiler = Compiler(self.env)
        self.count = 0

    def eval(self, text: str, run: bool = True) -> str:
        """Evaluate one input and return the text to echo (maybe empty).

        Raises VexError for compile errors (the session state is rolled back),
        VexRuntimeError for runtime errors and VexExit when the program calls exit().
        """
        self.count += 1
        source = _source_of_input(f"<repl:{self.count}>", text)
        sink = DiagSink()
        mod = parse_source(source, sink, repl=True)
        sink.raise_if_errors()
        snap = _snapshot(self.env)
        try:
            resolver = Resolver(self.env, sink, repl=True)
            fns = resolver.resolve_modules([mod])
            repl_fn = resolver.resolve_repl(mod)
            if repl_fn is not None:
                fns = [*fns, repl_fn]
            Checker(self.env, sink).check_all(fns)
            sink.raise_if_errors()
            self.env.fns.pop("__repl__", None)
            self.compiler.compile_functions(fns)
        except VexError:
            _restore(self.env, snap)
            raise
        except RecursionError as e:
            _restore(self.env, snap)
            raise too_deep_error(e, source) from None
        if repl_fn is None:
            return ""
        assert repl_fn.ret is not None
        ret = zonk(repl_fn.ret)
        if not run:
            return show(ret)
        value = self.vm.run_function(repl_fn.code)
        if mod.tail is None or is_unit(ret):
            return ""
        return f"{display(value, False)} : {show(ret)}"

    def handle(self, text: str, run: bool = True) -> None:
        """Evaluate and print results/diagnostics to the session output."""
        try:
            res = self.eval(text, run=run)
            if res:
                self.out.write(res + "\n")
        except VexError as e:
            self.out.write(render_all(e.diagnostics) + "\n")
        except VexRuntimeError as e:
            self.out.write(e.render() + "\n")
        except VexExit as e:
            self.exit_code = e.code


def run_repl(stdin: TextIO, stdout: TextIO) -> int:
    session = Session(stdout)
    interactive = hasattr(stdin, "isatty") and stdin.isatty()
    if interactive:
        stdout.write("Vex REPL. :help for help, :quit to leave.\n")
    pending = ""
    while session.exit_code is None:
        if interactive:
            stdout.write("...> " if pending else "vex> ")
            stdout.flush()
        line = stdin.readline()
        if line == "":
            if interactive:
                stdout.write("\n")
            break
        line = line.rstrip("\r\n")
        if first_undecodable(line) is not None:
            pending = ""
            session.handle(line)
            continue
        if not pending and line.strip().startswith(":"):
            cmd, _, rest = line.strip().partition(" ")
            if cmd in (":quit", ":q", ":exit"):
                break
            if cmd == ":help":
                stdout.write(HELP + "\n")
            elif cmd == ":reset":
                session.reset()
                stdout.write("state cleared\n")
            elif cmd == ":type":
                session.handle(rest, run=False)
            else:
                stdout.write(f"unknown command {cmd}; try :help\n")
            continue
        pending = pending + "\n" + line if pending else line
        if not pending.strip():
            pending = ""
            continue
        if is_incomplete(pending):
            continue
        text, pending = pending, ""
        session.handle(text)
    return session.exit_code or 0
