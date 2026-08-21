"""Tests for pr_enforcement.py — the push+PR enforcement chokepoint every lane
calls (rij-7, lane-matrix). gh_pr_ensure, append_receipt and subprocess.run are
mocked; nothing here touches GitHub or a real git repo.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pr_enforcement as pe


def _kwargs(**overrides):
    base = dict(
        dispatch_id="d1",
        branch="dispatch/d1",
        worktree_state="pushed",
        repo_root=Path("/repo"),
        receipts_file="/tmp/does-not-matter.ndjson",
        pr_title="t",
        pr_body="b",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Out-of-scope worktree states — no gh call at all
# ---------------------------------------------------------------------------

def test_not_applicable_when_clean_or_dirty(monkeypatch):
    """clean and dirty have nothing deterministically pushable — no gh call at all.

    (committed is no longer in this set: rij-7 binds it to push+PR — see the
    committed-state tests below.)
    """
    called = {"n": 0}

    def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("gh_pr_ensure must not be imported/called")

    import gh_pr_ensure
    monkeypatch.setattr(gh_pr_ensure, "ensure_pr", _boom)

    for state in ("clean", "dirty"):
        result = pe.enforce_pr_exists(**_kwargs(worktree_state=state))
        assert result.applicable is False
        assert result.ok is True
        assert result.pr_number is None
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# committed → push, then PR (rij-7 fix)
# ---------------------------------------------------------------------------

def _ok_push(monkeypatch):
    """Mock subprocess.run so `git push -u origin <branch>` succeeds once."""
    import pr_enforcement
    calls = {"n": 0}

    def _fake_run(args, **kw):
        if args[0] == "git" and "push" in args:
            calls["n"] += 1
            return _CompletedRC(0)
        raise AssertionError(f"unexpected subprocess.run call: {args}")

    monkeypatch.setattr(pr_enforcement.subprocess, "run", _fake_run)
    return calls


class _CompletedRC:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_committed_pushes_then_creates_pr(monkeypatch):
    import gh_pr_ensure
    push_calls = _ok_push(monkeypatch)

    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": 202, "created": True, "reason": None},
    )

    result = pe.enforce_pr_exists(**_kwargs(worktree_state="committed"))

    assert result.applicable is True
    assert result.ok is True
    assert result.pushed is True
    assert result.pr_number == 202
    assert result.created is True
    assert push_calls["n"] == 1


def test_committed_push_failure_is_loud(monkeypatch, tmp_path):
    """A failed push must NOT silently resolve as done — ok=False + corrective receipt."""
    import pr_enforcement
    monkeypatch.setattr(
        pr_enforcement.subprocess, "run",
        lambda *a, **kw: _CompletedRC(128, err="fatal: could not read username"),
    )

    import gh_pr_ensure
    gh_called = {"n": 0}
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: (gh_called.__setitem__("n", gh_called["n"] + 1),
                          {"pr_number": None, "created": False, "reason": "x"})[1],
    )

    import append_receipt
    captured = {}
    monkeypatch.setattr(
        append_receipt, "append_receipt_payload",
        lambda payload, **kw: captured.update(payload=payload),
    )

    result = pe.enforce_pr_exists(**_kwargs(receipts_file=str(tmp_path / "r.ndjson"), worktree_state="committed"))

    assert result.applicable is True
    assert result.ok is False
    assert result.pushed is False
    assert "git push" in result.reason
    assert "could not read username" in result.reason
    # PR creation must never be attempted when the push failed.
    assert gh_called["n"] == 0
    payload = captured["payload"]
    assert payload["status"] == "failed"
    assert payload["autopr_rejected"] is True
    assert payload["autopr_kind"] == "push_failed"
    assert payload["branch"] == "dispatch/d1"


# ---------------------------------------------------------------------------
# (a) pushed + no PR -> creates exactly one
# ---------------------------------------------------------------------------

def test_pushed_no_pr_creates_one(monkeypatch):
    import gh_pr_ensure
    captured = {}

    def _fake_ensure_pr(branch, repo_root, *, title, body, draft=False):
        captured.update(branch=branch, repo_root=repo_root, title=title, body=body, draft=draft)
        return {"pr_number": 101, "created": True, "reason": None}

    monkeypatch.setattr(gh_pr_ensure, "ensure_pr", _fake_ensure_pr)

    result = pe.enforce_pr_exists(**_kwargs())

    assert result.applicable is True
    assert result.ok is True
    assert result.pr_number == 101
    assert result.created is True
    assert captured["branch"] == "dispatch/d1"
    assert captured["draft"] is False


# ---------------------------------------------------------------------------
# (b) re-running the enforcement path is idempotent — still one PR, no re-create
# ---------------------------------------------------------------------------

def test_rerun_is_idempotent(monkeypatch):
    import gh_pr_ensure

    calls = {"n": 0}

    def _fake_ensure_pr(branch, repo_root, *, title, body, draft=False):
        calls["n"] += 1
        # ensure_pr itself is idempotent (tested in test_gh_pr_ensure.py); simulate
        # its post-creation no-op behavior on the second call.
        if calls["n"] == 1:
            return {"pr_number": 101, "created": True, "reason": None}
        return {"pr_number": 101, "created": False, "reason": None}

    monkeypatch.setattr(gh_pr_ensure, "ensure_pr", _fake_ensure_pr)

    first = pe.enforce_pr_exists(**_kwargs())
    second = pe.enforce_pr_exists(**_kwargs())

    assert first.pr_number == 101 and first.created is True
    assert second.pr_number == 101 and second.created is False
    assert calls["n"] == 2  # enforce_pr_exists was invoked twice, but only 1 PR ever exists


# ---------------------------------------------------------------------------
# (c) existing-PR branch -> pure no-op
# ---------------------------------------------------------------------------

def test_existing_pr_is_noop(monkeypatch):
    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": 55, "created": False, "reason": None},
    )

    result = pe.enforce_pr_exists(**_kwargs())

    assert result.applicable is True
    assert result.ok is True
    assert result.pr_number == 55
    assert result.created is False


# ---------------------------------------------------------------------------
# Enforcement: creation failure is a LOUD, receipt-visible failure
# ---------------------------------------------------------------------------

def test_creation_failure_appends_corrective_receipt(monkeypatch, tmp_path):
    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": None, "created": False, "reason": "gh auth expired"},
    )

    import append_receipt
    captured = {}
    monkeypatch.setattr(
        append_receipt, "append_receipt_payload",
        lambda payload, **kw: captured.update(payload=payload, kw=kw),
    )

    result = pe.enforce_pr_exists(**_kwargs(receipts_file=str(tmp_path / "r.ndjson")))

    assert result.applicable is True
    assert result.ok is False
    assert result.pr_number is None
    assert result.reason == "gh auth expired"

    payload = captured["payload"]
    assert payload["status"] == "failed"
    assert payload["autopr_rejected"] is True
    assert payload["autopr_reason"] == "gh auth expired"
    assert payload["dispatch_id"] == "d1"
    assert payload["branch"] == "dispatch/d1"
    # event_type must be one the watchers honor (ACTIONABLE_EVENTS), mirrors phantom_guard
    assert payload["event_type"] == "subprocess_completion"
    assert payload["synthesized"] is False  # else dedup Tier-2 would drop it
    assert payload["timestamp"].endswith("Z")


def test_creation_failure_default_reason_when_ensure_pr_omits_one(monkeypatch, tmp_path):
    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": None, "created": False, "reason": None},
    )
    import append_receipt
    monkeypatch.setattr(append_receipt, "append_receipt_payload", lambda *a, **k: None)

    result = pe.enforce_pr_exists(**_kwargs())

    assert result.ok is False
    assert "dispatch/d1" in result.reason


def test_ensure_pr_exception_is_treated_as_failure_not_raised(monkeypatch, tmp_path):
    import gh_pr_ensure

    def _boom(*a, **kw):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(gh_pr_ensure, "ensure_pr", _boom)

    import append_receipt
    captured = {}
    monkeypatch.setattr(
        append_receipt, "append_receipt_payload",
        lambda payload, **kw: captured.update(payload=payload),
    )

    result = pe.enforce_pr_exists(**_kwargs(receipts_file=str(tmp_path / "r.ndjson")))

    assert result.ok is False
    assert "network exploded" in result.reason
    assert captured["payload"]["autopr_rejected"] is True


def test_corrective_receipt_append_failure_is_non_fatal(monkeypatch, tmp_path):
    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": None, "created": False, "reason": "boom"},
    )
    import append_receipt

    def _boom(*a, **kw):
        raise RuntimeError("append exploded")

    monkeypatch.setattr(append_receipt, "append_receipt_payload", _boom)

    # Must not raise even though the corrective append itself fails.
    result = pe.enforce_pr_exists(**_kwargs())
    assert result.ok is False
    assert result.reason == "boom"


def test_success_path_never_touches_append_receipt(monkeypatch):
    """A found/created PR must NOT append any corrective receipt."""
    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": 9, "created": True, "reason": None},
    )
    import append_receipt
    calls = {"n": 0}
    monkeypatch.setattr(
        append_receipt, "append_receipt_payload",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )

    result = pe.enforce_pr_exists(**_kwargs())

    assert result.ok is True
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# OI-1113: containment check — verifies new HEAD contains pre-worker HEAD
# ---------------------------------------------------------------------------


def test_containment_is_checked_when_target_remote_head_set(monkeypatch):
    """When target_remote_head is set, _check_containment MUST be called after push.

    This test proves the containment guard is active: if the condition were
    reversed (``target_remote_head is None`` instead of ``is not None``), the
    containment call would be skipped and this assertion would fail — a green
    test that cannot fail proves nothing (OI-1113 definition of done).
    """
    import pr_enforcement as _mod
    containment_args = []

    def _fake_check_containment(*, branch, old_head, repo_root):
        containment_args.append({"branch": branch, "old_head": old_head})
        return True, None

    monkeypatch.setattr(_mod, "_push_branch", lambda **kw: _mod._PushOutcome(ok=True))
    monkeypatch.setattr(_mod, "_check_containment", _fake_check_containment)

    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": 1, "created": True, "reason": None},
    )

    result = pe.enforce_pr_exists(
        **_kwargs(worktree_state="committed", target_remote_head="abc123def")
    )

    assert len(containment_args) == 1, (
        "_check_containment was NOT called — if the condition "
        "'target_remote_head is not None' were reversed, this assertion would fail"
    )
    assert containment_args[0]["old_head"] == "abc123def"
    assert result.ok is True


def test_containment_skipped_when_target_remote_head_none(monkeypatch):
    """When target_remote_head is None (new branch), containment is NOT checked."""
    import pr_enforcement as _mod
    containment_calls = []

    def _fake_check_containment(*, branch, old_head, repo_root):
        containment_calls.append(1)
        return True, None

    monkeypatch.setattr(_mod, "_push_branch", lambda **kw: _mod._PushOutcome(ok=True))
    monkeypatch.setattr(_mod, "_check_containment", _fake_check_containment)

    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": 1, "created": True, "reason": None},
    )

    result = pe.enforce_pr_exists(
        **_kwargs(worktree_state="committed")  # target_remote_head defaults to None
    )

    assert len(containment_calls) == 0, (
        "_check_containment WAS called when target_remote_head is None — "
        "containment should be skipped for new branches"
    )
    assert result.ok is True


def test_containment_failure_is_loud(monkeypatch, tmp_path):
    """Containment failure → ok=False + corrective receipt with kind='containment_failed'.

    The failure is receipt-visible, mirroring push_failed and pr_failed.
    PR creation must NOT be attempted after containment fails.
    """
    import pr_enforcement as _mod

    monkeypatch.setattr(_mod, "_push_branch", lambda **kw: _mod._PushOutcome(ok=True))
    monkeypatch.setattr(
        _mod, "_check_containment",
        lambda **kw: (False, "containment violated: HEAD abc was replaced by def"),
    )

    import gh_pr_ensure
    gh_calls = {"n": 0}
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: (
            gh_calls.__setitem__("n", gh_calls["n"] + 1),
            {"pr_number": None, "created": False, "reason": "x"},
        )[1],
    )

    import append_receipt
    captured = {}
    monkeypatch.setattr(
        append_receipt, "append_receipt_payload",
        lambda payload, **kw: captured.update(payload=payload),
    )

    result = pe.enforce_pr_exists(
        **_kwargs(
            receipts_file=str(tmp_path / "r.ndjson"),
            worktree_state="committed",
            target_remote_head="abc123",
        )
    )

    assert result.applicable is True
    assert result.ok is False
    assert "containment violated" in result.reason
    # PR creation must NOT be attempted after containment failure.
    assert gh_calls["n"] == 0

    payload = captured["payload"]
    assert payload["status"] == "failed"
    assert payload["autopr_rejected"] is True
    assert payload["autopr_kind"] == "containment_failed"
    assert payload["branch"] == "dispatch/d1"
    assert "containment violated" in payload["autopr_reason"]


def test_containment_failure_on_pushed_state(monkeypatch, tmp_path):
    """Containment is checked even when worktree_state is 'pushed' (worker already pushed).

    A worker that force-pushes before the lane enforcement runs would leave the
    branch in 'pushed' state — the containment check must still run.
    """
    import pr_enforcement as _mod

    monkeypatch.setattr(
        _mod, "_check_containment",
        lambda **kw: (False, "containment violated: force-push detected"),
    )

    import append_receipt
    captured = {}
    monkeypatch.setattr(
        append_receipt, "append_receipt_payload",
        lambda payload, **kw: captured.update(payload=payload),
    )

    result = pe.enforce_pr_exists(
        **_kwargs(
            receipts_file=str(tmp_path / "r.ndjson"),
            worktree_state="pushed",
            target_remote_head="abc123",
        )
    )

    assert result.ok is False
    assert "containment violated" in result.reason
    assert captured["payload"]["autopr_kind"] == "containment_failed"


# ---------------------------------------------------------------------------
# OI-1115: single destination — skip auto-PR on existing dispatch branches
# ---------------------------------------------------------------------------


def test_skip_pr_pushes_but_creates_no_pr(monkeypatch):
    """skip_pr=True: pushes the branch (committed state) but skips PR creation."""
    import pr_enforcement as _mod
    push_calls = {"n": 0}

    def _fake_push(*, branch, repo_root):
        push_calls["n"] += 1
        return _mod._PushOutcome(ok=True)

    monkeypatch.setattr(_mod, "_push_branch", _fake_push)

    import gh_pr_ensure
    gh_calls = {"n": 0}

    def _boom(*a, **kw):
        gh_calls["n"] += 1
        raise AssertionError("ensure_pr must NOT be called when skip_pr=True")

    monkeypatch.setattr(gh_pr_ensure, "ensure_pr", _boom)

    result = pe.enforce_pr_exists(
        **_kwargs(worktree_state="committed", skip_pr=True)
    )

    assert result.applicable is True
    assert result.ok is True
    assert result.pushed is True
    assert result.pr_number is None
    assert result.created is False
    assert "skip_pr=True" in result.reason
    assert push_calls["n"] == 1
    assert gh_calls["n"] == 0


def test_skip_pr_pushed_state_noop(monkeypatch):
    """skip_pr=True + pushed state: no push needed, no PR created — just reports ok."""
    import gh_pr_ensure
    gh_calls = {"n": 0}
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: (
            gh_calls.__setitem__("n", gh_calls["n"] + 1),
            {"pr_number": None, "created": False, "reason": "x"},
        )[1],
    )

    result = pe.enforce_pr_exists(
        **_kwargs(worktree_state="pushed", skip_pr=True)
    )

    assert result.applicable is True
    assert result.ok is True
    assert result.pushed is True
    assert result.pr_number is None
    assert "skip_pr=True" in result.reason
    assert gh_calls["n"] == 0


def test_pr_still_created_when_skip_pr_false(monkeypatch):
    """skip_pr=False (default): PR is created normally — existing behavior unchanged."""
    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": 42, "created": True, "reason": None},
    )

    result = pe.enforce_pr_exists(**_kwargs(worktree_state="pushed", skip_pr=False))

    assert result.applicable is True
    assert result.ok is True
    assert result.pr_number == 42
    assert result.created is True


# ---------------------------------------------------------------------------
# OI-1113/OI-1115 combined: containment + skip_pr together
# ---------------------------------------------------------------------------


def test_containment_checked_before_skip_pr(monkeypatch):
    """Containment runs before the skip_pr short-circuit.

    When both target_remote_head is set AND skip_pr is True, containment must
    still be checked — a force-push on an existing dispatch branch is just as
    bad as on a new one.
    """
    import pr_enforcement as _mod
    containment_calls = []

    monkeypatch.setattr(_mod, "_push_branch", lambda **kw: _mod._PushOutcome(ok=True))
    monkeypatch.setattr(
        _mod, "_check_containment",
        lambda **kw: containment_calls.append(1) or (True, None),
    )

    result = pe.enforce_pr_exists(
        **_kwargs(
            worktree_state="committed",
            target_remote_head="abc123",
            skip_pr=True,
        )
    )

    assert len(containment_calls) == 1, (
        "containment must be checked BEFORE skip_pr — "
        "skipping PR does not mean skipping containment"
    )
    assert result.ok is True
    assert result.pushed is True
    assert result.pr_number is None  # PR skipped


# ---------------------------------------------------------------------------
# Existing behaviors: push_failed and pr_failed still work with new params
# ---------------------------------------------------------------------------


def test_push_failed_still_works_with_new_params(monkeypatch, tmp_path):
    """push_failed is still recorded when push fails, new params passed but ignored."""
    import pr_enforcement as _mod
    monkeypatch.setattr(
        _mod.subprocess, "run",
        lambda *a, **kw: _CompletedRC(128, err="fatal: remote rejected"),
    )

    import append_receipt
    captured = {}
    monkeypatch.setattr(
        append_receipt, "append_receipt_payload",
        lambda payload, **kw: captured.update(payload=payload),
    )

    result = pe.enforce_pr_exists(
        **_kwargs(
            receipts_file=str(tmp_path / "r.ndjson"),
            worktree_state="committed",
            target_remote_head="abc123",
            skip_pr=False,
        )
    )

    assert result.ok is False
    assert "remote rejected" in result.reason
    assert captured["payload"]["autopr_kind"] == "push_failed"


def test_pr_failed_still_works_with_new_params(monkeypatch, tmp_path):
    """pr_failed is still recorded when ensure_pr fails, new params passed."""
    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": None, "created": False, "reason": "gh rate limited"},
    )

    import append_receipt
    captured = {}
    monkeypatch.setattr(
        append_receipt, "append_receipt_payload",
        lambda payload, **kw: captured.update(payload=payload),
    )

    result = pe.enforce_pr_exists(
        **_kwargs(
            receipts_file=str(tmp_path / "r.ndjson"),
            worktree_state="pushed",
            target_remote_head=None,
            skip_pr=False,
        )
    )

    assert result.ok is False
    assert "gh rate limited" in result.reason
    assert captured["payload"]["autopr_kind"] == "pr_failed"


# ---------------------------------------------------------------------------
# OI-1392: work_ref is authoritative — branch/worktree_state never consulted.
# Lane-level, real-git regression tests live in
# test_oi1392_pr_enforcement_work_ref.py; these are the pure-function unit
# tests for the same contract.
# ---------------------------------------------------------------------------

def test_work_ref_bypasses_branch_and_worktree_state(monkeypatch):
    """work_ref set → ensure_pr is called for work_ref, never for `branch`, and
    the worktree_state dispatch (committed → push) never runs."""
    import gh_pr_ensure
    push_calls = {"n": 0}

    import pr_enforcement
    monkeypatch.setattr(
        pr_enforcement.subprocess, "run",
        lambda *a, **kw: (push_calls.__setitem__("n", push_calls["n"] + 1), _CompletedRC(0))[1],
    )

    captured = {}

    def _fake_ensure_pr(branch, repo_root, *, title, body, draft=False):
        captured.update(branch=branch)
        return {"pr_number": 6001, "created": True, "reason": None}

    monkeypatch.setattr(gh_pr_ensure, "ensure_pr", _fake_ensure_pr)

    result = pe.enforce_pr_exists(
        **_kwargs(worktree_state="committed", work_ref="work/oi1392-target")
    )

    assert result.applicable is True
    assert result.ok is True
    assert result.pushed is False
    assert result.pr_number == 6001
    assert result.created is True
    assert captured["branch"] == "work/oi1392-target"
    assert push_calls["n"] == 0, "work_ref must never trigger a git push"


def test_work_ref_strips_ref_prefixes(monkeypatch):
    """A work_ref declared with a remote/refs prefix is normalized to a bare
    branch name before it reaches gh_pr_ensure (mirrors phantom_guard's own
    normalization of the same field)."""
    import gh_pr_ensure
    captured = {}
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda branch, repo_root, **kw: (
            captured.update(branch=branch), {"pr_number": 1, "created": True, "reason": None}
        )[1],
    )

    pe.enforce_pr_exists(
        **_kwargs(worktree_state="clean", work_ref="origin/work/oi1392-target")
    )
    assert captured["branch"] == "work/oi1392-target"


def test_work_ref_already_open_pr_creates_nothing(monkeypatch):
    """work_ref with an already-open PR → nothing created, no push."""
    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {"pr_number": 4321, "created": False, "reason": None},
    )

    result = pe.enforce_pr_exists(
        **_kwargs(worktree_state="dirty", work_ref="work/oi1392-existing")
    )

    assert result.applicable is True
    assert result.ok is True
    assert result.pr_number == 4321
    assert result.created is False
    assert result.pushed is False


def test_work_ref_missing_from_origin_is_loud_failure_not_a_second_branch(monkeypatch, tmp_path):
    """work_ref set but never actually pushed to origin (gh pr create fails) →
    a genuine, receipt-visible failure — NEVER a fallback to `branch`."""
    import gh_pr_ensure
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda *a, **kw: {
            "pr_number": None, "created": False,
            "reason": "gh pr create failed for branch 'work/oi1392-target' (no open PR found on retry)",
        },
    )

    import pr_enforcement
    push_calls = {"n": 0}
    monkeypatch.setattr(
        pr_enforcement.subprocess, "run",
        lambda *a, **kw: (push_calls.__setitem__("n", push_calls["n"] + 1), _CompletedRC(0))[1],
    )

    import append_receipt
    captured = {}
    monkeypatch.setattr(
        append_receipt, "append_receipt_payload",
        lambda payload, **kw: captured.update(payload=payload),
    )

    result = pe.enforce_pr_exists(
        **_kwargs(
            receipts_file=str(tmp_path / "r.ndjson"),
            worktree_state="committed",
            work_ref="work/oi1392-target",
        )
    )

    assert result.applicable is True
    assert result.ok is False
    assert result.pushed is False
    assert push_calls["n"] == 0, "a missing work_ref branch must never fall back to pushing `branch`"
    payload = captured["payload"]
    assert payload["branch"] == "work/oi1392-target"
    assert payload["branch"] != "dispatch/d1"


def test_empty_work_ref_falls_through_to_normal_behavior(monkeypatch):
    """work_ref="" (or None) is indistinguishable from an absent work_ref — the
    existing branch/worktree_state path runs unchanged."""
    import gh_pr_ensure
    captured = {}
    monkeypatch.setattr(
        gh_pr_ensure, "ensure_pr",
        lambda branch, repo_root, **kw: (
            captured.update(branch=branch), {"pr_number": 101, "created": True, "reason": None}
        )[1],
    )

    result = pe.enforce_pr_exists(**_kwargs(work_ref=""))

    assert result.ok is True
    assert captured["branch"] == "dispatch/d1"
