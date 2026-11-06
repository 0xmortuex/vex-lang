"""Bytecode shape tests: the compiler emits the instructions we claim it does."""

from __future__ import annotations

from vex import bytecode as bc
from vex.api import compile_text
from vex.bytecode import Code, all_code, disassemble


def ops_of(src: str, fn: str = "main") -> list[str]:
    prog = compile_text(src)
    code = next(f.code for f in prog.functions if f.qualname == fn)
    return [bc.NAMES[op] for op, _ in code.instrs]


def test_opcode_table_is_consistent() -> None:
    assert len(bc.NAMES) == bc.NUM_OPS
    assert len(set(bc.NAMES)) == bc.NUM_OPS
    assert bc.NAMES[bc.CONST] == "CONST" and bc.NAMES[bc.NE_LK] == "NE_LK"


def test_int_arithmetic_uses_checked_int_ops() -> None:
    ops = ops_of("fn main() { let a = 1; let b = 2; print(a * b + a - b); }")
    assert {"MUL_I", "ADD_I", "SUB_I"} <= set(ops)
    assert "ADD_F" not in ops


def test_float_arithmetic_uses_float_ops() -> None:
    ops = ops_of("fn main() { let a = 1.5; print(a * a / a); }")
    assert {"MUL_F", "DIV_F"} <= set(ops)
    assert "MUL_I" not in ops


def test_string_concat_uses_add_s() -> None:
    assert "ADD_S" in ops_of('fn main() { let a = "x"; print(a + a); }')


def test_fused_local_plus_constant() -> None:
    ops = ops_of("fn main() { let mut i = 0; while i < 10 { i += 1; } }")
    assert "LT_LK" in ops and "ADDK_L" in ops


def test_direct_calls_use_call_fn_and_closures_use_call() -> None:
    ops = ops_of("fn f() -> Int { 1 } fn main() { f(); let g = || 2; g(); }")
    assert "CALL_FN" in ops and "CALL" in ops and "MAKE_CLOSURE" in ops


def test_builtins_are_called_directly() -> None:
    ops = ops_of('fn main() { let n = "abc".len(); print(n); }')
    assert "CALL_BI" in ops and "CALL_BIV" in ops


def test_boxed_variable_only_when_mutated_and_captured() -> None:
    prog = compile_text("fn main() { let mut n = 0; let f = || { n += 1; }; f(); }")
    every = [bc.NAMES[op] for c in all_code([f.code for f in prog.functions]) for op, _ in c.instrs]
    assert "MAKE_CELL" in every and "LOAD_CELL" in every and "STORE_CELL" in every
    plain = ops_of("fn main() { let n = 0; let f = || n + 1; f(); }")
    assert "MAKE_CELL" not in plain
    unshared = ops_of("fn main() { let mut n = 0; n += 1; print(n); }")
    assert "MAKE_CELL" not in unshared


def test_match_emits_tag_tests_and_a_panic_fallthrough() -> None:
    ops = ops_of("enum E { A, B(Int) } fn main() { let e = E.B(1); match e { A => print(0), B(n) => print(n) } }")
    assert ops.count("TAG_IS") == 2 and "PANIC" in ops


def test_for_loops_use_iter_protocol() -> None:
    ops = ops_of("fn main() { for i in 0..3 { print(i); } }")
    assert ops[ops.index("RANGE") + 1 : ops.index("RANGE") + 3] == ["ITER", "FOR_ITER"]


def test_break_from_for_pops_iterator() -> None:
    ops = ops_of("fn main() { for i in 0..3 { if i == 1 { break; } } }")
    assert "POP" in ops


def test_and_or_short_circuit_use_keep_jumps() -> None:
    ops = ops_of("fn main() { let a = true; print(a && a || a); }")
    assert "JMP_F_KEEP" in ops and "JMP_T_KEEP" in ops


def test_jump_targets_are_in_range_and_functions_end_in_ret() -> None:
    src = open_examples()
    prog = compile_text(src)
    for code in all_code([f.code for f in prog.functions]):
        n = len(code.instrs)
        assert code.instrs[-1][0] == bc.RET
        assert len(code.spans) == n
        for op, arg in code.instrs:
            if op in bc.JUMPS:
                assert isinstance(arg, int) and 0 <= arg <= n, (code.name, op, arg)


def open_examples() -> str:
    import pathlib

    return (pathlib.Path(__file__).parent.parent / "examples" / "json_parser.vx").read_text(encoding="utf-8")


def test_every_instruction_has_a_source_span() -> None:
    prog = compile_text(open_examples())
    for code in all_code([f.code for f in prog.functions]):
        missing = [i for i, sp in enumerate(code.spans) if sp is None]
        assert not missing, (code.name, missing[:3])


def test_disassembly_lists_functions_lines_and_args() -> None:
    prog = compile_text("fn add(a: Int, b: Int) -> Int { a + b }\nfn main() { print(add(1, 2)); }\n")
    text = prog.disassemble()
    assert "== fn add" in text and "== fn main" in text
    assert "CALL_FN      add/2" in text
    assert "ADD_I" in text
    assert "params=2" in text


def test_disassemble_single_code_object() -> None:
    c = Code("x", "f.vx", 1)
    c.instrs = [(bc.CONST, 5), (bc.RET, None)]
    c.spans = [None, None]
    text = disassemble(c)
    assert "CONST        5" in text and text.rstrip().endswith("RET")


def test_lambda_code_objects_are_discoverable() -> None:
    prog = compile_text("fn main() { let k = 1; let f = |x: Int| x + k; print(f(1)); }")
    names = [c.name for c in all_code([f.code for f in prog.functions])]
    assert names.count("<lambda>") == 1
    lam = next(c for c in all_code([f.code for f in prog.functions]) if c.name == "<lambda>")
    assert lam.ncaptured == 1 and lam.nparams == 1


def test_struct_literal_fields_evaluated_in_source_order() -> None:
    from helpers import run_source

    out = run_source(
        'struct P { a: Int, b: Int }\nfn t(n: Int) -> Int { print("t{n}"); n }\n'
        "fn main() { let p = P { b: t(1), a: t(2) }; print(p.a * 10 + p.b); }\n"
    )
    assert out == "t1\nt2\n21\n"


def test_method_receiver_evaluated_before_arguments() -> None:
    from helpers import run_source

    out = run_source(
        'fn r() -> List[Int] { print("recv"); [1] }\nfn a() -> Int { print("arg"); 2 }\n'
        "fn main() { let mut x = r(); x.push(a()); }\n"
    )
    assert out == "recv\narg\n"
