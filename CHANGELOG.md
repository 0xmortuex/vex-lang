# Changelog

## 0.1.0 - unreleased

First version. Nothing has been tagged or published yet. The PyPI name `vex-lang` belongs to an unrelated
project, so install from this repository.

### Embedding

- Per-instance Python host API (`vex.host.Host`): host functions bound under `host.*` with a signature
  string, capability bits that start closed, and an execution budget that defaults to 5,000,000 units. An
  ungranted name is a `capability-denied` compile error.
- Native embedding ABI, version 1 (`vex.embed` and the `VexApi` table), with status codes for compile,
  runtime, binding and budget failures, and no exception crossing the boundary. `docs/ffi.md` is the normative
  reference.
- Mort binding library (`embed/mort/vex.mx`) and an order-pricing demo in which one Mort executable runs
  swappable Vex rule scripts (`examples/embed/`).
- `vex.stop()` is refused from inside a running call, and `vex.mx` is safe to use after a stop.
- CI builds the demo with `ziglang` against a pinned Mort commit on Windows and Linux (CPython 3.13, x86-64), runs
  it and compares its output with `expected.out`. The job fails if any demo test was skipped.

### Budget and limits

- The budget charges function entry and loop back-edges, plus work that scales with data: every data-sized
  builtin (including the set-up of callback builtins, before the first callback), value construction per
  element, interpolation part by part, map keys, retained ranges, and strings crossing the host boundary.
- One operation can build a `Str` of at most 50,000,000 characters or a `List` of at most 10,000,000
  elements, in every builtin and in list and map literals.
- `tests/test_budget_audit.py` lists every builtin and fails if a new one is left out.
- The budget bounds work, not memory, and is not a security boundary.

### Fixed

- `VM.run_function` no longer changes the caller's argument list, so a list can be reused, and accepts a tuple.
- A program too large to compile reports `out-of-memory` reliably. The report could itself run out of memory because
  compile frames were still held, depending on the Python version and where the limit landed.
- `break` and `continue` inside an expression no longer corrupt the operand stack. They silently miscompiled
  or hung before, and now unwind held operands to the loop's depth. Arithmetic whose left operand has type
  `Never` also compiles.
- Self-containing values: printing or comparing a value that contains itself is a defined, located runtime
  error instead of a hang or a crash.
- Hostile input cannot crash the front end or runtime:
  - Source: non-ASCII digits, oversized integer and float literals, invalid unicode escapes, invalid UTF-8 and
    nesting overflow are positioned diagnostics.
  - Values: numeric and text conversions are total, lone surrogates stay out of strings, and `math.gcd` and
    `Range.len` keep to the 64-bit contract. `Unit` has a defined ordering.
  - Runtime: file and stream failures are Vex errors, invalid UTF-8 on standard input is reported, and stack
    and heap exhaustion are located at the failing instruction.
  - REPL: failures only the REPL can reach are survived, continuation is decided incrementally, and each input
    costs what it changes rather than the whole session.
  - Diagnostics are rendered within a bounded window of a long line.

### Tooling and documentation

- Fuzzing covers hostile inputs and has a time-boxed mode (`--fuzz-seconds`, `--fuzz-seed`).
- `DECISIONS.md` records Vex's niche, execution model and interop design. `ARCHITECTURE.md` and `ROADMAP.md`
  record the audit.
- Five new examples: a rules engine, a config validator, text templating, a CSV report and a state machine. A
  test now fails if any example is not run.
- Every code block in the language tour is compiled and run by a test, with its output asserted.
- `CONTRIBUTING.md`.

### Language and tools in this version

- Language: `let` / `let mut`; functions with explicit parameter types and inferred or declared
  return types; `Int` (checked 64-bit), `Float`, `Bool`, `Str`, `Unit`, `List[T]`, `Map[K, V]`,
  tuples, structs, enums with data, built-in `Option[T]` / `Result[T, E]` with the `?` operator,
  generics on functions, structs, enums and `impl` blocks (bounds `Eq`, `Ord`, `Hash`), closures with
  shared mutable captures, `if` / `match` as expressions, `while`, `for` over ranges and lists,
  string interpolation, methods via `impl`, flat `import`.
- Pattern matching: literals, tuples, variants, structs, or-patterns, guards, exhaustiveness checking
  with a witness for the missing case, unreachable-arm warnings.
- Pipeline: lexer, parser with panic-mode error recovery, name resolution (scopes, captures, did-you-mean),
  unification-based type inference, bytecode compiler, stack VM, disassembler.
- Diagnostics: `file:line:col`, source snippet with caret, labels, help text and suggestions, secondary
  spans, several errors per run; runtime errors with stack traces mapped to source lines.
- Standard library: `print` / `panic` / `assert`, `math`, `io.read_file` / `write_file` / `read_line`,
  `env.args` / `env.get`, and methods on Int, Float, Str, List, Map, Option, Result, Range.
- CLI: `vex run`, `vex check`, `vex disasm`, `vex repl`.
- Tests: type-error corpus with exact positions, behaviour tests, golden outputs for the examples,
  a fuzz suite asserting the compiler only ever produces Vex diagnostics, REPL and CLI tests,
  a property test of integer arithmetic against reference semantics.
- Not included: a formatter (`vex fmt`), traits, qualified imports. See the README limitations.
