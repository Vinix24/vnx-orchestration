"""Tests for scripts/build_strategy_projection.py.

Covers:
  1. build_projection: sample root roadmap -> valid strategy Roadmap
  2. build_projection: status mapping (done->completed, planned->planned)
  3. build_projection: next_actionable_wave resolves to non-None
  4. build_projection: dependency sort (zero-dep features first among planned)
  5. build_projection: phase grouping by milestone
  6. write_strategy_roadmap: writes valid YAML readable by load_roadmap
  7. write_strategy_roadmap: includes generated-header banner
  8. Idempotency: re-running projection produces same wave count
  9. seed_decisions: appends decisions via record_decision
  10. seed_decisions: idempotent (skips existing decision IDs)
  11. Edge case: empty features list
  12. Edge case: missing optional fields (branch_name, plan_path, etc.)
  13. Integration: write + load + next_actionable_wave round-trip
  14. build_projection: completed_history populated from completed waves
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from build_strategy_projection import (
    _GENERATED_HEADER,
    _PHASE_TITLES,
    _STATUS_MAP,
    _map_status,
    _resolve_phase_id,
    build_projection,
    seed_decisions,
    write_strategy_roadmap,
)
from strategy.decisions import DecisionValidationError, recent_decisions, record_decision
from strategy.roadmap import (
    RoadmapValidationError,
    load_roadmap,
    next_actionable_wave,
    validate_roadmap,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_NOW_ISO = _NOW.isoformat()


def _make_sample_roadmap(features: list[dict] | None = None) -> dict:
    """Build a minimal ROADMAP.yaml structure for testing."""
    if features is None:
        features = [
            {
                "feature_id": "feat-a",
                "title": "Feature A — zero deps",
                "status": "done",
                "milestone": "1.0",
                "risk_class": "low",
                "depends_on": [],
                "review_stack": ["codex_gate"],
            },
            {
                "feature_id": "feat-b",
                "title": "Feature B — depends on A",
                "status": "planned",
                "milestone": "1.0.1",
                "risk_class": "medium",
                "depends_on": ["feat-a"],
                "review_stack": ["gemini_review", "codex_gate"],
                "branch_name": "feat/feature-b",
                "plan_path": "claudedocs/FEATURE-B.md",
            },
            {
                "feature_id": "feat-c",
                "title": "Feature C — zero deps, planned",
                "status": "planned",
                "milestone": "1.0",
                "risk_class": "high",
                "depends_on": [],
                "review_stack": ["codex_gate"],
                "notes": "This is the first actionable wave.",
            },
        ]
    return {
        "roadmap_id": "test-roadmap",
        "title": "Test Roadmap",
        "features": features,
    }


def _write_sample_roadmap(
    tmp_dir: Path, features: list[dict] | None = None
) -> Path:
    """Write a sample ROADMAP.yaml to ``tmp_dir`` and return the path."""
    path = tmp_dir / "ROADMAP.yaml"
    path.write_text(
        yaml.safe_dump(_make_sample_roadmap(features), sort_keys=False),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 1-5: build_projection
# ---------------------------------------------------------------------------

class TestBuildProjection:
    def test_returns_typed_roadmap(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        from strategy.roadmap import Roadmap
        assert isinstance(roadmap, Roadmap)
        assert roadmap.schema_version == 1

    def test_wave_count_matches_features(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        assert len(roadmap.waves) == 3

    def test_wave_fields_preserved(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        wave_a = next(w for w in roadmap.waves if w.wave_id == "feat-a")
        assert wave_a.title == "Feature A — zero deps"
        assert wave_a.status == "completed"
        assert wave_a.phase_id == 1
        assert wave_a.risk_class == "low"
        assert wave_a.review_stack == ["codex_gate"]

    def test_status_mapping_done_to_completed(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        wave_a = next(w for w in roadmap.waves if w.wave_id == "feat-a")
        assert wave_a.status == "completed"

    def test_status_mapping_planned_to_planned(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        wave_c = next(w for w in roadmap.waves if w.wave_id == "feat-c")
        assert wave_c.status == "planned"

    def test_depends_on_preserved(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        wave_b = next(w for w in roadmap.waves if w.wave_id == "feat-b")
        assert wave_b.depends_on == ["feat-a"]

    def test_optional_fields_preserved(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        wave_b = next(w for w in roadmap.waves if w.wave_id == "feat-b")
        assert wave_b.branch_name == "feat/feature-b"
        assert wave_b.plan_path == "claudedocs/FEATURE-B.md"

    def test_notes_preserved(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        wave_c = next(w for w in roadmap.waves if w.wave_id == "feat-c")
        assert wave_c.notes == "This is the first actionable wave."

    def test_phase_grouping_by_milestone(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        # feat-a + feat-c: milestone 1.0 -> phase 1
        # feat-b: milestone 1.0.1 -> phase 2
        phase_ids = {p.phase_id for p in roadmap.phases}
        assert phase_ids == {1, 2}
        phase_1 = next(p for p in roadmap.phases if p.phase_id == 1)
        assert set(phase_1.waves) == {"feat-a", "feat-c"}

    def test_next_actionable_resolves(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        nw = next_actionable_wave(roadmap)
        assert nw is not None
        # feat-c is zero-dep planned in phase 1 -> should be first actionable
        assert nw.wave_id == "feat-c"

    def test_zero_dep_planned_before_planned_with_deps(self, tmp_path):
        """feat-c (0 deps, planned) should sort before feat-b (1 dep, planned)."""
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        planned_waves = [w for w in roadmap.waves if w.status == "planned"]
        # feat-c (phase 1, 0 deps) before feat-b (phase 2, 1 dep)
        assert planned_waves[0].wave_id == "feat-c"
        assert planned_waves[1].wave_id == "feat-b"

    def test_completed_history_populated(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        completed_ids = {e["wave_id"] for e in roadmap.completed_history}
        assert "feat-a" in completed_ids

    def test_roadmap_id_from_source(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        assert roadmap.roadmap_id == "test-roadmap"
        assert roadmap.title == "Test Roadmap"

    def test_notes_dict_has_generator_warning(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        assert "source" in roadmap.notes
        assert roadmap.notes["source"] == "ROADMAP.yaml"


# ---------------------------------------------------------------------------
# 6-8: write_strategy_roadmap
# ---------------------------------------------------------------------------

class TestWriteStrategyRoadmap:
    def test_writes_valid_yaml_loadable_by_loader(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        out_dir = tmp_path / "strategy"
        out_dir.mkdir()
        write_strategy_roadmap(roadmap, output_dir=out_dir)
        out_file = out_dir / "roadmap.yaml"
        assert out_file.exists()

        # Load via strategy module's own loader
        loaded = load_roadmap(out_file, strict=True)
        assert len(loaded.waves) == 3

    def test_includes_generated_header(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        out_dir = tmp_path / "strategy"
        out_dir.mkdir()
        write_strategy_roadmap(roadmap, output_dir=out_dir)
        content = (out_dir / "roadmap.yaml").read_text(encoding="utf-8")
        assert "AUTO-GENERATED" in content
        assert "build_strategy_projection.py" in content
        assert "DO NOT EDIT" in content

    def test_dry_run_does_not_write(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        out_dir = tmp_path / "strategy"
        out_dir.mkdir()
        result = write_strategy_roadmap(roadmap, output_dir=out_dir, dry_run=True)
        assert not (out_dir / "roadmap.yaml").exists()

    def test_idempotent_same_wave_count(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap1 = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        roadmap2 = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        assert len(roadmap1.waves) == len(roadmap2.waves)

    def test_written_file_passes_strict_validation(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        out_dir = tmp_path / "strategy"
        out_dir.mkdir()
        write_strategy_roadmap(roadmap, output_dir=out_dir)
        loaded = load_roadmap(out_dir / "roadmap.yaml", strict=True)
        errors = validate_roadmap(loaded)
        assert errors == [], f"Validation errors: {errors}"


# ---------------------------------------------------------------------------
# 9-10: seed_decisions
# ---------------------------------------------------------------------------

class TestSeedDecisions:
    def test_seeds_four_decisions(self, tmp_path):
        out_dir = tmp_path / "strategy"
        out_dir.mkdir(parents=True)
        appended = seed_decisions(output_dir=out_dir)
        assert len(appended) == 4
        decisions_path = out_dir / "decisions.ndjson"
        assert decisions_path.exists()

    def test_decision_ids_follow_schema(self, tmp_path):
        out_dir = tmp_path / "strategy"
        out_dir.mkdir(parents=True)
        seed_decisions(output_dir=out_dir)
        decisions = recent_decisions(20, path=out_dir / "decisions.ndjson")
        for d in decisions:
            assert d.decision_id.startswith("OD-2026-06-01-")
            assert len(d.decision_id) == 17  # OD-YYYY-MM-DD-NNN (17 chars)

    def test_decisions_readable_via_recent_decisions(self, tmp_path):
        out_dir = tmp_path / "strategy"
        out_dir.mkdir(parents=True)
        seed_decisions(output_dir=out_dir)
        decisions = recent_decisions(20, path=out_dir / "decisions.ndjson")
        assert len(decisions) == 4
        scopes = {d.scope for d in decisions}
        assert "GAP 3b: receipt hash-chain" in scopes
        assert "FUT-1/2 tracks-layer activation" in scopes

    def test_idempotent_skips_existing(self, tmp_path):
        out_dir = tmp_path / "strategy"
        out_dir.mkdir(parents=True)
        first = seed_decisions(output_dir=out_dir)
        assert len(first) == 4
        second = seed_decisions(output_dir=out_dir)
        assert len(second) == 0  # all skipped

    def test_dry_run_does_not_write(self, tmp_path):
        out_dir = tmp_path / "strategy"
        out_dir.mkdir(parents=True)
        appended = seed_decisions(output_dir=out_dir, dry_run=True)
        assert len(appended) == 4
        assert not (out_dir / "decisions.ndjson").exists()

    def test_decision_fields_match_required(self, tmp_path):
        out_dir = tmp_path / "strategy"
        out_dir.mkdir(parents=True)
        seed_decisions(output_dir=out_dir)
        decisions = recent_decisions(20, path=out_dir / "decisions.ndjson")
        for d in decisions:
            assert d.decision_id
            assert d.scope
            assert d.ts
            assert d.rationale


# ---------------------------------------------------------------------------
# 11: Edge case — empty features
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_features_produces_empty_waves(self, tmp_path):
        root = _write_sample_roadmap(tmp_path, features=[])
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        assert len(roadmap.waves) == 0
        assert len(roadmap.phases) == 0

    def test_missing_optional_fields_handled(self, tmp_path):
        features = [
            {
                "feature_id": "minimal",
                "title": "Minimal Feature",
                "status": "planned",
                "milestone": "1.0",
            }
        ]
        root = _write_sample_roadmap(tmp_path, features=features)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        wave = roadmap.waves[0]
        assert wave.wave_id == "minimal"
        assert wave.branch_name is None
        assert wave.plan_path is None
        assert wave.notes is None
        assert wave.depends_on == []
        assert wave.review_stack == []

    def test_unknown_status_defaults_to_planned(self, tmp_path):
        features = [
            {
                "feature_id": "weird-status",
                "title": "Weird Status",
                "status": "unknown-weird-value",
                "milestone": "1.0",
            }
        ]
        root = _write_sample_roadmap(tmp_path, features=features)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        assert roadmap.waves[0].status == "planned"

    def test_empty_feature_id_skipped(self, tmp_path):
        features = [
            {
                "feature_id": "",
                "title": "Bad Feature",
                "status": "planned",
                "milestone": "1.0",
            },
            {
                "feature_id": "good-one",
                "title": "Good Feature",
                "status": "done",
                "milestone": "1.0",
            },
        ]
        root = _write_sample_roadmap(tmp_path, features=features)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        assert len(roadmap.waves) == 1
        assert roadmap.waves[0].wave_id == "good-one"

    def test_unknown_milestone_defaults_to_phase_5(self, tmp_path):
        features = [
            {
                "feature_id": "future-thing",
                "title": "Future Thing",
                "status": "planned",
                "milestone": "3.0",
            }
        ]
        root = _write_sample_roadmap(tmp_path, features=features)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        assert roadmap.waves[0].phase_id == 5


# ---------------------------------------------------------------------------
# 12: Integration — write + load + next_actionable_wave round-trip
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_full_roundtrip(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        out_dir = tmp_path / "strategy"
        out_dir.mkdir()
        write_strategy_roadmap(roadmap, output_dir=out_dir)
        seed_decisions(output_dir=out_dir)

        # Simulate what loaders.py:load_strategy_for_boot does
        from strategy.loaders import load_strategy_for_boot
        loaded = load_strategy_for_boot(out_dir, decisions_n=20)

        assert loaded["roadmap"] is not None
        assert len(loaded["decisions"]) == 4

        nw = next_actionable_wave(loaded["roadmap"])
        assert nw is not None
        assert nw.status == "planned"

    def test_current_focus_flow_matches_build_t0_state(self, tmp_path):
        """Simulate the exact path build_t0_state uses to derive current_focus."""
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        out_dir = tmp_path / "strategy"
        out_dir.mkdir()
        write_strategy_roadmap(roadmap, output_dir=out_dir)

        from strategy.loaders import load_strategy_for_boot
        from strategy.roadmap import next_actionable_wave

        loaded = load_strategy_for_boot(out_dir, decisions_n=5)
        r = loaded["roadmap"]

        current_focus = None
        nw = next_actionable_wave(r)
        if nw is not None:
            current_focus = {
                "wave_id": nw.wave_id,
                "title": nw.title,
                "phase_id": nw.phase_id,
            }
        else:
            for w in r.waves:
                if w.status == "in_progress":
                    current_focus = {
                        "wave_id": w.wave_id,
                        "title": w.title,
                        "phase_id": w.phase_id,
                    }
                    break

        assert current_focus is not None
        assert current_focus["wave_id"] == "feat-c"


# ---------------------------------------------------------------------------
# 13: Helper functions
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_map_status_known_values(self):
        assert _map_status("done") == "completed"
        assert _map_status("shipped-dark") == "completed"
        assert _map_status("planned") == "planned"

    def test_map_status_unknown_defaults_to_planned(self):
        assert _map_status("bogus") == "planned"

    def test_resolve_phase_id_known_milestones(self):
        assert _resolve_phase_id("1.0") == 1
        assert _resolve_phase_id("1.0.1") == 2
        assert _resolve_phase_id("1.1") == 3
        assert _resolve_phase_id("1.2") == 4
        assert _resolve_phase_id("1.x") == 5

    def test_resolve_phase_id_none_defaults_to_5(self):
        assert _resolve_phase_id(None) == 5

    def test_resolve_phase_id_unknown_defaults_to_5(self):
        assert _resolve_phase_id("9.9") == 5

    def test_status_map_covers_all_expected(self):
        assert "done" in _STATUS_MAP
        assert "shipped-dark" in _STATUS_MAP
        assert "planned" in _STATUS_MAP

    def test_phase_titles_cover_all_phase_ids(self):
        for pid in range(1, 6):
            assert pid in _PHASE_TITLES

    def test_generated_header_is_yaml_comment(self):
        for line in _GENERATED_HEADER.strip().split("\n"):
            assert line.startswith("#")


# ---------------------------------------------------------------------------
# 14: Validation — no dangling references in projection
# ---------------------------------------------------------------------------

class TestValidation:
    def test_no_dangling_depends_on(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        errors = validate_roadmap(roadmap)
        cross_ref_errors = [
            e for e in errors if "dangling" in e or "not present" in e
        ]
        assert cross_ref_errors == [], f"Dangling refs: {cross_ref_errors}"

    def test_no_duplicate_wave_ids(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        errors = validate_roadmap(roadmap)
        dup_errors = [e for e in errors if "duplicate" in e]
        assert dup_errors == [], f"Duplicate errors: {dup_errors}"

    def test_phase_waves_reference_valid_waves(self, tmp_path):
        root = _write_sample_roadmap(tmp_path)
        roadmap = build_projection(root_roadmap_path=root, now_iso=_NOW_ISO)
        wave_ids = {w.wave_id for w in roadmap.waves}
        for p in roadmap.phases:
            for w_ref in p.waves:
                assert w_ref in wave_ids, f"Phase {p.phase_id} refs unknown wave {w_ref}"
