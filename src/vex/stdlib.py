"""The standard library: signatures (written in Vex's own type syntax) and Python implementations."""

from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Callable
from typing import Any

from vex import syntax as ast
from vex.diagnostics import DiagSink, Source
from vex.parser import Parser
from vex.runtime import (
    INT_MAX,
    INT_MIN,
    NONE,
    VexExit,
    VexPanic,
    charge_size,
    display,
    err,
    ok,
    some,
)
from vex.symbols import Builtin, Scope
from vex.vtypes import (
    FLOAT,
    INT,
    NEVER,
    PRIMITIVES,
    UNIT,
    TCon,
    TFn,
    TParam,
    TTuple,
    Type,
)

GLOBALS: dict[str, Builtin] = {}
METHODS: dict[str, dict[str, Builtin]] = {}
NAMESPACES: dict[str, dict[str, Builtin]] = {"math": {}, "io": {}, "env": {}}
BOUNDS = ("Eq", "Ord")


def _convert(te: ast.TypeExpr, tps: dict[str, TParam]) -> Type:
    if isinstance(te, ast.TyTuple):
        return UNIT if not te.items else TTuple(tuple(_convert(i, tps) for i in te.items))
    if isinstance(te, ast.TyFn):
        return TFn(tuple(_convert(p, tps) for p in te.params), _convert(te.ret, tps) if te.ret else UNIT)
    if te.name in tps:
        return tps[te.name]
    if te.name == "Never":
        return NEVER
    if te.name in PRIMITIVES:
        return PRIMITIVES[te.name]
    return TCon(te.name, tuple(_convert(a, tps) for a in te.args))


def _parse_sig(sig: str) -> tuple[list[TParam], TFn]:
    sink = DiagSink()
    p = Parser(Source("<builtin>", sig), sink)
    decls = p.parse_tparams()
    tps = {d.name: TParam(d.name, tuple(d.bounds)) for d in decls}
    p.expect("(")
    params: list[ast.TypeExpr] = []
    while not p.at(")"):
        params.append(p.parse_type())
        if not p.eat(","):
            break
    p.expect(")")
    ret = p.parse_type() if p.eat("->") else None
    if sink.has_errors:  # pragma: no cover - a bug in this file
        raise AssertionError(f"bad builtin signature {sig!r}: {sink.items[0].message}")
    fn = TFn(tuple(_convert(t, tps) for t in params), _convert(ret, tps) if ret else UNIT)
    return list(tps.values()), fn


def _reg(table: dict[str, Builtin], qual: str, name: str, sig: str, impl: Callable[..., Any],
         mutates: bool = False, needs_vm: bool = False) -> None:  # fmt: skip
    tps, fn = _parse_sig(sig)
    table[name] = Builtin(qual, tps, fn, impl, mutates, needs_vm)


def method(head: str, name: str, sig: str, impl: Callable[..., Any], mutates: bool = False,
           needs_vm: bool = False) -> None:  # fmt: skip
    _reg(METHODS.setdefault(head, {}), f"{head}.{name}", name, sig, impl, mutates, needs_vm)


def glob(name: str, sig: str, impl: Callable[..., Any], needs_vm: bool = False) -> None:
    _reg(GLOBALS, name, name, sig, impl, needs_vm=needs_vm)


def ns(space: str, name: str, sig: str, impl: Callable[..., Any], needs_vm: bool = False) -> None:
    _reg(NAMESPACES[space], f"{space}.{name}", name, sig, impl, needs_vm=needs_vm)


def const(space: str, name: str, ty: Type, value: Any) -> None:
    NAMESPACES[space][name] = Builtin(f"{space}.{name}", [], ty, None, value=value, is_const=True)


def _by_input(impl: Callable[..., Any], factor: int = 1) -> Callable[..., Any]:
    """Charge the size of a builtin's text and collection arguments before it runs (``needs_vm=True``).

    A builder that makes a new object per element passes a higher ``factor``, so a unit buys the same memory everywhere.
    """

    def go(vm: Any, *args: Any) -> Any:
        vm.charge(factor * sum(len(a) for a in args if type(a) in (str, list, dict)))
        return impl(*args)

    return go


