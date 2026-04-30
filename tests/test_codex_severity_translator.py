"""Tests for CFX-17 codex severity translator."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from codex_severity_translator import (  # noqa: E402
    DEFAULT_POLICY_PATH,
    load_policy,
    main,
    translate_findings,
)


def _write_policy(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Policy patterns demote correctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message",
    [
        "Truncated SHA hash present in repo",
        "field stores truncated  digest hash",
    ],
)
def test_truncated_hash_demoted_to_warning(message: str) -> None:
    findings = [{"severity": "error", "message": message}]
    out = translate_findings(findings)
    assert out[0]["severity"] == "warning"
    assert out[0]["original_severity"] == "error"
    assert "truncated" in out[0]["demotion_reason"].lower() or "digest" in out[0]["demotion_reason"].lower()


def test_hardcoded_test_url_demoted_to_warning() -> None:
    out = translate_findings(
        [{"severity": "error", "message": "Found hardcoded test URL in fixture"}]
    )
    assert out[0]["severity"] == "warning"
    assert out[0]["original_severity"] == "error"


def test_hardcoded_localhost_demoted_to_warning() -> None:
    out = translate_findings(
        [{"severity": "error", "message": "hardcoded localhost reference"}]
    )
    assert out[0]["severity"] == "warning"


def test_stderr_plain_text_demoted_to_warning() -> None:
    out = translate_findings(
        [{"severity": "error", "message": "stderr emitted as plain text instead of JSON"}]
    )
    assert out[0]["severity"] == "warning"


def test_out_of_scope_finding_demoted_to_warning() -> None:
    out = translate_findings(
        [{"severity": "error", "message": "Issue identified in out_of_scope module"}]
    )
    assert out[0]["severity"] == "warning"


def test_adjacent_code_demoted_to_warning() -> None:
    out = translate_findings(
        [{"severity": "error", "message": "minor concern in adjacent code path"}]
    )
    assert out[0]["severity"] == "warning"


@pytest.mark.parametrize(
    "message",
    ["minor style issue", "formatting nit", "indentation off in helper"],
)
def test_style_findings_demoted_to_info(message: str) -> None:
    out = translate_findings([{"severity": "error", "message": message}])
    assert out[0]["severity"] == "info"
    assert out[0]["original_severity"] == "error"


# ---------------------------------------------------------------------------
# Non-matching patterns unchanged
# ---------------------------------------------------------------------------

def test_non_matching_finding_passthrough() -> None:
    findings = [
        {"severity": "error", "message": "SQL injection vulnerability in handler"},
        {"severity": "error", "message": "race condition on receipt write"},
    ]
    out = translate_findings(findings)
    assert [f["severity"] for f in out] == ["error", "error"]
    for f in out:
        assert "original_severity" not in f
        assert "demotion_reason" not in f


def test_already_warning_finding_unchanged() -> None:
    findings = [{"severity": "warning", "message": "non-blocking nit"}]
    out = translate_findings(findings)
    assert out[0]["severity"] == "warning"
    assert "original_severity" not in out[0]


# ---------------------------------------------------------------------------
# Policy file missing -> no-op
# ---------------------------------------------------------------------------

def test_missing_policy_file_is_noop(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.yaml"
    findings = [
        {"severity": "error", "message": "truncated digest hash present"},
        {"severity": "error", "message": "minor style issue"},
    ]
    out = translate_findings(findings, policy_path=missing)
    assert [f["severity"] for f in out] == ["error", "error"]
    for f in out:
        assert "original_severity" not in f


def test_malformed_policy_file_is_noop(tmp_path: Path) -> None:
    bad = _write_policy(tmp_path / "bad.yaml", "::: not yaml :::\n  - [")
    out = translate_findings(
        [{"severity": "error", "message": "truncated hash field"}],
        policy_path=bad,
    )
    assert out[0]["severity"] == "error"


def test_empty_findings_returns_empty() -> None:
    assert translate_findings([]) == []


# ---------------------------------------------------------------------------
# Original severity preserved
# ---------------------------------------------------------------------------

def test_original_severity_preserved_on_demotion() -> None:
    out = translate_findings(
        [{"severity": "error", "message": "truncated hash digest"}]
    )
    assert out[0]["original_severity"] == "error"
    assert out[0]["severity"] == "warning"
    assert out[0]["demotion_reason"]


def test_input_list_not_mutated() -> None:
    findings = [{"severity": "error", "message": "truncated hash"}]
    translate_findings(findings)
    assert findings[0]["severity"] == "error"
    assert "original_severity" not in findings[0]


# ---------------------------------------------------------------------------
# Multiple patterns per finding -> first match wins (within category)
# ---------------------------------------------------------------------------

def test_first_warning_match_wins(tmp_path: Path) -> None:
    """When two demote_to_warning rules could match, the first listed rule is used."""
    policy = _write_policy(
        tmp_path / "policy.yaml",
        """
