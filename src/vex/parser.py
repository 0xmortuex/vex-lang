"""Recursive-descent / Pratt parser with panic-mode error recovery."""

from __future__ import annotations

from vex import syntax as ast
from vex.diagnostics import DiagSink, Source, Span, suggest
from vex.lexer import Token, tokenize

INT_MAX = 2**63 - 1
INT_MIN = -(2**63)

BINARY_PREC = {
    "||": 1, "&&": 2,
    "==": 3, "!=": 3, "<": 4, "<=": 4, ">": 4, ">=": 4,
    "+": 5, "-": 5, "*": 6, "/": 6, "%": 6,
}  # fmt: skip
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%="}
ITEM_KEYWORDS = {"fn", "struct", "enum", "impl", "import"}
BLOCK_LIKE = {"if", "match", "while", "for", "{"}
MAX_DEPTH = 150


class ParseError(Exception):
    """Internal: a syntax error already reported to the sink; unwinds to a recovery point."""


class Parser:
    def __init__(
        self, source: Source, sink: DiagSink, tokens: list[Token] | None = None, depth: int = 0
    ) -> None:
        self.src = source
        self.sink = sink
        self.toks = tokens if tokens is not None else tokenize(source, sink)
        self.pos = 0
        self.no_struct = False
        self.depth = depth
        self.repl = False

    # ------------------------------------------------------------ helpers
    @property
    def tok(self) -> Token:
        return self.toks[self.pos]

    def peek(self, k: int = 1) -> Token:
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def at(self, kind: str) -> bool:
        return self.toks[self.pos].kind == kind

    def span_of(self, t: Token) -> Span:
        return Span(self.src, t.start, max(t.end, t.start + 1))

    def span_from(self, start: Token) -> Span:
        prev = self.toks[max(self.pos - 1, 0)]
        return Span(self.src, start.start, max(prev.end, start.start + 1))

    def advance(self) -> Token:
        t = self.toks[self.pos]
        if t.kind != "EOF":
            self.pos += 1
        return t

    def eat(self, kind: str) -> Token | None:
        if self.at(kind):
            return self.advance()
        return None

    def describe(self, t: Token) -> str:
        if t.kind == "EOF":
            return "end of file"
        if t.kind == "IDENT":
            return f"identifier `{t.text}`"
        if t.kind in ("INT", "FLOAT"):
            return f"number `{t.text}`"
        if t.kind == "STRING":
            return "a string literal"
        return f"`{t.text}`"

    def fail(self, code: str, message: str, t: Token | None = None, label: str = "",
             help: str | None = None) -> ParseError:  # fmt: skip
        t = t or self.tok
        self.sink.error(code, message, self.span_of(t), label, help=help)
        return ParseError()

    def expect(self, kind: str, what: str | None = None, ctx: str = "") -> Token:
        if self.at(kind):
            return self.advance()
        shown = what or f"`{kind}`"
        found = self.describe(self.tok)
        raise self.fail("expected-token", f"expected {shown}{ctx}, found {found}", label=f"expected {shown}")

    def ident(self, what: str = "a name") -> Token:
        if self.at("IDENT"):
            return self.advance()
        t = self.tok
        if t.kind in ast_keywords():
            raise self.fail("keyword-as-name", f"expected {what}, found keyword `{t.text}`",
                            label="`" + t.text + "` is a reserved word",
                            help=f"pick another name, e.g. `{t.text}_`")  # fmt: skip
        raise self.fail("expected-name", f"expected {what}, found {self.describe(t)}", label=f"expected {what}")

    # ------------------------------------------------------------ module
    def parse_module(self, path: str, repl: bool = False) -> ast.Module:
        mod = ast.Module(path, [])
        self.repl = repl
        while not self.at("EOF"):
            if self.at("}"):
                try:
                    raise self.fail("unmatched-brace", "unmatched `}`", label="no block is open here")
                except ParseError:
                    self.advance()
                    continue
            before = self.pos
            try:
                if repl and self._starts_statement():
                    self._repl_statement(mod)
                else:
                    mod.items.append(self.parse_item())
            except ParseError:
                self._sync_item()
                if self.pos == before:
                    self.advance()  # always make progress
            if len(self.sink.items) > self.sink.MAX_ERRORS:
                break
        return mod

    def _starts_statement(self) -> bool:
        return self.tok.kind not in ITEM_KEYWORDS

    def _repl_statement(self, mod: ast.Module) -> None:
        st = self.parse_stmt_or_tail()
        if isinstance(st, ast.Expr):
            mod.tail = st
            if not self.at("EOF"):
                raise self.fail("expected-token", f"unexpected {self.describe(self.tok)}",
                                label="expected end of input")  # fmt: skip
        else:
            mod.stmts.append(st)

    def _sync_item(self) -> None:
        depth = 0
        while not self.at("EOF"):
            k = self.tok.kind
            if depth <= 0 and k in ITEM_KEYWORDS:
                return
            if k in ("{", "(", "["):
                depth += 1
            elif k in ("}", ")", "]"):
                depth -= 1
            self.advance()

    # ------------------------------------------------------------ items
    def parse_item(self) -> ast.Item:
        k = self.tok.kind
        if k == "fn":
            return self.parse_fn()
        if k == "struct":
            return self.parse_struct()
        if k == "enum":
            return self.parse_enum()
        if k == "impl":
            return self.parse_impl()
        if k == "import":
            start = self.advance()
            name = self.ident("a module name")
            self.expect(";", ctx=" after import")
            return ast.Import(self.span_from(start), name.text)
        t = self.tok
        extra = None
        if k in ("let", "IDENT"):
            extra = "top-level code must be inside `fn main() { ... }`"
        raise self.fail("expected-item", f"expected an item (`fn`, `struct`, `enum`, `impl`, `import`), "
                        f"found {self.describe(t)}", label="not allowed at the top level", help=extra)  # fmt: skip

    def parse_tparams(self) -> list[ast.TParamDecl]:
        out: list[ast.TParamDecl] = []
        if not self.eat("["):
            return out
        while not self.at("]"):
            name = self.ident("a type parameter name")
            bounds: list[str] = []
            if self.eat(":"):
                bounds.append(self.ident("a bound such as `Ord`").text)
                while self.at("+") and self.peek().kind == "IDENT":
                    self.advance()
                    bounds.append(self.advance().text)
            out.append(ast.TParamDecl(self.span_of(name), name.text, bounds))
            if not self.eat(","):
                break
        self.expect("]", ctx=" after type parameters")
        return out

    def parse_fn(self) -> ast.FnDecl:
        start = self.expect("fn")
        name = self.ident("a function name")
        tparams = self.parse_tparams()
        self.expect("(", ctx=" after the function name")
        params: list[ast.Param] = []
        self_mode: str | None = None
        first = True
        while not self.at(")"):
            if first and self.at("mut") and self.peek().kind == "IDENT" and self.peek().text == "self":
                self.advance()
                self.advance()
                self_mode = "mut self"
            elif first and self.at("IDENT") and self.tok.text == "self":
                self.advance()
                self_mode = "self"
            else:
                is_mut = self.eat("mut") is not None
                pt = self.ident("a parameter name")
                if not self.at(":"):
                    raise self.fail("missing-param-type",
                                    f"parameter `{pt.text}` needs a type annotation", pt,
                                    label="expected `: Type` after this name",
                                    help=f"write `{pt.text}: Int` (parameter types are never inferred)")  # fmt: skip
                self.advance()
                params.append(ast.Param(self.span_of(pt), pt.text, self.parse_type(), is_mut))
            first = False
            if not self.eat(","):
                break
        self.expect(")", ctx=" after parameters")
        ret = self.parse_type() if self.eat("->") else None
        body = self.parse_block()
        return ast.FnDecl(self.span_from(start), name.text, tparams, params, ret, body, self_mode,
                          name_span=self.span_of(name))  # fmt: skip

    def parse_struct(self) -> ast.StructDecl:
        start = self.expect("struct")
        name = self.ident("a struct name")
        tparams = self.parse_tparams()
        self.expect("{", ctx=" after the struct name")
        fields: list[ast.FieldDecl] = []
        while not self.at("}"):
            ft = self.ident("a field name")
            self.expect(":", ctx=" after the field name")
            fields.append(ast.FieldDecl(self.span_of(ft), ft.text, self.parse_type()))
            if not self.eat(","):
                break
        self.expect("}", ctx=" after struct fields")
        return ast.StructDecl(self.span_from(start), name.text, tparams, fields)

    def parse_enum(self) -> ast.EnumDecl:
        start = self.expect("enum")
        name = self.ident("an enum name")
        tparams = self.parse_tparams()
        self.expect("{", ctx=" after the enum name")
        variants: list[ast.VariantDecl] = []
        while not self.at("}"):
            vt = self.ident("a variant name")
            fields: list[ast.TypeExpr] = []
            if self.eat("("):
                while not self.at(")"):
                    fields.append(self.parse_type())
                    if not self.eat(","):
                        break
                self.expect(")", ctx=" after variant fields")
            variants.append(ast.VariantDecl(self.span_of(vt), vt.text, fields))
            if not self.eat(","):
                break
        self.expect("}", ctx=" after enum variants")
        return ast.EnumDecl(self.span_from(start), name.text, tparams, variants)

    def parse_impl(self) -> ast.ImplDecl:
        start = self.expect("impl")
        tparams = self.parse_tparams()
        target = self.parse_type()
        self.expect("{", ctx=" after the impl target")
        methods: list[ast.FnDecl] = []
        while not self.at("}") and not self.at("EOF"):
            if not self.at("fn"):
                raise self.fail("expected-fn", f"expected `fn` inside impl, found {self.describe(self.tok)}",
                                label="only methods can appear in an impl block")  # fmt: skip
            try:
                methods.append(self.parse_fn())
            except ParseError:
                self._sync_method()
        self.expect("}", ctx=" to close the impl block")
        return ast.ImplDecl(self.span_from(start), tparams, target, methods)

    def _sync_method(self) -> None:
        depth = 0
        while not self.at("EOF"):
            k = self.tok.kind
            if depth <= 0 and (k == "fn" or k == "}"):
                return
            if k in ("{", "(", "["):
                depth += 1
            elif k in ("}", ")", "]"):
                depth -= 1
            self.advance()

    # ------------------------------------------------------------ types
    def parse_type(self) -> ast.TypeExpr:
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                raise self.fail("too-deep", "type is nested too deeply")
            start = self.tok
            if self.eat("("):
                items: list[ast.TypeExpr] = []
                while not self.at(")"):
                    items.append(self.parse_type())
                    if not self.eat(","):
                        break
                self.expect(")", ctx=" in type")
                if len(items) == 1 and self.toks[self.pos - 2].kind != ",":
                    return items[0]
                return ast.TyTuple(self.span_from(start), items)
            if self.eat("fn"):
                self.expect("(", ctx=" in function type")
                params: list[ast.TypeExpr] = []
                while not self.at(")"):
                    params.append(self.parse_type())
                    if not self.eat(","):
                        break
                self.expect(")", ctx=" in function type")
                ret = self.parse_type() if self.eat("->") else None
                return ast.TyFn(self.span_from(start), params, ret)
            name = self.ident("a type name")
            args: list[ast.TypeExpr] = []
            if self.eat("["):
                while not self.at("]"):
                    args.append(self.parse_type())
                    if not self.eat(","):
                        break
                self.expect("]", ctx=" after type arguments")
            return ast.TyName(self.span_from(name), name.text, args)
        finally:
            self.depth -= 1

    # ------------------------------------------------------------ blocks and statements
    def parse_block(self) -> ast.Block:
        start = self.expect("{", ctx="")
        saved = self.no_struct
        self.no_struct = False
        stmts: list[ast.Stmt] = []
        tail: ast.Expr | None = None
        try:
            while not self.at("}"):
                if self.at("EOF"):
                    self.sink.error("unclosed-block", "unclosed `{`", self.span_of(start),
                                    "this block is never closed", help="add a matching `}`")  # fmt: skip
                    raise ParseError()
                if self.eat(";"):
                    continue
                before = self.pos
                try:
                    st = self.parse_stmt_or_tail()
                except ParseError:
                    self._sync_stmt()
                    if self.pos == before:
                        self.advance()  # always make progress
                    continue
                if isinstance(st, ast.Expr):
                    if self.at("}"):
                        tail = st
                        break
                    stmts.append(ast.ExprStmt(st.span, st))
                else:
                    stmts.append(st)
        finally:
            self.no_struct = saved
        if self.at("}"):
            self.advance()
        return ast.Block(self.span_from(start), stmts, tail)

    def _sync_stmt(self) -> None:
        depth = 0
        while not self.at("EOF"):
            k = self.tok.kind
            if depth == 0 and k == ";":
                self.advance()
                return
            if depth == 0 and k == "}":
                return
            if depth == 0 and k in ("let", "fn", "struct", "enum", "impl"):
                return
            if k in ("{", "(", "["):
                depth += 1
            elif k in ("}", ")", "]"):
                depth -= 1
            self.advance()

    def parse_stmt_or_tail(self) -> ast.Stmt | ast.Expr:
        """A `let`, an expression statement (returned as Stmt) or a tail expression (Expr)."""
        if self.at("let"):
            return self.parse_let()
        if self.at("fn") or self.at("struct") or self.at("enum") or self.at("impl"):
            err = self.fail("nested-item", f"`{self.tok.text}` declarations are only allowed at the top level",
                            label="move this out of the function body")  # fmt: skip
            try:
                self.parse_item()  # consume the whole declaration so recovery does not cascade
            except ParseError:
                pass
            raise err
        start = self.tok
        if self.tok.kind in BLOCK_LIKE:
            e = self.parse_block_like()
            if self.at(";"):
                self.advance()
                return ast.ExprStmt(self.span_from(start), e)
            if self.at("}") or self.at("EOF"):
                return e
            if self.tok.kind in (".", "?"):
                # `match x { .. }.method()` continues the expression; operators do not (as in Rust).
                e = self._continue_expr(e)
                return self._finish_expr_stmt(start, e)
            return ast.ExprStmt(self.span_from(start), e)
        e = self.parse_expr()
        return self._finish_expr_stmt(start, e)

    def _continue_expr(self, left: ast.Expr) -> ast.Expr:
        left = self.parse_postfix_ops(left)
        return self.parse_binary_rest(left, 1)

    def _finish_expr_stmt(self, start: Token, e: ast.Expr) -> ast.Stmt | ast.Expr:
        if self.tok.kind in ASSIGN_OPS:
            op = self.advance()
            if not isinstance(e, (ast.Name, ast.Field, ast.Index)):
                self.sink.error("invalid-assign-target", "cannot assign to this expression", e.span,
                                "not a variable, field or index", help="only `x`, `x.field` and `x[i]` can be assigned to")  # fmt: skip
                raise ParseError()
            value = self.parse_expr()
            e = ast.Assign(self.span_from(start), e, op.kind, value)
        if self.at(";"):
            self.advance()
            return ast.ExprStmt(self.span_from(start), e)
        if self.at("}") or (self.repl and self.at("EOF")):
            return e
        t = self.tok
        prev = self.toks[self.pos - 1]
        self.sink.error("expected-semicolon", f"expected `;` or `}}`, found {self.describe(t)}",
                        Span(self.src, prev.end - 1, prev.end), "add `;` after this",
                        help="statements are separated by `;`")  # fmt: skip
        raise ParseError()

    def parse_let(self) -> ast.Let:
        start = self.expect("let")
        pat = self.parse_pattern(allow_mut=True)
        ty = self.parse_type() if self.eat(":") else None
        if not self.at("="):
            raise self.fail("expected-token", f"expected `=` in let, found {self.describe(self.tok)}",
                            label="every `let` needs an initial value",
                            help="write `let x = value;`")  # fmt: skip
        self.advance()
        init = self.parse_expr()
        self.expect(";", ctx=" after let statement")
        return ast.Let(self.span_from(start), pat, ty, init)

    # ------------------------------------------------------------ expressions
    def parse_expr(self) -> ast.Expr:
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                raise self.fail("too-deep", "expression is nested too deeply",
                                label="split this into smaller expressions")  # fmt: skip
            start = self.tok
            left = self.parse_binary(1)
            if self.at("..") or self.at("..="):
                inclusive = self.advance().kind == "..="
                right = self.parse_binary(1)
                return ast.RangeExpr(self.span_from(start), left, right, inclusive)
            return left
        finally:
            self.depth -= 1

    def parse_binary(self, min_prec: int) -> ast.Expr:
        return self.parse_binary_rest(self.parse_unary(), min_prec)

    def parse_binary_rest(self, left: ast.Expr, min_prec: int) -> ast.Expr:
        while True:
            op = self.tok.kind
            prec = BINARY_PREC.get(op)
            if prec is None or prec < min_prec:
                return left
            self.advance()
            right = self.parse_binary(prec + 1)
            left = ast.Binary(left.span.to(right.span), op, left, right)

    def parse_unary(self) -> ast.Expr:
        t = self.tok
        if t.kind == "-" and self.peek().kind == "INT" and self.peek().value == INT_MAX + 1:
            self.advance()
            self.advance()
            return ast.IntLit(self.span_from(t), INT_MIN)
        if t.kind in ("-", "!"):
            self.depth += 1
            try:
                if self.depth > MAX_DEPTH:
                    raise self.fail("too-deep", "expression is nested too deeply")
                self.advance()
                operand = self.parse_unary()
            finally:
                self.depth -= 1
            return ast.Unary(self.span_from(t), t.kind, operand)
        return self.parse_postfix()

    def parse_postfix(self) -> ast.Expr:
        return self.parse_postfix_ops(self.parse_primary())

    def parse_postfix_ops(self, e: ast.Expr) -> ast.Expr:
        while True:
            k = self.tok.kind
            if k == "(":
                self.advance()
                args = self.parse_args(")")
                e = ast.Call(e.span.to(self.span_of(self.toks[self.pos - 1])), e, args)
            elif k == "[":
                self.advance()
                saved, self.no_struct = self.no_struct, False
                idx = self.parse_expr()
                self.no_struct = saved
                close = self.expect("]", ctx=" after index")
                e = ast.Index(e.span.to(self.span_of(close)), e, idx)
            elif k == ".":
                self.advance()
                nt = self.tok
                if nt.kind == "IDENT" or nt.kind == "INT":
                    self.advance()
                    e = ast.Field(e.span.to(self.span_of(nt)), e, nt.text, self.span_of(nt))
                else:
                    raise self.fail("expected-name", f"expected a field or method name after `.`, found {self.describe(nt)}",
                                    label="expected a name")  # fmt: skip
            elif k == "?":
                q = self.advance()
                e = ast.Try(e.span.to(self.span_of(q)), e)
            else:
                return e

    def parse_args(self, close: str) -> list[ast.Expr]:
        saved, self.no_struct = self.no_struct, False
        args: list[ast.Expr] = []
        while not self.at(close):
            args.append(self.parse_expr())
            if not self.eat(","):
                break
        self.no_struct = saved
        self.expect(close, ctx=" to close the argument list")
        return args

    def parse_block_like(self) -> ast.Expr:
        k = self.tok.kind
        if k == "if":
            return self.parse_if()
        if k == "match":
            return self.parse_match()
        if k == "while":
            start = self.advance()
            cond = self.parse_cond()
            body = self.parse_block()
            return ast.While(self.span_from(start), cond, body)
        if k == "for":
            start = self.advance()
            pat = self.parse_pattern()
            self.expect("in", ctx=" in for loop")
            it = self.parse_cond()
            body = self.parse_block()
            return ast.For(self.span_from(start), pat, it, body)
        return self.parse_block()

    def parse_cond(self) -> ast.Expr:
        saved, self.no_struct = self.no_struct, True
        try:
            return self.parse_expr()
        finally:
            self.no_struct = saved

    def parse_if(self) -> ast.If:
        start = self.expect("if")
        cond = self.parse_cond()
        then = self.parse_block()
        els: ast.Expr | None = None
        if self.eat("else"):
            els = self.parse_if() if self.at("if") else self.parse_block()
        return ast.If(self.span_from(start), cond, then, els)

    def parse_match(self) -> ast.Match:
        start = self.expect("match")
        scrut = self.parse_cond()
        self.expect("{", ctx=" after the match subject")
        arms: list[ast.Arm] = []
        saved, self.no_struct = self.no_struct, False
        while not self.at("}"):
            if self.at("EOF"):
                self.sink.error("unclosed-block", "unclosed `{` in match", self.span_of(start),
                                "this match is never closed")  # fmt: skip
                raise ParseError()
            a0 = self.tok
            pat = self.parse_pattern()
            guard = self.parse_cond() if self.eat("if") else None
            self.expect("=>", ctx=" after the pattern")
            body_block_like = self.tok.kind == "{"
            body = self.parse_expr()
            arms.append(ast.Arm(self.span_from(a0), pat, guard, body))
            if not self.eat(","):
                if not (body_block_like or self.at("}")):
                    raise self.fail("expected-token",
                                    f"expected `,` or `}}` after match arm, found {self.describe(self.tok)}",
                                    label="separate match arms with `,`")  # fmt: skip
        self.expect("}")
        self.no_struct = saved
        return ast.Match(self.span_from(start), scrut, arms)

    def parse_primary(self) -> ast.Expr:
        t = self.tok
        k = t.kind
        if k == "INT":
            self.advance()
            assert isinstance(t.value, int)
            if t.value > INT_MAX:
                self.sink.error("int-literal-range", f"integer literal `{t.text}` is out of range",
                                self.span_of(t), "does not fit in a 64-bit Int",
                                help=f"Int is a signed 64-bit integer ({INT_MIN} to {INT_MAX})")  # fmt: skip
                return ast.IntLit(self.span_of(t), 0)
            return ast.IntLit(self.span_of(t), t.value)
        if k == "FLOAT":
            self.advance()
            assert isinstance(t.value, float)
            return ast.FloatLit(self.span_of(t), t.value)
        if k == "STRING":
            self.advance()
            return self.string_expr(t)
        if k == "true" or k == "false":
            self.advance()
            return ast.BoolLit(self.span_of(t), k == "true")
        if k == "IDENT":
            self.advance()
            if (
                self.at("{")
                and not self.no_struct
                and t.text[:1].isupper()
                and self._looks_like_struct_lit()
            ):
                return self.parse_struct_lit(t)
            return ast.Name(self.span_of(t), t.text)
        if k == "(":
            self.advance()
            saved, self.no_struct = self.no_struct, False
            items: list[ast.Expr] = []
            trailing = False
            while not self.at(")"):
                items.append(self.parse_expr())
                trailing = False
                if not self.eat(","):
                    break
                trailing = True
            self.no_struct = saved
            self.expect(")", ctx=" to close the parenthesis")
            if not items:
                return ast.UnitLit(self.span_from(t))
            if len(items) == 1 and not trailing:
                return items[0]
            return ast.TupleLit(self.span_from(t), items)
        if k == "[":
            self.advance()
            items = self.parse_args("]")
            return ast.ListLit(self.span_from(t), items)
        if k == "#{":
            self.advance()
            saved, self.no_struct = self.no_struct, False
            pairs: list[tuple[ast.Expr, ast.Expr]] = []
            while not self.at("}"):
                key = self.parse_expr()
                self.expect(":", ctx=" in map literal")
                pairs.append((key, self.parse_expr()))
                if not self.eat(","):
                    break
            self.no_struct = saved
            self.expect("}", ctx=" to close the map literal")
            return ast.MapLit(self.span_from(t), pairs)
        if k == "|" or k == "||":
            return self.parse_lambda()
        if k in BLOCK_LIKE:
            return self.parse_block_like()
        if k == "return":
            self.advance()
            value = None
            if self.tok.kind not in (";", "}", ")", ",", "EOF"):
                value = self.parse_expr()
            return ast.Return(self.span_from(t), value)
        if k == "break":
            self.advance()
            return ast.Break(self.span_of(t))
        if k == "continue":
            self.advance()
            return ast.Continue(self.span_of(t))
        if k == "let":
            raise self.fail("let-in-expression", "`let` is a statement, not an expression",
                            label="not allowed here", help="put it on its own line ending in `;`")  # fmt: skip
        raise self.fail("expected-expression", f"expected an expression, found {self.describe(t)}",
                        label="expected an expression here")  # fmt: skip

    def _looks_like_struct_lit(self) -> bool:
        # `Name {` followed by `}` or `ident :` or `ident ,` or `ident }`.
        a, b = self.peek(1), self.peek(2)
        if a.kind == "}":
            return True
        return a.kind == "IDENT" and b.kind in (":", ",", "}")

    def parse_struct_lit(self, name: Token) -> ast.StructLit:
        self.expect("{")
        saved, self.no_struct = self.no_struct, False
        fields: list[tuple[str, Span, ast.Expr]] = []
        while not self.at("}"):
            ft = self.ident("a field name")
            if self.eat(":"):
                value = self.parse_expr()
            else:
                value = ast.Name(self.span_of(ft), ft.text)
            fields.append((ft.text, self.span_of(ft), value))
            if not self.eat(","):
                break
        self.no_struct = saved
        self.expect("}", ctx=" to close the struct literal")
        return ast.StructLit(self.span_from(name), name.text, fields)

    def parse_lambda(self) -> ast.Lambda:
        start = self.tok
        params: list[ast.Param] = []
        if self.eat("||"):
            pass
        else:
            self.expect("|")
            while not self.at("|"):
                pt = self.ident("a parameter name")
                ty = self.parse_type() if self.eat(":") else None
                params.append(ast.Param(self.span_of(pt), pt.text, ty))
                if not self.eat(","):
                    break
            self.expect("|", ctx=" after lambda parameters")
        ret = None
        if self.eat("->"):
            ret = self.parse_type()
            body: ast.Expr = self.parse_block()
        else:
            saved, self.no_struct = self.no_struct, False
            body = self.parse_expr()
            self.no_struct = saved
        return ast.Lambda(self.span_from(start), params, ret, body)

    # ------------------------------------------------------------ strings
    def string_expr(self, t: Token) -> ast.Expr:
        parts = t.value
        assert isinstance(parts, list)
        if all(isinstance(p, str) for p in parts):
            return ast.StrLit(self.span_of(t), "".join(p for p in parts if isinstance(p, str)))
        out: list[ast.StrPartAst] = []
        for p in parts:
            if isinstance(p, str):
                if p:
                    out.append(p)
                continue
            a, b = p
            sub_sink = self.sink
            from vex.lexer import Lexer

            toks = Lexer(self.src, sub_sink, a, b).lex()
            sub = Parser(self.src, sub_sink, toks, self.depth + 1)
            if sub.at("EOF"):
                self.sink.error("empty-interpolation", "empty `{}` in string", Span(self.src, a - 1, b + 1),
                                "put an expression inside the braces",
                                help="write `\\{` for a literal brace")  # fmt: skip
                continue
            try:
                e = sub.parse_expr()
                if not sub.at("EOF"):
                    raise sub.fail("expected-token", f"unexpected {sub.describe(sub.tok)} in interpolation",
                                   label="interpolations hold a single expression")  # fmt: skip
                out.append(e)
            except ParseError:
                continue
        return ast.Interp(self.span_of(t), out)

    # ------------------------------------------------------------ patterns
    def parse_pattern(self, allow_mut: bool = False) -> ast.Pat:
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                raise self.fail("too-deep", "pattern is nested too deeply")
            start = self.tok
            first = self.parse_pat_atom(allow_mut)
            if self.at("|"):
                alts = [first]
                while self.eat("|"):
                    alts.append(self.parse_pat_atom(allow_mut))
                return ast.POr(self.span_from(start), alts)
            return first
        finally:
            self.depth -= 1

    def parse_pat_atom(self, allow_mut: bool) -> ast.Pat:
        t = self.tok
        k = t.kind
        if k == "mut":
            if not allow_mut:
                raise self.fail("bad-pattern", "`mut` is only allowed in `let` patterns",
                                label="remove `mut`")  # fmt: skip
            self.advance()
            name = self.ident("a variable name")
            return ast.PBind(self.span_from(t), name.text, True)
        if k == "IDENT":
            if t.text == "_":
                self.advance()
                return ast.PWild(self.span_of(t))
            self.advance()
            path = [t.text]
            while self.at(".") and self.peek().kind == "IDENT":
                self.advance()
                path.append(self.advance().text)
            if self.at("(") and path[0][:1].isupper() or (self.at("(") and len(path) > 1):
                self.advance()
                args: list[ast.Pat] = []
                while not self.at(")"):
                    args.append(self.parse_pattern(allow_mut))
                    if not self.eat(","):
                        break
                self.expect(")", ctx=" in pattern")
                return ast.PCtor(self.span_from(t), path, args)
            if self.at("{") and len(path) == 1 and t.text[:1].isupper():
                self.advance()
                fields: list[tuple[str, ast.Pat]] = []
                rest = False
                while not self.at("}"):
                    if self.eat(".."):
                        rest = True
                        break
                    ft = self.ident("a field name")
                    if self.eat(":"):
                        fields.append((ft.text, self.parse_pattern(allow_mut)))
                    else:
                        fields.append((ft.text, ast.PBind(self.span_of(ft), ft.text, False)))
                    if not self.eat(","):
                        break
                self.expect("}", ctx=" in struct pattern")
                return ast.PStruct(self.span_from(t), t.text, fields, rest)
            if len(path) == 1:
                return ast.PBind(self.span_of(t), t.text, False)
            return ast.PCtor(self.span_from(t), path, None)
        if k == "(":
            self.advance()
            items: list[ast.Pat] = []
            trailing = False
            while not self.at(")"):
                items.append(self.parse_pattern(allow_mut))
                trailing = False
                if not self.eat(","):
                    break
                trailing = True
            self.expect(")", ctx=" in pattern")
            if len(items) == 1 and not trailing:
                return items[0]
            if not items:
                return ast.PLit(self.span_from(t), ast.UnitLit(self.span_from(t)))
            return ast.PTuple(self.span_from(t), items)
        if k in ("INT", "FLOAT", "true", "false") or k == "-":
            return ast.PLit(self.span_of(t), self._literal_pattern_expr())
        if k == "STRING":
            self.advance()
            e = self.string_expr(t)
            if not isinstance(e, ast.StrLit):
                raise self.fail("bad-pattern", "string patterns cannot contain `{}` interpolation", t)
            return ast.PLit(self.span_of(t), e)
        if k in ast_keywords():
            self.advance()
            raise self.fail("keyword-as-name", f"expected a name, found keyword `{t.text}`", t,
                            f"`{t.text}` is a reserved word", f"pick another name, e.g. `{t.text}_`")
        raise self.fail("expected-pattern", f"expected a pattern, found {self.describe(t)}",
                        label="expected a pattern")  # fmt: skip

    def _literal_pattern_expr(self) -> ast.Expr:
        t = self.advance()
        neg = False
        if t.kind == "-":
            neg = True
            t = self.tok
            if t.kind not in ("INT", "FLOAT"):
                raise self.fail("bad-pattern", "expected a number after `-` in pattern")
            self.advance()
        sp = self.span_from(t)
        if t.kind == "INT":
            assert isinstance(t.value, int)
            v = -t.value if neg else t.value
            if v > INT_MAX or v < INT_MIN:
                raise self.fail("int-literal-range", f"integer literal `{t.text}` is out of range", t)
            return ast.IntLit(sp, v)
        if t.kind == "FLOAT":
            assert isinstance(t.value, float)
            return ast.FloatLit(sp, -t.value if neg else t.value)
        return ast.BoolLit(sp, t.kind == "true")


def ast_keywords() -> frozenset[str]:
    from vex.lexer import KEYWORDS

    return KEYWORDS


def parse_source(source: Source, sink: DiagSink, repl: bool = False) -> ast.Module:
    try:
        return Parser(source, sink).parse_module(source.path, repl=repl)
    except RecursionError:
        sink.error("too-deep", "program is nested too deeply to parse", Span(source, 0, 1))
        return ast.Module(source.path, [])


__all__ = ["Parser", "parse_source", "ParseError", "suggest"]
