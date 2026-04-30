"""Tests for scripts/check_active_drain.py — dispatch active-drain janitor."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Make scripts/ importable
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(_SCRIPTS / "lib") not in sys.path:
    sys.path.insert(0, str(_SCRIPTS / "lib"))

from check_active_drain import (  # noqa: E402
    DrainResult,
    build_receipt_index,
    build_receipt_status_index,
    drain_active,
    drain_one,
    iter_active_dispatches,
    DispatchEntry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_data_dir(tmp_path: Path) -> Path:
    """Create minimal .vnx-data skeleton with active/ and receipts/processed/."""
    data = tmp_path / ".vnx-data"
    (data / "dispatches" / "active").mkdir(parents=True)
    (data / "dispatches" / "completed").mkdir(parents=True)
    (data / "dispatches" / "dead_letter").mkdir(parents=True)
    (data / "receipts" / "processed").mkdir(parents=True)
    return data


def _make_active_dispatch(
    data: Path,
    dispatch_id: str,
    hours_old: float = 2.0,
) -> Path:
    """Create a minimal active dispatch directory with manifest.json."""
    d = data / "dispatches" / "active" / dispatch_id
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc) - timedelta(hours=hours_old)
    (d / "manifest.json").write_text(
        json.dumps({
            "dispatch_id": dispatch_id,
            "timestamp": ts.isoformat(),
            "terminal": "T1",
            "model": "sonnet",
            "role": "backend-developer",
        }),
        encoding="utf-8",
    )
    return d


def _make_receipt(
    data: Path,
    dispatch_id: str,
    pid: int = 9999,
    status: str = "success",
) -> Path:
    """Create a processed receipt for the given dispatch_id."""
    receipt = data / "receipts" / "processed" / f"1776{pid}-{dispatch_id[:12]}-{pid}.json"
    receipt.write_text(
        json.dumps({
            "dispatch_id": dispatch_id,
            "event_type": "task_complete",
            "status": status,
        }),
        encoding="utf-8",
    )
    return receipt


# ---------------------------------------------------------------------------
# build_receipt_index
# ---------------------------------------------------------------------------

class TestBuildReceiptIndex:
    def test_empty_dir(self, tmp_path: Path) -> None:
        processed = tmp_path / "receipts" / "processed"
        processed.mkdir(parents=True)
        assert build_receipt_index(tmp_path / "receipts") == frozenset()

    def test_missing_processed_dir(self, tmp_path: Path) -> None:
        assert build_receipt_index(tmp_path / "receipts") == frozenset()

    def test_indexes_known_dispatch_ids(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        _make_receipt(data, "20260414-090100-f58-pr2-A", pid=1001)
        _make_receipt(data, "20260414-090200-f58-pr3-A", pid=1002)
        index = build_receipt_index(data / "receipts")
        assert "20260414-090100-f58-pr2-A" in index
        assert "20260414-090200-f58-pr3-A" in index

    def test_skips_unknown_dispatch_id(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        receipt = data / "receipts" / "processed" / "1776-unknown-99.json"
        receipt.write_text(json.dumps({"dispatch_id": "unknown"}), encoding="utf-8")
        index = build_receipt_index(data / "receipts")
        assert "unknown" not in index

    def test_skips_empty_dispatch_id(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        receipt = data / "receipts" / "processed" / "1776-empty-99.json"
        receipt.write_text(json.dumps({"dispatch_id": ""}), encoding="utf-8")
        index = build_receipt_index(data / "receipts")
        assert "" not in index

    def test_skips_malformed_json(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        bad = data / "receipts" / "processed" / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        # Should not raise; bad file is silently skipped
        index = build_receipt_index(data / "receipts")
        assert len(index) == 0

    def test_ignores_non_json_files(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        (data / "receipts" / "processed" / "readme.txt").write_text("hello", encoding="utf-8")
        index = build_receipt_index(data / "receipts")
        assert len(index) == 0


# ---------------------------------------------------------------------------
# iter_active_dispatches
# ---------------------------------------------------------------------------

class TestIterActiveDispatches:
    def test_empty_active(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        entries = list(iter_active_dispatches(data / "dispatches"))
        assert entries == []

    def test_missing_active_dir(self, tmp_path: Path) -> None:
        dispatches = tmp_path / "dispatches"
        dispatches.mkdir()
        entries = list(iter_active_dispatches(dispatches))
        assert entries == []

    def test_yields_dispatch_with_manifest(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        _make_active_dispatch(data, "20260414-test-A", hours_old=3.0)
        entries = list(iter_active_dispatches(data / "dispatches"))
        assert len(entries) == 1
        assert entries[0].dispatch_id == "20260414-test-A"
        assert entries[0].timestamp is not None

    def test_yields_dispatch_without_manifest(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        bare = data / "dispatches" / "active" / "bare-dispatch-B"
        bare.mkdir(parents=True)
        entries = list(iter_active_dispatches(data / "dispatches"))
        assert len(entries) == 1
        assert entries[0].dispatch_id == "bare-dispatch-B"
        assert entries[0].timestamp is None

    def test_skips_files_in_active(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        (data / "dispatches" / "active" / "stray.txt").write_text("x")
        entries = list(iter_active_dispatches(data / "dispatches"))
        assert entries == []


# ---------------------------------------------------------------------------
# drain_one
# ---------------------------------------------------------------------------

class TestDrainOne:
    def _now(self) -> datetime:
        return datetime.now(tz=timezone.utc)

    def test_moves_to_completed_when_receipt_exists(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        did = "20260414-090100-test-A"
        d = _make_active_dispatch(data, did, hours_old=5.0)
        receipt_index = frozenset({did})

        ts = datetime.now(tz=timezone.utc) - timedelta(hours=5)
        entry = DispatchEntry(dispatch_id=did, directory=d, timestamp=ts)

        result = drain_one(
            entry=entry,
            receipt_index=receipt_index,
            dispatches_dir=data / "dispatches",
            now=self._now(),
            older_than_seconds=3600,
            dry_run=False,
        )
        assert result.action == "completed"
        assert not (data / "dispatches" / "active" / did).exists()
        assert (data / "dispatches" / "completed" / did).exists()

    def test_moves_to_dead_letter_when_old_no_receipt(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        did = "20260414-old-orphan-A"
        d = _make_active_dispatch(data, did, hours_old=10.0)
        ts = datetime.now(tz=timezone.utc) - timedelta(hours=10)
        entry = DispatchEntry(dispatch_id=did, directory=d, timestamp=ts)

        result = drain_one(
            entry=entry,
            receipt_index=frozenset(),
            dispatches_dir=data / "dispatches",
            now=self._now(),
            older_than_seconds=3600,
            dry_run=False,
        )
        assert result.action == "dead_letter"
        assert not (data / "dispatches" / "active" / did).exists()
        assert (data / "dispatches" / "dead_letter" / did).exists()

    def test_skips_young_dispatch_without_receipt(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        did = "20260423-new-dispatch-A"
        d = _make_active_dispatch(data, did, hours_old=0.1)
        ts = datetime.now(tz=timezone.utc) - timedelta(minutes=6)
        entry = DispatchEntry(dispatch_id=did, directory=d, timestamp=ts)

        result = drain_one(
            entry=entry,
            receipt_index=frozenset(),
            dispatches_dir=data / "dispatches",
            now=self._now(),
            older_than_seconds=3600,
            dry_run=False,
        )
        assert result.action == "skipped"
        assert (data / "dispatches" / "active" / did).exists()

    def test_dry_run_does_not_move(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        did = "20260414-dry-run-A"
        d = _make_active_dispatch(data, did, hours_old=5.0)
        receipt_index = frozenset({did})
        ts = datetime.now(tz=timezone.utc) - timedelta(hours=5)
        entry = DispatchEntry(dispatch_id=did, directory=d, timestamp=ts)

        result = drain_one(
            entry=entry,
            receipt_index=receipt_index,
            dispatches_dir=data / "dispatches",
            now=self._now(),
            older_than_seconds=3600,
            dry_run=True,
        )
        assert result.action == "completed"
        assert result.dry_run is True
        # Original directory still exists
        assert (data / "dispatches" / "active" / did).exists()

    def test_no_timestamp_treated_as_dead_letter(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        did = "no-timestamp-dispatch-A"
        d = data / "dispatches" / "active" / did
        d.mkdir(parents=True)
        entry = DispatchEntry(dispatch_id=did, directory=d, timestamp=None)

        result = drain_one(
            entry=entry,
            receipt_index=frozenset(),
            dispatches_dir=data / "dispatches",
            now=self._now(),
            older_than_seconds=3600,
            dry_run=False,
        )
        assert result.action == "dead_letter"

    def test_receipt_beats_age_threshold(self, tmp_path: Path) -> None:
        """A dispatch with a receipt moves to completed even if it is very old."""
        data = _make_data_dir(tmp_path)
        did = "20260401-old-but-receipted-A"
        d = _make_active_dispatch(data, did, hours_old=200.0)
        receipt_index = frozenset({did})
        ts = datetime.now(tz=timezone.utc) - timedelta(hours=200)
        entry = DispatchEntry(dispatch_id=did, directory=d, timestamp=ts)

        result = drain_one(
            entry=entry,
            receipt_index=receipt_index,
            dispatches_dir=data / "dispatches",
            now=self._now(),
            older_than_seconds=3600,
            dry_run=False,
        )
        assert result.action == "completed"


# ---------------------------------------------------------------------------
# drain_active (integration)
# ---------------------------------------------------------------------------

class TestDrainActive:
    def test_empty_active_returns_empty(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        results = drain_active(data_dir=data, older_than_hours=1.0, dry_run=False)
        assert results == []

    def test_mixed_bag(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        # completed via receipt
        _make_active_dispatch(data, "dispatch-with-receipt", hours_old=5.0)
        _make_receipt(data, "dispatch-with-receipt", pid=1)
        # dead_letter: old + no receipt
        _make_active_dispatch(data, "dispatch-orphan-old", hours_old=48.0)
        # skipped: new + no receipt
        _make_active_dispatch(data, "dispatch-new", hours_old=0.2)

        results = drain_active(data_dir=data, older_than_hours=1.0, dry_run=False)
        by_id = {r.dispatch_id: r for r in results}

        assert by_id["dispatch-with-receipt"].action == "completed"
        assert by_id["dispatch-orphan-old"].action == "dead_letter"
        assert by_id["dispatch-new"].action == "skipped"

    def test_dry_run_leaves_active_untouched(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        _make_active_dispatch(data, "dry-run-dispatch", hours_old=5.0)
        _make_receipt(data, "dry-run-dispatch", pid=2)

        results = drain_active(data_dir=data, older_than_hours=1.0, dry_run=True)
        assert len(results) == 1
        assert results[0].action == "completed"
        assert results[0].dry_run is True
        assert (data / "dispatches" / "active" / "dry-run-dispatch").exists()
        assert not (data / "dispatches" / "completed" / "dry-run-dispatch").exists()

    def test_custom_age_threshold(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        # 3 hours old with no receipt — below a 6h threshold → skipped
        _make_active_dispatch(data, "dispatch-medium-age", hours_old=3.0)

        results = drain_active(data_dir=data, older_than_hours=6.0, dry_run=False)
        assert results[0].action == "skipped"

    def test_multiple_receipts_for_different_dispatches(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        for i in range(3):
            did = f"multi-dispatch-{i:03d}"
            _make_active_dispatch(data, did, hours_old=5.0)
            _make_receipt(data, did, pid=100 + i)

        results = drain_active(data_dir=data, older_than_hours=1.0, dry_run=False)
        assert all(r.action == "completed" for r in results)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# CFX-1 round-1 finding: status-aware drain
# ---------------------------------------------------------------------------

class TestStatusAwareDrain:
    """Codex round-1 PR #320: receipts with failure statuses must NOT cause
    a dispatch to be drained as completed work."""

    def test_status_index_classifies_success_and_failure(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        _make_receipt(data, "ok-dispatch", pid=1, status="success")
        _make_receipt(data, "bad-dispatch", pid=2, status="failed")
        _make_receipt(data, "err-dispatch", pid=3, status="error")
        _make_receipt(data, "blocked-dispatch", pid=4, status="blocked")
        _make_receipt(data, "weird-dispatch", pid=5, status="bananas")

        idx = build_receipt_status_index(data / "receipts")
        assert idx["ok-dispatch"] == "success"
        assert idx["bad-dispatch"] == "failure"
        assert idx["err-dispatch"] == "failure"
        assert idx["blocked-dispatch"] == "failure"
        assert idx["weird-dispatch"] == "unknown"

    def test_status_index_failure_wins_over_success(self, tmp_path: Path) -> None:
        """If two receipts disagree, the failure status must win — fail-closed."""
        data = _make_data_dir(tmp_path)
        _make_receipt(data, "split-dispatch", pid=10, status="success")
        _make_receipt(data, "split-dispatch", pid=11, status="failed")

        idx = build_receipt_status_index(data / "receipts")
        assert idx["split-dispatch"] == "failure"

    def test_legacy_build_receipt_index_still_returns_frozenset(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        _make_receipt(data, "legacy-dispatch", pid=20, status="success")
        idx = build_receipt_index(data / "receipts")
        assert isinstance(idx, frozenset)
        assert "legacy-dispatch" in idx

    def test_failed_receipt_routes_to_dead_letter(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        did = "20260429-failed-dispatch"
        d = _make_active_dispatch(data, did, hours_old=2.0)
        _make_receipt(data, did, pid=30, status="failed")

        results = drain_active(data_dir=data, older_than_hours=1.0, dry_run=False)
        assert len(results) == 1
        assert results[0].action == "dead_letter"
        assert "failure" in results[0].reason
        assert (data / "dispatches" / "dead_letter" / did).exists()
        assert not (data / "dispatches" / "completed" / did).exists()
        # dispatch directory must be removed from active/
        assert not d.exists()

    def test_timeout_receipt_routes_to_dead_letter(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        did = "20260429-timeout-dispatch"
        _make_active_dispatch(data, did, hours_old=2.0)
        _make_receipt(data, did, pid=31, status="timeout")

        results = drain_active(data_dir=data, older_than_hours=1.0, dry_run=False)
        assert results[0].action == "dead_letter"

    def test_unknown_status_routes_to_dead_letter(self, tmp_path: Path) -> None:
        """Unrecognised statuses fail closed — never silently completed."""
        data = _make_data_dir(tmp_path)
        did = "20260429-mystery-dispatch"
        _make_active_dispatch(data, did, hours_old=2.0)
        _make_receipt(data, did, pid=32, status="surprise")

        results = drain_active(data_dir=data, older_than_hours=1.0, dry_run=False)
        assert results[0].action == "dead_letter"
        assert "unrecognised" in results[0].reason

    def test_success_receipt_still_routes_to_completed(self, tmp_path: Path) -> None:
        data = _make_data_dir(tmp_path)
        did = "20260429-happy-dispatch"
        _make_active_dispatch(data, did, hours_old=2.0)
        _make_receipt(data, did, pid=33, status="success")

        results = drain_active(data_dir=data, older_than_hours=1.0, dry_run=False)
        assert results[0].action == "completed"
        assert "success" in results[0].reason

    def test_drain_one_accepts_legacy_frozenset_index(self, tmp_path: Path) -> None:
        """External callers passing the legacy frozenset must still get the
        success-routing they previously relied on."""
        data = _make_data_dir(tmp_path)
        did = "legacy-frozenset"
        d = _make_active_dispatch(data, did, hours_old=2.0)
        from datetime import datetime, timedelta, timezone as _tz
        ts = datetime.now(tz=_tz.utc) - timedelta(hours=2)
        entry = DispatchEntry(dispatch_id=did, directory=d, timestamp=ts)

        result = drain_one(
            entry=entry,
            receipt_index=frozenset({did}),
            dispatches_dir=data / "dispatches",
            now=datetime.now(tz=_tz.utc),
            older_than_seconds=3600,
            dry_run=False,
        )
        assert result.action == "completed"
