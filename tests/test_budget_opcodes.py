"""Every VM opcode, audited against the execution budget, and the memory a unit can buy.

Each opcode is either charged in proportion to what it builds or classified as constant work; `NAMES` is the source of truth, so a new
opcode that is not listed fails the test. The retention tests below measure traced Python memory per unit spent.
"""

from __future__ import annotations

import pathlib
import tracemalloc
from typing import Any

import pytest

from vex import bytecode as bc
from vex.diagnostics import VexRuntimeError
from vex.host import CAP_ALL, DEFAULT_BUDGET, Host, VexBindingError, VexBudgetError

W = 1000
ZEROS = ",".join(["0"] * W)
PAIRS = ",".join(f"{i}: 0" for i in range(W))
MANY_FIELDS = "struct S { " + ", ".join(f"f{i}: Int" for i in range(W)) + " }"
INIT_FIELDS = ", ".join(f"f{i}: 0" for i in range(W))
MANY_LETS = " ".join(f"let a{i} = 0;" for i in range(W))
LARGE = f'let s = "x".repeat({W}); let t = "x".repeat({W}); let ys = (0..{W}).to_list(); let zs = (0..{W}).to_list();'
KEYED = f'let k = "a".repeat({W}); let k2 = "a".repeat({W}); let mut m: Map[Str, Int] = #{{k: 1}};'

# opcode name -> (top-level declarations, statements before the op, statements under test, minimum units).
CHARGED: dict[str, tuple[str, str, str, int]] = {
    "MAKE_LIST": ("", "", f"let _a = [{ZEROS}];", W),
    "MAKE_TUPLE": ("", "", f"let _a = ({ZEROS});", W),
    "MAKE_MAP": ("", "", "let _a = #{" + PAIRS + "};", 2 * W),
    "MAKE_ADT": (MANY_FIELDS, "", "let _a = S { " + INIT_FIELDS + " };", W),
    "MAKE_CLOSURE": ("", "let c0 = 0; let c1 = 0; let c2 = 0;", "let f = |x: Int| c0 + c1 + c2 + x;", 3),
    "MAKE_CELL": ("", "", "let mut c = 0; let f = |x: Int| { c = x; 0 };", 1),
    "BUILD_STR": ("", 'let e = "";', 'let _a = "' + "{e}" * W + '";', W),
    "CALL_FN": ("fn wide() -> Int { " + MANY_LETS + " 0 }", "", "wide();", W),
    "CALL": ("", "let f = |x: Int| { " + MANY_LETS + " x };", "f(1);", W),
    "CALL_BI": ("", "let xs = [1];", "xs.len();" * W, W),
    "CALL_BIV": ("", 'let s = "a";', "s.upper();" * W, W),
    "ADD_S": ("", LARGE, "let _a = s + t;", 2 * W),
    "EQ": ("", LARGE, "let _a = s == t;", W),
    "NE": ("", LARGE, "let _a = s != t;", W),
    "LT": ("", LARGE, "let _a = s < t;", W),
    "LE": ("", LARGE, "let _a = s <= t;", W),
    "GT": ("", LARGE, "let _a = s > t;", W),
    "GE": ("", LARGE, "let _a = s >= t;", W),
    "INDEX": ("", KEYED, "let _a = m[k2];", W),
    "SET_INDEX": ("", KEYED, "m[k2] = 1;", W),
}

CONSTANT: dict[str, str] = {
    **dict.fromkeys(
        [
            "CONST",
            "LOAD",
            "STORE",
            "LOAD_CELL",
            "STORE_CELL",
            "LOAD_GLOBAL",
            "STORE_GLOBAL",
            "POP",
            "DUP",
            "DUP2",
        ],
        "O(1) stack or slot move",
    ),
    **dict.fromkeys(
        [
            "ADD_I",
            "SUB_I",
            "MUL_I",
            "DIV_I",
            "MOD_I",
            "NEG_I",
            "ADD_F",
            "SUB_F",
            "MUL_F",
            "DIV_F",
            "MOD_F",
            "NEG_F",
        ],
        "64-bit or float scalar",
    ),
    "NOT": "scalar",
    **dict.fromkeys(
        ["JMP", "JMP_F", "JMP_T", "JMP_F_KEEP", "JMP_T_KEEP"], "O(1); a back-edge is charged one unit"
    ),
    "RET": "O(1)",
    **dict.fromkeys(["GET_FIELD", "SET_FIELD", "GET_TUPLE", "TAG_IS", "TRY"], "O(1)"),
    **dict.fromkeys(["RANGE", "RANGE_INCL"], "a range object is constant size"),
    **dict.fromkeys(["ITER", "FOR_ITER"], "O(1) per step, charged by the loop back-edge"),
    "PANIC": "ends the script",
    **dict.fromkeys(
        ["ADDK_L", "SUBK_L", "LT_LK", "LE_LK", "GT_LK", "GE_LK", "EQ_LK", "NE_LK"],
        "local Int and an Int constant",
    ),
}


def test_every_opcode_is_in_the_audit() -> None:
    assert set(bc.NAMES) == set(CHARGED) | set(CONSTANT)
    assert not set(CHARGED) & set(CONSTANT)


def program(decls: str, prelude: str, body: str) -> Host:
    h = Host(CAP_ALL)
    h.budget(100_000_000)
    h.load("r.vx", f"{decls}\nfn go() {{ {prelude} {body} 0 }}")
    return h


def spent_by(h: Host) -> int:
    h.call("go", [])
    return 100_000_000 - h.fuel_left()


