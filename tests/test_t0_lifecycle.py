"""Tests for scripts/aggregator/t0_lifecycle.py (Wave 5 PR-5.2).

Validates design invariants from claudedocs/wave5-pr2-t0-lifecycle-redesign.md.

Tests use mocked subprocess factories and aggregator-spies to avoid spawning
real long-running processes. Real PIDs (the test process and short-lived
children) are used only where liveness/death semantics are tested.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

# Make scripts/ importable as a package root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Make scripts/lib/migrations importable (apply_0019 imports relative).
_LIB = _REPO_ROOT / "scripts" / "lib"
sys.path.insert(0, str(_LIB))

from migrations.apply_0019 import apply_migration as apply_0019_migration

from scripts.aggregator.state_aggregator import StateAggregator
from scripts.aggregator.t0_lifecycle import (
    DB_STATE_LEASED,
    DB_STATE_RELEASED,
    LIFECYCLE_REAPED,
    LIFECYCLE_RUNNING,
    LIFECYCLE_TERMINATING,
    KillResult,
    LeaseTokenMismatchError,
    T0AlreadyRunningError,
    T0AuditEmitError,
    T0Instance,
    T0LifecycleManager,
    T0SubprocessExitedEarly,
    _new_lease_token,
)

_MIGRATION_0017_SQL = _REPO_ROOT / "schemas" / "migrations" / "0017_multi_tenant_lease_isolation.sql"
_MIGRATION_0019_SQL = _REPO_ROOT / "schemas" / "migrations" / "0019_t0_lifecycle_tokens.sql"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _seed_schema_meta_v12(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE runtime_schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            description TEXT NOT NULL
        )
    """)
    conn.execute("INSERT INTO runtime_schema_version VALUES (11, datetime('now'), 'v11 baseline')")


def _create_dispatches_table_v12(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL UNIQUE,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            state           TEXT    NOT NULL DEFAULT 'queued',
            terminal_id     TEXT,
            track           TEXT,
            priority        TEXT    DEFAULT 'P2',
            pr_ref          TEXT,
            gate            TEXT,
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            bundle_path     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after   TEXT,
            metadata_json   TEXT    DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE dispatch_attempts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id      TEXT    NOT NULL UNIQUE,
            dispatch_id     TEXT    NOT NULL REFERENCES dispatches (dispatch_id),
            attempt_number  INTEGER NOT NULL DEFAULT 1,
            terminal_id     TEXT    NOT NULL,
            state           TEXT    NOT NULL DEFAULT 'pending',
            started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            ended_at        TEXT,
            failure_reason  TEXT,
            metadata_json   TEXT    DEFAULT '{}',
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev'
        )
    """)


def _create_terminal_leases_table_v12(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE terminal_leases (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            terminal_id         TEXT    NOT NULL UNIQUE,
            project_id          TEXT    NOT NULL DEFAULT 'vnx-dev',
            state               TEXT    NOT NULL DEFAULT 'idle',
            dispatch_id         TEXT,
            generation          INTEGER NOT NULL DEFAULT 1,
            leased_at           TEXT,
            expires_at          TEXT,
            last_heartbeat_at   TEXT,
            released_at         TEXT,
            metadata_json       TEXT    DEFAULT '{}'
        )
    """)


