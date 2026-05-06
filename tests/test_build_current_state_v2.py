"""Tests for the typed current_state.md projector (W-state-3 rewrite).

Test plan:
- Section presence: all 7 sections present in output
- Section order: in exact expected sequence
- Idempotency: two consecutive runs produce byte-identical output
- Deterministic ordering: 2 decisions with same timestamp sorted by decision_id
- Empty state: no roadmap, no decisions → output still has all sections
- Recommended next move: resolved deps → returns wave; unresolved → blocked or None
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import build_current_state as bcs

SECTION_HEADERS = [
    "# Mission",
    "## Current focus",
    "## Roadmap snapshot",
    "## In flight",
    "## Last 3 decisions",
    "## Recommended next move",
    "## Resume hints",
]


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_data_dir(tmp_path: Path) -> Path:
    (tmp_path / "strategy").mkdir(parents=True)
    (tmp_path / "state").mkdir(parents=True)
    return tmp_path


def _write_roadmap(strategy_dir: Path, data: dict) -> None:
    import yaml
    (strategy_dir / "roadmap.yaml").write_text(yaml.dump(data))


def _write_decisions(strategy_dir: Path, records: list[dict]) -> None:
    (strategy_dir / "decisions.ndjson").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )


def _write_t0_state(state_dir: Path, data: dict) -> None:
    (state_dir / "t0_state.json").write_text(json.dumps(data))


def _no_prs(*_args, **_kwargs):
    return []


def _minimal_roadmap(phase_id: int = 1) -> dict:
    return {
        "schema_version": 1,
        "roadmap_id": "test-roadmap",
        "title": "Test Roadmap",
        "generated_at": "2026-05-06T00:00:00Z",
        "phases": [
            {
                "phase_id": phase_id,
                "title": "Test Phase",
                "waves": ["w-1"],
                "estimated_loc": 0,
                "estimated_weeks": 0.0,
                "blocked_on": [],
            }
        ],
        "waves": [
            {
                "wave_id": "w-1",
                "title": "Test Wave",
                "phase_id": phase_id,
                "status": "planned",
                "depends_on": [],
            }
        ],
    }


def _decision_record(decision_id: str, ts: str, scope: str = "arch", rationale: str = "test") -> dict:
    return {"decision_id": decision_id, "scope": scope, "ts": ts, "rationale": rationale}


# ---------------------------------------------------------------------------
# Section presence
# ---------------------------------------------------------------------------

class TestSectionPresence:
    def test_all_7_sections_present_empty_state(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        for header in SECTION_HEADERS:
            assert header in content, f"Missing section: {header!r}"

    def test_all_7_sections_present_with_roadmap(self, tmp_data_dir: Path) -> None:
        _write_roadmap(tmp_data_dir / "strategy", _minimal_roadmap())
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        for header in SECTION_HEADERS:
            assert header in content, f"Missing section: {header!r}"

    def test_all_7_sections_present_with_decisions(self, tmp_data_dir: Path) -> None:
        _write_decisions(
            tmp_data_dir / "strategy",
            [_decision_record("OD-2026-05-01-001", "2026-05-01T10:00:00Z")],
        )
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        for header in SECTION_HEADERS:
            assert header in content, f"Missing section: {header!r}"


# ---------------------------------------------------------------------------
# Section order
# ---------------------------------------------------------------------------

class TestSectionOrder:
    def test_sections_in_expected_sequence(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        lines = content.splitlines()

        positions: list[int] = []
        for header in SECTION_HEADERS:
            for i, line in enumerate(lines):
                if line == header:
                    positions.append(i)
                    break
            else:
                pytest.fail(f"Section header not found: {header!r}")

        assert positions == sorted(positions), (
            f"Sections out of order. Positions: {list(zip(SECTION_HEADERS, positions))}"
        )

    def test_mission_is_first_section(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert content.startswith("# Mission")

    def test_resume_hints_is_last_section(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        lines = content.splitlines()
        section_lines = [i for i, l in enumerate(lines) if l in SECTION_HEADERS]
        assert lines[section_lines[-1]] == "## Resume hints"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_empty_state_idempotent(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            run1 = bcs.build(tmp_data_dir)
            run2 = bcs.build(tmp_data_dir)
        assert run1 == run2, "Output differed between run 1 and run 2"

    def test_with_roadmap_and_decisions_idempotent(self, tmp_data_dir: Path) -> None:
        _write_roadmap(tmp_data_dir / "strategy", _minimal_roadmap())
        _write_decisions(
            tmp_data_dir / "strategy",
            [_decision_record("OD-2026-05-01-001", "2026-05-01T10:00:00Z")],
        )
        with patch.object(bcs, "_fetch_prs", _no_prs):
            run1 = bcs.build(tmp_data_dir)
            run2 = bcs.build(tmp_data_dir)
        assert run1 == run2, "Output differed between run 1 and run 2"

    def test_no_datetime_now_in_body(self, tmp_data_dir: Path) -> None:
        import datetime
        today = datetime.date.today().isoformat()
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        offending = [
            line for line in content.splitlines()
            if today in line and not line.startswith("Last updated:")
        ]
        assert not offending, f"Body contains today's date outside 'Last updated:': {offending}"

    def test_with_t0_state_idempotent(self, tmp_data_dir: Path) -> None:
        _write_t0_state(tmp_data_dir / "state", {
            "tracks": {"A": {"active_dispatch_id": "d-001", "status": "working", "current_gate": "g1"}},
        })
        with patch.object(bcs, "_fetch_prs", _no_prs):
            run1 = bcs.build(tmp_data_dir)
            run2 = bcs.build(tmp_data_dir)
        assert run1 == run2


# ---------------------------------------------------------------------------
# Deterministic decision ordering
# ---------------------------------------------------------------------------

class TestDeterministicOrdering:
    def test_same_timestamp_sorted_by_decision_id(self, tmp_data_dir: Path) -> None:
        shared_ts = "2026-05-01T10:00:00Z"
        records = [
            _decision_record("OD-2026-05-01-002", shared_ts, rationale="second-rationale"),
            _decision_record("OD-2026-05-01-001", shared_ts, rationale="first-rationale"),
        ]
        _write_decisions(tmp_data_dir / "strategy", records)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)

        idx1 = content.index("OD-2026-05-01-001")
        idx2 = content.index("OD-2026-05-01-002")
        assert idx1 < idx2, "OD-001 should appear before OD-002 (sorted by decision_id)"

    def test_same_timestamp_idempotent(self, tmp_data_dir: Path) -> None:
        shared_ts = "2026-05-01T10:00:00Z"
        records = [
            _decision_record("OD-2026-05-01-003", shared_ts, rationale="r3"),
            _decision_record("OD-2026-05-01-001", shared_ts, rationale="r1"),
            _decision_record("OD-2026-05-01-002", shared_ts, rationale="r2"),
        ]
        _write_decisions(tmp_data_dir / "strategy", records)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            run1 = bcs.build(tmp_data_dir)
            run2 = bcs.build(tmp_data_dir)
        assert run1 == run2

    def test_different_timestamps_ordered_chronologically(self, tmp_data_dir: Path) -> None:
        records = [
            _decision_record("OD-2026-05-01-001", "2026-05-01T08:00:00Z", rationale="early"),
            _decision_record("OD-2026-05-01-002", "2026-05-01T12:00:00Z", rationale="late"),
        ]
        _write_decisions(tmp_data_dir / "strategy", records)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        idx_early = content.index("early")
        idx_late = content.index("late")
        assert idx_early < idx_late


# ---------------------------------------------------------------------------
# Empty state — all sections still present with placeholder text
# ---------------------------------------------------------------------------

class TestEmptyState:
    def test_mission_placeholder_when_no_roadmap(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "# Mission" in content
        assert "_No mission set._" in content

    def test_current_focus_placeholder_when_no_roadmap(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "## Current focus" in content
        assert "_No roadmap available._" in content

    def test_roadmap_snapshot_placeholder_when_no_roadmap(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "## Roadmap snapshot" in content
        assert "_No roadmap data._" in content

    def test_in_flight_placeholder_when_no_prs(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "## In flight" in content
        assert "_No open PRs or gh CLI unavailable._" in content

    def test_decisions_placeholder_when_no_file(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "## Last 3 decisions" in content
        assert "_No decisions recorded._" in content

    def test_recommended_next_move_placeholder_when_no_roadmap(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "## Recommended next move" in content
        assert "_No roadmap available._" in content

    def test_resume_hints_always_present(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "## Resume hints" in content
        assert "Last updated:" in content

    def test_last_updated_unknown_when_no_files(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "Last updated: unknown" in content


# ---------------------------------------------------------------------------
# Recommended next move — depends_on resolution
# ---------------------------------------------------------------------------

class TestRecommendedNextMove:
    def _roadmap_with_deps(self, w1_status: str, w2_status: str = "planned") -> dict:
        return {
            "schema_version": 1,
            "roadmap_id": "r1",
            "title": "Dep Test",
            "generated_at": "2026-05-06T00:00:00Z",
            "phases": [
                {
                    "phase_id": 1,
                    "title": "Phase One",
                    "waves": ["w-1", "w-2"],
                    "estimated_loc": 0,
                    "estimated_weeks": 0.0,
                    "blocked_on": [],
                }
            ],
            "waves": [
                {
                    "wave_id": "w-1",
                    "title": "First Wave",
                    "phase_id": 1,
                    "status": w1_status,
                    "depends_on": [],
                },
                {
                    "wave_id": "w-2",
                    "title": "Second Wave",
                    "phase_id": 1,
                    "status": w2_status,
                    "depends_on": ["w-1"],
                },
            ],
        }

    def test_resolved_deps_returns_expected_wave(self, tmp_data_dir: Path) -> None:
        """When depends_on wave is completed, next actionable wave is w-2."""
        _write_roadmap(
            tmp_data_dir / "strategy",
            self._roadmap_with_deps(w1_status="completed"),
        )
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "`w-2`" in content
        assert "ready to start" in content

    def test_unresolved_deps_returns_blocked_message(self, tmp_data_dir: Path) -> None:
        """When w-1 is in_progress, w-2's deps are unresolved → no actionable wave."""
        _write_roadmap(
            tmp_data_dir / "strategy",
            self._roadmap_with_deps(w1_status="in_progress"),
        )
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "## Recommended next move" in content
        assert "`w-2`" in content
        assert "Blocked on" in content

    def test_first_wave_no_deps_is_immediately_actionable(self, tmp_data_dir: Path) -> None:
        _write_roadmap(tmp_data_dir / "strategy", _minimal_roadmap())
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "`w-1`" in content
        assert "ready to start" in content

    def test_all_waves_completed_shows_nothing_to_do(self, tmp_data_dir: Path) -> None:
        roadmap = {
            "schema_version": 1,
            "roadmap_id": "r1",
            "title": "Done",
            "generated_at": "2026-05-06T00:00:00Z",
            "phases": [{"phase_id": 1, "title": "P1", "waves": ["w-1"],
                        "estimated_loc": 0, "estimated_weeks": 0.0, "blocked_on": []}],
            "waves": [{"wave_id": "w-1", "title": "W1", "phase_id": 1,
                       "status": "completed", "depends_on": []}],
        }
        _write_roadmap(tmp_data_dir / "strategy", roadmap)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "## Recommended next move" in content
        # No planned waves → "nothing to do" message
        assert "Nothing to do" in content or "completed" in content


