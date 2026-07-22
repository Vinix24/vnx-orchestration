#!/usr/bin/env python3
"""ADR-035 §9 PR-5 — field trim + rename + schema_version:2 cutover.

Covers: T10 (provenance.git_ref survives the trim + stays findable by
find_receipt_by_commit), T11 (v2 receipt has no session{} object; session_id
is present), T27 (schema_version:2 never co-occurs with the pre-PR-5 field
names validation{}/session{}), plus regression checks that the dead fields
(recorded_at, provenance.captured_at/captured_by, quality_advisory{}) are
gone on both write paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

import append_receipt as ar  # noqa: E402 — registers the Path-2 facade
import governance_emit  # noqa: E402
from receipt_provenance import find_receipt_by_commit  # noqa: E402
from append_receipt_internals.validation import _validate_receipt  # noqa: E402

# ADR-035 §3.3/§9 PR-5: the trimmed field set, mirrored from
# append_receipt_internals.validation.LEGACY_TRIMMED_TOP_LEVEL_FIELDS so the
# parametrization stays in lockstep with the reject-list the fix implements.
LEGACY_TOP_LEVEL_FIELDS = [
    "session",
    "validation",
    "recorded_at",
    "quality_advisory",
    "confidence",
    "tags",
    "root_cause",
    "dependencies",
    "metrics",
    "prevention_rules",
    "used_pattern_hashes",
    "legacy_format",
]
LEGACY_PROVENANCE_SUBKEYS = ["captured_at", "captured_by"]


def _read_lines(receipts_path: Path) -> list:
    if not receipts_path.exists():
        return []
    return [json.loads(l) for l in receipts_path.read_text().splitlines() if l.strip()]


def _base_path2_receipt(dispatch_id: str, **overrides: Any) -> Dict[str, Any]:
    receipt = {
        "timestamp": "2026-07-22T10:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": dispatch_id,
        "status": "done",
    }
    receipt.update(overrides)
    return receipt


# ---------------------------------------------------------------------------
# T10 — provenance.git_ref remains present + is correctly read by
# receipt_provenance.find_receipt_by_commit after the v2 field trim.
# ---------------------------------------------------------------------------


def test_t10_git_ref_present_and_findable_after_trim(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt(
        "t10-git-ref",
        verification={"method": "n/a"},
        provenance={
            "git_ref": "deadbeef1234567890",
            "branch": "main",
            "is_dirty": False,
            "dirty_files": 0,
            "diff_summary": None,
        },
    )
    result = ar.append_receipt_payload(receipt, receipts_file=str(receipts_path), skip_enrichment=True)
    assert result.status == "appended"

    found = find_receipt_by_commit(receipts_path, "deadbeef1234567890")
    assert found is not None
    assert found["dispatch_id"] == "t10-git-ref"

    # The trim removed captured_at/captured_by from provenance{} — confirm
    # git_ref survives that trim untouched, the specific regression this
    # test guards.
    assert "git_ref" in found["provenance"]
    assert "captured_at" not in found["provenance"]
    assert "captured_by" not in found["provenance"]


def test_t10_git_ref_present_path1_after_trim(tmp_path):
    """Same guarantee on Path 1 (governance_emit.emit_dispatch_receipt) —
    Path 1 never had provenance{} of its own, but recorded_at (a sibling
    trimmed field on the SAME receipt dict) must be gone without disturbing
    any other field find_receipt_by_commit or a reader might depend on."""
    receipts_path = tmp_path / "t0_receipts.ndjson"
    governance_emit.emit_dispatch_receipt(
        dispatch_id="t10-path1", terminal_id="T1", provider="claude",
        model="claude-sonnet-5", pr_id=None, status="success",
        completion_pct=100, risk=0.0, findings=[], duration_seconds=1.0,
        token_usage={"input": 1, "output": 1}, cost_usd=None,
        state_dir=receipts_path.parent,
        verification={"method": "pytest", "tests_run": 1, "tests_passed": 1, "tests_failed": 0},
    )
    line = _read_lines(receipts_path)[-1]
    assert "recorded_at" not in line
    assert line["schema_version"] == 2


# ---------------------------------------------------------------------------
# T11 — a v2 receipt has no session{} object; session_id is present.
# ---------------------------------------------------------------------------


def test_t11_v2_receipt_no_session_object_session_id_present(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = {
        "timestamp": "2026-07-22T10:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": "t11-session-collapse",
        "terminal": "T1",
        "status": "success",
        "source": "pytest",
        # Skip real git resolution -- T11 is about session{}, not provenance.
        "provenance": {
            "git_ref": "HEAD", "branch": "test", "is_dirty": False,
            "dirty_files": 0, "diff_summary": None,
        },
    }

    with patch.object(ar, "_resolve_model_provider",
                       return_value={"model": "claude-sonnet-4-6", "provider": "claude_code"}):
        with patch.object(ar, "_resolve_session_id", return_value="sess-t11-abc123"):
            with patch.object(ar, "_extract_session_token_usage", return_value=None):
                with patch.object(ar, "collect_terminal_snapshot") as mock_snap:
                    snap = MagicMock()
                    snap.to_dict.return_value = {"status": "ok"}
                    mock_snap.return_value = snap
                    with patch.object(ar, "enrich_receipt_provenance", return_value=None):
                        with patch.object(ar, "validate_receipt_provenance") as mock_val:
                            mock_val.return_value = MagicMock(gaps=[], chain_status="ok")
                            result = ar.append_receipt_payload(receipt, receipts_file=str(receipts_path))

    assert result.status == "appended"
    line = _read_lines(receipts_path)[-1]

    # No session{} object anywhere on the v2 receipt (§4).
    assert "session" not in line
    # session_id is present and IS the resolved value -- the one field §4 keeps.
    assert line["session_id"] == "sess-t11-abc123"
    # model/provider -- promoted to top-level, not lost by the collapse.
    assert line["model"] == "claude-sonnet-4-6"
    assert line["provider"] == "claude_code"
    assert line["schema_version"] == 2


def test_t11_path1_never_had_session_object(tmp_path):
    """Path 1 (governance_emit) never went through session{} enrichment at
    all -- confirm it still has no session{} post-trim (no regression) and
    carries its own session_id-independent identity fields unchanged."""
    receipts_path = tmp_path / "t0_receipts.ndjson"
    governance_emit.emit_dispatch_receipt(
        dispatch_id="t11-path1", terminal_id="T1", provider="claude",
        model="claude-sonnet-5", pr_id=None, status="success",
        completion_pct=100, risk=0.0, findings=[], duration_seconds=1.0,
        token_usage={"input": 1, "output": 1}, cost_usd=None,
        state_dir=receipts_path.parent,
        verification={"method": "pytest", "tests_run": 1, "tests_passed": 1, "tests_failed": 0},
    )
    line = _read_lines(receipts_path)[-1]
    assert "session" not in line
    assert "terminal_id" in line  # orchestrator/agent-style identity, kept (§3.3)


# ---------------------------------------------------------------------------
# T27 — no receipt with schema_version:2 ever carries validation{} or
# session{} (the pre-PR-5 field names) -- the version stamp and the trimmed
# shape land in the same commit (HIGH-6), so the mixed "v1.5" shape cannot
# exist.
# ---------------------------------------------------------------------------


def test_t27_path2_schema_v2_never_carries_legacy_fields(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt(
        "t27-path2",
        verification={"method": "pytest", "tests_run": 1, "tests_passed": 1, "tests_failed": 0},
    )
    ar.append_receipt_payload(receipt, receipts_file=str(receipts_path), skip_enrichment=True)
    line = _read_lines(receipts_path)[-1]
    assert line["schema_version"] == 2
    assert "validation" not in line
    assert "session" not in line


def test_t27_path1_schema_v2_never_carries_legacy_fields(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    governance_emit.emit_dispatch_receipt(
        dispatch_id="t27-path1", terminal_id="T1", provider="claude",
        model="claude-sonnet-5", pr_id=None, status="success",
        completion_pct=100, risk=0.0, findings=[], duration_seconds=1.0,
        token_usage={"input": 1, "output": 1}, cost_usd=None,
        state_dir=receipts_path.parent,
        verification={"method": "pytest", "tests_run": 1, "tests_passed": 1, "tests_failed": 0},
    )
    line = _read_lines(receipts_path)[-1]
    assert line["schema_version"] == 2
    assert "validation" not in line
    assert "session" not in line


def test_t27_report_parser_output_renames_validation_to_verification(tmp_path):
    """report_parser.py::_build_enhanced_receipt (the historical validation{}
    producer, §3.3) now emits verification{}, never validation{} -- the
    consolidation the dispatch's rename step requires."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    from report_parser import ReportParser

    body = (
        "# Completion Report\n"
        "**Status**: success\n"
        "**Dispatch-ID**: t27-report-parser\n\n"
        "## Summary\nA genuine completion summary comfortably longer than "
        "fifty non-whitespace characters for the body contract.\n\n"
        "## Test Results\n4 tests passed, 0 failed\n\n"
        "## Open Items\nNone\n"
    )
    report_path = tmp_path / "report.md"
    report_path.write_text(body, encoding="utf-8")

    receipt = ReportParser().parse_report(str(report_path))
    assert "validation" not in receipt
    assert "confidence" not in receipt
    assert "legacy_format" not in receipt
    assert "tags" not in receipt
    assert "root_cause" not in receipt
    assert "dependencies" not in receipt
    assert "metrics" not in receipt
    assert "used_pattern_hashes" not in receipt
    assert receipt["verification"]["method"] == "pytest"
    assert receipt["verification"]["tests_run"] == 4
    assert receipt["verification"]["tests_passed"] == 4


