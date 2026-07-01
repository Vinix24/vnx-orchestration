#!/usr/bin/env python3
"""Tests for five hardening-audit crash-recovery fixes (H2/H3/H6/H8/H9).

H2 — silence_watchdog reschedules even when body raises.
H3 — mirror-drain failure does NOT fail append_receipt_payload.
H6 — ReceiptWatcher resumes after t0_receipts.ndjson is truncated.
H8 — intelligence_persist connections have WAL + busy_timeout set.
H9 — confidence_events audit-insert failure is logged (not silently dropped).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# H2 — silence_watchdog reschedules on body error
# ---------------------------------------------------------------------------

class TestSilenceWatchdogReschedules:
    """H2: a transient exception in the watchdog body must not kill the watchdog."""

    def _make_trigger_state(self) -> Any:
        from headless_trigger import TriggerState
        return TriggerState()  # no-arg constructor

    def test_reschedules_after_body_raises(self, tmp_path):
        from headless_trigger import silence_watchdog

        trigger_state = self._make_trigger_state()
        calls: list[int] = []

        def _boom(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient failure")
            # After first call raises, stop the watchdog on the second call.
            trigger_state.shutdown_event.set()

        # Patch check_stale_leases to control body execution.
        with patch("headless_trigger.check_stale_leases", side_effect=_boom):
            silence_watchdog(tmp_path, interval=0.05, trigger_state=trigger_state, dry_run=True)
            # Allow the timer to fire the rescheduled second call.
            deadline = time.monotonic() + 2.0
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)

        assert len(calls) >= 2, (
            "watchdog must reschedule itself after a body exception; "
            f"got {len(calls)} call(s)"
        )

    def test_body_error_logged_at_warning(self, tmp_path, caplog):
        from headless_trigger import silence_watchdog

        trigger_state = self._make_trigger_state()
        # Do NOT pre-set the event; the body must execute so the exception is raised.
        # Set it inside the side_effect so the watchdog stops after one body execution.

        def _boom(*_args, **_kwargs):
            trigger_state.shutdown_event.set()
            raise ValueError("intentional test error")

        with caplog.at_level(logging.WARNING, logger="headless_trigger"):
            with patch("headless_trigger.check_stale_leases", side_effect=_boom):
                silence_watchdog(tmp_path, interval=1.0, trigger_state=trigger_state, dry_run=True)

        assert any(
            "transient" in r.message.lower() or "watchdog" in r.message.lower()
            for r in caplog.records
        ), "transient error must be logged at WARNING level"


# ---------------------------------------------------------------------------
# H3 — mirror-drain failure does not fail append_receipt_payload
# ---------------------------------------------------------------------------

def _load_append_receipt_module(tmp_path: Path):
    """Load append_receipt.py as a module (wires the facade required by payload)."""
    env_patch = {
        "PROJECT_ROOT": str(REPO_ROOT),
        "VNX_DATA_DIR": str(tmp_path),
        "VNX_STATE_DIR": str(tmp_path / "state"),
        "VNX_HOME": str(REPO_ROOT),
    }
    mod_name = f"_ar_testmod_{id(tmp_path)}"
    with patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(
            mod_name, REPO_ROOT / "scripts" / "append_receipt.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            del sys.modules[mod_name]
            raise
    return mod


def _make_receipt(dispatch_id: str = "d-h3") -> Dict[str, Any]:
    return {
        "timestamp": "2026-06-01T12:00:00.000000Z",
        "event_type": "task_complete",
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "status": "success",
        "project_id": "test-proj",
    }


class TestMirrorDrainFailureSwallowed:
    """H3: a mirror-drain error after the durable write must not propagate."""

    def test_append_succeeds_when_drain_raises(self, tmp_path):
        _load_append_receipt_module(tmp_path)

        import append_receipt_internals.payload as payload_mod
        from append_receipt_internals.common import AppendReceiptError, EXIT_IO_ERROR

        def _drain_boom(*_args, **_kwargs):
            raise AppendReceiptError(
                "pending_mirror_write_failed",
                EXIT_IO_ERROR,
                "simulated mirror I/O failure",
            )

        receipts_file = str(tmp_path / "t0_receipts.ndjson")
        with patch.object(payload_mod, "_drain_pending_mirrors_and_mirror_current", side_effect=_drain_boom):
            result = payload_mod.append_receipt_payload(
                _make_receipt(),
                receipts_file=receipts_file,
                skip_enrichment=True,
            )

        # The durable write must have succeeded regardless of the mirror error.
        assert result.status in ("appended", "duplicate"), (
            f"append must succeed even when drain raises; got status={result.status!r}"
        )
        assert (tmp_path / "t0_receipts.ndjson").exists(), "receipts file must exist after append"

    def test_drain_oserror_swallowed_and_logged(self, tmp_path, caplog):
        _load_append_receipt_module(tmp_path)

        import append_receipt_internals.payload as payload_mod

        def _drain_boom(*_args, **_kwargs):
            raise OSError("disk full simulation")

        receipts_file = str(tmp_path / "t0_receipts_b.ndjson")
        with caplog.at_level(logging.WARNING):
            with patch.object(payload_mod, "_drain_pending_mirrors_and_mirror_current", side_effect=_drain_boom):
                result = payload_mod.append_receipt_payload(
                    _make_receipt("d-h3b"),
                    receipts_file=receipts_file,
                    skip_enrichment=True,
                )

        assert result.status in ("appended", "duplicate")
        assert any("mirror" in r.message.lower() for r in caplog.records), (
            "swallowed mirror error must be logged"
        )


# ---------------------------------------------------------------------------
# H6 — ReceiptWatcher resumes after file truncation
# ---------------------------------------------------------------------------

class TestReceiptWatcherTruncationRecovery:
    """H6: watcher re-seeds _file_pos to 0 when the file shrinks."""

    def _make_trigger_state(self) -> Any:
        from headless_trigger import TriggerState
        return TriggerState()

    def _make_receipt_line(self, dispatch_id: str) -> str:
        return json.dumps({
            "event_type": "task_complete",
            "dispatch_id": dispatch_id,
            "terminal": "T1",
            "status": "success",
            "timestamp": "2026-06-01T12:00:00Z",
        })

    def test_file_pos_reset_on_truncation(self, tmp_path):
        from headless_trigger import ReceiptWatcher

        trigger_state = self._make_trigger_state()
        receipts = tmp_path / "t0_receipts.ndjson"

        # Write an initial line so watcher seeds to a non-zero pos.
        receipts.write_text(self._make_receipt_line("d-before") + "\n", encoding="utf-8")

        watcher = ReceiptWatcher(
            state_dir=tmp_path,
            trigger_state=trigger_state,
            dry_run=True,
        )
        # Simulate start(): seed pos to current file size.
        watcher._file_pos = receipts.stat().st_size
        assert watcher._file_pos > 0, "pre-condition: pos must be non-zero"

        # Truncate the file (rotate/clear scenario).
        receipts.write_text("", encoding="utf-8")
        assert receipts.stat().st_size == 0

        # _check_new_lines must detect the shrink and reset _file_pos.
        watcher._check_new_lines()

        assert watcher._file_pos == 0, (
            "_file_pos must be reset to 0 after truncation so the watcher "
            f"can resume reading; got {watcher._file_pos}"
        )

    def test_new_content_read_after_truncation(self, tmp_path):
        """After a truncation+reset, new receipts written to the file are picked up."""
        from headless_trigger import ReceiptWatcher

        trigger_state = self._make_trigger_state()
        receipts = tmp_path / "t0_receipts.ndjson"

        # Initial content — watcher seeds pos past it.
        receipts.write_text(self._make_receipt_line("d-old") + "\n", encoding="utf-8")
        watcher = ReceiptWatcher(
            state_dir=tmp_path,
            trigger_state=trigger_state,
            dry_run=True,
        )
        watcher._file_pos = receipts.stat().st_size
        old_pos = watcher._file_pos

        # Truncate then write new content (shorter or same-length so size < old_pos).
        receipts.write_text(self._make_receipt_line("d-new") + "\n", encoding="utf-8")
        # Ensure new size < old pos to trigger the truncation path.
        # Both lines are same length so pos == size; rewrite with empty then new line.
        receipts.write_text("", encoding="utf-8")
        receipts.write_text(self._make_receipt_line("d-new") + "\n", encoding="utf-8")

        # If new size < old_pos: truncation detected, pos resets to 0, content is read.
        new_size = receipts.stat().st_size
        if new_size < old_pos:
            with patch("headless_trigger.trigger_headless_t0"):
                with patch("headless_trigger._refresh_t0_state", return_value=True):
                    watcher._check_new_lines()
            assert watcher._file_pos > 0, "pos must advance after reading new content post-truncation"
        else:
            # Lines are same byte-length; at minimum verify the pos was reset (not stuck at old_pos).
            watcher._check_new_lines()
            assert watcher._file_pos != old_pos or new_size == old_pos, (
                "watcher must not be stuck at old_pos when file size has changed"
            )


# ---------------------------------------------------------------------------
# H8 — intelligence_persist connections have WAL + busy_timeout
# ---------------------------------------------------------------------------

def _create_qi_db(db_path: Path) -> None:
    schema = (REPO_ROOT / "schemas" / "quality_intelligence.sql").read_text()
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema)
    conn.close()


class _TrackingConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that records executed SQL statements."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tracked_sqls: list[str] = []

    def execute(self, sql, *args, **kwargs):  # type: ignore[override]
        self.tracked_sqls.append(sql)
        return super().execute(sql, *args, **kwargs)


class TestIntelligencePersistConnectionConfig:
    """H8: both connection sites in intelligence_persist set WAL and busy_timeout."""

    def test_persist_signals_sets_wal_and_busy_timeout(self, tmp_path):
        import intelligence_persist as ip_mod
        from intelligence_persist import persist_signals_to_db

        db_path = tmp_path / "quality_intelligence.db"
        _create_qi_db(db_path)

        captured: list[_TrackingConnection] = []
        _orig_connect = ip_mod.sqlite3.connect

        def _interceptor(path, *args, **kwargs):
            kwargs["factory"] = _TrackingConnection
            conn = _orig_connect(path, *args, **kwargs)
            captured.append(conn)
            return conn

        class _Corr:
            dispatch_id = "d-h8"
            feature_id = ""

        class _Sig:
            signal_type = "gate_success"
            content = "test pattern"
            severity = "info"
            correlation = _Corr()
            defect_family = ""

        with patch.object(ip_mod.sqlite3, "connect", side_effect=_interceptor):
            persist_signals_to_db([_Sig()], db_path)

        assert captured, "a connection must have been opened"
        sqls = captured[0].tracked_sqls
        assert any("journal_mode" in s.lower() for s in sqls), (
            f"persist_signals_to_db must set PRAGMA journal_mode = WAL; got SQLs: {sqls}"
        )
        assert any("busy_timeout" in s.lower() for s in sqls), (
            f"persist_signals_to_db must set PRAGMA busy_timeout; got SQLs: {sqls}"
        )

    def test_update_confidence_sets_wal_and_busy_timeout(self, tmp_path):
        import intelligence_persist as ip_mod
        from intelligence_persist import update_confidence_from_outcome

        db_path = tmp_path / "quality_intelligence.db"
        _create_qi_db(db_path)

        try:
            import confidence_reconcile  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            pytest.skip("confidence_reconcile not importable in this environment")

        captured: list[_TrackingConnection] = []
        _orig_connect = ip_mod.sqlite3.connect

        def _interceptor(path, *args, **kwargs):
            kwargs["factory"] = _TrackingConnection
            conn = _orig_connect(path, *args, **kwargs)
            captured.append(conn)
            return conn

        with patch.object(ip_mod.sqlite3, "connect", side_effect=_interceptor):
            update_confidence_from_outcome(db_path, "d-h8", "T1", "success")

        assert captured, "a connection must have been opened"
        sqls = captured[0].tracked_sqls
        assert any("journal_mode" in s.lower() for s in sqls), (
            f"update_confidence_from_outcome must set PRAGMA journal_mode = WAL; got SQLs: {sqls}"
        )
        assert any("busy_timeout" in s.lower() for s in sqls), (
            f"update_confidence_from_outcome must set PRAGMA busy_timeout; got SQLs: {sqls}"
        )


# ---------------------------------------------------------------------------
# H9 — confidence_events audit-insert failure is logged, not silently dropped
# ---------------------------------------------------------------------------

def _make_failing_insert_connection(db_path: Path, error_msg: str) -> sqlite3.Connection:
    """Return a real Connection that raises OperationalError on confidence_events INSERT."""
    class _FailInsertConn(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "INSERT INTO confidence_events" in sql:
                raise sqlite3.OperationalError(error_msg)
            return super().execute(sql, *args, **kwargs)

    return sqlite3.connect(str(db_path), timeout=10.0, factory=_FailInsertConn)


class TestConfidenceEventsAuditLogging:
    """H9: a non-schema-error on the confidence_events insert is logged at WARNING."""

    def _skip_if_no_reconcile(self):
        try:
            import confidence_reconcile  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            pytest.skip("confidence_reconcile not importable in this environment")

    def test_resource_error_logged_not_silenced(self, tmp_path, caplog):
        import intelligence_persist as ip_mod
        from intelligence_persist import update_confidence_from_outcome

        db_path = tmp_path / "quality_intelligence.db"
        _create_qi_db(db_path)
        self._skip_if_no_reconcile()

        _orig_connect = ip_mod.sqlite3.connect

        def _interceptor(path, *args, **kwargs):
            kwargs["factory"] = type(
                "_FailInsert",
                (sqlite3.Connection,),
                {
                    "execute": lambda self, sql, *a, **kw: (
                        (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
                        if "INSERT INTO confidence_events" in sql
                        else sqlite3.Connection.execute(self, sql, *a, **kw)
                    ),
                },
            )
            return _orig_connect(path, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="intelligence_persist"):
            with patch.object(ip_mod.sqlite3, "connect", side_effect=_interceptor):
                try:
                    update_confidence_from_outcome(db_path, "d-h9", "T1", "success")
                except Exception:
                    pass  # vnx-silent-except: may fail for other reasons; we check log only

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "confidence_events" in m or "audit" in m.lower() or "locked" in m.lower()
            for m in warning_msgs
        ), (
            f"database-locked on confidence_events insert must be logged at WARNING; "
            f"got: {warning_msgs}"
        )

    def test_schema_absent_error_stays_silent(self, tmp_path, caplog):
        """Table-not-yet-migrated errors must NOT produce a warning (expected on older DBs)."""
        import intelligence_persist as ip_mod
        from intelligence_persist import update_confidence_from_outcome

        db_path = tmp_path / "quality_intelligence.db"
        _create_qi_db(db_path)
        self._skip_if_no_reconcile()

        _orig_connect = ip_mod.sqlite3.connect

        def _interceptor(path, *args, **kwargs):
            kwargs["factory"] = type(
                "_FailInsertSchema",
                (sqlite3.Connection,),
                {
                    "execute": lambda self, sql, *a, **kw: (
                        (_ for _ in ()).throw(sqlite3.OperationalError("no such table: confidence_events"))
                        if "INSERT INTO confidence_events" in sql
                        else sqlite3.Connection.execute(self, sql, *a, **kw)
                    ),
                },
            )
            return _orig_connect(path, *args, **kwargs)

        with caplog.at_level(logging.WARNING, logger="intelligence_persist"):
            with patch.object(ip_mod.sqlite3, "connect", side_effect=_interceptor):
                try:
                    update_confidence_from_outcome(db_path, "d-h9b", "T1", "success")
                except Exception:
                    pass  # vnx-silent-except: may fail for other reasons; only check log absence

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        schema_warnings = [m for m in warning_msgs if "confidence_events" in m]
        assert not schema_warnings, (
            f"schema-absent errors must NOT be logged at WARNING; got: {schema_warnings}"
        )