# ---------------------------------------------------------------------------
# Current focus section
# ---------------------------------------------------------------------------

class TestCurrentFocus:
    def test_active_wave_shown(self, tmp_data_dir: Path) -> None:
        roadmap = {
            "schema_version": 1,
            "roadmap_id": "r1",
            "title": "Focus Test",
            "generated_at": "2026-05-06T00:00:00Z",
            "phases": [{"phase_id": 2, "title": "Build Phase",
                        "waves": ["w-active"], "estimated_loc": 0,
                        "estimated_weeks": 0.0, "blocked_on": []}],
            "waves": [{"wave_id": "w-active", "title": "Active Work",
                       "phase_id": 2, "status": "in_progress", "depends_on": []}],
        }
        _write_roadmap(tmp_data_dir / "strategy", roadmap)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "Phase 2" in content
        assert "Build Phase" in content
        assert "`w-active`" in content
        assert "[~]" in content

    def test_no_active_phase_when_all_completed(self, tmp_data_dir: Path) -> None:
        roadmap = {
            "schema_version": 1,
            "roadmap_id": "r1",
            "title": "Done",
            "generated_at": "2026-05-06T00:00:00Z",
            "phases": [{"phase_id": 1, "title": "P1", "waves": ["w-1"],
                        "estimated_loc": 0, "estimated_weeks": 0.0, "blocked_on": []}],
            "waves": [{"wave_id": "w-1", "title": "W1", "phase_id": 1,
                       "status": "completed", "depends_on": []}],
        }
        _write_roadmap(tmp_data_dir / "strategy", roadmap)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "_No active phase. All waves completed or roadmap empty._" in content


