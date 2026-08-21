#!/usr/bin/env python3
"""Tests for OI-1399 / OI-1386: pr_merge's exit code must follow the real
merge outcome, and ``--auto`` must be conditional on repo support.

Before this fix ``_do_merge`` always passed ``--auto`` to ``gh pr merge`` and
trusted its exit code as proof of a merge. Both were wrong:

  - ``gh pr merge --auto`` exits 0 both when it merges immediately AND when it
    only *enables* auto-merge (the actual merge stays pending on required
    checks/reviews) — the exit code alone cannot tell the two apart, so
    ``pr_merge`` reported success (exit 0) for a merge that had not actually
    happened yet (OI-1399).
  - ``--auto`` unconditionally requested a repo feature that GitHub rejects
    outright when "Allow auto-merge" is off, breaking the governed merge path
    on every such repo even though a plain merge would have worked (OI-1386).

These tests cover the three PASS-criterion cases end to end (merge succeeds ->
exit 0; merge does not happen -> non-zero + reason on stderr; auto-merge
unavailable -> falls back to the plain path instead of breaking), plus unit
coverage for the two new helpers (``_repo_auto_merge_allowed`` and
``_pr_actually_merged``) that make it possible.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import pr_merge


@pytest.fixture()
def vnx_env(tmp_path, monkeypatch):
    """Writable, isolated VNX state dirs so receipt/register writes never
    touch real project state (dispatch_register.append_event hard-guards
    against exactly that under pytest)."""
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    (data_dir / "dispatches").mkdir(parents=True, exist_ok=True)
    return {"receipts_path": state_dir / "t0_receipts.ndjson"}


def _run(rc, stdout="", stderr=""):
    return subprocess.CompletedProcess(["gh"], rc, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# _repo_auto_merge_allowed — repo-level "Allow auto-merge" detection
# ---------------------------------------------------------------------------


class TestRepoAutoMergeAllowed:
    def test_true_when_repo_allows_it(self, monkeypatch):
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: _run(0, stdout="true\n"))
        assert pr_merge._repo_auto_merge_allowed() is True

    def test_false_when_repo_disallows_it(self, monkeypatch):
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: _run(0, stdout="false\n"))
        assert pr_merge._repo_auto_merge_allowed() is False

    def test_none_when_gh_fails(self, monkeypatch):
        """gh failure never reads as available — the safe fallback is 'unknown'."""
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: _run(1, stderr="HTTP 404"))
        assert pr_merge._repo_auto_merge_allowed() is None

    def test_none_when_output_unparseable(self, monkeypatch):
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: _run(0, stdout="null\n"))
        assert pr_merge._repo_auto_merge_allowed() is None


# ---------------------------------------------------------------------------
# _pr_actually_merged — never trust gh's exit code alone
# ---------------------------------------------------------------------------


class TestPrActuallyMerged:
    def test_true_when_state_merged(self, monkeypatch):
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {"state": "MERGED"})
        ok, err = pr_merge._pr_actually_merged(5)
        assert ok is True
        assert err == ""

    def test_false_when_state_still_open(self, monkeypatch):
        """gh reported success, but the PR is still OPEN (pending auto-merge)."""
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {"state": "OPEN"})
        ok, err = pr_merge._pr_actually_merged(5)
        assert ok is False
        assert "state=OPEN" in err
        assert "#5" in err

    def test_false_when_pr_state_unqueryable(self, monkeypatch):
        """An unqueryable PR is treated as 'not merged', never a silent pass."""
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: None)
        ok, err = pr_merge._pr_actually_merged(5)
        assert ok is False
        assert "#5" in err


# ---------------------------------------------------------------------------
# _do_merge — --auto is conditional, never unconditional
# ---------------------------------------------------------------------------


class TestDoMergeConditionalAuto:
    def test_auto_added_when_repo_allows_it(self, monkeypatch):
        calls = []
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: calls.append(list(args)) or _run(0))
        monkeypatch.setattr(pr_merge, "_repo_auto_merge_allowed", lambda: True)
        monkeypatch.setattr(pr_merge, "_pr_actually_merged", lambda n: (True, ""))

        ok, err = pr_merge._do_merge(5, "squash")

        assert ok is True
        assert err == ""
        merge_call = next(c for c in calls if c[:2] == ["pr", "merge"])
        assert "--auto" in merge_call

    def test_auto_omitted_when_repo_disallows_it(self, monkeypatch):
        calls = []
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: calls.append(list(args)) or _run(0))
        monkeypatch.setattr(pr_merge, "_repo_auto_merge_allowed", lambda: False)
        monkeypatch.setattr(pr_merge, "_pr_actually_merged", lambda n: (True, ""))

        ok, _ = pr_merge._do_merge(5, "squash")

        assert ok is True
        merge_call = next(c for c in calls if c[:2] == ["pr", "merge"])
        assert "--auto" not in merge_call

    def test_auto_omitted_when_availability_unknown(self, monkeypatch):
        """An undetermined repo setting is treated as unavailable, never assumed True."""
        calls = []
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: calls.append(list(args)) or _run(0))
        monkeypatch.setattr(pr_merge, "_repo_auto_merge_allowed", lambda: None)
        monkeypatch.setattr(pr_merge, "_pr_actually_merged", lambda n: (True, ""))

        pr_merge._do_merge(5, "squash")

        merge_call = next(c for c in calls if c[:2] == ["pr", "merge"])
        assert "--auto" not in merge_call

    def test_gh_failure_stays_a_loud_failure_regardless_of_auto_path(self, monkeypatch):
        """Whichever path is picked, a gh failure is never swallowed into success."""
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: _run(1, stderr="Merge conflict"))
        monkeypatch.setattr(pr_merge, "_repo_auto_merge_allowed", lambda: False)

        ok, err = pr_merge._do_merge(5, "squash")

        assert ok is False
        assert "Merge conflict" in err


# ---------------------------------------------------------------------------
# PASS criterion — the three end-to-end cases via merge_pr()
# ---------------------------------------------------------------------------


class TestExitCodeFollowsRealOutcome:
    def test_case_a_merge_succeeds_is_exit_0(self, vnx_env, monkeypatch):
        """(a) merge slaagt -> exit 0."""
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n, "title": "feat: x", "headRefName": "feature/x", "state": "OPEN",
        })
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: _run(0))
        monkeypatch.setattr(pr_merge, "_repo_auto_merge_allowed", lambda: True)
        monkeypatch.setattr(pr_merge, "_pr_actually_merged", lambda n: (True, ""))

        result = pr_merge.merge_pr(pr_number=5, receipts_file=str(vnx_env["receipts_path"]))

        assert result["success"] is True
        assert result["error"] == ""

    def test_case_b_merge_did_not_happen_is_nonzero_with_reason_on_stderr(
        self, vnx_env, monkeypatch, capsys,
    ):
        """(b) merge vindt niet plaats -> niet-nul plus reden op stderr.

        gh exits 0 (auto-merge got enabled) but the PR is still OPEN: this
        must NOT read as success.
        """
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n, "title": "feat: x", "headRefName": "feature/x", "state": "OPEN",
        })
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: _run(0))
        monkeypatch.setattr(pr_merge, "_repo_auto_merge_allowed", lambda: True)

        result = pr_merge.merge_pr(pr_number=5, receipts_file=str(vnx_env["receipts_path"]))

        assert result["success"] is False
        assert result["error"], "a failed merge must carry a non-empty reason"
        assert "state=OPEN" in result["error"]

        captured = capsys.readouterr()
        assert result["error"] in captured.err

    def test_case_c_auto_merge_unavailable_falls_back_instead_of_breaking(
        self, vnx_env, monkeypatch,
    ):
        """(c) auto-merge niet beschikbaar -> valt terug op het gewone pad."""
        calls = []
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: calls.append(list(args)) or _run(0))
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n, "title": "feat: x", "headRefName": "feature/x", "state": "MERGED",
        })
        monkeypatch.setattr(pr_merge, "_repo_auto_merge_allowed", lambda: False)

        result = pr_merge.merge_pr(pr_number=5, receipts_file=str(vnx_env["receipts_path"]))

        assert result["success"] is True, "unavailable auto-merge must fall back, not break"
        merge_call = next(c for c in calls if c[:2] == ["pr", "merge"])
        assert "--auto" not in merge_call

    def test_main_returns_nonzero_when_merge_did_not_happen(self, monkeypatch, capsys):
        """End to end through main(): a failed outcome exits EXIT_ERROR, not EXIT_OK."""
        go = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(go), {"headRefOid": "a" * 40}))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (dict(go), None))
        monkeypatch.setattr(
            pr_merge, "merge_pr",
            lambda **k: {
                "success": False, "pr_number": 5, "dispatch_id": "", "merge_method": "squash",
                "pr_title": "", "branch": "", "receipt_status": None, "register_ok": False,
                "error": "gh pr merge meldde succes voor #5, maar de PR staat nog op state=OPEN",
                "dry_run": False, "overlaps": [],
            },
        )

        rc = pr_merge.main(["--pr", "5"])

        assert rc == pr_merge.EXIT_ERROR
        assert "state=OPEN" in capsys.readouterr().err
