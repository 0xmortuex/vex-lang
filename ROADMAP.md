# Vex roadmap and audit

Audit of commit `5a7302c` on 2026-11-08: Windows 11, CPython 3.13.12 (uv). Each claim comes from a command
run or a file read during the audit. Hours are rough estimates, not measurements.

## Exit criteria status

| | Criterion | Status | Measured evidence |
|-|-----------|--------|-------------------|
| a | Written spec | **Partial** | `docs/grammar.md` (137 lines, 6,414 B), `docs/types.md` (151 lines, 8,756 B), `docs/vm.md` (100 lines, 5,474 B). 24 grammar cases checked against the parser: 18 agree, 6 disagree (see below). No evaluation semantics: searching `docs/*.md` for "evaluat", "left to right" or "order of" returns nothing. |
| b | 200+ passing tests | **Met** | `uv run pytest -q`: **1773 passed, 0 failed, 0 skipped** in 18.14 s. 941 are seeded fuzz or property cases, leaving 832 others. |
| c | Fuzzed parser that never crashes | **Not met** | A fuzzer exists (`tests/test_fuzz.py`, 941 pass in 1.19 s) and my 2,000-input ASCII run had 0 crashes. A 55 s run with Unicode digits and huge literals: **571 uncaught `ValueError`s in 40,976 inputs (1.4%)**, 15 distinct messages, all at `lexer.py:160` or `:162`, two root causes. No hangs in the parser (slowest input 44 ms). |
| d | 10+ example programs | **Not met (9)** | `examples/*.vx`: 9 files, all exit 0, 612 lines in total. Three are toys (`hello`, `fizzbuzz`, `fibonacci`). |
| e | Documented FFI usable from Mort | **Not met** | No FFI of any kind. `extern fn` is a syntax error. Searching for `ffi`, `extern`, `ctypes`, `dlopen`, `native` finds no implementation or documentation. |
| f | CI green | **Met** | `gh run list -R 0xmortuex/vex-lang -L 5`: run 42241263824 on `main`, `success`, all 6 jobs pass. That is the only run listed. |
| g | Docs: quickstart, tour, contributor guide, CHANGELOG | **Partial** | README Install / Use it: yes. `docs/tour.md` (382 lines): yes. Contributor guide: **no `CONTRIBUTING.md`**, only a short README "Development" section. `CHANGELOG.md` exists (25 lines, "0.1.0 - unreleased"); no git tags or GitHub releases. |

### Grammar spot-check (`docs/grammar.md` against `parser.py`)

Agreeing (18), including: `import`, tparam bounds, `impl`, `self` first only, `let` with pattern and type, `=`
required, range precedence and non-chaining, match guards, arm commas, no struct literal in conditions, map
literal, or-patterns, struct `..`, nested item rejection.

The parser accepts these although the grammar excludes them (6):

| Input | Grammar rule | Parser location |
|-------|--------------|-----------------|
| `fn f[](x: Int) {}` | `tparams` needs one or more | `parse_tparams`, line 179 |
| `\|a, b,\| a` | `lambda` has no trailing comma | `parse_lambda`, line 727 |
| `fn(Int,) -> Int` | `fn` type has no trailing comma | `parse_type`, line 313 |
| `List[]` | type arguments need one or more | `parse_type`, line 323 |
| `A.B.C(y)` in a pattern | `IDENT [ "." IDENT ]` | `parse_pat_atom`, line 810 |
| `1_0e3` | exponent form has no `_` | `lexer._number`, line 126 |

## Feature matrix

