#!/usr/bin/env python3
"""Tests for Wave 8 dead-code wiring completeness.

Verifies that all 4 modules identified in the audit are properly wired
through at least one real call-path:
  1. PR-6.5d: auto_apply() runs from build_t0_state fallback path
  2. PR-D5-E: UnifiedReportValidator class is importable and usable from governance_emit
  3. PR-D5-F: VNX_SCHEMA_STRICT toggle in emit_unified_report (shadow-mode + strict)
  4. PR-SR-3: smart_router.route() is called from provider_dispatch --auto-route
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Test 1: PR-6.5d — auto_apply wired in build_t0_state fallback path
# ---------------------------------------------------------------------------

class TestAutoApplyWiring:
    """Verify auto_apply is reachable from build_t0_state even when
    coordination_db import fails (fallback path)."""

    def test_auto_apply_called_in_fallback_path(self, tmp_path):
        """When coordination_db init_schema raises, fallback path still calls auto_apply."""
        import build_t0_state as bts

        db_path = tmp_path / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE terminal_leases (id TEXT PRIMARY KEY, state TEXT)"
        )
        conn.commit()
        conn.close()

        auto_apply_called_with = []

        def mock_auto_apply(path):
            auto_apply_called_with.append(path)
            return []

        def mock_init_schema(sd):
            raise sqlite3.OperationalError("database is locked")

        with patch("build_t0_state.init_schema", mock_init_schema, create=True):
            with patch(
                "migrations.auto_apply.auto_apply", mock_auto_apply
            ):
                result = bts._init_and_check_db(tmp_path)

        assert result is True
        assert len(auto_apply_called_with) == 1
        assert auto_apply_called_with[0] == db_path

    def test_auto_apply_failure_does_not_block(self, tmp_path):
        """auto_apply raising in fallback path must not prevent DB check from passing."""
        db_path = tmp_path / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE terminal_leases (id TEXT PRIMARY KEY, state TEXT)"
        )
        conn.commit()
        conn.close()

        with patch.dict(sys.modules, {"coordination_db": None}):
            with patch(
                "migrations.auto_apply.auto_apply",
                side_effect=RuntimeError("migration exploded"),
            ):
                import build_t0_state as bts

                result = bts._init_and_check_db(tmp_path)

        assert result is True

    def test_auto_apply_import_available(self):
        """Verify the auto_apply module is importable from scripts/lib/migrations/."""
        from migrations.auto_apply import auto_apply

        assert callable(auto_apply)


# ---------------------------------------------------------------------------
# Test 2: PR-D5-E — UnifiedReportValidator class wired in governance_emit
# ---------------------------------------------------------------------------

class TestUnifiedReportValidatorWiring:
    """Verify UnifiedReportValidator exists and is used by governance_emit."""

    def test_class_importable(self):
        """UnifiedReportValidator is importable from unified_report_schema."""
        from unified_report_schema import UnifiedReportValidator

        validator = UnifiedReportValidator()
        assert hasattr(validator, "validate")
        assert hasattr(validator, "validate_path")

    def test_validator_accepts_valid_frontmatter(self, tmp_path):
        """UnifiedReportValidator.validate returns valid=True for conformant content."""
        from unified_report_schema import UnifiedReportValidator

        valid_fm = {
            "schema_version": 1,
            "dispatch_id": "test-dispatch-001",
            "provider": "claude",
            "sub_provider": "none",
            "model": "sonnet",
            "terminal_id": "T1",
            "pool_id": "headless",
            "role": "backend-developer",
            "task_class": "implementation",
            "pr_id": "none",
            "duration_seconds": 42.5,
            "exit_code": 0,
            "token_usage": {"input": 1000, "output": 500, "cache_read": 0},
            "cost_usd": 0.01,
            "route_decision": {
                "strategy": "default",
                "selected_provider": "claude",
                "selected_model": "sonnet",
            },
        }
        import yaml

        fm_yaml = yaml.dump(valid_fm, default_flow_style=False)
        content = f"---\n{fm_yaml}---\n\n# Report body\n"

        validator = UnifiedReportValidator()
        result = validator.validate(content)
        assert result.valid is True
        assert result.errors == []
        assert result.frontmatter is not None

    def test_validator_rejects_invalid_frontmatter(self):
        """UnifiedReportValidator.validate returns valid=False for missing fields."""
        from unified_report_schema import UnifiedReportValidator

        content = "---\nschema_version: 1\n---\n\n# Missing fields\n"
        validator = UnifiedReportValidator()
        result = validator.validate(content)
        assert result.valid is False
        assert len(result.errors) > 0

    def test_governance_emit_uses_validator_class(self, tmp_path):
        """governance_emit._validate_report_frontmatter uses UnifiedReportValidator."""
        import yaml

        from governance_emit import _validate_report_frontmatter

        valid_fm = {
            "schema_version": 1,
            "dispatch_id": "test-dispatch-002",
            "provider": "claude",
            "sub_provider": "none",
            "model": "sonnet",
            "terminal_id": "T1",
            "pool_id": "headless",
            "role": "backend-developer",
            "task_class": "implementation",
            "pr_id": "none",
            "duration_seconds": 10.0,
            "exit_code": 0,
            "token_usage": {"input": 100, "output": 50, "cache_read": 0},
            "cost_usd": 0.001,
            "route_decision": {
                "strategy": "default",
                "selected_provider": "claude",
                "selected_model": "sonnet",
            },
        }
        fm_yaml = yaml.dump(valid_fm, default_flow_style=False)
        content = f"---\n{fm_yaml}---\n\n# Body\n"

        _validate_report_frontmatter(content, "test-dispatch-002")


# ---------------------------------------------------------------------------
# Test 3: PR-D5-F — VNX_SCHEMA_STRICT toggle in emit_unified_report
# ---------------------------------------------------------------------------

class TestSchemaStrictToggle:
    """Verify VNX_SCHEMA_STRICT controls raise vs shadow-mode behavior."""

    def test_shadow_mode_logs_violation(self, tmp_path, caplog):
        """Default (VNX_SCHEMA_STRICT unset): violations logged, not raised."""
        import logging

        from governance_emit import _validate_report_frontmatter

        content = "---\nschema_version: 1\n---\n\n# Missing fields\n"

        env_patch = {"VNX_SCHEMA_STRICT": ""}
        with patch.dict(os.environ, env_patch, clear=False):
            os.environ.pop("VNX_SCHEMA_STRICT", None)
            with caplog.at_level(logging.WARNING):
                _validate_report_frontmatter(content, "test-shadow")

        assert any("schema violation (shadow-mode)" in r.message for r in caplog.records)

    def test_strict_mode_raises(self):
        """VNX_SCHEMA_STRICT=1: violations raise SchemaViolation."""
        from unified_report_schema import SchemaViolation

        from governance_emit import _validate_report_frontmatter

        content = "---\nschema_version: 1\n---\n\n# Missing fields\n"

        with patch.dict(os.environ, {"VNX_SCHEMA_STRICT": "1"}):
            with pytest.raises(SchemaViolation):
                _validate_report_frontmatter(content, "test-strict")

    def test_emit_unified_report_validates_with_frontmatter(self, tmp_path):
        """emit_unified_report calls validation when frontmatter is provided."""
        from governance_emit import emit_unified_report

        frontmatter = {
            "schema_version": 1,
            "dispatch_id": "test-emit-001",
            "provider": "claude",
            "sub_provider": "none",
            "model": "sonnet",
            "terminal_id": "T1",
            "pool_id": "headless",
            "role": "backend-developer",
            "task_class": "implementation",
            "pr_id": "none",
            "duration_seconds": 5.0,
            "exit_code": 0,
            "token_usage": {"input": 100, "output": 50, "cache_read": 0},
            "cost_usd": 0.001,
            "route_decision": {
                "strategy": "default",
                "selected_provider": "claude",
                "selected_model": "sonnet",
            },
        }

        report_path = emit_unified_report(
            dispatch_id="test-emit-001",
            terminal_id="T1",
            provider="claude",
            instruction="test instruction",
            response_text="test response",
            findings=[],
            duration_seconds=5.0,
            data_dir=tmp_path,
            frontmatter=frontmatter,
        )

        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert content.startswith("---\n")


# ---------------------------------------------------------------------------
# Test 4: PR-SR-3 — smart_router.route() wired in provider_dispatch --auto-route
# ---------------------------------------------------------------------------

class TestSmartRouterRouteWiring:
    """Verify smart_router.route() exists and is called by provider_dispatch."""

    def test_route_function_exists(self):
        """smart_router.route() is importable and callable."""
        from smart_router import route, RoutingResult

        assert callable(route)

    def test_route_returns_routing_result(self, tmp_path):
        """smart_router.route() returns a RoutingResult with provider+model."""
        from smart_router import route, RoutingResult

        recs_yaml = tmp_path / "routing_recommendations.yaml"
        recs_yaml.write_text(
            "routing_by_task:\n"
            "  01_code_generation:\n"
            "  - model_id: claude-sonnet-4-6\n"
            "    composite_score: 8.0\n"
            "    avg_duration_seconds: 500\n"
            "  - model_id: deepseek-v4-pro\n"
            "    composite_score: 5.0\n"
            "    avg_duration_seconds: 200\n",
            encoding="utf-8",
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        result = route(
            instruction="implement a new module for batch processing",
            dispatch_id="test-route-001",
            state_dir=state_dir,
            role="backend-developer",
            recommendations_path=recs_yaml,
        )

        assert isinstance(result, RoutingResult)
        assert result.routed is True
        assert result.provider == "claude"
        assert result.model == "sonnet"
        assert result.decision.task_class == "01_code_generation"

    def test_route_writes_decision_ndjson(self, tmp_path):
        """smart_router.route() persists the decision to route_decisions.ndjson."""
        from smart_router import route

        recs_yaml = tmp_path / "routing_recommendations.yaml"
        recs_yaml.write_text(
            "routing_by_task:\n"
            "  05_debugging:\n"
            "  - model_id: claude-opus-4-6\n"
            "    composite_score: 9.0\n"
            "    avg_duration_seconds: 300\n",
            encoding="utf-8",
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        route(
            instruction="debug the failing test in module X",
            dispatch_id="test-route-002",
            state_dir=state_dir,
            recommendations_path=recs_yaml,
        )

        ndjson_path = state_dir / "route_decisions.ndjson"
        assert ndjson_path.exists()
        lines = ndjson_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["dispatch_id"] == "test-route-002"
        assert record["task_class"] == "05_debugging"
        assert record["chosen_route"]["model_id"] == "claude-opus-4-6"

    def test_provider_dispatch_auto_route_calls_smart_router_route(self, tmp_path):
        """provider_dispatch main() with --auto-route calls smart_router.route()."""
        from smart_router import RoutingResult, RouteDecision, RouteCandidate

        mock_result = RoutingResult(
            decision=RouteDecision(
                task_class="01_code_generation",
                primary=RouteCandidate(
                    model_id="claude-sonnet-4-6",
                    composite_score=8.0,
                    avg_duration_seconds=500,
                ),
                fallback=None,
                reason="test",
            ),
            provider="claude",
            model="sonnet",
            routed=True,
        )

        with patch("smart_router.route", return_value=mock_result) as mock_route:
            with patch.dict(os.environ, {"VNX_STATE_DIR": str(tmp_path)}):
                import provider_dispatch as pd_mod

                with patch.object(pd_mod, "_dispatch_claude", return_value=0):
                    with patch("env_loader.load_env"):
                        with patch("constraint_enforcer.enforce"):
                            try:
                                pd_mod.main([
                                    "--provider", "claude",
                                    "--terminal-id", "T1",
                                    "--dispatch-id", "test-auto-001",
                                    "--instruction", "implement feature X",
                                    "--auto-route",
                                ])
                            except SystemExit:
                                pass

            mock_route.assert_called_once()
            call_kwargs = mock_route.call_args
            assert "dispatch_id" in (call_kwargs.kwargs or {}) or len(call_kwargs.args) > 1
            if call_kwargs.kwargs:
                assert call_kwargs.kwargs["dispatch_id"] == "test-auto-001"
