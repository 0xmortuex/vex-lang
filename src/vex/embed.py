"""The Python side of the Vex native embedding ABI (version 1). docs/ffi.md is the normative description.

A native host allocates a ``VexApi`` and runs ``vex.embed.attach(<address>)`` inside CPython. ``attach`` fills the
table with C-callable pointers, and from then on the host talks to Vex only through it.

Every entry point here turns every failure into a status code. ctypes prints and discards an exception that
leaves a callback and returns garbage to C, so nothing may escape.
"""

from __future__ import annotations

import ctypes
import itertools
import signal
import sys
from collections.abc import Callable
from ctypes import CFUNCTYPE, POINTER, Structure, c_char_p, c_double, c_longlong, c_void_p, string_at
from functools import wraps
from typing import Any

from vex.diagnostics import VexError, VexRuntimeError
from vex.host import CAP_ALL, CAP_PRINT, Host, VexBindingError, VexBudgetError
from vex.runtime import VexPanic

ABI_VERSION = 1

STATUS_OK = 0
STATUS_COMPILE = 1
STATUS_RUNTIME = 2
STATUS_BINDING = 3
STATUS_BUDGET = 4

TAG_UNIT = 0
TAG_INT = 1
TAG_FLOAT = 2
TAG_BOOL = 3
TAG_STR = 4

MAX_ARGC = 255


class VexValue(Structure):
    _fields_ = [
        ("tag", c_longlong),
        ("i", c_longlong),
        ("f", c_double),
        ("s", c_void_p),
        ("len", c_longlong),
    ]


_ValuePtr = POINTER(VexValue)
HOST_FN = CFUNCTYPE(c_longlong, c_void_p, c_void_p, _ValuePtr, c_longlong, _ValuePtr)

_OPEN = CFUNCTYPE(c_void_p, c_longlong)
_CLOSE = CFUNCTYPE(None, c_void_p)
_BIND = CFUNCTYPE(c_longlong, c_void_p, c_char_p, c_char_p, c_void_p, HOST_FN)
_LOAD = CFUNCTYPE(c_longlong, c_void_p, c_char_p, c_void_p, c_longlong)
_CALL = CFUNCTYPE(c_longlong, c_void_p, c_char_p, _ValuePtr, c_longlong, _ValuePtr)
_BUDGET = CFUNCTYPE(c_longlong, c_void_p, c_longlong)
_ERROR = CFUNCTYPE(c_void_p, c_void_p)
_FAIL = CFUNCTYPE(None, c_void_p, c_char_p)
_RETURN_STR = CFUNCTYPE(None, c_void_p, _ValuePtr, c_void_p, c_longlong)


class VexApi(Structure):
    _fields_ = [
        ("abi_version", c_longlong),
        ("ready", c_longlong),
        ("open", _OPEN),
        ("close", _CLOSE),
        ("bind", _BIND),
        ("load", _LOAD),
        ("call", _CALL),
        ("budget", _BUDGET),
        ("error", _ERROR),
        ("fail", _FAIL),
        ("return_str", _RETURN_STR),
    ]


def _c_string(text: str) -> ctypes.Array[ctypes.c_char]:
    return ctypes.create_string_buffer(text.encode("utf-8", "replace"))


class _Instance:
    def __init__(self, handle: int, host: Host) -> None:
        self.handle = handle
        self.host = host
        self.error = _c_string("")
        self.pending_fail: str | None = None
        self.ret_buffer: ctypes.Array[ctypes.c_char] | None = None
        self.host_returns: dict[int, ctypes.Array[ctypes.c_char] | None] = {}


_handles = itertools.count(1)
_instances: dict[int, _Instance] = {}
_no_instance_error = _c_string("")
# Used when even recording an error failed. Static, so reporting cannot fail again.
_UNREPORTABLE = ctypes.create_string_buffer(b"internal error: the failure could not be recorded")
_UNREPORTABLE_ADDRESS = ctypes.addressof(_UNREPORTABLE)


