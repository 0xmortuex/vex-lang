"""Source files, spans and diagnostics (the only way the compiler reports problems)."""

from __future__ import annotations

import bisect
import difflib
from collections.abc import Iterable
from dataclasses import dataclass, field


class Source:
    """A source file: path, text and a line-start table for offset -> line:col."""

    def __init__(self, path: str, text: str, decode_error: tuple[int, int] | None = None) -> None:
        self.path = path
        self.text = text
        self.decode_error = decode_error  # (text offset, byte value) of the first byte that is not UTF-8
        self._starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self._starts.append(i + 1)

    def line_col(self, offset: int) -> tuple[int, int]:
        """1-based (line, column) for a character offset."""
        line = bisect.bisect_right(self._starts, offset) - 1
        return line + 1, offset - self._starts[line] + 1

    def line_text(self, line: int) -> str:
        if line < 1 or line > len(self._starts):
            return ""
        start = self._starts[line - 1]
        end = self._starts[line] - 1 if line < len(self._starts) else len(self.text)
        return self.text[start:end].rstrip("\r")


def first_undecodable(text: str) -> int | None:
    """Offset of the first byte that stdin's surrogateescape handler could not decode as UTF-8."""
    for i, ch in enumerate(text):
        if "\udc80" <= ch <= "\udcff":
            return i
    return None


@dataclass(frozen=True)
class Span:
    source: Source
    start: int
    end: int

    @property
    def line(self) -> int:
        return self.source.line_col(self.start)[0]

    @property
    def col(self) -> int:
        return self.source.line_col(self.start)[1]

    def to(self, other: Span) -> Span:
        return Span(self.source, self.start, max(self.end, other.end))

    def __str__(self) -> str:
        return f"{self.source.path}:{self.line}:{self.col}"


@dataclass
class Diagnostic:
    code: str
    message: str
    span: Span | None = None
    label: str = ""
    severity: str = "error"
    notes: list[str] = field(default_factory=list)
    help: str | None = None
    secondary: list[tuple[Span, str]] = field(default_factory=list)

    def render(self) -> str:
        head = f"{self.severity}[{self.code}]: {self.message}"
        if self.span is None:
            return head + "".join(f"\n  = note: {n}" for n in self.notes)
        out = [head, _snippet(self.span, self.label, primary=True)]
        for sp, lab in self.secondary:
            out.append(_snippet(sp, lab, primary=False))
        gutter = " " * len(str(self.span.line))
        for n in self.notes:
            out.append(f"{gutter} = note: {n}")
        if self.help:
            out.append(f"{gutter} = help: {self.help}")
        return "\n".join(out)


def _snippet(span: Span, label: str, primary: bool) -> str:
    line, col = span.source.line_col(span.start)
    text = span.source.line_text(line)
    end_line, end_col = span.source.line_col(max(span.start, span.end - 1))
    width = (end_col - col + 1) if end_line == line else max(1, len(text) - col + 1)
    width = max(1, width)
    num = str(line)
    pad = " " * len(num)
    # Keep tabs so the caret lines up under the source text.
    lead = "".join("\t" if c == "\t" else " " for c in text[: col - 1])
    mark = ("^" if primary else "-") * width
    arrow = "-->" if primary else ":::"
    lines = [
        f"{pad}{arrow} {span.source.path}:{line}:{col}",
        f"{pad} |",
        f"{num} | {text}",
        f"{pad} | {lead}{mark}" + (f" {label}" if label else ""),
    ]
    return "\n".join(lines)


class VexError(Exception):
    """Raised when compilation fails. Carries every collected diagnostic."""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        super().__init__(render_all(diagnostics))
        self.diagnostics = diagnostics


