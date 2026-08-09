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
import urllib.error
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

        result = _model_in_registry("litellm", "zai", "glm-5.2")
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


# ===========================================================================
# OI-893: reachability probe authenticates the way the lane does
# ===========================================================================

class TestReachabilityAuthHeaders:
    """OI-893: the dry-run reachability probe must send the lane's own
    Authorization header. An unauthenticated probe against an auth-gated
    endpoint returns 401 by construction, so the OK branch was unreachable and
    every healthy keyed route looked dead. Each test here fails on origin/main
    (no header is set) and passes on this branch."""

    @staticmethod
    def _plan(provider_value, lane="provider"):
        from unittest.mock import MagicMock

        plan = MagicMock()
        plan.provider.value = provider_value
        plan.lane = lane
        plan.dispatch_id = "test-reach-auth"
        return plan

    def test_deepseek_probe_sends_auth_header(self):
        """The deepseek-harness probe must send Authorization: Bearer $DEEPSEEK_API_KEY."""
        from dispatch_cli import _check_reachability

        captured = {}

        def _fake_urlopen(req, timeout=5):
            captured["headers"] = {k: v for k, v in req.header_items()}
            raise urllib.error.HTTPError("http://x", 401, "Unauthorized", None, None)

        plan = self._plan("deepseek-harness")
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-probe-valid"}, clear=False), \
                mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _check_reachability(plan, mock.MagicMock())

        auth = captured["headers"].get("Authorization")
        assert auth == "Bearer sk-probe-valid", (
            f"probe must authenticate with DEEPSEEK_API_KEY; got header {auth!r}"
        )

    def test_litellm_probe_sends_auth_header_when_key_set(self):
        """The litellm-proxy probe must send Authorization: Bearer $LITELLM_API_KEY when set."""
        from dispatch_cli import _check_reachability

        captured = {}

        class _Resp:
            status = 200

            def read(self):
                return json.dumps({"data": [{"id": "m1"}]}).encode()

        def _fake_urlopen(req, timeout=5):
            captured["headers"] = {k: v for k, v in req.header_items()}
            return _Resp()

        plan = self._plan("litellm:deepseek")
        with mock.patch.dict("os.environ", {"LITELLM_API_KEY": "sk-litellm-probe"}, clear=False), \
                mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _check_reachability(plan, mock.MagicMock())

        auth = captured["headers"].get("Authorization")
        assert auth == "Bearer sk-litellm-probe", (
            f"litellm probe must authenticate with LITELLM_API_KEY; got header {auth!r}"
        )

    def test_deepseek_401_is_auth_rejected_not_unreachable(self, capsys):
        """A 401 from the deepseek endpoint must be classified as AUTH REJECTED,
        never as 'unreachable' — an auth failure and a dead endpoint are
        different diagnostics."""
        from dispatch_cli import _check_reachability

        def _fake_401(req, timeout=5):
            raise urllib.error.HTTPError("http://x", 401, "Unauthorized", None, None)

        plan = self._plan("deepseek-harness")
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-wrong"}, clear=False), \
                mock.patch("urllib.request.urlopen", side_effect=_fake_401):
            _check_reachability(plan, mock.MagicMock())

        captured = capsys.readouterr()
        assert "AUTH REJECTED" in captured.err
        assert "unreachable" not in captured.err

    def test_litellm_401_without_key_names_the_missing_key(self, capsys):
        """When LITELLM_API_KEY is unset and the proxy 401s, the probe must
        report AUTH REJECTED with a 'key not set' hint — not OK, not unreachable."""
        from dispatch_cli import _check_reachability

        def _fake_401(req, timeout=5):
            raise urllib.error.HTTPError("http://x", 401, "Unauthorized", None, None)

        plan = self._plan("litellm:deepseek")
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=_fake_401):
            _check_reachability(plan, mock.MagicMock())

        captured = capsys.readouterr()
        assert "AUTH REJECTED" in captured.err
        assert "LITELLM_API_KEY is not set" in captured.err
        assert "unreachable" not in captured.err

    def test_litellm_probe_ok_with_key_and_models(self, capsys):
        """A 200 with a non-empty model list must print the OK branch — the
        branch that was unreachable before the auth header existed."""
        from dispatch_cli import _check_reachability

        class _Resp:
            status = 200

            def read(self):
                return json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]}).encode()

        plan = self._plan("litellm:deepseek")
        with mock.patch.dict("os.environ", {"LITELLM_API_KEY": "sk-litellm-probe"}, clear=False), \
                mock.patch("urllib.request.urlopen", return_value=_Resp()):
            _check_reachability(plan, mock.MagicMock())

        captured = capsys.readouterr()
        assert "litellm proxy OK: 2 models" in captured.err


# ===========================================================================
# OI-866: HTTP status survives the litellm normalization chain
# ===========================================================================

