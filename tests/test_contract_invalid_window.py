"""tests/test_contract_invalid_window.py — contract_invalid counter-windowing.

Covers the windowing-only salvage of the parked report_contract_invalid fix
(dispatch 20260717-contract-invalid-windowing):

  1. scripts/lib/contract_invalid_window.py — is_stale_contract_invalid() unit
     tests: default/custom window, env override, fail-open on missing/
     unparseable time, ingested_at-over-timestamp precedence.
  2. scripts/lib/append_receipt_internals/payload.py — _stamp_ingested_at()
     always overwrites a caller-supplied value.
  3. scripts/weekly_digest.py, scripts/learning_loop.py,
     scripts/check_active_drain.py — a frozen old batch is excluded from the
     live counters while a fresh contract_invalid (including one with a
     forged old `timestamp` but a fresh `ingested_at`) still counts.

The classification half (report_exempt / panel-seat / benchmark exemptions)
is intentionally out of scope — parked for the receipt-v2 redesign.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
_LIB = _SCRIPTS / "lib"

if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from contract_invalid_window import (  # noqa: E402
    DEFAULT_WINDOW_DAYS,
    ENV_WINDOW_DAYS,
    contract_invalid_effective_timestamp,
    contract_invalid_window_days,
    is_stale_contract_invalid,
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_NOW = datetime.now(tz=timezone.utc)
_FRESH = _iso(_NOW - timedelta(hours=1))
_OLD_26D = _iso(_NOW - timedelta(days=26))
_OLD_10D = _iso(_NOW - timedelta(days=10))


# ---------------------------------------------------------------------------
# 1. is_stale_contract_invalid — unit tests
# ---------------------------------------------------------------------------

class TestIsStaleContractInvalid:
    def test_fresh_ingested_at_not_stale(self) -> None:
        record = {"ingested_at": _FRESH}
        assert is_stale_contract_invalid(record, now=_NOW) is False

    def test_old_ingested_at_is_stale(self) -> None:
        record = {"ingested_at": _OLD_26D}
        assert is_stale_contract_invalid(record, now=_NOW) is True

    def test_forged_old_timestamp_fresh_ingested_at_not_stale(self) -> None:
        """A worker cannot backdate its own failure out of the window: the
        record windows on ingested_at, not the worker-suppliable timestamp."""
        record = {"timestamp": _OLD_26D, "ingested_at": _FRESH}
        assert is_stale_contract_invalid(record, now=_NOW) is False

    def test_missing_ingested_at_falls_back_to_timestamp_when_old(self) -> None:
        """Old v1 record (pre-ingested_at) — falls back to timestamp."""
        record = {"timestamp": _OLD_26D}
        assert is_stale_contract_invalid(record, now=_NOW) is True

    def test_missing_ingested_at_falls_back_to_timestamp_when_fresh(self) -> None:
        record = {"timestamp": _FRESH}
        assert is_stale_contract_invalid(record, now=_NOW) is False

    def test_missing_both_fields_fail_open_not_stale(self) -> None:
        record = {"status": "contract_invalid"}
        assert is_stale_contract_invalid(record, now=_NOW) is False

    def test_unparseable_ingested_at_fail_open_not_stale(self) -> None:
        record = {"ingested_at": "not-a-timestamp"}
        assert is_stale_contract_invalid(record, now=_NOW) is False

    def test_empty_string_ingested_at_fail_open_not_stale(self) -> None:
        record = {"ingested_at": "", "timestamp": ""}
        assert is_stale_contract_invalid(record, now=_NOW) is False

    def test_custom_window_days_narrower_than_default(self) -> None:
        record = {"ingested_at": _OLD_10D}
        # 10 days old: within the default 14d window (not stale)...
        assert is_stale_contract_invalid(record, now=_NOW) is False
        # ...but stale under an explicit 1-day window.
        assert is_stale_contract_invalid(record, window_days=1, now=_NOW) is True

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_WINDOW_DAYS, "1")
        record = {"ingested_at": _OLD_10D}
        assert is_stale_contract_invalid(record, now=_NOW) is True
        assert contract_invalid_window_days() == 1

    def test_env_var_invalid_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_WINDOW_DAYS, "not-a-number")
        assert contract_invalid_window_days() == DEFAULT_WINDOW_DAYS

    def test_default_window_days_is_14(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_WINDOW_DAYS, raising=False)
        assert contract_invalid_window_days() == 14

    def test_effective_timestamp_prefers_ingested_at(self) -> None:
        record = {"timestamp": _OLD_26D, "ingested_at": _FRESH}
        assert contract_invalid_effective_timestamp(record) == _FRESH

    def test_effective_timestamp_falls_back_to_timestamp(self) -> None:
        record = {"timestamp": _OLD_26D}
        assert contract_invalid_effective_timestamp(record) == _OLD_26D

    def test_effective_timestamp_none_when_both_missing(self) -> None:
        assert contract_invalid_effective_timestamp({}) is None


# ---------------------------------------------------------------------------
# 2. payload._stamp_ingested_at — always overwrites
# ---------------------------------------------------------------------------

class TestStampIngestedAt:
    def test_sets_when_absent(self) -> None:
        import append_receipt_internals.payload as payload_mod

        receipt: dict = {"dispatch_id": "d-1"}
        payload_mod._stamp_ingested_at(receipt)
        assert "ingested_at" in receipt
        dt = datetime.fromisoformat(receipt["ingested_at"].replace("Z", "+00:00"))
        assert (datetime.now(timezone.utc) - dt) < timedelta(minutes=1)

    def test_always_overwrites_caller_supplied_value(self) -> None:
        """A worker-forged ingested_at in the JSON payload must never stick —
        otherwise a worker could pre-stamp an old ingested_at and defeat the
        staleness window this field exists to make forge-proof."""
        import append_receipt_internals.payload as payload_mod

        forged = "2020-01-01T00:00:00Z"
        receipt: dict = {"dispatch_id": "d-1", "ingested_at": forged}
        payload_mod._stamp_ingested_at(receipt)
        assert receipt["ingested_at"] != forged
        dt = datetime.fromisoformat(receipt["ingested_at"].replace("Z", "+00:00"))
        assert (datetime.now(timezone.utc) - dt) < timedelta(minutes=1)


# ---------------------------------------------------------------------------
# 3a. weekly_digest.collect_metrics — windowing integration
# ---------------------------------------------------------------------------

def _write_receipts(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _run_weekly_digest(records: list, *, tmp_path: Path, days: int = 7) -> dict:
    import unittest.mock as mock
    import weekly_digest

    receipts_path = tmp_path / "t0_receipts.ndjson"
    _write_receipts(receipts_path, records)

    with (
        mock.patch.object(weekly_digest, "RECEIPTS_PATH", receipts_path),
        mock.patch.object(weekly_digest, "DB_PATH", tmp_path / "nonexistent.db"),
        mock.patch.object(weekly_digest, "PENDING_PATH", tmp_path / "nonexistent.json"),
    ):
        metrics = weekly_digest.collect_metrics(days=days)
    return metrics["dispatch_outcomes"]


class TestWeeklyDigestWindowing:
    def test_frozen_batch_excluded_fresh_failure_counted(self, tmp_path: Path) -> None:
        """36 frozen contract_invalid records (one old June-style batch) plus a
        single fresh real failure — only the fresh one counts as live."""
        frozen_batch = [
            {"status": "contract_invalid", "ingested_at": _OLD_26D}
            for _ in range(36)
        ]
        fresh_failure = {"status": "contract_invalid", "ingested_at": _FRESH}
        out = _run_weekly_digest(frozen_batch + [fresh_failure], tmp_path=tmp_path)
        assert out["total"] == 1
        assert out["failure"] == 1

    def test_forged_timestamp_fresh_ingested_at_still_counted(self, tmp_path: Path) -> None:
        """A contract_invalid receipt with a forged old `timestamp` (outside the
        digest's own --days window) but a fresh ingested_at must still count —
        it must not be dropped by the generic --days filter before it reaches
        the dedicated staleness check."""
        record = {"status": "contract_invalid", "timestamp": _OLD_26D, "ingested_at": _FRESH}
        out = _run_weekly_digest([record], tmp_path=tmp_path, days=7)
        assert out["total"] == 1
        assert out["failure"] == 1

    def test_missing_ingested_at_fallback_counts_fresh_timestamp(self, tmp_path: Path) -> None:
        record = {"status": "contract_invalid", "timestamp": _FRESH}
        out = _run_weekly_digest([record], tmp_path=tmp_path)
        assert out["total"] == 1
        assert out["failure"] == 1

    def test_missing_ingested_at_fallback_excludes_old_timestamp(self, tmp_path: Path) -> None:
        record = {"status": "contract_invalid", "timestamp": _OLD_26D}
        out = _run_weekly_digest([record], tmp_path=tmp_path)
        assert out["total"] == 0
        assert out["failure"] == 0

    def test_missing_both_fields_fail_open_counted(self, tmp_path: Path) -> None:
        record = {"status": "contract_invalid"}
        out = _run_weekly_digest([record], tmp_path=tmp_path)
        assert out["total"] == 1
        assert out["failure"] == 1

    def test_event_type_report_contract_invalid_windowed_too(self, tmp_path: Path) -> None:
        stale = {"event_type": "report_contract_invalid", "status": "contract_invalid", "ingested_at": _OLD_26D}
        out = _run_weekly_digest([stale], tmp_path=tmp_path)
        assert out["total"] == 0

    def test_non_contract_invalid_failure_unaffected(self, tmp_path: Path) -> None:
        """Regression guard: ordinary failure statuses keep windowing on the
        worker-suppliable timestamp exactly as before."""
        old_failure = {"status": "failed", "timestamp": _OLD_26D}
        fresh_failure = {"status": "failed", "timestamp": _FRESH}
        out = _run_weekly_digest([old_failure, fresh_failure], tmp_path=tmp_path, days=7)
        assert out["total"] == 1
        assert out["failure"] == 1


# ---------------------------------------------------------------------------
# 3b. learning_loop.extract_failure_patterns — windowing integration
# ---------------------------------------------------------------------------

def _build_learning_loop(receipts_path: Path):
    import learning_loop as ll

    loop = ll.LearningLoop.__new__(ll.LearningLoop)
    loop.receipts_path = receipts_path
    return loop


class TestLearningLoopWindowing:
    def test_frozen_batch_excluded_fresh_failure_counted(self, tmp_path: Path) -> None:
        receipts_path = tmp_path / "t0_receipts.ndjson"
        frozen_batch = [
            {"status": "contract_invalid", "ingested_at": _OLD_26D, "provider": "claude"}
            for _ in range(36)
        ]
        fresh_failure = {"status": "contract_invalid", "ingested_at": _FRESH, "provider": "claude"}
        _write_receipts(receipts_path, frozen_batch + [fresh_failure])

        loop = _build_learning_loop(receipts_path)
        start_time = datetime.now(timezone.utc) - timedelta(days=365)
        patterns = loop.extract_failure_patterns(start_time=start_time)
        assert len(patterns) == 1

    def test_forged_timestamp_fresh_ingested_at_still_counted(self, tmp_path: Path) -> None:
        receipts_path = tmp_path / "t0_receipts.ndjson"
        record = {
            "status": "contract_invalid",
            "timestamp": _OLD_26D,
            "ingested_at": _FRESH,
            "provider": "claude",
        }
        _write_receipts(receipts_path, [record])

        loop = _build_learning_loop(receipts_path)
        start_time = datetime.now(timezone.utc) - timedelta(days=7)
        patterns = loop.extract_failure_patterns(start_time=start_time)
        assert len(patterns) == 1

    def test_missing_ingested_at_fallback_excludes_old_timestamp(self, tmp_path: Path) -> None:
        receipts_path = tmp_path / "t0_receipts.ndjson"
        record = {"status": "contract_invalid", "timestamp": _OLD_26D, "provider": "claude"}
        _write_receipts(receipts_path, [record])

        loop = _build_learning_loop(receipts_path)
        start_time = datetime.now(timezone.utc) - timedelta(days=365)
        patterns = loop.extract_failure_patterns(start_time=start_time)
        # Stale via the dedicated staleness check (falls back to `timestamp`).
        assert len(patterns) == 0

    def test_non_contract_invalid_failure_unaffected(self, tmp_path: Path) -> None:
        receipts_path = tmp_path / "t0_receipts.ndjson"
        fresh_failure = {"status": "failed", "timestamp": _FRESH, "provider": "claude"}
        _write_receipts(receipts_path, [fresh_failure])

        loop = _build_learning_loop(receipts_path)
        start_time = datetime.now(timezone.utc) - timedelta(days=7)
        patterns = loop.extract_failure_patterns(start_time=start_time)
        assert len(patterns) == 1


# ---------------------------------------------------------------------------
# 3c. check_active_drain.build_receipt_status_index — windowing integration
# ---------------------------------------------------------------------------

class TestCheckActiveDrainWindowing:
    def _write_processed_receipt(self, receipts_dir: Path, dispatch_id: str, record: dict) -> None:
        processed = receipts_dir / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        payload = {"dispatch_id": dispatch_id, **record}
        (processed / f"receipt-{dispatch_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_stale_contract_invalid_falls_through_as_if_absent(self, tmp_path: Path) -> None:
        from check_active_drain import build_receipt_status_index

        receipts_dir = tmp_path / "receipts"
        self._write_processed_receipt(
            receipts_dir, "d-stale", {"status": "contract_invalid", "ingested_at": _OLD_26D}
        )
        idx = build_receipt_status_index(receipts_dir)
        assert "d-stale" not in idx

    def test_fresh_contract_invalid_is_failure(self, tmp_path: Path) -> None:
        from check_active_drain import build_receipt_status_index

        receipts_dir = tmp_path / "receipts"
        self._write_processed_receipt(
            receipts_dir, "d-fresh", {"status": "contract_invalid", "ingested_at": _FRESH}
        )
        idx = build_receipt_status_index(receipts_dir)
        assert idx["d-fresh"] == "failure"

    def test_forged_timestamp_fresh_ingested_at_still_failure(self, tmp_path: Path) -> None:
        from check_active_drain import build_receipt_status_index

        receipts_dir = tmp_path / "receipts"
        self._write_processed_receipt(
            receipts_dir,
            "d-forged",
            {"status": "contract_invalid", "timestamp": _OLD_26D, "ingested_at": _FRESH},
        )
        idx = build_receipt_status_index(receipts_dir)
        assert idx["d-forged"] == "failure"

    def test_missing_timestamp_fail_open_still_failure(self, tmp_path: Path) -> None:
        """Backward-compat guard: a contract_invalid receipt with no timestamp
        field at all (pre-existing fixture shape) must still route to failure."""
        from check_active_drain import build_receipt_status_index

        receipts_dir = tmp_path / "receipts"
        self._write_processed_receipt(receipts_dir, "d-no-ts", {"status": "contract_invalid"})
        idx = build_receipt_status_index(receipts_dir)
        assert idx["d-no-ts"] == "failure"

    def test_stale_via_timestamp_fallback_falls_through(self, tmp_path: Path) -> None:
        from check_active_drain import build_receipt_status_index

        receipts_dir = tmp_path / "receipts"
        self._write_processed_receipt(
            receipts_dir, "d-old-v1", {"status": "contract_invalid", "timestamp": _OLD_26D}
        )
        idx = build_receipt_status_index(receipts_dir)
        assert "d-old-v1" not in idx


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
