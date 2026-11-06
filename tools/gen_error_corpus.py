"""Generate tests/errors/*.vx from a table. Run once; the generated files are committed."""
from __future__ import annotations

import os
import sys

OUT = os.path.join(os.path.dirname(__file__), "..", "tests", "errors")

# (name, code, source with @@ marking the primary span start, optional rendered-text substring)
CASES: list[tuple[str, str, str, str]] = [
    # ---- lexer
    ("unterminated_string", "unterminated-string", 'fn main() {\n    let s = @@"abc;\n}\n', ""),
    ("bad_escape", "bad-escape", 'fn main() {\n    print("a@@\\qb");\n}\n', ""),
    ("unexpected_char", "unexpected-character", "fn main() {\n    let x = 3 @@$ 4;\n}\n", ""),
    ("unterminated_comment", "unterminated-comment", "fn main() {\n    @@/* never closed\n}\n", ""),
    ("bad_number", "bad-number", "fn main() {\n    let x = @@12abc;\n}\n", ""),
    ("int_range", "int-literal-range", "fn main() {\n    let x = @@9223372036854775808;\n}\n", ""),
    ("empty_interp", "empty-interpolation", 'fn main() {\n    print("a@@{}b");\n}\n', ""),
    ("unterminated_interp", "unterminated-interpolation", 'fn main() {\n    print("a@@{b");\n}\n', ""),
    # ---- parser
    ("missing_semicolon_let", "expected-token", "fn main() {\n    let x = 5\n    @@let y = 6;\n}\n", "expected `;`"),
    ("expected_expression", "expected-expression", "fn main() {\n    let x = @@;\n}\n", ""),
    ("missing_param_type", "missing-param-type", "fn f(@@x) {}\nfn main() {}\n", "needs a type annotation"),
    ("keyword_as_name", "keyword-as-name", "fn main() {\n    let @@fn = 1;\n}\n", ""),
    ("stray_brace", "unmatched-brace", "fn main() {}\n@@}\n", ""),
    ("unclosed_block", "unclosed-block", "fn main() @@{\n    let x = 1;\n", ""),
    ("toplevel_let", "expected-item", "@@let x = 1;\nfn main() {}\n", "inside `fn main"),
    ("let_in_expr", "let-in-expression", "fn main() {\n    let x = @@let y = 1;\n}\n", ""),
    ("bad_assign_target", "invalid-assign-target", "fn main() {\n    @@1 + 2 = 3;\n}\n", ""),
    ("nested_fn", "nested-item", "fn main() {\n    @@fn inner() {}\n}\n", ""),
    ("missing_paren", "expected-token", "fn main() {\n    print(1@@;\n}\n", ""),
    ("missing_arrow_in_match", "expected-token", "fn main() {\n    match 1 {\n        1 @@2,\n    }\n}\n", ""),
    ("bad_pattern", "expected-pattern", "fn main() {\n    match 1 {\n        @@+ => 1,\n    }\n}\n", ""),
    ("missing_comma_in_arm", "expected-token",
     "fn main() {\n    let x = match 1 {\n        1 => 2\n        @@_ => 3,\n    };\n}\n", ""),
    ("no_expr_after_binary", "expected-expression", "fn main() {\n    let x = 1 + @@;\n}\n", ""),
    ("expected_name", "expected-name", "struct @@{ x: Int }\nfn main() {}\n", ""),
    # ---- resolver
    ("undefined_name", "undefined-name", "fn main() {\n    let total = 1;\n    print(@@totl);\n}\n", "did you mean `total`"),
    ("undefined_function", "undefined-name", "fn main() {\n    @@pritn(1);\n}\n", "did you mean `print`"),
    ("unknown_type", "unknown-type", "fn f(x: @@Integer) {}\nfn main() {}\n", ""),
    ("unknown_type_suggest", "unknown-type", "struct Point { x: Int }\nfn f(p: @@Pont) {}\nfn main() {}\n", "did you mean `Point`"),
    ("unknown_variant_pat", "unknown-variant",
     "enum E { A, B }\nfn main() {\n    match E.A {\n        E.A => 1,\n        @@E.C => 2,\n    };\n}\n", ""),
    ("unknown_math_member", "unknown-member", "fn main() {\n    print(math.@@sqr(2.0));\n}\n", "did you mean `sqrt`"),
    ("unknown_enum_member", "unknown-member",
     "enum Color { Red, Green }\nfn main() {\n    let c = Color.@@Blue;\n}\n", ""),
    ("duplicate_fn", "duplicate-definition", "fn f() {}\nfn @@f() {}\nfn main() {}\n", ""),
    ("duplicate_struct", "duplicate-definition", "struct P { x: Int }\n@@struct P { y: Int }\nfn main() {}\n", ""),
    ("break_outside_loop", "outside-loop", "fn main() {\n    @@break;\n}\n", ""),
    ("continue_outside_loop", "outside-loop", "fn main() {\n    @@continue;\n}\n", ""),
    ("list_arity", "type-arity", "fn f(x: @@List[Int, Int]) {}\nfn main() {}\n", ""),
    ("reserved_name", "reserved-name", "fn @@print() {}\nfn main() {}\n", ""),
    ("self_outside_impl", "self-outside-impl", "fn @@f(self) {}\nfn main() {}\n", ""),
    ("ambiguous_variant", "ambiguous-name",
     "enum A { X }\nenum B { X }\nfn main() {\n    let v = @@X;\n}\n", "A.X"),
    ("import_missing", "import-not-found", "@@import nothing_here;\nfn main() {}\n", ""),
    ("bad_impl_target", "bad-impl-target", "impl @@Int {\n    fn f(self) {}\n}\nfn main() {}\n", ""),
    ("or_pattern_bindings", "or-pattern-bindings",
     "fn main() {\n    match (1, 2) {\n        (a, 0) | (@@b, 1) => 1,\n        _ => 2,\n    };\n}\n", ""),
    ("duplicate_binding", "duplicate-binding", "fn main() {\n    let (a, @@a) = (1, 2);\n}\n", ""),
    ("pattern_variant_arity", "variant-arity",
     "enum E { A(Int, Int) }\nfn main() {\n    match E.A(1, 2) {\n        @@E.A(x) => x,\n    };\n}\n", ""),
    ("unknown_bound", "unknown-bound", "fn f[@@T: Numeric](x: T) {}\nfn main() {}\n", ""),
    ("bad_map_key_annot", "bad-map-key", "fn f(m: @@Map[List[Int], Int]) {}\nfn main() {}\n", ""),
    ("struct_unknown_in_literal", "unknown-type", "fn main() {\n    let p = @@Pt { x: 1 };\n}\n", ""),
    ("enum_as_struct", "not-a-struct", "enum E { A }\nfn main() {\n    let p = @@E { x: 1 };\n}\n", ""),
    ("duplicate_param", "duplicate-definition", "fn f(a: Int, @@a: Int) {}\nfn main() {}\n", ""),
    ("duplicate_variant", "duplicate-definition", "enum E { A, @@A }\nfn main() {}\n", ""),
    ("duplicate_field_decl", "duplicate-definition", "struct S { a: Int, @@a: Int }\nfn main() {}\n", ""),
    # ---- checker: mismatches
    ("let_annotation_mismatch", "type-mismatch", 'fn main() {\n    let x: Int = @@"hi";\n}\n', "expected `Int`, found `Str`"),
    ("arg_mismatch", "type-mismatch", 'fn f(n: Int) {}\nfn main() {\n    f(@@"x");\n}\n', "argument 1 of `f`"),
    ("return_mismatch", "type-mismatch", 'fn f() -> Int {\n    @@"s"\n}\nfn main() {}\n', ""),
    ("if_cond_not_bool", "type-mismatch", "fn main() {\n    if @@1 { }\n}\n", "must be Bool"),
    ("branch_mismatch", "type-mismatch",
     'fn main() {\n    let x = if true { 1 } else { @@"a" };\n}\n', ""),
    ("arm_mismatch", "type-mismatch",
     'fn main() {\n    let x = match 1 {\n        1 => 1,\n        _ => @@"a",\n    };\n}\n', ""),
    ("int_float_mix", "type-mismatch", "fn main() {\n    let x = @@1 + 2.0;\n}\n", ".to_float()"),
    ("add_str_int", "type-mismatch", 'fn main() {\n    let x = @@"a" + 1;\n}\n', "interpolation"),
    ("list_elem_mismatch", "type-mismatch", 'fn main() {\n    let xs = [1, @@"a"];\n}\n', ""),
    ("struct_field_mismatch", "type-mismatch",
     'struct P { x: Int }\nfn main() {\n    let p = P { x: @@"s" };\n}\n', "field `x`"),
    ("compound_assign_mismatch", "type-mismatch", 'fn main() {\n    let mut x = 1;\n    @@x += "a";\n}\n', ""),
    ("option_for_value", "type-mismatch", "fn f() -> Option[Int] {\n    @@5\n}\nfn main() {}\n", "Some("),
    ("unit_value", "type-mismatch", "fn main() {\n    let x: Int = @@print(1);\n}\n", ""),
    ("if_no_else_value", "type-mismatch", "fn main() {\n    let x = if true { @@1 };\n}\n", ""),
    ("missing_final_expr", "type-mismatch", "fn f() -> Int {\n    let x = 1;\n    x + 1;\n@@}\nfn main() {}\n", "remove the trailing `;`"),
    ("return_value_in_unit_fn", "type-mismatch", "fn f() -> Unit {\n    return @@1;\n}\nfn main() {}\n", ""),
    ("wrong_generic_arg", "type-mismatch", 'fn main() {\n    let xs: List[Int] = [@@"a"];\n}\n', ""),
    ("option_plus_int", "type-mismatch", "fn main() {\n    let x = @@Some(1) + 2;\n}\n", ""),
    ("method_arg_mismatch", "type-mismatch", 'fn main() {\n    let mut xs = [1];\n    xs.push(@@"a");\n}\n', ""),
    ("call_arity_many", "arity-mismatch", "fn f(a: Int) {}\nfn main() {\n    @@f(1, 2);\n}\n", "takes 1 argument"),
    ("call_arity_few", "arity-mismatch", "fn f(a: Int, b: Int) {}\nfn main() {\n    @@f(1);\n}\n", ""),
    ("lambda_arity", "arity-mismatch", "fn main() {\n    let xs = [1].map(@@|a, b| a);\n}\n", ""),
    ("not_callable", "not-callable", "fn main() {\n    let x = 1;\n    @@x(2);\n}\n", ""),
    ("no_field", "no-field", "struct P { x: Int }\nfn main() {\n    let p = P { x: 1 };\n    print(p.@@y);\n}\n", ""),
    ("no_field_suggest", "no-field", "struct P { width: Int }\nfn main() {\n    let p = P { width: 1 };\n    print(p.@@widht);\n}\n", "did you mean `width`"),
    ("no_method", "no-method", "fn main() {\n    let xs = [1];\n    xs.@@lenght();\n}\n", "did you mean `len`"),
    ("method_on_int", "no-method", "fn main() {\n    let n = 1;\n    n.@@push(2);\n}\n", ""),
    ("tuple_field_oob", "no-field", "fn main() {\n    let t = (1, 2);\n    print(t.@@5);\n}\n", ""),
    ("field_is_method", "no-field", "struct S {}\nimpl S {\n    fn go(self) {}\n}\nfn main() {\n    let s = S {};\n    s.@@go;\n}\n", ""),
    ("missing_struct_fields", "missing-fields", "struct P { x: Int, y: Int }\nfn main() {\n    let p = @@P { x: 1 };\n}\n", "`y`"),
    ("unknown_struct_field_lit", "no-field", "struct P { x: Int }\nfn main() {\n    let p = P { x: 1, @@z: 2 };\n}\n", ""),
    ("duplicate_field_lit", "duplicate-field", "struct P { x: Int }\nfn main() {\n    let p = P { x: 1, @@x: 2 };\n}\n", ""),
    ("not_a_method", "not-a-method", "struct S {}\nimpl S {\n    fn make() -> S { S {} }\n}\nfn main() {\n    let s = S {};\n    s.@@make();\n}\n", ""),
    ("unknown_assoc_fn", "unknown-member", "struct S {}\nfn main() {\n    let s = S.@@build();\n}\n", ""),
    # ---- checker: mutability
    ("assign_immutable", "immutable-binding", "fn main() {\n    let x = 1;\n    @@x = 2;\n}\n", "let mut x"),
    ("push_immutable", "immutable-binding", "fn main() {\n    let xs = [1];\n    xs.@@push(2);\n}\n", ""),
    ("assign_param", "immutable-binding", "fn f(n: Int) {\n    @@n = 2;\n}\nfn main() {}\n", "parameters are immutable"),
    ("field_assign_immutable", "immutable-binding", "struct P { x: Int }\nfn main() {\n    let p = P { x: 1 };\n    @@p.x = 2;\n}\n", ""),
    ("index_assign_immutable", "immutable-binding", "fn main() {\n    let xs = [1];\n    @@xs[0] = 2;\n}\n", ""),
    ("mut_self_needed", "immutable-binding", "struct C { n: Int }\nimpl C {\n    fn bump(self) {\n        @@self.n = 1;\n    }\n}\nfn main() {}\n", "mut self"),
    ("call_mut_method_on_immutable", "immutable-binding", "struct C { n: Int }\nimpl C {\n    fn bump(mut self) { self.n += 1; }\n}\nfn main() {\n    let c = C { n: 0 };\n    c.@@bump();\n}\n", ""),
    ("tuple_assign", "immutable-binding", "fn main() {\n    let mut t = (1, 2);\n    @@t.0 = 5;\n}\n", ""),
    ("pass_immutable_to_mut_param", "immutable-binding", "struct C { n: Int }\nfn bump(mut c: C) { c.n += 1; }\nfn main() {\n    let c = C { n: 0 };\n    bump(@@c);\n}\n", ""),
    ("mut_fn_as_value", "mut-fn-value", "struct C { n: Int }\nfn bump(mut c: C) { c.n += 1; }\nfn main() {\n    let f = @@bump;\n}\n", ""),
    ("assign_to_fn", "bad-assign", "fn f() {}\nfn main() {\n    @@f = 1;\n}\n", ""),
    # ---- checker: patterns
    ("non_exhaustive_enum", "non-exhaustive-match",
     "enum Shape { Circle(Float), Square(Float), Dot }\nfn main() {\n    match Shape.Dot {\n        Shape.Circle(r) => 1,\n        Shape.Square(s) => 2,\n    };\n}\n".replace("match Shape.Dot", "match @@Shape.Dot"), "Shape.Dot"),
    ("non_exhaustive_int", "non-exhaustive-match", "fn main() {\n    match @@5 {\n        1 => 1,\n        2 => 2,\n    };\n}\n", ""),
    ("non_exhaustive_bool", "non-exhaustive-match", "fn main() {\n    match @@true {\n        true => 1,\n    };\n}\n", "false"),
    ("non_exhaustive_option", "non-exhaustive-match", "fn main() {\n    match @@Some(1) {\n        Some(x) => x,\n    };\n}\n", "None"),
    ("non_exhaustive_nested", "non-exhaustive-match",
     "fn main() {\n    match @@(Some(1), true) {\n        (Some(_), true) => 1,\n        (None, _) => 2,\n    };\n}\n", "Some(_), false"),
    ("non_exhaustive_guard", "non-exhaustive-match", "fn main() {\n    match @@1 {\n        x if x > 0 => 1,\n    };\n}\n", ""),
    ("non_exhaustive_empty", "non-exhaustive-match", "fn main() {\n    match @@1 {\n    };\n}\n", ""),
    ("refutable_let", "refutable-pattern", "fn main() {\n    let @@Some(x) = Some(1);\n}\n", "None"),
    ("refutable_for", "refutable-pattern", "fn main() {\n    for @@Some(x) in [Some(1)] { }\n}\n", ""),
    ("pattern_type_mismatch", "type-mismatch", 'fn main() {\n    match 1 {\n        @@"a" => 1,\n        _ => 2,\n    };\n}\n', ""),
    ("tuple_pattern_mismatch", "pattern-mismatch", "fn main() {\n    let @@(a, b, c) = (1, 2);\n}\n", ""),
    ("enum_pattern_mismatch", "type-mismatch", "fn main() {\n    match 1 {\n        @@Some(x) => 1,\n        _ => 2,\n    };\n}\n", ""),
    ("guard_not_bool", "type-mismatch", "fn main() {\n    match 1 {\n        x if @@x => 1,\n        _ => 2,\n    };\n}\n", ""),
    # ---- checker: inference
    ("cannot_infer_empty_list", "cannot-infer", "fn main() {\n    let xs = @@[];\n}\n", ""),
    ("cannot_infer_none", "cannot-infer", "fn main() {\n    let x = @@None;\n}\n", ""),
    ("cannot_infer_lambda", "cannot-infer", "fn main() {\n    let f = @@|x| x;\n}\n", ""),
    ("method_on_unknown", "cannot-infer", "fn main() {\n    let f = |x| x.@@len();\n}\n", ""),
    ("infinite_type", "infinite-type", "fn main() {\n    let f = |x| @@x(x);\n}\n", ""),
    ("recursive_generic_no_ret", "recursive-inference", "fn f[T](x: T) {\n    @@f(x)\n}\nfn main() {}\n", ""),
    # ---- checker: ? operator
    ("try_in_int_fn", "bad-try", "fn f() -> Int {\n    let x = @@Some(1)?;\n    x\n}\nfn main() {}\n", ""),
    ("try_on_int", "bad-try", "fn f() -> Option[Int] {\n    let x = @@1?;\n    Some(x)\n}\nfn main() {}\n", ""),
    ("try_error_types", "bad-try", 'fn g() -> Result[Int, Str] { Ok(1) }\nfn f() -> Result[Int, Int] {\n    let x = @@g()?;\n    Ok(x)\n}\nfn main() {}\n', "map_err"),
    ("try_result_in_option_fn", "bad-try", 'fn g() -> Result[Int, Str] { Ok(1) }\nfn f() -> Option[Int] {\n    let x = @@g()?;\n    Some(x)\n}\nfn main() {}\n', ""),
    # ---- checker: operators, indexing, iteration
    ("bool_plus_int", "type-mismatch", "fn main() {\n    let x = @@true + 1;\n}\n", ""),
    ("negate_str", "bad-operand", 'fn main() {\n    let x = @@-"a";\n}\n', ""),
    ("bool_arith", "bad-operand", "fn main() {\n    let x = @@true - false;\n}\n", ""),
    ("compare_functions", "bad-operand", "fn f() {}\nfn g() {}\nfn main() {\n    let x = @@f == g;\n}\n", ""),
    ("order_structs", "bad-operand", "struct P { x: Int }\nfn main() {\n    let x = @@P { x: 1 } < P { x: 2 };\n}\n", ""),
    ("not_on_int", "type-mismatch", "fn main() {\n    let x = !@@1;\n}\n", ""),
    ("index_int", "not-indexable", "fn main() {\n    let n = 5;\n    print(@@n[0]);\n}\n", ""),
    ("index_str", "not-indexable", 'fn main() {\n    let s = "abc";\n    print(@@s[0]);\n}\n', "char_at"),
    ("index_list_with_str", "type-mismatch", 'fn main() {\n    let xs = [1];\n    print(xs[@@"a"]);\n}\n', ""),
    ("iterate_map", "not-iterable", "fn main() {\n    let m = #{1: 2};\n    for x in @@m { }\n}\n", "entries"),
    ("iterate_int", "not-iterable", "fn main() {\n    for x in @@5 { }\n}\n", ""),
    ("iterate_str", "not-iterable", 'fn main() {\n    for c in @@"abc" { }\n}\n', "chars"),
    ("range_bounds", "type-mismatch", "fn main() {\n    for i in 0..@@2.5 { }\n}\n", ""),
    # ---- checker: values vs types, generics, main
    ("struct_as_value", "not-a-value", "struct P { x: Int }\nfn main() {\n    let p = @@P;\n}\n", ""),
    ("module_as_value", "not-a-value", "fn main() {\n    let m = @@math;\n}\n", ""),
    ("unsatisfied_ord", "unsatisfied-bound", "struct P { x: Int }\nfn big[T: Ord](a: T, b: T) -> T { a }\nfn main() {\n    let p = big(@@P { x: 1 }, P { x: 2 });\n}\n".replace("big(@@P", "@@big(P"), ""),
    ("generic_no_ord", "bad-operand", "fn bigger[T](a: T, b: T) -> Bool {\n    @@a > b\n}\nfn main() {}\n", ""),
    ("generic_no_eq", "bad-operand", "fn same[T](a: T, b: T) -> Bool {\n    @@a == b\n}\nfn main() {}\n", ""),
    ("generic_plus", "bad-operand", "fn twice[T](a: T) -> T {\n    @@a + a\n}\nfn main() {}\n", ""),
    ("map_bad_key_inferred", "bad-map-key", "fn main() {\n    let m = @@#{[1]: 2};\n}\n", ""),
    ("no_main", "no-main", "fn helper() {}\n", ""),
    ("main_with_params", "bad-main", "fn @@main(x: Int) {}\n", ""),
    ("main_wrong_return", "bad-main", 'fn @@main() -> Str { "x" }\n', ""),
    ("let_type_arity", "type-arity", "fn main() {\n    let x: @@Option = None;\n}\n", ""),
    ("assert_eq_functions", "unsatisfied-bound", "fn f() {}\nfn main() {\n    @@assert_eq(f, f);\n}\n", ""),
    ("self_type_outside", "self-outside-impl", "fn f(x: @@Self) {}\nfn main() {}\n", ""),
    ("break_in_lambda", "outside-loop", "fn main() {\n    while true {\n        let f = || @@break;\n    }\n}\n", ""),
]


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for old in os.listdir(OUT):
        if old.endswith(".vx"):
            os.remove(os.path.join(OUT, old))
    seen = set()
    for name, code, src, substr in CASES:
        assert name not in seen, name
        seen.add(name)
        assert src.count("@@") <= 1, name
        if "@@" in src:
            before, after = src.split("@@")
            line = before.count("\n") + 2  # +1 for 1-based, +1 for the header comment
            col = len(before) - (before.rfind("\n") + 1) + 1
            where = f"{line}:{col}"
            text = before + after
        else:
            where, text = "-", src
        header = f"// expect: {code} {where}" + (f" | {substr}" if substr else "")
        with open(os.path.join(OUT, name + ".vx"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header + "\n" + text)
    print(len(CASES), "cases", file=sys.stderr)


if __name__ == "__main__":
    main()
