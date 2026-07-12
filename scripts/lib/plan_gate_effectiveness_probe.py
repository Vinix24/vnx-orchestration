"""plan_gate_effectiveness_probe — read-only health probe for the
plan-gate-panel subsystem (framework-status-audit-and-cockpit PR-7).

Two REAL, persisted signal sources (kimi finding — name the source):

1. ``.vnx-attest/plan-gates.ndjson`` — the same panel attestation ledger the
   governance probe verifies, read here for its ``resolver`` field. Per-panelist
   pass/revise/block counts are NOT persisted anywhere (only the final resolved
   ``plan_gate_pass`` record survives — see ``plan_gate_panel.run_panel`` /
   ``planning_cli.py``); ``resolver`` is the only durable proxy for whether a
   track's plan gate converged organically via the panel run (``"run"``) or
   needed a manual operator override (``"attest"`` — ``planning_cli.py``'s
   ``plan-gate attest`` command, the escape hatch for a panel that did not
   converge on its own).
2. The ``OI-PLAN-<track>`` blocker rows in ``track_open_items`` (the runtime
   coordination DB), under ``VNX_DATA_DIR`` — the same table
   ``plan_gate_enforcement.plan_gate_state()`` reads per-track, queried here
   in aggregate across every track.

Health is `ok` when gates resolve without a stuck backlog and organic panel
convergence is not swamped by manual overrides; `degraded` when a blocker has
sat open past the staleness window, or every resolved gate on record required a
manual attest (the panel itself never converged unassisted) — both read as
"panel verdicts disagree" in the PRD's vocabulary.

Deliberately NOT checked (kimi finding): whether complex tracks skip the panel
(``VNX_PLAN_GATE_COMPLEX_ONLY``) — the scope-skip read-site does not exist yet
(deferred to ``review-floor-enforcer``), so there is no signal to probe.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import project_root  # noqa: E402
from effectiveness_probe import EffectivenessProbe, register_probe  # noqa: E402
from ndjson_hash_chain import walk_chain  # noqa: E402

LEDGER_RELPATH = ".vnx-attest/plan-gates.ndjson"
COORDINATION_DB_FILENAME = "runtime_coordination.db"
STALE_DAYS = 7
_PLAN_OI_PREFIX = "OI-PLAN-"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


@register_probe("plan-gate-panel")
class PlanGateEffectivenessProbe(EffectivenessProbe):
    """Read-only over ``.vnx-attest/plan-gates.ndjson`` and ``track_open_items``.
    No new central-DB table (ADR-007 scope statement, PR-5)."""

    subsystem = "plan-gate-panel"

    def __init__(self, repo_root: Optional[Path] = None, state_dir: Optional[Path] = None) -> None:
        self._repo_root = Path(repo_root) if repo_root else project_root.resolve_project_root(__file__)
        self._state_dir = Path(state_dir) if state_dir else project_root.resolve_state_dir(__file__)

    def _ledger_path(self) -> Path:
        return self._repo_root / LEDGER_RELPATH

    def _db_path(self) -> Path:
        return self._state_dir / COORDINATION_DB_FILENAME

    def probe(self) -> Dict[str, Any]:
        ledger_total = 0
        ledger_attest_count = 0
        ledger_path = self._ledger_path()
        if ledger_path.exists():
            for _line_no, entry, _hash in walk_chain(ledger_path):
                if not isinstance(entry, dict) or entry.get("type") != "plan_gate_pass":
                    continue
                ledger_total += 1
                if entry.get("resolver") == "attest":
                    ledger_attest_count += 1

        oi_plan_unresolved = 0
        oi_plan_stale_unresolved = 0
        oi_plan_resolved = 0
        db_path = self._db_path()
        if db_path.exists():
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
            try:
                if _has_table(conn, "track_open_items") and _has_col(conn, "track_open_items", "resolved_at"):
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)).isoformat()
                    rows = conn.execute(
                        "SELECT linked_at, resolved_at FROM track_open_items "
                        "WHERE oi_id LIKE ? AND link_type='blocks'",
                        (f"{_PLAN_OI_PREFIX}%",),
                    ).fetchall()
                    for linked_at, resolved_at in rows:
                        if resolved_at:
                            oi_plan_resolved += 1
                        else:
                            oi_plan_unresolved += 1
                            if linked_at and str(linked_at) < cutoff:
                                oi_plan_stale_unresolved += 1
            finally:
                conn.close()

        return {
            "ledger_total": ledger_total,
            "ledger_attest_count": ledger_attest_count,
            "oi_plan_unresolved": oi_plan_unresolved,
            "oi_plan_stale_unresolved": oi_plan_stale_unresolved,
            "oi_plan_resolved": oi_plan_resolved,
        }

    def signal(self, raw: Dict[str, Any]) -> str:
        if not any(raw.values()):
            return "no plan-gate activity yet (no ledger records, no OI-PLAN blockers)"
        return (
            f"{raw['ledger_total']} plan-gate-pass record(s) "
            f"({raw['ledger_attest_count']} via manual attest); "
            f"{raw['oi_plan_unresolved']} unresolved OI-PLAN blocker(s) "
            f"({raw['oi_plan_stale_unresolved']} stale >{STALE_DAYS}d), "
            f"{raw['oi_plan_resolved']} resolved"
        )

    def health(self, raw: Dict[str, Any]) -> str:
        if not any(raw.values()):
            return "unknown"
        if raw["oi_plan_stale_unresolved"] > 0:
            return "degraded"
        if raw["ledger_total"] > 0 and raw["ledger_attest_count"] == raw["ledger_total"] and raw["oi_plan_resolved"] > 0:
            return "degraded"
        return "ok"


__all__ = ["PlanGateEffectivenessProbe", "LEDGER_RELPATH", "COORDINATION_DB_FILENAME", "STALE_DAYS"]
