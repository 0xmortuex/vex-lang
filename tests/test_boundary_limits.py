"""Hardening of the embedding boundary: nothing escapes it, and work proportional to data is budgeted."""

from __future__ import annotations

import ctypes
import math
import signal
import sys
import time
import tracemalloc
from collections.abc import Callable
from typing import Any

import pytest
from test_embed import BINDING, COMPILE, OK, RUNTIME, Api, make_args

from vex import embed
from vex.diagnostics import VexRuntimeError
from vex.embed import HOST_FN, VexValue
from vex.host import MAX_HOST_DEPTH, Host, VexBindingError, VexBudgetError
from vex.runtime import NONE, Adt, AdtInfo, display, values_equal
from vex.vm import VM

TREE = "enum T { Leaf, Node(T, T) }\n"
BUILD = "fn tree(n: Int) -> T { let mut t = Leaf; for _ in 0..n { t = Node(t, t); } t }\n"


def stopped_quickly(src: str, budget: int, call: str = "go", limit: float = 3.0) -> None:
    h = Host()
    h.budget(budget)
    h.load("r.vx", src)
    start = time.monotonic()
    with pytest.raises(VexBudgetError) as e:
        h.call(call, [])
    assert time.monotonic() - start < limit
    assert "r.vx:" in e.value.render()


# ------------------------------------------------------------------ work scaling with data is charged
EXPONENTIAL = [
    ("equal-trees", "fn go() { if tree(24) == tree(24) { 1 } else { 0 } }"),
    ("not-equal-trees", "fn go() { if tree(24) != tree(24) { 1 } else { 0 } }"),
    ("assert-eq", "fn go() { assert_eq(tree(24), tree(24)); 1 }"),
    ("list-contains", "fn go() { if [tree(24)].contains(tree(24)) { 1 } else { 0 } }"),
    ("list-index-of", "fn go() { [tree(24)].index_of(tree(24)).unwrap_or(0) }"),
    ("interpolation", 'fn go() { "{tree(24)}".len() }'),
    ("map-equal", "fn go() { let a = #{1: tree(24)}; let b = #{1: tree(24)}; if a == b { 1 } else { 0 } }"),
    ("tuple-equal", "fn go() { if (1, tree(24)) == (1, tree(24)) { 1 } else { 0 } }"),
    ("list-equal", "fn go() { if [tree(24)] == [tree(24)] { 1 } else { 0 } }"),
    ("err-unwrap-text", "fn go() -> Int { let r: Result[Int, T] = Err(tree(24)); r.unwrap() }"),
    ("err-expect-text", 'fn go() -> Int { let r: Result[Int, T] = Err(tree(24)); r.expect("x") }'),
    ("ok-unwrap-err-text", "fn go() -> Int { let r: Result[T, Int] = Ok(tree(24)); r.unwrap_err() }"),
]


@pytest.mark.parametrize(("name", "body"), EXPONENTIAL, ids=[n for n, _ in EXPONENTIAL])
def test_structural_work_on_shared_trees_is_stopped(name: str, body: str) -> None:
    stopped_quickly(TREE + BUILD + body, 1000)


def test_print_of_a_shared_tree_is_stopped_in_bounded_memory() -> None:
    h = Host(1, out=open(__import__("os").devnull, "w", encoding="utf-8"))
    h.budget(1000)
    h.load("r.vx", TREE + BUILD + "fn go() { print(tree(24)); }")
    tracemalloc.start()
    with pytest.raises(VexBudgetError):
        h.call("go", [])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 5_000_000


def test_display_is_charged_while_it_builds() -> None:
    h = Host()
    h.budget(2000)
    h.load("r.vx", TREE + BUILD + 'fn go() { let t = tree(24); "{t}".len() }')
    tracemalloc.start()
    with pytest.raises(VexBudgetError):
        h.call("go", [])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 5_000_000


