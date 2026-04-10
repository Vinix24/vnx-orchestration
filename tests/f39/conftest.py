"""F39 pytest configuration — registers custom CLI options."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--model", default=None, help="Claude model override for replay tests (haiku/sonnet/opus)")
    parser.addoption("--dry-run", action="store_true", default=False, help="Skip LLM calls — validate fixtures only")
