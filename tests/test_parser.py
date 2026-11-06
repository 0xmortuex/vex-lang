"""Parser unit tests: precedence, shapes of nodes, spans and error recovery."""

from __future__ import annotations

import pytest

from vex import syntax as ast
from vex.diagnostics import DiagSink, Source
from vex.parser import parse_source


def parse(text: str) -> tuple[ast.Module, DiagSink]:
    sink = DiagSink()
    return parse_source(Source("<t>", text), sink), sink


def expr(text: str) -> ast.Expr:
    """Parse ``text`` as the tail expression of main."""
    mod, sink = parse("fn main() { " + text + " }")
    assert not sink.items, [d.message for d in sink.items]
    fn = mod.items[0]
    assert isinstance(fn, ast.FnDecl)
    assert fn.body.tail is not None
    return fn.body.tail


def dump(e: ast.Node) -> str:
    if isinstance(e, ast.IntLit | ast.FloatLit):
        return str(e.value)
    if isinstance(e, ast.BoolLit):
        return "true" if e.value else "false"
    if isinstance(e, ast.StrLit):
        return repr(e.value)
    if isinstance(e, ast.UnitLit):
        return "()"
    if isinstance(e, ast.Name):
        return e.name
    if isinstance(e, ast.Binary):
        return f"({e.op} {dump(e.left)} {dump(e.right)})"
    if isinstance(e, ast.Unary):
        return f"({e.op} {dump(e.operand)})"
    if isinstance(e, ast.Call):
        return f"(call {dump(e.callee)}{''.join(' ' + dump(a) for a in e.args)})"
    if isinstance(e, ast.Field):
        return f"(. {dump(e.obj)} {e.name})"
    if isinstance(e, ast.Index):
        return f"(idx {dump(e.obj)} {dump(e.index)})"
    if isinstance(e, ast.Try):
        return f"(? {dump(e.operand)})"
    if isinstance(e, ast.RangeExpr):
        return f"({'..=' if e.inclusive else '..'} {dump(e.lo)} {dump(e.hi)})"
    if isinstance(e, ast.ListLit):
        return "[" + " ".join(dump(i) for i in e.items) + "]"
    if isinstance(e, ast.TupleLit):
        return "(tuple " + " ".join(dump(i) for i in e.items) + ")"
    if isinstance(e, ast.MapLit):
        return "#{" + " ".join(f"{dump(k)}:{dump(v)}" for k, v in e.pairs) + "}"
    if isinstance(e, ast.Lambda):
        return f"(lambda ({' '.join(p.name for p in e.params)}) {dump(e.body)})"
    if isinstance(e, ast.If):
        return f"(if {dump(e.cond)} {dump(e.then)}" + (f" {dump(e.els)})" if e.els else ")")
    if isinstance(e, ast.Block):
        parts = [dump(s.expr) if isinstance(s, ast.ExprStmt) else "let" for s in e.stmts]
        if e.tail is not None:
            parts.append("=>" + dump(e.tail))
        return "{" + " ".join(parts) + "}"
    if isinstance(e, ast.Match):
        return f"(match {dump(e.scrut)} {len(e.arms)})"
    if isinstance(e, ast.Interp):
        return "(interp " + " ".join(repr(p) if isinstance(p, str) else dump(p) for p in e.parts) + ")"
    if isinstance(e, ast.StructLit):
        return f"({e.name} " + " ".join(f"{n}={dump(v)}" for n, _, v in e.fields) + ")"
    if isinstance(e, ast.Assign):
        return f"({e.op} {dump(e.target)} {dump(e.value)})"
    if isinstance(e, ast.Return):
        return f"(return {dump(e.value) if e.value else ''})".replace(" )", ")")
    if isinstance(e, ast.Break):
        return "break"
    if isinstance(e, ast.Continue):
        return "continue"
    if isinstance(e, ast.While):
        return f"(while {dump(e.cond)} {dump(e.body)})"
    if isinstance(e, ast.For):
        return f"(for {dump(e.iter)} {dump(e.body)})"
    return type(e).__name__


