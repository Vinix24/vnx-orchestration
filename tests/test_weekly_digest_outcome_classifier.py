"""Unit tests for weekly_digest.py dispatch-outcome classification (fix A).

Covers:
  - success statuses classify as success
  - canonical failure statuses classify as failure
  - contract_invalid classifies as failure (RC-3 fix)
  - state_mutation events are skipped before total is counted (RC-2 fix)
  - review_gate_request events are skipped before total is counted (RC-2 fix)
  - empty / unknown status classifies as unknown
  - substring fallback for fail/error variants still works
  - timestamp filtering still works
  - mixed ledger: counts are accurate across all buckets
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Make scripts/ importable without env initialisation — we patch PATHS below.
_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
_LIB = _SCRIPTS / "lib"

if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


# ---------------------------------------------------------------------------
# Isolate collect_metrics from the real filesystem
# ---------------------------------------------------------------------------

def _write_receipts(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_NOW = datetime.now(tz=timezone.utc)
_RECENT = _iso(_NOW - timedelta(hours=1))
_OLD = _iso(_NOW - timedelta(days=10))


def _run_outcome_classifier(
    records: list[dict],
    *,
    tmp_path: Path,
    days: int = 7,
) -> dict:
    """Run only the receipts-outcome section of collect_metrics in isolation.

    Patches RECEIPTS_PATH and STATE_DIR so no real filesystem is touched.
    The DB and pending-edits paths are absent so only the receipts branch runs.
    """
    import importlib
    import unittest.mock as mock

    receipts_path = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts_path, records)

    # Patch module-level paths before importing to keep test hermetic.
    import weekly_digest

    with (
        mock.patch.object(weekly_digest, "RECEIPTS_PATH", receipts_path),
        mock.patch.object(weekly_digest, "DB_PATH", tmp_path / "nonexistent.db"),
        mock.patch.object(weekly_digest, "PENDING_PATH", tmp_path / "nonexistent.json"),
    ):
        metrics = weekly_digest.collect_metrics(days=days)

    return metrics["dispatch_outcomes"]


# ---------------------------------------------------------------------------
# Success statuses
# ---------------------------------------------------------------------------

class TestSuccessClassification:
    @pytest.mark.parametrize("status", ["success", "completed", "complete", "ok", "done"])
    def test_canonical_success_status(self, status: str, tmp_path: Path) -> None:
        records = [{"status": status, "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["success"] == 1
        assert out["failure"] == 0
        assert out["unknown"] == 0
        assert out["total"] == 1

    @pytest.mark.parametrize("status", ["SUCCESS", "Completed", "DONE"])
    def test_success_case_insensitive(self, status: str, tmp_path: Path) -> None:
        records = [{"status": status, "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["success"] == 1


# ---------------------------------------------------------------------------
# Failure statuses
# ---------------------------------------------------------------------------

class TestFailureClassification:
    @pytest.mark.parametrize("status", [
        "failed", "failure", "error", "blocked", "timeout", "contract_invalid",
    ])
    def test_canonical_failure_status(self, status: str, tmp_path: Path) -> None:
        records = [{"status": status, "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["failure"] == 1
        assert out["success"] == 0
        assert out["unknown"] == 0
        assert out["total"] == 1

    def test_contract_invalid_is_failure_not_unknown(self, tmp_path: Path) -> None:
        """RC-3 root cause: contract_invalid must map to failure, not unknown."""
        records = [{"status": "contract_invalid", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["failure"] == 1
        assert out["unknown"] == 0

    @pytest.mark.parametrize("status", ["FAILED", "FAILURE", "ERROR", "CONTRACT_INVALID"])
    def test_failure_case_insensitive(self, status: str, tmp_path: Path) -> None:
        records = [{"status": status, "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["failure"] == 1

    @pytest.mark.parametrize("status", ["task_failed", "deploy_error", "timed_out_with_error"])
    def test_substring_fallback_for_fail_error_variants(self, status: str, tmp_path: Path) -> None:
        """Substring fallback for non-canonical fail/error strings must still classify as failure."""
        records = [{"status": status, "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["failure"] == 1, f"Expected failure for status={status!r}"


# ---------------------------------------------------------------------------
# Skipped event types (RC-2 fix)
# ---------------------------------------------------------------------------

class TestSkippedEventTypes:
    @pytest.mark.parametrize("event_type", ["state_mutation", "review_gate_request"])
    def test_infra_event_not_counted_in_total(self, event_type: str, tmp_path: Path) -> None:
        """RC-2: infra events must be excluded from total before classification."""
        records = [{"event_type": event_type, "status": "success", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["total"] == 0
        assert out["success"] == 0
        assert out["unknown"] == 0

    def test_state_mutation_does_not_inflate_unknown(self, tmp_path: Path) -> None:
        """state_mutation with no recognisable status must NOT appear in unknown."""
        records = [{"event_type": "state_mutation", "status": "", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["total"] == 0
        assert out["unknown"] == 0

    def test_review_gate_request_does_not_inflate_unknown(self, tmp_path: Path) -> None:
        records = [{"event_type": "review_gate_request", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["total"] == 0

    def test_regular_event_with_status_still_counted(self, tmp_path: Path) -> None:
        """A dispatch record without a skip-event_type is counted normally."""
        records = [{"event_type": "subprocess_completion", "status": "success", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["total"] == 1
        assert out["success"] == 1


# ---------------------------------------------------------------------------
# Unknown classification
# ---------------------------------------------------------------------------

class TestUnknownClassification:
    def test_empty_status_is_unknown(self, tmp_path: Path) -> None:
        records = [{"status": "", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["unknown"] == 1
        assert out["total"] == 1

    def test_missing_status_is_unknown(self, tmp_path: Path) -> None:
        records = [{"timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["unknown"] == 1

    def test_unrecognised_status_is_unknown(self, tmp_path: Path) -> None:
        records = [{"status": "bananas", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["unknown"] == 1


class TestEventTypeFallback:
    """Empty status falls back to event_type — preserves pre-vocab recall
    (the old classifier read `status or event_type`); kimi-gate PR #837 F1."""

    def test_statusless_task_complete_is_success(self, tmp_path: Path) -> None:
        records = [{"status": "", "event_type": "task_complete", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["success"] == 1
        assert out["unknown"] == 0

    def test_statusless_task_failed_is_failure(self, tmp_path: Path) -> None:
        records = [{"status": "", "event_type": "task_failed", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["failure"] == 1

    def test_statusless_task_timeout_is_failure(self, tmp_path: Path) -> None:
        records = [{"status": "", "event_type": "task_timeout", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["failure"] == 1

    def test_explicit_status_wins_over_event_type(self, tmp_path: Path) -> None:
        # status="unknown" is an explicit (non-empty) value: no fallback,
        # stays unknown even when event_type says task_complete (RC-1 class).
        records = [{"status": "unknown", "event_type": "task_complete", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["unknown"] == 1


# ---------------------------------------------------------------------------
# Timestamp filtering
# ---------------------------------------------------------------------------

class TestTimestampFiltering:
    def test_old_record_excluded(self, tmp_path: Path) -> None:
        records = [{"status": "success", "timestamp": _OLD}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path, days=7)
        assert out["total"] == 0

    def test_recent_record_included(self, tmp_path: Path) -> None:
        records = [{"status": "success", "timestamp": _RECENT}]
        out = _run_outcome_classifier(records, tmp_path=tmp_path, days=7)
        assert out["total"] == 1


# ---------------------------------------------------------------------------
# Mixed ledger (representative of real-world data)
# ---------------------------------------------------------------------------

class TestMixedLedger:
    def test_representative_mixed_batch(self, tmp_path: Path) -> None:
        """Simulate a realistic slice: 3 success, 2 contract_invalid, 2 infra events, 1 unknown."""
        records = [
            {"status": "success", "timestamp": _RECENT},
            {"status": "completed", "timestamp": _RECENT},
            {"status": "done", "timestamp": _RECENT},
            {"status": "contract_invalid", "timestamp": _RECENT},
            {"status": "failed", "timestamp": _RECENT},
            {"event_type": "state_mutation", "status": "success", "timestamp": _RECENT},
            {"event_type": "review_gate_request", "timestamp": _RECENT},
            {"status": "bananas", "timestamp": _RECENT},
        ]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)

        assert out["total"] == 6           # 2 infra events skipped
        assert out["success"] == 3
        assert out["failure"] == 2         # contract_invalid + failed
        assert out["unknown"] == 1         # bananas

    def test_all_infra_events_give_zero_total(self, tmp_path: Path) -> None:
        records = [
            {"event_type": "state_mutation", "timestamp": _RECENT},
            {"event_type": "review_gate_request", "timestamp": _RECENT},
        ]
        out = _run_outcome_classifier(records, tmp_path=tmp_path)
        assert out["total"] == 0
        assert out["success"] == 0
        assert out["failure"] == 0
        assert out["unknown"] == 0

    def test_empty_receipts_file(self, tmp_path: Path) -> None:
        out = _run_outcome_classifier([], tmp_path=tmp_path)
        assert out == {"total": 0, "success": 0, "failure": 0, "unknown": 0}


# ---------------------------------------------------------------------------
# check_active_drain contract_invalid sync
# ---------------------------------------------------------------------------

class TestDrainContractInvalidSync:
    """Verify check_active_drain.py FAILURE_STATUSES now includes contract_invalid."""

    def test_contract_invalid_in_drain_failure_statuses(self) -> None:
        from check_active_drain import FAILURE_STATUSES
        assert "contract_invalid" in FAILURE_STATUSES

    def test_contract_invalid_routes_to_dead_letter(self, tmp_path: Path) -> None:
        """A receipt with status=contract_invalid must drain to dead_letter."""
        import json as _json
        from datetime import datetime, timedelta, timezone as _tz
        from check_active_drain import (
            DispatchEntry,
            drain_one,
            build_receipt_status_index,
        )

        # Build minimal .vnx-data structure
        receipts_processed = tmp_path / "receipts" / "processed"
        receipts_processed.mkdir(parents=True)
        dispatches_dir = tmp_path / "dispatches"
        for bucket in ("active", "completed", "dead_letter"):
            (dispatches_dir / bucket).mkdir(parents=True)

        did = "20260610-contract-invalid-test"
        active_dir = dispatches_dir / "active" / did
        active_dir.mkdir(parents=True)
        (active_dir / "manifest.json").write_text(
            _json.dumps({"dispatch_id": did, "timestamp": _RECENT}),
            encoding="utf-8",
        )

        receipt_file = receipts_processed / f"receipt-{did}.json"
        receipt_file.write_text(
            _json.dumps({"dispatch_id": did, "status": "contract_invalid"}),
            encoding="utf-8",
        )

        idx = build_receipt_status_index(tmp_path / "receipts")
        assert idx[did] == "failure"

        ts = datetime.now(tz=_tz.utc) - timedelta(hours=2)
        entry = DispatchEntry(dispatch_id=did, directory=active_dir, timestamp=ts)
        result = drain_one(
            entry=entry,
            receipt_index=idx,
            dispatches_dir=dispatches_dir,
            now=datetime.now(tz=_tz.utc),
            older_than_seconds=3600,
            dry_run=False,
        )
        assert result.action == "dead_letter"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
