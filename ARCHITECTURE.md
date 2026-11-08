# Vex architecture

Audited at commit `5a7302c` on 2026-11-08 (Windows 11, CPython 3.13.12 via uv). Every number here
came from a command run on that commit or a file read at it. Ranked gaps are in [ROADMAP.md](ROADMAP.md).

Vex is a statically typed language implemented in pure Python (`requires-python >= 3.10`, zero runtime
dependencies, about 5,000 lines in `src/vex/`). Source files use `.vx`. Programs compile to in-memory
bytecode and run on a stack VM that is itself written in Python.

## Pipeline

```text
.vx text
  │ lexer.py        tokens; a string token records interpolation regions as (start, end) offsets
  │ parser.py       AST with spans (recursive descent + precedence climbing, panic-mode recovery)
  │ api.Loader      parses the root and its `import`s, dependencies first; detects cycles
  │ resolver.py     declarations, scopes, closure captures, did-you-mean; fills symbols on the AST
  │ checker.py      unification inference, bounds, mutability; exhaust.py checks patterns
  │ compiler.py     typed AST -> Code objects; picks ADD_I / ADD_F / ADD_S from inferred types
  ▼ vm.py           one dispatch loop; types are erased, values are Python objects
```

`src/vex/api.py` drives it: `_compile()` runs the stages above and calls `sink.raise_if_errors()` after
loading and again after checking, so the compiler only ever sees programs that passed the checker.
There is no optimiser pass and no on-disk bytecode format. Instruction fusion happens during compilation.

## Modules

| File | Lines (`wc`-style) | Role |
|------|------:|------|
| `src/vex/lexer.py` | 240 | `Lexer`, `Token`, keywords, nested block comments, numeric literals, string escapes, `_interp_end` |
| `src/vex/parser.py` | 845 | `Parser`, `parse_source`; `MAX_DEPTH = 150` nesting cap; `no_struct` flag for conditions; `repl` mode |
| `src/vex/syntax.py` | 250 | AST dataclasses (`Expr`, `Pat`, `TypeExpr`, `Item` families); later stages annotate nodes in place |
| `src/vex/resolver.py` | 687 | `Resolver`, `make_env`, `FnCtx`; declares types then functions then resolves bodies; capture analysis |
| `src/vex/symbols.py` | 120 | `Env` plus `FnSym`, `LocalSym`, `GlobalVar`, `StructInfo`, `EnumInfo`, `VariantInfo`, `Builtin` |
| `src/vex/vtypes.py` | 182 | `TCon`, `TTuple`, `TFn`, `TVar`, `TParam`, `TNever`, `TError`; `unify`, `zonk`, `subst`, `walk` |
| `src/vex/checker.py` | 967 | `Checker`, `satisfies` (Eq / Ord / Hash), deferred operator and bound checks, on-demand body checking |
| `src/vex/exhaust.py` | 166 | Maranget-style matrix: `missing_pattern` (witness) and `arm_reachable` |
| `src/vex/compiler.py` | 603 | `Compiler`, `FnCompiler`, pattern compilation (`cpat`), loops, fused ops |
| `src/vex/bytecode.py` | 88 | 67 opcodes (`NUM_OPS`), `Code`, disassembler |
| `src/vex/vm.py` | 474 | `VM.execute`, `VM.call` (re-entry for builtins), `run_function`, `make_trace`, `MAX_DEPTH = 100_000` frames |
| `src/vex/runtime.py` | 111 | `Adt`, `Cell`, `Closure`, `BuiltinFn`, `VexPanic`, `VexExit`, `display` |
| `src/vex/stdlib.py` | 408 | Builtin signatures (parsed from Vex type syntax) with Python implementations |
| `src/vex/diagnostics.py` | 176 | `Source`, `Span`, `Diagnostic`, `DiagSink` (cap 50, dedupes), `VexError`, `VexRuntimeError`, `suggest` |
| `src/vex/api.py` | 127 | `Loader`, `compile_source`, `compile_text`, `compile_file`, `check_source`, `Program` |
| `src/vex/cli.py` | 134 | `vex run / check / disasm / repl`; big-stack thread; exit codes |
| `src/vex/repl.py` | 135 | `Session` with persistent `Env`, `VM`, `Compiler`; rollback on compile error; `:type`, `:reset`, `:help`, `:quit` |

## Data flow and key representations

- **Spans.** Each token and AST node holds `Span(source, start, end)`. Each bytecode instruction has a
  parallel `Code.spans` entry, which is how runtime errors reach `file:line:col`.
- **Diagnostics.** Stages report to a `DiagSink` and continue. `VexError` carries every collected
  `Diagnostic` (code, message, span, label, help, notes, secondary spans).
- **Types.** `TVar` is a mutable union-find cell solved in place. `TParam` is rigid inside generic bodies.
  `TError` poisons later checks to avoid cascades. `TNever` conforms to anything in `join` and `unify`.
