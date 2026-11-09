# Vex benchmarks

**Vex is slow.** The VM is an interpreter written in Python, so every Vex instruction costs several CPython
operations. Measured here it is **16 to 33 times slower than the same program in CPython** for a whole process,
and **26 to 81 times slower** once CPython's startup is taken out (below). Do not use it for number crunching.

## Results

Five runs of each program, alternating Vex and CPython. Times are whole-process wall time in seconds: median, then
`[min-max]`. The ratio is median over median, and its range is the extreme pairing (slowest Vex over fastest
CPython and the reverse).

| benchmark | size | Vex | CPython | Vex / CPython | checked result |
|---|---:|---:|---:|---:|---|
| `fib` | 30 | 7.507 [6.540-7.903] | 0.241 [0.157-0.275] | **31.1x** (23.8-50.5) | `832040` |
| `nbody` | 5000 steps | 3.068 [2.625-3.805] | 0.094 [0.081-0.179] | **32.6x** (14.7-47.0) | `-169075163 -169020000` |
| `spectral` | 100 | 3.943 [3.854-4.359] | 0.122 [0.101-0.133] | **32.2x** (29.0-43.1) | `1274219991` |
| `strings` | 20000 | 1.752 [1.735-1.909] | 0.111 [0.107-0.137] | **15.8x** (12.7-17.9) | `1734390175 1198 401905` |
| `hashmap` | 100000 | 1.953 [1.719-2.099] | 0.108 [0.086-0.109] | **18.1x** (15.7-24.5) | `355582556 56829 21580` |

Raw samples are in [`benchmarks/results/latest.json`](benchmarks/results/latest.json).

### These figures understate the slowdown

A do-nothing program took 0.247 s on Vex and 0.066 s on CPython (median of 5). That means CPython's whole-process
times above are mostly startup. The guard (below) times the workload alone, in-process, on smaller sizes; one run
measured these ratios:

| benchmark | Vex / CPython, in-process (fastest of 3) |
|---|---:|
| `fib` | 44.2x |
| `nbody` | 80.5x |
| `spectral` | 57.2x |
| `strings` | 26.5x |
| `hashmap` | 34.1x |

An earlier guard run on the same day gave 45.2x, 86.8x, 41.2x, 28.3x and 33.4x, so treat single-digit differences
between these as noise. CPython takes 5 to 11 ms on these sizes.

## Environment and caveats

- **Machine:** 12th Gen Intel Core i5-12400F, 12 logical CPUs, 15.8 GB RAM, Windows 11 (build 26300).
- **Software:** CPython 3.13.12; Vex at commit `ea44359` plus this suite.
- **The machine was shared.** Other agents were running pytest during the measurement (18 Python or Zig processes
  and 8.8 GB RAM free when it started). Absolute times are unreliable: in two full runs one minute apart, `fib`'s
  median on Vex moved from 5.09 s to 7.51 s while its ratio stayed at about 30x. Read ratios, not seconds, and even
  ratios only to about 20 to 30%.
- These programs do the same work in both languages, but the Vex side uses Vex's checked 64-bit integers and
  bounds-checked lists.

## Workloads

Each one is a pair in `benchmarks/suite`: `name.vx` and `name.py`, same algorithm, size from the command line.

- **fib**: recursive Fibonacci; calls and integer arithmetic.
- **nbody**: five-body simulation from the Benchmarks Game, floats and list indexing. Prints energy before and after
  as integers scaled by 1e9.
- **spectral**: spectral norm from the Benchmarks Game, float arithmetic through function calls. Same scaling.
- **strings**: interpolate, split, search, upper-case, then hash each character with a modular hash.
- **hashmap**: insert-or-update on the built-in `Map`, then lookups and removals over a pseudo-random key stream;
  `dict` on the CPython side.

**Results are checked, not only printed.** The runner fails if any output differs from the expected values pinned
in `run_suite.py`, in either language. The 1000-step n-body energies and spectral-norm n=100 match the published
Benchmarks Game reference output (truncated to 9 digits); the 5000-step n-body value was also produced by Mort's
independent C implementation.

`benchmarks/run.py` and its `fib.vx` / `loops.vx` are the older, smaller check and still run in CI.

## Reproducing

```text
uv sync
uv run python benchmarks/suite/run_suite.py --runs 5 --note "what else is running on the machine"
```

This writes `benchmarks/results/latest.json`, including CPU, memory, tool versions and startup cost.

## The regression guard

```text
uv run python benchmarks/suite/run_suite.py --guard    # CI step: results and ratio to CPython
uv run pytest tests/test_benchmarks.py                 # deterministic: results and counted work
```

- `tests/test_benchmarks.py` runs on every CI configuration. It checks each program's answer on both Vex and CPython
  and its **fuel**, the VM's deterministic count of calls, loop back-edges and built-in costs, against a limit about
  1.5 times what it used when written. Nothing depends on the clock.
- The `--guard` CI step times each workload in-process at small sizes and takes the fastest of 3 attempts on each
  side, since other load can only slow a run down. It fails if the Vex/CPython ratio passes a per-benchmark limit
  (150x to 350x, at least 3 times the worst seen on the development machine) or if any result is wrong.
- A wrong result always fails: a broken program copy and a wrong expected value each made the real guard exit 1.
