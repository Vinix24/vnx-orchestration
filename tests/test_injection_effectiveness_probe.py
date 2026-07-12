"""Tests for scripts/lib/injection_effectiveness_probe.py — the intelligence
self-learning-loop effectiveness probe (framework-status-audit-and-cockpit PR-6).

Dispatch-ID: 20260712-185055-cockpit-pr6
"""
from __future__ import annotations

import json
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
from injection_effectiveness_probe import (  # noqa: E402
    DEGRADED_THRESHOLD,
    PRODUCES_CRAP_THRESHOLD,
    STALL_THRESHOLD_DAYS,
    InjectionEffectivenessProbe,
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_db(tmp_path: Path, rows=(), dream_cycles=()) -> Path:
    """Create a quality_intelligence.db with a pattern_usage table (matching the
    schema learning_loop.py creates) and a dream_cycles table (matching
    schemas/migrations/0025_dream_consolidation.sql), pre-populated with rows."""
    db_path = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT NOT NULL,
                pattern_hash TEXT NOT NULL,
                used_count INTEGER DEFAULT 0,
                ignored_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0,
                last_used TIMESTAMP,
                last_offered TIMESTAMP,
                confidence REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for i, (used, ignored) in enumerate(rows):
            conn.execute(
                "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, "
                "used_count, ignored_count) VALUES (?, ?, ?, ?, ?)",
                (f"pat-{i}", f"Pattern {i}", f"hash-{i}", used, ignored),
            )
        conn.execute(
            """
            CREATE TABLE dream_cycles (
                cycle_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                started_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (cycle_id, project_id)
            )
            """
        )
        for i, started_at in enumerate(dream_cycles):
            conn.execute(
                "INSERT INTO dream_cycles (cycle_id, started_at) VALUES (?, ?)",
                (f"cycle-{i}", started_at),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _write_pending_rules(state_dir: Path, entries) -> None:
    (state_dir / "pending_rules.json").write_text(
        json.dumps({"pending_rules": entries}), encoding="utf-8"
    )


def _write_pending_refinements(state_dir: Path, entries) -> None:
    (state_dir / "pending_skill_refinements.json").write_text(
        json.dumps({"generated_at": _iso(datetime.now(timezone.utc)), "proposals": entries}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# health() classification — total function of ignore_rate + stall signal
# ---------------------------------------------------------------------------


def test_no_data_reports_unknown(tmp_path):
    _make_db(tmp_path, rows=())
    probe = InjectionEffectivenessProbe(state_dir=tmp_path)

    result = probe.run()

    assert result.status == "unknown"
    assert result.detail["ignore_rate"] is None
    assert result.detail["used_count"] == 0
    assert result.detail["ignored_count"] == 0


def test_no_db_at_all_reports_unknown(tmp_path):
    probe = InjectionEffectivenessProbe(state_dir=tmp_path)

    result = probe.run()

    assert result.status == "unknown"


def test_high_ignore_rate_reports_produces_crap(tmp_path):
    _make_db(tmp_path, rows=[(2, 98)])  # ignore_rate = 0.98
    probe = InjectionEffectivenessProbe(state_dir=tmp_path)

    result = probe.run()

    assert result.status == "produces_crap"
    assert result.detail["ignore_rate"] == pytest.approx(0.98)


def test_ignore_rate_exactly_at_produces_crap_threshold_is_produces_crap(tmp_path):
    _make_db(tmp_path, rows=[(10, 90)])  # ignore_rate = 0.90 exactly
    probe = InjectionEffectivenessProbe(state_dir=tmp_path)

    result = probe.run()

    assert result.detail["ignore_rate"] == pytest.approx(PRODUCES_CRAP_THRESHOLD)
    assert result.status == "produces_crap"


def test_mid_ignore_rate_reports_degraded(tmp_path):
    _make_db(tmp_path, rows=[(40, 60)])  # ignore_rate = 0.60
    probe = InjectionEffectivenessProbe(state_dir=tmp_path)

    result = probe.run()

    assert result.status == "degraded"


def test_ignore_rate_exactly_at_degraded_threshold_is_degraded(tmp_path):
    _make_db(tmp_path, rows=[(50, 50)])  # ignore_rate = 0.50 exactly
    probe = InjectionEffectivenessProbe(state_dir=tmp_path)

    result = probe.run()

    assert result.detail["ignore_rate"] == pytest.approx(DEGRADED_THRESHOLD)
    assert result.status == "degraded"


def test_balanced_low_ignore_rate_reports_ok(tmp_path):
    _make_db(tmp_path, rows=[(80, 20)])  # ignore_rate = 0.20
    probe = InjectionEffectivenessProbe(state_dir=tmp_path)

    result = probe.run()

    assert result.status == "ok"
    assert result.detail["ignore_rate"] == pytest.approx(0.20)


def test_low_ignore_rate_but_stalled_proposals_reports_degraded(tmp_path):
    _make_db(tmp_path, rows=[(80, 20)])  # ignore_rate = 0.20, would be ok alone
    stale_ts = _iso(datetime.now(timezone.utc) - timedelta(days=STALL_THRESHOLD_DAYS + 1))
    _write_pending_rules(
        tmp_path,
        [{"id": "r1", "status": "pending", "created_at": stale_ts, "pattern": "x", "prevention": "y"}],
    )

    probe = InjectionEffectivenessProbe(state_dir=tmp_path)
    result = probe.run()

    assert result.status == "degraded"
    assert result.detail["pending_proposals"] == 1
    assert result.detail["oldest_pending_age_days"] > STALL_THRESHOLD_DAYS


def test_low_ignore_rate_with_fresh_pending_proposals_reports_ok(tmp_path):
    _make_db(tmp_path, rows=[(80, 20)])  # ignore_rate = 0.20
    fresh_ts = _iso(datetime.now(timezone.utc) - timedelta(days=1))
    _write_pending_rules(
        tmp_path,
        [{"id": "r1", "status": "pending", "created_at": fresh_ts, "pattern": "x", "prevention": "y"}],
    )

    probe = InjectionEffectivenessProbe(state_dir=tmp_path)
    result = probe.run()

    assert result.status == "ok"
    assert result.detail["pending_proposals"] == 1


def test_non_pending_proposals_are_not_counted_or_stalling(tmp_path):
    _make_db(tmp_path, rows=[(80, 20)])
    stale_ts = _iso(datetime.now(timezone.utc) - timedelta(days=STALL_THRESHOLD_DAYS + 5))
    _write_pending_rules(
        tmp_path,
        [{"id": "r1", "status": "ingested", "created_at": stale_ts, "pattern": "x", "prevention": "y"}],
    )

    probe = InjectionEffectivenessProbe(state_dir=tmp_path)
    result = probe.run()

    assert result.status == "ok"
    assert result.detail["pending_proposals"] == 0


def test_skill_refinement_proposals_also_count_toward_pending_and_staleness(tmp_path):
    _make_db(tmp_path, rows=[(80, 20)])
    stale_ts = _iso(datetime.now(timezone.utc) - timedelta(days=STALL_THRESHOLD_DAYS + 3))
    _write_pending_refinements(
        tmp_path,
        [{"id": "skillref-1", "status": "pending", "generated_at": stale_ts, "role": "backend"}],
    )

    probe = InjectionEffectivenessProbe(state_dir=tmp_path)
    result = probe.run()

    assert result.status == "degraded"
    assert result.detail["pending_proposals"] == 1


# ---------------------------------------------------------------------------
# beacon detail contract
# ---------------------------------------------------------------------------


def test_beacon_detail_contains_required_keys(tmp_path):
    started_at = _iso(datetime.now(timezone.utc))
    _make_db(tmp_path, rows=[(80, 20)], dream_cycles=[started_at])

    probe = InjectionEffectivenessProbe(state_dir=tmp_path)
    result = probe.run()

    for key in ("ignore_rate", "used_count", "ignored_count", "pending_proposals", "last_dream_cycle_iso"):
        assert key in result.detail
    assert result.detail["last_dream_cycle_iso"] == started_at


def test_missing_pattern_usage_table_is_treated_as_no_data(tmp_path):
    # A DB file exists but never had pattern_usage created (e.g. fresh install).
    db_path = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.close()

    probe = InjectionEffectivenessProbe(state_dir=tmp_path)
    result = probe.run()

    assert result.status == "unknown"
    assert result.detail["used_count"] == 0
    assert result.detail["ignored_count"] == 0


def test_corrupt_pending_rules_json_is_ignored_not_raised(tmp_path):
    _make_db(tmp_path, rows=[(80, 20)])
    (tmp_path / "pending_rules.json").write_text("{not valid json", encoding="utf-8")

    probe = InjectionEffectivenessProbe(state_dir=tmp_path)
    result = probe.run()

    assert result.status == "ok"
    assert result.detail["pending_proposals"] == 0


# ---------------------------------------------------------------------------
# measurement only — no activation side effects
# ---------------------------------------------------------------------------


def test_probe_never_writes_to_the_database_it_reads(tmp_path):
    db_path = _make_db(tmp_path, rows=[(5, 95)])
    before = db_path.read_bytes()

    InjectionEffectivenessProbe(state_dir=tmp_path).run()

    assert db_path.read_bytes() == before


def test_probe_is_registered_under_intelligence_self_learning_loop():
    assert EFFECTIVENESS_PROBES["intelligence-self-learning-loop"] is InjectionEffectivenessProbe


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
