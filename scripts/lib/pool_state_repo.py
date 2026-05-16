"""pool_state_repo.py — SQLite repository for pool state queries.

Read methods are cacheable; write methods are atomic via BEGIN IMMEDIATE.
No business logic — pure persistence against schema v14 tables:
  pool_config, worker_pools, worker_pool_membership, terminal_leases.

Wave 6 PR-6.3 — ADR-018 elastic worker pool.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from pool_decision_engine import Membership, PoolConfig, PoolDecision, PoolState

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# UUID v7 — time-ordered unique IDs for membership_id / decision_id
# ---------------------------------------------------------------------------

def _uuid7() -> str:
    """Generate a UUID v7 (time-ordered) compatible with Python < 3.14."""
    ts_ms = int(time.time() * 1000)
    rand_bytes = os.urandom(10)
    rand_int = int.from_bytes(rand_bytes, "big")  # 80 bits of randomness

    rand_a = (rand_int >> 68) & 0xFFF        # top 12 bits -> rand_a field
    rand_b = rand_int & 0x3FFFFFFFFFFFFFFF   # low 62 bits -> rand_b field

    val = (
        ((ts_ms & 0xFFFFFFFFFFFF) << 80)
        | (0x7 << 76)      # version = 7
        | (rand_a << 64)   # rand_a (12 bits)
        | (0b10 << 62)     # variant = 10
        | rand_b           # rand_b (62 bits)
    )
    h = f"{val:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _iso_now(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _from_iso(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        s_clean = s.rstrip("Z")
        dt = datetime.fromisoformat(s_clean).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# PoolStateRepository
# ---------------------------------------------------------------------------

class PoolStateRepository:
    """SQLite read/write adapter for pool tables. No business logic."""

    def __init__(self, db_path: Path, project_id: str) -> None:
        self.db_path = db_path
        self.project_id = project_id

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: full autocommit; all transactions are explicit BEGIN … COMMIT.
        # Without this, Python's sqlite3 implicit transaction management can conflict with
        # our explicit BEGIN IMMEDIATE calls when two threads race on the same DB.
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        # Belt-and-suspenders: set busy_timeout via PRAGMA in addition to the connect-level
        # timeout so SQLite retries on SQLITE_BUSY regardless of how the busy handler was
        # previously configured on this connection.
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    def get_config(self, pool_id: str) -> Optional[PoolConfig]:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT pool_id, min_workers, max_workers, scale_policy,
                       provider_mix_json, cooldown_seconds
                FROM pool_config
                WHERE project_id = ? AND pool_id = ?
                """,
                (self.project_id, pool_id),
            ).fetchone()
            if row is None:
                return None
            provider_mix = json.loads(row["provider_mix_json"] or '["claude"]')
            return PoolConfig(
                pool_id=row["pool_id"],
                min_workers=row["min_workers"],
                max_workers=row["max_workers"],
                scaling_policy=row["scale_policy"],
                provider_mix=provider_mix,
                cooldown_seconds=float(row["cooldown_seconds"]),
            )
        finally:
            conn.close()

    def get_state(self, pool_id: str, now: float) -> PoolState:
        conn = self._connect()
        try:
            pool_row = conn.execute(
                """
                SELECT last_scaled_at
                FROM worker_pools
                WHERE project_id = ? AND pool_id = ?
                """,
                (self.project_id, pool_id),
            ).fetchone()

            last_scaled_at: Optional[float] = None
            if pool_row and pool_row["last_scaled_at"]:
                last_scaled_at = _from_iso(pool_row["last_scaled_at"])

            # Count queued dispatches for this project
            try:
                q_row = conn.execute(
                    """
                    SELECT COUNT(*) as cnt
                    FROM dispatches
                    WHERE project_id = ? AND state = 'queued'
                    """,
                    (self.project_id,),
                ).fetchone()
                queue_depth = int(q_row["cnt"]) if q_row else 0
            except sqlite3.OperationalError:
                # dispatches table may not exist in minimal test setups
                queue_depth = 0

            return PoolState(
                queue_depth=queue_depth,
                last_scaled_at=last_scaled_at,
                now=now,
            )
        finally:
            conn.close()

    def list_members(self, pool_id: str) -> List[Membership]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT
                    wpm.id,
                    wpm.terminal_id,
                    wpm.provider,
                    wpm.role,
                    wpm.joined_at,
                    wpm.metadata_json,
                    tl.last_heartbeat_at
                FROM worker_pool_membership wpm
                LEFT JOIN terminal_leases tl
                    ON tl.terminal_id = wpm.terminal_id
                   AND tl.project_id  = wpm.project_id
                WHERE wpm.project_id = ?
                  AND wpm.pool_id    = ?
                  AND wpm.released_at IS NULL
                """,
                (self.project_id, pool_id),
            ).fetchall()

            members: List[Membership] = []
            for row in rows:
                meta = json.loads(row["metadata_json"] or "{}")
                membership_id = meta.get("membership_id", str(row["id"]))
                last_heartbeat = _from_iso(row["last_heartbeat_at"])
                joined_ts = _from_iso(row["joined_at"]) or 0.0
                members.append(
                    Membership(
                        membership_id=membership_id,
                        terminal_id=row["terminal_id"],
                        provider=row["provider"],
                        pool_role=row["role"],
                        status="active",
                        joined_at=joined_ts,
                        last_heartbeat=last_heartbeat,
                    )
                )
            return members
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Write methods — all use BEGIN IMMEDIATE to prevent lost-update
    # ------------------------------------------------------------------

    def _emit_ledger(self, event_type: str, payload: Dict) -> None:
        """Append canonical event to .vnx-data/events/pool_events.ndjson (ADR-005)."""
        events_dir = self.db_path.parent.parent / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        events_file = events_dir / "pool_events.ndjson"
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event_type": event_type,
            "payload": payload,
        }
        with open(events_file, "ab") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write((json.dumps(event) + "\n").encode("utf-8"))
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def add_member(
        self,
        pool_id: str,
        terminal_id: str,
        provider: str,
        role: str,
        now: float,
    ) -> str:
        membership_id = _uuid7()
        joined_iso = _iso_now(now)
        meta = json.dumps({"membership_id": membership_id})
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO worker_pool_membership
                    (terminal_id, project_id, pool_id, provider, role, joined_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (terminal_id, self.project_id, pool_id, provider, role, joined_iso, meta),
            )
            conn.commit()
            log.debug(
                "add_member: pool=%s terminal=%s membership_id=%s",
                pool_id,
                terminal_id,
                membership_id,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._emit_ledger("pool.member.added", {
            "pool_id": pool_id,
            "membership_id": membership_id,
            "terminal_id": terminal_id,
            "provider": provider,
            "role": role,
            "now": now,
        })
        return membership_id

    def mark_member_reaped(
        self,
        membership_id: str,
        reason: str,
        now: float,
    ) -> None:
        released_iso = _iso_now(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE worker_pool_membership
                SET released_at   = ?,
                    release_reason = ?
                WHERE json_extract(metadata_json, '$.membership_id') = ?
                  AND released_at IS NULL
                """,
                (released_iso, reason, membership_id),
            )
            conn.commit()
            log.debug("mark_member_reaped: membership_id=%s reason=%s", membership_id, reason)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._emit_ledger("pool.member.reaped", {
            "membership_id": membership_id,
            "reason": reason,
            "now": now,
        })

    def update_heartbeat(self, membership_id: str, now: float) -> None:
        hb_iso = _iso_now(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE terminal_leases
                SET last_heartbeat_at = ?
                WHERE (terminal_id, project_id) IN (
                    SELECT terminal_id, project_id
                    FROM worker_pool_membership
                    WHERE json_extract(metadata_json, '$.membership_id') = ?
                      AND released_at IS NULL
                )
                """,
                (hb_iso, membership_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_last_scaled_at(self, pool_id: str, now: float) -> None:
        scaled_iso = _iso_now(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE worker_pools
                SET last_scaled_at = ?
                WHERE project_id = ? AND pool_id = ?
                """,
                (scaled_iso, self.project_id, pool_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_decision(
        self,
        pool_id: str,
        decision: PoolDecision,
        now: float,
    ) -> str:
        decision_id = _uuid7()
        scaled_iso = _iso_now(now)
        decision_json = json.dumps(
            {
                "decision_id": decision_id,
                "action": decision.action,
                "delta": decision.delta,
                "reason": decision.reason,
                "targets": list(decision.targets),
                "cooldown_remaining_s": decision.cooldown_remaining_s,
                "recorded_at": scaled_iso,
            }
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE worker_pools
                SET last_decision_json = ?,
                    last_scale_action  = ?
                WHERE project_id = ? AND pool_id = ?
                """,
                (
                    decision_json,
                    decision.action,
                    self.project_id,
                    pool_id,
                ),
            )
            conn.commit()
            log.debug(
                "record_decision: pool=%s action=%s decision_id=%s",
                pool_id,
                decision.action,
                decision_id,
            )
            return decision_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_current_size(self, pool_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) as cnt
                FROM worker_pool_membership
                WHERE project_id = ? AND pool_id = ? AND released_at IS NULL
                """,
                (self.project_id, pool_id),
            ).fetchone()
            return int(row["cnt"]) if row else 0
        finally:
            conn.close()

    def update_pool_size(self, pool_id: str, current_size: int) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE worker_pools
                SET current_size = ?
                WHERE project_id = ? AND pool_id = ?
                """,
                (current_size, self.project_id, pool_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_config(self, pool_id: str, updates: Dict) -> None:
        """Update pool_config fields. Keys map to PoolConfig field names."""
        _FIELD_MAP = {
            "min_workers": "min_workers",
            "max_workers": "max_workers",
            "scaling_policy": "scale_policy",
            "cooldown_seconds": "cooldown_seconds",
        }
        cols: List[str] = []
        vals: List = []
        for key, value in updates.items():
            col = _FIELD_MAP.get(key)
            if col is None:
                raise ValueError(f"Unknown config field: {key}")
            cols.append(f"{col} = ?")
            vals.append(value)

        if not cols:
            return

        now_iso = _iso_now(time.time())
        cols.append("updated_at = ?")
        vals.append(now_iso)
        vals.extend([self.project_id, pool_id])

        sql = f"UPDATE pool_config SET {', '.join(cols)} WHERE project_id = ? AND pool_id = ?"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql, vals)
            conn.commit()
            log.debug("update_config: pool=%s updates=%s", pool_id, updates)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
