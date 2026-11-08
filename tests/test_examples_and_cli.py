"""Golden-output tests for every example, plus end-to-end tests of the command line."""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).parent.parent
EXAMPLES = sorted((ROOT / "examples").glob("*.vx"))
MODULES = pathlib.Path(__file__).parent / "modules"


def vex(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "vex", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        timeout=120,
    )


def test_there_are_examples() -> None:
    assert len(EXAMPLES) >= 8


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_matches_golden_output(path: pathlib.Path) -> None:
    golden = path.with_suffix(".out")
    assert golden.exists(), f"missing golden file for {path.name}"
    res = vex("run", str(path))
    assert res.returncode == 0, res.stderr
    assert res.stdout == golden.read_text(encoding="utf-8")
    assert res.stderr == ""


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_checks_clean(path: pathlib.Path) -> None:
    res = vex("check", str(path))
    assert res.returncode == 0, res.stderr
    assert "ok" in res.stdout


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_disassembles(path: pathlib.Path) -> None:
    res = vex("disasm", str(path))
    assert res.returncode == 0, res.stderr
    assert "== fn main" in res.stdout
    assert "RET" in res.stdout


def test_version() -> None:
    res = vex("--version")
    assert res.returncode == 0 and res.stdout.startswith("vex ")


def test_run_reports_compile_errors_with_exit_1() -> None:
    res = vex("run", str(ROOT / "tests" / "errors" / "undefined_name.vx"))
    assert res.returncode == 1
    assert "error[undefined-name]" in res.stderr
    assert "did you mean `total`" in res.stderr
    assert "aborting: 1 error" in res.stderr
    assert res.stdout == ""


