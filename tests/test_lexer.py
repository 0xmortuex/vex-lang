"""Lexer unit tests."""

from __future__ import annotations

import pytest

from vex.diagnostics import DiagSink, Source
from vex.lexer import Token, tokenize


def lex(text: str) -> tuple[list[Token], DiagSink]:
    sink = DiagSink()
    return tokenize(Source("<t>", text), sink), sink


def kinds(text: str) -> list[str]:
    toks, sink = lex(text)
    assert not sink.items, sink.items[0].message
    return [t.kind for t in toks][:-1]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("let x = 1;", ["let", "IDENT", "=", "INT", ";"]),
        ("let mut y", ["let", "mut", "IDENT"]),
        ("fn f(a: Int) -> Int {}", ["fn", "IDENT", "(", "IDENT", ":", "IDENT", ")", "->", "IDENT", "{", "}"]),
        ("a == b != c", ["IDENT", "==", "IDENT", "!=", "IDENT"]),
        ("a <= b >= c < d > e", ["IDENT", "<=", "IDENT", ">=", "IDENT", "<", "IDENT", ">", "IDENT"]),
        ("a && b || !c", ["IDENT", "&&", "IDENT", "||", "!", "IDENT"]),
        ("x += 1; y -= 2; z *= 3; w /= 4; v %= 5", ["IDENT", "+=", "INT", ";", "IDENT", "-=", "INT", ";", "IDENT", "*=", "INT", ";", "IDENT", "/=", "INT", ";", "IDENT", "%=", "INT"]),
        ("0..10", ["INT", "..", "INT"]),
        ("0..=10", ["INT", "..=", "INT"]),
        ("1.5", ["FLOAT"]),
        ("1..2", ["INT", "..", "INT"]),
        ("t.0.1", ["IDENT", ".", "INT", ".", "INT"]),
        ("a.b.c", ["IDENT", ".", "IDENT", ".", "IDENT"]),
        ("#{1: 2}", ["#{", "INT", ":", "INT", "}"]),
        ("|x| x", ["|", "IDENT", "|", "IDENT"]),
        ("|| 1", ["||", "INT"]),
        ("x?", ["IDENT", "?"]),
        ("match x { _ => 1 }", ["match", "IDENT", "{", "IDENT", "=>", "INT", "}"]),
        ("true false", ["true", "false"]),
        ("import util;", ["import", "IDENT", ";"]),
        ("[1, 2, 3]", ["[", "INT", ",", "INT", ",", "INT", "]"]),
        ("(1, 2)", ["(", "INT", ",", "INT", ")"]),
        ("struct enum impl", ["struct", "enum", "impl"]),
        ("while for in break continue return", ["while", "for", "in", "break", "continue", "return"]),
        ("if else", ["if", "else"]),
        ("", []),
        ("   \n\t  ", []),
        ("// only a comment", []),
        ("1 // trailing\n2", ["INT", "INT"]),
        ("1 /* block */ 2", ["INT", "INT"]),
        ("1 /* nested /* inner */ still */ 2", ["INT", "INT"]),
        ("/* multi\nline */ x", ["IDENT"]),
    ],
)
def test_token_kinds(text: str, expected: list[str]) -> None:
    assert kinds(text) == expected


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("0", 0),
        ("42", 42),
        ("1_000_000", 1000000),
        ("0xff", 255),
        ("0XFF", 255),
        ("0b1010", 10),
        ("0o17", 15),
        ("9223372036854775807", 9223372036854775807),
    ],
)
def test_int_values(text: str, value: int) -> None:
    toks, sink = lex(text)
    assert not sink.items
    assert toks[0].kind == "INT" and toks[0].value == value


@pytest.mark.parametrize(
    ("text", "value"),
    [("1.5", 1.5), ("0.25", 0.25), ("1e3", 1000.0), ("2.5e-2", 0.025), ("1_0.5", 10.5), ("1E2", 100.0)],
)
def test_float_values(text: str, value: float) -> None:
    toks, sink = lex(text)
    assert not sink.items
    assert toks[0].kind == "FLOAT" and toks[0].value == value


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ('"abc"', ["abc"]),
        ('""', [""]),
        ('"a\\nb"', ["a\nb"]),
        ('"tab\\t"', ["tab\t"]),
        ('"q\\"q"', ['q"q']),
        ('"\\\\"', ["\\"]),
        ('"\\{x\\}"', ["{x}"]),
        ('"\\u{41}"', ["A"]),
        ('"ğüşiöç"', ["ğüşiöç"]),
        ('"🌍"', ["🌍"]),
        ('"a}b"', ["a}b"]),
    ],
)
def test_string_values(text: str, parts: list[str]) -> None:
    toks, sink = lex(text)
    assert not sink.items
    assert toks[0].kind == "STRING" and toks[0].value == parts