# ---------------------------------------------------------------------------
# T27 fix-r1: the version stamp alone is not the guarantee -- a caller-
# supplied legacy field on a schema_version>=2 receipt must be REJECTED
# (fail-closed, nothing written), never silently accepted or stripped. This
# is the structural enforcement of "v2 <=> no legacy field" the codex-BLOCKING
# finding on PR #1198 required.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("legacy_field", LEGACY_TOP_LEVEL_FIELDS)
def test_t27_schema_v2_rejects_each_trimmed_top_level_field(tmp_path, legacy_field):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt(
        f"t27-reject-{legacy_field}",
        verification={"method": "pytest", "tests_run": 1, "tests_passed": 1, "tests_failed": 0},
    )
    # {} is a safe marker for every field here: presence is all the check
    # cares about, and an empty dict never trips up an unrelated pre-validation
    # reader (e.g. quality_advisory is peeked at by open_items_created
    # bookkeeping before the receipt ever reaches the validator).
    receipt[legacy_field] = {}

    with pytest.raises(ar.AppendReceiptError) as excinfo:
        ar.append_receipt_payload(receipt, receipts_file=str(receipts_path), skip_enrichment=True)

    assert excinfo.value.code == "legacy_field_on_v2_receipt"
    assert legacy_field in str(excinfo.value)
    assert not receipts_path.exists() or _read_lines(receipts_path) == []


