"""Property / fuzz tests: the compiler must only ever produce Vex diagnostics, never a Python traceback."""

from __future__ import annotations

import io
import os
import pathlib
import random
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any

import pytest

from vex import runtime, stdlib
from vex.api import Program, compile_source, compile_text, read_source
from vex.cli import _run_big_stack
from vex.diagnostics import DiagSink, Source, VexError, VexRuntimeError
from vex.parser import parse_source
from vex.repl import run_repl
from vex.runtime import INT_MAX, INT_MIN, VexExit
from vex.symbols import Builtin
from vex.vm import VM
from vex.vtypes import BOOL, FLOAT, INT, NEVER, RANGE, STR, UNIT, TCon, TFn, TParam, TTuple, Type, show

ROOT = pathlib.Path(__file__).parent.parent
SEED_FILES = sorted((ROOT / "examples").glob("*.vx")) + sorted((ROOT / "tests" / "errors").glob("*.vx"))
SEED_TEXTS = [p.read_text(encoding="utf-8") for p in SEED_FILES]

VOCAB = [
    "fn", "let", "mut", "struct", "enum", "impl", "match", "if", "else", "while", "for", "in", "return",
    "break", "continue", "import", "true", "false", "main", "x", "y", "Int", "Str", "List", "Option", "Some",
    "None", "Ok", "Err", "self", "print", "math", "(", ")", "{", "}", "[", "]", "#{", ",", ";", ":", ".", "..",
    "..=", "->", "=>", "=", "==", "!=", "<", ">", "<=", ">=", "+", "-", "*", "/", "%", "&&", "||", "!", "?",
    "|", "1", "2.5", '"s"', '"a{x}b"', '"', "{", "'", "@", "0xZ", "99999999999999999999", "_", "T", "[T]",
    "// c\n", "/*", "*/", "\n", "é", "🌍",
]  # fmt: skip


def only_vex_diagnostics(text: str) -> None:
    try:
        compile_text(text, "<fuzz>")
    except VexError as e:
        assert e.diagnostics
        for d in e.diagnostics:
            d.render()  # rendering a diagnostic must not crash either


@pytest.mark.parametrize("seed", range(250))
def test_token_soup_never_crashes(seed: int) -> None:
    rng = random.Random(seed)
    n = rng.randint(1, 60)
    toks = [rng.choice(VOCAB) for _ in range(n)]
    body = " ".join(toks)
    prefix = rng.choice(["", "fn main() { ", "fn main() { let x = ", "struct S { a: Int } fn main() { match "])
    suffix = rng.choice(["", " }", " } }"])
    only_vex_diagnostics(prefix + body + suffix)


def mutate(rng: random.Random, text: str) -> str:
    for _ in range(rng.randint(1, 4)):
        if not text:
            return text
        kind = rng.randrange(7)
        i = rng.randrange(len(text))
        if kind == 0:  # delete a character
            text = text[:i] + text[i + 1 :]
        elif kind == 1:  # delete a span
            j = min(len(text), i + rng.randint(1, 12))
            text = text[:i] + text[j:]
        elif kind == 2:  # insert a vocabulary token
            text = text[:i] + " " + rng.choice(VOCAB) + " " + text[i:]
        elif kind == 3:  # swap two characters
            j = rng.randrange(len(text))
            chars = list(text)
            chars[i], chars[j] = chars[j], chars[i]
            text = "".join(chars)
        elif kind == 4:  # truncate
            text = text[:i]
        elif kind == 5:  # duplicate a line
            lines = text.split("\n")
            k = rng.randrange(len(lines))
            lines.insert(k, lines[k])
            text = "\n".join(lines)
        else:  # replace an identifier-ish chunk
            text = text[:i] + rng.choice(["x", "Int", "Str", "0", "()", "[]", "self"]) + text[i + 1 :]
    return text


@pytest.mark.parametrize("seed", range(300))
def test_mutated_programs_never_crash(seed: int) -> None:
    rng = random.Random(10_000 + seed)
    base = rng.choice(SEED_TEXTS)
    only_vex_diagnostics(mutate(rng, base))


def test_seed_corpus_is_nonempty() -> None:
    assert len(SEED_TEXTS) > 100


# ---------------------------------------------------------------- arithmetic property test
def py_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def py_mod(a: int, b: int) -> int:
    r = abs(a) % abs(b)
    return -r if a < 0 else r


class Overflow(Exception):
    pass


def gen_expr(rng: random.Random, depth: int) -> tuple[str, int]:
    """A random Int expression and its value under Vex's checked-arithmetic semantics."""
    if depth == 0 or rng.random() < 0.25:
        v = rng.choice([0, 1, 2, 3, 7, 10, 100, 12345, 2**31, 2**40, 2**62, INT_MAX, rng.randint(-50, 50)])
        return (str(v), v) if v >= 0 else (f"({v})", v)
    op = rng.choice(["+", "-", "*", "/", "%", "neg", "paren"])
    a_src, a = gen_expr(rng, depth - 1)
    if op == "neg":
        if -a > INT_MAX:
            raise Overflow
        return f"(-{a_src})", -a
    if op == "paren":
        return f"({a_src})", a
    b_src, b = gen_expr(rng, depth - 1)
    if op in "/%" and b == 0:
        raise ZeroDivisionError
    if op == "+":
        r = a + b
    elif op == "-":
        r = a - b
    elif op == "*":
        r = a * b
    elif op == "/":
        r = py_div(a, b)
    else:
        r = py_mod(a, b)
    if r > INT_MAX or r < INT_MIN:
        raise Overflow
    return f"({a_src} {op} {b_src})", r


