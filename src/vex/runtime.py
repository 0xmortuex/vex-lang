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
    if v is None:
        return "()"
    t = type(v)
    if t is str:
        return v if top else quote(v)
    if t is bool:
        return "true" if v else "false"
    if t is int:
        return str(v)
    if t is float:
        return fmt_float(v)
    if t is list:
        return "[" + ", ".join(display(x, False) for x in v) + "]"
    if t is tuple:
        inner = ", ".join(display(x, False) for x in v)
        return f"({inner},)" if len(v) == 1 else f"({inner})"
    if t is dict:
        return "{" + ", ".join(f"{display(k, False)}: {display(x, False)}" for k, x in v.items()) + "}"
    if t is range:
        return f"{v.start}..={v.stop - 1}" if v.stop > INT_MAX else f"{v.start}..{v.stop}"
    if t is Adt:
        info: AdtInfo = v.info
        name: str = info.variants[v.tag]
        if info.fields is not None:
            names = info.fields[v.tag]
            body = ", ".join(f"{n}: {display(x, False)}" for n, x in zip(names, v.f))
            return f"{info.name} {{ {body} }}" if names else info.name
        if not v.f:
            return name
        return f"{name}({', '.join(display(x, False) for x in v.f)})"
    if t is Closure:
        return f"<fn {v.code.name}>"
    if t is BuiltinFn:
        return f"<builtin {v.name}>"
    return str(v)
