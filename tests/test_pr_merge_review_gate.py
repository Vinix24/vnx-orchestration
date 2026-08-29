#!/usr/bin/env python3
"""Tests for the review-gate merge check (dispatch-20260816-gate-never-skippable).

The merge door (``pr_merge``) now runs a second fail-closed check after the CI
gate: a passing, fully-evidenced review-gate result must exist for the PR
before ``merge_pr`` runs. These tests cover the two layers:

  - ``closure_verifier.check_review_gate_for_merge`` — the pure evidence check
    (result exists, terminal, complete evidence, pass, no contradiction).
  - ``pr_merge._run_review_gate`` — resolution (PR number -> declared gate ->
    result key), the override valve, and the ``main()`` wiring (refuse before
    merge).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier
import pr_merge


def _write_result(results_dir: Path, pr_id: str, gate: str, data: dict) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    slug = pr_id.lower().replace("-", "")
    path = results_dir / f"{slug}-{gate}-contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _result(report_file: Path, **overrides) -> dict:
    data = {
        "gate": "codex_gate",
        "pr_id": "PR-42",
        "status": "completed",
        "blocking_count": 0,
        "contract_hash": "abcdef1234567890",
        "report_path": str(report_file),
        "branch": "feature/x",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Pure evidence check — closure_verifier.check_review_gate_for_merge
# ---------------------------------------------------------------------------


class TestCheckReviewGateForMerge:
    def test_missing_result_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        gate = closure_verifier.check_review_gate_for_merge(
            "PR-42", "codex_gate", results_dir, branch="feature/x"
        )
        assert gate["verdict"] == "NO-GO"
        assert "geen review-gate resultaat" in gate["message"]

    def test_empty_contract_hash_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = tmp_path / "report.md"
        report.write_text("All checks passed.\n", encoding="utf-8")
        _write_result(
            results_dir, "PR-42", "codex_gate",
            _result(report, contract_hash=""),
        )

        gate = closure_verifier.check_review_gate_for_merge(
            "PR-42", "codex_gate", results_dir, branch="feature/x"
        )

        assert gate["verdict"] == "NO-GO"
        assert "contract_hash" in gate["message"]

    def test_empty_report_path_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        _write_result(
            results_dir, "PR-42", "codex_gate",
            _result(Path("/nonexistent/report.md"), report_path=""),
        )

        gate = closure_verifier.check_review_gate_for_merge(
            "PR-42", "codex_gate", results_dir, branch="feature/x"
        )

        assert gate["verdict"] == "NO-GO"
        assert "report_path" in gate["message"]

    def test_non_terminal_result_is_no_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = tmp_path / "report.md"
        report.write_text("in flight\n", encoding="utf-8")
        _write_result(
            results_dir, "PR-42", "codex_gate",
            _result(report, status="pending"),
        )

        gate = closure_verifier.check_review_gate_for_merge(
            "PR-42", "codex_gate", results_dir, branch="feature/x"
        )

        assert gate["verdict"] == "NO-GO"
        assert "niet terminaal" in gate["message"]

    def test_passing_complete_result_is_go(self, tmp_path):
        results_dir = tmp_path / "results"
        report = tmp_path / "report.md"
        report.write_text("All checks passed.\n", encoding="utf-8")
        _write_result(results_dir, "PR-42", "codex_gate", _result(report))

        gate = closure_verifier.check_review_gate_for_merge(
            "PR-42", "codex_gate", results_dir, branch="feature/x"
        )

        assert gate["verdict"] == "GO"
        assert gate["overridden"] is False


# ---------------------------------------------------------------------------
# B5: the real writer (gate_artifacts) + the real merge check — no fabricated data
# ---------------------------------------------------------------------------


class TestRealWriterThroughRealMergeCheck:
    """B7: a result produced by the REAL writer chain must satisfy the REAL
    merge check — with no fabricated identity. The request handler resolves the
    PR head sha from GitHub (``gh pr view --json headRefOid``), then
    ``gate_artifacts.materialize_artifacts`` stamps branch + commit_sha from
    that request. The gh call is faked once at the subprocess boundary so the
    PR has a known headRefOid. If the writer resolved the sha from the local
    checkout HEAD instead, the first test must go RED: the merge check compares
    against headRefOid, so a local-HEAD sha can never match."""

    KNOWN_HEAD_OID = "e4f3c2b1a09876543210fedcba09876543210"

    def _fake_gh_pr_view_head(self, monkeypatch):
        import gate_recorder

        real_run = gate_recorder.subprocess.run

        def fake_run(argv, **kwargs):
            if (
                len(argv) >= 5
                and argv[:3] == ["gh", "pr", "view"]
                and argv[3] == "42"
                and "--json" in argv
                and "headRefOid" in argv
            ):
                return subprocess.CompletedProcess(
                    argv, 0, stdout=json.dumps({"headRefOid": self.KNOWN_HEAD_OID})
                )
            return real_run(argv, **kwargs)

        monkeypatch.setattr(gate_recorder.subprocess, "run", fake_run)

    def _materialize_via_real_writer(self, tmp_path, monkeypatch):
        import review_gate_manager as rgm
        from gate_artifacts import materialize_artifacts

        self._fake_gh_pr_view_head(monkeypatch)

        # The production writer: _request_gemini resolves the sha via
        # get_pr_head_sha (gh pr view headRefOid), not git rev-parse HEAD.
        manager = rgm.ReviewGateManager()
        monkeypatch.setattr(manager, "_gemini_available", lambda: True)
        request_payload = manager._request_gemini(
            42, "feature/x", "low", ["scripts/foo.py"], "per_pr", "d-real-writer-1",
        )

        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        reports_dir = tmp_path / "reports"
        for d in (results_dir, requests_dir, reports_dir):
            d.mkdir(parents=True, exist_ok=True)
        request_payload["report_path"] = str(reports_dir / "gemini_review-report.md")

        materialize_artifacts(
            gate="gemini_review",
            pr_number=42,
            pr_id="PR-42",
            stdout="Review complete.\nFindings: none.\nApproved.",
            request_payload=request_payload,
            duration_seconds=1.0,
            requests_dir=requests_dir,
            results_dir=results_dir,
            reports_dir=reports_dir,
        )
        return request_payload, results_dir

    def test_real_writer_result_passes_real_merge_check(self, tmp_path, monkeypatch):
        request_payload, results_dir = self._materialize_via_real_writer(tmp_path, monkeypatch)

        # The writer must have resolved the sha from GitHub, not the local HEAD.
        assert request_payload["commit_sha"] == self.KNOWN_HEAD_OID

        gate = closure_verifier.check_review_gate_for_merge(
            "PR-42", "gemini_review", results_dir,
            branch="feature/x",
            head_sha=self.KNOWN_HEAD_OID,
        )

        assert gate["verdict"] == "GO"

    def test_real_writer_result_with_wrong_head_sha_is_no_go(self, tmp_path, monkeypatch):
        _, results_dir = self._materialize_via_real_writer(tmp_path, monkeypatch)

        gate = closure_verifier.check_review_gate_for_merge(
            "PR-42", "gemini_review", results_dir,
            branch="feature/x",
            head_sha="9999999999999999999999999999999999999999",
        )

        assert gate["verdict"] == "NO-GO"
        assert "review-gate resultaat" in gate["message"]


# ---------------------------------------------------------------------------
# pr_id normalization + obligation lookup — the join key of the merge gate
# ---------------------------------------------------------------------------


def test_norm_pr_id_shapes():
    assert pr_merge._norm_pr_id("PR-879") == "879"
    assert pr_merge._norm_pr_id("pr879") == "879"
    assert pr_merge._norm_pr_id("879") == "879"
    assert pr_merge._norm_pr_id("PR-HYG-1") == "HYG-1"
    assert pr_merge._norm_pr_id("") == ""
    assert pr_merge._norm_pr_id(None) == ""


def test_resolve_declared_gate_matches_by_pr_number(tmp_path):
    from gate_obligations import register_obligation

    # The runner stamps pr_number on the obligation it fulfils — the primary
    # merge-time join key.
    register_obligation(tmp_path, dispatch_id="d1", gate="codex_gate", pr_id="PR-42", pr_number=42)

    assert pr_merge._resolve_declared_gate(42, state_dir=tmp_path) == "codex_gate"
    assert pr_merge._resolve_declared_gate(99, state_dir=tmp_path) == ""


def test_resolve_declared_gate_matches_by_normalized_pr_id(tmp_path):
    from gate_obligations import register_obligation

    # A numeric pr_id with no pr_number yet still joins: "PR-42" normalizes to
    # the same number as the GitHub PR.
    register_obligation(tmp_path, dispatch_id="d1", gate="codex_gate", pr_id="PR-42")

    assert pr_merge._resolve_declared_gate(42, state_dir=tmp_path) == "codex_gate"
    assert pr_merge._resolve_declared_gate(99, state_dir=tmp_path) == ""


def test_resolve_declared_gate_skips_no_gate_key(tmp_path):
    from gate_obligations import register_no_gate_obligation

    register_no_gate_obligation(tmp_path, dispatch_id="d1", pr_id="PR-42", pr_number=42)

    assert pr_merge._resolve_declared_gate(42, state_dir=tmp_path) == ""


# ---------------------------------------------------------------------------
# Resolution + override — pr_merge._run_review_gate
# ---------------------------------------------------------------------------


class TestRunReviewGate:
    def test_no_go_when_pr_unresolvable(self, monkeypatch):
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: None)
        gate, data = pr_merge._run_review_gate(5)
        assert gate["verdict"] == "NO-GO"
        assert data is None
        assert "niet toetsbaar" in gate["message"]

    def test_no_go_when_no_declared_gate(self, monkeypatch):
        monkeypatch.setattr(
            pr_merge, "_query_pr",
            lambda n: {"title": "add a thing", "headRefName": "feature/x",
                      "headRefOid": "6c925a1b2ee8b0cded8728d4f2c792fcf64be4d4"},
        )
        monkeypatch.setattr(pr_merge, "_resolve_declared_gate", lambda pr_number, state_dir=None: "")
        gate, _ = pr_merge._run_review_gate(5)
        assert gate["verdict"] == "NO-GO"
        assert "geen review-gate-verplichting" in gate["message"]

    def test_delegates_to_evidence_check(self, monkeypatch, tmp_path):
        # A PR title without any internal PR-N label still joins: the result
        # key is the bare GitHub PR number, not the title.
        monkeypatch.setattr(
            pr_merge, "_query_pr",
            lambda n: {"title": "no internal label here", "headRefName": "feature/x",
                       "headRefOid": "6c925a1b2ee8b0cded8728d4f2c792fcf64be4d4"},
        )
        monkeypatch.setattr(pr_merge, "ensure_env", lambda: {"VNX_STATE_DIR": str(tmp_path)})
        monkeypatch.setattr(pr_merge, "_resolve_declared_gate", lambda pr_number, state_dir=None: "codex_gate")
        seen = {}

        def fake_check(pr_id, gate, results_dir, *, branch=None, project_id=None, head_sha=None):
            seen["pr_id"] = pr_id
            seen["gate"] = gate
            seen["branch"] = branch
            seen["head_sha"] = head_sha
            return {
                "verdict": "GO",
                "message": f"{gate} resultaat aanwezig en passing voor {pr_id}",
                "overridden": False,
                "override_reason": None,
                "gate": gate,
            }

        monkeypatch.setattr(closure_verifier, "check_review_gate_for_merge", fake_check)

        gate, _ = pr_merge._run_review_gate(5)

        assert gate["verdict"] == "GO"
        assert seen == {
            "pr_id": "5",
            "gate": "codex_gate",
            "branch": "feature/x",
            # OI-1318: this used to assert head_sha="" with the note "mocked PR
            # data carries no headRefOid". That empty string was the defect
            # written down as an expectation: downstream it read as "no sha
            # constraint", so the one path that could not resolve its head was
            # the path that stopped requiring one. The door now refuses before
            # delegating, and a delegation carries a real head.
            "head_sha": "6c925a1b2ee8b0cded8728d4f2c792fcf64be4d4",
        }

    def test_override_does_not_bypass_an_undeterminable_head(self, monkeypatch):
        """OI-1318, and a deliberate narrowing of the override.

        The escape hatch skips the EVIDENCE check; it does not make an
        unmergeable state mergeable. With no head there is nothing to merge
        against — ``_do_merge`` cannot pass ``--match-head-commit`` either — so
        the refusal comes first, exactly as it already does in the sibling
        ``_run_ci_gate``, which returns NO-GO on an empty head before its own
        override reaches the check. Symmetry between the two gates is the whole
        point of OI-1318, and it has to hold on this path too or the asymmetry
        simply moves.
        """
        monkeypatch.setattr(
            pr_merge, "_query_pr",
            lambda n: {"title": "PR-42 add a thing", "headRefName": "feature/x",
                       "headRefOid": ""},
        )
        gate, _ = pr_merge._run_review_gate(5, override_reason="hotfix: verified by hand")
        assert gate["verdict"] == "NO-GO"
        assert "niet toetsbaar" in gate["message"]

    def test_override_empty_reason_refused(self, monkeypatch):
        monkeypatch.setattr(
            pr_merge, "_query_pr",
            lambda n: {"title": "PR-42 add a thing", "headRefName": "feature/x",
                      "headRefOid": "6c925a1b2ee8b0cded8728d4f2c792fcf64be4d4"},
        )
        gate, _ = pr_merge._run_review_gate(5, override_reason="   ")
        assert gate["verdict"] == "NO-GO"
        assert gate["overridden"] is True
        assert "override zonder reden" in gate["message"]

    def test_override_nonempty_reason_goes(self, monkeypatch):
        monkeypatch.setattr(
            pr_merge, "_query_pr",
            lambda n: {"title": "PR-42 add a thing", "headRefName": "feature/x",
                      "headRefOid": "6c925a1b2ee8b0cded8728d4f2c792fcf64be4d4"},
        )
        gate, _ = pr_merge._run_review_gate(5, override_reason="hotfix: re-verified by hand")
        assert gate["verdict"] == "GO"
        assert gate["overridden"] is True
        assert "OVERRIDE" in gate["message"]


# ---------------------------------------------------------------------------
# main() wiring — review gate runs after the CI gate, refuses before merge
# ---------------------------------------------------------------------------


class TestMainReviewGateWiring:
    def _ok_result(self):
        return {
            "success": True, "pr_number": 5, "dispatch_id": "", "merge_method": "squash",
            "pr_title": "", "branch": "", "receipt_status": None, "register_ok": False,
            "error": "", "dry_run": True, "overlaps": [],
        }

    def test_review_gate_no_go_refuses_after_ci_go(self, monkeypatch, capsys):
        merge_called = []
        monkeypatch.setattr(
            pr_merge, "_run_ci_gate",
            lambda pr, **k: ({"verdict": "GO", "message": "CI ok", "overridden": False, "override_reason": None}, None),
        )
        monkeypatch.setattr(
            pr_merge, "_run_review_gate",
            lambda pr, **k: ({"verdict": "NO-GO", "message": "geen review-gate resultaat", "overridden": False, "override_reason": None, "gate": None}, None),
        )
        monkeypatch.setattr(pr_merge, "merge_pr", lambda **k: merge_called.append(1) or self._ok_result())

        rc = pr_merge.main(["--pr", "5", "--dry-run"])

        assert rc == pr_merge.EXIT_ERROR
        assert not merge_called, "merge_pr must not run when the review gate is NO-GO"
        assert "NO-GO" in capsys.readouterr().err

    def test_both_gates_go_proceeds_to_merge(self, monkeypatch, capsys):
        merge_called = []
        go = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(go), None))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (dict(go), None))
        monkeypatch.setattr(pr_merge, "merge_pr", lambda **k: merge_called.append(1) or self._ok_result())

        rc = pr_merge.main(["--pr", "5", "--dry-run"])

        assert rc == pr_merge.EXIT_OK
        assert merge_called, "merge_pr must run when both gates are GO"
        assert "Review gate" in capsys.readouterr().out