@pytest.mark.parametrize("seed", range(150))
def test_arithmetic_matches_reference_semantics(seed: int) -> None:
    rng = random.Random(seed)
    try:
        src, expected = gen_expr(rng, rng.randint(1, 5))
        outcome = str(expected)
    except Overflow:
        src, outcome = None, "overflow"
    except ZeroDivisionError:
        src, outcome = None, "zero"
    if src is None:
        # Re-generate the same tree without evaluating so we can run it in the VM.
        rng = random.Random(seed)
        src = gen_source_only(rng, rng.randint(1, 5))
    prog = compile_text(f"fn main() {{ print({src}); }}", "<prop>")
    out = io.StringIO()
    assert prog.main is not None
    try:
        VM(out).run_function(prog.main)
        got = out.getvalue().strip()
    except VexRuntimeError as e:
        got = "overflow" if "overflow" in e.message else "zero" if "division by zero" in e.message else e.message
    assert got == outcome, src


def gen_source_only(rng: random.Random, depth: int) -> str:
    """Same random choices as gen_expr but ignoring errors, to obtain the source text."""
    if depth == 0 or rng.random() < 0.25:
        v = rng.choice([0, 1, 2, 3, 7, 10, 100, 12345, 2**31, 2**40, 2**62, INT_MAX, rng.randint(-50, 50)])
        return str(v) if v >= 0 else f"({v})"
    op = rng.choice(["+", "-", "*", "/", "%", "neg", "paren"])
    a = gen_source_only(rng, depth - 1)
    if op == "neg":
        return f"(-{a})"
    if op == "paren":
        return f"({a})"
    b = gen_source_only(rng, depth - 1)
    return f"({a} {op} {b})"


@pytest.mark.parametrize("seed", range(40))
def test_random_list_programs_match_python(seed: int) -> None:
    rng = random.Random(7_000 + seed)
    xs = [rng.randint(-100, 100) for _ in range(rng.randint(0, 12))]
    k = rng.randint(-5, 5)
    src = f"""
fn main() {{
    let xs = {xs};
    print(xs.map(|x| x * {k}).filter(|x| x % 2 == 0).sum());
    print(xs.fold(0, |a, x| a + x));
    let mut ys = xs.copy();
    ys.sort();
    print(ys);
    print(xs.len());
}}
"""
    out = io.StringIO()
    prog = compile_text(src, "<prop>")
    assert prog.main is not None
    VM(out).run_function(prog.main)
    mapped = [x * k for x in xs]
    even_sum = sum(x for x in mapped if py_mod(x, 2) == 0)
    expected = f"{even_sum}\n{sum(xs)}\n{sorted(xs)}\n{len(xs)}\n"
    assert out.getvalue() == expected


IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


@pytest.mark.parametrize("seed", range(200))
def test_identifier_swaps_never_crash(seed: int) -> None:
    """Swap identifiers between positions: mostly type errors, so this stresses the checker."""
    rng = random.Random(50_000 + seed)
    text = rng.choice(SEED_TEXTS)
    names = IDENT_RE.findall(text)
    for _ in range(rng.randint(1, 3)):
        spans = [m.span() for m in IDENT_RE.finditer(text)]
        a, b = rng.choice(spans)
        text = text[:a] + rng.choice(names) + text[b:]
    only_vex_diagnostics(text)


# ---------------------------------------------------------------- hostile inputs
#
# "Never crashes" means, for every input, text or bytes:
#   1. parse_source raises nothing at all, in file mode and in REPL mode;
#   2. each of its diagnostics has a line and column and renders (only the "too many errors" note,
#      which belongs to no position, is exempt);
#   3. the full compiler lets no exception out except VexError, whose diagnostics all render;
#   4. a program the compiler accepts runs, under RUN_BUDGET traced Python events and the FUZZ_MAX_* size
#      limits, with no exception out of the VM except VexRuntimeError or VexExit, printing through a stream
#      encoded as the CLI's is;
#   5. every input also drives the REPL, through a stdin configured as the CLI's is;
#   6. no input takes longer than HANG_SECONDS. The watchdog ends the process if one does. The budget in
#      (4) and (5) must not hide a slow REPL, so the repl_scale family runs without one.
#
# All of it runs in the CLI's own configuration (cli._run_big_stack: big stack, recursion limit 60,000).
# Inputs are a pure function of (seed, family, index), so any failure can be replayed.
# `pytest tests/test_fuzz.py --fuzz-seconds=60 --fuzz-seed=7` runs a time-boxed campaign.

HANG_SECONDS = 10.0
RUN_BUDGET = 20_000
# Not executed, since it writes to disk. The compile-time contract still applies to these programs.
UNSAFE_TO_RUN = re.compile(r"\bwrite_file\b")
# Memory protection: every one-call allocation is limited (runtime.MAX_*), and these make the limits small
# enough that nothing a fuzzed program builds within RUN_BUDGET can hurt the machine.
FUZZ_MAX_STR = 100_000
FUZZ_MAX_LIST = 10_000

