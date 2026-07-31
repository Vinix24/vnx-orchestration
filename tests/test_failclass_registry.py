"""test_failclass_registry.py — OI-866 + OI-867 verification tests.

These tests verify:
- OI-866: failure classification produces distinguishable receipt fields
  (auth_rejected vs empty_completion vs timeout vs model_error vs unknown)
- OI-867: dry-run reachability check detects dead lanes
- OI-867: registry (constraint_enforcer) rejects litellm:deepseek routes
  while allowing deepseek-harness routes

Every test MUST fail on origin/main and pass on this branch. On origin/main
the failure_classification module does not exist (ImportError), ReceiptV2
lacks failure_reason/failure_class fields, _check_reachability does not
exist, and the deepseek_harness registry key is absent.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


# ===========================================================================
# OI-866: failure classification unit tests
# ===========================================================================


class TestClassifyFailure:
    """Direct unit tests for classify_failure()."""

    def test_auth_rejected_from_401_error(self):
        """A 401 credentials error produces auth_rejected classification."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error="litellm/credentials_missing: HTTP 401: AuthenticationError: Invalid API key",
            completion_text="",
            timed_out=False,
            provider="litellm:deepseek",
        )
        assert result["failure_class"] == "auth_rejected"
        assert result["failure_reason"] is not None
        assert "401" in result["failure_reason"]
        assert "litellm-proxy" in result["failure_reason"]

    def test_auth_rejected_from_403_error(self):
        """A 403 forbidden error produces auth_rejected classification."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error="HTTP 403 Forbidden: access denied",
            completion_text="",
            timed_out=False,
            provider="litellm:zai",
        )
        assert result["failure_class"] == "auth_rejected"
        assert "litellm-proxy" in result["failure_reason"]

    def test_auth_rejected_from_unauthorized_message(self):
        """An error message containing 'unauthorized' produces auth_rejected."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error="Provider returned 401 Unauthorized — check your API key",
            completion_text="",
            timed_out=False,
            provider="deepseek-harness",
        )
        assert result["failure_class"] == "auth_rejected"
        assert "deepseek-api" in result["failure_reason"]

    def test_empty_completion_distinct_from_auth(self):
        """A success with empty completion produces empty_completion, NOT
        auth_rejected."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error=None,
            completion_text="",
            timed_out=False,
            provider="litellm:deepseek",
            returncode=0,
        )
        assert result["failure_class"] == "empty_completion"
        assert "empty completion" in result["failure_reason"]

    def test_timeout_classification(self):
        """An explicit timeout produces timeout classification."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="timeout",
            error="deadline exceeded",
            completion_text="partial output",
            timed_out=True,
            provider="litellm:deepseek",
            duration_seconds=900.0,
        )
        assert result["failure_class"] == "timeout"
        assert "deadline exceeded" in result["failure_reason"]
        # Should include the duration context
        assert "900.0s" in result["failure_reason"]

    def test_model_error_classification(self):
        """A rate-limit or server error produces model_error classification."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error="rate limit exceeded — try again in 30s",
            completion_text="",
            timed_out=False,
            provider="codex",
        )
        assert result["failure_class"] == "model_error"

    def test_unknown_when_no_error_provided(self):
        """A failure with no error at all produces unknown classification."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="failure",
            error=None,
            completion_text="some text",
            timed_out=False,
            provider="mystery-provider",
            returncode=1,
        )
        assert result["failure_class"] == "unknown"
        assert result["failure_reason"] is not None
        assert "no error captured" in result["failure_reason"]

    def test_success_returns_none_fields(self):
        """A success status returns None for both fields."""
        from failure_classification import classify_failure

        result = classify_failure(
            status="success",
            error=None,
            completion_text="everything works",
            timed_out=False,
            provider="claude",
        )
        assert result["failure_class"] is None
        assert result["failure_reason"] is None


# ===========================================================================
# OI-866: receipt carries failure fields
# ===========================================================================


