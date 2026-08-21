#!/usr/bin/env python3
"""Tests for the merge-pinning fix (OI-1264): the merge is pinned to the head
the gates approved.

Before this fix, ``_run_ci_gate`` verified the CI against a specific head SHA
and ``_run_review_gate`` verified the review-gate result on the same commit,
but ``_do_merge`` ran ``gh pr merge --auto`` with no ``--match-head-commit``.
In the ``--auto`` wait window a push to the branch moved the head, and ``gh``
then merged a different commit than the one the gates had approved.

These tests pin two guarantees:

  - ``gh pr merge`` is invoked with ``--match-head-commit`` carrying the SAME
    sha the CI gate approved — the sha is threaded from the gate's established
    source (``_run_ci_gate``'s ``pr_data``), not re-fetched or fed by the test
    to both sides.
  - a shifted head is a refused merge with a clear "the gates must re-run"
    message, not a generic ``gh`` failure.
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


SHA = "b" * 40


def _go_gate(**kw):
    gate = {
        "verdict": "GO",
        "message": "VNX CI geslaagd op bbbbbbbbbbbb",
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


def _ok_merge_result():
    return {
        "success": True, "pr_number": 5, "dispatch_id": "", "merge_method": "squash",
        "pr_title": "", "branch": "", "receipt_status": None, "register_ok": False,
        "error": "", "dry_run": True, "overlaps": [],
    }


class TestMergePinnedToApprovedHead:
    def test_merge_receives_the_sha_the_ci_gate_approved(self, monkeypatch, capsys):
        """The sha the merge is pinned to is the one the CI gate approved.

        The sha is injected exactly once (through ``_query_pr``). The real
        ``_run_ci_gate`` extracts ``headRefOid`` and hands it to
        ``check_ci_run_for_head``; ``main()`` threads that same value down to
        ``merge_pr``. Asserting both destinations equal proves herkomst (the
        value flowed through the real threading), not a value this test fed to
        both sides.
        """
        pr_data = {"number": 5, "headRefOid": SHA, "headRefName": "feature/x", "title": "fix: x"}
        gate_seen = {}
        merge_seen = {}

        monkeypatch.setattr(pr_merge, "_query_pr", lambda n: pr_data)

        def fake_check(project_root, *, branch, head_sha, override_reason=None):
            gate_seen["head_sha"] = head_sha
            return _go_gate(head_sha=head_sha)

        monkeypatch.setattr(pr_merge, "check_ci_run_for_head", fake_check)
        monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (_go_gate(), None))

        def fake_merge(**kw):
            merge_seen["head_sha"] = kw.get("head_sha")
            return _ok_merge_result()

        monkeypatch.setattr(pr_merge, "merge_pr", fake_merge)

        rc = pr_merge.main(["--pr", "5", "--dry-run"])

        assert rc == pr_merge.EXIT_OK
        assert gate_seen["head_sha"] == SHA
        assert merge_seen["head_sha"] == SHA
        assert merge_seen["head_sha"] == gate_seen["head_sha"]

    def test_do_merge_adds_match_head_commit_flag(self, monkeypatch):
        """``gh pr merge`` is invoked with ``--match-head-commit <sha>``."""
        seen = {}
        ok_run = subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")

        def fake_gh(args, **k):
            seen["args"] = list(args)
            return ok_run

        monkeypatch.setattr(pr_merge, "_gh", fake_gh)
        monkeypatch.setattr(pr_merge, "_repo_auto_merge_allowed", lambda: True)
        monkeypatch.setattr(pr_merge, "_pr_actually_merged", lambda n: (True, ""))

        ok, err = pr_merge._do_merge(5, "squash", head_sha=SHA)

        assert ok is True
        assert err == ""
        assert seen["args"] == [
            "pr", "merge", "5", "--squash", "--match-head-commit", SHA, "--auto",
        ]

    def test_do_merge_without_head_sha_omits_flag(self, monkeypatch):
        """Without a pinned head the flag is omitted (backward compatible)."""
        seen = {}
        ok_run = subprocess.CompletedProcess(["gh"], 0, stdout="", stderr="")

        def fake_gh(args, **k):
            seen["args"] = list(args)
            return ok_run

        monkeypatch.setattr(pr_merge, "_gh", fake_gh)
        monkeypatch.setattr(pr_merge, "_repo_auto_merge_allowed", lambda: True)
        monkeypatch.setattr(pr_merge, "_pr_actually_merged", lambda n: (True, ""))

        ok, _ = pr_merge._do_merge(5, "squash", head_sha="")

        assert ok is True
        assert "--match-head-commit" not in seen["args"]


class TestHeadMovedRefusal:
    def test_shifted_head_is_a_refused_merge_with_clear_message(self, monkeypatch):
        """A shifted head -> refused merge with a "gates must re-run" message."""
        raw = "the head branch is not up to date with the base branch"
        failed = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr=raw)
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: failed)

        ok, err = pr_merge._do_merge(5, "squash", head_sha=SHA)

        assert ok is False
        assert "verschoven" in err
        assert "opnieuw" in err
        assert SHA in err
        assert raw in err

    def test_unrelated_failure_keeps_raw_error(self, monkeypatch):
        """A non-head-moved failure is not mislabeled as a shifted head."""
        raw = "Merge conflict: unable to merge"
        failed = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr=raw)
        monkeypatch.setattr(pr_merge, "_gh", lambda args, **k: failed)

        ok, err = pr_merge._do_merge(5, "squash", head_sha=SHA)

        assert ok is False
        assert err == raw
        assert "verschoven" not in err
