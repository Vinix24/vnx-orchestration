"""plan_gate_effectiveness_probe — read-only health probe for the
plan-gate-panel subsystem (framework-status-audit-and-cockpit PR-7).

Three REAL, persisted signal sources (kimi finding — name the source):

1. ``.vnx-attest/plan-gates.ndjson`` — the same panel attestation ledger the
   governance probe verifies, read here for its ``resolver`` field. ``resolver``
   is a closed ``run`` | ``attest`` choice (``plan_gate_evidence``): the durable
   proxy for whether a track's plan gate converged organically via the panel run
   (``"run"``) or needed a manual operator override (``"attest"`` —
   ``planning_cli.py``'s ``plan-gate attest`` command, the escape hatch for a
   panel that did not converge on its own).
2. ``.vnx-attest/plan-gate-seats.ndjson`` — the per-seat verdict ledger (OI-888).
   ``plan_gate_panel.run_panel`` appends one append-only, hash-chained
   ``plan_gate_seat`` record per panelist per run (panelist id, model, effective
   verdict incl. abstain, and whether a report was returned at all). The
   per-panelist pass/revise/block counts previously survived nowhere — only the
   final resolved ``plan_gate_pass`` record was durable — so a degraded seat (3
   of 5 responding, say) was only visible during a live run.
3. The ``OI-PLAN-<track>`` blocker rows in ``track_open_items`` (the runtime
   coordination DB), under ``VNX_DATA_DIR`` — the same table
   ``plan_gate_enforcement.plan_gate_state()`` reads per-track, queried here
   in aggregate across every track.

Health is `ok` when gates resolve without a stuck backlog and organic panel
convergence is not swamped by manual overrides; `degraded` when a blocker has
sat open past the staleness window, or every gate on record was resolved by
manual attest (the panel itself never converged unassisted) — regardless of the
OI-PLAN resolution count, because the ledger is the durable record of whether
the panel converged (OI-888). Both read as "panel verdicts disagree" in the
PRD's vocabulary.

Scope-skip signal (OI-888): the ``VNX_PLAN_GATE_COMPLEX_ONLY`` read-site now
exists in ``plan_gate_enforcement.plan_gate_scope`` + ``complex_only_active``
(2026-08-08 dispatch) — a LIGHT-scope plan under the flag runs the reduced
2-seat panel. The probe reports that explicitly (``scope_skip_read_site:
"present"``) instead of staying silent about the signal.
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
SEAT_LEDGER_RELPATH = ".vnx-attest/plan-gate-seats.ndjson"
COORDINATION_DB_FILENAME = "runtime_coordination.db"
STALE_DAYS = 7
_PLAN_OI_PREFIX = "OI-PLAN-"
# The signal fields that count as "activity" for the unknown-vs-anything split.
# ``scope_skip_read_site`` is deliberately excluded: its value is a constant
# string (truthy) and would otherwise make every state look active.
_ACTIVITY_KEYS = (
    "ledger_total",
    "oi_plan_unresolved",
    "oi_plan_stale_unresolved",
    "oi_plan_resolved",
    "seat_total",
)


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

    def _seat_ledger_path(self) -> Path:
        return self._repo_root / SEAT_LEDGER_RELPATH

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

        seat_total = 0
        seat_responded = 0
        seat_abstain = 0
        seat_pass = 0
        seat_revise = 0
        seat_block = 0
        seat_path = self._seat_ledger_path()
        if seat_path.exists():
            for _line_no, entry, _hash in walk_chain(seat_path):
                if not isinstance(entry, dict) or entry.get("type") != "plan_gate_seat":
                    continue
                seat_total += 1
                if entry.get("responded"):
                    seat_responded += 1
                verdict = entry.get("verdict")
                if verdict == "pass":
                    seat_pass += 1
                elif verdict == "revise":
                    seat_revise += 1
                elif verdict == "block":
                    seat_block += 1
                elif verdict == "abstain":
                    seat_abstain += 1

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
            "seat_total": seat_total,
            "seat_responded": seat_responded,
            "seat_abstain": seat_abstain,
            "seat_pass": seat_pass,
            "seat_revise": seat_revise,
            "seat_block": seat_block,
            "scope_skip_read_site": "present",
        }

    def signal(self, raw: Dict[str, Any]) -> str:
        if not any(raw.get(k) for k in _ACTIVITY_KEYS):
            return "no plan-gate activity yet (no ledger records, no OI-PLAN blockers, no seat records)"
        parts = [
            f"{raw['ledger_total']} plan-gate-pass record(s) "
            f"({raw['ledger_attest_count']} via manual attest)",
            f"{raw['oi_plan_unresolved']} unresolved OI-PLAN blocker(s) "
            f"({raw['oi_plan_stale_unresolved']} stale >{STALE_DAYS}d), "
            f"{raw['oi_plan_resolved']} resolved",
        ]
        if raw["seat_total"]:
            parts.append(
                f"{raw['seat_total']} seat record(s) "
                f"({raw['seat_responded']} responded, {raw['seat_abstain']} abstained, "
                f"{raw['seat_pass']} pass/{raw['seat_revise']} revise/{raw['seat_block']} block)"
            )
        parts.append("VNX_PLAN_GATE_COMPLEX_ONLY scope-skip read-site present (light plans run a reduced panel)")
        return "; ".join(parts)

    def health(self, raw: Dict[str, Any]) -> str:
        if not any(raw.get(k) for k in _ACTIVITY_KEYS):
            return "unknown"
        if raw["oi_plan_stale_unresolved"] > 0:
            return "degraded"
        if raw["ledger_total"] > 0 and raw["ledger_attest_count"] == raw["ledger_total"]:
            # OI-888: every gate on record was resolved by manual attest, so the
            # panel itself never converged unassisted. This must fire regardless of
            # how many OI-PLAN blockers are currently resolved — the ledger is the
            # durable record of whether the panel converged organically.
            return "degraded"
        return "ok"


__all__ = [
    "PlanGateEffectivenessProbe",
    "LEDGER_RELPATH",
    "SEAT_LEDGER_RELPATH",
    "COORDINATION_DB_FILENAME",
    "STALE_DAYS",
]
