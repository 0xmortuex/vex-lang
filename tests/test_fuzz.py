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

import pytest

from vex.api import compile_source, compile_text, read_source
from vex.diagnostics import DiagSink, Source, VexError, VexRuntimeError
from vex.parser import parse_source
from vex.runtime import INT_MAX, INT_MIN
from vex.vm import VM

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
#   4. no input takes longer than HANG_SECONDS. The watchdog ends the process if one does.
#
# Inputs are a pure function of (seed, family, index), so any failure can be replayed.
# `pytest tests/test_fuzz.py --fuzz-seconds=60 --fuzz-seed=7` runs a time-boxed campaign.

HANG_SECONDS = 5.0

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


FAMILIES: dict[str, Callable[[random.Random], str | bytes]] = {
    "soup": gen_soup,
    "literal": gen_literal,
    "nesting": gen_nesting,
    "unicode": gen_unicode,
    "mutated": gen_mutated,
    "bytes": gen_bytes,
}
DEFAULT_CASES = {"soup": 300, "literal": 200, "nesting": 150, "unicode": 300, "mutated": 300, "bytes": 300}


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


def assert_never_crashes(prepared: str | pathlib.Path) -> None:
    source = read_source(str(prepared)) if isinstance(prepared, pathlib.Path) else Source("<fuzz>", prepared)
    assert_front_end_contract(source)
    try:
        compile_source(source)
    except VexError as e:
        assert e.diagnostics
        for d in e.diagnostics:
            d.render()


class Watchdog:
    """Ends the process if one input runs for too long: an in-process hang cannot be caught any other way."""

    def __init__(self) -> None:
        self.current: tuple[str, float] | None = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self) -> None:
        while not self.stopped.wait(0.25):
            current = self.current
            if current is not None and time.monotonic() - current[1] > HANG_SECONDS:
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
def watchdog() -> Iterator[Watchdog]:
    dog = Watchdog()
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
        assert_never_crashes(prepared)


def test_hostile_inputs_are_reproducible() -> None:
    for family in FAMILIES:
        assert hostile_input(3, family, 11) == hostile_input(3, family, 11)
        assert hostile_input(3, family, 11) != hostile_input(4, family, 11)


def test_hostile_generators_reach_the_audited_crash_inputs() -> None:
    texts = [hostile_input(0, "soup", i) for i in range(DEFAULT_CASES["soup"])]
    literals = [hostile_input(0, "literal", i) for i in range(DEFAULT_CASES["literal"])]
    assert any(isinstance(t, str) and "²" in t for t in texts)
    assert any(isinstance(t, str) and re.search("[0-9]{4300}", t) for t in literals)
    assert any(b"\xff" in c for c in (hostile_input(0, "bytes", i) for i in range(300)) if isinstance(c, bytes))


def test_hostile_campaign(
    request: pytest.FixtureRequest,
    fuzz_seed: int,
    watchdog: Watchdog,
    scratch_file: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round-robin over every family; time-boxed with --fuzz-seconds, otherwise a fixed 120 inputs."""
    seconds = request.config.getoption("--fuzz-seconds")
    assert isinstance(seconds, float)
    names = list(FAMILIES)
    crashes: dict[tuple[str, str], str] = {}
    counts = dict.fromkeys(names, 0)
    total_crashes = 0
    slowest = (0.0, "")
    started = time.monotonic()
    index = 0
    while (time.monotonic() - started < seconds) if seconds > 0 else (index < 120):
        family = names[index % len(names)]
        case = hostile_input(fuzz_seed, family, index)
        name = f"seed={fuzz_seed} family={family} index={index}"
        prepared = prepare(case, scratch_file)
        t0 = time.monotonic()
        try:
            with watchdog.timing(name):
                assert_never_crashes(prepared)
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
            f"uncaught={total_crashes} distinct={len(crashes)} hangs=0 slowest={slowest[0]:.3f}s ({slowest[1]}) "
            f"per-family={counts}"
        )
    assert not crashes, "\n".join(f"{k}: {v}" for k, v in crashes.items())
