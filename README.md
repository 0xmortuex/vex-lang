# Vex

**A friendly, statically typed language with type inference, enums with data, exhaustive
pattern matching, and its own bytecode VM.** Written in pure Python with zero runtime dependencies.

```vex
enum Shape {
    Circle(Float),
    Rect(Float, Float),
    Empty,
}

impl Shape {
    fn area(self) -> Float {
        match self {
            Shape.Circle(r) => 3.14159 * r * r,
            Shape.Rect(w, h) => w * h,
            Shape.Empty => 0.0,
        }
    }
}

fn largest[T: Ord](xs: List[T]) -> Option[T] {
    if xs.is_empty() { return None; }
    let mut best = xs[0];
    for x in xs {
        if x > best { best = x; }
    }
    Some(best)
}

fn main() {
    let shapes = [Shape.Circle(1.0), Shape.Rect(2.0, 3.5), Shape.Empty];
    let areas = shapes.map(|s| s.area());      // closure parameter types are inferred
    print("areas = {areas}");
    print("largest = {largest(areas)}");
    print("words: {largest(["pear", "apple", "zebra"])}");
}
```

```text
$ vex run shapes.vx
areas = [3.14159, 7.0, 0.0]
largest = Some(7.0)
words: Some("zebra")
```

Vex is the kind of language you can hold in your head: `let` / `let mut`, functions with
explicit parameter types and inferred results, structs, generics, closures, `Option` / `Result`
with `?`, and a `match` the compiler checks for you.

## Install

```text
uv tool install git+https://github.com/0xmortuex/vex-lang
vex --version
```

(Or `pip install git+https://github.com/0xmortuex/vex-lang`.) Python 3.10 or newer, no other
dependencies. From a checkout: `uv sync`, then `uv run vex run examples/hello.vx`.

## Use it

```text
vex run file.vx [args...]    compile and run (args are visible as env.args())
vex check file.vx...         type-check only; every error is reported
vex disasm file.vx           show the bytecode
vex repl                     interactive session with persistent definitions
```

Exit codes: `0` ok, `1` compile error, `2` runtime error, otherwise whatever `main` returns or `exit(n)` says.

## Diagnostics

Errors say where, show the line, and say what to do. Several are reported at once.

```text
error[undefined-name]: cannot find value `itmes` in this scope
 --> oops.vx:9:17
  |
9 |     print(total(itmes));
  |                 ^^^^^ not found in this scope
  = help: did you mean `items`?

error[type-mismatch]: mismatched types
  --> oops.vx:10:22
   |
10 |     let label: Str = total(items);
   |                      ^^^^^^^^^^^^ expected `Str`, found `Int`
  ::: oops.vx:10:16
   |
10 |     let label: Str = total(items);
   |                --- declared here
   = help: use string interpolation (`"{x}"`) or `.to_str()`
```

```text
error[non-exhaustive-match]: non-exhaustive `match`
 --> shape.vx:8:11
  |
8 |     match s {
  |           ^ pattern `Shape.Empty` is not covered
  = help: add an arm for it, or a catch-all arm `_ => ...`
```

Runtime errors have stack traces mapped back to source lines:

```text
runtime error: division by zero
 --> trace.vx:2:5
  |
2 |     10 / n
  |     ^^^^^^
stack trace (most recent call first):
  in inner at trace.vx:2:5
  in middle at trace.vx:5:5
  in main at trace.vx:8:11
```

## Features

- **Static types with inference.** `Int` `Float` `Bool` `Str` `Unit`, `List[T]`, `Map[K, V]`, tuples,
  function types, structs, enums. Unification-based inference: parameter types are written, return
  and local types are inferred. No implicit conversions.
- **Algebraic data types.** Enums with payloads (generic and recursive), struct and tuple patterns,
  or-patterns, guards, and **exhaustiveness checking with witnesses** plus unreachable-arm warnings.
