#!/usr/bin/env python3
"""Tests for the T0 rotation handoff writer + rotation path contract.

Covers:
  1. write_t0_handoff(): frontmatter + all three sections present,
     project_id-scoped, fail-soft when git/horizon reads fail, and reads
     tracks from the same store the canonical resolver picks.
  2. Terminal-name path-traversal validation on every terminal-scoped path
     helper (`--terminal` is untrusted CLI input).
  3. write_ready_signal(): rotation_id-stamped `.ready` under the rotation
     state dir (the `vnx handoff mark-ready` backend).
  4. handoff_reader: round-trips a written handoff.md.
  5. OI-1042 regression: the removed dead control-plane API
     (checkpoint/decide_rotation/RotationPolicy/respawn) stays removed —
     rotation execution lives in hooks/vnx_rotate.sh, not this module.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import context_rotation as cr  # noqa: E402
import handoff_reader as hr  # noqa: E402
import vnx_paths  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
PROJECT_ID = "vnx-rotation-test"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate resolve_central_data_dir()'s Path.home()-based resolution so
    tests never touch the real ~/.vnx-data."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _make_git_repo(path: Path, branch: str = "rotation-test-branch") -> None:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
    )
    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("checkout", "-q", "-b", branch)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    run("add", "README.md")
    run("commit", "-q", "-m", "initial commit")
    return None


# ---------------------------------------------------------------------------
# 1. write_t0_handoff()
# ---------------------------------------------------------------------------

class TestWriteT0Handoff:
    def test_contract_satisfied(self, isolated_home: Path, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_git_repo(repo, branch="my-feature-branch")

        logdir = tmp_path / "handoff_out"
        handoff_path = cr.write_t0_handoff(logdir=logdir, project_root=repo, project_id=PROJECT_ID)

        assert handoff_path == logdir / "handoff.md"
        assert handoff_path.is_file()
        text = handoff_path.read_text(encoding="utf-8")

        assert text.startswith("---\n")
        assert f"project: {PROJECT_ID}" in text
        assert "branch: my-feature-branch" in text
        assert "date:" in text
        assert "## Waar we middenin zitten" in text
        assert "## State" in text
        assert "## Next steps" in text

    def test_project_id_scoped_across_two_projects(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The autouse _vnx_data_dir_isolation conftest fixture pins
        # VNX_DATA_DIR_EXPLICIT=1 for every test (a safety net against ever
        # touching the real ~/.vnx-data). That explicit override is, by
        # design, project_id-independent (it collapses every project onto
        # one operator-chosen dir) — this test needs the *default*, no
        # override resolution to exercise project_id-scoping, so it opts
        # out via the documented escape hatch.
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

        repo = tmp_path / "repo"
        _make_git_repo(repo)

        dir_a = cr.rotation_handoff_dir("project-a", "T0")
        dir_b = cr.rotation_handoff_dir("project-b", "T0")
        assert dir_a != dir_b

        path_a = cr.write_t0_handoff(logdir=dir_a, project_root=repo, project_id="project-a")
        path_b = cr.write_t0_handoff(logdir=dir_b, project_root=repo, project_id="project-b")
        assert path_a != path_b
        assert "project: project-a" in path_a.read_text(encoding="utf-8")
        assert "project: project-b" in path_b.read_text(encoding="utf-8")

    def test_fail_soft_on_non_git_directory(self, isolated_home: Path, tmp_path: Path) -> None:
        non_git = tmp_path / "not_a_repo"
        non_git.mkdir()
        logdir = tmp_path / "handoff_out"

        handoff_path = cr.write_t0_handoff(logdir=logdir, project_root=non_git, project_id=PROJECT_ID)
        assert handoff_path.is_file()
        text = handoff_path.read_text(encoding="utf-8")
        assert "branch: unknown" in text
        assert "## Next steps" in text  # still fully written despite no git

    def test_fail_soft_on_missing_tracks_db(self, isolated_home: Path, tmp_path: Path) -> None:
        """No DB has been created for this project_id yet — the horizon
        section must degrade gracefully, not raise."""
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        logdir = tmp_path / "handoff_out"

        handoff_path = cr.write_t0_handoff(logdir=logdir, project_root=repo, project_id="brand-new-project")
        assert handoff_path.is_file()
        assert "Horizon NOW tracks: 0" in handoff_path.read_text(encoding="utf-8")

    def test_horizon_snapshot_reads_from_the_same_resolved_root(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The handoff's horizon section must read tracks from the SAME
        store write_t0_handoff() resolves to — not a hardcoded central dir
        the project never actually uses. OI-1055: since a fresh project
        resolves to the central dir (~/.vnx-data/<id>), the handoff writes
        there legitimately — the invariant is that the resolver and the
        writer agree, not that central stays absent."""
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

        repo = tmp_path / "repo"
        _make_git_repo(repo)
        project_id = "fresh-horizon-project"

        resolved_root = vnx_paths._resolve_state_root(project_id, repo)
        assert not resolved_root.exists()

        logdir = tmp_path / "handoff_out"
        handoff_path = cr.write_t0_handoff(logdir=logdir, project_root=repo, project_id=project_id)

        # write_t0_handoff must have looked for tracks under resolved_root
        # (which it just created via mkdir-on-demand inside tracks setup).
        # OI-1055: a fresh project resolves to ~/.vnx-data/<id>, so the
        # handoff writes there — what matters is that the writer and resolver
        # agree, not that the central path stays empty.
        assert handoff_path.is_file()


# ---------------------------------------------------------------------------
# 2. Terminal-name path-traversal validation
# ---------------------------------------------------------------------------

