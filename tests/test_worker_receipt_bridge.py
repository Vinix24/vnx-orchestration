#!/usr/bin/env python3
"""Tests for worker→receipt bridge: token_usage, cost_usd, pr_id persistence."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
from types import SimpleNamespace

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

import subprocess_dispatch as sd


def _make_append_result(status="appended", path=None):
    result = MagicMock()
    result.status = status
    result.receipts_file = path or Path("/tmp/test-state/t0_receipts.ndjson")
    return result


class TestTokenUsageInReceipt:
    """token_usage is persisted in subprocess completion receipts."""

    def test_token_usage_included_in_receipt_payload(self, tmp_path):
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        token_usage = {"input": 1500, "output": 800, "cache_hit": 200}

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            sd._write_receipt(
                dispatch_id="test-tokens-001",
                terminal_id="T1",
                status="done",
                token_usage=token_usage,
            )

        receipt_arg = append_mod.append_receipt_payload.call_args[0][0]
        assert receipt_arg["token_usage"] == {"input": 1500, "output": 800, "cache_hit": 200}

    def test_token_usage_none_omitted_from_receipt(self, tmp_path):
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            sd._write_receipt(
                dispatch_id="test-tokens-002",
                terminal_id="T1",
                status="done",
            )

        receipt_arg = append_mod.append_receipt_payload.call_args[0][0]
        assert "token_usage" not in receipt_arg

    def test_token_usage_in_bare_write_fallback(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        receipt_file = state_dir / "t0_receipts.ndjson"

        token_usage = {"input": 3000, "output": 1200, "cache_hit": 500}

        with patch.dict("sys.modules", {"append_receipt": None}), \
             patch.object(sd, "_default_state_dir", return_value=state_dir):
            sd._write_receipt(
                dispatch_id="test-tokens-003",
                terminal_id="T2",
                status="done",
                token_usage=token_usage,
            )

        receipt_data = json.loads(receipt_file.read_text().strip())
        assert receipt_data["token_usage"] == {"input": 3000, "output": 1200, "cache_hit": 500}


class TestCostUsdInReceipt:
    """cost_usd is persisted in subprocess completion receipts."""

    def test_cost_usd_included_in_receipt_payload(self, tmp_path):
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            sd._write_receipt(
                dispatch_id="test-cost-001",
                terminal_id="T1",
                status="done",
                cost_usd=0.0423,
            )

        receipt_arg = append_mod.append_receipt_payload.call_args[0][0]
        assert receipt_arg["cost_usd"] == 0.0423

    def test_cost_usd_none_omitted_from_receipt(self, tmp_path):
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            sd._write_receipt(
                dispatch_id="test-cost-002",
                terminal_id="T1",
                status="done",
            )

        receipt_arg = append_mod.append_receipt_payload.call_args[0][0]
        assert "cost_usd" not in receipt_arg

    def test_cost_usd_in_bare_write_fallback(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        receipt_file = state_dir / "t0_receipts.ndjson"

        with patch.dict("sys.modules", {"append_receipt": None}), \
             patch.object(sd, "_default_state_dir", return_value=state_dir):
            sd._write_receipt(
                dispatch_id="test-cost-003",
                terminal_id="T2",
                status="done",
                cost_usd=0.156,
            )

        receipt_data = json.loads(receipt_file.read_text().strip())
        assert receipt_data["cost_usd"] == 0.156


class TestPrIdInReceipt:
    """pr_id is persisted in subprocess completion receipts."""

    def test_pr_id_included_in_receipt_payload(self, tmp_path):
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            sd._write_receipt(
                dispatch_id="test-prid-001",
                terminal_id="T1",
                status="done",
                pr_id="PR-565",
            )

        receipt_arg = append_mod.append_receipt_payload.call_args[0][0]
        assert receipt_arg["pr_id"] == "PR-565"

    def test_pr_id_none_omitted_from_receipt(self, tmp_path):
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            sd._write_receipt(
                dispatch_id="test-prid-002",
                terminal_id="T1",
                status="done",
            )

        receipt_arg = append_mod.append_receipt_payload.call_args[0][0]
        assert "pr_id" not in receipt_arg

    def test_pr_id_in_bare_write_fallback(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        receipt_file = state_dir / "t0_receipts.ndjson"

        with patch.dict("sys.modules", {"append_receipt": None}), \
             patch.object(sd, "_default_state_dir", return_value=state_dir):
            sd._write_receipt(
                dispatch_id="test-prid-003",
                terminal_id="T2",
                status="done",
                pr_id="PR-566",
            )

        receipt_data = json.loads(receipt_file.read_text().strip())
        assert receipt_data["pr_id"] == "PR-566"


class TestAllFieldsCombined:
    """All three fields (token_usage, cost_usd, pr_id) persist together."""

    def test_all_fields_in_single_receipt(self, tmp_path):
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        token_usage = {"input": 5000, "output": 2000, "cache_hit": 1000}

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            sd._write_receipt(
                dispatch_id="test-combined-001",
                terminal_id="T1",
                status="done",
                event_count=42,
                committed=True,
                commit_hash_before="aaa111",
                commit_hash_after="bbb222",
                token_usage=token_usage,
                cost_usd=0.0891,
                pr_id="PR-567",
            )

        receipt_arg = append_mod.append_receipt_payload.call_args[0][0]
        assert receipt_arg["dispatch_id"] == "test-combined-001"
        assert receipt_arg["status"] == "done"
        assert receipt_arg["event_count"] == 42
        assert receipt_arg["committed"] is True
        assert receipt_arg["token_usage"] == token_usage
        assert receipt_arg["cost_usd"] == 0.0891
        assert receipt_arg["pr_id"] == "PR-567"

    def test_backward_compat_no_new_fields(self, tmp_path):
        """Existing callers without new params still produce valid receipts."""
        expected_path = tmp_path / "t0_receipts.ndjson"
        mock_result = _make_append_result(status="appended", path=expected_path)

        append_mod = MagicMock()
        append_mod.append_receipt_payload.return_value = mock_result

        with patch.dict("sys.modules", {"append_receipt": append_mod}):
            sd._write_receipt(
                dispatch_id="test-compat-001",
                terminal_id="T3",
                status="success",
                event_count=10,
                session_id="sess-abc",
            )

        receipt_arg = append_mod.append_receipt_payload.call_args[0][0]
        assert receipt_arg["dispatch_id"] == "test-compat-001"
        assert receipt_arg["event_type"] == "subprocess_completion"
        assert "token_usage" not in receipt_arg
        assert "cost_usd" not in receipt_arg
        assert "pr_id" not in receipt_arg


class TestResolveTokenUsageAndCost:
    """_resolve_token_usage_and_cost extracts from side-channel or sub_result."""

    def test_reads_from_side_channel(self):
        from subprocess_dispatch_internals.recovery import _resolve_token_usage_and_cost
        from subprocess_dispatch_internals.delivery import _dispatch_token_usage

        _dispatch_token_usage["test-side-001"] = {
            "input_tokens": 2000,
            "output_tokens": 900,
            "cache_read_input_tokens": 150,
        }

        sub_result = SimpleNamespace(token_usage=None)

        with patch(
            "subprocess_dispatch_internals.recovery._compute_cost",
            return_value=0.05,
        ):
            token_usage, cost_usd = _resolve_token_usage_and_cost(
                "test-side-001", sub_result, "claude-sonnet-4-6"
            )

        assert token_usage == {"input": 2000, "output": 900, "cache_hit": 150}
        assert cost_usd == 0.05
        # .get() leaves entry for _dispatch_claude governance receipt; clean up
        assert "test-side-001" in _dispatch_token_usage
        _dispatch_token_usage.pop("test-side-001", None)

    def test_falls_back_to_sub_result_token_usage(self):
        from subprocess_dispatch_internals.recovery import _resolve_token_usage_and_cost
        from subprocess_dispatch_internals.delivery import _dispatch_token_usage

        _dispatch_token_usage.pop("test-fallback-001", None)

        sub_result = SimpleNamespace(token_usage={
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 0,
        })

        with patch(
            "subprocess_dispatch_internals.recovery._compute_cost",
            return_value=0.02,
        ):
            token_usage, cost_usd = _resolve_token_usage_and_cost(
                "test-fallback-001", sub_result, "claude-opus-4-6"
            )

        assert token_usage == {"input": 1000, "output": 500, "cache_hit": 0}
        assert cost_usd == 0.02

    def test_returns_none_when_no_usage(self):
        from subprocess_dispatch_internals.recovery import _resolve_token_usage_and_cost
        from subprocess_dispatch_internals.delivery import _dispatch_token_usage

        _dispatch_token_usage.pop("test-none-001", None)
        sub_result = SimpleNamespace(token_usage=None)

        token_usage, cost_usd = _resolve_token_usage_and_cost(
            "test-none-001", sub_result, "claude-sonnet-4-6"
        )

        assert token_usage is None
        assert cost_usd is None