UNICODE_SAMPLES = [
    "٣", "९", "１", "\U0001d7d7",  # Nd: decimal digits
    "²", "³", "¹", "½", "①", "㊿",  # No: digit-like but not decimal
    "Ⅷ", "ↂ", "〇",  # Nl: letter numbers
    "é", "中", "ʰ", "ǅ",  # L*: letters
    "́", "ः", "⃝",  # M*: combining marks
    "‿", "—", "«", "»", "¡",  # P*: punctuation
    "¢", "¬", "˂", "\U0001f30d",  # S*: symbols
    " ", " ", "　", " ", " ",  # Z*: separators
    "\x00", "\x07", "\x0b", "\x0c", "\x1b", "\x7f", "\x85",  # Cc: controls
    "­", "​", "‍", "‮", "﻿",  # Cf: invisible format characters
    "\ud800", "\udfff", "", "\U0010ffff", "͸", "�",  # surrogate, private, unassigned
]  # fmt: skip

HOSTILE_VOCAB = [
    *UNICODE_SAMPLES,
    "2²", "1٣", "0x٣", "1e²", "1.²", "١٢٣",
    "9" * 4301, "0" * 4301 + "1", "1_" * 2200, "0x" + "F" * 4301, "1." + "9" * 4301, "1e" + "9" * 4301,
    "1e999", "1e-999", "9223372036854775807", "9223372036854775808", "-9223372036854775808",
    "0b2", "0o9", "0x", "0b", "0o", "0x_", "1__", "1_e5", "1._5", "1.e5", "1e+", "1e-_",
    '"\\u{D800}"', '"\\u{110000}"', '"\\u{}"', '"\\u{', '"\\u', '"\\', '"{²}"', '"{"', '"{"{"',
    "/*", "*/", "/* /*", "//",
]  # fmt: skip


def unicode_char(rng: random.Random) -> str:
    return rng.choice(UNICODE_SAMPLES) if rng.random() < 0.7 else chr(rng.randrange(sys.maxunicode + 1))


def gen_soup(rng: random.Random) -> str:
    toks = [rng.choice(HOSTILE_VOCAB if rng.random() < 0.4 else VOCAB) for _ in range(rng.randint(1, 60))]
    prefix = rng.choice(["", "fn main() { ", "fn main() { let x = ", "fn main() { print("])
    return prefix + rng.choice([" ", ""]).join(toks) + rng.choice(["", " }", "); }"])


def gen_literal(rng: random.Random) -> str:
    length = rng.choice([1, 18, 19, 20, 21, 64, 65, 4299, 4300, 4301, 5000, 20_000])
    alphabet = rng.choice(["0", "9", "0123456789", "01", "07", "0123456789abcdefABCDEF"])
    chars = [rng.choice(alphabet) for _ in range(length)]
    for _ in range(rng.choice([0, 0, 1, 3])):
        chars[rng.randrange(length)] = rng.choice(["_", "e", ".", "x", "+", unicode_char(rng)])
    digits = "".join(chars)
    shape = rng.choice(["{}", "0x{}", "0b{}", "0o{}", "1.{}", "{}.5", "1e{}", "1e-{}", "{}e999", "{}abc", "1_{}"])
    literal = shape.format(digits)
    context = rng.choice([
        "fn main() {{ let x = {}; }}", "fn main() {{ print(-{}); }}", "fn main() {{ print(1 + {}); }}",
        "fn main() {{ print((1, 2).{}); }}", "fn main() {{ match 1 {{ {} => 1, _ => 2 }}; }}",
        "fn main() {{ match 1 {{ -{} => 1, _ => 2 }}; }}", 'fn main() {{ print("{{{}}}"); }}', "{}",
    ])  # fmt: skip
    return context.format(literal)


NEST_PAIRS = [
    ("(", ")"), ("[", "]"), ("{ ", "}"), ("#{", "}"), ("-", ""), ("!", ""), ("|x| ", ""), ("if ", ""),
    ("if true { ", "}"), ("while true { ", "}"), ("match ", ""), ("match x { _ => ", "}"), ("for x in ", ""),
    ('"{', '}"'), ("else if true {} ", ""), ("a.b(", ")"), ("f[", "]"), ("(1, ", ")"), ("S { a: ", "}"),
    ("L[", "]"), ("fn(", ")"), ("S(", ")"), ("return ", ""),
]  # fmt: skip


def gen_nesting(rng: random.Random) -> str:
    depth = rng.choice([2, 74, 75, 76, 149, 150, 151, 152, 400, 1100, 5000])
    pairs = [rng.choice(NEST_PAIRS) for _ in range(rng.randint(1, 3))]
    chosen = [pairs[i % len(pairs)] for i in range(depth)]
    inner = rng.choice(["1", "x", "", "²", "9" * 4301])
    closes = "".join(c for _, c in reversed(chosen)) if rng.random() < 0.7 else ""
    body = "".join(o for o, _ in chosen) + inner + closes
    context = rng.choice(["fn main() {{ let x = {}; }}", "fn main() {{ {} }}", "fn f(a: {}) {{}}",
                          "fn main() {{ let {} = 1; }}", "{}"])  # fmt: skip
    return context.replace("{{", "{").replace("}}", "}").replace("{}", body, 1)


def gen_unicode(rng: random.Random) -> str:
    noise = "".join(unicode_char(rng) for _ in range(rng.randint(1, 80)))
    if rng.random() < 0.5:
        return noise
    text = rng.choice(SEED_TEXTS)
    i = rng.randrange(len(text) + 1)
    return text[:i] + noise + text[i:]


