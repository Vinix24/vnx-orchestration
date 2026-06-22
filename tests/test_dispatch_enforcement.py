#!/usr/bin/env python3
"""D1 (headless-block-core) enforcement tests.

Covers:
  D1.1 — claude-headless constraint: forbid_route, blocking, override_allowed.
  D1.2 — via='headless' when allow_headless=True; constraint fires blocking
          without flag; downgraded to warn with VNX_OVERRIDE_CLAUDE_HEADLESS=1.
  SAFETY REGRESSION — 5 existing blocking constraints (override_allowed: false)
          stay unconditionally blocking even when their VNX_OVERRIDE_* flag is set.
  D1.3 — _execute_claude_headless raises PermissionError without flag;
          dispatch_bridge guard returns 1 without flag.
"""

from __future__ import annotations

import hashlib
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from providers.constraint_enforcer import (  # noqa: E402
    ConstraintEnforcer,
    HardConstraintViolation,
)
from dispatch_cli import build_runtime_snapshot, _via_for_provider  # noqa: E402
from dispatch_spec import DispatchSpec, ValidatedSpec, Provider  # noqa: E402


@pytest.fixture
def real_enforcer() -> ConstraintEnforcer:
    return ConstraintEnforcer()


# ---------------------------------------------------------------------------
# D1.1 — claude-headless in YAML
# ---------------------------------------------------------------------------

class TestD11ConstraintLoads:

    def test_claude_headless_exists(self, real_enforcer: ConstraintEnforcer) -> None:
        headless = next(
            (c for c in real_enforcer._constraints if c.get("id") == "claude-headless"),
            None,
        )
        assert headless is not None, "claude-headless constraint missing from YAML"

    def test_is_forbid_route_blocking_override_allowed(self, real_enforcer: ConstraintEnforcer) -> None:
        headless = next(
            c for c in real_enforcer._constraints if c.get("id") == "claude-headless"
        )
        assert headless["rule"] == "forbid_route"
        assert headless["audit_severity"] == "blocking"
        assert headless["override_allowed"] is True

    def test_forbidden_route_spec(self, real_enforcer: ConstraintEnforcer) -> None:
        headless = next(
            c for c in real_enforcer._constraints if c.get("id") == "claude-headless"
        )
        forbidden = headless["forbidden_route"]
        assert forbidden["provider"] == "claude"
        assert forbidden["via"] == "headless"


# ---------------------------------------------------------------------------
# D1.2 — via computation + constraint firing / override
# ---------------------------------------------------------------------------

class TestD12ViaAndConstraint:

    def test_via_for_claude_is_cli(self) -> None:
        assert _via_for_provider("claude", None) == "cli"

    def test_headless_via_fires_blocking_without_flag(self, real_enforcer: ConstraintEnforcer) -> None:
        violations = real_enforcer.check_constraints(
            provider="claude", via="headless", env={}
        )
        codes = [v.code for v in violations]
        assert "claude-headless" in codes, f"claude-headless not in violations: {codes}"
        v = next(v for v in violations if v.code == "claude-headless")
        assert v.severity == "blocking"
        assert not v.override_applied

    def test_headless_via_downgraded_to_warn_with_flag(self, real_enforcer: ConstraintEnforcer) -> None:
        env = {"VNX_OVERRIDE_CLAUDE_HEADLESS": "1"}
        violations = real_enforcer.check_constraints(
            provider="claude", via="headless", env=env
        )
        v = next((v for v in violations if v.code == "claude-headless"), None)
        assert v is not None, "claude-headless violation missing when via=headless"
        assert v.severity == "warn"
        assert v.override_applied

    def test_normal_claude_lane_no_headless_violation(self, real_enforcer: ConstraintEnforcer) -> None:
        violations = real_enforcer.check_constraints(
            provider="claude", via="cli", env={}
        )
        codes = [v.code for v in violations]
        assert "claude-headless" not in codes

    def test_build_snapshot_via_headless_when_allow_headless_true(self, tmp_path: Path) -> None:
        captured: dict = {}

        def fake_check(*, via=None, **kwargs):
            captured["via"] = via
            return []

        with ExitStack() as stack:
            stack.enter_context(
                patch("providers.constraint_enforcer.check_constraints", side_effect=fake_check)
            )
            stack.enter_context(
                patch("staging_validator._exists_in_dir", return_value=False)
            )

            spec = DispatchSpec(
                schema_version=1,
                project_id="test-project",
                dispatch_id="test-d12-headless-snap",
                staging_id="test-d12-headless-snap",
                instruction_file=tmp_path / "instruction.md",
                role="T1",
                target_slot="T1",
                gate="",
                dispatch_paths=(),
                provider=Provider.CLAUDE,
                allow_headless=True,
                headless_reason="testing headless via",
            )
            inst_text = "test headless instruction"
            vspec = ValidatedSpec(
                spec=spec,
                instruction_text=inst_text,
                normalized_paths=(),
                instruction_sha256=hashlib.sha256(inst_text.encode()).hexdigest(),
            )

            build_runtime_snapshot(vspec, data_dir=tmp_path, spec_file=tmp_path / "spec.json")

        assert captured.get("via") == "headless", (
            f"expected via='headless', got {captured.get('via')!r}"
        )

    def test_build_snapshot_via_cli_when_allow_headless_false(self, tmp_path: Path) -> None:
        captured: dict = {}

        def fake_check(*, via=None, **kwargs):
            captured["via"] = via
            return []

        with ExitStack() as stack:
            stack.enter_context(
                patch("providers.constraint_enforcer.check_constraints", side_effect=fake_check)
            )
            stack.enter_context(
                patch("staging_validator._exists_in_dir", return_value=False)
            )

            spec = DispatchSpec(
                schema_version=1,
                project_id="test-project",
                dispatch_id="test-d12-normal-snap",
                staging_id="test-d12-normal-snap",
                instruction_file=tmp_path / "instruction.md",
                role="T1",
                target_slot="T1",
                gate="",
                dispatch_paths=(),
                provider=Provider.CLAUDE,
                allow_headless=False,
            )
            inst_text = "test normal instruction"
            vspec = ValidatedSpec(
                spec=spec,
                instruction_text=inst_text,
                normalized_paths=(),
                instruction_sha256=hashlib.sha256(inst_text.encode()).hexdigest(),
            )

            build_runtime_snapshot(vspec, data_dir=tmp_path, spec_file=tmp_path / "spec.json")

        assert captured.get("via") == "cli", (
            f"expected via='cli', got {captured.get('via')!r}"
        )


