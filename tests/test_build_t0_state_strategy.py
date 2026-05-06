"""Tests for _build_strategic_state / _build_strategic_state_heavy (W-state-5).

Covers:
  1. Happy path — strategy/ with full roadmap + decisions + indexes
  2. Missing strategy/ folder → available=false, no crash
  3. Malformed roadmap.yaml → degrades gracefully (roadmap=None, others may load)
  4. Missing decisions.ndjson → empty recent_decisions list
  5. Heavy detail caps decisions at 20 even when log holds more
  6. Light light caps decisions at 5
  7. Budget guard — single-call build budget < 200ms on representative fixture
  8. Integration: full build_t0_state output dict carries strategic_state +
     _strategic_state_heavy keys (the latter consumed by the detail writer)
  9. Detail-section map routes _strategic_state_heavy → t0_detail/strategic_state.json
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from build_t0_state import (  # noqa: E402
    _DETAIL_SECTION_MAP,
    _build_strategic_state,
    _build_strategic_state_heavy,
    _write_detail_files,
    build_t0_state,
)
from strategy.decisions import record_decision  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_MIN_ROADMAP_YAML = """\
schema_version: 1
roadmap_id: test-roadmap
title: Test Roadmap
generated_at: 2026-05-06T00:00:00Z
phases:
  - phase_id: 0
    title: Phase Zero
    waves: [w-a, w-b]
    estimated_loc: 100
    estimated_weeks: 0.5
    blocked_on: []
waves:
  - wave_id: w-a
    title: First wave
    phase_id: 0
    status: completed
    risk_class: low
  - wave_id: w-b
    title: Second wave (next actionable)
    phase_id: 0
    status: planned
    risk_class: low
    depends_on: [w-a]
operator_decisions: []
completed_history: []
notes: {}
"""

_MALFORMED_ROADMAP_YAML = """\
schema_version: 1
roadmap_id: bad
title: Bad
generated_at: 2026-05-06T00:00:00Z
phases:
  - phase_id: not-an-int
    title: Bad
    waves: []