| Feature | Status | Evidence (snippets run through `vex run`) |
|---------|--------|------------------------------------------|
| Type inference | Works, local only | `fn sq(n: Int) { n * n }` with `[1, 2].map(\|x\| sq(x) + 1)` prints `[2, 5]`. `let xs = [];` gives `cannot-infer`. |
| Generics | Works | Generic struct and `fn id[T: Ord]` at two types print `2` and `b`. Only `Eq` / `Ord` / `Hash` bounds. `id[Int](1)` is rejected (`not-indexable`). |
| Enums, ADTs, pattern matching | Works | Guards and payloads run correctly; a missing arm gives `pattern E.B is not covered`. `tests/test_exhaustiveness.py`: 44 pass. |
| Traits | **Missing** | `trait Show { ... }` gives `expected-item`. README lists this as a limitation. |
| Modules / imports | Partial | `tests/modules/main_ok.vx` prints `3,6`. Flat, sibling files only, no `pub` or namespaces. |
| Result / Option | Works | `?` on both, printing `Some(42)`, `None`, `Err("neg")`. |
| Closures | Works | Shared `let mut` capture prints `2`. A closure cannot refer to itself (`undefined-name`). |
| Errors with line/column | Works | `error[type-mismatch] --> t.vx:2:18` with caret and secondary span. `tests/test_error_corpus.py`: 147 exact-position cases pass. Runtime stack traces map to source. |

## Tooling matrix

| Tool | Status | Evidence |
|------|--------|----------|
| Formatter | **Missing** | `vex fmt`: `invalid choice: 'fmt'` |
| Linter | **Missing** | No command. The compiler's only warning is `unreachable-pattern` (`checker.py:857`). |
| Package manager | **Missing** | `import` only looks beside the importing file (`api.py:69`). |
| LSP | **Missing** | `vex lsp`: `invalid choice`. No related file in the repo. |
| REPL | Works | Piped `let x = 2;`, `x * 21`, `:type x` printed `42 : Int` and `Int`. `tests/test_repl.py`: 47 pass. |
| Test runner | Partial | `assert` and `assert_eq` builtins exist. `vex test`: `invalid choice`. |
| Doc generator | **Missing** | No command, no doc-comment syntax (the lexer discards comments). |
| Syntax highlighting | **Missing** | `git ls-files` has no grammar or editor extension. |

## Standard library and performance

- **Contents:** 6 globals (`print`, `eprint`, `panic`, `assert`, `assert_eq`, `exit`); 101 methods (Int 8, Float 10,
  Bool 1, Str 20, List 31, Map 10, Option 8, Result 10, Range 3); `math` (17 members), `io` (`read_file`,
  `write_file`, `read_line`), `env` (`args`, `get`). No time, random, JSON, path / directory, process or network APIs.
- **Benchmarks:** `benchmarks/fib.vx`, `benchmarks/loops.vx`, `benchmarks/run.py`.
- **fib(30), best of 3:** **4.956 s** on the VM against 0.110 s in CPython (**44.9x slower**), about 543,000 calls/s.
  Compile time 1 ms. `benchmarks/run.py`: fib(27) 1.127 s (52.7x); loops 3.478 s (37.4x). The README lists 0.76 s and 2.8 s. Other
  agents were working on this machine during the audit, so these timings may run high.

## FFI, exactly what exists

**Native code: nothing.** Vex cannot call native code, and native code cannot call Vex. There is no `extern`,
ABI type, pointer type or C API.

**Python host: two undocumented routes, both verified by running them.**

- Python calls Vex: `VM(out).run_function(prog.env.fns["add"].code, [2, 3])` returned `5`.
- Vex calls Python: `stdlib.glob("host_add", "(Int, Int) -> Int", fn)` before compiling made `host_add(2, 3)`
  callable from Vex. This mutates a module-level table, is not a public API, and **return values are not
  range-checked**: a host function returning `2**70` printed `1180591620717411303424` as an `Int`.

**Relevance to Mort.** Mort's `extern fn` declares C-ABI symbols resolved at link time (Mort `README.md`). A searched
subset of Mort (`README.md`, `mortc.py`, the language spec) has no shared-library output or export syntax. So today
there is no direct path in either direction; Mort could only spawn `vex` as a subprocess. No C compiler (`gcc`, `cc`,
`clang`, `zig`) is on this machine's PATH, so FFI work needs CI or a toolchain install to test.

## Ranked gaps

### 1. `break` / `continue` inside an expression corrupt the operand stack

