# Contributing to Vex

Vex is the statically checked extension language that native programs embed
([DECISIONS.md](DECISIONS.md), D1). When you propose a feature, ask whether it is about policy or about
the machine. Machine features belong in the host and reach scripts as host functions (D2).

Read [ARCHITECTURE.md](ARCHITECTURE.md) for how the code is laid out and [ROADMAP.md](ROADMAP.md) for
what is planned.

## Setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.10 or newer.

```text
git clone https://github.com/0xmortuex/vex-lang
cd vex-lang
uv sync
uv run vex run examples/hello.vx
```

## Checks

Run all of these before you send a change. CI runs the same checks on Linux, Windows and macOS.

| Check | Command |
|-------|---------|
| Tests | `uv run pytest -q` |
| Lint | `uv run ruff check src tests benchmarks` |
| Types | `uv run mypy --strict src tests` |
| Benchmarks still run | `uv run python benchmarks/run.py` |

## Fuzzing

`uv run pytest -q` includes a short fixed-size fuzz run. For a longer campaign, set a time box and a seed:

```text
uv run pytest tests/test_fuzz.py --fuzz-seconds=60 --fuzz-seed=7
```

The same seed generates the same inputs, so a failure can be replayed. Fix the cause and add the
reproducer to a regular test. The rule is that hostile input produces a Vex diagnostic or a Vex runtime
error, never a Python exception or a hang.

## Commit style

- Write the subject in the imperative and say what changes for a user: "Report float literals too large
  for 64 bits". No `feat:`-style prefix.
- Use the body to say why, and what you measured if the change makes a claim.
- Make one change per commit, with its tests in the same commit.
- Never delete or weaken a test to make a change pass. If a test is wrong, say why in the commit message.

## Adding a builtin

Builtins live in `src/vex/stdlib.py`. A script can reach a builtin from inside a host, so every builtin
must keep to two rules:

- **Charge the budget** for work that scales with the data, before doing that work.
- **Cap what it builds.** A builtin that returns a `Str` or a `List` calls `_check_str` or `_check_list`
  with the final size before allocating it.

For example, a `pad_right` method, modelled on the existing `_repeat`:

```python
def _pad_right(vm: Any, s: str, width: int) -> str:
    size = max(width, len(s))
    _check_str(size)
    vm.charge(size)
    return s.ljust(width)


method("Str", "pad_right", "(Str, Int) -> Str", _pad_right, needs_vm=True)
```

The signature is in Vex's own type syntax. `needs_vm=True` passes the VM as the first argument, and
`mutates=True` makes the checker require a `let mut` receiver. If the work is proportional to the
arguments alone, wrap the function in `_by_input`.

Then:

1. **Add it to the budget audit**, in `tests/test_budget_audit.py`. `test_every_builtin_is_in_the_audit`
   fails until you do. Put a builtin whose cost scales in `SCALED`, with a setup and one loop iteration:

   ```python
   "Str.pad_right": (STRS, "s.pad_right(n);"),
   ```

   That entry fails if the builtin is not charged. Use `CONSTANT` only for a builtin that really does
   bounded work per call, with the reason.
2. **Test its behaviour**, including the error cases, in `tests/test_language.py` or a neighbouring file.
   `test_every_builtin_survives_hostile_arguments` picks up every builtin automatically.
3. **If it touches the outside world**, add it to `_CAPABILITY_OF` in `src/vex/host.py`. Anything in the
   `io` or `env` namespaces, or a new global, is unavailable to embedded scripts unless it is listed there.
   Do not add threads, sockets or processes; these are non-goals (D1).
4. **Document it** in the method list in `docs/tour.md`. If its charge is unusual, add it to the charged-work
   list in `docs/ffi.md`.

## Adding other things

- **A diagnostic:** report through the sink with a new kebab-case code, then add `tests/errors/<name>.vx`
  starting with `// expect: <code> <line>:<col>`.
- **An example:** add `examples/<name>.vx` and `examples/<name>.out` with its exact output.
  `test_every_example_is_run_by_the_suite` fails if a program is left where the suite will not run it.
- **A tour section:** every fence in `docs/tour.md` is run by `tests/test_tour.py`, which documents the
  fence tags. Runnable blocks are followed by an `output` block with exactly what they print.
- **Syntax:** see "Adding things" in [ARCHITECTURE.md](ARCHITECTURE.md). Update the docs too, because
  nothing checks the grammar document against the parser.

The Mort embedding demo needs a Mort checkout and a C backend, and its tests skip with a stated reason
when either is missing. To run it: `uv run python examples/embed/run.py`.