LINEAR = [
    ("list-contains-int", "let xs = (0..50000).to_list(); while true { xs.contains(-1); }"),
    ("list-index-of-int", "let xs = (0..50000).to_list(); while true { xs.index_of(-1); }"),
    ("list-sort", "let mut xs = (0..50000).to_list(); while true { xs.reverse(); xs.sort(); }"),
    ("list-sorted", "let xs = (0..50000).to_list(); while true { xs.sorted(); }"),
    ("list-sum", "let xs = (0..50000).to_list(); while true { xs.sum(); }"),
    ("list-insert", "let mut xs = (0..50000).to_list(); while true { xs.insert(0, 1); xs.remove_at(0); }"),
    ("list-sum-float", "let xs = (0..50000).to_list().map(|x| x.to_float()); while true { xs.sum_float(); }"),
    ("str-eq", 'let a = "x".repeat(40000); let b = "x".repeat(40000); while true { a == b; }'),
    ("str-ne", 'let a = "x".repeat(40000); let b = "x".repeat(40000); while true { a != b; }'),
    ("str-lt", 'let a = "x".repeat(40000); let b = "x".repeat(40000); while true { a < b; }'),
    ("str-contains", 'let a = "x".repeat(40000); while true { a.contains("y"); }'),
    ("str-starts", 'let a = "x".repeat(40000); while true { a.starts_with(a); }'),
    ("str-ends", 'let a = "x".repeat(40000); while true { a.ends_with(a); }'),
    ("str-index-of", 'let a = "x".repeat(40000); while true { a.index_of("y"); }'),
    ("str-upper", 'let a = "x".repeat(40000); while true { a.upper(); }'),
    ("str-trim", 'let a = "x".repeat(40000); while true { a.trim(); }'),
    ("str-chars", 'let a = "x".repeat(40000); while true { a.chars(); }'),
    ("str-split", 'let a = "x,".repeat(20000); while true { a.split(","); }'),
    ("str-lines", 'let a = "x\\n".repeat(20000); while true { a.lines(); }'),
    ("str-slice", 'let a = "x".repeat(40000); while true { a.slice(0, 40000); }'),
    ("str-to-float", 'let a = "1".repeat(40000); while true { a.to_float(); }'),
    ("str-to-int", 'let a = " ".repeat(40000); while true { a.to_int(); }'),
    ("str-replace", 'let a = "x".repeat(40000); while true { a.replace("y", "z"); }'),
    ("list-of-str-sort", 'let xs = ["a".repeat(20000), "b".repeat(20000)]; while true { xs.sorted(); }'),
    ("map-keys", "let mut m = #{0: 0}; for i in 0..20000 { m.insert(i, i); } while true { m.keys(); }"),
    ("map-entries", "let mut m = #{0: 0}; for i in 0..20000 { m.insert(i, i); } while true { m.entries(); }"),
    ("list-compare", "let a = (0..50000).to_list(); let b = (0..50000).to_list(); while true { a == b; }"),
    ("list-order", "let a = [(0..50000).to_list()]; let b = [(0..50000).to_list()]; while true { a[0] < b[0]; }"),
]


@pytest.mark.parametrize(("name", "body"), LINEAR, ids=[n for n, _ in LINEAR])
def test_linear_work_is_charged(name: str, body: str) -> None:
    stopped_quickly("fn go() { " + body + " }", 300_000, limit=5.0)


def test_quadratic_float_parsing_is_gone() -> None:
    h = Host()
    h.load("r.vx", 'fn go() { "1".repeat(200000).to_float().is_some() }')
    start = time.monotonic()
    assert h.call("go", []) is True
    assert time.monotonic() - start < 2.0


# ------------------------------------------------------------------ equality and display keep their meaning
INFO = AdtInfo("E", ["A", "B"])
OTHER = AdtInfo("F", ["A", "B"])
nan = math.nan


def adt(tag: int, *fields: Any) -> Adt:
    return Adt(INFO, tag, list(fields))


SAMPLES = [
    1, 1.5, nan, True, "a", "b", None, [1, 2], [1, 3], [1], (1, 2), (1, "a"), {1: 2}, {1: 3}, {2: 2},
    adt(0), adt(1), adt(0, 1), adt(0, adt(1, [1, "x"])), adt(0, adt(1, [1, "y"])), Adt(OTHER, 0, []), [nan], (nan,),
]  # fmt: skip


