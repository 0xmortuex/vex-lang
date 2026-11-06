# Vex grammar

Notation: EBNF. `"x"` is a literal token, `[ ... ]` optional, `{ ... }` zero or more, `|`
alternatives, `( ... )` grouping. Whitespace and comments (`// line`, `/* block, nestable */`)
may appear between any two tokens. Source files are UTF-8.

## Lexical structure

```ebnf
IDENT      = ( letter | "_" ) { letter | digit | "_" } ;      (* Unicode letters allowed *)
INT        = digit { digit | "_" }
           | "0x" hexdigit { hexdigit | "_" } | "0b" bin { bin | "_" } | "0o" oct { oct | "_" } ;
FLOAT      = digit { digit | "_" } "." digit { digit | "_" } [ exponent ]
           | digit { digit } exponent ;
exponent   = ( "e" | "E" ) [ "+" | "-" ] digit { digit } ;
STRING     = '"' { char | escape | interpolation } '"' ;
escape     = "\n" | "\t" | "\r" | "\0" | "\\" | '\"' | "\{" | "\}" | "\u{" hexdigit { hexdigit } "}" ;
interpolation = "{" expr "}" ;                                (* expr may itself contain strings *)
```

Keywords (cannot be used as names): `let mut fn struct enum impl match if else while for in
return break continue import true false`. `self` and `Self` are ordinary identifiers with a
special meaning inside `impl` blocks. A `.` followed by digits lexes the digits as an integer
so `t.0.1` is two tuple projections.

An integer literal must fit in a signed 64-bit `Int`; `9223372036854775808` is accepted only
directly after unary `-`.

## Items

```ebnf
module     = { item } ;
item       = import | function | struct | enum | impl ;

import     = "import" IDENT ";" ;
function   = "fn" IDENT [ tparams ] "(" [ params ] ")" [ "->" type ] block ;
params     = [ selfparam "," ] param { "," param } [ "," ]  |  selfparam [ "," ] ;
selfparam  = [ "mut" ] "self" ;                                (* only as the first parameter *)
param      = [ "mut" ] IDENT ":" type ;
tparams    = "[" tparam { "," tparam } [ "," ] "]" ;
tparam     = IDENT [ ":" IDENT { "+" IDENT } ] ;               (* bounds: Eq, Ord, Hash *)

struct     = "struct" IDENT [ tparams ] "{" [ field { "," field } [ "," ] ] "}" ;
field      = IDENT ":" type ;
enum       = "enum" IDENT [ tparams ] "{" [ variant { "," variant } [ "," ] ] "}" ;
variant    = IDENT [ "(" type { "," type } [ "," ] ")" ] ;
impl       = "impl" [ tparams ] type "{" { function } "}" ;
```

## Types

```ebnf
type       = IDENT [ "[" type { "," type } [ "," ] "]" ]       (* Int, List[T], Map[K, V], Option[T], user types *)
           | "(" [ type { "," type } [ "," ] ] ")"             (* () is Unit; (T) is T; (T,) is a 1-tuple *)
           | "fn" "(" [ type { "," type } ] ")" [ "->" type ] ; (* result defaults to Unit *)
```

## Statements and blocks

```ebnf
block      = "{" { stmt } [ expr ] "}" ;                       (* the optional final expr is the value *)
stmt       = let | exprstmt | ";" ;
let        = "let" pattern [ ":" type ] "=" expr ";" ;          (* `mut` is allowed inside pattern *)
exprstmt   = blocklike [ ";" ]
           | ( expr | assign ) ";" ;
assign     = lvalue assignop expr ;
assignop   = "=" | "+=" | "-=" | "*=" | "/=" | "%=" ;
lvalue     = IDENT | expr "." IDENT | expr "[" expr "]" ;      (* checked after parsing *)
blocklike  = ifexpr | match | while | for | block ;
```

A block-like expression in statement position ends the statement without `;` and is **not**
continued by a following operator (so `for ... { } -1` is a loop followed by the expression
`-1`). A following `.` or `?` does continue it.

## Expressions

```ebnf
expr       = range ;
range      = binary [ ( ".." | "..=" ) binary ] ;
binary     = unary { binop unary } ;                           (* by precedence, below *)
unary      = ( "-" | "!" ) unary | postfix ;
postfix    = primary { "(" [ args ] ")" | "[" expr "]" | "." ( IDENT | INT ) | "?" } ;
args       = expr { "," expr } [ "," ] ;

primary    = INT | FLOAT | STRING | "true" | "false"
           | IDENT                                             (* variable, function, variant, namespace *)
           | IDENT "{" [ fieldinit { "," fieldinit } [ "," ] ] "}"   (* struct literal; name starts uppercase *)
           | "(" ")" | "(" expr ")" | "(" expr "," [ expr { "," expr } [ "," ] ] ")"
           | "[" [ args ] "]"
           | "#{" [ expr ":" expr { "," expr ":" expr } [ "," ] ] "}"
           | lambda | ifexpr | match | while | for | block
           | "return" [ expr ] | "break" | "continue" ;

fieldinit  = IDENT [ ":" expr ] ;                              (* `x` alone means `x: x` *)
lambda     = "|" [ lparam { "," lparam } ] "|" ( expr | "->" type block )
           | "||" ( expr | "->" type block ) ;
lparam     = IDENT [ ":" type ] ;

ifexpr     = "if" cond block [ "else" ( ifexpr | block ) ] ;
match      = "match" cond "{" { arm } "}" ;
arm        = pattern [ "if" cond ] "=>" expr [ "," ] ;         (* "," optional after a `{ }` body or last arm *)
while      = "while" cond block ;
for        = "for" pattern "in" cond block ;
cond       = expr ;                                            (* struct literals not allowed unparenthesised *)
```

Binary operator precedence, loosest first (all left-associative):

| Level | Operators |
|-------|-----------|
| 1 | `\|\|` |
| 2 | `&&` |
| 3 | `==` `!=` |
| 4 | `<` `<=` `>` `>=` |
| 5 | `+` `-` |
| 6 | `*` `/` `%` |

Ranges (`..`, `..=`) bind looser than all of these, so `0..n + 1` means `0..(n + 1)`.
Unary `-` and `!` bind tighter than binary operators; postfix operators bind tightest.

## Patterns

```ebnf
pattern    = pattern1 { "|" pattern1 } ;                       (* or-pattern: same variables in each side *)
pattern1   = "_"
           | [ "mut" ] IDENT                                   (* binds; `mut` only in `let` *)
           | literal                                           (* INT, FLOAT, STRING (no interpolation), true, false, -INT, -FLOAT *)
           | "(" ")" | "(" pattern ")" | "(" pattern "," [ pattern { "," pattern } [ "," ] ] ")"
           | IDENT [ "." IDENT ] "(" [ pattern { "," pattern } [ "," ] ] ")"   (* variant: Some(x), Shape.Circle(r) *)
           | IDENT "." IDENT                                   (* nullary variant: Shape.Empty *)
           | IDENT "{" [ fpat { "," fpat } [ "," ] [ ".." ] ] "}"   (* struct pattern *)
fpat       = IDENT [ ":" pattern ] ;
```

A bare uppercase identifier in a pattern (`None`, `Dot`) must name a variant that is unique among
all enums; a lowercase identifier is always a new variable.
