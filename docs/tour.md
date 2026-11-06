# A tour of Vex

Vex is a small, statically typed language. You write types where they matter (function
parameters, struct fields) and the compiler infers the rest. Programs are compiled to a
compact stack bytecode and run on a VM.

Every snippet below is real: save it as `tour.vx` and run `vex run tour.vx`.

- [Hello](#hello) · [Values and types](#values-and-types) · [Variables](#variables-and-mutability)
- [Functions](#functions) · [Control flow](#control-flow) · [Strings](#strings)
- [Lists, maps, tuples](#lists-maps-and-tuples) · [Structs and methods](#structs-and-methods)
- [Enums and match](#enums-and-match) · [Option, Result and `?`](#option-result-and-)
- [Closures](#closures-and-first-class-functions) · [Generics](#generics) · [Modules](#modules)
- [Standard library](#standard-library) · [Errors](#errors-and-diagnostics) · [The REPL](#the-repl)

## Hello

```vex
fn main() {
    print("Hello, Vex!");
}
```

`main` takes no parameters and returns `Unit` (nothing) or an `Int` (the process exit code).
Statements end in `;`. The last expression of a block, with no `;`, is the block's value.

## Values and types

| Type | Examples | Notes |
|------|----------|-------|
| `Int` | `42`, `-7`, `1_000`, `0xff`, `0b101` | 64-bit signed, **checked** (overflow is a runtime error) |
| `Float` | `3.14`, `1e-3`, `2.0` | IEEE double; `1` and `1.0` are different types |
| `Bool` | `true`, `false` | |
| `Str` | `"héllo"` | Unicode text; `len()` counts characters (code points) |
| `Unit` | `()` | the "no value" type |
| `List[T]` | `[1, 2, 3]` | growable, indexed from 0 |
| `Map[K, V]` | `#{"a": 1}` | insertion ordered; keys: `Int`, `Str`, `Bool` or tuples of those |
| tuples | `(1, "a", true)` | fixed size, immutable; fields are `t.0`, `t.1` |
| functions | `fn(Int) -> Int` | first-class |
| `Option[T]` | `Some(1)`, `None` | built in |
| `Result[T, E]` | `Ok(1)`, `Err("bad")` | built in |

There is **no implicit conversion**: `1 + 2.0` is a type error. Convert explicitly with
`n.to_float()`, `x.to_int()` (truncates), `n.to_str()`.

## Variables and mutability

```vex
fn main() {
    let name = "vex";          // immutable; type inferred as Str
    let mut count = 0;         // mutable
    count += 1;
    let xs: List[Int] = [];    // annotate when inference has nothing to go on
    let (a, b) = (1, "two");   // destructuring
    let name = name + "!";     // shadowing is fine
    print("{name} {count} {a} {b} {xs}");
}
```

Mutating methods (`push`, `insert`, ...), field assignment and index assignment all
require the variable to be declared `let mut`. The compiler tells you exactly which one:

```text
error[immutable-binding]: cannot call `push` on `xs`: it is not declared `mut`
 --> demo.vx:3:8
  |
3 |     xs.push(2);
  |        ^^^^ `xs` is immutable
 ::: demo.vx:2:9
  |
2 |     let xs = [1];
  |         -- declared here
  = help: declare it with `let mut xs`
```

**Aggregates are references.** `let b = a;` for a list, map or struct makes `b` another name
for the same object (like Python). Because `b` is immutable you cannot mutate through it,
but if `a` is `let mut` you will see `a`'s changes through `b`. Use `.copy()` for a copy.

## Functions

```vex
fn add(a: Int, b: Int) -> Int { a + b }

// The return type is optional: here it is inferred as Int.
fn square(n: Int) { n * n }

fn apply_twice(f: fn(Int) -> Int, x: Int) -> Int { f(f(x)) }

fn main() {
    print(add(2, 3));
    print(square(7));
    print(apply_twice(|n| n + 10, 1));
}
```

- Parameter types are always written; return types can be inferred (recursive functions
  too, as long as they are not generic).
- Functions can be used before they are defined and can be mutually recursive.
- `return x;` exits early. A function whose last statement ends in `;` returns `Unit`.
- A parameter is immutable unless written `mut x: T`. A `mut` parameter of a list, map or
  struct type lets the callee modify the caller's object, so the caller must pass a
  `let mut` variable. Functions with `mut` parameters cannot be used as first-class values.

## Control flow

`if` is an expression and `else` is required when you use its value:

```vex
fn main() {
    let n = 7;
    let kind = if n % 2 == 0 { "even" } else { "odd" };
    print(kind);

    let mut i = 0;
    while i < 3 {
        i += 1;
        if i == 2 { continue; }
        print(i);
    }

    for k in 0..3 { print(k); }          // 0 1 2
    for k in 1..=3 { print(k); }         // 1 2 3
    for (idx, ch) in "abc".chars().enumerate() { print("{idx}{ch}"); }
}
```

`break` and `continue` work in `while` and `for`. `for` iterates ranges (`a..b`, `a..=b`)
and lists. To iterate a map use `m.entries()`, `m.keys()` or `m.values()`.

## Strings

```vex
fn main() {
    let who = "world";
    print("hello, {who}! 2 + 2 = {2 + 2}");   // interpolation takes any expression
    print("a literal brace: \{ and \}");        // escape { as \{
    print("tab:\t quote:\" backslash:\\ unicode:\u{1F600}");
    print("İstanbul".lower());
    print("a,b,c".split(",").join("|"));
}
```

Because `{` starts an interpolation, write `\{` for a literal opening brace (a lone `}` needs
no escape). Strings can span lines. All source files are UTF-8.

## Lists, maps and tuples

```vex
fn main() {
    let mut xs = [3, 1, 2];
    xs.push(4);
    xs.sort();
    print(xs);                                  // [1, 2, 3, 4]
    print(xs.map(|x| x * x).filter(|x| x > 4)); // [9, 16]
    print(xs.fold(0, |acc, x| acc + x));        // 10

    let mut ages = #{"ada": 36};
    ages.insert("alan", 41);
    print(ages.get("ada"));                     // Some(36)
    print(ages["alan"]);                        // 41 (runtime error if missing)

    let t = (1, "x");
    print(t.1);
}
```

`xs[i]` out of range is a runtime error with a stack trace; `xs.get(i)` returns an `Option`
instead. See [the standard library](#standard-library) for the full list of methods.

## Structs and methods

```vex
struct Account {
    owner: Str,
    balance: Int,
}

impl Account {
    fn open(owner: Str) -> Account {            // no self: called as Account.open(...)
        Account { owner: owner, balance: 0 }
    }
    fn deposit(mut self, amount: Int) {         // `mut self` may modify the receiver
        self.balance += amount;
    }
    fn describe(self) -> Str {
        "{self.owner}: {self.balance}"
    }
}

fn main() {
    let mut acct = Account.open("ada");
    acct.deposit(50);
    print(acct.describe());
}
```

Field shorthand works (`Account { owner, balance }`). Calling a `mut self` method needs a
`let mut` receiver. A struct field that holds a function is called with parentheses:
`(h.handler)(3)`.

## Enums and match

```vex
enum Shape {
    Circle(Float),
    Rect(Float, Float),
    Empty,
}

fn area(s: Shape) -> Float {
    match s {
        Shape.Circle(r) => 3.14159 * r * r,
        Shape.Rect(w, h) => w * h,
        Shape.Empty => 0.0,
    }
}
```

`match` must be **exhaustive**. Forget a case and the compiler names the missing pattern:

```text
error[non-exhaustive-match]: non-exhaustive `match`
 --> shape.vx:9:11
  |
9 |     match s {
  |           ^ pattern `Shape.Empty` is not covered
  = help: add an arm for it, or a catch-all arm `_ => ...`
```

Patterns: `_`, variables, literals (`1`, `"a"`, `true`, `-1`), tuples `(a, b)`, variants
`Shape.Circle(r)` (or just `Circle(r)` when the name is unique), structs `P { x, y: 0, .. }`,
or-patterns `1 | 2`, and guards `x if x > 0`. Unreachable arms are reported as warnings.

Enums can be generic and recursive:

```vex
enum Tree[T] { Leaf, Node(Tree[T], T, Tree[T]) }
```

## Option, Result and `?`

There is no null. A value that might be missing is an `Option[T]`; an operation that can fail
is a `Result[T, E]`. The `?` operator unwraps the success case or returns the failure from the
enclosing function:

```vex
fn parse_pair(a: Str, b: Str) -> Result[Int, Str] {
    let x = a.to_int().ok_or("not a number: " + a)?;   // Option -> Result, then `?`
    let y = b.to_int().ok_or("not a number: " + b)?;
    Ok(x + y)
}

fn main() {
    print(parse_pair("1", "2"));      // Ok(3)
    print(parse_pair("1", "zz"));     // Err("not a number: zz")
}
```

`?` on an `Option` needs a function returning `Option`; on a `Result` it needs a `Result`
with the **same error type** (use `.map_err(...)` to convert).

## Closures and first-class functions

```vex
fn make_counter() -> fn() -> Int {
    let mut n = 0;
    || { n += 1; n }          // captures `n` by reference; it is shared, not copied
}

fn main() {
    let next = make_counter();
    next();
    print(next());                          // 2
    let add = |a: Int, b: Int| a + b;       // annotate lambda parameters when needed
    print([1, 2, 3].map(|x| add(x, 10)));
}
```

Lambda parameter types are inferred from context (`xs.map(|x| ...)` knows `x`), otherwise
annotate them. Each loop iteration captures its own copy of the loop variable.

## Generics

Type parameters are declared explicitly in `[...]`. Inside a generic function a type
parameter is opaque: you can pass values around, but to compare them you must say so with a
bound.

```vex
fn largest[T: Ord](xs: List[T]) -> Option[T] {
    if xs.is_empty() { return None; }
    let mut best = xs[0];
    for x in xs { if x > best { best = x; } }
    Some(best)
}

struct Stack[T] { items: List[T] }

impl[T] Stack[T] {
    fn new() -> Stack[T] { Stack { items: [] } }
    fn push(mut self, x: T) { self.items.push(x); }
}
```

Bounds: `Eq` (supports `==`), `Ord` (supports `<`, also implies `Eq`) and `Hash` (usable as a map
key). `Stack.new()` infers `T` from later use.

## Modules

`import util;` loads `util.vx` from the same directory. Imports are **flat**: every function,
struct and enum in `util.vx` becomes visible unqualified, and a name defined twice is an error
that points at both definitions. Cycles are reported.

## Standard library

Global functions: `print(x)`, `eprint(x)`, `panic(msg)`, `assert(cond)`, `assert_eq(a, b)`, `exit(code)`.

Namespaces: `math` (`pi`, `e`, `sqrt`, `sin`, `cos`, `tan`, `atan`, `atan2`, `exp`, `ln`, `log10`, `pow`,
`hypot`, `gcd`, `INT_MAX`, `INT_MIN`), `io` (`read_file(path) -> Result[Str, Str]`,
`write_file`, `read_line() -> Option[Str]`) and `env` (`args() -> List[Str]`, `get(name) -> Option[Str]`).

Methods by type:

- **Int**: `abs pow min max signum to_float to_str to_char`
- **Float**: `abs floor ceil round sqrt min max is_nan to_int to_str`
- **Str**: `len is_empty upper lower trim contains starts_with ends_with replace split lines chars repeat
  to_int to_float slice char_at index_of code`
- **List[T]**: `len is_empty push pop insert remove_at clear extend reverse sort sorted get first last
  contains index_of slice concat copy map filter fold any all find for_each enumerate zip sum sum_float join`
- **Map[K, V]**: `len is_empty get insert remove clear contains_key keys values entries`
- **Option / Result**: `is_some is_none unwrap expect unwrap_or map and_then ok_or` /
  `is_ok is_err unwrap expect unwrap_err unwrap_or ok map map_err and_then`
- **Range**: `to_list len contains`

## Errors and diagnostics

Compile errors show `file:line:col`, the source line with a caret, a label, and often a
suggestion. Several errors are reported in one run:

```text
error[undefined-name]: cannot find value `totl` in this scope
 --> demo.vx:3:11
  |
3 |     print(totl);
  |           ^^^^ not found in this scope
  = help: did you mean `total`?
```

Runtime errors carry a stack trace mapped to source lines. **Integer arithmetic is checked**:
overflow, division by zero, out-of-range indexes, `unwrap` of `None` and failed assertions
all stop the program with an error rather than wrapping or continuing:

```text
runtime error: integer overflow in `*`
 --> fact.vx:2:28
  |
2 |     if n <= 1 { 1 } else { n * fact(n - 1) }
  |                            ^^^^^^^^^^^^^^^
stack trace (most recent call first):
  in fact at fact.vx:2:28
  in main at fact.vx:6:11
```

Exit codes: `0` success, `1` compile error, `2` runtime error, or the value `main` returns / `exit(n)`.

## The REPL

```text
$ vex repl
vex> let x = 5;
vex> x * 2
10 : Int
vex> fn sq(n: Int) -> Int { n * n }
vex> sq(x)
25 : Int
vex> :type [1, 2].map(|v| v.to_float())
List[Float]
```

Definitions persist. Unfinished input (an open `{` or string) continues on the next line.
`:type EXPR`, `:reset`, `:help` and `:quit` are available. A `let` that fails to compile
leaves the session unchanged.
