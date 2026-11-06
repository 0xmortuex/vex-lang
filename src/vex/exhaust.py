"""Exhaustiveness and reachability of patterns (Maranget's matrix algorithm)."""

from __future__ import annotations

from typing import Union

from vex import syntax as ast
from vex.symbols import EnumInfo, StructInfo


class Ctor:
    """A pattern constructor. ``family`` lists every sibling for finite types, None for ints/strs."""

    def __init__(self, key: object, arity: int, name: str, kind: str, family: Family | None) -> None:
        self.key = key
        self.arity = arity
        self.name = name
        self.kind = kind
        self.family = family

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Ctor) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)


class Family:
    def __init__(self) -> None:
        self.ctors: list[Ctor] = []


class SWild:
    pass


class SCtor:
    def __init__(self, ctor: Ctor, args: list[SPat]) -> None:
        self.ctor = ctor
        self.args = args


class SOr:
    def __init__(self, alts: list[SPat]) -> None:
        self.alts = alts


SPat = Union[SWild, SCtor, SOr]
WILD = SWild()

_enum_families: dict[int, Family] = {}
_BOOL_FAMILY = Family()
_BOOL_FAMILY.ctors = [Ctor(("b", True), 0, "true", "bool", _BOOL_FAMILY), Ctor(("b", False), 0, "false", "bool", _BOOL_FAMILY)]
_UNIT_FAMILY = Family()
_UNIT_FAMILY.ctors = [Ctor("unit", 0, "()", "unit", _UNIT_FAMILY)]


def _enum_family(info: EnumInfo) -> Family:
    fam = _enum_families.get(id(info))
    if fam is None:
        fam = Family()
        for v in info.variants:
            fam.ctors.append(Ctor(("v", info.name, v.tag), len(v.fields), f"{info.name}.{v.name}", "variant", fam))
        _enum_families[id(info)] = fam
    return fam


def to_spat(p: ast.Pat) -> SPat:
    if isinstance(p, (ast.PWild, ast.PBind)):
        return WILD
    if isinstance(p, ast.PLit):
        v = p.value
        if isinstance(v, ast.BoolLit):
            return SCtor(_BOOL_FAMILY.ctors[0 if v.value else 1], [])
        if isinstance(v, ast.UnitLit):
            return SCtor(_UNIT_FAMILY.ctors[0], [])
        val = v.value if isinstance(v, (ast.IntLit, ast.FloatLit, ast.StrLit)) else None
        return SCtor(Ctor(("lit", type(val).__name__, val), 0, repr(val), "lit", None), [])
    if isinstance(p, ast.PTuple):
        fam = Family()
        c = Ctor(("tuple", len(p.items)), len(p.items), "tuple", "tuple", fam)
        fam.ctors = [c]
        return SCtor(c, [to_spat(i) for i in p.items])
    if isinstance(p, ast.PCtor):
        if p.variant is None:
            return WILD
        fam = _enum_family(p.variant.enum)
        c = fam.ctors[p.variant.tag]
        args = [to_spat(a) for a in (p.args or [])]
        while len(args) < c.arity:
            args.append(WILD)
        return SCtor(c, args)
    if isinstance(p, ast.PStruct):
        info: StructInfo | None = p.info
        if info is None:
            return WILD
        fam = Family()
        c = Ctor(("struct", info.name), len(info.fields), info.name, "struct", fam)
        fam.ctors = [c]
        sargs: list[SPat] = [WILD] * len(info.fields)
        for fname, sub in p.fields:
            i = info.field_index(fname)
            if i >= 0:
                sargs[i] = to_spat(sub)
        return SCtor(c, sargs)
    assert isinstance(p, ast.POr)
    return SOr([to_spat(a) for a in p.alts])


def show(p: SPat) -> str:
    if isinstance(p, SWild):
        return "_"
    if isinstance(p, SOr):
        return " | ".join(show(a) for a in p.alts)
    c = p.ctor
    if c.kind == "tuple":
        return "(" + ", ".join(show(a) for a in p.args) + ")"
    if c.kind == "struct":
        return f"{c.name} {{ .. }}"
    if c.kind in ("bool", "lit", "unit"):
        return c.name
    if not p.args:
        return c.name
    return f"{c.name}({', '.join(show(a) for a in p.args)})"


Row = list[SPat]


def _expand(rows: list[Row]) -> list[Row]:
    out: list[Row] = []
    for r in rows:
        if r and isinstance(r[0], SOr):
            out.extend(_expand([[a, *r[1:]] for a in r[0].alts]))
        else:
            out.append(r)
    return out


def _specialize(rows: list[Row], c: Ctor) -> list[Row]:
    out: list[Row] = []
    for r in rows:
        h = r[0]
        if isinstance(h, SCtor):
            if h.ctor == c:
                out.append([*h.args, *r[1:]])
        else:
            out.append([*([WILD] * c.arity), *r[1:]])
    return out


def _default(rows: list[Row]) -> list[Row]:
    return [r[1:] for r in rows if isinstance(r[0], SWild)]


def _witness(rows: list[Row], n: int) -> list[SPat] | None:
    """A vector of patterns not matched by any row, or None if the rows are exhaustive."""
    if n == 0:
        return [] if not rows else None
    rows = _expand(rows)
    heads = [r[0] for r in rows if isinstance(r[0], SCtor)]
    if heads:
        fam = heads[0].ctor.family
        present = {h.ctor for h in heads}
        if fam is not None and all(c in present for c in fam.ctors):
            for c in fam.ctors:
                res = _witness(_specialize(rows, c), c.arity + n - 1)
                if res is not None:
                    return [SCtor(c, res[: c.arity]), *res[c.arity :]]
            return None
        res = _witness(_default(rows), n - 1)
        if res is None:
            return None
        if fam is not None:
            missing = next(c for c in fam.ctors if c not in present)
            return [SCtor(missing, [WILD] * missing.arity), *res]
        return [WILD, *res]
    res = _witness(_default(rows), n - 1)
    return None if res is None else [WILD, *res]


def missing_pattern(pats: list[SPat]) -> SPat | None:
    """None if ``pats`` cover every value, otherwise an example of an uncovered one."""
    w = _witness([[p] for p in pats], 1)
    return None if w is None else w[0]


def _useful(rows: list[Row], q: Row) -> bool:
    if not q:
        return not rows
    rows = _expand(rows)
    h = q[0]
    if isinstance(h, SOr):
        return any(_useful(rows, [a, *q[1:]]) for a in h.alts)
    if isinstance(h, SCtor):
        return _useful(_specialize(rows, h.ctor), [*h.args, *q[1:]])
    heads = [r[0] for r in rows if isinstance(r[0], SCtor)]
    if heads:
        fam = heads[0].ctor.family
        present = {x.ctor for x in heads}
        if fam is not None and all(c in present for c in fam.ctors):
            return any(_useful(_specialize(rows, c), [*([WILD] * c.arity), *q[1:]]) for c in fam.ctors)
    return _useful(_default(rows), q[1:])


def arm_reachable(previous: list[SPat], arm: SPat) -> bool:
    return _useful([[p] for p in previous], [arm])
