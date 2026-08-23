"""gate_report_recovery.py tests (dispatch-20260823-beta2-j: "de tweede rapportput").

The four real companion filenames measured 23-08 (see gate_report_recovery.py's
module docstring for the full table) share no common substring — a path
DERIVATION finds one and misses the other three. These tests use all four real
filenames as candidate names (not a single invented pattern) to prove the
search is genuinely property-based, not secretly keyed on one of them.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import pytest

from gate_report_recovery import (
    AmbiguousRecoveryCandidates,
    extract_relabeled_verdict,
    find_recovery_candidate,
    recovered_verdict_conflicts,
)

# The four real companion filenames measured 23-08, verbatim.
REAL_COMPANION_FILENAMES = [
    "20260823-pr1672-1787474011-gate-review.md",
    "pr1674-glm-gate-review.md",
    "kimi-gate-pr1674-ledger-health-launchd.md",
    "pr-1674-glm-gate-review.md",
]

# The PR number embedded in each real filename above, in the same order.
REAL_COMPANION_PR_IDS = ["1672", "1674", "1674", "1674"]

_VERDICT_BODY = (
    "Reviewed the diff, no issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)


def _write(path: Path, text: str, *, mtime: "float | None" = None) -> Path:
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# 1. Each of the four REAL filenames is found by property, not by pattern guess
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename,pr_id", zip(REAL_COMPANION_FILENAMES, REAL_COMPANION_PR_IDS))
def test_real_companion_filename_is_found_by_property(tmp_path, filename, pr_id):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    companion = _write(reports_dir / filename, _VERDICT_BODY, mtime=now - 10)
    # The gate's own (fence-less) report for this exact dispatch — must be excluded.
    gate_report_name = f"glm-gate-pr{pr_id}-999999.md"
    _write(reports_dir / gate_report_name, "Reviewed in prose, no fence.\n", mtime=now)

    candidate = find_recovery_candidate(
        reports_dir,
        pr_id=pr_id,
        exclude_name=gate_report_name,
        window_start=now - 60,
        window_end=now,
    )

    assert candidate is not None, f"expected {filename!r} to be found for pr_id={pr_id!r}"
    assert candidate.path == companion
    assert candidate.verdict["verdict"] == "pass"


def test_no_single_pattern_covers_all_four_real_filenames():
    """Documents WHY a path derivation is wrong: no single ordering of
    prefix/timestamp/suffix tokens is shared by all four real filenames — the
    fix must search on properties, not derive a path from the dispatch_id."""
    starts_with_pr = [name.lower().startswith("pr") for name in REAL_COMPANION_FILENAMES]
    has_timestamp_segment = [
        any(tok.isdigit() and len(tok) >= 8 for tok in name.replace(".md", "").split("-"))
        for name in REAL_COMPANION_FILENAMES
    ]
    # Both properties vary across the four real filenames — no single derived
    # shape ("starts with pr", "has a trailing unix-timestamp segment") holds
    # for all of them, so a name derived from dispatch_id structure would miss
    # at least one.
    assert len(set(starts_with_pr)) > 1
    assert len(set(has_timestamp_segment)) > 1


# ---------------------------------------------------------------------------
# 2. Zero candidates -> None (absence of evidence, not a guess)
# ---------------------------------------------------------------------------


def test_zero_candidates_returns_none(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    _write(reports_dir / "glm-gate-pr42-111111.md", "no fence here\n", mtime=now)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="42", exclude_name="glm-gate-pr42-111111.md",
        window_start=now - 60, window_end=now,
    )
    assert candidate is None


def test_missing_unified_reports_dir_returns_none(tmp_path):
    candidate = find_recovery_candidate(
        tmp_path / "does-not-exist", pr_id="42", exclude_name="x.md",
        window_start=0, window_end=time.time(),
    )
    assert candidate is None


# ---------------------------------------------------------------------------
# 3. Two or more candidates -> fail-closed, loud, names both paths
# ---------------------------------------------------------------------------


def test_two_candidates_raises_ambiguous_and_names_both_paths(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    a = _write(reports_dir / "pr-42-glm-gate-review.md", _VERDICT_BODY, mtime=now - 20)
    b = _write(reports_dir / "pr42-glm-gate-review-retry.md", _VERDICT_BODY, mtime=now - 5)

    with pytest.raises(AmbiguousRecoveryCandidates) as excinfo:
        find_recovery_candidate(
            reports_dir, pr_id="42", exclude_name="glm-gate-pr42-999.md",
            window_start=now - 60, window_end=now,
        )

    assert set(excinfo.value.candidates) == {a, b}
    message = str(excinfo.value)
    assert str(a) in message
    assert str(b) in message


# ---------------------------------------------------------------------------
# Property boundaries: window, filename-PR-match, self-exclusion, fence requirement
# ---------------------------------------------------------------------------


def test_candidate_outside_time_window_is_excluded(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    _write(reports_dir / "pr-42-glm-gate-review.md", _VERDICT_BODY, mtime=now - 3600)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="42", exclude_name="glm-gate-pr42-999.md",
        window_start=now - 60, window_end=now,
    )
    assert candidate is None


def test_candidate_for_a_different_pr_is_excluded(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    _write(reports_dir / "pr-99-glm-gate-review.md", _VERDICT_BODY, mtime=now - 5)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="42", exclude_name="glm-gate-pr42-999.md",
        window_start=now - 60, window_end=now,
    )
    assert candidate is None


def test_pr_token_does_not_match_a_longer_number(tmp_path):
    """pr_id="42" must not match a companion mentioning pr420 or pr4242."""
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    _write(reports_dir / "pr-4242-glm-gate-review.md", _VERDICT_BODY, mtime=now - 5)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="42", exclude_name="glm-gate-pr42-999.md",
        window_start=now - 60, window_end=now,
    )
    assert candidate is None


def test_gate_own_report_is_excluded_even_if_it_matched_the_window(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    _write(reports_dir / "glm-gate-pr42-999.md", _VERDICT_BODY, mtime=now - 1)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="42", exclude_name="glm-gate-pr42-999.md",
        window_start=now - 60, window_end=now,
    )
    assert candidate is None


def test_file_without_a_fence_is_not_a_candidate(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    _write(reports_dir / "pr-42-notes.md", "Just some prose, no fence at all.\n", mtime=now - 5)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="42", exclude_name="glm-gate-pr42-999.md",
        window_start=now - 60, window_end=now,
    )
    assert candidate is None


def test_fence_with_template_verdict_is_not_a_candidate(tmp_path):
    """The gate's own echoed contract example (``"verdict": "pass|fail|blocked"``)
    must not be picked up as a real verdict — same rule as each gate's own
    _extract_verdict."""
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    template = (
        "```json\n"
        '{"verdict": "pass|fail|blocked", "findings": [], "residual_risk": null}\n'
        "```\n"
    )
    _write(reports_dir / "pr-42-echo.md", template, mtime=now - 5)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="42", exclude_name="glm-gate-pr42-999.md",
        window_start=now - 60, window_end=now,
    )
    assert candidate is None


def test_non_md_file_is_ignored(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    _write(reports_dir / "pr-42-glm-gate-review.txt", _VERDICT_BODY, mtime=now - 5)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="42", exclude_name="glm-gate-pr42-999.md",
        window_start=now - 60, window_end=now,
    )
    assert candidate is None


# ---------------------------------------------------------------------------
# recovered_verdict_conflicts — Deel 3 precondition #3 (also usable inline)
# ---------------------------------------------------------------------------


def test_no_conflict_when_primary_is_silent():
    primary = "Reviewed in prose, no structured verdict at all.\n"
    recovered = {"verdict": "pass", "findings": []}
    assert recovered_verdict_conflicts(primary, recovered) is None


def test_conflict_when_primary_verdict_word_disagrees():
    primary = 'Some malformed json: "verdict": "fail", trailing garbage'
    recovered = {"verdict": "pass", "findings": []}
    conflict = recovered_verdict_conflicts(primary, recovered)
    assert conflict is not None
    assert "fail" in conflict
    assert "pass" in conflict


def test_conflict_when_primary_mentions_blocking_but_recovered_has_none():
    primary = 'partial json: "severity": "error", cut off here'
    recovered = {"verdict": "pass", "findings": []}
    conflict = recovered_verdict_conflicts(primary, recovered)
    assert conflict is not None
    assert "blocking" in conflict.lower()


def test_no_conflict_when_recovered_verdict_word_matches_primary():
    primary = 'malformed: "verdict": "pass", but truncated'
    recovered = {"verdict": "pass", "findings": []}
    assert recovered_verdict_conflicts(primary, recovered) is None


def test_no_conflict_when_blocking_mentions_and_recovered_findings_agree():
    primary = 'partial: "severity": "error", also "severity": "blocked"'
    recovered = {
        "verdict": "fail",
        "findings": [{"severity": "error", "message": "x"}],
    }
    assert recovered_verdict_conflicts(primary, recovered) is None


# ---------------------------------------------------------------------------
# extract_relabeled_verdict — the plan-reviewer role's ```vnx-plan-verdict```
# fence, recognized and translated in place (no search needed). Verbatim from
# ~/.vnx-data/vnx-dev/unified_reports/glm-gate-pr1677-1787477675.md, the real
# run where glm answered inline, at the end of its response, under the WRONG
# fence label (measured 23-08).
# ---------------------------------------------------------------------------

REAL_RELABELED_FENCE_REPORT = (
    "## Residual risk\n\n"
    "The honest-retirement model is sound.\n\n"
    "```vnx-plan-verdict\n"
    "{\n"
    '  "verdict": "pass",\n'
    '  "blocking_findings": [],\n'
    '  "rationale": "The retired-status design is sound and verified against the real API."\n'
    "}\n"
    "```\n"
)


def test_real_pr1677_relabeled_fence_is_recognized_and_translated():
    verdict = extract_relabeled_verdict(REAL_RELABELED_FENCE_REPORT)
    assert verdict["verdict"] == "pass"
    assert verdict["findings"] == []
    assert "sound" in verdict["residual_risk"]


def test_relabeled_block_maps_to_blocked():
    text = (
        "```vnx-plan-verdict\n"
        '{"verdict": "block", "blocking_findings": ["unsafe migration"], "rationale": "no rollback"}\n'
        "```\n"
    )
    verdict = extract_relabeled_verdict(text)
    assert verdict["verdict"] == "blocked"
    assert verdict["findings"] == [{"severity": "error", "message": "unsafe migration"}]


def test_relabeled_revise_maps_to_fail_never_pass():
    """agents/plan-reviewer/CLAUDE.md defines "revise" as "real, fixable gaps
    remain" — never nothing, so it must never silently resolve to pass. The
    review-gate has no middle verdict, so this maps to the conservative side."""
    text = (
        "```vnx-plan-verdict\n"
        '{"verdict": "revise", "blocking_findings": ["missing test"], "rationale": "gaps remain"}\n'
        "```\n"
    )
    verdict = extract_relabeled_verdict(text)
    assert verdict["verdict"] == "fail"
    assert verdict["verdict"] != "pass"


def test_relabeled_verdict_with_no_blocking_findings_carries_empty_findings():
    text = '```vnx-plan-verdict\n{"verdict": "pass", "blocking_findings": [], "rationale": null}\n```\n'
    verdict = extract_relabeled_verdict(text)
    assert verdict["findings"] == []


def test_unrecognized_verdict_word_yields_no_verdict():
    text = '```vnx-plan-verdict\n{"verdict": "maybe", "blocking_findings": []}\n```\n'
    assert extract_relabeled_verdict(text) == {}


def test_absent_relabeled_fence_yields_no_verdict():
    assert extract_relabeled_verdict("Just prose, no fence at all.\n") == {}


def test_json_fence_report_yields_no_relabeled_verdict():
    """extract_relabeled_verdict only recognizes ```vnx-plan-verdict``` — a
    normal ```json``` report must not be picked up here (the gate's own
    _extract_verdict already handles that case first)."""
    text = '```json\n{"verdict": "pass", "findings": [], "residual_risk": null}\n```\n'
    assert extract_relabeled_verdict(text) == {}


# ---------------------------------------------------------------------------
# find_recovery_candidate must also recognize a companion file whose fence
# uses the wrong (plan-reviewer) label, not just the gate's own ```json```.
# ---------------------------------------------------------------------------


def test_search_recognizes_a_companion_with_the_relabeled_fence(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    companion = _write(
        reports_dir / "pr-42-relabeled-companion.md", REAL_RELABELED_FENCE_REPORT, mtime=now - 10,
    )

    candidate = find_recovery_candidate(
        reports_dir, pr_id="42", exclude_name="glm-gate-pr42-999.md",
        window_start=now - 60, window_end=now,
    )

    assert candidate is not None
    assert candidate.path == companion
    assert candidate.verdict["verdict"] == "pass"
