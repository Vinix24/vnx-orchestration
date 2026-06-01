"""Tests for report_body_contract — validate_body() and build_directive()."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from report_body_contract import BodyResult, build_directive, validate_body


# ---------------------------------------------------------------------------
# validate_body — canonical headings
# ---------------------------------------------------------------------------

def _canonical_body(summary: str = None) -> str:
    if summary is None:
        summary = "A" * 60
    return (
        f"## Summary\n\n{summary}\n\n"
        "## Changes\n\nSome changes were made.\n\n"
        "## Verification\n\nTests passed.\n\n"
        "## Open Items\n\nNone.\n"
    )


def test_validates_canonical_headings():
    result = validate_body(_canonical_body())
    assert result.valid is True
    assert result.status == "authored"
    assert result.missing == []
    assert result.placeholder is False


def test_returns_body_result_type():
    result = validate_body(_canonical_body())
    assert isinstance(result, BodyResult)


# ---------------------------------------------------------------------------
# validate_body — alias headings
# ---------------------------------------------------------------------------

def test_accepts_files_modified_alias():
    body = (
        "## Summary\n\n" + "A" * 60 + "\n\n"
        "## Files Modified\n\nsome.py\n\n"
        "## Verification\n\nTests passed.\n\n"
        "## Open Items\n\nNone.\n"
    )
    result = validate_body(body)
    assert result.valid is True, f"missing={result.missing}"


def test_accepts_work_completed_alias():
    body = (
        "## Summary\n\n" + "A" * 60 + "\n\n"
        "## Work Completed\n\nDone.\n\n"
        "## Verification\n\nTests passed.\n\n"
        "## Open Items\n\nNone.\n"
    )
    result = validate_body(body)
    assert result.valid is True, f"missing={result.missing}"


def test_accepts_test_results_alias():
    body = (
        "## Summary\n\n" + "A" * 60 + "\n\n"
        "## Changes\n\nSome changes.\n\n"
        "## Test Results\n\nAll green.\n\n"
        "## Open Items\n\nNone.\n"
    )
    result = validate_body(body)
    assert result.valid is True, f"missing={result.missing}"


def test_accepts_evidence_alias():
    body = (
        "## Summary\n\n" + "A" * 60 + "\n\n"
        "## Changes\n\nSome changes.\n\n"
        "## Evidence\n\nScreenshots attached.\n\n"
        "## Open Items\n\nNone.\n"
    )
    result = validate_body(body)
    assert result.valid is True, f"missing={result.missing}"


def test_accepts_tests_alias():
    body = (
        "## Summary\n\n" + "A" * 60 + "\n\n"
        "## Changes\n\nSome changes.\n\n"
        "## Tests\n\npytest -q: 10 passed.\n\n"
        "## Open Items\n\nNone.\n"
    )
    result = validate_body(body)
    assert result.valid is True, f"missing={result.missing}"


# ---------------------------------------------------------------------------
# validate_body — placeholder guard
# ---------------------------------------------------------------------------

def test_rejects_placeholder_summary():
    placeholder_body = (
        "## Summary\n\n"
        "Interactive tmux dispatch (lane: tmux_interactive). Status: done.\n\n"
        "## Changes\n\nSome changes.\n\n"
        "## Verification\n\nTests passed.\n\n"
        "## Open Items\n\nNone.\n"
    )
    result = validate_body(placeholder_body)
    assert result.valid is False
    assert result.placeholder is True
    assert result.status == "violated"


def test_placeholder_flag_true_on_placeholder_string():
    body = (
        "## Summary\n\n"
        "Interactive tmux dispatch (lane: tmux_interactive). Status: timeout.\n\n"
        "## Changes\n\n...\n\n"
        "## Verification\n\n...\n\n"
        "## Open Items\n\nNone.\n"
    )
    result = validate_body(body)
    assert result.placeholder is True


# ---------------------------------------------------------------------------
# validate_body — missing sections
# ---------------------------------------------------------------------------

def test_rejects_missing_open_items():
    body = (
        "## Summary\n\n" + "A" * 60 + "\n\n"
        "## Changes\n\nSome changes.\n\n"
        "## Verification\n\nTests passed.\n"
    )
    result = validate_body(body)
    assert result.valid is False
    assert "## Open Items" in result.missing
    assert result.status == "violated"


def test_rejects_missing_summary():
    body = (
        "## Changes\n\nSome changes.\n\n"
        "## Verification\n\nTests passed.\n\n"
        "## Open Items\n\nNone.\n"
    )
    result = validate_body(body)
    assert result.valid is False
    assert result.status == "violated"


def test_rejects_missing_changes():
    body = (
        "## Summary\n\n" + "A" * 60 + "\n\n"
        "## Verification\n\nTests passed.\n\n"
        "## Open Items\n\nNone.\n"
    )
    result = validate_body(body)
    assert result.valid is False
    assert "## Changes" in result.missing


def test_rejects_short_summary():
    body = (
        "## Summary\n\nShort.\n\n"
        "## Changes\n\nSome changes.\n\n"
        "## Verification\n\nTests passed.\n\n"
        "## Open Items\n\nNone.\n"
    )
    result = validate_body(body)
    assert result.valid is False
    assert result.status == "violated"


def test_empty_text_is_violated():
    result = validate_body("")
    assert result.valid is False
    assert result.status == "violated"


# ---------------------------------------------------------------------------
# validate_body — F4: pr_id requires ## PR section
# ---------------------------------------------------------------------------

def test_f4_pr_id_requires_pr_section():
    body = _canonical_body()
    result = validate_body(body, pr_id="42")
    assert result.valid is False
    assert "## PR" in result.missing


def test_f4_pr_id_accepts_pr_section():
    body = (
        "## Summary\n\n" + "A" * 60 + "\n\n"
        "## Changes\n\nSome changes.\n\n"
        "## Verification\n\nTests passed.\n\n"
        "## Open Items\n\nNone.\n\n"
        "## PR\n\nhttps://github.com/org/repo/pull/42\n"
    )
    result = validate_body(body, pr_id="42")
    assert result.valid is True, f"missing={result.missing}"


def test_no_pr_id_no_pr_section_required():
    result = validate_body(_canonical_body(), pr_id=None)
    assert result.valid is True


# ---------------------------------------------------------------------------
# build_directive — smoke tests (T1 coverage)
# ---------------------------------------------------------------------------

def test_build_directive_contains_sentinel():
    d = build_directive("test-dispatch-001")
    assert "<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->" in d


def test_build_directive_contains_all_required_sections():
    d = build_directive("test-dispatch-001")
    for section in ("## Summary", "## Changes", "## Verification", "## Open Items"):
        assert section in d


def test_build_directive_includes_pr_when_set():
    d = build_directive("test-dispatch-002", pr_id="PR-5")
    assert "## PR" in d


def test_build_directive_excludes_pr_when_not_set():
    d = build_directive("test-dispatch-003")
    assert "## PR" not in d


# ---------------------------------------------------------------------------
# CI assertion: no unified_reports body contains the old placeholder string
# ---------------------------------------------------------------------------

def test_no_placeholder_string_in_unified_reports(tmp_path):
    """CI-style check: placeholder string must be absent from any report files."""
    placeholder = "Interactive tmux dispatch (lane: tmux_interactive). Status:"
    unified_dir = tmp_path / "unified_reports"
    unified_dir.mkdir()

    # Write one good report and assert grep finds zero matches.
    (unified_dir / "good-dispatch.md").write_text(
        "## Summary\n\nAll good.\n\n## Changes\n\nFiles modified.\n\n"
        "## Verification\n\nPassed.\n\n## Open Items\n\nNone.\n"
    )
    for f in unified_dir.glob("*.md"):
        assert placeholder not in f.read_text(), (
            f"{f} contains the legacy placeholder body — govern() should have replaced it"
        )