- **Built-in `Option[T]` and `Result[T, E]`** and the `?` operator. There is no null.
- **Generics** on functions, structs, enums and `impl` blocks, with `Eq` / `Ord` / `Hash` bounds.
- **Closures and first-class functions**, shared mutable captures, `map` / `filter` / `fold` / `find` ...
- **Methods** via `impl` blocks (`self`, `mut self`, associated functions, `Self`).
- **Strings** with interpolation (`"x = {x + 1}"`), full Unicode (Turkish text, emoji), escapes.
- **Checked integers.** `Int` is 64-bit; overflow, division by zero and out-of-range indexes are
  runtime errors, never silent wraparound. See [docs/types.md](docs/types.md#integers-and-floats).
- **Modules** with `import name;` (flat), and a small standard library: `math`, `io.read_file`, `env.args`,
  string / list / map / option / result methods.
- **A real pipeline**: lexer, parser with error recovery, name resolution, type checker, bytecode
  compiler, stack VM, disassembler, REPL.

## Documentation

- [Language tour](docs/tour.md): everything, with runnable snippets
- [Grammar](docs/grammar.md): EBNF and operator precedence
- [Type rules](docs/types.md): inference, bounds, mutability, exhaustiveness, integer semantics
- [Bytecode and VM](docs/vm.md): instruction set, frames, closures, runtime errors
- [`examples/`](examples/): hello, fizzbuzz, fibonacci, linked list via enums, JSON-ish parser,
  bank account with structs and `Result`, generic stack, BFS maze solver, minimax tic-tac-toe
- Want to build one yourself? [vex-lang-the-hard-way](https://github.com/0xmortuex/vex-lang-the-hard-way)
  builds a minimal Vex from scratch in ten runnable modules.

## Performance (honest numbers)

The VM is an interpreter written in Python, so it is slow. `python benchmarks/run.py` on a Windows
x86-64 machine with CPython 3.13:

| benchmark | Vex VM | same algorithm in CPython | slowdown |
|-----------|-------:|--------------------------:|---------:|
| `fib(27)` (recursive, ~630k calls) | 0.80 s | 0.018 s | ~44x |
| 1M-iteration `while` loop + 600k nested `for` | 2.9 s | 0.08 s | ~35x |

Cheap optimisations that are in: calls go straight to the callee's code object, integer ops are
specialised by static type, local-vs-constant arithmetic and comparisons are fused, and hot
instructions are dispatched first. Anything more would be a different VM.

## Limitations (what Vex is not)

- **Slow** (above). Not for number crunching.
- **Imports are flat**: `import util;` makes everything in `util.vx` visible unqualified; there is no
  `pub`, no namespacing, and duplicate names are errors. `util.name` is not a thing.
- **No traits / interfaces.** Generic code is limited to the `Eq` / `Ord` / `Hash` bounds. No operator
  overloading, no user-defined `Display` (printing is structural).
- **Monomorphic `let` and lambdas.** Only declared type parameters are polymorphic. Closures cannot be
  recursive. Type arguments are never written at call sites.
- **References, not values.** Lists, maps and structs are shared by reference. A `let b = a` aliases.
  Mutation needs `let mut`, but an immutable alias can observe mutation done through another name.
- **Map keys** are limited to `Int`, `Str`, `Bool`, `Unit` and tuples of those. `Float` is not a key.
- **No `loop`**, no labelled breaks, no `break` with a value, no slices syntax, no list patterns, no
  `impl` for built-in types, no struct update syntax, no `fmt` command.
- **Strings** are sequences of code points (`"é".len() == 1`, but a combining accent counts separately).
  `{` in a string starts an interpolation; write `\{` for a literal brace.
- **Type inference is local.** A method call needs the receiver's type to be known at that point
  (`|x| x.len()` fails until `x` has a type; annotate it).
- **Very deep values**: comparing or printing a structure nested thousands of levels deep (a 5000-element
  linked list) can hit Python's recursion limits on some Python versions; that is reported as a runtime error, not a crash.
- **`stdin` / files**: only whole-file UTF-8 reads and writes, and line reads from stdin.
- Debugging aids stop at stack traces: no debugger, no profiler, no source maps for the REPL beyond `<repl:N>`.

## Development

```text
uv sync
uv run pytest                 # about 1600 tests
uv run ruff check src tests
uv run mypy --strict src tests
uv run python benchmarks/run.py
```

The type-error corpus lives in `tests/errors/`: each `.vx` file starts with
`// expect: <code> <line>:<col> [| text that must appear in the output]` and the test checks the
diagnostic code and exact position. See [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
