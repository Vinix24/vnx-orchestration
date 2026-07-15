"""Tests for pr_enforcement.py — the tmux-spawn build-dispatch auto-PR enforcement
chokepoint. gh_pr_ensure and append_receipt are mocked; nothing here touches
GitHub or a real git repo.
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

def test_not_applicable_when_not_pushed(monkeypatch):
    called = {"n": 0}

    def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("gh_pr_ensure must not be imported/called")

    import gh_pr_ensure
    monkeypatch.setattr(gh_pr_ensure, "ensure_pr", _boom)

    for state in ("clean", "committed", "dirty"):
        result = pe.enforce_pr_exists(**_kwargs(worktree_state=state))
        assert result.applicable is False
        assert result.ok is True
        assert result.pr_number is None
    assert called["n"] == 0


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