# ---------------------------------------------------------------------------
# Roadmap snapshot badges
# ---------------------------------------------------------------------------

class TestRoadmapSnapshot:
    def test_completed_badge_in_snapshot(self, tmp_data_dir: Path) -> None:
        roadmap = {
            "schema_version": 1,
            "roadmap_id": "r1",
            "title": "T",
            "generated_at": "2026-05-06T00:00:00Z",
            "phases": [{"phase_id": 1, "title": "P", "waves": ["w-1"],
                        "estimated_loc": 0, "estimated_weeks": 0.0, "blocked_on": []}],
            "waves": [{"wave_id": "w-1", "title": "W", "phase_id": 1,
                       "status": "completed", "depends_on": []}],
        }
        _write_roadmap(tmp_data_dir / "strategy", roadmap)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "[x]" in content

    def test_in_progress_badge_in_snapshot(self, tmp_data_dir: Path) -> None:
        roadmap = {
            "schema_version": 1,
            "roadmap_id": "r1",
            "title": "T",
            "generated_at": "2026-05-06T00:00:00Z",
            "phases": [{"phase_id": 1, "title": "P", "waves": ["w-1"],
                        "estimated_loc": 0, "estimated_weeks": 0.0, "blocked_on": []}],
            "waves": [{"wave_id": "w-1", "title": "W", "phase_id": 1,
                       "status": "in_progress", "depends_on": []}],
        }
        _write_roadmap(tmp_data_dir / "strategy", roadmap)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "[~]" in content


