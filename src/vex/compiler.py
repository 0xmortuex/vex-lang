"""Compiler: typed AST -> stack bytecode."""

from __future__ import annotations

from typing import Any

from vex import bytecode as bc
from vex import syntax as ast
from vex.diagnostics import Span
from vex.runtime import Adt, AdtInfo, BuiltinFn, Closure
from vex.symbols import Builtin, Env, FnSym, GlobalVar, LocalSym, VariantInfo
from vex.vtypes import TCon, TTuple, Type, zonk

INT_BINOPS = {"+": bc.ADD_I, "-": bc.SUB_I, "*": bc.MUL_I, "/": bc.DIV_I, "%": bc.MOD_I}
FLOAT_BINOPS = {"+": bc.ADD_F, "-": bc.SUB_F, "*": bc.MUL_F, "/": bc.DIV_F, "%": bc.MOD_F}
CMP_OPS = {"==": bc.EQ, "!=": bc.NE, "<": bc.LT, "<=": bc.LE, ">": bc.GT, ">=": bc.GE}
FUSED_CMP = {"==": bc.EQ_LK, "!=": bc.NE_LK, "<": bc.LT_LK, "<=": bc.LE_LK, ">": bc.GT_LK, ">=": bc.GE_LK}


def _ty_name(t: Type | None) -> str:
    if t is None:
        return ""
    t = zonk(t)
    return t.name if isinstance(t, TCon) else ("Tuple" if isinstance(t, TTuple) else "")


class Loop:
    def __init__(self, start: int | None, has_iter: bool, depth: int) -> None:
        self.start = start  # continue target (None until known)
        self.has_iter = has_iter
        self.depth = depth  # operands on the stack at a jump target, the iterator included
        self.breaks: list[int] = []
        self.continues: list[int] = []


class Compiler:
    """Compiles functions; shared across the REPL session so adt infos stay identical."""

    def __init__(self, env: Env) -> None:
        self.env = env
        self.adt_infos: dict[str, AdtInfo] = {}
        self.variant_consts: dict[int, Any] = {}

    def adt_info(self, name: str) -> AdtInfo:
        info = self.adt_infos.get(name)
        if info is None:
            if name == "Option":
                from vex.runtime import OPTION_INFO

                info = OPTION_INFO
            elif name == "Result":
                from vex.runtime import RESULT_INFO

                info = RESULT_INFO
            elif name in self.env.structs:
                s = self.env.structs[name]
                info = AdtInfo(name, [name], [[f for f, _ in s.fields]])
            else:
                en = self.env.enums[name]
                info = AdtInfo(name, [v.name for v in en.variants])
            self.adt_infos[name] = info
        return info

    def compile_functions(self, fns: list[FnSym]) -> None:
        for fn in fns:
            d = fn.decl
            code = bc.Code(fn.qualname, d.span.source.path, d.span.line)
            code.nparams = len(fn.params)
            fn.code = code
        for fn in fns:
            FnCompiler(self, fn.code, None).compile_fn(fn)


