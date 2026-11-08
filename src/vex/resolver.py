"""Name resolution: declarations, type expressions, local scopes and closure captures."""

from __future__ import annotations

from vex import syntax as ast
from vex.diagnostics import DiagSink, Span, suggest
from vex.stdlib import FULL_SCOPE, GLOBALS, NAMESPACES
from vex.symbols import (
    EnumInfo,
    Env,
    FnSym,
    GlobalVar,
    LocalSym,
    NamespaceSym,
    Scope,
    StructInfo,
    VariantInfo,
)
from vex.vtypes import (
    ERROR,
    PRIMITIVES,
    TCon,
    TFn,
    TParam,
    TTuple,
    TVar,
    Type,
    prune,
    tuple_of,
)

BUILTIN_TYPE_NAMES = {*PRIMITIVES, "List", "Map", "Option", "Result", "Never", "Self"}
KNOWN_BOUNDS = ("Eq", "Ord", "Hash")
HASHABLE = {"Int", "Str", "Bool", "Unit"}
HOST_NAMESPACE = "host"  # reserved everywhere so that a script means the same thing in every host


def make_env() -> Env:
    """An environment with the built-in Option and Result enums."""
    env = Env()
    for name, tps, variants in [
        ("Option", ["T"], [("Some", ["T"]), ("None", [])]),
        ("Result", ["T", "E"], [("Ok", ["T"]), ("Err", ["E"])]),
    ]:
        params = [TParam(t) for t in tps]
        lookup = {p.name: p for p in params}
        info = EnumInfo(name, params, None)
        for tag, (vname, fields) in enumerate(variants):
            v = VariantInfo(info, vname, tag)
            v.fields = [lookup[f] for f in fields]
            info.variants.append(v)
            env.variants.setdefault(vname, []).append(v)
        env.enums[name] = info
    return env


class FnCtx:
    def __init__(self, parent: FnCtx | None, tparams: dict[str, TParam], self_ty: Type | None,
                 lam: ast.Lambda | None = None) -> None:  # fmt: skip
        self.parent = parent
        self.scopes: list[dict[str, LocalSym]] = [{}]
        self.tparams = tparams
        self.self_ty = self_ty
        self.lam = lam
        self.loop_depth = 0