- **Checking order.** Function bodies are checked on demand (`ensure_checked`), so a call can see a
  callee's inferred return type. At function end, `finish()` runs deferred operator and bound checks and
  reports `cannot-infer`.
- **Runtime values.** `Int` is a Python `int` range-checked by each arithmetic opcode; `Float`, `Bool`,
  `Str` are Python scalars; `Unit` is `None`; `List` / `Map` / tuple / range are `list` / `dict` / `tuple` /
  `range`; structs and enums are `Adt(info, tag, fields)`.
- **Calls.** `CALL_FN` references the callee's `Code` directly. Vex-to-Vex calls use an explicit frame list,
  not Python recursion. Builtins that call back (`map`, `filter`, ...) re-enter `execute` through `VM.call`,
  so that path does consume Python stack.
- **Operand stacks are per frame.** `for` keeps its iterator on the operand stack, and expression
  temporaries live on the same stack. `break` / `continue` jump without unwinding temporaries (see
  ROADMAP gap 1).
- **Closures.** Immutable captures are copied. A `let mut` captured by a closure lives in a `Cell` from its
  declaration (`LocalSym.boxed`).

## Language conventions

- Parameter, struct-field and variant-payload types are always written; return types and locals are inferred.
- `let` and lambdas are monomorphic; only declared type parameters are polymorphic.
- Bounds are a closed set (`Eq`, `Ord`, `Hash`), defined in `resolver.KNOWN_BOUNDS` and `checker.satisfies`.
- Aggregates are references; mutation needs a mutable root variable.
- `Int` overflow is a runtime error, never wraparound.
- Imports are flat: `import x;` loads `x.vx` next to the importing file and merges its names.

## Code conventions

- Diagnostic codes are kebab-case strings, for example `non-exhaustive-match`. Tests assert on the code and
  exact position.
- Long reporting calls carry `# fmt: skip`. Ruff line length is 110; `mypy --strict` covers `src` and `tests`.
- Builtins are registered at import time with `glob` / `method` / `ns` in `stdlib.py`, using signatures in
  Vex's own type syntax. `mutates=True` makes the checker require a mutable root; `needs_vm=True` passes the VM.
- CLI exit codes: `0` ok, `1` compile error, `2` runtime error, `70` internal error, otherwise the value from
  `main` / `exit`.

## Build and test

```text
uv sync
uv run pytest -q                       # measured: 1773 passed in 18.14 s
uv run ruff check src tests benchmarks # measured: all checks passed
uv run mypy --strict src tests         # measured: no issues in 33 source files
uv run vex run examples/hello.vx
uv run python benchmarks/run.py
```

There is nothing to compile: `hatchling` packages `src/vex`, and the entry point is `vex.cli:main`.

| Test file | Collected | Covers |
|-----------|----------:|--------|
| `tests/test_fuzz.py` | 941 | Seeded token soup (250), mutations (300), identifier swaps (200), arithmetic property (150), list property (40), seed check (1) |
| `tests/test_language.py`, `test_language_more.py` | 260 | Behaviour |
| `tests/test_error_corpus.py` + `tests/errors/*.vx` | 147 | Each file's `// expect: <code> <line>:<col>` header |
| `tests/test_parser.py`, `test_lexer.py` | 174 | Front end |
| `tests/test_examples_and_cli.py` | 48 | Golden `.out` files, CLI, disassembly |
| `tests/test_repl.py` | 47 | REPL sessions |
| `tests/test_exhaustiveness.py` | 44 | Witnesses and reachability |
| `tests/test_runtime_errors.py` | 41 | Panics and traces |
| `tests/test_diagnostics.py` | 38 | Rendering and help text |
| `tests/test_bytecode.py` | 19 | Instruction selection |
| `tests/test_docs.py` | 14 | Every ` ```vex ` block in `docs/*.md` and `README.md` compiles and runs |

CI (`.github/workflows/ci.yml`) runs ruff, mypy, pytest, every example and the benchmarks on six
OS / Python combinations.

## Adding things

- **Builtin:** add a Python function and one `glob` / `method` / `ns` call in `stdlib.py`, then a test.
- **Diagnostic:** report through the sink with a new code, then add a `tests/errors/<name>.vx` file with an
  `// expect:` header. `tools/gen_error_corpus.py` holds the generator for that corpus.
- **Syntax:** change `lexer.py` / `parser.py` / `syntax.py`, then every walking stage (`resolver.rx`,
  `checker.infer_inner`, `compiler.cx_inner`), then `docs/grammar.md`. Nothing checks the grammar document
  against the parser automatically.
- **Opcode:** add to both the tuple and `NAMES` in `bytecode.py` (an `assert` checks they match), then a
  branch in `VM.execute`. Hot opcodes are placed first because dispatch is a linear `if` / `elif` chain.