@pytest.mark.parametrize("a", SAMPLES, ids=[repr(s) for s in SAMPLES])
def test_values_equal_matches_python_equality(a: Any) -> None:
    for b in SAMPLES:
        if type(a) is type(b):
            assert values_equal(a, b) == (a == b), (a, b)


def test_shared_nan_compares_like_python() -> None:
    x = [nan]
    assert values_equal([x[0]], x) == ([x[0]] == x)
    assert values_equal(adt(0, x[0]), adt(0, x[0])) == (adt(0, x[0]) == adt(0, x[0]))


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (adt(0, 1, "a"), 'A(1, "a")'),
        ([1, (2.5, True), {"k": None}], '[1, (2.5, true), {"k": ()}]'),
        ((1,), "(1,)"),
        (adt(1), "B"),
        (NONE, "None"),
        (Adt(AdtInfo("P", ["P"], [["x", "y"]]), 0, [1, "s"]), 'P { x: 1, y: "s" }'),
        (Adt(AdtInfo("Q", ["Q"], [[]]), 0, []), "Q"),
        (range(1, 4), "1..4"),
        ("top", "top"),
    ],
)
def test_display_keeps_its_formats(value: Any, text: str) -> None:
    assert display(value) == text


def test_display_quotes_nested_strings_only() -> None:
    assert display(["a"], False) == '["a"]' and display("a", False) == '"a"'


# ------------------------------------------------------------------ depth: the boundary keeps its headroom
@pytest.mark.parametrize("n", [50000])
def test_deep_equal_and_display_do_not_recurse(n: int) -> None:
    src = (
        "enum L { Nil, Cons(L) }\n"
        f"fn build() -> L {{ let mut a = Nil; for _ in 0..{n} {{ a = Cons(a); }} a }}\n"
        'fn eq() { if build() == build() { 1 } else { 0 } }\n'
        'fn show() { "{build()}".len() }\n'
    )
    h = Host()
    h.load("r.vx", src)
    assert h.call("eq", []) == 1
    assert h.call("show", []) == 3 + n * 5 + n


def test_map_recursion_past_the_limit_is_a_located_error() -> None:
    h = Host()
    h.load("r.vx", "fn g(n: Int) -> Int {\n  [n].map(|x| g(x + 1))[0]\n}")
    with pytest.raises(VexRuntimeError) as e:
        h.call("g", [0])
    assert "too deep" in e.value.message
    assert "r.vx:2:" in e.value.render()


def test_embedded_internal_failure_is_located() -> None:
    h = Host()
    h.load("r.vx", "fn go() {\n  (math.INT_MIN..math.INT_MAX).len()\n}")
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [])
    assert "internal error" in e.value.message and "r.vx:2:" in e.value.render()


def test_surrogate_to_char_is_a_located_runtime_error() -> None:
    h = Host()
    h.load("r.vx", "fn go() {\n  (55296).to_char()\n}")
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [])
    assert "not a valid code point" in e.value.message and "r.vx:2:" in e.value.render()


def test_invalid_result_error_is_located_at_the_function() -> None:
    h = Host()
    h.load("r.vx", "fn go() -> Int {\n  math.gcd(math.INT_MIN, 0)\n}")
    with pytest.raises(VexRuntimeError) as e:
        h.call("go", [])
    assert "64-bit" in e.value.message and "r.vx:1:" in e.value.render()


def test_cross_instance_reentry_shares_one_depth_limit() -> None:
    a = Host()
    b = Host()
    deepest = [0]

    def hop_a(n: int) -> int:
        deepest[0] = max(deepest[0], n)
        r: int = b.call("bounce", [n + 1])
        return r

    def hop_b(n: int) -> int:
        deepest[0] = max(deepest[0], n)
        r: int = a.call("bounce", [n + 1])
        return r

    a.bind("next", "(Int) -> Int", hop_a)
    b.bind("next", "(Int) -> Int", hop_b)
    a.load("a.vx", "fn bounce(n: Int) { host.next(n) }")
    b.load("b.vx", "fn bounce(n: Int) { host.next(n) }")
    with pytest.raises(VexRuntimeError):
        a.call("bounce", [0])
    assert deepest[0] <= MAX_HOST_DEPTH


