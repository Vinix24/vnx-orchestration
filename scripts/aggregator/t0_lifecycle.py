"""t0_lifecycle.py — Wave 5 PR-5.2: per-project T0 lifecycle management.

Clean redesign per claudedocs/wave5-pr2-t0-lifecycle-redesign.md (architect spec).

Replaces ad-hoc lease-mutation-as-process-control with an explicit state machine
that separates lease-state (SQLite row) from process-state (POSIX PID) and ties
every transition to a per-incarnation `lease_token` (UUID v7).

Public surface:
    - T0LifecycleManager: spawn, heartbeat, kill, reap_dead_t0s,
                          list_running, force_release_lease
    - T0Instance: dataclass returned by spawn()
    - KillResult: dataclass returned by kill()
    - ReapResult: dataclass returned per row by reap_dead_t0s()
    - LeaseTokenMismatchError: raised when an operation targets a stale lease

Lifecycle states (projected via metadata_json.lifecycle_state):
    PENDING → RUNNING → STALE → TERMINATING → REAPED

Only RUNNING, TERMINATING, REAPED are persisted; PENDING and STALE are
in-memory classifications. The SQLite `state` column stays in the
'leased' | 'released' enum (schema-compat with runtime_core_cli).

ADR-005 invariant: every state mutation emits a structured audit event BEFORE
the COMMIT. If the audit emit fails, the transaction is rolled back and any
external side-effect (subprocess spawn) is cleaned up via rollback_action.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .state_aggregator import ProjectStateUpdate, StateAggregator

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

T0_TERMINAL = "T0"

# Lifecycle state strings stored in metadata_json.lifecycle_state.
LIFECYCLE_PENDING = "PENDING"
LIFECYCLE_RUNNING = "RUNNING"
LIFECYCLE_STALE = "STALE"
LIFECYCLE_TERMINATING = "TERMINATING"
LIFECYCLE_REAPED = "REAPED"

# SQLite state column values (schema-compat with other terminal_leases consumers).
DB_STATE_LEASED = "leased"
DB_STATE_RELEASED = "released"

# Default tuning.
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 60.0
DEFAULT_KILL_WAIT_TIMEOUT_SECONDS = 10.0
DEFAULT_SIGKILL_WAIT_TIMEOUT_SECONDS = 5.0
DEFAULT_DB_CONNECT_TIMEOUT_SECONDS = 10.0

# Exponential backoff for BEGIN EXCLUSIVE contention.
_DB_BACKOFF_DELAYS = (0.5, 1.0, 2.0, 4.0)


# ----------------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------------


class T0LifecycleError(RuntimeError):
    """Base for all T0 lifecycle exceptions."""


class T0AlreadyRunningError(T0LifecycleError):
    """Raised when spawn() is called for a project that already has a leased T0."""

    def __init__(self, project_id: str, pid: Optional[int] = None) -> None:
        super().__init__(
            f"T0 for project {project_id!r} is already leased"
            + (f" (pid={pid})" if pid else "")
        )
        self.project_id = project_id
        self.pid = pid


class T0SpawnFailedError(T0LifecycleError):
    """Raised when subprocess.Popen fails or exits before lease commit."""


class T0SubprocessExitedEarly(T0SpawnFailedError):
    """Subprocess exited before the spawn transaction committed."""

    def __init__(self, returncode: int) -> None:
        super().__init__(f"T0 subprocess exited early with returncode={returncode}")
        self.returncode = returncode


class T0AuditEmitError(T0LifecycleError):
    """Raised when the aggregator-emit step inside _commit_with_audit fails.

    The transaction has been rolled back; any rollback_action has been run.
    """


class T0SpawnContentionError(T0LifecycleError):
    """Raised when BEGIN EXCLUSIVE cannot acquire the DB after retry budget."""


class LeaseTokenMismatchError(T0LifecycleError):
    """Raised when a kill/force_release operation targets a stale lease_token.

    Means: a successor lease has replaced the lease the caller thought it
    owned. Caller should refresh state and decide whether to abort or retarget.
    """

    def __init__(self, project_id: str, expected: str, actual: str) -> None:
        super().__init__(
            f"lease_token mismatch for project {project_id!r}: "
            f"caller expected {expected[:16]}..., DB has {actual[:16]}..."
        )
        self.project_id = project_id
        self.expected = expected
        self.actual = actual


# ----------------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------------


@dataclass
class T0Instance:
    """Return value of T0LifecycleManager.spawn().

    Caller persists `lease_token` to address this specific incarnation in
    subsequent heartbeat/kill calls.
    """

    project_id: str
    pid: int
    lease_token: str
    generation: int
    started_at: str
    project_root: str
    lifecycle_state: str = LIFECYCLE_RUNNING


@dataclass
class KillResult:
    """Return value of T0LifecycleManager.kill().

    Replaces a bool return — every kill path produces an explicit, structured
    outcome. Callers must check `verified_dead` (not `signaled`) to know
    whether the lease has been released.
    """

    project_id: str
    lease_token: str
    pid: int
    signaled: bool = False
    verified_dead: bool = False
    lease_released: bool = False
    escalated_to_sigkill: bool = False
    error: Optional[str] = None
    duration_ms: Optional[int] = None


@dataclass
class ReapResult:
    """Return value (per stale lease) of T0LifecycleManager.reap_dead_t0s()."""

    project_id: str
    lease_token: str
    pid: Optional[int]
    classification: str  # "already_dead" | "killed_by_reap" | "refuted_alive" | "error"
    lease_released: bool = False
    error: Optional[str] = None


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _new_lease_token() -> str:
    """UUID v7-ish: time-ordered prefix + random suffix.

    Format: <12 hex unix-ms>-<20 hex random>. 33 chars total (32 hex + 1 dash).
    Sort-stable by timestamp prefix; collision-resistant by random suffix.
    """
    ts_ms = _now_ms()
    return f"{ts_ms:012x}-{uuid.uuid4().hex[:20]}"


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse ISO 8601 timestamp; return None on failure."""
    if not ts:
        return None
    try:
        # Handle trailing Z
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# T0LifecycleManager
# ----------------------------------------------------------------------------


