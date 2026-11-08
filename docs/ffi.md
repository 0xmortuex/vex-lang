# Embedding Vex: the ABI, version 1

This document is normative. It is enough to write a native host without reading `src/vex/embed.py`. Mort hosts use
`embed/mort/vex.mx`, which declares everything below. A Python program uses the same rules through `vex.host.Host`.

## Overview

A native program hosts CPython in-process, and Vex runs inside it. The two sides communicate only through one table of
C function pointers, the `VexApi`, which the host allocates and Python fills. The host touches exactly three CPython functions:
`Py_Initialize`, `PyRun_SimpleString` and `Py_FinalizeEx`. There is no C shim.

```text
Native host (one process, one OS thread)
  Py_Initialize
  PyRun_SimpleString("import vex.embed; vex.embed.attach(<address of VexApi>)")
  api.open / api.bind / api.load       register host functions, load a script
  api.call  ──────────▶ Vex             native calls script
  HostFn    ◀────────── Vex             script calls native
  Py_FinalizeEx
```

## Building and running a host

The host must link against the CPython library for **one exact minor version** and load it at run time.

- Linking: `PyRun_SimpleString` is not in the stable ABI, so a host cannot link `python3.lib`. Link
  `python313.lib` (Windows) or `-lpython3.13` (other platforms). A binary built against 3.13 needs 3.13 at run time.
- Run time: `PYTHONHOME` must name that CPython's install directory, `PYTHONPATH` must include the directory holding the `vex`
  package (`src` in this repository), and the CPython shared library must be on the loader's path
  (`PATH` on Windows, `LD_LIBRARY_PATH` on Linux).

CPython 3.13 on Windows x86-64 is the only tested combination. Linux and macOS use the same design with a different link flag.

The host imports exactly three symbols from libpython: `Py_Initialize`, `PyRun_SimpleString` and `Py_FinalizeEx`.

Bootstrap: allocate a zeroed `VexApi`, write `abi_version = 1` and `ready = 0`, then run
`import vex.embed; vex.embed.attach(<decimal address of the table>)` with `PyRun_SimpleString`. `attach` writes every function
pointer and sets `ready` to 1 **last**. If `ready` is still 0, the table must not be used: either Python printed a traceback to
stderr (for example, `vex` is not on `PYTHONPATH`) or the host's `abi_version` does not match.

## Types

All sizes assume a 64-bit target. Layout is C, in declaration order, with no padding. In C:

```text
typedef struct {            // 40 bytes
    int64_t tag;            // offset 0: see the type table
    int64_t i;              // offset 8: Int value; Bool as 0 or 1
    double  f;              // offset 16: Float value
    uint8_t *s;             // offset 24: Str bytes
    int64_t len;            // offset 32: Str length in bytes
} VexValue;

// Returns 0 on success. Arguments: instance, the ctx given to bind, argument array, argument count, result slot.
typedef int64_t (*HostFn)(void *vm, void *ctx, VexValue *argv, int64_t argc, VexValue *ret);

typedef struct {
    int64_t abi_version;                    // host writes 1
    int64_t ready;                          // attach writes 1 once every pointer below is valid
    void *(*open)(int64_t caps);            // null on failure
    void (*close)(void *vm);
    int64_t (*bind)(void *vm, const char *name, const char *sig, void *ctx, HostFn f);
    int64_t (*load)(void *vm, const char *path, const uint8_t *src, int64_t len);
    int64_t (*call)(void *vm, const char *name, VexValue *argv, int64_t argc, VexValue *ret);
    int64_t (*budget)(void *vm, int64_t units);
    const char *(*error)(void *vm);          // accepts null vm
    void (*fail)(void *vm, const char *msg); // from inside a HostFn
    void (*return_str)(void *vm, VexValue *ret, const uint8_t *s, int64_t len);
} VexApi;
```

The field order of `VexApi` is the ABI. `bind` keeps the callback last because the Mort type parser cannot split a
function-type parameter that is followed by another parameter.

### Values crossing the boundary

