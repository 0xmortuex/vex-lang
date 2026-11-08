"""The virtual machine: executes bytecode with checked 64-bit integer arithmetic."""

from __future__ import annotations

import math
import sys
from typing import Any, TextIO

from vex import bytecode as bc
from vex import runtime
from vex.bytecode import (
    ADD_F,
    ADD_I,
    ADD_S,
    ADDK_L,
    BUILD_STR,
    CALL,
    CALL_BI,
    CALL_BIV,
    CALL_FN,
    CONST,
    DIV_F,
    DIV_I,
    DUP,
    DUP2,
    EQ,
    EQ_LK,
    FOR_ITER,
    GE,
    GE_LK,
    GET_FIELD,
    GET_TUPLE,
    GT,
    GT_LK,
    INDEX,
    ITER,
    JMP,
    JMP_F,
    JMP_F_KEEP,
    JMP_T,
    JMP_T_KEEP,
    LE,
    LE_LK,
    LOAD,
    LOAD_CELL,
    LOAD_GLOBAL,
    LT,
    LT_LK,
    MAKE_ADT,
    MAKE_CELL,
    MAKE_CLOSURE,
    MAKE_LIST,
    MAKE_MAP,
    MAKE_TUPLE,
    MOD_F,
    MOD_I,
    MUL_F,
    MUL_I,
    NE,
    NE_LK,
    NEG_F,
    NEG_I,
    NOT,
    PANIC,
    POP,
    RANGE,
    RANGE_INCL,
    RET,
    SET_FIELD,
    SET_INDEX,
    STORE,
    STORE_CELL,
    STORE_GLOBAL,
    SUB_F,
    SUB_I,
    SUBK_L,
    TAG_IS,
    TRY,
    Code,
)  # fmt: skip
from vex.diagnostics import Span, VexRuntimeError
from vex.runtime import (
    INT_MAX,
    INT_MIN,
    Adt,
    BuiltinFn,
    Cell,
    Closure,
    VexBudgetExceeded,
    VexExit,
    VexPanic,
    charge_size,
    display,
    values_equal,
)

MAX_DEPTH = 100_000
BUDGET_MESSAGE = "execution budget exhausted"

# Each level of a builtin calling back into Vex costs at most about 5 Python frames. In an embedding this cap,
# with the host re-entry cap, keeps every callback from native code well inside Python's recursion limit.
MAX_CALLBACK_DEPTH = 100
# A closure and an empty map are several Python objects: charged here so their memory stays within a unit's rate.
CLOSURE_COST = 2
MAP_BASE_COST = 8
_FLAT = frozenset({int, float, bool, type(None)})
_DONE = object()
_TOO_DEEP = "recursion too deep (a deeply nested value, or calls nested through builtins such as `map`)"
_OUT_OF_MEMORY = "out of memory"
_RESERVE_BYTES = 1024 * 1024  # room for reporting out of memory when values that cannot be released fill the heap