def gen_mutated(rng: random.Random) -> str:
    text = mutate(rng, rng.choice(SEED_TEXTS))
    for _ in range(rng.randint(1, 5)):
        i = rng.randrange(len(text) + 1)
        text = text[:i] + rng.choice(HOSTILE_VOCAB) + text[i:]
    return text


BAD_UTF8 = [
    b"\xff", b"\xfe", b"\x80", b"\xbf", b"\xc0\xaf", b"\xc1\xbf", b"\xe0\x80\xaf", b"\xed\xa0\x80",
    b"\xed\xbf\xbf", b"\xf0\x80\x80\xaf", b"\xf4\x90\x80\x80", b"\xf8\x88\x80\x80\x80", b"\xc3", b"\xe2\x82",
    b"\xf0\x9f\x8c", b"\xef\xbb\xbf", b"\x00", b"\r", b"\r\n",
]  # fmt: skip


def gen_bytes(rng: random.Random) -> bytes:
    data = bytearray(rng.choice(SEED_TEXTS).encode("utf-8"))
    for _ in range(rng.randint(1, 8)):
        i = rng.randrange(len(data) + 1)
        kind = rng.randrange(6)
        if kind == 0 and i < len(data):
            data[i] ^= 1 << rng.randrange(8)
        elif kind == 1 and i < len(data):
            data[i] = rng.randrange(256)
        elif kind == 2:
            data[i:i] = rng.choice(BAD_UTF8)
        elif kind == 3:
            data[i:i] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 16)))
        elif kind == 4:
            del data[i : i + rng.randint(1, 32)]
        else:
            del data[i:]
    return bytes(data)


def gen_accepted_literal(rng: random.Random) -> str:
    """Programs likely to pass the compiler and print a hostile literal, so the VM really sees it."""
    escape = rng.choice([
        f"\\u{{{rng.randrange(0xD7F0, 0xE010):X}}}", f"\\u{{{rng.randrange(0x110000):X}}}", f"\\u{{{rng.randrange(0x10FFF0, 0x110010):X}}}",
        unicode_char(rng), "\\n", "\\0", "\\{", "\\}", "\\\\", '\\"',
    ])  # fmt: skip
    value = rng.choice([
        f'"{escape}"', f'"a{escape}b"', f'"{escape}{{1}}"', f'"{{"{escape}"}}"',
        str(rng.choice([INT_MAX, INT_MIN + 1, 0, 2**62])), f"{rng.random()}e{rng.randint(-330, 330)}",
        "0x7FFFFFFFFFFFFFFF", "0b" + "1" * 63, "0" * 4301 + "7",
    ])  # fmt: skip
    return f"fn main() {{\n  let v = {value};\n  print(v);\n  print([v, v]);\n  print((v, 1));\n}}\n"


def gen_chain(rng: random.Random) -> str:
    """Long but flat source that passes the parser and overflows the later, recursive stages."""
    terms = rng.choice([2000, 15_000, 40_000])
    tail = rng.choice([" + ".join(["1"] * terms), "x" + ".a()" * terms, "x" + "[0]" * terms, "x" + "?" * terms])
    return f"fn main() {{\n  let v = {tail};\n}}\n"


HOSTILE_INTS = ["0", "1", "(-1)", "2", "64", "65", "55296", "57343", "1114111", "1114112", "9223372036854775807",
                "(-9223372036854775807)", "(0 - 9223372036854775807 - 1)", "9223372036854775806"]  # fmt: skip
HOSTILE_FLOATS = ["0.0", "(0.0 * -1.0)", "1.5", "(-2.5)", "1e308", "(-1e308)", "1e-320", "(1e308 * 10.0)",
                  "(-1e308 * 10.0)", "(1e308 * 10.0 - 1e308 * 10.0)"]  # fmt: skip
HOSTILE_STRS = ['""', '"a"', '"a,b"', '"a\\0b"', '"\\0"', '"\\u{10FFFF}"', '"é"', '"  12 "', '"+9"', '"1e999"', '"."',
                '".."', '"9".repeat(5000)', '"a".repeat(100000)', '"é".repeat(50000)', '"\\n\\n"']  # fmt: skip
HOSTILE_RANGES = ["0..0", "0..3", "5..1", "(-3)..3", "0..=0", "0..9223372036854775807",
                  "(0 - 9223372036854775807 - 1)..9223372036854775807", "0..=9223372036854775807",
                  "(0 - 9223372036854775807 - 1)..=(0 - 9223372036854775807 - 1)"]  # fmt: skip
# A path write_file can never create: it writes to disk, so it only gets arguments that must fail cleanly.
IMPOSSIBLE_PATHS = ['"a\\0b"', '"\\0"', '""', '"."', '"a".repeat(70000) + "\\0"']


def all_builtins() -> list[tuple[str, str, Builtin]]:
    """Every function and method of the standard library as (how it is called, name, builtin)."""
    found = [("global", name, b) for name, b in stdlib.GLOBALS.items()]
    found += [("method", name, b) for table in stdlib.METHODS.values() for name, b in table.items()]
    found += [("namespace", b.qual, b) for table in stdlib.NAMESPACES.values() for b in table.values() if not b.is_const]
    return found


BUILTINS = all_builtins()


