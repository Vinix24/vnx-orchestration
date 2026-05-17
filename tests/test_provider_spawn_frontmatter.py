"""test_provider_spawn_frontmatter.py — Tests for SpawnResult.frontmatter_fields() (PR-D5-F).

Verifies that all 5 provider spawn modules produce a consistent frontmatter dict
compatible with the unified_report_v1 schema token_usage shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib" / "provider_spawns"))

from provider_spawns.claude_spawn import ClaudeSpawnResult
from provider_spawns.codex_spawn import CodexSpawnResult
from provider_spawns.gemini_spawn import GeminiSpawnResult
from provider_spawns.kimi_spawn import KimiSpawnResult
from provider_spawns.litellm_spawn import LiteLLMSpawnResult


SCHEMA_KEYS = {"provider", "sub_provider", "exit_code", "token_usage"}
TOKEN_USAGE_KEYS = {"input", "output", "cache_read"}


# --- ClaudeSpawnResult ---


class TestClaudeFrontmatter:
    def test_with_full_usage(self):
        r = ClaudeSpawnResult(
            returncode=0,
            completion={"agent_message": "done"},
            events_written=5,
            session_id="sess-1",
            timed_out=False,
            token_usage={
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_input_tokens": 200,
            },
        )
        fm = r.frontmatter_fields()
        assert fm["provider"] == "claude"
        assert fm["sub_provider"] == "anthropic"
        assert fm["exit_code"] == 0
        assert fm["token_usage"] == {"input": 1000, "output": 500, "cache_read": 200}

    def test_without_usage(self):
        r = ClaudeSpawnResult(
            returncode=1,
            completion={},
            events_written=0,
            session_id=None,
            timed_out=True,
            token_usage=None,
        )
        fm = r.frontmatter_fields()
        assert fm["exit_code"] == 1
        assert fm["token_usage"] == {"input": 0, "output": 0, "cache_read": 0}

    def test_partial_usage(self):
        r = ClaudeSpawnResult(
            returncode=0,
            completion={},
            events_written=1,
            session_id=None,
            timed_out=False,
            token_usage={"input_tokens": 50},
        )
        fm = r.frontmatter_fields()
        assert fm["token_usage"] == {"input": 50, "output": 0, "cache_read": 0}


# --- CodexSpawnResult ---


class TestCodexFrontmatter:
    def test_with_full_usage(self):
        r = CodexSpawnResult(
            returncode=0,
            completion_text="result",
            events_written=10,
            session_id="codex-sess",
            timed_out=False,
            token_usage={
                "input_tokens": 800,
                "output_tokens": 300,
                "cache_read_tokens": 50,
            },
        )
        fm = r.frontmatter_fields()
        assert fm["provider"] == "codex"
        assert fm["sub_provider"] == "openai"
        assert fm["exit_code"] == 0
        assert fm["token_usage"] == {"input": 800, "output": 300, "cache_read": 50}

    def test_null_usage(self):
        r = CodexSpawnResult(
            returncode=127,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            token_usage=None,
        )
        fm = r.frontmatter_fields()
        assert fm["token_usage"] == {"input": 0, "output": 0, "cache_read": 0}


# --- GeminiSpawnResult ---


class TestGeminiFrontmatter:
    def test_with_usage(self):
        r = GeminiSpawnResult(
            returncode=0,
            completion_text="review",
            events_written=3,
            session_id=None,
            timed_out=False,
            token_usage={
                "input_tokens": 2000,
                "output_tokens": 1500,
                "cache_read_tokens": 0,
            },
        )
        fm = r.frontmatter_fields()
        assert fm["provider"] == "gemini"
        assert fm["sub_provider"] == "google"
        assert fm["token_usage"] == {"input": 2000, "output": 1500, "cache_read": 0}

    def test_no_usage(self):
        r = GeminiSpawnResult(
            returncode=1,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=True,
        )
        fm = r.frontmatter_fields()
        assert fm["token_usage"] == {"input": 0, "output": 0, "cache_read": 0}


# --- KimiSpawnResult ---


class TestKimiFrontmatter:
    def test_with_usage(self):
        r = KimiSpawnResult(
            returncode=0,
            completion_text="output",
            events_written=8,
            session_id=None,
            timed_out=False,
            token_usage={
                "input_tokens": 600,
                "output_tokens": 400,
                "cache_read_tokens": 0,
            },
        )
        fm = r.frontmatter_fields()
        assert fm["provider"] == "kimi"
        assert fm["sub_provider"] == "moonshot"
        assert fm["token_usage"] == {"input": 600, "output": 400, "cache_read": 0}

    def test_null_usage(self):
        r = KimiSpawnResult(
            returncode=0,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            token_usage=None,
        )
        fm = r.frontmatter_fields()
        assert fm["token_usage"] == {"input": 0, "output": 0, "cache_read": 0}


# --- LiteLLMSpawnResult ---


class TestLiteLLMFrontmatter:
    def test_openai_format(self):
        r = LiteLLMSpawnResult(
            returncode=0,
            completion_text="done",
            events_written=12,
            session_id=None,
            timed_out=False,
            token_usage={
                "prompt_tokens": 1200,
                "completion_tokens": 700,
                "prompt_tokens_details": {"cached_tokens": 300},
            },
        )
        fm = r.frontmatter_fields()
        assert fm["provider"] == "litellm"
        assert fm["sub_provider"] == "none"
        assert fm["token_usage"] == {"input": 1200, "output": 700, "cache_read": 300}

    def test_cache_hit_fallback(self):
        r = LiteLLMSpawnResult(
            returncode=0,
            completion_text="done",
            events_written=5,
            session_id=None,
            timed_out=False,
            token_usage={
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "prompt_cache_hit_tokens": 100,
            },
        )
        fm = r.frontmatter_fields()
        assert fm["token_usage"] == {"input": 500, "output": 200, "cache_read": 100}

    def test_null_usage(self):
        r = LiteLLMSpawnResult(
            returncode=1,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
        )
        fm = r.frontmatter_fields()
        assert fm["token_usage"] == {"input": 0, "output": 0, "cache_read": 0}


# --- Cross-provider consistency ---


class TestCrossProviderConsistency:
    def _all_results(self):
        return [
            ClaudeSpawnResult(
                returncode=0, completion={}, events_written=0,
                session_id=None, timed_out=False,
            ),
            CodexSpawnResult(
                returncode=0, completion_text="", events_written=0,
                session_id=None, timed_out=False,
            ),
            GeminiSpawnResult(
                returncode=0, completion_text="", events_written=0,
                session_id=None, timed_out=False,
            ),
            KimiSpawnResult(
                returncode=0, completion_text="", events_written=0,
                session_id=None, timed_out=False,
            ),
            LiteLLMSpawnResult(
                returncode=0, completion_text="", events_written=0,
                session_id=None, timed_out=False,
            ),
        ]

    def test_all_return_same_keys(self):
        for r in self._all_results():
            fm = r.frontmatter_fields()
            assert set(fm.keys()) == SCHEMA_KEYS, f"{type(r).__name__} keys mismatch"
            assert set(fm["token_usage"].keys()) == TOKEN_USAGE_KEYS, (
                f"{type(r).__name__} token_usage keys mismatch"
            )

    def test_all_token_usage_values_are_ints(self):
        results_with_usage = [
            ClaudeSpawnResult(
                returncode=0, completion={}, events_written=0,
                session_id=None, timed_out=False,
                token_usage={"input_tokens": 1, "output_tokens": 2},
            ),
            CodexSpawnResult(
                returncode=0, completion_text="", events_written=0,
                session_id=None, timed_out=False,
                token_usage={"input_tokens": 1, "output_tokens": 2},
            ),
            GeminiSpawnResult(
                returncode=0, completion_text="", events_written=0,
                session_id=None, timed_out=False,
                token_usage={"input_tokens": 1, "output_tokens": 2},
            ),
            KimiSpawnResult(
                returncode=0, completion_text="", events_written=0,
                session_id=None, timed_out=False,
                token_usage={"input_tokens": 1, "output_tokens": 2},
            ),
            LiteLLMSpawnResult(
                returncode=0, completion_text="", events_written=0,
                session_id=None, timed_out=False,
                token_usage={"prompt_tokens": 1, "completion_tokens": 2},
            ),
        ]
        for r in results_with_usage:
            fm = r.frontmatter_fields()
            for k, v in fm["token_usage"].items():
                assert isinstance(v, int), (
                    f"{type(r).__name__}.token_usage[{k}] is {type(v).__name__}, expected int"
                )

    def test_exit_code_reflects_returncode(self):
        for r in self._all_results():
            fm = r.frontmatter_fields()
            assert fm["exit_code"] == r.returncode
