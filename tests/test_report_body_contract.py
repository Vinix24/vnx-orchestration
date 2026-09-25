"""Tests for report_body_contract — validate_body() and build_directive()."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import logging
import re

from report_body_contract import (
    BodyResult,
    build_directive,
    divergent_report_headings,
    validate_body,
    warn_on_divergent_headings,
    with_directive,
)


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
# OI-1599 — the PR_Ref request is unconditional, unlike the ## PR heading
# ---------------------------------------------------------------------------

def test_build_directive_requests_pr_ref_even_without_pr_id():
    """A dispatch that will create a BRAND NEW PR has no pr_id yet at
    dispatch time — the door cannot know the PR's number until ``gh pr
    create`` runs mid-dispatch. If the request were gated on pr_id (like the
    ``## PR`` heading is), exactly the dispatches that most need to report
    their PR number would never be asked."""
    d = build_directive("test-dispatch-004")
    assert "**PR_Ref**" in d


def test_build_directive_requests_pr_ref_when_pr_id_also_set():
    """The request stays present when pr_id IS already known — the ## PR
    heading and the PR_Ref bold-field request are independent asks."""
    d = build_directive("test-dispatch-005", pr_id="PR-9")
    assert "**PR_Ref**" in d
    assert "## PR" in d


def test_build_directive_pr_ref_request_does_not_add_a_required_heading():
    """The PR_Ref instruction must not turn into a NEW required section —
    a report that never produces a PR must still validate cleanly."""
    d = build_directive("test-dispatch-006")
    body = _canonical_body()
    result = validate_body(body)
    assert result.valid is True, f"missing={result.missing}"
    # And the directive text itself carries no NEW "## " heading — the
    # PR_Ref ask is a bold field, not a section header.
    import re
    headings = set(re.findall(r"^## .+", d, re.MULTILINE))
    assert headings == {"## Report Body Contract"}


# ---------------------------------------------------------------------------
# OI-1850 — identity block, with_directive, and the divergent-role check
# ---------------------------------------------------------------------------

def test_build_directive_identity_block_carries_known_values():
    d = build_directive("disp-x", model="sonnet", provider="claude")
    assert "`**Dispatch-ID**: disp-x`" in d
    assert "`**Model**: sonnet`" in d
    assert "`**Provider**: claude`" in d


def test_build_directive_identity_block_tells_the_shape_when_values_unknown():
    d = build_directive("disp-x")
    assert "`**Model**: <the short id of the model you run as" in d
    assert "no spaces, no backticks" in d
    assert "`**Provider**: <the provider you run on" in d


def test_build_directive_identity_block_adds_no_required_heading():
    """The identity block is a request: no heading, so no report is newly invalid."""
    d = build_directive("disp-x", model="sonnet", provider="claude")
    assert set(re.findall(r"^## .+", d, re.MULTILINE)) == {"## Report Body Contract"}


def test_with_directive_appends_once(monkeypatch):
    monkeypatch.delenv("VNX_REPORT_CONTRACT_DIRECTIVE", raising=False)
    once = with_directive("body", "disp-y")
    twice = with_directive(once, "disp-y")
    assert once.startswith("body\n\n<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->")
    assert twice == once


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_with_directive_is_a_no_op_when_the_switch_is_off(monkeypatch, value):
    monkeypatch.setenv("VNX_REPORT_CONTRACT_DIRECTIVE", value)
    assert with_directive("body", "disp-y") == "body"


def test_with_directive_passes_pr_and_identity_values_through(monkeypatch):
    monkeypatch.delenv("VNX_REPORT_CONTRACT_DIRECTIVE", raising=False)
    out = with_directive("body", "disp-y", pr_id="PR-3", model="opus", provider="claude")
    assert "`## PR`" in out
    assert "`**Model**: opus`" in out
    assert "`**Provider**: claude`" in out


_CONTRACT_LIST = "`## Summary` / `## Changes` / `## Verification` / `## Open Items`"


def test_divergent_headings_none_for_the_contract_list():
    assert divergent_report_headings(f"Report headings: {_CONTRACT_LIST}.") == ([], [])


def test_divergent_headings_accepts_the_validator_aliases():
    text = "Report headings: `## Summary` / `## Files Modified` / `## Tests` / `## Open Items`"
    assert divergent_report_headings(text) == ([], [])


def test_divergent_headings_names_unknown_and_missing_for_the_probe_list():
    text = (
        "Your report has: `## What changed` / `## Commands run` / `## Tests` / "
        "`## Answer` / `## Known limitations` / `## Open Items`"
    )
    unknown, missing = divergent_report_headings(text)
    assert unknown == ["## What changed", "## Commands run", "## Answer", "## Known limitations"]
    assert missing == ["## Summary", "## Changes"]


def test_divergent_headings_reads_bullets_and_report_skeletons():
    bullets = "The report has:\n- `## What changed`\n- `## Open Items`\n"
    assert divergent_report_headings(bullets)[0] == ["## What changed"]
    skeleton = "Use this report skeleton:\n\n```markdown\n## Summary\nx\n## Changes\ny\n## Notes\n```\n"
    unknown, missing = divergent_report_headings(skeleton)
    assert unknown == ["## Notes"]
    assert missing == ["## Verification", "## Open Items"]


def test_divergent_headings_is_case_sensitive_like_the_validator():
    text = "Report headings: `## summary` / `## Changes` / `## Verification` / `## Open Items`"
    unknown, missing = divergent_report_headings(text)
    assert unknown == ["## summary"]
    assert missing == ["## Summary"]


def test_divergent_headings_ignores_the_role_files_own_structure_and_unrelated_refs():
    text = (
        "# Role\n\n## Capabilities\n- x\n\n## Workflow\n- y\n\n"
        "The lane appends `## Scope Guard` and `## Completion Protocol` itself.\n"
    )
    assert divergent_report_headings(text) == ([], [])


def test_divergent_headings_empty_text():
    assert divergent_report_headings("") == ([], [])


def test_warn_on_divergent_headings_logs_source_and_returns_true(caplog):
    text = "Report headings: `## What changed` / `## Open Items`"
    with caplog.at_level(logging.WARNING, logger="report_body_contract"):
        assert warn_on_divergent_headings(text, "agents/x/CLAUDE.md") is True
    message = caplog.records[-1].getMessage()
    assert "agents/x/CLAUDE.md" in message
    assert "## What changed" in message


def test_warn_on_divergent_headings_silent_for_the_contract(caplog):
    with caplog.at_level(logging.WARNING, logger="report_body_contract"):
        assert warn_on_divergent_headings(_CONTRACT_LIST + " report", "x.md") is False
    assert caplog.records == []


def _shipped_role_texts():
    root = Path(__file__).resolve().parents[1]
    files = [root / "scripts" / "lib" / "prompts" / "base_worker.md"]
    files += sorted((root / "scripts" / "lib" / "prompts" / "roles").glob("*.md"))
    files += sorted((root / "agents").glob("*/CLAUDE.md"))
    return files


@pytest.mark.parametrize("path", _shipped_role_texts(), ids=lambda p: p.parent.name + "/" + p.name)
def test_no_shipped_role_text_lists_headings_that_differ_from_the_contract(path):
    """The fabric prompt and every shipped role file say what the validator says."""
    assert divergent_report_headings(path.read_text()) == ([], [])


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
