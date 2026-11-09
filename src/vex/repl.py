"""The interactive REPL: a persistent environment, globals and VM across inputs."""

from __future__ import annotations

import re
from typing import Any, TextIO

from vex.checker import Checker
from vex.compiler import Compiler
from vex.diagnostics import (
    DiagSink,
    Source,
    VexError,
    VexRuntimeError,
    drop_tracebacks,
    first_undecodable,
    out_of_memory_error,
    render_all,
    too_deep_error,
)
from vex.parser import parse_source
from vex.resolver import Resolver, make_env
from vex.runtime import VexExit
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


_CODE, _LINE_COMMENT, _BLOCK_COMMENT, _STRING, _UNICODE_ESCAPE, _INTERPOLATION, _NESTED_STRING = range(7)
_NEXT_STOP = {
    _CODE: re.compile(r'[/"()\[\]{}]'),
    _LINE_COMMENT: re.compile(r"\n"),
    _BLOCK_COMMENT: re.compile(r"[/*]"),
    _STRING: re.compile(r'["\\{]'),
    _UNICODE_ESCAPE: re.compile(r"}"),
    _INTERPOLATION: re.compile(r'["{}]'),
    _NESTED_STRING: re.compile(r'["\\]'),
}


class Continuation:
    """Decides whether the input so far ends inside a bracket, string or comment, one chunk at a time.

    It keeps the state of the scan, so each chunk costs only its own length, however much input is
    already pending. It follows the lexer's rules for everything that can open or close one of those
    regions (`Lexer._skip_trivia`, `_string`, `_escape`, `_interp_end`); no other token can contain
    those characters, so the rest of the input can be skipped.
    """

    def __init__(self) -> None:
        self.state = _CODE
        self.depth = 0  # open brackets in code
        self.comment_depth = 0
        self.interpolation_depth = 0
        self.held = ""  # the end of the last chunk, when deciding what it means needs the next character

    @property
    def incomplete(self) -> bool:
        return self.state not in (_CODE, _LINE_COMMENT) or self.depth > 0

    def feed(self, chunk: str) -> None:
        text = self.held + chunk if self.held else chunk
        self.held = ""
        i, end = 0, len(text)
        while i < end:
            found = _NEXT_STOP[self.state].search(text, i)
            if found is None:
                return
            i = found.start()
            step = self._step(text, i, text[i])
            if step == 0:  # needs a character that has not arrived yet
                self.held = text[i:]
                return
            i += step

    def _step(self, text: str, i: int, ch: str) -> int:
        """Handle the significant character ``ch`` at ``i``; return how many to consume (0: wait for more)."""
        if self.state == _CODE:
            return self._code(text, i, ch)
        if self.state == _LINE_COMMENT:
            self.state = _CODE
            return 1
        if self.state == _BLOCK_COMMENT:
            return self._block_comment(text, i, ch)
        if self.state == _STRING:
            return self._string(text, i, ch)
        if self.state == _UNICODE_ESCAPE:
            self.state = _STRING
            return 1
        if self.state == _INTERPOLATION:
            if ch == '"':
                self.state = _NESTED_STRING
            elif ch == "{":
                self.interpolation_depth += 1
            else:
                self.interpolation_depth -= 1
                if self.interpolation_depth == 0:
                    self.state = _STRING
            return 1
        if ch == '"':  # _NESTED_STRING
            self.state = _INTERPOLATION
            return 1
        return 2 if i + 1 < len(text) else 0

    def _code(self, text: str, i: int, ch: str) -> int:
        if ch == '"':
            self.state = _STRING
        elif ch in "([{":
            self.depth += 1
        elif ch in ")]}":
            self.depth -= 1
        else:  # "/"
            if i + 1 >= len(text):
                return 0
            if text[i + 1] == "/":
                self.state = _LINE_COMMENT
                return 2
            if text[i + 1] == "*":
                self.state = _BLOCK_COMMENT
                self.comment_depth = 1
                return 2
        return 1

    def _block_comment(self, text: str, i: int, ch: str) -> int:
        if i + 1 >= len(text):
            return 0
        if ch == "/" and text[i + 1] == "*":
            self.comment_depth += 1
            return 2
        if ch == "*" and text[i + 1] == "/":
            self.comment_depth -= 1
            if self.comment_depth == 0:
                self.state = _CODE
            return 2
        return 1

    def _string(self, text: str, i: int, ch: str) -> int:
        if ch == '"':
            self.state = _CODE
            return 1
        if ch == "{":
            self.state = _INTERPOLATION
            self.interpolation_depth = 1
            return 1
        if i + 1 >= len(text):  # the backslash is the last character so far
            return 0
        if text[i + 1] != "u":
            return 2
        if i + 2 >= len(text):
            return 0
        if text[i + 2] == "{":
            self.state = _UNICODE_ESCAPE
            return 3
        return 2


