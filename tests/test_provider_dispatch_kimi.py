"""Tests for the kimi provider routing in provider_dispatch.py (Wave 7.7)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def _build_args(**overrides):
    """Build a minimal argparse.Namespace for dispatch tests."""
    import argparse

    defaults = {
        "provider": "kimi",
        "terminal_id": "T1",
        "dispatch_id": "test-dispatch-kimi-01",
        "instruction": "Say hi",
        "model": "default",
        "max_retries": 3,
        "no_auto_commit": False,
        "gate": "",
        "dispatch_paths": "",
        "pr_id": None,
        "role": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestProviderDispatchKimiArgParser(unittest.TestCase):
    def test_provider_kimi_accepted_in_arg_parser(self):
        import provider_dispatch as pd

        parser = pd._build_parser()
        args = parser.parse_args([
            "--provider", "kimi",
            "--terminal-id", "T1",
            "--dispatch-id", "d1",
            "--instruction", "test",
        ])
        self.assertEqual(args.provider, "kimi")

    def test_parser_help_mentions_kimi(self):
        import provider_dispatch as pd

        parser = pd._build_parser()
        help_text = parser.format_help()
        self.assertIn("kimi", help_text)


class TestDispatchKimiSuccess(unittest.TestCase):
    def _make_success_result(self):
        from provider_spawns.kimi_spawn import KimiSpawnResult

        return KimiSpawnResult(
            returncode=0,
            completion_text="Hello from Kimi!",
            events_written=3,
            session_id=None,
            timed_out=False,
            stopped_early=False,
            token_usage={"input_tokens": 50, "output_tokens": 20, "cache_read_tokens": 0, "cache_creation_tokens": 0},
            error=None,
            event_writer_failures=0,
        )

    def test_dispatch_kimi_emits_receipt_with_provider_kimi(self):
        import provider_dispatch as pd

        args = _build_args()
        result = self._make_success_result()

        with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result), \
             patch("event_store.EventStore", side_effect=Exception("no store")), \
             patch("governance_emit.emit_dispatch_receipt") as mock_receipt, \
             patch("governance_emit.emit_unified_report") as mock_report:
            mock_receipt.return_value = Path("/tmp/receipts.ndjson")
            mock_report.return_value = Path("/tmp/report.md")
            exit_code = pd._dispatch_kimi(args)

        self.assertEqual(exit_code, 0)
        mock_receipt.assert_called_once()
        # provider is always passed as keyword arg
        call_kwargs = mock_receipt.call_args.kwargs
        self.assertEqual(call_kwargs.get("provider"), "kimi")

    def test_dispatch_kimi_emits_unified_report(self):
        import provider_dispatch as pd

        args = _build_args()
        result = self._make_success_result()

        with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result), \
             patch("event_store.EventStore", side_effect=Exception("no store")), \
             patch("governance_emit.emit_dispatch_receipt") as mock_receipt, \
             patch("governance_emit.emit_unified_report") as mock_report:
            mock_receipt.return_value = Path("/tmp/receipts.ndjson")
            mock_report.return_value = Path("/tmp/report.md")
            pd._dispatch_kimi(args)

        mock_report.assert_called_once()

    def test_dispatch_kimi_failure_emits_receipt_with_status_failure(self):
        import provider_dispatch as pd
        from provider_spawns.kimi_spawn import KimiSpawnResult

        args = _build_args()
        fail_result = KimiSpawnResult(
            returncode=1,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            error="kimi exited with code 1",
        )

        with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=fail_result), \
             patch("event_store.EventStore", side_effect=Exception("no store")), \
             patch("governance_emit.emit_dispatch_receipt") as mock_receipt, \
             patch("governance_emit.emit_unified_report") as mock_report:
            mock_receipt.return_value = Path("/tmp/receipts.ndjson")
            mock_report.return_value = Path("/tmp/report.md")
            exit_code = pd._dispatch_kimi(args)

        self.assertEqual(exit_code, 1)
        mock_receipt.assert_called_once()
        call_kwargs = mock_receipt.call_args.kwargs
        self.assertEqual(call_kwargs.get("status"), "failure")


class TestComputeKimiCost(unittest.TestCase):
    def test_cost_computed_when_usage_present(self):
        import provider_dispatch as pd

        token_usage = {"input": 1_000_000, "output": 500_000, "cache_hit": 0}
        # Without registry (isolated): returns None gracefully
        with patch("provider_dispatch._compute_kimi_cost") as mock_cost:
            mock_cost.return_value = 0.00185
            cost = pd._compute_cost("kimi", "kimi-default", token_usage)
        self.assertEqual(cost, 0.00185)

    def test_compute_kimi_cost_returns_none_on_zero_usage(self):
        import provider_dispatch as pd

        cost = pd._compute_kimi_cost("kimi-default", {"input": 0, "output": 0, "cache_hit": 0})
        self.assertIsNone(cost)

    def test_compute_kimi_cost_returns_none_when_registry_missing(self):
        import provider_dispatch as pd

        with patch("provider_dispatch._compute_kimi_cost", wraps=pd._compute_kimi_cost):
            # Patch load to raise FileNotFoundError
            with patch("providers.provider_registry.load", side_effect=FileNotFoundError):
                cost = pd._compute_kimi_cost("kimi-default", {"input": 100, "output": 50, "cache_hit": 0})
        self.assertIsNone(cost)

    def test_extract_token_usage_kimi_uses_input_output_keys(self):
        import provider_dispatch as pd
        from provider_spawns.kimi_spawn import KimiSpawnResult

        result = KimiSpawnResult(
            returncode=0,
            completion_text="",
            events_written=1,
            session_id=None,
            timed_out=False,
            token_usage={"input_tokens": 300, "output_tokens": 120, "cache_read_tokens": 5, "cache_creation_tokens": 0},
        )
        usage = pd._extract_token_usage(result, "kimi")
        self.assertEqual(usage["input"], 300)
        self.assertEqual(usage["output"], 120)
        self.assertEqual(usage["cache_hit"], 5)


if __name__ == "__main__":
    unittest.main()
