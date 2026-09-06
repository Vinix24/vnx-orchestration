#!/usr/bin/env python3
"""OI-1645: only REVIEW gates may stand in as a peer signer — ci_gate is a CI
check, never a code review.

Measured live 2026-09-06 on the vnx-dev store, at the merge of PR #1781 (main
c41c7f36): the door said, literally, "ci_gate droeg op dezelfde head
zelfstandig een geldige, volledig bewezen pass en telt als ondertekenaar bij
afwezigheid van de gedeclareerde poort". A dry-run simulation against a tmp
results-dir carrying only ``pr-1781-codex_gate.json`` (status=unavailable, the
real recorded reason) and ``pr-1781-ci_gate.json`` (status=pass, the real
recorded contract_hash/report_path/commit_sha) confirmed:
``check_review_gate_for_merge(pr_id="1781", gate="codex_gate", ...)`` returned
GO with ``evidence_gate="ci_gate"`` — zero review gates had spoken for this
head. Not exploited live: every one of the eight merges around this date also
carried an independent glm_gate PASS on the same head (verified against
t0_receipts.ndjson pr_merged events since 2026-09-06T10:00Z and the merge
logs), so this dispatch's own report carries that verification, not this test
file.

Root cause: ``closure_verifier._find_peer_gate_results`` (OI-1624/OI-1642)
iterated ``_KNOWN_GATES`` — the enum-derived set of gates the closure verifier
can INTERPRET a result record for. ``ci_gate`` is in that set (the verifier
does know how to read a ci_gate record) but it is not a review: it reports on
CI checks (tests, lint, build), never on the code change itself. Being
interpretable and being a review are two different claims that
``_find_peer_gate_results`` conflated into one.

The fix (``_NON_REVIEW_SIGNER_REASONS`` / ``_REVIEW_PEER_GATES``, both derived
from the ``Gate`` enum with a reason per exclusion, same discipline as
``_GATES_NOT_IMPLEMENTED_BY_CLOSURE``) narrows both the OI-1624/OI-1642 peer
route (``_find_peer_gate_results``) AND the OI-1576 takeover-successor route
(``_find_takeover_successor_results``) to gates that are actual reviews.
``ci_gate`` and ``wiring_gate`` are excluded by name with a reason;
``claude_github_optional`` is deliberately kept IN the peer set — its
``completed`` state is a genuine Claude code review of the diff, not a CI
signal, so it counts when it exists (see the reason comment on
``_NON_REVIEW_SIGNER_REASONS`` in ``closure_verifier.py``).

Scenario 3 below also pins a related design decision: a ci_gate FAIL is no
longer read as a "real rejection" in the peer route either (symmetry — a gate
that cannot sign a PASS should not silently block via a FAIL in the same
route). This is not a safety regression: ``pr_merge.main`` calls
``_run_ci_gate`` (a LIVE ``gh`` statusCheckRollup check via
``merge_preflight_ci_check.check_ci_run_for_head``) BEFORE the review gate
and returns EXIT_ERROR immediately on a non-green CI conclusion — a real CI
failure already blocks the merge through that separate, independent check,
so the review-gate peer route reading a stale ci_gate result-file FAIL as a
second rejection would be double-counting, not added safety.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier as cv
from dispatch_spec import Gate

# Mirrored from the live vnx-dev store shape for PR #1781 (2026-09-06).
PR_ID = "1781"
BRANCH = "dispatch/20260905-golf4-klasse-wachter"
HEAD_SHA = "ea42e9221ecb51c2290440b76e9d4c90a3c9a72b"

CLEAN_REPORT = "# Gate report\n\nAll findings reviewed, nothing blocking.\n"


def _write_result(results_dir: Path, gate: str, data: dict) -> Path:
    """Write a result record under the writer's real naming (pr-<n>-<gate>.json)."""
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"pr-{PR_ID}-{gate}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _report_file(tmp_path: Path, name: str = "report.md", content: str = CLEAN_REPORT) -> Path:
    report = tmp_path / name
    report.write_text(content, encoding="utf-8")
    return report


