"""AST node definitions. Every node carries a source span."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Union

from vex.diagnostics import Span
from vex.vtypes import Type

if TYPE_CHECKING:
    from vex.symbols import EnumInfo, FnSym, LocalSym, StructInfo, VariantInfo

StrPartAst = Union[str, "Expr"]


@dataclass(eq=False)
class Node:
    span: Span


# ---------------------------------------------------------------- type expressions
@dataclass(eq=False)
class TyName(Node):
    name: str
    args: list[TypeExpr]


@dataclass(eq=False)
class TyTuple(Node):
    items: list[TypeExpr]


@dataclass(eq=False)
class TyFn(Node):
    params: list[TypeExpr]
    ret: Optional[TypeExpr]


TypeExpr = Union[TyName, TyTuple, TyFn]


# ---------------------------------------------------------------- patterns
@dataclass(eq=False)
class Pat(Node):
    ty: Optional[Type] = field(default=None, kw_only=True)


@dataclass(eq=False)
class PWild(Pat):
    pass


@dataclass(eq=False)
class PBind(Pat):
    name: str
    mutable: bool
    sym: Optional[LocalSym] = None


@dataclass(eq=False)
class PLit(Pat):
    value: Expr  # IntLit / FloatLit / BoolLit / StrLit (possibly negated)


@dataclass(eq=False)
class PTuple(Pat):
    items: list[Pat]


@dataclass(eq=False)
class PCtor(Pat):
    path: list[str]
    args: Optional[list[Pat]]  # None when written without parentheses
    variant: Optional[VariantInfo] = None


@dataclass(eq=False)
class PStruct(Pat):
    name: str
    fields: list[tuple[str, Pat]]
    rest: bool
    info: Optional[StructInfo] = None


@dataclass(eq=False)
class POr(Pat):
    alts: list[Pat]


# ---------------------------------------------------------------- expressions
@dataclass(eq=False)
class Expr(Node):
    ty: Optional[Type] = field(default=None, kw_only=True)


@dataclass(eq=False)
class IntLit(Expr):
    value: int


@dataclass(eq=False)
class FloatLit(Expr):
    value: float


@dataclass(eq=False)
class BoolLit(Expr):
    value: bool


@dataclass(eq=False)
class StrLit(Expr):
    value: str


@dataclass(eq=False)
class Interp(Expr):
    parts: list[StrPartAst]


@dataclass(eq=False)
class UnitLit(Expr):
    pass


@dataclass(eq=False)
class Name(Expr):
    name: str
    sym: object = None  # LocalSym | FnSym | VariantInfo | Builtin | GlobalVar | NamespaceSym | adt info


@dataclass(eq=False)
class ListLit(Expr):
    items: list[Expr]


@dataclass(eq=False)
class MapLit(Expr):
    pairs: list[tuple[Expr, Expr]]


@dataclass(eq=False)
class TupleLit(Expr):
    items: list[Expr]


@dataclass(eq=False)
class Unary(Expr):
    op: str
    operand: Expr


@dataclass(eq=False)
class Binary(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass(eq=False)
class Call(Expr):
    callee: Expr
    args: list[Expr]
    # filled in by the checker for method calls
    target: object = None  # FnSym | Builtin
    is_method: bool = False


@dataclass(eq=False)
class Field(Expr):
    obj: Expr
    name: str
    name_span: Optional[Span] = None
    # resolver: static path (Type.member / namespace.member); checker: struct field index
    static: object = None
    index: int = -1
    parenthesized: bool = False  # `(x.f)(1)` calls a field value instead of a method


@dataclass(eq=False)
class Index(Expr):
    obj: Expr
    index: Expr


@dataclass(eq=False)
class Try(Expr):
    operand: Expr


@dataclass(eq=False)
class Param(Node):
    name: str
    ty_expr: Optional[TypeExpr]
    mutable: bool = False
    sym: Optional[LocalSym] = None


@dataclass(eq=False)
class Lambda(Expr):
    params: list[Param]
    ret: Optional[TypeExpr]
    body: Expr
    captures: list[LocalSym] = field(default_factory=list)
    ret_ty: Optional[Type] = None


@dataclass(eq=False)
class Block(Expr):
    stmts: list[Stmt]
    tail: Optional[Expr]


@dataclass(eq=False)
class If(Expr):
    cond: Expr
    then: Block
    els: Optional[Expr]


@dataclass(eq=False)
class Arm(Node):
    pat: Pat
    guard: Optional[Expr]
    body: Expr


@dataclass(eq=False)
class Match(Expr):
    scrut: Expr
    arms: list[Arm]


@dataclass(eq=False)
class While(Expr):
    cond: Expr
    body: Block


@dataclass(eq=False)
class For(Expr):
    pat: Pat
    iter: Expr
    body: Block


@dataclass(eq=False)
class Return(Expr):
    value: Optional[Expr]


@dataclass(eq=False)
class Break(Expr):
    pass


@dataclass(eq=False)
class Continue(Expr):
    pass


@dataclass(eq=False)
class RangeExpr(Expr):
    lo: Expr
    hi: Expr
    inclusive: bool


@dataclass(eq=False)
class StructLit(Expr):
    name: str
    fields: list[tuple[str, Span, Expr]]
    info: Optional[StructInfo] = None


@dataclass(eq=False)
class Assign(Expr):
    target: Expr
    op: str  # "=", "+=", ...
    value: Expr


# ---------------------------------------------------------------- statements
@dataclass(eq=False)
class Let(Node):
    pat: Pat
    ty_expr: Optional[TypeExpr]
    init: Expr
    annot: Optional[Type] = None


@dataclass(eq=False)
class ExprStmt(Node):
    expr: Expr


Stmt = Union[Let, ExprStmt]


# ---------------------------------------------------------------- items
@dataclass(eq=False)
class TParamDecl(Node):
    name: str
    bounds: list[str]


@dataclass(eq=False)
class FnDecl(Node):
    name: str
    tparams: list[TParamDecl]
    params: list[Param]
    ret: Optional[TypeExpr]
    body: Block
    self_mode: Optional[str] = None  # None | "self" | "mut self"
    sym: Optional[FnSym] = None
    name_span: Optional[Span] = None


@dataclass(eq=False)
class FieldDecl(Node):
    name: str
    ty_expr: TypeExpr


@dataclass(eq=False)
class StructDecl(Node):
    name: str
    tparams: list[TParamDecl]
    fields: list[FieldDecl]
    info: Optional[StructInfo] = None


@dataclass(eq=False)
class VariantDecl(Node):
    name: str
    fields: list[TypeExpr]


@dataclass(eq=False)
class EnumDecl(Node):
    name: str
    tparams: list[TParamDecl]
    variants: list[VariantDecl]
    info: Optional[EnumInfo] = None


@dataclass(eq=False)
class ImplDecl(Node):
    tparams: list[TParamDecl]
    target: TypeExpr
    methods: list[FnDecl]


@dataclass(eq=False)
class Import(Node):
    name: str


Item = Union[FnDecl, StructDecl, EnumDecl, ImplDecl, Import]


@dataclass(eq=False)
class Module:
    path: str
    items: list[Item]
    # REPL only: top-level statements
    stmts: list[Stmt] = field(default_factory=list)
    tail: Optional[Expr] = None
