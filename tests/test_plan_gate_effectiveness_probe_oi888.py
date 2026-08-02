"""OI-888 regression + coverage for plan_gate_effectiveness_probe.py.

Gap 1 (regression, red on origin/main): the all-manual-attest degraded clause
required ``oi_plan_resolved > 0``, so the real state — 55 ledger records, all
``attest``, zero resolved OI-PLAN blockers — reported ``ok``. ``degraded`` must
fire whenever every gate on record was resolved by manual attest, regardless of
the OI-PLAN resolution count.

Gap 2 (readability): the probe now reads the per-seat verdict ledger
``.vnx-attest/plan-gate-seats.ndjson`` (persisted by plan_gate_panel.run_panel).

Gap 3 (visibility): the probe reports that the ``VNX_PLAN_GATE_COMPLEX_ONLY``
scope-skip read-site is missing instead of staying silent.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from ndjson_hash_chain import append_chained_entry  # noqa: E402
from plan_gate_effectiveness_probe import (  # noqa: E402
    COORDINATION_DB_FILENAME,
    PlanGateEffectivenessProbe,
)


def _ledger_path(repo_root: Path) -> Path:
    return repo_root / ".vnx-attest" / "plan-gates.ndjson"


def _seat_ledger_path(repo_root: Path) -> Path:
    return repo_root / ".vnx-attest" / "plan-gate-seats.ndjson"


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


def _seed_attest_ledger(repo_root: Path, count: int = 55) -> None:
    ledger = _ledger_path(repo_root)
    for i in range(count):
        append_chained_entry(ledger, {
            "type": "plan_gate_pass", "track_id": f"t{i}", "resolver": "attest",
        })


# ---------------------------------------------------------------------------
# Gap 1 — the real 55/55/0 state must be degraded (regression, red on origin/main)
# ---------------------------------------------------------------------------

def test_all_attest_zero_resolved_blockers_is_degraded(tmp_path):
    """The measured real state (55 ledger records, 55 attest, 0 resolved OI-PLAN
    blockers) must read as degraded — the panel never converged unassisted."""
    _seed_attest_ledger(tmp_path, count=55)
    state_dir = tmp_path / "state"
    now = datetime.now(timezone.utc).isoformat()
    _make_db(state_dir, [("t0", "p", "OI-PLAN-t0", "blocks", now, None)])

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=state_dir).run()

    assert result.detail["ledger_total"] == 55
    assert result.detail["ledger_attest_count"] == 55
    assert result.detail["oi_plan_resolved"] == 0
    assert result.status == "degraded"


def test_all_attest_with_no_db_at_all_is_degraded(tmp_path):
    """All-attest is degraded even when there are no OI-PLAN rows at all — the
    ledger itself is the durable record that every gate needed a manual override."""
    _seed_attest_ledger(tmp_path, count=3)

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=tmp_path / "state").run()

    assert result.detail["ledger_total"] == 3
    assert result.detail["ledger_attest_count"] == 3
    assert result.status == "degraded"


def test_all_attest_after_fix_still_degraded_with_resolved_backlog(tmp_path):
    """The pre-existing 'all attest with resolved items' scenario stays degraded."""
    _seed_attest_ledger(tmp_path, count=1)
    state_dir = tmp_path / "state"
    now = datetime.now(timezone.utc).isoformat()
    _make_db(state_dir, [("t1", "p", "OI-PLAN-t1", "blocks", now, now)])

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=state_dir).run()

    assert result.status == "degraded"


def test_mixed_resolvers_with_no_stale_backlog_still_ok(tmp_path):
    """A single organic run keeps health ok — the panel does converge sometimes."""
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


# ---------------------------------------------------------------------------
# Gap 2 — the probe reads the per-seat verdict ledger
# ---------------------------------------------------------------------------

def test_probe_reads_seat_ledger(tmp_path):
    seat_ledger = _seat_ledger_path(tmp_path)
    append_chained_entry(seat_ledger, {
        "type": "plan_gate_seat", "panelist_id": "opus", "model": "opus",
        "verdict": "pass", "responded": True, "parse_error": False,
    })
    append_chained_entry(seat_ledger, {
        "type": "plan_gate_seat", "panelist_id": "kimi", "model": "kimi-k3",
        "verdict": "abstain", "responded": False, "parse_error": False,
    })

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=tmp_path / "state").run()

    assert result.detail["seat_total"] == 2
    assert result.detail["seat_responded"] == 1
    assert result.detail["seat_abstain"] == 1
    assert result.detail["seat_pass"] == 1
    assert result.detail["seat_revise"] == 0
    assert result.detail["seat_block"] == 0


def test_probe_signal_includes_seat_counts_when_present(tmp_path):
    seat_ledger = _seat_ledger_path(tmp_path)
    append_chained_entry(seat_ledger, {
        "type": "plan_gate_seat", "panelist_id": "opus", "model": "opus",
        "verdict": "pass", "responded": True, "parse_error": False,
    })

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=tmp_path / "state").run()

    assert "1 seat record(s)" in result.signal
    assert "1 responded" in result.signal


# ---------------------------------------------------------------------------
# Gap 3 — the missing scope-skip read-site is reported, not silent
# ---------------------------------------------------------------------------

def test_probe_reports_missing_scope_skip_read_site_in_detail(tmp_path):
    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=tmp_path / "state").run()

    assert result.detail.get("scope_skip_read_site") == "missing"


def test_probe_signal_mentions_missing_scope_skip_read_site(tmp_path):
    append_chained_entry(_ledger_path(tmp_path), {
        "type": "plan_gate_pass", "track_id": "t1", "resolver": "run",
    })

    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=tmp_path / "state").run()

    assert "VNX_PLAN_GATE_COMPLEX_ONLY read-site MISSING" in result.signal


def test_unknown_state_still_reported_when_nothing_exists(tmp_path):
    result = PlanGateEffectivenessProbe(repo_root=tmp_path, state_dir=tmp_path / "state").run()

    assert result.status == "unknown"
    assert "no plan-gate activity yet" in result.signal


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
