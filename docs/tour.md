# A tour of Vex

Vex is a small, statically checked language for scripts that a program embeds: rules, hooks and
config logic that users change without a compiler. A script can do only what its host grants and
fails with a source-located error instead of taking the host down. You write types where they
matter (function parameters, struct fields) and the compiler infers the rest.

`tests/test_tour.py` runs every block on this page and compares what it prints with the block
below it. To try one yourself, save it as `tour.vx` and run `vex run tour.vx`.

- [Hello](#hello) · [Values and types](#values-and-types) · [Variables](#variables-and-mutability)
- [Functions](#functions) · [Control flow](#control-flow) · [Strings](#strings)
- [Lists, maps, tuples](#lists-maps-and-tuples) · [Structs and methods](#structs-and-methods)
- [Enums and match](#enums-and-match) · [Option, Result and `?`](#option-result-and-)
- [Closures](#closures-and-first-class-functions) · [Generics](#generics) · [Modules](#modules)
- [Standard library](#standard-library) · [Errors](#errors-and-diagnostics) · [The REPL](#the-repl)
- [Embedding Vex](#embedding-vex)

## Hello

```vex
fn main() {
    print("Hello, Vex!");
}
```

```output
Hello, Vex!
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
| `Map[K, V]` | `#{"a": 1}` | insertion ordered; keys: `Int`, `Str`, `Bool`, `Unit` or tuples of those |
| tuples | `(1, "a", true)` | fixed size, immutable; fields are `t.0`, `t.1` |
| functions | `fn(Int) -> Int` | first-class |
| `Option[T]` | `Some(1)`, `None` | built in |
| `Result[T, E]` | `Ok(1)`, `Err("bad")` | built in |

There is **no implicit conversion**. Convert explicitly with `n.to_float()`, `x.to_int()`
(truncates) or `n.to_str()`:

```vex-error
fn main() {
    print(1 + 2.0);
}
```

```output
error[type-mismatch]: mismatched types for `+`
 --> tour.vx:2:11
  |
2 |     print(1 + 2.0);
  |           ^^^^^^^ left is `Int`, right is `Float`
  = help: Vex never mixes Int and Float; convert with `.to_float()` or `.to_int()`

aborting: 1 error
```

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

```output
vex! 1 1 two []
```

Mutating methods (`push`, `insert`, ...), field assignment and index assignment all
require the variable to be declared `let mut`. The compiler tells you exactly which one:

```vex-error
fn main() {
    let xs = [1];
    xs.push(2);
}
```

```output
error[immutable-binding]: cannot call `push` on `xs`: it is not declared `mut`
 --> tour.vx:3:8
  |
3 |     xs.push(2);
  |        ^^^^ `xs` is immutable
 ::: tour.vx:2:9
  |
2 |     let xs = [1];
  |         -- declared here
  = help: declare it with `let mut xs`

aborting: 1 error
```

**Aggregates are references.** `let b = a;` for a list, map or struct makes `b` another name
for the same object (like Python). Because `b` is immutable you cannot mutate through it,
but if `a` is `let mut` you will see `a`'s changes through `b`. Use `.copy()` for a copy.

```vex
fn main() {
    let mut a = [1];
    let b = a;
    let c = a.copy();
    a.push(2);
    print("{b} {c}");
}
```

```output
[1, 2] [1]
```

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

```output
5
49
21
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

```output
odd
1
3
0
1
2
1
2
3
0a
1b
2c
```

`break` and `continue` work in `while` and `for`. `for` iterates ranges (`a..b`, `a..=b`)
and lists. To iterate a map use `m.entries()`, `m.keys()` or `m.values()`.

`break`, `continue`, `return` and `panic` have type `Never`, so they can stand in any expression
position. The values already computed in that expression are discarded:

```vex
fn main() {
    let mut total = 0;
    for n in [1, -2, 3, 99, 5] {
        total += n * if n < 0 { continue } else if n == 99 { break } else { 2 };
    }
    print(total);
}
```

```output
8
```

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

```output
hello, world! 2 + 2 = 4
a literal brace: { and }
tab:	 quote:" backslash:\ unicode:😀
i̇stanbul
a|b|c
```

Because `{` starts an interpolation, write `\{` for a literal opening brace (a lone `}` needs
no escape). Strings can span lines. All source files are UTF-8.

## Lists, maps and tuples

```vex
fn main() {
    let mut xs = [3, 1, 2];
    xs.push(4);
    xs.sort();
    print(xs);
    print(xs.map(|x| x * x).filter(|x| x > 4));
    print(xs.fold(0, |acc, x| acc + x));

    let mut ages = #{"ada": 36};
    ages.insert("alan", 41);
    print(ages.get("ada"));
    print(ages["alan"]);                        // a runtime error if the key is missing

    let t = (1, "x");
    print(t.1);
}
```

```output
[1, 2, 3, 4]
[9, 16]
10
Some(36)
41
x
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
    print(acct);
}
```

```output
ada: 50
Account { owner: "ada", balance: 50 }
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

fn main() {
    print([Shape.Circle(1.0), Shape.Rect(2.0, 3.5), Shape.Empty].map(|s| area(s)));
}
```

```output
[3.14159, 7.0, 0.0]
```

`match` must be **exhaustive**. Forget a case and the compiler names the missing pattern:

```vex-error
enum Shape { Circle(Float), Empty }

fn area(s: Shape) -> Float {
    match s {
        Shape.Circle(r) => r,
    }
}

fn main() { print(area(Shape.Empty)); }
```

```output
error[non-exhaustive-match]: non-exhaustive `match`
 --> tour.vx:4:11
  |
4 |     match s {
  |           ^ pattern `Shape.Empty` is not covered
  = help: add an arm for it, or a catch-all arm `_ => ...`

aborting: 1 error
```

Patterns: `_`, variables, literals (`1`, `"a"`, `true`, `-1`), tuples `(a, b)`, variants
`Shape.Circle(r)` (or just `Circle(r)` when the name is unique), structs `P { x, y: 0, .. }`,
or-patterns `1 | 2`, and guards `x if x > 0`. Unreachable arms are reported as warnings.

Enums can be generic and recursive:

```vex
enum Tree[T] { Leaf, Node(Tree[T], T, Tree[T]) }

fn depth[T](t: Tree[T]) -> Int {
    match t {
        Tree.Leaf => 0,
        Tree.Node(l, _, r) => 1 + depth(l).max(depth(r)),
    }
}

fn main() {
    let leaf: Tree[Int] = Tree.Leaf;
    print(depth(Tree.Node(leaf, 1, Tree.Node(leaf, 2, leaf))));
}
```

```output
2
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
    print(parse_pair("1", "2"));
    print(parse_pair("1", "zz"));
}
```

```output
Ok(3)
Err("not a number: zz")
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
    print(next());
    let add = |a: Int, b: Int| a + b;       // annotate lambda parameters when needed
    print([1, 2, 3].map(|x| add(x, 10)));
}
```

```output
2
[11, 12, 13]
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

fn main() {
    print(largest([3, 9, 2]));
    print(largest(["pear", "zebra"]));
    let mut s = Stack.new();
    s.push(1.5);
    print(s.items);
}
```

```output
Some(9)
Some("zebra")
[1.5]
```

Bounds: `Eq` (supports `==`), `Ord` (supports `<`, also implies `Eq`) and `Hash` (usable as a map
key). `Stack.new()` infers `T` from later use.

## Modules

`import util;` loads `util.vx` from the same directory.

```vex-file
// util.vx
fn double(n: Int) -> Int { n * 2 }
```

```vex-import
import util;

fn main() {
    print(double(21));
}
```

```output
42
```

Imports are **flat**: every function, struct and enum in `util.vx` becomes visible unqualified,
and a name defined twice is an error that points at both definitions. Cycles are reported.

## Standard library

Global functions: `print(x)`, `eprint(x)`, `panic(msg)`, `assert(cond)`, `assert_eq(a, b)`, `exit(code)`.

Namespaces: `math` (`pi`, `e`, `tau`, `sqrt`, `sin`, `cos`, `tan`, `atan`, `atan2`, `exp`, `ln`, `log10`, `pow`,
`hypot`, `gcd`, `INT_MAX`, `INT_MIN`), `io` (`read_file(path) -> Result[Str, Str]`,
`write_file`, `read_line() -> Option[Str]`) and `env` (`args() -> List[Str]`, `get(name) -> Option[Str]`).

Methods by type:

- **Int**: `abs pow min max signum to_float to_str to_char`
- **Float**: `abs floor ceil round sqrt min max is_nan to_int to_str`
- **Bool**: `to_str`
- **Str**: `len is_empty upper lower trim contains starts_with ends_with replace split lines chars repeat
  to_int to_float to_str slice char_at index_of code`
- **List[T]**: `len is_empty push pop insert remove_at clear extend reverse sort sorted get first last
  contains index_of slice concat copy map filter fold any all find for_each enumerate zip sum sum_float join`
- **Map[K, V]**: `len is_empty get insert remove clear contains_key keys values entries`
- **Option / Result**: `is_some is_none unwrap expect unwrap_or map and_then ok_or` /
  `is_ok is_err unwrap expect unwrap_err unwrap_or ok map map_err and_then`
- **Range**: `to_list len contains`

One operation can build a `Str` of at most 50,000,000 characters or a `List` of at most
10,000,000 elements. Past that, it is a runtime error.

## Errors and diagnostics

Compile errors show `file:line:col`, the source line with a caret, a label, and often a
suggestion. Several errors are reported in one run:

```vex-error
fn main() {
    let total = 3;
    print(totl);
    print(totl + 1);
    let n: Int = "three";
}
```

```output
error[undefined-name]: cannot find value `totl` in this scope
 --> tour.vx:3:11
  |
3 |     print(totl);
  |           ^^^^ not found in this scope
  = help: did you mean `total`?

error[undefined-name]: cannot find value `totl` in this scope
 --> tour.vx:4:11
  |
4 |     print(totl + 1);
  |           ^^^^ not found in this scope
  = help: did you mean `total`?

error[type-mismatch]: mismatched types
 --> tour.vx:5:18
  |
5 |     let n: Int = "three";
  |                  ^^^^^^^ expected `Int`, found `Str`
 ::: tour.vx:5:12
  |
5 |     let n: Int = "three";
  |            --- declared here

aborting: 3 errors
```

Runtime errors carry a stack trace mapped to source lines. **Integer arithmetic is checked**:
overflow, division by zero, out-of-range indexes, `unwrap` of `None` and failed assertions
all stop the program with an error rather than wrapping or continuing:

```vex-error
fn fact(n: Int) -> Int {
    if n <= 1 { 1 } else { n * fact(n - 1) }
}

fn main() {
    print(fact(25));
}
```

```output
runtime error: integer overflow in `*`
 --> tour.vx:2:28
  |
2 |     if n <= 1 { 1 } else { n * fact(n - 1) }
  |                            ^^^^^^^^^^^^^^^
stack trace (most recent call first):
  in fact at tour.vx:2:28
  in fact at tour.vx:2:32
  in fact at tour.vx:2:32
  in fact at tour.vx:2:32
  in fact at tour.vx:2:32
  in main at tour.vx:6:11
```

A value that contains itself cannot be printed or compared, and doing so is a runtime error too,
not a hang:

```vex-error
struct Node { value: Int, next: Option[Node] }

fn main() {
    let mut a = Node { value: 1, next: None };
    a.next = Some(a);
    print(a);
}
```

```output
runtime error: cannot print a value that contains itself
 --> tour.vx:6:5
  |
6 |     print(a);
  |     ^^^^^^^^
stack trace (most recent call first):
  in main at tour.vx:6:5
```

Exit codes: `0` success, `1` compile error, `2` runtime error, or the value `main` returns / `exit(n)`.

## The REPL

```repl
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

## Embedding Vex

This is what Vex is for. A host program loads a script, gives it the functions it may call under
`host.`, and calls into it. Only `Unit`, `Int`, `Float`, `Bool` and `Str` cross the boundary.

```python
from vex.host import Host

RULES = """
fn price(total: Int, tier: Str) -> Int {
    let rate = match tier { "gold" => 10, "silver" => 5, _ => 0 };
    total - total * rate / 100 - host.loyalty(total)
}
"""

host = Host()
host.bind("loyalty", "(Int) -> Int", lambda total: total // 1000)
host.load("rules.vx", RULES)
print(host.call("price", [2500, "gold"]))
print(host.call("price", [2500, "bronze"]))
```

```output
2248
2498
```

A `Host` starts with no capabilities, so even `print` has to be granted. Using a name that was
not granted is a compile error. A runtime error or a spent budget stops the script, and the host
carries on and can keep calling it:

```python
from vex.diagnostics import VexError, VexRuntimeError, render_all
from vex.host import CAP_PRINT, Host, VexBudgetError

denied = Host()
try:
    denied.load("a.vx", 'fn go() { print("hi"); }')
except VexError as e:
    print(render_all(e.diagnostics))

allowed = Host(CAP_PRINT)
allowed.load("b.vx", 'fn go() { print("hi"); }')
allowed.call("go", [])

script = Host()
script.budget(10_000)
script.load("c.vx", """
fn spin() -> Int { while true { } 0 }
fn half(n: Int) -> Int { 100 / n }
""")
try:
    script.call("spin", [])
except VexBudgetError as e:
    print(e.render())
try:
    script.call("half", [0])
except VexRuntimeError as e:
    print(e.render())
print(script.call("half", [4]))
```

```output
error[capability-denied]: `print` is not available to this script
 --> a.vx:1:11
  |
1 | fn go() { print("hi"); }
  |           ^^^^^ not granted
  = help: the host did not grant the `print` capability (bit 1)
hi
runtime error: execution budget exhausted
 --> c.vx:2:20
  |
2 | fn spin() -> Int { while true { } 0 }
  |                    ^^^^^^^^^^^^^^
stack trace (most recent call first):
  in spin at c.vx:2:20
runtime error: division by zero
 --> c.vx:3:26
  |
3 | fn half(n: Int) -> Int { 100 / n }
  |                          ^^^^^^^
stack trace (most recent call first):
  in half at c.vx:3:26
25
```

The budget limits work, not memory, and Vex does not claim to contain hostile code. Native
programs use the same model through a C-compatible table of function pointers.
[docs/ffi.md](ffi.md) is the normative reference, with capabilities, budget charges, the ABI and
the Mort bindings. [`examples/embed/`](../examples/embed/) is a working Mort host.