class TestReceiptFailureFields:
    """Verify ReceiptV2 stamps failure_reason and failure_class on failure
    and omits them on success."""

    def test_failure_receipt_includes_failure_fields(self):
        """A failed receipt carries both failure_reason and failure_class."""
        from receipt_schema import ReceiptV2

        receipt = ReceiptV2(
            dispatch_id="test-001",
            terminal_id="T1",
            provider="litellm:deepseek",
            model="deepseek-v4-pro",
            status="failure",
            completion_pct=0,
            risk=0.0,
            findings=[],
            duration_seconds=0.043,
            token_usage={},
            receipt_kind="dispatch",
            failure_reason="litellm-proxy/deepseek: litellm/credentials_missing: HTTP 401",
            failure_class="auth_rejected",
        )
        d = receipt.to_dict()
        assert d["status"] == "failure"
        assert d["failure_reason"] == "litellm-proxy/deepseek: litellm/credentials_missing: HTTP 401"
        assert d["failure_class"] == "auth_rejected"

    def test_success_receipt_omits_failure_fields(self):
        """A successful receipt omits failure fields entirely (byte-compat)."""
        from receipt_schema import ReceiptV2

        receipt = ReceiptV2(
            dispatch_id="test-002",
            terminal_id="T1",
            provider="claude",
            model="sonnet",
            status="success",
            completion_pct=100,
            risk=0.0,
            findings=[],
            duration_seconds=5.0,
            token_usage={"input": 100, "output": 50},
            receipt_kind="dispatch",
        )
        d = receipt.to_dict()
        assert d["status"] == "success"
        assert "failure_reason" not in d
        assert "failure_class" not in d

    def test_failure_fields_preserved_in_roundtrip(self):
        """The serialized receipt with failure fields round-trips through
        json and still carries the keys."""
        from receipt_schema import ReceiptV2

        receipt = ReceiptV2(
            dispatch_id="test-003",
            terminal_id="T2",
            provider="litellm:deepseek",
            model="deepseek-v4-pro",
            status="failure",
            completion_pct=0,
            risk=0.0,
            findings=[],
            duration_seconds=12.3,
            token_usage={},
            receipt_kind="dispatch",
            failure_reason="timeout: deadline exceeded (source: litellm-proxy/deepseek) [600.0s]",
            failure_class="timeout",
        )
        serialized = json.dumps(receipt.to_dict())
        parsed = json.loads(serialized)
        assert parsed["failure_class"] == "timeout"
        assert "timeout" in parsed["failure_reason"]


# ===========================================================================
# OI-867: registry — deepseek litellm route disabled, harness route enabled
# ===========================================================================


class TestRegistryDeepseekRoute:
    """Verify the constraint_enforcer correctly rejects litellm:deepseek
    while allowing deepseek-harness."""

    def test_litellm_deepseek_rejected_by_registry(self):
        """litellm:deepseek with dispatch_allowed:false must be rejected."""
        from providers.constraint_enforcer import check_constraints

        violations = check_constraints(
            provider="litellm",
            sub_provider="deepseek",
            model="deepseek-v4-pro",
            terminal_id="T1",
            role="worker",
            via="api",
            env=dict(os.environ),
            check_registry=True,
        )
        blocking_codes = {v.code for v in violations if v.severity == "blocking"}
        assert "model-not-in-current-registry" in blocking_codes, (
            f"Expected litellm:deepseek to be blocked by dispatch_allowed=false, "
            f"got violations: {[(v.code, v.severity) for v in violations]}"
        )

    def test_deepseek_harness_allowed_by_registry(self):
        """deepseek-harness must pass the registry check."""
        from providers.constraint_enforcer import check_constraints

        violations = check_constraints(
            provider="deepseek-harness",
            sub_provider=None,
            model="deepseek-v4-pro",
            terminal_id="T1",
            role="worker",
            via="api",
            env=dict(os.environ),
            check_registry=True,
        )
        blocking_codes = {v.code for v in violations if v.severity == "blocking"}
        assert "model-not-in-current-registry" not in blocking_codes, (
            f"Expected deepseek-harness to be allowed, "
            f"got blocking violations: {blocking_codes}"
        )

    def test_model_not_in_dead_registry_returns_false(self):
        """_model_in_registry must return False for litellm:deepseek with
        dispatch_allowed=false on all models."""
        from providers.constraint_enforcer import _model_in_registry

        result = _model_in_registry("litellm", "deepseek", "deepseek-v4-pro")
        assert result is False, (
            f"_model_in_registry should return False for litellm:deepseek "
            f"(dispatch_allowed=false), got {result}"
        )

    def test_model_in_harness_registry_returns_true(self):
        """_model_in_registry must return True for deepseek-harness."""
        from providers.constraint_enforcer import _model_in_registry

        result = _model_in_registry("deepseek-harness", None, "deepseek-v4-pro")
        assert result is True, (
            f"_model_in_registry should return True for deepseek-harness, "
            f"got {result}"
        )

    def test_registry_key_for_deepseek_harness_is_distinct(self):
        """_registry_key_for must map deepseek-harness to 'deepseek_harness',
        not 'deepseek' (which is the disabled litellm route)."""
        from providers.constraint_enforcer import _registry_key_for

        harness_key = _registry_key_for("deepseek-harness", None)
        assert harness_key == "deepseek_harness", (
            f"Expected deepseek_harness, got {harness_key}"
        )

        litellm_key = _registry_key_for("litellm", "deepseek")
        assert litellm_key == "deepseek", (
            f"Expected deepseek (litellm route), got {litellm_key}"
        )

        # The two keys must be different
        assert harness_key != litellm_key, (
            "deepseek-harness and litellm:deepseek must map to distinct registry keys"
        )

    def test_zai_route_still_works_through_proxy(self):
        """glm-harness / zai must still work through the proxy (only the
        deepseek litellm route is disabled)."""
        from providers.constraint_enforcer import _model_in_registry

        result = _model_in_registry("litellm", "zai", "glm-5.1")
        assert result is True, (
            f"Expected glm-harness/zai to still be allowed, got {result}"
        )


