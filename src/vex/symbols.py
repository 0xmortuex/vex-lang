"""Symbols produced by name resolution and the global environment."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from vex.diagnostics import Span
from vex.vtypes import TParam, Type

if TYPE_CHECKING:
    from vex import syntax as ast


class LocalSym:
    """A local variable or parameter."""

    def __init__(self, name: str, mutable: bool, span: Span) -> None:
        self.name = name
        self.mutable = mutable
        self.span = span
        self.ty: Optional[Type] = None
        self.captured = False
        self.is_self = False

    @property
    def boxed(self) -> bool:
        """Mutable variables captured by a closure live in a shared Cell."""
        return self.mutable and self.captured


class GlobalVar:
    """A REPL top-level `let` binding."""

    _counter = 0

    def __init__(self, name: str, ty: Type, mutable: bool) -> None:
        GlobalVar._counter += 1
        self.name = name
        self.key = f"{name}#{GlobalVar._counter}"  # unique, so redefinitions never alias old code
        self.ty = ty
        self.mutable = mutable


class StructInfo:
    def __init__(self, name: str, tparams: list[TParam], span: Span) -> None:
        self.name = name
        self.tparams = tparams
        self.fields: list[tuple[str, Type]] = []
        self.span = span

    def field_index(self, name: str) -> int:
        for i, (n, _) in enumerate(self.fields):
            if n == name:
                return i
        return -1


class VariantInfo:
    def __init__(self, enum: EnumInfo, name: str, tag: int) -> None:
        self.enum = enum
        self.name = name
        self.tag = tag
        self.fields: list[Type] = []


class EnumInfo:
    def __init__(self, name: str, tparams: list[TParam], span: Span | None) -> None:
        self.name = name
        self.tparams = tparams
        self.variants: list[VariantInfo] = []
        self.span = span
        self.family: Any = None  # cached pattern-constructor family (see exhaust.py)

    def variant(self, name: str) -> VariantInfo | None:
        for v in self.variants:
            if v.name == name:
                return v
        return None


AdtInfoT = StructInfo | EnumInfo


class FnSym:
    """A user function or method."""

    def __init__(self, name: str, decl: ast.FnDecl, owner: str | None) -> None:
        self.name = name
        self.decl = decl
        self.owner = owner
        self.tparams: list[TParam] = []
        self.params: list[Type] = []
        self.ret: Type | None = None
        self.declared_ret = False
        self.state = 0  # 0 = unchecked, 1 = in progress, 2 = done
        self.self_mode: str | None = decl.self_mode
        self.code: Any = None  # bytecode Code, filled by the compiler
        self.has_self = decl.self_mode is not None
        self.self_local: LocalSym | None = None
        self.mut_params: list[bool] = []
        self.scope_tps: dict[str, TParam] = {}
        self.self_ty: Type | None = None

    @property
    def qualname(self) -> str:
        return f"{self.owner}.{self.name}" if self.owner else self.name


class Builtin:
    """A standard-library function, method or constant."""

    def __init__(
        self,
        qual: str,
        tparams: list[TParam],
        ty: Type,
        impl: Callable[..., Any] | None,
        mutates: bool = False,
        needs_vm: bool = False,
        value: Any = None,
        is_const: bool = False,
    ) -> None:
        self.qual = qual
        self.tparams = tparams
        self.ty = ty
        self.impl = impl
        self.mutates = mutates
        self.needs_vm = needs_vm
        self.value = value
        self.is_const = is_const


class Scope:
    """The built-in names one compilation can see.

    The CLI and REPL use every name in the standard library. An embedded script gets a filtered copy,
    and ``denied`` maps each withheld name (``print``, ``io.write_file``) to the reason it is withheld.
    """

    def __init__(
        self,
        globals: dict[str, Builtin],
        namespaces: dict[str, dict[str, Builtin]],
        denied: dict[str, str] | None = None,
    ) -> None:
        self.globals = globals
        self.namespaces = namespaces
        self.denied = denied or {}


class NamespaceSym:
    def __init__(self, name: str) -> None:
        self.name = name


class Env:
    """Everything declared at the top level (shared across modules and REPL inputs)."""

    def __init__(self) -> None:
        self.structs: dict[str, StructInfo] = {}
        self.enums: dict[str, EnumInfo] = {}
        self.fns: dict[str, FnSym] = {}
        self.methods: dict[tuple[str, str], FnSym] = {}
        self.variants: dict[str, list[VariantInfo]] = {}
        self.fn_list: list[FnSym] = []
        self.globals: dict[str, GlobalVar] = {}
        self.decl_spans: dict[str, Span] = {}

    def adt(self, name: str) -> StructInfo | EnumInfo | None:
        return self.structs.get(name) or self.enums.get(name)

    def type_names(self) -> list[str]:
        return [*self.structs, *self.enums]
