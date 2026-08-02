"""F39 pytest configuration — registers custom CLI options.

OI-908: the f39 replay tests drive REAL headless claude inference per scenario.
A plain `pytest tests/` must stay deterministic and offline, so the replay tests
are marked `live` and DESELECTED by default. Opt in explicitly with `-m live`
(real inference) or `--dry-run` (fixture validation only, no LLM call). Without
this, every default suite run paid for live LLM calls on scenarios whose
red/green depended on a model answer instead of on the code.
"""

import pytest
from _pytest.mark import MarkMatcher
from _pytest.mark.expression import Expression

try:  # pytest < 9
    from _pytest.mark.expression import ParseError
except ImportError:  # pytest >= 9 removed ParseError from _pytest.mark.expression
    # Fail closed on ANY expression error below: the replay tests stay
    # deselected and pytest itself reports the usage error for the expression.
    ParseError = Exception


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--model", default=None, help="Claude model override for replay tests (haiku/sonnet/opus)")
    parser.addoption("--dry-run", action="store_true", default=False, help="Skip LLM calls — validate fixtures only")


def _markexpr_selects_live(markexpr: str) -> bool:
    """True when the user's ``-m`` expression deliberately selects the ``live`` marker.

    Reuses pytest's own marker-expression engine on two synthetic items: one
    carrying the ``live`` marker and one carrying no markers. ``-m live`` matches
    only the live item, so it is a deliberate opt-in. A broad filter such as
    ``-m "not integration"`` matches both items and is therefore NOT an opt-in —
    the replay tests stay deselected.
    """
    live_matcher = MarkMatcher.from_markers([pytest.mark.live.mark])
    bare_matcher = MarkMatcher.from_markers([])
    expr = Expression.compile(markexpr)
    return expr.evaluate(live_matcher) and not expr.evaluate(bare_matcher)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect `live`-marked replay tests unless the user explicitly opted in.

    Opt-in signals: an explicit `-m` expression that actually selects the `live`
    marker (e.g. `-m live`), or `--dry-run` (fixture validation only, no LLM call).
    The mere presence of any `-m` expression is NOT an opt-in: `-m "not integration"`
    must keep the replay tests deselected so an unrelated filter cannot silently
    re-enable 31 real headless-`claude -p` calls. The project has no global
    `addopts`, so the gate lives here, scoped to tests/f39 — a plain `pytest tests/`
    (or `pytest tests/f39/`) collects zero replay tests and stays offline.
    """
    if config.getoption("--dry-run"):
        return
    markexpr = config.getoption("-m")
    if markexpr:
        try:
            if _markexpr_selects_live(markexpr):
                return
        except ParseError:
            # Invalid expression: fail closed (keep the replay tests deselected);
            # pytest itself reports the usage error for the same expression.
            pass
    items[:] = [item for item in items if item.get_closest_marker("live") is None]
