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


def test_window_is_the_discriminator_not_find_recovery_candidate_itself(tmp_path):
    """T0 measured this 23-08 on the real unified_reports directory (4657 files,
    read-only copy): a WIDE window (0 .. infinity) on PR 1672 finds 5 candidates
    and on PR 1674 finds 3 — both AmbiguousRecoveryCandidates. The gate's REAL,
    narrow window (the run's own wall-clock start/end) finds exactly 1 for each.

    find_recovery_candidate itself has no opinion on how narrow its window is —
    that choice is made by the CALLER (glm_gate.py/kimi_gate.py), not visible in
    this module at all. If a caller ever "widens the window for safety", nothing
    goes red: every case just routes to recovery_ambiguous, and recovery quietly
    stops working. This test pins the window itself as load-bearing, not merely
    filtering, by calling find_recovery_candidate TWICE against the exact same
    directory contents: a narrow (real-shaped) window resolves to one candidate,
    and a wide (0, inf) window over the identical files raises ambiguity.
    """
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()

    # The one companion report actually inside the run's own (narrow) window.
    real_companion = _write(
        reports_dir / "pr-42-glm-gate-review.md", _VERDICT_BODY, mtime=now - 10,
    )
    # Two more verdict-bearing reports for the SAME PR, from earlier gate runs —
    # long outside the narrow window, but a (0, inf) window sees them too.
    _write(reports_dir / "pr-42-glm-gate-review-lastweek.md", _VERDICT_BODY, mtime=now - 7 * 86400)
    _write(reports_dir / "pr-42-glm-gate-review-lastmonth.md", _VERDICT_BODY, mtime=now - 30 * 86400)
    # The gate's own (fence-less) report — excluded either way, in both calls.
    gate_report_name = "glm-gate-pr42-999.md"
    _write(reports_dir / gate_report_name, "Reviewed in prose, no fence.\n", mtime=now)

    narrow = find_recovery_candidate(
        reports_dir, pr_id="42", exclude_name=gate_report_name,
        window_start=now - 60, window_end=now,
    )
    assert narrow is not None
    assert narrow.path == real_companion

    with pytest.raises(AmbiguousRecoveryCandidates) as excinfo:
        find_recovery_candidate(
            reports_dir, pr_id="42", exclude_name=gate_report_name,
            window_start=0, window_end=float("inf"),
        )
    assert len(excinfo.value.candidates) == 3


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


# ---------------------------------------------------------------------------
# (c) dispatch-20260826-beta3-b: a companion named after its own dispatch-id
# (no PR token anywhere in the filename) must still be found via its BODY
# text. Real evidence, measured 26-08:
# ``20260824-alpha-a5-config-reader-fallback_report.md`` carries a clean pass
# verdict for PR 1684, written 11s before the gate's own report (inside the
# recovery window), but its filename contains no "1684" at all — the
# filename-only check dropped this exact evidence, surfacing
# ``recovery_empty`` on a pass verdict that was sitting right there. The real
# body spells the PR identity as "PR 1684" (a literal space, not a dash),
# verbatim from the measured file's own H1.
# ---------------------------------------------------------------------------

_REAL_PR1684_BODY_PREFIX = (
    "# PR 1684 — config_runtime state-dir fallback + loud fail-soft (OI-1461)\n\n"
    "**Dispatch-ID**: 20260824-alpha-a5-config-reader-fallback (OI-1461)\n\n"
    "## Summary\n\n"
    "Code-review gate verdict for PR 1684. The diff adds a canonical-resolver "
    "fallback.\n\n"
)


def test_companion_named_by_dispatch_id_with_pr_token_only_in_body_is_found(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    companion = _write(
        reports_dir / "20260824-alpha-a5-config-reader-fallback_report.md",
        _REAL_PR1684_BODY_PREFIX + _VERDICT_BODY,
        mtime=now - 11,
    )
    gate_report_name = "glm-gate-pr1684-999999.md"
    _write(reports_dir / gate_report_name, "Reviewed in prose, no fence.\n", mtime=now)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="1684", exclude_name=gate_report_name,
        window_start=now - 60, window_end=now,
    )

    assert candidate is not None, (
        "a companion named after its own dispatch-id, with the PR token only "
        "in the body text, must still be found — this is the exact PR #1684 "
        "shape that recovery_empty dropped before this fix"
    )
    assert candidate.path == companion
    assert candidate.verdict["verdict"] == "pass"


def test_bare_number_in_body_without_pr_prefix_does_not_match(tmp_path):
    """A companion that merely mentions the bare number (e.g. a line
    reference) must NOT become a candidate — only a "pr"-prefixed token
    counts in the body, exactly as it already does in the filename."""
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    body = "Line 1684 of the diff changed. Nothing else here.\n\n" + _VERDICT_BODY
    _write(reports_dir / "some-other-dispatch-id_report.md", body, mtime=now - 10)
    gate_report_name = "glm-gate-pr1684-999999.md"
    _write(reports_dir / gate_report_name, "Reviewed in prose, no fence.\n", mtime=now)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="1684", exclude_name=gate_report_name,
        window_start=now - 60, window_end=now,
    )
    assert candidate is None


def test_pr_token_in_body_does_not_match_a_longer_number(tmp_path):
    """pr_id="1684" must not match body text mentioning PR 16840 — same
    trailing-digit boundary rule as the filename check."""
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    body = "# PR 16840 — unrelated change\n\n" + _VERDICT_BODY
    _write(reports_dir / "some-other-dispatch-id_report.md", body, mtime=now - 10)
    gate_report_name = "glm-gate-pr1684-999999.md"
    _write(reports_dir / gate_report_name, "Reviewed in prose, no fence.\n", mtime=now)

    candidate = find_recovery_candidate(
        reports_dir, pr_id="1684", exclude_name=gate_report_name,
        window_start=now - 60, window_end=now,
    )
    assert candidate is None


