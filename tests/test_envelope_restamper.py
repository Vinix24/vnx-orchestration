"""Tests for migrate_phase3_envelope.py (Phase 6 P3 re-stamper).

Covers:
- Dry-run: counts lines without writing
- Envelope fields populated on old-format lines
- Existing envelope fields preserved (idempotent)
- Malformed JSON lines pass through unchanged
- fcntl locking: concurrent append during restamp does not lose data
- restamp_project dispatches to both primary and central dirs
"""

from __future__ import annotations

import json
import sys
import os
import threading
import time
from pathlib import Path
from typing import List

import pytest

# Add scripts to path so migrate_phase3_envelope can be imported.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import migrate_phase3_envelope as migrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_ndjson(path: Path, records: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _read_ndjson(path: Path) -> List[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


ENVELOPE = {
    "operator_id": "op-test",
    "project_id": "test-proj",
    "orchestrator_id": "orch-1",
    "agent_id": None,
}


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_returns_correct_count(self, tmp_path):
        ndjson = tmp_path / "dispatch_register.ndjson"
        _write_ndjson(ndjson, [
            {"timestamp": "2026-01-01T00:00:00Z", "event": "dispatch_created"},
            {"timestamp": "2026-01-02T00:00:00Z", "event": "dispatch_promoted"},
        ])
        count = migrator._restamp_ndjson_inplace(ndjson, ENVELOPE, dry_run=True)
        assert count == 2

    def test_dry_run_does_not_modify_file(self, tmp_path):
        ndjson = tmp_path / "test.ndjson"
        original = [{"event": "dispatch_created", "dispatch_id": "d-1"}]
        _write_ndjson(ndjson, original)
        original_mtime = ndjson.stat().st_mtime

        migrator._restamp_ndjson_inplace(ndjson, ENVELOPE, dry_run=True)

        assert ndjson.stat().st_mtime == original_mtime, "dry-run must not modify file"
        records = _read_ndjson(ndjson)
        assert records == original

    def test_dry_run_returns_zero_for_absent_file(self, tmp_path):
        ndjson = tmp_path / "absent.ndjson"
        count = migrator._restamp_ndjson_inplace(ndjson, ENVELOPE, dry_run=True)
        assert count == 0


# ---------------------------------------------------------------------------
# Envelope stamping
# ---------------------------------------------------------------------------

class TestEnvelopeStamping:
    def test_old_format_gets_envelope_fields(self, tmp_path):
        ndjson = tmp_path / "t0_receipts.ndjson"
        _write_ndjson(ndjson, [{"event_type": "task_complete", "dispatch_id": "d-1"}])

        migrator._restamp_ndjson_inplace(ndjson, ENVELOPE)

        records = _read_ndjson(ndjson)
        assert records[0]["operator_id"] == "op-test"
        assert records[0]["project_id"] == "test-proj"
        assert records[0]["orchestrator_id"] == "orch-1"

    def test_existing_envelope_fields_not_overwritten(self, tmp_path):
        ndjson = tmp_path / "existing.ndjson"
        _write_ndjson(ndjson, [{
            "event_type": "task_complete",
            "project_id": "original-proj",
            "operator_id": "original-op",
        }])

        migrator._restamp_ndjson_inplace(ndjson, ENVELOPE)

        records = _read_ndjson(ndjson)
        assert records[0]["project_id"] == "original-proj", "existing project_id must not be overwritten"
        assert records[0]["operator_id"] == "original-op", "existing operator_id must not be overwritten"

    def test_idempotent_double_run(self, tmp_path):
        ndjson = tmp_path / "idem.ndjson"
        _write_ndjson(ndjson, [{"event": "dispatch_created", "dispatch_id": "d-idem"}])

        migrator._restamp_ndjson_inplace(ndjson, ENVELOPE)
        first_result = _read_ndjson(ndjson)

        migrator._restamp_ndjson_inplace(ndjson, ENVELOPE)
        second_result = _read_ndjson(ndjson)

        assert first_result == second_result, "second run must produce identical output"

    def test_malformed_json_lines_pass_through(self, tmp_path):
        ndjson = tmp_path / "bad.ndjson"
        ndjson.write_text(
            '{"event":"dispatch_created","dispatch_id":"d-1"}\n'
            "NOT_JSON\n"
            '{"event":"dispatch_promoted","dispatch_id":"d-2"}\n',
            encoding="utf-8",
        )

        migrator._restamp_ndjson_inplace(ndjson, ENVELOPE)

        lines = ndjson.read_text().splitlines()
        assert len(lines) == 3
        assert lines[1] == "NOT_JSON", "malformed line must be preserved"

    def test_count_reflects_parseable_lines(self, tmp_path):
        ndjson = tmp_path / "mixed.ndjson"
        ndjson.write_text(
            '{"event":"e1","dispatch_id":"d-1"}\n'
            "BROKEN\n"
            '{"event":"e2","dispatch_id":"d-2"}\n',
            encoding="utf-8",
        )
        count = migrator._restamp_ndjson_inplace(ndjson, ENVELOPE)
        assert count == 2


