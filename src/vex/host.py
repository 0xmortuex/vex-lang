"""The public, per-instance host API: run Vex scripts inside a Python program.

A ``Host`` owns one script at a time. It binds host functions into the reserved ``host.*`` namespace,
grants capabilities (everything starts closed) and limits each call with an execution budget. Nothing here
changes the standard library's module-level tables, so instances never see each other's bindings.

Only ``Unit``, ``Int``, ``Float``, ``Bool`` and ``Str`` cross between a script and its host. The rules are the
ones in docs/ffi.md, because the native embedding in ``vex.embed`` is a thin layer over this class.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, NoReturn, TextIO

from vex.api import Program, compile_source
from vex.diagnostics import Diagnostic, DiagSink, Source, VexError, VexRuntimeError, suggest
from vex.lexer import KEYWORDS
from vex.parser import Parser
from vex.runtime import INT_MAX, INT_MIN, VexBudgetExceeded, VexHostContractError, VexPanic
from vex.stdlib import GLOBALS, NAMESPACES, _convert
from vex.symbols import Builtin, Scope
from vex.vm import VM
from vex.vtypes import BOOL, FLOAT, INT, STR, UNIT, TFn, TNever, TVar, Type, prune, show

CAP_PRINT = 1
CAP_READ = 2
CAP_WRITE = 4
CAP_ENV = 8
CAP_ALL = CAP_PRINT | CAP_READ | CAP_WRITE | CAP_ENV

DEFAULT_BUDGET = 10_000_000

# Each host re-entry (script to host to script) nests native and interpreter frames on one OS thread.
# The limit is process-wide, so chains across instances count too, and keeps that well inside the 1 MiB
# default stack of a Windows main thread.
MAX_HOST_DEPTH = 32

_CAPABILITY_OF: dict[str, tuple[int, str]] = {
    "print": (CAP_PRINT, "print"),
    "eprint": (CAP_PRINT, "print"),
    "io.read_file": (CAP_READ, "read"),
    "io.read_line": (CAP_READ, "read"),
    "io.write_file": (CAP_WRITE, "write"),
    "env.get": (CAP_ENV, "env"),
    "env.args": (CAP_ENV, "env"),
}
_PURE_GLOBALS = {"panic", "assert", "assert_eq"}
_PURE_NAMESPACES = {"math"}

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SCALARS: tuple[Type, ...] = (UNIT, INT, FLOAT, BOOL, STR)


class VexBindingError(Exception):
    """A misuse of the host boundary: bad name, signature, arity or argument (status 3 in the ABI)."""


class VexBudgetError(VexRuntimeError):
    """A call used up its execution budget (status 4 in the ABI)."""


def _is_scalar(t: Type) -> bool:
    return any(t is s for s in _SCALARS)


def parse_signature(sig: str) -> TFn:
    """Parse a binding signature such as ``(Int, Str) -> Bool``. Raises VexBindingError."""
    sink = DiagSink()
    try:
        p = Parser(Source("<signature>", sig), sink)
        p.expect("(")
        params = []
        while not p.at(")"):
            params.append(p.parse_type())
            if not p.eat(","):
                break
        p.expect(")")
        ret = p.parse_type() if p.eat("->") else None
        if not p.at("EOF"):
            raise VexBindingError(f"bad signature `{sig}`: unexpected text after the type")
    except VexBindingError:
        raise
    except Exception as e:
        raise VexBindingError(f"bad signature `{sig}`: {e}") from None
    if sink.has_errors:
        raise VexBindingError(f"bad signature `{sig}`: {sink.items[0].message}")
    fn = TFn(tuple(_convert(t, {}) for t in params), _convert(ret, {}) if ret is not None else UNIT)
    for t in (*fn.params, fn.ret):
        if not _is_scalar(t):
            raise VexBindingError(f"bad signature `{sig}`: `{show(t)}` does not cross the host boundary")
    return fn


def _value_problem(t: Type, v: Any) -> str | None:
    """Why ``v`` is not a valid value of scalar type ``t``, or None if it is."""
    if t is INT:
        if type(v) is not int:
            return f"expected Int, got {type(v).__name__}"
        if not INT_MIN <= v <= INT_MAX:
            return f"Int value {v} is outside the 64-bit range"
    elif t is FLOAT:
        if type(v) is not float:
            return f"expected Float, got {type(v).__name__}"
    elif t is BOOL:
        if type(v) is not bool:
            return f"expected Bool, got {type(v).__name__}"
    elif t is STR:
        if type(v) is not str:
            return f"expected Str, got {type(v).__name__}"
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:
            return "Str value cannot be encoded as UTF-8"
    elif v is not None:
        return f"expected Unit, got {type(v).__name__}"
    return None


class Host:
    """One embedded Vex instance."""

    _active_calls = 0  # process-wide: chains across instances share the native stack

    def __init__(self, caps: int = 0, out: TextIO | None = None) -> None:
        if type(caps) is not int or caps < 0 or caps > CAP_ALL:
            raise VexBindingError(f"unknown capability bits in {caps!r}")
        self.caps = caps
        self._vm = VM(out)
        self._vm.embedded = True
        self._bindings: dict[str, Builtin] = {}
        self._program: Program | None = None
        self._budget = DEFAULT_BUDGET
        self._depth = 0

    # ------------------------------------------------------------ setup
    def bind(self, name: str, sig: str, fn: Callable[..., Any]) -> None:
        """Make ``fn`` callable as ``host.<name>`` from scripts loaded after this call."""
        if not _NAME_RE.match(name) or name in KEYWORDS:
            raise VexBindingError(f"bad host function name {name!r}")
        if name in self._bindings:
            raise VexBindingError(f"host function `{name}` is already bound")
        ty = parse_signature(sig)
        self._bindings[name] = Builtin(f"host.{name}", [], ty, self._wrap(name, ty, fn))

    def budget(self, units: int) -> None:
        """Set the units each outermost call may spend."""
        if type(units) is not int or units <= 0:
            raise VexBindingError(f"budget must be a positive integer, got {units!r}")
        self._budget = units

    def load(self, path: str, src: str) -> None:
        """Compile ``src``, replacing the current script. On failure the current script stays."""
        try:
            program = compile_source(Source(path, src), False, self._scope(), allow_imports=False)
        except VexError:
            raise
        except Exception as e:
            raise VexError([Diagnostic("internal-error", f"the compiler failed: {type(e).__name__}: {e}")]) from None
        self._program = program

    def fail(self, message: str) -> NoReturn:
        """Called from inside a host function: stop the script with a runtime error."""
        raise VexPanic(message)

    def fuel_left(self) -> int:
        """Units remaining in the current or most recent call."""
        return int(self._vm.fuel)

    # ------------------------------------------------------------ running scripts
    def call(self, name: str, args: list[Any]) -> Any:
        """Call a script function. Raises VexRuntimeError, VexBudgetError or VexBindingError."""
        sym, code, ret = self._lookup(name, args)
        # A failure before any script code has run has no running frame to point at; name the function being entered.
        entry = [(sym.name, sym.decl.name_span or sym.decl.span)]
        if Host._active_calls >= MAX_HOST_DEPTH:
            raise VexRuntimeError(f"host re-entry is nested too deeply (more than {MAX_HOST_DEPTH} levels)", entry)
        vm = self._vm
        if self._depth == 0:
            vm.fuel = self._budget
        saved_frames = vm.frames
        vm.frames = []
        self._depth += 1
        Host._active_calls += 1
        try:
            vm.charge(1 + sum(len(a) for a in args if type(a) is str))
            result = vm.execute(code, list(args) + [None] * (code.nlocals - len(args)))
            if type(result) is str:  # the result is copied to the host, once per crossing
                vm.charge(len(result))
        except VexBudgetExceeded as e:
            raise VexBudgetError(e.message, e.trace or entry) from None
        except VexHostContractError as e:
            rendered = VexRuntimeError(e.message, e.trace or entry).render()
            raise VexBindingError(rendered.removeprefix("runtime error: ")) from None
        except VexPanic as e:
            raise VexRuntimeError(e.message, e.trace or entry) from None
        except RecursionError:
            raise VexRuntimeError("recursion too deep while comparing or printing a value", entry) from None
        except Exception as e:
            raise VexRuntimeError(f"internal error: {type(e).__name__}: {e}", entry) from None
        finally:
            self._depth -= 1
            Host._active_calls -= 1
            vm.frames = saved_frames
        if not isinstance(ret, (TNever, TVar)):  # a body that only panics never produces a value
            problem = _value_problem(ret, result)
            if problem is not None:
                raise VexRuntimeError(f"`{name}` returned an invalid value: {problem}", entry)
        return result

    def _lookup(self, name: str, args: list[Any]) -> tuple[Any, Any, Type]:
        if self._program is None:
            raise VexBindingError("no script is loaded")
        fns = self._program.env.fns
        sym = fns.get(name)
        if sym is None:
            s = suggest(name, fns)
            raise VexBindingError(f"unknown function `{name}`" + (f"; did you mean `{s}`?" if s else ""))
        if sym.tparams:
            raise VexBindingError(f"generic function `{name}` does not cross the host boundary")
        assert sym.ret is not None
        ret = prune(sym.ret)
        params = [prune(p) for p in sym.params]
        for t in (*params, ret):
            if not _is_scalar(t) and not isinstance(t, (TNever, TVar)):
                raise VexBindingError(f"`{name}` uses `{show(t)}`, which does not cross the host boundary")
        if len(args) != len(params):
            raise VexBindingError(f"`{name}` expects {len(params)} argument(s), got {len(args)}")
        for i, (t, v) in enumerate(zip(params, args)):
            problem = _value_problem(t, v)
            if problem is not None:
                raise VexBindingError(f"argument {i + 1} of `{name}`: {problem}")
        return sym, sym.code, ret

    def _wrap(self, name: str, ty: TFn, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Turn a host callable into a builtin: every failure becomes a VexPanic with a script location."""

        def invoke(*args: Any) -> Any:
            for a in args:  # copying a Str across the boundary costs its length
                if type(a) is str:
                    self._vm.charge(len(a))
            try:
                result = fn(*args)
            except VexPanic:
                if self._vm.fuel < 0:  # a re-entrant call ran out and the host reported a failure instead
                    raise VexBudgetExceeded("execution budget exhausted") from None
                raise
            except VexBudgetError as e:
                raise VexBudgetExceeded(e.message) from None
            except VexRuntimeError as e:
                raise VexPanic(e.message) from None
            except VexBindingError as e:
                raise VexHostContractError(str(e)) from None
            except Exception as e:
                raise VexPanic(f"host function `{name}` raised {type(e).__name__}: {e}") from None
            if self._vm.fuel < 0:  # a re-entrant call ran out, and the host carried on regardless
                raise VexBudgetExceeded("execution budget exhausted")
            if type(result) is str:
                self._vm.charge(len(result))
            problem = _value_problem(ty.ret, result)
            if problem is not None:
                raise VexHostContractError(f"host function `{name}` broke its signature: {problem}")
            return result

        return invoke

    # ------------------------------------------------------------ what a script can see
    def _scope(self) -> Scope:
        denied: dict[str, str] = {}

        def granted(qual: str) -> bool:
            need = _CAPABILITY_OF.get(qual)
            if need is None:
                denied[qual] = "this name is not available to embedded scripts"
                return False
            if self.caps & need[0]:
                return True
            denied[qual] = f"the host did not grant the `{need[1]}` capability (bit {need[0]})"
            return False

        globals_ = {n: b for n, b in GLOBALS.items() if n in _PURE_GLOBALS or granted(n)}
        namespaces: dict[str, dict[str, Builtin]] = {"host": dict(self._bindings)}
        for space, members in NAMESPACES.items():
            if space in _PURE_NAMESPACES:
                namespaces[space] = members
            else:
                namespaces[space] = {n: b for n, b in members.items() if granted(f"{space}.{n}")}
        return Scope(globals_, namespaces, denied)


__all__ = [
    "CAP_ALL",
    "CAP_ENV",
    "CAP_PRINT",
    "CAP_READ",
    "CAP_WRITE",
    "DEFAULT_BUDGET",
    "MAX_HOST_DEPTH",
    "Host",
    "VexBindingError",
    "VexBudgetError",
    "parse_signature",
]
