"""Tests for scripts/lib/plan_gate_effectiveness_probe.py
(framework-status-audit-and-cockpit PR-7).

Dispatch-ID: 20260712-185712-cockpit-pr7
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from effectiveness_probe import EFFECTIVENESS_PROBES  # noqa: E402
from ndjson_hash_chain import append_chained_entry  # noqa: E402
from plan_gate_effectiveness_probe import (  # noqa: E402
    COORDINATION_DB_FILENAME,
    PlanGateEffectivenessProbe,
)


def _ledger_path(repo_root: Path) -> Path:
    return repo_root / ".vnx-attest" / "plan-gates.ndjson"


def _make_db(state_dir: Path, rows) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / COORDINATION_DB_FILENAME))
    conn.execute(
        "CREATE TABLE track_open_items (track_id TEXT, project_id TEXT, oi_id TEXT, "
        "link_type TEXT, linked_at TEXT, resolved_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO track_open_items "
        "(track_id, project_id, oi_id, link_type, linked_at, resolved_at) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_registered_under_plan_gate_panel():
    assert EFFECTIVENESS_PROBES["plan-gate-panel"] is PlanGateEffectivenessProbe


def test_unknown_when_nothing_exists(tmp_path):
    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=tmp_path / "state").run()
    assert result.status == "unknown"


def test_fresh_unresolved_blocker_alone_is_ok_not_degraded(tmp_path):
    """A newly-seeded OI-PLAN blocker is expected transient state (every track is
    'born plan-gated') — not, by itself, a sign of trouble."""
    state_dir = tmp_path / "state"
    now = datetime.now(timezone.utc).isoformat()
    _make_db(state_dir, [("t1", "p", "OI-PLAN-t1", "blocks", now, None)])

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=state_dir).run()

    assert result.status == "ok"
    assert result.detail["oi_plan_unresolved"] == 1
    assert result.detail["oi_plan_stale_unresolved"] == 0


def test_stale_unresolved_blocker_is_degraded(tmp_path):
    state_dir = tmp_path / "state"
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _make_db(state_dir, [("t1", "p", "OI-PLAN-t1", "blocks", old, None)])

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=state_dir).run()

    assert result.status == "degraded"
    assert result.detail["oi_plan_stale_unresolved"] == 1


def test_all_manual_attest_resolutions_is_degraded(tmp_path):
    """Every ledger record needing a manual operator override — never an organic
    panel convergence — reads as the panel not converging on its own."""
    ledger = _ledger_path(tmp_path)
    append_chained_entry(ledger, {"type": "plan_gate_pass", "track_id": "t1", "resolver": "attest"})

    state_dir = tmp_path / "state"
    now = datetime.now(timezone.utc).isoformat()
    _make_db(state_dir, [("t1", "p", "OI-PLAN-t1", "blocks", now, now)])

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=state_dir).run()

    assert result.status == "degraded"
    assert result.detail["ledger_attest_count"] == result.detail["ledger_total"] == 1


def test_mixed_resolver_with_no_stale_backlog_is_ok(tmp_path):
    ledger = _ledger_path(tmp_path)
    append_chained_entry(ledger, {"type": "plan_gate_pass", "track_id": "t1", "resolver": "run"})
    append_chained_entry(ledger, {"type": "plan_gate_pass", "track_id": "t2", "resolver": "attest"})

    state_dir = tmp_path / "state"
    now = datetime.now(timezone.utc).isoformat()
    _make_db(state_dir, [
        ("t1", "p", "OI-PLAN-t1", "blocks", now, now),
        ("t2", "p", "OI-PLAN-t2", "blocks", now, now),
    ])

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=state_dir).run()

    assert result.status == "ok"


def test_default_construction_resolves_real_paths_without_crashing():
    result = PlanGateEffectivenessProbe().run()
    assert result.status in {"ok", "degraded", "produces_crap", "unknown"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
