"""Property / fuzz tests: the compiler must only ever produce Vex diagnostics, never a Python traceback."""

from __future__ import annotations

import io
import pathlib
import random

import pytest

from vex.api import compile_text
from vex.diagnostics import VexError, VexRuntimeError
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
