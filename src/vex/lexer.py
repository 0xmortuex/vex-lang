"""Lexer: source text -> tokens. String literals carry interpolation regions."""

from __future__ import annotations

import math
from dataclasses import dataclass

from vex.diagnostics import DiagSink, Source, Span

KEYWORDS = frozenset(
    {
        "let", "mut", "fn", "struct", "enum", "impl", "match", "if", "else", "while", "for", "in",
        "return", "break", "continue", "import", "true", "false",
    }
)  # fmt: skip

# Longest first so that "..=" wins over ".." and "<=" over "<".
PUNCT = [
    "..=", "==", "!=", "<=", ">=", "&&", "||", "+=", "-=", "*=", "/=", "%=", "->", "=>", "..", "#{",
    "+", "-", "*", "/", "%", "<", ">", "=", "!", ".", ",", ";", ":", "?", "(", ")", "[", "]", "{", "}", "|",
]  # fmt: skip

StrPart = str | tuple[int, int]

# Numeric literals are ASCII only. str.isdigit() also accepts characters such as "²" and "٣".
DIGITS = "0123456789"
BASE_DIGITS = {2: "01", 8: "01234567", 16: DIGITS + "abcdefABCDEF"}

INT_MAX = 2**63 - 1
INT_MIN = -(2**63)
# Significant digits needed to write 2**63, the largest magnitude a literal can have (it is only valid
# after unary minus). A literal with more digits is out of range and is never converted to a Python int.
MAX_INT_DIGITS = {2: 64, 8: 22, 10: 19, 16: 16}
INT_RANGE_LABEL = "does not fit in a 64-bit Int"
INT_RANGE_HELP = f"Int is a signed 64-bit integer ({INT_MIN} to {INT_MAX})"


def int_range_message(literal: str) -> str:
    shown = literal if len(literal) <= 32 else f"{literal[:16]}...{literal[-8:]}"
    return f"integer literal `{shown}` is out of range"

ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "{": "{", "}": "}"}


@dataclass
class Token:
    kind: str  # "IDENT" "INT" "FLOAT" "STRING" "EOF" or the keyword/punctuation text
    text: str
    start: int
    end: int
    value: object = None  # int / float / list[StrPart]


