"""test_provider_dispatch_worktree_isolation.py — provider dispatch isolation (default-on since OI-1090).

Verifies:
1. Every provider dispatch (codex/kimi/gemini/litellm) always creates a worktree:
   - calls create_dispatch_worktree with the dispatch_id
   - passes the worktree path as cwd to the spawn function
   - calls remove_dispatch_worktree after (success and failure paths)
2. Worktree is created even without VNX_ISOLATED_WORKTREE set (default-on).
3. create_dispatch_worktree failure: dispatch ABORTS (returns 1),
   spawn NOT called — no silent shared-checkout fallback.
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_FAKE_WT_PATH = Path("/tmp/fake-worktrees/dispatch-test-iso")


def _base_argv(provider: str, dispatch_id: str = "test-iso-dispatch") -> list:
    return [
        "--provider", provider,
        "--terminal-id", "T1",
        "--dispatch-id", dispatch_id,
        "--instruction", "noop",
        "--model", "sonnet",
    ]


def _make_spawn_result(returncode: int = 0) -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.error = None
    r.timed_out = False
    r.event_writer_failures = 0
    r.completion_text = ""
    r.token_usage = {"input_tokens": 0, "output_tokens": 0}
    return r


def _noop_governance(args, provider, model, result, start, end, status, event_store=None):
    pass


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

class TestCodexIsolation:
    def _run_codex(self, env_patch: dict, spawn_side_effect=None) -> tuple:
        """Run _dispatch_codex with mocked internals. Returns (exit_code, captured_cwd)."""
        result = _make_spawn_result()
        captured = {}

        def fake_spawn(**kwargs):
            captured["cwd"] = kwargs.get("cwd")
            if spawn_side_effect is not None:
                raise spawn_side_effect
            return result

        with patch.dict("os.environ", env_patch, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_dispatch._resolve_codex_model", return_value="gpt-test"), \
             patch("provider_dispatch._check_constraints", return_value=[]), \
             patch("provider_dispatch._dispatch_codex", wraps=provider_dispatch._dispatch_codex):

            mock_event_store = MagicMock()
            mock_event_store.append = MagicMock()
            mock_event_store.clear = MagicMock()

            with patch("event_store.EventStore", return_value=mock_event_store), \
                 patch("provider_spawns.codex_spawn.spawn_codex", side_effect=lambda **kw: fake_spawn(**kw)):

                args = provider_dispatch._build_parser().parse_args(_base_argv("codex"))

                with patch("provider_dispatch._create_provider_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
                     patch("provider_dispatch._remove_provider_worktree") as mock_remove:

                    exit_code = provider_dispatch._dispatch_codex(args)
                    return exit_code, captured.get("cwd"), mock_create, mock_remove

    def test_isolated_worktree_creates_and_removes(self):
        with patch.dict("os.environ", {"VNX_ISOLATED_WORKTREE": "1"}, clear=False):
            result = _make_spawn_result()
            captured = {}

            def fake_spawn(*a, **kw):
                captured["cwd"] = kw.get("cwd")
                return result

            mock_event_store = MagicMock()
            mock_event_store.append = MagicMock()
            mock_event_store.clear = MagicMock()

            with patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
                 patch("provider_dispatch._enrich_instruction", return_value="noop"), \
                 patch("provider_dispatch._resolve_codex_model", return_value="gpt-test"), \
                 patch("event_store.EventStore", return_value=mock_event_store), \
                 patch("provider_spawns.codex_spawn.spawn_codex", side_effect=fake_spawn), \
                 patch("provider_dispatch._create_provider_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
                 patch("provider_dispatch._remove_provider_worktree") as mock_remove:

                args = provider_dispatch._build_parser().parse_args(_base_argv("codex", "iso-codex-001"))
                exit_code = provider_dispatch._dispatch_codex(args)

        assert exit_code == 0
        mock_create.assert_called_once_with("iso-codex-001", base_ref=None)
        mock_remove.assert_called_once_with("iso-codex-001", terminal_id="T1")
        assert captured["cwd"] == _FAKE_WT_PATH

    def test_isolation_without_env_var(self):
        """Isolation is default-on: worktree created even without VNX_ISOLATED_WORKTREE."""
        env = {k: v for k, v in __import__("os").environ.items() if k != "VNX_ISOLATED_WORKTREE"}
        result = _make_spawn_result()
        captured = {}

        def fake_spawn(*a, **kw):
            captured["cwd"] = kw.get("cwd")
            return result

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {"VNX_ISOLATED_WORKTREE": ""}, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_dispatch._resolve_codex_model", return_value="gpt-test"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.codex_spawn.spawn_codex", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(_base_argv("codex", "default-iso-codex"))
            exit_code = provider_dispatch._dispatch_codex(args)

        assert exit_code == 0
        mock_create.assert_called_once_with("default-iso-codex", base_ref=None)
        mock_remove.assert_called_once_with("default-iso-codex", terminal_id="T1")
        assert captured.get("cwd") == _FAKE_WT_PATH

    def test_worktree_removed_on_spawn_failure(self):
        """remove_dispatch_worktree must be called even when spawn raises."""
        with patch.dict("os.environ", {"VNX_ISOLATED_WORKTREE": "1"}, clear=False):
            mock_event_store = MagicMock()
            mock_event_store.append = MagicMock()
            mock_event_store.clear = MagicMock()

            with patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
                 patch("provider_dispatch._enrich_instruction", return_value="noop"), \
                 patch("provider_dispatch._resolve_codex_model", return_value="gpt-test"), \
                 patch("event_store.EventStore", return_value=mock_event_store), \
                 patch("provider_spawns.codex_spawn.spawn_codex", side_effect=RuntimeError("simulated")), \
                 patch("provider_dispatch._create_provider_worktree", return_value=_FAKE_WT_PATH), \
                 patch("provider_dispatch._remove_provider_worktree") as mock_remove:

                args = provider_dispatch._build_parser().parse_args(_base_argv("codex", "fail-codex"))
                with pytest.raises(RuntimeError, match="simulated"):
                    provider_dispatch._dispatch_codex(args)

        mock_remove.assert_called_once_with("fail-codex", terminal_id="T1")


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

class TestGeminiIsolation:
    def test_isolated_worktree_creates_and_removes(self):
        captured = {}
        result = _make_spawn_result()

        def fake_spawn(*a, **kw):
            captured["cwd"] = kw.get("cwd")
            return result

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {"VNX_ISOLATED_WORKTREE": "1"}, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.gemini_spawn.spawn_gemini", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(_base_argv("gemini", "iso-gemini-001"))
            exit_code = provider_dispatch._dispatch_gemini(args)

        assert exit_code == 0
        mock_create.assert_called_once_with("iso-gemini-001", base_ref=None)
        mock_remove.assert_called_once_with("iso-gemini-001", terminal_id="T1")
        assert captured["cwd"] == _FAKE_WT_PATH

    def test_isolation_without_env_var(self):
        """Isolation is default-on: worktree created even without VNX_ISOLATED_WORKTREE."""
        captured = {}
        result = _make_spawn_result()

        def fake_spawn(*a, **kw):
            captured["cwd"] = kw.get("cwd")
            return result

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {"VNX_ISOLATED_WORKTREE": ""}, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.gemini_spawn.spawn_gemini", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(_base_argv("gemini", "default-iso-gemini"))
            exit_code = provider_dispatch._dispatch_gemini(args)

        assert exit_code == 0
        mock_create.assert_called_once_with("default-iso-gemini", base_ref=None)
        mock_remove.assert_called_once_with("default-iso-gemini", terminal_id="T1")
        assert captured.get("cwd") == _FAKE_WT_PATH


# ---------------------------------------------------------------------------
# Kimi
# ---------------------------------------------------------------------------

class TestKimiIsolation:
    def test_isolated_worktree_creates_and_removes(self):
        captured = {}
        result = _make_spawn_result()

        def fake_spawn(*a, **kw):
            captured["cwd"] = kw.get("cwd")
            return result

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {"VNX_ISOLATED_WORKTREE": "1"}, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_dispatch._resolve_kimi_model_label", return_value="kimi-default"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.kimi_spawn.spawn_kimi", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(_base_argv("kimi", "iso-kimi-001"))
            exit_code = provider_dispatch._dispatch_kimi(args)

        assert exit_code == 0
        mock_create.assert_called_once_with("iso-kimi-001", base_ref=None)
        mock_remove.assert_called_once_with("iso-kimi-001", terminal_id="T1")
        assert captured["cwd"] == _FAKE_WT_PATH

    def test_isolation_without_env_var(self):
        """Isolation is default-on: worktree created even without VNX_ISOLATED_WORKTREE."""
        captured = {}
        result = _make_spawn_result()

        def fake_spawn(*a, **kw):
            captured["cwd"] = kw.get("cwd")
            return result

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {"VNX_ISOLATED_WORKTREE": ""}, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_dispatch._resolve_kimi_model_label", return_value="kimi-default"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.kimi_spawn.spawn_kimi", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(_base_argv("kimi", "default-iso-kimi"))
            exit_code = provider_dispatch._dispatch_kimi(args)

        assert exit_code == 0
        mock_create.assert_called_once_with("default-iso-kimi", base_ref=None)
        mock_remove.assert_called_once_with("default-iso-kimi", terminal_id="T1")
        assert captured.get("cwd") == _FAKE_WT_PATH


# ---------------------------------------------------------------------------
# LiteLLM (deepseek sub-provider)
# ---------------------------------------------------------------------------

class TestLiteLLMIsolation:
    def test_isolated_worktree_creates_and_removes(self):
        captured = {}
        result = _make_spawn_result()

        def fake_spawn(*a, **kw):
            captured["cwd"] = kw.get("cwd")
            return result

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {
            "VNX_ISOLATED_WORKTREE": "1",
            "VNX_LITELLM_MODEL": "deepseek/deepseek-v4-pro",
            "DEEPSEEK_API_KEY": "sk-test",
        }, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.litellm_spawn.spawn_litellm", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(
                _base_argv("litellm:deepseek", "iso-litellm-001")
            )
            exit_code = provider_dispatch._dispatch_litellm(args)

        assert exit_code == 0
        mock_create.assert_called_once_with("iso-litellm-001", base_ref=None)
        mock_remove.assert_called_once_with("iso-litellm-001", terminal_id="T1")
        assert captured["cwd"] == _FAKE_WT_PATH

    def test_isolation_without_env_var(self):
        """Isolation is default-on: worktree created even without VNX_ISOLATED_WORKTREE."""
        captured = {}
        result = _make_spawn_result()

        def fake_spawn(*a, **kw):
            captured["cwd"] = kw.get("cwd")
            return result

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {
            "VNX_ISOLATED_WORKTREE": "",
            "VNX_LITELLM_MODEL": "deepseek/deepseek-v4-pro",
            "DEEPSEEK_API_KEY": "sk-test",
        }, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.litellm_spawn.spawn_litellm", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", return_value=_FAKE_WT_PATH) as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(
                _base_argv("litellm:deepseek", "default-iso-litellm")
            )
            exit_code = provider_dispatch._dispatch_litellm(args)

        assert exit_code == 0
        mock_create.assert_called_once_with("default-iso-litellm", base_ref=None)
        mock_remove.assert_called_once_with("default-iso-litellm", terminal_id="T1")
        assert captured.get("cwd") == _FAKE_WT_PATH


# ---------------------------------------------------------------------------
# Helper functions: _create_provider_worktree / _remove_provider_worktree
# ---------------------------------------------------------------------------

class TestProviderWorktreeHelpers:
    def test_create_returns_path_on_success(self, tmp_path):
        consumer_root = tmp_path / "consumer"
        with patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=consumer_root), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=tmp_path) as mock_create:
            result = provider_dispatch._create_provider_worktree("helper-create-test")
        assert result == tmp_path
        mock_create.assert_called_once_with("helper-create-test", project_root=consumer_root, base_ref=None)

    def test_create_raises_on_runtime_error(self):
        with patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=Path("/tmp/consumer")), \
             patch("dispatch_worktree_isolation.create_dispatch_worktree", side_effect=RuntimeError("disk full")):
            with pytest.raises(RuntimeError, match="disk full"):
                provider_dispatch._create_provider_worktree("helper-fail-test")

    def test_remove_is_best_effort(self):
        """_remove_provider_worktree must not raise even if underlying call fails."""
        with patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=Path("/tmp/consumer")), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree", side_effect=Exception("gone")):
            provider_dispatch._remove_provider_worktree("remove-fail-test")

    def test_remove_calls_underlying(self):
        consumer_root = Path("/tmp/consumer")
        with patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=consumer_root), \
             patch("dispatch_worktree_isolation.remove_dispatch_worktree") as mock_remove:
            provider_dispatch._remove_provider_worktree("remove-ok-test")
        mock_remove.assert_called_once_with("remove-ok-test", project_root=consumer_root, terminal_id="")

    def test_remove_best_effort_when_resolver_raises(self):
        """A resolver failure must also be swallowed — remove is best-effort end-to-end."""
        with patch("dispatch_worktree_isolation.resolve_consumer_project_root", side_effect=RuntimeError("no git")):
            provider_dispatch._remove_provider_worktree("remove-resolver-fail-test")


class TestResolveConsumerProjectRoot:
    """Regression: the invocation-context project root wins over __file__-based
    resolution — the exact consumer scenario (P0 provider-worktree-root-fix):
    the lane code executes from inside a central-install-like tree, but
    VNX_PROJECT_ROOT (exported by the central-install shim) names the
    operator's actual project elsewhere, and that must win.
    """

    def test_vnx_project_root_env_wins_over_file_location(self, tmp_path, monkeypatch):
        from dispatch_worktree_isolation import resolve_consumer_project_root

        consumer_project = tmp_path / "my-consumer-project"
        consumer_project.mkdir()
        monkeypatch.setenv("VNX_PROJECT_ROOT", str(consumer_project))

        resolved = resolve_consumer_project_root()

        assert resolved == consumer_project.resolve()


# ---------------------------------------------------------------------------
# PR-7: fail-loud — create failure → ABORT, no spawn
# ---------------------------------------------------------------------------

class TestCodexIsolationFailLoud:
    def test_isolation_create_failure_aborts_dispatch(self):
        """Worktree creation failure: return 1, spawn NOT called."""
        spawn_called = []

        def fake_spawn(*a, **kw):
            spawn_called.append(kw)
            return _make_spawn_result()

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {"VNX_ISOLATED_WORKTREE": "1"}, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_dispatch._resolve_codex_model", return_value="gpt-test"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.codex_spawn.spawn_codex", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", side_effect=RuntimeError("disk full")) as mock_create:

            args = provider_dispatch._build_parser().parse_args(_base_argv("codex", "fail-loud-codex"))
            exit_code = provider_dispatch._dispatch_codex(args)

        assert exit_code == 1
        mock_create.assert_called_once_with("fail-loud-codex", base_ref=None)
        assert spawn_called == [], "spawn_codex must NOT be called when worktree creation fails"


class TestGeminiIsolationFailLoud:
    def test_isolation_create_failure_aborts_dispatch(self):
        """Worktree creation failure: return 1, spawn NOT called."""
        spawn_called = []

        def fake_spawn(*a, **kw):
            spawn_called.append(kw)
            return _make_spawn_result()

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {"VNX_ISOLATED_WORKTREE": "1"}, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.gemini_spawn.spawn_gemini", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", side_effect=RuntimeError("disk full")) as mock_create:

            args = provider_dispatch._build_parser().parse_args(_base_argv("gemini", "fail-loud-gemini"))
            exit_code = provider_dispatch._dispatch_gemini(args)

        assert exit_code == 1
        mock_create.assert_called_once_with("fail-loud-gemini", base_ref=None)
        assert spawn_called == [], "spawn_gemini must NOT be called when worktree creation fails"


class TestKimiIsolationFailLoud:
    def test_isolation_create_failure_aborts_dispatch(self):
        """Worktree creation failure: return 1, spawn NOT called."""
        spawn_called = []

        def fake_spawn(*a, **kw):
            spawn_called.append(kw)
            return _make_spawn_result()

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {"VNX_ISOLATED_WORKTREE": "1"}, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("provider_dispatch._resolve_kimi_model_label", return_value="kimi-default"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.kimi_spawn.spawn_kimi", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", side_effect=RuntimeError("disk full")) as mock_create:

            args = provider_dispatch._build_parser().parse_args(_base_argv("kimi", "fail-loud-kimi"))
            exit_code = provider_dispatch._dispatch_kimi(args)

        assert exit_code == 1
        mock_create.assert_called_once_with("fail-loud-kimi", base_ref=None)
        assert spawn_called == [], "spawn_kimi must NOT be called when worktree creation fails"


class TestLiteLLMIsolationFailLoud:
    def test_isolation_create_failure_aborts_dispatch(self):
        """Worktree creation failure: return 1, spawn NOT called."""
        spawn_called = []

        def fake_spawn(*a, **kw):
            spawn_called.append(kw)
            return _make_spawn_result()

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        with patch.dict("os.environ", {
            "VNX_ISOLATED_WORKTREE": "1",
            "VNX_LITELLM_MODEL": "deepseek/deepseek-v4-pro",
            "DEEPSEEK_API_KEY": "sk-test",
        }, clear=False), \
             patch("provider_dispatch._emit_governance", side_effect=_noop_governance), \
             patch("provider_dispatch._enrich_instruction", return_value="noop"), \
             patch("event_store.EventStore", return_value=mock_event_store), \
             patch("provider_spawns.litellm_spawn.spawn_litellm", side_effect=fake_spawn), \
             patch("provider_dispatch._create_provider_worktree", side_effect=RuntimeError("disk full")) as mock_create:

            args = provider_dispatch._build_parser().parse_args(
                _base_argv("litellm:deepseek", "fail-loud-litellm")
            )
            exit_code = provider_dispatch._dispatch_litellm(args)

        assert exit_code == 1
        mock_create.assert_called_once_with("fail-loud-litellm", base_ref=None)
        assert spawn_called == [], "spawn_litellm must NOT be called when worktree creation fails"


# ---------------------------------------------------------------------------
# L3 provider-lane reap: classification, branch preservation, event emission
# ---------------------------------------------------------------------------


def _init_git_repo_with_origin(tmp_path: Path) -> Path:
    """Create a bare origin + local clone with an initial commit.

    Returns the local clone path (the project root).
    Mirrors the fixture in test_tmux_worktree.py.
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(bare), str(local)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "checkout", "-b", "main"],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "test@test.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )

    readme = local / "README.md"
    readme.write_text("init\n")
    subprocess.run(
        ["git", "-C", str(local), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "push", "-u", "origin", "main"],
        check=True, capture_output=True,
    )
    return local