# ---------------------------------------------------------------------------
# In flight — t0_state dispatches
# ---------------------------------------------------------------------------

class TestInFlight:
    def test_active_dispatches_shown(self, tmp_data_dir: Path) -> None:
        _write_t0_state(tmp_data_dir / "state", {
            "tracks": {
                "A": {
                    "active_dispatch_id": "disp-abc-123",
                    "status": "working",
                    "current_gate": "gate_review",
                }
            }
        })
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "disp-abc-123" in content
        assert "gate_review" in content

    def test_no_active_dispatches_placeholder(self, tmp_data_dir: Path) -> None:
        _write_t0_state(tmp_data_dir / "state", {
            "tracks": {"A": {"active_dispatch_id": None, "status": "idle"}}
        })
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "_No active dispatches._" in content

    def test_open_prs_shown(self, tmp_data_dir: Path) -> None:
        prs = [{"number": 42, "title": "feat: my-pr", "headRefName": "my-branch"}]
        with patch.object(bcs, "_fetch_prs", return_value=prs):
            content = bcs.build(tmp_data_dir)
        assert "PR #42" in content
        assert "feat: my-pr" in content
        assert "`my-branch`" in content


# ---------------------------------------------------------------------------
# Output limits
# ---------------------------------------------------------------------------

class TestOutputLimits:
    def test_large_roadmap_within_200_lines(self, tmp_data_dir: Path) -> None:
        many_waves = [
            {"wave_id": f"w-{i}", "title": f"Wave {i}", "phase_id": 1,
             "status": "planned", "depends_on": []}
            for i in range(100)
        ]
        roadmap = {
            "schema_version": 1,
            "roadmap_id": "r1",
            "title": "Big",
            "generated_at": "2026-05-06T00:00:00Z",
            "phases": [{"phase_id": 1, "title": "Big Phase",
                        "waves": [f"w-{i}" for i in range(100)],
                        "estimated_loc": 0, "estimated_weeks": 0.0, "blocked_on": []}],
            "waves": many_waves,
        }
        _write_roadmap(tmp_data_dir / "strategy", roadmap)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert len(content.splitlines()) <= 200


# ---------------------------------------------------------------------------
# Mission from roadmap title
# ---------------------------------------------------------------------------

class TestMissionSection:
    def test_mission_shows_roadmap_title(self, tmp_data_dir: Path) -> None:
        _write_roadmap(tmp_data_dir / "strategy", {
            **_minimal_roadmap(),
            "title": "VNX Orchestration Platform",
        })
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "VNX Orchestration Platform" in content

    def test_mission_placeholder_without_title(self, tmp_data_dir: Path) -> None:
        roadmap_data = _minimal_roadmap()
        roadmap_data["title"] = ""
        _write_roadmap(tmp_data_dir / "strategy", roadmap_data)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "_No mission set._" in content
