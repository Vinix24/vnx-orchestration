"""F39 pytest configuration — registers custom CLI options.

OI-908: the f39 replay tests drive REAL headless claude inference per scenario.
A plain `pytest tests/` must stay deterministic and offline, so the replay tests
are marked `live` and DESELECTED by default. Opt in explicitly with `-m live`
(real inference) or `--dry-run` (fixture validation only, no LLM call). Without
this, every default suite run paid for live LLM calls on scenarios whose
red/green depended on a model answer instead of on the code.
"""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--model", default=None, help="Claude model override for replay tests (haiku/sonnet/opus)")
    parser.addoption("--dry-run", action="store_true", default=False, help="Skip LLM calls — validate fixtures only")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect `live`-marked replay tests unless the user opted in.

    Opt-in signals: an explicit `-m` expression (e.g. `-m live`), or `--dry-run`
    (fixture-only validation). The project has no global `addopts`, so the gate
    lives here, scoped to tests/f39 — a plain `pytest tests/` (or `pytest tests/f39/`)
    collects zero replay tests and stays offline.
    """
    if config.getoption("-m"):
        return
    if config.getoption("--dry-run"):
        return
    items[:] = [item for item in items if item.get_closest_marker("live") is None]
