#!/usr/bin/env python3
"""PR-SR-2 — ConstraintEnforcer tests.

Covers every constraint in provider_constraints.yaml:
  - kimi-via-cli-only       (forbid_route, blocking)
  - t0-opus-only            (require_route, warn, override)
  - workers-sonnet-pinned   (require_route, warn, override)
  - no-anthropic-sdk        (forbid_import — skipped at runtime)
  - zai-via-openrouter-only (forbid_route, blocking)
  - deprecated-glm-models   (forbid_route, blocking)
  - deepseek-path-d-blocked (forbid_route, blocking)

Plus: file-not-found, bad version, override env var, non-matching routes.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from constraint_enforcer import ConstraintEnforcer, HardConstraintViolation


@pytest.fixture
def real_enforcer() -> ConstraintEnforcer:
    """Enforcer loaded from the real provider_constraints.yaml."""
    return ConstraintEnforcer()


@pytest.fixture
def tmp_constraints(tmp_path: Path):
    """Factory: write a minimal constraints YAML and return a ConstraintEnforcer."""

    def _make(yaml_text: str) -> ConstraintEnforcer:
        p = tmp_path / "constraints.yaml"
        p.write_text(textwrap.dedent(yaml_text))
        return ConstraintEnforcer(path=p)

    return _make


# ---------------------------------------------------------------------------
# kimi-via-cli-only — forbid_route provider=moonshot via=api
# ---------------------------------------------------------------------------

class TestKimiViaCliOnly:

    def test_moonshot_via_api_blocked(self, real_enforcer: ConstraintEnforcer):
        with pytest.raises(HardConstraintViolation, match="kimi-via-cli-only"):
            real_enforcer.enforce(provider="litellm", sub_provider="moonshot", via="api")

    def test_moonshot_via_cli_allowed(self, real_enforcer: ConstraintEnforcer):
        real_enforcer.enforce(provider="litellm", sub_provider="moonshot", via="cli")

    def test_moonshot_no_via_allowed(self, real_enforcer: ConstraintEnforcer):
        real_enforcer.enforce(provider="litellm", sub_provider="moonshot")

    def test_non_moonshot_via_api_allowed(self, real_enforcer: ConstraintEnforcer):
        real_enforcer.enforce(provider="deepseek", via="api")


# ---------------------------------------------------------------------------
# t0-opus-only — require_route role=T0 model=claude-opus-4-7
# ---------------------------------------------------------------------------

class TestT0OpusOnly:

    def test_t0_with_sonnet_warns(self, real_enforcer: ConstraintEnforcer, caplog):
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T0", model="claude-sonnet-4-6")
        assert "t0-opus-only" in caplog.text

    def test_t0_with_opus_allowed(self, real_enforcer: ConstraintEnforcer, caplog):
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T0", model="claude-opus-4-7")
        assert "t0-opus-only" not in caplog.text

    def test_t0_override_env(self, real_enforcer: ConstraintEnforcer, caplog, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_T0_OPUS_ONLY", "1")
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T0", model="claude-sonnet-4-6")
        assert "overridden" in caplog.text

    def test_non_t0_any_model_allowed(self, real_enforcer: ConstraintEnforcer, caplog):
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T1", model="claude-sonnet-4-6")
        assert "t0-opus-only" not in caplog.text


# ---------------------------------------------------------------------------
# workers-sonnet-pinned — require_route role=[T1,T2,T3] model=claude-sonnet-4-6
# ---------------------------------------------------------------------------

class TestWorkersSonnetPinned:

    def test_t1_with_opus_warns(self, real_enforcer: ConstraintEnforcer, caplog):
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T1", model="claude-opus-4-7")
        assert "workers-sonnet-pinned" in caplog.text

    def test_t2_with_sonnet_allowed(self, real_enforcer: ConstraintEnforcer, caplog):
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T2", model="claude-sonnet-4-6")
        assert "workers-sonnet-pinned" not in caplog.text

    def test_t3_with_haiku_warns(self, real_enforcer: ConstraintEnforcer, caplog):
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T3", model="claude-haiku-4-5")
        assert "workers-sonnet-pinned" in caplog.text

    def test_t1_override_env(self, real_enforcer: ConstraintEnforcer, caplog, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_WORKERS_SONNET_PINNED", "1")
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T1", model="claude-opus-4-7")
        assert "overridden" in caplog.text


# ---------------------------------------------------------------------------
# no-anthropic-sdk — forbid_import (skipped at runtime, CI-only)
# ---------------------------------------------------------------------------

class TestNoAnthropicSdk:

    def test_forbid_import_skipped_at_runtime(self, real_enforcer: ConstraintEnforcer):
        real_enforcer.enforce(provider="claude", model="claude-opus-4-7")


# ---------------------------------------------------------------------------
# zai-via-openrouter-only — forbid_route provider=zai via=direct
# ---------------------------------------------------------------------------

class TestZaiViaOpenrouterOnly:

    def test_zai_direct_blocked(self, real_enforcer: ConstraintEnforcer):
        with pytest.raises(HardConstraintViolation, match="zai-via-openrouter-only"):
            real_enforcer.enforce(provider="zai", via="direct")

    def test_zai_via_openrouter_allowed(self, real_enforcer: ConstraintEnforcer):
        real_enforcer.enforce(provider="litellm", sub_provider="zai", via="openrouter")


# ---------------------------------------------------------------------------
# deprecated-glm-models — forbid_route provider=zai model=[glm-4.5, glm-4.6]
# ---------------------------------------------------------------------------

class TestDeprecatedGlmModels:

    def test_glm45_blocked(self, real_enforcer: ConstraintEnforcer):
        with pytest.raises(HardConstraintViolation, match="deprecated-glm-models"):
            real_enforcer.enforce(provider="zai", model="glm-4.5")

    def test_glm46_blocked(self, real_enforcer: ConstraintEnforcer):
        with pytest.raises(HardConstraintViolation, match="deprecated-glm-models"):
            real_enforcer.enforce(provider="zai", model="glm-4.6")

    def test_glm51_allowed(self, real_enforcer: ConstraintEnforcer):
        real_enforcer.enforce(provider="zai", model="glm-5.1")


# ---------------------------------------------------------------------------
# deepseek-path-d-blocked — forbid_route provider=deepseek via=claude_harness
# ---------------------------------------------------------------------------

class TestDeepseekPathDBlocked:

    def test_deepseek_claude_harness_blocked(self, real_enforcer: ConstraintEnforcer):
        with pytest.raises(HardConstraintViolation, match="deepseek-path-d-blocked"):
            real_enforcer.enforce(provider="deepseek", via="claude_harness")

    def test_deepseek_via_litellm_allowed(self, real_enforcer: ConstraintEnforcer):
        real_enforcer.enforce(provider="litellm", sub_provider="deepseek", via="litellm")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            ConstraintEnforcer(path=tmp_path / "nonexistent.yaml")

    def test_bad_version(self, tmp_constraints):
        with pytest.raises(ValueError, match="Unsupported constraints version"):
            tmp_constraints("""\
                version: 99
                constraints: []
            """)

    def test_empty_enforce_no_crash(self, real_enforcer: ConstraintEnforcer):
        real_enforcer.enforce()

    def test_all_none_no_crash(self, real_enforcer: ConstraintEnforcer):
        real_enforcer.enforce(
            provider=None, sub_provider=None, model=None,
            terminal_id=None, role=None, via=None,
        )

    def test_override_not_allowed_still_raises(self, real_enforcer: ConstraintEnforcer, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_KIMI_VIA_CLI_ONLY", "1")
        with pytest.raises(HardConstraintViolation, match="kimi-via-cli-only"):
            real_enforcer.enforce(provider="litellm", sub_provider="moonshot", via="api")

    def test_custom_constraint_file(self, tmp_constraints):
        enforcer = tmp_constraints("""\
            version: 1
            constraints:
              - id: test-block
                rule: forbid_route
                forbidden_route:
                  provider: acme
                reason: test
                enforcement: code_raise
                audit_severity: blocking
                override_allowed: false
        """)
        with pytest.raises(HardConstraintViolation, match="test-block"):
            enforcer.enforce(provider="acme")

    def test_case_insensitive_match(self, real_enforcer: ConstraintEnforcer):
        with pytest.raises(HardConstraintViolation, match="deprecated-glm-models"):
            real_enforcer.enforce(provider="ZAI", model="GLM-4.5")


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

class TestRequireRouteModelNoneStrict:
    """require_route with model=None must trigger violation, not silently skip."""

    def test_t0_model_none_warns(self, real_enforcer: ConstraintEnforcer, caplog):
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T0", model=None)
        assert "t0-opus-only" in caplog.text

    def test_worker_model_none_warns(self, real_enforcer: ConstraintEnforcer, caplog):
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T1", model=None)
        assert "workers-sonnet-pinned" in caplog.text

    def test_non_matching_role_model_none_ok(self, real_enforcer: ConstraintEnforcer, caplog):
        with caplog.at_level("WARNING"):
            real_enforcer.enforce(terminal_id="T9", model=None)
        assert "t0-opus-only" not in caplog.text
        assert "workers-sonnet-pinned" not in caplog.text


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

class TestModuleLevelEnforce:

    def test_module_enforce_raises(self):
        from constraint_enforcer import enforce
        with pytest.raises(HardConstraintViolation, match="kimi-via-cli-only"):
            enforce(provider="litellm", sub_provider="moonshot", via="api")

    def test_module_enforce_allows(self):
        from constraint_enforcer import enforce
        enforce(provider="claude", model="claude-opus-4-7", terminal_id="T0")