def _entry(default: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Make a table entry unable to raise: the handler does no work that could fail, not even a call."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def guarded(*args: Any) -> Any:
            try:
                return fn(*args)
            except BaseException:
                return default

        return guarded

    return decorate


def _instance(vm: int | None) -> _Instance:
    inst = _instances.get(vm) if vm is not None else None
    if inst is None:
        raise VexBindingError("unknown or closed Vex instance")
    return inst


def _set_error(inst: _Instance | None, text: str) -> None:
    global _no_instance_error
    if inst is None:
        _no_instance_error = _c_string(text)
    else:
        inst.error = _c_string(text)


def _text(raw: bytes | None, what: str) -> str:
    if raw is None:
        raise VexBindingError(f"{what} is null")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise VexBindingError(f"{what} is not valid UTF-8") from None


def _status_and_text(e: BaseException, internal_status: int) -> tuple[int, str]:
    if isinstance(e, VexError):
        return STATUS_COMPILE, str(e)
    if isinstance(e, VexBudgetError):
        return STATUS_BUDGET, e.render()
    if isinstance(e, VexRuntimeError):
        return STATUS_RUNTIME, e.render()
    if isinstance(e, VexBindingError):
        return STATUS_BINDING, f"binding error: {e}"
    return internal_status, f"internal error: {type(e).__name__}: {e}"


def _status(vm: int | None, internal_status: int, body: Callable[[_Instance], None]) -> int:
    """Run one API call: clear the last error, then map every failure to its status code."""
    inst: _Instance | None = None
    status = internal_status
    try:
        inst = _instance(vm)
        inst.error = _c_string("")
        body(inst)
        return STATUS_OK
    except BaseException as e:
        try:
            status, text = _status_and_text(e, internal_status)
            _set_error(inst, text)
        except BaseException:
            _note_unreportable(inst)
        return status


def _note_unreportable(inst: _Instance | None) -> None:
    global _no_instance_error
    if inst is None:
        _no_instance_error = _UNREPORTABLE
    else:
        inst.error = _UNREPORTABLE


def _from_wire(v: VexValue) -> Any:
    tag = v.tag
    if tag == TAG_UNIT:
        return None
    if tag == TAG_INT:
        return v.i
    if tag == TAG_FLOAT:
        return v.f
    if tag == TAG_BOOL:
        if v.i not in (0, 1):
            raise VexBindingError(f"a Bool value must be 0 or 1, got {v.i}")
        return v.i == 1
    if tag == TAG_STR:
        if v.len < 0:
            raise VexBindingError(f"a Str length must not be negative, got {v.len}")
        if v.len == 0:
            return ""
        if v.s is None:
            raise VexBindingError("a Str with a non-zero length has a null pointer")
        try:
            return string_at(v.s, v.len).decode("utf-8")
        except UnicodeDecodeError:
            raise VexBindingError("a Str value is not valid UTF-8") from None
    raise VexBindingError(f"unknown value tag {tag}")


def _str_buffer(s: str) -> ctypes.Array[ctypes.c_char]:
    return ctypes.create_string_buffer(s.encode("utf-8"))


def _to_wire(slot: VexValue, value: Any) -> ctypes.Array[ctypes.c_char] | None:
    """Fill ``slot`` from a scalar. For a Str, returns the buffer that the caller must keep alive."""
    if value is None:
        slot.tag = TAG_UNIT
    elif type(value) is bool:
        slot.tag, slot.i = TAG_BOOL, int(value)
    elif type(value) is int:
        slot.tag, slot.i = TAG_INT, value
    elif type(value) is float:
        slot.tag, slot.f = TAG_FLOAT, value
    else:
        buffer = _str_buffer(value)
        slot.tag, slot.s, slot.len = TAG_STR, ctypes.addressof(buffer), len(buffer) - 1
        return buffer
    return None


# ------------------------------------------------------------------ the table's entry points
@_entry(None)
def _api_open(caps: int) -> int | None:
    try:
        if caps < 0 or caps > CAP_ALL:
            raise VexBindingError(f"unknown capability bits in {caps}")
        host = Host(caps)
        handle = next(_handles)
        _instances[handle] = _Instance(handle, host)
        return handle
    except BaseException as e:
        _set_error(None, f"open failed: {type(e).__name__}: {e}")
        return None


@_entry(None)
def _api_close(vm: int | None) -> None:
    try:
        if vm is not None:
            _instances.pop(vm, None)
    except BaseException as e:
        _set_error(None, f"close failed: {type(e).__name__}: {e}")


def _host_trampoline(inst: _Instance, name: str, ctx: int | None, fn: Any) -> Callable[..., Any]:
    """The Python callable a script's ``host.<name>(...)`` reaches: marshals into a HostFn call."""

    def go(*args: Any) -> Any:
        n = len(args)
        argv = (VexValue * max(n, 1))()
        try:
            keep = [_to_wire(argv[i], a) for i, a in enumerate(args)]
        except UnicodeEncodeError:
            raise VexBindingError(f"host function `{name}` was passed a Str that cannot be encoded as UTF-8") from None
        ret = VexValue()
        ret_address = ctypes.addressof(ret)
        inst.host_returns[ret_address] = None
        saved_fail, inst.pending_fail = inst.pending_fail, None
        try:
            status = fn(inst.handle, ctx, argv, n, ctypes.byref(ret))
            failed, inst.pending_fail = inst.pending_fail, saved_fail
            del keep
            if status != 0 or failed is not None:
                raise VexPanic(failed if failed is not None else f"host function `{name}` failed with status {status}")
            try:
                return _from_wire(ret)
            except VexBindingError as e:
                raise VexBindingError(f"host function `{name}` returned a bad value: {e}") from None
        finally:
            inst.host_returns.pop(ret_address, None)

    return go


@_entry(STATUS_BINDING)
def _api_bind(vm: int | None, name: bytes | None, sig: bytes | None, ctx: int | None, fn: Any) -> int:
    def body(inst: _Instance) -> None:
        text = _text(name, "the function name")
        signature = _text(sig, "the signature")
        if not fn:
            raise VexBindingError(f"host function `{text}` has a null callback")
        inst.host.bind(text, signature, _host_trampoline(inst, text, ctx, fn))

    return _status(vm, STATUS_BINDING, body)


@_entry(STATUS_COMPILE)
def _api_load(vm: int | None, path: bytes | None, src: int | None, length: int) -> int:
    def body(inst: _Instance) -> None:
        name = _text(path, "the script path")
        if length < 0:
            raise VexBindingError(f"the script length must not be negative, got {length}")
        if src is None:
            raise VexBindingError("the script text is null")
        try:
            text = string_at(src, length).decode("utf-8")
        except UnicodeDecodeError:
            raise VexBindingError("the script text is not valid UTF-8") from None
        inst.host.load(name, text)

    return _status(vm, STATUS_COMPILE, body)


@_entry(STATUS_RUNTIME)
def _api_call(vm: int | None, name: bytes | None, argv: Any, argc: int, ret: Any) -> int:
    def body(inst: _Instance) -> None:
        if not ret:
            raise VexBindingError("ret is null")
        result = ret.contents
        result.tag, result.i, result.f, result.s, result.len = TAG_UNIT, 0, 0.0, None, 0
        fn = _text(name, "the function name")
        if argc < 0 or argc > MAX_ARGC:
            raise VexBindingError(f"argc must be between 0 and {MAX_ARGC}, got {argc}")
        if argc and not argv:
            raise VexBindingError("argv is null")
        args = [_from_wire(argv[i]) for i in range(argc)]
        try:
            value = inst.host.call(fn, args)
        finally:
            if inst.host.caps & CAP_PRINT:
                sys.stdout.flush()
        inst.ret_buffer = _to_wire(result, value)

    return _status(vm, STATUS_RUNTIME, body)


@_entry(STATUS_BINDING)
def _api_budget(vm: int | None, units: int) -> int:
    return _status(vm, STATUS_BINDING, lambda inst: inst.host.budget(units))


@_entry(_UNREPORTABLE_ADDRESS)
def _api_error(vm: int | None) -> int:
    inst = _instances.get(vm) if vm is not None else None
    return ctypes.addressof(inst.error if inst is not None else _no_instance_error)


@_entry(None)
def _api_fail(vm: int | None, message: bytes | None) -> None:
    # If this cannot record the message, the HostFn's non-zero return still reports a failure.
    inst = _instance(vm)
    inst.pending_fail = "host function failed" if message is None else message.decode("utf-8", "replace")


@_entry(None)
def _api_return_str(vm: int | None, ret: Any, s: int | None, length: int) -> None:
    inst: _Instance | None = None
    try:
        inst = _instance(vm)
        if not ret:
            raise VexBindingError("return_str: ret is null")
        address = ctypes.addressof(ret.contents)
        if address not in inst.host_returns:
            raise VexBindingError("return_str: ret does not belong to the running host function")
        if length < 0:
            raise VexBindingError(f"return_str: length must not be negative, got {length}")
        if s is None and length != 0:
            raise VexBindingError("return_str: pointer is null with a non-zero length")
        data = b"" if length == 0 or s is None else string_at(s, length)
        buffer = ctypes.create_string_buffer(data, length + 1)
        inst.host_returns[address] = buffer
        value = ret.contents
        value.tag, value.s, value.len = TAG_STR, ctypes.addressof(buffer), length
    except BaseException as e:
        message = f"return_str failed: {type(e).__name__}: {e}"
        if inst is not None:
            inst.pending_fail = message
        else:
            _set_error(None, message)


_TABLE: list[tuple[str, Any]] = [
    ("open", _OPEN(_api_open)),
    ("close", _CLOSE(_api_close)),
    ("bind", _BIND(_api_bind)),
    ("load", _LOAD(_api_load)),
    ("call", _CALL(_api_call)),
    ("budget", _BUDGET(_api_budget)),
    ("error", _ERROR(_api_error)),
    ("fail", _FAIL(_api_fail)),
    ("return_str", _RETURN_STR(_api_return_str)),
]


def attach(address: int) -> None:
    """Fill the host's ``VexApi``. An ABI mismatch leaves ``ready`` at 0."""
    api = VexApi.from_address(address)
    if api.abi_version != ABI_VERSION:
        sys.stderr.write(f"vex.embed: host ABI version {api.abi_version} does not match {ABI_VERSION}\n")
        return
    # Python's default handler would raise KeyboardInterrupt at any bytecode, even entering a callback from
    # native code. A host that installed its own SIGINT handler keeps it.
    if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    for field, pointer in _TABLE:
        setattr(api, field, pointer)
    api.ready = 1


__all__ = ["ABI_VERSION", "HOST_FN", "VexApi", "VexValue", "attach"]
