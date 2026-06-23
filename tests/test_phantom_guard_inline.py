"""test_phantom_guard_inline.py — P0.2: the inline govern-time hook (guard_at_govern +
record_phantom_if_any). The git-diff resolution is isolated from these tests via monkeypatch;
the pure phantom decision is covered in test_phantom_guard.py."""
from __future__ import annotations

import sys
from pathlib import Path

import phantom_guard as pg

# record_phantom_if_any lazily imports append_receipt (top-level scripts/, not scripts/lib).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_guard_at_govern_abstains_on_unresolvable_ref():
    # an unresolvable branch/worktree must ABSTAIN (ok), never false-reject as "empty diff"
    v = pg.guard_at_govern(dispatch_id="no-such-dispatch-xyz", role="backend-developer",
                           status="done", token_usage=0)
    assert not v.is_phantom
    assert "ABSTAIN" in v.reason


def test_guard_at_govern_override(monkeypatch):
    monkeypatch.setenv("VNX_OVERRIDE_PHANTOM_GUARD", "1")
    v = pg.guard_at_govern(dispatch_id="x", role="backend-developer", status="done")
    assert not v.is_phantom
    assert "overridden" in v.reason


def test_record_phantom_appends_corrective_failed_receipt(monkeypatch, tmp_path):
    # force a phantom verdict to isolate the propagation from the git diff
    monkeypatch.setattr(pg, "guard_at_govern",
                        lambda **kw: pg.PhantomVerdict(True, "PHANTOM: test reason"))
    import append_receipt
    captured = {}
    monkeypatch.setattr(append_receipt, "append_receipt_payload",
                        lambda payload, **kw: captured.update(payload=payload, kw=kw))
    v = pg.record_phantom_if_any(dispatch_id="d1", role="backend-developer", status="done",
                                 receipts_file=str(tmp_path / "r.ndjson"))
    assert v.is_phantom
    assert captured["payload"]["status"] == "failed"
    assert captured["payload"]["phantom_rejected"] is True
    assert captured["payload"]["dispatch_id"] == "d1"
    # event_type must be one the watchers honor (ACTIONABLE_EVENTS) — codex F3
    assert captured["payload"]["event_type"] == "subprocess_completion"
    assert captured["payload"]["synthesized"] is False  # else dedup Tier-2 drops it
    # timestamp must be the ...Z format the other receipts use — codex F4
    assert captured["payload"]["timestamp"].endswith("Z")


def test_precaptured_worktree_diff_bypasses_resolution():
    # the provider lane passes a pre-captured diff (its worktree is torn down before govern). Empty
    # pre-captured diff + delivery + done -> phantom (no abstain, the diff is authoritative here).
    v = pg.guard_at_govern(dispatch_id="d1", role="backend-developer", status="done",
                           token_usage=0, worktree_diff="")
    assert v.is_phantom
    v2 = pg.guard_at_govern(dispatch_id="d1", role="backend-developer", status="done",
                            worktree_diff="diff --git a/x b/x\n+y\n")
    assert not v2.is_phantom


def test_dedup_phantom_rejected_wins_over_worker_done():
    import dispatch_govern as dg
    worker = {"dispatch_id": "d1", "status": "done", "timestamp": "2026-06-23T10:00:00Z"}
    corrective = {"dispatch_id": "d1", "status": "failed", "phantom_rejected": True,
                  "timestamp": "2026-06-23T10:00:00Z"}  # same-second tie
    # worker appended first; without Tier-0 the tie would let 'done' win
    assert dg.dedup_completion_receipts([worker, corrective])["status"] == "failed"
    assert dg.dedup_completion_receipts([corrective, worker])["status"] == "failed"


def test_record_phantom_no_append_when_not_phantom(monkeypatch, tmp_path):
    monkeypatch.setattr(pg, "guard_at_govern", lambda **kw: pg.PhantomVerdict(False, "ok"))
    import append_receipt
    calls = {"n": 0}
    monkeypatch.setattr(append_receipt, "append_receipt_payload",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    v = pg.record_phantom_if_any(dispatch_id="d1", role="backend-developer", status="done",
                                 receipts_file=str(tmp_path / "r.ndjson"))
    assert not v.is_phantom
    assert calls["n"] == 0


def test_record_phantom_append_failure_is_non_fatal(monkeypatch, tmp_path):
    # a corrective-append failure must NOT raise (govern must not lose the report)
    monkeypatch.setattr(pg, "guard_at_govern",
                        lambda **kw: pg.PhantomVerdict(True, "PHANTOM"))
    import append_receipt

    def _boom(*a, **k):
        raise RuntimeError("append exploded")

    monkeypatch.setattr(append_receipt, "append_receipt_payload", _boom)
    v = pg.record_phantom_if_any(dispatch_id="d1", role="backend-developer", status="done",
                                 receipts_file=str(tmp_path / "r.ndjson"))
    assert v.is_phantom  # verdict still returned, no exception escaped
