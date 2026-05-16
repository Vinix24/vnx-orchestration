"""t0_lifecycle.py — Wave 5 PR-5.2: per-project T0 spawn/heartbeat/kill mechanics.

Control Centre uses this to manage N per-project T0 processes. Each T0 runs
in its own project worktree with isolated VNX_PROJECT_ID env. State aggregator
(PR-5.1) is the central state-sharing mechanism.

Lease isolation uses runtime_coordination.db terminal_leases with composite
UNIQUE(terminal_id="T0", project_id) per schema v10 (Wave 5 PR-5.3).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_T0_TERMINAL_ID = "T0"
_HEARTBEAT_TIMEOUT_DEFAULT = 120
_KILL_POLL_INTERVAL_DEFAULT = 0.5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_iso(ts: str) -> float:
    """Return POSIX timestamp from ISO 8601 string. Raises ValueError on bad input."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


@dataclass
class T0Instance:
    project_id: str
    pid: int
    started_at: str             # ISO 8601
    project_root: str           # absolute path
    state: str                  # spawning | running | stopping | stopped
    last_heartbeat_at: Optional[str] = None


class T0LifecycleManager:
    """Manages spawn/kill/heartbeat for per-project T0 processes.

    Thread-safe via SQLite WAL + EXCLUSIVE transactions. Each T0 is tracked
    as a lease row with terminal_id="T0" and project_id=<project_id>.
    """

    def __init__(self, opts: dict) -> None:
        self._coord_db = Path(opts["coord_db_path"])
        self._project_registry: Dict[str, dict] = opts.get("projects", {})
        self._heartbeat_timeout_seconds: float = opts.get(
            "heartbeat_timeout", _HEARTBEAT_TIMEOUT_DEFAULT
        )
        self._kill_poll_interval: float = opts.get(
            "kill_poll_interval", _KILL_POLL_INTERVAL_DEFAULT
        )
        self._aggregator = opts.get("aggregator")
        self._coord_db.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def spawn(self, project_id: str) -> T0Instance:
        """Spawn a per-project T0 process.

        Refuses if a T0 for this project_id is already running.
        Uses composite lease row (terminal_id="T0", project_id) from schema v10.

        Raises ValueError if T0 already running for this project_id.
        Raises RuntimeError if subprocess.Popen fails.
        """
        project_cfg = self._project_registry.get(project_id, {})
        project_root = project_cfg.get("root", "")
        cmd = list(project_cfg.get("cmd", ["claude"])) + list(
            project_cfg.get("claude_args", [])
        )
        now = _now_iso()

        conn = self._connect()
        try:
            conn.execute("BEGIN EXCLUSIVE")
            existing = self._get_t0_row(conn, project_id)
            if existing and existing["state"] == "leased":
                meta = json.loads(existing.get("metadata_json") or "{}")
                conn.execute("ROLLBACK")
                raise ValueError(
                    f"T0 for project {project_id!r} is already running "
                    f"(pid={meta.get('pid')})"
                )

            env = {**os.environ, "VNX_PROJECT_ID": project_id}
            if project_root:
                env["VNX_PROJECT_ROOT"] = project_root

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=project_root or None,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as e:
                conn.execute("ROLLBACK")
                raise RuntimeError(
                    f"Failed to spawn T0 for {project_id!r}: {e}"
                ) from e

            pid = proc.pid
            metadata = json.dumps(
                {"pid": pid, "project_root": project_root, "started_at": now}
            )

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO terminal_leases
                        (terminal_id, project_id, state, dispatch_id, generation,
                         leased_at, last_heartbeat_at, metadata_json)
                    VALUES (?, ?, 'leased', NULL, 1, ?, ?, ?)
                    """,
                    (_T0_TERMINAL_ID, project_id, now, now, metadata),
                )
            else:
                conn.execute(
                    """
                    UPDATE terminal_leases
                    SET state='leased', generation=generation+1,
                        leased_at=?, last_heartbeat_at=?,
                        released_at=NULL, dispatch_id=NULL, metadata_json=?
                    WHERE terminal_id=? AND project_id=?
                    """,
                    (now, now, metadata, _T0_TERMINAL_ID, project_id),
                )
            conn.execute("COMMIT")
        finally:
            conn.close()

        instance = T0Instance(
            project_id=project_id,
            pid=pid,
            started_at=now,
            project_root=project_root,
            state="running",
            last_heartbeat_at=now,
        )
        self._emit_event(project_id, "t0_spawned", {
            "pid": pid,
            "project_root": project_root,
            "started_at": now,
        })
        return instance

    def heartbeat(self, project_id: str, pid: int) -> bool:
        """Update last_heartbeat_at for the given project T0.

        Returns True if recorded. Returns False if no matching running lease found
        or if the pid does not match the stored pid.
        """
        now = _now_iso()
        with self._connect() as conn:
            row = self._get_t0_row(conn, project_id)
            if not row or row["state"] != "leased":
                return False
            meta = json.loads(row.get("metadata_json") or "{}")
            if meta.get("pid") != pid:
                return False
            conn.execute(
                """
                UPDATE terminal_leases SET last_heartbeat_at=?
                WHERE terminal_id=? AND project_id=?
                """,
                (now, _T0_TERMINAL_ID, project_id),
            )
            conn.commit()

        self._emit_event(project_id, "t0_heartbeat", {
            "pid": pid,
            "heartbeat_at": now,
        })
        return True

    def list_running(self) -> List[T0Instance]:
        """Return all T0 instances currently in running (leased) state."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM terminal_leases WHERE terminal_id=? AND state='leased'",
                (_T0_TERMINAL_ID,),
            ).fetchall()

        result: List[T0Instance] = []
        for row in rows:
            r = dict(row)
            meta = json.loads(r.get("metadata_json") or "{}")
            result.append(T0Instance(
                project_id=r["project_id"],
                pid=meta.get("pid", -1),
                started_at=meta.get("started_at", r.get("leased_at", "")),
                project_root=meta.get("project_root", ""),
                state="running",
                last_heartbeat_at=r.get("last_heartbeat_at"),
            ))
        return result

    def kill(
        self,
        project_id: str,
        *,
        signal_type: int = signal.SIGTERM,
    ) -> bool:
        """Send signal to T0 process.

        SIGTERM: wait up to heartbeat_timeout_seconds for graceful shutdown,
        then escalate to SIGKILL. SIGKILL: immediate, no wait.
        Returns True if a running lease was found and signalled.
        """
        with self._connect() as conn:
            row = self._get_t0_row(conn, project_id)
            if not row or row["state"] != "leased":
                return False
            meta = json.loads(row.get("metadata_json") or "{}")
            pid = meta.get("pid")
            if not pid:
                return False

        try:
            os.kill(pid, signal_type)
        except ProcessLookupError:
            pass
        except OSError as e:
            log.warning("t0_lifecycle: kill pid=%d failed: %s", pid, e)

        if signal_type == signal.SIGTERM:
            deadline = time.monotonic() + self._heartbeat_timeout_seconds
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(self._kill_poll_interval)
                except ProcessLookupError:
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        now = _now_iso()
        with self._connect() as conn:
            row = self._get_t0_row(conn, project_id)
            if row:
                existing_meta = json.loads(row.get("metadata_json") or "{}")
                existing_meta["killed_at"] = now
                conn.execute(
                    """
                    UPDATE terminal_leases
                    SET state='released', released_at=?, metadata_json=?
                    WHERE terminal_id=? AND project_id=?
                    """,
                    (now, json.dumps(existing_meta), _T0_TERMINAL_ID, project_id),
                )
                conn.commit()

        self._emit_event(project_id, "t0_killed", {
            "pid": pid,
            "signal": signal_type,
            "killed_at": now,
        })
        return True

    def reap_dead_t0s(self) -> List[str]:
        """Detect T0s where last_heartbeat older than heartbeat_timeout_seconds.

        Marks their leases released and returns list of reaped project_ids.
        Idempotent: already-released T0s are not re-reaped.
        """
        now_ts = time.time()
        reaped: List[str] = []

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM terminal_leases WHERE terminal_id=? AND state='leased'",
                (_T0_TERMINAL_ID,),
            ).fetchall()

            for row in rows:
                r = dict(row)
                hb = r.get("last_heartbeat_at")
                if not hb:
                    continue
                try:
                    age = now_ts - _parse_iso(hb)
                except ValueError:
                    continue
                if age > self._heartbeat_timeout_seconds:
                    released_at = _now_iso()
                    conn.execute(
                        """
                        UPDATE terminal_leases
                        SET state='released', released_at=?
                        WHERE terminal_id=? AND project_id=?
                        """,
                        (released_at, _T0_TERMINAL_ID, r["project_id"]),
                    )
                    reaped.append(r["project_id"])

            if reaped:
                conn.commit()

        for project_id in reaped:
            self._emit_event(project_id, "t0_reaped", {
                "reason": "heartbeat_timeout",
                "timeout_seconds": self._heartbeat_timeout_seconds,
            })
        return reaped

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._coord_db), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _get_t0_row(
        self, conn: sqlite3.Connection, project_id: str
    ) -> Optional[dict]:
        row = conn.execute(
            """
            SELECT * FROM terminal_leases
            WHERE terminal_id=? AND project_id=?
            """,
            (_T0_TERMINAL_ID, project_id),
        ).fetchone()
        return dict(row) if row else None

    def _emit_event(
        self, project_id: str, event_type: str, payload: dict
    ) -> None:
        if self._aggregator is None:
            return
        try:
            from scripts.aggregator.state_aggregator import ProjectStateUpdate
            update = ProjectStateUpdate(
                project_id=project_id,
                timestamp=_now_iso(),
                event_type=event_type,
                payload=payload,
                source_t0="T0-lifecycle",
            )
            self._aggregator.submit(update)
        except Exception as e:
            log.warning(
                "t0_lifecycle: aggregator emit failed for %s/%s: %s",
                project_id,
                event_type,
                e,
            )
