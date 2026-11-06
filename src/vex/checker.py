"""Type inference and checking: unification-based, with explicit generics and exhaustive matches."""

from __future__ import annotations

from collections.abc import Callable

from vex import syntax as ast
from vex.diagnostics import DiagSink, Span, suggest
from vex.exhaust import SPat, arm_reachable, missing_pattern, to_spat
from vex.exhaust import show as show_pat
from vex.stdlib import METHODS
from vex.symbols import (
    Builtin,
    EnumInfo,
    Env,
    FnSym,
    GlobalVar,
    LocalSym,
    NamespaceSym,
    StructInfo,
    VariantInfo,
)
from vex.vtypes import (
    BOOL,
    ERROR,
    FLOAT,
    INT,
    NEVER,
    RANGE,
    STR,
    UNIT,
    Mismatch,
    TCon,
    TError,
    TFn,
    TNever,
    TParam,
    TTuple,
    TVar,
    Type,
    list_of,
    map_of,
    prune,
    show,
    subst,
    unify,
    walk,
    zonk,
)

ARITH = {"-", "*", "/", "%"}
CMP = {"<", "<=", ">", ">="}


def _is(t: Type, name: str) -> bool:
    t = prune(t)
    return isinstance(t, TCon) and t.name == name


def _head(t: Type) -> str | None:
    t = prune(t)
    return t.name if isinstance(t, TCon) else None


def satisfies(t: Type, bound: str) -> bool:
    t = prune(t)
    if isinstance(t, (TError, TNever, TVar)):
        return True
    if bound == "Eq":
        if isinstance(t, TParam):
            return bool({"Eq", "Ord", "Hash"} & set(t.bounds))
        if isinstance(t, TFn):
            return False
        if isinstance(t, TCon):
            if t.name in ("Range",):
                return True
            return all(satisfies(a, "Eq") for a in t.args)
        if isinstance(t, TTuple):
            return all(satisfies(a, "Eq") for a in t.items)
        return True
    if bound == "Ord":
        if isinstance(t, TParam):
            return "Ord" in t.bounds
        if isinstance(t, TCon):
            if t.name in ("Int", "Float", "Str", "Bool", "Unit"):
                return True
            if t.name == "List":
                return satisfies(t.args[0], "Ord")
            return False
        if isinstance(t, TTuple):
            return all(satisfies(a, "Ord") for a in t.items)
        return False
    if bound == "Hash":
        if isinstance(t, TParam):
            return "Hash" in t.bounds
        if isinstance(t, TCon):
            return t.name in ("Int", "Str", "Bool", "Unit")
        if isinstance(t, TTuple):
            return all(satisfies(a, "Hash") for a in t.items)
        return False
    return True


def value_span(e: ast.Expr) -> Span:
    """The span that best locates the value an expression produces (the tail of blocks)."""
    while isinstance(e, ast.Block) and e.tail is not None:
        e = e.tail
    return e.span


def hint_for(expected: Type, actual: Type) -> str | None:
    e, a = prune(expected), prune(actual)
    if _is(e, "Float") and _is(a, "Int"):
        return "convert with `.to_float()`; Vex never mixes Int and Float implicitly"
    if _is(e, "Int") and _is(a, "Float"):
        return "convert with `.to_int()` (it truncates toward zero)"
    if _is(e, "Str") and isinstance(a, TCon) and a.name in ("Int", "Float", "Bool"):
        return "use string interpolation (`\"{x}\"`) or `.to_str()`"
    if _is(e, "Unit") and not _is(a, "Unit"):
        return "if you meant to discard the value, end the statement with `;`"
    if isinstance(e, TCon) and e.name == "Option" and not _is(a, "Option") and not isinstance(a, TVar):
        return "wrap the value in `Some(...)`"
    if isinstance(e, TCon) and e.name == "Result" and not _is(a, "Result") and not isinstance(a, TVar):
        return "wrap the value in `Ok(...)` or `Err(...)`"
    if isinstance(a, TCon) and a.name in ("Option", "Result") and not isinstance(e, TCon):
        return "unwrap it with `?`, `.unwrap()` or a `match`"
    if isinstance(a, TCon) and a.name in ("Option", "Result") and isinstance(e, TCon) and e.name != a.name:
        return "unwrap it with `?`, `.unwrap()` or a `match`"
    return None


class Deferred:
    def __init__(self, kind: str, ty: Type, span: Span, extra: str = "") -> None:
        self.kind = kind
        self.ty = ty
        self.span = span
        self.extra = extra


