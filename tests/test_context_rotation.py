#!/usr/bin/env python3
"""Tests for the T0 context-rotation control-plane (rev 3, default OFF).

Design authority: claudedocs/plans/t0-context-rotation-revival.md.

Covers:
  1. RotationPolicy.load(): default-off (no yaml, no env); env overrides yaml;
     explicit disabled policy wins regardless of env.
  2. decide_rotation truth table, incl. durable boundary-count debounce
     across simulated sessions (a fresh checkpoint() call re-reads the
     durable JSON from disk each time — no in-memory session state).
  3. checkpoint(): writes marker+handoff+durable-timestamp on a decided
     rotation; is a strict no-op when disabled (zero filesystem side
     effects); is idempotent against a duplicate in-flight call; debounce
     persists across separate checkpoint() calls ("sessions"); an ABORTed
     respawn does NOT advance the durable counter/timestamp and does NOT
     emit the continuation receipt, but DOES allow a later retry.
  4. respawn(): ready-signal -> success; no-ready-within-timeout -> ABORT
     (retains handoff/marker, reaps ONLY the orphan session it created,
     never a "kill"/"vnx start" of any existing/current session).
  5. write_t0_handoff(): frontmatter + all three sections present,
     project_id-scoped, fail-soft when git/horizon reads fail.
  6. handoff_reader: round-trips a written handoff.md.
  7. session_stop_rotation.py hook: no-op (and no handoff write) when
     VNX_T0_ROTATION is unset; writes handoff.md when set.
  8. Guard-safety: the default tmux spawn implementation's argv[0] is always
     "tmux" — "claude" never appears as an invoked executable, only as a
     literal send-keys payload with no flags.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

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


def _enabled_policy(**overrides: Any) -> cr.RotationPolicy:
    base = dict(enabled=True, min_boundaries_between_rotations=0, respawn="off")
    base.update(overrides)
    return cr.RotationPolicy(**base)


# ---------------------------------------------------------------------------
# 1. RotationPolicy.load()
# ---------------------------------------------------------------------------

class TestRotationPolicyLoad:
    def test_default_off_no_yaml_no_env(self, tmp_path: Path) -> None:
        policy = cr.RotationPolicy.load(config_path=tmp_path / "missing.yaml", env={})
        assert policy.enabled is False
        assert policy.respawn == "off"
        assert policy.trigger == "governance_boundary"

    def test_shipped_config_is_disabled(self) -> None:
        """The repo-committed configs/context_rotation.yaml must itself ship
        with enabled: false — this is the actual file T0 loads in production."""
        shipped = REPO_ROOT / "configs" / "context_rotation.yaml"
        assert shipped.is_file()
        policy = cr.RotationPolicy.load(config_path=shipped, env={})
        assert policy.enabled is False

    def test_env_var_enables(self, tmp_path: Path) -> None:
        policy = cr.RotationPolicy.load(
            config_path=tmp_path / "missing.yaml", env={"VNX_T0_ROTATION": "1"},
        )
        assert policy.enabled is True

    def test_env_var_non_one_does_not_enable(self, tmp_path: Path) -> None:
        policy = cr.RotationPolicy.load(
            config_path=tmp_path / "missing.yaml", env={"VNX_T0_ROTATION": "true"},
        )
        assert policy.enabled is False

    def test_yaml_enables_when_env_absent(self, tmp_path: Path) -> None:
        cfg = tmp_path / "context_rotation.yaml"
        cfg.write_text("enabled: true\nmin_boundaries_between_rotations: 5\n", encoding="utf-8")
        policy = cr.RotationPolicy.load(config_path=cfg, env={})
        assert policy.enabled is True
        assert policy.min_boundaries_between_rotations == 5

    def test_env_overrides_yaml(self, tmp_path: Path) -> None:
        cfg = tmp_path / "context_rotation.yaml"
        cfg.write_text("enabled: true\n", encoding="utf-8")
        policy = cr.RotationPolicy.load(config_path=cfg, env={"VNX_T0_ROTATION": "0"})
        assert policy.enabled is False

    def test_invalid_respawn_mode_falls_back_to_off(self, tmp_path: Path) -> None:
        cfg = tmp_path / "context_rotation.yaml"
        cfg.write_text("respawn: something_destructive\n", encoding="utf-8")
        policy = cr.RotationPolicy.load(config_path=cfg, env={})
        assert policy.respawn == "off"


# ---------------------------------------------------------------------------
# 2. decide_rotation truth table
# ---------------------------------------------------------------------------

class TestDecideRotation:
    def test_disabled_never_rotates(self) -> None:
        policy = cr.RotationPolicy(enabled=False, min_boundaries_between_rotations=0)
        decision = cr.decide_rotation(
            policy=policy, at_governance_boundary=True, boundaries_since_last_rotation=99,
        )
        assert decision.should_rotate is False
        assert decision.reason == "disabled"

    def test_mid_action_never_rotates(self) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=0)
        decision = cr.decide_rotation(
            policy=policy, at_governance_boundary=True, boundaries_since_last_rotation=99, mid_action=True,
        )
        assert decision.should_rotate is False
        assert decision.reason == "mid_action"

    def test_not_at_boundary_never_rotates(self) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=0)
        decision = cr.decide_rotation(
            policy=policy, at_governance_boundary=False, boundaries_since_last_rotation=99,
        )
        assert decision.should_rotate is False
        assert decision.reason == "not_at_boundary"

    def test_debounced_below_min_boundaries(self) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=3)
        decision = cr.decide_rotation(
            policy=policy, at_governance_boundary=True, boundaries_since_last_rotation=2,
        )
        assert decision.should_rotate is False
        assert decision.reason == "debounced"

    def test_rotates_once_debounce_clears(self) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=3)
        decision = cr.decide_rotation(
            policy=policy, at_governance_boundary=True, boundaries_since_last_rotation=3,
        )
        assert decision.should_rotate is True
        assert decision.reason == "boundary_debounce_cleared"

    def test_pct_ceiling_backstop_bypasses_debounce(self) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=10, pct_ceiling=80.0)
        decision = cr.decide_rotation(
            policy=policy, at_governance_boundary=True, boundaries_since_last_rotation=1, context_pct=85.0,
        )
        assert decision.should_rotate is True
        assert decision.reason == "pct_ceiling_backstop"

    def test_pct_below_ceiling_does_not_bypass_debounce(self) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=10, pct_ceiling=80.0)
        decision = cr.decide_rotation(
            policy=policy, at_governance_boundary=True, boundaries_since_last_rotation=1, context_pct=50.0,
        )
        assert decision.should_rotate is False
        assert decision.reason == "debounced"

    def test_pct_backstop_still_requires_boundary(self) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=10, pct_ceiling=80.0)
        decision = cr.decide_rotation(
            policy=policy, at_governance_boundary=False, boundaries_since_last_rotation=1, context_pct=99.0,
        )
        assert decision.should_rotate is False
        assert decision.reason == "not_at_boundary"

    def test_durable_debounce_across_simulated_sessions(self, isolated_home: Path) -> None:
        """Each checkpoint() call re-reads durable state from disk — this is
        what makes the debounce durable ACROSS separate 'sessions' (process
        invocations), not just in-memory within one."""
        policy = _enabled_policy(min_boundaries_between_rotations=3, respawn="off")

        # "Session A": three boundary calls, none should rotate yet (0,1,2 < 3).
        for expected_before in range(3):
            durable = cr._load_durable(cr.durable_state_path(PROJECT_ID, "T0"))
            assert durable["boundaries_since_last_rotation"] == expected_before
            out = cr.checkpoint(project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT))
            assert out.rotated is False

        # "Session B" (a brand new checkpoint() call, simulating a fresh
        # process): the durable counter now reads 3 from disk and rotates.
        out = cr.checkpoint(project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT))
        assert out.rotated is True

        # "Session C": counter was reset to 0 on the confirmed rotation —
        # immediately debounced again.
        out2 = cr.checkpoint(project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT))
        assert out2.rotated is False
        assert out2.reason == "debounced"


# ---------------------------------------------------------------------------
# 3. checkpoint()
# ---------------------------------------------------------------------------

class TestCheckpoint:
    def test_disabled_is_zero_side_effect_noop(self, isolated_home: Path) -> None:
        policy = cr.RotationPolicy(enabled=False)
        out = cr.checkpoint(project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT))
        assert out.rotated is False
        assert out.reason == "disabled"
        # Not even the rotation state directory should have been created.
        assert not cr.rotation_state_dir(PROJECT_ID).exists()

    def test_explicit_disabled_policy_wins_over_env(self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_T0_ROTATION", "1")
        policy = cr.RotationPolicy(enabled=False)
        out = cr.checkpoint(project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT))
        assert out.rotated is False
        assert out.reason == "disabled"
        assert not cr.rotation_state_dir(PROJECT_ID).exists()

    def test_rotation_writes_marker_handoff_and_durable_timestamp(self, isolated_home: Path) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=0, respawn="off")
        out = cr.checkpoint(project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT))

        assert out.rotated is True
        assert out.handoff_path is not None
        assert out.handoff_path.is_file()
        assert out.marker_path is not None

        marker = json.loads(out.marker_path.read_text(encoding="utf-8"))
        assert marker["status"] == "success"
        assert marker["rotation_id"] == out.rotation_id

        durable = cr._load_durable(cr.durable_state_path(PROJECT_ID, "T0"))
        assert durable["boundaries_since_last_rotation"] == 0
        assert durable["last_rotation_at"] is not None

    def test_idempotent_duplicate_call_is_noop(self, isolated_home: Path) -> None:
        """A duplicate checkpoint() call while a rotation is 'in_progress'
        must not write a second handoff/marker. min_boundaries=0 isolates
        this from the normal counter-debounce (which would also block a
        second call, masking whether the marker itself is doing anything)."""
        policy = _enabled_policy(min_boundaries_between_rotations=0, respawn="off")

        request_path = cr.request_marker_path(PROJECT_ID, "T0")
        cr._write_json_atomic(request_path, {
            "rotation_id": "already-running",
            "status": "in_progress",
            "created_at": cr._iso(cr._utc_now()),
        })

        out = cr.checkpoint(project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT))
        assert out.rotated is False
        assert out.reason == "already_in_progress"
        assert out.rotation_id == "already-running"
        # No handoff was written for this call.
        assert not cr.rotation_handoff_dir(PROJECT_ID, "T0").joinpath(cr.HANDOFF_FILENAME).is_file()

    def test_stale_in_progress_marker_allows_retry(self, isolated_home: Path) -> None:
        """An in_progress marker older than request_ttl_seconds is treated as
        a crashed attempt, not a live duplicate — the next call proceeds."""
        policy = _enabled_policy(min_boundaries_between_rotations=0, respawn="off")
        request_path = cr.request_marker_path(PROJECT_ID, "T0")
        cr._write_json_atomic(request_path, {
            "rotation_id": "crashed-attempt",
            "status": "in_progress",
            "created_at": "2000-01-01T00:00:00Z",
        })

        out = cr.checkpoint(
            project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT), request_ttl_seconds=1.0,
        )
        assert out.rotated is True
        assert out.rotation_id != "crashed-attempt"

    def test_aborted_respawn_does_not_advance_debounce_or_emit_receipt(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=0, respawn="tmux_new_session")

        def fake_respawn(**kwargs: Any) -> cr.RespawnResult:
            return cr.RespawnResult(success=False, reason="timeout_no_ready", rotation_id=kwargs["rotation_id"])

        emitted: List[Dict[str, Any]] = []
        monkeypatch.setattr(cr, "_emit_continuation_receipt", lambda **kw: emitted.append(kw))

        out = cr.checkpoint(
            project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT), respawn_fn=fake_respawn,
        )

        assert out.rotated is False
        assert out.reason == "abort:timeout_no_ready"
        assert emitted == []  # finding #1: receipt only fires on a successful rotate

        durable = cr._load_durable(cr.durable_state_path(PROJECT_ID, "T0"))
        assert durable["boundaries_since_last_rotation"] == 0  # never advanced past pre-attempt value
        assert durable["last_rotation_at"] is None  # finding #3: not stamped on abort

        marker = json.loads(cr.request_marker_path(PROJECT_ID, "T0").read_text(encoding="utf-8"))
        assert marker["status"] == "aborted"

        # Handoff is retained (not deleted) so the operator never loses state.
        assert out.handoff_path.is_file()

        # A later retry is not blocked by the aborted marker.
        out2 = cr.checkpoint(
            project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT), respawn_fn=fake_respawn,
        )
        assert out2.reason == "abort:timeout_no_ready"
        assert out2.rotation_id != out.rotation_id

    def test_successful_respawn_emits_continuation_receipt(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=0, respawn="tmux_new_session")

        def fake_respawn(**kwargs: Any) -> cr.RespawnResult:
            return cr.RespawnResult(success=True, reason="ready", rotation_id=kwargs["rotation_id"])

        emitted: List[Dict[str, Any]] = []
        monkeypatch.setattr(cr, "_emit_continuation_receipt", lambda **kw: emitted.append(kw))

        out = cr.checkpoint(
            project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT), respawn_fn=fake_respawn,
        )
        assert out.rotated is True
        assert len(emitted) == 1
        assert emitted[0]["terminal"] == "T0"
        assert emitted[0]["dispatch_id"] == out.rotation_id
        assert emitted[0]["project_id"] == PROJECT_ID

    def test_handoff_write_failure_aborts_and_never_calls_respawn(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=0, respawn="tmux_new_session")

        def boom(**kwargs: Any) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr(cr, "write_t0_handoff", boom)
        respawn_calls: List[Any] = []

        out = cr.checkpoint(
            project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT),
            respawn_fn=lambda **kw: respawn_calls.append(kw) or cr.RespawnResult(success=True, reason="ready"),
        )
        assert out.rotated is False
        assert out.reason == "handoff_write_failed"
        assert respawn_calls == []


# ---------------------------------------------------------------------------
# 3b. P1 regression: rotation must use the canonical data root, never
# first-create a competing ~/.vnx-data/<project_id> central store.
# ---------------------------------------------------------------------------

class TestRotationUsesCanonicalDataRoot:
    """A prior version hardcoded resolve_central_data_dir(project_id) in the
    rotation path helpers. For a project that doesn't already have a central
    ~/.vnx-data/<project_id> (i.e. it resolves to project-local or XDG
    state), the FIRST checkpoint() call would nonetheless CREATE
    ~/.vnx-data/<project_id> as a side effect (via state_dir.mkdir()) —
    after which vnx_paths._resolve_state_root's existence-gated central
    branch prefers that now-existing (but empty) dir over the project's
    real store for every subsequent `vnx track`/`vnx horizon`/`status` call:
    a state-store split-brain. checkpoint() must resolve (and only ever
    write under) whatever store this project's project_root ALREADY
    resolves to via the same canonical resolver the rest of VNX uses.
    """

    def test_checkpoint_never_creates_competing_central_dir(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Opt out of the autouse VNX_DATA_DIR_EXPLICIT override (see the
        # comment in test_project_id_scoped_across_two_projects above) — this
        # test exercises the default central/local/XDG resolution, which the
        # explicit override would otherwise short-circuit.
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

        # The (separate, pre-existing) Phase-6-P3 receipt dual-write mirror
        # legitimately writes a cross-project shadow copy of every appended
        # receipt under resolve_central_data_dir(receipt["project_id"]) —
        # that subsystem is out of scope here and NOT what this regression
        # guards. Stub it out so this test only observes context_rotation's
        # OWN rotation-file path resolution (durable/marker/handoff).
        monkeypatch.setattr(cr, "_emit_continuation_receipt", lambda **kw: None)

        repo = tmp_path / "repo"
        _make_git_repo(repo)
        project_id = "xdg-only-project"

        central_dir = Path.home() / ".vnx-data" / project_id
        assert not central_dir.exists()

        expected_root = vnx_paths._resolve_state_root(project_id, repo)
        # Sanity check on the fixture: with no existing central/local store,
        # the canonical resolver itself lands on the XDG default, not central.
        assert not str(expected_root).startswith(str(Path.home() / ".vnx-data"))

        policy = _enabled_policy(min_boundaries_between_rotations=0, respawn="off")
        out = cr.checkpoint(project_id=project_id, policy=policy, project_root=str(repo))
        assert out.rotated is True

        # Rotation files landed under the SAME root the rest of VNX resolves
        # to for this project — not a hardcoded central path.
        assert str(out.handoff_path.resolve()).startswith(str(expected_root.resolve()))
        assert str(out.marker_path.resolve()).startswith(str(expected_root.resolve()))

        # The P1 bug: checkpoint() must NEVER create a competing
        # ~/.vnx-data/<project_id> rotation/state tree as a side effect when
        # the project doesn't already resolve there.
        assert not central_dir.exists()

        # The store the rest of VNX sees for this project is unchanged
        # before/after the rotation call.
        assert vnx_paths._resolve_state_root(project_id, repo) == expected_root

    def test_horizon_snapshot_reads_from_the_same_resolved_root(
        self, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The handoff's horizon section must read tracks from the SAME
        store checkpoint()/write_t0_handoff() resolves to — not a
        hardcoded central dir the project never actually uses (the exact
        failure mode described in the P1 finding: the handoff missed the
        real NOW/NEXT tracks because it read from the wrong, freshly-created
        empty central store)."""
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

        repo = tmp_path / "repo"
        _make_git_repo(repo)
        project_id = "xdg-horizon-project"

        resolved_root = vnx_paths._resolve_state_root(project_id, repo)
        assert not resolved_root.exists()

        logdir = tmp_path / "handoff_out"
        handoff_path = cr.write_t0_handoff(logdir=logdir, project_root=repo, project_id=project_id)

        # write_t0_handoff must have looked for tracks under resolved_root
        # (which it just created via mkdir-on-demand inside tracks setup, or
        # left absent if tracks lazily no-ops) — never under a central dir
        # this project doesn't use.
        assert not (Path.home() / ".vnx-data" / project_id).exists()
        assert handoff_path.is_file()