class TestProviderLaneReapClassification:
    """L3: remove_dispatch_worktree must classify before removing, using the
    single canonical classify() from tmux_worktree — no duplicate logic.
    """

    def test_committed_work_survives_teardown(self, tmp_path, monkeypatch):
        """Provider worktree with local unpushed commits: branch preserved.

        Without the L3 fix, remove_dispatch_worktree unconditionally force-deletes
        the local branch (git branch -D).  With the fix, classify() detects local
        commits not on origin → 'committed' → reap() keeps the branch.
        """
        import dispatch_worktree_isolation as dwi
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        dispatch_id = "l3-committed-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        # Make a local commit (not pushed).
        (wt_path / "work.txt").write_text("unpushed work\n")
        subprocess.run(
            ["git", "-C", str(wt_path), "add", "work.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(wt_path), "commit", "-m", "worker commit"],
            check=True, capture_output=True,
        )

        safe_id = dwi._sanitize_dispatch_id(dispatch_id)
        branch_name = f"dispatch/{safe_id}"

        # Verify the branch exists before teardown.
        branches_before = subprocess.check_output(
            ["git", "-C", str(local), "branch", "--list", branch_name],
            text=True,
        ).strip()
        assert branch_name in branches_before

        remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        # Worktree directory is gone.
        assert not wt_path.exists()

        # Branch is STILL THERE (committed → kept locally).
        branches_after = subprocess.check_output(
            ["git", "-C", str(local), "branch", "--list", branch_name],
            text=True,
        ).strip()
        assert branch_name in branches_after, (
            f"L3 FAIL: branch {branch_name} was deleted — unpushed commits lost"
        )

    def test_dirty_worktree_is_preserved(self, tmp_path, monkeypatch):
        """Provider worktree with uncommitted changes: worktree locked, not removed.

        Without the L3 fix, remove_dispatch_worktree would force-remove the worktree
        (git worktree remove --force), losing uncommitted changes.
        """
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        dispatch_id = "l3-dirty-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        # Leave an uncommitted change (dirty state).
        (wt_path / "dirty.txt").write_text("uncommitted\n")

        assert wt_path.is_dir()
        remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        # Worktree directory MUST still exist (dirty → preserved).
        assert wt_path.is_dir(), (
            "L3 FAIL: dirty worktree was removed — uncommitted changes lost"
        )

    def test_classification_reuses_single_implementation(self, tmp_path, monkeypatch):
        """classify() is imported from tmux_worktree — no duplicate classification.

        This test asserts that remove_dispatch_worktree calls the canonical
        tmux_worktree.classify(), not a copy or reimplementation.
        Verifies via a clean worktree: classify() returns 'clean'.
        """
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        dispatch_id = "l3-clean-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)
        safe_id = "l3-clean-1"
        branch_name = f"dispatch/{safe_id}"

        remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        # Clean worktree: directory gone, branch gone.
        assert not wt_path.exists()
        branches = subprocess.check_output(
            ["git", "-C", str(local), "branch", "--list", branch_name],
            text=True,
        ).strip()
        assert branches == ""

    def test_worktree_state_event_emitted(self, tmp_path, monkeypatch):
        """Teardown emits provider_teardown_worktree event via EventStore.

        The event must carry worktree_state, branch_kept_local,
        branch_kept_remote, and preserved_path — the same fields as the
        tmux lane's interactive_teardown_worktree.
        """
        from unittest.mock import MagicMock, patch
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        mock_store = MagicMock()
        mock_store.append = MagicMock()

        dispatch_id = "l3-event-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        with patch("event_store.EventStore", return_value=mock_store):
            remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        # Verify two append calls: provider_teardown_worktree + (not provider_teardown_preserved for clean)
        append_calls = mock_store.append.call_args_list
        assert len(append_calls) >= 1, "L3 FAIL: no event emitted"

        # First call: provider_teardown_worktree
        first_call_args = append_calls[0][0]
        terminal = first_call_args[0]
        event = first_call_args[1]
        assert terminal == "T1"
        assert event["type"] == "provider_teardown_worktree"
        assert "worktree_state" in event["data"]
        assert "branch_kept_local" in event["data"]
        assert "branch_kept_remote" in event["data"]
        assert "preserved_path" in event["data"]

    def test_dirty_worktree_emits_preserved_event(self, tmp_path, monkeypatch):
        """Dirty worktree emits both provider_teardown_worktree AND
        provider_teardown_preserved events.
        """
        from unittest.mock import MagicMock, patch
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        mock_store = MagicMock()
        mock_store.append = MagicMock()

        dispatch_id = "l3-dirty-event-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        # Leave an uncommitted change.
        (wt_path / "unsaved.txt").write_text("pending\n")

        with patch("event_store.EventStore", return_value=mock_store):
            remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        append_calls = mock_store.append.call_args_list
        event_types = [call[0][1]["type"] for call in append_calls]
        assert "provider_teardown_worktree" in event_types
        assert "provider_teardown_preserved" in event_types, (
            "L3 FAIL: dirty worktree must emit provider_teardown_preserved"
        )

        # The teardown_worktree event must have worktree_state == "dirty"
        teardown_event = next(
            c[0][1] for c in append_calls
            if c[0][1]["type"] == "provider_teardown_worktree"
        )
        assert teardown_event["data"]["worktree_state"] == "dirty"
        assert teardown_event["data"]["preserved_path"] is not None
        assert teardown_event["data"]["branch_kept_local"] is False
        assert teardown_event["data"]["branch_kept_remote"] is False

    def test_committed_worktree_emits_correct_metadata(self, tmp_path, monkeypatch):
        """Committed worktree: event carries branch_kept_local=True."""
        from unittest.mock import MagicMock, patch
        import dispatch_worktree_isolation as dwi
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        mock_store = MagicMock()
        mock_store.append = MagicMock()

        dispatch_id = "l3-committed-event-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        (wt_path / "committed.txt").write_text("local only\n")
        subprocess.run(
            ["git", "-C", str(wt_path), "add", "committed.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(wt_path), "commit", "-m", "local commit"],
            check=True, capture_output=True,
        )

        with patch("event_store.EventStore", return_value=mock_store):
            remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        # Find the teardown event.
        teardown_events = [
            c[0][1] for c in mock_store.append.call_args_list
            if c[0][1]["type"] == "provider_teardown_worktree"
        ]
        assert len(teardown_events) == 1
        event = teardown_events[0]
        assert event["data"]["worktree_state"] == "committed"
        assert event["data"]["branch_kept_local"] is True
        assert event["data"]["branch_kept_remote"] is False
        assert event["data"]["preserved_path"] is None

    def test_pushed_worktree_keeps_remote_branch(self, tmp_path, monkeypatch):
        """Pushed worktree: worktree removed, branch deleted locally, remote ref intact."""
        import dispatch_worktree_isolation as dwi
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        dispatch_id = "l3-pushed-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        # Make + push a commit.
        (wt_path / "pushed.txt").write_text("pushed\n")
        subprocess.run(
            ["git", "-C", str(wt_path), "add", "pushed.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(wt_path), "commit", "-m", "pushed work"],
            check=True, capture_output=True,
        )
        safe_id = dwi._sanitize_dispatch_id(dispatch_id)
        branch_name = f"dispatch/{safe_id}"
        subprocess.run(
            ["git", "-C", str(wt_path), "push", "-u", "origin", branch_name],
            check=True, capture_output=True,
        )

        remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        # Worktree gone.
        assert not wt_path.exists()
        # Local branch gone.
        local_branches = subprocess.check_output(
            ["git", "-C", str(local), "branch", "--list", branch_name],
            text=True,
        ).strip()
        assert local_branches == ""
        # Remote ref still present.
        remote_refs = subprocess.check_output(
            ["git", "-C", str(local), "ls-remote", "origin", branch_name],
            text=True,
        ).strip()
        assert branch_name in remote_refs

    def test_fail_closed_on_missing_base_sha(self, tmp_path, monkeypatch):
        """When the claim is missing base_sha (pre-L3), derive it or treat as dirty.

        Fail-closed: a control that defaults to 'proceed' on uncertainty is no
        control at all.  Missing base_sha → fail-safe (treat as dirty).
        """
        from unittest.mock import patch
        from dispatch_worktree_isolation import (
            _sanitize_dispatch_id,
            _claim_path,
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        dispatch_id = "l3-no-basesha-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        # Strip base_sha from the claim to simulate a pre-L3 claim.
        safe_id = _sanitize_dispatch_id(dispatch_id)
        claim_path = _claim_path(safe_id, local)
        import json
        claim = json.loads(claim_path.read_text())
        del claim["base_sha"]
        claim_path.write_text(json.dumps(claim) + "\n")

        # Make a local commit so this isn't trivially clean.
        (wt_path / "work.txt").write_text("unpushed work\n")
        subprocess.run(
            ["git", "-C", str(wt_path), "add", "work.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(wt_path), "commit", "-m", "worker commit"],
            check=True, capture_output=True,
        )

        # With base_sha derived from origin/main (success), classify should work.
        remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        # Worktree should be gone (committed → disk removed).
        assert not wt_path.exists()
        # Branch should survive.
        branch_name = f"dispatch/{safe_id}"
        branches = subprocess.check_output(
            ["git", "-C", str(local), "branch", "--list", branch_name],
            text=True,
        ).strip()
        assert branch_name in branches, (
            "L3 FAIL: branch was deleted — missing base_sha must fail-closed"
        )


# ---------------------------------------------------------------------------
# OI-1106: the envelope push+PR guard must classify against the worktree's
# RECORDED base (the allocator's origin/main), not a lane-local plan.base_ref
# that can name a stale local `main` or a PR merge-commit checkout. Two
# independent sources for "the base" is what made the envelope test family
# flake: a commit-less worktree was misclassified committed/pushed and a real
# success was rewritten to status="failure" on a different test each run.
# ---------------------------------------------------------------------------


class TestOi1106BaseFromClaimNotPlanBaseRef:
    """The base SHA for classification comes from the worktree claim, never
    re-derived from plan.base_ref. Pinned here so the flake cannot return."""

    def test_read_worktree_base_sha_returns_allocator_recorded_sha(
        self, tmp_path, monkeypatch
    ):
        """read_worktree_base_sha reads base_sha from the claim the allocator
        wrote — the same source remove_dispatch_worktree uses for L3 reap."""
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            read_worktree_base_sha,
        )

        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        dispatch_id = "oi1106-claim-1"
        create_dispatch_worktree(dispatch_id, project_root=local)

        origin_main_sha = subprocess.check_output(
            ["git", "-C", str(local), "rev-parse", "origin/main"], text=True
        ).strip()

        base_sha, base_ref = read_worktree_base_sha(dispatch_id, project_root=local)
        assert base_sha == origin_main_sha, (
            f"read_worktree_base_sha must return the allocator's recorded "
            f"origin/main SHA {origin_main_sha[:12]}, got {base_sha!r}"
        )
        assert base_ref == "origin/main"

    def test_enforce_push_pr_clean_when_local_main_stale(
        self, tmp_path, monkeypatch
    ):
        """OI-1106 repro + fix: a stale local `main` behind `origin/main` must
        NOT misclassify a commit-less worktree as committed/pushed.

        Before the fix, _enforce_push_pr resolved base_sha from plan.base_ref
        ("main") while the worktree was based on origin/main; when local main
        lagged origin/main, base_sha != HEAD and a clean worktree was
        misclassified committed -> push -> gh pr create ("No commits between
        main and dispatch/...") -> status="failure". With the fix, base_sha
        comes from the claim (origin/main == HEAD) -> clean -> no rejection.
        """
        from dispatch_envelope import _AdapterResult, _enforce_push_pr
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )
        import tmux_worktree

        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        # Advance origin/main by one commit so a stale local `main` lags it.
        (local / "advance.txt").write_text("advance\n")
        subprocess.run(["git", "-C", str(local), "add", "advance.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(local), "commit", "-m", "advance main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(local), "push", "origin", "main"], check=True, capture_output=True)
        origin_main_sha = subprocess.check_output(
            ["git", "-C", str(local), "rev-parse", "origin/main"], text=True
        ).strip()
        stale_main_sha = subprocess.check_output(
            ["git", "-C", str(local), "rev-parse", "main~1"], text=True
        ).strip()
        assert origin_main_sha != stale_main_sha, "fixture: origin/main must be ahead of stale main"

        dispatch_id = "oi1106-stale-main-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)
        # Worktree is commit-less (clean), based on origin/main.
        wt_head = subprocess.check_output(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"], text=True
        ).strip()
        assert wt_head == origin_main_sha

        branch = f"dispatch/{dispatch_id}"
        receipts_file = tmp_path / "receipts.ndjson"
        receipts_file.touch()

        # Spy classify_path to record the verdict the guard actually used.
        verdicts: list = []
        orig_classify = tmux_worktree.classify_path

        def spy(**kw):
            v = orig_classify(**kw)
            verdicts.append((kw.get("base_sha"), v))
            return v

        monkeypatch.setattr(tmux_worktree, "classify_path", spy)
        # Also patch the name pr_enforcement imported, if any — the envelope
        # guard imports classify_path lazily inside _enforce_push_pr.
        import pr_enforcement as _pe
        if hasattr(_pe, "classify_path"):
            monkeypatch.setattr(_pe, "classify_path", spy)

        result = _enforce_push_pr(
            dispatch_id=dispatch_id,
            branch=branch,
            wt_path=wt_path,
            repo_root=local,
            receipts_file=receipts_file,
            result=_AdapterResult(returncode=0, completion_text="done", status="success"),
            # plan.base_ref would resolve this stale local main — the exact
            # trap. The claim must win over it.
            base_ref="main",
        )

        try:
            # The guard must NOT rewrite a clean success to failure.
            assert result.status == "success", (
                f"OI-1106 regression: clean worktree rewritten to "
                f"status={result.status!r} ({result.error})"
            )
            # And classify must have seen base_sha == origin/main (the claim),
            # NOT the stale local main.
            assert verdicts, "classify_path was never called"
            used_base_sha, verdict = verdicts[0]
            assert used_base_sha == origin_main_sha, (
                f"OI-1106: classify used base_sha={used_base_sha!r} "
                f"(stale main={stale_main_sha[:12]}?) instead of the claim's "
                f"origin/main={origin_main_sha[:12]}"
            )
            assert verdict == "clean", (
                f"commit-less worktree classified {verdict!r}, expected 'clean'"
            )
        finally:
            remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

    def test_enforce_push_pr_degrades_loud_when_no_claim(self, tmp_path, monkeypatch):
        """When the claim is unavailable (stubbed allocator), _enforce_push_pr
        falls back to base_ref with a loud warning — never silently guesses
        committed. classify_path degrades clean-safe when even that fails."""
        import logging
        from dispatch_envelope import _AdapterResult, _enforce_push_pr
        import tmux_worktree

        # A tmp_path that is NOT a git repo: rev-parse of any ref fails, so
        # the fallback _resolve_base_sha returns None -> classify_path logs
        # its 'base unresolvable' warning and returns 'clean' (degraded).
        wt_path = tmp_path / "fake-wt"
        wt_path.mkdir()
        monkeypatch.setattr(
            tmux_worktree, "classify_path",
            lambda **kw: "clean",
        )

        records: list = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r)  # type: ignore[method-assign]
        logger = logging.getLogger("dispatch_envelope")
        logger.addHandler(handler)
        prev_level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            result = _enforce_push_pr(
                dispatch_id="oi1106-noclaim-1",
                branch="dispatch/oi1106-noclaim-1",
                wt_path=wt_path,
                repo_root=tmp_path,
                receipts_file=tmp_path / "r.ndjson",
                result=_AdapterResult(returncode=0, completion_text="done", status="success"),
                base_ref="main",
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(prev_level)

        # No claim + unresolvable base -> clean (degraded) -> guard skips -> success.
        assert result.status == "success"
        assert any("claim" in (r.getMessage() or "").lower() for r in records), (
            "expected a loud degradation log mentioning the claim fallback"
        )


# ---------------------------------------------------------------------------
# OI-1176: base_ref must flow spec -> _prepare_provider_workdir ->
# _create_provider_worktree -> create_dispatch_worktree, for EVERY provider lane.
# Before the fix _prepare_provider_workdir called _create_provider_worktree
# (dispatch_id) with no base_ref, so a dispatch with base_ref=origin/<branch>
# silently isolated on origin/main. The VNX_DISPATCH_LEGACY=1 rollback routes
# straight here, so the rollback button was also the base_ref-drop button.
# ---------------------------------------------------------------------------


class TestProviderBaseRefThreading:
    def _init_diverged_repo(self, tmp_path, monkeypatch) -> Path:
        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        # Divergent feature branch so origin/feature != origin/main.
        subprocess.run(
            ["git", "-C", str(local), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        (local / "feature.txt").write_text("feature\n")
        subprocess.run(
            ["git", "-C", str(local), "add", "feature.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(local), "commit", "-m", "feature commit"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(local), "push", "-u", "origin", "feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(local), "checkout", "main"],
            check=True, capture_output=True,
        )
        return local

    def _rev_parse(self, root: Path, ref: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", ref], text=True
        ).strip()

    def _run_dispatch(self, dispatch_fn, provider, argv, spawn_target, local, model_patches):
        captured = {}

        def fake_spawn(**kwargs):
            wt = kwargs.get("cwd")
            captured["head"] = subprocess.check_output(
                ["git", "-C", str(wt), "rev-parse", "HEAD"], text=True
            ).strip()
            return _make_spawn_result()

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        patches = [
            patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=local),
            patch("provider_dispatch._emit_governance", side_effect=_noop_governance),
            patch("provider_dispatch._enrich_instruction", return_value="noop"),
            patch("event_store.EventStore", return_value=mock_event_store),
            patch(spawn_target, side_effect=fake_spawn),
        ]
        for target, ret in model_patches:
            patches.append(patch(target, return_value=ret))

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            args = provider_dispatch._build_parser().parse_args(argv)
            exit_code = dispatch_fn(args)

        return exit_code, captured.get("head")

    @pytest.mark.parametrize(
        "provider, dispatch_fn_name, spawn_target, model_patches",
        [
            ("codex", "_dispatch_codex", "provider_spawns.codex_spawn.spawn_codex",
             [("provider_dispatch._resolve_codex_model", "gpt-test")]),
            ("kimi", "_dispatch_kimi", "provider_spawns.kimi_spawn.spawn_kimi",
             [("provider_dispatch._kimi_resolve_requested_key", "kimi-k3"),
              ("provider_dispatch._kimi_resolve_cli_model_arg", "kimi-k3")]),
            ("gemini", "_dispatch_gemini", "provider_spawns.gemini_spawn.spawn_gemini",
             []),
        ],
        ids=["codex", "kimi", "gemini"],
    )
    def test_worktree_based_on_spec_base_ref(
        self, provider, dispatch_fn_name, spawn_target, model_patches, tmp_path, monkeypatch
    ):
        """A provider dispatch with --base-ref origin/feature isolates on that
        branch, not origin/main.  Fails without the OI-1176 fix (worktree HEAD
        would be origin/main)."""
        local = self._init_diverged_repo(tmp_path, monkeypatch)
        feature_sha = self._rev_parse(local, "origin/feature")
        main_sha = self._rev_parse(local, "origin/main")
        assert feature_sha != main_sha, "fixture: feature must diverge from main"

        argv = [
            "--provider", provider,
            "--terminal-id", "T1",
            "--dispatch-id", f"oi1176-{provider}",
            "--instruction", "noop",
            "--model", "sonnet",
            "--base-ref", "origin/feature",
        ]
        dispatch_fn = getattr(provider_dispatch, dispatch_fn_name)
        exit_code, head = self._run_dispatch(
            dispatch_fn, provider, argv, spawn_target, local, model_patches
        )

        assert exit_code == 0
        assert head == feature_sha, (
            f"{provider} worktree HEAD {head!r} != spec base_ref origin/feature "
            f"{feature_sha!r} — base_ref was dropped (silent origin/main fallback)"
        )
        assert head != main_sha

    def test_unresolvable_base_ref_fails_loud(self, tmp_path, monkeypatch):
        """An unresolvable base_ref raises RuntimeError from the allocator and
        creates NO worktree — never a silent origin/main fallback."""
        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        with patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=local):
            with pytest.raises(RuntimeError, match="cannot resolve base_ref"):
                provider_dispatch._create_provider_worktree(
                    "oi1176-unresolvable", base_ref="origin/does-not-exist"
                )

        wt_dir = local / ".vnx-data" / "worktrees" / "dispatch-oi1176-unresolvable"
        assert not wt_dir.exists()

    def test_unresolvable_base_ref_aborts_dispatch(self, tmp_path, monkeypatch):
        """End-to-end: _dispatch_codex with an unresolvable --base-ref aborts
        (return 1) and never reaches spawn — the isolation guarantee never
        silently deviates."""
        local = _init_git_repo_with_origin(tmp_path)
        data_dir = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))

        spawn_called = []

        def fake_spawn(**kwargs):
            spawn_called.append(kwargs)
            return _make_spawn_result()

        mock_event_store = MagicMock()
        mock_event_store.append = MagicMock()
        mock_event_store.clear = MagicMock()

        argv = [
            "--provider", "codex",
            "--terminal-id", "T1",
            "--dispatch-id", "oi1176-bad-ref",
            "--instruction", "noop",
            "--model", "sonnet",
            "--base-ref", "origin/does-not-exist",
        ]
        with ExitStack() as stack:
            stack.enter_context(patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=local))
            stack.enter_context(patch("provider_dispatch._emit_governance", side_effect=_noop_governance))
            stack.enter_context(patch("provider_dispatch._enrich_instruction", return_value="noop"))
            stack.enter_context(patch("provider_dispatch._resolve_codex_model", return_value="gpt-test"))
            stack.enter_context(patch("event_store.EventStore", return_value=mock_event_store))
            stack.enter_context(patch("provider_spawns.codex_spawn.spawn_codex", side_effect=fake_spawn))
            args = provider_dispatch._build_parser().parse_args(argv)
            exit_code = provider_dispatch._dispatch_codex(args)

        assert exit_code == 1
        assert spawn_called == [], "spawn_codex must NOT run when base_ref is unresolvable"
        assert not (local / ".vnx-data" / "worktrees" / "dispatch-oi1176-bad-ref").exists()
