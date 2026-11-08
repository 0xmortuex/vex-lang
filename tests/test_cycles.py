"""Self-referencing values must end in a defined runtime error, in the CLI and when embedded, without hanging."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from vex.diagnostics import VexError, VexRuntimeError
from vex.host import CAP_ALL, Host, VexBudgetError

SRC = Path(__file__).resolve().parent.parent / "src"
NODE = "struct N { v: Int, next: Option[N] }\n"
TREE = "struct K { kids: List[K] }\n"
MAPPED = "struct M { kids: Map[Int, M] }\n"
LOOP = "fn loop(v: Int) -> N { let mut a = N { v: v, next: None }; a.next = Some(a); a }\n"


def host(src: str) -> Host:
    h = Host(CAP_ALL, out=open(os.devnull, "w", encoding="utf-8"))
    h.budget(100_000)
    h.load("r.vx", src)
    return h


CYCLIC_DISPLAY = [
    ("interpolate", NODE + LOOP + 'fn go() { "{loop(1)}".len() }'),
    ("print", NODE + LOOP + "fn go() { print(loop(1)); }"),
    ("list-of-cycle", NODE + LOOP + 'fn go() { let a = loop(1); "{[1, 2, 3]}{[a]}".len() }'),
    ("through-list", TREE + 'fn go() { let mut a = K { kids: [] }; a.kids.push(a); "{a}".len() }'),
    ("through-map", MAPPED + 'fn go() { let mut a = M { kids: #{} }; a.kids.insert(1, a); "{a}".len() }'),
    ("err-unwrap", NODE + LOOP + "fn go() -> Int { let r: Result[Int, N] = Err(loop(1)); r.unwrap() }"),
]


@pytest.mark.parametrize(("name", "src"), CYCLIC_DISPLAY, ids=[d[0] for d in CYCLIC_DISPLAY])
def test_display_of_a_cycle_is_a_located_runtime_error(name: str, src: str) -> None:
    h = host(src)
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [])
    assert not isinstance(e.value, VexBudgetError)
    assert "contains itself" in e.value.message
    assert "r.vx:" in e.value.render()


CYCLIC_EQUAL = [
    ("eq", NODE + LOOP + "fn go() { if loop(1) == loop(1) { 1 } else { 0 } }"),
    ("ne", NODE + LOOP + "fn go() { if loop(1) != loop(1) { 1 } else { 0 } }"),
    ("assert-eq", NODE + LOOP + "fn go() { assert_eq(loop(1), loop(1)); 1 }"),
    ("contains", NODE + LOOP + "fn go() { if [loop(1)].contains(loop(1)) { 1 } else { 0 } }"),
    ("index-of", NODE + LOOP + "fn go() { [loop(1)].index_of(loop(1)).unwrap_or(0) }"),
    (
        "via-list",
        TREE
        + "fn k() -> K { let mut a = K { kids: [] }; a.kids.push(a); a }\nfn go() { if k() == k() { 1 } else { 0 } }",
    ),
]


@pytest.mark.parametrize(("name", "src"), CYCLIC_EQUAL, ids=[d[0] for d in CYCLIC_EQUAL])
def test_equality_of_cycles_is_a_located_runtime_error(name: str, src: str) -> None:
    h = host(src)
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [])
    assert not isinstance(e.value, VexBudgetError)
    assert "contain themselves" in e.value.message
    assert "r.vx:" in e.value.render()


def test_equality_of_different_cycles_of_different_length_terminates() -> None:
    src = (
        NODE
        + LOOP
        + (
            "fn two() -> N { let mut a = N { v: 1, next: None }; let mut b = N { v: 1, next: Some(a) }; a.next = Some(b); a }\n"
            "fn go() { if loop(1) == two() { 1 } else { 0 } }"
        )
    )
    with pytest.raises(VexRuntimeError):
        host(src).call("go", [])


def test_equality_stops_at_a_difference_before_reaching_the_cycle() -> None:
    src = NODE + LOOP + "fn go() { if loop(1) == loop(2) { 1 } else { 0 } }"
    assert host(src).call("go", []) == 0


def test_comparing_a_cycle_with_itself_is_true() -> None:
    src = NODE + LOOP + "fn go() { let a = loop(1); if a == a { 1 } else { 0 } }"
    assert host(src).call("go", []) == 1


@pytest.mark.parametrize(
    "src",
    [
        "struct T { v: Int }\nfn go() { let t = T { v: 1 }; let a = [t, t]; let b = [t, t]; if a == b { 1 } else { 0 } }",
        "enum E { L, B(E, E) }\nfn go() { let a = B(L, L); let c = B(a, a); let d = B(a, a); if c == d { 1 } else { 0 } }",
        'enum E { L, B(E, E) }\nfn go() { let a = B(L, L); let c = B(a, a); if "{c}" == "{c}" { 1 } else { 0 } }',
    ],
    ids=["shared-list", "shared-tree-equal", "shared-tree-display"],
)
def test_shared_substructure_is_not_mistaken_for_a_cycle(src: str) -> None:
    assert host(src).call("go", []) == 1


@pytest.mark.parametrize(
    "src",
    [
        "struct N { v: Int }\nfn go() { let m: Map[N, Int] = #{}; m.len() }",
        "struct N { v: Int }\nfn go() { let a = N { v: 1 }; if a < a { 1 } else { 0 } }",
        "struct N { v: Int }\nfn go() { let mut xs = [N { v: 1 }]; xs.sort(); 1 }",
        "fn go() { let mut xs = []; xs.push(xs); 1 }",
    ],
    ids=["map-key", "order", "sort", "self-containing-list"],
)
def test_hash_and_order_cannot_reach_a_cycle_because_the_checker_rejects_them(src: str) -> None:
    with pytest.raises(VexError):
        Host().load("r.vx", src)


def run_cli(tmp_path: Path, src: str) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "cyc.vx"
    script.write_text(src, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "vex", "run", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=20,
    )


def test_cli_display_of_a_cycle_exits_with_a_runtime_error(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path, NODE + "fn main() { let mut a = N { v: 1, next: None }; a.next = Some(a); print(a); }"
    )
    assert result.returncode == 2
    assert "contains itself" in result.stderr
    assert "cyc.vx:" in result.stderr


def test_cli_equality_of_cycles_exits_with_a_runtime_error(tmp_path: Path) -> None:
    result = run_cli(tmp_path, NODE + LOOP + "fn main() { if loop(1) == loop(1) { print(1); } }")
    assert result.returncode == 2
    assert "contain themselves" in result.stderr


def test_cli_prints_a_deep_but_finite_value(tmp_path: Path) -> None:
    src = 'enum L { Nil, Cons(L) }\nfn main() { let mut a = Nil; for i in 0..100000 { a = Cons(a); } print(1); print("{a}".len()); }'
    result = run_cli(tmp_path, src)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["1", str(3 + 100000 * 5 + 100000)]


def test_repl_echoing_a_cycle_reports_a_runtime_error() -> None:
    import io

    from vex.repl import Session

    out = io.StringIO()
    s = Session(out)
    s.handle(NODE.strip())
    s.handle("let mut a = N { v: 1, next: None };")
    s.handle("a.next = Some(a);")
    s.handle("a")
    assert "contains itself" in out.getvalue()
