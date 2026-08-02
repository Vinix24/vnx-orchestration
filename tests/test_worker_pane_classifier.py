"""test_worker_pane_classifier.py — OI-863 pane classification.

A detached tmux worker blocked on a permission prompt shows no event, no
receipt, no exit — only the pane betrays it.  This tests the classifier that
detects the prompt and labels the state ``awaiting_permission``, DISTINCT from
a dead worker (which a monitor would be justified in killing).
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from worker_pane_classifier import (
    STATE_AWAITING_INPUT,
    STATE_AWAITING_PERMISSION,
    STATE_DEAD,
    STATE_STALLED,
    STATE_UNKNOWN,
    STATE_WORKING,
    classify_worker_pane,
)


# The literal OI-863 measurement: sfp1c stuck on a Bash(chmod:*) permission rule.
PERMISSION_PANE = (
    "Permission rule Bash(chmod:*) requires confirmation for this command.\n"
    "\n"
    "Do you want to proceed?\n"
    "  1. Yes\n"
    "  2. Yes, and don't ask again for this project\n"
    "  3. No\n"
)


class TestAwaitingPermission:
    def test_permission_prompt_is_awaiting_permission(self):
        """The exact OI-863 pane shape classifies as awaiting_permission."""
        result = classify_worker_pane(PERMISSION_PANE)
        assert result.state == STATE_AWAITING_PERMISSION
        assert result.is_awaiting_permission

    def test_requires_your_permission_variant(self):
        result = classify_worker_pane(
            "● Tool call: Edit(file)\n"
            "This tool requires your permission to proceed.\n"
            "Do you want to make this edit?"
        )
        assert result.state == STATE_AWAITING_PERMISSION

    def test_matched_marker_is_recorded(self):
        result = classify_worker_pane(PERMISSION_PANE)
        # The pane contains several markers; the classifier records the first
        # one in its priority tuple ("do you want to proceed" precedes
        # "requires confirmation").
        assert result.matched_marker == "do you want to proceed"
        assert result.matched_marker in (
            "do you want to proceed",
            "requires confirmation",
        )


class TestDistinctFromDeadWorker:
    """The load-bearing OI-863 distinction: awaiting_permission != dead."""

    def test_permission_pane_is_not_dead(self):
        result = classify_worker_pane(PERMISSION_PANE)
        assert result.state != STATE_DEAD

    def test_empty_pane_is_dead(self):
        result = classify_worker_pane("")
        assert result.state == STATE_DEAD

    def test_whitespace_pane_is_dead(self):
        result = classify_worker_pane("   \n  \n")
        assert result.state == STATE_DEAD

    def test_none_is_unknown_not_dead(self):
        # A None capture means the caller failed to read the pane — unknown,
        # not a confident "dead" (and certainly not awaiting_permission).
        result = classify_worker_pane(None)
        assert result.state == STATE_UNKNOWN
        assert not result.is_awaiting_permission

    def test_stale_content_is_stalled_not_awaiting(self):
        # Content, but no prompt, no working indicator, no input glyph.
        result = classify_worker_pane("some stale log text\nthat is not a prompt")
        assert result.state == STATE_STALLED
        assert not result.is_awaiting_permission


class TestOtherStates:
    def test_working_token_counter(self):
        result = classify_worker_pane(
            "? for shortcuts\n✢ Working… (3s · ↓ 120 tokens) · esc to interrupt"
        )
        assert result.state == STATE_WORKING

    def test_working_legacy_literal(self):
        result = classify_worker_pane("… still thinking · esc to interrupt")
        assert result.state == STATE_WORKING

    def test_idle_at_input_prompt(self):
        result = classify_worker_pane("Welcome to Claude\n? for shortcuts\n❯")
        assert result.state == STATE_AWAITING_INPUT

    def test_idle_marker_without_glyph(self):
        result = classify_worker_pane("? for shortcuts")
        assert result.state == STATE_AWAITING_INPUT


class TestMarkerParity:
    """The classifier's prompt markers must stay in parity with the relay's.

    The relay (worker_permission_relay.PROMPT_MARKERS) is the active responder;
    this classifier is the passive detector.  A drift means one stops seeing a
    prompt the other would answer.
    """

    def test_prompt_markers_match_relay(self):
        import worker_permission_relay

        from worker_pane_classifier import PROMPT_MARKERS

        assert set(PROMPT_MARKERS) == set(worker_permission_relay.PROMPT_MARKERS)
