"""migration_effectiveness_probe — read-only health probe for the
migration-mechanisms subsystem (framework-status-audit-and-cockpit PR-7).

Reads the declarative invariant manifest (``schema_manifest.py``, PR-A2) and
compares it against the actual runtime coordination DB's claimed
``PRAGMA user_version``: does the DB's claimed version match a real manifest
entry, does that entry's invariants fully hold, and how far behind the
terminal (highest known) manifest version is it.

This mirrors what ``schema_manifest.reconcile_user_version()`` already checks
at migration-walk time, but read-only and outside that write path: a probe run
never mutates ``user_version`` or any table (ADR-007 scope statement, PR-5 —
no new central-DB table, no write beyond the beacon).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import project_root  # noqa: E402
import schema_manifest  # noqa: E402
from effectiveness_probe import EffectivenessProbe, register_probe  # noqa: E402

COORDINATION_DB_FILENAME = "runtime_coordination.db"


@register_probe("migration-mechanisms")
class MigrationEffectivenessProbe(EffectivenessProbe):
    """Read-only over the runtime coordination DB's ``PRAGMA user_version`` vs
    ``schema_manifest.SCHEMA_MANIFEST``. No new central-DB table (ADR-007 scope
    statement, PR-5)."""

    subsystem = "migration-mechanisms"

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self._state_dir = Path(state_dir) if state_dir else project_root.resolve_state_dir(__file__)

    def _db_path(self) -> Path:
        return self._state_dir / COORDINATION_DB_FILENAME

    def probe(self) -> Dict[str, Any]:
        path = self._db_path()
        if not path.exists():
            return {"db_exists": False}

        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
        try:
            claimed = conn.execute("PRAGMA user_version").fetchone()[0]
            terminal = schema_manifest.TERMINAL_VERSION
            candidates = [v for v in schema_manifest.SCHEMA_MANIFEST if v <= claimed]
            effective = max(candidates) if candidates else None
            if effective is None:
                return {
                    "db_exists": True,
                    "claimed_version": claimed,
                    "terminal_version": terminal,
                    "effective_version": None,
                    "violation_count": 0,
                    "violations_sample": [],
                }
            violations = schema_manifest.validate_db_at_version(conn, effective)
            return {
                "db_exists": True,
                "claimed_version": claimed,
                "terminal_version": terminal,
                "effective_version": effective,
                "violation_count": len(violations),
                "violations_sample": list(violations[:3]),
            }
        finally:
            conn.close()

    def signal(self, raw: Dict[str, Any]) -> str:
        if not raw["db_exists"]:
            return "no runtime coordination DB yet"
        if raw["effective_version"] is None:
            return f"user_version={raw['claimed_version']} predates the manifest floor (v{schema_manifest.MIN_VERSION})"
        if raw["violation_count"]:
            return (
                f"user_version={raw['claimed_version']} FAILS its v{raw['effective_version']} "
                f"invariant manifest ({raw['violation_count']} violation(s))"
            )
        lag = raw["terminal_version"] - raw["claimed_version"]
        if lag > 0:
            return (
                f"user_version={raw['claimed_version']} valid, {lag} version(s) behind "
                f"terminal v{raw['terminal_version']}"
            )
        return f"user_version={raw['claimed_version']} matches terminal v{raw['terminal_version']}, manifest holds"

    def health(self, raw: Dict[str, Any]) -> str:
        if not raw["db_exists"] or raw["effective_version"] is None:
            return "unknown"
        if raw["violation_count"] > 0:
            return "produces_crap"
        if raw["claimed_version"] < raw["terminal_version"]:
            return "degraded"
        return "ok"


__all__ = ["MigrationEffectivenessProbe", "COORDINATION_DB_FILENAME"]
