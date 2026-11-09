#!/usr/bin/env python3
"""DR-7 (requirement 9): the ``pr_merged`` receipt names the merge commit and the
tested tree, so a merge can be reconstructed from the trail alone.

The audit-impact study (``20261108-res-graafaudit``) could not answer "which tree
landed on main" for PR #2457 (gap M8). Every test drives a real throwaway git
repository through the receipt writer with only the ``gh`` merge-commit lookup
patched, and no test merges a real PR (``_do_merge`` is patched).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import pr_merge  # noqa: E402
from merge_target import MergeTarget  # noqa: E402

PR, PROJECT_ID, DISPATCH_ID, RULE_VERSION = 2457, "vnx-dev", "dlv-2c40439fd971", "67591075cb2c59b7"


# --- a real throwaway repository -------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "T")):
        _git(repo, *args)
    (repo / "app.py").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _commit(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _feature_head(repo: Path, branch: str = "feature") -> str:
    """A feature branch off main with one commit; the checkout returns to main."""
    _git(repo, "checkout", "-q", "-b", branch)
    head = _commit(repo, "feature.py", "feature\n", "feat: feature")
    _git(repo, "checkout", "-q", "main")
    return head


def _squash(repo: Path, branch: str = "feature") -> str:
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--squash", branch)
    _git(repo, "commit", "-q", "-m", f"squash {branch}")
    return _git(repo, "rev-parse", "HEAD")


def _tree(repo: Path, sha: str) -> str:
    return _git(repo, "rev-parse", f"{sha}^{{tree}}")


# --- environment and the merge driver --------------------------------------


@pytest.fixture()
def vnx_env(tmp_path, monkeypatch):
    data_dir, state_dir = tmp_path / "data", tmp_path / "data" / "state"
    for sub in ("state", "events", "dispatches", "logs", "pids", "locks", "database"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    env = {"PROJECT_ROOT": tmp_path, "VNX_DATA_DIR": data_dir, "VNX_STATE_DIR": state_dir,
           "VNX_HOME": VNX_ROOT, "VNX_DISPATCH_DIR": data_dir / "dispatches",
           "VNX_LOGS_DIR": data_dir / "logs", "VNX_PIDS_DIR": data_dir / "pids",
           "VNX_LOCKS_DIR": data_dir / "locks", "VNX_REPORTS_DIR": data_dir / "unified_reports",
           "VNX_DB_DIR": data_dir / "database"}
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return {"receipts_path": state_dir / "t0_receipts.ndjson", "state_dir": state_dir}


def _drive_merge(monkeypatch, vnx_env, repo: Path, *, tested_head: str, gh_error: str = "",
                 gh_json: Dict[str, Any] | None = None,
                 review_seats: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Run ``merge_pr``: real git, patched ``gh`` and a patched (never ran) merge."""
    monkeypatch.setattr(pr_merge, "_ACTIVE_TARGET", MergeTarget(project_root=repo, repo="acme/widgets"))
    monkeypatch.setattr(pr_merge, "_query_pr", lambda n: {
        "number": n, "title": "feat: x (PR-2457)",
        "headRefName": "dispatch/dlv-2c40439fd971", "state": "OPEN"})
    monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m, *a, **k: (True, ""))
    monkeypatch.setattr(pr_merge, "_branch_overlaps", lambda n, b: [])
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)

    def fake_gh(args, **kwargs):
        if gh_error:
            return subprocess.CompletedProcess(list(args), 1, stdout="", stderr=gh_error)
        payload = gh_json if gh_json is not None else {"mergeCommit": {"oid": tested_head}}
        return subprocess.CompletedProcess(list(args), 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(pr_merge, "_gh", fake_gh)
    return pr_merge.merge_pr(
        pr_number=PR, dispatch_id=DISPATCH_ID, merge_method="squash",
        receipts_file=str(vnx_env["receipts_path"]), head_sha=tested_head,
        review_seats=review_seats)


def _pr_merged(vnx_env) -> Dict[str, Any]:
    lines = [json.loads(line) for line in
             vnx_env["receipts_path"].read_text(encoding="utf-8").splitlines() if line.strip()]
    merged = [r for r in lines if r.get("event_type") == "pr_merged" and r.get("pr_number") == PR]
    assert len(merged) == 1, f"expected exactly one pr_merged for #{PR}, got {len(merged)}"
    return merged[0]


# --- the three receipt outcomes --------------------------------------------


def test_squash_on_unmoved_main_records_equal_trees(vnx_env, monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    tested_head = _feature_head(repo)
    merge_sha = _squash(repo)

    result = _drive_merge(monkeypatch, vnx_env, repo, tested_head=tested_head,
                          gh_json={"mergeCommit": {"oid": merge_sha}})

    assert result["success"] is True and result["receipt_ok"] is True
    receipt = _pr_merged(vnx_env)
    assert receipt["merge_sha"] == merge_sha
    assert receipt["merged_tree"] == _tree(repo, merge_sha)
    assert receipt["tested_head"] == tested_head
    assert receipt["tested_tree"] == _tree(repo, tested_head)
    assert receipt["tree_equal"] is True
    assert "merge_tree_unavailable" not in receipt


def test_squash_after_main_moved_records_both_trees_and_false(vnx_env, monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    tested_head = _feature_head(repo)
    _commit(repo, "other.py", "other\n", "chore: main moved")  # main moved between review and merge
    merge_sha = _squash(repo)

    result = _drive_merge(monkeypatch, vnx_env, repo, tested_head=tested_head,
                          gh_json={"mergeCommit": {"oid": merge_sha}})

    assert result["success"] is True and result["receipt_ok"] is True
    receipt = _pr_merged(vnx_env)
    assert receipt["merge_sha"] == merge_sha
    assert receipt["merged_tree"] == _tree(repo, merge_sha)
    assert receipt["tested_tree"] == _tree(repo, tested_head)
    assert receipt["merged_tree"] != receipt["tested_tree"]
    assert receipt["tree_equal"] is False


def test_gh_lookup_failure_still_merges_and_names_the_reason(vnx_env, monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    tested_head = _feature_head(repo)
    _squash(repo)

    result = _drive_merge(monkeypatch, vnx_env, repo, tested_head=tested_head,
                          gh_error="gh: not authenticated")

    # The merge already happened and stays a success: no refusal, no exit change.
    assert result["success"] is True and result["receipt_ok"] is True
    receipt = _pr_merged(vnx_env)
    assert "merge_sha" not in receipt and "merged_tree" not in receipt
    assert "gh_error:" in receipt["merge_tree_unavailable"]
    assert "not authenticated" in receipt["merge_tree_unavailable"]
    # What could be read is booked; unknown is never equal.
    assert receipt["tested_head"] == tested_head
    assert receipt["tested_tree"] == _tree(repo, tested_head)
    assert "tree_equal" not in receipt


# --- the reconstruction test: six questions from the trail alone ------------


def _round_line(head_sha: str, *, takeover_from: str = "",
                recorded_at: str = "2026-11-07T06:15:03Z") -> Dict[str, Any]:
    return {"project_id": PROJECT_ID, "pr_number": PR, "head_sha": head_sha, "seat": "codex_gate",
            "reviewer": "codex_gate", "takeover_from": takeover_from, "verdict": "pass",
            "blocking_count": 0, "report_path": f"reports/codex_gate-{head_sha[:12]}.md",
            "dispatch_id": f"{DISPATCH_ID}-r", "recorded_at": recorded_at,
            "branch": "dispatch/dlv-2c40439fd971", "source": "gate"}


def _answers(receipt: Dict[str, Any], rounds: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The six audit answers, from the receipt and the rounds lines only: no gh, no git."""
    seats = receipt.get("review_seats") or {}
    merged_head = seats.get("head_sha") or ""
    counted = [line for line in rounds if line.get("head_sha") == merged_head]
    return {
        "1_merged_head": merged_head,
        "2_seat_head_reviewer": [(l["seat"], l["head_sha"], l["reviewer"]) for l in rounds],
        "3_takeover": {"from": sorted({l["takeover_from"] for l in rounds if l["takeover_from"]}),
                       "routes": dict(seats.get("routes") or {})},
        "4_security": {"touched": list((seats.get("risk") or {}).get("files") or []),
                       "rule_version": (seats.get("risk") or {}).get("rule_version") or ""},
        "5_landed": {"merge_sha": receipt.get("merge_sha") or "",
                     "merged_tree": receipt.get("merged_tree") or "",
                     "tested_tree": receipt.get("tested_tree") or "",
                     "tree_equal": receipt.get("tree_equal")},
        "6_counted_lines": {"declared": (seats.get("round_ledger") or {}).get("lines"),
                            "matched": [(l["seat"], l["head_sha"]) for l in counted]},
    }


def test_six_questions_answerable_from_receipt_and_rounds(vnx_env, monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    head_one = _feature_head(repo)
    _git(repo, "checkout", "-q", "feature")
    head_two = _commit(repo, "feature.py", "feature, fixed\n", "fix: address finding")
    _git(repo, "checkout", "-q", "main")
    merge_sha = _squash(repo)
    expected_merged_tree, expected_tested_tree = _tree(repo, merge_sha), _tree(repo, head_two)

    rounds = [_round_line(head_one), _round_line(head_two, takeover_from="claude_gate",
                                                 recorded_at="2026-11-07T06:39:11Z")]
    rounds_path = vnx_env["state_dir"] / "review_gates" / "rounds.ndjson"
    rounds_path.parent.mkdir(parents=True, exist_ok=True)
    rounds_path.write_text("".join(json.dumps(line) + "\n" for line in rounds), encoding="utf-8")
    review_seats = {
        "rule": "risk", "pr_id": "PR-2457", "head_sha": head_two, "seats": ["codex_gate"],
        "source": "profile", "filled_by": {"codex_gate": "codex_gate"},
        "routes": {"codex_gate": "takeover"}, "risk_class": "medium",
        "risk": {"risk_class": "medium", "reason": "no high-risk path", "files": [],
                 "rule_version": RULE_VERSION},
        "round_ledger": {"path": str(rounds_path), "lines": 1, "cap": {"cap": 3, "counted": 1}},
    }

    result = _drive_merge(monkeypatch, vnx_env, repo, tested_head=head_two,
                          gh_json={"mergeCommit": {"oid": merge_sha}}, review_seats=review_seats)
    assert result["success"] is True and result["receipt_ok"] is True

    # From here on: the written line and the synthetic rounds only.
    receipt = _pr_merged(vnx_env)
    trail_rounds = [json.loads(line) for line in
                    rounds_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    answers = _answers(receipt, trail_rounds)

    # (1) Which head was merged.
    assert answers["1_merged_head"] == head_two and receipt["tested_head"] == head_two
    # (2) Which seat reviewed which head, with which reviewer.
    assert answers["2_seat_head_reviewer"] == [
        ("codex_gate", head_one, "codex_gate"), ("codex_gate", head_two, "codex_gate")]
    # (3) Whether a verdict was taken over: which seat and by which route.
    assert answers["3_takeover"] == {"from": ["claude_gate"], "routes": {"codex_gate": "takeover"}}
    # (4) Whether a security path was touched, under which rule version.
    assert answers["4_security"] == {"touched": [], "rule_version": RULE_VERSION}
    # (5) Which commit and tree landed, and whether that is the tested tree.
    assert answers["5_landed"] == {"merge_sha": merge_sha, "merged_tree": expected_merged_tree,
                                   "tested_tree": expected_tested_tree, "tree_equal": True}
    # (6) Which ledger lines the door counted: the declared count and the matching line agree.
    assert answers["6_counted_lines"] == {"declared": 1, "matched": [("codex_gate", head_two)]}
