"""tests/test_governance_emit_lane_log_anchor.py — regression coverage for
``governance_emit._bounded_snippet``'s ``anchor`` windowing (dispatch
20260826-beta3-b, part (b)).

The glm-reviewer on #1683 flagged a coverage gap, not a proven defect:
``_bounded_snippet(text, anchor=...)`` (``governance_emit.py`` ~L494) and its
caller ``_classify_lane_log_text`` (~L518) had no test for a marker sitting
further than 80 characters into the text — the exact real-world shape
(measured 26-08 on the live PR #1677 kimi lane log,
``~/.vnx-data/vnx-dev/logs/conversations/kimi-gate-pr1677-1787477677.log``,
51784 chars): its "reached your usage limit" marker sits at character offset
51551, nowhere near the fixture ``tests/fixtures/lane_logs/kimi_403_quota.log``
uses (offset 48) — that fixture alone would pass even a naive prefix-only
snippet and prove nothing about the anchor windowing.

The property that matters, per ``_classify_lane_log_text``'s own docstring:
the marker that DECIDED the classification must be findable in the lifted
reason — asserted on the measured marker text, never on a composed verdict.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from governance_emit import _bounded_snippet, _classify_lane_log_text

_MAX_LEN = 300


# ---------------------------------------------------------------------------
# 1. Marker at offset 0
# ---------------------------------------------------------------------------


def test_marker_at_offset_zero_survives_the_snippet():
    text = "access_terminated_error: account closed. " + ("filler word " * 50)
    state, reason = _classify_lane_log_text(text)
    assert state == "lane_exhausted"
    assert reason is not None
    assert "access_terminated_error" in reason


# ---------------------------------------------------------------------------
# 2. Marker far beyond 80 chars — the measured real shape (~2000+, up to the
#    real PR #1677 log's 51551)
# ---------------------------------------------------------------------------


def test_marker_far_beyond_eighty_chars_survives_the_snippet():
    filler = "tool call chatter with no quota keywords in it. " * 40  # ~2000 chars
    marker_idx = len(filler)
    text = filler + "Error 403: access_terminated_error, account closed."

    state, reason = _classify_lane_log_text(text)
    assert state == "lane_exhausted"
    assert reason is not None
    assert "access_terminated_error" in reason, (
        f"marker at offset {marker_idx} (>80) must survive the anchor window, got: {reason!r}"
    )

    # Prove the assertion above is meaningful: the OLD prefix-only behavior
    # (no anchor — always window from position 0) drops this exact marker,
    # so a test that passed regardless of anchoring would prove nothing.
    prefix_only = _bounded_snippet(text, max_len=_MAX_LEN)
    assert "access_terminated_error" not in prefix_only, (
        "fixture is not actually testing the far-offset case — the prefix-only "
        "snippet must NOT contain the marker, or this test can't distinguish "
        "the fix from the pre-fix behavior"
    )


# ---------------------------------------------------------------------------
# 3. Two markers: the earliest IN POSITION is not the first in tuple order
#    (_LANE_EXHAUSTED_MARKERS lists "access_terminated_error" first, but here
#    "reached your usage limit" occurs earlier in the text)
# ---------------------------------------------------------------------------


def test_earliest_position_marker_wins_not_first_tuple_order():
    early_marker = "reached your usage limit"  # last in _LANE_EXHAUSTED_MARKERS
    late_marker = "access_terminated_error"  # first in _LANE_EXHAUSTED_MARKERS
    filler = "tool call chatter with no quota keywords in it. " * 40  # ~2000 chars
    text = f"You've {early_marker} for this cycle. " + filler + f"{late_marker} — done."

    state, reason = _classify_lane_log_text(text)
    assert state == "lane_exhausted"
    assert reason is not None
    assert early_marker in reason, (
        "the EARLIEST-position marker must decide the anchor, regardless of "
        "its tuple order in _LANE_EXHAUSTED_MARKERS"
    )
    assert late_marker not in reason, (
        "the tuple-first marker sits far past the earliest marker's window and "
        "must not leak into the snippet — otherwise this test can't tell "
        "position-based anchoring apart from tuple-order anchoring"
    )
