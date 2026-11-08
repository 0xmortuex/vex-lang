"""The Python side of the C ABI, driven the way a native host drives it: through the function pointers."""

from __future__ import annotations

import ctypes
from collections.abc import Callable, Iterator
from ctypes import addressof, byref, sizeof, string_at
from typing import Any

import pytest

from vex import embed
from vex.embed import HOST_FN, VexApi, VexValue
from vex.host import DEFAULT_BUDGET, MAX_HOST_DEPTH
from vex.stdlib import METHODS

OK, COMPILE, RUNTIME, BINDING, BUDGET = 0, 1, 2, 3, 4
UNIT, INT, FLOAT, BOOL, STR = 0, 1, 2, 3, 4

pending: list[Any] = []  # keeps argv strings alive for the duration of a test


class Api:
    """A convenience front for the table, standing in for a native host."""

    def __init__(self) -> None:
        self.table = VexApi()
        self.table.abi_version = 1
        embed.attach(addressof(self.table))
        assert self.table.ready == 1

    def open(self, caps: int = 0) -> int:
        vm = self.table.open(caps)
        assert vm
        return int(vm)

    def error(self, vm: int | None) -> str:
        addr = self.table.error(vm)
        assert addr
        return string_at(addr).decode("utf-8")

    def load(self, vm: int | None, src: str, path: bytes | None = b"rule.vx") -> int:
        data = src.encode("utf-8")
        return int(self.table.load(vm, path, data, len(data)))

    def call(self, vm: int | None, name: bytes | None, args: list[Any]) -> tuple[int, Any]:
        argv = make_args(args)
        ret = VexValue()
        status = self.table.call(vm, name, argv, len(args), byref(ret))
        return int(status), read(ret)

    def bind(self, vm: int, name: bytes, sig: bytes, fn: Callable[..., int], ctx: int = 0) -> int:
        cb = HOST_FN(fn)
        pending.append(cb)
        return int(self.table.bind(vm, name, sig, ctx or None, cb))


def make_args(args: list[Any]) -> Any:
    argv = (VexValue * max(len(args), 1))()
    for slot, a in zip(argv, args):
        if isinstance(a, VexValue):
            ctypes.memmove(byref(slot), byref(a), sizeof(VexValue))
        elif a is None:
            slot.tag = UNIT
        elif isinstance(a, bool):
            slot.tag, slot.i = BOOL, int(a)
        elif isinstance(a, int):
            slot.tag, slot.i = INT, a
        elif isinstance(a, float):
            slot.tag, slot.f = FLOAT, a
        else:
            data = a.encode("utf-8") if isinstance(a, str) else a
            buf = ctypes.create_string_buffer(data, len(data))
            pending.append(buf)
            slot.tag, slot.s, slot.len = STR, addressof(buf), len(data)
    return argv


def read(v: VexValue) -> Any:
    if v.tag == UNIT:
        return None
    if v.tag == INT:
        return v.i
    if v.tag == FLOAT:
        return v.f
    if v.tag == BOOL:
        return bool(v.i)
    assert v.tag == STR
    assert string_at(v.s + v.len, 1) == b"\0"  # NUL-terminated
    return string_at(v.s, v.len).decode("utf-8")


@pytest.fixture
def api() -> Iterator[Api]:
    yield Api()
    pending.clear()


# ------------------------------------------------------------------ layout and attach
def test_value_layout_is_the_abi() -> None:
    assert sizeof(VexValue) == 40
    assert [getattr(VexValue, f).offset for f in ("tag", "i", "f", "s", "len")] == [0, 8, 16, 24, 32]


def test_api_field_order_is_the_abi() -> None:
    order = ["abi_version", "ready", "open", "close", "bind", "load", "call", "budget", "error", "fail", "return_str"]
    assert [getattr(VexApi, f).offset for f in order] == [8 * i for i in range(11)]
    assert sizeof(VexApi) == 88


def test_attach_fills_every_pointer_and_sets_ready() -> None:
    t = VexApi()
    t.abi_version = 1
    embed.attach(addressof(t))
    assert t.ready == 1
    for f in ("open", "close", "bind", "load", "call", "budget", "error", "fail", "return_str"):
        assert getattr(t, f)


