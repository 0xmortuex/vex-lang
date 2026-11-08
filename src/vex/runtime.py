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

# The largest Str and List one operation may build. Allocating is the one thing a single VM instruction
# can do without limit, so operations whose result is not bounded by their arguments' sizes check these first.
MAX_STR_LEN = 50_000_000
MAX_LIST_LEN = 10_000_000


class VexPanic(Exception):
    """A runtime error raised by a builtin or the VM; the VM attaches the stack trace."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.trace: list[Any] | None = None


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


def display(v: Any, top: bool = True) -> str:
    """Format a value the way ``print`` and string interpolation do."""
    if top and type(v) is str:
        return v
    return _Renderer().show(v)


class _Renderer:
    """Formats one value. Shared substructure can make the text exponentially longer than the value is big,
    so it charges every piece against the string limit as it goes."""

    def __init__(self) -> None:
        self.left = MAX_STR_LEN

    def charge(self, n: int) -> None:
        self.left -= n
        if self.left < 0:
            raise VexPanic(f"value is too large to display (over {MAX_STR_LEN} characters)")

    def scalar(self, text: str) -> str:
        self.charge(len(text))
        return text

    def show(self, v: Any) -> str:  # noqa: C901 - one case per runtime representation
        if v is None:
            return self.scalar("()")
        t = type(v)
        if t is str:
            return self.scalar(quote(v))
        if t is bool:
            return self.scalar("true" if v else "false")
        if t is int:
            return self.scalar(str(v))
        if t is float:
            return self.scalar(fmt_float(v))
        if t is list:
            self.charge(2 + _separators(len(v)))
            return "[" + ", ".join([self.show(x) for x in v]) + "]"
        if t is tuple:
            self.charge(2 + _separators(len(v)) + (len(v) == 1))
            inner = ", ".join([self.show(x) for x in v])
            return f"({inner},)" if len(v) == 1 else f"({inner})"
        if t is dict:
            self.charge(2 + _separators(len(v)) + 2 * len(v))
            return "{" + ", ".join([f"{self.show(k)}: {self.show(x)}" for k, x in v.items()]) + "}"
        if t is range:
            return self.scalar(f"{v.start}..={v.stop - 1}" if v.stop > INT_MAX else f"{v.start}..{v.stop}")
        if t is Adt:
            return self.adt(v)
        return self.scalar(_show_other(v))

    def adt(self, v: Adt) -> str:
        info = v.info
        name = info.variants[v.tag]
        self.charge(len(name) + len(info.name) + 4 + 2 * len(v.f))
        if info.fields is not None:
            names = info.fields[v.tag]
            body = ", ".join([f"{n}: {self.show(x)}" for n, x in zip(names, v.f)])
            return f"{info.name} {{ {body} }}" if names else info.name
        if not v.f:
            return name
        return f"{name}({', '.join([self.show(x) for x in v.f])})"


def _separators(n: int) -> int:
    return 2 * max(n - 1, 0)


def _show_other(v: Any) -> str:
    t = type(v)
    if t is Closure:
        return f"<fn {v.code.name}>"
    if t is BuiltinFn:
        return f"<builtin {v.name}>"
    return str(v)
