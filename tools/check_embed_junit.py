"""Fail unless every test in tests/test_embed_demo.py actually ran and passed (used by the embed-demo CI job).

Those tests skip when the toolchain is missing, and a skipped test leaves pytest green, so the job would otherwise
pass without having built anything.

    python tools/check_embed_junit.py embed-junit.xml
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

TEST_FILE = Path(__file__).resolve().parent.parent / "tests" / "test_embed_demo.py"


def problems(junit: Path, test_file: Path) -> list[str]:
    """Why the run recorded in `junit` does not prove every test in `test_file` passed; empty if it does."""
    declared = re.findall(r"^def (test_\w+)\(", test_file.read_text(encoding="utf-8"), re.MULTILINE)
    if not declared:
        return [f"{test_file} declares no tests"]
    cases = {case.get("name"): case for case in ElementTree.parse(junit).iter("testcase")}
    found: list[str] = []
    for name in declared:
        case = cases.get(name)
        if case is None:
            found.append(f"{name}: did not run")
            continue
        skipped = case.find("skipped")
        if skipped is not None:
            found.append(f"{name}: skipped: {skipped.get('message')}")
        elif case.find("failure") is not None or case.find("error") is not None:
            found.append(f"{name}: failed")
    return found


def main() -> int:
    found = problems(Path(sys.argv[1]), TEST_FILE)
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    print("every test in tests/test_embed_demo.py ran and passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
