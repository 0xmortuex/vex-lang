"""Runtime value representations shared by the VM and the standard library.

Int -> Python int (checked to 64 bits), Float -> float, Bool -> bool, Str -> str,
Unit -> None, tuples -> tuple, List -> list, Map -> dict, Range -> range,
structs/enums -> Adt, functions -> Closure / BuiltinFn.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vex.bytecode import Code

INT_MIN = -(2**63)
INT_MAX = 2**63 - 1


class VexPanic(Exception):
    """A runtime error raised by a builtin or the VM; the VM attaches the stack trace."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.trace: list[Any] | None = None


class VexBudgetExceeded(VexPanic):
    """The embedded script used up its execution budget."""


class VexHostContractError(VexPanic):
    """A host function broke its declared signature (wrong type, bad UTF-8, out-of-range Int)."""


class VexExit(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"exit({code})")
        self.code = code


class AdtInfo:
    """Runtime description of a struct or enum (used for printing)."""

    __slots__ = ("name", "variants", "fields")

    def __init__(self, name: str, variants: list[str], fields: list[list[str]] | None = None) -> None:
        self.name = name
        self.variants = variants
        self.fields = fields  # struct field names per variant (structs only)


class Adt:
    __slots__ = ("info", "tag", "f")

    def __init__(self, info: AdtInfo, tag: int, f: list[Any]) -> None:
        self.info = info
        self.tag = tag
        self.f = f

    def __eq__(self, other: object) -> bool:
        return type(other) is Adt and self.tag == other.tag and self.info is other.info and self.f == other.f

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return display(self, False)


class Cell:
    """A boxed mutable variable shared between a function and the closures capturing it."""

    __slots__ = ("v",)

    def __init__(self, v: Any) -> None:
        self.v = v


class Closure:
    __slots__ = ("code", "captured")

    def __init__(self, code: Code, captured: list[Any]) -> None:
        self.code = code
        self.captured = captured


class BuiltinFn:
    __slots__ = ("name", "fn", "needs_vm")

    def __init__(self, name: str, fn: Callable[..., Any], needs_vm: bool = False) -> None:
        self.name = name
        self.fn = fn
        self.needs_vm = needs_vm


OPTION_INFO = AdtInfo("Option", ["Some", "None"])
RESULT_INFO = AdtInfo("Result", ["Ok", "Err"])
NONE = Adt(OPTION_INFO, 1, [])


def some(x: Any) -> Adt:
    return Adt(OPTION_INFO, 0, [x])


def ok(x: Any) -> Adt:
    return Adt(RESULT_INFO, 0, [x])


def err(x: Any) -> Adt:
    return Adt(RESULT_INFO, 1, [x])


def fmt_float(f: float) -> str:
    if f != f:
        return "NaN"
    if f in (float("inf"), float("-inf")):
        return "inf" if f > 0 else "-inf"
    return repr(f)


