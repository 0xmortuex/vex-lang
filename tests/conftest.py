"""Options for the hostile fuzz tests in test_fuzz.py."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--fuzz-seed", type=int, default=0, help="seed for the hostile fuzz inputs (default: 0, deterministic)"
    )
    parser.addoption(
        "--fuzz-seconds",
        type=float,
        default=0.0,
        help="run the hostile fuzz campaign for this many seconds (default: a short fixed-size run)",
    )
