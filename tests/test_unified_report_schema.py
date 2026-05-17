"""Tests for unified_report_schema validator (PR-D5-E)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# scripts/lib is added to sys.path by tests/conftest.py
from unified_report_schema import (  # type: ignore  # noqa: E402
    SCHEMA_PATH,
    SchemaViolation,
    extract_frontmatter,
    load_schema,
    parse_frontmatter,
    validate_file,
    validate_frontmatter,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARDRAIL = REPO_ROOT / "scripts" / "guardrails" / "verify_report_schema.sh"


def _valid_frontmatter() -> dict:
    """Minimal-valid frontmatter mapping for the v1 schema."""
    return {
        "schema_version": 1,
        "dispatch_id": "20260517-pr-d5-e-test",
        "provider": "claude",
        "sub_provider": "anthropic",
        "model": "claude-opus-4-7",
        "terminal_id": "W4",
        "pool_id": "headless",
        "role": "backend-developer",
        "task_class": "implementation",
        "pr_id": "PR-D5-E",
        "duration_seconds": 12.5,
        "exit_code": 0,
        "token_usage": {"input": 1234, "output": 567, "cache_read": 89},
        "cost_usd": 0.0421,
        "route_decision": {
            "strategy": "default",
            "selected_provider": "claude",
            "selected_model": "claude-opus-4-7",
            "reason": "primary route",
        },
    }


def _render_report(frontmatter: dict, body: str = "Report body.") -> str:
    """Render a unified report markdown string with the given frontmatter."""
    import yaml

    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False).strip()
    return f"---\n{yaml_text}\n---\n\n{body}\n"


# ---------------------------------------------------------------------------
# Schema file
# ---------------------------------------------------------------------------


def test_schema_file_loads_and_is_draft_2020_12():
    schema = load_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["title"] == "Unified Report Frontmatter v1"
    assert schema["properties"]["schema_version"]["const"] == 1


def test_schema_path_resolves_to_repo_schemas_dir():
    assert SCHEMA_PATH.exists(), f"schema missing at {SCHEMA_PATH}"
    assert SCHEMA_PATH.name == "unified_report_v1.json"


# ---------------------------------------------------------------------------
# Frontmatter extraction
# ---------------------------------------------------------------------------


def test_extract_frontmatter_splits_correctly():
    text = "---\nkey: value\n---\n\nbody line\n"
    fm, body = extract_frontmatter(text)
    assert "key: value" in fm
    assert "body line" in body


def test_extract_frontmatter_missing_fence_raises():
    with pytest.raises(SchemaViolation, match="missing YAML frontmatter"):
        extract_frontmatter("# just a heading\nno frontmatter here\n")


def test_extract_frontmatter_unterminated_raises():
    with pytest.raises(SchemaViolation, match="unterminated YAML frontmatter"):
        extract_frontmatter("---\nkey: value\nno closing fence\n")


def test_extract_frontmatter_empty_text_raises():
    with pytest.raises(SchemaViolation, match="missing YAML frontmatter"):
        extract_frontmatter("")


def test_parse_frontmatter_non_mapping_raises():
    text = "---\n- just\n- a\n- list\n---\n"
    with pytest.raises(SchemaViolation, match="mapping"):
        parse_frontmatter(text)


def test_parse_frontmatter_invalid_yaml_raises():
    text = "---\nkey: [unterminated\n---\n"
    with pytest.raises(SchemaViolation, match="invalid YAML"):
        parse_frontmatter(text)


# ---------------------------------------------------------------------------
# validate_frontmatter — happy path
# ---------------------------------------------------------------------------


def test_validate_frontmatter_accepts_minimal_valid_payload():
    text = _render_report(_valid_frontmatter())
    data = validate_frontmatter(text)
    assert data["dispatch_id"] == "20260517-pr-d5-e-test"
    assert data["schema_version"] == 1


def test_validate_frontmatter_allows_unknown_top_level_keys():
    fm = _valid_frontmatter()
    fm["custom_extension_field"] = "future-use"
    text = _render_report(fm)
    data = validate_frontmatter(text)
    assert data["custom_extension_field"] == "future-use"


# ---------------------------------------------------------------------------
# validate_frontmatter — required-field violations
# ---------------------------------------------------------------------------


REQUIRED_FIELDS = [
    "schema_version",
    "dispatch_id",
    "provider",
    "sub_provider",
    "model",
    "terminal_id",
    "pool_id",
    "role",
    "task_class",
    "pr_id",
    "duration_seconds",
    "exit_code",
    "token_usage",
    "cost_usd",
    "route_decision",
]


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_required_field_reports_missing_field(field):
    fm = _valid_frontmatter()
    fm.pop(field)
    text = _render_report(fm)
    with pytest.raises(SchemaViolation) as excinfo:
        validate_frontmatter(text)
    assert "missing field" in str(excinfo.value)
    assert field in str(excinfo.value)


# ---------------------------------------------------------------------------
# validate_frontmatter — type violations
# ---------------------------------------------------------------------------


def test_invalid_schema_version_const_rejected():
    fm = _valid_frontmatter()
    fm["schema_version"] = 2
    text = _render_report(fm)
    with pytest.raises(SchemaViolation, match="schema_version"):
        validate_frontmatter(text)


def test_invalid_cost_usd_type_rejected():
    fm = _valid_frontmatter()
    fm["cost_usd"] = "not-a-number"
    text = _render_report(fm)
    with pytest.raises(SchemaViolation) as excinfo:
        validate_frontmatter(text)
    msg = str(excinfo.value)
    assert "invalid type" in msg
    assert "cost_usd" in msg


def test_negative_duration_rejected():
    fm = _valid_frontmatter()
    fm["duration_seconds"] = -1.0
    text = _render_report(fm)
    with pytest.raises(SchemaViolation, match="duration_seconds"):
        validate_frontmatter(text)


def test_non_integer_exit_code_rejected():
    fm = _valid_frontmatter()
    fm["exit_code"] = 0.5
    text = _render_report(fm)
    with pytest.raises(SchemaViolation, match="exit_code"):
        validate_frontmatter(text)


def test_token_usage_missing_subfield_rejected():
    fm = _valid_frontmatter()
    fm["token_usage"] = {"input": 1, "output": 2}  # cache_read missing
    text = _render_report(fm)
    with pytest.raises(SchemaViolation) as excinfo:
        validate_frontmatter(text)
    msg = str(excinfo.value)
    assert "missing field" in msg and "cache_read" in msg


def test_token_usage_wrong_subfield_type_rejected():
    fm = _valid_frontmatter()
    fm["token_usage"] = {"input": "lots", "output": 2, "cache_read": 0}
    text = _render_report(fm)
    with pytest.raises(SchemaViolation) as excinfo:
        validate_frontmatter(text)
    msg = str(excinfo.value)
    assert "invalid type" in msg
    assert "token_usage" in msg


def test_route_decision_missing_subfield_rejected():
    fm = _valid_frontmatter()
    fm["route_decision"] = {"strategy": "default", "selected_provider": "claude"}
    text = _render_report(fm)
    with pytest.raises(SchemaViolation) as excinfo:
        validate_frontmatter(text)
    msg = str(excinfo.value)
    assert "missing field" in msg
    assert "selected_model" in msg


def test_empty_provider_string_rejected():
    fm = _valid_frontmatter()
    fm["provider"] = ""
    text = _render_report(fm)
    with pytest.raises(SchemaViolation, match="provider"):
        validate_frontmatter(text)


# ---------------------------------------------------------------------------
# validate_file
# ---------------------------------------------------------------------------


def test_validate_file_accepts_valid_report(tmp_path: Path):
    report = tmp_path / "report.md"
    report.write_text(_render_report(_valid_frontmatter()), encoding="utf-8")
    data = validate_file(report)
    assert data["schema_version"] == 1


def test_validate_file_missing_path_raises(tmp_path: Path):
    missing = tmp_path / "does_not_exist.md"
    with pytest.raises(SchemaViolation, match="not found"):
        validate_file(missing)


# ---------------------------------------------------------------------------
# Bash guardrail wrapper — black-box integration
# ---------------------------------------------------------------------------


def _run_guardrail(report_path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("PYTHON_BIN", sys.executable)
    return subprocess.run(
        ["bash", str(GUARDRAIL), str(report_path)],
        capture_output=True,
        text=True,
        env=env,
    )


def test_guardrail_exits_zero_on_valid_report(tmp_path: Path):
    assert GUARDRAIL.exists(), f"guardrail missing: {GUARDRAIL}"
    report = tmp_path / "valid_report.md"
    report.write_text(_render_report(_valid_frontmatter()), encoding="utf-8")
    result = _run_guardrail(report)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_guardrail_exits_one_on_missing_field(tmp_path: Path):
    fm = _valid_frontmatter()
    fm.pop("provider")
    report = tmp_path / "missing_provider.md"
    report.write_text(_render_report(fm), encoding="utf-8")
    result = _run_guardrail(report)
    assert result.returncode == 1
    assert "missing field" in result.stderr
    assert "provider" in result.stderr


def test_guardrail_exits_one_on_type_violation(tmp_path: Path):
    fm = _valid_frontmatter()
    fm["cost_usd"] = "free"
    report = tmp_path / "bad_cost.md"
    report.write_text(_render_report(fm), encoding="utf-8")
    result = _run_guardrail(report)
    assert result.returncode == 1
    assert "invalid type" in result.stderr
    assert "cost_usd" in result.stderr


def test_guardrail_exits_one_when_report_missing(tmp_path: Path):
    result = _run_guardrail(tmp_path / "nope.md")
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_guardrail_usage_when_no_args():
    env = os.environ.copy()
    env.setdefault("PYTHON_BIN", sys.executable)
    result = subprocess.run(
        ["bash", str(GUARDRAIL)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()