| Vex | Wire | Rule |
|-----|------|------|
| `Unit` | tag 0 | Other fields ignored |
| `Int` | tag 1, `i` | Exact 64-bit. A value outside that range never reaches the wire: it is an error instead |
| `Float` | tag 2, `f` | IEEE 754 double. NaN passes through |
| `Bool` | tag 3, `i` | Exactly 0 or 1. Anything else is status 3 |
| `Str` | tag 4, `s`, `len` | UTF-8. `len` is the byte count and is authoritative, interior NUL is allowed. Invalid UTF-8 is status 3 |

Tags outside this table are status 3. Lists, maps, tuples, structs, enums and closures do not cross: `bind` rejects a signature
that mentions them (status 3), and `call` rejects a script function that takes or returns them (status 3). A generic function
cannot be called from the host. Represent a host object as an `Int` handle the host manages. Mort `resource` values never
cross; keep them on the Mort side behind `ctx` or a handle.

Strings Vex provides (`argv` strings to a `HostFn`, `ret.s` from `call`) are additionally NUL-terminated, so they can be
passed straight to C-string functions as long as the text has no interior NUL.

A `HostFn` receives `argc` equal to the bound signature's parameter count, already type-checked by the checker when the
script was compiled. `call` accepts at most 255 arguments.

## Signatures and names

A binding signature uses Vex type syntax: `"(Int, Str) -> Bool"`. No return type means `Unit`: `"(Int)"`. The empty parameter
list is `"()"`. `Unit`, `Int`, `Float`, `Bool` and `Str` are the only types permitted. A name must be an identifier that is not a
Vex keyword. Binding the same name twice in one instance is status 3. `bind` applies to scripts loaded afterwards.

A bound function appears only as `host.<name>` in scripts, and a script is type-checked against its signature when it is
loaded, so `host.tax("x")` and an unbound `host.typo(1)` are compile errors. `host` is reserved in every Vex program (a local
variable can still be named `host`).

## Lifetimes and ownership

Nothing is ever freed across the boundary.

| Memory | Valid until | Who may free |
|--------|-------------|--------------|
| `argv` and its strings, on any call | The callee returns | The caller. A callee that keeps data copies it |
| A string given to `return_str` | Copied before `return_str` returns, so a stack buffer is fine | The host, immediately |
| `ret.s` from `call` | The next `load`, `call` or `close` on that instance | Vex |
| The `error` string | The next API call on that instance | Vex |
| `ctx` | Must outlive the instance | The host |

A `HostFn` sets a `Str` result with `return_str(vm, ret, bytes, len)`; `ret` must be the pointer it was given. It sets any other
result by writing `ret` directly (`tag` and the value field).

## API

Every call taking `vm` returns status 3 for a null, unknown or closed instance, unless noted.

- `open(caps)` creates an instance. Returns null on bad capability bits; the reason is `error(NULL)`.
- `close(vm)` destroys an instance. Null or unknown is a no-op.
- `bind(vm, name, sig, ctx, f)` registers a host function. `ctx` is passed back unchanged.
- `load(vm, path, src, len)` compiles a script and replaces the instance's current script. On failure the previous
  script stays loaded. `path` is used only in diagnostics. `src` need not be NUL-terminated.
- `call(vm, name, argv, argc, ret)` runs a top-level script function. `argv` may be null when `argc` is 0. On any failure `ret` is
  left as `Unit`.
- `budget(vm, units)` sets the units each outermost call may spend. Units must be positive; otherwise status 3.
- `error(vm)` returns the UTF-8, NUL-terminated message from the last failed call. It is empty after a success. `error(NULL)`
  and any unknown instance report the instance-less error (`open` failures, a bad handle).
- `fail(vm, msg)` and `return_str(vm, ret, s, len)` are used inside a `HostFn` only.

A script cannot import other files: the host supplies all its text. An `import` is a compile error.

## Lifecycle: start and stop