def is_incomplete(text: str) -> bool:
    """True when ``text`` ends inside a bracket, string or comment (more lines are needed)."""
    continuation = Continuation()
    continuation.feed(text)
    return continuation.incomplete


def _source_of_input(path: str, text: str) -> Source:
    """A Source for one input; bytes stdin could not decode become a positioned diagnostic in parse_source."""
    bad = first_undecodable(text)
    if bad is None:
        return Source(path, text)
    clean = text.translate({c: 0xFFFD for c in range(0xDC80, 0xDD00)})
    return Source(path, clean, decode_error=(bad, ord(text[bad]) - 0xDC00))


_MISSING = object()


class _Journal(dict[Any, Any]):
    """A table of the environment that can undo the current input, at a cost set by what the input changed.

    Copying the tables before each input made every input as expensive as the whole session. The stages
    change them only through the methods overridden here.
    """

    def __init__(self, items: dict[Any, Any]) -> None:
        super().__init__(items)
        self.saved: dict[Any, Any] = {}

    def remember(self, key: Any) -> None:
        if key not in self.saved:
            self.saved[key] = self.get(key, _MISSING)

    def __setitem__(self, key: Any, value: Any) -> None:
        self.remember(key)
        super().__setitem__(key, value)

    def pop(self, key: Any, *default: Any) -> Any:
        self.remember(key)
        return super().pop(key, *default)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self.remember(key)
        return super().setdefault(key, default)

    def rollback(self) -> None:
        for key, value in self.saved.items():
            if value is _MISSING:
                super().pop(key, None)
            else:
                super().__setitem__(key, value)


class _VariantJournal(_Journal):
    """`variants` maps a name to a list that the resolver appends to in place, so the list's contents are saved."""

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self.saved:
            self.saved[key] = list(self[key]) if key in self else _MISSING
        return dict.setdefault(self, key, default)


class _EnvJournal:
    """Puts undo in front of an Env's tables; `begin` starts an input and `rollback` undoes it."""

    def __init__(self, env: Env) -> None:
        self.env = env
        env.structs, env.enums, env.fns, env.methods = (_Journal(env.structs), _Journal(env.enums), _Journal(env.fns),
                                                        _Journal(env.methods))  # fmt: skip
        env.globals, env.decl_spans = _Journal(env.globals), _Journal(env.decl_spans)
        env.variants = _VariantJournal(env.variants)
        self.tables = [env.structs, env.enums, env.fns, env.methods, env.globals, env.decl_spans, env.variants]
        self.fn_count = 0

    def begin(self) -> None:
        for table in self.tables:
            table.saved.clear()
        self.fn_count = len(self.env.fn_list)

    def rollback(self) -> None:
        for table in self.tables:
            table.rollback()
        del self.env.fn_list[self.fn_count :]


class Session:
    """One REPL session. ``eval`` is the whole pipeline for a single input."""

    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.exit_code: int | None = None
        self.reset()

    def reset(self) -> None:
        self.env = make_env()
        self.journal = _EnvJournal(self.env)
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
        self.journal.begin()
        try:
            mod = parse_source(source, sink, repl=True)
            sink.raise_if_errors()
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
            self.journal.rollback()
            raise
        except RecursionError as e:
            self.journal.rollback()
            raise too_deep_error(e, source) from None
        except MemoryError as e:
            drop_tracebacks(e)  # its frames pin the tokens and syntax tree that filled the heap
            self.journal.rollback()
            raise out_of_memory_error(source) from None
        if repl_fn is None:
            return ""
        assert repl_fn.ret is not None
        ret = zonk(repl_fn.ret)
        if not run:
            return show(ret)
        value = self.vm.run_owned(repl_fn.code, [])
        if mod.tail is None or is_unit(ret):
            return ""
        return f"{self.vm.show_result(repl_fn.code, value)} : {show(ret)}"

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
    pending: list[str] = []
    continuation = Continuation()
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
            pending, continuation = [], Continuation()
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
        if not pending and not line.strip():
            continue
        continuation.feed("\n" + line if pending else line)
        pending.append(line)
        if continuation.incomplete:
            continue
        text = "\n".join(pending)
        pending, continuation = [], Continuation()
        session.handle(text)
    return session.exit_code or 0