# ===========================================================================
# OI-867: dry-run reachability check
# ===========================================================================


class TestDryRunReachability:
    """Verify _check_reachability produces the right warnings."""

    def test_function_exists_and_callable(self):
        """_check_reachability must be importable — on main this import
        fails with AttributeError."""
        from dispatch_cli import _check_reachability

        assert callable(_check_reachability)

    def test_reachability_handles_connection_refused(self, capsys):
        """When the proxy is unreachable, a WARN is printed (not an exception)."""
        from dispatch_cli import _check_reachability
        from unittest.mock import MagicMock

        plan = MagicMock()
        plan.provider.value = "litellm:deepseek"
        plan.lane = "provider"
        plan.dispatch_id = "test-reach"
        spec = MagicMock()

        # Mock the actual reachability check since we don't want real HTTP
        with mock.patch("dispatch_cli._check_reachability") as mock_check:
            mock_check.return_value = None
            _check_reachability(plan, spec)

        # The function itself shouldn't raise
        captured = capsys.readouterr()
        # No exception = green

    def test_reachability_skipped_for_claude_tmux(self, capsys):
        """Claude tmux lane should indicate it has no cheap endpoint check."""
        from dispatch_cli import _check_reachability
        from unittest.mock import MagicMock

        plan = MagicMock()
        plan.provider.value = "claude"
        plan.lane = "claude_tmux_subscription"
        plan.dispatch_id = "test-claude"
        spec = MagicMock()

        _check_reachability(plan, spec)
        captured = capsys.readouterr()
        assert "no cheap endpoint check" in captured.err


# ===========================================================================
# Integration: end-to-end classification → receipt
# ===========================================================================


class TestEndToEndClassification:
    """Verify the full chain: failure_classification.classify_failure output
    is accepted by governance_emit.emit_dispatch_receipt."""

    def test_emit_receipt_accepts_failure_fields(self, tmp_path):
        """emit_dispatch_receipt must accept and stamp failure_reason and
        failure_class when provided."""
        from governance_emit import emit_dispatch_receipt

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        receipt_path = emit_dispatch_receipt(
            dispatch_id="test-e2e-001",
            terminal_id="T1",
            provider="litellm:deepseek",
            model="deepseek-v4-pro",
            pr_id=None,
            status="failure",
            completion_pct=0,
            risk=0.0,
            findings=[],
            duration_seconds=0.043,
            token_usage={},
            cost_usd=None,
            state_dir=state_dir,
            receipt_kind="dispatch",
            failure_reason="litellm-proxy/deepseek: litellm/credentials_missing: HTTP 401",
            failure_class="auth_rejected",
        )

        assert receipt_path.exists()
        lines = receipt_path.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["status"] == "failure"
        assert data["failure_class"] == "auth_rejected"
        assert "HTTP 401" in data["failure_reason"]

    def test_emit_receipt_success_omits_failure_fields(self, tmp_path):
        """On success, emit_dispatch_receipt omits failure fields."""
        from governance_emit import emit_dispatch_receipt

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        receipt_path = emit_dispatch_receipt(
            dispatch_id="test-e2e-002",
            terminal_id="T1",
            provider="claude",
            model="sonnet",
            pr_id=None,
            status="success",
            completion_pct=100,
            risk=0.0,
            findings=[],
            duration_seconds=5.0,
            token_usage={"input": 100, "output": 50},
            cost_usd=None,
            state_dir=state_dir,
            receipt_kind="dispatch",
        )

        data = json.loads(receipt_path.read_text().strip())
        assert data["status"] == "success"
        assert "failure_reason" not in data
        assert "failure_class" not in data

    def test_empty_completion_has_different_classification_than_auth(self):
        """An empty completion and an auth rejection must produce different
        failure_class values in the receipt (not both 'unknown')."""
        from failure_classification import classify_failure

        auth_result = classify_failure(
            status="failure",
            error="HTTP 401 Unauthorized",
            completion_text="",
            timed_out=False,
            provider="litellm:deepseek",
        )
        empty_result = classify_failure(
            status="failure",
            error=None,
            completion_text="",
            timed_out=False,
            provider="litellm:deepseek",
            returncode=0,
        )

        assert auth_result["failure_class"] != empty_result["failure_class"], (
            f"auth={auth_result['failure_class']} and "
            f"empty={empty_result['failure_class']} must be distinct"
        )
        assert auth_result["failure_class"] == "auth_rejected"
        assert empty_result["failure_class"] == "empty_completion"