`Py_FinalizeEx` destroys the interpreter that owns every table pointer, so **no table entry may be called after it**. Doing so
through a raw table entry crashes the process, because nothing can catch it on the Vex side. Mort hosts get protection from
`vex.mx`: `vex.stop()` clears `ready`, and from then on every `vex.*` wrapper returns status 3 (`open` returns null,
`error` returns a fixed message, `close`, `fail` and `return_str` do nothing). `vex.stop()` returns false if the
runtime is not running. CPython cannot be restarted in the same process reliably, so `vex.start()` after `vex.stop()` returns a
table whose `ready` is 0.

`vex.stop()` from inside a running `vex.call` (a `HostFn`, at any re-entry depth) is **refused**: it returns false, stops nothing,
and the runtime and the call carry on normally. The interpreter is still on the stack below the `HostFn`, so finalising it there
would crash the process. Call `vex.stop()` after the outermost `vex.call` has returned.

A C host calling the table directly must clear its own copy of the pointers before calling `Py_FinalizeEx`, and must not call
`Py_FinalizeEx` from inside a `HostFn`: the raw table cannot refuse it, because the running call is the host's own.

## Status codes

| Status | Meaning | `error(vm)` |
|-------:|---------|-------------|
| 0 | OK | empty |
| 1 | Compile error, or a compiler failure | Every rendered diagnostic, with `file:line:col` |
| 2 | Runtime error | The rendered stack trace, with `file:line:col` |
| 3 | Binding error: bad handle, unknown function, wrong arity or tag, invalid UTF-8, bad signature, unknown capability | `binding error: ...` |
| 4 | Budget exhausted | The rendered stack trace, with `file:line:col` |

Scripts cannot catch these. No exception or crash crosses the boundary: every entry point catches `BaseException`, including
any raised while it reports another failure, and returns a status (a failed `open` returns null, `error` always returns a valid
string). The instance stays usable after any failure. A status 2 or 4 message from running a script names a script location. For
a stop that comes before any script code has run in that call (a re-entry past the depth limit, or a budget stop at function
entry), the location is the declaration of the function that was being entered, which is the location the host asked to reach.
The only exception is a failure of the boundary itself, such as `MemoryError` while copying a result: it is reported as status 2
with the text `internal error: ...` and no script location, because no script is at fault.

The guarantee needs Python stack headroom when native code calls in, because `ctypes` drops a callback that fails on entry and
returns garbage. Depth limits (below) provide that headroom at every callback, on the default recursion limit of 1000. Python's default SIGINT
handler would raise at any bytecode, so `attach` restores the default signal action unless the host installed its own handler.
One failure cannot be converted, by design: if CPython itself cannot allocate memory at the moment a callback is entered, it
reports that to stderr and the return value is undefined. The whole process is out of memory at that point.

### Failing from a host function

To fail, a `HostFn` calls `fail(vm, "message")` and returns non-zero. Vex raises a runtime error with that message at the script's
call site, and it reaches the outer `call` as status 2 with the script's trace. Returning non-zero without `fail` produces a
generic message. Calling `fail` and returning 0 is still a failure.

A `HostFn` that returns the wrong tag, invalid UTF-8 or a `Bool` other than 0 or 1 reaches the outer `call` as **status 3**, with
the script location of the call in the message.

Mort's own traps are out of scope: a failed Mort bounds check or `assert` ends the process, as it would in any Mort program.

## Capabilities

`open(caps)` takes a bit set. Everything starts closed. An ungranted name is a compile error (`capability-denied`) rather than a
runtime surprise.

| Bit | Grants |
|----:|--------|
| 1 | `print`, `eprint` |
| 2 | `io.read_file`, `io.read_line` |
| 4 | `io.write_file` |
| 8 | `env.get`, `env.args` |

`exit` is never available to an embedded script. `math`, `panic`, `assert` and `assert_eq` need no capability. The Vex CLI
grants everything and keeps `exit`. These names are reserved in all embedded scripts, granted or not.

Python's stdout is flushed after each `call` on an instance with bit 1. The host's own output buffering is the host's to manage.

## Execution budget

A call may spend a fixed number of units; the default is 5,000,000. A call to a function or closure charges `1 + locals` units
and a loop back-edge charges one, so a function with no locals costs exactly one unit per entry.

