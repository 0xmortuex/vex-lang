"""REPL tests: persistent state, multi-line input, error recovery and rollback."""

from __future__ import annotations

import io

import pytest

from vex.diagnostics import VexError, VexRuntimeError
from vex.repl import Session, is_incomplete, run_repl


def session() -> tuple[Session, io.StringIO]:
    out = io.StringIO()
    return Session(out), out


def feed(text: str) -> str:
    """Run a whole script through the line-oriented REPL driver and return its output."""
    out = io.StringIO()
    code = run_repl(io.StringIO(text), out)
    assert code == 0
    return out.getvalue()


@pytest.mark.parametrize(
    ("src", "echo"),
    [
        ("1 + 2", "3 : Int"),
        ("2.5 * 2.0", "5.0 : Float"),
        ('"a" + "b"', '"ab" : Str'),
        ("true && false", "false : Bool"),
        ("[1, 2, 3]", "[1, 2, 3] : List[Int]"),
        ("(1, \"x\")", '(1, "x") : (Int, Str)'),
        ("Some(3)", "Some(3) : Option[Int]"),
        ('#{"a": 1}', '{"a": 1} : Map[Str, Int]'),
        ("[1, 2].map(|x| x * 2)", "[2, 4] : List[Int]"),
        ("1..4", "1..4 : Range"),
        ("-7 / 2", "-3 : Int"),
        ("\"ğüşiöç\".len()", "6 : Int"),
    ],
)
def test_expression_echo(src: str, echo: str) -> None:
    s, _ = session()
    assert s.eval(src) == echo


def test_unit_expressions_echo_nothing() -> None:
    s, out = session()
    assert s.eval('print("hi")') == ""
    assert out.getvalue() == "hi\n"


def test_let_persists_across_inputs() -> None:
    s, _ = session()
    assert s.eval("let x = 5;") == ""
    assert s.eval("x * 2") == "10 : Int"
    assert s.eval("let mut y = 1;") == ""
    s.eval("y = y + 41;")
    assert s.eval("y") == "42 : Int"


def test_let_can_be_shadowed_with_a_new_type() -> None:
    s, _ = session()
    s.eval("let x = 1;")
    s.eval('let x = "now a string";')
    assert s.eval("x") == '"now a string" : Str'


def test_functions_persist_and_can_be_redefined() -> None:
    s, _ = session()
    s.eval("fn double(n: Int) -> Int { n * 2 }")
    assert s.eval("double(21)") == "42 : Int"
    s.eval("fn double(n: Int) -> Int { n * 3 }")
    assert s.eval("double(21)") == "63 : Int"


def test_old_functions_keep_old_globals_after_shadowing() -> None:
    s, _ = session()
    s.eval("let k = 1;")
    s.eval("fn get() -> Int { k }")
    s.eval('let k = "text";')
    assert s.eval("get()") == "1 : Int"


def test_structs_enums_impls_persist() -> None:
    s, _ = session()
    s.eval("struct P { x: Int, y: Int }")
    s.eval("impl P { fn sum(self) -> Int { self.x + self.y } }")
    s.eval("enum Shape { Dot, Sq(Int) }")
    assert s.eval("P { x: 2, y: 3 }.sum()") == "5 : Int"
    assert s.eval("Shape.Sq(4)") == "Sq(4) : Shape"
    s.eval("fn area(s: Shape) -> Int { match s { Dot => 0, Sq(n) => n * n } }")
    assert s.eval("area(Shape.Sq(5))") == "25 : Int"


def test_several_statements_in_one_input() -> None:
    s, _ = session()
    assert s.eval("let a = 2; let b = 3; a * b") == "6 : Int"


def test_compile_error_is_raised_and_state_is_rolled_back() -> None:
    s, _ = session()
    s.eval("let x = 1;")
    with pytest.raises(VexError) as info:
        s.eval("fn broken() -> Int { \"s\" }")
    assert info.value.diagnostics[0].code == "type-mismatch"
    with pytest.raises(VexError):
        s.eval("broken()")  # the failed definition must not linger
    assert s.eval("x") == "1 : Int"


def test_failed_let_does_not_define_the_variable() -> None:
    s, _ = session()
    with pytest.raises(VexError):
        s.eval('let bad: Int = "no";')
    with pytest.raises(VexError):
        s.eval("bad")


def test_runtime_error_keeps_session_alive() -> None:
    s, _ = session()
    s.eval("let xs = [1];")
    with pytest.raises(VexRuntimeError):
        s.eval("xs[9]")
    assert s.eval("xs[0]") == "1 : Int"


def test_mutation_through_globals() -> None:
    s, _ = session()
    s.eval("let mut xs = [1];")
    s.eval("xs.push(2);")
    s.eval("xs.push(3);")
    assert s.eval("xs") == "[1, 2, 3] : List[Int]"


def test_closures_capture_globals() -> None:
    s, _ = session()
    s.eval("let base = 100;")
    s.eval("let add = |n: Int| n + base;")
    assert s.eval("add(5)") == "105 : Int"


