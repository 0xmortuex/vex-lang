"""Behavioural tests: small programs with their exact expected output."""

from __future__ import annotations

import pytest
from helpers import run_body, run_source

# (id, body of main, expected stdout)
BODIES: list[tuple[str, str, str]] = [
    # ---- integers
    ("int_add", "print(1 + 2);", "3\n"),
    ("int_precedence", "print(2 + 3 * 4);", "14\n"),
    ("int_parens", "print((2 + 3) * 4);", "20\n"),
    ("int_sub_neg", "print(5 - 8);", "-3\n"),
    ("int_unary_minus", "let x = 4; print(-x);", "-4\n"),
    ("int_div_trunc", "print(7 / 2);", "3\n"),
    ("int_div_neg_trunc", "print(-7 / 2);", "-3\n"),
    ("int_div_both_neg", "print(-7 / -2);", "3\n"),
    ("int_mod", "print(7 % 3);", "1\n"),
    ("int_mod_neg_dividend", "print(-7 % 3);", "-1\n"),
    ("int_mod_neg_divisor", "print(7 % -3);", "1\n"),
    ("int_max", "print(9223372036854775807);", "9223372036854775807\n"),
    ("int_min_literal", "print(-9223372036854775808);", "-9223372036854775808\n"),
    ("int_hex", "print(0xff + 0b101 + 0o17);", "275\n"),
    ("int_underscores", "print(1_000_000);", "1000000\n"),
    ("int_compare_chain", "print(1 < 2 && 2 <= 2 && 3 > 2 && 3 >= 3 && 1 != 2);", "true\n"),
    ("int_pow", "print(2.pow(10));", "1024\n"),
    ("int_abs_min_max", "print((-5).abs()); print(3.max(9)); print(3.min(9));", "5\n9\n3\n"),
    ("int_signum", "print((-9).signum()); print(0.signum()); print(4.signum());", "-1\n0\n1\n"),
    ("int_to_float", "print(3.to_float() / 2.0);", "1.5\n"),
    ("int_to_str", "print(42.to_str() + \"!\");", "42!\n"),
    ("int_gcd", "print(math.gcd(12, 18));", "6\n"),
    ("compound_assign_all", "let mut x = 10; x += 5; x -= 3; x *= 2; x /= 4; x %= 4; print(x);", "2\n"),
    # ---- floats
    ("float_basic", "print(1.5 + 2.25);", "3.75\n"),
    ("float_whole_prints_dot_zero", "print(2.0 * 3.0);", "6.0\n"),
    ("float_div", "print(1.0 / 4.0);", "0.25\n"),
    ("float_sci", "print(1e3);", "1000.0\n"),
    ("float_neg", "print(-2.5);", "-2.5\n"),
    ("float_floor_ceil_round", "print(2.7.floor()); print(2.1.ceil()); print(2.5.round());", "2.0\n3.0\n2.0\n"),
    ("float_to_int_trunc", "print(2.9.to_int()); print((-2.9).to_int());", "2\n-2\n"),
    ("float_sqrt", "print(math.sqrt(16.0));", "4.0\n"),
    ("float_sqrt_negative_is_nan", "print(math.sqrt(-1.0).is_nan());", "true\n"),
    ("float_compare", "print(1.5 < 2.5);", "true\n"),
    ("float_pi", "print(math.pi > 3.14 && math.pi < 3.15);", "true\n"),
    ("float_pow_fn", "print(math.pow(2.0, 8.0));", "256.0\n"),
    ("float_abs", "print((-1.25).abs());", "1.25\n"),
    ("float_mod", "print(7.5 % 2.0);", "1.5\n"),
    # ---- booleans
    ("bool_ops", "print(true && false); print(true || false); print(!true);", "false\ntrue\nfalse\n"),
    ("bool_short_circuit_and", "fn boom() -> Bool { panic(\"no\") } fn main() {}", ""),
    ("bool_eq", "print(true == true); print(true != false);", "true\ntrue\n"),
    ("bool_to_str", "print(true.to_str());", "true\n"),
    # ---- strings
    ("str_concat", 'print("a" + "b" + "c");', "abc\n"),
    ("str_interp", 'let n = 3; print("n = {n}, double = {n * 2}");', "n = 3, double = 6\n"),
    ("str_interp_nested_call", 'print("len={"abc".len()}");', "len=3\n"),
    ("str_interp_string_literal", 'let x = "inner"; print("[{x}]");', "[inner]\n"),
    ("str_escape_quote", 'print("say \\"hi\\"");', 'say "hi"\n'),
    ("str_escape_brace", 'print("\\{x\\}");', "{x}\n"),
    ("str_escape_unicode", 'print("\\u{1F600}");', "\U0001f600\n"),
    ("str_unicode_len", 'print("ğüşiöç".len());', "6\n"),
    ("str_turkish_upper", 'print("ığdır".upper());', "IĞDIR\n"),
    ("str_emoji_roundtrip", 'print("héllo wörld 🌍");', "héllo wörld 🌍\n"),
    ("str_multiline", 'print("a\nb");', "a\nb\n"),
    ("str_compare", 'print("apple" < "banana"); print("b" == "b");', "true\ntrue\n"),
    ("str_split_join", 'print("a,b,c".split(",").join("|"));', "a|b|c\n"),
    ("str_trim", 'print("  hi  ".trim() + "|");', "hi|\n"),
    ("str_contains", 'print("hello".contains("ell")); print("hello".contains("z"));', "true\nfalse\n"),
    ("str_starts_ends", 'print("hello".starts_with("he")); print("hello".ends_with("lo"));', "true\ntrue\n"),
    ("str_replace", 'print("a-b-c".replace("-", "+"));', "a+b+c\n"),
    ("str_chars", 'print("abc".chars());', '["a", "b", "c"]\n'),
    ("str_repeat", 'print("ab".repeat(3));', "ababab\n"),
    ("str_slice", 'print("hello".slice(1, 4));', "ell\n"),
    ("str_char_at", 'print("hey".char_at(1)); print("hey".char_at(9));', 'Some("e")\nNone\n'),
    ("str_index_of", 'print("hello".index_of("l")); print("hello".index_of("z"));', "Some(2)\nNone\n"),
    ("str_to_int_ok", 'print("123".to_int()); print("-7".to_int()); print(" 8 ".to_int());', "Some(123)\nSome(-7)\nSome(8)\n"),
    ("str_to_int_bad", 'print("12x".to_int()); print("".to_int()); print("1_0".to_int());', "None\nNone\nNone\n"),
    ("str_to_int_overflow", 'print("9223372036854775808".to_int());', "None\n"),
    ("str_to_float", 'print("2.5".to_float()); print("abc".to_float());', "Some(2.5)\nNone\n"),
    ("str_lines", 'print("a\nb\nc".lines().len());', "3\n"),
    ("str_is_empty", 'print("".is_empty()); print("x".is_empty());', "true\nfalse\n"),
    ("str_code_char", 'print("A".code()); print(98.to_char());', "65\nb\n"),
    ("str_lower", 'print("HeLLo".lower());', "hello\n"),
    # ---- lists
    ("list_literal", "print([1, 2, 3]);", "[1, 2, 3]\n"),
    ("list_index", "let xs = [10, 20, 30]; print(xs[1]);", "20\n"),
    ("list_push_pop", "let mut xs = [1]; xs.push(2); xs.push(3); print(xs.pop()); print(xs);", "Some(3)\n[1, 2]\n"),
    ("list_pop_empty", "let mut xs: List[Int] = []; print(xs.pop());", "None\n"),
    ("list_len_empty", "let xs: List[Int] = []; print(xs.len()); print(xs.is_empty());", "0\ntrue\n"),
    ("list_index_assign", "let mut xs = [1, 2, 3]; xs[1] = 9; print(xs);", "[1, 9, 3]\n"),
    ("list_compound_index", "let mut xs = [1, 2, 3]; xs[2] += 10; xs[0] *= 5; print(xs);", "[5, 2, 13]\n"),
    ("list_get", "let xs = [1, 2]; print(xs.get(1)); print(xs.get(5));", "Some(2)\nNone\n"),
    ("list_first_last", "let xs = [1, 2, 3]; print(xs.first()); print(xs.last());", "Some(1)\nSome(3)\n"),
    ("list_map", "print([1, 2, 3].map(|x| x * x));", "[1, 4, 9]\n"),
    ("list_filter", "print([1, 2, 3, 4, 5].filter(|x| x % 2 == 1));", "[1, 3, 5]\n"),
    ("list_fold", "print([1, 2, 3, 4].fold(0, |a, x| a + x));", "10\n"),
    ("list_fold_string", 'print(["a", "b"].fold("", |acc, s| acc + s + "."));', "a.b.\n"),
    ("list_any_all", "let xs = [2, 4, 6]; print(xs.any(|x| x > 5)); print(xs.all(|x| x % 2 == 0));", "true\ntrue\n"),
    ("list_find", "print([1, 2, 3].find(|x| x > 1)); print([1].find(|x| x > 5));", "Some(2)\nNone\n"),
    ("list_contains", "print([1, 2].contains(2)); print([1, 2].contains(5));", "true\nfalse\n"),
    ("list_index_of", "print([5, 6, 7].index_of(7));", "Some(2)\n"),
    ("list_sort", "let mut xs = [3, 1, 2]; xs.sort(); print(xs);", "[1, 2, 3]\n"),
    ("list_sorted_strs", 'print(["pear", "apple", "fig"].sorted());', '["apple", "fig", "pear"]\n'),
    ("list_reverse", "let mut xs = [1, 2, 3]; xs.reverse(); print(xs);", "[3, 2, 1]\n"),
    ("list_slice", "print([1, 2, 3, 4].slice(1, 3));", "[2, 3]\n"),
    ("list_concat", "print([1].concat([2, 3]));", "[1, 2, 3]\n"),
    ("list_insert_remove", "let mut xs = [1, 3]; xs.insert(1, 2); print(xs); print(xs.remove_at(0)); print(xs);", "[1, 2, 3]\n1\n[2, 3]\n"),
    ("list_extend_clear", "let mut xs = [1]; xs.extend([2, 3]); print(xs); xs.clear(); print(xs);", "[1, 2, 3]\n[]\n"),
    ("list_enumerate", 'print(["a", "b"].enumerate());', '[(0, "a"), (1, "b")]\n'),
    ("list_zip", 'print([1, 2].zip(["x", "y"]));', '[(1, "x"), (2, "y")]\n'),
    ("list_sum", "print([1, 2, 3].sum());", "6\n"),
    ("list_sum_float", "print([0.5, 0.25].sum_float());", "0.75\n"),
    ("list_nested", "let g = [[1, 2], [3, 4]]; print(g[1][0]);", "3\n"),
    ("list_nested_assign", "let mut g = [[1, 2], [3, 4]]; g[0][1] = 9; print(g);", "[[1, 9], [3, 4]]\n"),
    ("list_copy_independent", "let mut a = [1, 2]; let mut b = a.copy(); b.push(3); print(a); print(b);", "[1, 2]\n[1, 2, 3]\n"),
    ("list_alias_shares", "let mut a = [1]; let b = a; a.push(2); print(b);", "[1, 2]\n"),
    ("list_for_each", "let mut total = 0; [1, 2, 3].for_each(|x| { total += x; }); print(total);", "6\n"),
    ("list_equality", "print([1, 2] == [1, 2]); print([1, 2] == [2, 1]);", "true\nfalse\n"),
    ("list_of_tuples", 'let ps = [(1, "a"), (2, "b")]; print(ps[1].1);', "b\n"),
    # ---- maps
    ("map_literal_get", 'let m = #{"a": 1, "b": 2}; print(m.get("b")); print(m.get("z"));', "Some(2)\nNone\n"),
    ("map_index", 'let m = #{"a": 1}; print(m["a"]);', "1\n"),
    ("map_insert_overwrite", 'let mut m = #{"a": 1}; m.insert("a", 5); m.insert("b", 6); print(m);', '{"a": 5, "b": 6}\n'),
    ("map_remove", 'let mut m = #{1: "x", 2: "y"}; print(m.remove(1)); print(m.remove(9)); print(m.len());', 'Some("x")\nNone\n1\n'),
    ("map_contains_key", "let m = #{1: 2}; print(m.contains_key(1)); print(m.contains_key(2));", "true\nfalse\n"),
    ("map_keys_values", 'let m = #{"x": 1, "y": 2}; print(m.keys()); print(m.values());', '["x", "y"]\n[1, 2]\n'),
    ("map_entries_loop", 'let m = #{"x": 1, "y": 2}; for (k, v) in m.entries() { print("{k}={v}"); }', "x=1\ny=2\n"),
    ("map_index_assign", "let mut m: Map[Int, Int] = #{}; m[3] = 4; m[3] += 1; print(m);", "{3: 5}\n"),
    ("map_tuple_keys", "let mut m = #{(0, 0): \"origin\"}; m.insert((1, 2), \"p\"); print(m[(1, 2)]);", "p\n"),
    ("map_empty_annotated", "let m: Map[Str, Int] = #{}; print(m.is_empty());", "true\n"),
    # ---- tuples
    ("tuple_access", 'let t = (1, "two", 3.0); print(t.0); print(t.1); print(t.2);', "1\ntwo\n3.0\n"),
    ("tuple_destructure", "let (a, b) = (1, 2); print(a + b);", "3\n"),
    ("tuple_nested", "let t = ((1, 2), 3); print(t.0.1);", "2\n"),
    ("tuple_return", "fn f() -> (Int, Int) { (3, 4) } fn main() { let (a, b) = f(); print(a * b); }", ""),
    ("tuple_equality", "print((1, 2) == (1, 2)); print((1, 2) < (1, 3));", "true\ntrue\n"),
    ("tuple_swap", "let (mut a, mut b) = (1, 2);", ""),
    ("unit_value", "let u = (); print(u);", "()\n"),
    # ---- control flow
    ("if_expr", 'let x = if 3 > 2 { "yes" } else { "no" }; print(x);', "yes\n"),
    ("if_else_if", "let n = 5; print(if n < 3 { 1 } else if n < 10 { 2 } else { 3 });", "2\n"),
    ("if_no_else_stmt", "let mut x = 0; if true { x = 1; } print(x);", "1\n"),
    ("while_loop", "let mut i = 0; let mut s = 0; while i < 5 { s += i; i += 1; } print(s);", "10\n"),
    ("while_break", "let mut i = 0; while true { if i == 3 { break; } i += 1; } print(i);", "3\n"),
    ("while_continue", "let mut i = 0; let mut s = 0; while i < 6 { i += 1; if i % 2 == 0 { continue; } s += i; } print(s);", "9\n"),
    ("for_range", "let mut s = 0; for i in 0..5 { s += i; } print(s);", "10\n"),
    ("for_range_inclusive", "let mut s = 0; for i in 1..=5 { s += i; } print(s);", "15\n"),
    ("for_empty_range", "for i in 5..2 { print(i); } print(\"done\");", "done\n"),
    ("for_list", 'for x in ["a", "b"] { print(x); }', "a\nb\n"),
    ("for_break", "for i in 0..10 { if i == 2 { break; } print(i); }", "0\n1\n"),
    ("for_continue", "for i in 0..4 { if i == 1 { continue; } print(i); }", "0\n2\n3\n"),
    ("for_nested_break_inner", "for i in 0..2 { for j in 0..5 { if j == 1 { break; } print(\"{i}{j}\"); } }", "00\n10\n"),
    ("for_tuple_pattern", "for (a, b) in [(1, 2), (3, 4)] { print(a * b); }", "2\n12\n"),
    ("for_enumerate", 'for (i, s) in ["x", "y"].enumerate() { print("{i}:{s}"); }', "0:x\n1:y\n"),
    ("range_to_list", "print((2..5).to_list()); print((1..=3).to_list());", "[2, 3, 4]\n[1, 2, 3]\n"),
    ("range_len_contains", "print((0..10).len()); print((0..10).contains(5)); print((0..10).contains(10));", "10\ntrue\nfalse\n"),
    ("block_expr", "let x = { let a = 2; a * 3 }; print(x);", "6\n"),
    ("shadowing", 'let x = 1; let x = x + 1; let x = "s{x}"; print(x);', "s2\n"),
    ("inner_scope_shadow", "let x = 1; { let x = 2; print(x); } print(x);", "2\n1\n"),
    ("early_return_in_loop", "fn f() -> Int { for i in 0..10 { if i == 4 { return i; } } -1 } fn main() { print(f()); }", ""),
    ("loop_return_value_via_flag", "fn f() -> Int { let mut i = 0; while i < 100 { i += 7; if i % 5 == 0 { return i; } } 0 } fn main() { print(f()); }", ""),
    # ---- match
    ("match_int", 'let n = 2; print(match n { 1 => "one", 2 => "two", _ => "many" });', "two\n"),
    ("match_bool", 'print(match true { true => 1, false => 0 });', "1\n"),
    ("match_str", 'print(match "hi" { "hi" => 1, _ => 2 });', "1\n"),
    ("match_or_pattern", "print(match 3 { 1 | 2 => \"low\", 3 | 4 => \"mid\", _ => \"hi\" });", "mid\n"),
    ("match_guard", 'let n = -5; print(match n { x if x < 0 => "neg", 0 => "zero", _ => "pos" });', "neg\n"),
    ("match_binding", "print(match 41 { n => n + 1 });", "42\n"),
    ("match_tuple", 'print(match (1, "a") { (1, "a") => "both", (1, _) => "first", _ => "none" });', "both\n"),
    ("match_negative_literal", 'print(match -1 { -1 => "m1", _ => "other" });', "m1\n"),
    ("match_option", "print(match Some(5) { Some(x) => x, None => 0 });", "5\n"),
    ("match_nested_option", "print(match Some(Some(3)) { Some(Some(x)) => x, Some(None) => -1, None => -2 });", "3\n"),
    ("match_result", 'let r: Result[Int, Str] = Err("bad"); print(match r { Ok(v) => "ok", Err(e) => e });', "bad\n"),
    ("match_as_stmt", "match 1 { 1 => print(\"one\"), _ => print(\"other\") }", "one\n"),
    ("match_block_arm", "match 2 { 2 => { print(\"a\"); print(\"b\"); }, _ => {}, }", "a\nb\n"),
    ("match_fallthrough_guard", "print(match 5 { x if x > 10 => 1, x if x > 3 => 2, _ => 3 });", "2\n"),
    ("match_list_of_pairs", 'for p in [(1, true), (2, false)] { match p { (n, true) => print("T{n}"), (n, false) => print("F{n}") } }', "T1\nF2\n"),
    # ---- option / result
    ("option_methods", "let a = Some(2); let b: Option[Int] = None; print(a.is_some()); print(b.is_none()); print(b.unwrap_or(9)); print(a.unwrap());", "true\ntrue\n9\n2\n"),
    ("option_map", "print(Some(2).map(|x| x * 10)); print(None.map(|x: Int| x));", "Some(20)\nNone\n"),
    ("option_and_then", "print(Some(4).and_then(|x| if x > 3 { Some(x) } else { None }));", "Some(4)\n"),
    ("option_ok_or", 'let o: Option[Int] = None; print(o.ok_or("missing"));', 'Err("missing")\n'),
    ("result_methods", 'let r: Result[Int, Str] = Ok(3); print(r.is_ok()); print(r.unwrap()); print(r.ok());', "true\n3\nSome(3)\n"),
    ("result_map_err", 'let r: Result[Int, Str] = Err("e"); print(r.map_err(|s| s.len()));', "Err(1)\n"),
    ("result_map_and_then", "let r: Result[Int, Str] = Ok(1); print(r.map(|x| x + 1)); print(r.and_then(|x| Ok(x * 7)));", "Ok(2)\nOk(7)\n"),
    ("result_unwrap_or", 'let r: Result[Int, Str] = Err("x"); print(r.unwrap_or(0));', "0\n"),
    ("try_option_chain", "fn f(xs: List[Int]) -> Option[Int] { let a = xs.get(0)?; let b = xs.get(1)?; Some(a + b) } fn main() { print(f([1, 2])); print(f([1])); }", ""),
    ("try_result_chain", 'fn p(s: Str) -> Result[Int, Str] { match s.to_int() { Some(n) => Ok(n), None => Err("bad " + s) } } fn add(a: Str, b: Str) -> Result[Int, Str] { let x = p(a)?; let y = p(b)?; Ok(x + y) } fn main() { print(add("1", "2")); print(add("1", "z")); }', ""),
    ("try_in_lambda", "fn main() { let f = |x: Int| -> Option[Int] { let y = Some(x)?; Some(y + 1) }; print(f(1)); }", ""),
    # ---- functions and closures
    ("fn_basic", "fn add(a: Int, b: Int) -> Int { a + b } fn main() { print(add(2, 3)); }", ""),
    ("fn_inferred_return", "fn sq(n: Int) { n * n } fn main() { print(sq(7)); }", ""),
    ("fn_unit_return", "fn hello() { print(\"hi\"); } fn main() { hello(); }", ""),
    ("fn_recursion", "fn fact(n: Int) -> Int { if n <= 1 { 1 } else { n * fact(n - 1) } } fn main() { print(fact(10)); }", ""),
    ("fn_mutual_recursion", "fn a(n: Int) -> Bool { if n == 0 { true } else { b(n - 1) } } fn b(n: Int) -> Bool { if n == 0 { false } else { a(n - 1) } } fn main() { print(a(4)); }", ""),
    ("fn_forward_reference", "fn main() { print(later()); } fn later() -> Int { 42 }", ""),
    ("fn_as_value", "fn double(x: Int) -> Int { x * 2 } fn main() { let f = double; print(f(4)); print([1, 2].map(double)); }", ""),
    ("fn_higher_order", "fn apply(f: fn(Int) -> Int, x: Int) -> Int { f(f(x)) } fn main() { print(apply(|n| n + 3, 10)); }", ""),
    ("fn_returning_fn", "fn adder(n: Int) -> fn(Int) -> Int { |x| x + n } fn main() { print(adder(5)(6)); }", ""),
    ("closure_capture", "let k = 10; let f = |x: Int| x + k; print(f(5));", "15\n"),
    ("closure_mutable_capture", "let mut n = 0; let inc = || { n += 1; }; inc(); inc(); print(n);", "2\n"),
    ("closure_shared_cell", "let mut n = 0; let a = || { n += 1; }; let b = || { n += 10; }; a(); b(); a(); print(n);", "12\n"),
    ("closure_in_loop_distinct", "let mut fs = []; for i in 0..3 { fs.push(|| i); } print(fs.map(|f| f()));", "[0, 1, 2]\n"),
    ("closure_nested_capture", "let a = 1; let f = |x: Int| { let g = |y: Int| x + y + a; g(10) }; print(f(100));", "111\n"),
    ("closure_returns_closure", "let mk = |n: Int| |x: Int| x * n; print(mk(3)(4));", "12\n"),
    ("closure_typed_params", "let f = |a: Int, b: Int| -> Int { a - b }; print(f(9, 4));", "5\n"),
    ("closure_sort_by_hand", "let xs = [3, 1, 2]; print(xs.fold([], |acc: List[Int], x| { let mut a = acc.copy(); a.push(x * 2); a }));", "[6, 2, 4]\n"),
    ("lambda_no_params", "let f = || 7; print(f());", "7\n"),
    # ---- structs and impl
    ("struct_basic", "struct P { x: Int, y: Int } fn main() { let p = P { x: 1, y: 2 }; print(p.x + p.y); }", ""),
    ("struct_field_shorthand", "struct P { x: Int, y: Int } fn main() { let x = 3; let y = 4; let p = P { x, y }; print(p); }", ""),
    ("struct_update_fields", "struct P { x: Int } fn main() { let mut p = P { x: 1 }; p.x = 5; p.x += 1; print(p.x); }", ""),
    ("struct_nested", "struct A { n: Int } struct B { a: A } fn main() { let mut b = B { a: A { n: 1 } }; b.a.n = 7; print(b.a.n); }", ""),
    ("struct_equality", "struct P { x: Int } fn main() { print(P { x: 1 } == P { x: 1 }); print(P { x: 1 } == P { x: 2 }); }", ""),
    ("struct_display", "struct P { x: Int, name: Str } fn main() { print(P { x: 1, name: \"n\" }); }", ""),
    ("struct_pattern", "struct P { x: Int, y: Int } fn main() { let p = P { x: 1, y: 2 }; let P { x, y } = p; print(x * 10 + y); }", ""),
    ("struct_pattern_partial", "struct P { x: Int, y: Int } fn main() { match (P { x: 0, y: 5 }) { P { x: 0, y } => print(y), P { .. } => print(-1) } }", ""),
    ("impl_method", "struct C { n: Int } impl C { fn get(self) -> Int { self.n } } fn main() { print(C { n: 4 }.get()); }", ""),
    ("impl_mut_method", "struct C { n: Int } impl C { fn inc(mut self) { self.n += 1; } fn get(self) -> Int { self.n } } fn main() { let mut c = C { n: 0 }; c.inc(); c.inc(); print(c.get()); }", ""),
    ("impl_assoc_fn", "struct C { n: Int } impl C { fn new(n: Int) -> C { C { n: n } } } fn main() { print(C.new(5).n); }", ""),
    ("impl_method_args", "struct C { n: Int } impl C { fn add(self, k: Int) -> Int { self.n + k } } fn main() { print(C { n: 1 }.add(2)); }", ""),
    ("impl_self_type", "struct C { n: Int } impl C { fn make(n: Int) -> Self { C { n: n } } } fn main() { print(C.make(3).n); }", ""),
    ("impl_static_call_with_self", "struct C { n: Int } impl C { fn get(self) -> Int { self.n } } fn main() { let c = C { n: 8 }; print(C.get(c)); }", ""),
    ("impl_method_chain", "struct C { n: Int } impl C { fn with(self, k: Int) -> C { C { n: self.n + k } } } fn main() { print(C { n: 1 }.with(2).with(3).n); }", ""),
    ("impl_method_inferred_ret", "struct C { n: Int } impl C { fn twice(self) { self.n * 2 } } fn main() { print(C { n: 4 }.twice()); }", ""),
    ("mut_param_mutates_caller", "struct C { n: Int } fn bump(mut c: C) { c.n += 1; } fn main() { let mut c = C { n: 0 }; bump(c); bump(c); print(c.n); }", ""),
    # ---- enums
    ("enum_basic", "enum Color { Red, Green } fn main() { print(Color.Red); print(Color.Green == Color.Green); }", ""),
    ("enum_data", "enum S { Circle(Int), Rect(Int, Int) } fn main() { print(S.Circle(2)); print(S.Rect(1, 2)); }", ""),
    ("enum_match_data", "enum S { Circle(Int), Rect(Int, Int) } fn area(s: S) -> Int { match s { S.Circle(r) => 3 * r * r, S.Rect(w, h) => w * h } } fn main() { print(area(S.Circle(2))); print(area(S.Rect(3, 4))); }", ""),
    ("enum_unqualified_variant", "enum S { Circle(Int), Dot } fn main() { let s = Circle(3); match s { Circle(r) => print(r), Dot => print(0) } }", ""),
    ("enum_equality", "enum E { A(Int), B } fn main() { print(E.A(1) == E.A(1)); print(E.A(1) == E.A(2)); print(E.B == E.A(1)); }", ""),
    ("enum_in_list", "enum E { A(Int), B } fn main() { print([E.A(1), E.B]); }", ""),
    ("enum_recursive", "enum L { Nil, Cons(Int, L) } fn sum(l: L) -> Int { match l { Nil => 0, Cons(h, t) => h + sum(t) } } fn main() { print(sum(L.Cons(1, L.Cons(2, L.Nil)))); }", ""),
    ("enum_constructor_as_fn", "enum E { A(Int) } fn main() { print([1, 2].map(E.A)); }", ""),
    ("enum_option_constructor_fn", "fn main() { print([1, 2].map(Some)); }", ""),
    ("enum_nested_match", "enum E { A(Option[Int]), B } fn f(e: E) -> Int { match e { E.A(Some(n)) => n, E.A(None) => -1, E.B => -2 } } fn main() { print(f(E.A(Some(4)))); print(f(E.A(None))); print(f(E.B)); }", ""),
    # ---- generics
    ("generic_identity", "fn id[T](x: T) -> T { x } fn main() { print(id(5)); print(id(\"s\")); print(id([1])); }", ""),
    ("generic_pair_swap", "fn swap[A, B](p: (A, B)) -> (B, A) { (p.1, p.0) } fn main() { print(swap((1, \"a\"))); }", ""),
    ("generic_struct", "struct Box[T] { v: T } fn main() { let a = Box { v: 1 }; let b = Box { v: \"s\" }; print(a.v + 1); print(b.v + \"!\"); }", ""),
    ("generic_method", "struct Box[T] { v: T } impl[T] Box[T] { fn get(self) -> T { self.v } fn set(mut self, x: T) { self.v = x; } } fn main() { let mut b = Box { v: 1 }; b.set(5); print(b.get()); }", ""),
    ("generic_enum", "enum Maybe[T] { Just(T), Nothing } fn main() { let m = Maybe.Just(3); match m { Just(x) => print(x), Nothing => print(0) } }", ""),
    ("generic_fn_hof", "fn map_all[T, U](xs: List[T], f: fn(T) -> U) -> List[U] { xs.map(f) } fn main() { print(map_all([1, 2], |x| x.to_str() + \"!\")); }", ""),
    ("generic_ord_bound", "fn maxof[T: Ord](a: T, b: T) -> T { if a > b { a } else { b } } fn main() { print(maxof(3, 9)); print(maxof(\"a\", \"b\")); print(maxof(2.5, 1.5)); }", ""),
    ("generic_eq_bound", "fn same[T: Eq](a: T, b: T) -> Bool { a == b } fn main() { print(same(1, 1)); print(same(\"a\", \"b\")); }", ""),
    ("generic_inferred_ret", "fn first[T](xs: List[T]) { xs.first() } fn main() { print(first([7, 8])); }", ""),
    ("generic_stack_methods", "struct S[T] { items: List[T] } impl[T] S[T] { fn new() -> S[T] { S { items: [] } } fn push(mut self, x: T) { self.items.push(x); } fn len(self) -> Int { self.items.len() } } fn main() { let mut s = S.new(); s.push(1); s.push(2); print(s.len()); }", ""),
    ("generic_ord_on_tuples", "fn mx[T: Ord](a: T, b: T) -> T { if a > b { a } else { b } } fn main() { print(mx((1, 2), (1, 3))); }", ""),
    ("generic_hash_key", "fn count[K: Hash](xs: List[K]) -> Int { let mut m: Map[K, Int] = #{}; for x in xs { m.insert(x, 1); } m.len() } fn main() { print(count([1, 1, 2])); }", ""),
    # ---- misc stdlib
    ("assert_ok", "assert(1 + 1 == 2); assert_eq(3, 3); print(\"fine\");", "fine\n"),
    ("print_nested_structures", "print(Some([1, 2])); print((Some(1), Some(2)));", "Some([1, 2])\n(Some(1), Some(2))\n"),
    ("print_string_in_option", 'print(Some("x"));', 'Some("x")\n'),
    ("env_args_empty", "print(env.args().len());", "0\n"),
    ("env_get_missing", 'print(env.get("VEX_SURELY_NOT_SET_12345"));', "None\n"),
    ("io_read_missing_file", 'match io.read_file("/definitely/not/here.txt") { Ok(_) => print("?"), Err(_) => print("error") }', "error\n"),
    ("math_consts", "print(math.INT_MAX); print(math.INT_MIN);", "9223372036854775807\n-9223372036854775808\n"),
    ("math_trig", "print(math.sin(0.0)); print(math.cos(0.0));", "0.0\n1.0\n"),
    ("math_ln_exp", "print(math.ln(1.0)); print(math.exp(0.0));", "0.0\n1.0\n"),
]

