#!/usr/bin/env python3
"""Tests for the merge gate wired into pr_merge.py (OI-1216 merge-gate).

The CLI (``pr_merge.main``) runs a fail-closed VNX CI-workflow-conclusion check
against the exact PR head SHA before delegating to ``merge_pr``. These tests
cover the ``_run_ci_gate`` helper and the ``main()`` wiring: GO proceeds, NO-GO
refuses before merge, an unresolvable PR head refuses (never a silent pass),
and the override is loud.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import pr_merge


SHA = "a" * 40


def _go_gate(**kw):
    gate = {
        "verdict": "GO",
        "message": "VNX CI geslaagd op aaaaaaaaaaaa",
        "ci_conclusion": "success",
        "ran_on_sha": True,
        "head_sha": SHA,
        "ci_run_id": 1,
        "workflow_name": "VNX CI",
        "overridden": False,
        "override_reason": None,
    }
    gate.update(kw)
    return gate


def _no_go_gate(message="Geen VNX CI-run gevonden: deze merge is niet toetsbaar"):
    return {
        "verdict": "NO-GO",
        "message": message,
        "overridden": False,
        "override_reason": None,
    }


class TestRunCiGate:
    def test_go_uses_pr_head_and_branch(self, monkeypatch):
        """A resolvable PR head is passed to the check verbatim (exact head SHA)."""
        pr_data = {"number": 5, "headRefOid": SHA, "headRefName": "feature/x"}
        seen = {}
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: pr_data)

        def fake_check(project_root, *, branch, head_sha, override_reason=None):
            seen["branch"] = branch
            seen["head_sha"] = head_sha
            seen["override_reason"] = override_reason
            return _go_gate()

        monkeypatch.setattr(pr_merge, "check_ci_run_for_head", fake_check)

        gate, data = pr_merge._run_ci_gate(5)

        assert gate["verdict"] == "GO"
        assert data is pr_data
        assert seen["head_sha"] == SHA
        assert seen["branch"] == "feature/x"
        assert seen["override_reason"] is None

    def test_no_go_when_ci_red(self, monkeypatch):
        """A failing workflow conclusion is a NO-GO, not a silent pass."""
        monkeypatch.setattr(
            pr_merge, "_query_pr",
            lambda n: {"headRefOid": SHA, "headRefName": "feature/x"},
        )
        monkeypatch.setattr(
            pr_merge, "check_ci_run_for_head",
            lambda *a, **k: _no_go_gate("VNX CI conclusion is 'failure' op aaaaaaaa"),
        )

        gate, _ = pr_merge._run_ci_gate(5)

        assert gate["verdict"] == "NO-GO"
        assert "failure" in gate["message"]

    def test_refuses_when_head_unresolvable(self, monkeypatch):
        """An unresolvable PR head is a refusal; the check never runs."""
        called = []
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: None)

        def fake_check(*a, **k):
            called.append(1)
            return _go_gate()

        monkeypatch.setattr(pr_merge, "check_ci_run_for_head", fake_check)

        gate, _ = pr_merge._run_ci_gate(5)

        assert gate["verdict"] == "NO-GO"
        assert "niet toetsbaar" in gate["message"]
        assert not called, "check must not run when the PR head is unresolvable"

    def test_forwards_override_reason(self, monkeypatch):
        """A non-empty override reason reaches the check and surfaces as overridden."""
        monkeypatch.setattr(
            pr_merge, "_query_pr",
            lambda n: {"headRefOid": SHA, "headRefName": "feature/x"},
        )
        seen = {}

        def fake_check(project_root, *, branch, head_sha, override_reason=None):
            seen["override_reason"] = override_reason
            return _go_gate(
                overridden=True,
                override_reason=override_reason,
                message=f"OVERRIDE: VNX CI-check overgeslagen ({override_reason})",
            )

        monkeypatch.setattr(pr_merge, "check_ci_run_for_head", fake_check)

        gate, _ = pr_merge._run_ci_gate(5, override_reason="hotfix: re-verified")

        assert gate["verdict"] == "GO"
        assert gate["overridden"] is True
        assert seen["override_reason"] == "hotfix: re-verified"


class TestMainGateWiring:
    def _ok_result(self):
        return {
            "success": True, "pr_number": 5, "dispatch_id": "", "merge_method": "squash",
            "pr_title": "", "branch": "", "receipt_status": None, "register_ok": False,
            "error": "", "dry_run": True, "overlaps": [],
        }

    def test_no_go_refuses_before_merge(self, monkeypatch, capsys):
        """A NO-GO gate exits nonzero and never calls merge_pr."""
        merge_called = []
        monkeypatch.setattr(
            pr_merge, "_run_ci_gate",
            lambda pr, **k: (_no_go_gate("Geen VNX CI-run gevonden"), None),
        )
        monkeypatch.setattr(
            pr_merge, "merge_pr",
            lambda **k: merge_called.append(1) or self._ok_result(),
        )

        rc = pr_merge.main(["--pr", "5", "--dry-run"])

        assert rc == pr_merge.EXIT_ERROR
        assert not merge_called, "merge_pr must not run when the gate is NO-GO"
        assert "NO-GO" in capsys.readouterr().err

    def test_go_proceeds_to_merge(self, monkeypatch, capsys):
        """A GO gate proceeds to merge_pr and prints the basis of the verdict."""
        merge_called = []
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (_go_gate(), None))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (_go_gate(), None))
        monkeypatch.setattr(
            pr_merge, "merge_pr",
            lambda **k: merge_called.append(1) or self._ok_result(),
        )

        rc = pr_merge.main(["--pr", "5", "--dry-run"])

        assert rc == pr_merge.EXIT_OK
        assert merge_called, "merge_pr must run when the gate is GO"
        assert "CI gate" in capsys.readouterr().out

    def test_override_is_loud(self, monkeypatch, capsys):
        """An overridden GO is printed loudly so the bypass is visible."""
        monkeypatch.setattr(
            pr_merge, "_run_ci_gate",
            lambda pr, **k: (
                _go_gate(overridden=True, override_reason="r",
                         message="OVERRIDE: VNX CI-check overgeslagen (r)"),
                None,
            ),
        )
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (_go_gate(), None))
        monkeypatch.setattr(pr_merge, "merge_pr", lambda **k: self._ok_result())

        rc = pr_merge.main(["--pr", "5", "--dry-run"])

        assert rc == pr_merge.EXIT_OK
        assert "OVERRIDE" in capsys.readouterr().out

    def test_json_no_go_outputs_json(self, monkeypatch, capsys):
        """--json emits a machine-readable failure object on NO-GO."""
        monkeypatch.setattr(
            pr_merge, "_run_ci_gate",
            lambda pr, **k: (_no_go_gate("deze merge is niet toetsbaar"), None),
        )

        rc = pr_merge.main(["--pr", "5", "--json"])

        assert rc == pr_merge.EXIT_ERROR
        out = json.loads(capsys.readouterr().out)
        assert out["success"] is False
        assert "niet toetsbaar" in out["error"]
