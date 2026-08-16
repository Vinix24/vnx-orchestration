#!/usr/bin/env python3
"""test_provider_delivery_observability.py — provider-lane delivery-state receipts.

dispatch 20260816-p10b-provider-observability: delivery and reporting must be
separately observable on the PROVIDER lane. This module verifies the four
delivery states land in the receipt ledger (not just stdout):

  session_ready    — delivered AND reported (status=success)
  submit_failed    — delivered but nothing reported (status=failure/timeout)
  deliver_failed   — never delivered (spawn boundary returncode 126/127, or an
                     uncaught spawn exception in main())
  delivery_refused — refused for delivery (pre-flight gate: constraint
                     violation, missing key, model-resolution failure,
                     claude-not-a-provider reject, delegation reject)

Plus a constructed death case (invalid key -> 401/402 provider error, no real
credits) proving the stored state is readable from t0_receipts.ndjson alone,
without consulting the door log.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import provider_dispatch as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        dispatch_id="delivery-test-001",
        terminal_id="T1",
        pr_id=None,
        instruction="Reply OK.",
        model="deepseek-v4-pro",
        provider="deepseek-harness",
        role="backend-developer",
        project_id="test-proj",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class _FakeResult:
    """Minimal spawn result carrying only what _emit_governance reads."""

    def __init__(self, *, error=None, completion_text=None, timed_out=False,
                 returncode=None, token_usage=None, session_id=None):
        self.error = error
        self.completion_text = completion_text
        self.timed_out = timed_out
        self.returncode = returncode
        self.token_usage = token_usage if token_usage is not None else {"input": 0, "output": 0}
        self.session_id = session_id


def _now():
    return datetime.now(timezone.utc)


def _read_receipts(state_dir: Path) -> list[dict]:
    receipt_file = state_dir / "t0_receipts.ndjson"
    assert receipt_file.exists(), f"expected receipt ledger at {receipt_file}"
    lines = [ln for ln in receipt_file.read_text().splitlines() if ln.strip()]
    assert lines, "receipt ledger must be non-empty"
    return [json.loads(ln) for ln in lines]


def _find_dispatch(receipts: list[dict], dispatch_id: str) -> dict:
    matching = [r for r in receipts if r.get("dispatch_id") == dispatch_id]
    assert len(matching) == 1, f"expected exactly 1 receipt for {dispatch_id}, got {len(matching)}"
    return matching[0]


# ---------------------------------------------------------------------------
# 1. _derive_delivery_state: the four states
# ---------------------------------------------------------------------------

class TestDeriveDeliveryState:
    def test_success_is_session_ready(self):
        assert pd._derive_delivery_state("success", _FakeResult(returncode=0)) == "session_ready"

    def test_blocked_is_delivery_refused(self):
        assert pd._derive_delivery_state("blocked", _FakeResult(returncode=64)) == "delivery_refused"

    def test_returncode_126_is_deliver_failed(self):
        assert pd._derive_delivery_state("failure", _FakeResult(returncode=126)) == "deliver_failed"

    def test_returncode_127_is_deliver_failed(self):
        assert pd._derive_delivery_state("failure", _FakeResult(returncode=127)) == "deliver_failed"

    def test_failure_with_normal_returncode_is_submit_failed(self):
        assert pd._derive_delivery_state("failure", _FakeResult(returncode=1)) == "submit_failed"

    def test_timeout_is_submit_failed(self):
        assert pd._derive_delivery_state("timeout", _FakeResult(timed_out=True)) == "submit_failed"


# ---------------------------------------------------------------------------
# 2. _emit_governance stamps the derived delivery_state onto the receipt
# ---------------------------------------------------------------------------

class TestEmitGovernanceDeliveryState:
    def _emit(self, status, result):
        args = _make_args()
        with (
            patch("provider_costs.emit_provider_cost", return_value=None),
            patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/r.ndjson")) as mock_receipt,
            patch("governance_emit.emit_unified_report", return_value=Path("/tmp/report.md")),
        ):
            pd._emit_governance(
                args, args.provider, args.model, result, _now(), _now(), status,
            )
        return mock_receipt.call_args.kwargs

    def test_success_stamps_session_ready(self):
        kwargs = self._emit("success", _FakeResult(returncode=0))
        assert kwargs["delivery_state"] == "session_ready"

    def test_blocked_stamps_delivery_refused(self):
        kwargs = self._emit("blocked", _FakeResult(returncode=64))
        assert kwargs["delivery_state"] == "delivery_refused"

    def test_returncode_127_stamps_deliver_failed(self):
        kwargs = self._emit("failure", _FakeResult(returncode=127))
        assert kwargs["delivery_state"] == "deliver_failed"

    def test_normal_failure_stamps_submit_failed(self):
        kwargs = self._emit("failure", _FakeResult(returncode=1, error="some error"))
        assert kwargs["delivery_state"] == "submit_failed"


# ---------------------------------------------------------------------------
# 3. _emit_refusal_receipt writes delivery_refused to storage
# ---------------------------------------------------------------------------

class TestRefusalReceiptStorage:
    def test_refusal_lands_in_ledger_not_just_stdout(self, tmp_path):
        state_dir = tmp_path / "state"
        args = _make_args(dispatch_id="refusal-001")

        with patch.dict(
            "os.environ",
            {"VNX_STATE_DIR": str(state_dir), "VNX_DATA_DIR_EXPLICIT": "1"},
            clear=False,
        ):
            pd._emit_refusal_receipt(args, "claude", "sonnet", "claude is not a provider-lane provider")

        r = _find_dispatch(_read_receipts(state_dir), "refusal-001")
        assert r["delivery_state"] == "delivery_refused"
        assert r["status"] == "blocked"
        assert "claude is not a provider-lane provider" in r["failure_reason"]
        assert r["failure_class"], "failure_class must be stamped (reuse of the receipt taxonomy)"

    def test_refusal_derive_deliver_failed_override(self, tmp_path):
        state_dir = tmp_path / "state"
        args = _make_args(dispatch_id="refusal-002")

        with patch.dict(
            "os.environ",
            {"VNX_STATE_DIR": str(state_dir), "VNX_DATA_DIR_EXPLICIT": "1"},
            clear=False,
        ):
            pd._emit_refusal_receipt(
                args, "deepseek-harness", "deepseek-v4-pro",
                "uncaught spawn exception: RuntimeError('boom')",
                delivery_state=pd._DELIVERY_STATE_DELIVER_FAILED,
                status="failure",
            )

        r = _find_dispatch(_read_receipts(state_dir), "refusal-002")
        assert r["delivery_state"] == "deliver_failed"
        assert r["status"] == "failure"


# ---------------------------------------------------------------------------
# 4. Constructed death case: invalid key -> 401/402, stored state readable
#    WITHOUT the door log.
# ---------------------------------------------------------------------------

class TestDeathCaseStoredStateReadableWithoutDoorLog:
    """A provider dispatch failing on quota/auth via an invalid key (no real
    credits, no real subprocess) must leave a receipt whose delivery_state,
    failure_class, and failure_reason are self-contained in t0_receipts.ndjson."""

    def _run_death_case(self, tmp_path, dispatch_id, error_text):
        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        args = _make_args(dispatch_id=dispatch_id)
        result = _FakeResult(returncode=1, error=error_text, completion_text=None)

        with patch.dict(
            "os.environ",
            {
                "VNX_STATE_DIR": str(state_dir),
                "VNX_DATA_DIR": str(data_dir),
                "VNX_DATA_DIR_EXPLICIT": "1",
            },
            clear=False,
        ), patch("provider_costs.emit_provider_cost", return_value=None):
            pd._emit_governance(
                args, "deepseek-harness", "deepseek-v4-pro",
                result, _now(), _now(), "failure",
            )

        return _find_dispatch(_read_receipts(state_dir), dispatch_id)

    def test_auth_error_stored_state(self, tmp_path):
        r = self._run_death_case(
            tmp_path, "death-auth-001",
            "API Error: 401 Unauthorized - invalid API key",
        )
        assert r["status"] == "failure"
        assert r["delivery_state"] == "submit_failed"
        assert r["failure_class"] == "auth_rejected"
        # The reason is readable from the receipt alone (no door log needed).
        assert "401" in r["failure_reason"] or "invalid API key" in r["failure_reason"]

    def test_quota_error_stored_state(self, tmp_path):
        r = self._run_death_case(
            tmp_path, "death-quota-001",
            "API Error: 402 Insufficient Balance",
        )
        assert r["status"] == "failure"
        assert r["delivery_state"] == "submit_failed"
        assert r["failure_class"] == "credit_exhausted"
        assert "credit" in r["failure_reason"].lower()


# ---------------------------------------------------------------------------
# 5. main() catches an uncaught spawn exception and records deliver_failed
# ---------------------------------------------------------------------------

class TestMainUncaughtDeliverFailed:
    def test_uncaught_spawn_exception_writes_deliver_failed(self, tmp_path):
        state_dir = tmp_path / "state"
        with patch.dict(
            "os.environ",
            {"VNX_STATE_DIR": str(state_dir), "VNX_DATA_DIR_EXPLICIT": "1"},
            clear=False,
        ), patch.object(pd, "_check_constraints", return_value=[]), \
           patch.object(pd, "_dispatch_gemini", side_effect=RuntimeError("spawn exploded")):
            rc = pd.main([
                "--provider", "gemini",
                "--terminal-id", "T1",
                "--dispatch-id", "deliver-failed-001",
                "--instruction", "noop",
            ])

        assert rc == 1
        r = _find_dispatch(_read_receipts(state_dir), "deliver-failed-001")
        assert r["delivery_state"] == "deliver_failed"
        assert r["status"] == "failure"
        assert "spawn exploded" in r["failure_reason"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