class Checker:
    def __init__(self, env: Env, sink: DiagSink) -> None:
        self.env = env
        self.sink = sink
        self.exprs: list[ast.Expr] = []
        self.deferred: list[Deferred] = []
        self.ret_stack: list[Type] = []
        self.cur: FnSym | None = None
        self.callees: set[int] = set()
        self.errors_before = 0

    # ------------------------------------------------------------ driver
    def check_all(self, fns: list[FnSym]) -> None:
        for fn in fns:
            self.ensure_checked(fn)

    def ensure_checked(self, fn: FnSym) -> None:
        if fn.state == 0:
            self.check_fn(fn)

    def err(self, code: str, msg: str, span: Span | None, label: str = "", help: str | None = None,
            secondary: list[tuple[Span, str]] | None = None, notes: list[str] | None = None) -> None:  # fmt: skip
        self.sink.error(code, msg, span, label, help=help, secondary=secondary, notes=notes)

    def check_fn(self, fn: FnSym) -> None:
        saved = (self.exprs, self.deferred, self.ret_stack, self.cur, self.errors_before)
        self.exprs, self.deferred, self.ret_stack, self.cur = [], [], [], fn
        fn.state = 1
        self.errors_before = len(self.sink.errors)
        try:
            assert fn.ret is not None
            self.ret_stack.append(fn.ret)
            body = fn.decl.body
            t = self.infer_block(body, fn.ret)
            self.unify_block_result(body, t, fn.ret, fn)
            self.finish()
        finally:
            fn.state = 2
            self.exprs, self.deferred, self.ret_stack, self.cur, self.errors_before = saved

    def unify_block_result(self, body: ast.Block, t: Type, ret: Type, fn: FnSym) -> None:
        try:
            unify(ret, t)
        except Mismatch:
            end = Span(body.span.source, body.span.end - 1, body.span.end)
            if body.tail is None:
                help = None
                if body.stmts and isinstance(body.stmts[-1], ast.ExprStmt):
                    last = body.stmts[-1].expr
                    if last.ty is not None and self.compatible(ret, last.ty):
                        help = "remove the trailing `;` so the last expression becomes the result"
                self.err("type-mismatch", "mismatched types", end,
                         f"function `{fn.name}` must return `{show(ret)}` but its body has no final expression",
                         help=help)  # fmt: skip
            else:
                self.mismatch(ret, t, body.tail.span, note=f"in the return value of `{fn.name}`")

    def compatible(self, a: Type, b: Type) -> bool:
        """Would unify succeed? (does not leave bindings behind for concrete mismatches)"""
        from vex.vtypes import TVar as _TV

        def snapshot(t: Type, acc: list[_TV]) -> None:
            for x in walk(t):
                if isinstance(x, _TV):
                    acc.append(x)

        vs: list[_TV] = []
        snapshot(a, vs)
        snapshot(b, vs)
        try:
            unify(a, b)
            ok = True
        except Mismatch:
            ok = False
        for v in vs:
            v.ref = None
        return ok

    # ------------------------------------------------------------ finishing a function
    def finish(self) -> None:
        for d in self.deferred:
            self.check_deferred(d)
        reported = False
        for e in sorted(self.exprs, key=lambda x: (x.span.start, -x.span.end)):
            if e.ty is None:
                continue
            z = zonk(e.ty)
            for x in walk(z):
                if isinstance(x, TCon) and x.name == "Map":
                    k = prune(x.args[0])
                    if not satisfies(k, "Hash") or isinstance(k, TParam) and "Hash" not in k.bounds:
                        self.err("bad-map-key", "Map keys must be Int, Str, Bool or tuples of those", e.span,
                                 f"key type `{show(k)}` is not allowed")  # fmt: skip
                        break
            if not reported and len(self.sink.errors) == self.errors_before and any(
                isinstance(x, TVar) for x in walk(z)
            ):
                reported = True
                self.err("cannot-infer", "cannot infer the type of this expression", e.span,
                         f"type so far: `{show(z)}`",
                         help="add a type annotation, e.g. `let xs: List[Int] = [];`")  # fmt: skip

    def check_deferred(self, d: Deferred) -> None:
        t = zonk(d.ty)
        if isinstance(prune(t), TVar):
            return
        k = d.kind
        if k == "num":
            ok = _is(t, "Int") or _is(t, "Float") or isinstance(prune(t), (TError, TNever))
            if not ok:
                self.err("bad-operand", f"operator `{d.extra}` cannot be applied to `{show(t)}`", d.span,
                         "expected Int or Float")  # fmt: skip
        elif k == "add":
            ok = any(_is(t, n) for n in ("Int", "Float", "Str")) or isinstance(prune(t), (TError, TNever))
            if not ok:
                self.err("bad-operand", f"operator `+` cannot be applied to `{show(t)}`", d.span,
                         "expected Int, Float or Str")  # fmt: skip
        elif k.startswith("bound:"):
            b = k[6:]
            if not satisfies(t, b):
                self.err("unsatisfied-bound", f"type `{show(t)}` does not satisfy the bound `{b}`", d.span,
                         f"required by `{d.extra}`" if d.extra else "")  # fmt: skip
        elif k == "eq":
            if not satisfies(t, "Eq"):
                self.err("bad-operand", f"cannot compare values of type `{show(t)}` with `{d.extra}`", d.span,
                         "functions cannot be compared" if any(isinstance(x, TFn) for x in walk(t))
                         else "this type has no equality here (declare `T: Eq`)")  # fmt: skip
        elif k == "ord":
            if not satisfies(t, "Ord"):
                self.err("bad-operand", f"cannot order values of type `{show(t)}` with `{d.extra}`", d.span,
                         "only Int, Float, Str, Bool, tuples and lists of those can be ordered")  # fmt: skip

    # ------------------------------------------------------------ reporting helpers
    def mismatch(self, expected: Type, actual: Type, span: Span, note: str | None = None,
                 secondary: list[tuple[Span, str]] | None = None) -> None:  # fmt: skip
        e, a = prune(expected), prune(actual)
        if isinstance(e, TError) or isinstance(a, TError):
            return
        self.err("type-mismatch", "mismatched types", span,
                 f"expected `{show(e)}`, found `{show(a)}`", help=hint_for(e, a),
                 notes=[note] if note else None, secondary=secondary)  # fmt: skip

    def unify_at(self, expected: Type, actual: Type, span: Span, note: str | None = None,
                 secondary: list[tuple[Span, str]] | None = None) -> bool:  # fmt: skip
        try:
            unify(expected, actual)
            return True
        except Mismatch as m:
            if m.infinite:
                self.err("infinite-type", "this would create an infinite type", span,
                         f"`{show(expected)}` would contain itself")  # fmt: skip
            else:
                self.mismatch(expected, actual, span, note, secondary)
            return False

    def expect(self, e: ast.Expr, ty: Type, note: str | None = None,
               secondary: list[tuple[Span, str]] | None = None) -> Type:  # fmt: skip
        t = self.infer(e, ty)
        self.unify_at(ty, t, e.span, note, secondary)
        return t

    def join(self, acc: Type | None, t: Type, span: Span, what: str) -> Type:
        if acc is None:
            return t
        if isinstance(prune(acc), TNever):
            return t
        if isinstance(prune(t), TNever):
            return acc
        try:
            unify(acc, t)
        except Mismatch:
            self.err("type-mismatch", f"{what} have incompatible types", span,
                     f"expected `{show(acc)}`, found `{show(t)}`", help=hint_for(acc, t))  # fmt: skip
            return ERROR
        return acc

    # ------------------------------------------------------------ expressions
    def infer(self, e: ast.Expr, expected: Type | None = None) -> Type:
        t = self.infer_inner(e, expected)
        e.ty = t
        self.exprs.append(e)
        return t

    def infer_inner(self, e: ast.Expr, expected: Type | None) -> Type:
        if isinstance(e, ast.IntLit):
            return INT
        if isinstance(e, ast.FloatLit):
            return FLOAT
        if isinstance(e, ast.BoolLit):
            return BOOL
        if isinstance(e, ast.StrLit):
            return STR
        if isinstance(e, ast.UnitLit):
            return UNIT
        if isinstance(e, ast.Interp):
            for p in e.parts:
                if not isinstance(p, str):
                    self.infer(p)
            return STR
        if isinstance(e, ast.Name):
            return self.infer_name(e)
        if isinstance(e, ast.ListLit):
            return self.infer_list(e, expected)
        if isinstance(e, ast.MapLit):
            return self.infer_map(e, expected)
        if isinstance(e, ast.TupleLit):
            exp_items: list[Type | None] = [None] * len(e.items)
            pe = prune(expected) if expected is not None else None
            if isinstance(pe, TTuple) and len(pe.items) == len(e.items):
                exp_items = list(pe.items)
            return TTuple(tuple(self.infer(i, x) for i, x in zip(e.items, exp_items)))
        if isinstance(e, ast.Unary):
            return self.infer_unary(e)
        if isinstance(e, ast.Binary):
            return self.infer_binary(e)
        if isinstance(e, ast.Call):
            return self.infer_call(e, expected)
        if isinstance(e, ast.Field):
            return self.infer_field(e)
        if isinstance(e, ast.Index):
            return self.infer_index(e)
        if isinstance(e, ast.Try):
            return self.infer_try(e)
        if isinstance(e, ast.Lambda):
            return self.infer_lambda(e, expected)
        if isinstance(e, ast.Block):
            return self.infer_block(e, expected)
        if isinstance(e, ast.If):
            return self.infer_if(e, expected)
        if isinstance(e, ast.Match):
            return self.infer_match(e, expected)
        if isinstance(e, ast.While):
            self.expect(e.cond, BOOL)
            self.infer_block(e.body, None)
            return UNIT
        if isinstance(e, ast.For):
            return self.infer_for(e)
        if isinstance(e, ast.Return):
            ret = self.ret_stack[-1]
            if e.value is None:
                self.unify_at(ret, UNIT, e.span, "this `return` has no value")
            else:
                self.expect(e.value, ret)
            return NEVER
        if isinstance(e, (ast.Break, ast.Continue)):
            return NEVER
        if isinstance(e, ast.RangeExpr):
            self.expect(e.lo, INT)
            self.expect(e.hi, INT)
            return RANGE
        if isinstance(e, ast.StructLit):
            return self.infer_struct_lit(e)
        if isinstance(e, ast.Assign):
            return self.infer_assign(e)
        raise AssertionError(f"unhandled expression {type(e).__name__}")  # pragma: no cover

    # ---- names and symbols
    def instantiate(self, tparams: list[TParam], span: Span, what: str) -> dict[TParam, Type]:
        m: dict[TParam, Type] = {}
        for p in tparams:
            v = TVar()
            m[p] = v
            for b in p.bounds:
                self.deferred.append(Deferred(f"bound:{b}", v, span, what))
        return m

    def sym_type(self, sym: object, span: Span, name: str) -> Type:
        if isinstance(sym, LocalSym):
            if sym.ty is None:
                sym.ty = TVar()
            return sym.ty
        if isinstance(sym, GlobalVar):
            return sym.ty
        if isinstance(sym, FnSym):
            if sym.state == 0 and not sym.declared_ret:
                self.ensure_checked(sym)
            elif sym.state == 1 and not sym.declared_ret and sym.tparams:
                self.err("recursive-inference", f"recursive generic function `{sym.name}` needs a declared return type",
                         span, "return type cannot be inferred here",
                         help=f"add `-> Type` to `fn {sym.name}`")  # fmt: skip
                return ERROR
            assert sym.ret is not None
            m = self.instantiate(sym.tparams, span, sym.qualname) if sym.tparams else {}
            return subst(TFn(tuple(sym.params), sym.ret), m)
        if isinstance(sym, VariantInfo):
            en = sym.enum
            m = {p: TVar() for p in en.tparams}
            ety = TCon(en.name, tuple(m[p] for p in en.tparams))
            if sym.fields:
                return TFn(tuple(subst(f, m) for f in sym.fields), ety)
            return ety
        if isinstance(sym, Builtin):
            m = self.instantiate(sym.tparams, span, sym.qual) if sym.tparams else {}
            return subst(sym.ty, m)
        if isinstance(sym, NamespaceSym):
            self.err("not-a-value", f"module `{sym.name}` is not a value", span,
                     help=f"access a member, e.g. `{sym.name}.{'sqrt' if sym.name == 'math' else 'args'}`")  # fmt: skip
            return ERROR
        if isinstance(sym, StructInfo):
            self.err("not-a-value", f"`{sym.name}` is a type, not a value", span,
                     help=f"construct one with `{sym.name} {{ ... }}`")  # fmt: skip
            return ERROR
        if isinstance(sym, EnumInfo):
            self.err("not-a-value", f"`{sym.name}` is a type, not a value", span,
                     help=f"use a variant such as `{sym.name}.{sym.variants[0].name if sym.variants else 'Variant'}`")  # fmt: skip
            return ERROR
        return ERROR

    def is_aggregate(self, t: Type) -> bool:
        t = prune(t)
        return isinstance(t, TCon) and (t.name in ("List", "Map") or t.name in self.env.structs)

    def check_value_use(self, sym: object, e: ast.Expr, name: str) -> None:
        if isinstance(sym, FnSym) and any(sym.mut_params) and id(e) not in self.callees:
            self.err("mut-fn-value", f"function `{name}` has `mut` parameters and cannot be used as a value", e.span,
                     "call it directly instead")  # fmt: skip

    def infer_name(self, e: ast.Name) -> Type:
        if e.sym is None:
            return ERROR
        self.check_value_use(e.sym, e, e.name)
        return self.sym_type(e.sym, e.span, e.name)

    # ---- literals
    def infer_list(self, e: ast.ListLit, expected: Type | None) -> Type:
        pe = prune(expected) if expected is not None else None
        elem: Type = pe.args[0] if isinstance(pe, TCon) and pe.name == "List" else TVar()
        for item in e.items:
            t = self.infer(item, elem)
            self.unify_at(elem, t, item.span, "all list elements must have the same type")
        return list_of(elem)

    def infer_map(self, e: ast.MapLit, expected: Type | None) -> Type:
        pe = prune(expected) if expected is not None else None
        if isinstance(pe, TCon) and pe.name == "Map":
            k, v = pe.args
        else:
            k, v = TVar(), TVar()
        for ke, ve in e.pairs:
            self.unify_at(k, self.infer(ke, k), ke.span, "all map keys must have the same type")
            self.unify_at(v, self.infer(ve, v), ve.span, "all map values must have the same type")
        return map_of(k, v)

    # ---- operators
    def infer_unary(self, e: ast.Unary) -> Type:
        if e.op == "!":
            self.expect(e.operand, BOOL)
            return BOOL
        t = self.infer(e.operand)
        self.deferred.append(Deferred("num", t, e.span, "-"))
        return t

    def operand_mismatch(self, e: ast.Binary, lt: Type, rt: Type) -> None:
        help = None
        if {_head(lt), _head(rt)} == {"Int", "Float"}:
            help = "Vex never mixes Int and Float; convert with `.to_float()` or `.to_int()`"
        elif e.op == "+" and (_is(lt, "Str") or _is(rt, "Str")):
            help = "build strings with interpolation: `\"{a}{b}\"`"
        self.err("type-mismatch", f"mismatched types for `{e.op}`", e.span,
                 f"left is `{show(lt)}`, right is `{show(rt)}`", help=help)  # fmt: skip

    def infer_binary(self, e: ast.Binary) -> Type:
        op = e.op
        if op in ("&&", "||"):
            self.expect(e.left, BOOL)
            self.expect(e.right, BOOL)
            return BOOL
        lt = self.infer(e.left)
        rt = self.infer(e.right, lt)
        try:
            unify(lt, rt)
        except Mismatch:
            if not isinstance(prune(lt), TError) and not isinstance(prune(rt), TError):
                self.operand_mismatch(e, lt, rt)
            return BOOL if op in CMP or op in ("==", "!=") else ERROR
        if op in ("==", "!="):
            self.deferred.append(Deferred("eq", lt, e.span, op))
            return BOOL
        if op in CMP:
            self.deferred.append(Deferred("ord", lt, e.span, op))
            return BOOL
        self.deferred.append(Deferred("add" if op == "+" else "num", lt, e.span, op))
        return lt

    # ---- calls
    def infer_call(self, e: ast.Call, expected: Type | None) -> Type:
        callee = e.callee
        if isinstance(callee, ast.Field) and callee.static is None and not callee.parenthesized:
            return self.infer_method_call(e, callee, expected)
        ct: Type
        target: object = None
        if isinstance(callee, ast.Name):
            self.callees.add(id(callee))
            ct = self.infer(callee)
            target = callee.sym
        elif isinstance(callee, ast.Field):
            self.callees.add(id(callee))
            ct = self.infer(callee)
            target = callee.static
        else:
            ct = self.infer(callee)
        if isinstance(target, (FnSym, Builtin, VariantInfo)):
            e.target = target
        name = callee.name if isinstance(callee, (ast.Name, ast.Field)) else "function value"
        return self.apply(e, ct, e.args, name, 0, expected)

    def apply(self, e: ast.Call, ct: Type, args: list[ast.Expr], name: str, skip: int,
              expected: Type | None) -> Type:  # fmt: skip
        ct = prune(ct)
        if isinstance(ct, TError):
            for a in args:
                self.infer(a)
            return ERROR
        if isinstance(ct, TVar):
            ats = [self.infer(a) for a in args]
            ret = TVar()
            self.unify_at(ct, TFn(tuple(ats), ret), e.span)
            return ret
        if not isinstance(ct, TFn):
            self.err("not-callable", f"cannot call a value of type `{show(ct)}`", e.callee.span,
                     "this is not a function")  # fmt: skip
            for a in args:
                self.infer(a)
            return ERROR
        params = ct.params[skip:]
        if len(params) != len(args):
            self.err("arity-mismatch",
                     f"`{name}` takes {len(params)} argument{'s' if len(params) != 1 else ''} "
                     f"but {len(args)} {'was' if len(args) == 1 else 'were'} supplied", e.span,
                     f"expected {len(params)}, found {len(args)}")  # fmt: skip
            for a in args:
                self.infer(a)
            return ct.ret
        order = sorted(range(len(args)), key=lambda i: isinstance(args[i], ast.Lambda))
        for i in order:
            self.expect(args[i], params[i], note=f"in argument {i + 1} of `{name}`")
        target = e.target
        if isinstance(target, FnSym) and any(target.mut_params):
            for i, a in enumerate(args):
                if target.mut_params[i + skip] and self.is_aggregate(params[i]):
                    self.require_mutable(a, a.span, "pass", f" to the `mut` parameter of `{name}`")
        return ct.ret

    def infer_method_call(self, e: ast.Call, callee: ast.Field, expected: Type | None) -> Type:
        recv = callee.obj
        rt = self.infer(recv)
        prt = prune(rt)
        if isinstance(prt, TError):
            for a in e.args:
                self.infer(a)
            return ERROR
        name = callee.name
        span = callee.name_span or callee.span
        found = self.find_method(prt, name)
        if found is None:
            self.report_no_method(prt, name, span, recv)
            for a in e.args:
                self.infer(a)
            return ERROR
        target, mutating = found
        e.target = target
        e.is_method = True
        if isinstance(target, FnSym):
            self.ensure_checked(target) if not target.declared_ret else None
            if target.state == 1 and not target.declared_ret and target.tparams:
                self.err("recursive-inference", f"recursive generic method `{target.name}` needs a declared return type",
                         span, help=f"add `-> Type` to `fn {target.name}`")  # fmt: skip
                return ERROR
            assert target.ret is not None
            if not target.has_self:
                self.err("not-a-method", f"`{name}` is an associated function, not a method", span,
                         "it takes no `self`", help=f"call it as `{target.owner}.{name}(...)`")  # fmt: skip
                for a in e.args:
                    self.infer(a)
                return ERROR
            m = self.instantiate(target.tparams, span, target.qualname)
            fty = subst(TFn(tuple(target.params), target.ret), m)
            qual = target.qualname
        else:
            m = self.instantiate(target.tparams, span, target.qual)
            fty = subst(target.ty, m)
            qual = target.qual
        assert isinstance(fty, TFn)
        if not self.unify_at(fty.params[0], rt, recv.span, f"receiver of `{qual}`"):
            for a in e.args:
                self.infer(a)
            return ERROR
        if mutating:
            self.require_mutable(recv, span, f"call `{name}` on")
        return self.apply(e, fty, e.args, name, 1, expected)

    def find_method(self, t: Type, name: str) -> tuple[FnSym | Builtin, bool] | None:
        if isinstance(t, TCon):
            user = self.env.methods.get((t.name, name))
            if user is not None:
                return user, user.self_mode == "mut self"
            b = METHODS.get(t.name, {}).get(name)
            if b is not None:
                return b, b.mutates
        return None

    def report_no_method(self, t: Type, name: str, span: Span, recv: ast.Expr) -> None:
        if isinstance(t, TVar):
            self.err("cannot-infer", f"cannot call `.{name}()` on a value whose type is not known yet", span,
                     "type must be known at this point",
                     help="annotate the variable or lambda parameter, e.g. `|x: Int| ...`")  # fmt: skip
            return
        pool: list[str] = []
        if isinstance(t, TCon):
            pool = [k[1] for k in self.env.methods if k[0] == t.name] + list(METHODS.get(t.name, {}))
            if isinstance(self.env.structs.get(t.name), StructInfo):
                info = self.env.structs[t.name]
                if info.field_index(name) >= 0:
                    self.err("no-method", f"`{name}` is a field of `{t.name}`, not a method", span,
                             "field", help=f"to call a function stored in a field write `(x.{name})(...)`")  # fmt: skip
                    return
        s = suggest(name, pool)
        self.err("no-method", f"type `{show(t)}` has no method `{name}`", span, "no such method",
                 help=f"did you mean `{s}`?" if s else None)  # fmt: skip

    # ---- fields and indexing
    def infer_field(self, e: ast.Field) -> Type:
        if e.static is not None:
            self.check_value_use(e.static, e, e.name)
            return self.sym_type(e.static, e.span, e.name)
        if isinstance(e.obj, ast.Name) and isinstance(e.obj.sym, (NamespaceSym, StructInfo, EnumInfo)):
            return ERROR  # resolver already reported the unknown member
        t = prune(self.infer(e.obj))
        if isinstance(t, TError):
            return ERROR
        span = e.name_span or e.span
        if isinstance(t, TTuple):
            if e.name.isdigit() and int(e.name) < len(t.items):
                e.index = int(e.name)
                return t.items[e.index]
            self.err("no-field", f"tuple of {len(t.items)} elements has no field `{e.name}`", span,
                     f"valid fields: 0..{len(t.items) - 1}")  # fmt: skip
            return ERROR
        if isinstance(t, TCon):
            info = self.env.structs.get(t.name)
            if info is not None:
                i = info.field_index(e.name)
                if i >= 0:
                    e.index = i
                    m = dict(zip(info.tparams, t.args))
                    return subst(info.fields[i][1], m)
                s = suggest(e.name, [f for f, _ in info.fields])
                meth = (t.name, e.name) in self.env.methods
                self.err("no-field", f"struct `{t.name}` has no field `{e.name}`", span, "no such field",
                         help=(f"`{e.name}` is a method; call it with `.{e.name}()`" if meth
                               else f"did you mean `{s}`?" if s else None))  # fmt: skip
                return ERROR
            if e.name in METHODS.get(t.name, {}) or (t.name, e.name) in self.env.methods:
                self.err("no-field", f"`{e.name}` is a method of `{show(t)}`, not a field", span, "method",
                         help=f"call it: `.{e.name}(...)`")  # fmt: skip
                return ERROR
        if isinstance(t, TVar):
            self.err("cannot-infer", f"cannot access `.{e.name}` on a value whose type is not known yet", span,
                     "type must be known at this point",
                     help="annotate the variable or lambda parameter")  # fmt: skip
            return ERROR
        self.err("no-field", f"type `{show(t)}` has no field `{e.name}`", span, "no such field")
        return ERROR

    def infer_index(self, e: ast.Index) -> Type:
        t = prune(self.infer(e.obj))
        if isinstance(t, TError):
            self.infer(e.index)
            return ERROR
        if isinstance(t, TCon) and t.name == "List":
            self.expect(e.index, INT, note="list indexes are Int")
            return t.args[0]
        if isinstance(t, TCon) and t.name == "Map":
            self.expect(e.index, t.args[0], note="the index must be the map's key type")
            return t.args[1]
        self.infer(e.index)
        if isinstance(t, TVar):
            self.err("cannot-infer", "cannot index a value whose type is not known yet", e.span,
                     help="annotate the variable or lambda parameter")  # fmt: skip
        else:
            help = "use `.char_at(i)` or `.chars()`" if _is(t, "Str") else None
            self.err("not-indexable", f"cannot index into a value of type `{show(t)}`", e.obj.span,
                     "only List and Map can be indexed", help=help)  # fmt: skip
        return ERROR

    def infer_try(self, e: ast.Try) -> Type:
        t = prune(self.infer(e.operand))
        if isinstance(t, TError):
            return ERROR
        ret = prune(self.ret_stack[-1])
        if isinstance(t, TCon) and t.name == "Option":
            if isinstance(ret, TVar):
                unify(ret, TCon("Option", (TVar(),)))
                ret = prune(ret)
            if not (isinstance(ret, TCon) and ret.name == "Option"):
                self.err("bad-try", "`?` on an Option needs the enclosing function to return an Option", e.span,
                         f"enclosing function returns `{show(ret)}`",
                         help="change the return type to `Option[...]` or handle the None with `match`")  # fmt: skip
            return t.args[0]
        if isinstance(t, TCon) and t.name == "Result":
            if isinstance(ret, TVar):
                unify(ret, TCon("Result", (TVar(), t.args[1])))
                ret = prune(ret)
            if not (isinstance(ret, TCon) and ret.name == "Result"):
                self.err("bad-try", "`?` on a Result needs the enclosing function to return a Result", e.span,
                         f"enclosing function returns `{show(ret)}`",
                         help="change the return type to `Result[..., E]` or handle the Err with `match`")  # fmt: skip
            else:
                try:
                    unify(ret.args[1], t.args[1])
                except Mismatch:
                    self.err("bad-try", "`?` cannot convert between different error types", e.span,
                             f"this error is `{show(t.args[1])}`, but the function returns `Result[_, {show(ret.args[1])}]`",
                             help="use `.map_err(...)` to convert the error first")  # fmt: skip
            return t.args[0]
        if isinstance(t, TVar):
            self.err("cannot-infer", "cannot use `?` on a value whose type is not known yet", e.span)
        else:
            self.err("bad-try", f"`?` can only be used on Option or Result, found `{show(t)}`", e.span,
                     "not an Option or Result")  # fmt: skip
        return ERROR

    # ---- lambdas
    def infer_lambda(self, e: ast.Lambda, expected: Type | None) -> Type:
        pe = prune(expected) if expected is not None else None
        exp_params: tuple[Type, ...] | None = None
        exp_ret: Type | None = None
        if isinstance(pe, TFn) and len(pe.params) == len(e.params):
            exp_params, exp_ret = pe.params, pe.ret
        elif isinstance(pe, TFn):
            self.err("arity-mismatch", f"this function expects a closure with {len(pe.params)} parameter"
                     f"{'s' if len(pe.params) != 1 else ''}, but the lambda has {len(e.params)}", e.span,
                     f"expected {len(pe.params)}")  # fmt: skip
        ptypes: list[Type] = []
        for i, p in enumerate(e.params):
            assert p.sym is not None
            s = p.sym
            if s.ty is None:
                s.ty = exp_params[i] if exp_params is not None else TVar()
            elif exp_params is not None:
                self.unify_at(exp_params[i], s.ty, p.span, "lambda parameter annotation")
            ptypes.append(s.ty)
        ret: Type = e.ret_ty or exp_ret or TVar()
        self.ret_stack.append(ret)
        bt = self.infer(e.body, ret)
        self.ret_stack.pop()
        if isinstance(e.body, ast.Block) and e.ret is not None and e.body.tail is None and not isinstance(prune(bt), TNever):
            pass
        self.unify_at(ret, bt, e.body.span, "in the lambda body")
        return TFn(tuple(ptypes), ret)

    # ---- blocks and statements
    def infer_block(self, b: ast.Block, expected: Type | None) -> Type:
        diverged = False
        for st in b.stmts:
            if isinstance(st, ast.Let):
                self.check_let(st)
            else:
                t = self.infer(st.expr)
                if isinstance(prune(t), TNever):
                    diverged = True
        if b.tail is not None:
            t = self.infer(b.tail, expected)
        else:
            t = NEVER if diverged else UNIT
        b.ty = t
        if b.tail is not None and diverged and not isinstance(prune(t), TNever):
            return t
        return t

    def check_let(self, st: ast.Let) -> None:
        t = self.infer(st.init, st.annot)
        vt = t
        if st.annot is not None:
            self.unify_at(st.annot, t, st.init.span, None,
                          secondary=[(st.ty_expr.span, "declared here")] if st.ty_expr else None)  # fmt: skip
            vt = st.annot
        self.check_pat(st.pat, vt)
        w = missing_pattern([to_spat(st.pat)])
        if w is not None:
            self.err("refutable-pattern", "refutable pattern in `let`", st.pat.span,
                     f"pattern `{show_pat(w)}` is not covered",
                     help="use `match` to handle the other cases")  # fmt: skip

    def infer_if(self, e: ast.If, expected: Type | None) -> Type:
        self.expect(e.cond, BOOL, note="the condition of `if` must be Bool")
        tt = self.infer_block(e.then, expected if e.els is not None else None)
        if e.els is None:
            if not isinstance(prune(tt), (TNever, TError)):
                try:
                    unify(UNIT, tt)
                except Mismatch:
                    self.err("type-mismatch", "`if` without `else` must have type Unit", e.then.tail.span if e.then.tail else e.then.span,
                             f"this branch has type `{show(tt)}`", help="add an `else` branch that produces a value of the same type")  # fmt: skip
            return UNIT
        et = self.infer(e.els, expected)
        return self.join(tt, et, value_span(e.els), "`if` and `else` branches")

    def infer_match(self, e: ast.Match, expected: Type | None) -> Type:
        st = self.infer(e.scrut)
        acc: Type | None = None
        for arm in e.arms:
            self.check_pat(arm.pat, st)
            if arm.guard is not None:
                self.expect(arm.guard, BOOL, note="match guards must be Bool")
            bt = self.infer(arm.body, expected if acc is None else acc)
            acc = self.join(acc, bt, value_span(arm.body), "match arms")
        covered = [to_spat(a.pat) for a in e.arms if a.guard is None]
        w = missing_pattern(covered)
        if w is not None:
            self.err("non-exhaustive-match", "non-exhaustive `match`", e.scrut.span,
                     f"pattern `{show_pat(w)}` is not covered",
                     help="add an arm for it, or a catch-all arm `_ => ...`")  # fmt: skip
        prev: list[SPat] = []
        for arm in e.arms:
            sp = to_spat(arm.pat)
            if not arm_reachable(prev, sp):
                self.sink.warning("unreachable-pattern", "unreachable pattern", arm.pat.span,
                                  "this arm can never match: earlier arms cover it")  # fmt: skip
            if arm.guard is None:
                prev.append(sp)
        return acc if acc is not None else NEVER

    def infer_for(self, e: ast.For) -> Type:
        it = prune(self.infer(e.iter))
        elem: Type = ERROR
        if isinstance(it, TCon) and it.name == "Range":
            elem = INT
        elif isinstance(it, TCon) and it.name == "List":
            elem = it.args[0]
        elif isinstance(it, TCon) and it.name == "Map":
            self.err("not-iterable", "cannot iterate over a Map directly", e.iter.span,
                     "Map is not a list", help="use `m.entries()`, `m.keys()` or `m.values()`")  # fmt: skip
        elif isinstance(it, TCon) and it.name == "Str":
            self.err("not-iterable", "cannot iterate over a Str directly", e.iter.span, "Str is not a list",
                     help="use `s.chars()`")  # fmt: skip
        elif isinstance(it, TVar):
            self.err("cannot-infer", "cannot iterate over a value whose type is not known yet", e.iter.span)
        elif not isinstance(it, TError):
            self.err("not-iterable", f"cannot iterate over a value of type `{show(it)}`", e.iter.span,
                     "expected a List or a range like `0..n`")  # fmt: skip
        self.check_pat(e.pat, elem)
        w = missing_pattern([to_spat(e.pat)])
        if w is not None:
            self.err("refutable-pattern", "refutable pattern in `for`", e.pat.span,
                     f"pattern `{show_pat(w)}` is not covered")  # fmt: skip
        self.infer_block(e.body, None)
        return UNIT

    # ---- structs
    def infer_struct_lit(self, e: ast.StructLit) -> Type:
        info = e.info
        if info is None:
            for _, _, v in e.fields:
                self.infer(v)
            return ERROR
        m: dict[TParam, Type] = {p: TVar() for p in info.tparams}
        seen: set[str] = set()
        for fname, fspan, v in e.fields:
            i = info.field_index(fname)
            if i < 0:
                s = suggest(fname, [f for f, _ in info.fields])
                self.err("no-field", f"struct `{info.name}` has no field `{fname}`", fspan, "no such field",
                         help=f"did you mean `{s}`?" if s else None)  # fmt: skip
                self.infer(v)
                continue
            if fname in seen:
                self.err("duplicate-field", f"field `{fname}` is initialized twice", fspan)
            seen.add(fname)
            self.expect(v, subst(info.fields[i][1], m), note=f"in field `{fname}` of `{info.name}`")
        missing = [f for f, _ in info.fields if f not in seen]
        if missing:
            self.err("missing-fields", f"missing field{'s' if len(missing) > 1 else ''} "
                     f"{', '.join('`' + f + '`' for f in missing)} in `{info.name}`", e.span,
                     "incomplete initializer")  # fmt: skip
        return TCon(info.name, tuple(m[p] for p in info.tparams))

    # ---- assignment
    def root_of(self, e: ast.Expr) -> ast.Expr:
        while isinstance(e, (ast.Field, ast.Index)):
            e = e.obj
        return e

    def require_mutable(self, target: ast.Expr, span: Span, verb: str, detail: str = "") -> None:
        root = self.root_of(target)
        if isinstance(root, ast.Name):
            sym = root.sym
            if isinstance(sym, (LocalSym, GlobalVar)) and not sym.mutable:
                what = f"`{sym.name}`"
                if isinstance(sym, LocalSym) and sym.is_self:
                    help = "declare the method with `mut self`"
                elif isinstance(sym, LocalSym) and sym.span is not None and self.is_param(sym):
                    help = "function parameters are immutable"
                else:
                    help = f"declare it with `let mut {sym.name}`"
                self.err("immutable-binding", f"cannot {verb} {what}{detail}: it is not declared `mut`", span,
                         f"{what} is immutable", help=help,
                         secondary=[(sym.span, "declared here")] if isinstance(sym, LocalSym) else None)  # fmt: skip

    def is_param(self, sym: LocalSym) -> bool:
        fn = self.cur
        return bool(fn and any(p.sym is sym for p in fn.decl.params))

    def infer_assign(self, e: ast.Assign) -> Type:
        tgt = e.target
        tt = self.infer(tgt)
        if isinstance(tgt, ast.Name) and not isinstance(tgt.sym, (LocalSym, GlobalVar)):
            if tgt.sym is not None:
                self.err("bad-assign", "cannot assign to this name", tgt.span, "not a variable")
            self.infer(e.value)
            return UNIT
        if isinstance(tgt, ast.Field) and isinstance(prune(tgt.obj.ty or ERROR), TTuple):
            self.err("immutable-binding", "tuples are immutable: cannot assign to a tuple element", tgt.span,
                     help="build a new tuple instead")  # fmt: skip
        else:
            if isinstance(tgt, ast.Name):
                self.require_mutable(tgt, tgt.span, "assign to")
            else:
                self.require_mutable(tgt, tgt.span, "modify")
        if e.op == "=":
            self.expect(e.value, tt, note="the right-hand side must match the variable's type")
        else:
            vt = self.infer(e.value, tt)
            try:
                unify(tt, vt)
            except Mismatch:
                self.err("type-mismatch", f"mismatched types for `{e.op}`", e.span,
                         f"left is `{show(tt)}`, right is `{show(vt)}`", help=hint_for(tt, vt))  # fmt: skip
                return UNIT
            op = e.op[0]
            self.deferred.append(Deferred("add" if op == "+" else "num", tt, e.span, op))
        return UNIT

    # ------------------------------------------------------------ patterns
    def check_pat(self, p: ast.Pat, ty: Type) -> None:
        p.ty = ty
        if isinstance(p, ast.PWild):
            return
        if isinstance(p, ast.PBind):
            assert p.sym is not None
            if p.sym.ty is None:
                p.sym.ty = ty
            else:
                self.unify_at(p.sym.ty, ty, p.span, "in this or-pattern alternative")
            return
        if isinstance(p, ast.PLit):
            lt = self.infer(p.value)
            self.unify_at(ty, lt, p.span, "pattern type must match the matched value")
            return
        if isinstance(p, ast.PTuple):
            pt = prune(ty)
            if isinstance(pt, TVar):
                items = tuple(TVar() for _ in p.items)
                unify(pt, TTuple(items))
                pt = prune(pt)
            if isinstance(pt, TTuple) and len(pt.items) == len(p.items):
                for sp, it in zip(p.items, pt.items):
                    self.check_pat(sp, it)
            else:
                if not isinstance(pt, TError):
                    self.err("pattern-mismatch", "mismatched types in pattern", p.span,
                             f"this is a {len(p.items)}-tuple pattern, but the value has type `{show(pt)}`")  # fmt: skip
                for sp in p.items:
                    self.check_pat(sp, ERROR)
            return
        if isinstance(p, ast.PCtor):
            v = p.variant
            if v is None:
                for a in p.args or []:
                    self.check_pat(a, ERROR)
                return
            m: dict[TParam, Type] = {q: TVar() for q in v.enum.tparams}
            ety = TCon(v.enum.name, tuple(m[q] for q in v.enum.tparams))
            self.unify_at(ty, ety, p.span, "pattern type must match the matched value")
            for a, ft in zip(p.args or [], v.fields):
                self.check_pat(a, subst(ft, m))
            return
        if isinstance(p, ast.PStruct):
            info = p.info
            if info is None:
                return
            m2: dict[TParam, Type] = {q: TVar() for q in info.tparams}
            sty = TCon(info.name, tuple(m2[q] for q in info.tparams))
            self.unify_at(ty, sty, p.span, "pattern type must match the matched value")
            for fname, sp in p.fields:
                i = info.field_index(fname)
                self.check_pat(sp, subst(info.fields[i][1], m2) if i >= 0 else ERROR)
            return
        assert isinstance(p, ast.POr)
        for a in p.alts:
            self.check_pat(a, ty)


__all__ = ["Checker", "satisfies", "Callable"]