# ---------------------------------------------------------------------------
# 4. respawn()
# ---------------------------------------------------------------------------

class TestRespawn:
    def test_ready_signal_yields_success(self, isolated_home: Path) -> None:
        spawn_calls: List[Any] = []

        def fake_spawn(session_name: str, project_root: str, resume_prompt: str) -> None:
            spawn_calls.append(session_name)
            cr.write_ready_signal(PROJECT_ID, "T0", "rot-ready")

        result = cr.respawn(
            handoff_path=Path("handoff.md"), terminal="T0", project_id=PROJECT_ID,
            project_root=REPO_ROOT, rotation_id="rot-ready", tmux_spawn_fn=fake_spawn,
            timeout_seconds=5, poll_interval_seconds=0.01,
        )
        assert result.success is True
        assert result.reason == "ready"
        assert spawn_calls == [result.session_name]

    def test_stale_ready_with_different_rotation_id_does_not_confirm(self, isolated_home: Path) -> None:
        """Finding #6: a .ready left over from a PREVIOUS rotation must not
        false-confirm a new one."""
        cr.write_ready_signal(PROJECT_ID, "T0", "old-rotation")

        killed: List[str] = []
        result = cr.respawn(
            handoff_path=Path("handoff.md"), terminal="T0", project_id=PROJECT_ID,
            project_root=REPO_ROOT, rotation_id="new-rotation",
            tmux_spawn_fn=lambda *a: None, tmux_kill_fn=lambda name: killed.append(name),
            timeout_seconds=0.05, poll_interval_seconds=0.01,
        )
        assert result.success is False
        assert result.reason == "timeout_no_ready"
        assert killed == [result.session_name]

    def test_no_ready_within_timeout_aborts_and_reaps_only_orphan(
        self, isolated_home: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        killed: List[str] = []
        spawned: List[str] = []

        def fake_spawn(session_name: str, project_root: str, resume_prompt: str) -> None:
            spawned.append(session_name)  # never writes .ready

        with caplog.at_level("ERROR"):
            result = cr.respawn(
                handoff_path=Path("handoff.md"), terminal="T0", project_id=PROJECT_ID,
                project_root=REPO_ROOT, rotation_id="rot-timeout", tmux_spawn_fn=fake_spawn,
                tmux_kill_fn=lambda name: killed.append(name),
                timeout_seconds=0.05, poll_interval_seconds=0.01,
            )

        assert result.success is False
        assert result.reason == "timeout_no_ready"
        # Non-destructive: the ONLY session reaped is the one THIS call spawned.
        assert killed == spawned == [result.session_name]
        assert "ABORT" in caplog.text

    def test_spawn_failure_never_calls_kill(self, isolated_home: Path) -> None:
        """A spawn that raises before any session exists must not attempt to
        kill anything — there is nothing to reap."""
        killed: List[str] = []

        def failing_spawn(*args: Any) -> None:
            raise RuntimeError("tmux not found")

        result = cr.respawn(
            handoff_path=Path("handoff.md"), terminal="T0", project_id=PROJECT_ID,
            project_root=REPO_ROOT, rotation_id="rot-spawnfail", tmux_spawn_fn=failing_spawn,
            tmux_kill_fn=lambda name: killed.append(name),
            timeout_seconds=1, poll_interval_seconds=0.01,
        )
        assert result.success is False
        assert result.reason.startswith("spawn_failed")
        assert killed == []

    def test_new_session_ok_then_send_keys_raises_reaps_partial_session(
        self, isolated_home: Path,
    ) -> None:
        """Codex P2: `tmux new-session` succeeds but a later `send-keys`
        raises. The session that WAS created must be reaped — not left as
        an orphan/duplicate T0 — and the call must return a clean failure."""
        killed: List[str] = []
        calls: List[List[str]] = []

        def flaky_run(cmd: List[str], *args: Any, **kwargs: Any) -> Any:
            calls.append(list(cmd))
            if cmd[:2] == ["tmux", "new-session"]:
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[:2] == ["tmux", "send-keys"]:
                raise subprocess.CalledProcessError(1, cmd)
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        import unittest.mock as mock
        with mock.patch("context_rotation.subprocess.run", side_effect=flaky_run), \
             mock.patch("context_rotation.time.sleep", lambda *_: None):
            result = cr.respawn(
                handoff_path=Path("handoff.md"), terminal="T0", project_id=PROJECT_ID,
                project_root=REPO_ROOT, rotation_id="rot-partial",
                tmux_spawn_fn=cr._default_tmux_spawn,
                tmux_kill_fn=lambda name: killed.append(name),
                timeout_seconds=1, poll_interval_seconds=0.01,
            )

        assert result.success is False
        assert result.reason.startswith("spawn_partial_failure")
        # The session that new-session actually created is the ONE reaped —
        # no orphan left behind, and no duplicate/zero T0.
        assert killed == [result.session_name]
        assert any(c[:2] == ["tmux", "new-session"] for c in calls)

    def test_never_calls_a_destructive_or_kill_path_on_success(self, isolated_home: Path) -> None:
        """Assert the whole respawn() call graph, when it succeeds, contains
        zero destructive verbs anywhere (no kill-session, no `vnx start`)."""
        calls: List[List[str]] = []
        real_run = subprocess.run

        def recording_run(cmd: List[str], *args: Any, **kwargs: Any) -> Any:
            calls.append(list(cmd))
            if cmd[:2] == ["tmux", "new-session"]:
                return real_run(["true"], check=True)
            if cmd[:2] == ["tmux", "send-keys"]:
                return real_run(["true"], check=True)
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        import unittest.mock as mock
        with mock.patch("context_rotation.subprocess.run", side_effect=recording_run), \
             mock.patch("context_rotation.time.sleep", lambda *_: None):
            def spawn_and_confirm(session_name: str, project_root: str, resume_prompt: str) -> None:
                cr._default_tmux_spawn(session_name, project_root, resume_prompt, boot_delay_seconds=0)
                cr.write_ready_signal(PROJECT_ID, "T0", "rot-real")

            result = cr.respawn(
                handoff_path=Path("handoff.md"), terminal="T0", project_id=PROJECT_ID,
                project_root=REPO_ROOT, rotation_id="rot-real", tmux_spawn_fn=spawn_and_confirm,
                timeout_seconds=2, poll_interval_seconds=0.01,
            )

        assert result.success is True
        assert calls  # at least new-session + send-keys calls recorded
        for cmd in calls:
            assert "kill-session" not in cmd
            assert not any("vnx" == c or c.endswith("/vnx") for c in cmd)


# ---------------------------------------------------------------------------
# 4b. Terminal-name path-traversal validation
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
        cr.durable_state_path,
        cr.request_marker_path,
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

    def test_checkpoint_rejects_malicious_terminal(self, isolated_home: Path) -> None:
        policy = _enabled_policy(min_boundaries_between_rotations=0, respawn="off")
        with pytest.raises(ValueError):
            cr.checkpoint(
                project_id=PROJECT_ID, policy=policy, project_root=str(REPO_ROOT),
                terminal="../../../../.ssh/x",
            )

    @pytest.mark.parametrize("good_terminal", ["T0", "T1", "T2", "T3", "my-term_1", "ABC123"])
    def test_valid_terminal_names_still_work(self, isolated_home: Path, good_terminal: str) -> None:
        base = cr._project_data_root(PROJECT_ID)
        for helper in self.PATH_HELPERS:
            produced = helper(PROJECT_ID, good_terminal)
            assert str(produced.resolve()).startswith(str(base.resolve()))


# ---------------------------------------------------------------------------
# 5. write_t0_handoff()
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


# ---------------------------------------------------------------------------
# 6. handoff_reader
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
# 7. session_stop_rotation.py hook
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
# 8. Guard-safety: argv[0] is always "tmux", never "claude"
# ---------------------------------------------------------------------------

class TestGuardSafeSpawnShape:
    def test_default_tmux_spawn_never_invokes_claude_as_executable(self) -> None:
        calls: List[List[str]] = []

        import unittest.mock as mock
        with mock.patch("context_rotation.subprocess.run") as run_mock, \
             mock.patch("context_rotation.time.sleep", lambda *_: None):
            run_mock.side_effect = lambda cmd, **kw: calls.append(list(cmd))
            cr._default_tmux_spawn("vnx-t0-rotation-t0-abc123", "/tmp/repo", "resume prompt text", boot_delay_seconds=0)

        assert calls, "expected at least one subprocess.run call"
        for cmd in calls:
            assert cmd[0] == "tmux", f"argv[0] must always be tmux, got: {cmd}"
            # "claude" (when present) is a literal send-keys payload, never argv[0].
            assert cmd[0] != "claude"
        dangerous_flags = {"-p", "--print", "--dangerously-skip-permissions"}
        for cmd in calls:
            assert not (dangerous_flags & set(cmd)), f"dangerous flag present in {cmd}"
