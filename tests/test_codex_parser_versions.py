#!/usr/bin/env python3
"""Fixture-based regression tests for codex stream parser across CLI versions.

Each test class covers one scenario captured in tests/fixtures/codex_streams/.
Guards against silent regressions when codex CLI output shape changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures" / "codex_streams"
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from codex_parser import parse_codex_findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture(name: str) -> str:
    """Return raw NDJSON content of a fixture file as a string."""
    path = FIXTURES_DIR / name
    assert path.exists(), f"Fixture missing: {path}"
    return path.read_text(encoding="utf-8")


def _blocking_findings(result: dict) -> list:
    """Return findings with severity 'critical' or 'high'."""
    return [
        f for f in result["findings"]
        if f.get("severity", "").lower() in {"critical", "high"}
    ]


# ---------------------------------------------------------------------------
# v0.118 success
# ---------------------------------------------------------------------------

class TestV0118Success:
    """v0.118 stream: gate passed, empty findings."""

    def setup_method(self):
        self.stdout = _load_fixture("v0.118-success.json")
        self.result = parse_codex_findings(self.stdout)

    def test_result_has_required_keys(self):
        assert "findings" in self.result
        assert "residual_risk" in self.result
        assert "verdict" in self.result
        assert "raw_text" in self.result

    def test_verdict_is_pass(self):
        assert self.result["verdict"].get("verdict") == "pass"

    def test_no_findings(self):
        assert self.result["findings"] == []

    def test_no_blocking_findings(self):
        assert _blocking_findings(self.result) == []

    def test_residual_risk_extracted(self):
        assert self.result["residual_risk"] == "monitoring not yet proven in production"

    def test_raw_text_contains_review_content(self):
        assert "Reviewing the PR changes" in self.result["raw_text"]

    def test_raw_text_is_nonempty(self):
        assert len(self.result["raw_text"]) > 0


# ---------------------------------------------------------------------------
# v0.118 failure
# ---------------------------------------------------------------------------

class TestV0118Failure:
    """v0.118 stream: gate failed, structured findings."""

    def setup_method(self):
        self.stdout = _load_fixture("v0.118-failure.json")
        self.result = parse_codex_findings(self.stdout)

    def test_result_has_required_keys(self):
        assert "findings" in self.result
        assert "residual_risk" in self.result
        assert "verdict" in self.result
        assert "raw_text" in self.result

    def test_verdict_is_fail(self):
        assert self.result["verdict"].get("verdict") == "fail"

    def test_findings_nonempty(self):
        assert len(self.result["findings"]) > 0

    def test_blocking_findings_present(self):
        assert len(_blocking_findings(self.result)) > 0

    def test_critical_finding_extracted(self):
        messages = [f["message"] for f in self.result["findings"]]
        assert any("lease release" in m for m in messages)

    def test_high_finding_extracted(self):
        messages = [f["message"] for f in self.result["findings"]]
        assert any("race condition" in m for m in messages)

    def test_findings_have_severity_and_message(self):
        for f in self.result["findings"]:
            assert "severity" in f, f"finding missing severity: {f}"
            assert "message" in f, f"finding missing message: {f}"
            assert isinstance(f["severity"], str)
            assert isinstance(f["message"], str)

    def test_residual_risk_extracted(self):
        assert "concurrent writes" in self.result["residual_risk"]

    def test_raw_text_contains_review_content(self):
        assert "Reviewing the PR changes" in self.result["raw_text"]


# ---------------------------------------------------------------------------
# v0.118 rate-limit
# ---------------------------------------------------------------------------

class TestV0118RateLimit:
    """v0.118 stream: rate-limit error event, no verdict JSON."""

    def setup_method(self):
        self.stdout = _load_fixture("v0.118-rate-limit.json")
        self.result = parse_codex_findings(self.stdout)

    def test_result_has_required_keys(self):
        assert "findings" in self.result
        assert "residual_risk" in self.result
        assert "verdict" in self.result
        assert "raw_text" in self.result

    def test_verdict_is_empty_or_dict(self):
        assert isinstance(self.result["verdict"], dict)

    def test_no_blocking_findings_from_error_event(self):
        assert _blocking_findings(self.result) == []

    def test_residual_risk_is_string(self):
        assert isinstance(self.result["residual_risk"], str)

    def test_parser_does_not_raise_on_error_event(self):
        result = parse_codex_findings(self.stdout)
        assert result is not None


# ---------------------------------------------------------------------------
# v0.118 blocking findings (item-wrapper format)
# ---------------------------------------------------------------------------

class TestV0118BlockingFindings:
    """v0.118 stream: critical/security findings via item-wrapped agent_message."""

    def setup_method(self):
        self.stdout = _load_fixture("v0.118-blocking-findings.json")
        self.result = parse_codex_findings(self.stdout)

    def test_result_has_required_keys(self):
        assert "findings" in self.result
        assert "residual_risk" in self.result
        assert "verdict" in self.result
        assert "raw_text" in self.result

    def test_verdict_is_fail(self):
        assert self.result["verdict"].get("verdict") == "fail"

    def test_two_critical_findings(self):
        critical = [f for f in self.result["findings"] if f.get("severity") == "critical"]
        assert len(critical) == 2

    def test_sql_injection_finding_present(self):
        messages = [f["message"] for f in self.result["findings"]]
        assert any("sql injection" in m.lower() for m in messages)

    def test_credential_leak_finding_present(self):
        messages = [f["message"] for f in self.result["findings"]]
        assert any("logged in plaintext" in m.lower() or "API key" in m for m in messages)

    def test_path_traversal_finding_present(self):
        messages = [f["message"] for f in self.result["findings"]]
        assert any("path traversal" in m.lower() for m in messages)

    def test_blocking_findings_count(self):
        assert len(_blocking_findings(self.result)) >= 2

    def test_residual_risk_signals_security(self):
        assert "security" in self.result["residual_risk"].lower()

    def test_item_wrapper_text_extracted(self):
        assert "Critical blocking findings" in self.result["raw_text"]


# ---------------------------------------------------------------------------
# Future-version stubs
# ---------------------------------------------------------------------------

class TestV0119Stub:
    """v0.119 stub: parser handles future-version fixture without error."""

    def test_parses_without_error(self):
        stdout = _load_fixture("v0.119-stub.json")
        result = parse_codex_findings(stdout)
        assert result is not None
        assert "findings" in result
        assert "verdict" in result
        assert "raw_text" in result

    def test_verdict_pass(self):
        result = parse_codex_findings(_load_fixture("v0.119-stub.json"))
        assert result["verdict"].get("verdict") == "pass"


class TestV0120Stub:
    """v0.120 stub: parser handles future-version fixture without error."""

    def test_parses_without_error(self):
        stdout = _load_fixture("v0.120-stub.json")
        result = parse_codex_findings(stdout)
        assert result is not None
        assert "findings" in result
        assert "verdict" in result
        assert "raw_text" in result

    def test_verdict_pass(self):
        result = parse_codex_findings(_load_fixture("v0.120-stub.json"))
        assert result["verdict"].get("verdict") == "pass"


# ---------------------------------------------------------------------------
# Cross-version invariants
# ---------------------------------------------------------------------------

class TestCrossVersionInvariants:
    """Properties that must hold for every fixture, regardless of version."""

    ALL_FIXTURES = [
        "v0.118-success.json",
        "v0.118-failure.json",
        "v0.118-rate-limit.json",
        "v0.118-blocking-findings.json",
        "v0.119-stub.json",
        "v0.120-stub.json",
    ]

    @pytest.mark.parametrize("fixture_name", ALL_FIXTURES)
    def test_parse_does_not_raise(self, fixture_name):
        stdout = _load_fixture(fixture_name)
        result = parse_codex_findings(stdout)
        assert result is not None

    @pytest.mark.parametrize("fixture_name", ALL_FIXTURES)
    def test_required_keys_present(self, fixture_name):
        result = parse_codex_findings(_load_fixture(fixture_name))
        for key in ("findings", "residual_risk", "verdict", "raw_text"):
            assert key in result, f"{fixture_name}: missing key '{key}'"

    @pytest.mark.parametrize("fixture_name", ALL_FIXTURES)
    def test_findings_is_list(self, fixture_name):
        result = parse_codex_findings(_load_fixture(fixture_name))
        assert isinstance(result["findings"], list), (
            f"{fixture_name}: findings must be a list, got {type(result['findings'])}"
        )

    @pytest.mark.parametrize("fixture_name", ALL_FIXTURES)
    def test_verdict_is_dict(self, fixture_name):
        result = parse_codex_findings(_load_fixture(fixture_name))
        assert isinstance(result["verdict"], dict), (
            f"{fixture_name}: verdict must be a dict"
        )

    @pytest.mark.parametrize("fixture_name", ALL_FIXTURES)
    def test_raw_text_is_string(self, fixture_name):
        result = parse_codex_findings(_load_fixture(fixture_name))
        assert isinstance(result["raw_text"], str)

    @pytest.mark.parametrize("fixture_name", ALL_FIXTURES)
    def test_findings_have_normalized_shape(self, fixture_name):
        result = parse_codex_findings(_load_fixture(fixture_name))
        for f in result["findings"]:
            assert isinstance(f, dict), f"{fixture_name}: finding must be a dict"
            assert "severity" in f, f"{fixture_name}: finding missing severity"
            assert "message" in f, f"{fixture_name}: finding missing message"
            assert isinstance(f["severity"], str)
            assert isinstance(f["message"], str)

    @pytest.mark.parametrize("fixture_name", ALL_FIXTURES)
    def test_parse_is_idempotent(self, fixture_name):
        stdout = _load_fixture(fixture_name)
        result_a = parse_codex_findings(stdout)
        result_b = parse_codex_findings(stdout)
        assert result_a["findings"] == result_b["findings"]
        assert result_a["verdict"] == result_b["verdict"]

    @pytest.mark.parametrize("fixture_name", ALL_FIXTURES)
    def test_fixture_file_is_valid_ndjson(self, fixture_name):
        path = FIXTURES_DIR / fixture_name
        errors = []
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"line {i}: {e}")
        assert not errors, f"{fixture_name} has invalid NDJSON lines:\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# Edge-case resilience
# ---------------------------------------------------------------------------

class TestParserResilience:
    """Parser must handle malformed or empty input without crashing."""

    def test_empty_string(self):
        result = parse_codex_findings("")
        assert result is not None
        assert result["findings"] == []
        assert result["verdict"] == {}

    def test_whitespace_only(self):
        result = parse_codex_findings("   \n  \n  ")
        assert result is not None
        assert result["findings"] == []

    def test_non_json_lines_skipped(self):
        stdout = "not json at all\nalso not json\n"
        result = parse_codex_findings(stdout)
        assert result is not None

    def test_plain_text_verdict_fallback(self):
        stdout = "## Findings\n\n- critical: missing null check\n- high: unhandled exception\n"
        result = parse_codex_findings(stdout)
        assert len(result["findings"]) >= 2
        severities = {f["severity"] for f in result["findings"]}
        assert "critical" in severities or "high" in severities

    def test_fenced_json_verdict_extracted(self):
        text = '{"type": "agent_message", "text": "Review done.\\n\\n```json\\n{\"verdict\": \"pass\", \"findings\": []}\\n```"}'
        result = parse_codex_findings(text)
        assert result["verdict"].get("verdict") == "pass"

    def test_unfenced_inline_json_verdict_extracted(self):
        raw = '{"type": "agent_message", "text": "Summary: {\"verdict\": \"fail\", \"findings\": [{\"severity\": \"critical\", \"message\": \"no tests\"}]}"}'
        result = parse_codex_findings(raw)
        assert result["verdict"].get("verdict") == "fail"
        assert len(result["findings"]) == 1

    def test_multiple_agent_messages_concatenated(self):
        lines = [
            json.dumps({"type": "agent_message", "text": "First part of review."}),
            json.dumps({"type": "agent_message", "text": '```json\n{"verdict": "pass", "findings": []}\n```'}),
        ]
        result = parse_codex_findings("\n".join(lines))
        assert result["verdict"].get("verdict") == "pass"
        assert "First part" in result["raw_text"]