# ---------------------------------------------------------------------------
# SAFETY REGRESSION — 5 existing blocking constraints stay blocking
# ---------------------------------------------------------------------------

class TestSafetyRegression:
    """Constraints with override_allowed: false must NOT be downgraded even
    when VNX_OVERRIDE_<CODE>=1 is set in the environment."""

    def test_kimi_via_cli_only_stays_blocking_with_flag(self, real_enforcer: ConstraintEnforcer) -> None:
        env = {"VNX_OVERRIDE_KIMI_VIA_CLI_ONLY": "1"}
        violations = real_enforcer.check_constraints(
            provider="litellm", sub_provider="moonshot", via="api", env=env
        )
        v = next((v for v in violations if v.code == "kimi-via-cli-only"), None)
        assert v is not None
        assert v.severity == "blocking"
        assert not v.override_applied

    def test_zai_via_openrouter_only_stays_blocking_with_flag(self, real_enforcer: ConstraintEnforcer) -> None:
        env = {"VNX_OVERRIDE_ZAI_VIA_OPENROUTER_ONLY": "1"}
        violations = real_enforcer.check_constraints(
            provider="zai", via="direct", env=env
        )
        v = next((v for v in violations if v.code == "zai-via-openrouter-only"), None)
        assert v is not None
        assert v.severity == "blocking"
        assert not v.override_applied

    def test_deprecated_glm_models_stays_blocking_with_flag(self, real_enforcer: ConstraintEnforcer) -> None:
        env = {"VNX_OVERRIDE_DEPRECATED_GLM_MODELS": "1"}
        violations = real_enforcer.check_constraints(
            provider="zai", model="glm-4.5", env=env
        )
        v = next((v for v in violations if v.code == "deprecated-glm-models"), None)
        assert v is not None
        assert v.severity == "blocking"
        assert not v.override_applied

    def test_deepseek_harness_blocked_stays_blocking_with_flag(self, real_enforcer: ConstraintEnforcer) -> None:
        env = {"VNX_OVERRIDE_DEEPSEEK_HARNESS_SUBSCRIPTION_BLOCKED": "1"}
        violations = real_enforcer.check_constraints(
            provider="deepseek", via="claude_harness_subscription", env=env
        )
        v = next((v for v in violations if v.code == "deepseek-harness-subscription-blocked"), None)
        assert v is not None
        assert v.severity == "blocking"
        assert not v.override_applied

    def test_no_anthropic_sdk_override_applied_is_false(self, real_enforcer: ConstraintEnforcer) -> None:
        env = {"VNX_OVERRIDE_NO_ANTHROPIC_SDK": "1"}
        violations = real_enforcer.check_constraints(
            instruction_text="import anthropic\nfrom anthropic import Client",
            env=env,
        )
        v = next((v for v in violations if v.code == "no-anthropic-sdk"), None)
        assert v is not None
        assert not v.override_applied


# ---------------------------------------------------------------------------
# D1.3 — executor backstop + bridge guard
# ---------------------------------------------------------------------------

class TestD13Backstops:

    def test_execute_claude_headless_raises_without_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VNX_OVERRIDE_CLAUDE_HEADLESS", raising=False)
        from dispatch_cli import _execute_claude_headless  # noqa: PLC0415

        with pytest.raises(PermissionError, match="VNX_OVERRIDE_CLAUDE_HEADLESS"):
            _execute_claude_headless(
                None, None, state_dir=Path("/tmp"), data_dir=Path("/tmp")
            )

    def test_execute_claude_headless_proceeds_with_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_OVERRIDE_CLAUDE_HEADLESS", "1")
        from dispatch_cli import _execute_claude_headless  # noqa: PLC0415

        mock_result = type("R", (), {"returncode": 0})()
        with patch("dispatch_cli.run_envelope_headless_plan", return_value=mock_result):
            rc = _execute_claude_headless(
                None, None, state_dir=Path("/tmp"), data_dir=Path("/tmp")
            )
        assert rc == 0

    def test_bridge_dispatch_blocks_allow_headless_without_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VNX_OVERRIDE_CLAUDE_HEADLESS", raising=False)
        from dispatch_bridge import bridge_dispatch  # noqa: PLC0415

        rc = bridge_dispatch(
            instruction_text="test instruction",
            dispatch_id="test-bridge-block-d13",
            role="T1",
            target_slot="T1",
            allow_headless=True,
        )
        assert rc == 1