@pytest.mark.parametrize(
    ("src", "tree"),
    [
        ("1 + 2 * 3", "(+ 1 (* 2 3))"),
        ("1 * 2 + 3", "(+ (* 1 2) 3)"),
        ("1 - 2 - 3", "(- (- 1 2) 3)"),
        ("1 / 2 / 3", "(/ (/ 1 2) 3)"),
        ("(1 + 2) * 3", "(* (+ 1 2) 3)"),
        ("a || b && c", "(|| a (&& b c))"),
        ("a && b || c", "(|| (&& a b) c)"),
        ("a == b && c != d", "(&& (== a b) (!= c d))"),
        ("a < b == c > d", "(== (< a b) (> c d))"),
        ("a + b < c * d", "(< (+ a b) (* c d))"),
        ("-a * b", "(* (- a) b)"),
        ("!a && b", "(&& (! a) b)"),
        ("- -a", "(- (- a))"),
        ("a % b * c", "(* (% a b) c)"),
        ("0..n + 1", "(.. 0 (+ n 1))"),
        ("0..=10", "(..= 0 10)"),
        ("a.b.c", "(. (. a b) c)"),
        ("a.b(1, 2)", "(call (. a b) 1 2)"),
        ("f(1)(2)", "(call (call f 1) 2)"),
        ("a[0][1]", "(idx (idx a 0) 1)"),
        ("a.b[0].c", "(. (idx (. a b) 0) c)"),
        ("f(x)?", "(? (call f x))"),
        ("a?.b", "(. (? a) b)"),
        ("t.0.1", "(. (. t 0) 1)"),
        ("[]", "[]"),
        ("[1, 2, 3]", "[1 2 3]"),
        ("[1, 2,]", "[1 2]"),
        ("()", "()"),
        ("(1,)", "(tuple 1)"),
        ("(1, 2)", "(tuple 1 2)"),
        ("(1)", "1"),
        ("#{}", "#{}"),
        ('#{"a": 1, "b": 2}', "#{'a':1 'b':2}"),
        ("|x| x + 1", "(lambda (x) (+ x 1))"),
        ("|a, b| a * b", "(lambda (a b) (* a b))"),
        ("|| 5", "(lambda () 5)"),
        ("|x: Int| -> Int { x }", "(lambda (x) {=>x})"),
        ("f(|x| x, 2)", "(call f (lambda (x) x) 2)"),
        ("if a { 1 } else { 2 }", "(if a {=>1} {=>2})"),
        ("if a { 1 } else if b { 2 } else { 3 }", "(if a {=>1} (if b {=>2} {=>3}))"),
        ("{ 1; 2 }", "{1 =>2}"),
        ("Point { x: 1, y: 2 }", "(Point x=1 y=2)"),
        ("Point { x, y }", "(Point x=x y=y)"),
        ("Point {}", "(Point )"),
        ("x = 5", "(= x 5)"),
        ('"a {x} b"', "(interp 'a ' x ' b')"),
        ('"{a + b}"', "(interp (+ a b))"),
        ('"plain"', "'plain'"),
        ("return", "(return)"),
        ("return 1 + 2", "(return (+ 1 2))"),
        ("-9223372036854775808", "-9223372036854775808"),
        ("x.len() + 1", "(+ (call (. x len)) 1)"),
        ("a.b = c", "(= (. a b) c)"),
        ("a[i] += 1", "(+= (idx a i) 1)"),
        ("true && false", "(&& true false)"),
        ("1.5 * 2.0", "(* 1.5 2.0)"),
    ],
)
def test_expression_shapes(src: str, tree: str) -> None:
    mod, sink = parse("fn main() { " + src + " }")
    assert not sink.items, [d.message for d in sink.items]
    fn = mod.items[0]
    assert isinstance(fn, ast.FnDecl)
    node: ast.Node
    if fn.body.tail is not None:
        node = fn.body.tail
    else:
        stmt = fn.body.stmts[-1]
        assert isinstance(stmt, ast.ExprStmt)
        node = stmt.expr
    assert dump(node) == tree