def instantiate(t: Type, chosen: dict[str, Type]) -> Type:
    if isinstance(t, TParam):
        return chosen[t.name]
    if isinstance(t, TCon):
        return TCon(t.name, tuple(instantiate(a, chosen) for a in t.args))
    if isinstance(t, TTuple):
        return TTuple(tuple(instantiate(i, chosen) for i in t.items))
    if isinstance(t, TFn):
        return TFn(tuple(instantiate(p, chosen) for p in t.params), instantiate(t.ret, chosen))
    return t


SIMPLE_VALUES = {INT: HOSTILE_INTS, FLOAT: HOSTILE_FLOATS, STR: HOSTILE_STRS, RANGE: HOSTILE_RANGES, BOOL: ["true", "false"],
                 UNIT: ["()"]}  # fmt: skip
SIMPLE_VALUES_BY_NAME = {t.name: v for t, v in SIMPLE_VALUES.items()}


def hostile_value(t: Type, rng: random.Random) -> str:  # noqa: C901 - one case per type former
    """A Vex expression of type ``t``, built from the values builtins are most likely to mishandle."""
    if isinstance(t, TCon) and t.name in SIMPLE_VALUES_BY_NAME:
        return rng.choice(SIMPLE_VALUES_BY_NAME[t.name])
    if isinstance(t, TTuple):
        return "(" + ", ".join(hostile_value(i, rng) for i in t.items) + ")"
    if isinstance(t, TFn):
        params = ", ".join(f"p{i}: {show(p)}" for i, p in enumerate(t.params))
        return f"|{params}| " + ('panic("callback")' if rng.random() < 0.2 else hostile_value(t.ret, rng))
    assert isinstance(t, TCon), show(t)
    if t.name == "List":
        items = [hostile_value(t.args[0], rng) for _ in range(rng.choice([0, 1, 2, 3]))]
        return "[" + ", ".join(items) + "]"
    if t.name == "Map":
        pairs = [f"{hostile_value(t.args[0], rng)}: {hostile_value(t.args[1], rng)}" for _ in range(rng.choice([0, 1, 2]))]
        return "#{" + ", ".join(pairs) + "}"
    if t.name == "Option":
        return rng.choice(["None", f"Some({hostile_value(t.args[0], rng)})"])
    if t.name == "Result":
        return f"Ok({hostile_value(t.args[0], rng)})" if rng.random() < 0.5 else f"Err({hostile_value(t.args[1], rng)})"
    raise AssertionError(f"no hostile values for {show(t)}")


def program_calling(kind: str, name: str, builtin: Builtin, rng: random.Random) -> str:
    """One complete program calling ``builtin``, its arguments bound first so that empty literals have types."""
    chosen = {p.name: rng.choice([INT, STR, UNIT, TTuple((INT, STR))]) for p in builtin.tparams}
    fn = instantiate(builtin.ty, chosen)
    assert isinstance(fn, TFn)
    values = [hostile_value(p, rng) for p in fn.params]
    if builtin.qual == "io.write_file":
        values[0] = rng.choice(IMPOSSIBLE_PATHS)
    lets = [f"  let mut a{i}: {show(p)} = {v};\n" for i, (p, v) in enumerate(zip(fn.params, values))]
    args = [f"a{i}" for i in range(len(values))]
    call = f"{args[0]}.{name}({', '.join(args[1:])})" if kind == "method" else f"{name}({', '.join(args)})"
    statement = f"  {call};\n" if fn.ret in (UNIT, NEVER) else f"  print({call});\n"
    return "fn main() {\n" + "".join(lets) + statement + "}\n"


def gen_builtin_call(rng: random.Random) -> str:
    kind, name, builtin = rng.choice(BUILTINS)
    return program_calling(kind, name, builtin, rng)


REPL_LINES = [
    'let a: Int = panic("x");', "a", "a + 1", "a = 1;", "let mut s = \"ab\";", "s = s + s;", "s", "s.len()",
    "fn g() -> Int { a }", "g()", ":type a", ":reset", "[1, 2].map(|x| x * 2)", "1..=9223372036854775807",
    "struct N { kids: List[N] }", "let mut c = N { kids: [] };", "for i in 0..30 { c = N { kids: [c, c] }; }", "c",
    "let mut n = N { kids: [] };", "n.kids.push(n);", "n", "[n] == [n]", "55296.to_char()", "(0..9223372036854775807).to_list()",
    'io.read_file("a\\0b")', '"a".repeat(9223372036854775807)', "panic(\"boom\")", "() < ()", '"{', "(", ")", '"', "/*", "*/",
]  # fmt: skip


def gen_repl_session(rng: random.Random) -> bytes:
    """A REPL session mixing statements that fail halfway with values that are hard to echo."""
    lines = []
    for _ in range(rng.randint(2, 30)):
        if rng.random() < 0.15:
            lines.extend(gen_builtin_call(rng).splitlines()[1:-1])  # the body of a call, one statement per input
        else:
            lines.append(rng.choice(REPL_LINES))
    return "\n".join(lines).encode("utf-8", "surrogatepass") + b"\n"


UNFINISHED_LINES = ["(", "[", "#{", "{", '"abc', "/* abc", "( // abc", '"{', '"{"abc', '"\\u{', '"\\', "", "   ", "/*", "\\"]
FINISHED_LINES = ["1", "let x = 1;", "fn f() {}", "// comment", "", ":help", '"a"', "1 + 2 * 3", "[1, 2, 3].len()"]