demote_to_warning:
  - pattern: "first-rule"
    rationale: "first wins"
  - pattern: "second-rule|first-rule"
    rationale: "second"
demote_to_info: []
""",
    )
    out = translate_findings(
        [{"severity": "error", "message": "matches first-rule and second-rule"}],
        policy_path=policy,
    )
    assert out[0]["severity"] == "warning"
    assert out[0]["demotion_reason"] == "first wins"


def test_demote_to_info_overrides_warning() -> None:
    """A finding matching both warning and info rules ends up at info (lowest)."""
    findings = [
        {"severity": "error", "message": "truncated hash with style nit"},
    ]
    out = translate_findings(findings)
    # truncated hash matches demote_to_warning; style matches demote_to_info.
    # Demotion is monotonic to the lower severity, so info wins.
    assert out[0]["severity"] == "info"


def test_info_demotion_not_promoted_back(tmp_path: Path) -> None:
    """Once a finding is info, a later warning rule does not promote it back."""
    policy = _write_policy(
        tmp_path / "policy.yaml",
        """
demote_to_warning:
  - pattern: "shared-token"
    rationale: "warning"
demote_to_info:
  - pattern: "shared-token"
    rationale: "info"
""",
    )
    out = translate_findings(
        [{"severity": "error", "message": "shared-token finding"}],
        policy_path=policy,
    )
    assert out[0]["severity"] == "info"


# ---------------------------------------------------------------------------
# Default policy file is loadable
# ---------------------------------------------------------------------------

def test_default_policy_file_loads() -> None:
    assert DEFAULT_POLICY_PATH.exists(), "default policy YAML must ship with the module"
    policy = load_policy()
    assert any(rule["pattern"] for rule in policy["demote_to_warning"])
    assert any(rule["pattern"] for rule in policy["demote_to_info"])


# ---------------------------------------------------------------------------
# CLI --review
# ---------------------------------------------------------------------------

def test_cli_review_reports_demotion(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps(
            {
                "blocking_findings": [
                    {"severity": "error", "message": "truncated hash digest"},
                    {"severity": "error", "message": "real SQL injection"},
                ]
            }
        ),
        encoding="utf-8",
    )

    rc = main(["--review", str(result_file)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Blocking before: 2" in out
    assert "Blocking after: 1" in out
    assert "Demoted: 1" in out


def test_cli_review_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--review", str(tmp_path / "missing.json")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not found" in err


# ---------------------------------------------------------------------------
# Robustness against odd input shapes
# ---------------------------------------------------------------------------

def test_finding_without_message_treated_as_error() -> None:
    out = translate_findings([{"severity": "error"}])
    assert out[0]["severity"] == "error"


def test_finding_without_severity_defaults_to_error() -> None:
    out = translate_findings([{"message": "truncated digest hash"}])
    assert out[0]["severity"] == "warning"
    assert out[0]["original_severity"] == "error"


def test_invalid_regex_in_policy_skipped(tmp_path: Path) -> None:
    policy = _write_policy(
        tmp_path / "policy.yaml",
        """
demote_to_warning:
  - pattern: "[unclosed"
    rationale: "broken"
  - pattern: "valid-pattern"
    rationale: "ok"
""",
    )
    out = translate_findings(
        [{"severity": "error", "message": "valid-pattern hit"}],
        policy_path=policy,
    )
    assert out[0]["severity"] == "warning"
    assert out[0]["demotion_reason"] == "ok"