Work whose cost scales with data is charged one unit per character or element touched, before the work it pays for, in these places:

- `==` and `!=` on strings, lists, tuples, maps and enums; `<`, `<=`, `>`, `>=` on strings and sequences. `==` walks values
  without recursion, so depth is limited only by the budget.
- Formatting: `print`, `eprint`, interpolation, and the text of `assert_eq`, `unwrap`, `expect` and `unwrap_err`.
- Builtins that read or build strings and collections, including `contains`, `index_of`, `sort`, `sorted`, `reverse`, `insert`,
  `remove_at`, `sum`, `repeat`, `replace`, `join`, `split`, `concat`, `copy`, range materialisation and `Map.keys`.
- Builtins that take a callback (`map`, `filter`, `any`, `all`, `for_each`) charge for the copy of the list they loop over before
  the first callback, so an early exit still pays for the set-up. `find` and `fold` copy nothing and charge per callback only.
- Map lookups and updates (`m[k]`, `m[k] = v`, `get`, `insert`, `remove`, `contains_key`, map literals) charge the size of the key.
- Reading and writing through `io.*`, and a `Str` crossing the boundary in either direction, one unit per character.
- `+` on `Str`.
- Constructing a value, per operand built: a list, tuple or struct/enum literal costs `1 + fields`, a map literal `8 + 2 * pairs`,
  a closure `2 + captures`, a captured-variable cell 1, an interpolation one per part. There are no pre-built array constants.
- Every builtin call charges `1 + arguments`. `enumerate`, `zip` and `Map.entries` charge 3 per element and `chars` 2, since each
  element is a new object; displaying a list charges 3 per element and a map 6.

`List.push`, `List.clear` and `Map.clear` are constant or amortised: `push` is paid for by whatever loop makes the calls, and
`clear` frees only what was charged when it was built. `tests/test_budget_audit.py` lists every builtin with its classification.

The budget resets at each outermost `call`. When it runs out, the script stops with status 4 at its current location.

The exact memory guarantee is this. The budget is a work limit, not a memory cap, and it has no hard memory cap in version 1.

- A result whose size is known is paid for before it is allocated: `repeat`, `join`, `+`, `to_list`, `concat` and the like. A
  budget stop leaves nothing allocated for that result.
- Interpolation charges each part before the join copies anything, so `"{s}{s}..."` stops at the first part the budget cannot pay
  for, without allocating the result. Measured: 300 parts of a 1 MB string with a budget of 1.1 million stop with status 4 at a
  peak of 1 MB of traced Python allocations, which is the script's own `s`.
- Formatting a value (`display`) spends budget for each piece of text before it appends it. `io.read_file` and `io.read_line` read
  and charge in pieces of at most 64 KiB, so they can be at most one piece ahead of the budget.
- Memory grows with the units spent, at a rate set by CPython's object sizes. `tests/test_budget_opcodes.py` keeps the worst
  retained rate under 40 bytes of traced Python allocation per unit, for every value-building opcode and builtin; the native rate
  for the worst builder (`xs.push(Some(i))`) measured 31 bytes per unit.
- At the default budget, the highest peak process memory measured across 16 adversarial scripts on Windows x86-64, CPython 3.13,
  was **191 MB** of working set (187 MB committed), for a script that nests 99,000 calls and then retains `Some` values until the
  budget stops it. That includes CPython itself: a script doing nothing peaks at 17-20 MB. Retaining values at the default without
  recursion peaked at 171 MB; a retained wide literal (`[0, ... 1000 items]` pushed in a loop) at 59 MB.
- That figure depends on more than the budget, because three things are not charged by the unit: the compiled script (bytecode and
  constants, proportional to script text, which the host supplies), nested call frames (at most 100,000, about 300 bytes each plus
  the locals they charged, a fixed ceiling independent of budget), and CPython itself, which the 191 MB includes. Time per unit
  also depends on script size: one unit covers one loop back-edge or call, so it can run up to the longest straight-line stretch of
  instructions in that script. A host that wants a tighter bound sets a lower budget with `budget`; the memory bound scales with it
  and the rest stays fixed.