def quote(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{out}"'


# Display work items: (value or literal text, how to treat it).
_NESTED = 0
_TOP = 1
_TEXT = 2


def _no_charge(units: int) -> None:
    pass


def values_equal(a: Any, b: Any, charge: Callable[[int], None] | None = None) -> bool:
    """Vex ``==``: the same result as Python's, but without recursion and spending budget as it goes."""
    spend = charge or _no_charge
    work: list[tuple[Any, Any]] = [(a, b)]
    while work:
        x, y = work.pop()
        spend(1)
        t = type(x)
        if t is not type(y):
            if x != y:
                return False
        elif t is Adt:
            if x.tag != y.tag or x.info is not y.info or len(x.f) != len(y.f):
                return False
            work.extend((p, q) for p, q in zip(reversed(x.f), reversed(y.f)) if p is not q)
        elif t is list or t is tuple:
            if len(x) != len(y):
                return False
            spend(len(x))
            work.extend((p, q) for p, q in zip(reversed(x), reversed(y)) if p is not q)
        elif t is dict:
            if len(x) != len(y):
                return False
            spend(len(x))
            for k, p in x.items():
                if k not in y:
                    return False
                q = y[k]
                if p is not q:
                    work.append((p, q))
        elif t is str:
            if len(x) != len(y):
                return False
            spend(len(x))
            if x != y:
                return False
        elif x != y:
            return False
    return True


def charge_size(value: Any, charge: Callable[[int], None]) -> int:
    """Spend one unit per element or character reachable from ``value``, and return the total."""
    total = 0
    work = [value]
    while work:
        x = work.pop()
        t = type(x)
        size = 1
        if t is str:
            size += len(x)
        elif t is list or t is tuple:
            size += len(x)
            work.extend(x)
        elif t is dict:
            size += len(x)
            work.extend(x.values())
        elif t is Adt:
            size += len(x.f)
            work.extend(x.f)
        charge(size)
        total += size
    return total


def _scalar_text(v: Any) -> str:
    if v is None:
        return "()"
    t = type(v)
    if t is bool:
        return "true" if v else "false"
    if t is float:
        return fmt_float(v)
    return str(v)


def display(v: Any, top: bool = True, charge: Callable[[int], None] | None = None) -> str:
    """Format a value the way ``print`` and string interpolation do.

    Without recursion, so nesting cannot exhaust the Python stack, and spending budget as text is produced.
    """
    if v is None or type(v) in (bool, int, float):
        return _scalar_text(v)
    if type(v) is str and top:
        return v
    spend = charge or _no_charge
    out: list[str] = []
    work: list[tuple[Any, int]] = [(v, _TOP if top else _NESTED)]
    while work:
        x, mode = work.pop()
        if mode == _TEXT:
            out.append(x)
            continue
        spend(1)
        t = type(x)
        if x is None or t in (bool, int, float):
            out.append(_scalar_text(x))
        elif t is str:
            spend(len(x))
            out.append(x if mode == _TOP else quote(x))
        elif t is list or t is tuple or t is dict:
            spend(len(x))
            _push_container(work, x, t)
        elif t is Adt:
            spend(len(x.f))
            _push_adt(work, x)
        elif t is range:
            out.append(f"{x.start}..{x.stop}")
        elif t is Closure:
            out.append(f"<fn {x.code.name}>")
        elif t is BuiltinFn:
            out.append(f"<builtin {x.name}>")
        else:
            out.append(str(x))
    return "".join(out)


def _push_separated(work: list[tuple[Any, int]], items: list[list[tuple[Any, int]]]) -> None:
    """Push ``items`` (each already in display order) so they pop in order, with ", " between them."""
    for i in range(len(items) - 1, -1, -1):
        work.extend(reversed(items[i]))
        if i:
            work.append((", ", _TEXT))


def _push_container(work: list[tuple[Any, int]], x: Any, t: type) -> None:
    if t is dict:
        work.append(("}", _TEXT))
        _push_separated(work, [[(k, _NESTED), (": ", _TEXT), (v, _NESTED)] for k, v in x.items()])
        work.append(("{", _TEXT))
        return
    closing = "]" if t is list else (",)" if len(x) == 1 else ")")
    work.append((closing, _TEXT))
    _push_separated(work, [[(i, _NESTED)] for i in x])
    work.append(("[" if t is list else "(", _TEXT))


def _push_adt(work: list[tuple[Any, int]], v: Adt) -> None:
    info: AdtInfo = v.info
    name: str = info.variants[v.tag]
    if info.fields is not None:
        names = info.fields[v.tag]
        if not names:
            work.append((info.name, _TEXT))
            return
        work.append((" }", _TEXT))
        _push_separated(work, [[(f"{n}: ", _TEXT), (x, _NESTED)] for n, x in zip(names, v.f)])
        work.append((f"{info.name} {{ ", _TEXT))
    elif not v.f:
        work.append((name, _TEXT))
    else:
        work.append((")", _TEXT))
        _push_separated(work, [[(x, _NESTED)] for x in v.f])
        work.append((f"{name}(", _TEXT))