def _by_key(impl: Callable[..., Any]) -> Callable[..., Any]:
    """Charge the size of a builtin's map key before it runs (``needs_vm=True``); see ``VM.charge_key``."""

    def go(vm: Any, m: Any, key: Any, *rest: Any) -> Any:
        vm.charge_key(key)
        return impl(m, key, *rest)

    return go


def _snapshot(vm: Any, xs: list[Any]) -> list[Any]:
    """A callback may change ``xs``, so loop over a copy. The copy is real work, charged before it is made."""
    vm.charge(len(xs))
    return list(xs)


def checked(r: int, what: str) -> int:
    if r > INT_MAX or r < INT_MIN:
        raise VexPanic(f"integer overflow in `{what}`")
    return r


# ------------------------------------------------------------------ global functions
def _print(vm: Any, x: Any) -> None:
    vm.out.write(display(x, charge=vm.charge) + "\n")


def _eprint(vm: Any, x: Any) -> None:
    sys.stderr.write(display(x, charge=vm.charge) + "\n")


def _panic(msg: str) -> None:
    raise VexPanic(msg)


def _assert(c: bool) -> None:
    if not c:
        raise VexPanic("assertion failed")


def _assert_eq(vm: Any, a: Any, b: Any) -> None:
    if not vm.equal(a, b):
        left, right = display(a, False, vm.charge), display(b, False, vm.charge)
        raise VexPanic(f"assertion failed: left = {left}, right = {right}")


def _exit(code: int) -> None:
    raise VexExit(code)


glob("print", "[T](T)", _print, needs_vm=True)
glob("eprint", "[T](T)", _eprint, needs_vm=True)
glob("panic", "(Str) -> Never", _panic)
glob("assert", "(Bool)", _assert)
glob("assert_eq", "[T: Eq](T, T)", _assert_eq, needs_vm=True)
glob("exit", "(Int) -> Never", _exit)


# ------------------------------------------------------------------ Int / Float / Bool
def _idiv(a: int, b: int) -> int:
    if b == 0:
        raise VexPanic("division by zero")
    q = abs(a) // abs(b)
    return checked(-q if (a < 0) != (b < 0) else q, "/")


def _pow(a: int, n: int) -> int:
    if n < 0:
        raise VexPanic("negative exponent in `pow`")
    if abs(a) > 1 and n > 64:
        raise VexPanic("integer overflow in `pow`")
    return checked(a**n, "pow")


def _to_int(f: float) -> int:
    if f != f or f in (float("inf"), float("-inf")):
        raise VexPanic(f"cannot convert {display(f)} to Int")
    return checked(int(f), "to_int")


def _to_char(n: int) -> str:
    if n < 0 or n > 0x10FFFF or 0xD800 <= n <= 0xDFFF:
        raise VexPanic(f"{n} is not a valid code point")
    return chr(n)


method("Int", "abs", "(Int) -> Int", lambda a: checked(abs(a), "abs"))
method("Int", "pow", "(Int, Int) -> Int", _pow)
method("Int", "min", "(Int, Int) -> Int", min)
method("Int", "max", "(Int, Int) -> Int", max)
method("Int", "signum", "(Int) -> Int", lambda a: (a > 0) - (a < 0))
method("Int", "to_float", "(Int) -> Float", float)
method("Int", "to_str", "(Int) -> Str", str)
method("Int", "to_char", "(Int) -> Str", _to_char)
method("Float", "abs", "(Float) -> Float", abs)
method("Float", "floor", "(Float) -> Float", lambda f: float(math.floor(f)) if math.isfinite(f) else f)
method("Float", "ceil", "(Float) -> Float", lambda f: float(math.ceil(f)) if math.isfinite(f) else f)
method("Float", "round", "(Float) -> Float", lambda f: float(round(f)) if math.isfinite(f) else f)
method("Float", "sqrt", "(Float) -> Float", lambda f: math.sqrt(f) if f >= 0 else math.nan)
method("Float", "min", "(Float, Float) -> Float", min)
method("Float", "max", "(Float, Float) -> Float", max)
method("Float", "is_nan", "(Float) -> Bool", math.isnan)
method("Float", "to_int", "(Float) -> Int", _to_int)
method("Float", "to_str", "(Float) -> Str", display)
method("Bool", "to_str", "(Bool) -> Str", display)

