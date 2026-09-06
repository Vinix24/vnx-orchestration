#!/usr/bin/env python3
"""OI-1624: a gate UITVAL (not_executable/unavailable) is an ABSENCE, never a
rejection — and absence must never silently become "nul geldige
ondertekenaars" when another gate, dispatched in the SAME review round
against the SAME commit, already rendered its own valid verdict.

Measured live on PR #1777 (vnx-dev store, 2026-09-05/06): the declared gate
(``codex_gate``, from the door's obligation) carries a TERMINAL
``not_executable`` record (``reason=provider_not_installed`` — codex is not
installed in this environment, a permanent condition, not a transient
outage). ``glm_gate`` — dispatched in the same review round — independently
recorded a full, evidenced PASS on the exact same head sha, with NO
``takeover``/``takeover_path`` annotation linking the two records (the
OI-1576 chain-walk was never invoked; both gates were requested directly as
part of the review stack). Before this fix, ``check_review_gate_for_merge``
fed the absent ``codex_gate`` record straight into
``_merge_door_record_verdict`` (the machinery that judges "is this a valid
pass"), which read the missing ``contract_hash``/``report_path`` as
"bewijs onvolledig" — indistinguishable from a botched review attempt — and
returned NO-GO with no route out: OI-1576's takeover-successor evidence
route requires a formal ``takeover_path`` annotation, which this PR's
records never carried.

The fix distinguishes "terminal" (this attempt concluded) from "a verdict
was rendered" (pass or fail) as two independent axes
(``closure_verifier._is_absent_without_verdict``), and — only once the
declared gate is confirmed absent by that test, with no formal takeover
successor either — consults every OTHER known gate's own rendered verdict
for the exact same PR/branch/sha (``closure_verifier._find_peer_gate_results``).

Hard invariants that must survive untouched:
  - zero valid signers (no peer renders a usable pass either) stays NO-GO.
  - a real rejection (fail) anywhere in the peer set still blocks the merge,
    even next to another peer's pass.
  - the seven evidence invariants (record exists, terminal, contract_hash,
    report_path, report exists on disk, verdicts do not contradict, sha
    equals the head) still apply in full to whichever peer ends up signing.
  - OI-1576's own contract (a declared gate with NO record at all is never
    handed a stranger's unrelated pass) is untouched — see
    ``tests/test_oi1576_merge_door_takeover_evidence.py::TestUnrelatedRecordsNeverTakeOver``,
    which this file does not modify and must keep passing unchanged.

A SECOND, upstream defect surfaced while proving this live against PR #1777:
its on-disk ``codex_gate`` not_executable record carries neither ``branch``
nor ``commit_sha`` at all (``gate_request_handler._mark_gate_unavailable`` ->
``gate_report_generator._write_not_executable_result`` never threaded them
through, even though every caller — ``_request_codex`` etc. — already
resolves both before deciding the gate is unavailable). A record missing
those fields fails ``closure_verifier``'s scope match unconditionally and
reads as NO RECORD AT ALL, so the absence-vs-rejection fix above never even
gets to see it. ``TestNotExecutableRecordCarriesScope`` below covers that
fix end-to-end through the real ``request_reviews`` -> ``_request_codex`` ->
``_mark_gate_unavailable`` -> ``_write_not_executable_result`` chain.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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


def _codex_not_executable() -> dict:
    """pr-1777-codex_gate.json as measured: terminal not_executable, no
    takeover annotation, no evidence fields — codex is not installed."""
    return {
        "gate": "codex_gate",
        "pr_id": PR_ID,
        "status": "not_executable",
        "reason": "provider_not_installed",
        "reason_detail": "codex binary not found in PATH",
        "contract_hash": "",
        "report_path": "",
        "branch": BRANCH,
        "commit_sha": HEAD_SHA,
    }


def _glm_pass(report: Path, **overrides) -> dict:
    """pr-1777-glm_gate.json as measured: a plain, unlinked pass — no
    takeover claim of any kind."""
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


class TestAbsentDeclaredGateAcceptsPeerVerdict:
    """The live #1777 shape: declared codex_gate is a confirmed, terminal
    absence; glm_gate independently passed the exact same head — no formal
    takeover linkage between the two records at all."""

    def test_terminal_not_executable_with_peer_pass_is_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_not_executable())
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["gate"] == "codex_gate"
        assert verdict["evidence_gate"] == "glm_gate"
        assert "afwezig" in verdict["message"]

    def test_non_terminal_unavailable_with_peer_pass_is_also_go(self, tmp_path):
        """OI-1624: absence applies "ongeacht of het record terminaal is
        opgeslagen" — a non-terminal ``unavailable`` declared-gate record
        must be treated identically to a terminal ``not_executable`` one."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        codex = _codex_not_executable()
        codex["status"] = "unavailable"
        codex["reason"] = "dispatch_error"
        _write_result(results_dir, "codex_gate", codex)
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["evidence_gate"] == "glm_gate"


