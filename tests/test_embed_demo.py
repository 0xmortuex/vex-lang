"""End to end: real Mort programs hosting Vex through vex.mx, built with the real toolchain.

These need a Mort checkout and a C backend, so they are skipped with a stated reason when either is missing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "examples" / "embed" / "run.py"
JUNIT_CHECK = ROOT / "tools" / "check_embed_junit.py"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_runner() -> ModuleType:
    return load_module("embed_demo_run", RUNNER)


@pytest.fixture(scope="module")
def runner() -> Any:
    module = load_runner()
    problem = module.toolchain_problem()
    if problem is not None:
        pytest.skip(problem)
    return module


def test_the_runner_exists() -> None:
    assert RUNNER.is_file()
    assert (ROOT / "embed" / "mort" / "vex.mx").is_file()


def test_demo_output_matches_the_golden_transcript(runner: Any, tmp_path: Path) -> None:
    exe = runner.build(runner.DEMO_SOURCE, tmp_path)
    output = runner.run_demo(exe)
    expected = (ROOT / "examples" / "embed" / "expected.out").read_text(encoding="utf-8")
    assert output.replace("\r\n", "\n") == expected


def test_demo_proves_both_directions_and_does_not_rebuild(runner: Any, tmp_path: Path) -> None:
    exe = runner.build(runner.DEMO_SOURCE, tmp_path)
    before = hashlib.sha256(exe.read_bytes()).hexdigest()
    output = runner.run_demo(exe)
    assert hashlib.sha256(exe.read_bytes()).hexdigest() == before
    for rules in runner.RULES:
        assert f"rules: {rules}" in output
    assert "Mort -> Vex price(" in output
    assert "Vex -> Mort tax_cents(" in output
    assert "Vex -> Mort customer_tier(" in output
    assert "budget exhausted" in output
    assert "integer overflow" in output and "3_overflow.vx:" in output
    assert "capability-denied" in output and "4_forbidden.vx:" in output


def test_rule_scripts_are_distinct() -> None:
    texts = [p.read_text(encoding="utf-8") for p in sorted((ROOT / "examples" / "embed" / "rules").glob("*.vx"))]
    assert len(set(texts)) == len(texts) == 5


def test_real_mort_error_paths(runner: Any, tmp_path: Path) -> None:
    exe = runner.build(ROOT / "tests" / "embed" / "paths.mx", tmp_path)
    result = subprocess.run([str(exe)], env=runner.host_env(), capture_output=True, text=True, timeout=120)
    lines = result.stdout.replace("\r\n", "\n").splitlines()
    assert result.returncode == 0, result.stdout + result.stderr
    assert lines == [
        "ok-call:ok",
        "nonzero-status:runtime error",
        "wrong-return-tag:binding error",
        "bad-utf8-arg:binding error",
        "compile:compile error",
        "denied:compile error",
        "runaway-reentry:runtime error",
        "reentry-ok:ok",
        "after-errors:ok",
        "deep-sweep:ok",
        "stop-during-call:ok",
        "stop-during-call-refused:ok",
        "after-refused-stop:ok",
        "stop:ok",
        "stop-twice:binding error",
        "after-stop-call:binding error",
        "after-stop-load:binding error",
        "after-stop-budget:binding error",
        "after-stop-bind:binding error",
        "the Vex runtime has been stopped",
    ]
    assert result.stderr == ""


def test_real_mort_host_imports_exactly_the_documented_cpython_functions(runner: Any, tmp_path: Path) -> None:
    exe = runner.build(ROOT / "tests" / "embed" / "paths.mx", tmp_path)
    data = exe.read_bytes()
    assert b"Py_IsInitialized" not in data
    for name in (b"Py_Initialize", b"PyRun_SimpleString", b"Py_FinalizeEx"):
        assert name in data


def test_demo_survives_a_script_passing_a_huge_string(runner: Any, tmp_path: Path) -> None:
    script = tmp_path / "long.vx"
    script.write_text(
        "fn price(quantity: Int, unit_cents: Int, customer: Str) -> Int {\n"
        '    host.customer_tier("x".repeat(3000));\n'
        "    quantity * unit_cents\n"
        "}\n",
        encoding="utf-8",
    )
    exe = runner.build(runner.DEMO_SOURCE, tmp_path)
    env = runner.host_env()
    env["VEX_DEMO_RULES"] = str(script)
    result = subprocess.run([str(exe)], env=env, cwd=runner.HERE, capture_output=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Mort: no such customer" in result.stdout
    assert "host continues" in result.stdout


def test_the_transcript_check_accepts_the_golden_output_with_either_line_ending() -> None:
    module = load_runner()
    expected = (ROOT / "examples" / "embed" / "expected.out").read_text(encoding="utf-8")
    assert module.transcript_difference(expected) is None
    assert module.transcript_difference(expected.replace("\n", "\r\n")) is None


def test_the_transcript_check_reports_a_changed_line() -> None:
    module = load_runner()
    expected = (ROOT / "examples" / "embed" / "expected.out").read_text(encoding="utf-8")
    difference = module.transcript_difference(expected.replace("3645 cents", "3646 cents"))
    assert difference is not None
    assert "-  <- 3645 cents" in difference and "+  <- 3646 cents" in difference


JUNIT_CASE = '<testcase classname="tests.test_embed_demo" name="{name}">{body}</testcase>'
ONE_TEST = "def test_a() -> None:\n    pass\n"


def junit_problems(tmp_path: Path, source: str, **cases: str) -> list[str]:
    test_file = tmp_path / "test_embed_demo.py"
    test_file.write_text(source, encoding="utf-8")
    junit = tmp_path / "junit.xml"
    body = "".join(JUNIT_CASE.format(name=name, body=result) for name, result in cases.items())
    junit.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>", encoding="utf-8")
    problems: list[str] = load_module("check_embed_junit", JUNIT_CHECK).problems(junit, test_file)
    return problems


def test_the_junit_check_passes_when_every_declared_test_ran(tmp_path: Path) -> None:
    assert junit_problems(tmp_path, ONE_TEST, test_a="") == []


def test_the_junit_check_rejects_a_skipped_test_and_gives_the_reason(tmp_path: Path) -> None:
    problems = junit_problems(tmp_path, ONE_TEST, test_a='<skipped message="no uv"/>')
    assert len(problems) == 1 and "test_a" in problems[0] and "no uv" in problems[0]


def test_the_junit_check_rejects_a_test_that_did_not_run(tmp_path: Path) -> None:
    problems = junit_problems(tmp_path, ONE_TEST + "\n\ndef test_b() -> None:\n    pass\n", test_a="")
    assert len(problems) == 1 and "test_b" in problems[0]


def test_the_junit_check_rejects_a_failed_test(tmp_path: Path) -> None:
    problems = junit_problems(tmp_path, ONE_TEST, test_a='<failure message="boom"/>')
    assert len(problems) == 1 and "test_a" in problems[0]


def test_the_junit_check_rejects_a_file_with_no_tests(tmp_path: Path) -> None:
    assert junit_problems(tmp_path, "x = 1\n") != []