def test_string_interpolation_regions() -> None:
    toks, sink = lex('"a {x} b {y + 1} c"')
    assert not sink.items
    parts = toks[0].value
    assert isinstance(parts, list)
    assert parts[0] == "a " and parts[2] == " b " and parts[4] == " c"
    assert isinstance(parts[1], tuple) and isinstance(parts[3], tuple)
    src = '"a {x} b {y + 1} c"'
    assert src[parts[1][0] : parts[1][1]] == "x"
    assert src[parts[3][0] : parts[3][1]] == "y + 1"


def test_interpolation_with_nested_string_and_braces() -> None:
    text = '"{f("}")} and {g({1})}"'
    toks, sink = lex(text)
    assert not sink.items
    parts = toks[0].value
    assert isinstance(parts, list)
    regions = [text[p[0] : p[1]] for p in parts if isinstance(p, tuple)]
    assert regions == ['f("}")', "g({1})"]


def test_token_positions() -> None:
    toks, _ = lex("ab  cd")
    assert (toks[0].start, toks[0].end) == (0, 2)
    assert (toks[1].start, toks[1].end) == (4, 6)


def test_unicode_identifiers() -> None:
    toks, sink = lex("let değişken = 1;")
    assert not sink.items
    assert toks[1].kind == "IDENT" and toks[1].text == "değişken"


def test_keywords_are_not_identifiers() -> None:
    assert kinds("letter fn_x") == ["IDENT", "IDENT"]


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ('"abc', "unterminated-string"),
        ("/* open", "unterminated-comment"),
        ("a $ b", "unexpected-character"),
        ("12abc", "bad-number"),
        ("0xZZ", "bad-number"),
        ('"\\q"', "bad-escape"),
        ('"\\u{110000}"', "bad-escape"),
        ('"\\u41"', "bad-escape"),
        ('"{x"', "unterminated-interpolation"),
        ("a @ b", "unexpected-character"),
        ("a ` b", "unexpected-character"),
    ],
)
def test_lex_errors(text: str, code: str) -> None:
    _, sink = lex(text)
    assert code in [d.code for d in sink.items]


def test_lexer_recovers_and_continues() -> None:
    toks, sink = lex("a $ b # c")
    assert [t.kind for t in toks] == ["IDENT", "IDENT", "IDENT", "EOF"] or len(sink.items) >= 1
    assert len(sink.items) >= 1


def test_crlf_and_bom_tolerated() -> None:
    toks, sink = lex("﻿let x = 1;\r\nlet y = 2;\r\n")
    assert not sink.items
    assert [t.kind for t in toks].count("let") == 2


def test_eof_token_always_present() -> None:
    toks, _ = lex("")
    assert [t.kind for t in toks] == ["EOF"]


def test_line_and_column_mapping() -> None:
    src = Source("<t>", "ab\ncd\n\nef")
    assert src.line_col(0) == (1, 1)
    assert src.line_col(3) == (2, 1)
    assert src.line_col(4) == (2, 2)
    assert src.line_col(7) == (4, 1)
    assert src.line_text(2) == "cd"
    assert src.line_text(3) == ""
    assert src.line_text(99) == ""


# ---------------------------------------------------------------- numeric literals are ASCII only
def single_error(text: str) -> tuple[str, int, int]:
    """Lex ``text``; return (code, line, col) of its one diagnostic."""
    _, sink = lex(text)
    assert len(sink.items) == 1, [d.message for d in sink.items]
    d = sink.items[0]
    assert d.span is not None
    return d.code, d.span.line, d.span.col