class Resolver:
    def __init__(self, env: Env, sink: DiagSink, repl: bool = False, scope: Scope | None = None) -> None:
        self.env = env
        self.sink = sink
        self.repl = repl
        self.scope = scope if scope is not None else FULL_SCOPE
        self.ctx: FnCtx | None = None
        self.global_block: ast.Block | None = None

    def resolve_repl(self, mod: ast.Module) -> FnSym | None:
        """Wrap the REPL statements of ``mod`` in a synthetic function; its top-level lets become globals."""
        if not mod.stmts and mod.tail is None:
            return None
        first = mod.stmts[0].span if mod.stmts else mod.tail.span  # type: ignore[union-attr]
        last = mod.tail.span if mod.tail is not None else mod.stmts[-1].span
        span = first.to(last)
        body = ast.Block(span, mod.stmts, mod.tail)
        decl = ast.FnDecl(span, "__repl__", [], [], None, body, None, name_span=span)
        n = len(self.env.fn_list)
        self.declare_fn(decl, None, {}, None)
        if len(self.env.fn_list) == n:
            return None
        self.global_block = body
        assert decl.sym is not None
        self.resolve_fn(decl.sym)
        self.global_block = None
        return decl.sym

    # ------------------------------------------------------------ driver
    def resolve_modules(self, modules: list[ast.Module]) -> list[FnSym]:
        """Resolve declarations of all modules, then their bodies. Returns the new functions."""
        start = len(self.env.fn_list)
        items = [it for m in modules for it in m.items]
        for it in items:
            if isinstance(it, (ast.StructDecl, ast.EnumDecl)):
                self.declare_type(it)
        for it in items:
            if isinstance(it, ast.StructDecl):
                self.fill_struct(it)
            elif isinstance(it, ast.EnumDecl):
                self.fill_enum(it)
        for it in items:
            if isinstance(it, ast.FnDecl):
                self.declare_fn(it, None, {}, None)
            elif isinstance(it, ast.ImplDecl):
                self.declare_impl(it)
        new_fns = self.env.fn_list[start:]
        for fn in new_fns:
            self.resolve_fn(fn)
        return new_fns

    def err(self, code: str, msg: str, span: Span | None, label: str = "", help: str | None = None,
            secondary: list[tuple[Span, str]] | None = None) -> None:  # fmt: skip
        self.sink.error(code, msg, span, label, help=help, secondary=secondary)

    # ------------------------------------------------------------ declarations
    def check_new_name(self, name: str, span: Span, what: str) -> bool:
        if name in BUILTIN_TYPE_NAMES or name in GLOBALS or name in NAMESPACES or name == HOST_NAMESPACE:
            self.err("reserved-name", f"`{name}` is a built-in name and cannot be redefined", span,
                     f"this {what} name is reserved")  # fmt: skip
            return False
        return True

    def declare_type(self, it: ast.StructDecl | ast.EnumDecl) -> None:
        name = it.name
        if not self.check_new_name(name, it.span, "type"):
            return
        old = self.env.decl_spans.get(name)
        if name in self.env.structs or name in self.env.enums or name in self.env.fns:
            self.err("duplicate-definition", f"`{name}` is defined more than once", it.span,
                     "redefined here", secondary=[(old, "first defined here")] if old else None)  # fmt: skip
            return
        tps = self.make_tparams(it.tparams)
        self.env.decl_spans[name] = it.span
        if isinstance(it, ast.StructDecl):
            it.info = self.env.structs[name] = StructInfo(name, tps, it.span)
        else:
            it.info = self.env.enums[name] = EnumInfo(name, tps, it.span)

    def make_tparams(self, decls: list[ast.TParamDecl]) -> list[TParam]:
        out: list[TParam] = []
        seen: set[str] = set()
        for d in decls:
            if d.name in seen:
                self.err("duplicate-definition", f"type parameter `{d.name}` is declared twice", d.span)
            seen.add(d.name)
            for b in d.bounds:
                if b not in KNOWN_BOUNDS:
                    self.err("unknown-bound", f"unknown bound `{b}`", d.span,
                             help=f"available bounds: {', '.join(KNOWN_BOUNDS)}")  # fmt: skip
            out.append(TParam(d.name, tuple(d.bounds)))
        return out

    def fill_struct(self, it: ast.StructDecl) -> None:
        info = it.info
        if info is None:
            return
        tps = {p.name: p for p in info.tparams}
        seen: set[str] = set()
        for f in it.fields:
            if f.name in seen:
                self.err("duplicate-definition", f"field `{f.name}` is declared twice in `{it.name}`", f.span)
                continue
            seen.add(f.name)
            info.fields.append((f.name, self.convert(f.ty_expr, tps, None)))

    def fill_enum(self, it: ast.EnumDecl) -> None:
        info = it.info
        if info is None:
            return
        tps = {p.name: p for p in info.tparams}
        for tag, v in enumerate(it.variants):
            if info.variant(v.name):
                self.err("duplicate-definition", f"variant `{v.name}` is declared twice in `{it.name}`", v.span)
                continue
            vi = VariantInfo(info, v.name, tag)
            vi.fields = [self.convert(t, tps, None) for t in v.fields]
            info.variants.append(vi)
            self.env.variants.setdefault(v.name, []).append(vi)

    def declare_impl(self, it: ast.ImplDecl) -> None:
        tps_list = self.make_tparams(it.tparams)
        tps = {p.name: p for p in tps_list}
        t = it.target
        if not isinstance(t, ast.TyName) or t.name not in self.env.structs and t.name not in self.env.enums:
            self.err("bad-impl-target", "`impl` must target a struct or enum declared in this program",
                     it.target.span, "cannot add methods to this type")  # fmt: skip
            return
        if t.name in ("Option", "Result"):
            self.err("bad-impl-target", f"cannot add methods to built-in `{t.name}`", it.target.span)
            return
        self_ty = self.convert(t, tps, None)
        if isinstance(self_ty, TCon):
            for a in self_ty.args:
                if not isinstance(a, TParam):
                    self.err("bad-impl-target", "impl type arguments must be plain type parameters",
                             it.target.span, help="write `impl[T] Stack[T] { ... }`")  # fmt: skip
                    return
        for m in it.methods:
            self.declare_fn(m, t.name, tps, self_ty)

    def declare_fn(self, d: ast.FnDecl, owner: str | None, outer_tps: dict[str, TParam],
                   self_ty: Type | None) -> None:  # fmt: skip
        key_span = d.name_span or d.span
        own = self.make_tparams(d.tparams)
        tps = dict(outer_tps)
        for tp in own:
            if tp.name in tps:
                self.err("duplicate-definition", f"type parameter `{tp.name}` shadows an outer one", key_span)
            tps[tp.name] = tp
        sym = FnSym(d.name, d, owner)
        sym.tparams = list(outer_tps.values()) + own
        if d.self_mode is not None and self_ty is None:
            self.err("self-outside-impl", "`self` parameter outside of an `impl` block", key_span,
                     help="move this function into an `impl` block or remove `self`")  # fmt: skip
            sym.has_self = False
        params: list[Type] = [self_ty] if (d.self_mode is not None and self_ty is not None) else []
        seen: set[str] = set()
        for p in d.params:
            if p.name in seen:
                self.err("duplicate-definition", f"parameter `{p.name}` is declared twice", p.span)
            seen.add(p.name)
            assert p.ty_expr is not None
            params.append(self.convert(p.ty_expr, tps, self_ty))
        sym.params = params
        sym.mut_params = ([d.self_mode == "mut self"] if len(params) > len(d.params) else []) + [
            p.mutable for p in d.params
        ]
        if d.ret is not None:
            sym.ret = self.convert(d.ret, tps, self_ty)
            sym.declared_ret = True
        else:
            sym.ret = TVar()
        d.sym = sym
        if owner is None:
            if not self.check_new_name(d.name, key_span, "function"):
                return
            exists = d.name in self.env.fns and not self.repl
            if exists or d.name in self.env.structs or d.name in self.env.enums:
                old = self.env.fns[d.name].decl.name_span if d.name in self.env.fns else self.env.decl_spans.get(d.name)
                self.err("duplicate-definition", f"`{d.name}` is defined more than once", key_span,
                         "redefined here", secondary=[(old, "first defined here")] if old else None)  # fmt: skip
                return
            self.env.fns[d.name] = sym
            self.env.decl_spans.setdefault(d.name, key_span)
        else:
            key = (owner, d.name)
            if key in self.env.methods and not self.repl:
                old = self.env.methods[key].decl.name_span
                self.err("duplicate-definition", f"method `{d.name}` is defined more than once for `{owner}`",
                         key_span, "redefined here", secondary=[(old, "first defined here")] if old else None)  # fmt: skip
                return
            self.env.methods[key] = sym
        self.env.fn_list.append(sym)
        sym.scope_tps = tps
        sym.self_ty = self_ty

    # ------------------------------------------------------------ type expressions
    def convert(self, te: ast.TypeExpr, tps: dict[str, TParam], self_ty: Type | None) -> Type:
        if isinstance(te, ast.TyTuple):
            return tuple_of([self.convert(i, tps, self_ty) for i in te.items])
        if isinstance(te, ast.TyFn):
            ret = self.convert(te.ret, tps, self_ty) if te.ret else PRIMITIVES["Unit"]
            return TFn(tuple(self.convert(p, tps, self_ty) for p in te.params), ret)
        name = te.name
        if name in tps:
            if te.args:
                self.err("type-arity", f"type parameter `{name}` takes no type arguments", te.span)
            return tps[name]
        if name == "Self":
            if self_ty is None:
                self.err("self-outside-impl", "`Self` is only available inside an `impl` block", te.span)
                return ERROR
            return self_ty
        if name in PRIMITIVES:
            if te.args:
                self.err("type-arity", f"`{name}` takes no type arguments", te.span,
                         f"remove `[{', '.join(_te_name(a) for a in te.args)}]`")  # fmt: skip
            return PRIMITIVES[name]
        args = tuple(self.convert(a, tps, self_ty) for a in te.args)
        want: int | None
        if name == "List":
            want = 1
        elif name == "Map":
            want = 2
        else:
            info = self.env.adt(name)
            if info is None:
                names = [*PRIMITIVES, "List", "Map", *self.env.type_names(), *tps]
                s = suggest(name, names)
                self.err("unknown-type", f"cannot find type `{name}` in this scope", te.span,
                         "not a known type", help=f"did you mean `{s}`?" if s else None)  # fmt: skip
                return ERROR
            want = len(info.tparams)
        if len(args) != want:
            self.err("type-arity", f"`{name}` expects {want} type argument{'s' if want != 1 else ''}, "
                     f"found {len(args)}", te.span,
                     f"write `{name}[{', '.join('T' + str(i + 1) for i in range(want))}]`" if want else
                     f"`{name}` is not generic")  # fmt: skip
            return ERROR
        if name == "Map":
            self.check_key(args[0], te.span)
        return TCon(name, args)

    def check_key(self, k: Type, span: Span) -> None:
        k = prune(k)
        ok = True
        if isinstance(k, TParam):
            ok = "Hash" in k.bounds
        elif isinstance(k, TCon):
            ok = k.name in HASHABLE
        elif isinstance(k, TTuple):
            for i in k.items:
                self.check_key(i, span)
            return
        elif isinstance(k, TFn):
            ok = False
        if not ok:
            self.err("bad-map-key", "Map keys must be Int, Str, Bool or tuples of those", span,
                     "this type cannot be used as a key",
                     help="for a generic key use `[K: Hash]`" if isinstance(k, TParam) else None)  # fmt: skip

    # ------------------------------------------------------------ function bodies
    def resolve_fn(self, fn: FnSym) -> None:
        d = fn.decl
        tps = fn.scope_tps
        self_ty = fn.self_ty
        self.ctx = FnCtx(None, tps, self_ty)
        if d.self_mode is not None and self_ty is not None:
            s = LocalSym("self", d.self_mode == "mut self", d.span)
            s.is_self = True
            s.ty = self_ty
            fn.self_local = s
            self.declare(s)
        for p, ty in zip(d.params, fn.params[1 if (d.self_mode is not None and self_ty is not None) else 0:]):
            ps = LocalSym(p.name, p.mutable, p.span)
            ps.ty = ty
            p.sym = ps
            self.declare(ps)
        self.rblock(d.body)
        self.ctx = None

    def declare(self, sym: LocalSym) -> None:
        assert self.ctx is not None
        self.ctx.scopes[-1][sym.name] = sym

    def visible_locals(self) -> list[str]:
        out: list[str] = []
        c = self.ctx
        while c is not None:
            for sc in c.scopes:
                out.extend(sc)
            c = c.parent
        return out

    def lookup_local(self, name: str) -> LocalSym | None:
        c = self.ctx
        chain: list[FnCtx] = []
        while c is not None:
            for sc in reversed(c.scopes):
                if name in sc:
                    sym = sc[name]
                    if chain:
                        sym.captured = True
                        for lam_ctx in chain:
                            assert lam_ctx.lam is not None
                            if sym not in lam_ctx.lam.captures:
                                lam_ctx.lam.captures.append(sym)
                    return sym
            chain.append(c)
            c = c.parent
        return None

    # ------------------------------------------------------------ blocks and statements
    def push(self) -> None:
        assert self.ctx is not None
        self.ctx.scopes.append({})

    def pop(self) -> None:
        assert self.ctx is not None
        self.ctx.scopes.pop()

    def rblock(self, b: ast.Block) -> None:
        self.push()
        top = b is self.global_block
        for st in b.stmts:
            self.rstmt(st, top)
        if b.tail is not None:
            self.rx(b.tail)
        self.pop()

    def rstmt(self, st: ast.Stmt, top: bool = False) -> None:
        if isinstance(st, ast.Let):
            self.rx(st.init)
            if st.ty_expr is not None:
                assert self.ctx is not None
                st.annot = self.convert(st.ty_expr, self.ctx.tparams, self.ctx.self_ty)
            if top and isinstance(st.pat, ast.PBind) and not st.pat.name[:1].isupper():
                gv = GlobalVar(st.pat.name, TVar(), st.pat.mutable)
                self.env.globals[st.pat.name] = gv
                st.pat.sym = gv  # type: ignore[assignment]
            else:
                if top:
                    self.err("repl-let", "at the prompt, `let` binds a single variable name", st.pat.span)
                st.pat = self.rpat(st.pat, {})
        else:
            self.rx(st.expr)

    # ------------------------------------------------------------ expressions
    def rx(self, e: ast.Expr) -> None:
        if isinstance(e, ast.Name):
            self.rname(e, False)
        elif isinstance(e, (ast.IntLit, ast.FloatLit, ast.BoolLit, ast.StrLit, ast.UnitLit, ast.Break, ast.Continue)):
            if isinstance(e, (ast.Break, ast.Continue)):
                assert self.ctx is not None
                if self.ctx.loop_depth == 0:
                    kw = "break" if isinstance(e, ast.Break) else "continue"
                    self.err("outside-loop", f"`{kw}` outside of a loop", e.span, "not inside `while` or `for`")
        elif isinstance(e, ast.Interp):
            for p in e.parts:
                if not isinstance(p, str):
                    self.rx(p)
        elif isinstance(e, (ast.ListLit, ast.TupleLit)):
            for i in e.items:
                self.rx(i)
        elif isinstance(e, ast.MapLit):
            for k, v in e.pairs:
                self.rx(k)
                self.rx(v)
        elif isinstance(e, ast.Unary):
            self.rx(e.operand)
        elif isinstance(e, ast.Binary):
            self.rx(e.left)
            self.rx(e.right)
        elif isinstance(e, ast.Call):
            if isinstance(e.callee, ast.Name):
                self.rname(e.callee, True)
            else:
                self.rx(e.callee)
            for a in e.args:
                self.rx(a)
        elif isinstance(e, ast.Field):
            self.rfield(e)
        elif isinstance(e, ast.Index):
            self.rx(e.obj)
            self.rx(e.index)
        elif isinstance(e, ast.Try):
            self.rx(e.operand)
        elif isinstance(e, ast.Lambda):
            self.rlambda(e)
        elif isinstance(e, ast.Block):
            self.rblock(e)
        elif isinstance(e, ast.If):
            self.rx(e.cond)
            self.rblock(e.then)
            if e.els is not None:
                self.rx(e.els)
        elif isinstance(e, ast.Match):
            self.rx(e.scrut)
            for arm in e.arms:
                self.push()
                arm.pat = self.rpat(arm.pat, {})
                if arm.guard is not None:
                    self.rx(arm.guard)
                self.rx(arm.body)
                self.pop()
        elif isinstance(e, ast.While):
            assert self.ctx is not None
            self.rx(e.cond)
            self.ctx.loop_depth += 1
            self.rblock(e.body)
            self.ctx.loop_depth -= 1
        elif isinstance(e, ast.For):
            assert self.ctx is not None
            self.rx(e.iter)
            self.push()
            e.pat = self.rpat(e.pat, {})
            self.ctx.loop_depth += 1
            self.rblock(e.body)
            self.ctx.loop_depth -= 1
            self.pop()
        elif isinstance(e, ast.Return):
            if e.value is not None:
                self.rx(e.value)
        elif isinstance(e, ast.RangeExpr):
            self.rx(e.lo)
            self.rx(e.hi)
        elif isinstance(e, ast.StructLit):
            info = self.env.structs.get(e.name)
            if info is None:
                if e.name in self.env.enums:
                    self.err("not-a-struct", f"`{e.name}` is an enum, not a struct", e.span,
                             help=f"build a value with `{e.name}.Variant(...)`")  # fmt: skip
                else:
                    s = suggest(e.name, self.env.structs)
                    self.err("unknown-type", f"cannot find struct `{e.name}` in this scope", e.span,
                             "not a known struct", help=f"did you mean `{s}`?" if s else None)  # fmt: skip
            e.info = info
            for _, _, v in e.fields:
                self.rx(v)
        elif isinstance(e, ast.Assign):
            self.rx(e.target)
            self.rx(e.value)

    def rlambda(self, e: ast.Lambda) -> None:
        assert self.ctx is not None
        ctx = FnCtx(self.ctx, self.ctx.tparams, self.ctx.self_ty, e)
        self.ctx = ctx
        seen: set[str] = set()
        for p in e.params:
            if p.name in seen:
                self.err("duplicate-definition", f"parameter `{p.name}` is declared twice", p.span)
            seen.add(p.name)
            s = LocalSym(p.name, False, p.span)
            if p.ty_expr is not None:
                s.ty = self.convert(p.ty_expr, ctx.tparams, ctx.self_ty)
            p.sym = s
            self.declare(s)
        if e.ret is not None:
            e.ret_ty = self.convert(e.ret, ctx.tparams, ctx.self_ty)
        self.rx(e.body)
        assert ctx.parent is not None
        self.ctx = ctx.parent

    def candidates(self) -> list[str]:
        return [*self.visible_locals(), *self.env.fns, *self.scope.globals, *self.env.variants, *self.scope.namespaces,
                *self.env.structs, *self.env.globals]  # fmt: skip

    def rname(self, e: ast.Name, as_callee: bool) -> None:
        n = e.name
        sym = self.lookup_local(n)
        if sym is not None:
            e.sym = sym
            return
        if n in self.env.globals:
            e.sym = self.env.globals[n]
        elif n in self.env.fns:
            e.sym = self.env.fns[n]
        elif n in self.scope.globals:
            e.sym = self.scope.globals[n]
        elif n in self.env.variants:
            vs = self.env.variants[n]
            if len(vs) > 1:
                names = ", ".join(f"{v.enum.name}.{n}" for v in vs)
                self.err("ambiguous-name", f"variant `{n}` exists in several enums", e.span,
                         help=f"write one of: {names}")  # fmt: skip
            e.sym = vs[0]
        elif n in self.scope.namespaces:
            e.sym = NamespaceSym(n)
        elif n in self.env.structs or n in self.env.enums:
            e.sym = self.env.adt(n)
        elif n in self.scope.denied:
            self.err("capability-denied", f"`{n}` is not available to this script", e.span,
                     "not granted", help=self.scope.denied[n])  # fmt: skip
        else:
            what = "function" if as_callee else "value"
            s = suggest(n, self.candidates())
            help = f"did you mean `{s}`?" if s else None
            if n == "self":
                help = "`self` is only available in methods that take a `self` parameter"
            elif n[:1].isupper() and not s:
                help = "variants of user enums are written `Enum.Variant`"
            self.err("undefined-name", f"cannot find {what} `{n}` in this scope", e.span,
                     "not found in this scope", help=help)  # fmt: skip

    def rfield(self, e: ast.Field) -> None:
        obj = e.obj
        if isinstance(obj, ast.Name) and self.lookup_local(obj.name) is None and obj.name not in self.env.globals:
            n = obj.name
            if n in self.scope.namespaces:
                obj.sym = NamespaceSym(n)
                members = self.scope.namespaces[n]
                member = members.get(e.name)
                qual = f"{n}.{e.name}"
                if member is None:
                    obj.sym = None  # already reported below; the checker must not add "not a value"
                if member is None and qual in self.scope.denied:
                    self.err("capability-denied", f"`{qual}` is not available to this script",
                             e.name_span or e.span, "not granted", help=self.scope.denied[qual])  # fmt: skip
                elif member is None:
                    s = suggest(e.name, members)
                    self.err("unknown-member", f"module `{n}` has no member `{e.name}`", e.name_span or e.span,
                             "not found", help=f"did you mean `{s}`?" if s else None)  # fmt: skip
                e.static = member
                return
            info = self.env.adt(n)
            if info is not None and n not in self.env.fns:
                obj.sym = info
                if isinstance(info, EnumInfo):
                    v = info.variant(e.name)
                    if v is not None:
                        e.static = v
                        return
                m = self.env.methods.get((n, e.name))
                if m is not None:
                    e.static = m
                    return
                pool = [v.name for v in info.variants] if isinstance(info, EnumInfo) else []
                pool += [k[1] for k in self.env.methods if k[0] == n]
                s = suggest(e.name, pool)
                what = "variant or function" if isinstance(info, EnumInfo) else "associated function"
                self.err("unknown-member", f"type `{n}` has no {what} named `{e.name}`", e.name_span or e.span,
                         "not found", help=f"did you mean `{s}`?" if s else None)  # fmt: skip
                return
        self.rx(obj)

    # ------------------------------------------------------------ patterns
    def rpat(self, p: ast.Pat, binds: dict[str, LocalSym], reuse: dict[str, LocalSym] | None = None) -> ast.Pat:
        if isinstance(p, ast.PWild):
            return p
        if isinstance(p, ast.PBind):
            n = p.name
            if n[:1].isupper():
                vs = self.env.variants.get(n)
                if vs and len(vs) == 1:
                    v = vs[0]
                    if v.fields:
                        self.err("variant-arity", f"variant `{n}` has {len(v.fields)} field(s)", p.span,
                                 help=f"write `{n}({', '.join('_' for _ in v.fields)})`")  # fmt: skip
                    return ast.PCtor(p.span, [n], None, variant=v)
                s = suggest(n, self.env.variants)
                self.err("unknown-variant", f"cannot find variant `{n}`", p.span, "no such variant",
                         help=f"did you mean `{s}`?" if s else "variants of user enums are written `Enum.Variant`")  # fmt: skip
                return ast.PWild(p.span)
            if n in binds:
                self.err("duplicate-binding", f"`{n}` is bound more than once in this pattern", p.span)
            if reuse is not None:
                if n not in reuse:
                    self.err("or-pattern-bindings", f"`{n}` is not bound in every alternative", p.span)
                    sym = LocalSym(n, p.mutable, p.span)
                else:
                    sym = reuse[n]
                binds[n] = sym
                p.sym = sym
                return p
            sym = LocalSym(n, p.mutable, p.span)
            p.sym = sym
            binds[n] = sym
            self.declare(sym)
            return p
        if isinstance(p, ast.PLit):
            return p
        if isinstance(p, ast.PTuple):
            p.items = [self.rpat(i, binds, reuse) for i in p.items]
            return p
        if isinstance(p, ast.PCtor):
            self.rctor(p)
            if p.args is not None:
                p.args = [self.rpat(a, binds, reuse) for a in p.args]
            return p
        if isinstance(p, ast.PStruct):
            info = self.env.structs.get(p.name)
            if info is None:
                s = suggest(p.name, self.env.structs)
                self.err("unknown-type", f"cannot find struct `{p.name}`", p.span, "not a known struct",
                         help=f"did you mean `{s}`?" if s else None)  # fmt: skip
                return ast.PWild(p.span)
            p.info = info
            seen: set[str] = set()
            for fname, _ in p.fields:
                if info.field_index(fname) < 0:
                    s = suggest(fname, [f for f, _ in info.fields])
                    self.err("unknown-field", f"struct `{p.name}` has no field `{fname}`", p.span,
                             help=f"did you mean `{s}`?" if s else None)  # fmt: skip
                if fname in seen:
                    self.err("duplicate-binding", f"field `{fname}` appears twice in this pattern", p.span)
                seen.add(fname)
            p.fields = [(f, self.rpat(sp, binds, reuse)) for f, sp in p.fields]
            if not p.rest:
                missing = [f for f, _ in info.fields if f not in seen]
                if missing:
                    self.err("missing-fields", f"pattern does not mention field(s): {', '.join(missing)}", p.span,
                             help="add the fields or end the pattern with `..`")  # fmt: skip
            return p
        assert isinstance(p, ast.POr)
        first: dict[str, LocalSym] = {}
        p.alts[0] = self.rpat(p.alts[0], first, reuse)
        for i in range(1, len(p.alts)):
            other: dict[str, LocalSym] = {}
            p.alts[i] = self.rpat(p.alts[i], other, first)
            absent = set(first) - set(other)
            if absent:
                self.err("or-pattern-bindings", f"`{sorted(absent)[0]}` is not bound in every alternative",
                         p.alts[i].span)  # fmt: skip
        for k, bound in first.items():
            if k in binds:
                self.err("duplicate-binding", f"`{k}` is bound more than once in this pattern", p.span)
            binds[k] = bound
        return p

    def rctor(self, p: ast.PCtor) -> None:
        path = p.path
        if len(path) == 1:
            vs = self.env.variants.get(path[0])
            if not vs:
                s = suggest(path[0], self.env.variants)
                self.err("unknown-variant", f"cannot find variant `{path[0]}`", p.span, "no such variant",
                         help=f"did you mean `{s}`?" if s else "variants of user enums are written `Enum.Variant`")  # fmt: skip
                return
            if len(vs) > 1:
                names = ", ".join(f"{v.enum.name}.{path[0]}" for v in vs)
                self.err("ambiguous-name", f"variant `{path[0]}` exists in several enums", p.span,
                         help=f"write one of: {names}")  # fmt: skip
            p.variant = vs[0]
        elif len(path) == 2:
            en = self.env.enums.get(path[0])
            if en is None:
                s = suggest(path[0], self.env.enums)
                self.err("unknown-type", f"cannot find enum `{path[0]}`", p.span, "not an enum",
                         help=f"did you mean `{s}`?" if s else None)  # fmt: skip
                return
            v = en.variant(path[1])
            if v is None:
                s = suggest(path[1], [x.name for x in en.variants])
                self.err("unknown-variant", f"enum `{path[0]}` has no variant `{path[1]}`", p.span,
                         "no such variant", help=f"did you mean `{s}`?" if s else None)  # fmt: skip
                return
            p.variant = v
        else:
            self.err("bad-pattern", "patterns can only be `Variant` or `Enum.Variant`", p.span)
            return
        v = p.variant
        n = len(p.args) if p.args is not None else 0
        if v is not None and n != len(v.fields):
            self.err("variant-arity", f"variant `{v.name}` has {len(v.fields)} field(s) but the pattern has {n}",
                     p.span, f"expected {len(v.fields)}")  # fmt: skip


def _te_name(te: ast.TypeExpr) -> str:
    return te.name if isinstance(te, ast.TyName) else "_"


__all__ = ["Resolver", "make_env", "GlobalVar"]