@pytest.mark.parametrize("provenance_subkey", LEGACY_PROVENANCE_SUBKEYS)
def test_t27_schema_v2_rejects_trimmed_provenance_subkeys(tmp_path, provenance_subkey):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt(
        f"t27-reject-provenance-{provenance_subkey}",
        verification={"method": "pytest", "tests_run": 1, "tests_passed": 1, "tests_failed": 0},
        provenance={
            "git_ref": "HEAD", "branch": "main", "is_dirty": False,
            "dirty_files": 0, "diff_summary": None,
            provenance_subkey: "2026-01-01T00:00:00Z",
        },
    )

    with pytest.raises(ar.AppendReceiptError) as excinfo:
        ar.append_receipt_payload(receipt, receipts_file=str(receipts_path), skip_enrichment=True)

    assert excinfo.value.code == "legacy_field_on_v2_receipt"
    assert f"provenance.{provenance_subkey}" in str(excinfo.value)
    assert not receipts_path.exists() or _read_lines(receipts_path) == []


def test_t27_schema_v2_explicit_stamp_still_rejects(tmp_path):
    """An explicit schema_version=2 (not just the append primitive's own
    setdefault-stamp) is rejected exactly like the implicit/absent case --
    the check reads the resolved version, not how it got there."""
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt("t27-explicit-v2", schema_version=2, session={})

    with pytest.raises(ar.AppendReceiptError) as excinfo:
        ar.append_receipt_payload(receipt, receipts_file=str(receipts_path), skip_enrichment=True)

    assert excinfo.value.code == "legacy_field_on_v2_receipt"
    assert not receipts_path.exists() or _read_lines(receipts_path) == []