# ------------------------------------------------------------------ Str
_INT_RE = re.compile(r"[+-]?[0-9]+")
_FLOAT_RE = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")


def _str_to_int(s: str) -> Any:
    s = s.strip()
    if _INT_RE.fullmatch(s):
        n = int(s)
        if INT_MIN <= n <= INT_MAX:
            return some(n)
    return NONE


def _str_to_float(s: str) -> Any:
    s = s.strip()
    return some(float(s)) if _FLOAT_RE.fullmatch(s) else NONE


def _str_slice(vm: Any, s: str, a: int, b: int) -> str:
    if a < 0 or b > len(s) or a > b:
        raise VexPanic(f"slice {a}..{b} out of range for string of length {len(s)}")
    vm.charge(b - a)
    return s[a:b]


def _replace(vm: Any, s: str, a: str, b: str) -> str:
    if a == "":
        raise VexPanic("`replace` pattern must not be empty")
    vm.charge(len(s) + len(a))
    vm.charge(s.count(a) * max(0, len(b) - len(a)))
    return s.replace(a, b)


def _repeat(vm: Any, s: str, n: int) -> str:
    if n < 0:
        raise VexPanic("negative repeat count")
    if len(s) * n > 50_000_000:
        raise VexPanic("string too large")
    vm.charge(len(s) * n)
    return s * n


def _split(s: str, sep: str) -> list[str]:
    return list(s) if sep == "" else s.split(sep)


def _char_at(s: str, i: int) -> Any:
    return some(s[i]) if 0 <= i < len(s) else NONE


def _index_of(s: str, t: str) -> Any:
    i = s.find(t)
    return NONE if i < 0 else some(i)


def _code(s: str) -> int:
    if not s:
        raise VexPanic("`code` of an empty string")
    return ord(s[0])


method("Str", "len", "(Str) -> Int", len)
method("Str", "is_empty", "(Str) -> Bool", lambda s: not s)
method("Str", "upper", "(Str) -> Str", _by_input(str.upper), needs_vm=True)
method("Str", "lower", "(Str) -> Str", _by_input(str.lower), needs_vm=True)
method("Str", "trim", "(Str) -> Str", _by_input(str.strip), needs_vm=True)
method("Str", "contains", "(Str, Str) -> Bool", _by_input(lambda s, t: t in s), needs_vm=True)
method("Str", "starts_with", "(Str, Str) -> Bool", _by_input(str.startswith), needs_vm=True)
method("Str", "ends_with", "(Str, Str) -> Bool", _by_input(str.endswith), needs_vm=True)
method("Str", "replace", "(Str, Str, Str) -> Str", _replace, needs_vm=True)
method("Str", "split", "(Str, Str) -> List[Str]", _by_input(_split), needs_vm=True)
method("Str", "lines", "(Str) -> List[Str]", _by_input(str.splitlines), needs_vm=True)
method("Str", "chars", "(Str) -> List[Str]", _by_input(list, factor=2), needs_vm=True)
method("Str", "repeat", "(Str, Int) -> Str", _repeat, needs_vm=True)
method("Str", "to_int", "(Str) -> Option[Int]", _by_input(_str_to_int), needs_vm=True)
method("Str", "to_float", "(Str) -> Option[Float]", _by_input(_str_to_float), needs_vm=True)
method("Str", "slice", "(Str, Int, Int) -> Str", _str_slice, needs_vm=True)
method("Str", "char_at", "(Str, Int) -> Option[Str]", _char_at)
method("Str", "index_of", "(Str, Str) -> Option[Int]", _by_input(_index_of), needs_vm=True)
method("Str", "code", "(Str) -> Int", _code)
method("Str", "to_str", "(Str) -> Str", lambda s: s)


# ------------------------------------------------------------------ List
def _pop(xs: list[Any]) -> Any:
    return some(xs.pop()) if xs else NONE


