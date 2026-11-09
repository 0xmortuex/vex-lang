# Blockers

Status on 2026-11-10 at commit `cbf424b`. Each entry names its source.

Nothing blocks work on the language, the embedding ABI or the replacement backend. One release step is blocked:
publishing to PyPI under the current name.

## Blocked

### Publishing to PyPI as `vex-lang`

`pyproject.toml` has `name = "vex-lang"`, and that name on PyPI belongs to an unrelated project, so this repository
cannot publish under it until it picks another name. Install from the repository instead.

Source: `ROADMAP.md` gap 6, `CHANGELOG.md`. Rechecked on 2026-11-10 against `https://pypi.org/pypi/vex-lang/json`.

## Decided and not started

### The replacement backend

`DECISIONS.md` D3 replaces the VM with a backend that lowers the checked AST to Python `ast` nodes and runs them with
`compile()`. No part of it exists: `src/vex` has no such module and the VM is still the only backend. Nothing blocks
it. `ROADMAP.md` ranks it 10th, after the ABI, which does not depend on it, and the ABI is merged (`docs/ffi.md`).

| Measure | Figure | Source |
|---|---|---|
| Today, whole process | 16 to 33x slower than CPython | `BENCHMARKS.md` |
| Today, CPython startup removed | 26 to 81x slower than CPython | `BENCHMARKS.md` |
| Target | `fib(30)` within 5x of CPython, with overflow checks and the budget on | `DECISIONS.md` D3 |
| Chosen design, hand-written shape only | 2.6x, or 4.1x with the budget | `DECISIONS.md` D3 |
| Stop condition | If the real backend cannot reach 10x CPython, stop and revisit D3 before removing the VM | `DECISIONS.md` D3 |
| Estimated size | 30 to 50 hours | `DECISIONS.md` D3 |

`BENCHMARKS.md` says the machine was shared during measurement, so read its ratios only to about 20 to 30%.

The backend raises `requires-python` to 3.11 (`DECISIONS.md` D3). `pyproject.toml` still has `>=3.10`, and CI still
tests 3.10.

## Known gaps, not blockers

| Gap | Status | Source |
|---|---|---|
| Exponential compile time on small inputs | Open. `DECISIONS.md` D7 lists it among the fixes an embedded script needs. Doubled nested tuples under `vex check` on 2026-11-10: 15 levels 0.6 s, 18 levels 2.9 s, whole process. The or-pattern case was not re-measured | `ROADMAP.md` gap 7, command run |
| Super-linear time after parsing on large inputs | Open. 12,500 duplicate `match` arms took over 30 s; a 10,000-variant enum took 9.6 s | `ROADMAP.md` gap 16 |
| `==` on values that share substructure | Takes exponential time, with no bound | `ROADMAP.md` gap 3 |
| Traits | Missing. Generic code is limited to `Eq`, `Ord` and `Hash` bounds. Ranked last in the roadmap | `README.md`, `ROADMAP.md` gap 8 |
| Namespaced imports | Missing. Imports are flat. The package manager was dropped from the roadmap | `README.md`, `ROADMAP.md` gap 9 |
| `vex fmt`, `vex lsp`, `vex test` | Missing. `src/vex/cli.py` defines only `run`, `check`, `disasm` and `repl` | `ROADMAP.md` gaps 10 to 12, `src/vex/cli.py` |
| Lint warnings | Missing. `unreachable-pattern` is the only warning in the checker | `ROADMAP.md` gap 15, `src/vex/checker.py` |
| Standard library | No JSON or time helpers in `src/vex/stdlib.py` | `ROADMAP.md` gap 14 |
| Types across the ABI | Lists, maps, structs, enums and closures do not cross in version 1 | `DECISIONS.md` D6 |
| Release | 0.1.0 is unreleased | `CHANGELOG.md` |