class FnCompiler:
    def __init__(self, comp: Compiler, code: bc.Code, parent: FnCompiler | None) -> None:
        self.comp = comp
        self.code = code
        self.parent = parent
        self.slots: dict[LocalSym, int] = {}
        self.nlocals = 0
        self.loops: list[Loop] = []
        self.depth = 0  # operands held on the stack while the current expression is compiled
        self.span: Span | None = None

    # ------------------------------------------------------------ emit helpers
    def emit(self, op: int, arg: Any = None) -> int:
        self.code.instrs.append((op, arg))
        self.code.spans.append(self.span)
        return len(self.code.instrs) - 1

    def here(self) -> int:
        return len(self.code.instrs)

    def patch(self, at: int, target: int | None = None) -> None:
        op, _ = self.code.instrs[at]
        self.code.instrs[at] = (op, self.here() if target is None else target)

    def patch_all(self, ats: list[int]) -> None:
        for a in ats:
            self.patch(a)

    def new_slot(self) -> int:
        n = self.nlocals
        self.nlocals += 1
        return n

    def slot(self, sym: LocalSym) -> int:
        s = self.slots.get(sym)
        if s is None:
            s = self.slots[sym] = self.new_slot()
        return s

    # ------------------------------------------------------------ functions
    def compile_fn(self, fn: FnSym) -> None:
        d = fn.decl
        params: list[LocalSym] = []
        if fn.self_local is not None:
            params.append(fn.self_local)
        for p in d.params:
            assert p.sym is not None
            params.append(p.sym)
        for ps in params:
            self.slot(ps)
        self.code.nparams = len(params)
        self.span = d.span
        for ps in params:
            if ps.boxed:
                self.emit(bc.MAKE_CELL, self.slots[ps])
        self.cx(d.body)
        self.emit(bc.RET)
        self.code.nlocals = self.nlocals

    def compile_lambda(self, lam: ast.Lambda) -> bc.Code:
        code = bc.Code("<lambda>", lam.span.source.path, lam.span.line)
        sub = FnCompiler(self.comp, code, self)
        for c in lam.captures:
            sub.slot(c)
        code.ncaptured = len(lam.captures)
        for p in lam.params:
            assert p.sym is not None
            sub.slot(p.sym)
        code.nparams = len(lam.params)
        sub.span = lam.span
        sub.cx(lam.body)
        sub.emit(bc.RET)
        code.nlocals = sub.nlocals
        return code

    # ------------------------------------------------------------ expressions
    def cx(self, e: ast.Expr) -> None:
        saved = self.span
        self.span = e.span
        try:
            self.cx_inner(e)
        finally:
            self.span = saved

    def cx_over(self, held: int, e: ast.Expr) -> None:
        """Compile ``e`` while ``held`` operands sit below it; a jump out of ``e`` must discard them."""
        self.depth += held
        self.cx(e)
        self.depth -= held

    def cx_each(self, es: list[ast.Expr]) -> None:
        """Compile ``es`` left to right, each value staying on the stack under the next."""
        for held, e in enumerate(es):
            self.cx_over(held, e)

    def unwind(self, loop: Loop) -> None:
        """Discard the operands above ``loop``'s entry depth, just before jumping to it."""
        for _ in range(self.depth - loop.depth):
            self.emit(bc.POP)

    def cstmt(self, e: ast.Expr) -> None:
        """Compile ``e`` for effect only (no value left on the stack)."""
        saved = self.span
        self.span = e.span
        try:
            if isinstance(e, ast.Block):
                self.cblock(e, want=False)
            elif isinstance(e, ast.If):
                self.cif(e, want=False)
            elif isinstance(e, ast.While):
                self.cwhile(e, want=False)
            elif isinstance(e, ast.For):
                self.cfor(e, want=False)
            elif isinstance(e, ast.Assign):
                self.cassign(e, want=False)
            elif isinstance(e, ast.Match):
                self.cmatch(e, want=False)
            else:
                self.cx_inner(e)
                self.emit(bc.POP)
        finally:
            self.span = saved

    def cx_inner(self, e: ast.Expr) -> None:
        if isinstance(e, ast.IntLit):
            self.emit(bc.CONST, e.value)
        elif isinstance(e, ast.FloatLit):
            self.emit(bc.CONST, e.value)
        elif isinstance(e, ast.BoolLit):
            self.emit(bc.CONST, e.value)
        elif isinstance(e, ast.StrLit):
            self.emit(bc.CONST, e.value)
        elif isinstance(e, ast.UnitLit):
            self.emit(bc.CONST, None)
        elif isinstance(e, ast.Interp):
            for held, p in enumerate(e.parts):
                if isinstance(p, str):
                    self.emit(bc.CONST, p)
                else:
                    self.cx_over(held, p)
            self.emit(bc.BUILD_STR, len(e.parts))
        elif isinstance(e, ast.Name):
            self.cname(e)
        elif isinstance(e, ast.ListLit):
            self.cx_each(e.items)
            self.emit(bc.MAKE_LIST, len(e.items))
        elif isinstance(e, ast.TupleLit):
            self.cx_each(e.items)
            self.emit(bc.MAKE_TUPLE, len(e.items))
        elif isinstance(e, ast.MapLit):
            self.cx_each([x for pair in e.pairs for x in pair])
            self.emit(bc.MAKE_MAP, len(e.pairs))
        elif isinstance(e, ast.Unary):
            self.cx(e.operand)
            if e.op == "!":
                self.emit(bc.NOT)
            else:
                self.emit(bc.NEG_F if _ty_name(e.operand.ty) == "Float" else bc.NEG_I)
        elif isinstance(e, ast.Binary):
            self.cbinary(e)
        elif isinstance(e, ast.Call):
            self.ccall(e)
        elif isinstance(e, ast.Field):
            self.cfield(e)
        elif isinstance(e, ast.Index):
            self.cx_each([e.obj, e.index])
            self.emit(bc.INDEX)
        elif isinstance(e, ast.Try):
            self.cx(e.operand)
            self.emit(bc.TRY)
        elif isinstance(e, ast.Lambda):
            code = self.compile_lambda(e)
            for c in e.captures:
                self.emit(bc.LOAD, self.slots[c])
            self.emit(bc.MAKE_CLOSURE, (code, len(e.captures)))
        elif isinstance(e, ast.Block):
            self.cblock(e, want=True)
        elif isinstance(e, ast.If):
            self.cif(e, want=True)
        elif isinstance(e, ast.Match):
            self.cmatch(e, want=True)
        elif isinstance(e, ast.While):
            self.cwhile(e, want=True)
        elif isinstance(e, ast.For):
            self.cfor(e, want=True)
        elif isinstance(e, ast.Return):
            if e.value is None:
                self.emit(bc.CONST, None)
            else:
                self.cx(e.value)
            self.emit(bc.RET)
        elif isinstance(e, ast.Break):
            loop = self.loops[-1]
            self.unwind(loop)
            loop.breaks.append(self.emit(bc.JMP, None))
        elif isinstance(e, ast.Continue):
            loop = self.loops[-1]
            self.unwind(loop)
            if loop.start is not None:
                self.emit(bc.JMP, loop.start)
            else:
                loop.continues.append(self.emit(bc.JMP, None))
        elif isinstance(e, ast.RangeExpr):
            self.cx_each([e.lo, e.hi])
            self.emit(bc.RANGE_INCL if e.inclusive else bc.RANGE)
        elif isinstance(e, ast.StructLit):
            self.cstruct(e)
        elif isinstance(e, ast.Assign):
            self.cassign(e, want=True)
        else:
            raise AssertionError(f"cannot compile {type(e).__name__}")  # pragma: no cover

    # ---- names
    def cname(self, e: ast.Name | ast.Field, sym: object = None) -> None:
        sym = sym if sym is not None else (e.sym if isinstance(e, ast.Name) else e.static)
        if isinstance(sym, LocalSym):
            self.emit(bc.LOAD_CELL if sym.boxed else bc.LOAD, self.slots[sym])
        elif isinstance(sym, GlobalVar):
            self.emit(bc.LOAD_GLOBAL, sym.key)
        elif isinstance(sym, FnSym):
            self.emit(bc.CONST, Closure(sym.code, []))
        elif isinstance(sym, VariantInfo):
            info = self.comp.adt_info(sym.enum.name)
            if sym.fields:
                self.emit(bc.CONST, make_ctor(info, sym))
            else:
                self.emit(bc.MAKE_ADT, (info, sym.tag, 0))
        elif isinstance(sym, Builtin):
            if sym.is_const:
                self.emit(bc.CONST, sym.value)
            else:
                assert sym.impl is not None
                self.emit(bc.CONST, BuiltinFn(sym.qual, sym.impl, sym.needs_vm))
        else:
            raise AssertionError(f"cannot compile name {e}")  # pragma: no cover

    def cfield(self, e: ast.Field) -> None:
        if e.static is not None:
            self.cname(e, e.static)
            return
        self.cx(e.obj)
        if isinstance(zonk(e.obj.ty) if e.obj.ty else None, TTuple):
            self.emit(bc.GET_TUPLE, e.index)
        else:
            self.emit(bc.GET_FIELD, e.index)

    # ---- operators
    def is_plain_local(self, e: ast.Expr) -> bool:
        return isinstance(e, ast.Name) and isinstance(e.sym, LocalSym) and not e.sym.boxed

    def cbinary(self, e: ast.Binary) -> None:
        op = e.op
        if op in ("&&", "||"):
            self.cx(e.left)
            j = self.emit(bc.JMP_F_KEEP if op == "&&" else bc.JMP_T_KEEP, None)
            self.cx(e.right)
            self.patch(j)
            return
        tn = _ty_name(e.left.ty)
        if (
            tn == "Int"
            and isinstance(e.right, ast.IntLit)
            and self.is_plain_local(e.left)
            and (op in FUSED_CMP or op in ("+", "-"))
        ):
            assert isinstance(e.left, ast.Name) and isinstance(e.left.sym, LocalSym)
            slot = self.slots[e.left.sym]
            if op == "+":
                self.emit(bc.ADDK_L, (slot, e.right.value))
            elif op == "-":
                self.emit(bc.SUBK_L, (slot, e.right.value))
            else:
                self.emit(FUSED_CMP[op], (slot, e.right.value))
            return
        self.cx_each([e.left, e.right])
        if op in CMP_OPS:
            self.emit(CMP_OPS[op])
        elif tn == "Int":
            self.emit(INT_BINOPS[op])
        elif tn == "Float":
            self.emit(FLOAT_BINOPS[op])
        elif tn == "Str":
            self.emit(bc.ADD_S)
        else:
            raise AssertionError(f"binary {op} on {tn!r}")  # pragma: no cover

    # ---- calls
    def ccall(self, e: ast.Call) -> None:
        target = e.target
        n = len(e.args)
        operands = list(e.args)
        if e.is_method:
            assert isinstance(e.callee, ast.Field)
            operands.insert(0, e.callee.obj)
            n += 1
        elif target is None:
            operands.insert(0, e.callee)
        self.cx_each(operands)
        if target is None and not e.is_method:
            self.emit(bc.CALL, len(e.args))
        elif isinstance(target, FnSym):
            self.emit(bc.CALL_FN, (target.code, n))
        elif isinstance(target, Builtin):
            assert target.impl is not None
            self.emit(bc.CALL_BIV if target.needs_vm else bc.CALL_BI, (target.impl, n))
        elif isinstance(target, VariantInfo):
            self.emit(bc.MAKE_ADT, (self.comp.adt_info(target.enum.name), target.tag, n))
        else:
            raise AssertionError("bad call target")  # pragma: no cover

    def cstruct(self, e: ast.StructLit) -> None:
        info = e.info
        assert info is not None
        order = [info.field_index(n) for n, _, _ in e.fields]
        if order == list(range(len(info.fields))):
            self.cx_each([v for _, _, v in e.fields])
        else:
            temps: dict[int, int] = {}
            for (_, _, v), idx in zip(e.fields, order):
                self.cx(v)
                t = self.new_slot()
                temps[idx] = t
                self.emit(bc.STORE, t)
            for idx in range(len(info.fields)):
                self.emit(bc.LOAD, temps[idx])
        self.emit(bc.MAKE_ADT, (self.comp.adt_info(info.name), 0, len(info.fields)))

    # ---- assignment
    def store_local(self, sym: LocalSym) -> None:
        self.emit(bc.STORE_CELL if sym.boxed else bc.STORE, self.slots[sym])

    def cassign(self, e: ast.Assign, want: bool) -> None:
        tgt = e.target
        compound = e.op != "="
        if isinstance(tgt, ast.Name):
            sym = tgt.sym
            assert isinstance(sym, (LocalSym, GlobalVar))
            fused = (
                compound
                and e.op in ("+=", "-=")
                and isinstance(e.value, ast.IntLit)
                and isinstance(sym, LocalSym)
                and not sym.boxed
                and _ty_name(tgt.ty) == "Int"
            )
            if fused:
                assert isinstance(sym, LocalSym) and isinstance(e.value, ast.IntLit)
                self.emit(bc.ADDK_L if e.op == "+=" else bc.SUBK_L, (self.slots[sym], e.value.value))
            else:
                if compound:
                    self.cname(tgt)
                self.cx_over(1 if compound else 0, e.value)
                if compound:
                    self.carith(e.op[0], tgt.ty)
            if isinstance(sym, LocalSym):
                self.store_local(sym)
            else:
                self.emit(bc.STORE_GLOBAL, sym.key)
        elif isinstance(tgt, ast.Field):
            self.cx(tgt.obj)
            if compound:
                self.emit(bc.DUP)
                self.emit(bc.GET_FIELD, tgt.index)
            self.cx_over(2 if compound else 1, e.value)
            if compound:
                self.carith(e.op[0], tgt.ty)
            self.emit(bc.SET_FIELD, tgt.index)
        else:
            assert isinstance(tgt, ast.Index)
            self.cx_each([tgt.obj, tgt.index])
            if compound:
                self.emit(bc.DUP2)
                self.emit(bc.INDEX)
            self.cx_over(3 if compound else 2, e.value)
            if compound:
                self.carith(e.op[0], tgt.ty)
            self.emit(bc.SET_INDEX)
        if want:
            self.emit(bc.CONST, None)

    def carith(self, op: str, ty: Type | None) -> None:
        tn = _ty_name(ty)
        if tn == "Int":
            self.emit(INT_BINOPS[op])
        elif tn == "Float":
            self.emit(FLOAT_BINOPS[op])
        elif tn == "Str":
            self.emit(bc.ADD_S)
        else:
            raise AssertionError(f"compound op on {tn!r}")  # pragma: no cover

    # ---- control flow
    def cblock(self, b: ast.Block, want: bool) -> None:
        for st in b.stmts:
            if isinstance(st, ast.Let):
                self.clet(st)
            else:
                self.cstmt(st.expr)
        if b.tail is not None:
            if want:
                self.cx(b.tail)
            else:
                self.cstmt(b.tail)
        elif want:
            self.emit(bc.CONST, None)

    def clet(self, st: ast.Let) -> None:
        saved = self.span
        self.span = st.span
        self.cx(st.init)
        self.bind_irrefutable(st.pat)
        self.span = saved

    def bind_irrefutable(self, pat: ast.Pat) -> None:
        """The value to bind is on the stack."""
        if isinstance(pat, ast.PBind):
            self.bind_sym(pat)
            return
        if isinstance(pat, ast.PWild):
            self.emit(bc.POP)
            return
        tmp = self.new_slot()
        self.emit(bc.STORE, tmp)
        fails: list[int] = []
        self.cpat(pat, tmp, fails)
        if fails:
            j = self.emit(bc.JMP, None)
            self.patch_all(fails)
            self.emit(bc.PANIC, "pattern did not match")
            self.patch(j)

    def bind_sym(self, pat: ast.PBind) -> None:
        sym = pat.sym
        if isinstance(sym, GlobalVar):
            self.emit(bc.STORE_GLOBAL, sym.key)
            return
        assert isinstance(sym, LocalSym)
        existing = sym in self.slots
        slot = self.slot(sym)
        if sym.boxed and not existing:
            self.emit(bc.STORE, slot)
            self.emit(bc.MAKE_CELL, slot)
        elif sym.boxed:
            self.emit(bc.STORE_CELL, slot)
        else:
            self.emit(bc.STORE, slot)

    def cif(self, e: ast.If, want: bool) -> None:
        self.cx(e.cond)
        jf = self.emit(bc.JMP_F, None)
        self.cblock(e.then, want)
        if e.els is None:
            if want:
                je = self.emit(bc.JMP, None)
                self.patch(jf)
                self.emit(bc.CONST, None)
                self.patch(je)
            else:
                self.patch(jf)
            return
        je = self.emit(bc.JMP, None)
        self.patch(jf)
        if want:
            self.cx(e.els)
        else:
            self.cstmt(e.els)
        self.patch(je)

    def cwhile(self, e: ast.While, want: bool) -> None:
        start = self.here()
        loop = Loop(start, False, self.depth)
        self.cx(e.cond)  # outside the loop's scope, as in the resolver: a jump here leaves the enclosing loop
        jf = self.emit(bc.JMP_F, None)
        self.loops.append(loop)
        self.cblock(e.body, False)
        self.emit(bc.JMP, start)
        self.loops.pop()
        self.patch(jf)
        self.patch_all(loop.breaks)
        if want:
            self.emit(bc.CONST, None)

    def cfor(self, e: ast.For, want: bool) -> None:
        self.cx(e.iter)
        self.emit(bc.ITER)
        start = self.here()
        self.depth += 1  # the iterator stays below everything the body evaluates
        loop = Loop(start, True, self.depth)
        self.loops.append(loop)
        fo = self.emit(bc.FOR_ITER, None)
        self.bind_irrefutable(e.pat)
        self.cblock(e.body, False)
        self.emit(bc.JMP, start)
        self.loops.pop()
        self.depth -= 1
        if loop.breaks:
            self.patch_all(loop.breaks)
            self.emit(bc.POP)  # discard the iterator when leaving via break
        self.patch(fo)
        if want:
            self.emit(bc.CONST, None)

    def cmatch(self, e: ast.Match, want: bool) -> None:
        self.cx(e.scrut)
        tmp = self.new_slot()
        self.emit(bc.STORE, tmp)
        ends: list[int] = []
        for arm in e.arms:
            fails: list[int] = []
            self.cpat(arm.pat, tmp, fails)
            if arm.guard is not None:
                self.cx(arm.guard)
                fails.append(self.emit(bc.JMP_F, None))
            if want:
                self.cx(arm.body)
            else:
                self.cstmt(arm.body)
            ends.append(self.emit(bc.JMP, None))
            self.patch_all(fails)
        self.emit(bc.PANIC, "no match arm matched (non-exhaustive match)")
        self.patch_all(ends)

    # ---- patterns
    def cpat(self, p: ast.Pat, slot: int, fails: list[int]) -> None:
        """Test the value in local ``slot`` against ``p``; jump to a ``fails`` entry on mismatch."""
        if isinstance(p, ast.PWild):
            return
        if isinstance(p, ast.PBind):
            self.emit(bc.LOAD, slot)
            self.bind_sym(p)
            return
        if isinstance(p, ast.PLit):
            if isinstance(p.value, ast.UnitLit):
                return
            self.emit(bc.LOAD, slot)
            v = p.value
            assert isinstance(v, (ast.IntLit, ast.FloatLit, ast.BoolLit, ast.StrLit))
            self.emit(bc.CONST, v.value)
            self.emit(bc.EQ)
            fails.append(self.emit(bc.JMP_F, None))
            return
        if isinstance(p, ast.PTuple):
            for i, sub in enumerate(p.items):
                self.csub(sub, slot, bc.GET_TUPLE, i, fails)
            return
        if isinstance(p, ast.PCtor):
            variant = p.variant
            assert variant is not None
            self.emit(bc.LOAD, slot)
            self.emit(bc.TAG_IS, variant.tag)
            fails.append(self.emit(bc.JMP_F, None))
            for i, sub in enumerate(p.args or []):
                self.csub(sub, slot, bc.GET_FIELD, i, fails)
            return
        if isinstance(p, ast.PStruct):
            info = p.info
            assert info is not None
            for fname, sub in p.fields:
                self.csub(sub, slot, bc.GET_FIELD, info.field_index(fname), fails)
            return
        assert isinstance(p, ast.POr)
        done: list[int] = []
        for i, alt in enumerate(p.alts):
            if i == len(p.alts) - 1:
                self.cpat(alt, slot, fails)
            else:
                local: list[int] = []
                self.cpat(alt, slot, local)
                done.append(self.emit(bc.JMP, None))
                self.patch_all(local)
        self.patch_all(done)

    def csub(self, sub: ast.Pat, slot: int, getter: int, index: int, fails: list[int]) -> None:
        if isinstance(sub, ast.PWild):
            return
        self.emit(bc.LOAD, slot)
        self.emit(getter, index)
        if isinstance(sub, ast.PBind):
            self.bind_sym(sub)
            return
        tmp = self.new_slot()
        self.emit(bc.STORE, tmp)
        self.cpat(sub, tmp, fails)


def make_ctor(info: AdtInfo, v: VariantInfo) -> BuiltinFn:
    n = len(v.fields)
    tag = v.tag

    def ctor(*args: Any) -> Adt:
        assert len(args) == n
        return Adt(info, tag, list(args))

    return BuiltinFn(v.name, ctor)