def gen_repl_scale(rng: random.Random) -> bytes:
    """Input whose only hostile property is its size, to expose a REPL that is not linear in it."""
    shape = rng.random()
    if shape < 0.6:
        body = "(\n" * rng.choice([0, 1]) + (rng.choice(UNFINISHED_LINES) + "\n") * rng.choice([1000, 3000, 12_000])
    elif shape < 0.8:  # every line grows the session, so per-input work proportional to the session is quadratic
        template = rng.choice(["let x{} = 1;\n", "fn f{}() {{}}\n", "struct S{} {{}}\n"])
        body = "".join(template.format(i) for i in range(rng.choice([6000, 24_000])))
    else:
        body = (rng.choice(FINISHED_LINES) + "\n") * rng.choice([1000, 3000, 6000])
    return body.encode("utf-8")


FAMILIES: dict[str, Callable[[random.Random], str | bytes]] = {
    "soup": gen_soup,
    "literal": gen_literal,
    "accepted": gen_accepted_literal,
    "nesting": gen_nesting,
    "chain": gen_chain,
    "unicode": gen_unicode,
    "mutated": gen_mutated,
    "bytes": gen_bytes,
    "builtin": gen_builtin_call,
    "repl_session": gen_repl_session,
    "repl_scale": gen_repl_scale,
}
DEFAULT_CASES = {"soup": 200, "literal": 120, "accepted": 150, "nesting": 60, "chain": 3, "unicode": 200, "mutated": 200, "bytes": 200,
                 "builtin": 200, "repl_session": 100, "repl_scale": 12}  # fmt: skip
UNBUDGETED_FAMILIES = {"repl_scale"}


def repl_budget(family: str) -> int | None:
    return None if family in UNBUDGETED_FAMILIES else RUN_BUDGET


def hostile_input(seed: int, family: str, index: int) -> str | bytes:
    return FAMILIES[family](random.Random(f"{seed}/{family}/{index}"))


def assert_front_end_contract(source: Source) -> None:
    for repl in (False, True):
        sink = DiagSink()
        parse_source(source, sink, repl=repl)
        for d in sink.items:
            if d.code == "too-many-errors":
                continue
            assert d.span is not None, f"{d.code} has no position"
            assert d.span.line >= 1 and d.span.col >= 1, d.code
            d.render()


def prepare(case: str | bytes, scratch: pathlib.Path) -> str | pathlib.Path:
    """Put bytes on disk first, so that a slow disk is not mistaken for a hang in the compiler."""
    if isinstance(case, bytes):
        scratch.write_bytes(case)
        return scratch
    return case


class RunBudgetExceeded(BaseException):
    """Stops a fuzzed program. BaseException, so that nothing in the VM can swallow it."""


def run_within_budget(call: Callable[[], object], budget: int | None = RUN_BUDGET) -> bool:
    """Run ``call`` for at most ``budget`` traced Python events (None: no limit); False if it ran out."""
    if budget is None:
        call()
        return True
    remaining = budget

    def tracer(frame: FrameType, event: str, arg: Any) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise RunBudgetExceeded
        return tracer

    sys.settrace(tracer)
    try:
        call()
    except RunBudgetExceeded:
        return False
    finally:
        sys.settrace(None)
    return True


def like_the_cli(call: Callable[[], None]) -> None:
    """Run ``call`` the way `vex` does: on its big-stack thread, at its recursion limit, with empty stdin."""
    limit, stack, stdin = sys.getrecursionlimit(), threading.stack_size(), sys.stdin

    def body() -> int:
        call()
        return 0

    sys.stdin = io.StringIO("")
    try:
        _run_big_stack(body)
    finally:
        sys.stdin = stdin
        threading.stack_size(stack)
        sys.setrecursionlimit(limit)


def cli_output() -> io.TextIOWrapper:
    """A stream that encodes as the CLI's stdout does, so an unencodable string fails here as it would there."""
    return io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="\n")


def run_program(prog: Program) -> bool:
    assert prog.main is not None
    main = prog.main

    def call() -> None:
        try:
            VM(cli_output()).run_function(main)
        except (VexRuntimeError, VexExit):
            pass

    return run_within_budget(call)


def drive_repl(data: bytes, budget: int | None = RUN_BUDGET) -> None:
    stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="surrogateescape")
    run_within_budget(lambda: run_repl(stdin, cli_output()), budget)


@contextmanager
def fuzz_limits() -> Iterator[None]:
    saved = runtime.MAX_STR_LEN, runtime.MAX_LIST_LEN
    runtime.MAX_STR_LEN, runtime.MAX_LIST_LEN = FUZZ_MAX_STR, FUZZ_MAX_LIST
    try:
        yield
    finally:
        runtime.MAX_STR_LEN, runtime.MAX_LIST_LEN = saved


def as_bytes(prepared: str | pathlib.Path) -> bytes:
    if isinstance(prepared, pathlib.Path):
        return prepared.read_bytes()
    return prepared.encode("utf-8", "surrogatepass")


