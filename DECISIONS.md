# Vex design decisions

Decided 2026-11-08 against Vex `c82e226` and Mort `25255e6`. The ranked work that follows from these decisions
is in [ROADMAP.md](ROADMAP.md#roadmap-implications-of-the-niche). Hours are estimates, not measurements.

## Evidence used

Everything below was run on 2026-11-08 on Windows 11 x86-64 with CPython 3.13.12 and `python -m ziglang cc`. Other
agents were using the machine, so read timings as ratios from one run.

**Interop spike.** A throwaway Mort program (compiled by the unmodified `mortc.py`, linked with
`--link python313.lib`) started CPython in its own process and ran Vex from `src/vex` without changing either repo.

| Probe | Result |
|-------|--------|
| Mort calls Vex, which calls Mort, and returns | `damage(50, 10)` returned `29` |
| `Str` crossing both ways, non-ASCII | `hello Mört! (len 4)`, 20 bytes reported |
| Vex division by zero | status 2, `division by zero`, host continued |
| `9223372036854775807 * 2` in Vex | status 2, `integer overflow`, host continued |
| Mort host function reports failure | Vex runtime error carrying Mort's message, status 2 at the outer call |
| Unknown function name | status 3, host continued |
| Compile error in the script | status 1 with the diagnostic code and message |
| 100,000-deep recursion through `map` | status 2, `recursion too deep`, no crash |
| Script calls `exit(7)` | caught only because the spike catches `BaseException`; host continued |
| Script calls `io.write_file` | **the file was written**: scripts currently have full file access |
| Mort to Vex rate | 452,003 calls/s (500,006 calls counted, 1.106 s) |
| Vex to Mort rate | 275,412 calls/s |
| Cold start, whole process | 140-187 ms, of which `import vex` + compile is 67-77 ms |

**Execution shapes**, fib(30), best of 3 (VM best of 2), same process. Shapes 2-4 are hand-written forms of what each
approach would generate, so they are upper bounds on a real implementation.

| Shape | Time | Against CPython |
|-------|-----:|----------------:|
| 1. Plain CPython | 0.081 s | 1.0x |
| 2. Python with checked `Int` arithmetic | 0.206 s | 2.6x |
| 3. Shape 2 plus a budget charged at each call | 0.331 s | 4.1x |
| 4. Tree of Python closures, checked arithmetic | 0.768 s | 9.5x |
| 5. Current VM | 3.588 s | 44.4x |

The audit measured the VM at 4.956 s and 44.9x. The ratio agrees.

**What Mort provides, read from its source and confirmed by the spike:** `extern fn` with C-ABI types; `fn(...) -> R`
values that are plain C function pointers, callable from C and storable in structs; non-generic structs emitted as C
structs in declaration order; `--link` for import libraries; Zig detection through `python -m ziglang cc`. A hosted
build must have `main` (`typechecker.py:892`), and the spec reserves dynamic loading and a stable binary ABI
(Appendix B). Hosted Mort traps call `exit(1)` (`codegen.py:467`).

**Three constraints the spike found:**

1. Mort rejects a function-type parameter that is followed by another parameter. `_split_type_list`
   (`typechecker.py:149`, `codegen.py:83`) counts the `>` of a nested `->` as a closing angle bracket, so the comma after
   it never splits. A type alias does not help because aliases expand first. A callback in the **last** position works.
2. `PyRun_SimpleString` is not in the stable ABI: linking `python3.lib` fails with an undefined symbol. A host binary is
   tied to one CPython minor version (`python313.lib`).
3. Mort's spec makes generated layout non-portable "except at a declared and documented FFI boundary"
   (`language-specification.md:530`) and never declares one for structs or function values. The spike relies on behaviour
   the spec does not yet promise.

---

## D1. Niche

**Context.** Vex and Mort are both statically typed, both implemented in Python, and share enums, generics and
exhaustive `match`. Vex needs a job Mort cannot do well, or it has no reason to exist.

**Options.**

| Candidate | For | Against |
|-----------|-----|---------|
| A. Embeddable extension language for native programs, Mort first | Uses every measured Vex strength; gives interop a purpose; needs no competition with Mort | Requires sandboxing work that does not exist; a hosted runtime has a cost |
| B. Teaching language | `vex-lang-the-hard-way` exists; small and readable | A crowded field; says nothing about interop or roadmap priorities |
| C. Managed general-purpose language ("easy Mort") | Closures and `List`/`Map` are more pleasant than Mort | 44.9x slower than CPython; would need Mort's stdlib and tooling again; this is the Mort clone |
| D. Typed configuration language only | Static checks suit config | Aggregates must then cross the boundary from day one; a narrower case of A that A can cover with `fn config()` |

**Choice: A.**

> Vex is the statically checked extension language that native programs, Mort programs first, embed so that their users
> can change behaviour without a compiler, with scripts that can do only what the host grants and that fail with a
> source-located error instead of taking the host down.

**Target users.** First, authors of Mort programs who want rules, plugins, hooks or config logic that are editable after
shipping. Second, the people who write those scripts, who are often not systems programmers. Python programs can embed Vex
through the same host API at no extra cost.

**Why A wins, using measured facts.**

- *No C compiler needed.* This is the sharpest split with Mort. A Mort change needs `mortc` and a C backend, and this
  machine has none on PATH. A Vex change needs a text editor, and compile time is about 1 ms.
- *Trapping `Int`.* Mort wraps by specification, which suits kernels. A script author's arithmetic bug should stop with a
  location, and the spike showed exactly that.
- *Witness-based exhaustiveness* names the missing case to someone who did not write the host.
- *Source-mapped traces* can be handed back to the script author through the ABI.
- *Closures, `?` and interpolation* are what short scripts are made of, and Mort has no closure expression. The REPL gives
  scripts a place to be tried.
- *44.9x is survivable here and fatal in C.* An extension language does little work per call and delegates the rest to the
  host. It is still too slow to leave alone (D3).

**Non-goals.**

1. **No systems programming.** No pointers, no fixed-width integers, no `asm`, no freestanding output, nothing in MORT OS's
   kernel. A kernel cannot host CPython, so Vex does not run there.
2. **No script-side FFI.** No `extern`, no library loading, no way to name a native symbol. Scripts call what the host binds
   and nothing else (D5).
3. **No compute competition.** No JIT and no compile-to-C or compile-to-Mort. The target is "close to CPython"; heavy work
   belongs in a host function.
4. **Not a standalone platform.** No threads, sockets, processes or async in the standard library, and no package manager.
   The standard library grows only for data handling.
5. **No claim to contain hostile code.** "Safe" means: a script cannot touch memory, cannot use what was not granted, stops
   at its budget, and reports every failure as a status. It runs inside CPython, and that is not a security boundary until
   someone audits it as one.

---

## D2. Vex and Mort, side by side

**Context.** The division has to be simple enough to apply to future feature requests.

**Options.** (a) Overlap freely and let users pick. (b) A strict host/guest split.

**Choice: (b).** Mort is the host and owns memory, I/O, devices and speed. Vex is the guest and owns changeable policy.

| Question | Mort | Vex |
|----------|------|-----|
| Changes need | C backend, rebuild | Reload the script |
| Integer overflow | Wraps | Traps |
| Memory | Ownership, manual | Host-runtime managed |
| Reach | Everything the process can do | What the host granted |
| Runs in a kernel | Yes | Never |

**Reason.** When a feature is proposed for Vex, ask whether it is about policy or about the machine. If it is the machine,
it goes in Mort and reaches Vex as a host function.

---

## D3. Execution model

**Context.** Vex runs 44.4x to 44.9x slower than CPython. A scripting language far slower than Python, running on Python,
has no good answer to "why not use Python?". The bytecode VM also has no budget, so a looping script hangs its host.

**Performance target.** With overflow checks and the budget on, **fib(30) within 5x of CPython on the same machine**
(0.41 s here against 3.59 s today). Keep cold start under 200 ms and Mort-to-Vex above 200,000 calls/s, both already met.
Nothing faster is a goal. At that target a Vex call costs about 0.15 us, so one 2.2 us boundary crossing equals about 15
Vex calls and hooks are limited by the boundary rather than by the language. Native speed is what Mort is for.

**Options.**

| Option | Measured or estimated result | Cost | Verdict |
|--------|------------------------------|------|---------|
| Keep the VM and tune dispatch | Bounded by per-instruction Python overhead; the closure shape below is a better form of this idea and reaches only 9.5x | 10-20 h | Misses the target |
| Closure compilation | 9.5x | 15-25 h | Misses the target |
| **Compile typed AST to Python `ast`, run with `compile()`** | **2.6x, 4.1x with budget** (hand-written shape) | 30-50 h | **Chosen** |
| Native VM written in Mort or C | Removes libpython; needs a heap, GC and a bytecode file format | 100 h or more | Rejected for now |
| Compile Vex to C or Mort | Native speed | 100 h or more, and needs a C compiler at script-change time | Rejected: destroys the niche |

**Choice.** Replace the VM with a backend that lowers the checked AST to Python `ast` nodes and runs them with `compile()`.
Type information already selects `Int`, `Float` and `Str` operations, so emitted code carries inline range checks only where
`Int` arithmetic occurs. The front end is unchanged.

Requirements on the backend:

- Generate `ast` nodes, never source text. Script identifiers get a reserved prefix and script strings are constants, so a
  script cannot name a Python object.
- Set `lineno` and `col_offset` from Vex spans and use the Vex path as the filename, so Python tracebacks are Vex stack
  traces. Column data needs CPython 3.11 or later, so **`requires-python` rises to 3.11**.
- Lower expression-position `if`, `match` and blocks to statements with temporaries, which also preserves left-to-right
  evaluation and makes the gap 1 class of bug impossible.
- Capture immutable bindings at closure creation, because Python closures bind late and Vex specifies a copy.
- Charge the budget at function entry and loop back-edges, the same points as the VM, so a script consumes the same budget
  under either backend.
- Keep the VM's `Int` division and remainder semantics through helpers.

**Migration, with a gate.** Build it behind a flag, run examples and the fuzz corpus through both backends and compare output
and exit status, then delete the VM so that one backend remains. `vex disasm` then prints the generated Python.
**If the real backend cannot reach 10x CPython, stop and revisit this decision before removing the VM,** because the numbers
above come from hand-written code.

**Reason.** It is the only cheap option that reaches the target. The cost is accepted: emitted code is closer to the host
than interpreted bytecode, so a backend bug has a worse failure mode, which is one reason for non-goal 5.

**Reconsider a native VM when** a real host cannot ship libpython or needs cold start well under 100 ms. Until then, it is
a large project with no user.

---

## D4. Interop architecture

**Context.** The exit criterion is "Vex and Mort call each other through a documented FFI/ABI, with a working demo". Vex runs
on Python, and Mort compiles to native code through C.

**Options.**

| Option | Both directions real? | Mort compiler changes | Verdict |
|--------|-----------------------|-----------------------|---------|
| Subprocess with JSON-RPC | Not an ABI; no shared calls | None | Rejected |
| Mort as a shared library loaded by Python through `ctypes` | Yes | Output mode, export syntax, no-`main` rule; the spec reserves dynamic loading | Rejected: makes Vex the host, inverting D2 |
| **Mort hosts CPython in-process; a table of C function pointers joins them** | **Yes, proven by the spike** | **None** | **Chosen** |
| Native Vex VM linked into Mort | Yes | None | Rejected for now (D3) |
| Vex compiled to C and linked | Yes | None | Rejected: destroys the niche |

**Choice.**

```text
Mort executable (one process, one OS thread)
  main()
   ├─ Py_Initialize, PyRun_SimpleString("import vex.embed; vex.embed.attach(<address of VexApi>)")
   │       the only CPython functions Mort touches (plus Py_FinalizeEx)
   ├─ api.open / api.bind / api.load   Mort registers host functions, loads a script
   ├─ api.call  ───────────────────▶ ctypes callback ▶ Vex          Mort calling Vex
   └─ HostFn    ◀─────────────────── ctypes pointer  ◀ Vex          Vex calling Mort
```

Python writes `ctypes` callback pointers into a struct that Mort allocated. Mort calls them through ordinary `fn` fields, and
Vex calls Mort through `fn` values the host handed over. There is no C shim, and no CPython C API beyond those three functions.

**Costs, stated plainly.** The host needs libpython at build and run time and `PYTHONHOME`/`PYTHONPATH` set. A binary is tied
to one CPython minor version. Linux and macOS use the same design with a different link flag, but only Windows was tested.

**Reason.** It is the smallest thing that really runs both ways, needs no compiler work, and matches the host/guest split.

---

## D5. How scripts reach the host

**Context.** Criterion (e) could suggest an `extern fn` syntax in Vex.

**Options.** (a) `extern fn` in Vex scripts. (b) Host-registered functions typed by a Vex signature string, in a reserved
namespace. (c) Host-registered functions as globals, as the spike did.

**Choice: (b).** The host calls `bind(vm, "tax", "(Int) -> Int", ctx, fn)` and the script writes `host.tax(100)`.

**Reason.** `extern` would let a script name any native symbol, ending the capability model. The checker still types every
call, and an unbound name is a compile error with did-you-mean. A namespace beats globals because Vex has one flat name table
where duplicates are errors, so a new host function would otherwise break existing scripts that happen to use that name.

The FFI that satisfies criterion (e) is therefore this host binding interface. **Registration is per instance.** The public
API must not mutate `stdlib`'s module-level tables, as the spike did.

**Capabilities.** `open` takes a bit set; everything starts closed. Ungranted names do not resolve, so use is a compile
error rather than a runtime surprise. This closes the `io.write_file` hole measured above.

| Bit | Grants |
|----:|--------|
| 1 | `print`, `eprint` |
| 2 | `io.read_file`, `io.read_line` |
| 4 | `io.write_file` |
| 8 | `env.get`, `env.args` |

`exit` is never available to an embedded script. The `vex` CLI grants everything and keeps `exit`.

---

## D6. The ABI (version 1)

**Context.** Types, ownership, errors and strings must be fixed so both sides can be written independently. The normative text
will live in `docs/ffi.md`; this section is its specification.

**Options for call shape.** (a) Natural C signatures per function, built from signature strings with `ctypes`. (b) One uniform
signature with tagged values.

**Choice: (b).** One `CFUNCTYPE` covers every function in both directions, there are no per-signature code paths, and failure
has an obvious channel. The cost is a few lines of unpacking per Mort host function, which `vex.mx` helpers reduce.

```text
struct VexValue {            // 40 bytes, C layout, no padding on 64-bit targets
    tag: i64,                // 0 Unit, 1 Int, 2 Float, 3 Bool, 4 Str
    i:   i64,                // Int value; Bool as 0 or 1
    f:   f64,                // Float value
    s:   *u8,                // Str bytes
    len: i64                 // Str length in bytes
}

type HostFn = fn(*void vm, *void ctx, *VexValue argv, i64 argc, *VexValue ret) -> i64;   // 0 = success

struct VexApi {              // host allocates, sets abi_version, attach fills the rest
    abi_version: i64,        // host writes 1
    ready:       i64,        // attach writes 1 only after every pointer below is valid
    open:        fn(caps: i64) -> *void,                                // null on failure
    close:       fn(vm: *void) -> void,
    bind:        fn(vm: *void, name: *u8, sig: *u8, ctx: *void, f: HostFn) -> i64,
    load:        fn(vm: *void, path: *u8, src: *u8, len: i64) -> i64,
    call:        fn(vm: *void, name: *u8, argv: *VexValue, argc: i64, ret: *VexValue) -> i64,
    budget:      fn(vm: *void, units: i64) -> i64,
    error:       fn(vm: *void) -> *u8,                                  // accepts null vm
    fail:        fn(vm: *void, msg: *u8) -> void,                       // from inside a HostFn
    return_str:  fn(vm: *void, ret: *VexValue, s: *u8, len: i64) -> void
}
```

The field order is the ABI. `bind` keeps its callback last because of the Mort parser bug noted above.

**Types crossing the boundary.**

| Vex | Wire | Rule |
|-----|------|------|
| `Unit` | tag 0 | Other fields ignored |
| `Int` | tag 1, `i` | Exact 64-bit. Python range-checks before writing, since `ctypes` silently truncates |
| `Float` | tag 2, `f` | IEEE 754 double, NaN passes through |
| `Bool` | tag 3, `i` | Exactly 0 or 1; anything else is status 3 |
| `Str` | tag 4, `s`, `len` | UTF-8, `len` is authoritative, interior NUL allowed. Invalid UTF-8 is status 3 |

Lists, maps, structs, enums and closures do not cross in version 1. A signature mentioning them is rejected by `bind`. A host
object is represented by an `Int` handle that the host manages. Mort `resource` values never cross; a resource stays on the Mort
side behind `ctx` or a handle.

**Ownership.** Nothing is ever freed across the boundary.

| Memory | Valid until | Who may free |
|--------|-------------|--------------|
| `argv` and its strings, on any call | The callee returns | The caller. A callee that keeps data copies it |
| A string set with `return_str` | Copied before `return_str` returns, so a stack buffer is fine | The host, immediately |
| `ret.s` from `call` (also NUL-terminated) | The next `load`, `call` or `close` on that instance | Vex |
| The `error` string | The next API call on that instance | Vex |
| `ctx` | Must outlive the instance | The host |

**Errors.** Every `load`, `call`, `bind` and `budget` returns a status. Text comes from `error(vm)`.

| Status | Meaning |
|-------:|---------|
| 0 | OK |
| 1 | Compile error, with all rendered diagnostics |
| 2 | Runtime error, with the rendered stack trace |
| 3 | Binding error: unknown function, wrong arity or tag, invalid UTF-8, bad signature |
| 4 | Budget exhausted |

Rules that make this hold:

- **No exception crosses a `ctypes` frame.** `ctypes` prints and discards exceptions from callbacks, so an escape would return
  garbage. Each entry point catches `BaseException` and converts it to a status.
- A `HostFn` signals failure by returning non-zero after calling `fail`. Vex raises a runtime error with that message and the
  script's trace. Scripts cannot catch it, and it reaches the outer `call` as status 2.
- **Mort's own traps are out of scope.** A failed Mort bounds check or `assert` calls `exit(1)` and ends the process, as it
  would in any Mort program.

**Budget.** One unit per function entry and loop back-edge, reset at each outermost `call`, default 10,000,000. Builtins that can
amplify size (`repeat`, range materialisation) charge in proportion to the size created, so growth is bounded by the budget.
There is no hard heap cap in version 1.

**Threads and re-entry.** One instance family, one OS thread: the thread that ran `Py_Initialize`, which keeps the GIL. A call
from another thread deadlocks and is unsupported. Re-entry on that thread (Mort to Vex to Mort to Vex) is supported and shares
the outer budget.

**Versioning.** `abi_version` is 1. Any layout or semantic change increments it. A mismatch leaves `ready` at 0.

---

## D7. Changes required, and the demo

**Vex changes.**

1. Fix gaps 1, 2, 3 and 7: an embedded script must not be able to crash or stall its host.
2. `src/vex/host.py` (new): the public, per-instance Python host API. `resolver.py`, `stdlib.py` and `api.py` take a host table
   and capability set instead of reading only module-level tables.
3. Capability gating and the budget in `resolver.py`, `vm.py`, `stdlib.py`; a new status for an exhausted budget.
4. `src/vex/embed.py` (new): the `VexApi` table, `attach`, trampolines and conversions.
5. `embed/mort/vex.mx` (new): Mort declarations, bootstrap and value helpers.
6. `docs/ffi.md` (new), normative. `examples/embed/` (new). A CI job that installs `ziglang` and a pinned Mort.

**Mort changes.**

1. **Compiler: none required.**
2. **Spec, required:** declare the FFI boundary at `language-specification.md` section 5. A non-generic struct whose fields are
   FFI types has C layout in declaration order, and a `fn` value is a C-callable function pointer using the target C ABI. Add a
   conformance test. This promises what the implementation does today.
3. **Bug fix, recommended and not blocking:** make `_split_type_list` in `mort/typechecker.py` and `mort/codegen.py` treat `->`
   as one token, with a regression test. The ABI avoids the bug by keeping callbacks last. Per Mort's `CLAUDE.md`, run the kernel
   build check after the `codegen.py` change.

**Demo decision: an order-pricing Mort program with its pricing rules in Vex**, in `examples/embed/`, run by CI on Windows and
Linux against a golden transcript.

- Mort owns the orders and calls `price(quantity, unit_cents, customer)` in the script, which is Mort calling Vex.
- The script calls `host.tax_cents(amount)` and `host.customer_tier(id)` (`Str` back), which is Vex calling Mort.
- The same executable runs with four scripts and no rebuild:
  1. Normal rules.
  2. Different rules, to show behaviour changing without a C compiler.
  3. A rule that overflows, reported with `file:line:col` while the host carries on.
  4. An infinite loop stopped by the budget (status 4), and a script using `io.write_file` without the capability, rejected at
     compile time (status 1).

**Reason.** It shows both directions, strings, and every safety claim from D1 in one program a reader can follow. Each claim fails
visibly if it stops being true.

**Done means** the demo passes in CI on both platforms and `docs/ffi.md` is enough to write a host without reading `embed.py`.
