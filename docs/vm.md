# The bytecode and the VM

```text
source ─► lexer ─► parser ─► resolver ─► checker ─► compiler ─► bytecode ─► VM
         tokens    AST+spans  symbols    types +      Code objects          stack machine
                              + captures  exhaustive
```

`vex disasm file.vx` prints the bytecode of a program. Source files in `src/vex/`:
`lexer.py`, `parser.py`, `resolver.py`, `checker.py` (+ `exhaust.py`), `compiler.py`,
`bytecode.py`, `vm.py`, `runtime.py` (value representations), `stdlib.py`.

## Values at runtime

| Vex | Python representation |
|-----|-----------------------|
| `Int` | `int`, range-checked to 64 bits at every arithmetic instruction |
| `Float`, `Bool`, `Str` | `float`, `bool`, `str` |
| `Unit` | `None` |
| `List`, `Map`, tuple | `list`, `dict` (insertion ordered), `tuple` |
| range `a..b` | `range` |
| struct / enum value | `Adt(info, tag, fields)`; structs have tag 0 |
| `Option` / `Result` | `Adt` with tags `Some=0, None=1` / `Ok=0, Err=1` |
| function | `Closure(code, captured)` or `BuiltinFn` |
| captured `let mut` variable | `Cell(v)` shared between frames |

Types are erased after checking; the VM never inspects static types. The compiler picks
specialised instructions (`ADD_I` vs `ADD_F` vs `ADD_S`) from the inferred types.

## Code objects and frames

Each function, method and lambda compiles to a `Code`: a list of `(opcode, argument)` tuples,
a parallel list of source spans (so every instruction maps to `file:line:col`), the number of
parameters, captured values and local slots. Locals live in a per-frame list: captured values
first, then parameters, then every `let`/pattern variable (each gets its own slot). Each frame
also has its own operand stack. A call pushes the caller's `(code, pc, locals, stack)` on an
explicit frame stack, so Vex recursion does not use Python recursion; depth is limited to
100 000 frames (`stack overflow` runtime error). Builtins that call back into Vex
(`map`, `filter`, `fold`, ...) re-enter the interpreter loop on the same frame stack.

Closures capture **by value**, except `let mut` variables that some closure captures: those are
stored in a `Cell` from their declaration, and both the function and every closure access them
with `LOAD_CELL`/`STORE_CELL`. Since immutable variables cannot change, copying them is
indistinguishable from sharing.

## Instruction set

| Group | Instructions |
|-------|--------------|
| Stack and locals | `CONST v`, `LOAD s`, `STORE s`, `POP`, `DUP`, `DUP2`, `LOAD_GLOBAL k`, `STORE_GLOBAL k` (REPL) |
| Cells | `MAKE_CELL s`, `LOAD_CELL s`, `STORE_CELL s` |
| Int (checked) | `ADD_I SUB_I MUL_I DIV_I MOD_I NEG_I` |
| Float | `ADD_F SUB_F MUL_F DIV_F MOD_F NEG_F` |
| Str | `ADD_S` |
| Compare / logic | `EQ NE LT LE GT GE NOT` (structural equality) |
| Fused local-vs-constant | `ADDK_L (s,k)`, `SUBK_L`, `LT_LK`, `LE_LK`, `GT_LK`, `GE_LK`, `EQ_LK`, `NE_LK` |
| Jumps | `JMP t`, `JMP_F t` (pop), `JMP_T t`, `JMP_F_KEEP t`, `JMP_T_KEEP t` (for `&&` / `\|\|`) |
| Calls | `CALL_FN (code,n)` known function, `CALL n` function value, `CALL_BI (impl,n)` builtin, `CALL_BIV (impl,n)` builtin that needs the VM, `RET` |
| Construction | `MAKE_LIST n`, `MAKE_TUPLE n`, `MAKE_MAP n`, `MAKE_ADT (info,tag,n)`, `MAKE_CLOSURE (code,k)` |
| Access | `GET_FIELD i`, `SET_FIELD i`, `GET_TUPLE i`, `INDEX`, `SET_INDEX` |
| Loops | `RANGE`, `RANGE_INCL`, `ITER`, `FOR_ITER t` (pushes the next item or pops the iterator and jumps) |
| Strings | `BUILD_STR n` (display-formats and joins `n` values) |
| Patterns | `TAG_IS t`, `PANIC msg` (unreachable fall-through of a match) |
| Errors | `TRY` (the `?` operator: return the `None`/`Err` or unwrap the payload) |

A short example, `fn add(a: Int, b: Int) -> Int { a + b }`:

```text
== fn add (add.vx:1) params=2 captured=0 locals=2 ==
  0000    1  LOAD         0
  0001       LOAD         1
  0002       ADD_I
  0003       RET
```

## Integer semantics (the contract)

* `Int` is 64-bit two's complement range, `-2^63 … 2^63 - 1`.
* `+ - *`, unary `-` and the library functions `abs`, `pow`, `sum`, `gcd`, `Range.len` are **checked**: a result outside the
  range raises the runtime error ``integer overflow in `op` ``. There is no wraparound mode.
* `/` truncates toward zero; `%` has the sign of the left operand; `x / 0` and `x % 0` raise
  `division by zero`; `MIN / -1` raises ``integer overflow in `/` ``.
* Integer literals outside the range are compile errors.
* Python's arbitrary-precision integers are used internally, but every instruction that can
  produce an out-of-range value checks it before the value can escape into the program.

## Runtime errors

All runtime failures are `VexPanic` exceptions raised inside the interpreter, caught at the
innermost `execute` loop, and converted to a `VexRuntimeError` carrying a stack trace built from
each frame's saved program counter and the instruction span table. The CLI prints the message,
the failing source line with a caret and "in <function> at file:line:col" for each frame
(long traces are elided in the middle). Process exit codes: 2 for a runtime error.

## Allocation limits

A `Str` built by one operation is limited to 50,000,000 characters and a `List` built by one operation to
10,000,000 elements. `repeat`, `replace`, `join`, `+`, interpolation, `Range.to_list`, `chars`, `split`,
`concat`, `extend` and the text of `print` / `eprint` stop the program with a runtime error instead of
exceeding them, and `io.read_file` returns `Err`. Reaching them by repeated `push` is not checked; running
out of memory there is a runtime error like any other (see above).

## Performance notes

The VM is a plain `while True:` dispatch loop in CPython, ordered so hot instructions are tested
first, with specialised and fused instructions for the common `i + 1` / `i < n` shapes. It is
roughly 35-45x slower than the same algorithm written directly in Python (see
`benchmarks/run.py`); it is meant for clarity and good diagnostics, not speed.