def _get(xs: list[Any], i: int) -> Any:
    return some(xs[i]) if 0 <= i < len(xs) else NONE


def _list_slice(vm: Any, xs: list[Any], a: int, b: int) -> list[Any]:
    if a < 0 or b > len(xs) or a > b:
        raise VexPanic(f"slice {a}..{b} out of range for list of length {len(xs)}")
    vm.charge(b - a)
    return xs[a:b]


def _insert(xs: list[Any], i: int, x: Any) -> None:
    if i < 0 or i > len(xs):
        raise VexPanic(f"insert index {i} out of range for list of length {len(xs)}")
    xs.insert(i, x)


def _remove_at(xs: list[Any], i: int) -> Any:
    if i < 0 or i >= len(xs):
        raise VexPanic(f"remove_at index {i} out of range for list of length {len(xs)}")
    return xs.pop(i)


def _find(vm: Any, xs: list[Any], f: Any) -> Any:
    for x in xs:
        if vm.call(f, [x]):
            return some(x)
    return NONE


def _fold(vm: Any, xs: list[Any], init: Any, f: Any) -> Any:
    acc = init
    for x in xs:
        acc = vm.call(f, [acc, x])
    return acc


def _index_of_list(vm: Any, xs: list[Any], x: Any) -> Any:
    vm.charge(len(xs))
    for i, y in enumerate(xs):
        if vm.equal(y, x):
            return some(i)
    return NONE


def _contains(vm: Any, xs: list[Any], x: Any) -> bool:
    vm.charge(len(xs))
    if type(x) in (int, float, bool):
        return x in xs
    return any(y is x or vm.equal(y, x) for y in xs)


def _sort_cost(vm: Any, xs: list[Any]) -> None:
    n = len(xs)
    vm.charge(n)
    levels = n.bit_length()
    if xs and type(xs[0]) not in (int, float, bool):
        vm.charge(charge_size(xs, vm.charge) * levels)
    else:
        vm.charge(n * levels)


def _sort(vm: Any, xs: list[Any]) -> None:
    _sort_cost(vm, xs)
    xs.sort()


def _sorted(vm: Any, xs: list[Any]) -> list[Any]:
    _sort_cost(vm, xs)
    return sorted(xs)


def _sum(xs: list[int]) -> int:
    return checked(sum(xs), "sum")


def _join(vm: Any, xs: list[str], sep: str) -> str:
    vm.charge(len(xs))
    vm.charge(sum(map(len, xs)) + len(sep) * len(xs))
    return sep.join(xs)


def _extend(vm: Any, xs: list[Any], ys: list[Any]) -> None:
    vm.charge(len(ys))
    xs.extend(ys)


def _range_to_list(vm: Any, r: range) -> list[int]:
    vm.charge(max(0, r.stop - r.start))
    return list(r)


def _first(xs: list[Any]) -> Any:
    return some(xs[0]) if xs else NONE


def _last(xs: list[Any]) -> Any:
    return some(xs[-1]) if xs else NONE


def _for_each(vm: Any, xs: list[Any], f: Any) -> None:
    for x in _snapshot(vm, xs):
        vm.call(f, [x])


