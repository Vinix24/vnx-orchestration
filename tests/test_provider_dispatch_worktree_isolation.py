"""test_provider_dispatch_worktree_isolation.py — VNX_ISOLATED_WORKTREE for providers.

Verifies (PR-PROVIDER-ISO):
1. With VNX_ISOLATED_WORKTREE=1, each provider dispatch (codex/kimi/gemini/litellm):
   - calls create_dispatch_worktree with the dispatch_id
   - passes the worktree path as cwd to the spawn function
   - calls remove_dispatch_worktree after (success and failure paths)
2. Without VNX_ISOLATED_WORKTREE, spawn functions receive cwd=None (no isolation).
3. create_dispatch_worktree failure: dispatch continues in shared path (cwd=None).
"""

from __future__ import annotations

import sys
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
        mock_create.assert_called_once_with("iso-codex-001")
        mock_remove.assert_called_once_with("iso-codex-001")
        assert captured["cwd"] == _FAKE_WT_PATH

    def test_no_isolation_when_env_unset(self):
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
             patch("provider_dispatch._create_provider_worktree") as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(_base_argv("codex", "no-iso-codex"))
            exit_code = provider_dispatch._dispatch_codex(args)

        assert exit_code == 0
        mock_create.assert_not_called()
        mock_remove.assert_not_called()
        assert captured.get("cwd") is None

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

        mock_remove.assert_called_once_with("fail-codex")


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
        mock_create.assert_called_once_with("iso-gemini-001")
        mock_remove.assert_called_once_with("iso-gemini-001")
        assert captured["cwd"] == _FAKE_WT_PATH

    def test_no_isolation_when_env_unset(self):
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
             patch("provider_dispatch._create_provider_worktree") as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(_base_argv("gemini", "no-iso-gemini"))
            exit_code = provider_dispatch._dispatch_gemini(args)

        assert exit_code == 0
        mock_create.assert_not_called()
        mock_remove.assert_not_called()
        assert captured.get("cwd") is None


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
        mock_create.assert_called_once_with("iso-kimi-001")
        mock_remove.assert_called_once_with("iso-kimi-001")
        assert captured["cwd"] == _FAKE_WT_PATH

    def test_no_isolation_when_env_unset(self):
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
             patch("provider_dispatch._create_provider_worktree") as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(_base_argv("kimi", "no-iso-kimi"))
            exit_code = provider_dispatch._dispatch_kimi(args)

        assert exit_code == 0
        mock_create.assert_not_called()
        mock_remove.assert_not_called()
        assert captured.get("cwd") is None


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
        mock_create.assert_called_once_with("iso-litellm-001")
        mock_remove.assert_called_once_with("iso-litellm-001")
        assert captured["cwd"] == _FAKE_WT_PATH

    def test_no_isolation_when_env_unset(self):
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
             patch("provider_dispatch._create_provider_worktree") as mock_create, \
             patch("provider_dispatch._remove_provider_worktree") as mock_remove:

            args = provider_dispatch._build_parser().parse_args(
                _base_argv("litellm:deepseek", "no-iso-litellm")
            )
            exit_code = provider_dispatch._dispatch_litellm(args)

        assert exit_code == 0
        mock_create.assert_not_called()
        mock_remove.assert_not_called()
        assert captured.get("cwd") is None


# ---------------------------------------------------------------------------
# Helper functions: _create_provider_worktree / _remove_provider_worktree
# ---------------------------------------------------------------------------

class TestProviderWorktreeHelpers:
    def test_create_returns_path_on_success(self, tmp_path):
        with patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=tmp_path) as mock_create:
            result = provider_dispatch._create_provider_worktree("helper-create-test")
        assert result == tmp_path
        mock_create.assert_called_once_with("helper-create-test")

    def test_create_returns_none_on_runtime_error(self):
        with patch("dispatch_worktree_isolation.create_dispatch_worktree", side_effect=RuntimeError("disk full")):
            result = provider_dispatch._create_provider_worktree("helper-fail-test")
        assert result is None

    def test_remove_is_best_effort(self):
        """_remove_provider_worktree must not raise even if underlying call fails."""
        with patch("dispatch_worktree_isolation.remove_dispatch_worktree", side_effect=Exception("gone")):
            provider_dispatch._remove_provider_worktree("remove-fail-test")

    def test_remove_calls_underlying(self):
        with patch("dispatch_worktree_isolation.remove_dispatch_worktree") as mock_remove:
            provider_dispatch._remove_provider_worktree("remove-ok-test")
        mock_remove.assert_called_once_with("remove-ok-test")