def test_struct_literal_not_allowed_in_condition() -> None:
    e = expr("if x == y { 1 } else { 2 }")
    assert isinstance(e, ast.If)
    assert dump(e.cond) == "(== x y)"


def test_struct_literal_allowed_in_parens_inside_condition() -> None:
    e = expr("if (P { x: 1 }) == q { 1 } else { 2 }")
    assert isinstance(e, ast.If) and dump(e.cond) == "(== (P x=1) q)"


def test_match_arms_shapes() -> None:
    e = expr("match x { 1 => 2, Some(y) if y > 0 => y, _ => { 0 } }")
    assert isinstance(e, ast.Match) and len(e.arms) == 3
    assert isinstance(e.arms[0].pat, ast.PLit)
    assert isinstance(e.arms[1].pat, ast.PCtor) and e.arms[1].guard is not None
    assert isinstance(e.arms[2].pat, ast.PWild)


def test_pattern_kinds() -> None:
    mod, sink = parse("fn main() { match x { (a, b) => 1, P { x, y: 0 } => 2, E.V(a) | E.W => 3, -1 => 4, _ => 5 } }")
    assert not sink.items
    fn = mod.items[0]
    assert isinstance(fn, ast.FnDecl) and isinstance(fn.body.tail, ast.Match)
    kinds = [type(a.pat).__name__ for a in fn.body.tail.arms]
    assert kinds == ["PTuple", "PStruct", "POr", "PLit", "PWild"]


def test_block_like_statements_need_no_semicolon() -> None:
    mod, sink = parse("fn main() { if a { } while b { } for i in c { } match d { _ => 1 } let x = 1; x }")
    assert not sink.items
    fn = mod.items[0]
    assert isinstance(fn, ast.FnDecl)
    assert len(fn.body.stmts) == 5 and fn.body.tail is not None


def test_block_like_statement_does_not_swallow_negative_literal() -> None:
    mod, sink = parse("fn main() { for i in 0..3 { } -1 }")
    assert not sink.items
    fn = mod.items[0]
    assert isinstance(fn, ast.FnDecl) and isinstance(fn.body.tail, ast.Unary)


def test_items_parse() -> None:
    mod, sink = parse(
        """
import util;
struct P[T] { x: T, y: Int, }
enum E[T] { A, B(T, Int), C(List[T]) }
impl[T] P[T] { fn get(self) -> T { self.x } fn set(mut self, v: T) { self.x = v; } fn make() -> Int { 1 } }
fn id[T: Ord + Eq](x: T) -> T { x }
fn main() {}
"""
    )
    assert not sink.items, [d.message for d in sink.items]
    kinds = [type(i).__name__ for i in mod.items]
    assert kinds == ["Import", "StructDecl", "EnumDecl", "ImplDecl", "FnDecl", "FnDecl"]
    impl = mod.items[3]
    assert isinstance(impl, ast.ImplDecl)
    assert [m.self_mode for m in impl.methods] == ["self", "mut self", None]
    fn = mod.items[4]
    assert isinstance(fn, ast.FnDecl) and fn.tparams[0].bounds == ["Ord", "Eq"]


def test_type_expressions() -> None:
    mod, sink = parse("fn f(a: List[Int], b: Map[Str, (Int, Bool)], c: fn(Int) -> Int, d: (), e: (Int,)) {}")
    assert not sink.items
    fn = mod.items[0]
    assert isinstance(fn, ast.FnDecl)
    tys = [p.ty_expr for p in fn.params]
    assert isinstance(tys[0], ast.TyName) and tys[0].args[0].span is not None
    assert isinstance(tys[1], ast.TyName) and isinstance(tys[1].args[1], ast.TyTuple)
    assert isinstance(tys[2], ast.TyFn)
    assert isinstance(tys[3], ast.TyTuple) and tys[3].items == []
    assert isinstance(tys[4], ast.TyTuple) and len(tys[4].items) == 1


