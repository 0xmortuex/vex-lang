"""Every builtin and data-sized opcode, audited against the execution budget.

An op's real cost must be charged while it happens, including set-up that a short-circuiting callback would otherwise hide. The audit
loop is bounded, so an uncharged op finishes and the test fails instead of hanging: each op below scales with `n`, 200 iterations would
cost 4,000,000 units, and the budget is 1,000,000.
"""

from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from vex.host import CAP_ALL, Host, VexBudgetError
from vex.stdlib import GLOBALS, METHODS, NAMESPACES

N = 20_000
BUDGET = 1_000_000
ROUNDS = 200

LIST = "let mut xs = (0..n).to_list(); let ys = (0..n).to_list();"
FLOATS = "let fs = (0..n).to_list().map(|x| x.to_float());"
STRS = 'let s = "ab".repeat(n / 2); let t = "ab".repeat(n / 2);'
TEXT = 'let s = "ab\\n".repeat(n / 3);'
PARTS = 'let parts = "a,".repeat(n / 2).split(",");'
MAP = "let mut m: Map[Int, Int] = #{}; for i in 0..n { m.insert(i, i); }"
# Two equal keys that are different objects: the lookup must compare them, character by character.
KEYED = 'let k = "a".repeat(n); let k2 = "a".repeat(n); let mut m: Map[Str, Int] = #{k: 1};'
TUPLE_KEYED = 'let k = ("a".repeat(n), 1); let k2 = ("a".repeat(n), 1); let mut m: Map[(Str, Int), Int] = #{k: 1};'


# builtin name -> (setup, one iteration of the loop). The cost of each op scales with `n`.
SCALED: dict[str, tuple[str, str]] = {
    "print": (STRS, "print(s);"),
    "eprint": (STRS, "eprint(s);"),
    "assert_eq": (STRS, "assert_eq(s, t);"),
    "Str.upper": (STRS, "s.upper();"),
    "Str.lower": (STRS, "s.lower();"),
    "Str.trim": (STRS, "s.trim();"),
    "Str.contains": (STRS, 's.contains("z");'),
    "Str.starts_with": (STRS, "s.starts_with(t);"),
    "Str.ends_with": (STRS, "s.ends_with(t);"),
    "Str.replace": (STRS, 's.replace("z", "y");'),
    "Str.split": (STRS, 's.split("a");'),
    "Str.lines": (TEXT, "s.lines();"),
    "Str.chars": (STRS, "s.chars();"),
    "Str.repeat": (STRS, "s.repeat(2);"),
    "Str.to_int": (STRS, "s.to_int();"),
    "Str.to_float": (STRS, "s.to_float();"),
    "Str.slice": (STRS, "s.slice(0, n);"),
    "Str.index_of": (STRS, 's.index_of("z");'),
    "List.insert": (LIST, "xs.insert(0, 1); xs.remove_at(0);"),
    "List.remove_at": (LIST, "let v = xs.remove_at(0); xs.push(v);"),
    "List.extend": (LIST, "xs.extend(ys); let mut z = ys.copy();"),
    "List.reverse": (LIST, "xs.reverse();"),
    "List.sort": (LIST, "xs.sort();"),
    "List.sorted": (LIST, "xs.sorted();"),
    "List.contains": (LIST, "xs.contains(-1);"),
    "List.index_of": (LIST, "xs.index_of(-1);"),
    "List.slice": (LIST, "xs.slice(0, n);"),
    "List.concat": (LIST, "xs.concat(ys);"),
    "List.copy": (LIST, "xs.copy();"),
    "List.map": (LIST, "xs.map(|x| x);"),
    "List.filter": (LIST, "xs.filter(|x| false);"),
    "List.fold": (LIST, "xs.fold(0, |a, x| a);"),
    "List.any": (LIST, "xs.any(|x| true);"),
    "List.all": (LIST, "xs.all(|x| false);"),
    "List.find": (LIST, "xs.find(|x| false);"),
    "List.for_each": (LIST, "xs.for_each(|x| { });"),
    "List.enumerate": (LIST, "xs.enumerate();"),
    "List.zip": (LIST, "xs.zip(ys);"),
    "List.sum": (LIST, "xs.sum();"),
    "List.sum_float": (FLOATS, "fs.sum_float();"),
    "List.join": (PARTS, 'parts.join("-");'),
    "Map.get": (KEYED, "m.get(k2);"),
    "Map.insert": (KEYED, "m.insert(k2, 1);"),
    "Map.remove": (KEYED, "m.remove(k2); m.insert(k, 1);"),
    "Map.contains_key": (KEYED, "m.contains_key(k2);"),
    "Map.keys": (MAP, "m.keys();"),
    "Map.values": (MAP, "m.values();"),
    "Map.entries": (MAP, "m.entries();"),
    "Range.to_list": ("", "(0..n).to_list();"),
    "env.get": (STRS, "env.get(s);"),
}

