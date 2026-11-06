"""The type language and unification."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from typing import Optional

_ids = itertools.count()


class Type:
    pass


class TCon(Type):
    """A nominal type: Int, List[T], Point, Option[Int] ..."""

    __slots__ = ("name", "args")

    def __init__(self, name: str, args: tuple[Type, ...] = ()) -> None:
        self.name = name
        self.args = args


class TTuple(Type):
    __slots__ = ("items",)

    def __init__(self, items: tuple[Type, ...]) -> None:
        self.items = items


class TFn(Type):
    __slots__ = ("params", "ret")

    def __init__(self, params: tuple[Type, ...], ret: Type) -> None:
        self.params = params
        self.ret = ret


class TVar(Type):
    """An inference variable; ``ref`` is set once it has been solved."""

    __slots__ = ("id", "ref")

    def __init__(self) -> None:
        self.id = next(_ids)
        self.ref: Optional[Type] = None


class TParam(Type):
    """A rigid (declared) type parameter such as the T in ``fn id[T](x: T)``."""

    __slots__ = ("name", "bounds", "uid")

    def __init__(self, name: str, bounds: tuple[str, ...] = ()) -> None:
        self.name = name
        self.bounds = bounds
        self.uid = next(_ids)


class TNever(Type):
    """The type of expressions that never produce a value (return, panic ...)."""


class TError(Type):
    """Poison type after a reported error; unifies with everything silently."""


INT = TCon("Int")
FLOAT = TCon("Float")
BOOL = TCon("Bool")
STR = TCon("Str")
UNIT = TCon("Unit")
RANGE = TCon("Range")
NEVER = TNever()
ERROR = TError()

PRIMITIVES = {"Int": INT, "Float": FLOAT, "Bool": BOOL, "Str": STR, "Unit": UNIT, "Range": RANGE}


def list_of(t: Type) -> TCon:
    return TCon("List", (t,))


def map_of(k: Type, v: Type) -> TCon:
    return TCon("Map", (k, v))


def option_of(t: Type) -> TCon:
    return TCon("Option", (t,))


def result_of(t: Type, e: Type) -> TCon:
    return TCon("Result", (t, e))


def prune(t: Type) -> Type:
    while isinstance(t, TVar) and t.ref is not None:
        t = t.ref
    return t


def zonk(t: Type) -> Type:
    """Fully resolve all solved variables inside ``t``."""
    t = prune(t)
    if isinstance(t, TCon):
        return TCon(t.name, tuple(zonk(a) for a in t.args)) if t.args else t
    if isinstance(t, TTuple):
        return TTuple(tuple(zonk(a) for a in t.items))
    if isinstance(t, TFn):
        return TFn(tuple(zonk(a) for a in t.params), zonk(t.ret))
    return t


def show(t: Type) -> str:
    t = prune(t)
    if isinstance(t, TCon):
        if t.args:
            return f"{t.name}[{', '.join(show(a) for a in t.args)}]"
        return t.name
    if isinstance(t, TTuple):
        if len(t.items) == 1:
            return f"({show(t.items[0])},)"
        return f"({', '.join(show(a) for a in t.items)})"
    if isinstance(t, TFn):
        ret = show(t.ret)
        return f"fn({', '.join(show(a) for a in t.params)}) -> {ret}"
    if isinstance(t, TVar):
        return "_"
    if isinstance(t, TParam):
        return t.name
    if isinstance(t, TNever):
        return "Never"
    return "<error>"


def walk(t: Type) -> Iterator[Type]:
    """Yield ``t`` and every type nested inside it (after pruning)."""
    t = prune(t)
    yield t
    if isinstance(t, TCon):
        for a in t.args:
            yield from walk(a)
    elif isinstance(t, TTuple):
        for a in t.items:
            yield from walk(a)
    elif isinstance(t, TFn):
        for a in t.params:
            yield from walk(a)
        yield from walk(t.ret)


def has_var(t: Type) -> bool:
    return any(isinstance(x, TVar) for x in walk(t))


class Mismatch(Exception):
    def __init__(self, infinite: bool = False) -> None:
        super().__init__("type mismatch")
        self.infinite = infinite


def _occurs(v: TVar, t: Type) -> bool:
    return any(x is v for x in walk(t))


def unify(a: Type, b: Type) -> None:
    a = prune(a)
    b = prune(b)
    if a is b:
        return
    if isinstance(a, TVar) and isinstance(b, TError):
        a.ref = b  # poison: the error was already reported, do not report "cannot infer"
        return
    if isinstance(b, TVar) and isinstance(a, TError):
        b.ref = a
        return
    if isinstance(a, (TNever, TError)) or isinstance(b, (TNever, TError)):
        return
    if isinstance(a, TVar):
        if _occurs(a, b):
            raise Mismatch(infinite=True)
        a.ref = b
        return
    if isinstance(b, TVar):
        if _occurs(b, a):
            raise Mismatch(infinite=True)
        b.ref = a
        return
    if isinstance(a, TCon) and isinstance(b, TCon):
        if a.name != b.name or len(a.args) != len(b.args):
            raise Mismatch
        for x, y in zip(a.args, b.args):
            unify(x, y)
        return
    if isinstance(a, TTuple) and isinstance(b, TTuple):
        if len(a.items) != len(b.items):
            raise Mismatch
        for x, y in zip(a.items, b.items):
            unify(x, y)
        return
    if isinstance(a, TFn) and isinstance(b, TFn):
        if len(a.params) != len(b.params):
            raise Mismatch
        for x, y in zip(a.params, b.params):
            unify(x, y)
        unify(a.ret, b.ret)
        return
    raise Mismatch


def subst(t: Type, m: dict[TParam, Type]) -> Type:
    t = prune(t)
    if isinstance(t, TParam):
        return m.get(t, t)
    if isinstance(t, TCon):
        return TCon(t.name, tuple(subst(a, m) for a in t.args)) if t.args else t
    if isinstance(t, TTuple):
        return TTuple(tuple(subst(a, m) for a in t.items))
    if isinstance(t, TFn):
        return TFn(tuple(subst(a, m) for a in t.params), subst(t.ret, m))
    return t


def instantiate(
    tparams: list[TParam], on_fresh: Callable[[TParam, TVar], None] | None = None
) -> dict[TParam, Type]:
    out: dict[TParam, Type] = {}
    for p in tparams:
        v = TVar()
        out[p] = v
        if on_fresh is not None:
            on_fresh(p, v)
    return out


def tuple_of(items: list[Type]) -> Type:
    return UNIT if not items else TTuple(tuple(items))


def is_unit(t: Type) -> bool:
    t = prune(t)
    return isinstance(t, TCon) and t.name == "Unit"