@pytest.mark.parametrize("version", [0, 2, -1])
def test_abi_mismatch_leaves_ready_at_zero(version: int, capsys: pytest.CaptureFixture[str]) -> None:
    t = VexApi()
    t.abi_version = version
    embed.attach(addressof(t))
    assert t.ready == 0
    assert "ABI" in capsys.readouterr().err


def test_abi_version_is_one() -> None:
    assert embed.ABI_VERSION == 1


# ------------------------------------------------------------------ open / close
@pytest.mark.parametrize("caps", [16, -1, 1 << 40])
def test_open_with_bad_caps_returns_null_with_a_message(api: Api, caps: int) -> None:
    assert not api.table.open(caps)
    assert "capability" in api.error(None)


def test_close_then_use_is_a_binding_error(api: Api) -> None:
    vm = api.open()
    api.table.close(vm)
    assert api.load(vm, "fn go() { 1 }") == BINDING
    assert "unknown or closed" in api.error(vm)


def test_close_of_null_and_unknown_handles_is_harmless(api: Api) -> None:
    api.table.close(None)
    api.table.close(424242)


@pytest.mark.parametrize("vm", [None, 424242])
def test_every_status_call_rejects_a_bad_handle(api: Api, vm: int | None) -> None:
    assert api.load(vm, "fn go() { 1 }") == BINDING
    assert api.call(vm, b"go", [])[0] == BINDING
    assert api.table.budget(vm, 5) == BINDING
    assert api.table.bind(vm, b"f", b"()", None, HOST_FN(lambda *a: 0)) == BINDING
    assert api.error(vm) != ""


# ------------------------------------------------------------------ Mort calls Vex
def test_call_passes_and_returns_every_scalar(api: Api) -> None:
    vm = api.open()
    assert api.load(vm, 'fn f(a: Int, b: Float, c: Bool, d: Str) { "{a} {b} {c} {d}" }\nfn u() { }') == OK
    assert api.call(vm, b"f", [7, 1.5, True, "mört"]) == (OK, "7 1.5 true mört")
    assert api.call(vm, b"u", []) == (OK, None)