# ---------------------------------------------------------------------------
# flock concurrency: concurrent append during restamp does not lose data
# ---------------------------------------------------------------------------

class TestFlockConcurrency:
    def test_concurrent_append_does_not_lose_records(self, tmp_path):
        """Simulate a concurrent appender that writes while the stamper holds LOCK_EX.

        The appender must block until the stamper releases the lock. After both
        complete, the file must contain both the stamped records AND the newly
        appended record.
        """
        ndjson = tmp_path / "concurrent.ndjson"
        _write_ndjson(ndjson, [{"event": "dispatch_created", "dispatch_id": "d-pre"}])

        appended_result = []
        appender_started = threading.Event()
        stamper_can_proceed = threading.Event()

        def concurrent_appender():
            """Tries to append a record — will block until stamper releases the lock."""
            appender_started.set()
            # Small sleep to give stamper time to acquire the lock first.
            time.sleep(0.05)
            import fcntl as _fcntl
            with ndjson.open("a", encoding="utf-8") as fh:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
                fh.write(json.dumps({"event": "dispatch_promoted",
                                     "dispatch_id": "d-appended"}) + "\n")
            appended_result.append(True)

        appender_thread = threading.Thread(target=concurrent_appender)
        appender_thread.start()

        # Give appender a moment to try to acquire lock.
        appender_started.wait(timeout=2)
        time.sleep(0.01)

        # Stamper runs — holds lock during atomic rename.
        migrator._restamp_ndjson_inplace(ndjson, ENVELOPE)

        appender_thread.join(timeout=5)

        assert appended_result, "concurrent appender never completed"

        records = _read_ndjson(ndjson)
        dispatch_ids = {r.get("dispatch_id") for r in records}

        # The stamped record must be present.
        assert "d-pre" in dispatch_ids, "stamped record missing"
        # The appended record must also be present (not lost by the rename).
        assert "d-appended" in dispatch_ids, "concurrent append was lost"


# ---------------------------------------------------------------------------
# restamp_project integration
# ---------------------------------------------------------------------------