def test_two_candidates_matched_via_body_text_still_raises_ambiguous(tmp_path):
    """Widening point 2 to body text makes 2+ candidates MORE likely — that
    is correct, not a regression: the fail-closed ambiguity check must still
    refuse to guess between them."""
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()
    a = _write(
        reports_dir / "dispatch-a_report.md",
        "# PR 1684 — first companion\n\n" + _VERDICT_BODY, mtime=now - 20,
    )
    b = _write(
        reports_dir / "dispatch-b_report.md",
        "# PR 1684 — second companion\n\n" + _VERDICT_BODY, mtime=now - 5,
    )
    gate_report_name = "glm-gate-pr1684-999999.md"
    _write(reports_dir / gate_report_name, "Reviewed in prose, no fence.\n", mtime=now)

    with pytest.raises(AmbiguousRecoveryCandidates) as excinfo:
        find_recovery_candidate(
            reports_dir, pr_id="1684", exclude_name=gate_report_name,
            window_start=now - 60, window_end=now,
        )
    assert set(excinfo.value.candidates) == {a, b}


# ---------------------------------------------------------------------------
# T0 addendum (26-08, dispatch-20260826-beta3-b): a test that only proves
# content-matching finds the companion file IN ISOLATION proves the wrong
# thing — the live PR #1684 store also holds a SECOND glm-gate run for the
# same PR (a later retry) whose FILENAME matches the "pr1684" token and which
# carries its own valid pass verdict. An unbounded search (no window, no
# exclude_name) finds BOTH that name-match decoy and the content-match
# companion and raises AmbiguousRecoveryCandidates — measured live 26-08
# against the real store. It is the run's own recovery window (not the
# content-vs-name distinction) that resolves this down to exactly the one
# candidate that matters. These two tests exercise the FULL chain — name
# filter, content filter, exclude_name, and the mtime window — together, with
# the real measured numbers, instead of each property in isolation.
# ---------------------------------------------------------------------------

# Real values, read 26-08 from the live store
# (~/.vnx-data/vnx-dev/unified_reports/), read-only:
_REAL_PR1684_DISPATCH_ID = "glm-gate-pr1684-1787550833"
_REAL_PR1684_WINDOW_START = 1787550833.0  # dispatch_id's own embedded floor
_REAL_PR1684_OWN_REPORT_MTIME = 1787551030.278105
_REAL_PR1684_COMPANION_MTIME = 1787551019.4603355
# glm-gate-pr1684-1787552314.md: a LATER retry for the same PR — matches by
# NAME ("pr1684"), carries its own valid pass verdict, but landed long after
# this dispatch's own recovery window had already closed.
_REAL_PR1684_LATER_RETRY_MTIME = 1787552730.9582276


def test_real_pr1684_window_plus_exclude_resolves_the_name_match_decoy(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()

    own_report_name = f"{_REAL_PR1684_DISPATCH_ID}.md"
    _write(reports_dir / own_report_name, "Reviewed in prose, no fence.\n", mtime=_REAL_PR1684_OWN_REPORT_MTIME)
    companion = _write(
        reports_dir / "20260824-alpha-a5-config-reader-fallback_report.md",
        _REAL_PR1684_BODY_PREFIX + _VERDICT_BODY,
        mtime=_REAL_PR1684_COMPANION_MTIME,
    )
    _write(
        reports_dir / "glm-gate-pr1684-1787552314.md",
        _VERDICT_BODY,
        mtime=_REAL_PR1684_LATER_RETRY_MTIME,
    )

    # Unbounded: both the later-retry decoy (name match) and the companion
    # (content match) qualify — ambiguous, exactly as measured live.
    with pytest.raises(AmbiguousRecoveryCandidates) as excinfo:
        find_recovery_candidate(
            reports_dir, pr_id="1684", exclude_name="__none__.md",
            window_start=0, window_end=float("inf"),
        )
    assert len(excinfo.value.candidates) == 2

    # The REAL call: this dispatch's own window + its own exclude_name.
    # Resolves to exactly the one candidate inside the window — the window,
    # not the content-vs-name distinction, is what saves this from ambiguity.
    candidate = find_recovery_candidate(
        reports_dir, pr_id="1684", exclude_name=own_report_name,
        window_start=_REAL_PR1684_WINDOW_START, window_end=_REAL_PR1684_OWN_REPORT_MTIME,
    )
    assert candidate is not None
    assert candidate.path == companion
    assert candidate.verdict["verdict"] == "pass"


def test_name_match_and_content_match_both_inside_window_still_ambiguous(tmp_path):
    """The fail-closed property must survive widening point 2 to body text:
    a NAME-matching candidate and a CONTENT-matching candidate that both fall
    INSIDE the same recovery window are still 2 candidates — refuse, never
    guess which one is real."""
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    now = time.time()

    name_match = _write(
        reports_dir / "pr-1684-glm-gate-review.md", _VERDICT_BODY, mtime=now - 15,
    )
    content_match = _write(
        reports_dir / "some-dispatch-id_report.md",
        "# PR 1684 — unrelated companion\n\n" + _VERDICT_BODY,
        mtime=now - 10,
    )
    gate_report_name = "glm-gate-pr1684-999999.md"
    _write(reports_dir / gate_report_name, "Reviewed in prose, no fence.\n", mtime=now)

    with pytest.raises(AmbiguousRecoveryCandidates) as excinfo:
        find_recovery_candidate(
            reports_dir, pr_id="1684", exclude_name=gate_report_name,
            window_start=now - 60, window_end=now,
        )
    assert set(excinfo.value.candidates) == {name_match, content_match}