def assert_never_crashes(prepared: str | pathlib.Path, budget: int | None = RUN_BUDGET) -> str:
    """Enforce the contract above like the CLI would; return "rejected", "ran" or "out-of-budget"."""
    outcome = ["rejected"]

    def check() -> None:
        source = read_source(str(prepared)) if isinstance(prepared, pathlib.Path) else Source("<fuzz>", prepared)
        assert_front_end_contract(source)
        runnable = UNSAFE_TO_RUN.search(source.text) is None
        try:
            prog = compile_source(source)
        except VexError as e:
            assert e.diagnostics
            for d in e.diagnostics:
                d.render()
        else:
            if runnable:
                outcome[0] = "ran" if run_program(prog) else "out-of-budget"
        if runnable:
            drive_repl(as_bytes(prepared), budget)

    with fuzz_limits():
        like_the_cli(check)
    return outcome[0]


class Watchdog:
    """Ends the process if one input runs for too long: an in-process hang cannot be caught any other way."""

    def __init__(self, before_report: Callable[[], None]) -> None:
        self.before_report = before_report
        self.current: tuple[str, float] | None = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self) -> None:
        while not self.stopped.wait(0.25):
            current = self.current
            if current is not None and time.monotonic() - current[1] > HANG_SECONDS:
                self.before_report()
                print(f"\nHANG: {current[0]} ran for more than {HANG_SECONDS} s", file=sys.__stderr__, flush=True)
                os._exit(3)

    @contextmanager
    def timing(self, name: str) -> Iterator[None]:
        self.current = (name, time.monotonic())
        try:
            yield
        finally:
            self.current = None

    def stop(self) -> None:
        self.stopped.set()
        self.thread.join()


@pytest.fixture(scope="module")
def watchdog(request: pytest.FixtureRequest) -> Iterator[Watchdog]:
    capture = request.config.pluginmanager.getplugin("capturemanager")  # the report must not be swallowed
    dog = Watchdog(capture.suspend_global_capture)
    yield dog
    dog.stop()


