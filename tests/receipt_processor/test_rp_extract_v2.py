"""Tests for rp_enrich_fields.py — v2 receipt field enrichment."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

_RP_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "lib" / "receipt_processor"
sys.path.insert(0, str(_RP_DIR))

from rp_enrich_fields import enrich_fields, _parse_tests_passed, _derive_next_action  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _base_receipt(**overrides: Any) -> Dict[str, Any]:
    r: Dict[str, Any] = {
        "dispatch_id": "20260603-163106-receipt-v2-format-enrichment",
        "status": "done",
        "provider": "claude",
        "model": "sonnet",
        "lane": "tmux_interactive",
        "pool_id": "interactive",
        "pr_id": "817",
        "exit_code": 0,
        "smart_context": True,
        "provenance": {
            "diff_summary": {"files_changed": 5, "insertions": 120, "deletions": 30}
        },
    }
    r.update(overrides)
    return r


_REPORT_WITH_TESTS = """\
## Summary
Receipt v2 enrichment implementation complete.

## Changes
- scripts/lib/receipt_processor/rp_enrich_fields.py added

## Verification
pytest tests/receipt_processor/test_rp_extract_v2.py -v: 8 passed, 0 failed

## Open Items
None
"""

_REPORT_NO_TESTS = """\
## Summary
Docs update only.

## Changes
- docs/CHANGELOG.md updated

## Verification
No automated tests for docs.

## Open Items
None
"""

_REPORT_PYTEST_FORMAT = """\
## Verification
pytest tests/test_foo.py::test_bar -v
============================= 12 passed in 0.42s ==============================
"""


def _make_gate_file(directory: Path, pr_id: str, gate: str, *, blockers: int = 0, advisories: int = 0, state: str = "completed") -> Path:
    data = {
        "gate": gate,
        "pr_id": pr_id,
        "state": state,
        "blocking_count": blockers,
        "advisory_count": advisories,
        "blocking_findings": [],
        "advisory_findings": [
            {"message": "Unused import in helper module", "severity": "warning"}
        ] if advisories > 0 else [],
    }
    path = directory / f"{pr_id}-{gate}.json"
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_full_fields_present():
    """All v2 fields are populated from a complete receipt + report + gate."""
    with tempfile.TemporaryDirectory() as tmp:
        gate_dir = Path(tmp)
        _make_gate_file(gate_dir, "817", "codex_gate", blockers=0, advisories=1)

        fields = enrich_fields(_base_receipt(), _REPORT_WITH_TESTS, gate_dir)

    assert fields["provider"] == "claude"
    assert fields["model"] == "sonnet"
    assert fields["lane"] == "tmux_interactive"
    assert fields["isolation_mode"] == "interactive"
    assert fields["files_changed"] == "5"
    assert fields["insertions"] == "120"
    assert fields["deletions"] == "30"
    assert fields["tests_passed"] == "8"
    assert fields["smart_context"] == "✓"
    assert fields["gate_name"] == "codex_gate"
    assert fields["gate_blockers"] == "0"
    assert fields["gate_advisories"] == "1"
    assert "Unused import" in fields["gate_top_advisory"]
    assert fields["pr_title_slug"] == "receipt-v2-format-enrichment"
    assert fields["next_action"] == "ready for merge"


def test_missing_pr_id():
    """When pr_id is absent the header uses dispatch-id-slug and next is 'T0 to open PR'."""
    receipt = _base_receipt(pr_id="", status="done", exit_code=0)
    fields = enrich_fields(receipt, _REPORT_WITH_TESTS, None)

    assert fields["pr_title_slug"] == "receipt-v2-format-enrichment"
    assert fields["next_action"] == "T0 to open PR"


def test_missing_gate():
    """When no gate file exists gate_name is empty and next_action is 'request codex_gate'."""
    with tempfile.TemporaryDirectory() as tmp:
        gate_dir = Path(tmp)  # empty — no gate files
        receipt = _base_receipt(pr_id="817", status="done", exit_code=0)
        fields = enrich_fields(receipt, _REPORT_WITH_TESTS, gate_dir)

    assert fields["gate_name"] == ""
    assert fields["next_action"] == "request codex_gate"


def test_failed_status():
    """exit_code != 0 forces 'fix needed' regardless of other fields."""
    with tempfile.TemporaryDirectory() as tmp:
        gate_dir = Path(tmp)
        _make_gate_file(gate_dir, "817", "codex_gate", blockers=0)
        receipt = _base_receipt(pr_id="817", status="failed", exit_code=1)
        fields = enrich_fields(receipt, _REPORT_WITH_TESTS, gate_dir)

    assert fields["next_action"] == "fix needed"
    assert fields["exit_code"] == "1"


def test_missing_tests():
    """Report body without test result pattern yields empty tests_passed."""
    fields = enrich_fields(_base_receipt(), _REPORT_NO_TESTS, None)
    assert fields["tests_passed"] == ""


def test_smart_context_present():
    """smart_context=True maps to '✓'."""
    fields = enrich_fields(_base_receipt(smart_context=True), None, None)
    assert fields["smart_context"] == "✓"


def test_smart_context_absent():
    """smart_context falsy maps to empty string."""
    for falsy in (False, None, 0, ""):
        fields = enrich_fields(_base_receipt(smart_context=falsy), None, None)
        assert fields["smart_context"] == "", f"Expected '' for smart_context={falsy!r}"


def test_gate_with_blockers():
    """Gate blockers > 0 → next_action is 'fix-forward needed'."""
    with tempfile.TemporaryDirectory() as tmp:
        gate_dir = Path(tmp)
        _make_gate_file(gate_dir, "817", "codex_gate", blockers=2, advisories=0, state="completed")
        receipt = _base_receipt(pr_id="817", status="done", exit_code=0)
        fields = enrich_fields(receipt, _REPORT_WITH_TESTS, gate_dir)

    assert fields["gate_blockers"] == "2"
    assert fields["next_action"] == "fix-forward needed"
