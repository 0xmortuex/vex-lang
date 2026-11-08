# The Vex Language Specification

**Status: normative.** This document defines the Vex language. Where it and any other document disagree, this one is
right. Where it and the implementation disagree, one of them has a bug, and `tests/test_spec_conformance.py` fails.
`docs/vm.md` and `ARCHITECTURE.md` describe one implementation and are not normative; `docs/tour.md` is a tutorial;
`docs/ffi.md` is the normative embedding ABI and is referenced from here rather than repeated.

The specification is executable. The conformance test reads the grammar blocks below and checks them against the lexer
and parser, in both directions, and compiles and runs every example block, comparing its result exactly. Section 11
records every place where the implementation and the previous documents disagreed and how each was settled.

Contents: 1 [Notation](#1-notation) · 2 [Lexical structure](#2-lexical-structure) · 3 [Syntax](#3-syntax) ·
4 [Programs and names](#4-programs-and-names) · 5 [Types](#5-types) · 6 [Evaluation](#6-evaluation) ·
7 [Integers and floats](#7-integers-and-floats) · 8 [Values and memory](#8-values-and-memory) ·
9 [Errors](#9-errors) · 10 [Limits](#10-limits) · 11 [Disagreements settled](#11-disagreements-settled)

## 1. Notation

Grammars are written in EBNF, read as **parsing expression grammars**, because that is what a recursive-descent parser
with committed choices is:

| Form | Meaning |
|------|---------|
| `a b` | `a` then `b` |
| `a \| b` | ordered choice: `a` if it matches here, otherwise `b` |
| `[ a ]` | `a` if it matches, otherwise nothing |
| `{ a }` | as many `a` as match (greedy) |
| `( a )` | grouping |
| `&a` | succeeds, consuming nothing, if `a` matches here |
| `!a` | succeeds, consuming nothing, if `a` does not match here |
| `"x"` or `'x'` | a literal: characters in the lexical grammar, a token in the syntactic grammar |
| `(* ... *)` | comment |

A choice commits at the first alternative that succeeds: a later failure never re-tries it. Inside a string,
`\n \t \r \\ \" \'` and `\uXXXX` have their usual meaning.

The lexical grammar is over characters and uses four character classes that no rule defines: `any` (one character),
`alpha` and `alnum` (one character that is alphabetic, respectively alphanumeric, by the Unicode database as exposed by
Python's `str.isalpha` and `str.isalnum`), and `surrogate` (a code point in U+D800..U+DFFF). The syntactic grammar is over
the tokens of section 2 and uses the token kinds `IDENT`, `INT`, `FLOAT`, `STRING` and `EOF` (end of the source text),
and four token classes:

| Token class | Is an `IDENT` or `STRING` token that... |
|-------------|------------------------------------------|
| `UPPER_IDENT` | is an `IDENT` whose first character is upper-case (`str.isupper`) |
| `SELF` | is the `IDENT` spelled `self` |
| `WILD` | is the `IDENT` spelled `_` |
| `PLAIN_STRING` | is a `STRING` containing no interpolation |

## 2. Lexical structure

Source text is UTF-8. An invalid byte is the error `invalid-utf8`. A leading byte order mark is removed and CR LF is
normalised to LF when a file is read. Between any two tokens there may be trivia, which is discarded:

```ebnf lexical
trivia         = { whitespace | line_comment | block_comment | bom } ;
whitespace     = " " | "\t" | "\r" | "\n" ;
bom            = "﻿" ;
line_comment   = "//" { !"\n" any } ;
block_comment  = "/*" { block_comment | !( "*/" | "/*" ) any } "*/" ;
```

Block comments nest. An unterminated one is `unterminated-comment`. No other character is whitespace: a form feed is
`unexpected-character`.

```vex example lex-comments
// a line comment
/* outer /* inner */ still a comment */
fn main() { print(/* here too */ 1); }
```

```output
1
```

### Identifiers and keywords

```ebnf lexical
IDENT          = !keyword ident_start { ident_part } ;
keyword        = keyword_word !ident_part ;
keyword_word   = "let" | "mut" | "fn" | "struct" | "enum" | "impl" | "match" | "if" | "else" | "while" | "for" | "in"
               | "return" | "break" | "continue" | "import" | "true" | "false" ;
ident_start    = alpha | "_" ;
ident_part     = alnum | "_" ;
```

The keywords are reserved. `self`, `Self` and `_` are ordinary identifiers that some positions give meaning to.

### Numbers

```ebnf lexical
INT            = ( radix_int | dec_int ) !ident_part ;
radix_int      = hex_int | oct_int | bin_int ;
hex_int        = "0" ( "x" | "X" ) { "_" } hex_digit { hex_digit | "_" } ;
oct_int        = "0" ( "o" | "O" ) { "_" } oct_digit { oct_digit | "_" } ;
bin_int        = "0" ( "b" | "B" ) { "_" } bin_digit { bin_digit | "_" } ;
dec_int        = digit { digit | "_" } ;
digit          = "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" ;
hex_digit      = digit | "a" | "b" | "c" | "d" | "e" | "f" | "A" | "B" | "C" | "D" | "E" | "F" ;
oct_digit      = "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" ;
bin_digit      = "0" | "1" ;

FLOAT          = dec_int ( fraction [ exponent ] | exponent ) !ident_part ;
fraction       = "." digit { digit | "_" } ;
exponent       = ( "e" | "E" ) [ "+" | "-" ] digit { digit } ;
```

Digits are ASCII. The digits of a number other than the exponent may contain `_`. A number followed directly by a letter
or `_` is `bad-number`. A number with too many significant digits to fit (more than 19 decimal, 16 hexadecimal, 22 octal
or 64 binary digits) is `int-literal-range`. A value above `9223372036854775807` is `int-literal-range`, except that a
literal equal to `2^63` directly after a prefix `-` denotes `-2^63`. A `FLOAT` that would be infinite is
`float-literal-range`; one too small to represent is `0.0`.

When the previous token is `.`, a digit sequence is always a decimal tuple index and is read as `dec_int` with no fraction
or exponent, so `t.0.1` is two projections.

```vex example lex-numbers
fn main() {
    print(1_000 + 0xFF + 0b11 + 0o7);
    print(1.5e3);
    print(1_0e3);
    print(0X_1F);
    let t = ((1, 2), 3);
    print(t.0.1);
}
```

```output
1265
1500.0
10000.0
31
2
```

```vex example int-literal-out-of-range
fn main() { print(9223372036854775808); }
```

```error
int-literal-range 1:19
```

### Strings

```ebnf lexical
STRING         = '"' { escape | interpolation | string_char } '"' ;
escape         = unicode_escape | "\\" simple_escape ;
simple_escape  = "n" | "t" | "r" | "0" | "\\" | '"' | "{" | "}" ;
unicode_escape = "\\u{" hex_digit { hex_digit } "}" ;
interpolation  = "{" interp_text "}" ;
interp_text    = { nested_string | "{" interp_text "}" | !( "{" | "}" | '"' ) any } ;
nested_string  = '"' { "\\" any | !'"' any } '"' ;
string_char    = !( '"' | "\\" | "{" | surrogate ) any ;
```

A string may span lines. An unescaped `}` is an ordinary character; a literal `{` needs `\{`. A `unicode_escape` must
denote a Unicode scalar value (at most `10FFFF`, not `D800`..`DFFF`), otherwise `bad-escape`. The text of an interpolation
is found by balancing braces and skipping nested strings, without looking at comments, so a `}` inside a comment inside
an interpolation ends the interpolation. That text is then lexed and parsed by the rule `interp`, so it holds exactly one
expression (`empty-interpolation` if it holds none).

```vex example lex-strings
fn main() {
    let name = "Vex";
    print("hi {name}, {1 + 2} \{braces\} \u{41}");
    print("{"nested {name}"}");
    print("a}b");
}
```

```output
hi Vex, 3 {braces} A
nested Vex
a}b
```

### Punctuation

Longest match, in exactly this order:

```ebnf lexical
PUNCT          = "..=" | "==" | "!=" | "<=" | ">=" | "&&" | "||" | "+=" | "-=" | "*=" | "/=" | "%=" | "->" | "=>"
               | ".." | "#{" | "+" | "-" | "*" | "/" | "%" | "<" | ">" | "=" | "!" | "." | "," | ";" | ":" | "?"
               | "(" | ")" | "[" | "]" | "{" | "}" | "|" ;
```

Any other character is `unexpected-character`.

## 3. Syntax

```ebnf syntax
module       = { item } EOF ;
repl_input   = { item | let_stmt | block_led | expr_stmt } EOF ;

item         = import_decl | fn_decl | struct_decl | enum_decl | impl_decl ;
import_decl  = "import" IDENT ";" ;
fn_decl      = "fn" IDENT [ tparams ] "(" [ params ] ")" [ "->" type ] block ;
params       = ( self_param | param ) { "," param } [ "," ] ;
self_param   = [ "mut" ] SELF ;
param        = [ "mut" ] IDENT ":" type ;
tparams      = "[" [ tparam { "," tparam } [ "," ] ] "]" ;
tparam       = IDENT [ ":" IDENT { "+" IDENT } ] ;
struct_decl  = "struct" IDENT [ tparams ] "{" [ field { "," field } [ "," ] ] "}" ;
field        = IDENT ":" type ;
enum_decl    = "enum" IDENT [ tparams ] "{" [ variant { "," variant } [ "," ] ] "}" ;
variant      = IDENT [ "(" [ type_list ] ")" ] ;
impl_decl    = "impl" [ tparams ] type "{" { fn_decl } "}" ;

type         = "(" [ type_list ] ")"
             | "fn" "(" [ type_list ] ")" [ "->" type ]
             | IDENT [ "[" [ type_list ] "]" ] ;
type_list    = type { "," type } [ "," ] ;
```

A parameter named `self` is a receiver only in first position; `self: T` there is a syntax error, while a later parameter
may be called `self` like any other name. Type-parameter lists and type-argument lists may be empty syntactically; the
checker gives empty argument lists to generic types `type-arity`. A dotted name with more than two parts parses as a
pattern but is `bad-pattern` when resolved.

In a type, `()` is `Unit`, `(T)` is `T`, and `(T,)` is a one-element tuple. An omitted function result means `Unit`.

### Statements and blocks

```ebnf syntax
block        = "{" { block_entry } "}" ;
block_entry  = ";" | let_stmt | block_led | expr_stmt ;
let_stmt     = "let" let_pattern [ ":" type ] "=" expr ";" ;
block_led    = blocklike ( &( "." | "?" ) led_tail stmt_end | [ ";" ] ) ;
led_tail     = { suffix &suffix } place_suffix assign_op expr
             | suffix { suffix } { binop unary } ;
expr_stmt    = ( place assign_op expr | expr ) stmt_end ;
stmt_end     = ";" | &( "}" | EOF ) ;
place        = primary { suffix &suffix } place_suffix
             | "(" place ")"
             | IDENT ;
place_suffix = "." ( IDENT | INT ) | "[" expr "]" ;
assign_op    = "=" | "+=" | "-=" | "*=" | "/=" | "%=" ;

blocklike    = if_expr | match_expr | while_expr | for_expr | block ;
if_expr      = "if" cond block [ "else" ( if_expr | block ) ] ;
match_expr   = "match" cond "{" { arm } "}" ;
arm          = pattern [ "if" cond ] "=>" ( &"{" expr [ "," ] | expr ( "," | &"}" ) ) ;
while_expr   = "while" cond block ;
for_expr     = "for" pattern "in" cond block ;
```

The value of a block is its last entry when that is an expression with no following `;`. Otherwise it is `()`. Items
may not nest: `fn`, `struct`, `enum` and `impl` inside a block are `nested-item`. An assignment is only a statement; its
target is a variable, a field or an index expression, possibly parenthesised, anything else being `invalid-assign-target`.

An entry that starts with a block-like expression (`if`, `match`, `while`, `for`, `{`) ends at its closing brace and needs
no `;`. A following binary operator does **not** continue it, so `for ... { } -1` is a loop then the expression `-1`.
Only `.` and `?` continue such an expression. After them the rest of the statement follows normal rules, except that no
range can follow, because `..` is not one of the operators in `led_tail`.

In a match arm, `,` may be omitted after an arm body that **starts** with `{`, and after the last arm. An `if` or `match`
as an arm body starts with neither, so it needs its `,`.

```vex example syn-blocklike-statement
fn f() -> Int {
    if true { print("stmt"); } else { }
    -1
}
fn main() {
    print(f());
    let xs = [1, 2];
    match 1 { _ => xs }.len() + 1;
    print(xs.len());
}
```

```output
stmt
-1
2
```

```vex example syn-arm-comma
fn main() {
    match 1 {
        1 => { print("a"); }
        _ => print("b"),
    }
}
```

```output
a
```

### Expressions

```ebnf syntax
expr         = binary [ range_op binary ] ;
range_op     = ".." | "..=" ;
binary       = unary { binop unary } ;
binop        = "||" | "&&" | "==" | "!=" | "<" | "<=" | ">" | ">=" | "+" | "-" | "*" | "/" | "%" ;
unary        = ( "-" | "!" ) unary | postfix ;
postfix      = primary { suffix } ;
suffix       = "(" [ expr_list ] ")" | "[" expr "]" | "." ( IDENT | INT ) | "?" ;
primary      = struct_lit | return_expr | atom ;
return_expr  = "return" ( &return_end | expr ) ;
return_end   = ";" | "}" | ")" | "," | EOF ;
atom         = INT | FLOAT | STRING | "true" | "false" | IDENT
             | paren | list_lit | map_lit | lambda | blocklike | "break" | "continue" ;
paren        = "(" [ expr_list ] ")" ;
list_lit     = "[" [ expr_list ] "]" ;
expr_list    = expr { "," expr } [ "," ] ;
map_lit      = "#{" [ map_entry { "," map_entry } [ "," ] ] "}" ;
map_entry    = expr ":" expr ;
struct_lit   = UPPER_IDENT "{" [ field_init { "," field_init } [ "," ] ] "}" ;
field_init   = IDENT [ ":" expr ] ;
lambda       = ( "||" | "|" [ lambda_param { "," lambda_param } [ "," ] ] "|" ) ( "->" type block | expr ) ;
lambda_param = IDENT [ ":" type ] ;
interp       = expr EOF ;

cond         = cond_binary [ range_op cond_binary ] ;
cond_binary  = cond_unary { binop cond_unary } ;
cond_unary   = ( "-" | "!" ) cond_unary | cond_postfix ;
cond_postfix = cond_primary { suffix } ;
cond_primary = "return" cond | atom ;
```

`cond` is `expr` with struct literals disallowed outside brackets, so that `if x { ... }` is not read as a struct literal.
Inside parentheses, brackets, a block, a lambda body, a struct literal or a map literal the restriction is lifted. In a
`cond`, `S { ... }` is not an error by itself: it reads the name `S` and then a block.

A struct literal needs an upper-case name. `Name {` starts one only if the next token is `}`, or an identifier followed by
`:`, `,` or `}`. A field written `x` alone means `x: x`. `return` has a value unless the next token is in `return_end`; so
in `[return]` the next token is `]`, a value is required, and this is a syntax error.

#### Binary operators

Loosest first; all left-associative.

| Level | Operators |
|-------|-----------|
| 1 | `\|\|` |
| 2 | `&&` |
| 3 | `==` `!=` |
| 4 | `<` `<=` `>` `>=` |
| 5 | `+` `-` |
| 6 | `*` `/` `%` |

Ranges (`..`, `..=`) bind looser than level 1 and do not chain. Prefix `-` and `!` bind tighter than any binary operator;
suffixes (call, index, field, `?`) bind tightest, so `-2.pow(2)` is `-(2.pow(2))`.

```vex example syn-precedence
fn main() {
    print(1 + 2 * 3);
    print(-2.pow(2));
    print(!true || true);
    print(1 + 1 == 2 && 3 > 2);
    for i in 0..1 + 1 { print(i); }
}
```

```output
7
-4
true
true
0
1
```

```vex example syn-struct-in-condition
struct P { x: Int }
fn main() {
    if (P { x: 1 }).x == 1 { print("parenthesised"); }
}
```

```output
parenthesised
```

```vex example syn-struct-in-condition-rejected
struct P { x: Int }
fn main() {
    if P { x: 1 }.x == 1 { }
}
```

```error
expected-semicolon 3:12
expected-semicolon 3:24
```

### Patterns

```ebnf syntax
pattern      = pat_atom { "|" pat_atom } ;
pat_atom     = WILD | pat_ctor | pat_struct | pat_path | IDENT | pat_tuple | pat_literal ;
pat_ctor     = ( UPPER_IDENT { "." IDENT } | IDENT "." IDENT { "." IDENT } ) "(" [ pat_list ] ")" ;
pat_path     = IDENT "." IDENT { "." IDENT } ;
pat_struct   = UPPER_IDENT "{" [ pat_field { "," pat_field } [ "," [ ".." ] ] | ".." ] "}" ;
pat_field    = IDENT [ ":" pattern ] ;
pat_tuple    = "(" [ pat_list ] ")" ;
pat_list     = pattern { "," pattern } [ "," ] ;
pat_literal  = [ "-" ] ( INT | FLOAT ) | "true" | "false" | PLAIN_STRING ;

let_pattern    = let_pat_atom { "|" let_pat_atom } ;
let_pat_atom   = "mut" IDENT | WILD | let_pat_ctor | let_pat_struct | pat_path | IDENT | let_pat_tuple | pat_literal ;
let_pat_ctor   = ( UPPER_IDENT { "." IDENT } | IDENT "." IDENT { "." IDENT } ) "(" [ let_pat_list ] ")" ;
let_pat_struct = UPPER_IDENT "{" [ let_pat_field { "," let_pat_field } [ "," [ ".." ] ] | ".." ] "}" ;
let_pat_field  = IDENT [ ":" let_pattern ] ;
let_pat_tuple  = "(" [ let_pat_list ] ")" ;
let_pat_list   = let_pattern { "," let_pattern } [ "," ] ;
```

`let_pattern` is `pattern` that additionally allows `mut`; `mut` anywhere else in a pattern is `bad-pattern`. A pattern
alternative `A | B` must bind the same names in each alternative. `..` closes a struct pattern and nothing may follow it,
and a tuple pattern of one element needs its trailing comma, as in expressions.

A bare identifier in a pattern is a binding unless its first character is upper-case. An upper-case bare identifier is a
variant, which must be unique among all enums.

## 4. Programs and names

A program is a root source file and the files it imports. `import x;` loads `x.vx` from the directory of the importing
file; imports are transitive, dependencies load first, an import cycle is `import-cycle` and a missing file is
`import-not-found`. Imports are flat: the names of every loaded file are merged into one table, in which duplicates
are errors. A top-level name (function, struct or enum) must be unique and must not be a reserved built-in name
(`reserved-name`: the built-in types, `Never`, `Self`, the global functions, the namespaces `math`, `io`, `env`, and `host`).
A local variable may shadow any of them. Items may be used before they are declared, across files. A runnable program has
a function `main` with no parameters and return type `Unit` or `Int` (`no-main`, `bad-main`). An embedded script is one
source text; `import` in it is `import-denied`.

`repl_input` is one entry at the interactive prompt (`vex repl`): items, or statements, the last of which may be a bare expression.
At the prompt a `let` binds a single name (otherwise `repl-let`) and persists across entries, and a function may be redefined.

Scopes nest lexically; a block opens a scope and `let` adds a binding after evaluating its initialiser, so a new `let`
may shadow the old value of the same name. A name resolves to, in order: a local or parameter; a REPL global; a top-level
function; a built-in global function; a variant; a namespace; a type. Field access `Name.member` on a non-local name
resolves statically to a variant or an associated function or a namespace member.

```vex example name-shadowing
fn main() {
    print(later());
    let x = 1;
    let x = x + 1;
    print(x);
    { let x = 10; print(x); }
    print(x);
}
fn later() -> Int { 5 }
```

```output
5
2
10
2
```

## 5. Types

```text
τ ::= Int | Float | Bool | Str | Unit | Range
    | List[τ] | Map[τ, τ]
    | Option[τ] | Result[τ, τ]            (built-in enums)
    | N[τ, …]                              (user struct or enum N applied to its type arguments)
    | (τ, …, τ)                            (tuple, one or more elements; () is Unit)
    | fn(τ, …) -> τ
    | T                                    (declared type parameter, rigid inside its scope)
```

There are also inference variables, the type `Never`, and an error type that silences follow-on errors after a reported
one. **Conversions are never implicit**: `1 + 1.0` is a type error; use `to_float` or `to_int`.

### Inference

Vex uses unification-based inference with explicit generics.

1. Parameter types, struct fields and variant payloads are always written. Return types and locals are inferred.
2. An unannotated return type starts as a fresh variable. Function bodies are checked on demand: when a function whose
   return type is unknown is called, its body is checked first. Inside its own recursion it sees the variable, monomorphically.
   A generic function that calls itself before its return type is known is `recursive-inference`.
3. `let`, lambda parameters and `[]` and `None` get fresh variables solved by unification. An expected type flows down into
   lambda and list literals, and lambda arguments are checked after the other arguments, so `xs.map(|x| …)` knows `x`.
4. A method call, field access, index or `?` needs its receiver's type to be known at that point, or it is `cannot-infer`.
5. At the end of each function, a variable still unsolved in the type of any expression is `cannot-infer`, unless another
   error was reported in that function.

`let` bindings and lambdas are monomorphic. Only top-level functions, methods, structs and enums declared with type
parameters are polymorphic, by fresh instantiation at each use. There are no explicit type arguments (`f[Int](x)` indexes `f`).

```vex example ty-inference
fn sq(n: Int) { n * n }
fn main() {
    let xs = [1, 2].map(|x| sq(x) + 1);
    print(xs);
}
```

```output
[2, 5]
```

```vex example ty-cannot-infer
fn main() {
    let xs = [];
}
```

```error
cannot-infer 2:14
```

```vex example ty-no-let-polymorphism
fn main() {
    let id = |x| x;
    print(id(1));
    print(id("a"));
}
```

```error
type-mismatch 4:14
```

```vex example ty-method-needs-known-type
fn main() {
    let f = |x| x.len();
}
```

```error
cannot-infer 2:19
```

### Generics and bounds

A type parameter is rigid within its declaration, so a generic function type-checks once for all instantiations. An
operation on `T` requires a bound. There are exactly three bounds, built into the language; **Vex has no user-defined traits
or interfaces**, and `trait` is not a keyword. An unknown bound is `unknown-bound`. `impl` blocks only target your own
structs and enums, with type arguments that are plain parameters (`impl[T] Stack[T]`). There is no `impl Trait for T`.

| Bound | Grants | Satisfied by |
|-------|--------|--------------|
| `Eq` | `==` `!=` | every type that holds no function: not `fn` types, nor tuples, generic instantiations, structs or enums holding one at any depth |
| `Ord` | `<` `<=` `>` `>=` and `Eq` | `Int` `Float` `Str` `Bool` `Unit`, and tuples and lists of `Ord` types |
| `Hash` | use as a `Map` key, and `Eq` | `Int` `Str` `Bool` `Unit`, and tuples of those |

A bound is checked at every instantiation (`unsatisfied-bound`), at the end of the enclosing function if the type is not
yet known. `Ord` and `Hash` imply `Eq`; neither implies the other. Built-ins use the bounds too: `List.contains` and
`assert_eq` need `Eq`, `List.sort` needs `Ord`.

There are no higher-kinded types and no associated types or constants.

`Float` is not `Hash` because of `NaN`, and structs and enums are neither `Ord` nor `Hash`, which is also why ordering
and hashing can never reach a value that contains itself (section 8).

```vex example ty-generic-bound
fn largest[T: Ord](a: T, b: T) -> T { if a > b { a } else { b } }
fn main() {
    print(largest(1, 2));
    print(largest("a", "b"));
}
```

```output
2
b
```

```vex example ty-bound-unsatisfied
struct S {}
fn largest[T: Ord](a: T, b: T) -> T { if a > b { a } else { b } }
fn main() { largest(S {}, S {}); }
```

```error
unsatisfied-bound 3:13
```

```vex example ty-no-traits
trait Show {}
fn main() {}
```

```error
expected-item 1:1
```

```vex example ty-unknown-bound
fn f[T: Show](x: T) {}
fn main() {}
```

```error
unknown-bound 1:6
```

```vex example ty-eq-rejects-function-in-struct
struct S { f: fn() -> Int }
fn g() -> Int { 1 }
fn main() { print(S { f: g } == S { f: g }); }
```

```error
bad-operand 3:19
```

```vex example ty-hash-keys
fn main() {
    let mut m = #{(1, "a"): 1};
    m[(2, "b")] = 2;
    print(m.len());
    print(m[(1, "a")]);
}
```

```output
2
1
```

```vex example ty-hash-rejects-float
fn main() { let m = #{1.0: 2}; }
```

```error
bad-map-key 1:21
```

```vex example ty-ord-rejects-option
fn main() { print(Some(1) < Some(2)); }
```

```error
bad-operand 1:19
```

### Expressions

The operand types are the same on both sides, with no conversions:

```text
a + b                          a, b : Int | Float | Str          result: that type
a - b  a * b  a / b  a % b     a, b : Int | Float
-a                             a : Int | Float
a == b  a != b                 a, b : τ with Eq τ                result: Bool
a < b  a <= b  a > b  a >= b   a, b : τ with Ord τ               result: Bool
a && b  a || b  !a             Bool operands, result Bool
a..b  a..=b                    a, b : Int                        result: Range
```

When an operand type is still a variable, the requirement is deferred to the end of the function.

Literals have the obvious types; an interpolated string is `Str` and its parts may have any type. `if c { a } else { b }`
needs `c : Bool` and unifying branches. `if` without `else` has type `Unit` and its branch must be `Unit` or `Never`.
`match` patterns are checked against the scrutinee's type, guards are `Bool`, arms unify. `while` and `for` have type `Unit`.
`for` iterates a `Range` (elements `Int`) or a `List[T]` (elements `T`): a `Map` or `Str` is `not-iterable`, use `entries()`,
`keys()`, `values()` or `chars()`. A `let` or `for` pattern must be irrefutable (`refutable-pattern`).

`f(a…)` needs `f : fn(τ…) -> τ` with matching arity. `x.m(a…)` looks `m` up on `x`'s type: `impl` methods, then built-in
methods. A struct field holding a function is called `(x.f)(…)`; `x.f(…)` is a method call (`no-method`). A function with
a `mut` parameter cannot be used as a value (`mut-fn-value`).

`e?` on `Option[T]` has type `T` and needs the enclosing function or lambda to return an `Option`. On `Result[T, E]` it has
type `T` and needs the enclosing function to return `Result[U, E]` with the same `E` (`bad-try`). `s.f` substitutes the
struct's type arguments, `t.0` projects a tuple, `l[i]` needs `i : Int`, `m[k]` needs `k : K`; a `Str` is `not-indexable`.
A struct literal gives each field exactly once. A variant with a payload is a function: `xs.map(Some)` works.

A one-element tuple is a distinct type from its element.

```vex example ty-one-tuple
fn main() {
    let t: (Int,) = (1,);
    print(t.0);
    print(t);
}
```

```output
1
(1,)
```

### `Never`

`Never` is the type of expressions that do not produce a value: `return`, `break`, `continue`, `panic(..)` and `exit(..)`.
It **cannot be written** in a program (`unknown-type`), although the name is reserved. `Never` conforms to every type:
a branch or arm of type `Never` does not constrain the result, and unifying with `Never` does not solve an inference
variable, so a value of type `Never` gives no type information to anything else.

A block has type `Never` if it has no final expression and one of its expression statements has type `Never`. A `let`
statement does not count, even if its initialiser diverges. `while` and `for` are `Unit`, even `while true`.

An unannotated function whose body is `Never` has a return type nothing has solved, and a call to it is `cannot-infer`;
write its return type. Function results of any type accept a `Never` body.

```vex example ty-never-branch
fn f(n: Int) -> Int {
    let v = if n > 0 { n } else { return 0 };
    v * 2
}
fn main() {
    print(f(2));
    print(f(-1));
}
```

```output
4
0
```

```vex example ty-never-not-writable
fn f() -> Never { panic("x") }
fn main() { f(); }
```

```error
unknown-type 1:11
```

```vex example ty-diverging-needs-annotation
fn f() { panic("x"); }
fn main() { f(); }
```

```error
cannot-infer 2:13
```

```vex example ty-diverging-annotated
fn f() -> Int { panic("x") }
fn main() { print(f()); }
```

```panic
x
```

```vex example ty-let-does-not-diverge
fn f() -> Int { let x = return 1; }
fn main() { f(); }
```

```error
type-mismatch 1:35
```

```vex example ty-while-is-unit
fn f() -> Int { while true { return 1; } }
fn main() { f(); }
```

```error
type-mismatch 1:17
```

### Mutability

The *root* of an assignment target is the variable at its left end: in `a.b[i].c = v` it is `a`.

- `x = v` and `x += v` need `x` declared `let mut`, or a `mut` parameter.
- `p.f = v` and `xs[i] = v` need a mutable root. Tuples are immutable (`immutable-binding`).
- A method declared `mut self`, and the mutating built-ins (`push` `pop` `insert` `remove` `remove_at` `clear` `extend`
  `reverse` `sort`), need a mutable root.
- Passing a `List`, `Map` or struct to a `mut` parameter needs a mutable root.
- Pattern bindings and loop variables are immutable unless `let` patterns say `mut`.
- The rule only looks at a root that is a variable. A target whose root is any other expression (a call, a block, a literal)
  is not restricted, and it may reach an aggregate aliased elsewhere.

```vex example val-root-not-a-variable
fn main() {
    let xs = [1];
    { xs }.push(2);
    print(xs);
}
```

```output
[1, 2]
```

### Patterns and exhaustiveness

Pattern typing: a literal has its literal's type; `(p, …)` needs a tuple of that arity; `V(p…)` needs the variant's enum
(arity checked, `variant-arity`); `S { f: p, … }` needs the struct, naming only existing fields and all of them unless
`..` is present. Bindings get the scrutinee's sub-type, and each alternative of an or-pattern binds the same names at the
same types (`or-pattern-bindings`, `duplicate-binding`).

Exhaustiveness uses Maranget's matrix algorithm over constructors. `Bool`, `Unit`, enums, tuples and structs have finite
constructors; `Int`, `Float` and `Str` literals never cover their type, so such a match needs a catch-all. Arms with guards
do not count towards coverage. A `match` with no arms is non-exhaustive. If values are uncovered, `non-exhaustive-match`
names one witness. An arm covered by earlier unguarded arms is the warning `unreachable-pattern`; the program still runs.

```vex example ty-exhaustive-witness
enum E { A, B }
fn main() { match E.A { E.A => print(1) } }
```

```error
non-exhaustive-match 2:19
```

```vex example ty-guards-do-not-cover
fn main() { match 1 { x if x > 0 => print(1) } }
```

```error
non-exhaustive-match 1:19
```

```vex example ty-unreachable-only-warns
fn main() { match 1 { _ => print(1), 2 => print(2) } }
```

```output
1
```

## 6. Evaluation

**Everything is evaluated left to right, each operand exactly once, and strictly**, with the exceptions listed. An
expression's sub-expressions complete, in source order, before the expression itself takes effect. A failure (section 9)
stops the program at the point it happens, and everything before it has happened.

| Expression | Order |
|------------|-------|
| `a op b` | `a`, `b`, then the operation |
| `a && b`, `a \|\| b` | `a`; `b` only if needed. The operands are `Bool`, and the whole expression is not otherwise lazy |
| `a..b`, `a..=b` | `a`, `b` |
| `[a, b]`, `(a, b)` | in order |
| `#{k: v, …}` | each key, then its value, in source order |
| interpolation | parts in source order, converted at the end |
| `f(a, b)` | the callee if it is a value (not a named function), then `a`, `b` |
| `x.m(a)` | receiver `x`, then the arguments |
| `S { b: x, a: y }` | field initialisers in the order **written**, not declared |
| `obj.f`, `xs[i]` | object, then index |
| `x = e` | `e`, then the store |
| `obj.f = e` | `obj`, then `e`, then the store |
| `xs[i] = e` | `xs`, `i`, `e`, then the bounds check and store |
| `x += e` | reads `x` **first**, then `e`, then adds and stores |
| `obj.f += e` | `obj`, reads the field, then `e`, then stores |
| `xs[i] += e` | `xs`, `i`, reads `xs[i]` (bounds checked here), then `e`, then stores |
| `let p = e;` | `e`, then binds `p` |
| `if` | condition, then one branch |
| `match e` | `e` once. Arms in order: a pattern is tested left to right and binds as it goes, then its guard |
| `for p in e` | `e` once, before the first iteration |
| `while c` | `c` at the start of every iteration |

```vex example ev-operands
fn t(s: Str, v: Int) -> Int { print(s); v }
fn add(a: Int, b: Int) -> Int { a + b }
fn main() {
    add(t("arg0", 1), t("arg1", 2));
    t("left", 1) + t("right", 2);
    [t("list0", 0), t("list1", 1)];
    (t("tuple0", 0), t("tuple1", 1));
    #{t("key", 0): t("value", 1)};
    t("lo", 0)..t("hi", 1);
    [1, 2][t("index", 0)];
    print("{t("p0", 0)}-{t("p1", 1)}");
}
```

```output
arg0
arg1
left
right
list0
list1
tuple0
tuple1
key
value
lo
hi
index
p0
p1
0-1
```

```vex example ev-callee-then-args
fn f(s: Str) -> fn(Int) -> Int { print(s); |x| x }
fn v(s: Str, n: Int) -> Int { print(s); n }
fn r(s: Str) -> List[Int] { print(s); [] }
fn main() {
    f("callee")(v("arg", 1));
    r("receiver").contains(v("method arg", 1));
}
```

```output
callee
arg
receiver
method arg
```

```vex example ev-struct-written-order
fn t(s: Str, v: Int) -> Int { print(s); v }
struct P { a: Int, b: Int }
fn main() { print(P { b: t("b", 2), a: t("a", 1) }); }
```

```output
b
a
P { a: 1, b: 2 }
```

In a map literal a repeated key keeps its first position and takes its last value.

```vex example ev-map-duplicate-key
fn main() { print(#{1: "a", 2: "b", 1: "c"}); }
```

```output
{1: "c", 2: "b"}
```

```vex example ev-compound-reads-first
fn main() {
    let mut x = 1;
    let f = || { x = 10; 5 };
    x += f();
    print(x);
}
```

```output
6
```

```vex example ev-index-assign-checks-last
fn t(s: Str, v: Int) -> Int { print(s); v }
fn main() {
    let mut xs = [0];
    xs[t("index", 5)] = t("value", 1);
}
```

```panic
index
value
index 5 out of bounds for list of length 1
```

```vex example ev-compound-index-checks-first
fn t(s: Str, v: Int) -> Int { print(s); v }
fn main() {
    let mut xs = [0];
    xs[t("index", 5)] += t("value", 1);
}
```

```panic
index
index 5 out of bounds for list of length 1
```

```vex example ev-short-circuit
fn t(s: Str, b: Bool) -> Bool { print(s); b }
fn main() {
    t("a", false) && t("b", true);
    t("c", true) || t("d", true);
    print(true || panic("never evaluated"));
}
```

```output
a
c
true
```

```vex example ev-match-order
fn v() -> Int { print("scrutinee once"); 1 }
fn t(s: Str, b: Bool) -> Bool { print(s); b }
fn main() {
    match v() {
        2 => print("two"),
        1 if t("guard 1", false) => print("no"),
        1 if t("guard 2", true) => print("yes"),
        _ => print("other"),
    }
    match (1, 2) {
        (a, 1) | (1, a) => print(a),
        _ => print("none"),
    }
}
```

```output
scrutinee once
guard 1
guard 2
yes
2
```

### Loops

`for` evaluates its iterable once. A `Range` is a snapshot. A `List` is **read live, by position**: on each iteration the
next index is checked against the list's current length, so `push` during iteration extends it and `clear` ends it. `for`
over a list holds no copy.

```vex example ev-for-reads-the-list-live
fn main() {
    let mut xs = [1, 2];
    for x in xs {
        if xs.len() < 4 { xs.push(x * 10); }
        print(x);
    }
}
```

```output
1
2
10
20
```

```vex example ev-while-condition-each-iteration
fn main() {
    let mut n = 0;
    let more = || { n += 1; n < 3 };
    while more() { }
    print(n);
}
```

```output
3
```

### `break`, `continue` and `return` inside expressions

`break`, `continue` and `return` are expressions of type `Never` and may appear anywhere an expression may, including inside
operands that are half evaluated. They jump, and whatever operands had been evaluated so far are discarded; nothing further
in that expression is evaluated. `break` and `continue` refer to the innermost enclosing `while` or `for` **body**. A
`while` condition and a `for` iterable are outside that loop's body, so in them `break` targets an enclosing loop. A lambda
is a new function: `break` inside one with no loop of its own is `outside-loop`. `return` inside a lambda returns from
the lambda.

```vex example ev-break-mid-list
fn main() {
    let mut n = 0;
    while n < 3 {
        n += 1;
        print([1, if n == 2 { break } else { n }, 3]);
    }
    print(n);
}
```

```output
[1, 1, 3]
2
```

```vex example ev-continue-as-argument
fn add(a: Int, b: Int) -> Int { a + b }
fn main() {
    for i in 0..3 {
        print(add(i, if i == 1 { continue } else { 2 }));
    }
}
```

```output
2
4
```

```vex example ev-continue-in-interpolation
fn main() {
    for i in 0..3 {
        print("{i}{if i == 1 { continue } else { "x" }}");
    }
}
```

```output
0x
2x
```

```vex example ev-break-bare-operand
fn main() {
    let mut c = 0;
    while c < 2 {
        c += 1;
        print(c + break);
    }
    print(c);
}
```

```output
1
```

```vex example ev-break-in-while-condition
fn main() {
    let mut i = 0;
    while i < 2 {
        i += 1;
        while { if i == 1 { break } false } { }
        print(i);
    }
    print("done");
}
```

```output
done
```

```vex example ev-break-outside-loop-in-lambda
fn main() {
    while true {
        let f = || { break; 1 };
    }
}
```

```error
outside-loop 3:22
```

```vex example ev-return-inside-operands
fn f() -> Int { [1, return 7, 3].len() }
fn main() { print(f()); }
```

```output
7
```

```vex example ev-return-from-lambda
fn g() -> Int {
    let f = || { return 5; };
    f();
    1
}
fn main() { print(g()); }
```

```output
1
```

```vex example ev-try-inside-operands
fn g(o: Option[Int]) -> Option[Int] { Some([1, o?, 3].len()) }
fn main() {
    print(g(None));
    print(g(Some(1)));
}
```

```output
None
Some(3)
```

`?` returns the `None` or `Err` value from the enclosing function or lambda; otherwise it evaluates to the payload.

```vex example ev-try-in-lambda
fn main() {
    let f = |o: Option[Int]| Some(o? + 1);
    print(f(None));
    print(f(Some(1)));
}
```

```output
None
Some(2)
```

### Closures

A lambda captures at creation. An immutable variable is captured **by value**. A `let mut` variable (or `mut` parameter)
that some closure captures is shared: the function and every closure see the same variable. Each execution of a
`let mut` in a loop body creates a new variable, so a closure captures a separate one per iteration. A lambda cannot
refer to itself (`undefined-name`); use a named function.

```vex example ev-closure-capture
fn main() {
    let a = 1;
    let f = || a;
    let a = 2;
    print(f());
    let mut b = 1;
    let g = || b;
    b = 2;
    print(g());
    let mut fs: List[fn() -> Int] = [];
    for i in 0..2 {
        let mut j = i;
        fs.push(|| { j += 1; j });
    }
    print(fs[0]());
    print(fs[0]());
    print(fs[1]());
}
```

```output
1
2
1
2
2
```

Calls pass arguments by value in the sense of section 8: an aggregate argument is the same object. A `mut` parameter
is a local variable; assigning to it does not affect the caller, but mutating an aggregate through it does.

## 7. Integers and floats

`Int` is a signed 64-bit integer, `-2^63` to `2^63 - 1`. **Every** result outside that range stops the program with a
runtime error: `+`, `-`, `*`, `/`, unary `-`, `abs`, `pow`, `sum`, `math.gcd`, `Range.len`, `to_int` and compound assignment.
There is no wraparound, saturation or silent bignum. `/` truncates toward zero. `%` has the sign of its left operand.
`/` and `%` by zero are runtime errors. `INT_MIN / -1` overflows; `INT_MIN % -1` is `0`.

```vex example int-add-overflow
fn main() { let x = math.INT_MAX; print(x + 1); }
```

```panic
integer overflow in `+`
```

```vex example int-compound-overflow
fn main() { let mut x = math.INT_MAX; x += 1; }
```

```panic
integer overflow in `+`
```

```vex example int-negate-overflow
fn main() { let x = math.INT_MIN; print(-x); }
```

```panic
integer overflow in unary `-`
```

```vex example int-pow-overflow
fn main() { print(2.pow(63)); }
```

```panic
integer overflow in `pow`
```

```vex example int-division
fn main() {
    print(-7 / 2);
    print(-7 % 3);
    print(7 % -3);
    print(math.INT_MIN % -1);
}
```

```output
-3
-1
1
0
```

```vex example int-min-divided-by-minus-one
fn main() { print(math.INT_MIN / -1); }
```

```panic
integer overflow in `/`
```

```vex example int-division-by-zero
fn main() { print(1 % 0); }
```

```panic
division by zero in `%`
```

`Float` is IEEE-754 double. `+ - *` follow IEEE (overflow to `inf`, `NaN` propagates), but `/` and `%` by zero (including
`-0.0`) are runtime errors. `%` by a non-zero divisor is the remainder with the sign of the left operand; an infinite left
operand gives `NaN`. `NaN` is not equal to anything including itself and every ordering comparison with it is `false`.
`Float.to_int` truncates, and rejects `NaN`, the infinities and values out of `Int` range.

```vex example float-ieee
fn main() {
    let inf = 1e308 * 10.0;
    print(inf);
    print(-inf);
    print(0.1 + 0.2);
    print(-7.5 % 2.0);
    print(inf % 2.0);
    print(0.0 == -0.0);
    print(3.9.to_int());
}
```

```output
inf
-inf
0.30000000000000004
-1.5
NaN
true
3
```

```vex example float-division-by-zero
fn main() { print(1.0 / 0.0); }
```

```panic
division by zero
```

```vex example float-nan
fn main() {
    let n = (0.0 - 1.0).sqrt();
    print(n == n);
    print(n != n);
    print(n < n);
}
```

```output
false
true
false
```

```vex example float-to-int-rejects-nan
fn main() { print((0.0 - 1.0).sqrt().to_int()); }
```

```panic
cannot convert NaN to Int
```

## 8. Values and memory

`Int`, `Float`, `Bool`, `Unit`, `Str` and `Range` are immutable. A `Str` is a sequence of Unicode scalar values and is
indexed by them; `len` counts them. `List`, `Map`, struct values and tuples are **references to objects**: assigning, passing
or capturing one aliases the same object and never copies. A tuple object and an enum value cannot be changed, but they can
refer to objects that can. The built-in `copy` makes a shallow copy of a `List`.

Vex has no explicit allocation or freeing, no pointers, no destructors and no finalisers. Objects are managed by the host
runtime, and memory is reclaimed when nothing can reach an object, cycles included; when that happens is not observable.

```vex example val-aliasing
struct P { a: Int }
fn main() {
    let mut p = P { a: 1 };
    let mut q = p;
    q.a = 2;
    print(p.a);
    let mut xs = [[1]];
    let ys = xs.copy();
    xs[0].push(2);
    xs.push([3]);
    print(ys);
}
```

```output
2
[[1, 2]]
```

```vex example val-mut-parameter
fn f(mut xs: List[Int]) { xs.push(1); }
fn g(mut n: Int) { n += 1; print(n); }
fn main() {
    let mut a = [0];
    f(a);
    print(a);
    let b = 1;
    g(b);
    print(b);
}
```

```output
[0, 1]
2
1
```

```vex example val-mut-parameter-needs-mutable-root
fn f(mut xs: List[Int]) { }
fn main() {
    let a = [0];
    f(a);
}
```

```error
immutable-binding 4:7
```

```vex example val-tuple-immutable
fn main() { let mut t = (1, 2); t.0 = 3; }
```

```error
immutable-binding 1:33
```

### Equality

`==` is structural and needs `Eq`. Values of different variants differ; a struct or enum's fields are compared in order. A
`Map` compares as a set of key/value pairs, ignoring order. Two ranges are equal if they contain the same numbers, so all
empty ranges are equal. Elements that are the **same object** are equal without being compared; that makes `a == a` true on
any value, and also means a `NaN` inside a container can compare equal where a scalar `NaN` does not. Which `NaN` values are the
same object is unspecified, so code must not rely on equality of containers holding `NaN`.

```vex example val-equality
fn main() {
    print((1..=3) == (1..4));
    print((0..0) == (5..5));
    print(#{1: 2, 3: 4} == #{3: 4, 1: 2});
    print(() == ());
    print([1, 2] == [1, 2]);
    let n = (0.0 - 1.0).sqrt();
    let a = [n];
    print(n == n);
    print(a == a);
}
```

```output
true
true
true
true
true
false
true
```

### Order

`Int` and `Float` are numeric, `Str` is by code point, `false < true`, and `Unit` is equal to itself. Lists and tuples are
lexicographic: they compare at the first position whose elements differ, and a shorter list that is a prefix of a longer
one is smaller. `sort` and `sorted` are stable and ascending; with a `NaN` present the resulting order is unspecified.

```vex example val-order
fn main() {
    print([1, 2] < [1, 3]);
    print((1, "b") < (1, "a"));
    print("a" < "B");
    print("é" > "z");
    print(false < true);
    print([1] < [1, 0]);
    print(() < ());
    print(() <= ());
}
```

```output
true
false
false
true
true
true
false
true
```

### `Map`

A `Map` keeps insertion order. Re-inserting an existing key keeps its position; removing and re-adding moves it to the end.
Keys are compared structurally. `Map` entries are visited in that order by `keys`, `values` and `entries`.

```vex example val-map-order
fn main() {
    let mut m = #{"b": 1, "a": 2};
    m.insert("c", 3);
    m.insert("b", 9);
    print(m);
    m.remove("b");
    m.insert("b", 1);
    print(m.keys());
}
```

```output
{"b": 9, "a": 2, "c": 3}
["a", "c", "b"]
```

### Cycles

A list, map or tuple cannot directly contain itself: the types would be infinite (`infinite-type`). A cycle therefore always
passes through a struct field or enum payload, which can be assigned after construction. A cycle is legal and is
collected when unreachable. Printing or interpolating a value that contains itself, or comparing two values that agree all
the way to a cycle, is a runtime error at that operation. `a == a` on one object is true without reaching the cycle;
comparison that finds a difference before any cycle returns `false`. Ordering and map keys cannot reach a cycle at all,
because structs and enums are not `Ord` or `Hash`.

```vex example val-list-cannot-contain-itself
fn main() { let mut xs = []; xs.push(xs); }
```

```error
infinite-type 1:38
```

```vex example val-cycle-print
struct N { next: Option[N] }
fn main() {
    let mut a = N { next: None };
    a.next = Some(a);
    print(a);
}
```

```panic
cannot print a value that contains itself
```

```vex example val-cycle-compare
struct N { next: Option[N] }
fn main() {
    let mut a = N { next: None };
    a.next = Some(a);
    let mut b = N { next: None };
    b.next = Some(b);
    print(a == a);
    print(a == b);
}
```

```panic
true
cannot compare values that contain themselves
```

### Display

`print`, `eprint` and interpolation use one format. At top level a `Str` is its text; nested in anything else it is quoted, with
`\\`, `\"`, `\n` and `\t` escaped. `Unit` is `()`, a one-element tuple keeps its comma, a struct prints as `Name { f: v }`, a
nullary variant by name, a variant with payload as `Name(v)`. A float prints in shortest round-trip form with at least one
fractional digit or an exponent, `inf`, `-inf`, or `NaN`. A `Range` keeps no record of how it was written and prints half-open,
`a..b`, unless its end would exceed `Int`, when it prints `a..=b`. A function prints as `<fn name>` or `<builtin name>`.

```vex example val-display
struct P { a: Int, b: Str }
enum C { A, B(Int, Str) }
fn g() {}
fn main() {
    print(P { a: 1, b: "x" });
    print(C.A);
    print(C.B(1, "y"));
    print(["a\n\"b"]);
    print((1,));
    print(());
    print(Some("x"));
    print(1..=3);
    print(0..=math.INT_MAX);
    print(1.0);
    print(1e21);
    let f = g;
    print(f);
}
```

```output
P { a: 1, b: "x" }
A
B(1, "y")
["a\n\"b"]
(1,)
()
Some("x")
1..4
0..=9223372036854775807
1.0
1e+21
<fn g>
```

## 9. Errors

Vex has two kinds of failure, and they do not mix.

**A recoverable failure is a value**: `Option` or `Result`. It is handled by `match`, `?`, `unwrap_or`, `map_err` and so on. `io.*`
returns `Err` when the operating system refuses, and parsing such as `Str.to_int` returns `None`.

**A panic is a runtime error**: it stops the program immediately and **cannot be caught by Vex code**. There is no `try` and
no unwinding hook. Everything printed before the panic stays printed. Causes:

| Cause | Message |
|-------|---------|
| `panic(msg)` | `msg` |
| `assert` / `assert_eq` | `assertion failed`, `assertion failed: left = …, right = …` |
| `unwrap`, `expect`, `unwrap_err` | `called `unwrap()` on a `None` value`, the `expect` message, … |
| Integer overflow | ``integer overflow in `+` `` and so on |
| Division | `division by zero`, ``division by zero in `%` `` |
| List index, map key | `index i out of bounds for list of length n`, `key k not found in map` |
| Conversions | `cannot convert NaN to Int`, `negative exponent in `pow``, `… is not a valid code point` |
| Self-containing values | `cannot print a value that contains itself`, `cannot compare values that contain themselves` |
| Limits (section 10) | `string too large`, `list too large (…)`, `stack overflow (…)`, `recursion too deep (…)` |
| Memory | `out of memory` |

### Compile-time diagnostics

Compilation reports **every** error it can find, each with a stable kebab-case code, a `file:line:col` location (1-based,
counting characters), the source line with a caret, and optional notes, help text and secondary locations. Lexing and parsing
are one stage; if it reports errors, later stages do not run. Name resolution and type checking are one stage. Diagnostics are sorted by
position, duplicates are removed, and at most 50 are reported before `too-many-errors`. A program with errors is never run.
Warnings go to standard error before the program runs, and do not stop it. The codes in use are listed by
`tests/errors/*.vx`, one file per code and position.

### Runtime errors

A runtime error is reported as `runtime error: <message>`, the failing source line with a caret, then a stack trace, most
recent call first, as `in <function> at file:line:col` lines (long traces are shortened in the middle). Standard output is
flushed first, so program output comes before the error.

### Exit codes (`vex run`)

| Code | Meaning |
|------|---------|
| 0 | `main` returned `Unit`, or `main` returned `0` |
| the value | `main` returned an `Int`, or the program called `exit(n)` |
| 1 | compile error, or the source file cannot be read |
| 2 | runtime error, or standard output failed |
| 70 | internal error in the implementation (a bug in Vex) |
| 130 | interrupted |

`exit` and an `Int` from `main` give the number to the operating system, which may reduce it (POSIX keeps the low 8 bits). `vex check`
exits `0` or `1`. An embedded call reports status codes instead; see `docs/ffi.md`. `exit` is not available to an embedded script.

```vex example err-panic-stops-everything
fn main() {
    print(1);
    panic("boom");
    print(2);
}
```

```panic
1
boom
```

```vex example err-result-is-a-value
fn parse(s: Str) -> Result[Int, Str] {
    match s.to_int() {
        Some(n) => Ok(n),
        None => Err("not a number"),
    }
}
fn twice(s: Str) -> Result[Int, Str] { Ok(parse(s)? * 2) }
fn main() {
    print(twice("21"));
    print(twice("x"));
}
```

```output
Ok(42)
Err("not a number")
```

```vex example err-try-needs-matching-result
fn f() -> Result[Int, Str] {
    let r: Result[Int, Int] = Err(1);
    Ok(r?)
}
fn main() { f(); }
```

```error
bad-try 3:8
```

```vex example err-unwrap-panics
fn main() { let o: Option[Int] = None; o.unwrap(); }
```

```panic
called `unwrap()` on a `None` value
```

```vex example err-index
fn main() { print([1][5]); }
```

```panic
index 5 out of bounds for list of length 1
```

```vex example err-map-key
fn main() { print(#{1: 2}[5]); }
```

```panic
key 5 not found in map
```

```vex example err-exit-code
fn main() {
    print(1);
    exit(7);
    print(2);
}
```

```output exit=7
1
```

```vex example err-main-returns-code
fn main() -> Int { 3 }
```

```output exit=3
```

## 10. Limits

These limits are part of the language. Each one, when exceeded, is a located error and never a crash of the host.

| Limit | Value | Name |
|-------|------:|------|
| Nesting of expressions, types and patterns while parsing | 150 | `parser.MAX_DEPTH` |
| Characters in a `Str` built by one operation | 50,000,000 | `runtime.MAX_STR_LEN` |
| Elements in a `List` built by one operation, a list literal or map literal | 10,000,000 | `runtime.MAX_LIST_LEN` |
| Nested calls (frames) | 100,000 | `vm.MAX_DEPTH` |
| Builtins calling back into Vex in an embedding (`map` inside `map`) | 100 | `vm.MAX_CALLBACK_DEPTH` |
| Calls into Vex nested through host functions in an embedding | 32 | `host.MAX_HOST_DEPTH` |
| Default execution budget of an embedded call, in units | 5,000,000 | `host.DEFAULT_BUDGET` |
| Diagnostics reported before stopping | 50 | `DiagSink.MAX_ERRORS` |

- **Literals.** `Int` and `Float` literal ranges are given in section 2. A list or map literal is subject to `MAX_LIST_LEN` when it
  is evaluated. A string literal is limited only by the source text: `MAX_STR_LEN` limits what operations build, not literals.
- **Nesting.** Each nested parenthesis, type or pattern and each prefix `-` or `!` uses one of the 150 levels (`too-deep`).
  An interpolation uses two. Statements nest block in block with no such counter, and if the host's recursion limit is reached
  while parsing, checking or compiling, that is the same located `too-deep`.
- **Strings and lists.** `MAX_STR_LEN` and `MAX_LIST_LEN` apply to `+`, interpolation, `extend`, literals, and every builtin returning a
  `Str` or `List`, as a runtime error before allocating. `io.read_file` returns `Err` instead. A `List` or `Map` can pass the cap only
  by growing one `push` or `insert` at a time; builtins then refuse to build a new list from it. There is no cap on total memory, and
  running out is `out of memory`.
- **Recursion.** A Vex-to-Vex call uses a frame on a heap-allocated stack, limited to 100,000 by count (`stack overflow`). A builtin
  that calls back (`map`, `filter`, …) uses the host's stack too, so such recursion can stop sooner with `recursion too deep`.
- **Budget.** A program run by `vex run` has no execution budget. An embedded call may spend the budget set by `budget(vm, units)`,
  default 5,000,000. One unit per loop back-edge and call, and per-element charges for data-sized work, are defined in
  [`docs/ffi.md`](ffi.md#execution-budget). The budget bounds work, not memory.

```vex example lim-string-cap
fn main() { print("a".repeat(50000001).len()); }
```

```panic
string too large
```

```vex example lim-list-cap
fn main() { print((0..10000001).to_list().len()); }
```

```panic
list too large (10000001 elements, the limit is 10000000)
```

```vex example lim-recursion
fn r(n: Int) -> Int { r(n + 1) }
fn main() { r(0); }
```

```panic
stack overflow (more than 100000 nested calls)
```

```vex example lim-nesting
fn main() { print(((1))); }
```

```output
1
```

## 11. Disagreements settled

Found while checking this document against the implementation. In every case the design decided which side was right; the
implementation is unchanged unless the entry says otherwise.

| # | Previous statement | Implementation | Resolution |
|--:|--------------------|----------------|------------|
| 1 | `tparams` needs one or more | `fn f[]` parses | Spec: empty is syntactically allowed |
| 2 | Lambda parameters have no trailing comma | Accepted | Spec: allowed |
| 3 | `fn(...)` types have no trailing comma | Accepted | Spec: allowed |
| 4 | Type arguments need one or more | `List[]` parses, then `type-arity` | Spec: syntax allows it, the checker rejects |
| 5 | Pattern paths have at most one dot | `A.B.C` parses, then `bad-pattern` | Spec: syntax allows it, the resolver rejects |
| 6 | The exponent form has no `_` in digits | `1_0e3` is valid | Spec: the digits before the exponent may have `_`. Exponent digits may not |
| 7 | Radix prefixes are lower-case, digits start immediately | `0X1F`, `0x_1` valid | Spec: as implemented |
| 8 | `variant` needs one or more payload types | `A()` parses; same as `A` | Spec: as implemented |
| 9 | `S { a .. }` valid, `S { .. }` not | Exactly the opposite | Spec: as implemented (`..` follows a comma or is alone) |
| 10 | Arm comma optional after a block body | Only when the body **starts** with `{`, whatever follows it | Spec: precise rule |
| 11 | `return [ expr ]` | No value only before `;` `}` `)` `,` or end of input; `[return]` is an error | Spec: `return_end` |
| 12 | Assignment target is an `lvalue` | A parenthesised target is valid too (`(x.y) = 1`) | Spec: `place` |
| 13 | `cond` "does not allow struct literals" | `S { }` in a `cond` is not an error, it is a name and a block | Spec: stated |
| 14 | Trivia is whitespace and comments | A byte order mark (U+FEFF) is trivia anywhere | Spec: `bom` |
| 15 | Tuples have at least 2 elements | One-element tuples exist | Spec: one or more |
| 16 | `x % 0` raises `division by zero` | The message is ``division by zero in `%` `` | Spec: as implemented; `docs/vm.md` fixed |
| 17 | `Eq` holds for "every type that contains no function" | `S { f: g } == S { f: g }` was accepted and compared closures by identity | **Code fixed** (`checker.satisfies`, with tests): a struct or enum holding a function has no `Eq` |
