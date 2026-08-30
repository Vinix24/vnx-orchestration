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

import json
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
                "pr_title": "", "branch": "", "receipt_status": None, "receipt_ok": False,
                "register_ok": False,
                "error": "gh pr merge meldde succes voor #5, maar de PR staat nog op state=OPEN",
                "dry_run": False, "overlaps": [],
            },
        )

        rc = pr_merge.main(["--pr", "5"])

        assert rc == pr_merge.EXIT_ERROR
        assert "state=OPEN" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# OI-1518 — a merge whose receipt did NOT land must exit non-zero
# ---------------------------------------------------------------------------
#
# Before this fix, `merge_pr` set result["success"] = True the moment
# `_do_merge` succeeded, and the receipt-emit try/except only set
# `receipt_status` + a WARN to stderr. `result["success"]` was never touched,
# so `main()` returned EXIT_OK for a merge whose proof could not be written.
#
# Two failure shapes must both fail closed:
#   1. `_emit_receipt` raises.
#   2. `_emit_receipt` returns but `append_status` is not a recognized success
#      value (incl. the key missing -> "unknown" default).
# And the two success shapes must still pass so the test can detect an
# over-tight fix: `appended` -> exit 0, `duplicate` -> exit 0.


class TestReceiptEmissionFailClosed:
    def _merge_succeeds(self, monkeypatch, vnx_env):
        """Wire merge_pr so _do_merge succeeds; receipt behavior is set per-test."""
        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
            "number": n, "title": "feat: x", "headRefName": "feature/x", "state": "MERGED",
        })
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: _run(0))
        monkeypatch.setattr(pr_merge, "_repo_auto_merge_allowed", lambda: True)
        monkeypatch.setattr(pr_merge, "_pr_actually_merged", lambda n: (True, ""))
        monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)
        monkeypatch.setattr(pr_merge, "_lookup_dispatch_id_by_pr_number", lambda n: "")

    def test_emit_raises_exits_nonzero_and_says_merge_happened(
        self, vnx_env, monkeypatch, capsys,
    ):
        """Failure shape 1: _emit_receipt raises. Merge did happen; proof missing."""
        self._merge_succeeds(monkeypatch, vnx_env)

        def _boom(**kw):
            raise RuntimeError("receipts file unwritable (simulated)")

        monkeypatch.setattr(pr_merge, "_emit_receipt", _boom)

        result = pr_merge.merge_pr(
            pr_number=99999, receipts_file=str(vnx_env["receipts_path"]),
        )

        # success = "merge happened" -> still True (do not re-merge on a retry)
        assert result["success"] is True
        # receipt_ok = "binding proof landed" -> False
        assert result["receipt_ok"] is False
        assert "receipts file unwritable" in result["receipt_status"]

        err = capsys.readouterr().err
        # The text must make unmistakably clear the MERGE happened.
        assert "merge happened" in err.lower() or "was merged" in err.lower()
        # And that only the proof is missing, plus a re-run instruction.
        assert "irreversible" in err.lower()
        assert "99999" in err

    def test_emit_returns_unrecognized_status_exits_nonzero(
        self, vnx_env, monkeypatch, capsys,
    ):
        """Failure shape 2: append_status not recognized (the 'unknown' branch)."""
        self._merge_succeeds(monkeypatch, vnx_env)
        # Missing key entirely -> merge_pr defaults to "unknown".
        monkeypatch.setattr(pr_merge, "_emit_receipt", lambda **kw: {})

        result = pr_merge.merge_pr(
            pr_number=88888, receipts_file=str(vnx_env["receipts_path"]),
        )

        assert result["success"] is True
        assert result["receipt_ok"] is False
        assert result["receipt_status"] == "unknown"
        err = capsys.readouterr().err
        assert "irreversible" in err.lower()

    def test_emit_returns_explicit_unknown_status_exits_nonzero(
        self, vnx_env, monkeypatch,
    ):
        """A literal 'unknown' status is the same third branch — fail safe."""
        self._merge_succeeds(monkeypatch, vnx_env)
        monkeypatch.setattr(
            pr_merge, "_emit_receipt", lambda **kw: {"append_status": "unknown"},
        )

        result = pr_merge.merge_pr(
            pr_number=77777, receipts_file=str(vnx_env["receipts_path"]),
        )

        assert result["success"] is True
        assert result["receipt_ok"] is False
        assert result["receipt_status"] == "unknown"

    def test_main_exits_nonzero_when_receipt_emit_raises(self, monkeypatch, capsys):
        """End-to-end through main(): merge ok + receipt raise -> EXIT_ERROR."""
        go = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(go), {"headRefOid": "a" * 40}))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (dict(go), None))

        def fake_merge(**k):
            return {
                "success": True, "pr_number": 5, "dispatch_id": "", "merge_method": "squash",
                "pr_title": "", "branch": "", "receipt_status": "error: boom",
                "receipt_ok": False, "register_ok": False,
                "error": "", "dry_run": False, "overlaps": [],
            }

        monkeypatch.setattr(pr_merge, "merge_pr", fake_merge)

        rc = pr_merge.main(["--pr", "5"])

        assert rc == pr_merge.EXIT_ERROR
        captured = capsys.readouterr()
        # The output must distinguish "merge happened + proof missing" from a
        # plain merge failure. It must NOT read as "the merge failed".
        assert "WAS MERGED" in captured.err or "was merged" in captured.err.lower()

    def test_appended_receipt_exits_zero(self, vnx_env, monkeypatch, capsys):
        """Success shape: appended -> exit 0. Guards against an over-tight fix."""
        self._merge_succeeds(monkeypatch, vnx_env)
        monkeypatch.setattr(
            pr_merge, "_emit_receipt", lambda **kw: {"append_status": "appended"},
        )

        result = pr_merge.merge_pr(
            pr_number=5, receipts_file=str(vnx_env["receipts_path"]),
        )

        assert result["success"] is True
        assert result["receipt_ok"] is True
        assert result["receipt_status"] == "appended"

    def test_duplicate_receipt_exits_zero(self, vnx_env, monkeypatch):
        """Success shape: duplicate -> exit 0 (idempotent re-emit is not a fault)."""
        self._merge_succeeds(monkeypatch, vnx_env)
        monkeypatch.setattr(
            pr_merge, "_emit_receipt", lambda **kw: {"append_status": "duplicate"},
        )

        result = pr_merge.merge_pr(
            pr_number=6, receipts_file=str(vnx_env["receipts_path"]),
        )

        assert result["success"] is True
        assert result["receipt_ok"] is True
        assert result["receipt_status"] == "duplicate"

    def test_main_exits_zero_when_receipt_landed(self, monkeypatch, capsys):
        """End-to-end through main(): merge ok + receipt ok -> EXIT_OK."""
        go = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(go), {"headRefOid": "a" * 40}))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (dict(go), None))

        def fake_merge(**k):
            return {
                "success": True, "pr_number": 5, "dispatch_id": "", "merge_method": "squash",
                "pr_title": "", "branch": "", "receipt_status": "appended",
                "receipt_ok": True, "register_ok": True,
                "error": "", "dry_run": False, "overlaps": [],
            }

        monkeypatch.setattr(pr_merge, "merge_pr", fake_merge)

        rc = pr_merge.main(["--pr", "5"])
        assert rc == pr_merge.EXIT_OK

    def test_dry_run_still_exits_zero_without_receipt(self, monkeypatch, capsys):
        """--dry-run is unchanged: no merge, no receipt, exit 0."""
        go = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(go), {"headRefOid": "a" * 40}))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (dict(go), None))

        def fake_merge(**k):
            return {
                "success": True, "pr_number": 5, "dispatch_id": "", "merge_method": "squash",
                "pr_title": "", "branch": "", "receipt_status": None, "receipt_ok": False,
                "register_ok": False,
                "error": "dry_run: no merge executed", "dry_run": True, "overlaps": [],
            }

        monkeypatch.setattr(pr_merge, "merge_pr", fake_merge)

        rc = pr_merge.main(["--pr", "5", "--dry-run"])
        assert rc == pr_merge.EXIT_OK
        assert "dry-run" in capsys.readouterr().out

    def test_json_carries_receipt_ok_and_distinguishes_three_outcomes(
        self, monkeypatch, capsys,
    ):
        """--json is machine-readable and carries the three outcomes distinctly."""
        go = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(go), {"headRefOid": "a" * 40}))
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (dict(go), None))

        def fake_merge(**k):
            return {
                "success": True, "pr_number": 5, "dispatch_id": "", "merge_method": "squash",
                "pr_title": "", "branch": "", "receipt_status": "error: boom",
                "receipt_ok": False, "register_ok": False,
                "error": "", "dry_run": False, "overlaps": [],
            }

        monkeypatch.setattr(pr_merge, "merge_pr", fake_merge)

        rc = pr_merge.main(["--pr", "5", "--json"])
        assert rc == pr_merge.EXIT_ERROR
        raw = capsys.readouterr().out
        # main() prints gate lines before the JSON; take the last JSON object.
        out = json.loads(raw[raw.index("{"):])
        # merge happened but proof missing — both fields present and distinct.
        assert out["success"] is True
        assert out["receipt_ok"] is False
