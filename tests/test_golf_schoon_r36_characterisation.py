"""Characterisation tests for golf-schoon R36 (pr_enforcement part).

Pin the exact behaviour of ``enforce_pr_exists`` and ``_handle_dirty_substantive``
before the extract-method refactor: every return path with its exact
``PrEnforcementResult``, the git/gh calls in order, the corrective receipts and
the warning/info log lines. The subprocess and receipt layers are patched; no
real git, gh or receipt file is touched.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import gh_pr_ensure
import pr_enforcement as pe
import tmux_worktree

R = pe.PrEnforcementResult
WT = Path("/tmp/r36-wt")
ROOT = Path("/tmp/r36-root")


class Rec:
    """Records every patched call in order."""

    def __init__(self) -> None:
        self.calls: list = []

    def receipts(self) -> list:
        return [c[1] for c in self.calls if c[0] == "receipt"]


@pytest.fixture()
def rec(monkeypatch):
    r = Rec()
    monkeypatch.setattr(pe, "_record_corrective_receipt",
                        lambda **kw: r.calls.append(("receipt", kw)))
    return r


def _ensure(rec, result=None, exc=None):
    def fake(branch, cwd, *, title, body, draft):
        rec.calls.append(("ensure_pr", (branch, cwd, title, body, draft)))
        if exc is not None:
            raise exc
        return result
    return patch.object(gh_pr_ensure, "ensure_pr", fake)


def _push(rec, ok=True, reason=None):
    def fake(*, branch, cwd):
        rec.calls.append(("push", (branch, cwd)))
        return pe._PushOutcome(ok=ok, reason=reason)
    return patch.object(pe, "_push_branch", fake)


def _run(**over):
    kw = dict(dispatch_id="d1", branch="dispatch/d1", worktree_state="pushed",
              repo_root=ROOT, receipts_file="/tmp/r36.ndjson", pr_title="T", pr_body="B")
    kw.update(over)
    return pe.enforce_pr_exists(**kw)


def _msgs(caplog):
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == pe.logger.name]


# --------------------------------------------------------------------------- enforce_pr_exists

def test_work_ref_bypasses_worktree_and_never_pushes(rec):
    with _ensure(rec, {"pr_number": 7, "created": True}), _push(rec):
        res = _run(work_ref="origin/feature/x", wt_path=WT, worktree_state="committed")
    assert res == R(applicable=True, ok=True, pushed=False, pr_number=7, created=True,
                    reason="work_ref='feature/x' (OI-1392) — PR created")
    assert rec.calls == [("ensure_pr", ("feature/x", WT, "T", "B", False))]


def test_clean_is_not_applicable(rec):
    with _ensure(rec, {}), _push(rec):
        res = _run(worktree_state="clean")
    assert res == R(applicable=False, ok=True,
                    reason="worktree_state='clean' — no changes to push or open a PR for")
    assert rec.calls == []


def test_dirty_without_wt_path_keeps_back_compat(rec):
    res = _run(worktree_state="dirty")
    assert res == R(applicable=False, ok=True, reason=(
        "worktree_state='dirty' — uncommitted changes left in the worktree; wt_path not "
        "provided, cannot classify substantive vs scratch (phantom_guard's domain)"))
    assert rec.calls == []


def test_dirty_substantive_goes_to_salvage(rec, monkeypatch):
    cls = pe._DirtyClassification(substantive=True, evidence="ev",
                                  tracked_paths=("a.py",), untracked_paths=("n.py",))
    monkeypatch.setattr(pe, "_classify_dirty_worktree", lambda *, wt_path: cls)
    seen = {}
    sentinel = R(applicable=True, ok=False, reason="salvaged")
    monkeypatch.setattr(pe, "_handle_dirty_substantive",
                        lambda **kw: seen.update(kw) or sentinel)
    res = _run(worktree_state="dirty", wt_path=str(WT), skip_pr=True, base_sha="b0",
               provider="codex", terminal_id="T2")
    assert res is sentinel
    assert seen == dict(dispatch_id="d1", branch="dispatch/d1", wt_path=WT, repo_root=ROOT,
                        receipts_file="/tmp/r36.ndjson", tracked_paths=("a.py",),
                        untracked_paths=("n.py",), evidence="ev", pr_title="T", pr_body="B",
                        skip_pr=True, base_sha="b0", provider="codex", terminal="T2")


def test_dirty_trivial_is_not_applicable(rec, monkeypatch):
    cls = pe._DirtyClassification(substantive=False, evidence="scratch only")
    monkeypatch.setattr(pe, "_classify_dirty_worktree", lambda *, wt_path: cls)
    assert _run(worktree_state="dirty", wt_path=WT) == R(applicable=False, ok=True,
                                                          reason="scratch only")


@pytest.mark.parametrize("state", ["clean", "dirty"])
def test_dirty_generated_with_nothing_committed(rec, monkeypatch, caplog, state):
    cls = pe._DirtyClassification(substantive=False, evidence="gen", generated_paths=("g",))
    monkeypatch.setattr(pe, "_classify_dirty_worktree", lambda *, wt_path: cls)
    seen = {}
    monkeypatch.setattr(tmux_worktree, "classify_path", lambda **kw: seen.update(kw) or state)
    caplog.set_level(logging.INFO, logger=pe.logger.name)
    res = _run(worktree_state="dirty", wt_path=WT, base_sha="b0")
    assert res == R(applicable=False, ok=True,
                    reason=f"gen; commit state {state!r} — nothing to push")
    assert seen == dict(wt=WT, branch="dispatch/d1", dispatch_id="d1", base_sha="b0",
                        ignore_working_tree=True)
    assert _msgs(caplog) == [("INFO", (
        "pr_enforcement: dirty tree holds only generated artifacts dispatch=d1 "
        f"branch=dispatch/d1 — enforcing the commit state {state!r} instead (OI-1846): gen"))]


def test_dirty_generated_committed_is_pushed_and_pr_opened(rec, monkeypatch):
    cls = pe._DirtyClassification(substantive=False, evidence="gen", generated_paths=("g",))
    monkeypatch.setattr(pe, "_classify_dirty_worktree", lambda *, wt_path: cls)
    monkeypatch.setattr(tmux_worktree, "classify_path", lambda **kw: "committed")
    with _ensure(rec, {"pr_number": 3}), _push(rec):
        res = _run(worktree_state="dirty", wt_path=WT)
    assert res == R(applicable=True, ok=True, pushed=True, pr_number=3, created=False)
    assert rec.calls == [("push", ("dispatch/d1", WT)),
                         ("ensure_pr", ("dispatch/d1", WT, "T", "B", False))]


def test_committed_push_failure(rec, caplog):
    caplog.set_level(logging.INFO, logger=pe.logger.name)
    with _ensure(rec, {}), _push(rec, ok=False, reason="rejected"):
        res = _run(worktree_state="committed", provider="kimi", terminal="T1")
    assert res == R(applicable=True, ok=False, pushed=False, reason="rejected")
    assert rec.calls == [("push", ("dispatch/d1", ROOT)), ("receipt", dict(
        dispatch_id="d1", branch="dispatch/d1", reason="rejected",
        receipts_file="/tmp/r36.ndjson", kind="push_failed", provider="kimi", terminal="T1"))]
    assert _msgs(caplog) == [("WARNING",
                              "pr_enforcement: push FAILED dispatch=d1 branch=dispatch/d1 — rejected")]


@pytest.mark.parametrize("state,pushed", [("committed", True), ("pushed", True)])
def test_containment_failure(rec, monkeypatch, caplog, state, pushed):
    seen = {}
    monkeypatch.setattr(pe, "_check_containment",
                        lambda **kw: seen.update(kw) or (False, "dropped commits"))
    caplog.set_level(logging.INFO, logger=pe.logger.name)
    with _ensure(rec, {}), _push(rec):
        res = _run(worktree_state=state, target_remote_head="abc", wt_path=WT)
    assert res == R(applicable=True, ok=False, pushed=pushed, reason="dropped commits")
    assert seen == dict(branch="dispatch/d1", old_head="abc", repo_root=ROOT)
    assert rec.receipts()[0]["kind"] == "containment_failed"
    assert _msgs(caplog)[-1] == ("WARNING", "pr_enforcement: containment FAILED dispatch=d1 "
                                            "branch=dispatch/d1 — dropped commits")


def test_containment_ok_then_skip_pr(rec, monkeypatch, caplog):
    monkeypatch.setattr(pe, "_check_containment", lambda **kw: (True, None))
    caplog.set_level(logging.INFO, logger=pe.logger.name)
    with _ensure(rec, {}), _push(rec):
        res = _run(target_remote_head="abc", skip_pr=True)
    assert res == R(applicable=True, ok=True, pushed=True, pr_number=None, created=False,
                    reason="skip_pr=True — branch pushed, auto-PR suppressed (existing dispatch branch)")
    assert rec.calls == []
    assert _msgs(caplog) == [("INFO", "pr_enforcement: PR skipped for dispatch=d1 "
                                      "branch=dispatch/d1 (skip_pr=True) — branch is on origin, "
                                      "no auto-PR created")]


def test_pushed_pr_found(rec):
    with _ensure(rec, {"pr_number": 12, "created": 1}):
        res = _run(wt_path=WT)
    assert res == R(applicable=True, ok=True, pushed=True, pr_number=12, created=True)
    assert rec.calls == [("ensure_pr", ("dispatch/d1", WT, "T", "B", False))]


@pytest.mark.parametrize("result,exc,reason", [
    ({"pr_number": None, "reason": "gh down"}, None, "gh down"),
    ({}, None, "gh pr create failed for branch 'dispatch/d1'"),
    (None, RuntimeError("boom"), "ensure_pr exception: boom"),
])
def test_gh_failure(rec, caplog, result, exc, reason):
    caplog.set_level(logging.INFO, logger=pe.logger.name)
    with _ensure(rec, result, exc):
        res = _run()
    assert res == R(applicable=True, ok=False, pushed=True, reason=reason)
    assert rec.receipts() == [dict(dispatch_id="d1", branch="dispatch/d1", reason=reason,
                                   receipts_file="/tmp/r36.ndjson", kind="pr_failed",
                                   provider=None, terminal=None)]
    expected = [("WARNING", f"pr_enforcement: REJECTED dispatch=d1 branch=dispatch/d1 — {reason}")]
    if exc is not None:
        expected.insert(0, ("WARNING", "pr_enforcement: ensure_pr raised for dispatch/d1: boom"))
    assert _msgs(caplog) == expected


# --------------------------------------------------------------------------- _handle_dirty_substantive

@pytest.fixture()
def salvage(rec, monkeypatch):
    """Patch the git layer; ``rec.git`` maps a git subcommand to rc / exception."""
    rec.git = {}

    def fake_run(argv, **kw):
        rec.calls.append(("git", tuple(argv), kw))
        out = rec.git.get(argv[3], 0)
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess(argv, out, "", f"{argv[3]} err")

    monkeypatch.setattr(pe.subprocess, "run", fake_run)
    monkeypatch.setattr(pe, "_head_sha", lambda wt: "pre123")
    monkeypatch.setattr(pe, "_worktree_kept_fields",
                        lambda wt, pushed: {} if pushed else {"worktree_kept": str(wt)})
    return rec


def _salvage(**over):
    kw = dict(dispatch_id="d1", branch="dispatch/d1", wt_path=WT, repo_root=ROOT,
              receipts_file="/tmp/r36.ndjson", tracked_paths=("a.py",), evidence="EV",
              pr_title="T", pr_body="B", skip_pr=False, untracked_paths=("n.py",))
    kw.update(over)
    return pe._handle_dirty_substantive(**kw)


def _extra(pushed, pr_number):
    out = {"dirty_substantive": True, "dirty_files": ["a.py"], "dirty_file_count": 1,
           "salvaged_untracked_files": ["n.py"], "salvaged_untracked_count": 1,
           "salvaged": pushed, "salvage_pr_number": pr_number}
    if not pushed:
        out["worktree_kept"] = str(WT)
    return out


_MSG = ("chore(salvage): unvouched auto-salvage of uncommitted work [SALVAGED, UNREVIEWED]\n\n"
        "Auto-committed by pr_enforcement (OI-1119) because dispatch d1 left substantive "
        "tracked changes uncommitted when the worktree was about to be reaped. No worker "
        "reviewed or vouched for this diff — treat with elevated scrutiny before merging.\n\n"
        "Dispatch-ID: d1")
_ADD = ("git", ("git", "-C", str(WT), "add", "--", "a.py", "n.py"),
        {"capture_output": True, "text": True, "timeout": 30})
_COMMIT = ("git", ("git", "-C", str(WT), "commit", "-m", _MSG),
           {"capture_output": True, "text": True, "timeout": 30})


@pytest.mark.parametrize("git,reason,calls", [
    ({"add": OSError("no git")}, "EV; salvage git-add raised: no git", [_ADD]),
    ({"add": 1}, "EV; salvage git-add failed: add err", [_ADD]),
    ({"commit": 1}, "EV; salvage git-commit failed: commit err", [_ADD, _COMMIT]),
    ({"commit": OSError("x")}, "EV; salvage git-commit raised: x", [_ADD, _COMMIT]),
])
def test_salvage_unsalvaged_before_push(salvage, caplog, git, reason, calls):
    salvage.git = git
    caplog.set_level(logging.INFO, logger=pe.logger.name)
    res = _salvage(provider="p", terminal="T3")
    assert res == R(applicable=True, ok=False, pushed=False, pr_number=None, reason=reason)
    assert salvage.calls == calls + [("receipt", dict(
        dispatch_id="d1", branch="dispatch/d1", reason=reason, receipts_file="/tmp/r36.ndjson",
        kind="dirty_substantive_unsalvaged", provider="p", terminal="T3",
        extra_fields=_extra(False, None)))]
    assert _msgs(caplog) == [("WARNING", "pr_enforcement: DIRTY-SUBSTANTIVE dispatch=d1 "
                                         f"branch=dispatch/d1 salvaged=False — {reason}")]


def test_salvage_push_fails_but_worker_delivered(salvage, monkeypatch):
    seen = {}
    delivery = pe._Delivery(ok=True, pr_number=5)
    monkeypatch.setattr(pe, "_verify_delivered_work", lambda **kw: seen.update(v=kw) or delivery)
    sentinel = R(applicable=True, ok=True, reason="delivered")
    monkeypatch.setattr(pe, "_delivered_despite_failed_salvage",
                        lambda **kw: seen.update(d=kw) or sentinel)
    with _push(salvage, ok=False, reason="hook"):
        res = _salvage(base_sha="b0", skip_pr=True)
    assert res is sentinel
    assert seen["v"] == dict(branch="dispatch/d1", wt_path=WT, repo_root=ROOT,
                             pre_salvage_head="pre123", base_sha="b0", skip_pr=True,
                             pr_title="T", pr_body="B")
    assert seen["d"] == dict(dispatch_id="d1", branch="dispatch/d1",
                             receipts_file="/tmp/r36.ndjson",
                             salvage_reason="EV; salvage committed locally but push failed: hook",
                             delivery=delivery, tracked_paths=("a.py",), untracked_paths=("n.py",),
                             provider=None, terminal=None, wt_path=WT)
    assert salvage.receipts() == []


def test_salvage_push_fails_and_delivery_not_established(salvage, monkeypatch):
    monkeypatch.setattr(pe, "_verify_delivered_work",
                        lambda **kw: pe._Delivery(ok=False, reason="no PR"))
    with _push(salvage, ok=False, reason="hook"):
        res = _salvage()
    reason = ("EV; salvage committed locally but push failed: hook; "
              "worker delivery not established: no PR")
    assert res == R(applicable=True, ok=False, pushed=False, reason=reason)
    assert salvage.receipts()[0]["kind"] == "dirty_substantive_unsalvaged"
    assert salvage.receipts()[0]["extra_fields"] == _extra(False, None)


_SALVAGE_BODY = ("**Auto-generated by push-enforcement salvage (OI-1119). No worker committed "
                 "or reviewed this diff** — it was recovered from a dirty worktree about to be "
                 "reaped. Do not merge without review; verify the diff matches the dispatch "
                 "instruction before treating this as a normal delivery.\n\n---\n\nB")


@pytest.mark.parametrize("skip_pr,result,exc,pr_number", [
    (True, None, None, None),
    (False, {"pr_number": 9}, None, 9),
    (False, {"pr_number": None, "reason": "gh"}, None, None),
    (False, None, RuntimeError("gh boom"), None),
])
def test_salvage_pushed(salvage, caplog, skip_pr, result, exc, pr_number):
    caplog.set_level(logging.INFO, logger=pe.logger.name)
    with _push(salvage), _ensure(salvage, result, exc):
        res = _salvage(skip_pr=skip_pr)
    reason = ("EV; SALVAGED as unvouched [SALVAGED, UNREVIEWED] commit on 'dispatch/d1'"
              + (f", draft PR #{pr_number}" if pr_number else ""))
    assert res == R(applicable=True, ok=False, pushed=True, pr_number=pr_number, reason=reason)
    expected = [_ADD, _COMMIT, ("push", ("dispatch/d1", WT))]
    if not skip_pr:
        expected.append(("ensure_pr", ("dispatch/d1", WT, "[SALVAGED-UNVOUCHED] T",
                                       _SALVAGE_BODY, True)))
    expected.append(("receipt", dict(
        dispatch_id="d1", branch="dispatch/d1", reason=reason, receipts_file="/tmp/r36.ndjson",
        kind="dirty_substantive_salvaged", provider=None, terminal=None,
        extra_fields=_extra(True, pr_number))))
    assert salvage.calls == expected
    assert _msgs(caplog) == [("WARNING", "pr_enforcement: DIRTY-SUBSTANTIVE dispatch=d1 "
                                         f"branch=dispatch/d1 salvaged=True — {reason}")]