# Bodies that are really complete programs (contain `fn `) are run as-is.
CASES = [(n, b, e) for n, b, e in BODIES if e != "" or "fn main" not in b]
FULL = [(n, b) for n, b, e in BODIES if e == "" and "fn main" in b]


def _run(body: str) -> str:
    return run_source(body) if "fn main" in body else run_body(body)


@pytest.mark.parametrize(("name", "body", "expected"), CASES, ids=[c[0] for c in CASES])
def test_program_output(name: str, body: str, expected: str) -> None:
    assert _run(body) == expected


# Full programs whose output is checked separately (kept readable).
FULL_EXPECTED = {
    "tuple_return": "12\n",
    "early_return_in_loop": "4\n",
    "loop_return_value_via_flag": "35\n",
    "fn_basic": "5\n",
    "fn_inferred_return": "49\n",
    "fn_unit_return": "hi\n",
    "fn_recursion": "3628800\n",
    "fn_mutual_recursion": "true\n",
    "fn_forward_reference": "42\n",
    "fn_as_value": "8\n[2, 4]\n",
    "fn_higher_order": "16\n",
    "fn_returning_fn": "11\n",
    "try_option_chain": "Some(3)\nNone\n",
    "try_result_chain": 'Ok(3)\nErr("bad z")\n',
    "try_in_lambda": "Some(2)\n",
    "struct_basic": "3\n",
    "struct_field_shorthand": "P { x: 3, y: 4 }\n",
    "struct_update_fields": "6\n",
    "struct_nested": "7\n",
    "struct_equality": "true\nfalse\n",
    "struct_display": 'P { x: 1, name: "n" }\n',
    "struct_pattern": "12\n",
    "struct_pattern_partial": "5\n",
    "impl_method": "4\n",
    "impl_mut_method": "2\n",
    "impl_assoc_fn": "5\n",
    "impl_method_args": "3\n",
    "impl_self_type": "3\n",
    "impl_static_call_with_self": "8\n",
    "impl_method_chain": "6\n",
    "impl_method_inferred_ret": "8\n",
    "mut_param_mutates_caller": "2\n",
    "enum_basic": "Red\ntrue\n",
    "enum_data": "Circle(2)\nRect(1, 2)\n",
    "enum_match_data": "12\n12\n",
    "enum_unqualified_variant": "3\n",
    "enum_equality": "true\nfalse\nfalse\n",
    "enum_in_list": "[A(1), B]\n",
    "enum_recursive": "3\n",
    "enum_constructor_as_fn": "[A(1), A(2)]\n",
    "enum_option_constructor_fn": "[Some(1), Some(2)]\n",
    "enum_nested_match": "4\n-1\n-2\n",
    "generic_identity": '5\ns\n[1]\n',
    "generic_pair_swap": '("a", 1)\n',
    "generic_struct": "2\ns!\n",
    "generic_method": "5\n",
    "generic_enum": "3\n",
    "generic_fn_hof": '["1!", "2!"]\n',
    "generic_ord_bound": "9\nb\n2.5\n",
    "generic_eq_bound": "true\nfalse\n",
    "generic_inferred_ret": "Some(7)\n",
    "generic_stack_methods": "2\n",
    "generic_ord_on_tuples": "(1, 3)\n",
    "generic_hash_key": "2\n",
    "bool_short_circuit_and": "",
}


@pytest.mark.parametrize(("name", "src"), FULL, ids=[c[0] for c in FULL])
def test_full_program_output(name: str, src: str) -> None:
    assert run_source(src) == FULL_EXPECTED[name]


def test_short_circuit_does_not_evaluate_rhs() -> None:
    src = """
fn boom() -> Bool { panic("must not run") }
fn main() {
    print(false && boom());
    print(true || boom());
}
"""
    assert run_source(src) == "false\ntrue\n"


def test_if_branch_only_evaluated_once() -> None:
    src = """
fn main() {
    let mut n = 0;
    let v = if { n += 1; n > 0 } { n } else { -1 };
    print(v);
    print(n);
}
"""
    assert run_source(src) == "1\n1\n"


def test_unused_tuple_swap_compiles() -> None:
    assert run_body("let (mut a, mut b) = (1, 2); a += 10; b += 20; print(a + b);") == "33\n"