- **Missing:** Jumps do not unwind expression temporaries. `for` keeps its iterator on the same stack.
- **Why it matters:** A well-typed program gets the wrong behaviour with no diagnostic. This is the only silent
  miscompile found, so it comes first.
  - `for i in 0..3 { let x = 1 + if i == 1 { continue } else { 2 }; }` exits 70: `'int' object is not an iterator`.
  - With `break` in a nested `for`, the output runs `0 10 3`, `end 0`, `12 10 3`, `end 12`, ... and **never terminates**
    (timeout at 20 s), because the outer loop resumes the inner iterator.
- **Size:** 4-8 h. Track operand depth per loop and emit `POP`s before the jump, or reject these forms in the checker.
- **Depends on:** nothing.
- **Files:** `src/vex/compiler.py` (`Loop`, `cx_inner` Break / Continue, `cfor`, `cwhile`), `tests/test_language.py`.

### 2. Lexer crashes, and a fuzzer that cannot see them (criterion c)

- **Missing:** `_number` trusts `str.isdigit()`, which is true for `²` and other non-ASCII digits, and calls `int()` on
  decimal runs over Python's 4,300-digit limit. Both raise uncaught `ValueError`.
- **Why it matters:** `fn main() { print(2²); }` exits 70 with `internal compiler error`. `compile_text` raises a raw
  `ValueError` to embedders. `tests/test_fuzz.py` misses this because its vocabulary contains neither input.
- **Size:** 3-5 h, including hostile vocabulary in the seeded tests and a time-boxed fuzz job in CI.
- **Depends on:** nothing.
- **Files:** `src/vex/lexer.py` (`lex`, `_number`, lines 57, 126, 160, 162), `tests/test_fuzz.py`, `.github/workflows/ci.yml`.

### 3. Python exceptions and out-of-range values escape at runtime

- **Missing:** `VM.run_function` converts only `VexPanic` and `RecursionError`.
- **Why it matters:** Each of these was run through the CLI. The first six rows exit 70, an internal error, instead of a Vex runtime error.

  | Program | Result |
  |---------|--------|
  | `() < ()`, `[(), ()].sort()` | `TypeError`, though `docs/types.md` lists `Unit` as `Ord` |
  | `(1e308 * 10.0) % 2.0` | `ValueError` from `math.fmod`; `docs/types.md` only defines `%` by `0.0` as a runtime error |
  | `(math.INT_MIN..math.INT_MAX).len()` | `OverflowError` |
  | `"9".repeat(5000).to_int()` | `ValueError` (4,300-digit limit) |
  | `print(55296.to_char())`, `print("\u{D800}")` | `UnicodeEncodeError` on a lone surrogate |
  | `let mut x = panic("a"); x += 1;` | `AssertionError: compound op on ''` in the compiler |
  | `math.gcd(math.INT_MIN, 0)` | **No error:** prints `9223372036854775808`, breaking the 64-bit contract |

  Lesser issues: `print(1..=3)` displays `1..4`. `let x = panic("a"); print(x + 1);` reports a misleading `cannot-infer`
  on `print`. `S { f: g } == S { f: g }` is accepted although `docs/types.md` says `Eq` excludes types containing
  functions; `satisfies` only inspects type arguments. Deep recursion through `map` reports "while comparing or printing a value".
- **Size:** 8-12 h.
- **Depends on:** nothing.
- **Files:** `src/vex/vm.py`, `src/vex/stdlib.py`, `src/vex/runtime.py`, `src/vex/lexer.py` (`_escape`), `src/vex/compiler.py` (`carith`, `cbinary`), `src/vex/checker.py` (`satisfies`), `tests/test_runtime_errors.py`.

### 4. No FFI or host API (criterion e)