method("List", "len", "[T](List[T]) -> Int", len)
method("List", "is_empty", "[T](List[T]) -> Bool", lambda xs: not xs)
method("List", "push", "[T](List[T], T)", lambda xs, x: xs.append(x), mutates=True)
method("List", "pop", "[T](List[T]) -> Option[T]", _pop, mutates=True)
method("List", "insert", "[T](List[T], Int, T)", _by_input(_insert), mutates=True, needs_vm=True)
method("List", "remove_at", "[T](List[T], Int) -> T", _by_input(_remove_at), mutates=True, needs_vm=True)
method("List", "clear", "[T](List[T])", lambda xs: xs.clear(), mutates=True)
method("List", "extend", "[T](List[T], List[T])", _extend, mutates=True, needs_vm=True)
method("List", "reverse", "[T](List[T])", _by_input(lambda xs: xs.reverse()), mutates=True, needs_vm=True)
method("List", "sort", "[T: Ord](List[T])", _sort, mutates=True, needs_vm=True)
method("List", "sorted", "[T: Ord](List[T]) -> List[T]", _sorted, needs_vm=True)
method("List", "get", "[T](List[T], Int) -> Option[T]", _get)
method("List", "first", "[T](List[T]) -> Option[T]", _first)
method("List", "last", "[T](List[T]) -> Option[T]", _last)
method("List", "contains", "[T: Eq](List[T], T) -> Bool", _contains, needs_vm=True)
method("List", "index_of", "[T: Eq](List[T], T) -> Option[Int]", _index_of_list, needs_vm=True)
method("List", "slice", "[T](List[T], Int, Int) -> List[T]", _list_slice, needs_vm=True)
method("List", "concat", "[T](List[T], List[T]) -> List[T]", _by_input(lambda xs, ys: xs + ys), needs_vm=True)
method("List", "copy", "[T](List[T]) -> List[T]", _by_input(list), needs_vm=True)
method("List", "map", "[T, U](List[T], fn(T) -> U) -> List[U]",
       lambda vm, xs, f: [vm.call(f, [x]) for x in _snapshot(vm, xs)], needs_vm=True)  # fmt: skip
method("List", "filter", "[T](List[T], fn(T) -> Bool) -> List[T]",
       lambda vm, xs, f: [x for x in _snapshot(vm, xs) if vm.call(f, [x])], needs_vm=True)  # fmt: skip
method("List", "fold", "[T, U](List[T], U, fn(U, T) -> U) -> U", _fold, needs_vm=True)
method("List", "any", "[T](List[T], fn(T) -> Bool) -> Bool",
       lambda vm, xs, f: any(vm.call(f, [x]) for x in _snapshot(vm, xs)), needs_vm=True)  # fmt: skip
method("List", "all", "[T](List[T], fn(T) -> Bool) -> Bool",
       lambda vm, xs, f: all(vm.call(f, [x]) for x in _snapshot(vm, xs)), needs_vm=True)  # fmt: skip
method("List", "find", "[T](List[T], fn(T) -> Bool) -> Option[T]", _find, needs_vm=True)
method("List", "for_each", "[T](List[T], fn(T) -> Unit)", _for_each, needs_vm=True)
method("List", "enumerate", "[T](List[T]) -> List[(Int, T)]", _by_input(lambda xs: list(enumerate(xs)), factor=3), needs_vm=True)
method("List", "zip", "[T, U](List[T], List[U]) -> List[(T, U)]", _by_input(lambda xs, ys: list(zip(xs, ys)), factor=3), needs_vm=True)
method("List", "sum", "(List[Int]) -> Int", _by_input(_sum), needs_vm=True)
method("List", "sum_float", "(List[Float]) -> Float", _by_input(lambda xs: math.fsum(xs)), needs_vm=True)
method("List", "join", "(List[Str], Str) -> Str", _join, needs_vm=True)


# ------------------------------------------------------------------ Map
def _map_get(m: dict[Any, Any], k: Any) -> Any:
    return some(m[k]) if k in m else NONE


def _map_remove(m: dict[Any, Any], k: Any) -> Any:
    return some(m.pop(k)) if k in m else NONE


method("Map", "len", "[K, V](Map[K, V]) -> Int", len)
method("Map", "is_empty", "[K, V](Map[K, V]) -> Bool", lambda m: not m)
method("Map", "get", "[K, V](Map[K, V], K) -> Option[V]", _by_key(_map_get), needs_vm=True)
method("Map", "insert", "[K, V](Map[K, V], K, V)", _by_key(lambda m, k, v: m.__setitem__(k, v)), mutates=True, needs_vm=True)
method("Map", "remove", "[K, V](Map[K, V], K) -> Option[V]", _by_key(_map_remove), mutates=True, needs_vm=True)
method("Map", "clear", "[K, V](Map[K, V])", lambda m: m.clear(), mutates=True)
method("Map", "contains_key", "[K, V](Map[K, V], K) -> Bool", _by_key(lambda m, k: k in m), needs_vm=True)
method("Map", "keys", "[K, V](Map[K, V]) -> List[K]", _by_input(lambda m: list(m.keys())), needs_vm=True)
method("Map", "values", "[K, V](Map[K, V]) -> List[V]", _by_input(lambda m: list(m.values())), needs_vm=True)
method("Map", "entries", "[K, V](Map[K, V]) -> List[(K, V)]", _by_input(lambda m: list(m.items()), factor=3), needs_vm=True)


