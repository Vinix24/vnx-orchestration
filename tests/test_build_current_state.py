"""Unit tests for scripts/build_current_state.py.

Cases:
- empty roadmap
- all-completed roadmap
- in-progress + blocked OI
- idempotence (run twice → byte-identical output)
- graceful degrade if gh CLI fails
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import build_current_state as bcs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_data_dir(tmp_path: Path) -> Path:
    strategy = tmp_path / "strategy"
    state = tmp_path / "state"
    strategy.mkdir(parents=True)
    state.mkdir(parents=True)
    return tmp_path


def _write_roadmap(strategy_dir: Path, content: dict) -> None:
    import yaml
    (strategy_dir / "roadmap.yaml").write_text(yaml.dump(content))


def _write_oi_digest(state_dir: Path, content: dict) -> None:
    (state_dir / "open_items_digest.json").write_text(json.dumps(content))


def _write_receipts(state_dir: Path, records: list[dict]) -> None:
    (state_dir / "t0_receipts.ndjson").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )


# ---------------------------------------------------------------------------
# Helpers to suppress gh CLI calls during tests
# ---------------------------------------------------------------------------

def _no_prs(*_args, **_kwargs):
    return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmptyRoadmap:
    def test_builds_without_error(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "# VNX Project State" in content

    def test_empty_roadmap_section(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "No roadmap data" in content

    def test_no_active_phase(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "No active phase" in content

    def test_within_200_lines(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert len(content.splitlines()) <= 200


class TestAllCompletedRoadmap:
    def _make_roadmap(self) -> dict:
        return {
            "schema_version": 1,
            "phases": [
                {
                    "phase_id": 0,
                    "title": "Phase Zero",
                    "waves": ["w-a", "w-b"],
                    "blocked_on": [],
                }
            ],
            "waves": [
                {"wave_id": "w-a", "title": "Wave A", "phase_id": 0,
                 "status": "completed", "depends_on": []},
                {"wave_id": "w-b", "title": "Wave B", "phase_id": 0,
                 "status": "completed", "depends_on": []},
            ],
        }

    def test_completed_badges(self, tmp_data_dir: Path) -> None:
        _write_roadmap(tmp_data_dir / "strategy", self._make_roadmap())
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "[x]" in content

    def test_no_active_phase_when_all_done(self, tmp_data_dir: Path) -> None:
        _write_roadmap(tmp_data_dir / "strategy", self._make_roadmap())
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "No active phase" in content


class TestInProgressWithBlockedOI:
    def _make_roadmap(self) -> dict:
        return {
            "schema_version": 1,
            "phases": [
                {
                    "phase_id": 0,
                    "title": "Operator UX",
                    "waves": ["w-ux-1", "w-ux-2"],
                    "blocked_on": [],
                }
            ],
            "waves": [
                {"wave_id": "w-ux-1", "title": "Bootstrap strategy/", "phase_id": 0,
                 "status": "in_progress", "depends_on": []},
                {"wave_id": "w-ux-2", "title": "State projector", "phase_id": 0,
                 "status": "planned", "depends_on": ["w-ux-1"]},
            ],
        }

    def _make_oi_digest(self) -> dict:
        return {
            "summary": {
                "open_count": 5,
                "blocker_count": 1,
                "warn_count": 3,
                "info_count": 1,
            },
            "top_blockers": [
                {"id": "OI-001", "title": "Critical blocker", "pr_id": "PR-1"},
            ],
            "open_items": [
                {"id": "OI-002", "severity": "warn", "title": "Minor warning",
                 "pr_id": None},
            ],
            "recent_closures": [],
        }

    def test_in_progress_badge(self, tmp_data_dir: Path) -> None:
        _write_roadmap(tmp_data_dir / "strategy", self._make_roadmap())
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "[~]" in content

    def test_focus_shows_active_phase(self, tmp_data_dir: Path) -> None:
        _write_roadmap(tmp_data_dir / "strategy", self._make_roadmap())
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "Phase 0" in content
        assert "Operator UX" in content

    def test_blocker_shown_in_oi(self, tmp_data_dir: Path) -> None:
        _write_roadmap(tmp_data_dir / "strategy", self._make_roadmap())
        _write_oi_digest(tmp_data_dir / "state", self._make_oi_digest())
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "OI-001" in content
        assert "Critical blocker" in content


class TestIdempotence:
    """Two consecutive runs on unchanged inputs must produce byte-identical output."""

    def test_idempotent_empty(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            run1 = bcs.build(tmp_data_dir)
            run2 = bcs.build(tmp_data_dir)
        assert run1 == run2, "Output differed between run 1 and run 2"

    def test_idempotent_with_data(self, tmp_data_dir: Path) -> None:
        roadmap = {
            "schema_version": 1,
            "phases": [{"phase_id": 0, "title": "T", "waves": ["w1"],
                        "blocked_on": []}],
            "waves": [{"wave_id": "w1", "title": "Wave", "phase_id": 0,
                       "status": "in_progress", "depends_on": []}],
        }
        _write_roadmap(tmp_data_dir / "strategy", roadmap)
        _write_receipts(tmp_data_dir / "state", [
            {"event_type": "task_complete", "timestamp": "2026-05-01T10:00:00",
             "terminal": "T1", "dispatch_id": "disp-001", "status": "success"},
        ])
        with patch.object(bcs, "_fetch_prs", _no_prs):
            run1 = bcs.build(tmp_data_dir)
            run2 = bcs.build(tmp_data_dir)
        assert run1 == run2, "Output differed between run 1 and run 2"

    def test_no_live_timestamp_in_body(self, tmp_data_dir: Path) -> None:
        """The body must not contain the current date (only mtime-derived timestamp)."""
        import datetime
        today = datetime.date.today().isoformat()
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        # "Last updated: unknown" is fine; "Last updated: <today>" would only appear
        # if the roadmap.yaml file was touched today, which it wasn't (tmpdir is fresh).
        # The key assertion: datetime.now() is never called.
        lines_with_today = [
            line for line in content.splitlines()
            if today in line and not line.startswith("Last updated:")
        ]
        assert not lines_with_today, (
            f"Body contains today's date outside 'Last updated:' line: {lines_with_today}"
        )


class TestGhCliFail:
    """Projector must degrade gracefully when gh CLI is unavailable."""

    def _failing_gh(self, *_args, **_kwargs):
        raise FileNotFoundError("gh not found")

    def test_no_prs_on_gh_failure(self, tmp_data_dir: Path) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            content = bcs.build(tmp_data_dir)
        assert "gh CLI unavailable" in content or "No open PRs" in content

    def test_output_still_valid_markdown(self, tmp_data_dir: Path) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            content = bcs.build(tmp_data_dir)
        assert content.startswith("# VNX Project State")
        assert len(content.splitlines()) <= 200

    def test_gh_nonzero_exit(self, tmp_data_dir: Path) -> None:
        from unittest.mock import MagicMock
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            content = bcs.build(tmp_data_dir)
        assert "# VNX Project State" in content


class TestLastUpdatedLine:
    def test_last_updated_from_mtime(self, tmp_data_dir: Path) -> None:
        roadmap_path = tmp_data_dir / "strategy" / "roadmap.yaml"
        import yaml
        roadmap_path.write_text(yaml.dump({"schema_version": 1}))
        # Set mtime to a known epoch
        known_mtime = 1777939200.0  # 2026-05-05T00:00:00Z
        os.utime(roadmap_path, (known_mtime, known_mtime))

        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)

        assert "Last updated: 2026-05-05T00:00:00Z" in content

    def test_last_updated_unknown_when_no_files(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "Last updated: unknown" in content


class TestDecisionsSection:
    def test_decisions_rendered(self, tmp_data_dir: Path) -> None:
        decisions = [
            {"timestamp": "2026-05-01T12:00:00Z", "title": "auth-model",
             "decision": "jwt_symmetric"},
        ]
        (tmp_data_dir / "strategy" / "decisions.ndjson").write_text(
            "\n".join(json.dumps(d) for d in decisions) + "\n"
        )
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "auth-model" in content
        assert "jwt_symmetric" in content

    def test_missing_decisions_file_graceful(self, tmp_data_dir: Path) -> None:
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert "# VNX Project State" in content


class TestOutputLength:
    def test_large_roadmap_truncated_to_200_lines(self, tmp_data_dir: Path) -> None:
        import yaml
        many_waves = [
            {"wave_id": f"w-{i}", "title": f"Wave {i}", "phase_id": 0,
             "status": "planned", "depends_on": []}
            for i in range(100)
        ]
        roadmap = {
            "schema_version": 1,
            "phases": [{"phase_id": 0, "title": "Big Phase",
                        "waves": [f"w-{i}" for i in range(100)],
                        "blocked_on": []}],
            "waves": many_waves,
        }
        _write_roadmap(tmp_data_dir / "strategy", roadmap)
        with patch.object(bcs, "_fetch_prs", _no_prs):
            content = bcs.build(tmp_data_dir)
        assert len(content.splitlines()) <= 200