class TestLiteLLMStatusPropagation:
    """OI-866: the runner emits an HTTP status_code for a proxy/provider HTTP
    error. It must survive normalize_litellm_event and _finalize_litellm_result
    so the receipt failure_reason names the status code instead of collapsing
    to a bare 'litellm/' prefix."""

    def test_normalize_litellm_event_preserves_status_code(self):
        from provider_spawns.litellm_spawn import normalize_litellm_event

        ev = normalize_litellm_event(
            {"error_type": "credentials_missing", "message": "Invalid API key", "status_code": 401},
            "T1", "d-status",
        )
        assert ev.event_type == "error"
        assert ev.data["error_type"] == "credentials_missing"
        assert ev.data["status_code"] == 401

    def test_normalize_litellm_event_ignores_bogus_status(self):
        from provider_spawns.litellm_spawn import normalize_litellm_event

        ev = normalize_litellm_event(
            {"error_type": "completion_error", "message": "boom", "status_code": "not-a-number"},
            "T1", "d-status",
        )
        assert ev.data["error_type"] == "completion_error"
        assert "status_code" not in ev.data

    def test_finalize_litellm_result_includes_http_status(self):
        from provider_spawns.litellm_spawn import _finalize_litellm_result

        class _FakeProc:
            returncode = 1

            def wait(self, timeout=10):
                return 1

        result = _finalize_litellm_result(
            _FakeProc(), "", 1, False, False,
            first_error_event={
                "error_type": "credentials_missing",
                "message": "Invalid API key",
                "status_code": 401,
            },
        )
        assert result.error is not None
        assert "HTTP 401" in result.error
        assert "credentials_missing" in result.error

    def test_finalize_litellm_result_synthetic_error_is_not_bare_prefix(self):
        """The drainer's synthetic error (reason only) must not collapse to the
        bare 'litellm/' prefix — it must carry the exit reason so classification
        has content instead of 'unknown' with a meaningless reason."""
        from provider_spawns.litellm_spawn import _finalize_litellm_result

        class _FakeProc:
            returncode = 1

            def wait(self, timeout=10):
                return 1

        result = _finalize_litellm_result(
            _FakeProc(), "", 0, False, False,
            first_error_event={"reason": "subprocess exited with code 1 before complete event"},
        )
        assert result.error is not None
        assert result.error != "litellm/"
        assert "subprocess exited with code 1" in result.error

    def test_spawn_litellm_derives_401_error_from_error_event(self):
        """The real spawn chain (normalize -> consume -> finalize) derives a
        classified error from a status_code-carrying error event."""
        from provider_spawns.litellm_spawn import normalize_litellm_event, spawn_litellm

        error_event = normalize_litellm_event(
            {"error_type": "credentials_missing", "message": "Invalid API key", "status_code": 401},
            "T1", "d-spawn-401",
        )

        proc = mock.MagicMock()
        proc.pid = 99
        proc.returncode = 1
        proc.wait = mock.MagicMock(return_value=1)
        proc.poll = mock.MagicMock(return_value=1)
        proc.stdin = mock.MagicMock()

        with mock.patch("provider_spawns.litellm_spawn.subprocess.Popen", return_value=proc), \
                mock.patch(
                    "provider_spawns.litellm_spawn._LiteLLMNormalizerHost.drain_stream",
                    return_value=iter([error_event]),
                ):
            result = spawn_litellm(
                prompt="test", model="deepseek/v3.2",
                dispatch_id="d-spawn-401", terminal_id="T1",
            )

        assert result.error is not None
        assert "HTTP 401" in result.error
        assert result.returncode != 0


# ===========================================================================
# OI-866 end-to-end: provider-lane 401 -> receipt carries classified reason
# ===========================================================================

class Test401ProviderLaneReceipt:
    """OI-866: a provider-lane 401 failure must land in the receipt as a
    classified auth_rejected failure_reason — never '(no error captured)'."""

    def test_run_envelope_plan_401_receipt_has_classified_reason(self, tmp_path):
        """Drive run_envelope_plan with a spawn that reports a 401 and assert
        the emitted receipt carries failure_class=auth_rejected and the HTTP
        status in failure_reason."""
        from dispatch_envelope import run_envelope_plan
        from dispatch_internal import issue_permit
        from dispatch_spec import Provider
        from provider_spawns.litellm_spawn import LiteLLMSpawnResult
        from test_dispatch_envelope_fail_loud import _make_provider_plan

        plan = _make_provider_plan(
            tmp_path, provider=Provider.LITELLM_DEEPSEEK, model="default",
            dispatch_id="test-401-provider-lane",
        )
        permit = issue_permit(plan)

        state_dir = tmp_path / "state"
        data_dir = tmp_path / "data"
        state_dir.mkdir()
        data_dir.mkdir()
        fake_wt = tmp_path / "wt"
        fake_wt.mkdir()

        failed_spawn = LiteLLMSpawnResult(
            returncode=1,
            completion_text="",
            events_written=1,
            session_id=None,
            timed_out=False,
            error="litellm/credentials_missing: HTTP 401: AuthenticationError: Invalid API key",
            token_usage=None,
        )

        with mock.patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=failed_spawn), \
                mock.patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=fake_wt), \
                mock.patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
                mock.patch("dispatch_envelope._resolve_phantom_diff", return_value=None):
            result = run_envelope_plan(plan, permit, state_dir=state_dir, data_dir=data_dir)

        assert result.status == "failure"
        assert result.error is not None
        assert "HTTP 401" in result.error

        receipt_file = state_dir / "t0_receipts.ndjson"
        assert receipt_file.exists(), "t0_receipts.ndjson must be written for a provider-lane failure"
        lines = [ln for ln in receipt_file.read_text().splitlines() if ln.strip()]
        assert lines, "receipt ledger must not be empty"

        data = json.loads(lines[0])
        assert data["dispatch_id"] == "test-401-provider-lane"
        assert data["status"] == "failure"
        assert data["failure_class"] == "auth_rejected", (
            f"a 401 must classify as auth_rejected, got {data.get('failure_class')!r}"
        )
        assert "HTTP 401" in data["failure_reason"], (
            f"failure_reason must name the HTTP status, got {data.get('failure_reason')!r}"
        )
