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
  5. session_stop_rotation.py hook: no-op (and no handoff write) when
     VNX_T0_ROTATION is unset; writes handoff.md when set; never lets a
     worker session clobber the T0 handoff.
  6. OI-1042 regression: the removed dead control-plane API
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
HOOK_PATH = REPO_ROOT / "scripts" / "hooks" / "session_stop_rotation.py"
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
# 5. session_stop_rotation.py hook
# ---------------------------------------------------------------------------

class TestSessionStopHook:
    def _run_hook(self, cwd: Path, env: Dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps({"cwd": str(cwd), "session_id": "test-session"}),
            capture_output=True, text=True, cwd=str(cwd), env=env, timeout=15,
        )

    def test_noop_when_flag_unset(self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        (repo / ".vnx-project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")

        env = {**__import__("os").environ, "HOME": str(isolated_home)}
        env.pop("VNX_T0_ROTATION", None)

        result = self._run_hook(repo, env)
        assert result.returncode == 0
        assert result.stdout.strip() == "{}"
        assert not cr.rotation_handoff_dir(PROJECT_ID, "T0").joinpath(cr.HANDOFF_FILENAME).is_file()

    def test_writes_handoff_when_flag_set(self, isolated_home: Path, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        (repo / ".vnx-project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")

        env = {**__import__("os").environ, "HOME": str(isolated_home), "VNX_T0_ROTATION": "1"}
        # Simulate a genuine interactive T0 session (no dispatch/signal env) —
        # strip whatever this test process itself may have inherited from
        # running as a dispatch worker (VNX_DISPATCH_ID/VNX_TMUX_SIGNAL_DIR),
        # so the test is deterministic regardless of the ambient shell it
        # runs under.
        env.pop("VNX_DISPATCH_ID", None)
        env.pop("VNX_TMUX_SIGNAL_DIR", None)

        result = self._run_hook(repo, env)
        assert result.returncode == 0
        assert result.stdout.strip() == "{}"
        assert cr.rotation_handoff_dir(PROJECT_ID, "T0").joinpath(cr.HANDOFF_FILENAME).is_file()

    def _seed_t0_handoff_sentinel(self, tmp_path: Path) -> Path:
        """Pre-existing T0 handoff (simulating a prior real rotation) that a
        stopping worker's Stop hook must never clobber."""
        handoff_dir = cr.rotation_handoff_dir(PROJECT_ID, "T0")
        handoff_dir.mkdir(parents=True, exist_ok=True)
        sentinel = handoff_dir / cr.HANDOFF_FILENAME
        sentinel.write_text("SENTINEL-DO-NOT-OVERWRITE\n", encoding="utf-8")
        return sentinel

    def test_noop_for_non_t0_terminal_env(self, isolated_home: Path, tmp_path: Path) -> None:
        """Codex P2: the empty-matcher Stop hook fires for every session.
        A stopping T1 (VNX_TERMINAL=T1) must not write/overwrite the T0
        handoff."""
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        (repo / ".vnx-project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")
        sentinel = self._seed_t0_handoff_sentinel(tmp_path)

        env = {
            **__import__("os").environ, "HOME": str(isolated_home),
            "VNX_T0_ROTATION": "1", "VNX_TERMINAL": "T1",
        }

        result = self._run_hook(repo, env)
        assert result.returncode == 0
        assert result.stdout.strip() == "{}"
        assert sentinel.read_text(encoding="utf-8") == "SENTINEL-DO-NOT-OVERWRITE\n"

    def test_noop_for_worker_terminal_cwd(self, isolated_home: Path, tmp_path: Path) -> None:
        """Same as above but detected purely from cwd (no env var set) —
        mirrors how a real T1/T2/T3 worker session's cwd looks."""
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        (repo / ".vnx-project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")
        sentinel = self._seed_t0_handoff_sentinel(tmp_path)

        worker_cwd = repo / ".claude" / "terminals" / "T2"
        worker_cwd.mkdir(parents=True, exist_ok=True)

        env = {**__import__("os").environ, "HOME": str(isolated_home), "VNX_T0_ROTATION": "1"}
        env.pop("VNX_TERMINAL", None)
        env.pop("VNX_TERMINAL_ID", None)

        result = self._run_hook(worker_cwd, env)
        assert result.returncode == 0
        assert result.stdout.strip() == "{}"
        assert sentinel.read_text(encoding="utf-8") == "SENTINEL-DO-NOT-OVERWRITE\n"

    def test_noop_for_dispatch_worker_env(self, isolated_home: Path, tmp_path: Path) -> None:
        """OI-619 finding #2: a tmux-spawn dispatch worker inherits
        VNX_T0_ROTATION=1 from the parent T0 environment but is NOT T0 — it
        has VNX_DISPATCH_ID set, no VNX_TERMINAL, and runs in an isolated
        dispatch worktree whose cwd does not match .claude/terminals/T{1,2,3}
        either. Before the fix this fell through to the cwd fallback's
        "no worker-cwd match -> assume T0" default and could clobber the
        real T0 handoff on Stop."""
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        (repo / ".vnx-project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")
        sentinel = self._seed_t0_handoff_sentinel(tmp_path)

        worker_cwd = tmp_path / ".vnx-data" / "worktrees" / "dispatch-20260713-oi619-worker"
        worker_cwd.mkdir(parents=True, exist_ok=True)
        _make_git_repo(worker_cwd)
        (worker_cwd / ".vnx-project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")

        env = {
            **__import__("os").environ, "HOME": str(isolated_home),
            "VNX_T0_ROTATION": "1", "VNX_DISPATCH_ID": "20260713-oi619-worker",
        }
        env.pop("VNX_TERMINAL", None)
        env.pop("VNX_TERMINAL_ID", None)

        result = self._run_hook(worker_cwd, env)
        assert result.returncode == 0
        assert result.stdout.strip() == "{}"
        assert sentinel.read_text(encoding="utf-8") == "SENTINEL-DO-NOT-OVERWRITE\n"

    def test_dispatch_env_does_not_override_explicit_t0_terminal(self, isolated_home: Path, tmp_path: Path) -> None:
        """A real T0 Stop must still write even if VNX_DISPATCH_ID happens to
        be set in its environment (e.g. T0 itself was launched by a
        dispatcher) — VNX_TERMINAL=T0 wins over the dispatch/signal check."""
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        (repo / ".vnx-project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")

        env = {
            **__import__("os").environ, "HOME": str(isolated_home),
            "VNX_T0_ROTATION": "1", "VNX_TERMINAL": "T0", "VNX_DISPATCH_ID": "some-dispatch-id",
        }

        result = self._run_hook(repo, env)
        assert result.returncode == 0
        assert result.stdout.strip() == "{}"
        assert cr.rotation_handoff_dir(PROJECT_ID, "T0").joinpath(cr.HANDOFF_FILENAME).is_file()

    def test_t0_terminal_env_still_writes(self, isolated_home: Path, tmp_path: Path) -> None:
        """Control case: an explicit VNX_TERMINAL=T0 must still write —
        scoping must not turn into a blanket no-op."""
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        (repo / ".vnx-project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")

        env = {
            **__import__("os").environ, "HOME": str(isolated_home),
            "VNX_T0_ROTATION": "1", "VNX_TERMINAL": "T0",
        }

        result = self._run_hook(repo, env)
        assert result.returncode == 0
        assert result.stdout.strip() == "{}"
        assert cr.rotation_handoff_dir(PROJECT_ID, "T0").joinpath(cr.HANDOFF_FILENAME).is_file()


# ---------------------------------------------------------------------------
# 6. OI-1042 finding: the settings.json Stop-hook wrapper around
#    session_stop_rotation.py has ZERO filesystem side effects (no
#    .vnx-data/logs dir, no .err file) when VNX_T0_ROTATION is unset.
# ---------------------------------------------------------------------------

class TestSessionStopSettingsJsonWrapper:
    def _wrapper_command(self) -> str:
        settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        for entry in settings["hooks"]["Stop"]:
            cmd = entry["hooks"][0]["command"]
            if "session_stop_rotation.py" in cmd:
                return cmd
        raise AssertionError("session_stop_rotation.py Stop hook not found in .claude/settings.json")

    def _run_wrapper(self, repo: Path, env: Dict[str, str]) -> subprocess.CompletedProcess:
        import shlex
        cmd = self._wrapper_command()
        assert cmd.startswith("bash -c ")
        body = shlex.split(cmd)[2]
        return subprocess.run(
            ["bash", "-c", body],
            input=json.dumps({"cwd": str(repo), "session_id": "test-session"}),
            capture_output=True, text=True, cwd=str(repo), env=env, timeout=15,
        )

    def _make_repo_with_real_scripts(self, repo: Path) -> None:
        """The wrapper resolves ROOT via `git rev-parse --show-toplevel`
        inside the fixture repo, so `$ROOT/scripts/hooks/...` must resolve —
        symlink in the real scripts/ tree (its own `_LIB_DIR` bootstrap
        follows `__file__.resolve()` through the symlink to the real
        scripts/lib) instead of duplicating hook code into the fixture."""
        _make_git_repo(repo)
        (repo / ".vnx-project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")
        (repo / "scripts").symlink_to(REPO_ROOT / "scripts")

    def test_disabled_creates_no_logs_dir_or_err_file(self, isolated_home: Path, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        self._make_repo_with_real_scripts(repo)

        env = {**__import__("os").environ, "HOME": str(isolated_home)}
        env.pop("VNX_T0_ROTATION", None)

        result = self._run_wrapper(repo, env)
        assert result.returncode == 0
        logs_dir = repo / ".vnx-data" / "logs"
        assert not logs_dir.exists(), "disabled feature must create zero filesystem side effects"
        assert not (logs_dir / "session_stop_rotation.err").exists()

    def test_enabled_still_creates_logs_dir_and_err_file(self, isolated_home: Path, tmp_path: Path) -> None:
        """Regression: enabling the feature must retain the pre-existing
        mkdir + stderr-capture + handoff-write behavior."""
        repo = tmp_path / "repo"
        self._make_repo_with_real_scripts(repo)

        env = {**__import__("os").environ, "HOME": str(isolated_home), "VNX_T0_ROTATION": "1"}
        env.pop("VNX_DISPATCH_ID", None)
        env.pop("VNX_TMUX_SIGNAL_DIR", None)

        result = self._run_wrapper(repo, env)
        assert result.returncode == 0
        logs_dir = repo / ".vnx-data" / "logs"
        assert logs_dir.is_dir()
        assert (logs_dir / "session_stop_rotation.err").exists()
        assert cr.rotation_handoff_dir(PROJECT_ID, "T0").joinpath(cr.HANDOFF_FILENAME).is_file()


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