def test_spans_cover_the_source_text() -> None:
    text = "fn main() { let x = 1 + 22; }"
    mod, _ = parse(text)
    fn = mod.items[0]
    assert isinstance(fn, ast.FnDecl)
    let = fn.body.stmts[0]
    assert isinstance(let, ast.Let)
    init = let.init
    assert text[init.span.start : init.span.end] == "1 + 22"
    assert init.span.line == 1 and init.span.col == 21


def test_interpolation_spans_point_into_the_string() -> None:
    text = 'fn main() { "ab {x + 1} cd" }'
    mod, _ = parse(text)
    fn = mod.items[0]
    assert isinstance(fn, ast.FnDecl) and isinstance(fn.body.tail, ast.Interp)
    inner = fn.body.tail.parts[1]
    assert isinstance(inner, ast.Binary)
    assert text[inner.span.start : inner.span.end] == "x + 1"


# ---------------------------------------------------------------- syntax errors
@pytest.mark.parametrize(
    ("src", "code"),
    [
        ("fn main() { let x = ; }", "expected-expression"),
        ("fn main() { let x 5; }", "expected-token"),
        ("fn main( { }", "keyword-as-name" if False else "expected-name"),
        ("fn () {}", "expected-name"),
        ("fn main() { 1 + ; }", "expected-expression"),
        ("fn main() { f(1, ; }", "expected-expression"),
        ("fn main() { x.; }", "expected-name"),
        ("fn main() { [1, 2 }", "expected-token"),
        ("struct { }", "expected-name"),
        ("enum E { A( }", "expected-name"),
        ("fn main() { match x { 1 => } }", "expected-expression"),
        ("fn main() { if { } }", "expected-token"),
        ("fn main() { let = 1; }", "expected-pattern"),
        ("let x = 1;", "expected-item"),
        ("1 + 2", "expected-item"),
        ("fn main() { a b }", "expected-semicolon"),
        ("fn main() { return 1 2 }", "expected-semicolon"),
        ("impl X { let y = 1; }", "expected-fn"),
        ("fn main() { 1 = 2; }", "invalid-assign-target"),
        ("fn f(x) {}", "missing-param-type"),
        ("fn main() { while { } }", "expected-token"),
        ("fn main() { for in x { } }", "keyword-as-name"),
        ("fn main() { |x| }", "expected-expression"),
        ("fn main() { 99999999999999999999 }", "int-literal-range"),
    ],
)
def test_syntax_error_codes(src: str, code: str) -> None:
    _, sink = parse(src)
    assert code in [d.code for d in sink.items], [d.code for d in sink.items]


def test_multiple_syntax_errors_are_reported() -> None:
    src = """
fn a() { let x = ; }
fn b() { let y = ; }
fn c() { let z = ; }
fn main() { }
"""
    _, sink = parse(src)
    assert len([d for d in sink.items if d.code == "expected-expression"]) == 3


def test_recovery_continues_after_bad_statement() -> None:
    src = "fn main() { let x = ; let y = 2; let z = ; }"
    _, sink = parse(src)
    assert len(sink.items) == 2


def test_recovery_after_bad_item_finds_next_item() -> None:
    src = "fn broken( { }\nfn good() { let x = ; }\n"
    _, sink = parse(src)
    codes = [d.code for d in sink.items]
    assert "expected-expression" in codes


def test_parser_never_loops_on_garbage() -> None:
    for junk in ["}}}}", "((((", "fn fn fn", "let let let", "struct struct", "{ { { {", ")))", "]]]", "match match", "@@@@"]:
        parse(junk)


def test_deep_nesting_is_a_diagnostic_not_a_crash() -> None:
    _, sink = parse("fn main() { " + "(" * 400 + "1" + ")" * 400 + " }")
    assert "too-deep" in [d.code for d in sink.items]


def test_deep_unary_nesting_is_a_diagnostic() -> None:
    _, sink = parse("fn main() { " + "-" * 500 + "1 }")
    assert "too-deep" in [d.code for d in sink.items]


def test_repl_mode_accepts_statements_and_tail() -> None:
    sink = DiagSink()
    mod = parse_source(Source("<r>", "let x = 1; x + 1"), sink, repl=True)
    assert not sink.items
    assert len(mod.stmts) == 1 and mod.tail is not None