def _codex_unavailable() -> dict:
    """pr-1781-codex_gate.json as measured live: terminal-absence via
    UNAVAILABLE_STATES, in scope (carries the real branch/commit_sha)."""
    return {
        "gate": "codex_gate",
        "pr_id": PR_ID,
        "status": "unavailable",
        "reason": "exit_nonzero",
        "contract_hash": "",
        "report_path": "",
        "branch": BRANCH,
        "commit_sha": HEAD_SHA,
    }


def _ci_gate_pass(report: Path, **overrides) -> dict:
    """pr-1781-ci_gate.json as measured live: a full, all-invariants-passing
    PASS record — status/contract_hash/report_path/commit_sha all populated,
    same shape a genuine review gate would carry."""
    data = {
        "gate": "ci_gate",
        "pr_id": PR_ID,
        "status": "pass",
        "blocking_findings": [],
        "advisory_findings": [],
        "blocking_count": 0,
        "advisory_count": 0,
        "contract_hash": "2245497555d37997",
        "report_path": str(report),
        "branch": BRANCH,
        "commit_sha": HEAD_SHA,
    }
    data.update(overrides)
    return data


def _glm_pass(report: Path, **overrides) -> dict:
    data = {
        "gate": "glm_gate",
        "pr_id": PR_ID,
        "status": "pass",
        "blocking_count": 0,
        "blocking_findings": [],
        "contract_hash": "48ac3a901eb93175",
        "report_path": str(report),
        "branch": BRANCH,
        "commit_sha": HEAD_SHA,
    }
    data.update(overrides)
    return data


def _check(results_dir: Path, gate: str = "codex_gate") -> dict:
    return cv.check_review_gate_for_merge(
        PR_ID, gate, results_dir, branch=BRANCH, head_sha=HEAD_SHA
    )


class TestCiGateAloneNeverSigns:
    """Scenario 1 (dispatch): codex absent, only ci_gate PASS on the head.
    Before the fix this returned GO with evidence_gate=ci_gate (measured live
    on PR #1781) — RED against pre-fix code. After the fix: zero review gates
    have spoken, so this stays NO-GO, honestly reported as absence."""

    def test_ci_gate_only_pass_is_no_go_zero_signers(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_unavailable())
        _write_result(results_dir, "ci_gate", _ci_gate_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "nul geldige ondertekenaars" in verdict["message"]
        assert verdict.get("evidence_gate") != "ci_gate"


class TestGenuineReviewPeerStillSigns:
    """Scenario 2 (dispatch): codex absent, ci_gate PASS AND glm_gate PASS on
    the same head. glm_gate must be the signer — never ci_gate, even though
    'ci_gate' sorts alphabetically before 'glm_gate' (pre-fix, the peer loop's
    sorted-first-match-wins order made ci_gate win the race; that ordering
    bug is now moot because ci_gate is not a candidate at all)."""

    def test_glm_pass_signs_ci_gate_pass_does_not(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_unavailable())
        _write_result(results_dir, "ci_gate", _ci_gate_pass(report))
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["evidence_gate"] == "glm_gate"


class TestCiGateFailureDoesNotDoubleBlockThePeerRoute:
    """Scenario 3 (dispatch): codex absent, ci_gate FAIL, glm_gate PASS.

    Design decision (documented in closure_verifier._NON_REVIEW_SIGNER_REASONS):
    ci_gate is excluded from the peer route SYMMETRICALLY — it can neither
    sign a pass nor block via a fail there. A real CI failure on the head
    already blocks the merge through the separate, independent
    ``pr_merge._run_ci_gate`` check (a live gh statusCheckRollup query) before
    the review gate is ever reached; letting a stale ci_gate result-file FAIL
    also reject here would be double-counting the same fact, not added
    safety. This test pins that choice: the genuine review peer (glm_gate)
    still signs.
    """

    def test_ci_gate_fail_does_not_block_when_a_review_peer_passes(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_unavailable())
        _write_result(
            results_dir, "ci_gate",
            _ci_gate_pass(report, status="fail", blocking_count=1,
                          blocking_findings=[{"severity": "blocking", "message": "CI red"}]),
        )
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["evidence_gate"] == "glm_gate"


class TestTakeoverSuccessorMustAlsoBeAReviewGate:
    """Scenario 4 (dispatch): the OI-1576 takeover-successor route must apply
    the same review-only boundary. A ci_gate record that annotates a
    takeover_path naming codex_gate has still never reviewed the code — it
    must not be accepted as codex_gate's successor evidence."""

    def test_ci_gate_takeover_annotation_naming_codex_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_unavailable())
        _write_result(
            results_dir, "ci_gate",
            _ci_gate_pass(
                report,
                takeover=True,
                takeover_from="codex_gate",
                takeover_path=[{"gate": "codex_gate", "status": "unavailable"}],
            ),
        )

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert verdict.get("evidence_gate") != "ci_gate"


