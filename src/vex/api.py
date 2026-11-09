"""The compiler driver: source text / files -> checked, compiled Program."""

from __future__ import annotations

import codecs
import os
from dataclasses import dataclass, field

from vex import syntax as ast
from vex.bytecode import Code, all_code, disassemble
from vex.checker import Checker
from vex.compiler import Compiler
from vex.diagnostics import Diagnostic, DiagSink, Source, VexError, out_of_memory_error, too_deep_error
from vex.parser import parse_source
from vex.resolver import Resolver, make_env
from vex.symbols import Env, FnSym, Scope
from vex.vtypes import INT, UNIT, Mismatch, prune, show, unify


@dataclass
class Program:
    env: Env
    functions: list[FnSym]
    main: Code | None
    warnings: list[Diagnostic] = field(default_factory=list)
    modules: list[ast.Module] = field(default_factory=list)

    def disassemble(self) -> str:
        roots = [f.code for f in self.functions if f.code is not None]
        return "\n\n".join(disassemble(c) for c in all_code(roots))


def read_source(path: str) -> Source:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except MemoryError:
        raise OSError("file is too large to read") from None
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8) :]
    try:
        return Source(path, raw.decode("utf-8").replace("\r\n", "\n"))
    except UnicodeDecodeError as e:
        offset = len(raw[: e.start].decode("utf-8").replace("\r\n", "\n"))
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return Source(path, text, decode_error=(offset, raw[e.start]))


class Loader:
    """Parses the root module and everything it imports (depth first, dependencies first)."""

    def __init__(self, sink: DiagSink, base_dir: str | None, allow_imports: bool = True) -> None:
        self.sink = sink
        self.base_dir = base_dir
        self.allow_imports = allow_imports
        self.done: dict[str, ast.Module] = {}
        self.order: list[ast.Module] = []
        self.stack: list[str] = []

    def load_root(self, source: Source) -> None:
        key = os.path.abspath(source.path) if os.path.exists(source.path) else source.path
        self.load(source, key)

    def load(self, source: Source, key: str) -> None:
        self.stack.append(key)
        mod = parse_source(source, self.sink)
        directory = os.path.dirname(os.path.abspath(source.path)) if os.path.exists(source.path) else self.base_dir
        for it in mod.items:
            if isinstance(it, ast.Import):
                self.do_import(it, directory)
        self.stack.pop()
        self.done[key] = mod
        self.order.append(mod)

    def do_import(self, imp: ast.Import, directory: str | None) -> None:
        name = imp.name
        if not self.allow_imports:
            self.sink.error("import-denied", f"embedded scripts cannot import `{name}`", imp.span,
                            "not available here", help="the host supplies the whole script as one source text")  # fmt: skip
            return
        if directory is None:
            self.sink.error("import-not-found", f"cannot find module `{name}`", imp.span,
                            "imports need a source file on disk")  # fmt: skip
            return
        path = os.path.join(directory, name + ".vx")
        if not os.path.isfile(path):
            self.sink.error("import-not-found", f"cannot find module `{name}`", imp.span,
                            f"looked for `{path}`", help=f"create `{name}.vx` next to this file")  # fmt: skip
            return
        key = os.path.abspath(path)
        if key in self.stack:
            chain = " -> ".join(os.path.basename(k) for k in [*self.stack[self.stack.index(key):], key])
            self.sink.error("import-cycle", f"import cycle: {chain}", imp.span, "this import closes a cycle")
            return
        if key in self.done:
            return
        try:
            src = read_source(path)
        except OSError as e:
            self.sink.error("import-unreadable", f"cannot read module `{name}`: {e}", imp.span)
            return
        self.load(src, key)


def check_main(env: Env, sink: DiagSink) -> FnSym | None:
    m = env.fns.get("main")
    if m is None:
        sink.error("no-main", "this program has no `main` function", None,
                   help="add `fn main() { ... }`")  # fmt: skip
        return None
    if m.decl.params or m.decl.self_mode is not None:
        sink.error("bad-main", "`main` must take no parameters", m.decl.name_span or m.decl.span,
                   "remove the parameters; use `env.args()` for command-line arguments")  # fmt: skip
    ret = prune(m.ret) if m.ret is not None else UNIT
    try:
        unify(ret, UNIT)
    except Mismatch:
        try:
            unify(ret, INT)
        except Mismatch:
            sink.error("bad-main", f"`main` must return Unit or Int (the exit code), not `{show(ret)}`",
                       m.decl.name_span or m.decl.span)  # fmt: skip
    return m


def compile_source(
    source: Source, require_main: bool = True, scope: Scope | None = None, allow_imports: bool = True
) -> Program:
    """Compile a source file (and its imports). Raises VexError with all diagnostics."""
    try:
        return _compile(source, require_main, scope, allow_imports)
    except RecursionError as e:
        raise too_deep_error(e, source) from None
    except MemoryError as e:
        _drop_tracebacks(e)
        raise out_of_memory_error(source) from None


def _drop_tracebacks(error: BaseException | None) -> None:
    """Release the frames, which pin the tokens and syntax tree that filled the heap, of a whole exception chain.

    The chain has more than one link when unwinding could not allocate a traceback entry: CPython then raises another
    MemoryError with the first as its context. Each traceback is cut after its first entry as well as dropped, because
    Python 3.10 keeps that entry, the frame handling the exception, in the handled-exception state until the handler ends.
    """
    while error is not None:
        if error.__traceback__ is not None:
            error.__traceback__.tb_next = None
        error.__traceback__ = None
        error = error.__context__


def _compile(
    source: Source, require_main: bool, scope: Scope | None = None, allow_imports: bool = True
) -> Program:
    sink = DiagSink()
    base = os.path.dirname(os.path.abspath(source.path)) if os.path.exists(source.path) else None
    loader = Loader(sink, base, allow_imports)
    loader.load_root(source)
    sink.raise_if_errors()
    env = make_env()
    resolver = Resolver(env, sink, scope=scope)
    fns = resolver.resolve_modules(loader.order)
    Checker(env, sink).check_all(fns)
    if require_main:
        check_main(env, sink)
    sink.raise_if_errors()
    comp = Compiler(env)
    comp.compile_functions(fns)
    main = env.fns["main"].code if "main" in env.fns else None
    warnings = [d for d in sink.sorted() if d.severity == "warning"]
    return Program(env, fns, main, warnings, loader.order)


def compile_text(text: str, path: str = "<string>", require_main: bool = True) -> Program:
    return compile_source(Source(path, text), require_main)


def compile_file(path: str, require_main: bool = True) -> Program:
    return compile_source(read_source(path), require_main)


def check_source(source: Source, require_main: bool = True) -> list[Diagnostic]:
    """Run every compile stage; return all diagnostics (empty list = clean)."""
    try:
        prog = compile_source(source, require_main)
    except VexError as e:
        return e.diagnostics
    return prog.warnings