waves: []
operator_decisions: []
completed_history: []
notes: {}
"""


def _make_strategy(tmp_path: Path, *, with_roadmap: bool = True,
                   roadmap_yaml: str = _MIN_ROADMAP_YAML,
                   with_prd_index: bool = False,
                   with_adr_index: bool = False) -> Path:
    strat = tmp_path / "strategy"
    strat.mkdir(parents=True, exist_ok=True)
    if with_roadmap:
        (strat / "roadmap.yaml").write_text(roadmap_yaml, encoding="utf-8")
    if with_prd_index:
        (strat / "prd_index.json").write_text(
            json.dumps([{"id": "PRD-1", "path": "prd.md", "version": "1",
                         "status": "active", "supersedes": None,
                         "title": "Sample PRD"}]),
            encoding="utf-8",
        )
    if with_adr_index:
        (strat / "adr_index.json").write_text(
            json.dumps([{"id": "ADR-1", "path": "adr.md", "version": "1",
                         "status": "active", "supersedes": None,
                         "title": "Sample ADR"}]),
            encoding="utf-8",
        )
    return strat


def _record_decisions(strategy_dir: Path, count: int) -> None:
    decisions_path = strategy_dir / "decisions.ndjson"
    for i in range(count):
        # Spread across days so decision_id sequence is unique within a day
        # and we still respect the OD-YYYY-MM-DD-NNN format constraints.
        day = (i % 28) + 1
        seq = (i // 28) + 1
        decision_id = f"OD-2026-05-{day:02d}-{seq:03d}"
        record_decision(
            decision_id=decision_id,
            scope="test",
            rationale=f"rationale {i:03d}",
            path=decisions_path,
        )


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_light_returns_expected_shape(self, tmp_path):
        strat = _make_strategy(tmp_path, with_prd_index=True, with_adr_index=True)
        _record_decisions(strat, 3)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _build_strategic_state(state_dir, strategy_dir=strat)

        assert result["available"] is True
        assert result["next_actionable_wave_id"] == "w-b"
        assert result["current_focus"] == {
            "wave_id": "w-b",
            "title": "Second wave (next actionable)",
            "phase_id": 0,
        }
        assert len(result["recent_decisions"]) == 3
        assert all("decision_id" in d for d in result["recent_decisions"])
        index_names = {idx["name"] for idx in result["available_indexes"]}
        assert index_names == {"prd_index", "adr_index"}

    def test_heavy_includes_full_roadmap(self, tmp_path):
        strat = _make_strategy(tmp_path)
        _record_decisions(strat, 2)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        heavy = _build_strategic_state_heavy(state_dir, strategy_dir=strat)

        assert heavy["available"] is True
        assert heavy["roadmap"] is not None
        wave_ids = {w["wave_id"] for w in heavy["roadmap"]["waves"]}
        assert wave_ids == {"w-a", "w-b"}
        assert len(heavy["decisions"]) == 2


# ---------------------------------------------------------------------------
# 2. Missing strategy/
# ---------------------------------------------------------------------------

class TestMissingStrategy:
    def test_returns_unavailable_when_folder_absent(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = _build_strategic_state(state_dir, strategy_dir=tmp_path / "absent")
        assert result["available"] is False
        assert result["current_focus"] is None
        assert result["next_actionable_wave_id"] is None
        assert result["recent_decisions"] == []

    def test_heavy_unavailable_when_folder_absent(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        heavy = _build_strategic_state_heavy(state_dir, strategy_dir=tmp_path / "absent")
        assert heavy["available"] is False


# ---------------------------------------------------------------------------
# 3. Malformed roadmap.yaml
# ---------------------------------------------------------------------------

class TestMalformedRoadmap:
    def test_malformed_roadmap_does_not_crash(self, tmp_path):
        strat = _make_strategy(tmp_path, roadmap_yaml=_MALFORMED_ROADMAP_YAML)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _build_strategic_state(state_dir, strategy_dir=strat)

        # Loader is non-strict and roadmap parsing rejects bad phase_id;
        # available is still True (folder exists), but current_focus is None
        # because roadmap could not be parsed.
        assert result["available"] is True
        assert result["current_focus"] is None
        assert result["next_actionable_wave_id"] is None


# ---------------------------------------------------------------------------
# 4. Missing decisions.ndjson
# ---------------------------------------------------------------------------

class TestMissingDecisions:
    def test_missing_decisions_returns_empty_list(self, tmp_path):
        strat = _make_strategy(tmp_path)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _build_strategic_state(state_dir, strategy_dir=strat)

        assert result["available"] is True
        assert result["recent_decisions"] == []


# ---------------------------------------------------------------------------
# 5/6. Decision count caps
# ---------------------------------------------------------------------------

class TestDecisionCaps:
    def test_light_caps_at_five(self, tmp_path):
        strat = _make_strategy(tmp_path)
        _record_decisions(strat, 12)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _build_strategic_state(state_dir, strategy_dir=strat)

        assert len(result["recent_decisions"]) == 5

    def test_heavy_caps_at_twenty(self, tmp_path):
        strat = _make_strategy(tmp_path)
        _record_decisions(strat, 30)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        heavy = _build_strategic_state_heavy(state_dir, strategy_dir=strat)

        assert len(heavy["decisions"]) == 20


# ---------------------------------------------------------------------------
# 7. Budget guard
# ---------------------------------------------------------------------------

class TestBudgetGuard:
    def test_single_call_under_200ms(self, tmp_path):
        # Representative fixture: full strategy/ with indexes + 30 decisions
        # (heavy bound) — single call budget per W-state-5 success criteria.
        strat = _make_strategy(
            tmp_path, with_prd_index=True, with_adr_index=True
        )
        _record_decisions(strat, 30)
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Warm-up to amortize import / yaml-loader caches.
        _build_strategic_state(state_dir, strategy_dir=strat)
        _build_strategic_state_heavy(state_dir, strategy_dir=strat)

        start = time.monotonic()
        _build_strategic_state(state_dir, strategy_dir=strat)
        _build_strategic_state_heavy(state_dir, strategy_dir=strat)
        elapsed_ms = (time.monotonic() - start) * 1000

        # 200ms budget covers BOTH light + heavy combined — that is the
        # actual cost added to build_t0_state on the boot path.
        assert elapsed_ms < 200, (
            f"strategic_state build took {elapsed_ms:.1f}ms "
            f"(budget 200ms)"
        )


# ---------------------------------------------------------------------------
# 8. Integration with build_t0_state
# ---------------------------------------------------------------------------

class TestBuildIntegration:
    def test_state_dict_carries_strategic_state(self, tmp_path):
        # Build a minimal valid strategy/ co-located with state_dir/ so that
        # _resolve_strategy_dir finds it via the sibling-of-state convention.
        data_dir = tmp_path / ".vnx-data"
        state_dir = data_dir / "state"
        state_dir.mkdir(parents=True)
        dispatch_dir = data_dir / "dispatches"
        dispatch_dir.mkdir(parents=True)
        _make_strategy(data_dir)

        state = build_t0_state(state_dir, dispatch_dir)

        assert "strategic_state" in state
        assert state["strategic_state"]["available"] is True
        # Heavy version travels under a private key the main() handler pops
        # before persisting t0_state.json.
        assert "_strategic_state_heavy" in state
        assert state["_strategic_state_heavy"]["available"] is True

    def test_existing_schema_preserved(self, tmp_path):
        # ADDITIVE-ONLY guarantee: the pre-W-state-5 keys must all remain.
        data_dir = tmp_path / ".vnx-data"
        state_dir = data_dir / "state"
        state_dir.mkdir(parents=True)
        dispatch_dir = data_dir / "dispatches"
        dispatch_dir.mkdir(parents=True)

        state = build_t0_state(state_dir, dispatch_dir)

        required = {
            "schema_version",
            "generated_at",
            "terminals",
            "queues",
            "tracks",
            "pr_progress",
            "feature_state",
            "open_items",
            "quality_digest",
            "dispatch_insights",
            "active_work",
            "recent_receipts",
            "dispatch_register_events",
            "git_context",
            "system_health",
            "pr_queue",
            "_build_seconds",
        }
        missing = required - set(state.keys())
        assert not missing, f"build_t0_state lost legacy keys: {missing}"
        assert state["schema_version"] == "2.1"


# ---------------------------------------------------------------------------
# 9. Detail map routing
# ---------------------------------------------------------------------------

class TestDetailMapRouting:
    def test_heavy_strategic_state_routed_to_strategic_state_json(self, tmp_path):
        assert _DETAIL_SECTION_MAP.get("_strategic_state_heavy") == "strategic_state"

        detail_dir = tmp_path / "t0_detail"
        state = {
            "_strategic_state_heavy": {
                "available": True,
                "roadmap": {"waves": []},
                "decisions": [],
                "prd_index": [],
                "adr_index": [],
            }
        }
        manifest = _write_detail_files(state, detail_dir)

        out = detail_dir / "strategic_state.json"
        assert out.exists()
        assert manifest.get("_strategic_state_heavy") == str(out)
        body = json.loads(out.read_text(encoding="utf-8"))
        assert body["available"] is True


# ---------------------------------------------------------------------------
# 10. Defensive: garbage strategy_dir argument
# ---------------------------------------------------------------------------

class TestDefensiveArgs:
    def test_file_instead_of_dir(self, tmp_path):
        bogus = tmp_path / "not_a_dir"
        bogus.write_text("hi", encoding="utf-8")
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = _build_strategic_state(state_dir, strategy_dir=bogus)
        assert result["available"] is False