def test_t27_schema_v2_rejection_lists_every_offending_field(tmp_path):
    """Multiple legacy fields present at once are all named in the error,
    not just the first hit -- makes the rejection message actionable."""
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt("t27-multi-reject", session={}, validation={}, tags=[])

    with pytest.raises(ar.AppendReceiptError) as excinfo:
        ar.append_receipt_payload(receipt, receipts_file=str(receipts_path), skip_enrichment=True)

    message = str(excinfo.value)
    assert "session" in message
    assert "validation" in message
    assert "tags" in message


@pytest.mark.parametrize("legacy_field", LEGACY_TOP_LEVEL_FIELDS)
def test_t27_schema_v1_explicit_tolerates_legacy_field(tmp_path, legacy_field):
    """schema_version=1 (explicit) keeps full v1 tolerance end-to-end through
    the real append primitive -- the legacy field survives untouched. This is
    the append-only guarantee: v1 lines are never rewritten/rejected by the
    v2 shape rule."""
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt(f"t27-v1-tolerate-{legacy_field}", schema_version=1)
    receipt[legacy_field] = {}

    result = ar.append_receipt_payload(receipt, receipts_file=str(receipts_path), skip_enrichment=True)
    assert result.status == "appended"
    stored = _read_lines(receipts_path)[-1]
    assert stored["schema_version"] == 1
    assert stored[legacy_field] == {}


@pytest.mark.parametrize("legacy_field", LEGACY_TOP_LEVEL_FIELDS)
def test_t27_validator_schema_version_absent_tolerates_legacy_field(legacy_field):
    """Direct unit coverage of the shared validator (`_validate_receipt`,
    the exact choke point both write paths funnel through): a receipt with NO
    `schema_version` key at all resolves to v1 (`_resolve_schema_version`'s
    documented default) and tolerates every trimmed field. This is the
    validator-level guarantee distinct from `append_receipt_payload`'s own
    `setdefault("schema_version", 2)`, which promotes an absent key to v2
    before the validator ever sees it on that particular entry point."""
    receipt = {
        "timestamp": "2026-07-22T10:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": "t27-validator-absent",
        legacy_field: {},
    }
    event_name = _validate_receipt(receipt)
    assert event_name == "task_complete"


@pytest.mark.parametrize("legacy_field", LEGACY_TOP_LEVEL_FIELDS)
def test_t27_validator_schema_version_2_rejects_legacy_field(legacy_field):
    """Mirror of the tolerance test above, at the same direct-validator
    level: schema_version=2 rejects every trimmed field."""
    receipt = {
        "timestamp": "2026-07-22T10:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": "t27-validator-v2",
        "schema_version": 2,
        legacy_field: {},
    }
    with pytest.raises(ar.AppendReceiptError) as excinfo:
        _validate_receipt(receipt)
    assert excinfo.value.code == "legacy_field_on_v2_receipt"


# ---------------------------------------------------------------------------
# Field-trim regression checks: quality_advisory{} is gone, no live reader
# broken.
# ---------------------------------------------------------------------------


def test_quality_advisory_no_longer_written_on_completion_receipt(tmp_path):
    receipts_path = tmp_path / "t0_receipts.ndjson"
    receipt = _base_path2_receipt(
        "t-qa-gone",
        provenance={
            "git_ref": "HEAD", "branch": "test", "is_dirty": False,
            "dirty_files": 0, "diff_summary": None,
        },
        terminal="T1",
    )
    with patch.object(ar, "_resolve_model_provider",
                       return_value={"model": "claude-sonnet-4-6", "provider": "claude_code"}):
        with patch.object(ar, "_resolve_session_id", return_value="sess-qa-0001"):
            with patch.object(ar, "_extract_session_token_usage", return_value=None):
                with patch.object(ar, "collect_terminal_snapshot") as mock_snap:
                    snap = MagicMock()
                    snap.to_dict.return_value = {"status": "ok"}
                    mock_snap.return_value = snap
                    with patch.object(ar, "enrich_receipt_provenance", return_value=None):
                        with patch.object(ar, "validate_receipt_provenance") as mock_val:
                            mock_val.return_value = MagicMock(gaps=[], chain_status="ok")
                            result = ar.append_receipt_payload(receipt, receipts_file=str(receipts_path))

    assert result.status == "appended"
    line = _read_lines(receipts_path)[-1]
    assert "quality_advisory" not in line


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