- **Missing:** Everything under [FFI](#ffi-exactly-what-exists).
- **Why it matters:** It is the blocker for Mort interop and for using Vex as an embedded language.
- **Size:** 8-12 h for a public, documented Python host API with range-checked conversions. 30-60 h for native interop,
  depending on the design: `extern` declarations through `ctypes` in the VM (Vex calls a Mort-built library), an embedding
  shim (Mort calls Vex, requiring libpython), or a code generator targeting C or Mort (far larger, 100 h or more).
- **Depends on:** the niche decision; shared-library output from Mort for the Vex-calls-Mort route; a C toolchain in CI; gap 3 for the value contract.
- **Files:** `src/vex/api.py`, `src/vex/stdlib.py`, `src/vex/parser.py`, `src/vex/resolver.py`, `src/vex/checker.py`, new `docs/ffi.md`.

### 5. Finish the spec (criterion a)

- **Missing:** Evaluation semantics (operand, argument and field order; pattern order; closure capture timing; panic behaviour),
  the 6 grammar drifts, a statement of which document is normative, and a check that keeps grammar and parser aligned.
- **Why it matters:** Without it, implementation behaviour is the only definition, and interop or a second backend has nothing to conform to.
- **Size:** 12-16 h.
- **Depends on:** gaps 1 and 3 settle behaviour worth specifying.
- **Files:** `docs/grammar.md`, `docs/types.md`, new `docs/semantics.md`, `src/vex/parser.py`, `src/vex/lexer.py`, `tests/`.

### 6. Close the small documentation and release items (criteria d, g)

- **Missing:** One more example, ideally a larger real program. `CONTRIBUTING.md`. A dated 0.1.0, a git tag and a release.
  A distribution name: **`vex-lang` on PyPI is already taken** by an unrelated project (`sohailcodes-ai/Vex-Lang`, v0.3.0), so this
  repo's `pyproject.toml` name cannot be published as is.
- **Why it matters:** The cheapest way to close two exit criteria, and `pip install vex-lang` today installs someone else's package.
- **Size:** 4-8 h.
- **Depends on:** nothing, though a release should follow gaps 1-3.
- **Files:** `examples/`, `tests/test_examples_and_cli.py`, new `CONTRIBUTING.md`, `CHANGELOG.md`, `pyproject.toml`, `README.md`.

### 7. Exponential compile time on small inputs

- **Missing:** Type walks ignore sharing, and the exhaustiveness search has no budget.
- **Why it matters:** A short source file can stall the compiler, and `zonk` rebuilds types without sharing, so memory grows too.
  - Doubled nested tuples (`let a1 = (a0, a0); ...`): 15 levels 0.76 s, 18 levels 6.89 s, 20 levels over 25 s.
  - One `(true|false, ...)` arm: 18 columns 0.86 s, 20 columns 2.81 s, 24 columns over 20 s.
- **Size:** 6-10 h: memoise `walk` / `zonk` by identity, cap or compress the or-pattern search, and report a diagnostic at the limit.
- **Depends on:** nothing.
- **Files:** `src/vex/vtypes.py`, `src/vex/checker.py` (`finish`), `src/vex/exhaust.py`.

### 8. Traits

- **Missing:** User-defined interfaces, user `Display`, operator overloading. Generic code is limited to three built-in bounds.
  A user `to_str` method is ignored by `print` (it prints `P { x: 1 }`).
- **Why it matters:** The largest expressiveness gap. It limits generic libraries, and stdlib growth depends on it.
- **Size:** 40-80 h.
- **Depends on:** gap 5 is advisable first.
- **Files:** `src/vex/parser.py`, `src/vex/syntax.py`, `src/vex/resolver.py`, `src/vex/checker.py`, `src/vex/compiler.py`, `src/vex/runtime.py`, `src/vex/stdlib.py`.

### 9. Real modules

- **Missing:** Namespaces, `pub`, paths beyond sibling files, a stdlib written as importable modules.
- **Why it matters:** All names share one global table, and duplicates are errors, so libraries collide. Prerequisite for a package manager.
- **Size:** 20-30 h; a package manager is another 30-60 h.
- **Depends on:** nothing for modules; the package manager depends on modules.
- **Files:** `src/vex/api.py` (`Loader`), `src/vex/resolver.py`, `src/vex/symbols.py`, `src/vex/parser.py`.

### 10. Editor support: LSP and syntax highlighting

- **Missing:** Both.
- **Why it matters:** Spans, diagnostic codes and `check_source` already exist, so a diagnostics-first LSP is cheap for the value it adds.
- **Size:** 4-6 h for a TextMate grammar; 20-40 h for an LSP with diagnostics, hover and go-to-definition.
- **Depends on:** nothing.
- **Files:** new `editors/`, new `src/vex/lsp.py`, `src/vex/cli.py`.

### 11. Formatter

- **Missing:** `vex fmt`. The AST has no comments or trivia, so it needs a lossless token stream.
- **Why it matters:** Standard for a language at this stage, and useful for generated code and examples.
- **Size:** 20-30 h.
- **Depends on:** nothing.
- **Files:** `src/vex/lexer.py`, new `src/vex/fmt.py`, `src/vex/cli.py`.

### 12. `vex test`

- **Missing:** Test discovery, reporting and a test syntax or naming convention.
- **Why it matters:** Vex programs currently have only `assert` and a manual `main`.
- **Size:** 8-12 h.
- **Depends on:** nothing.
- **Files:** `src/vex/cli.py`, `src/vex/api.py`, `docs/tour.md`.

### 13. VM performance

- **Missing:** A faster execution strategy. Dispatch is a linear `if` / `elif` chain in CPython, about 45x slower than CPython itself on fib(30).
- **Why it matters:** Rules out compute-heavy use. The README already says further gains need a different VM.
- **Size:** 10-20 h for table dispatch or closure compilation (gain unmeasured); 100 h or more for a native VM or compile-to-C.
- **Depends on:** the niche decision.
- **Files:** `src/vex/vm.py`, `src/vex/compiler.py`, `benchmarks/`.

### 14. Standard library breadth

- **Missing:** Time, random, JSON, path / directory, process, and any byte-level I/O. `io` handles only whole UTF-8 files and stdin lines.
- **Why it matters:** Limits real programs, including the larger examples in gap 6.
- **Size:** 15-30 h.
- **Depends on:** gaps 8 and 9 for a good design; simple additions can come sooner.
- **Files:** `src/vex/stdlib.py`, `docs/tour.md`.

### 15. Lint warnings

- **Missing:** Unused variables, unused imports, unused results. There is one warning today.
- **Why it matters:** Low-cost correctness help; the resolver already has the scope data.
- **Size:** 6-10 h.
- **Depends on:** nothing.
- **Files:** `src/vex/resolver.py`, `src/vex/checker.py`, `tests/errors/`.

## What Vex is distinctive at, compared with Mort (facts only)

Vex facts were measured here. Mort facts come from reading its `README.md`, `docs/language-specification.md`, `CLAUDE.md` and `mortc.py`; Mort was not run for this audit.

**Vex has, and those Mort documents do not show:**

- Closures capturing local state, including shared mutable capture. Mort has function values and callbacks; its spec has no lambda or closure expression.
- A REPL with persistent state and `:type`. `mortc.py` has no REPL command.
- String interpolation with full expressions, and a `?` operator for `Option` and `Result`.
- Exhaustiveness across nested patterns, tuples, bools, literals, or-patterns and guards, with a witness for the missing case. Mort's spec requires exhaustive enum matches, with positional payload bindings.
- A function return type is inferred when omitted (`fn sq(n: Int) { n * n }`). Mort's grammar also makes `-> type` optional; whether omission means inference there was not verified.
- Trapping integer overflow on every operation. Mort's fixed-width integers wrap modulo 2^N by specification.
- Source-mapped runtime stack traces and a bytecode disassembler, with no C compiler needed: zero dependencies beyond Python.
- Built-in growable `List`, `Map` and Unicode `Str` values managed by the host runtime.

**Mort has, and Vex does not:**

Native code through C, `extern fn` C interop, freestanding output and an OS kernel written in it, `resource` ownership, pointers, fixed-width integers, threads and sockets in its standard library, and `fmt`, `test`, `doc`, `lsp` and `fuzz` commands, plus a versioned normative spec with a conformance suite.

**Shared:** static types, enums with data, generics, Python implementation, tuples, structs and exhaustive enum matching.