# Each of these does a bounded amount of work per call, ends the script, or runs one callback already charged by `vm.call`.
CONSTANT: dict[str, str] = {
    "panic": "ends the script",
    "assert": "constant",
    "exit": "ends the script",
    **dict.fromkeys(
        ["Int.abs", "Int.pow", "Int.min", "Int.max", "Int.signum", "Int.to_float", "Int.to_str", "Int.to_char",
         "Float.abs", "Float.floor", "Float.ceil", "Float.round", "Float.sqrt", "Float.min", "Float.max", "Float.is_nan",
         "Float.to_int", "Float.to_str", "Bool.to_str"],
        "scalar input, bounded result",
    ),  # fmt: skip
    **dict.fromkeys(["Str.len", "Str.is_empty", "Str.char_at", "Str.code", "Str.to_str"], "O(1)"),
    **dict.fromkeys(["List.len", "List.is_empty", "List.push", "List.pop", "List.get", "List.first", "List.last"], "O(1), amortised"),
    **dict.fromkeys(["List.clear", "Map.clear"], "frees only what was already charged when it was built"),
    **dict.fromkeys(["Map.len", "Map.is_empty", "Range.len", "Range.contains"], "O(1)"),
    **dict.fromkeys(
        ["Option.is_some", "Option.is_none", "Option.unwrap", "Option.expect", "Option.unwrap_or", "Option.ok_or",
         "Result.is_ok", "Result.is_err", "Result.unwrap_or", "Result.ok"],
        "O(1)",
    ),  # fmt: skip
    **dict.fromkeys(
        ["Option.map", "Option.and_then", "Result.map", "Result.map_err", "Result.and_then"],
        "one callback, charged by vm.call",
    ),
    **dict.fromkeys(["Result.unwrap", "Result.expect", "Result.unwrap_err"], "only formats on the path that ends the script"),
    **dict.fromkeys(
        ["math.sqrt", "math.sin", "math.cos", "math.tan", "math.atan", "math.exp", "math.log10", "math.ln", "math.pow",
         "math.atan2", "math.gcd", "math.hypot"],
        "scalar input, bounded result",
    ),  # fmt: skip
    "env.args": "bounded by the host's own arguments; empty when embedded",
}

# Builtins whose fixtures need the file system or stdin; covered by their own tests below.
IO_TESTED = {"io.read_file", "io.write_file", "io.read_line"}


def all_builtins() -> set[str]:
    names = {b.qual for b in GLOBALS.values()}
    for table in (*METHODS.values(), *NAMESPACES.values()):
        names |= {b.qual for b in table.values() if not b.is_const}
    return names


def test_every_builtin_is_in_the_audit() -> None:
    audited = set(SCALED) | set(CONSTANT) | IO_TESTED
    assert all_builtins() == audited


def loop_host(*bindings: tuple[str, str, Any]) -> Host:
    h = Host(CAP_ALL, out=open(os.devnull, "w", encoding="utf-8"))
    h.budget(BUDGET)
    for name, sig, fn in bindings:
        h.bind(name, sig, fn)
    return h


def assert_charged(setup: str, op: str, *bindings: tuple[str, str, Any]) -> None:
    h = loop_host(*bindings)
    h.load("r.vx", f"fn go() {{ let n = {N}; {setup} for _ in 0..{ROUNDS} {{ {op} }} 0 }}")
    with pytest.raises(VexBudgetError) as e:
        h.call("go", [])
    assert "r.vx:" in e.value.render()


