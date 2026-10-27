"""F39 replay context assembly carries no feature plan (ADR-047 / B2).

The assembler used to append "SECTION 4: Feature Plan", an excerpt of the
``FEATURE_PLAN.md`` named by ``--feature-plan`` (defaulting to a root-level
``FEATURE_PLAN.md``). ADR-047 makes the tracks DB the only plan source, so the
assembler now accepts no plan path at all: section 4 is the dispatch track's
goal read from the tracks DB, and it is omitted entirely when no track
resolves — which is every replay, because the harness assembles its prompt
from a scenario fixture with no live state dir.

These tests are NOT marked ``live``: they build prompts, they never call a
model.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_F39_DIR = Path(__file__).resolve().parents[2] / "scripts" / "f39"
_SCRIPTS_LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib"
for _path in (_F39_DIR, _SCRIPTS_LIB):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from replay_context_assembler import assemble_t0_context  # noqa: E402
from replay_harness import chain_replay, single_replay  # noqa: E402
from replay_harness.models import ChainStep  # noqa: E402

_ASSEMBLER = _F39_DIR / "replay_context_assembler.py"
_RECEIPT = {"terminal": "T1", "status": "success", "task_id": "dlv-test"}


def _state_file(tmp_path: Path) -> Path:
    state_path = tmp_path / "state" / "t0_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"current_terminal": "T0", "active_workers": {}}), encoding="utf-8")
    return state_path


def _tracks_db(state_dir: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """Create a minimal tracks store holding exactly the columns the goal read uses."""
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "CREATE TABLE tracks (track_id TEXT, project_id TEXT, title TEXT,"
        " goal_state TEXT, phase TEXT)"
    )
    conn.executemany("INSERT INTO tracks VALUES (?, ?, ?, ?, ?)", [
        (track_id, project_id, title, goal, phase) for track_id, project_id, title, goal, phase in rows
    ])
    conn.commit()
    conn.close()


class TestNoPlanInput:
    """Deliverable: the assembler has no plan argument and reads no plan file."""

    def test_feature_plan_argument_is_rejected(self, tmp_path: Path) -> None:
        """``feature_plan_path`` is gone from the signature — not merely ignored."""
        with pytest.raises(TypeError, match="feature_plan_path"):
            assemble_t0_context(
                state_path=_state_file(tmp_path),
                receipt=_RECEIPT,
                feature_plan_path=tmp_path / "FEATURE_PLAN.md",
            )

    def test_prompt_contains_no_plan_text_even_with_a_plan_file_present(self, tmp_path: Path) -> None:
        """A root FEATURE_PLAN.md beside the state file is never read.

        That is exactly where the old default (``<state>`` parent) pointed, so
        the file exists at the path the retired code loaded and its marker text
        must still not reach the prompt.
        """
        state_path = _state_file(tmp_path)
        legacy_plan = state_path.parent / "FEATURE_PLAN.md"
        legacy_plan.write_text("# Feature: Legacy\n\nLEGACY-PLAN-MARKER\n\n## PR-0: x\n", encoding="utf-8")

        prompt = assemble_t0_context(state_path=state_path, receipt=_RECEIPT)

        assert "LEGACY-PLAN-MARKER" not in prompt
        assert "FEATURE_PLAN" not in prompt
        assert "Feature Plan" not in prompt

    def test_cli_has_no_feature_plan_flag(self, tmp_path: Path) -> None:
        """The old ``--feature-plan`` flag is an argparse error (exit 2), not a silent no-op."""
        proc = subprocess.run(
            [
                sys.executable,
                str(_ASSEMBLER),
                "--state", str(_state_file(tmp_path)),
                "--receipt", json.dumps(_RECEIPT),
                "--feature-plan", "FEATURE_PLAN.md",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 2, proc.stderr
        assert "--feature-plan" in proc.stderr

    def test_cli_assembles_without_a_plan_flag(self, tmp_path: Path) -> None:
        """The happy path needs a receipt and a state file — nothing plan-shaped."""
        output = tmp_path / "prompt.txt"
        proc = subprocess.run(
            [
                sys.executable,
                str(_ASSEMBLER),
                "--state", str(_state_file(tmp_path)),
                "--receipt", json.dumps(_RECEIPT),
                "--output", str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr
        prompt = output.read_text(encoding="utf-8")
        assert "# SECTION 3: Current System State" in prompt
        assert "# SECTION 5: Receipt to Process" in prompt


class TestSection4TrackGoal:
    """Deliverable: section 4 is the track's goal from the tracks DB, or nothing."""

    def test_track_goal_replaces_the_feature_plan_section(self, tmp_path: Path) -> None:
        state_path = _state_file(tmp_path)
        state_dir = tmp_path / "state"
        _tracks_db(state_dir, [("track-a", "vnx-dev", "Track A", "Ship the tracks-DB plan end to end.", "active")])

        prompt = assemble_t0_context(
            state_path=state_path,
            receipt={"track": "track-a", **_RECEIPT},
            state_dir=state_dir,
        )

        assert "# SECTION 4: Track Goal (tracks DB) — track-a" in prompt
        assert "Ship the tracks-DB plan end to end." in prompt
        assert "Phase: active" in prompt
        assert "Feature Plan" not in prompt
        # The later section numbers are fixed, not derived from section 4.
        assert "# SECTION 5: Receipt to Process" in prompt

    def test_section_4_omitted_without_a_state_dir(self, tmp_path: Path) -> None:
        """The replay harness has no live state dir; it gets no invented plan."""
        prompt = assemble_t0_context(state_path=_state_file(tmp_path), receipt={"track": "track-a", **_RECEIPT})

        assert "SECTION 4" not in prompt
        assert "# SECTION 5: Receipt to Process" in prompt

    def test_section_4_omitted_when_the_receipt_names_no_track(self, tmp_path: Path) -> None:
        state_path = _state_file(tmp_path)
        state_dir = tmp_path / "state"
        _tracks_db(state_dir, [("track-a", "vnx-dev", "Track A", "Ship it.", "active")])

        prompt = assemble_t0_context(state_path=state_path, receipt=_RECEIPT, state_dir=state_dir)

        assert "SECTION 4" not in prompt

    def test_section_4_omitted_when_the_track_is_not_in_the_db(self, tmp_path: Path) -> None:
        state_path = _state_file(tmp_path)
        state_dir = tmp_path / "state"
        _tracks_db(state_dir, [("track-a", "vnx-dev", "Track A", "Ship it.", "active")])

        prompt = assemble_t0_context(
            state_path=state_path,
            receipt={"track": "track-missing", **_RECEIPT},
            state_dir=state_dir,
        )

        assert "SECTION 4" not in prompt

    def test_section_4_omitted_when_the_track_has_no_goal(self, tmp_path: Path) -> None:
        state_path = _state_file(tmp_path)
        state_dir = tmp_path / "state"
        _tracks_db(state_dir, [("track-a", "vnx-dev", "Track A", "", "active")])

        prompt = assemble_t0_context(
            state_path=state_path,
            receipt={"track": "track-a", **_RECEIPT},
            state_dir=state_dir,
        )

        assert "SECTION 4" not in prompt

    def test_section_4_omitted_when_no_tracks_db_exists(self, tmp_path: Path) -> None:
        """A missing store is "no planning context", not a reason to fail assembly."""
        state_path = _state_file(tmp_path)

        prompt = assemble_t0_context(
            state_path=state_path,
            receipt={"track": "track-a", **_RECEIPT},
            state_dir=tmp_path / "state",
        )

        assert "SECTION 4" not in prompt
        assert not (tmp_path / "state" / "runtime_coordination.db").exists(), (
            "the read must not create a store"
        )

    def test_unreadable_tracks_db_is_named_in_the_prompt(self, tmp_path: Path) -> None:
        """An unreadable store is surfaced, never silently downgraded."""
        state_path = _state_file(tmp_path)
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "runtime_coordination.db").write_bytes(b"not a sqlite database")

        prompt = assemble_t0_context(
            state_path=state_path,
            receipt={"track": "track-a", **_RECEIPT},
            state_dir=state_dir,
        )

        assert "# SECTION 4: Track Goal" in prompt
        assert "[TRACKS DB UNREADABLE:" in prompt


class TestHarnessAssemblesWithoutAPlan:
    """Deliverable: the replay harnesses build prompts with no plan source."""

    def test_single_replay_prompt_has_no_plan_section(self) -> None:
        prompt, errors = single_replay._assemble_replay_prompt(
            _RECEIPT, {"current_terminal": "T0", "active_workers": {}}
        )

        assert errors == []
        assert prompt
        assert "SECTION 4" not in prompt
        assert "Feature Plan" not in prompt

    def test_chain_replay_prompt_has_no_plan_section(self) -> None:
        step = ChainStep(
            step_name="step-1",
            receipt=dict(_RECEIPT),
            state_delta={},
            expected_decision="DISPATCH",
            expected_next_action="dispatch T2",
        )

        prompt = chain_replay._assemble_chain_step_prompt(
            step, {"current_terminal": "T0", "active_workers": {}}, [], []
        )

        assert prompt
        assert "SECTION 4" not in prompt
        assert "Feature Plan" not in prompt
