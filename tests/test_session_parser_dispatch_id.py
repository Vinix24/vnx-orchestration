"""test_session_parser_dispatch_id.py — Unit tests for dispatch_id validation in SessionParser.

Verifies the Cluster-B (OI-872) fix: the parser must reject template placeholders
like <dispatch_id>, accept real dispatch IDs, and leave dispatch_id empty (None)
when no reliable ID is found.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add scripts/ to path so the conversation_analyzer package can be imported.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from conversation_analyzer.parser import SessionParser


@pytest.fixture
def parser():
    return SessionParser()


def _make_user_record(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def _make_metrics():
    from conversation_analyzer.models import SessionMetrics
    return SessionMetrics()


class TestValidateDispatchId:
    """Tests for SessionParser._validate_dispatch_id — the validation gate."""

    def test_null_for_empty_string(self):
        """Empty string returns None."""
        assert SessionParser._validate_dispatch_id("") is None

    def test_null_for_whitespace_only(self):
        """Whitespace-only string returns None."""
        assert SessionParser._validate_dispatch_id("   ") is None

    def test_null_for_none(self):
        """None input returns None (fails open — caller handles it)."""
        assert SessionParser._validate_dispatch_id(None) is None  # type: ignore[arg-type]

    def test_rejects_placeholder_dispatch_id(self):
        """The literal <dispatch_id> template placeholder is rejected."""
        assert SessionParser._validate_dispatch_id("<dispatch_id>") is None

    def test_rejects_placeholder_with_whitespace(self):
        """<dispatch_id> with surrounding whitespace is rejected."""
        assert SessionParser._validate_dispatch_id("  <dispatch_id>  ") is None

    def test_rejects_any_angle_bracket_placeholder(self):
        """Any <...> template placeholder is rejected."""
        for placeholder in [
            "<dispatch_id>",
            "<your-dispatch-id>",
            "<id>",
            "<REPLACE_ME>",
            "<TODO>",
            "<placeholder>",
        ]:
            assert SessionParser._validate_dispatch_id(placeholder) is None, (
                f"Expected {placeholder!r} to be rejected"
            )

    def test_rejects_free_form_text(self):
        """Free-form text that is not a dispatch ID is rejected."""
        assert SessionParser._validate_dispatch_id("a plain sentence") is None

    def test_rejects_empty_angle_brackets(self):
        """Empty angle brackets <> are rejected."""
        assert SessionParser._validate_dispatch_id("<>") is None

    def test_accepts_real_dispatch_id_date_prefix(self):
        """A real dispatch ID starting with YYYYMMDD- is accepted."""
        real_id = "20260731-clusterB-analyzer-dispatchid"
        assert SessionParser._validate_dispatch_id(real_id) == real_id

    def test_accepts_real_dispatch_id_with_time(self):
        """A dispatch ID with full YYYYMMDD-HHMMSS- is accepted."""
        real_id = "20260603-123456-feature-name-A"
        assert SessionParser._validate_dispatch_id(real_id) == real_id

    def test_accepts_with_surrounding_whitespace(self):
        """Whitespace is stripped from a valid ID."""
        assert SessionParser._validate_dispatch_id("  20260731-my-feature  ") == "20260731-my-feature"

    def test_rejects_non_date_prefix(self):
        """An otherwise plausible ID missing the date prefix is rejected."""
        assert SessionParser._validate_dispatch_id("fix-dispatch-id-bug") is None

    def test_rejects_partial_date(self):
        """A partial date prefix (fewer than 8 digits) is rejected."""
        assert SessionParser._validate_dispatch_id("2026073-broken") is None

    def test_rejects_bench_id_missing_date_prefix(self):
        """Bench IDs like bench-* lack a date prefix — correctly rejected as non-dispatch IDs."""
        assert SessionParser._validate_dispatch_id(
            "bench-claude-opus-4-7-05_extractor_subclass-r1-20260604-142018"
        ) is None


class TestProcessUserDispatchId:
    """Integration-style tests: _process_user sets dispatch_id only for valid IDs."""

    def test_no_dispatch_id_when_message_has_none(self, parser):
        """dispatch_id remains empty when no dispatch table or header is present."""
        metrics = _make_metrics()
        record = _make_user_record("Just a regular user message.")
        parser._process_user(record, metrics)
        assert metrics.dispatch_id == ""

    def test_null_for_placeholder_in_table(self, parser):
        """<dispatch_id> in a dispatch table row is rejected — no ID stored."""
        metrics = _make_metrics()
        record = _make_user_record("| **Dispatch-ID** | <dispatch_id> |")
        parser._process_user(record, metrics)
        assert metrics.dispatch_id == ""

    def test_null_for_placeholder_in_header(self, parser):
        """<dispatch_id> in a Dispatch-ID header is rejected."""
        metrics = _make_metrics()
        record = _make_user_record("Dispatch-ID: <dispatch_id>")
        parser._process_user(record, metrics)
        assert metrics.dispatch_id == ""

    def test_real_id_from_table(self, parser):
        """A real dispatch ID in a table row is accepted."""
        metrics = _make_metrics()
        record = _make_user_record("| **Dispatch-ID** | 20260731-123456-my-feature |")
        parser._process_user(record, metrics)
        assert metrics.dispatch_id == "20260731-123456-my-feature"

    def test_real_id_from_header(self, parser):
        """A real dispatch ID in a Dispatch-ID header is accepted."""
        metrics = _make_metrics()
        record = _make_user_record("Dispatch-ID: 20260731-123456-my-feature")
        parser._process_user(record, metrics)
        assert metrics.dispatch_id == "20260731-123456-my-feature"

    def test_real_id_from_bold_header(self, parser):
        """A dispatch ID in a bold-styled header is accepted."""
        metrics = _make_metrics()
        record = _make_user_record("**Dispatch-ID:** 20260731-123456-my-feature")
        parser._process_user(record, metrics)
        assert metrics.dispatch_id == "20260731-123456-my-feature"

    def test_real_id_with_trailing_period(self, parser):
        """A dispatch ID followed by a sentence period is cleaned before storage."""
        metrics = _make_metrics()
        record = _make_user_record("Dispatch-ID: 20260731-123456-my-feature.")
        parser._process_user(record, metrics)
        assert metrics.dispatch_id == "20260731-123456-my-feature"

    def test_placeholder_before_real_id_same_message(self, parser):
        """A template placeholder before the real ID does not block extraction.

        The worker-context template carries ``Dispatch-ID: <dispatch_id>`` in
        its Commit Convention section, ahead of the Dispatch Metadata footer
        with the real ID. First-match-only extraction lost these sessions
        (OI-872); the parser must keep scanning and take the first VALID ID.
        """
        metrics = _make_metrics()
        record = _make_user_record(
            "Include in commit body:\n```\nDispatch-ID: <dispatch_id>\n```\n\n"
            "### Dispatch Metadata\n\n- Dispatch-ID: 20260731-123456-my-feature\n"
            "- Model: sonnet\n"
        )
        parser._process_user(record, metrics)
        assert metrics.dispatch_id == "20260731-123456-my-feature"

    def test_placeholder_only_message_stays_empty(self, parser):
        """A message with only a placeholder keeps dispatch_id empty."""
        metrics = _make_metrics()
        record = _make_user_record(
            "### Dispatch Metadata\n\n- Dispatch-ID: <dispatch_id>\n"
        )
        parser._process_user(record, metrics)
        assert metrics.dispatch_id == ""

    def test_only_first_scan_window_messages_checked(self, parser):
        """After MAX_DISPATCH_SCAN_MESSAGES user messages, the parser stops looking."""
        metrics = _make_metrics()
        # First window-size messages have no dispatch ID
        for _ in range(parser.MAX_DISPATCH_SCAN_MESSAGES):
            parser._process_user(_make_user_record("regular message"), metrics)
        # The next message has a real ID but is outside the scan window
        parser._process_user(
            _make_user_record("| **Dispatch-ID** | 20260731-123456-real |"), metrics)
        assert metrics.dispatch_id == ""

    def test_real_id_inside_scan_window(self, parser):
        """A real ID in the 6th user message is still within the scan window."""
        metrics = _make_metrics()
        for _ in range(5):
            parser._process_user(_make_user_record("regular message"), metrics)
        parser._process_user(
            _make_user_record("| **Dispatch-ID** | 20260731-123456-real |"), metrics)
        assert metrics.dispatch_id == "20260731-123456-real"

    def test_first_id_wins(self, parser):
        """The first valid dispatch ID found is kept; later ones are ignored."""
        metrics = _make_metrics()
        record1 = _make_user_record("| **Dispatch-ID** | 20260731-123456-first |")
        record2 = _make_user_record("| **Dispatch-ID** | 20260731-123456-second |")
        parser._process_user(record1, metrics)
        parser._process_user(record2, metrics)
        assert metrics.dispatch_id == "20260731-123456-first"


class TestExtractDispatchId:
    """Tests for SessionParser._extract_dispatch_id — first-valid-in-document-order."""

    def test_none_for_no_mentions(self, parser):
        """No Dispatch-ID mention yields None."""
        assert parser._extract_dispatch_id("no dispatch here") is None

    def test_skips_placeholder_to_real_id(self, parser):
        """A later real ID is returned even when a placeholder appears first."""
        text = "Dispatch-ID: <dispatch_id>\n\n### Dispatch Metadata\n- Dispatch-ID: 20260731-abc-123"
        assert parser._extract_dispatch_id(text) == "20260731-abc-123"

    def test_none_for_placeholder_only(self, parser):
        """Placeholder-only text yields None."""
        assert parser._extract_dispatch_id("Dispatch-ID: <dispatch_id>") is None

    def test_first_valid_wins(self, parser):
        """The first VALID ID wins; an invalid first candidate is skipped."""
        text = ("Dispatch-ID: 20260731-abc-123\n"
                "Dispatch-ID: 20260731-xyz-789")
        assert parser._extract_dispatch_id(text) == "20260731-abc-123"

    def test_table_and_header_order(self, parser):
        """Table and header mentions are scanned in document order."""
        text = ("Dispatch-ID: 20260731-header-1\n"
                "| **Dispatch-ID** | 20260731-table-2 |")
        assert parser._extract_dispatch_id(text) == "20260731-header-1"