@pytest.mark.parametrize("name", list(SCALED))
def test_data_sized_builtin_is_charged(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    setup, op = SCALED[name]
    assert_charged(setup, op)


OPS = [
    ("index-map", KEYED, "m[k2];"),
    ("set-index-map", KEYED, "m[k2] = 1;"),
    ("map-literal-key", KEYED, "#{k2: 1};"),
    ("index-map-tuple-key", TUPLE_KEYED, "m[k2];"),
    ("map-literal-tuple-key", TUPLE_KEYED, "#{k2: 1};"),
    ("interpolate-list", LIST, '"{xs}".len();'),
    ("interpolate-str", STRS, '"{s}".len();'),
]


@pytest.mark.parametrize(("name", "setup", "op"), OPS, ids=[o[0] for o in OPS])
def test_data_sized_opcode_is_charged(name: str, setup: str, op: str) -> None:
    assert_charged(setup, op)


BIG_RESULT = "x" * N


def test_str_arguments_to_a_host_function_are_charged() -> None:
    assert_charged(STRS, "host.sink(s);", ("sink", "(Str)", lambda s: None))


def test_str_results_from_a_host_function_are_charged() -> None:
    assert_charged("", "host.big();", ("big", "() -> Str", lambda: BIG_RESULT))


def test_a_returned_str_crossing_a_reentrant_call_is_charged() -> None:
    h = Host()
    h.budget(BUDGET)
    h.bind("again", "()", lambda: h.call("echo", [BIG_RESULT]) and None)
    h.load("r.vx", f"fn echo(s: Str) {{ s }}\nfn go() {{ for _ in 0..{ROUNDS} {{ host.again(); }} }}")
    with pytest.raises(VexBudgetError):
        h.call("go", [])


@pytest.fixture
def text_file(tmp_path: Path) -> str:
    path = tmp_path / "big.txt"
    path.write_text("x" * N, encoding="utf-8")
    return str(path).replace("\\", "/")


def test_read_file_is_charged(text_file: str) -> None:
    assert_charged("", f'io.read_file("{text_file}").unwrap();')


def test_write_file_is_charged(text_file: str) -> None:
    assert_charged(STRS, f'io.write_file("{text_file}", s).unwrap();')


def test_read_line_is_charged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(("x" * N + "\n") * (ROUNDS + 2)))
    assert_charged("", "io.read_line().unwrap();")


def test_read_file_stops_while_reading_a_large_file(tmp_path: Path) -> None:
    path = tmp_path / "huge.txt"
    path.write_text("x" * 2_000_000, encoding="utf-8")
    h = Host(CAP_ALL)
    h.budget(100_000)
    h.load("r.vx", f'fn go() {{ io.read_file("{str(path).replace(chr(92), "/")}").unwrap().len() }}')
    with pytest.raises(VexBudgetError):
        h.call("go", [])


def test_read_line_keeps_a_long_line_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("y" * 100_000 + "\nsecond\n"))
    h = Host(CAP_ALL)
    h.load("r.vx", "fn go() -> Int { io.read_line().unwrap().len() }\nfn second() { io.read_line().unwrap() }")
    assert h.call("go", []) == 100_000
    assert h.call("second", []) == "second"


# The exact reproducers from the failed verification, as wall-clock checks at their original sizes.
@pytest.mark.parametrize(
    "op",
    [
        "let _a = xs.any(|x| true);",
        "let _a = xs.all(|x| false);",
        "let _a = xs.map(|x| x);",
        "xs.for_each(|x| { });",
    ],
    ids=["any", "all", "map", "for-each"],
)
def test_short_circuit_setup_is_stopped_at_the_budget(op: str) -> None:
    h = Host()
    h.budget(1_000_000)
    h.load("r.vx", "fn go() -> Int { let mut xs = (0..200000).to_list(); while true { " + op + " } 0 }")
    start = time.monotonic()
    with pytest.raises(VexBudgetError):
        h.call("go", [])
    assert time.monotonic() - start < 5.0


def test_find_still_stops_at_its_first_match_for_free_setup() -> None:
    """Early exit from `find` costs only the elements visited: it copies nothing."""
    h = Host()
    h.budget(50_000)
    h.load("r.vx", "fn go() { let xs = (0..40000).to_list(); xs.find(|x| true).unwrap() }")
    assert h.call("go", []) == 0