## Values that contain themselves

A struct field or enum can be assigned a value that reaches back to itself (`a.next = Some(a)`, or a struct in its own list field).
Printing and interpolating such a value, and comparing two of them with `==`, `!=`, `assert_eq`, `contains` or `index_of`, is a runtime error:
status 2 here, exit code 2 from the Vex CLI, at the script location of the operation. It is detected the moment the cycle is
reached, so it needs no budget to end it. `a == a`, on the same object, is simply true, and comparing two values that differ before
any cycle is reached returns `false`. Two cycles of different lengths whose elements agree still terminate, with the same error. Shared
values that are not cycles are fine; the check tracks only what is on the current path.

Hashing and ordering cannot reach a cycle. A map key must be `Int`, `Str`, `Bool` or a tuple of those, and only those and lists of
those can be ordered, so the checker rejects a struct or enum in either position.

## Threads and re-entry

Everything runs on one OS thread: the thread that ran `Py_Initialize`, which keeps the GIL. A call from any other thread deadlocks and
is unsupported.

A `HostFn` may call back into `call` on the same thread (native to Vex to native to Vex). Re-entry shares the outer call's budget, and the
outer call reports a budget stop as status 4 even if the `HostFn` reports a failure of its own. `load` and `close` should not be called
during a running `call` on the same instance.

Two limits apply to the **whole process**, across every instance. A deeper call fails with status 2 at a script location: the
call to the builtin for callbacks, and the declaration of the function being entered for host re-entry, since the limit stops it
before any of its code runs:

| Limit | Value | Bounds |
|-------|------:|--------|
| Calls into Vex nested through host functions | 32 | Native stack: measured 1 MiB safe to 142 hops, on Windows x86-64 |
| Builtins calling back into Vex (`map` inside `map`) | 100 | Python recursion, keeping callback headroom |

Ordinary Vex-to-Vex recursion is not limited by these; it uses a heap-allocated frame stack limited to 100,000 frames.

## Versioning

`abi_version` is 1. Any change to layout or semantics increments it. A mismatch leaves `ready` at 0 and writes a message to stderr.

## Mort helpers (`embed/mort/vex.mx`)

Add it to a build with `--package vex=embed/mort/vex.mx --link <libpython>`.

- `vex.start() -> *VexApi` and `vex.stop() -> bool` (see [Lifecycle](#lifecycle-start-and-stop)).
- Text helpers `vex.write_text`, `write_decimal` and `write_i64` build a line in a `[]u8`. They stop at the end of the buffer rather
  than trapping, so text from a script can never overrun it.
- One wrapper per table entry, for example `vex.call(api, vm, name, argv, argc, ret)`. Mort can call a function value only through
  a named binding, so the wrappers keep host code to one line per call.
- Constructors `vex.of_unit`, `of_int`, `of_float`, `of_bool`, `of_str`, `of_cstr`.
- Accessors `vex.arg`, `arg_int`, `arg_float`, `arg_bool`, `arg_str`, returning fields of `argv[i]`. The typed accessors `assert` the tag,
  which a compiled script cannot violate.
- Result helpers `vex.return_int`, `return_float`, `return_bool`, `return_unit`, each returning 0 for a one-line success return.
- `vex.status_name(status) -> *u8` and `vex.c_len(text) -> i64`.

The worked example is `examples/embed/pricing.mx`; run it with `uv run python examples/embed/run.py`.

## Python hosts

`vex.host.Host` is the same model for a Python program, without the wire format:

```text
from vex.host import CAP_PRINT, Host

h = Host(CAP_PRINT)
h.bind("tax", "(Int) -> Int", lambda n: n // 10)
h.load("rules.vx", "fn total(n: Int) { n + host.tax(n) }")
h.call("total", [100])     # 110
```

Failures raise `vex.diagnostics.VexError` (status 1), `VexRuntimeError` (2), `vex.host.VexBindingError` (3) and `vex.host.VexBudgetError`
(4; a subclass of `VexRuntimeError`).