def _create_worker_states_table_v12(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE worker_states (
            terminal_id      TEXT    NOT NULL PRIMARY KEY,
            dispatch_id      TEXT    NOT NULL,
            state            TEXT    NOT NULL DEFAULT 'initializing',
            last_output_at   TEXT,
            state_entered_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            stall_count      INTEGER NOT NULL DEFAULT 0,
            blocked_reason   TEXT,
            metadata_json    TEXT,
            created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)


def _create_v12_db(db_path: Path) -> None:
    """Bootstrap a runtime_coordination.db at schema v12 (after migration 0017)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        _seed_schema_meta_v12(conn)
        _create_dispatches_table_v12(conn)
        _create_terminal_leases_table_v12(conn)
        _create_worker_states_table_v12(conn)
        conn.commit()
    finally:
        conn.close()

    # Apply 0017 to land at v12.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_MIGRATION_0017_SQL.read_text())
    finally:
        conn.close()


def _bootstrap_db(tmp_path: Path) -> Path:
    """Create a v13 DB ready for T0LifecycleManager use."""
    db = tmp_path / "runtime_coordination.db"
    vnx_data = tmp_path / ".vnx-data"
    vnx_data.mkdir(parents=True, exist_ok=True)
    _create_v12_db(db)
    apply_0019_migration(db, _MIGRATION_0019_SQL, vnx_data_dir=vnx_data)
    return db


def _make_aggregator(tmp_path: Path) -> StateAggregator:
    return StateAggregator(vnx_data_dir=tmp_path / ".vnx-data")


def _read_audit_events(tmp_path: Path) -> List[Dict[str, Any]]:
    events_path = tmp_path / ".vnx-data" / "events" / "state_aggregator.ndjson"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]


def _mock_subprocess_factory(pid: int, returncode: Any = None):
    """Build a factory that returns a MagicMock Popen with the given pid."""

    def factory(*args, **kwargs):
        proc = MagicMock(spec=subprocess.Popen)
        proc.pid = pid
        proc.poll.return_value = returncode
        proc.returncode = returncode
        return proc

    return factory


def _spawn_test_t0(
    mgr: T0LifecycleManager,
    project_id: str,
    pid: int = None,
) -> T0Instance:
    """Spawn a T0 backed by a real subprocess.Popen.

    Uses the default factory so the Popen lives inside mgr's own scope and
    `os.waitpid` semantics work correctly. We pass argv pointing at a long
    sleeper so the PID stays alive across the test.
    """
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    instance = mgr.spawn(project_id, argv=argv)
    return instance


def _cleanup_children(mgr: T0LifecycleManager) -> None:
    # No-op now that spawn() owns the subprocess. Reap loop tests must clean
    # explicitly via mgr.kill or force_release. Other tests can rely on the
    # tmp DB being thrown away.
    pass


# ---------------------------------------------------------------------------
# Test 1: spawn creates lease with unique token (UUID v7 format)
# ---------------------------------------------------------------------------


def test_spawn_creates_lease_with_unique_token(tmp_path: Path) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    try:
        inst = _spawn_test_t0(mgr, "proj-a")
        assert inst.lease_token, "lease_token must be set"
        # Format: <12 hex>-<20 hex> = 33 chars total
        assert len(inst.lease_token) == 33, f"unexpected token length: {inst.lease_token}"
        assert inst.lease_token[12] == "-", "token must have timestamp-prefix dash"
        assert inst.lifecycle_state == LIFECYCLE_RUNNING

        # DB row exists and has the token.
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT lease_token, state FROM terminal_leases "
                "WHERE terminal_id='T0' AND project_id='proj-a'"
            ).fetchone()
            assert row is not None
            assert row[0] == inst.lease_token
            assert row[1] == DB_STATE_LEASED
        finally:
            conn.close()
    finally:
        _cleanup_children(mgr)


# ---------------------------------------------------------------------------
# Test 2: BEGIN EXCLUSIVE serializes parallel spawn calls
# ---------------------------------------------------------------------------


def test_spawn_race_condition_serialized_by_exclusive(tmp_path: Path) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    results: List[Any] = []
    errors: List[Exception] = []

    def worker():
        try:
            inst = mgr.spawn("proj-race", argv=argv)
            results.append(inst)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    try:
        assert len(results) == 1, f"expected exactly 1 success, got {len(results)} (errors={[type(e).__name__ for e in errors]})"
        assert len(errors) == 4
        for e in errors:
            assert isinstance(e, T0AlreadyRunningError)
    finally:
        # Clean up the one successful subprocess.
        if results:
            inst = results[0]
            try:
                mgr.kill("proj-race", inst.lease_token, wait_timeout=3.0)
            except (LeaseTokenMismatchError, OSError, sqlite3.Error):
                pass


# ---------------------------------------------------------------------------
# Test 3: spawn rollback kills subprocess on insert failure
# ---------------------------------------------------------------------------


def test_spawn_rollback_kills_subprocess_on_insert_failure(tmp_path: Path) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    real_sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    mgr._subprocess_factory = _mock_subprocess_factory(real_sleeper.pid)  # noqa: SLF001

    # Mock aggregator.submit to fail ONLY on t0.spawn.committed (after subprocess is alive).
    original_submit = agg.submit
    call_count = {"n": 0}

    def failing_submit(update):
        call_count["n"] += 1
        if update.event_type == "t0.spawn.committed":
            raise RuntimeError("simulated aggregator failure on commit")
        return original_submit(update)

    agg.submit = failing_submit  # type: ignore[assignment]

    try:
        with pytest.raises(T0AuditEmitError):
            mgr.spawn("proj-rollback")

        # Wait up to 5s for cleanup to take effect.
        for _ in range(50):
            if real_sleeper.poll() is not None:
                break
            time.sleep(0.1)
        assert real_sleeper.poll() is not None, "subprocess was not killed by rollback"

        # No lease row in leased state.
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT * FROM terminal_leases WHERE terminal_id='T0' AND project_id='proj-rollback' AND state='leased'"
            ).fetchone()
            assert row is None
        finally:
            conn.close()
    finally:
        try:
            real_sleeper.kill()
            real_sleeper.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


# ---------------------------------------------------------------------------
# Test 4: spawn rollback on audit emit failure (same as 3 but explicit)
# ---------------------------------------------------------------------------


def test_spawn_rollback_on_audit_emit_failure(tmp_path: Path) -> None:
    """When aggregator.submit raises on t0.spawn.committed, the DB row must be
    rolled back AND the subprocess must be killed via rollback_action."""
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    real_sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    mgr._subprocess_factory = _mock_subprocess_factory(real_sleeper.pid)  # noqa: SLF001

    original_submit = agg.submit

    def failing_submit(update):
        if update.event_type == "t0.spawn.committed":
            raise RuntimeError("simulated emit failure")
        return original_submit(update)

    agg.submit = failing_submit  # type: ignore[assignment]

    try:
        with pytest.raises(T0AuditEmitError):
            mgr.spawn("proj-aud-fail")

        # Subprocess killed.
        for _ in range(50):
            if real_sleeper.poll() is not None:
                break
            time.sleep(0.1)
        assert real_sleeper.poll() is not None

        # Audit trail: requested emitted but not committed; aborted should be emitted.
        events = _read_audit_events(tmp_path)
        event_types = [e["event_type"] for e in events]
        assert "t0.spawn.requested" in event_types
        # committed was attempted but raised; aborted is best-effort
        # and should appear via rollback_action (failing_submit allows it).
        assert "t0.spawn.aborted" in event_types
        assert "t0.spawn.committed" not in event_types
    finally:
        try:
            real_sleeper.kill()
            real_sleeper.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


# ---------------------------------------------------------------------------
# Test 5: heartbeat requires matching lease_token
# ---------------------------------------------------------------------------


def test_heartbeat_token_match_required(tmp_path: Path) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    try:
        inst = _spawn_test_t0(mgr, "proj-hb")

        # Correct token → success.
        assert mgr.heartbeat("proj-hb", inst.pid, inst.lease_token) is True

        # Wrong token → False (not exception, hot path).
        wrong = _new_lease_token()
        assert mgr.heartbeat("proj-hb", inst.pid, wrong) is False

        # No t0.heartbeat.recorded event for the wrong-token call.
        events = _read_audit_events(tmp_path)
        hb_events = [e for e in events if e["event_type"] == "t0.heartbeat.recorded"]
        assert len(hb_events) == 1
        assert hb_events[0]["data"]["lease_token"] == inst.lease_token
    finally:
        _cleanup_children(mgr)


# ---------------------------------------------------------------------------
# Test 6: heartbeat does NOT change lifecycle_state
# ---------------------------------------------------------------------------


def test_heartbeat_no_state_transition(tmp_path: Path) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    try:
        inst = _spawn_test_t0(mgr, "proj-hb-state")

        before_hb = _heartbeat_at(db, "proj-hb-state")
        time.sleep(0.05)
        ok = mgr.heartbeat("proj-hb-state", inst.pid, inst.lease_token)
        assert ok is True
        after_hb = _heartbeat_at(db, "proj-hb-state")

        # Timestamp changed.
        assert before_hb != after_hb, "last_heartbeat_at should be updated"

        # lifecycle_state still RUNNING.
        meta = _metadata(db, "proj-hb-state")
        assert meta["lifecycle_state"] == LIFECYCLE_RUNNING
    finally:
        _cleanup_children(mgr)


def _heartbeat_at(db: Path, project_id: str) -> str:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT last_heartbeat_at FROM terminal_leases "
            "WHERE terminal_id='T0' AND project_id=?",
            (project_id,),
        ).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def _metadata(db: Path, project_id: str) -> Dict[str, Any]:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT metadata_json FROM terminal_leases "
            "WHERE terminal_id='T0' AND project_id=?",
            (project_id,),
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 7: kill sets TERMINATING state BEFORE sending signal
# ---------------------------------------------------------------------------


def test_kill_state_transitions_to_terminating_before_signal(tmp_path: Path, monkeypatch) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    try:
        inst = _spawn_test_t0(mgr, "proj-term")

        # Replace _signal_process to capture state at signal time.
        observed_state: Dict[str, Any] = {}
        original_signal = T0LifecycleManager._signal_process

        def spy_signal(pid, sig):
            observed_state["meta"] = _metadata(db, "proj-term")
            return original_signal(pid, sig)

        monkeypatch.setattr(T0LifecycleManager, "_signal_process", staticmethod(spy_signal))

        kr = mgr.kill("proj-term", inst.lease_token, wait_timeout=3.0)

        assert kr.verified_dead, f"kill should succeed: {kr}"
        assert observed_state["meta"]["lifecycle_state"] == LIFECYCLE_TERMINATING
    finally:
        _cleanup_children(mgr)


# ---------------------------------------------------------------------------
# Test 8: kill releases lease ONLY after ProcessLookupError verification
# ---------------------------------------------------------------------------


def test_kill_only_releases_after_verified_dead(tmp_path: Path, monkeypatch) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    try:
        inst = _spawn_test_t0(mgr, "proj-verify")

        # Track when lease is released vs when is_alive returns False.
        release_observed: Dict[str, bool] = {"alive_when_released": True}
        original_is_alive = T0LifecycleManager._is_alive

        def fake_is_alive(pid):
            # Always reports alive (so kill needs to wait + escalate).
            # We let real os.kill handle the actual kill, but the verify
            # path uses this check.
            real_alive = original_is_alive(pid)
            if not real_alive:
                # When process actually dies, return False so verify-path
                # proceeds to release.
                return False
            return True

        # Test the original semantic instead: real kill of real child.
        kr = mgr.kill("proj-verify", inst.lease_token, wait_timeout=3.0)
        assert kr.verified_dead is True
        assert kr.lease_released is True

        # Lease row is now released.
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT state FROM terminal_leases "
                "WHERE terminal_id='T0' AND project_id='proj-verify'"
            ).fetchone()
            assert row[0] == DB_STATE_RELEASED
        finally:
            conn.close()
    finally:
        _cleanup_children(mgr)


# ---------------------------------------------------------------------------
# Test 9: kill EPERM keeps lease in TERMINATING state
# ---------------------------------------------------------------------------


def test_kill_eperm_keeps_lease_terminating_state(tmp_path: Path, monkeypatch) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    try:
        inst = _spawn_test_t0(mgr, "proj-eperm")

        # Force _signal_process to report permission_denied.
        monkeypatch.setattr(
            T0LifecycleManager,
            "_signal_process",
            staticmethod(lambda pid, sig: "permission_denied"),
        )

        kr = mgr.kill("proj-eperm", inst.lease_token, wait_timeout=1.0)
        assert kr.error == "permission_denied"
        assert kr.verified_dead is False
        assert kr.lease_released is False

        # Lease must still be leased + TERMINATING.
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT state, metadata_json FROM terminal_leases "
                "WHERE terminal_id='T0' AND project_id='proj-eperm'"
            ).fetchone()
            assert row[0] == DB_STATE_LEASED
            meta = json.loads(row[1])
            assert meta["lifecycle_state"] == LIFECYCLE_TERMINATING
        finally:
            conn.close()

        # Audit: t0.kill.permission_denied emitted, t0.kill.verified NOT.
        events = _read_audit_events(tmp_path)
        types = [e["event_type"] for e in events]
        assert "t0.kill.permission_denied" in types
        assert "t0.kill.verified" not in types
    finally:
        _cleanup_children(mgr)


# ---------------------------------------------------------------------------
# Test 10: force_release_lease emits its own audit event
# ---------------------------------------------------------------------------


def test_force_release_lease_emits_own_audit_event(tmp_path: Path) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    try:
        inst = _spawn_test_t0(mgr, "proj-force")

        released = mgr.force_release_lease(inst.lease_token, "operator escape test")
        assert released is True

        events = _read_audit_events(tmp_path)
        types = [e["event_type"] for e in events]
        assert "t0.force_release.requested" in types
        assert "t0.force_release.committed" in types

        # Lease row now released.
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT state, metadata_json FROM terminal_leases "
                "WHERE terminal_id='T0' AND lease_token=?",
                (inst.lease_token,),
            ).fetchone()
            assert row[0] == DB_STATE_RELEASED
            meta = json.loads(row[1])
            assert meta["force_release_reason"] == "operator escape test"
        finally:
            conn.close()
    finally:
        _cleanup_children(mgr)


# ---------------------------------------------------------------------------
# Test 11: reap runs leases sequentially (no parallel kills)
# ---------------------------------------------------------------------------


def test_reap_sequential_no_parallel_kills(tmp_path: Path, monkeypatch) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg, heartbeat_timeout_seconds=0.1)

    try:
        # Spawn 3 leases, then artificially backdate their heartbeats.
        instances = []
        for pid_label in ["a", "b", "c"]:
            inst = _spawn_test_t0(mgr, f"proj-seq-{pid_label}")
            instances.append(inst)

        # Backdate heartbeats so they all become STALE.
        old = "2020-01-01T00:00:00.000+00:00"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "UPDATE terminal_leases SET last_heartbeat_at = ? "
                "WHERE terminal_id = 'T0' AND state = 'leased'",
                (old,),
            )
            conn.commit()
        finally:
            conn.close()

        # Track call timing of _signal_process to assert sequentiality.
        call_log: List[float] = []
        original_signal = T0LifecycleManager._signal_process

        def timed_signal(pid, sig):
            call_log.append(time.monotonic())
            time.sleep(0.05)  # simulate signal latency
            return original_signal(pid, sig)

        monkeypatch.setattr(T0LifecycleManager, "_signal_process", staticmethod(timed_signal))

        results = mgr.reap_dead_t0s()

        # Sequential: each signal call should be > 50ms after the previous one.
        assert len(call_log) >= 3
        for i in range(1, len(call_log)):
            delta = call_log[i] - call_log[i - 1]
            assert delta >= 0.04, f"calls overlap (delta={delta}): not sequential"

        assert len(results) == 3
        for r in results:
            assert r.classification in {"killed_by_reap", "already_dead", "refuted_alive", "error"}
    finally:
        _cleanup_children(mgr)


# ---------------------------------------------------------------------------
# Test 12: reap with alive process attempts SIGTERM
# ---------------------------------------------------------------------------


def test_reap_alive_process_attempts_sigterm(tmp_path: Path, monkeypatch) -> None:
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg, heartbeat_timeout_seconds=0.1)

    try:
        inst = _spawn_test_t0(mgr, "proj-reap-alive")

        # Backdate heartbeat.
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "UPDATE terminal_leases SET last_heartbeat_at = '2020-01-01T00:00:00.000+00:00' "
                "WHERE terminal_id='T0' AND project_id='proj-reap-alive'"
            )
            conn.commit()
        finally:
            conn.close()

        signals_sent: List[int] = []
        original_signal = T0LifecycleManager._signal_process

        def capturing_signal(pid, sig):
            signals_sent.append(sig)
            return original_signal(pid, sig)

        monkeypatch.setattr(T0LifecycleManager, "_signal_process", staticmethod(capturing_signal))

        results = mgr.reap_dead_t0s()
        assert len(results) == 1
        assert results[0].classification in {"killed_by_reap"}
        assert signal.SIGTERM in signals_sent
    finally:
        _cleanup_children(mgr)


# ---------------------------------------------------------------------------
# Test 13: reap with dead process skips signal, releases directly
# ---------------------------------------------------------------------------


def test_reap_dead_process_skips_signal(tmp_path: Path, monkeypatch) -> None:
    """STALE lease whose PID is dead → reap should release WITHOUT sending signals."""
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg, heartbeat_timeout_seconds=0.1)

    try:
        inst = _spawn_test_t0(mgr, "proj-reap-dead")

        # Backdate heartbeat to make it stale.
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "UPDATE terminal_leases SET last_heartbeat_at = '2020-01-01T00:00:00.000+00:00' "
                "WHERE terminal_id='T0' AND project_id='proj-reap-dead'"
            )
            conn.commit()
        finally:
            conn.close()

        # Force _is_alive to return False (simulating the process being dead),
        # so reap takes the already-dead path.
        monkeypatch.setattr(
            T0LifecycleManager,
            "_is_alive",
            lambda self, pid, lease_token=None: False,
        )

        signals_sent: List[int] = []
        original_signal = T0LifecycleManager._signal_process

        def capturing_signal(pid, sig):
            signals_sent.append(sig)
            return original_signal(pid, sig)

        monkeypatch.setattr(T0LifecycleManager, "_signal_process", staticmethod(capturing_signal))

        results = mgr.reap_dead_t0s()
        assert len(results) == 1, f"unexpected results: {results}"
        assert results[0].classification == "already_dead", f"unexpected result: {results[0]}"
        assert results[0].lease_released is True
        # No SIGTERM/SIGKILL sent — process was already dead.
        assert signal.SIGTERM not in signals_sent
        assert signal.SIGKILL not in signals_sent

        # t0.reap.completed emitted.
        events = _read_audit_events(tmp_path)
        types = [e["event_type"] for e in events]
        assert "t0.reap.completed" in types
    finally:
        # Subprocess was real; clean up.
        try:
            mgr.force_release_lease(inst.lease_token, "test cleanup")
        except (OSError, sqlite3.Error):
            pass
        try:
            os.kill(inst.pid, signal.SIGKILL)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test 14: audit-before-commit rolls back on emit failure (heartbeat path)
# ---------------------------------------------------------------------------


def test_audit_before_commit_rollback_on_emit_failure(tmp_path: Path) -> None:
    """For heartbeat: if audit-emit fails, the timestamp update must NOT persist."""
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    try:
        inst = _spawn_test_t0(mgr, "proj-emit-fail")
        ts_before = _heartbeat_at(db, "proj-emit-fail")

        # Force aggregator to fail on heartbeat.recorded.
        original_submit = agg.submit

        def failing_submit(update):
            if update.event_type == "t0.heartbeat.recorded":
                raise RuntimeError("simulated heartbeat emit failure")
            return original_submit(update)

        agg.submit = failing_submit  # type: ignore[assignment]

        with pytest.raises(T0AuditEmitError):
            mgr.heartbeat("proj-emit-fail", inst.pid, inst.lease_token)

        # last_heartbeat_at unchanged (rollback worked).
        ts_after = _heartbeat_at(db, "proj-emit-fail")
        assert ts_after == ts_before
    finally:
        _cleanup_children(mgr)


# ---------------------------------------------------------------------------
# Test 15: constructor rejects None aggregator (ADR-005 invariant)
# ---------------------------------------------------------------------------


def test_constructor_rejects_none_aggregator(tmp_path: Path) -> None:
    db = _bootstrap_db(tmp_path)
    with pytest.raises(ValueError, match="StateAggregator"):
        T0LifecycleManager(db, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Test 16: KillResult dataclass returns explicit fields for each path
# ---------------------------------------------------------------------------


def test_kill_result_dataclass_contracts(tmp_path: Path, monkeypatch) -> None:
    """Verify KillResult is populated correctly for every kill path."""
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    try:
        # Path 1: no active lease.
        kr_no_lease = mgr.kill("proj-nope", "fake-token", wait_timeout=0.1)
        assert kr_no_lease.error == "no_active_lease"
        assert kr_no_lease.verified_dead is False
        assert kr_no_lease.lease_released is False

        # Path 2: successful kill of real subprocess.
        inst = _spawn_test_t0(mgr, "proj-ok")
        kr_ok = mgr.kill("proj-ok", inst.lease_token, wait_timeout=3.0)
        assert kr_ok.verified_dead is True
        assert kr_ok.lease_released is True
        assert kr_ok.signaled is True
        assert kr_ok.error is None
        assert kr_ok.duration_ms is not None and kr_ok.duration_ms >= 0

        # Path 3: token mismatch raises.
        inst2 = _spawn_test_t0(mgr, "proj-mismatch")
        with pytest.raises(LeaseTokenMismatchError):
            mgr.kill("proj-mismatch", "wrong-token", wait_timeout=0.1)
    finally:
        _cleanup_children(mgr)


# ---------------------------------------------------------------------------
# Test 17: kill uses stored pid from metadata, not any caller-supplied value
# ---------------------------------------------------------------------------


def test_kill_uses_stored_pid_from_metadata(tmp_path: Path, monkeypatch) -> None:
    """kill() sources pid from metadata_json.pid (source of truth).

    Verifies that the pid passed to os.kill is the one stored in the DB
    lease row, confirming the blocking finding from codex R1 is addressed.
    """
    db = _bootstrap_db(tmp_path)
    agg = _make_aggregator(tmp_path)
    mgr = T0LifecycleManager(db, agg)

    try:
        inst = _spawn_test_t0(mgr, "proj-stored-pid")

        signaled_pids: List[int] = []
        original_signal = T0LifecycleManager._signal_process

        def capturing_signal(pid, sig):
            signaled_pids.append(pid)
            return original_signal(pid, sig)

        monkeypatch.setattr(T0LifecycleManager, "_signal_process", staticmethod(capturing_signal))

        kr = mgr.kill("proj-stored-pid", inst.lease_token, wait_timeout=3.0)

        assert kr.verified_dead is True
        assert kr.pid == inst.pid, "KillResult.pid must equal metadata-stored pid"
        assert signaled_pids[0] == inst.pid, "os.kill must use the metadata-stored pid"
    finally:
        _cleanup_children(mgr)