def test_run_reports_runtime_errors_with_exit_2(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "boom.vx"
    f.write_text('fn main() {\n    print("before");\n    let z = 0;\n    print(1 / z);\n}\n', encoding="utf-8")
    res = vex("run", str(f))
    assert res.returncode == 2
    assert res.stdout == "before\n"
    assert "runtime error: division by zero" in res.stderr
    assert "in main at" in res.stderr


def test_exit_code_from_main(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "code.vx"
    f.write_text("fn main() -> Int { 3 }\n", encoding="utf-8")
    assert vex("run", str(f)).returncode == 3
    g = tmp_path / "ex.vx"
    g.write_text("fn main() { exit(5); }\n", encoding="utf-8")
    assert vex("run", str(g)).returncode == 5


def test_program_arguments(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "args.vx"
    f.write_text('fn main() {\n    for a in env.args() { print("arg:" + a); }\n}\n', encoding="utf-8")
    res = vex("run", str(f), "one", "two three")
    assert res.stdout == "arg:one\narg:two three\n"


def test_read_file_and_unicode_output(tmp_path: pathlib.Path) -> None:
    data = tmp_path / "data.txt"
    data.write_text("Merhaba dünya ğüşiöç\nikinci satır\n", encoding="utf-8")
    prog = tmp_path / "rd.vx"
    prog.write_text(
        "fn main() {\n    match io.read_file(env.args()[0]) {\n"
        "        Ok(text) => { print(text.lines().len()); print(text.lines()[1].upper()); },\n"
        '        Err(e) => print("error: " + e),\n    }\n}\n',
        encoding="utf-8",
    )
    res = vex("run", str(prog), str(data))
    assert res.returncode == 0, res.stderr
    assert res.stdout == "2\nIKINCI SATIR\n"


def test_unicode_source_path_and_literals(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "türkçe.vx"
    f.write_text('fn main() {\n    let şehir = "İstanbul";\n    print("{şehir} 🌍");\n}\n', encoding="utf-8")
    res = vex("run", str(f))
    assert res.returncode == 0, res.stderr
    assert res.stdout == "İstanbul 🌍\n"


def test_stdin_read_line(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "echo.vx"
    f.write_text(
        'fn main() {\n    match io.read_line() { Some(l) => print("got " + l), None => print("eof") }\n'
        '    match io.read_line() { Some(l) => print("got " + l), None => print("eof") }\n}\n',
        encoding="utf-8",
    )
    res = vex("run", str(f), stdin="hello\n")
    assert res.stdout == "got hello\neof\n"


def test_check_multiple_files_and_summary() -> None:
    a = str(ROOT / "tests" / "errors" / "assign_immutable.vx")
    b = str(ROOT / "examples" / "hello.vx")
    res = vex("check", a, b)
    assert res.returncode == 1
    assert "1 error" in res.stderr
    assert "hello.vx: ok" in res.stdout


def test_check_no_main_flag(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "lib.vx"
    f.write_text("fn helper() -> Int { 1 }\n", encoding="utf-8")
    assert vex("check", str(f)).returncode == 1
    assert vex("check", "--no-main", str(f)).returncode == 0


def test_missing_file_is_a_clean_error() -> None:
    res = vex("run", "does_not_exist.vx")
    assert res.returncode == 1
    assert "cannot read" in res.stderr
    assert "Traceback" not in res.stderr


def test_no_traceback_on_any_corpus_error() -> None:
    for p in sorted((ROOT / "tests" / "errors").glob("*.vx"))[:25]:
        res = vex("check", str(p))
        assert "Traceback" not in res.stderr, p.name
        assert res.returncode == 1, p.name


# ---------------------------------------------------------------- modules
def test_imports_resolve_transitively() -> None:
    res = vex("run", str(MODULES / "main_ok.vx"))
    assert res.returncode == 0, res.stderr
    assert res.stdout == "3,6\n"


def test_import_cycle_is_reported() -> None:
    res = vex("check", str(MODULES / "cycle_a.vx"))
    assert res.returncode == 1
    assert "import cycle" in res.stderr


def test_duplicate_across_modules_points_at_both() -> None:
    res = vex("check", str(MODULES / "dup_a.vx"))
    assert res.returncode == 1
    assert "defined more than once" in res.stderr
    assert "dup_a.vx" in res.stderr and "dup_b.vx" in res.stderr


def test_error_in_imported_module_names_that_file() -> None:
    res = vex("check", str(MODULES / "bad_type.vx"))
    assert res.returncode == 1
    assert "bad_type_lib.vx:2:5" in res.stderr


# ---------------------------------------------------------------- repl through the CLI
def test_repl_over_stdin() -> None:
    res = vex("repl", stdin="1 + 2\nlet x = 10;\nx * x\n")
    assert res.returncode == 0
    assert res.stdout == "3 : Int\n100 : Int\n"


# ---------------------------------------------------------------- deep structures (need the big stack)
def test_very_long_expression_chain_compiles_through_the_cli(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "chain.vx"
    f.write_text("fn main() { print(" + " + ".join(["1"] * 3000) + "); }\n", encoding="utf-8")
    res = vex("run", str(f))
    assert res.returncode == 0, res.stderr
    assert res.stdout == "3000\n"


def test_printing_and_comparing_deeply_nested_values(tmp_path: pathlib.Path) -> None:
    f = tmp_path / "deep.vx"
    f.write_text(
        "enum L { Nil, Cons(Int, L) }\n"
        "fn build(n: Int) -> L {\n    let mut l = L.Nil;\n    for i in 0..n { l = L.Cons(i, l); }\n    l\n}\n"
        'fn main() {\n    let big = build(300);\n    print(big == big);\n    print("{big}".len());\n}\n',
        encoding="utf-8",
    )
    res = vex("run", str(f))
    assert res.returncode == 0, res.stderr
    expected_len = sum(len(f"Cons({i}, ") + 1 for i in range(300)) + len("Nil")
    assert res.stdout == f"true\n{expected_len}\n"


def test_disassembly_names_builtins_without_underscores() -> None:
    res = vex("disasm", str(ROOT / "examples" / "hello.vx"))
    assert "print/1" in res.stdout and "_print" not in res.stdout


def test_cli_reports_runaway_nesting_on_the_big_stack(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "deep.vx"
    path.write_text("fn main() {\n" + "{" * 100_000, encoding="utf-8")
    proc = vex("check", str(path))
    assert proc.returncode == 1, proc.stderr[-500:]
    assert "too-deep" in proc.stderr
    assert "internal compiler error" not in proc.stderr
