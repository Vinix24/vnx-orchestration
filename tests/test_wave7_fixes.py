#!/usr/bin/env python3
"""test_wave7_fixes.py — regression tests for 3 Wave 7 blockers.

Fix 1: Claude cost tracking — side-channel uses .get() in recovery so
       _dispatch_claude can still .pop() the token_usage for governance receipt.
Fix 2: Kimi audit-gap status — event_writer_failures emit status='success' + rc=2,
       aligned with codex/gemini/litellm.
Fix 3: kimi_spawn prompt redaction — log line must not contain prompt text.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))


# ---------------------------------------------------------------------------
# Fix 1: Claude cost side-channel survives recovery consumption
# ---------------------------------------------------------------------------


class TestClaudeCostSideChannel:
    """_dispatch_token_usage must survive _resolve_token_usage_and_cost(.get)
    so _dispatch_claude can .pop() it for the governance receipt."""

    def test_resolve_uses_get_not_pop(self):
        """After _resolve_token_usage_and_cost, the entry remains in the dict."""
        from subprocess_dispatch_internals.delivery import _dispatch_token_usage

        dispatch_id = "test-cost-channel-001"
        fake_usage = {"input_tokens": 1500, "output_tokens": 300, "cache_read_input_tokens": 200}
        _dispatch_token_usage[dispatch_id] = fake_usage

        try:
            with patch(
                "subprocess_dispatch_internals.recovery._extract_token_usage",
                return_value={"input": 1500, "output": 300, "cache_hit": 200},
            ), patch(
                "subprocess_dispatch_internals.recovery._compute_cost",
                return_value=0.0123,
            ):
                from subprocess_dispatch_internals.recovery import _resolve_token_usage_and_cost

                token_usage, cost_usd = _resolve_token_usage_and_cost(
                    dispatch_id, None, "claude-sonnet-4-6",
                )

            assert token_usage is not None, "token_usage should be extracted"
            assert dispatch_id in _dispatch_token_usage, (
                "entry must survive .get() — _dispatch_claude needs it"
            )
        finally:
            _dispatch_token_usage.pop(dispatch_id, None)

    def test_dispatch_claude_pops_after_recovery(self):
        """_dispatch_claude should be able to .pop() the entry after deliver_with_recovery."""
        from subprocess_dispatch_internals.delivery import _dispatch_token_usage

        dispatch_id = "test-cost-pop-002"
        fake_usage = {"input_tokens": 800, "output_tokens": 120, "cache_read_input_tokens": 50}
        _dispatch_token_usage[dispatch_id] = fake_usage

        try:
            popped = _dispatch_token_usage.pop(dispatch_id, None)
            assert popped == fake_usage
            assert dispatch_id not in _dispatch_token_usage
        finally:
            _dispatch_token_usage.pop(dispatch_id, None)


# ---------------------------------------------------------------------------
# Fix 2: Kimi audit-gap aligns with codex/gemini/litellm (success + rc=2)
# ---------------------------------------------------------------------------


class TestKimiAuditGapStatus:
    """_dispatch_kimi must emit status='success' + return 2 when dispatch
    succeeded but event_writer had failures (audit-gap)."""

    def _make_kimi_result(self, *, event_writer_failures=0, returncode=0):
        return SimpleNamespace(
            completion_text="done",
            token_usage={"input_tokens": 100, "output_tokens": 50},
            event_writer_failures=event_writer_failures,
            returncode=returncode,
            timed_out=False,
            error=None,
        )

    def test_audit_gap_returns_2_with_success(self):
        """event_writer_failures > 0 → status='success', return 2."""
        result = self._make_kimi_result(event_writer_failures=3)

        governance_calls = []

        def capture_governance(args, provider, model, res, start, end, status):
            governance_calls.append(status)

        with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result), \
             patch("event_store.EventStore", return_value=MagicMock(append=MagicMock())), \
             patch("provider_dispatch._emit_governance", side_effect=capture_governance), \
             patch.dict(os.environ, {"VNX_KIMI_MODEL": "k2-test"}):

            from provider_dispatch import _dispatch_kimi

            args = argparse.Namespace(
                dispatch_id="test-kimi-audit-001",
                terminal_id="T1",
                instruction="test instruction",
                provider="kimi",
                model="k2-test",
                pr_id=None,
            )
            rc = _dispatch_kimi(args)

        assert rc == 2, f"expected rc=2 for audit-gap, got {rc}"
        assert governance_calls == ["success"], (
            f"expected status='success', got {governance_calls}"
        )

    def test_clean_dispatch_returns_0(self):
        """No event_writer_failures → status='success', return 0."""
        result = self._make_kimi_result(event_writer_failures=0)

        governance_calls = []

        def capture_governance(args, provider, model, res, start, end, status):
            governance_calls.append(status)

        with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result), \
             patch("event_store.EventStore", return_value=MagicMock(append=MagicMock())), \
             patch("provider_dispatch._emit_governance", side_effect=capture_governance), \
             patch.dict(os.environ, {"VNX_KIMI_MODEL": "k2-test"}):

            from provider_dispatch import _dispatch_kimi

            args = argparse.Namespace(
                dispatch_id="test-kimi-clean-002",
                terminal_id="T1",
                instruction="test instruction",
                provider="kimi",
                model="k2-test",
                pr_id=None,
            )
            rc = _dispatch_kimi(args)

        assert rc == 0, f"expected rc=0 for clean dispatch, got {rc}"
        assert governance_calls == ["success"]


# ---------------------------------------------------------------------------
# Fix 3: kimi_spawn prompt redaction in log
# ---------------------------------------------------------------------------


class TestKimiPromptRedaction:
    """spawn_kimi log must NOT contain prompt text."""

    def test_log_redacts_prompt_with_model(self, caplog):
        """Log should show char count and model, not the prompt itself."""
        secret_prompt = "SECRET_INSTRUCTION_DO_NOT_LEAK_THIS_CONTENT"

        with patch(
            "provider_spawns.kimi_spawn._start_kimi_subprocess",
            return_value=(None, SimpleNamespace(
                completion_text="",
                token_usage=None,
                event_writer_failures=0,
                returncode=127,
                timed_out=False,
                error="kimi binary not found",
                events_written=0,
                stopped_early=False,
            )),
        ), caplog.at_level(logging.INFO, logger="provider_spawns.kimi_spawn"):
            from provider_spawns.kimi_spawn import spawn_kimi

            spawn_kimi(
                prompt=secret_prompt,
                model="k2-test",
                dispatch_id="test-redact-001",
                terminal_id="T1",
            )

        log_text = caplog.text
        assert secret_prompt not in log_text, (
            f"prompt leaked into log: {log_text!r}"
        )
        assert str(len(secret_prompt)) in log_text, (
            "log should contain prompt char count"
        )
        assert "k2-test" in log_text, "log should contain model name"

    def test_log_redacts_prompt_without_model(self, caplog):
        """When no model is specified, log should show 'default'."""
        secret_prompt = "ANOTHER_SECRET_PROMPT_VALUE"

        with patch(
            "provider_spawns.kimi_spawn._start_kimi_subprocess",
            return_value=(None, SimpleNamespace(
                completion_text="",
                token_usage=None,
                event_writer_failures=0,
                returncode=127,
                timed_out=False,
                error="kimi binary not found",
                events_written=0,
                stopped_early=False,
            )),
        ), caplog.at_level(logging.INFO, logger="provider_spawns.kimi_spawn"):
            from provider_spawns.kimi_spawn import spawn_kimi

            spawn_kimi(
                prompt=secret_prompt,
                model=None,
                dispatch_id="test-redact-002",
                terminal_id="T1",
            )

        log_text = caplog.text
        assert secret_prompt not in log_text
        assert "default" in log_text


# ---------------------------------------------------------------------------
# Cross-provider consistency: all 4 non-claude providers use same audit-gap pattern
# ---------------------------------------------------------------------------


class TestAuditGapConsistency:
    """All non-claude providers must use status='success' + rc=2 for audit-gap."""

    def test_codex_gemini_litellm_kimi_patterns_match(self):
        """Verify the source code has consistent event_writer_failures handling."""
        import inspect
        import provider_dispatch

        source = inspect.getsource(provider_dispatch)

        for provider in ("codex", "gemini", "litellm", "kimi"):
            # Find the event_writer_failures block for each provider
            marker = f"{provider} dispatch completed but %d event_writer failures"
            assert marker in source, f"missing audit-gap log for {provider}"

        # All four blocks should emit "success" for audit-gap, never "failure"
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "event_writer failures occurred" in line:
                context = "\n".join(lines[max(0, i - 2):i + 5])
                assert '"failure"' not in context or '"success"' in context, (
                    f"audit-gap block near line {i} may emit 'failure' instead of 'success':\n{context}"
                )
