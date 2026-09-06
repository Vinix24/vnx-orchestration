#!/usr/bin/env python3
"""OI-1642: the peer route must also open when the declared gate's OWN record
is not found IN SCOPE at all (``_find_gate_result`` returns ``None``), as long
as a record for that exact gate+pr_id exists somewhere OUTSIDE the requested
branch/project_id/head_sha scope and that out-of-scope record is itself a
confirmed absence.

Measured live on PR #1777 (vnx-dev store, 2026-09-06, main 7b1fef53 — after
the OI-1624 fix landed in #1783): ``pr-1777-codex_gate.json`` is a terminal
``not_executable`` record written 2026-09-05T20:20:41Z, BEFORE #1783 threaded
``branch``/``commit_sha`` through the writer — it carries neither field.
``_find_gate_result("codex_gate", ..., branch=..., head_sha=...)`` therefore
returns ``None`` (``_record_matches_scope`` rejects a record missing a
required field unconditionally), even though the record obviously exists and
is a confirmed absence. ``pr-1777-glm_gate.json`` independently carries a full
PASS on the exact head sha (``gh pr view 1777 --json headRefOid``). Because
``result is None``, the OI-1624 peer route (guarded by
``result is not None and _is_absent_without_verdict(result)``) was never
reached, and ``python3 scripts/pr_merge.py --pr 1777 --squash --dry-run``
returned NO-GO: "geen review-gate resultaat gevonden voor codex_gate op 1777".
The record is invisible to the reader (fails scope) and terminal for the
writer (the overwrite guard, OI-1469/OI-1470, permanently refuses to rerun a
terminal record with a non-terminal reassessment) — a dead end with no
upstream fix possible for records already on disk.

The fix (``closure_verifier._find_gate_result_ignoring_scope`` +
``_consult_peers_for_absence``) distinguishes three cases where
``_find_gate_result`` returns ``None``:

  (a) NO record at all exists for this gate+pr_id — stays NO-GO, UNCHANGED.
      This is ``TestUnrelatedRecordsNeverTakeOver``'s contract
      (test_oi1576_merge_door_takeover_evidence.py) and MUST NOT be touched;
      pinned again here as a regression guard specific to this fix's own
      code path.
  (b) a record exists but falls outside scope AND is itself a confirmed
      absence (``_is_absent_without_verdict``) — the SAME peer route as
      OI-1624 opens, via the shared ``_consult_peers_for_absence``.
  (c) a record exists outside scope but carries a DECIDED verdict (pass/fail
      on a stale branch/sha) — stale evidence, stays NO-GO with the existing
      "no evidence found" message, never treated as an absence.

Measured (see this file's own test run in the dispatch report): patching the
peer route to fire UNCONDITIONALLY whenever ``result is None`` — without the
(a)/(b)/(c) split — breaks
``TestUnrelatedRecordsNeverTakeOver::test_plain_pass_for_other_gate_is_no_go``
and ``::test_takeover_path_without_declared_gate_is_no_go`` (both flip from
NO-GO to GO on an unrelated stranger's pass). That is exactly the scenario
case (a) must keep refusing: a gate that was never even asked for this PR
must never accept a stranger's unrelated pass as a signer.
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

# Mirrored from the live vnx-dev store shape for PR #1777 (2026-09-05/06).
PR_ID = "1777"
BRANCH = "dispatch/20260905-golf1b-report-store-split"
HEAD_SHA = "01f54411d975e39f41cb2b0d005fd8dcd5aad04a"
OTHER_SHA = "99999999975e39f41cb2b0d005fd8dcd5aad999"

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


def _codex_not_executable_out_of_scope() -> dict:
    """pr-1777-codex_gate.json exactly as measured live: terminal
    not_executable, but written BEFORE OI-1624 threaded branch/commit_sha
    through the writer — carries neither field at all."""
    return {
        "gate": "codex_gate",
        "pr_id": PR_ID,
        "status": "not_executable",
        "reason": "provider_not_installed",
        "reason_detail": "codex binary not found in PATH",
        "contract_hash": "",
        "report_path": "",
        # deliberately no "branch", no "commit_sha" — the measured live shape.
    }


def _glm_pass(report: Path, **overrides) -> dict:
    """pr-1777-glm_gate.json as measured: a plain, unlinked pass on the head."""
    data = {
        "gate": "glm_gate",
        "pr_id": PR_ID,
        "status": "pass",
        "blocking_count": 0,
        "blocking_findings": [],
        "contract_hash": "549c0288ef98004e",
        "report_path": str(report),
        "branch": BRANCH,
        "commit_sha": HEAD_SHA,
    }
    data.update(overrides)
    return data


def _check(results_dir: Path, gate: str = "codex_gate", head_sha: str = HEAD_SHA) -> dict:
    return cv.check_review_gate_for_merge(
        PR_ID, gate, results_dir, branch=BRANCH, head_sha=head_sha
    )


class TestOutOfScopeAbsenceAcceptsPeerVerdict:
    """The live #1777 shape: codex_gate's only on-disk record fails the scope
    match entirely (no branch/commit_sha field at all), so
    ``_find_gate_result`` returns None — but the record IS a confirmed
    absence, and glm_gate independently passed the exact head."""

    def test_1_out_of_scope_absent_declared_gate_with_peer_pass_is_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_not_executable_out_of_scope())
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["gate"] == "codex_gate"
        assert verdict["evidence_gate"] == "glm_gate"
        assert "afwezig" in verdict["message"]
        assert "buiten scope" in verdict["message"]

    def test_2_out_of_scope_absent_declared_gate_with_peer_fail_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_not_executable_out_of_scope())
        _write_result(
            results_dir, "glm_gate",
            _glm_pass(report, status="fail", blocking_count=1,
                      blocking_findings=[{"severity": "blocking", "message": "real defect"}]),
        )

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO", verdict["message"]

    def test_3_out_of_scope_absent_declared_gate_with_missing_report_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        missing_report = tmp_path / "does-not-exist.md"
        _write_result(results_dir, "codex_gate", _codex_not_executable_out_of_scope())
        _write_result(results_dir, "glm_gate", _glm_pass(missing_report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO", verdict["message"]

    def test_4_out_of_scope_absent_declared_gate_with_peer_on_other_sha_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_not_executable_out_of_scope())
        _write_result(results_dir, "glm_gate", _glm_pass(report, commit_sha=OTHER_SHA))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO", verdict["message"]


class TestCaseADistinctFromCaseB:
    """Case (a) — no record at all for this gate+pr_id — must stay
    unchanged: no peer fallback, per TestUnrelatedRecordsNeverTakeOver."""

    def test_5_no_codex_record_at_all_with_peer_pass_stays_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "geen review-gate resultaat" in verdict["message"]


class TestCaseCStaleVerdictNeverReadAsAbsence:
    """Case (c) — an out-of-scope record that DID render a verdict (pass on a
    different sha) is stale evidence, not an absence, and must not unlock the
    peer route either."""

    def test_6_out_of_scope_codex_pass_on_other_sha_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        codex_pass_stale = {
            "gate": "codex_gate",
            "pr_id": PR_ID,
            "status": "pass",
            "blocking_count": 0,
            "blocking_findings": [],
            "contract_hash": "aaaaaaaaaaaaaaaa",
            "report_path": str(report),
            "branch": BRANCH,
            "commit_sha": OTHER_SHA,
        }
        _write_result(results_dir, "codex_gate", codex_pass_stale)
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "geen review-gate resultaat" in verdict["message"]


class TestTestRunRecordNeverCountsAsOutOfScopeEvidence:
    """A test_run: true record for codex_gate must be treated exactly like
    case (a) — no record at all — never as a confirmed out-of-scope absence."""

    def test_7_out_of_scope_codex_test_run_record_stays_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        codex_test_run = _codex_not_executable_out_of_scope()
        codex_test_run["test_run"] = True
        _write_result(results_dir, "codex_gate", codex_test_run)
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "geen review-gate resultaat" in verdict["message"]


class TestRealRejectionStillWinsOverPassThroughTheNewRoute:
    """Harde grens, replicated for the OI-1642 out-of-scope path: a real peer
    rejection blocks the merge even next to another peer's pass."""

    def test_8_two_peers_fail_wins_over_pass(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_not_executable_out_of_scope())
        _write_result(
            results_dir, "kimi_gate",
            {
                "gate": "kimi_gate", "pr_id": PR_ID, "status": "fail",
                "blocking_count": 1,
                "blocking_findings": [{"severity": "blocking", "message": "real defect"}],
                "contract_hash": "aa11bb22cc33dd44", "report_path": str(report),
                "branch": BRANCH, "commit_sha": HEAD_SHA,
            },
        )
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