class Lexer:
    def __init__(self, source: Source, sink: DiagSink, start: int = 0, end: int | None = None) -> None:
        self.src = source
        self.text = source.text
        self.pos = start
        self.end = len(self.text) if end is None else end
        self.sink = sink
        self.tokens: list[Token] = []

    def span(self, a: int, b: int) -> Span:
        return Span(self.src, a, max(b, a + 1))

    def error(self, code: str, msg: str, a: int, b: int, label: str = "", help: str | None = None) -> None:
        self.sink.error(code, msg, self.span(a, b), label, help=help)

    def lex(self) -> list[Token]:
        while True:
            self._skip_trivia()
            if self.pos >= self.end:
                break
            ch = self.text[self.pos]
            if ch in DIGITS:
                self._number()
            elif ch.isalpha() or ch == "_":
                self._ident()
            elif ch == '"':
                self._string()
            else:
                self._punct()
        self.tokens.append(Token("EOF", "", self.end, self.end))
        return self.tokens

    def _skip_trivia(self) -> None:
        t, n = self.text, self.end
        while self.pos < n:
            ch = t[self.pos]
            if ch in " \t\r\n":
                self.pos += 1
            elif ch == "/" and self.pos + 1 < n and t[self.pos + 1] == "/":
                while self.pos < n and t[self.pos] != "\n":
                    self.pos += 1
            elif ch == "/" and self.pos + 1 < n and t[self.pos + 1] == "*":
                start = self.pos
                depth = 1
                self.pos += 2
                while self.pos < n and depth:
                    if t.startswith("/*", self.pos):
                        depth += 1
                        self.pos += 2
                    elif t.startswith("*/", self.pos):
                        depth -= 1
                        self.pos += 2
                    else:
                        self.pos += 1
                if depth:
                    self.error("unterminated-comment", "unterminated block comment", start, start + 2,
                               "this comment is never closed")  # fmt: skip
            elif ch == "﻿":
                self.pos += 1
            else:
                return

    def _ident(self) -> None:
        t = self.text
        start = self.pos
        while self.pos < self.end and (t[self.pos].isalnum() or t[self.pos] == "_"):
            self.pos += 1
        word = t[start : self.pos]
        kind = word if word in KEYWORDS else "IDENT"
        self.tokens.append(Token(kind, word, start, self.pos))

    def _number(self) -> None:
        t = self.text
        start = self.pos
        after_dot = bool(self.tokens) and self.tokens[-1].kind == "."
        if t[start] == "0" and start + 1 < self.end and t[start + 1] in "xXbBoO":
            base = {"x": 16, "b": 2, "o": 8}[t[start + 1].lower()]
            self.pos += 2
            digits_start = self.pos
            while self.pos < self.end and (t[self.pos].isalnum() or t[self.pos] == "_"):
                self.pos += 1
            raw = t[digits_start : self.pos].replace("_", "")
            if raw and all(c in BASE_DIGITS[base] for c in raw):
                value = self._int_value(raw, base, start)
            else:
                self.error("bad-number", f"invalid base-{base} integer literal", start, self.pos,
                           "not a valid number")  # fmt: skip
                value = 0
            self.tokens.append(Token("INT", t[start : self.pos], start, self.pos, value))
            return
        while self.pos < self.end and (t[self.pos] in DIGITS or t[self.pos] == "_"):
            self.pos += 1
        is_float = False
        # "1.5" is a float but "1..2" and "t.0.1" must not be.
        if (
            not after_dot
            and self.pos + 1 < self.end
            and t[self.pos] == "."
            and t[self.pos + 1] in DIGITS
        ):
            is_float = True
            self.pos += 1
            while self.pos < self.end and (t[self.pos] in DIGITS or t[self.pos] == "_"):
                self.pos += 1
        if not after_dot and self.pos < self.end and t[self.pos] in "eE":
            save = self.pos
            self.pos += 1
            if self.pos < self.end and t[self.pos] in "+-":
                self.pos += 1
            if self.pos < self.end and t[self.pos] in DIGITS:
                is_float = True
                while self.pos < self.end and t[self.pos] in DIGITS:
                    self.pos += 1
            else:
                self.pos = save
        raw = t[start : self.pos].replace("_", "")
        if self.pos < self.end and (t[self.pos].isalpha() or t[self.pos] == "_"):
            # e.g. 12abc: report once, swallow the identifier tail.
            tail = self.pos
            while self.pos < self.end and (t[self.pos].isalnum() or t[self.pos] == "_"):
                self.pos += 1
            self.error("bad-number", "invalid suffix on number literal", start, self.pos,
                       f"`{t[tail:self.pos]}` is not a valid suffix")  # fmt: skip
        if is_float:
            number = float(raw)
            if math.isinf(number):
                self.error("float-literal-range", "float literal is out of range", start, self.pos,
                           "too large for a 64-bit Float",
                           "the largest Float is about 1.8e308")  # fmt: skip
                number = 0.0
            self.tokens.append(Token("FLOAT", raw, start, self.pos, number))
        else:
            value = self._int_value(raw, 10, start)
            # After a dot this is a tuple index, which later stages read back from the token text.
            self.tokens.append(Token("INT", str(value) if after_dot else raw, start, self.pos, value))

    def _int_value(self, digits: str, base: int, start: int) -> int:
        significant = digits.lstrip("0")
        if len(significant) > MAX_INT_DIGITS[base]:
            self.error("int-literal-range", int_range_message(self.text[start : self.pos]), start, self.pos,
                       INT_RANGE_LABEL, INT_RANGE_HELP)  # fmt: skip
            return 0
        return int(significant or "0", base)

    def _punct(self) -> None:
        t = self.text
        for p in PUNCT:
            if t.startswith(p, self.pos) and self.pos + len(p) <= self.end:
                self.tokens.append(Token(p, p, self.pos, self.pos + len(p)))
                self.pos += len(p)
                return
        ch = t[self.pos]
        self.error("unexpected-character", f"unexpected character {ch!r}", self.pos, self.pos + 1,
                   "not part of the Vex language")  # fmt: skip
        self.pos += 1

    def _string(self) -> None:
        t = self.text
        start = self.pos
        self.pos += 1
        parts: list[StrPart] = []
        buf: list[str] = []
        while True:
            if self.pos >= self.end:
                self.error("unterminated-string", "unterminated string literal", start, start + 1,
                           "this string is never closed", "add a closing `\"`")  # fmt: skip
                break
            ch = t[self.pos]
            if ch == '"':
                self.pos += 1
                break
            if ch == "\\":
                self._escape(buf)
            elif ch == "{":
                close = self._interp_end(self.pos + 1)
                if close < 0:
                    self.error("unterminated-interpolation", "unterminated `{` in string", self.pos,
                               self.pos + 1, "this interpolation is never closed",
                               "write `\\{` for a literal brace")  # fmt: skip
                    self.pos = self.end
                    break
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append((self.pos + 1, close))
                self.pos = close + 1
            else:
                buf.append(ch)
                self.pos += 1
        if buf or not parts:
            parts.append("".join(buf))
        self.tokens.append(Token("STRING", t[start : self.pos], start, self.pos, parts))

    def _escape(self, buf: list[str]) -> None:
        t = self.text
        start = self.pos
        self.pos += 1
        if self.pos >= self.end:
            return
        c = t[self.pos]
        if c == "u":
            if self.pos + 1 < self.end and t[self.pos + 1] == "{":
                close = t.find("}", self.pos)
                digits = t[self.pos + 2 : close] if close != -1 else ""
                try:
                    buf.append(chr(int(digits, 16)))
                except (ValueError, OverflowError):
                    self.error("bad-escape", "invalid unicode escape", start, max(close, self.pos) + 1,
                               "expected `\\u{XXXX}` with a valid code point")  # fmt: skip
                self.pos = (close if close != -1 else self.pos) + 1
                return
            self.error("bad-escape", "invalid unicode escape", start, self.pos + 1,
                       "expected `\\u{XXXX}`")  # fmt: skip
            self.pos += 1
            return
        if c in ESCAPES:
            buf.append(ESCAPES[c])
        else:
            self.error("bad-escape", f"unknown escape sequence `\\{c}`", start, self.pos + 1,
                       "unknown escape", "valid escapes: \\n \\t \\r \\0 \\\\ \\\" \\{ \\} \\u{...}")  # fmt: skip
        self.pos += 1

    def _interp_end(self, i: int) -> int:
        """Index of the `}` closing an interpolation that starts at ``i``; -1 if none."""
        t = self.text
        depth = 1
        while i < self.end:
            ch = t[i]
            if ch == '"':
                i += 1
                while i < self.end and t[i] != '"':
                    i += 2 if t[i] == "\\" else 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1


def tokenize(source: Source, sink: DiagSink) -> list[Token]:
    return Lexer(source, sink).lex()