class TestRestampProject:
    def test_restamp_project_dry_run(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        _write_ndjson(state_dir / "dispatch_register.ndjson",
                      [{"event": "dispatch_created", "dispatch_id": "d-1"},
                       {"event": "dispatch_promoted", "dispatch_id": "d-2"}])
        _write_ndjson(state_dir / "t0_receipts.ndjson",
                      [{"event_type": "task_complete", "dispatch_id": "d-1"}])

        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")

        from unittest.mock import patch
        with patch.object(migrator, "resolve_central_data_dir", side_effect=Exception("no central")):
            results = migrator.restamp_project(state_dir, "test-proj", also_central=False, dry_run=True)

        assert results["dispatch_register.ndjson"] == 2
        assert results["t0_receipts.ndjson"] == 1

    def test_restamp_project_live_stamps_envelope(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        _write_ndjson(state_dir / "dispatch_register.ndjson",
                      [{"event": "dispatch_created", "dispatch_id": "d-live"}])
        _write_ndjson(state_dir / "t0_receipts.ndjson", [])

        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        monkeypatch.setenv("VNX_OPERATOR_ID", "test-op")

        from unittest.mock import patch
        with patch.object(migrator, "resolve_central_data_dir", side_effect=Exception("no central")):
            migrator.restamp_project(state_dir, "test-proj", also_central=False)

        records = _read_ndjson(state_dir / "dispatch_register.ndjson")
        assert records[0]["project_id"] == "test-proj"
        assert records[0]["operator_id"] == "test-op"

    def test_restamp_project_also_stamps_central(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "primary" / "state"
        state_dir.mkdir(parents=True)
        _write_ndjson(state_dir / "dispatch_register.ndjson",
                      [{"event": "dispatch_created", "dispatch_id": "d-central"}])
        _write_ndjson(state_dir / "t0_receipts.ndjson", [])

        central_base = tmp_path / "central"
        central_state = central_base / "test-proj" / "state"
        central_state.mkdir(parents=True)
        _write_ndjson(central_state / "dispatch_register.ndjson",
                      [{"event": "dispatch_created", "dispatch_id": "d-central"}])
        _write_ndjson(central_state / "t0_receipts.ndjson", [])

        monkeypatch.setenv("VNX_OPERATOR_ID", "test-op")

        from unittest.mock import patch
        with patch.object(migrator, "resolve_central_data_dir",
                          return_value=central_base / "test-proj"):
            migrator.restamp_project(state_dir, "test-proj", also_central=True)

        central_records = _read_ndjson(central_state / "dispatch_register.ndjson")
        assert central_records[0]["project_id"] == "test-proj"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

class TestCli:
    def test_dry_run_cli_exits_zero(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        _write_ndjson(state_dir / "dispatch_register.ndjson",
                      [{"event": "dispatch_created", "dispatch_id": "d-cli"}])
        _write_ndjson(state_dir / "t0_receipts.ndjson", [])

        from unittest.mock import patch
        with patch.object(migrator, "resolve_central_data_dir", side_effect=Exception("no central")):
            rc = migrator.main([
                "--project-id", "test-proj",
                "--state-dir", str(state_dir),
                "--dry-run",
            ])
        assert rc == 0

    def test_live_cli_exits_zero(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        _write_ndjson(state_dir / "dispatch_register.ndjson", [])
        _write_ndjson(state_dir / "t0_receipts.ndjson", [])

        from unittest.mock import patch
        with patch.object(migrator, "resolve_central_data_dir", side_effect=Exception("no central")):
            rc = migrator.main([
                "--project-id", "test-proj",
                "--state-dir", str(state_dir),
            ])
        assert rc == 0


# ---------------------------------------------------------------------------
# Sentinel lock race (BLOCKING 2)
# ---------------------------------------------------------------------------

class TestSentinelLockRace:
    """Sentinel lock must coordinate re-stamper and dispatch_register appenders
    so that appenders always write to the current inode — never to an unlinked
    old inode that the re-stamper replaced via os.replace().
    """

    def test_100_concurrent_appends_vs_restamper_no_data_loss(self, tmp_path):
        """Race 100 dispatch_register writers against the re-stamper.

        All 100 appended events must appear in the final NDJSON after
        _restamp_ndjson_inplace completes.  With the sentinel fix both
        parties acquire .state.lock before touching the file, ensuring
        appenders that acquire the sentinel after the rename always open
        the new inode.
        """
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        ndjson = state_dir / "dispatch_register.ndjson"
        ndjson.write_text(
            json.dumps({"event": "dispatch_created", "dispatch_id": "d-0"}) + "\n",
            encoding="utf-8",
        )

        # Import the appender that uses the sentinel after the fix.
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
        from dispatch_register import _write_event_locked

        APPENDERS = 100
        errors: List[Exception] = []

        def do_append(i: int) -> None:
            try:
                _write_event_locked(ndjson, {
                    "event": "dispatch_completed",
                    "dispatch_id": f"d-{i}",
                })
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=do_append, args=(i,))
            for i in range(1, APPENDERS + 1)
        ]
        for t in threads:
            t.start()

        # Re-stamper runs concurrently with the 100 appenders.
        migrator._restamp_ndjson_inplace(ndjson, ENVELOPE)

        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Appender errors: {errors[:3]!r}"

        records = _read_ndjson(ndjson)
        dispatch_ids = {r.get("dispatch_id") for r in records}
        missing = [f"d-{i}" for i in range(1, APPENDERS + 1) if f"d-{i}" not in dispatch_ids]
        assert not missing, (
            f"Lost {len(missing)}/{APPENDERS} events to rename race: {missing[:5]!r}"
        )
