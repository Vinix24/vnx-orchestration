"""Tests for gemini model-arg respecting in provider_dispatch.py (OI-155)."""
from __future__ import annotations

import os
import sys
import argparse
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import provider_dispatch as pd


def _args(**kw):
    defaults = dict(
        provider="gemini", terminal_id="T1", dispatch_id="test-gemini-oi155",
        instruction="test", model="sonnet", max_retries=3, no_auto_commit=False,
        gate="", dispatch_paths="", pr_id=None, role="developer",
        no_repo_map=False, tags=[],
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


@dataclass
class _R:
    returncode: int = 0
    completion_text: str = "ok"
    events_written: int = 1
    session_id: Optional[str] = None
    timed_out: bool = False
    stopped_early: bool = False
    token_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    event_writer_failures: int = 0


def _env_without_gemini():
    return {k: v for k, v in os.environ.items() if k != "VNX_GEMINI_MODEL"}


def _dispatch_with_env(args, env):
    es = MagicMock()
    with patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=_R()) as m, \
         patch("event_store.EventStore", return_value=es), \
         patch.object(pd, "_emit_governance"), \
         patch.object(pd, "_enrich_instruction", return_value="test"), \
         patch.object(pd, "_create_provider_worktree", return_value=None), \
         patch.dict(os.environ, env, clear=True):
        pd._dispatch_gemini(args)
    return m.call_args.kwargs.get("model")


class TestDispatchGeminiArgsModel(unittest.TestCase):
    def test_dispatch_gemini_respects_args_model(self):
        model = _dispatch_with_env(_args(model="gemini-2.5-flash"), _env_without_gemini())
        self.assertEqual(model, "gemini-2.5-flash")

    def test_dispatch_gemini_falls_back_to_env_when_args_sonnet(self):
        env = {**_env_without_gemini(), "VNX_GEMINI_MODEL": "gemini-2.5-flash"}
        model = _dispatch_with_env(_args(model="sonnet"), env)
        self.assertEqual(model, "gemini-2.5-flash")

    def test_dispatch_gemini_falls_back_to_default_when_neither(self):
        model = _dispatch_with_env(_args(model="sonnet"), _env_without_gemini())
        self.assertEqual(model, "gemini-2.5-pro")

    def test_resolve_gemini_model_respects_args_model(self):
        with patch.dict(os.environ, _env_without_gemini(), clear=True):
            result = pd._constraint_model_for_provider(_args(model="gemini-2.5-flash"), "gemini")
        self.assertEqual(result, "gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()
