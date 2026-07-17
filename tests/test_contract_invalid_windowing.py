"""Integration tests for the 14-day contract_invalid staleness window.

Acceptance criterion (dispatch 20260716-report-contract-scope, part 2): a
frozen historical batch of `contract_invalid` receipts (bulk-emitted, all
sharing one old timestamp) must not inflate a "live failure" counter, while a
genuinely fresh failure in the same ledger still counts. Each of the three
named counter scripts gets its own tmp-ledger fixture with exactly that shape:
one old contract_invalid batch + one fresh real failure.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
LIB = SCRIPTS / "lib"
for p in (str(SCRIPTS), str(LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

_NOW = datetime.now(tz=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_FROZEN_BATCH_TS = _iso(_NOW - timedelta(days=26))  # older than the 14d window
_FRESH_FAILURE_TS = _iso(_NOW - timedelta(hours=2))  # inside the window

# codex-gate fix-round (#1184) Finding 2 (HIGH): a worker-forged old
# report-body `timestamp` (report_to_receipt_converter.py copies it straight
# out of the report frontmatter) must not vanish a receipt that was actually
# INGESTED just now. 45 days back — older than the 14d staleness window but
# still inside a generous 60d digest/mining lookback so the coarse --days /
# start_time cutoff (which still keys off `timestamp`) does not itself
# exclude it; only the ingested_at-aware staleness check is under test.
_FORGED_OLD_BODY_TS = _iso(_NOW - timedelta(days=45))


# ---------------------------------------------------------------------------
# weekly_digest.collect_metrics
# ---------------------------------------------------------------------------

class TestWeeklyDigestWindowing:
    def _run(self, records: list[dict], tmp_path: Path, days: int = 30) -> dict:
        import weekly_digest

        receipts_path = tmp_path / "t0_receipts.ndjson"
        receipts_path.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )
        with (
            patch.object(weekly_digest, "RECEIPTS_PATH", receipts_path),
            patch.object(weekly_digest, "DB_PATH", tmp_path / "nonexistent.db"),
            patch.object(weekly_digest, "PENDING_PATH", tmp_path / "nonexistent.json"),
        ):
            metrics = weekly_digest.collect_metrics(days=days)
        return metrics["dispatch_outcomes"]

    def test_frozen_batch_excluded_fresh_failure_counted(self, tmp_path):
        records = [
            {"status": "contract_invalid", "timestamp": _FROZEN_BATCH_TS}
            for _ in range(36)  # mirrors the measured seocrawler-v2 frozen batch size
        ] + [
            {"status": "failed", "timestamp": _FRESH_FAILURE_TS},
        ]
        # days=30 so the digest window alone would NOT exclude the 26-day batch —
        # only the dedicated 14-day contract_invalid staleness check does.
        out = self._run(records, tmp_path, days=30)
        assert out["total"] == 1
        assert out["failure"] == 1
        assert out["success"] == 0
        assert out["unknown"] == 0

    def test_fresh_contract_invalid_still_counts_as_failure(self, tmp_path):
        """A contract_invalid receipt inside the 14d window is still live."""
        records = [{"status": "contract_invalid", "timestamp": _FRESH_FAILURE_TS}]
        out = self._run(records, tmp_path, days=30)
        assert out["total"] == 1
        assert out["failure"] == 1

    def test_report_contract_invalid_event_type_also_windowed(self, tmp_path):
        """Frozen batches emitted via event_type (not status) are also excluded."""
        records = [
            {"event_type": "report_contract_invalid", "status": "contract_invalid",
             "timestamp": _FROZEN_BATCH_TS}
            for _ in range(10)
        ]
        out = self._run(records, tmp_path, days=30)
        assert out["total"] == 0
        assert out["failure"] == 0

    def test_fresh_ingested_at_beats_forged_old_body_timestamp(self, tmp_path):
        """T-adv3: a receipt ingested moments ago, whose report body forged
        an old `timestamp`, still counts as a live failure — the staleness
        check windows on ingested_at, not the worker-suppliable timestamp."""
        records = [{
            "status": "contract_invalid",
            "timestamp": _FORGED_OLD_BODY_TS,
            "ingested_at": _FRESH_FAILURE_TS,
        }]
        out = self._run(records, tmp_path, days=60)
        assert out["total"] == 1
        assert out["failure"] == 1

    def test_no_parseable_timestamp_at_all_fails_open_and_counts(self, tmp_path):
        """T-adv4: a contract_invalid receipt with NEITHER ingested_at NOR
        timestamp still counts — consistent fail-open (weekly_digest already
        did this; this locks it in against regression)."""
        records = [{"status": "contract_invalid"}]
        out = self._run(records, tmp_path, days=30)
        assert out["total"] == 1
        assert out["failure"] == 1

    def test_t_adv8_fresh_ingested_at_beats_forged_old_timestamp_narrow_days(self, tmp_path):
        """T-adv8 (fix-r2, Finding 3 HIGH): with a NARROW --days window, the
        generic worker-`timestamp` window filter used to run before the
        dedicated contract_invalid staleness check ever saw the record — a
        forged old body `timestamp` (45d) outside a 7-day digest window
        dropped the record even though its processor-stamped ingested_at was
        fresh. Windowing for contract_invalid records must key off
        ingested_at exclusively, for both the --days cutoff and the
        dedicated staleness check."""
        records = [{
            "status": "contract_invalid",
            "timestamp": _FORGED_OLD_BODY_TS,
            "ingested_at": _FRESH_FAILURE_TS,
        }]
        out = self._run(records, tmp_path, days=7)
        assert out["total"] == 1
        assert out["failure"] == 1


# ---------------------------------------------------------------------------
# learning_loop.LearningLoop.extract_failure_patterns
# ---------------------------------------------------------------------------

class _FakePaths:
    def __init__(self, state_dir: Path, vnx_home: Path):
        self._d = {
            "VNX_STATE_DIR": str(state_dir),
            "VNX_HOME": str(vnx_home),
            "VNX_DATA_DIR": str(state_dir.parent),
        }

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


class TestLearningLoopWindowing:
    @pytest.fixture
    def loop_env(self, tmp_path):
        import learning_loop as ll

        state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
        state_dir.mkdir(parents=True)
        vnx_home = tmp_path / "repo"
        vnx_home.mkdir()

        fake = _FakePaths(state_dir, vnx_home)
        with patch.object(ll, "ensure_env", return_value=fake):
            loop = ll.LearningLoop()
            yield loop, state_dir
        try:
            loop.conn.close()
        except Exception:
            pass

    def test_frozen_batch_excluded_fresh_failure_included(self, loop_env):
        loop, state_dir = loop_env
        records = [
            {"status": "contract_invalid", "terminal": "T1", "dispatch_id": f"d-old-{i}",
             "timestamp": _FROZEN_BATCH_TS}
            for i in range(20)
        ] + [
            {"status": "failed", "failure_reason": "Exhausted 3 retries", "terminal": "T1",
             "provider": "claude", "dispatch_id": "d-fresh", "timestamp": _FRESH_FAILURE_TS},
        ]
        (state_dir / "t0_receipts.ndjson").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

        # start_time reaches back 60 days — wide enough that, without the
        # dedicated contract_invalid staleness check, the frozen batch would
        # still be inside the window and get mined.
        failures = loop.extract_failure_patterns(
            start_time=datetime.now(timezone.utc) - timedelta(days=60)
        )

        assert len(failures) == 1
        assert failures[0]["error"] == "Exhausted 3 retries"

    def test_fresh_contract_invalid_still_mined(self, loop_env):
        loop, state_dir = loop_env
        records = [
            {"status": "contract_invalid", "terminal": "T1", "provider": "claude",
             "dispatch_id": "d-fresh-ci", "contract_violations": ["## Changes"],
             "timestamp": _FRESH_FAILURE_TS},
        ]
        (state_dir / "t0_receipts.ndjson").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

        failures = loop.extract_failure_patterns(
            start_time=datetime.now(timezone.utc) - timedelta(days=2)
        )
        assert len(failures) == 1

    def test_fresh_ingested_at_beats_forged_old_body_timestamp(self, loop_env):
        """T-adv3: ingested_at (fresh) wins over a forged old report-body
        timestamp for the contract_invalid staleness check."""
        loop, state_dir = loop_env
        records = [{
            "status": "contract_invalid", "terminal": "T1", "provider": "claude",
            "dispatch_id": "d-adv3-forged-old-ts", "contract_violations": ["## Changes"],
            "timestamp": _FORGED_OLD_BODY_TS, "ingested_at": _FRESH_FAILURE_TS,
        }]
        (state_dir / "t0_receipts.ndjson").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

        failures = loop.extract_failure_patterns(
            start_time=datetime.now(timezone.utc) - timedelta(days=60)
        )
        assert len(failures) == 1

    def test_no_parseable_timestamp_at_all_fails_open_and_still_mined(self, loop_env):
        """T-adv4: a contract_invalid receipt with NO timestamp field at all
        (missing, not merely unparseable) still gets mined. Previously the
        is_stale_contract_invalid() fail-open (missing timestamp -> not
        stale, not skipped) was contradicted by a SECOND `ts_dt is None`
        drop a few lines later — this locks in the fix so both checks agree."""
        loop, state_dir = loop_env
        records = [{
            "status": "contract_invalid", "terminal": "T1", "provider": "claude",
            "dispatch_id": "d-adv4-no-ts", "contract_violations": ["## Changes"],
        }]
        (state_dir / "t0_receipts.ndjson").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

        failures = loop.extract_failure_patterns(
            start_time=datetime.now(timezone.utc) - timedelta(days=2)
        )
        assert len(failures) == 1

    def test_t_adv8_fresh_ingested_at_beats_forged_old_timestamp_narrow_start(self, loop_env):
        """T-adv8 (fix-r2, Finding 3 HIGH): a SECOND generic window filter on
        the worker-suppliable `timestamp` ran after the dedicated staleness
        check and could still drop a freshly-ingested contract_invalid
        receipt whose report body forged an old `timestamp` (45d) outside a
        narrow start_time (2d). Both window decisions for contract_invalid
        records must key off ingested_at exclusively."""
        loop, state_dir = loop_env
        records = [{
            "status": "contract_invalid", "terminal": "T1", "provider": "claude",
            "dispatch_id": "d-adv8-forged-old-ts", "contract_violations": ["## Changes"],
            "timestamp": _FORGED_OLD_BODY_TS, "ingested_at": _FRESH_FAILURE_TS,
        }]
        (state_dir / "t0_receipts.ndjson").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

        failures = loop.extract_failure_patterns(
            start_time=datetime.now(timezone.utc) - timedelta(days=2)
        )
        assert len(failures) == 1


# ---------------------------------------------------------------------------
# check_active_drain.build_receipt_status_index
# ---------------------------------------------------------------------------

class TestCheckActiveDrainWindowing:
    def test_stale_contract_invalid_receipt_excluded_from_index(self, tmp_path):
        from check_active_drain import build_receipt_status_index

        receipts_processed = tmp_path / "receipts" / "processed"
        receipts_processed.mkdir(parents=True)

        did = "20260610-stale-contract-invalid"
        (receipts_processed / f"receipt-{did}.json").write_text(
            json.dumps({"dispatch_id": did, "status": "contract_invalid",
                        "timestamp": _FROZEN_BATCH_TS}),
            encoding="utf-8",
        )

        idx = build_receipt_status_index(tmp_path / "receipts")
        assert did not in idx

    def test_fresh_contract_invalid_receipt_still_indexed_as_failure(self, tmp_path):
        from check_active_drain import build_receipt_status_index

        receipts_processed = tmp_path / "receipts" / "processed"
        receipts_processed.mkdir(parents=True)

        did = "20260716-fresh-contract-invalid"
        (receipts_processed / f"receipt-{did}.json").write_text(
            json.dumps({"dispatch_id": did, "status": "contract_invalid",
                        "timestamp": _FRESH_FAILURE_TS}),
            encoding="utf-8",
        )

        idx = build_receipt_status_index(tmp_path / "receipts")
        assert idx[did] == "failure"

    def test_missing_timestamp_still_indexed_as_failure(self, tmp_path):
        """Regression guard: a receipt with NO timestamp field (pre-existing
        real-world shape) must still classify as failure — fail-open, not
        fail-closed, on missing dating information. (T-adv4: check_active_drain
        already had this right via is_stale_contract_invalid(None)==False;
        this test locks it in against regression.)"""
        from check_active_drain import build_receipt_status_index

        receipts_processed = tmp_path / "receipts" / "processed"
        receipts_processed.mkdir(parents=True)

        did = "20260610-no-timestamp-field"
        (receipts_processed / f"receipt-{did}.json").write_text(
            json.dumps({"dispatch_id": did, "status": "contract_invalid"}),
            encoding="utf-8",
        )

        idx = build_receipt_status_index(tmp_path / "receipts")
        assert idx[did] == "failure"

    def test_fresh_ingested_at_beats_forged_old_body_timestamp(self, tmp_path):
        """T-adv3: ingested_at (fresh) wins over a forged old report-body
        timestamp for the contract_invalid staleness check."""
        from check_active_drain import build_receipt_status_index

        receipts_processed = tmp_path / "receipts" / "processed"
        receipts_processed.mkdir(parents=True)

        did = "20260716-t-adv3-forged-old-body-ts"
        (receipts_processed / f"receipt-{did}.json").write_text(
            json.dumps({"dispatch_id": did, "status": "contract_invalid",
                        "timestamp": _FORGED_OLD_BODY_TS, "ingested_at": _FRESH_FAILURE_TS}),
            encoding="utf-8",
        )

        idx = build_receipt_status_index(tmp_path / "receipts")
        assert idx[did] == "failure"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