# ------------------------------------------------------------------ Option / Result
def _unwrap_opt(o: Any) -> Any:
    if o.tag == 1:
        raise VexPanic("called `unwrap()` on a `None` value")
    return o.f[0]


def _expect_opt(o: Any, msg: str) -> Any:
    if o.tag == 1:
        raise VexPanic(msg)
    return o.f[0]


def _unwrap_res(vm: Any, r: Any) -> Any:
    if r.tag == 1:
        raise VexPanic(f"called `unwrap()` on an `Err` value: {display(r.f[0], False, vm.charge)}")
    return r.f[0]


def _expect_res(vm: Any, r: Any, msg: str) -> Any:
    if r.tag == 1:
        raise VexPanic(f"{msg}: {display(r.f[0], False, vm.charge)}")
    return r.f[0]


def _unwrap_err(vm: Any, r: Any) -> Any:
    if r.tag == 0:
        raise VexPanic(f"called `unwrap_err()` on an `Ok` value: {display(r.f[0], False, vm.charge)}")
    return r.f[0]


method("Option", "is_some", "[T](Option[T]) -> Bool", lambda o: o.tag == 0)
method("Option", "is_none", "[T](Option[T]) -> Bool", lambda o: o.tag == 1)
method("Option", "unwrap", "[T](Option[T]) -> T", _unwrap_opt)
method("Option", "expect", "[T](Option[T], Str) -> T", _expect_opt)
method("Option", "unwrap_or", "[T](Option[T], T) -> T", lambda o, d: d if o.tag == 1 else o.f[0])
method("Option", "map", "[T, U](Option[T], fn(T) -> U) -> Option[U]",
       lambda vm, o, f: o if o.tag == 1 else some(vm.call(f, [o.f[0]])), needs_vm=True)  # fmt: skip
method("Option", "and_then", "[T, U](Option[T], fn(T) -> Option[U]) -> Option[U]",
       lambda vm, o, f: o if o.tag == 1 else vm.call(f, [o.f[0]]), needs_vm=True)  # fmt: skip
method("Option", "ok_or", "[T, E](Option[T], E) -> Result[T, E]",
       lambda o, e: err(e) if o.tag == 1 else ok(o.f[0]))  # fmt: skip
method("Result", "is_ok", "[T, E](Result[T, E]) -> Bool", lambda r: r.tag == 0)
method("Result", "is_err", "[T, E](Result[T, E]) -> Bool", lambda r: r.tag == 1)
method("Result", "unwrap", "[T, E](Result[T, E]) -> T", _unwrap_res, needs_vm=True)
method("Result", "expect", "[T, E](Result[T, E], Str) -> T", _expect_res, needs_vm=True)
method("Result", "unwrap_err", "[T, E](Result[T, E]) -> E", _unwrap_err, needs_vm=True)
method("Result", "unwrap_or", "[T, E](Result[T, E], T) -> T", lambda r, d: d if r.tag == 1 else r.f[0])
method("Result", "ok", "[T, E](Result[T, E]) -> Option[T]", lambda r: NONE if r.tag == 1 else some(r.f[0]))
method("Result", "map", "[T, E, U](Result[T, E], fn(T) -> U) -> Result[U, E]",
       lambda vm, r, f: r if r.tag == 1 else ok(vm.call(f, [r.f[0]])), needs_vm=True)  # fmt: skip
method("Result", "map_err", "[T, E, F](Result[T, E], fn(E) -> F) -> Result[T, F]",
       lambda vm, r, f: r if r.tag == 0 else err(vm.call(f, [r.f[0]])), needs_vm=True)  # fmt: skip
method("Result", "and_then", "[T, E, U](Result[T, E], fn(T) -> Result[U, E]) -> Result[U, E]",
       lambda vm, r, f: r if r.tag == 1 else vm.call(f, [r.f[0]]), needs_vm=True)  # fmt: skip