class TestGuardStillHoldsForThePeerRoute:
    """Breaking the new peer route three separate ways — each must still
    refuse on its own, exactly like the declared gate's own evidence would."""

    def test_peer_pass_on_a_different_sha_than_head_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_not_executable())
        _write_result(results_dir, "glm_gate", _glm_pass(report, commit_sha=OTHER_SHA))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "nul geldige ondertekenaars" in verdict["message"]

    def test_peer_pass_without_contract_hash_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_not_executable())
        _write_result(results_dir, "glm_gate", _glm_pass(report, contract_hash=""))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "nul geldige ondertekenaars" in verdict["message"]

    def test_peer_pass_with_nonexistent_report_path_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        missing_report = tmp_path / "does-not-exist.md"
        _write_result(results_dir, "codex_gate", _codex_not_executable())
        _write_result(results_dir, "glm_gate", _glm_pass(missing_report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "nul geldige ondertekenaars" in verdict["message"]

    def test_real_peer_fail_still_blocks_next_to_another_peers_pass(self, tmp_path):
        """Harde grens: een echte afkeuring blijft blokkeren, ook naast een
        pass van een andere poort."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_not_executable())
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

        assert verdict["verdict"] == "NO-GO"
        assert "kimi_gate" in verdict["message"]
        assert "afkeuring" in verdict["message"]


class TestZeroValidSignersStaysNoGo:
    def test_absent_declared_gate_with_no_peer_evidence_at_all_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        _write_result(results_dir, "codex_gate", _codex_not_executable())

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "nul geldige ondertekenaars" in verdict["message"]
        assert "afwezig" in verdict["message"]

    def test_absent_declared_gate_with_only_incomplete_peers_is_no_go(self, tmp_path):
        """A peer that is itself still in flight (pending) never counts —
        only a RENDERED verdict (pass/fail) is peer evidence."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _codex_not_executable())
        _write_result(
            results_dir, "kimi_gate",
            {
                "gate": "kimi_gate", "pr_id": PR_ID, "status": "pending",
                "contract_hash": "", "report_path": str(report),
                "branch": BRANCH, "commit_sha": HEAD_SHA,
            },
        )

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "nul geldige ondertekenaars" in verdict["message"]


class TestInFlightDeclaredGateIsNotAbsence:
    """OI-1624 deliberately excludes INCOMPLETE_STATES (pending/running/
    queued/requested) from "absence": the SAME record may still mature into
    a verdict, so consulting a peer while it is still in flight would be
    premature. This must keep going through the ORIGINAL "niet terminaal"
    path, unchanged (see test_pr_merge_review_gate.py::test_non_terminal_result_is_no_go)."""

    def test_pending_declared_gate_with_peer_pass_stays_not_terminal_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        codex = _codex_not_executable()
        codex["status"] = "pending"
        _write_result(results_dir, "codex_gate", codex)
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "niet terminaal" in verdict["message"]


class TestNoRegressionOnZeroRecordAbsence:
    """The pre-existing OI-1576 contract must survive byte-for-byte: a
    declared gate with NO record at all (never even asked) is never handed a
    stranger's unrelated pass. This mirrors
    test_oi1576_merge_door_takeover_evidence.py::TestUnrelatedRecordsNeverTakeOver
    exactly, as an explicit regression pin for THIS fix."""

    def test_no_record_at_all_with_unrelated_peer_pass_stays_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "geen review-gate resultaat" in verdict["message"]


@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    """Same isolated-store fixture shape as test_gate_request_handler_w3f.py."""
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")
    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


class TestNotExecutableRecordCarriesScope:
    """OI-1624, second defect: a not_executable RESULT record must carry the
    same ``branch``/``commit_sha`` the caller already resolved for the
    REQUEST record — measured live on PR #1777's ``codex_gate`` record,
    which had neither and was therefore invisible to
    ``closure_verifier``'s scope match (reads as "no record", not as a
    confirmed absence). Driven through the REAL production chain:
    ``ReviewGateManager.request_reviews`` -> ``_request_codex`` ->
    ``_mark_gate_unavailable`` -> ``_write_not_executable_result``.
    """

    def test_codex_not_executable_result_carries_branch_and_commit_sha(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        import gate_request_handler
        fake_head_oid = "01f54411d975e39f41cb2b0d005fd8dcd5aad04a"
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: fake_head_oid)

        import review_gate_manager as rgm
        manager = rgm.ReviewGateManager()

        with patch("governance_receipts.emit_governance_receipt"):
            manager.request_reviews(
                pr_number=1777,
                branch="dispatch/20260905-golf1b-report-store-split",
                review_stack=["codex_gate"],
                risk_class="low",
                changed_files=["scripts/foo.py"],
                mode="per_pr",
                dispatch_id="test-oi1624-codex-scope",
            )

        result_file = manager_env["results_dir"] / "pr-1777-codex_gate.json"
        assert result_file.exists()
        record = json.loads(result_file.read_text())

        assert record["status"] == "not_executable"
        assert record["branch"] == "dispatch/20260905-golf1b-report-store-split"
        assert record["commit_sha"] == fake_head_oid

        # And the closure_verifier merge door can now find it as a genuine,
        # scoped absence rather than reading it as "no record at all".
        found = cv._find_gate_result(
            "codex_gate", "1777", manager_env["results_dir"],
            branch="dispatch/20260905-golf1b-report-store-split",
            head_sha=fake_head_oid,
        )
        assert found is not None, "the not_executable record must be scope-matchable, not invisible"
        assert cv._is_absent_without_verdict(found) is True
