"""tests/test_fulfill_obligation_branches_1a.py — golf 2, G2-1a.

Characterisation of the three ``decision["kind"]`` branches that G2-1a moves
out of ``fulfill_obligation`` into private helpers:

  * ``sha_unverifiable``                -> ``_fulfill_sha_unverifiable``
  * ``unresolvable`` / ``escalate``      -> ``_fulfill_unresolvable_or_escalate``
  * ``retire``                           -> ``_fulfill_retire``

Every test drives the REAL ``fulfill_obligation`` against a real obligation
file in a throwaway state dir (``tmp_path``). Only the two inputs above the
branch --- the pre-execution decision and, where the branch reads it, the PR
resolution --- are patched, because those are the two facts the branch under
test is *about*; everything the branch then writes is the real writer.

Written and run GREEN on the old code first (base ``01f8db0a``), so the suite
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
    REASON_NO_PR_BRANCH_GONE,
    REASON_PR_CLOSED,
    REASON_PR_MERGED,
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    STATUS_RETIRED,
    STATUS_UNRESOLVABLE,
    obligation_path,
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


# ---------------------------------------------------------------------------
# 1. sha_unverifiable — suspends judgement, stays pending, names the reason
# ---------------------------------------------------------------------------


def test_sha_unverifiable_stays_pending_with_a_named_reason(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-sha")
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "sha_unverifiable", "detail": "head_sha is missing"})

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": "dlv-g2-1a-sha",
        "gate": GATE,
        "action": "pending",
        "detail": "existing evidence found but its sha binding is unverifiable",
    }
    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == 1
    assert stored["reason"] == "sha_binding_unverifiable"
    assert stored["reason_detail"] == (
        f"{GATE} has existing complete evidence for dispatch dlv-g2-1a-sha but its "
        "sha binding to the PR head could not be verified (head_sha is missing) — "
        "suspending judgement (OI-1571 tak 3) rather than silently accepting or "
        "discarding it; will re-check on the next run"
    )
    assert stored["resolved_at"] is None
    _assert_iso_timestamp(stored["last_attempt_at"])
    _assert_only_expected_log_lines(caplog)


# ---------------------------------------------------------------------------
# 2. unresolvable / escalate — a fault, not a wait
# ---------------------------------------------------------------------------


def test_unresolvable_records_a_fault_and_stays_retryable(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-unresolvable")
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "unresolvable", "detail": "PR state undeterminable"})

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": "dlv-g2-1a-unresolvable",
        "gate": GATE,
        "action": "unresolvable",
        "detail": "PR state undeterminable",
    }
    stored = _record(path)
    assert stored["status"] == STATUS_UNRESOLVABLE
    assert stored["attempts"] == 1
    assert stored["reason"] == "unresolvable_repo"
    assert stored["reason_detail"] == "PR state undeterminable"
    assert stored["resolved_at"] is None
    _assert_iso_timestamp(stored["last_attempt_at"])
    _assert_only_expected_log_lines(caplog)


def test_unresolvable_falls_back_to_the_resolutions_own_reason(tmp_path, monkeypatch):
    """No ``decision["detail"]``: ``resolution.reason`` is the fallback (OI-1508)."""
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-unresolvable-reason", pr_number=None)
    record = _record(path)
    _patch_decision(
        monkeypatch, {"kind": "unresolvable"},
        resolution=runner.PrResolution(runner.RESOLUTION_UNRESOLVABLE, reason="no owner repo"),
    )

    outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome["action"] == "unresolvable"
    assert outcome["detail"] == "no owner repo"
    stored = _record(path)
    assert stored["status"] == STATUS_UNRESOLVABLE
    assert stored["reason_detail"] == "no owner repo"


def test_escalate_books_the_loud_terminal_not_executable(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-escalate")
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "escalate", "detail": "repo unattributable"})

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": "dlv-g2-1a-escalate",
        "gate": GATE,
        "action": "not_executable",
        "detail": "repo unattributable",
    }
    stored = _record(path)
    assert stored["status"] == STATUS_NOT_EXECUTABLE
    assert stored["attempts"] == 1
    assert stored["reason"] == "unresolvable_timeout"
    assert stored["reason_detail"] == (
        "PR unresolved after 1 attempts because the environment is misconfigured: "
        "repo unattributable. Fix the project attribution (VNX_PROJECT_ID / "
        "~/.vnx/projects.json / the checkout's git origin remote) and reset this "
        "obligation to pending."
    )
    _assert_iso_timestamp(stored["resolved_at"])
    _assert_only_expected_log_lines(caplog)


def test_escalate_carries_the_rejected_evidence_note(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-escalate-note")
    record = _record(path)
    _patch_decision(monkeypatch, {
        "kind": "escalate",
        "detail": "env wrong",
        "mismatch_detail": "abc123 != def456",
    })

    runner.fulfill_obligation(state_dir, path, record)

    detail = _record(path)["reason_detail"]
    note = (
        f" A prior {GATE} result exists for this dispatch but is about a DIFFERENT "
        "commit (abc123 != def456) and was rejected as rescue evidence, never "
        "silently reused (OI-1571 tak 3)."
    )
    assert detail.startswith(
        "PR unresolved after 1 attempts because the environment is misconfigured: "
        "env wrong."
    )
    assert detail.endswith(note)
    assert detail.count(note) == 1


# ---------------------------------------------------------------------------
# 3. retire — every return path
# ---------------------------------------------------------------------------


def test_retire_merged_pr_is_retired_with_its_number(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-retire-merged")
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "retire", "retire_reason": REASON_PR_MERGED})

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": "dlv-g2-1a-retire-merged",
        "gate": GATE,
        "action": STATUS_RETIRED,
        "detail": f"PR #{PR_NUMBER} is merged — retired",
    }
    stored = _record(path)
    assert stored["status"] == STATUS_RETIRED
    assert stored["pr_number"] == PR_NUMBER
    assert stored["attempts"] == 1
    assert stored["reason"] == REASON_PR_MERGED
    assert stored["reason_detail"] == (
        f"PR #{PR_NUMBER} for dispatch dlv-g2-1a-retire-merged is merged and no "
        f"rescuing {GATE} evidence was found — nothing left for {GATE} to guard."
    )
    _assert_iso_timestamp(stored["resolved_at"])
    _assert_only_expected_log_lines(caplog)


def test_retire_closed_pr_says_closed_without_merge(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-retire-closed")
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "retire", "retire_reason": REASON_PR_CLOSED})

    outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome["action"] == STATUS_RETIRED
    assert outcome["detail"] == f"PR #{PR_NUMBER} is closed without merge — retired"
    stored = _record(path)
    assert stored["status"] == STATUS_RETIRED
    assert stored["reason"] == REASON_PR_CLOSED
    assert "is closed without merge and no rescuing" in stored["reason_detail"]


def test_retire_branch_gone_without_a_pr_retires_the_dispatch_branch(tmp_path, monkeypatch):
    """No ``retire_reason``: the default no-PR/branch-gone path (OI-1388)."""
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-retire-gone", pr_number=None)
    record = _record(path)
    _patch_decision(
        monkeypatch, {"kind": "retire"},
        resolution=runner.PrResolution(runner.RESOLUTION_AWAITING),
    )

    outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": "dlv-g2-1a-retire-gone",
        "gate": GATE,
        "action": STATUS_RETIRED,
        "detail": "dispatch died without ever producing a PR; branch gone — retired",
    }
    stored = _record(path)
    assert stored["status"] == STATUS_RETIRED
    assert stored["branch"] == "dispatch/dlv-g2-1a-retire-gone"
    assert stored["reason"] == REASON_NO_PR_BRANCH_GONE
    assert stored["reason_detail"] == (
        "dispatch dlv-g2-1a-retire-gone never produced a PR and its head branch "
        "dispatch/dlv-g2-1a-retire-gone no longer exists on the resolved repo — "
        f"nothing left for {GATE} to guard."
    )


def test_retire_branch_gone_names_the_resolved_owner_repo(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-retire-owner", pr_number=None)
    record = _record(path)
    _patch_decision(
        monkeypatch, {"kind": "retire"},
        resolution=runner.PrResolution(
            runner.RESOLUTION_AWAITING, owner_repo="Vinix24/vnx-orchestration",
        ),
    )

    runner.fulfill_obligation(state_dir, path, record)

    assert "Vinix24/vnx-orchestration" in _record(path)["reason_detail"]


def test_retire_carries_the_unverifiable_evidence_note(tmp_path, monkeypatch):
    """The unverifiable shape says "could not be verified", never "DIFFERENT commit"."""
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-retire-note")
    record = _record(path)
    _patch_decision(monkeypatch, {
        "kind": "retire",
        "retire_reason": REASON_PR_MERGED,
        "mismatch_detail": "both shas missing",
        "rejected_evidence": "unverifiable",
    })

    runner.fulfill_obligation(state_dir, path, record)

    detail = _record(path)["reason_detail"]
    assert "could not be verified (both shas missing)" in detail
    assert "DIFFERENT commit" not in detail


# ---------------------------------------------------------------------------
# 4. The pre-execution decision is what routes the branch (patch-binding net)
# ---------------------------------------------------------------------------


def test_the_branch_is_chosen_by_the_pre_execution_decision(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    path = _register(state_dir, "dlv-g2-1a-binding")
    record = _record(path)
    seen = _patch_decision(monkeypatch, {"kind": "sha_unverifiable", "detail": "d"})

    runner.fulfill_obligation(state_dir, path, record)

    assert seen, "the patched decision was never reached — the branch would run for real"
    assert seen["dispatch_id"] == "dlv-g2-1a-binding"
    assert seen["gate"] == GATE
    assert seen["attempts"] == 1
    assert seen["resolution"].pr_number == PR_NUMBER
