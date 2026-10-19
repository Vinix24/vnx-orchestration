"""Cost provenance reaches the written report (dispatch 20261019-kp-k1-r2).

The codex seat FAIL on head ``845d0d5a`` was the ``_compute_cost`` wrapper
discarding ``price_run``'s ``(cost_usd, cost_source)`` pair: an unpriced lane
resolved to ``cost_usd=None``, which ``_build_frontmatter`` then collapsed to
``0.0`` — recording a billable run as free with no provenance.

These tests drive the real report path — ``_compute_cost`` followed by
``_build_frontmatter`` — and pin the two ends of the contract:

* an unavailable price stays ``None`` and carries an ``unavailable:`` source
  (never a silent 0), and
* a genuinely free run (measured $0, e.g. local inference) keeps ``0.0`` with
  its own non-unavailable source.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch  # noqa: E402


def _make_args() -> argparse.Namespace:
    return argparse.Namespace(
        dispatch_id="cost-source-test-001",
        terminal_id="T1",
        role="backend-developer",
        pr_id=None,
        task_class=None,
    )


class _BareResult:
    """Result stub with no frontmatter_fields() — matches the hasattr() miss path."""

    returncode = 0


def _frontmatter(provider: str, model: str, token_usage: dict, cost_usd, cost_source: str) -> dict:
    return provider_dispatch._build_frontmatter(
        _make_args(),
        provider,
        model,
        _BareResult(),
        1.0,
        token_usage,
        cost_usd,
        cost_source,
    )


# Red run: on the unfixed head 845d0d5a this test fails — _build_frontmatter
# collapsed the unavailable price's None to 0.0 and dropped the source, so a
# billable lane was recorded as free.
def test_unavailable_price_reaches_frontmatter_as_none_not_zero():
    token_usage = {"input": 1_000_000, "output": 1_000_000, "cache_hit": 0}
    cost, cost_source = provider_dispatch._compute_cost(
        "litellm:openrouter", "openrouter/openai/gpt-4o-mini", token_usage
    )
    assert cost is None
    assert cost_source.startswith("unavailable:")

    fm = _frontmatter(
        "litellm:openrouter", "openrouter/openai/gpt-4o-mini", token_usage, cost, cost_source
    )
    assert fm["cost_usd"] is None
    assert fm["cost_source"].startswith("unavailable:")


def test_measured_free_run_keeps_zero_with_non_unavailable_source():
    """local-gemma bills 0.0/0.0 per MTok — a measured $0, not an unavailable price."""
    token_usage = {"input": 100, "output": 50, "cache_hit": 0}
    cost, cost_source = provider_dispatch._compute_cost(
        "local-gemma", "gemma-4b-local", token_usage
    )
    assert cost == 0.0
    assert cost_source and not cost_source.startswith("unavailable:")

    fm = _frontmatter("local-gemma", "gemma-4b-local", token_usage, cost, cost_source)
    assert fm["cost_usd"] == 0.0
    assert fm["cost_source"] and not fm["cost_source"].startswith("unavailable:")


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