class TestReviewPeerGatesDerivedFromEnumNeverUnclassified:
    """Scenario 5 (dispatch): drift pin alongside
    test_closure_verifier_gate_enum_drift.py — every Gate enum member is
    EITHER a review peer OR carries an explicit exclusion reason. A gate
    added to the enum tomorrow with no disposition here would silently
    inherit (or silently lose) peer-signing rights; this test fails loud
    instead."""

    def test_every_enum_member_is_either_a_peer_or_excluded_with_reason(self):
        enum_values = {g.value for g in Gate}
        accounted = cv._REVIEW_PEER_GATES | set(cv._NON_REVIEW_SIGNER_REASONS)
        missing = enum_values - accounted
        assert not missing, (
            f"Gate enum members with no peer-signer disposition: {sorted(missing)}. "
            f"Add them to _REVIEW_PEER_GATES (if a real review) or to "
            f"_NON_REVIEW_SIGNER_REASONS (with a reason)."
        )

    def test_peer_gates_and_exclusions_are_disjoint(self):
        overlap = cv._REVIEW_PEER_GATES & set(cv._NON_REVIEW_SIGNER_REASONS)
        assert not overlap, (
            f"gates in both _REVIEW_PEER_GATES and _NON_REVIEW_SIGNER_REASONS: "
            f"{sorted(overlap)}"
        )

    def test_exclusions_are_a_subset_of_known_gates(self):
        """A gate excluded from peer-signing must still be a real enum
        member the closure verifier knows about (or wiring_gate, the one
        enum member _KNOWN_GATES itself excludes)."""
        enum_values = {g.value for g in Gate}
        assert set(cv._NON_REVIEW_SIGNER_REASONS).issubset(enum_values), (
            f"_NON_REVIEW_SIGNER_REASONS has values not in the Gate enum: "
            f"{sorted(set(cv._NON_REVIEW_SIGNER_REASONS) - enum_values)}"
        )

    def test_ci_gate_and_wiring_gate_are_excluded_with_reasons(self):
        assert "ci_gate" in cv._NON_REVIEW_SIGNER_REASONS
        assert cv._NON_REVIEW_SIGNER_REASONS["ci_gate"]
        assert "wiring_gate" in cv._NON_REVIEW_SIGNER_REASONS
        assert cv._NON_REVIEW_SIGNER_REASONS["wiring_gate"]
        assert "ci_gate" not in cv._REVIEW_PEER_GATES
        assert "wiring_gate" not in cv._REVIEW_PEER_GATES

    def test_claude_github_optional_remains_a_review_peer(self):
        """It IS optional (may never run), but when it renders a decided
        verdict that verdict is a genuine Claude code review, not a CI
        signal — it must stay a valid peer signer."""
        assert "claude_github_optional" in cv._REVIEW_PEER_GATES
        assert "claude_github_optional" not in cv._NON_REVIEW_SIGNER_REASONS