def test_reentry_past_the_limit_names_a_script_location() -> None:
    h = Host()
    rendered: list[str] = []

    def deep() -> int:
        try:
            r: int = h.call("f", [])
        except VexRuntimeError as e:
            rendered.append(e.render())
            raise
        return r

    h.bind("deep", "() -> Int", deep)
    h.load("d.vx", "fn f() -> Int {\n  host.deep()\n}")
    with pytest.raises(VexRuntimeError):
        h.call("f", [])
    assert "nested too deeply" in rendered[0] and "d.vx:1:" in rendered[0]


def test_reentry_past_the_limit_is_located_through_the_abi() -> None:
    api = Api()
    vm = api.open()
    errors: list[str] = []

    def deep(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        inner = VexValue()
        status = int(api.table.call(vm_, b"f", None, 0, ctypes.byref(inner)))
        if status != OK:
            errors.append(api.error(vm_))
            return status
        ret[0].tag, ret[0].i = 1, inner.i
        return 0

    cb = HOST_FN(deep)
    assert api.table.bind(vm, b"deep", b"() -> Int", None, cb) == OK
    assert api.load(vm, "fn f() -> Int {\n  host.deep()\n}") == OK
    assert api.call(vm, b"f", [])[0] == RUNTIME
    assert "nested too deeply" in errors[0]
    assert "rule.vx:1:" in errors[0]


def test_budget_stop_at_the_entry_of_a_reentrant_call_is_located() -> None:
    h = Host()
    rendered: list[str] = []

    def again() -> None:
        try:
            h.call("inner", [])
        except VexBudgetError as e:
            rendered.append(e.render())
            raise

    h.budget(2)  # the outer entry and the call to the host function; the inner entry cannot be paid for
    h.bind("again", "()", again)
    h.load("e.vx", "fn inner() { 1 }\nfn go() { host.again(); }")
    with pytest.raises(VexBudgetError):
        h.call("go", [])
    assert "e.vx:" in rendered[0]


def test_budget_stop_while_returning_a_str_is_located() -> None:
    h = Host()
    h.budget(3)
    h.load("e.vx", 'fn go() { "xxxxxxxxxx" }')
    with pytest.raises(VexBudgetError) as e:
        h.call("go", [])
    assert "e.vx:1:" in e.value.render()


def test_max_depth_is_counted_correctly_after_a_failed_chain() -> None:
    h = Host()
    h.bind("loop", "()", lambda: h.call("go", []))
    h.load("r.vx", "fn go() { host.loop(); }\nfn ok() { 1 }")
    with pytest.raises(VexRuntimeError):
        h.call("go", [])
    assert h.call("ok", []) == 1
    other = Host()
    other.load("o.vx", "fn ok() { 2 }")
    assert other.call("ok", []) == 2


def test_callback_entry_always_has_python_stack_headroom() -> None:
    """The worst case nesting of builtin callbacks and host hops stays well inside the recursion limit."""
    api = Api()
    worst = [0]
    api_table = api.table

    def re(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        k = argv[0].i
        depth = 0
        frame = sys._getframe()
        while frame is not None:
            depth += 1
            frame = frame.f_back  # type: ignore[assignment]
        worst[0] = max(worst[0], depth)
        if k <= 0:
            ret[0].tag, ret[0].i = 1, k
            return 0
        inner = VexValue()
        args = make_args([95, k - 1])
        status = api_table.call(vm_, b"g", args, 2, ctypes.byref(inner))
        ret[0].tag, ret[0].i = 1, k
        return 0 if status in (OK, RUNTIME) else int(status)

    vm = api.open()
    cb = HOST_FN(re)
    assert api_table.bind(vm, b"re", b"(Int) -> Int", None, cb) == OK
    src = "fn g(n: Int, k: Int) -> Int {\n  if n == 0 { host.re(k) } else { [n].map(|x| g(x - 1, k))[0] }\n}"
    assert api.load(vm, src) == OK
    status, _ = api.call(vm, b"g", [95, MAX_HOST_DEPTH + 3])
    assert status in (OK, RUNTIME)
    assert worst[0] + 64 <= sys.getrecursionlimit()


# ------------------------------------------------------------------ no exception escapes, even while reporting
EXCEPTIONS = [RecursionError, MemoryError, KeyboardInterrupt, SystemExit, UnicodeError, OSError]
ENTRIES: list[tuple[str, Callable[[], Any]]] = [
    ("open", lambda: embed._api_open(0)),
    ("close", lambda: embed._api_close(1)),
    ("bind", lambda: embed._api_bind(1, b"f", b"()", None, HOST_FN(0))),
    ("load", lambda: embed._api_load(1, b"p", 1, 0)),
    ("call", lambda: embed._api_call(1, b"f", None, 0, None)),
    ("budget", lambda: embed._api_budget(1, 1)),
    ("error", lambda: embed._api_error(1)),
    ("fail", lambda: embed._api_fail(1, b"x")),
    ("return_str", lambda: embed._api_return_str(1, None, None, 0)),
]


@pytest.mark.parametrize("exc", EXCEPTIONS, ids=[e.__name__ for e in EXCEPTIONS])
@pytest.mark.parametrize("attr", ["_instance", "_c_string", "_set_error", "_text"])
@pytest.mark.parametrize("entry", ENTRIES, ids=[n for n, _ in ENTRIES])
def test_entries_never_raise(
    entry: tuple[str, Callable[[], Any]], attr: str, exc: type[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise exc("boom")

    monkeypatch.setattr(embed, attr, boom)
    result = entry[1]()  # must return normally
    if entry[0] == "error":
        assert isinstance(result, int) and result != 0
    elif entry[0] in ("bind", "load", "call", "budget"):
        assert result in (COMPILE, RUNTIME, BINDING)


def test_reporting_failure_keeps_the_status_and_a_valid_message(monkeypatch: pytest.MonkeyPatch) -> None:
    api = Api()
    vm = api.open()
    api.load(vm, "fn f() { 1 / 0 }")
    real = embed._c_string
    calls = [0]

    def flaky(text: str) -> Any:
        calls[0] += 1
        if text:
            raise MemoryError("no room")
        return real(text)

    monkeypatch.setattr(embed, "_c_string", flaky)
    status, _ = api.call(vm, b"f", [])
    assert status == RUNTIME
    assert api.error(vm) != ""


def test_fail_reporting_failure_is_still_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    api = Api()
    vm = api.open()
    real = embed._instance

    def h(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        monkeypatch.setattr(embed, "_instance", lambda v: (_ for _ in ()).throw(RecursionError()))
        api.table.fail(vm_, b"lost")
        monkeypatch.setattr(embed, "_instance", real)
        return 1

    cb = HOST_FN(h)
    api.table.bind(vm, b"h", b"()", None, cb)
    api.load(vm, "fn f() { host.h(); }")
    assert api.call(vm, b"f", [])[0] == RUNTIME


def test_attach_replaces_a_default_sigint_handler() -> None:
    previous = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        Api()
        assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
        def custom(signum: int, frame: Any) -> None:
            return None

        signal.signal(signal.SIGINT, custom)
        Api()
        assert signal.getsignal(signal.SIGINT) is custom
    finally:
        signal.signal(signal.SIGINT, previous)


def test_bad_surrogate_in_host_args_is_status_3() -> None:
    api = Api()
    vm = api.open()
    inst = embed._instances[vm]
    tramp = embed._host_trampoline(inst, "f", None, lambda *a: 0)
    with pytest.raises(VexBindingError, match="host function `f`"):
        tramp("\ud800")


def test_values_equal_and_vm_equal_agree() -> None:
    vm = VM()
    assert vm.equal([1, adt(0)], [1, adt(0)]) is True
    assert vm.equal("a", "b") is False
