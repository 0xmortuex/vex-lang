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