def test_type_command_does_not_run_code() -> None:
    out = feed('let x = 1;\n:type [x, 2]\n:type "s".len()\nprint("side effect")\n')
    assert out.splitlines()[0] == "List[Int]"
    assert out.splitlines()[1] == "Int"
    assert out.count("side effect") == 1  # only the real print


def test_multiline_input_continues() -> None:
    out = feed("fn f(n: Int) -> Int {\n    n + 1\n}\nf(1)\n")
    assert out == "2 : Int\n"


def test_multiline_match() -> None:
    out = feed("match Some(3) {\n  Some(v) => v,\n  None => 0,\n}\n")
    assert out == "3 : Int\n"


def test_unterminated_string_continues() -> None:
    out = feed('"a\nb"\n')
    assert out == '"a\\nb" : Str\n'


def test_error_output_contains_diagnostic() -> None:
    out = feed("1 + \"a\"\n")
    assert "error[type-mismatch]" in out
    assert "<repl:1>:1:1" in out


def test_errors_do_not_stop_the_loop() -> None:
    out = feed("oops\n1 + 1\n")
    assert "error[undefined-name]" in out
    assert out.rstrip().endswith("2 : Int")


def test_help_reset_and_unknown_commands() -> None:
    out = feed(":help\nlet a = 1;\n:reset\na\n:nope\n")
    assert ":quit" in out
    assert "state cleared" in out
    assert "error[undefined-name]" in out
    assert "unknown command :nope" in out


def test_quit_stops_reading() -> None:
    out = feed("1\n:quit\n2\n")
    assert out == "1 : Int\n"


def test_blank_lines_and_comments_are_ignored() -> None:
    out = feed("\n\n// comment only\n3\n")
    assert out == "3 : Int\n"


def test_exit_function_ends_session_with_code() -> None:
    out = io.StringIO()
    code = run_repl(io.StringIO("exit(4)\n1\n"), out)
    assert code == 4
    assert out.getvalue() == ""


def test_repl_prints_to_the_session_stream() -> None:
    s, out = session()
    s.eval("for i in 0..3 { print(i); }")
    assert out.getvalue() == "0\n1\n2\n"


def test_cannot_infer_in_repl_is_a_diagnostic() -> None:
    s, _ = session()
    with pytest.raises(VexError) as info:
        s.eval("let e = [];")
    assert info.value.diagnostics[0].code == "cannot-infer"


@pytest.mark.parametrize(
    ("text", "incomplete"),
    [
        ("1 + 2", False),
        ("fn f() {", True),
        ("[1, 2", True),
        ("(1,", True),
        ('"abc', True),
        ("/* open", True),
        ("#{1: 2", True),
        ("f(1)", False),
        ("{ }", False),
        ("", False),
        ("}", False),
    ],
)
def test_is_incomplete(text: str, incomplete: bool) -> None:
    assert is_incomplete(text) is incomplete


# ---------------------------------------------------------------- stdin that is not UTF-8
def repl_on_bytes(data: bytes) -> tuple[int, str]:
    """Drive the REPL through the same stream configuration the CLI gives stdin."""
    stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="surrogateescape")
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="\n")
    code = run_repl(stdin, out)
    out.seek(0)
    return code, out.buffer.getvalue().decode("utf-8")


def test_invalid_utf8_line_is_a_positioned_diagnostic() -> None:
    code, out = repl_on_bytes(b"1 + \xff\n")
    assert code == 0
    assert "invalid-utf8" in out and "<repl:1>:1:5" in out


@pytest.mark.parametrize(
    "data",
    [b"\xff\n", b":\xff\n", b":type \xff\n", b'"\xc0\xaf"\n', b"\xed\xa0\x80\n", b"\xe2\x82"],
    ids=["bare", "command", "type-command", "overlong-in-string", "surrogate", "truncated-at-eof"],
)
def test_invalid_utf8_never_crashes_the_repl(data: bytes) -> None:
    code, out = repl_on_bytes(data)
    assert code == 0
    assert "invalid-utf8" in out


def test_repl_keeps_working_after_invalid_utf8() -> None:
    _, out = repl_on_bytes(b"\xff\n1 + 1\n")
    assert "invalid-utf8" in out and "2 : Int" in out


def test_invalid_utf8_discards_the_unfinished_input() -> None:
    _, out = repl_on_bytes(b"(\n\xff\n1 + 1\n")
    assert "invalid-utf8" in out and "2 : Int" in out


def test_too_deep_input_is_a_diagnostic_and_keeps_the_session() -> None:
    out = io.StringIO()
    session = Session(out)
    session.handle("let keep = 41;")
    session.handle("\n" + "+".join(["1"] * 40_000))
    assert "too-deep" in out.getvalue() and "<repl:2>:2:1" in out.getvalue()
    session.handle("keep + 1")
    assert "42 : Int" in out.getvalue()
