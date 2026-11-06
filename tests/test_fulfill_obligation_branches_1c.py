"""tests/test_fulfill_obligation_branches_1c.py — golf 2, G2-1c.

Characterisation of the two ``decision["kind"]`` branches that G2-1c moves out
of ``fulfill_obligation`` into private helpers:

  * ``escalate_stay_pending`` -> ``_fulfill_escalate_stay_pending``
  * ``stay_pending``          -> ``_fulfill_stay_pending``

Built on the fixtures of ``tests/test_fulfill_obligation_branches_1b.py``:
every test drives the REAL ``fulfill_obligation`` against a real obligation
file in a throwaway state dir (``tmp_path``). Only the pre-execution decision —
the fact the branch under test is *about* — is patched; everything the branch
then writes is the real writer.

Written and run GREEN on the old code first (base ``7c6f51a3``), so the suite
is the baseline the move must preserve. It asserts the whole outcome dict, the
obligation file on disk, and that these branches stay silent on the log.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import (  # noqa: E402
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    register_obligation,
    update_obligation,
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


def _seed_attempts(path: Path, attempts: int) -> None:
    """Book a prior attempt count the way the runner itself would."""
    update_obligation(path, attempts=attempts)


def _patch_decision(monkeypatch, decision: dict) -> dict:
    """Patch the pre-execution decision.

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


def _hours(attempts: int) -> str:
    return str(round(attempts * 900 / 3600, 1))


# ---------------------------------------------------------------------------
# 1. escalate_stay_pending — the bounded loud terminal, at two attempt counts
# ---------------------------------------------------------------------------


def test_escalate_stay_pending_books_not_executable_after_a_short_wait(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1c-escalate-short"
    path = _register(state_dir, dispatch_id)
    _seed_attempts(path, 5)
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "escalate_stay_pending"})

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "not_executable",
        "detail": (
            "obligation waited 6 attempts for a PR that never appeared — "
            "escalating the unbounded wait to a loud terminal outcome (OI-1532)"
        ),
    }
    stored = _record(path)
    assert stored["status"] == STATUS_NOT_EXECUTABLE
    assert stored["attempts"] == 6
    assert stored["reason"] == "stay_pending_timeout"
    assert stored["reason_detail"] == (
        "obligation waited 6 attempts (≈ 1.5h at the 900s cadence) for "
        "a PR that never appeared, with the branch existing or "
        "undetermined. The unbounded wait is closed loudly: either "
        "the dispatch is stuck (its gate will never run) or it was "
        "never going to produce a PR. Reset to pending if the "
        "dispatch is known to be live (OI-1532)."
    )
    # This branch writes no branch name: it must stay as registered.
    assert stored["branch"] is None
    _assert_iso_timestamp(stored["resolved_at"])
    assert stored["last_attempt_at"] == stored["resolved_at"]
    _assert_only_expected_log_lines(caplog)


def test_escalate_stay_pending_books_not_executable_after_a_long_wait(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1c-escalate-long"
    path = _register(state_dir, dispatch_id)
    _seed_attempts(path, 95)
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "escalate_stay_pending"})

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "not_executable",
        "detail": (
            "obligation waited 96 attempts for a PR that never appeared — "
            "escalating the unbounded wait to a loud terminal outcome (OI-1532)"
        ),
    }
    stored = _record(path)
    assert stored["status"] == STATUS_NOT_EXECUTABLE
    assert stored["attempts"] == 96
    assert stored["reason"] == "stay_pending_timeout"
    assert stored["reason_detail"] == (
        f"obligation waited 96 attempts (≈ {_hours(96)}h at the 900s cadence) for "
        "a PR that never appeared, with the branch existing or "
        "undetermined. The unbounded wait is closed loudly: either "
        "the dispatch is stuck (its gate will never run) or it was "
        "never going to produce a PR. Reset to pending if the "
        "dispatch is known to be live (OI-1532)."
    )
    assert stored["branch"] is None
    _assert_iso_timestamp(stored["resolved_at"])
    assert stored["last_attempt_at"] == stored["resolved_at"]
    _assert_only_expected_log_lines(caplog)


def test_escalate_stay_pending_hours_differ_between_attempt_counts():
    """The two runs above must not collapse to the same wait in the record."""
    assert _hours(6) != _hours(96)


# ---------------------------------------------------------------------------
# 2. stay_pending — a genuine wait, silent and without a reason
# ---------------------------------------------------------------------------


def test_stay_pending_keeps_the_obligation_pending_without_a_reason(tmp_path, monkeypatch, caplog):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1c-stay-pending"
    path = _register(state_dir, dispatch_id)
    before = _record(path)
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "stay_pending"})

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "pending",
        "detail": "no PR resolvable yet — stays pending for the freshness monitor",
    }
    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == 1
    # This branch writes NO reason: the record keeps what it had (None here).
    assert stored["reason"] == before["reason"]
    assert stored["reason_detail"] == before["reason_detail"]
    assert stored["branch"] == before["branch"]
    assert stored["resolved_at"] == before["resolved_at"] is None
    _assert_iso_timestamp(stored["last_attempt_at"])
    _assert_only_expected_log_lines(caplog)


def test_stay_pending_does_not_overwrite_an_existing_reason(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1c-stay-pending-reason"
    path = _register(state_dir, dispatch_id)
    update_obligation(path, reason="prior_reason", reason_detail="prior detail")
    before = _record(path)
    record = _record(path)
    _patch_decision(monkeypatch, {"kind": "stay_pending"})

    runner.fulfill_obligation(state_dir, path, record)

    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == before["attempts"] + 1
    assert stored["reason"] == "prior_reason"
    assert stored["reason_detail"] == "prior detail"
    assert stored["branch"] == before["branch"]


# ---------------------------------------------------------------------------
# 3. The pre-execution decision is what routes the branches (patch-binding net)
# ---------------------------------------------------------------------------


def test_the_branch_is_chosen_by_the_pre_execution_decision(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1c-binding"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    seen = _patch_decision(monkeypatch, {"kind": "stay_pending"})

    runner.fulfill_obligation(state_dir, path, record)

    assert seen, "the patched decision was never reached — the branch would run for real"
    assert seen["dispatch_id"] == dispatch_id
    assert seen["gate"] == GATE
    assert seen["attempts"] == 1