def test_int_extremes_cross_exactly(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f(n: Int) { n }")
    for n in (2**63 - 1, -(2**63), 0):
        assert api.call(vm, b"f", [n]) == (OK, n)


def test_non_ascii_lengths_are_in_bytes(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f(s: Str) { s }")
    v = VexValue()
    argv = make_args(["hé"])
    assert api.table.call(vm, b"f", argv, 1, byref(v)) == OK
    assert (v.tag, v.len) == (STR, 3)


def test_interior_nul_survives(api: Api) -> None:
    vm = api.open()
    api.load(vm, 'fn f(s: Str) { s.len() }\nfn g() { "a\\0b" }')
    assert api.call(vm, b"f", ["a\0b"]) == (OK, 3)
    v = VexValue()
    assert api.table.call(vm, b"g", make_args([]), 0, byref(v)) == OK
    assert v.len == 3 and string_at(v.s, 3) == b"a\0b"


def test_ret_string_lives_until_the_next_call(api: Api) -> None:
    vm = api.open()
    api.load(vm, 'fn f() { "keep me" }\nfn n() { 1 }')
    v = VexValue()
    api.table.call(vm, b"f", make_args([]), 0, byref(v))
    assert string_at(v.s, v.len) == b"keep me"


@pytest.mark.parametrize(
    "arg",
    [True, 1.5, "x", None],
)
def test_wrong_tag_is_status_3(api: Api, arg: Any) -> None:
    vm = api.open()
    api.load(vm, "fn f(n: Int) { n }")
    assert api.call(vm, b"f", [arg])[0] == BINDING
    assert "argument 1" in api.error(vm)


def test_unknown_tag_is_status_3(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f(n: Int) { n }")
    bad = VexValue()
    bad.tag = 99
    assert api.call(vm, b"f", [bad])[0] == BINDING


def test_bool_must_be_0_or_1(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f(b: Bool) { b }")
    bad = VexValue()
    bad.tag, bad.i = BOOL, 2
    assert api.call(vm, b"f", [bad])[0] == BINDING


def test_invalid_utf8_argument_is_status_3(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f(s: Str) { s }")
    assert api.call(vm, b"f", [b"\xff\xfe"])[0] == BINDING
    assert "UTF-8" in api.error(vm)


def test_null_string_with_length_is_status_3(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f(s: Str) { s }")
    bad = VexValue()
    bad.tag, bad.s, bad.len = STR, None, 5
    assert api.call(vm, b"f", [bad])[0] == BINDING


def test_negative_string_length_is_status_3(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f(s: Str) { s }")
    bad = make_args(["abc"])[0]
    bad.len = -1
    assert api.call(vm, b"f", [bad])[0] == BINDING


def test_empty_string_with_null_pointer_is_empty(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f(s: Str) { s }")
    v = VexValue()
    v.tag, v.s, v.len = STR, None, 0
    assert api.call(vm, b"f", [v]) == (OK, "")


def test_unknown_function_is_status_3(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn price() { 1 }")
    assert api.call(vm, b"pricee", [])[0] == BINDING
    assert "did you mean `price`" in api.error(vm)


def test_wrong_arity_is_status_3(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f(n: Int) { n }")
    assert api.call(vm, b"f", [1, 2])[0] == BINDING


def test_aggregates_are_status_3(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f() { [1] }")
    assert api.call(vm, b"f", [])[0] == BINDING
    assert "cross" in api.error(vm)


def test_null_name_is_status_3(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f() { 1 }")
    assert api.call(vm, None, [])[0] == BINDING


def test_null_ret_is_status_3(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f() { 1 }")
    assert api.table.call(vm, b"f", make_args([]), 0, None) == BINDING


@pytest.mark.parametrize("argc", [-1, 1 << 40])
def test_bad_argc_is_status_3(api: Api, argc: int) -> None:
    vm = api.open()
    api.load(vm, "fn f() { 1 }")
    v = VexValue()
    assert api.table.call(vm, b"f", make_args([]), argc, byref(v)) == BINDING


def test_null_argv_with_args_is_status_3(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f(n: Int) { n }")
    v = VexValue()
    assert api.table.call(vm, b"f", None, 1, byref(v)) == BINDING


def test_null_argv_with_no_args_is_fine(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f() { 1 }")
    v = VexValue()
    assert api.table.call(vm, b"f", None, 0, byref(v)) == OK
    assert v.i == 1


# ------------------------------------------------------------------ errors and status codes
def test_compile_error_is_status_1_with_rendered_diagnostics(api: Api) -> None:
    vm = api.open()
    assert api.load(vm, "fn f() {\n  let x: Int = true;\n}", b"rules/bad.vx") == COMPILE
    text = api.error(vm)
    assert "error[type-mismatch]" in text and "rules/bad.vx:2:" in text


def test_denied_capability_is_status_1(api: Api) -> None:
    vm = api.open(1)
    assert api.load(vm, 'fn f() { io.write_file("a", "b"); }') == COMPILE
    assert "capability-denied" in api.error(vm)


def test_runtime_error_is_status_2_with_location_and_host_continues(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn boom(x: Int) {\n  x / 0\n}\nfn ok() { 4 }", b"rules/r.vx")
    assert api.call(vm, b"boom", [1]) == (RUNTIME, None)
    text = api.error(vm)
    assert "division by zero" in text and "rules/r.vx:2:3" in text
    assert api.call(vm, b"ok", []) == (OK, 4)


def test_budget_is_status_4_and_host_continues(api: Api) -> None:
    vm = api.open()
    assert api.table.budget(vm, 100) == OK
    api.load(vm, "fn spin() {\n  while true { }\n}\nfn ok() { 4 }", b"r.vx")
    assert api.call(vm, b"spin", [])[0] == BUDGET
    assert "budget" in api.error(vm) and "r.vx:" in api.error(vm)
    assert api.call(vm, b"ok", []) == (OK, 4)


def test_default_budget_stops_a_loop(api: Api) -> None:
    assert DEFAULT_BUDGET == 5_000_000
    vm = api.open()
    assert api.table.budget(vm, 200_000) == OK
    api.load(vm, "fn spin() { while true { } }")
    assert api.call(vm, b"spin", [])[0] == BUDGET


@pytest.mark.parametrize("units", [0, -5])
def test_bad_budget_is_status_3(api: Api, units: int) -> None:
    vm = api.open()
    assert api.table.budget(vm, units) == BINDING


def test_error_is_cleared_by_the_next_call(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f() { 1 / 0 }\nfn g() { 1 }")
    api.call(vm, b"f", [])
    assert api.error(vm) != ""
    api.call(vm, b"g", [])
    assert api.error(vm) == ""


def test_load_with_invalid_utf8_is_status_3(api: Api) -> None:
    vm = api.open()
    assert api.table.load(vm, b"x.vx", b"\xff", 1) == BINDING


def test_load_with_bad_length_is_status_3(api: Api) -> None:
    vm = api.open()
    assert api.table.load(vm, b"x.vx", b"abc", -1) == BINDING
    assert api.table.load(vm, b"x.vx", None, 3) == BINDING


def test_load_replaces_the_script(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f() { 1 }")
    assert api.call(vm, b"f", []) == (OK, 1)
    api.load(vm, "fn f() { 2 }")
    assert api.call(vm, b"f", []) == (OK, 2)


# ------------------------------------------------------------------ Vex calls Mort
def adder(vm: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
    ret[0].tag, ret[0].i = INT, argv[0].i + 1
    return 0


def test_script_calls_a_host_function(api: Api) -> None:
    vm = api.open()
    assert api.bind(vm, b"inc", b"(Int) -> Int", adder) == OK
    api.load(vm, "fn f(n: Int) { host.inc(n) * 10 }")
    assert api.call(vm, b"f", [4]) == (OK, 50)


def test_host_function_receives_vm_ctx_and_scalars(api: Api) -> None:
    seen: list[Any] = []

    def h(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        seen.append((vm_, ctx, argc, argv[0].i, argv[1].f, argv[2].i, string_at(argv[3].s, argv[3].len), argv[3].len))
        assert string_at(argv[3].s + argv[3].len, 1) == b"\0"
        ret[0].tag = UNIT
        return 0

    vm = api.open()
    api.bind(vm, b"h", b"(Int, Float, Bool, Str)", h, ctx=1234)
    api.load(vm, 'fn f() { host.h(2, 1.5, true, "é"); }')
    assert api.call(vm, b"f", [])[0] == OK
    assert seen == [(vm, 1234, 4, 2, 1.5, 1, "é".encode(), 2)]


def test_return_str_copies_and_is_decoded(api: Api) -> None:
    def h(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        buf = ctypes.create_string_buffer("gold ✓".encode())
        api.table.return_str(vm_, ret, buf, len(buf.value))
        del buf  # the host's memory may go away immediately
        return 0

    vm = api.open()
    api.bind(vm, b"tier", b"() -> Str", h)
    api.load(vm, "fn f() { host.tier() }")
    assert api.call(vm, b"f", []) == (OK, "gold ✓")


def test_host_failure_is_a_located_runtime_error_with_its_message(api: Api) -> None:
    def h(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        api.table.fail(vm_, "customer not found".encode())
        return 1

    vm = api.open()
    api.bind(vm, b"h", b"()", h)
    api.load(vm, "fn f() {\n  host.h();\n}", b"r.vx")
    assert api.call(vm, b"f", [])[0] == RUNTIME
    text = api.error(vm)
    assert "customer not found" in text and "r.vx:2:" in text


def test_nonzero_without_fail_is_still_a_runtime_error(api: Api) -> None:
    vm = api.open()
    api.bind(vm, b"h", b"()", lambda *a: 7)
    api.load(vm, "fn f() { host.h(); }")
    assert api.call(vm, b"f", [])[0] == RUNTIME
    assert "failed" in api.error(vm) and "7" in api.error(vm)


def test_fail_then_zero_is_still_a_failure(api: Api) -> None:
    def h(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        api.table.fail(vm_, b"oops")
        return 0

    vm = api.open()
    api.bind(vm, b"h", b"()", h)
    api.load(vm, "fn f() { host.h(); }")
    assert api.call(vm, b"f", [])[0] == RUNTIME
    assert "oops" in api.error(vm)


def test_fail_with_invalid_utf8_or_null_is_reported(api: Api) -> None:
    def h(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        api.table.fail(vm_, b"\xff!")
        return 1

    vm = api.open()
    api.bind(vm, b"h", b"()", h)
    api.load(vm, "fn f() { host.h(); }")
    assert api.call(vm, b"f", [])[0] == RUNTIME

    def n(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        api.table.fail(vm_, None)
        return 1

    api.bind(vm, b"n", b"()", n)
    api.load(vm, "fn f() { host.n(); }")
    assert api.call(vm, b"f", [])[0] == RUNTIME


def bad_bool(r: Any) -> None:
    r.tag, r.i = BOOL, 7


@pytest.mark.parametrize(
    ("set_tag", "sig"),
    [
        (lambda r: setattr(r, "tag", STR), "() -> Int"),
        (lambda r: setattr(r, "tag", INT), "() -> Str"),
        (lambda r: setattr(r, "tag", 99), "() -> Int"),
        (bad_bool, "() -> Bool"),
    ],
)
def test_wrong_return_tag_is_status_3(api: Api, set_tag: Callable[[Any], Any], sig: str) -> None:
    def h(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        set_tag(ret[0])
        return 0

    vm = api.open()
    api.bind(vm, b"h", sig.encode(), h)
    api.load(vm, "fn f() {\n  host.h();\n}", b"r.vx")
    assert api.call(vm, b"f", [])[0] == BINDING
    assert "host function" in api.error(vm) and "r.vx:2:" in api.error(vm)


def test_invalid_utf8_return_is_status_3(api: Api) -> None:
    def h(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        api.table.return_str(vm_, ret, b"\xff\xfe", 2)
        return 0

    vm = api.open()
    api.bind(vm, b"h", b"() -> Str", h)
    api.load(vm, "fn f() { host.h() }")
    assert api.call(vm, b"f", [])[0] == BINDING


def test_return_str_outside_a_host_call_does_not_write(api: Api) -> None:
    vm = api.open()
    v = VexValue()
    api.table.return_str(vm, byref(v), b"x", 1)
    assert v.s is None


@pytest.mark.parametrize(
    ("name", "sig"),
    [(b"a.b", b"()"), (b"f", b"(List[Int])"), (b"f", b"nope"), (b"\xff", b"()"), (b"f", b"\xff")],
)
def test_bad_binds_are_status_3(api: Api, name: bytes, sig: bytes) -> None:
    vm = api.open()
    assert api.bind(vm, name, sig, lambda *a: 0) == BINDING
    assert api.error(vm) != ""


def test_null_callback_or_null_strings_are_status_3(api: Api) -> None:
    vm = api.open()
    assert api.table.bind(vm, b"f", b"()", None, HOST_FN(0)) == BINDING
    assert api.table.bind(vm, None, b"()", None, HOST_FN(lambda *a: 0)) == BINDING
    assert api.table.bind(vm, b"f", None, None, HOST_FN(lambda *a: 0)) == BINDING


# ------------------------------------------------------------------ nothing escapes
class Boom(BaseException):
    pass


def explode(*a: Any, **k: Any) -> Any:
    raise Boom("no")


@pytest.mark.parametrize(
    ("method", "status_of"),
    [
        ("load", lambda api, vm: api.load(vm, "fn f() { 1 }")),
        ("call", lambda api, vm: api.call(vm, b"f", [])[0]),
        ("bind", lambda api, vm: api.bind(vm, b"f", b"()", lambda *a: 0)),
    ],
)
def test_base_exceptions_become_statuses(
    api: Api, monkeypatch: pytest.MonkeyPatch, method: str, status_of: Callable[[Api, int], int]
) -> None:
    vm = api.open()
    api.load(vm, "fn f() { 1 }")
    monkeypatch.setattr("vex.host.Host." + method, explode)
    assert status_of(api, vm) != OK
    assert "Boom" in api.error(vm)


def test_compiler_crash_is_status_1(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vex.host.compile_source", lambda *a, **k: 1 / 0)
    vm = api.open()
    assert api.load(vm, "fn f() { 1 }") == COMPILE
    assert "ZeroDivisionError" in api.error(vm)


def test_internal_runtime_exception_is_status_2(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    # Range.len now checks the 64-bit contract itself, so force the Python error to exercise this path.
    def overflow(r: range) -> int:
        raise OverflowError("Python int too large to convert to C ssize_t")

    monkeypatch.setattr(METHODS["Range"]["len"], "impl", overflow)
    vm = api.open()
    api.load(vm, "fn f() { (math.INT_MIN..math.INT_MAX).len() }")
    assert api.call(vm, b"f", [])[0] == RUNTIME
    assert "internal error" in api.error(vm)


def test_gcd_out_of_range_is_status_2_not_a_truncated_int(api: Api) -> None:
    vm = api.open()
    api.load(vm, "fn f() { math.gcd(math.INT_MIN, 0) }")
    assert api.call(vm, b"f", [])[0] == RUNTIME


def test_open_failure_does_not_raise(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vex.embed.Host", explode)
    assert not api.table.open(0)
    assert "Boom" in api.error(None)


# ------------------------------------------------------------------ re-entry
def test_reentry_mort_vex_mort_vex(api: Api) -> None:
    def again(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        inner = VexValue()
        st = api.table.call(vm_, b"inner", argv, 1, byref(inner))
        if st != OK:
            api.table.fail(vm_, api.table.error(vm_) and b"inner failed")
            return 1
        ret[0].tag, ret[0].i = INT, inner.i
        return 0

    vm = api.open()
    api.bind(vm, b"again", b"(Int) -> Int", again)
    api.load(vm, "fn inner(n: Int) { n * 3 }\nfn f(n: Int) { 1 + host.again(n) }")
    assert api.call(vm, b"f", [5]) == (OK, 16)


def test_reentry_shares_budget_and_reports_it(api: Api) -> None:
    def again(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        inner = VexValue()
        st = api.table.call(vm_, b"spin", None, 0, byref(inner))
        api.table.fail(vm_, b"inner stopped")
        return int(st)

    vm = api.open()
    api.table.budget(vm, 50)
    api.bind(vm, b"again", b"()", again)
    api.load(vm, "fn spin() { while true { } }\nfn f() { host.again(); }")
    assert api.call(vm, b"f", [])[0] == BUDGET


def test_runaway_reentry_is_a_status_not_a_stack_overflow(api: Api) -> None:
    depth = [0]

    def again(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        depth[0] += 1
        inner = VexValue()
        st = api.table.call(vm_, b"f", None, 0, byref(inner))
        if st != OK:
            api.table.fail(vm_, b"too deep")
            return int(st)
        ret[0].tag = UNIT
        return 0

    vm = api.open()
    api.bind(vm, b"again", b"()", again)
    api.load(vm, "fn f() { host.again(); }")
    assert api.call(vm, b"f", [])[0] == RUNTIME
    assert depth[0] <= MAX_HOST_DEPTH + 1


def test_argv_strings_are_not_clobbered_by_reentry(api: Api) -> None:
    def again(vm_: Any, ctx: Any, argv: Any, argc: int, ret: Any) -> int:
        before = string_at(argv[0].s, argv[0].len)
        inner = VexValue()
        api.table.call(vm_, b"other", None, 0, byref(inner))
        api.table.call(vm_, b"other", None, 0, byref(inner))
        assert string_at(argv[0].s, argv[0].len) == before
        api.table.return_str(vm_, ret, before, len(before))
        return 0

    vm = api.open()
    api.bind(vm, b"again", b"(Str) -> Str", again)
    api.load(vm, 'fn other() { "xxxxxxxxxxxxx" }\nfn f() { host.again("hello") }')
    assert api.call(vm, b"f", []) == (OK, "hello")


# ------------------------------------------------------------------ output
def test_print_is_flushed_after_each_call(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    class Out:
        text = ""
        flushed = ""

        def write(self, s: str) -> int:
            self.text += s
            return len(s)

        def flush(self) -> None:
            self.flushed = self.text

    out = Out()
    monkeypatch.setattr("sys.stdout", out)
    vm = api.open(1)
    api.load(vm, 'fn f() { print("hi"); }')
    assert api.call(vm, b"f", [])[0] == OK
    assert out.flushed == "hi\n"


