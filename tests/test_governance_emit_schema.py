"""test_governance_emit_schema.py — Tests for PR-D5-F schema validation in governance_emit.

Covers: frontmatter generation, shadow-mode validation, strict-mode enforcement,
and backward-compatible behavior (no frontmatter → no validation).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from governance_emit import emit_unified_report, _validate_report_frontmatter
from unified_report_schema import SchemaViolation, SCHEMA_VERSION


@pytest.fixture()
def tmp_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


def _valid_frontmatter():
    return {
        "schema_version": SCHEMA_VERSION,
        "dispatch_id": "test-d5f-001",
        "provider": "claude",
        "sub_provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "terminal_id": "T1",
        "pool_id": "headless",
        "role": "backend-developer",
        "task_class": "implementation",
        "pr_id": "PR-D5-F",
        "duration_seconds": 42.5,
        "exit_code": 0,
        "token_usage": {"input": 1000, "output": 500, "cache_read": 200},
        "cost_usd": 0.015,
        "route_decision": {
            "strategy": "default",
            "selected_provider": "claude",
            "selected_model": "claude-sonnet-4-6",
        },
    }


def _base_report_kwargs(data_dir):
    return dict(
        dispatch_id="test-d5f-001",
        terminal_id="T1",
        provider="claude",
        instruction="Do the thing",
        response_text="Done.",
        findings=[],
        duration_seconds=42.5,
        data_dir=data_dir,
    )


# --- Frontmatter generation ---


class TestFrontmatterGeneration:
    def test_report_with_frontmatter_has_yaml_block(self, tmp_data):
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["frontmatter"] = _valid_frontmatter()
        path = emit_unified_report(**kwargs)
        content = path.read_text()
        assert content.startswith("---\n")
        assert "\n---\n" in content

    def test_report_without_frontmatter_has_no_yaml_block(self, tmp_data):
        path = emit_unified_report(**_base_report_kwargs(tmp_data))
        content = path.read_text()
        assert not content.startswith("---")

    def test_frontmatter_fields_roundtrip(self, tmp_data):
        kwargs = _base_report_kwargs(tmp_data)
        fm = _valid_frontmatter()
        kwargs["frontmatter"] = fm
        path = emit_unified_report(**kwargs)
        content = path.read_text()
        lines = content.split("\n")
        assert lines[0] == "---"
        end_idx = None
        for i in range(1, len(lines)):
            if lines[i] == "---":
                end_idx = i
                break
        assert end_idx is not None
        parsed = yaml.safe_load("\n".join(lines[1:end_idx]))
        assert parsed["schema_version"] == SCHEMA_VERSION
        assert parsed["dispatch_id"] == "test-d5f-001"
        assert parsed["provider"] == "claude"
        assert parsed["token_usage"]["input"] == 1000

    def test_body_preserved_after_frontmatter(self, tmp_data):
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["frontmatter"] = _valid_frontmatter()
        path = emit_unified_report(**kwargs)
        content = path.read_text()
        assert "# Dispatch test-d5f-001" in content
        assert "## Instruction" in content
        assert "## Response" in content


# --- Shadow-mode validation ---


class TestShadowMode:
    def test_invalid_frontmatter_does_not_raise(self, tmp_data, caplog):
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["dispatch_id"] = "test-shadow-001"
        kwargs["frontmatter"] = {"schema_version": 1}
        with caplog.at_level(logging.WARNING):
            path = emit_unified_report(**kwargs)
        assert path.exists()
        assert "schema violation (shadow-mode)" in caplog.text

    def test_valid_frontmatter_no_warning(self, tmp_data, caplog):
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["frontmatter"] = _valid_frontmatter()
        with caplog.at_level(logging.WARNING):
            emit_unified_report(**kwargs)
        assert "schema violation" not in caplog.text

    def test_missing_required_field_logged(self, tmp_data, caplog):
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["dispatch_id"] = "test-shadow-missing"
        fm = _valid_frontmatter()
        del fm["provider"]
        kwargs["frontmatter"] = fm
        with caplog.at_level(logging.WARNING):
            path = emit_unified_report(**kwargs)
        assert path.exists()
        assert "schema violation (shadow-mode)" in caplog.text


# --- Strict mode ---


class TestStrictMode:
    def test_raises_on_invalid(self, tmp_data, monkeypatch):
        monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["dispatch_id"] = "test-strict-001"
        kwargs["frontmatter"] = {"schema_version": 1}
        with pytest.raises(SchemaViolation):
            emit_unified_report(**kwargs)

    def test_passes_on_valid(self, tmp_data, monkeypatch):
        monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["frontmatter"] = _valid_frontmatter()
        path = emit_unified_report(**kwargs)
        assert path.exists()


# --- _validate_report_frontmatter standalone ---


class TestValidateReportFrontmatter:
    def test_valid_content(self):
        fm = _valid_frontmatter()
        fm_yaml = yaml.dump(fm, default_flow_style=False, sort_keys=False)
        content = f"---\n{fm_yaml}---\n\n# Body"
        _validate_report_frontmatter(content, "test-001")

    def test_invalid_shadow_logs(self, caplog):
        content = "---\nschema_version: 1\n---\n\n# Body"
        with caplog.at_level(logging.WARNING):
            _validate_report_frontmatter(content, "test-002")
        assert "schema violation (shadow-mode)" in caplog.text

    def test_strict_raises(self, monkeypatch):
        monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")
        content = "---\nschema_version: 1\n---\n\n# Body"
        with pytest.raises(SchemaViolation):
            _validate_report_frontmatter(content, "test-003")


# --- Idempotency preserved ---


class TestIdempotency:
    def test_idempotent_with_frontmatter(self, tmp_data):
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["frontmatter"] = _valid_frontmatter()
        path1 = emit_unified_report(**kwargs)
        mtime1 = path1.stat().st_mtime
        kwargs["response_text"] = "Different"
        path2 = emit_unified_report(**kwargs)
        assert path1 == path2
        assert path2.stat().st_mtime == mtime1

    def test_idempotent_without_frontmatter(self, tmp_data):
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["dispatch_id"] = "test-idem-no-fm"
        path1 = emit_unified_report(**kwargs)
        path2 = emit_unified_report(**kwargs)
        assert path1 == path2


# --- Integration with unified_report_schema validator ---


class TestSchemaIntegration:
    def test_all_required_fields_present_validates(self, tmp_data, monkeypatch):
        monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["dispatch_id"] = "test-integration-001"
        fm = _valid_frontmatter()
        fm["dispatch_id"] = "test-integration-001"
        kwargs["frontmatter"] = fm
        path = emit_unified_report(**kwargs)
        assert path.exists()
        from unified_report_schema import validate_file
        validate_file(path)

    def test_wrong_schema_version_fails_strict(self, tmp_data, monkeypatch):
        monkeypatch.setenv("VNX_SCHEMA_STRICT", "1")
        kwargs = _base_report_kwargs(tmp_data)
        kwargs["dispatch_id"] = "test-wrong-version"
        fm = _valid_frontmatter()
        fm["schema_version"] = 999
        fm["dispatch_id"] = "test-wrong-version"
        kwargs["frontmatter"] = fm
        with pytest.raises(SchemaViolation):
            emit_unified_report(**kwargs)
