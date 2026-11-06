"""The Vex bytecode: opcodes, code objects and the disassembler.

An instruction is an ``(opcode, argument)`` tuple. Each function (and lambda) compiles
to one ``Code`` object; calls reference callee ``Code`` objects directly.
"""

from __future__ import annotations

from typing import Any

from vex.diagnostics import Span

(
    CONST, LOAD, STORE, LOAD_CELL, STORE_CELL, MAKE_CELL, LOAD_GLOBAL, STORE_GLOBAL, POP, DUP, DUP2,
    ADD_I, SUB_I, MUL_I, DIV_I, MOD_I, NEG_I,
    ADD_F, SUB_F, MUL_F, DIV_F, MOD_F, NEG_F, ADD_S,
    EQ, NE, LT, LE, GT, GE, NOT,
    JMP, JMP_F, JMP_T, JMP_F_KEEP, JMP_T_KEEP,
    CALL_FN, CALL, CALL_BI, CALL_BIV, RET,
    MAKE_CLOSURE, MAKE_LIST, MAKE_TUPLE, MAKE_MAP, MAKE_ADT,
    GET_FIELD, SET_FIELD, GET_TUPLE, INDEX, SET_INDEX,
    RANGE, RANGE_INCL, ITER, FOR_ITER, BUILD_STR, TAG_IS, TRY, PANIC,
    ADDK_L, SUBK_L, LT_LK, LE_LK, GT_LK, GE_LK, EQ_LK, NE_LK,
    NUM_OPS,
) = range(68)  # fmt: skip

NAMES = (
    "CONST LOAD STORE LOAD_CELL STORE_CELL MAKE_CELL LOAD_GLOBAL STORE_GLOBAL POP DUP DUP2 "
    "ADD_I SUB_I MUL_I DIV_I MOD_I NEG_I ADD_F SUB_F MUL_F DIV_F MOD_F NEG_F ADD_S "
    "EQ NE LT LE GT GE NOT JMP JMP_F JMP_T JMP_F_KEEP JMP_T_KEEP "
    "CALL_FN CALL CALL_BI CALL_BIV RET MAKE_CLOSURE MAKE_LIST MAKE_TUPLE MAKE_MAP MAKE_ADT "
    "GET_FIELD SET_FIELD GET_TUPLE INDEX SET_INDEX RANGE RANGE_INCL ITER FOR_ITER BUILD_STR TAG_IS TRY PANIC "
    "ADDK_L SUBK_L LT_LK LE_LK GT_LK GE_LK EQ_LK NE_LK"
).split()
assert len(NAMES) == NUM_OPS, (len(NAMES), NUM_OPS)

JUMPS = {JMP, JMP_F, JMP_T, JMP_F_KEEP, JMP_T_KEEP, FOR_ITER}


class Code:
    """A compiled function body."""

    def __init__(self, name: str, source_path: str, line: int) -> None:
        self.name = name
        self.source_path = source_path
        self.line = line
        self.instrs: list[tuple[int, Any]] = []
        self.spans: list[Span | None] = []
        self.nparams = 0
        self.ncaptured = 0
        self.nlocals = 0

    def __repr__(self) -> str:
        return f"<Code {self.name}>"


def _fmt_arg(op: int, arg: Any) -> str:
    if arg is None:
        return ""
    if op == CALL_FN:
        return f"{arg[0].name}/{arg[1]}"
    if op == MAKE_CLOSURE:
        return f"{arg[0].name} captures={arg[1]}"
    if op in (CALL_BI, CALL_BIV):
        impl, n = arg
        return f"{getattr(impl, '__name__', 'builtin')}/{n}"
    if op == MAKE_ADT:
        info, tag, n = arg
        return f"{info.name}.{info.variants[tag]}/{n}"
    if op in JUMPS:
        return f"-> {arg:04d}"
    if op == CONST:
        text = repr(arg)
        return text if len(text) < 40 else text[:37] + "..."
    if op in (ADDK_L, SUBK_L, LT_LK, LE_LK, GT_LK, GE_LK, EQ_LK, NE_LK):
        return f"local {arg[0]}, {arg[1]!r}"
    return str(arg)


def disassemble(code: Code) -> str:
    out = [f"== fn {code.name} ({code.source_path}:{code.line}) params={code.nparams} "
           f"captured={code.ncaptured} locals={code.nlocals} =="]  # fmt: skip
    last_line = -1
    for i, (op, arg) in enumerate(code.instrs):
        sp = code.spans[i]
        line = sp.line if sp is not None else -1
        mark = f"{line:4d}" if line != last_line and line > 0 else "    "
        last_line = line if line > 0 else last_line
        out.append(f"  {i:04d} {mark}  {NAMES[op]:<12} {_fmt_arg(op, arg)}".rstrip())
    return "\n".join(out)


def all_code(roots: list[Code]) -> list[Code]:
    """Every function reachable from ``roots`` including lambdas (in discovery order)."""
    seen: dict[int, Code] = {}
    stack = list(reversed(roots))
    while stack:
        c = stack.pop()
        if id(c) in seen:
            continue
        seen[id(c)] = c
        for op, arg in c.instrs:
            if op == MAKE_CLOSURE:
                stack.append(arg[0])
    return list(seen.values())