class VM:
    callback_depth = 0  # process-wide: nesting crosses instances when hosts re-enter each other

    def __init__(self, out: TextIO | None = None, args: list[str] | None = None) -> None:
        self.out: Any = out if out is not None else sys.stdout
        self.args = args or []
        self.frames: list[tuple[Code, int, list[Any], list[Any]]] = []
        self.globals: dict[str, Any] = {}
        self.reserve: bytearray | None = bytearray(_RESERVE_BYTES)
        self._oom_code: Code | None = None  # the instruction that ran out of memory
        self._oom_pc = 0
        self._oom_pushed = False  # that instruction's own frame is on the frame list (a builtin given the VM)
        self._oom_error = VexRuntimeError(_OUT_OF_MEMORY)  # built now: the last resort needs no allocation
        # Units left in the current embedded call. Infinite for programs outside an embedding.
        self.fuel: float = math.inf
        self.embedded = False  # an embedded VM caps callback nesting and locates internal failures

    # ------------------------------------------------------------ public API
    def charge(self, units: int) -> None:
        """Spend budget; builtins call this for work proportional to the size they create."""
        self.fuel -= units
        if self.fuel < 0:
            raise VexBudgetExceeded(BUDGET_MESSAGE)

    def charge_key(self, key: Any) -> None:
        """Spend budget for hashing and comparing a map key: a key is a scalar, a Str, or a tuple of those."""
        if type(key) is str:
            self.charge(len(key))
        elif type(key) is tuple:
            charge_size(key, self.charge)

    def equal(self, a: Any, b: Any) -> bool:
        """Vex ``==``, charged to the budget while it compares."""
        return values_equal(a, b, self.charge)

    def _order(self, op: int, a: Any, b: Any) -> bool:
        """``<`` and friends on strings and sequences: charge for the data, then compare."""
        if type(a) is str:
            self.charge(min(len(a), len(b)))
        else:
            charge_size(a, self.charge)
        if op == LT:
            return bool(a < b)
        if op == LE:
            return bool(a <= b)
        if op == GT:
            return bool(a > b)
        return bool(a >= b)

    def call(self, f: Any, args: list[Any]) -> Any:
        """Call a Vex function value from Python (used by higher-order builtins)."""
        self.charge(1 + (f.code.nlocals if type(f) is Closure else len(args)))
        if self.embedded and VM.callback_depth >= MAX_CALLBACK_DEPTH:
            raise VexPanic(f"recursion through builtins is too deep (more than {MAX_CALLBACK_DEPTH} levels)")
        VM.callback_depth += 1
        try:
            if type(f) is Closure:
                code = f.code
                locals_ = f.captured + args
                pad = code.nlocals - len(locals_)
                if pad > 0:
                    locals_ = locals_ + [None] * pad
                return self.execute(code, locals_)
            if type(f) is BuiltinFn:
                return f.fn(self, *args) if f.needs_vm else f.fn(*args)
            raise VexPanic(f"value of type {type(f).__name__} is not callable")  # pragma: no cover
        finally:
            VM.callback_depth -= 1

    def run_function(self, code: Code, args: list[Any] | None = None) -> Any:
        """Run a top-level function to completion, converting failures to VexRuntimeError."""
        args = args or []
        locals_ = list(args) + [None] * (code.nlocals - len(args))
        args = None  # locals_ holds them now, and must be the only thing that does when memory runs out
        self._oom_code = None
        if self.reserve is None:
            try:
                self.reserve = bytearray(_RESERVE_BYTES)
            except MemoryError:  # still full; the report falls back on the error built in __init__
                pass
        try:
            return self.execute(code, locals_)
        except VexPanic as e:
            trace = e.trace or []
            self.frames.clear()
            raise VexRuntimeError(e.message, trace) from None
        except RecursionError:
            self.frames.clear()
            raise VexRuntimeError(_TOO_DEEP) from None
        except MemoryError as e:
            e.__traceback__ = None  # its frames pin every Python local of the failed `execute` calls
            locals_.clear()
            raise self._report_out_of_memory() from None

    def _report_out_of_memory(self) -> VexRuntimeError:
        """Release the failed program's values, then describe where it failed. Runs with the heap full."""
        frames = self.frames
        for frame in frames:  # (code, pc, locals, stack); the trace only needs the first two
            frame[2].clear()
            frame[3].clear()
        code, pc = self._oom_code, self._oom_pc
        self._oom_code = None
        try:
            outer = frames[:-1] if self._oom_pushed else frames
            error = VexRuntimeError(_OUT_OF_MEMORY, self.make_trace(code, pc - 1, outer) if code is not None else [])
        except MemoryError:  # values that cannot be released (REPL globals) still fill the heap
            error = self._oom_error
            error.__traceback__ = None
        frames.clear()
        return error

    def show_result(self, code: Code, value: Any) -> str:
        """Format the value of a finished REPL input; a failure is located at that input, as a panic would be."""
        try:
            return display(value, False)
        except VexPanic as e:
            message = e.message
        except RecursionError:
            message = _TOO_DEEP
        except MemoryError:
            self.reserve = None
            message = _OUT_OF_MEMORY
        raise VexRuntimeError(message, [(code.name, _span_at(code, len(code.spans) - 1))])

    def make_trace(self, code: Code, pc: int, frames: list[Any]) -> list[tuple[str, Span | None]]:
        trace: list[tuple[str, Span | None]] = [(code.name, _span_at(code, pc))]
        for fcode, fpc, _, _ in reversed(frames):
            trace.append((fcode.name, _span_at(fcode, fpc - 1)))
        return trace

    # ------------------------------------------------------------ the interpreter loop
    def execute(self, code: Code, locals_: list[Any]) -> Any:  # noqa: C901 - one big dispatch loop
        frames = self.frames
        base = len(frames)
        instrs = code.instrs
        stack: list[Any] = []
        pc = 0
        push = stack.append
        pop = stack.pop
        try:
            while True:
                op, arg = instrs[pc]
                pc += 1
                if op == LOAD:
                    push(locals_[arg])
                elif op == CONST:
                    push(arg)
                elif op == ADDK_L:
                    r = locals_[arg[0]] + arg[1]
                    if r > INT_MAX or r < INT_MIN:
                        raise VexPanic("integer overflow in `+`")
                    push(r)
                elif op == SUBK_L:
                    r = locals_[arg[0]] - arg[1]
                    if r > INT_MAX or r < INT_MIN:
                        raise VexPanic("integer overflow in `-`")
                    push(r)
                elif op == LT_LK:
                    push(locals_[arg[0]] < arg[1])
                elif op == ADD_I:
                    b = pop()
                    r = stack[-1] + b
                    if r > INT_MAX or r < INT_MIN:
                        raise VexPanic("integer overflow in `+`")
                    stack[-1] = r
                elif op == SUB_I:
                    b = pop()
                    r = stack[-1] - b
                    if r > INT_MAX or r < INT_MIN:
                        raise VexPanic("integer overflow in `-`")
                    stack[-1] = r
                elif op == JMP_F:
                    if not pop():
                        pc = arg
                elif op == STORE:
                    locals_[arg] = pop()
                elif op == CALL_FN:
                    fcode, n = arg
                    self.fuel -= 1 + fcode.nlocals
                    if self.fuel < 0:
                        raise VexBudgetExceeded(BUDGET_MESSAGE)
                    if len(frames) >= MAX_DEPTH:
                        raise VexPanic(f"stack overflow (more than {MAX_DEPTH} nested calls)")
                    if n:
                        args = stack[-n:]
                        del stack[-n:]
                    else:
                        args = []
                    frames.append((code, pc, locals_, stack))
                    code = fcode
                    instrs = code.instrs
                    pc = 0
                    stack = []
                    push = stack.append
                    pop = stack.pop
                    pad = code.nlocals - n
                    locals_ = args + [None] * pad if pad else args
                elif op == RET:
                    val = pop()
                    if len(frames) == base:
                        return val
                    code, pc, locals_, stack = frames.pop()
                    instrs = code.instrs
                    push = stack.append
                    pop = stack.pop
                    push(val)
                elif op == JMP:
                    if arg < pc:  # a loop back-edge
                        self.fuel -= 1
                        if self.fuel < 0:
                            raise VexBudgetExceeded(BUDGET_MESSAGE)
                    pc = arg
                elif op == LT:
                    b = pop()
                    if b is None:  # Unit is Ord, with its single value equal to itself, but Python's None is not
                        stack[-1] = False
                    elif type(b) in _FLAT:
                        stack[-1] = stack[-1] < b
                    else:
                        stack[-1] = self._order(LT, stack[-1], b)
                elif op == MUL_I:
                    b = pop()
                    r = stack[-1] * b
                    if r > INT_MAX or r < INT_MIN:
                        raise VexPanic("integer overflow in `*`")
                    stack[-1] = r
                elif op == MOD_I:
                    b = pop()
                    a = stack[-1]
                    if b > 0 and a >= 0:
                        stack[-1] = a % b
                    elif b == 0:
                        raise VexPanic("division by zero in `%`")
                    else:
                        r = abs(a) % abs(b)
                        stack[-1] = -r if a < 0 else r
                elif op == DIV_I:
                    b = pop()
                    a = stack[-1]
                    if b > 0 and a >= 0:
                        stack[-1] = a // b
                    elif b == 0:
                        raise VexPanic("division by zero")
                    else:
                        q = abs(a) // abs(b)
                        if (a < 0) != (b < 0):
                            q = -q
                        if q > INT_MAX:
                            raise VexPanic("integer overflow in `/`")
                        stack[-1] = q
                elif op == EQ:
                    b = pop()
                    if type(b) in _FLAT:
                        stack[-1] = stack[-1] == b
                    else:
                        stack[-1] = values_equal(stack[-1], b, self.charge)
                elif op == LOAD_CELL:
                    push(locals_[arg].v)
                elif op == POP:
                    pop()
                elif op == LE:  # Unit is Ord, with its single value equal to itself, but Python's None is not
                    b = pop()
                    if b is None:
                        stack[-1] = True
                    elif type(b) in _FLAT:
                        stack[-1] = stack[-1] <= b
                    else:
                        stack[-1] = self._order(LE, stack[-1], b)
                elif op == GT:
                    b = pop()
                    if b is None:
                        stack[-1] = False
                    elif type(b) in _FLAT:
                        stack[-1] = stack[-1] > b
                    else:
                        stack[-1] = self._order(GT, stack[-1], b)
                elif op == GE:
                    b = pop()
                    if b is None:
                        stack[-1] = True
                    elif type(b) in _FLAT:
                        stack[-1] = stack[-1] >= b
                    else:
                        stack[-1] = self._order(GE, stack[-1], b)
                elif op == NE:
                    b = pop()
                    if type(b) in _FLAT:
                        stack[-1] = stack[-1] != b
                    else:
                        stack[-1] = not values_equal(stack[-1], b, self.charge)
                elif op == LE_LK:
                    push(locals_[arg[0]] <= arg[1])
                elif op == GT_LK:
                    push(locals_[arg[0]] > arg[1])
                elif op == GE_LK:
                    push(locals_[arg[0]] >= arg[1])
                elif op == EQ_LK:
                    push(locals_[arg[0]] == arg[1])
                elif op == NE_LK:
                    push(locals_[arg[0]] != arg[1])
                elif op == JMP_T:
                    if pop():
                        pc = arg
                elif op == JMP_F_KEEP:
                    if not stack[-1]:
                        pc = arg
                    else:
                        pop()
                elif op == JMP_T_KEEP:
                    if stack[-1]:
                        pc = arg
                    else:
                        pop()
                elif op == NOT:
                    stack[-1] = not stack[-1]
                elif op == GET_FIELD:
                    stack[-1] = stack[-1].f[arg]
                elif op == GET_TUPLE:
                    stack[-1] = stack[-1][arg]
                elif op == TAG_IS:
                    stack[-1] = stack[-1].tag == arg
                elif op == STORE_CELL:
                    locals_[arg].v = pop()
                elif op == MAKE_CELL:
                    self.charge(1)
                    locals_[arg] = Cell(locals_[arg])
                elif op == CALL:
                    n = arg
                    f = stack[-n - 1]
                    if n:
                        args = stack[-n:]
                        del stack[-n:]
                    else:
                        args = []
                    pop()
                    if type(f) is Closure:
                        self.fuel -= 1 + f.code.nlocals
                        if self.fuel < 0:
                            raise VexBudgetExceeded(BUDGET_MESSAGE)
                        if len(frames) >= MAX_DEPTH:
                            raise VexPanic(f"stack overflow (more than {MAX_DEPTH} nested calls)")
                        frames.append((code, pc, locals_, stack))
                        code = f.code
                        instrs = code.instrs
                        pc = 0
                        stack = []
                        push = stack.append
                        pop = stack.pop
                        locals_ = f.captured + args
                        pad = code.nlocals - len(locals_)
                        if pad > 0:
                            locals_ = locals_ + [None] * pad
                    else:
                        self.charge(1 + n)
                        if f.needs_vm:
                            frames.append((code, pc, locals_, stack))
                            r = f.fn(self, *args)
                            frames.pop()
                        else:
                            r = f.fn(*args)
                        push(r)
                elif op == CALL_BI:
                    impl, n = arg
                    self.charge(1 + n)
                    if n:
                        args = stack[-n:]
                        del stack[-n:]
                        push(impl(*args))
                    else:
                        push(impl())
                elif op == CALL_BIV:
                    impl, n = arg
                    self.charge(1 + n)
                    if n:
                        args = stack[-n:]
                        del stack[-n:]
                    else:
                        args = []
                    frames.append((code, pc, locals_, stack))
                    r = impl(self, *args)
                    frames.pop()
                    push(r)
                elif op == MAKE_ADT:
                    info, tag, n = arg
                    self.charge(1 + n)
                    if n:
                        fields = stack[-n:]
                        del stack[-n:]
                    else:
                        fields = []
                    push(Adt(info, tag, fields))
                elif op == ADD_F:
                    b = pop()
                    stack[-1] = stack[-1] + b
                elif op == SUB_F:
                    b = pop()
                    stack[-1] = stack[-1] - b
                elif op == MUL_F:
                    b = pop()
                    stack[-1] = stack[-1] * b
                elif op == DIV_F:
                    b = pop()
                    if b == 0.0:
                        raise VexPanic("division by zero")
                    stack[-1] = stack[-1] / b
                elif op == MOD_F:
                    b = pop()
                    if b == 0.0:
                        raise VexPanic("division by zero in `%`")
                    a = stack[-1]
                    stack[-1] = math.fmod(a, b) if math.isfinite(a) else math.nan
                elif op == NEG_F:
                    stack[-1] = -stack[-1]
                elif op == NEG_I:
                    r = -stack[-1]
                    if r > INT_MAX:
                        raise VexPanic("integer overflow in unary `-`")
                    stack[-1] = r
                elif op == ADD_S:
                    b = pop()
                    a = stack[-1]
                    if len(a) + len(b) > runtime.MAX_STR_LEN:
                        raise VexPanic("string too large")
                    self.charge(len(a) + len(b))
                    stack[-1] = a + b
                elif op == MAKE_LIST:
                    self.charge(1 + arg)
                    if arg:
                        items = stack[-arg:]
                        del stack[-arg:]
                    else:
                        items = []
                    push(items)
                elif op == MAKE_TUPLE:
                    self.charge(1 + arg)
                    items = stack[-arg:]
                    del stack[-arg:]
                    push(tuple(items))
                elif op == MAKE_MAP:
                    self.charge(MAP_BASE_COST + 2 * arg)
                    n2 = arg * 2
                    flat = stack[-n2:] if n2 else []
                    if n2:
                        del stack[-n2:]
                    for i in range(0, n2, 2):
                        self.charge_key(flat[i])
                    push({flat[i]: flat[i + 1] for i in range(0, n2, 2)})
                elif op == INDEX:
                    i = pop()
                    obj = stack[-1]
                    if type(obj) is list:
                        if i < 0 or i >= len(obj):
                            raise VexPanic(f"index {i} out of bounds for list of length {len(obj)}")
                        stack[-1] = obj[i]
                    else:
                        self.charge_key(i)
                        if i not in obj:
                            raise VexPanic(f"key {display(i, False, self.charge)} not found in map")
                        stack[-1] = obj[i]
                elif op == SET_INDEX:
                    v = pop()
                    i = pop()
                    obj = pop()
                    if type(obj) is list:
                        if i < 0 or i >= len(obj):
                            raise VexPanic(f"index {i} out of bounds for list of length {len(obj)}")
                    else:
                        self.charge_key(i)
                    obj[i] = v
                elif op == SET_FIELD:
                    v = pop()
                    pop().f[arg] = v
                elif op == DUP:
                    push(stack[-1])
                elif op == DUP2:
                    stack.extend(stack[-2:])
                elif op == ITER:
                    stack[-1] = iter(stack[-1])
                elif op == FOR_ITER:
                    nxt = next(stack[-1], _DONE)
                    if nxt is _DONE:
                        pop()
                        pc = arg
                    else:
                        push(nxt)
                elif op == RANGE:
                    self.charge(arg)
                    hi = pop()
                    stack[-1] = range(stack[-1], hi)
                elif op == RANGE_INCL:
                    self.charge(arg)
                    hi = pop()
                    stack[-1] = range(stack[-1], hi + 1)
                elif op == BUILD_STR:
                    self.charge(arg)
                    parts = stack[-arg:]
                    del stack[-arg:]
                    pieces: list[str] = []
                    size = 0
                    for p in parts:  # each part's size is known here, so a stop comes before the join copies anything
                        piece = p if type(p) is str else display(p, charge=self.charge)
                        self.charge(len(piece))
                        size += len(piece)
                        if size > runtime.MAX_STR_LEN:
                            raise VexPanic("string too large")
                        pieces.append(piece)
                    push("".join(pieces))
                elif op == TRY:
                    v = stack[-1]
                    if v.tag == 1:
                        if len(frames) == base:
                            return v
                        code, pc, locals_, stack = frames.pop()
                        instrs = code.instrs
                        push = stack.append
                        pop = stack.pop
                        push(v)
                    else:
                        stack[-1] = v.f[0]
                elif op == MAKE_CLOSURE:
                    fcode, k = arg
                    self.charge(CLOSURE_COST + k)
                    if k:
                        cap = stack[-k:]
                        del stack[-k:]
                    else:
                        cap = []
                    push(Closure(fcode, cap))
                elif op == LOAD_GLOBAL:
                    try:
                        push(self.globals[arg])
                    except KeyError:  # an earlier REPL input panicked before this `let` ran
                        raise VexPanic(f"`{arg.partition('#')[0]}` has no value: its `let` did not finish") from None
                elif op == STORE_GLOBAL:
                    self.globals[arg] = pop()
                elif op == PANIC:
                    raise VexPanic(arg)
                else:  # pragma: no cover
                    raise AssertionError(f"unknown opcode {op}")
        except VexPanic as e:
            if e.trace is None:
                e.trace = self._located_trace(code, pc, locals_, frames)
            raise
        except VexExit:
            raise
        except RecursionError:
            # One of the two deliberate last-resort boundaries. These are not hostile arguments to one builtin:
            # comparing or calling back can recurse, and any instruction can allocate, wherever the Python stack
            # or the heap happens to run out. Catching them here is what locates them.
            # The trace may hit the same limit; the next `execute` out, with more stack, then handles it.
            panic = VexPanic(_TOO_DEEP)
            panic.trace = self._located_trace(code, pc, locals_, frames)
            raise panic from None
        except MemoryError:
            self.reserve = None
            if self.embedded:  # Host.call has no run_function behind it, and the host keeps its heap anyway
                panic = VexPanic(_OUT_OF_MEMORY)
                panic.trace = self._located_trace(code, pc, locals_, frames)
                raise panic from None
            # Nothing can be built yet: this activation's locals pin the program's values. Note the innermost
            # instruction (no allocation), and let run_function report once they are gone.
            if self._oom_code is None:
                self._oom_code = code
                self._oom_pc = pc
                self._oom_pushed = bool(frames) and frames[-1][2] is locals_
            raise
        except Exception as e:
            if not self.embedded:
                raise
            panic = VexPanic(f"internal error: {type(e).__name__}: {e}")
            panic.trace = self._located_trace(code, pc, locals_, frames)
            raise panic from None

    def _located_trace(self, code: Code, pc: int, locals_: list[Any], frames: list[Any]) -> list[tuple[str, Span | None]]:
        # A builtin that takes the VM runs with the calling frame pushed; do not report it twice.
        outer = frames[:-1] if frames and frames[-1][2] is locals_ else frames
        return self.make_trace(code, pc - 1, outer)


def _span_at(code: Code, pc: int) -> Span | None:
    if 0 <= pc < len(code.spans):
        return code.spans[pc]
    return None


__all__ = ["VM", "bc"]