@pytest.mark.parametrize("name", list(CHARGED))
def test_opcode_is_charged_for_what_it_builds(name: str) -> None:
    decls, prelude, body, minimum = CHARGED[name]
    h = program(decls, prelude, body)
    code = h._program.env.fns["go"].code  # type: ignore[union-attr]
    ops = {op for c in bc.all_code([code]) for op, _ in c.instrs}
    assert getattr(bc, name) in ops, f"this program does not exercise {name}"
    extra = spent_by(h) - spent_by(program(decls, prelude, ""))
    assert extra >= minimum, f"{name} charged {extra} units for work of {minimum}"


def test_a_wide_literal_in_a_loop_stops_at_the_budget() -> None:
    h = Host()
    h.budget(200_000)
    h.load("r.vx", "fn go() { while true { let _a = [" + ZEROS + "]; } 0 }")
    with pytest.raises(VexBudgetError):
        h.call("go", [])


def test_a_retained_wide_literal_uses_memory_only_as_budget_allows() -> None:
    h = Host()
    h.budget(20_000)
    h.load("r.vx", "fn go() { let mut xs = []; for i in 0..1000 { xs.push([" + ZEROS + "]); } xs.len() }")
    tracemalloc.start()
    with pytest.raises(VexBudgetError):
        h.call("go", [])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 1_000_000, f"20,000 units bought {peak} bytes"


# The most memory a unit can retain. Each builder keeps every value it makes for the whole loop.
RATE_LIMIT = 40.0

NONLATIN = '"\\u{4e2d}"'
RETAINING = [
    ("int-values", "xs.push(i + 1000);", 3000),
    ("float-values", "xs.push(i.to_float());", 3000),
    ("wide-list", "xs.push([" + ZEROS + "]);", 200),
    ("wide-tuple", "xs.push((" + ZEROS + "));", 200),
    ("empty-list", "xs.push([1]);", 3000),
    ("pair-tuple", "xs.push((i, i));", 3000),
    ("some-int", "xs.push(Some(i));", 3000),
    ("first-of-list", "let ys = [i]; xs.push(ys.first());", 3000),
    ("get-of-map", "let m2 = #{1: i}; xs.push(m2.get(1));", 2000),
    ("map-one-pair", "xs.push(#{1: i});", 2000),
    ("map-empty", "let mut m3: Map[Int, Int] = #{}; xs.push(m3);", 3000),
    ("closure", "let j = i; xs.push(|x: Int| x + j);", 3000),
    ("int-to-str", "xs.push(i.to_str());", 3000),
    ("enumerate", "xs.extend([i, i, i].enumerate());", 2000),
    ("zip", "xs.extend([i, i, i].zip([i, i, i]));", 2000),
    ("chars-non-latin", f"xs.extend({NONLATIN}.repeat(5).chars());", 500),
    ("split-into-short-strings", 'xs.extend("ab,ab,ab".split(","));', 2000),
]


@pytest.mark.parametrize(("name", "op", "rounds"), RETAINING, ids=[r[0] for r in RETAINING])
def test_memory_retained_per_unit_is_bounded(name: str, op: str, rounds: int) -> None:
    h = Host()
    h.budget(1_000_000_000)
    src = f"fn go() -> Int {{ let mut xs = []; for i in 0..{rounds} {{ {op} }} xs.len() }}"
    h.load("r.vx", src)
    tracemalloc.start()
    h.call("go", [])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    units = 1_000_000_000 - h.fuel_left()
    rate = peak / units
    assert rate <= RATE_LIMIT, f"{name}: {peak} bytes over {units} units is {rate:.1f} bytes per unit"


def test_map_growth_stays_within_the_rate() -> None:
    h = Host()
    h.budget(1_000_000_000)
    h.load(
        "r.vx",
        "fn go() -> Int { let mut m: Map[Int, Int] = #{}; for i in 1000..21000 { m.insert(i, i); } m.len() }",
    )
    tracemalloc.start()
    h.call("go", [])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak / (1_000_000_000 - h.fuel_left()) <= RATE_LIMIT


def test_the_default_budget_keeps_memory_modest_and_recursion_within_its_cap() -> None:
    assert DEFAULT_BUDGET == 5_000_000
    h = Host()  # the default budget: nested calls stop at MAX_DEPTH or the budget, whichever is first
    h.load("r.vx", "fn r(n: Int) -> Int { r(n + 1) + 1 }")
    tracemalloc.start()
    with pytest.raises(VexRuntimeError):
        h.call("r", [0])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 40_000_000, f"recursion to the cap traced {peak} bytes"


EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.vx")), ids=lambda p: p.stem)
def test_normal_examples_run_well_within_the_default_budget(path: pathlib.Path) -> None:
    class Sink:
        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            pass

    h = Host(CAP_ALL, out=Sink())  # type: ignore[arg-type]
    try:
        h.load(str(path), path.read_text(encoding="utf-8"))
    except Exception as e:  # an example that needs `import` or `exit` cannot run embedded
        pytest.skip(str(e)[:80])
    try:
        h.call("main", [])
    except VexBudgetError as e:
        pytest.fail(f"{path.name} exhausted the default budget: {e.message}")
    except (VexRuntimeError, VexBindingError) as e:
        pytest.skip(str(e)[:80])  # needs stdin or arguments the host did not give it
    used: Any = DEFAULT_BUDGET - h.fuel_left()
    assert used < DEFAULT_BUDGET / 4, f"{path.name} used {used} units"
