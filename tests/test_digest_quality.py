#!/usr/bin/env python3
"""Tests for digest_text — boundary-safe truncation and suggestion de-duplication.

Covers the nightly-digest defect of 2026-06-03: suggestions truncated mid-word
("...Pref", "that re", "skill sk", "for l") and near-duplicate suggestions (the same
MEMORY token-profile line three times, the same claim twice).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from digest_text import (  # noqa: E402
    smart_truncate,
    dedup_suggestions,
    decision_grade,
    is_raw_internal_dump,
)


# ---------------------------------------------------------------------------
# Truncation — never mid-word
# ---------------------------------------------------------------------------

def _ends_mid_word(text: str) -> bool:
    """A suggestion ends mid-word if its last char is a letter AND the original
    continued with a letter — approximated here: ends with a letter and no terminal
    ellipsis/punctuation marking an intentional stop."""
    if not text:
        return False
    return text[-1].isalnum() and not text.endswith("…")


def test_smart_truncate_never_ends_mid_word():
    text = "Prefer claude-sonnet voor debugging taken vs claude-opus 40% first-try success"
    out = smart_truncate(text, 12)
    assert len(out) <= 12
    assert not out.endswith("Pref")  # the exact original defect
    assert out.endswith("…")
    # The visible part before the ellipsis must be whole words.
    visible = out[:-1]
    assert visible == "" or not visible[-1].isalnum() or " " not in text[:12] or visible in text


def test_smart_truncate_real_defect_strings():
    # Reconstruct the kinds of strings that produced "...Pref", "that re", "for l".
    # Limits exceed the first word, as the real digest does (80/120/150).
    cases = [
        ("Prefer claude-sonnet voor debugging", 10),
        ("ensure that recovery is logged", 12),
        ("add skill skeleton for new agents", 11),
        ("watch for large refactors", 9),
    ]
    for text, limit in cases:
        out = smart_truncate(text, limit)
        assert len(out) <= limit
        # No half-words: visible text (minus ellipsis) is a prefix that ends on a
        # whole word from the source.
        if out.endswith("…"):
            visible = out[:-1].rstrip()
            words = text.split(" ")
            # visible must be a sequence of whole leading words
            assert visible == "" or text.startswith(visible)
            if visible:
                last_word = visible.split(" ")[-1]
                assert last_word in words


def test_smart_truncate_short_text_unchanged():
    assert smart_truncate("short", 80) == "short"


def test_smart_truncate_no_suggestion_ends_mid_word():
    suggestions = [
        "Prefer claude-sonnet boven claude-opus voor debugging taken (routine bucket).",
        "Add rule: net-deletion sanity check that flags PRs deleting many files",
        "Model performance summary for long sessions with cache reuse",
    ]
    for s in suggestions:
        out = smart_truncate(s, 20)
        assert not _ends_mid_word(out), f"mid-word truncation: {out!r}"


# ---------------------------------------------------------------------------
# Dedup — same target+intent collapses to one
# ---------------------------------------------------------------------------

def test_dedup_collapses_token_profile_variants():
    """Three 'Model token profiel' lines at different sample sizes -> one."""
    suggestions = [
        {
            "category": "memory",
            "target": "MEMORY.md",
            "content": "- Model token profiel (7d): opus avg=1817K/sess cache=80%",
            "confidence": 0.95,
            "suggested_at": "2026-06-01T00:00:00Z",
        },
        {
            "category": "memory",
            "target": "MEMORY.md",
            "content": "- Model token profiel (7d): opus avg=1650K/sess cache=78%",
            "confidence": 0.95,
            "suggested_at": "2026-06-02T00:00:00Z",
        },
        {
            "category": "memory",
            "target": "MEMORY.md",
            "content": "- Model token profiel (7d): opus avg=1500K/sess cache=75%",
            "confidence": 0.95,
            "suggested_at": "2026-06-03T00:00:00Z",
        },
    ]
    out = dedup_suggestions(suggestions)
    assert len(out) == 1
    # Keeps latest on confidence tie.
    assert out[0]["suggested_at"] == "2026-06-03T00:00:00Z"


def test_dedup_keeps_highest_confidence():
    suggestions = [
        {"category": "memory", "target": "MEMORY.md",
         "content": "- Prefer opus voor debugging taken", "confidence": 0.70},
        {"category": "memory", "target": "MEMORY.md",
         "content": "- Prefer opus voor debugging taken", "confidence": 0.92},
    ]
    out = dedup_suggestions(suggestions)
    assert len(out) == 1
    assert out[0]["confidence"] == 0.92


def test_dedup_preserves_distinct_suggestions():
    suggestions = [
        {"category": "memory", "target": "MEMORY.md",
         "content": "- Prefer opus voor debugging taken", "confidence": 0.8},
        {"category": "prevention_rules", "target": ".claude/rules/antipatterns.md",
         "content": "Add rule: net-deletion sanity check", "confidence": 0.8},
    ]
    out = dedup_suggestions(suggestions)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# Decision-grade — drop raw internals
# ---------------------------------------------------------------------------

def test_is_raw_internal_dump_flags_token_profile():
    assert is_raw_internal_dump(
        {"content": "- Model token profiel (7d): opus avg=1817K/sess"}
    )
    assert not is_raw_internal_dump(
        {"content": "- Prefer opus voor debugging taken"}
    )


def test_decision_grade_drops_dumps_and_dedups():
    suggestions = [
        {"category": "memory", "target": "MEMORY.md",
         "content": "- Model token profiel (7d): opus avg=1817K", "confidence": 0.95},
        {"category": "memory", "target": "MEMORY.md",
         "content": "- Prefer opus voor debugging taken", "confidence": 0.80},
        {"category": "memory", "target": "MEMORY.md",
         "content": "- Prefer opus voor debugging taken", "confidence": 0.85},
    ]
    out = decision_grade(suggestions)
    # token-profile dropped, two prefer-lines collapsed -> 1
    assert len(out) == 1
    assert "token profiel" not in out[0]["content"].lower()
    assert out[0]["confidence"] == 0.85


def test_decision_grade_respects_limit():
    suggestions = [
        {"category": "memory", "target": "MEMORY.md",
         "content": f"- Prefer model{i} voor taak{i}", "confidence": 0.5 + i * 0.05}
        for i in range(6)
    ]
    out = decision_grade(suggestions, limit=3)
    assert len(out) == 3
    # highest confidence first
    assert out[0]["confidence"] >= out[1]["confidence"] >= out[2]["confidence"]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