def too_deep_error(exc: RecursionError, source: Source) -> VexError:
    """A too-deep diagnostic at the innermost syntax node that was being processed when ``exc`` was raised."""
    span = Span(source, 0, 1)
    tb = exc.__traceback__
    while tb is not None:
        for value in tb.tb_frame.f_locals.values():
            candidate = getattr(value, "span", None)
            if isinstance(candidate, Span):
                span = candidate
                break
        tb = tb.tb_next
    return VexError([Diagnostic("too-deep", "program is nested too deeply to compile", span,
                                "simplify the deepest expression")])  # fmt: skip


def out_of_memory_error(source: Source) -> VexError:
    return VexError([Diagnostic("out-of-memory", "not enough memory to compile this program", Span(source, 0, 1))])


class VexRuntimeError(Exception):
    """A runtime failure with a source-mapped stack trace."""

    def __init__(self, message: str, trace: list[tuple[str, Span | None]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.trace = trace or []

    def render(self, max_frames: int = 12) -> str:
        out = [f"runtime error: {self.message}"]
        if self.trace and self.trace[0][1] is not None:
            top = self.trace[0][1]
            assert top is not None
            out.append(_snippet(top, "", primary=True))
        out.append("stack trace (most recent call first):")
        frames = self.trace
        if len(frames) > max_frames:
            head = frames[: max_frames // 2]
            tail = frames[-(max_frames // 2) :]
            skipped = len(frames) - len(head) - len(tail)
            shown: list[tuple[str, Span | None] | None] = [*head, None, *tail]
            for fr in shown:
                if fr is None:
                    out.append(f"  ... {skipped} more frames ...")
                else:
                    out.append(_frame_line(fr))
        else:
            out.extend(_frame_line(fr) for fr in frames)
        return "\n".join(out)


def _frame_line(fr: tuple[str, Span | None]) -> str:
    name, span = fr
    return f"  in {name} at {span}" if span is not None else f"  in {name}"


def render_all(diags: Iterable[Diagnostic]) -> str:
    return "\n\n".join(d.render() for d in diags)


def suggest(name: str, candidates: Iterable[str]) -> str | None:
    """The closest candidate to ``name`` (for "did you mean"), or None."""
    pool = sorted({c for c in candidates if c != name})
    lowered = {c.lower(): c for c in pool}
    if name.lower() in lowered:
        return lowered[name.lower()]
    close = difflib.get_close_matches(name, pool, n=1, cutoff=0.6)
    return close[0] if close else None


class DiagSink:
    """Collects diagnostics; the compiler stages report here and keep going."""

    MAX_ERRORS = 50

    def __init__(self) -> None:
        self.items: list[Diagnostic] = []
        self._capped = False

    def add(self, d: Diagnostic) -> None:
        if len(self.items) >= self.MAX_ERRORS:
            if not self._capped:
                self._capped = True
                self.items.append(Diagnostic("too-many-errors", f"too many diagnostics; stopping after {self.MAX_ERRORS}"))
            return
        for old in self.items:
            if (
                old.code == d.code
                and old.message == d.message
                and old.span is not None
                and d.span is not None
                and (old.span.source, old.span.start) == (d.span.source, d.span.start)
            ):
                return
        self.items.append(d)

    def error(
        self,
        code: str,
        message: str,
        span: Span | None,
        label: str = "",
        *,
        help: str | None = None,
        notes: list[str] | None = None,
        secondary: list[tuple[Span, str]] | None = None,
    ) -> None:
        self.add(Diagnostic(code, message, span, label, "error", notes or [], help, secondary or []))

    def warning(self, code: str, message: str, span: Span | None, label: str = "") -> None:
        self.add(Diagnostic(code, message, span, label, "warning"))

    @property
    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.items)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.items if d.severity == "error"]

    def sorted(self) -> list[Diagnostic]:
        def key(d: Diagnostic) -> tuple[str, int]:
            return (d.span.source.path, d.span.start) if d.span else ("", 0)

        return sorted(self.items, key=key)

    def raise_if_errors(self) -> None:
        if self.has_errors:
            raise VexError(self.sorted())