class T0LifecycleManager:
    """Manages per-project T0 process lifecycle with explicit state machine.

    Construction REQUIRES a StateAggregator instance — no silent fallback to
    logging. ADR-005 invariant demands every mutation has an audit record.
    """

    def __init__(
        self,
        coord_db_path: Path,
        aggregator: StateAggregator,
        *,
        heartbeat_timeout_seconds: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        kill_wait_timeout_seconds: float = DEFAULT_KILL_WAIT_TIMEOUT_SECONDS,
        sigkill_wait_timeout_seconds: float = DEFAULT_SIGKILL_WAIT_TIMEOUT_SECONDS,
        db_connect_timeout_seconds: float = DEFAULT_DB_CONNECT_TIMEOUT_SECONDS,
        subprocess_factory: Optional[Callable[..., subprocess.Popen]] = None,
    ) -> None:
        if aggregator is None:
            raise ValueError(
                "T0LifecycleManager requires a StateAggregator instance "
                "(ADR-005: every mutation needs an audit record)."
            )

        self._db_path = Path(coord_db_path)
        self._aggregator = aggregator
        self._heartbeat_timeout_seconds = float(heartbeat_timeout_seconds)
        self._kill_wait_timeout_seconds = float(kill_wait_timeout_seconds)
        self._sigkill_wait_timeout_seconds = float(sigkill_wait_timeout_seconds)
        self._db_connect_timeout_seconds = float(db_connect_timeout_seconds)
        self._subprocess_factory = subprocess_factory or subprocess.Popen
        self._lock = threading.Lock()
        # Retained Popen handles indexed by lease_token. Used for waitpid
        # zombie drain by the parent process (mgr is the Popen parent).
        # Released when the lease is reaped/released.
        self._popen_handles: Dict[str, subprocess.Popen] = {}

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=self._db_connect_timeout_seconds,
            isolation_level=None,  # explicit transaction control
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _begin_exclusive_with_retry(self, conn: sqlite3.Connection) -> None:
        """BEGIN EXCLUSIVE with exponential backoff on cross-process contention."""
        last_err: Optional[sqlite3.OperationalError] = None
        for attempt, delay in enumerate(_DB_BACKOFF_DELAYS):
            try:
                conn.execute("BEGIN EXCLUSIVE")
                return
            except sqlite3.OperationalError as e:
                last_err = e
                if "locked" not in str(e).lower():
                    raise
                if attempt < len(_DB_BACKOFF_DELAYS) - 1:
                    time.sleep(delay)
                    continue
                break
        raise T0SpawnContentionError(
            f"DB contention after {len(_DB_BACKOFF_DELAYS)} attempts"
        ) from last_err

    # ------------------------------------------------------------------
    # Audit-event helpers
    # ------------------------------------------------------------------

    def _emit_event(
        self,
        project_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        """Emit an audit event via the aggregator.

        Raises on aggregator failure — caller (_commit_with_audit) handles
        the rollback path.
        """
        update = ProjectStateUpdate(
            project_id=project_id,
            timestamp=_now_iso(),
            event_type=event_type,
            payload=payload,
            source_t0=T0_TERMINAL,
        )
        self._aggregator.submit(update)

    def _commit_with_audit(
        self,
        conn: sqlite3.Connection,
        project_id: str,
        event_type: str,
        payload: Dict[str, Any],
        *,
        rollback_action: Optional[Callable[[], None]] = None,
    ) -> None:
        """Emit audit event BEFORE COMMIT.

        On audit-emit failure: ROLLBACK, run rollback_action (if any), then
        raise T0AuditEmitError. ADR-005 invariant: no DB mutation without
        a corresponding audit record.
        """
        try:
            self._emit_event(project_id, event_type, payload)
        except (OSError, RuntimeError, ValueError) as e:
            log.error(
                "t0_lifecycle: audit emit failed (event=%s project=%s): %s; rolling back",
                event_type,
                project_id,
                e,
            )
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as rollback_err:
                log.error("t0_lifecycle: ROLLBACK also failed: %s", rollback_err)
            if rollback_action is not None:
                try:
                    rollback_action()
                except Exception as ra_err:  # noqa: BLE001 — bounded cleanup
                    log.error(
                        "t0_lifecycle: rollback_action failed: %s", ra_err
                    )
            raise T0AuditEmitError(
                f"audit emit failed for {event_type}: {e}"
            ) from e

        try:
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            log.error("t0_lifecycle: COMMIT failed after audit emit: %s", e)
            if rollback_action is not None:
                try:
                    rollback_action()
                except Exception as ra_err:  # noqa: BLE001
                    log.error(
                        "t0_lifecycle: rollback_action failed after commit error: %s",
                        ra_err,
                    )
            raise

    # ------------------------------------------------------------------
    # Lease-row helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_metadata(metadata_raw: Optional[str]) -> Dict[str, Any]:
        if not metadata_raw:
            return {}
        try:
            return json.loads(metadata_raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("t0_lifecycle: corrupt metadata_json, treating as empty")
            return {}

    def _get_active_row(
        self,
        conn: sqlite3.Connection,
        project_id: str,
    ) -> Optional[sqlite3.Row]:
        cur = conn.execute(
            "SELECT * FROM terminal_leases "
            "WHERE terminal_id = ? AND project_id = ? AND state = ?",
            (T0_TERMINAL, project_id, DB_STATE_LEASED),
        )
        return cur.fetchone()

    def _get_row_by_token(
        self,
        conn: sqlite3.Connection,
        lease_token: str,
    ) -> Optional[sqlite3.Row]:
        if not lease_token:
            return None
        cur = conn.execute(
            "SELECT * FROM terminal_leases "
            "WHERE terminal_id = ? AND lease_token = ?",
            (T0_TERMINAL, lease_token),
        )
        return cur.fetchone()

    # ------------------------------------------------------------------
    # Subprocess helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _signal_process(pid: int, sig: int) -> str:
        """Send a signal to pid. Returns outcome marker:
        'sent' | 'already_dead' | 'permission_denied' | 'error:<errno>'.
        """
        try:
            os.kill(pid, sig)
            return "sent"
        except ProcessLookupError:
            return "already_dead"
        except PermissionError:
            return "permission_denied"
        except OSError as e:
            if e.errno == errno.ESRCH:
                return "already_dead"
            if e.errno == errno.EPERM:
                return "permission_denied"
            return f"error:{e.errno}"

    def _is_alive(self, pid: int, lease_token: Optional[str] = None) -> bool:
        """Liveness probe. True iff process is running (not a zombie).

        When a retained Popen handle exists for lease_token, prefer Popen.poll()
        — it reaps zombies and returns the returncode, so we can distinguish
        "still running" from "zombie not yet reaped". For unowned pids, falls
        back to os.kill(pid, 0) (which considers zombies as alive).
        """
        if lease_token and lease_token in self._popen_handles:
            proc = self._popen_handles[lease_token]
            try:
                return proc.poll() is None
            except OSError as e:
                log.debug("_is_alive: Popen.poll failed for pid=%s: %s", pid, e)
                # Fall through to os.kill check.
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we don't own it.
            return True
        except OSError as e:
            if e.errno == errno.ESRCH:
                return False
            if e.errno == errno.EPERM:
                return True
            raise

    def _wait_for_exit(
        self,
        pid: int,
        timeout_seconds: float,
        lease_token: Optional[str] = None,
    ) -> bool:
        """Poll for process exit. Returns True if exited within timeout."""
        # When we own the Popen, use Popen.wait directly — reaps the zombie
        # and is the only correct way for start_new_session children.
        if lease_token and lease_token in self._popen_handles:
            proc = self._popen_handles[lease_token]
            try:
                proc.wait(timeout=max(0.0, timeout_seconds))
                return True
            except subprocess.TimeoutExpired:
                return False
            except OSError as e:
                log.debug("_wait_for_exit: Popen.wait failed for pid=%s: %s", pid, e)
                # Fall through to polling.

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        poll_interval = 0.1
        while time.monotonic() < deadline:
            if not self._is_alive(pid, lease_token):
                return True
            time.sleep(poll_interval)
        return not self._is_alive(pid, lease_token)

    def _drain_zombie(self, pid: int, lease_token: Optional[str] = None) -> None:
        """waitpid(pid, WNOHANG) to drain zombie if we are the parent.

        When the lease_token has a retained Popen handle (from spawn()), use
        Popen.poll() / Popen.wait() so the child is reaped via the Popen
        machinery — this is the only reliable path when start_new_session
        decoupled the OS-level process group.

        Silently ignores ChildProcessError (not our child) and other OS errors —
        the OS will reap unowned children eventually.
        """
        # Prefer Popen.wait / poll if we own the handle.
        if lease_token and lease_token in self._popen_handles:
            proc = self._popen_handles[lease_token]
            try:
                # poll() is non-blocking. If returncode is set, the child is reaped.
                proc.poll()
                if proc.returncode is None:
                    # Best-effort short wait — caller already verified death.
                    try:
                        proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        log.debug(
                            "t0_lifecycle: Popen.wait timed out for pid=%s", pid
                        )
            except OSError as e:
                log.debug(
                    "t0_lifecycle: Popen.poll/wait failed for pid=%s: %s", pid, e
                )
            return

        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            log.debug("t0_lifecycle: pid=%s not our child, skip waitpid", pid)
        except OSError as e:
            log.debug("t0_lifecycle: waitpid(%s) failed: %s", pid, e)

    def _release_popen_handle(self, lease_token: str) -> None:
        """Discard the Popen handle for a released lease."""
        self._popen_handles.pop(lease_token, None)

    def _force_kill_subprocess(self, pid: int) -> None:
        """Best-effort cleanup path used by spawn rollback.

        Escalates SIGTERM → wait → SIGKILL → wait → waitpid. No exceptions
        propagate; the caller is already in an error path.
        """
        if pid <= 0:
            return
        try:
            outcome = self._signal_process(pid, signal.SIGTERM)
            if outcome == "sent":
                self._wait_for_exit(pid, self._sigkill_wait_timeout_seconds)
            if self._is_alive(pid):
                self._signal_process(pid, signal.SIGKILL)
                self._wait_for_exit(pid, self._sigkill_wait_timeout_seconds)
            self._drain_zombie(pid)
        except Exception as e:  # noqa: BLE001 — bounded cleanup path
            log.error("t0_lifecycle: rollback subprocess kill failed: %s", e)

    # ------------------------------------------------------------------
    # Public API: spawn
    # ------------------------------------------------------------------

    def spawn(
        self,
        project_id: str,
        *,
        argv: Optional[List[str]] = None,
        project_root: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> T0Instance:
        """Spawn a T0 worker subprocess for project_id.

        Invariant: a successful spawn yields exactly one (terminal_id="T0",
        project_id) row in `leased` state with a fresh lease_token, an audit
        trail of two events (t0.spawn.requested + t0.spawn.committed), and a
        live subprocess whose pid matches metadata_json.pid.

        Failure paths leave no orphan rows or processes.
        """
        with self._lock:
            return self._spawn_locked(project_id, argv, project_root, env)

    def _spawn_locked(
        self,
        project_id: str,
        argv: Optional[List[str]],
        project_root: Optional[str],
        env: Optional[Dict[str, str]],
    ) -> T0Instance:
        lease_token = _new_lease_token()
        resolved_root = project_root or os.getcwd()

        # Emit t0.spawn.requested BEFORE any DB-mutation or subprocess spawn,
        # so a crash inside the EXCLUSIVE block leaves a forensic breadcrumb.
        try:
            self._emit_event(
                project_id,
                "t0.spawn.requested",
                {
                    "lease_token": lease_token,
                    "project_root": resolved_root,
                },
            )
        except (OSError, RuntimeError, ValueError) as e:
            raise T0AuditEmitError(
                f"audit emit failed for t0.spawn.requested: {e}"
            ) from e

        conn = self._connect()
        proc: Optional[subprocess.Popen] = None
        pid: int = -1
        try:
            self._begin_exclusive_with_retry(conn)

            existing = self._get_active_row(conn, project_id)
            if existing is not None:
                existing_meta = self._parse_metadata(existing["metadata_json"])
                existing_pid = existing_meta.get("pid")
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    log.debug("t0_lifecycle: ROLLBACK on AlreadyRunning suppressed")
                raise T0AlreadyRunningError(project_id, existing_pid)

            # Existing row may be in 'released' state; we will UPSERT.
            cur = conn.execute(
                "SELECT generation FROM terminal_leases "
                "WHERE terminal_id = ? AND project_id = ?",
                (T0_TERMINAL, project_id),
            )
            prior = cur.fetchone()
            new_generation = (prior["generation"] + 1) if prior else 1

            # Spawn subprocess.
            spawn_argv = argv or [sys.executable, "-c", "import time; time.sleep(60)"]
            try:
                proc = self._subprocess_factory(
                    spawn_argv,
                    cwd=resolved_root,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                pid = proc.pid
            except OSError as e:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    log.debug("t0_lifecycle: ROLLBACK on spawn OSError suppressed")
                raise T0SpawnFailedError(
                    f"subprocess.Popen failed for {project_id}: {e}"
                ) from e

            # Detect early-exit (exec failure, etc.).
            time.sleep(0.05)
            returncode = proc.poll()
            if returncode is not None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    log.debug("t0_lifecycle: ROLLBACK on early-exit suppressed")
                # Try to drain in case of zombie.
                self._drain_zombie(pid)
                raise T0SubprocessExitedEarly(returncode)

            started_at = _now_iso()
            metadata = {
                "pid": pid,
                "project_root": resolved_root,
                "started_at": started_at,
                "lifecycle_state": LIFECYCLE_RUNNING,
                "lease_token": lease_token,
            }

            if prior is not None:
                conn.execute(
                    "UPDATE terminal_leases SET "
                    "state = ?, generation = ?, lease_token = ?, "
                    "leased_at = ?, last_heartbeat_at = ?, released_at = NULL, "
                    "metadata_json = ? "
                    "WHERE terminal_id = ? AND project_id = ?",
                    (
                        DB_STATE_LEASED,
                        new_generation,
                        lease_token,
                        started_at,
                        started_at,
                        json.dumps(metadata),
                        T0_TERMINAL,
                        project_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO terminal_leases "
                    "(terminal_id, project_id, state, generation, lease_token, "
                    " leased_at, last_heartbeat_at, metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        T0_TERMINAL,
                        project_id,
                        DB_STATE_LEASED,
                        new_generation,
                        lease_token,
                        started_at,
                        started_at,
                        json.dumps(metadata),
                    ),
                )

            committed_pid = pid

            def _rollback_subprocess() -> None:
                self._force_kill_subprocess(committed_pid)
                # Best-effort spawn-aborted audit so forensics knows why.
                try:
                    self._emit_event(
                        project_id,
                        "t0.spawn.aborted",
                        {
                            "lease_token": lease_token,
                            "pid": committed_pid,
                            "reason": "audit_or_commit_failed",
                            "subprocess_killed": True,
                        },
                    )
                except Exception as e:  # noqa: BLE001 — best-effort
                    log.error(
                        "t0_lifecycle: spawn.aborted emit failed: %s", e
                    )

            self._commit_with_audit(
                conn,
                project_id,
                "t0.spawn.committed",
                {
                    "lease_token": lease_token,
                    "pid": pid,
                    "started_at": started_at,
                    "generation": new_generation,
                    "project_root": resolved_root,
                },
                rollback_action=_rollback_subprocess,
            )

            # Retain Popen handle so we can call waitpid as the actual parent.
            if proc is not None:
                self._popen_handles[lease_token] = proc

            return T0Instance(
                project_id=project_id,
                pid=pid,
                lease_token=lease_token,
                generation=new_generation,
                started_at=started_at,
                project_root=resolved_root,
                lifecycle_state=LIFECYCLE_RUNNING,
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API: heartbeat
    # ------------------------------------------------------------------

    def heartbeat(
        self,
        project_id: str,
        pid: int,
        lease_token: str,
    ) -> bool:
        """Record a heartbeat for a specific lease incarnation.

        Returns True on match, False on token-mismatch / no active lease /
        pid-mismatch. Heartbeat NEVER causes a lifecycle state transition.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                cur = conn.execute(
                    "SELECT * FROM terminal_leases "
                    "WHERE terminal_id = ? AND project_id = ? "
                    "AND lease_token = ? AND state = ?",
                    (T0_TERMINAL, project_id, lease_token, DB_STATE_LEASED),
                )
                row = cur.fetchone()
                if row is None:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        log.debug("heartbeat: ROLLBACK on no-match suppressed")
                    return False

                meta = self._parse_metadata(row["metadata_json"])
                stored_pid = meta.get("pid")
                if stored_pid is not None and stored_pid != pid:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        log.debug("heartbeat: ROLLBACK on pid-mismatch suppressed")
                    log.warning(
                        "heartbeat: pid mismatch for project=%s token=%s "
                        "(stored=%s, given=%s)",
                        project_id,
                        lease_token[:16],
                        stored_pid,
                        pid,
                    )
                    return False

                now = _now_iso()
                conn.execute(
                    "UPDATE terminal_leases SET last_heartbeat_at = ? "
                    "WHERE terminal_id = ? AND project_id = ? AND lease_token = ?",
                    (now, T0_TERMINAL, project_id, lease_token),
                )

                try:
                    self._commit_with_audit(
                        conn,
                        project_id,
                        "t0.heartbeat.recorded",
                        {
                            "lease_token": lease_token,
                            "pid": pid,
                            "heartbeat_at": now,
                        },
                    )
                except T0AuditEmitError:
                    # Heartbeat is best-effort: emit failure surfaces upward
                    # but the lease itself is not corrupted (no state change
                    # was made because ROLLBACK already happened in helper).
                    raise
                return True
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Public API: list_running
    # ------------------------------------------------------------------

    def list_running(self) -> List[T0Instance]:
        """Return all active T0 leases (state=leased, terminal=T0)."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM terminal_leases "
                "WHERE terminal_id = ? AND state = ?",
                (T0_TERMINAL, DB_STATE_LEASED),
            )
            instances: List[T0Instance] = []
            for row in cur.fetchall():
                meta = self._parse_metadata(row["metadata_json"])
                instances.append(
                    T0Instance(
                        project_id=row["project_id"],
                        pid=meta.get("pid", -1),
                        lease_token=row["lease_token"] or "",
                        generation=row["generation"],
                        started_at=meta.get("started_at", row["leased_at"] or ""),
                        project_root=meta.get("project_root", ""),
                        lifecycle_state=meta.get("lifecycle_state", LIFECYCLE_RUNNING),
                    )
                )
            return instances
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API: kill
    # ------------------------------------------------------------------

    def kill(
        self,
        project_id: str,
        pid: int,
        lease_token: str,
        *,
        signal_type: int = signal.SIGTERM,
        wait_timeout: Optional[float] = None,
        source: str = "operator",
    ) -> KillResult:
        """Terminate a specific T0 incarnation.

        Sequence: TERMINATING state → signal → wait → verify dead → release.
        Lease is released ONLY after ProcessLookupError confirms process death.
        """
        with self._lock:
            return self._kill_locked(
                project_id,
                pid,
                lease_token,
                signal_type=signal_type,
                wait_timeout=wait_timeout,
                source=source,
            )

    def _kill_locked(
        self,
        project_id: str,
        pid: int,
        lease_token: str,
        *,
        signal_type: int,
        wait_timeout: Optional[float],
        source: str,
    ) -> KillResult:
        started_ms = _now_ms()
        wait = wait_timeout if wait_timeout is not None else self._kill_wait_timeout_seconds

        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = self._get_active_row(conn, project_id)
            if row is None:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    log.debug("kill: ROLLBACK on no-active-lease suppressed")
                return KillResult(
                    project_id=project_id,
                    lease_token=lease_token,
                    pid=pid,
                    error="no_active_lease",
                )

            db_token = row["lease_token"] or ""
            if db_token != lease_token:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    log.debug("kill: ROLLBACK on token-mismatch suppressed")
                raise LeaseTokenMismatchError(project_id, lease_token, db_token)

            # Transition to TERMINATING (persist via metadata_json).
            meta = self._parse_metadata(row["metadata_json"])
            meta["lifecycle_state"] = LIFECYCLE_TERMINATING
            meta["kill_signal_sent_at"] = _now_iso()
            meta["kill_initiated_by"] = source
            conn.execute(
                "UPDATE terminal_leases SET metadata_json = ? "
                "WHERE terminal_id = ? AND project_id = ? AND lease_token = ?",
                (json.dumps(meta), T0_TERMINAL, project_id, lease_token),
            )

            self._commit_with_audit(
                conn,
                project_id,
                "t0.kill.requested",
                {
                    "lease_token": lease_token,
                    "pid": pid,
                    "signal_type": int(signal_type),
                    "source": source,
                },
            )
        finally:
            conn.close()

        # Signal phase — outside transaction.
        result = KillResult(
            project_id=project_id,
            lease_token=lease_token,
            pid=pid,
        )

        outcome = self._signal_process(pid, signal_type)
        if outcome == "permission_denied":
            self._emit_event(
                project_id,
                "t0.kill.permission_denied",
                {
                    "lease_token": lease_token,
                    "pid": pid,
                    "errno": errno.EPERM,
                },
            )
            result.error = "permission_denied"
            result.duration_ms = _now_ms() - started_ms
            return result

        if outcome == "already_dead":
            # Skip signal phase, go straight to verify+release.
            result.signaled = False
        else:
            result.signaled = True
            self._emit_event(
                project_id,
                "t0.kill.signaled",
                {
                    "lease_token": lease_token,
                    "pid": pid,
                    "signal_type": int(signal_type),
                    "escalation": False,
                },
            )

            # Wait for exit.
            exited = self._wait_for_exit(pid, wait, lease_token=lease_token)
            if not exited:
                if signal_type == signal.SIGTERM:
                    # Escalate to SIGKILL.
                    result.escalated_to_sigkill = True
                    escalate_outcome = self._signal_process(pid, signal.SIGKILL)
                    if escalate_outcome == "sent":
                        self._emit_event(
                            project_id,
                            "t0.kill.signaled",
                            {
                                "lease_token": lease_token,
                                "pid": pid,
                                "signal_type": int(signal.SIGKILL),
                                "escalation": True,
                            },
                        )
                        self._wait_for_exit(
                            pid,
                            self._sigkill_wait_timeout_seconds,
                            lease_token=lease_token,
                        )

        # Verify death.
        if self._is_alive(pid, lease_token=lease_token):
            result.error = (
                "sigkill_zombie"
                if result.escalated_to_sigkill
                else "alive_after_wait"
            )
            result.duration_ms = _now_ms() - started_ms
            self._emit_event(
                project_id,
                "t0.error.kill",
                {
                    "lease_token": lease_token,
                    "pid": pid,
                    "error_type": result.error,
                    "message": "process still alive after wait window",
                },
            )
            return result

        result.verified_dead = True
        self._drain_zombie(pid, lease_token=lease_token)

        # Final release transaction.
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = self._get_row_by_token(conn, lease_token)
            if row is None or row["state"] != DB_STATE_LEASED:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    log.debug("kill: ROLLBACK on already-released suppressed")
                result.duration_ms = _now_ms() - started_ms
                return result

            meta = self._parse_metadata(row["metadata_json"])
            meta["lifecycle_state"] = LIFECYCLE_REAPED
            meta["reaped_at"] = _now_iso()
            conn.execute(
                "UPDATE terminal_leases SET state = ?, released_at = ?, metadata_json = ? "
                "WHERE terminal_id = ? AND lease_token = ?",
                (
                    DB_STATE_RELEASED,
                    meta["reaped_at"],
                    json.dumps(meta),
                    T0_TERMINAL,
                    lease_token,
                ),
            )

            result.duration_ms = _now_ms() - started_ms
            self._commit_with_audit(
                conn,
                project_id,
                "t0.kill.verified",
                {
                    "lease_token": lease_token,
                    "pid": pid,
                    "kill_duration_ms": result.duration_ms,
                    "escalated_to_sigkill": result.escalated_to_sigkill,
                },
            )
            result.lease_released = True
            self._release_popen_handle(lease_token)
            return result
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API: force_release_lease (operator escape hatch)
    # ------------------------------------------------------------------

    def force_release_lease(self, lease_token: str, reason: str) -> bool:
        """Operator-action: release a lease by token without process verification.

        Use case: a T0 process is stuck in a state where normal kill() cannot
        proceed (EPERM, sigkill_zombie). Operator takes explicit ownership of
        the lease so a successor can spawn.

        Emits its own audit-event pair (requested + committed). Returns True
        if a leased row was found and released, False otherwise.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                row = self._get_row_by_token(conn, lease_token)
                if row is None or row["state"] != DB_STATE_LEASED:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        log.debug(
                            "force_release: ROLLBACK on no-lease suppressed"
                        )
                    return False

                project_id = row["project_id"]
                meta = self._parse_metadata(row["metadata_json"])
                old_pid = meta.get("pid", -1)

                # Pre-commit audit (separate emit so we can still close
                # the transaction cleanly even if the released-event fails).
                self._emit_event(
                    project_id,
                    "t0.force_release.requested",
                    {
                        "lease_token": lease_token,
                        "pid": old_pid,
                        "reason": reason,
                    },
                )

                meta["lifecycle_state"] = LIFECYCLE_REAPED
                meta["force_released_at"] = _now_iso()
                meta["force_release_reason"] = reason
                conn.execute(
                    "UPDATE terminal_leases SET state = ?, released_at = ?, "
                    "metadata_json = ? "
                    "WHERE terminal_id = ? AND lease_token = ?",
                    (
                        DB_STATE_RELEASED,
                        meta["force_released_at"],
                        json.dumps(meta),
                        T0_TERMINAL,
                        lease_token,
                    ),
                )

                self._commit_with_audit(
                    conn,
                    project_id,
                    "t0.force_release.committed",
                    {
                        "lease_token": lease_token,
                        "pid": old_pid,
                        "reason": reason,
                    },
                )
                self._release_popen_handle(lease_token)
                return True
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Public API: reap_dead_t0s
    # ------------------------------------------------------------------

    def reap_dead_t0s(self) -> List[ReapResult]:
        """Process all STALE leases sequentially.

        For each lease with last_heartbeat_at older than heartbeat_timeout:
            1. liveness-check via os.kill(pid, 0)
            2. if alive → kill() pad (SIGTERM → wait → SIGKILL escalation)
            3. if dead → emit t0.reap.completed + release lease
            4. if liveness-check race (fresh heartbeat) → refuted_alive
        """
        with self._lock:
            return self._reap_locked()

    def _reap_locked(self) -> List[ReapResult]:
        results: List[ReapResult] = []
        threshold = _now_ms() - int(self._heartbeat_timeout_seconds * 1000)

        # Snapshot candidates (release connection before signal calls).
        candidates: List[Dict[str, Any]] = []
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT * FROM terminal_leases "
                "WHERE terminal_id = ? AND state = ? "
                "ORDER BY last_heartbeat_at ASC",
                (T0_TERMINAL, DB_STATE_LEASED),
            )
            for row in cur.fetchall():
                hb = _parse_iso(row["last_heartbeat_at"] or "")
                hb_ms = int(hb.timestamp() * 1000) if hb else 0
                if hb_ms >= threshold:
                    continue  # not stale
                meta = self._parse_metadata(row["metadata_json"])
                candidates.append(
                    {
                        "project_id": row["project_id"],
                        "lease_token": row["lease_token"] or "",
                        "pid": meta.get("pid", -1),
                        "last_heartbeat_ms": hb_ms,
                    }
                )
        finally:
            conn.close()

        for cand in candidates:
            results.append(self._reap_one(cand, threshold))

        return results

    def _reap_one(self, cand: Dict[str, Any], threshold: int) -> ReapResult:
        project_id = cand["project_id"]
        lease_token = cand["lease_token"]
        pid = cand["pid"]

        # Emit stale.detected before any action.
        try:
            self._emit_event(
                project_id,
                "t0.stale.detected",
                {
                    "lease_token": lease_token,
                    "pid": pid,
                    "last_heartbeat_ms": cand["last_heartbeat_ms"],
                },
            )
        except (OSError, RuntimeError, ValueError) as e:
            log.error("reap: stale.detected emit failed: %s", e)
            return ReapResult(
                project_id=project_id,
                lease_token=lease_token,
                pid=pid,
                classification="error",
                error=f"audit_emit_failed: {e}",
            )

        # Liveness check.
        try:
            alive = self._is_alive(pid, lease_token=lease_token) if pid > 0 else False
        except OSError as e:
            log.error("reap: liveness check failed for pid=%s: %s", pid, e)
            return ReapResult(
                project_id=project_id,
                lease_token=lease_token,
                pid=pid,
                classification="error",
                error=f"liveness_check_failed: {e}",
            )

        if not alive:
            # Already-dead path: emit reap.completed + release lease.
            return self._reap_release_dead(project_id, lease_token, pid)

        # Alive: re-check heartbeat (may have refreshed).
        conn = self._connect()
        try:
            cur = conn.execute(
                "SELECT last_heartbeat_at FROM terminal_leases "
                "WHERE terminal_id = ? AND lease_token = ?",
                (T0_TERMINAL, lease_token),
            )
            row = cur.fetchone()
        finally:
            conn.close()

        if row is None:
            return ReapResult(
                project_id=project_id,
                lease_token=lease_token,
                pid=pid,
                classification="error",
                error="lease_already_released",
            )

        fresh_hb = _parse_iso(row["last_heartbeat_at"] or "")
        fresh_ms = int(fresh_hb.timestamp() * 1000) if fresh_hb else 0
        if fresh_ms >= threshold:
            # Heartbeat caught up: refuted_alive.
            try:
                self._emit_event(
                    project_id,
                    "t0.stale.refuted",
                    {"lease_token": lease_token, "pid": pid},
                )
            except (OSError, RuntimeError, ValueError) as e:
                log.error("reap: stale.refuted emit failed: %s", e)
            return ReapResult(
                project_id=project_id,
                lease_token=lease_token,
                pid=pid,
                classification="refuted_alive",
            )

        # Alive + still stale: run kill() as if operator triggered it.
        try:
            self._emit_event(
                project_id,
                "t0.reap.detected",
                {"lease_token": lease_token, "pid": pid},
            )
        except (OSError, RuntimeError, ValueError) as e:
            log.error("reap: reap.detected emit failed: %s", e)

        try:
            kr = self._kill_locked(
                project_id,
                pid,
                lease_token,
                signal_type=signal.SIGTERM,
                wait_timeout=self._kill_wait_timeout_seconds,
                source="reap",
            )
        except LeaseTokenMismatchError as e:
            # Successor lease was spawned concurrently; abandon this iteration.
            return ReapResult(
                project_id=project_id,
                lease_token=lease_token,
                pid=pid,
                classification="error",
                error=f"token_mismatch: {e}",
            )

        return ReapResult(
            project_id=project_id,
            lease_token=lease_token,
            pid=pid,
            classification="killed_by_reap" if kr.verified_dead else "error",
            lease_released=kr.lease_released,
            error=kr.error,
        )

    def _reap_release_dead(
        self,
        project_id: str,
        lease_token: str,
        pid: int,
    ) -> ReapResult:
        """Release a lease whose process is already dead (no kill needed)."""
        self._drain_zombie(pid, lease_token=lease_token)
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = self._get_row_by_token(conn, lease_token)
            if row is None or row["state"] != DB_STATE_LEASED:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    log.debug(
                        "reap_release_dead: ROLLBACK on missing-lease suppressed"
                    )
                return ReapResult(
                    project_id=project_id,
                    lease_token=lease_token,
                    pid=pid,
                    classification="error",
                    error="lease_already_released",
                )

            meta = self._parse_metadata(row["metadata_json"])
            meta["lifecycle_state"] = LIFECYCLE_REAPED
            meta["reaped_at"] = _now_iso()
            meta["reap_classification"] = "already_dead"
            conn.execute(
                "UPDATE terminal_leases SET state = ?, released_at = ?, "
                "metadata_json = ? "
                "WHERE terminal_id = ? AND lease_token = ?",
                (
                    DB_STATE_RELEASED,
                    meta["reaped_at"],
                    json.dumps(meta),
                    T0_TERMINAL,
                    lease_token,
                ),
            )

            self._commit_with_audit(
                conn,
                project_id,
                "t0.reap.completed",
                {
                    "lease_token": lease_token,
                    "pid": pid,
                },
            )
            self._release_popen_handle(lease_token)
            return ReapResult(
                project_id=project_id,
                lease_token=lease_token,
                pid=pid,
                classification="already_dead",
                lease_released=True,
            )
        finally:
            conn.close()
