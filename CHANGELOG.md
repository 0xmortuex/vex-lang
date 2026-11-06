# Changelog

## 0.1.0 - unreleased

First version.

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
