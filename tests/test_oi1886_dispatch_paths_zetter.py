"""Tests for OI-1886 — VNX_DISPATCH_PATHS / VNX_WORKER_ROLE regain a setter.

Measured 27-09: scripts/hooks/pretooluse_worker_scope_enforce.py:151 is the
ONLY reader of VNX_DISPATCH_PATHS, and its one setter
(``TmuxInteractiveDispatch._spawn_session``) was removed with the tmux lane
itself (#1868, 2026-09-18). A dispatch declaring ``dispatch_paths`` was no
longer narrowed on ANY lane — only the coarse role scope still applied.

Covers, per lane, that the real production setter populates the worker
subprocess env with VNX_DISPATCH_PATHS (when the dispatch declared paths) and
VNX_WORKER_ROLE (when a role is known):

  (a) headless lane   — envelope_adapters_claude.ClaudeSubprocessAdapter.run
  (a) subprocess lane — subprocess_dispatch.deliver_via_subprocess
  (a) provider lanes  — provider_dispatch._worker_role_env (direct + via the
                         envelope_adapters_provider.ProviderAdapter codex branch)
  (b) end-to-end      — the ACTUAL env dict a lane setter (headless lane, here)
                         produces is fed into the real hook subprocess with
                         VNX_ENFORCE_WORKER_PERMISSIONS=1: a write outside the
                         declared paths is refused, one inside is not.
  (c) no dispatch_paths declared -> the var is never set (any lane).

No real claude/codex/provider process is ever spawned — every spawn_* call is
patched at its import site.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from dispatch_spec import DispatchPath, PathAccess, Provider  # noqa: E402
from envelope_adapters_claude import ClaudeSubprocessAdapter  # noqa: E402
from envelope_types import EnvelopeSpec  # noqa: E402
from provider_dispatch import _worker_role_env  # noqa: E402
from subprocess_dispatch import deliver_via_subprocess  # noqa: E402

HOOK_DIR = REPO_ROOT / "scripts" / "hooks"
HOOK_SH = HOOK_DIR / "pretooluse_worker_scope_enforce.sh"

ROLE = "backend-developer"  # file_write_scope: scripts/**, tests/**, dashboard/**


def _make_envelope_spec(tmp_path: Path, *, role=ROLE, dispatch_paths=()) -> EnvelopeSpec:
    # dispatch_paths is set via setattr rather than a constructor kwarg on
    # purpose: EnvelopeSpec.dispatch_paths is itself part of this OI-1886 fix,
    # and a constructor kwarg would TypeError on pre-fix code (an "unexpected
    # keyword argument", i.e. a missing-symbol failure, never a valid red-run
    # signal per the fabric's own measured antipattern — see
    # tests/test_dispatch_spec.py history / memory: "rode run op een
    # ontbrekend symbool is geen rode run"). setattr on a plain (non-slotted)
    # dataclass instance always succeeds; pre-fix ClaudeSubprocessAdapter.run
    # simply never reads the attribute, which is the actual historical bug
    # behavior this test pins.
    spec = EnvelopeSpec(
        dispatch_id="d-oi1886-headless",
        terminal_id="T1",
        provider="claude",
        model="sonnet",
        instruction="do work",
        role=role,
        pr_id=None,
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    spec.dispatch_paths = dispatch_paths
    return spec


def _fake_claude_spawn_result(**overrides):
    base = dict(
        error=None,
        timed_out=False,
        stopped_early=False,
        returncode=0,
        completion_text="",
        session_id="sess-test",
        token_usage=None,
        model=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# (a) headless lane: envelope_adapters_claude.ClaudeSubprocessAdapter
# ---------------------------------------------------------------------------


class TestHeadlessLaneExportsDispatchPathsAndRole:
    def test_declared_paths_and_role_are_exported(self, tmp_path):
        captured = {}

        def fake_spawn_claude(**kwargs):
            captured.update(kwargs)
            return _fake_claude_spawn_result()

        spec = _make_envelope_spec(
            tmp_path,
            dispatch_paths=("scripts/lib/foo.py:read_write", "tests/**:read_write"),
        )
        with patch("provider_spawns.claude_spawn.spawn_claude", side_effect=fake_spawn_claude):
            ClaudeSubprocessAdapter().run(spec, cwd=tmp_path)

        extra_env = captured["extra_env"]
        assert extra_env["VNX_WORKER_ROLE"] == ROLE
        assert json.loads(extra_env["VNX_DISPATCH_PATHS"]) == [
            "scripts/lib/foo.py:read_write",
            "tests/**:read_write",
        ]

    def test_no_declared_paths_leaves_var_unset(self, tmp_path):
        captured = {}

        def fake_spawn_claude(**kwargs):
            captured.update(kwargs)
            return _fake_claude_spawn_result()

        spec = _make_envelope_spec(tmp_path, dispatch_paths=())
        with patch("provider_spawns.claude_spawn.spawn_claude", side_effect=fake_spawn_claude):
            ClaudeSubprocessAdapter().run(spec, cwd=tmp_path)

        extra_env = captured["extra_env"]
        assert "VNX_DISPATCH_PATHS" not in extra_env
        # Role is independent of dispatch_paths — still exported when set.
        assert extra_env["VNX_WORKER_ROLE"] == ROLE

    def test_no_role_leaves_role_var_unset(self, tmp_path):
        captured = {}

        def fake_spawn_claude(**kwargs):
            captured.update(kwargs)
            return _fake_claude_spawn_result()

        spec = _make_envelope_spec(tmp_path, role=None, dispatch_paths=())
        with patch("provider_spawns.claude_spawn.spawn_claude", side_effect=fake_spawn_claude):
            ClaudeSubprocessAdapter().run(spec, cwd=tmp_path)

        assert "VNX_WORKER_ROLE" not in captured["extra_env"]


# ---------------------------------------------------------------------------
# (a) terminal-pinned subprocess lane: subprocess_dispatch.deliver_via_subprocess
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_adapter():
    # Same patch point as test_subprocess_role_passthrough.py's mock_adapter:
    # deliver_via_subprocess delegates the actual adapter creation + deliver()
    # call to spawn_claude, which binds SubprocessAdapter at call time.
    with patch("provider_spawns.claude_spawn.SubprocessAdapter") as cls:
        instance = MagicMock()
        instance.was_timed_out.return_value = False
        instance.deliver.return_value = MagicMock(success=True)
        instance.read_events_with_timeout.return_value = iter([])
        instance.get_session_id.return_value = "sess-test"
        obs = MagicMock()
        obs.transport_state = {"returncode": 0}
        instance.observe.return_value = obs
        cls.return_value = instance
        yield instance


class TestSubprocessLaneExportsDispatchPathsAndRole:
    def test_declared_paths_and_role_are_exported(self, mock_adapter):
        deliver_via_subprocess(
            "T1",
            "do work",
            "sonnet",
            "d-oi1886-subprocess-1",
            role=ROLE,
            dispatch_paths=["scripts/lib/foo.py:read_write"],
        )
        _, kwargs = mock_adapter.deliver.call_args
        extra_env = kwargs["extra_env"]
        assert extra_env["VNX_WORKER_ROLE"] == ROLE
        assert json.loads(extra_env["VNX_DISPATCH_PATHS"]) == ["scripts/lib/foo.py:read_write"]

    def test_no_declared_paths_leaves_var_unset(self, mock_adapter):
        deliver_via_subprocess(
            "T1",
            "do work",
            "sonnet",
            "d-oi1886-subprocess-2",
            role=ROLE,
            dispatch_paths=None,
        )
        _, kwargs = mock_adapter.deliver.call_args
        assert "VNX_DISPATCH_PATHS" not in kwargs["extra_env"]


# ---------------------------------------------------------------------------
# (a) provider lanes: provider_dispatch._worker_role_env — direct
# ---------------------------------------------------------------------------


class TestWorkerRoleEnvDispatchPaths:
    def test_declared_paths_are_exported(self):
        env = _worker_role_env(ROLE, dispatch_paths=["scripts/lib/foo.py:read_write"])
        assert env["VNX_WORKER_ROLE"] == ROLE
        assert json.loads(env["VNX_DISPATCH_PATHS"]) == ["scripts/lib/foo.py:read_write"]

    def test_no_declared_paths_leaves_var_unset(self):
        env = _worker_role_env(ROLE, dispatch_paths=None)
        assert "VNX_DISPATCH_PATHS" not in (env or {})

    def test_default_param_is_backward_compatible(self):
        """Existing callers that never learn the new kwarg keep working unchanged."""
        env = _worker_role_env(ROLE)
        assert "VNX_DISPATCH_PATHS" not in (env or {})
        assert env["VNX_WORKER_ROLE"] == ROLE


# ---------------------------------------------------------------------------
# (a) provider lanes via the envelope adapter: envelope_adapters_provider
# ---------------------------------------------------------------------------


class TestProviderEnvelopeAdapterExportsDispatchPaths:
    def test_codex_branch_exports_declared_paths(self):
        from envelope_adapters_provider import ProviderAdapter  # noqa: PLC0415

        captured = {}

        def fake_spawn_codex(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                error=None, timed_out=False, returncode=0,
                completion_text="", token_usage=None, event_writer_failures=0,
            )

        plan = SimpleNamespace(
            provider=Provider.CODEX,
            model="sonnet",
            dispatch_id="d-oi1886-codex",
            target_id="T1",
            role=ROLE,
            dispatch_paths=(
                DispatchPath(PurePosixPath("scripts/lib/foo.py"), PathAccess.READ_WRITE),
            ),
        )

        with patch("provider_spawns.codex_spawn.spawn_codex", side_effect=fake_spawn_codex):
            ProviderAdapter().run(plan, "do work", cwd=None)

        extra_env = captured["extra_env"]
        assert extra_env["VNX_WORKER_ROLE"] == ROLE
        assert json.loads(extra_env["VNX_DISPATCH_PATHS"]) == ["scripts/lib/foo.py:read_write"]


# ---------------------------------------------------------------------------
# (b) end-to-end: the ACTUAL headless-lane env fed into the real hook subprocess
# ---------------------------------------------------------------------------


def _make_hook_payload(tool_name: str, tool_input: dict, cwd: str) -> str:
    return json.dumps(
        {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "session_id": "test-session-oi1886",
            "cwd": cwd,
            "transcript_path": "/tmp/test.jsonl",
        }
    )


def _run_hook_with_env(payload: str, extra_env: dict) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env.pop("VNX_ENFORCE_WORKER_PERMISSIONS", None)
    env.pop("VNX_WORKER_ENFORCEMENT_SKIP", None)
    env.pop("VNX_WORKER_ROLE", None)
    env.pop("VNX_DISPATCH_PATHS", None)
    for _steer in ("PROJECT_ROOT", "VNX_PROJECT_ROOT", "VNX_HOME"):
        env.pop(_steer, None)
    env["VNX_ENFORCE_WORKER_PERMISSIONS"] = "1"
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK_SH)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


class TestHeadlessLaneEnvFeedsRealHook:
    """The env dict ClaudeSubprocessAdapter.run() actually builds, fed verbatim
    into the real hook subprocess — no hand-built env, no reimplementation."""

    def _build_real_headless_extra_env(self, tmp_path: Path, dispatch_paths) -> dict:
        captured = {}

        def fake_spawn_claude(**kwargs):
            captured.update(kwargs)
            return _fake_claude_spawn_result()

        spec = _make_envelope_spec(tmp_path, dispatch_paths=dispatch_paths)
        with patch("provider_spawns.claude_spawn.spawn_claude", side_effect=fake_spawn_claude):
            ClaudeSubprocessAdapter().run(spec, cwd=tmp_path)
        return captured["extra_env"]

    def test_write_within_declared_paths_allowed(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        extra_env = self._build_real_headless_extra_env(
            tmp_path, dispatch_paths=("tests/test_x.py:read_write",)
        )
        target = wt / "tests" / "test_x.py"
        res = _run_hook_with_env(
            _make_hook_payload("Write", {"file_path": str(target)}, cwd=str(wt)),
            extra_env,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", res.stdout

    def test_write_outside_declared_paths_blocked(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        extra_env = self._build_real_headless_extra_env(
            tmp_path, dispatch_paths=("tests/test_x.py:read_write",)
        )
        target = wt / "scripts" / "other.py"
        res = _run_hook_with_env(
            _make_hook_payload("Write", {"file_path": str(target)}, cwd=str(wt)),
            extra_env,
        )
        assert res.returncode == 0, res.stderr
        lines = [ln for ln in res.stdout.strip().splitlines() if ln.strip()]
        assert len(lines) == 1
        decision = json.loads(lines[0])["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "narrowed by this dispatch" in decision["permissionDecisionReason"]

    def test_no_declared_paths_falls_back_to_role_scope_only(self, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        extra_env = self._build_real_headless_extra_env(tmp_path, dispatch_paths=())
        assert "VNX_DISPATCH_PATHS" not in extra_env
        # Role scope alone (backend-developer: scripts/**) still allows this write.
        target = wt / "scripts" / "other.py"
        res = _run_hook_with_env(
            _make_hook_payload("Write", {"file_path": str(target)}, cwd=str(wt)),
            extra_env,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", res.stdout