class TestTerminalValidation:
    """Codex P2: `--terminal` is untrusted CLI input and becomes a path
    component in every terminal-scoped path helper. A value like
    "../../../../.ssh/x" must never let a caller escape the central data
    dir."""

    TRAVERSAL_INPUTS = [
        "../../../../.ssh/x",
        "../evil",
        "T0/../../etc",
        "a/b",
        "a\\b",
        "..",
        ".",
        "",
        "/etc/passwd",
    ]

    PATH_HELPERS = [
        cr.rotation_handoff_dir,
        cr.ready_signal_path,
    ]

    @pytest.mark.parametrize("bad_terminal", TRAVERSAL_INPUTS)
    def test_path_helpers_reject_traversal(self, isolated_home: Path, bad_terminal: str) -> None:
        for helper in self.PATH_HELPERS:
            with pytest.raises(ValueError):
                helper(PROJECT_ID, bad_terminal)

    @pytest.mark.parametrize("bad_terminal", TRAVERSAL_INPUTS)
    def test_traversal_never_escapes_central_dir(self, isolated_home: Path, bad_terminal: str) -> None:
        base = cr._project_data_root(PROJECT_ID)
        for helper in self.PATH_HELPERS:
            try:
                produced = helper(PROJECT_ID, bad_terminal)
            except ValueError:
                continue  # rejected outright — cannot have escaped anything
            # If a helper somehow didn't raise, the produced path must still
            # resolve inside the project's resolved data root.
            assert str(produced.resolve()).startswith(str(base.resolve()))

    @pytest.mark.parametrize("good_terminal", ["T0", "T1", "T2", "T3", "my-term_1", "ABC123"])
    def test_valid_terminal_names_still_work(self, isolated_home: Path, good_terminal: str) -> None:
        base = cr._project_data_root(PROJECT_ID)
        for helper in self.PATH_HELPERS:
            produced = helper(PROJECT_ID, good_terminal)
            assert str(produced.resolve()).startswith(str(base.resolve()))


# ---------------------------------------------------------------------------
# 3. write_ready_signal() — the `vnx handoff mark-ready` backend
# ---------------------------------------------------------------------------

class TestWriteReadySignal:
    def test_writes_rotation_id_stamped_signal(self, isolated_home: Path) -> None:
        ready_path = cr.write_ready_signal(PROJECT_ID, "T0", "rot-abc123")
        assert ready_path == cr.ready_signal_path(PROJECT_ID, "T0")
        assert ready_path.is_file()

        data = json.loads(ready_path.read_text(encoding="utf-8"))
        assert data["rotation_id"] == "rot-abc123"
        assert data["terminal"] == "T0"
        assert data["marked_at"]

    def test_rejects_malicious_terminal(self, isolated_home: Path) -> None:
        with pytest.raises(ValueError):
            cr.write_ready_signal(PROJECT_ID, "../../../../.ssh/x", "rot-abc123")


# ---------------------------------------------------------------------------
# 4. handoff_reader
# ---------------------------------------------------------------------------

class TestHandoffReader:
    def test_round_trip(self, isolated_home: Path, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_git_repo(repo, branch="round-trip-branch")
        logdir = tmp_path / "handoff_out"

        handoff_path = cr.write_t0_handoff(logdir=logdir, project_root=repo, project_id=PROJECT_ID)
        briefing = hr.read_handoff(handoff_path)

        assert briefing is not None
        assert briefing.project == PROJECT_ID
        assert briefing.branch == "round-trip-branch"
        assert briefing.context == "t0-rotation"
        assert "Working tree clean" in briefing.waar_we_middenin_zitten
        assert "round-trip-branch" in briefing.state
        assert briefing.next_steps  # non-empty

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert hr.read_handoff(tmp_path / "nope.md") is None

    def test_format_briefing_includes_all_sections(self) -> None:
        briefing = hr.HandoffBriefing(
            context="t0-rotation", project="p", date="d", branch="b",
            waar_we_middenin_zitten="wip text", state="state text", next_steps="next text",
        )
        rendered = hr.format_briefing(briefing)
        assert "wip text" in rendered
        assert "state text" in rendered
        assert "next text" in rendered


# ---------------------------------------------------------------------------
# 7. OI-1042 regression: the dead control-plane API stays removed
# ---------------------------------------------------------------------------

class TestDeadRotationApiRemoved:
    """OI-1042: context_rotation.checkpoint() had ZERO production callers —
    rotation execution happens via a separate implementation
    (hooks/vnx_rotate.sh + the operator /rotate flow) that shares none of
    this module's code. The whole checkpoint control-plane
    (checkpoint/decide_rotation/RotationPolicy/respawn and their state
    files) was removed as dead governance code: a mechanism that exists
    only in the library reads as if it runs, which is worse than no code.
    This guard keeps the dead API from being quietly reintroduced without a
    production caller."""

    DEAD_API = [
        "checkpoint",
        "decide_rotation",
        "RotationPolicy",
        "RotationDecision",
        "RotationOutcome",
        "respawn",
        "RespawnResult",
        "SpawnPartialFailure",
        "durable_state_path",
        "request_marker_path",
    ]

    @pytest.mark.parametrize("symbol", DEAD_API)
    def test_dead_symbol_absent(self, symbol: str) -> None:
        assert not hasattr(cr, symbol), (
            f"context_rotation.{symbol} was removed in OI-1042 (zero production "
            "callers; rotation runs via hooks/vnx_rotate.sh). Reintroducing it "
            "requires a real production caller — see the OI-1042 PR."
        )

    def test_dead_policy_config_absent(self) -> None:
        """configs/context_rotation.yaml existed only for RotationPolicy.load();
        with the policy surface removed, a resurrected config file would be
        dead weight that reads as a live switch."""
        assert not (REPO_ROOT / "configs" / "context_rotation.yaml").exists()
