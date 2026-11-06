"""tests/test_fulfill_obligation_branches_1b.py — golf 2, G2-1b.

Characterisation of the four ``decision["kind"]`` branches that G2-1b moves
out of ``fulfill_obligation`` into private helpers:

  * ``stay_pending_live``                    -> ``_fulfill_stay_pending_live``
  * ``stay_pending_dispatch_live``           -> ``_fulfill_stay_pending_dispatch_live``
  * ``stay_pending_unmeasured``              -> ``_fulfill_stay_pending_unmeasured``
  * ``escalate_stay_pending_unmeasured``     -> ``_fulfill_escalate_stay_pending_unmeasured``

Every test drives the REAL ``fulfill_obligation`` against a real obligation
file in a throwaway state dir (``tmp_path``). Only the two inputs above the
branch --- the pre-execution decision and, where the branch reads it, the PR
resolution --- are patched, because those are the two facts the branch under
test is *about*; everything the branch then writes is the real writer.

Written and run GREEN on the old code first (base ``8bb120f0``), so the suite
is the baseline the move must preserve. It asserts the whole outcome dict, the
obligation file on disk, and that these branches stay silent on the log.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import (  # noqa: E402
    REASON_DISPATCH_STILL_RUNNING,
    REASON_NO_PR_BRANCH_GONE_LIVE,
    REASON_NO_PR_BRANCH_GONE_UNMEASURED,
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    register_obligation,
)

GATE = "codex_gate"
PR_NUMBER = 4321


# ---------------------------------------------------------------------------
# Fixtures — a real state dir and a real obligation file
# ---------------------------------------------------------------------------


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "obligations").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    return state_dir


def _register(state_dir: Path, dispatch_id: str, *, pr_number: int | None = PR_NUMBER) -> Path:
    path = register_obligation(
        state_dir, dispatch_id=dispatch_id, gate=GATE, project_id="vnx-dev", pr_number=pr_number,
    )
    assert path is not None
    return path


def _record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _patch_decision(monkeypatch, decision: dict, *, resolution: "runner.PrResolution | None" = None) -> dict:
    """Patch the pre-execution decision (and optionally the PR resolution).

    Rule 1 of the refactor-safe skill: patch where the name is LOOKED UP — the
    runner module — and assert the patch was actually hit, so a rename cannot
    leave the test green while the real decision runs.
    """
    seen: dict = {}

    def fake_decision(state_dir, dispatch_id, gate, pr_resolution, attempts, result_index, snapshot=None):
        seen.update(
            state_dir=state_dir, dispatch_id=dispatch_id, gate=gate,
            resolution=pr_resolution, attempts=attempts, snapshot=snapshot,
        )
        return dict(decision)

    monkeypatch.setattr(runner, "_pre_execution_decision", fake_decision)
    if resolution is not None:
        monkeypatch.setattr(runner, "resolve_pr_number", lambda *a, **k: resolution)
    return seen


def _assert_only_expected_log_lines(caplog) -> None:
    """These branches are QUIET: no warning/error of their own.

    A move that loses (or gains) a log line changes behaviour just as much as
    a changed record — pinned here so the helper extraction cannot do it.
    """
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert loud == [], [r.getMessage() for r in loud]


def _assert_iso_timestamp(value) -> None:
    assert isinstance(value, str) and value.endswith("Z") and "T" in value


def _lock_path(state_dir: Path, dispatch_id: str) -> str:
    """The occupancy-lock path exactly as the branch spells it inside the detail."""
    return str(state_dir / "dispatch_worktree_claims" / f"{dispatch_id}.occupancy")


def _mismatch_note(gate: str, detail: str) -> str:
    """The proven-mismatch ``_rejected_evidence_note`` suffix (OI-1571 tak 3)."""
    return (
        f" A prior {gate} result exists for this dispatch but is about a DIFFERENT "
        f"commit ({detail}) and was rejected as rescue evidence, never "
        "silently reused (OI-1571 tak 3)."
    )


# ---------------------------------------------------------------------------
# 1. stay_pending_live — branch gone, dispatch positively live
# ---------------------------------------------------------------------------


def test_stay_pending_live_keeps_the_dispatch_pending(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1b-live"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "stay_pending_live"})

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "pending",
        "detail": (
            "dispatch still running (occupancy lock held) — not pushed yet, "
            "NOT retired (OI-1532)"
        ),
    }
    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == 1
    assert stored["branch"] == f"dispatch/{dispatch_id}"
    assert stored["reason"] == REASON_NO_PR_BRANCH_GONE_LIVE
    assert stored["reason_detail"] == (
        f"dispatch {dispatch_id} has no PR yet and its head branch "
        f"dispatch/{dispatch_id} is not on GitHub, but the dispatch's occupancy "
        "lock is held by a live process — it is still running and has not "
        "pushed yet. Staying pending; a live dispatch must never be retired "
        "(OI-1532)."
    )
    assert stored["resolved_at"] is None
    _assert_iso_timestamp(stored["last_attempt_at"])
    _assert_only_expected_log_lines(caplog)


def test_stay_pending_live_carries_the_rejected_evidence_note(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1b-live-note"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "stay_pending_live", "mismatch_detail": "abc123 != def456"})

    runner.fulfill_obligation(state_dir, path, record)

    note = _mismatch_note(GATE, "abc123 != def456")
    detail = _record(path)["reason_detail"]
    assert detail.endswith(note)
    assert detail.count(note) == 1


# ---------------------------------------------------------------------------
# 2. stay_pending_dispatch_live — resolved PR, but own dispatch still live
# ---------------------------------------------------------------------------


def test_stay_pending_dispatch_live_does_not_gate_the_previous_head(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1b-dispatch-live"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    decision_detail = (
        f"dispatch {dispatch_id} is still running (its occupancy lock "
        f"is held by a live process) — PR #{PR_NUMBER} is "
        "not gated and no verdict is booked against its current head "
        "until the dispatch has pushed and exited"
    )
    _patch_decision(
        monkeypatch, {"kind": "stay_pending_dispatch_live", "detail": decision_detail},
        resolution=runner.PrResolution(runner.RESOLUTION_RESOLVED, pr_number=PR_NUMBER),
    )

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "pending",
        "detail": decision_detail,
    }
    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == 1
    assert stored["reason"] == REASON_DISPATCH_STILL_RUNNING
    assert stored["reason_detail"] == (
        f"dispatch {dispatch_id} is still running (its occupancy lock "
        f"at {_lock_path(state_dir, dispatch_id)} is held by "
        f"a live process) and has not pushed yet, so PR "
        f"#{PR_NUMBER}'s current head is the previous "
        f"round's code. Not gating it and not booking any verdict "
        f"against it — the obligation stays pending until the dispatch "
        f"has pushed and exited, then it is evaluated against the new "
        f"head."
    )
    assert stored["resolved_at"] is None
    _assert_iso_timestamp(stored["last_attempt_at"])
    _assert_only_expected_log_lines(caplog)


# ---------------------------------------------------------------------------
# 3. stay_pending_unmeasured — branch gone, liveness unmeasurable
# ---------------------------------------------------------------------------


def test_stay_pending_unmeasured_stays_pending_on_ambiguous_evidence(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1b-unmeasured"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "stay_pending_unmeasured", "liveness": "unmeasured"})

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "pending",
        "detail": (
            "dispatch branch gone but liveness unmeasurable — staying pending "
            "rather than retiring on ambiguous evidence (OI-1532)"
        ),
    }
    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == 1
    assert stored["branch"] == f"dispatch/{dispatch_id}"
    assert stored["reason"] == REASON_NO_PR_BRANCH_GONE_UNMEASURED
    assert stored["reason_detail"] == (
        f"dispatch {dispatch_id} has no PR yet and its head branch "
        f"dispatch/{dispatch_id} is not on GitHub, and liveness could not be "
        f"measured (no occupancy lock file at "
        f"{_lock_path(state_dir, dispatch_id)} or the probe "
        f"failed). Staying pending rather than retiring on ambiguous "
        f"evidence — a live dispatch must never be closed unmeasured "
        f"(OI-1532)."
    )
    assert stored["resolved_at"] is None
    _assert_iso_timestamp(stored["last_attempt_at"])
    _assert_only_expected_log_lines(caplog)


def test_stay_pending_unmeasured_carries_the_rejected_evidence_note(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1b-unmeasured-note"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "stay_pending_unmeasured", "mismatch_detail": "abc123 != def456"})

    runner.fulfill_obligation(state_dir, path, record)

    note = _mismatch_note(GATE, "abc123 != def456")
    detail = _record(path)["reason_detail"]
    assert detail.endswith(note)
    assert detail.count(note) == 1


# ---------------------------------------------------------------------------
# 4. escalate_stay_pending_unmeasured — the bounded loud terminal
# ---------------------------------------------------------------------------


def test_escalate_stay_pending_unmeasured_books_not_executable(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1b-escalate"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    _patch_decision(
        monkeypatch, {"kind": "escalate_stay_pending_unmeasured", "liveness": "unmeasured"},
    )

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "not_executable",
        "detail": (
            "liveness unmeasurable for 1 consecutive attempts — "
            "escalating the unbounded unmeasured wait to a loud terminal "
            "outcome (OI-1587)"
        ),
    }
    stored = _record(path)
    assert stored["status"] == STATUS_NOT_EXECUTABLE
    assert stored["attempts"] == 1
    assert stored["branch"] == f"dispatch/{dispatch_id}"
    assert stored["reason"] == "stay_pending_unmeasured_timeout"
    assert stored["reason_detail"] == (
        f"dispatch {dispatch_id} has no PR, its head branch "
        f"dispatch/{dispatch_id} is not on GitHub, and liveness could not be "
        f"measured (no occupancy lock file at "
        f"{_lock_path(state_dir, dispatch_id)} or the probe "
        f"failed) for 1 consecutive attempts (≈ "
        f"0.2h at the 900s cadence). An "
        "unmeasurable state that never resolves is a measurement "
        "defect, not a wait (OI-1587). Restore the occupancy lock "
        "lane for this dispatch class, or reset this obligation to "
        f"pending if the dispatch is known to be live."
    )
    _assert_iso_timestamp(stored["resolved_at"])
    assert stored["last_attempt_at"] == stored["resolved_at"]
    _assert_only_expected_log_lines(caplog)


def test_escalate_stay_pending_unmeasured_carries_the_rejected_evidence_note(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1b-escalate-note"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    _patch_decision(monkeypatch, {
        "kind": "escalate_stay_pending_unmeasured",
        "mismatch_detail": "abc123 != def456",
    })

    runner.fulfill_obligation(state_dir, path, record)

    note = _mismatch_note(GATE, "abc123 != def456")
    detail = _record(path)["reason_detail"]
    assert detail.endswith(note)
    assert detail.count(note) == 1


# ---------------------------------------------------------------------------
# 5. The pre-execution decision is what routes the branch (patch-binding net)
# ---------------------------------------------------------------------------


def test_the_branch_is_chosen_by_the_pre_execution_decision(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1b-binding"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    seen = _patch_decision(monkeypatch, {"kind": "stay_pending_live"})

    runner.fulfill_obligation(state_dir, path, record)

    assert seen, "the patched decision was never reached — the branch would run for real"
    assert seen["dispatch_id"] == dispatch_id
    assert seen["gate"] == GATE
    assert seen["attempts"] == 1