@pytest.fixture(scope="module")
def scratch_file(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    return tmp_path_factory.mktemp("fuzz") / "input.vx"


@pytest.fixture
def fuzz_seed(request: pytest.FixtureRequest) -> int:
    seed = request.config.getoption("--fuzz-seed")
    assert isinstance(seed, int)
    return seed


@pytest.mark.parametrize(
    ("family", "index"), [(family, i) for family, count in DEFAULT_CASES.items() for i in range(count)]
)
def test_hostile_input_never_crashes(
    family: str, index: int, fuzz_seed: int, watchdog: Watchdog, scratch_file: pathlib.Path
) -> None:
    prepared = prepare(hostile_input(fuzz_seed, family, index), scratch_file)
    with watchdog.timing(f"seed={fuzz_seed} family={family} index={index}"):
        assert_never_crashes(prepared, repl_budget(family))


def test_harness_uses_the_cli_configuration_and_restores_it() -> None:
    before = sys.getrecursionlimit()
    seen: list[int] = []
    like_the_cli(lambda: seen.append(sys.getrecursionlimit()))
    assert seen == [60_000]
    assert sys.getrecursionlimit() == before


def test_harness_stops_a_program_that_never_ends() -> None:
    assert assert_never_crashes("fn main() { while true { } }") == "out-of-budget"


def test_harness_runs_accepted_programs_to_completion() -> None:
    assert assert_never_crashes("fn main() { print(1); }") == "ran"


def test_harness_catches_a_crash_in_an_accepted_program(monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(self: VM, code: object) -> None:
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(VM, "run_function", crash)
    with pytest.raises(UnicodeEncodeError):
        assert_never_crashes("fn main() { print(1); }")


def test_harness_output_refuses_what_the_cli_output_refuses() -> None:
    with pytest.raises(UnicodeEncodeError):
        cli_output().write("\ud800")


def test_accepted_family_really_reaches_the_vm(scratch_file: pathlib.Path) -> None:
    outcomes = [
        assert_never_crashes(prepare(hostile_input(0, "accepted", i), scratch_file)) for i in range(60)
    ]
    assert outcomes.count("ran") >= 20


EXAMPLES = sorted((ROOT / "examples").glob("*.vx"))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.stem)
def test_every_example_seed_reaches_the_vm(example: pathlib.Path, scratch_file: pathlib.Path) -> None:
    assert UNSAFE_TO_RUN.search(example.read_text(encoding="utf-8")) is None
    assert assert_never_crashes(example) in ("ran", "out-of-budget")


@pytest.mark.parametrize(
    "body",
    ["for i in 0..3 { print(i); }", "print((0..5).to_list());", 'print("ab".repeat(3));', 'print("ab".chars());',
     'print(["a"].join(","));', 'print(io.read_file("no-such-file"));', 'print(env.get("PATH").is_some());',
     'print((0..9223372036854775807).to_list());'],
    ids=["range", "to_list", "repeat", "chars", "join", "read_file", "env", "huge-to_list"],
)  # fmt: skip
def test_only_writing_files_is_excluded_from_running(body: str) -> None:
    assert UNSAFE_TO_RUN.search(body) is None
    assert assert_never_crashes(f"fn main() {{ {body} }}") == "ran"


def test_writing_files_is_never_run_by_the_fuzzer() -> None:
    assert assert_never_crashes('fn main() { io.write_file("x", "y"); }') == "rejected"


def test_fuzz_limits_apply_during_a_run_and_are_restored() -> None:
    before = runtime.MAX_STR_LEN, runtime.MAX_LIST_LEN
    with fuzz_limits():
        assert (runtime.MAX_STR_LEN, runtime.MAX_LIST_LEN) == (FUZZ_MAX_STR, FUZZ_MAX_LIST)
    assert (runtime.MAX_STR_LEN, runtime.MAX_LIST_LEN) == before


def test_harness_catches_a_crash_in_a_repl_input() -> None:
    def crash(*args: object) -> None:
        raise KeyError("a#1")

    original = run_repl.__globals__["Session"].handle
    run_repl.__globals__["Session"].handle = crash
    try:
        with pytest.raises(KeyError):
            assert_never_crashes("fn main() {}\n1\n")
    finally:
        run_repl.__globals__["Session"].handle = original


def test_scale_family_is_really_large_and_unbudgeted() -> None:
    sizes = [len(hostile_input(0, "repl_scale", i)) for i in range(DEFAULT_CASES["repl_scale"])]
    assert max(sizes) >= 24_000
    assert repl_budget("repl_scale") is None and repl_budget("soup") == RUN_BUDGET


def test_builtin_audit_covers_the_whole_standard_library() -> None:
    assert len(BUILTINS) >= 124
    tables = {b.qual.split(".")[0] for _, _, b in BUILTINS if "." in b.qual}
    assert tables >= {"Int", "Float", "Bool", "Str", "List", "Map", "Option", "Result", "Range", "math", "io", "env"}


@pytest.mark.parametrize("target", BUILTINS, ids=lambda t: t[2].qual)
def test_every_builtin_survives_hostile_arguments(target: tuple[str, str, Builtin], watchdog: Watchdog) -> None:
    """Every builtin, on the values most likely to break it: only a Vex error may stop it, and Ints stay in range."""
    kind, name, builtin = target
    rng = random.Random(f"audit/{builtin.qual}")
    assert isinstance(builtin.ty, TFn)
    returns_int = builtin.ty.ret == INT
    stdin = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        with fuzz_limits(), watchdog.timing(f"builtin {builtin.qual}"):
            for _ in range(40):
                prog = compile_text(program_calling(kind, name, builtin, rng), "<audit>")
                assert prog.main is not None
                out = io.StringIO()
                try:
                    VM(out).run_function(prog.main)
                except (VexRuntimeError, VexExit):
                    continue
                if returns_int:
                    assert INT_MIN <= int(out.getvalue()) <= INT_MAX, builtin.qual
    finally:
        sys.stdin = stdin


def test_hostile_inputs_are_reproducible() -> None:
    for family in FAMILIES:
        assert hostile_input(3, family, 11) == hostile_input(3, family, 11)
        assert hostile_input(3, family, 11) != hostile_input(4, family, 11)


def test_hostile_generators_reach_the_audited_crash_inputs() -> None:
    texts = [hostile_input(0, "soup", i) for i in range(DEFAULT_CASES["soup"])]
    literals = [hostile_input(0, "literal", i) for i in range(DEFAULT_CASES["literal"])]
    assert any(isinstance(t, str) and "²" in t for t in texts)
    assert any(isinstance(t, str) and re.search("[0-9]{4300}", t) for t in literals)
    assert any(b"\xff" in c for c in (hostile_input(0, "bytes", i) for i in range(DEFAULT_CASES["bytes"])) if isinstance(c, bytes))


def test_hostile_campaign(
    request: pytest.FixtureRequest,
    fuzz_seed: int,
    watchdog: Watchdog,
    scratch_file: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round-robin over every family; time-boxed with --fuzz-seconds, otherwise a fixed 24 inputs."""
    seconds = request.config.getoption("--fuzz-seconds")
    assert isinstance(seconds, float)
    names = list(FAMILIES)
    rotation = [name for name in names if name not in ("chain", "repl_scale")]  # slow, so they get a small share
    crashes: dict[tuple[str, str], str] = {}
    counts = dict.fromkeys(names, 0)
    total_crashes = 0
    slowest = (0.0, "")
    outcomes = {"rejected": 0, "ran": 0, "out-of-budget": 0}
    started = time.monotonic()
    index = 0
    while (time.monotonic() - started < seconds) if seconds > 0 else (index < 24):
        family = "chain" if index % 64 == 63 else "repl_scale" if index % 64 == 31 else rotation[index % len(rotation)]
        case = hostile_input(fuzz_seed, family, index)
        name = f"seed={fuzz_seed} family={family} index={index}"
        prepared = prepare(case, scratch_file)
        t0 = time.monotonic()
        try:
            with watchdog.timing(name):
                outcomes[assert_never_crashes(prepared, repl_budget(family))] += 1
        except Exception as e:  # recording every escape is the job of this harness
            total_crashes += 1
            frame = traceback.extract_tb(e.__traceback__)[-1]
            key = (type(e).__name__, f"{pathlib.Path(frame.filename).name}:{frame.lineno}")
            crashes.setdefault(key, f"{name}: {str(e)[:200]!a} on {case[:200]!a}")
        elapsed = time.monotonic() - t0
        slowest = max(slowest, (elapsed, name))
        counts[family] += 1
        index += 1
    with capsys.disabled():
        print(
            f"\nhostile fuzz: seed={fuzz_seed} seconds={time.monotonic() - started:.1f} inputs={index} "
            f"uncaught={total_crashes} distinct={len(crashes)} hangs=0 outcomes={outcomes} "
            f"slowest={slowest[0]:.3f}s ({slowest[1]}) per-family={counts}"
        )
    assert not crashes, "\n".join(f"{k}: {v}" for k, v in crashes.items())
