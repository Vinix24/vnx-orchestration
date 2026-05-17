"""Tests for security fixes: PRAGMA allowlist, project_id_fn multi-tenant, behavior contracts, provider_registry context_window, _recording import."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/lib is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))


# ---------------------------------------------------------------------------
# BLOCKER 1: PRAGMA table allowlist
# ---------------------------------------------------------------------------

class TestTableHasColumnAllowlist:
    """_table_has_column must reject table names not in the allowlist."""

    def _get_fn(self):
        from intelligence_sources._common import _table_has_column
        return _table_has_column

    def _make_conn(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE success_patterns (id INTEGER, name TEXT, project_id TEXT)")
        return conn

    def test_allowed_table_found(self):
        fn = self._get_fn()
        conn = self._make_conn()
        assert fn(conn, "success_patterns", "name") is True

    def test_allowed_table_column_missing(self):
        fn = self._get_fn()
        conn = self._make_conn()
        assert fn(conn, "success_patterns", "nonexistent_col") is False

    def test_disallowed_table_raises_valueerror(self):
        fn = self._get_fn()
        conn = self._make_conn()
        with pytest.raises(ValueError, match="not in allowed set"):
            fn(conn, "robert'; DROP TABLE students;--", "id")

    def test_disallowed_table_injection_attempt(self):
        fn = self._get_fn()
        conn = self._make_conn()
        with pytest.raises(ValueError, match="not in allowed set"):
            fn(conn, "success_patterns); SELECT * FROM sqlite_master; --", "id")

    def test_all_known_tables_allowed(self):
        from intelligence_sources._common import _ALLOWED_TABLES
        expected = {
            "success_patterns", "antipatterns", "prevention_rules",
            "dispatch_metadata", "code_snippets", "intelligence_injections",
            "pattern_usage", "dispatch_pattern_offered",
        }
        assert _ALLOWED_TABLES == expected


# ---------------------------------------------------------------------------
# BLOCKER 2: project_id_fn in _query_candidates
# ---------------------------------------------------------------------------

class TestProjectIdFnInQueryCandidates:
    """_query_candidates must pass project_id_fn to all query functions."""

    @patch("intelligence_selector.query_proven_patterns", return_value=[])
    @patch("intelligence_selector.query_failure_prevention", return_value=[])
    @patch("intelligence_selector.query_recent_comparable", return_value=[])
    def test_project_id_fn_passed(self, mock_recent, mock_failure, mock_proven):
        from intelligence_selector import IntelligenceSelector
        selector = IntelligenceSelector.__new__(IntelligenceSelector)
        selector._has_column = MagicMock(return_value=False)
        selector._get_central_qi_conn = MagicMock(return_value=None)

        mock_db = MagicMock()
        selector._get_quality_db = MagicMock(return_value=mock_db)

        selector._query_candidates("coding", ["scope-a"])

        for mock_fn in (mock_proven, mock_failure, mock_recent):
            call_kwargs = mock_fn.call_args[1]
            assert "project_id_fn" in call_kwargs, (
                f"{mock_fn._mock_name} missing project_id_fn kwarg"
            )
            assert call_kwargs["project_id_fn"] is not None


# ---------------------------------------------------------------------------
# MEDIUM: behavior_contracts — new lanes
# ---------------------------------------------------------------------------

class TestBehaviorContractsNewLanes:
    """New provider lanes must exist and pass invariants."""

    def test_deepseek_v4_flash_contract(self):
        from providers.behavior_contracts import get_contract
        c = get_contract("litellm:deepseek:deepseek-v4-flash")
        assert c.provider == "litellm"
        assert c.sub_provider == "deepseek"
        assert c.supports_streaming is True
        assert c.tool_call_shape == "openai_tools"
        assert c.max_context_tokens == 1_000_000
        assert c.max_output_tokens == 384_000
        assert c.audit_shape == "canonical_event"

    def test_kimi_k2_6_openrouter_contract(self):
        from providers.behavior_contracts import get_contract
        c = get_contract("litellm:openrouter:kimi-k2-6")
        assert c.provider == "litellm"
        assert c.sub_provider == "openrouter"
        assert c.supports_streaming is True
        assert c.tool_call_shape == "openai_tools"
        assert c.max_context_tokens == 200_000
        assert c.max_output_tokens == 8192
        assert c.audit_shape == "canonical_event"

    def test_audit_shape_uniform_invariant(self):
        from providers.behavior_contracts import validate_audit_shape_uniform
        assert validate_audit_shape_uniform() is True

    def test_unknown_lane_raises_keyerror(self):
        from providers.behavior_contracts import get_contract
        with pytest.raises(KeyError, match="Unknown provider lane"):
            get_contract("litellm:fake:nonexistent")


# ---------------------------------------------------------------------------
# MEDIUM: provider_registry — context_window parsing
# ---------------------------------------------------------------------------

class TestProviderRegistryContextWindow:
    """ProviderModel must parse context_window from YAML."""

    def test_context_window_parsed(self):
        from providers.provider_registry import load
        registry = load()
        anthropic = registry.get("anthropic")
        assert anthropic is not None
        opus = anthropic.models.get("opus")
        assert opus is not None
        assert opus.context_window == 200_000

    def test_context_window_deepseek(self):
        from providers.provider_registry import load
        registry = load()
        ds = registry.get("deepseek")
        assert ds is not None
        v4_pro = ds.models.get("deepseek-v4-pro")
        assert v4_pro is not None
        assert v4_pro.context_window == 1_000_000

    def test_context_window_none_when_missing(self):
        from providers.provider_registry import _parse_model
        data = {
            "litellm_name": "test/model",
            "cost_input_per_mtok": 1.0,
            "cost_output_per_mtok": 2.0,
            "max_tokens": 8192,
            "supports_streaming": True,
            "supports_tool_calls": True,
        }
        model = _parse_model(data)
        assert model.context_window is None


# ---------------------------------------------------------------------------
# MEDIUM: _recording.py import fallback
# ---------------------------------------------------------------------------

class TestRecordingImportFallback:
    """_recording.py must be importable and have current_project_id."""

    def test_module_imports(self):
        from intelligence_sources import _recording
        assert hasattr(_recording, "current_project_id")
        assert callable(_recording.current_project_id)