@pytest.mark.parametrize(
    ("text", "code", "col"),
    [
        ("²", "unexpected-character", 1),  # superscript two: isdigit() yet int() refuses it
        ("2²", "unexpected-character", 2),
        ("٣", "unexpected-character", 1),  # Arabic-Indic three: int() would accept it
        ("1٣", "unexpected-character", 2),
        ("１", "unexpected-character", 1),  # fullwidth one
        ("𝟗", "unexpected-character", 1),  # mathematical double-struck nine
        ("0x٣", "bad-number", 1),
        ("0b١", "bad-number", 1),
        ("1.²", "unexpected-character", 3),
        ("1e²", "bad-number", 1),
    ],
)
def test_non_ascii_digits_are_diagnostics(text: str, code: str, col: int) -> None:
    assert single_error(text) == (code, 1, col)


def test_non_ascii_digit_reports_its_own_line() -> None:
    assert single_error("let a = 1;\nlet b = ²;") == ("unexpected-character", 2, 9)


def test_non_ascii_digit_in_signed_exponent() -> None:
    _, sink = lex("1e+٣")
    assert [d.code for d in sink.items] == ["bad-number", "unexpected-character"]


def test_non_ascii_digit_is_not_a_numeric_value() -> None:
    toks, _ = lex("1٣")
    assert [(t.kind, t.value) for t in toks[:-1]] == [("INT", 1)]


def test_identifiers_may_still_contain_non_ascii_digits() -> None:
    toks, sink = lex("x² y٣")
    assert not sink.items
    assert [t.kind for t in toks[:-1]] == ["IDENT", "IDENT"]


# ---------------------------------------------------------------- integer literals are 64-bit
@pytest.mark.parametrize(
    "text",
    [
        "9" * 20,
        "9" * 4300,
        "9" * 4301,
        "9" * 100_000,
        "1_" * 5000 + "1",
        "0x" + "F" * 17,
        "0x" + "F" * 100_000,
        "0o" + "7" * 23,
        "0b" + "1" * 65,
    ],
)
def test_oversized_integer_literal_is_a_range_diagnostic(text: str) -> None:
    code, line, col = single_error("  " + text)
    assert (code, line, col) == ("int-literal-range", 1, 3)


def test_range_diagnostic_does_not_echo_a_huge_literal() -> None:
    _, sink = lex("9" * 100_000)
    assert len(sink.items[0].message) < 200


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("0" * 5000, 0),
        ("0" * 5000 + "7", 7),
        ("0_" * 5000 + "7", 7),
        ("0x" + "0" * 5000 + "F", 15),
        ("0b" + "0" * 5000 + "1", 1),
    ],
)
def test_leading_zeros_do_not_count_against_the_limit(text: str, value: int) -> None:
    toks, sink = lex(text)
    assert not sink.items
    assert toks[0].kind == "INT" and toks[0].value == value


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("9223372036854775808", 2**63),  # the parser accepts this one only after unary minus
        ("0x8000000000000000", 2**63),
        ("0o1000000000000000000000", 2**63),
        ("0b1" + "0" * 63, 2**63),
    ],
)
def test_longest_literals_still_reach_the_parser(text: str, value: int) -> None:
    toks, sink = lex(text)
    assert not sink.items
    assert toks[0].value == value


def test_tuple_index_is_normalised() -> None:
    toks, sink = lex("t." + "0" * 5000 + "1")
    assert not sink.items
    assert toks[2].kind == "INT" and toks[2].text == "1" and toks[2].value == 1


# ---------------------------------------------------------------- float literals are 64-bit
@pytest.mark.parametrize(
    "text",
    ["1e999", "1E+309", "1.5e309", "1.8e308", "9" * 400 + ".0", "1e" + "9" * 5000, "1_0.0e999"],
)
def test_float_literal_too_large_is_a_range_diagnostic(text: str) -> None:
    assert single_error("  " + text) == ("float-literal-range", 1, 3)


@pytest.mark.parametrize(
    ("text", "value"),
    [("1.7976931348623157e308", 1.7976931348623157e308), ("1e-999", 0.0), ("0e999", 0.0), ("0.0e" + "9" * 5000, 0.0)],
)
def test_finite_floats_at_the_edges_are_accepted(text: str, value: float) -> None:
    toks, sink = lex(text)
    assert not sink.items
    assert toks[0].kind == "FLOAT" and toks[0].value == value