# ------------------------------------------------------------------ Range
method("Range", "to_list", "(Range) -> List[Int]", _range_to_list, needs_vm=True)
method("Range", "len", "(Range) -> Int", len)
method("Range", "contains", "(Range, Int) -> Bool", lambda r, x: x in r)


# ------------------------------------------------------------------ math / io / env
def _wrap1(fn: Callable[[float], float]) -> Callable[[float], float]:
    def go(x: float) -> float:
        try:
            return fn(x)
        except ValueError:
            return math.nan
        except OverflowError:
            return math.inf

    return go


def _mpow(a: float, b: float) -> float:
    try:
        return math.pow(a, b)
    except ValueError:
        return math.nan
    except OverflowError:
        return math.inf


def _gcd(a: int, b: int) -> int:
    return math.gcd(a, b)


def _ln(x: float) -> float:
    if x == 0:
        return -math.inf
    return math.log(x) if x > 0 else math.nan


const("math", "pi", FLOAT, math.pi)
const("math", "e", FLOAT, math.e)
const("math", "tau", FLOAT, math.tau)
const("math", "INT_MAX", INT, INT_MAX)
const("math", "INT_MIN", INT, INT_MIN)
for _n, _f in [("sqrt", lambda x: math.sqrt(x)), ("sin", math.sin), ("cos", math.cos), ("tan", math.tan),
               ("atan", math.atan), ("exp", math.exp), ("log10", math.log10)]:  # fmt: skip
    ns("math", _n, "(Float) -> Float", _wrap1(_f))
ns("math", "ln", "(Float) -> Float", _ln)
ns("math", "pow", "(Float, Float) -> Float", _mpow)
ns("math", "atan2", "(Float, Float) -> Float", math.atan2)
ns("math", "gcd", "(Int, Int) -> Int", _gcd)
ns("math", "hypot", "(Float, Float) -> Float", math.hypot)


# Input of unknown size is read and charged in pieces of this many characters.
_IO_CHUNK = 65_536


def _read_file(vm: Any, path: str) -> Any:
    vm.charge(len(path))
    try:
        with open(path, encoding="utf-8") as fh:
            parts: list[str] = []
            while True:
                part = fh.read(_IO_CHUNK)
                vm.charge(len(part))
                parts.append(part)
                if len(part) < _IO_CHUNK:
                    return ok("".join(parts))
    except (OSError, UnicodeDecodeError) as e:
        return err(f"{path}: {e.strerror if isinstance(e, OSError) and e.strerror else e}")


def _write_file(vm: Any, path: str, text: str) -> Any:
    vm.charge(len(path) + len(text))
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return ok(None)
    except OSError as e:
        return err(f"{path}: {e.strerror or e}")


def _read_line(vm: Any) -> Any:
    parts: list[str] = []
    while True:
        part = sys.stdin.readline(_IO_CHUNK)
        vm.charge(len(part))
        parts.append(part)
        if len(part) < _IO_CHUNK or part.endswith("\n"):
            break
    line = "".join(parts)
    return NONE if line == "" else some(line.rstrip("\r\n"))


def _env_get(name: str) -> Any:
    v = os.environ.get(name)
    return NONE if v is None else some(v)


ns("io", "read_file", "(Str) -> Result[Str, Str]", _read_file, needs_vm=True)
ns("io", "write_file", "(Str, Str) -> Result[Unit, Str]", _write_file, needs_vm=True)
ns("io", "read_line", "() -> Option[Str]", _read_line, needs_vm=True)
ns("env", "get", "(Str) -> Option[Str]", _by_input(_env_get), needs_vm=True)
# env.args needs the VM (program arguments), registered with needs_vm below.
_reg(NAMESPACES["env"], "env.args", "args", "() -> List[Str]", lambda vm: list(vm.args), needs_vm=True)

# Which Vex type constructor names own which method tables.
METHOD_HEADS = {"Int", "Float", "Bool", "Str", "List", "Map", "Option", "Result", "Range"}

# The scope seen by programs that are not embedded: everything.
FULL_SCOPE = Scope(GLOBALS, NAMESPACES)
