"""tests/test_fulfill_obligation_attempt_gate_1d.py — golf 2, G2-1d.

Characterisation of the four blocks G2-1d moves out of the ``attempt_gate``
path of ``fulfill_obligation`` into private helpers:

  * the takeover booking          -> ``_fulfill_by_takeover``
  * the provider_not_installed    -> ``_drop_provider_not_installed_result``
    result cleanup
  * the temporary refusal         -> ``_fulfill_temporary_refusal``
  * the runner-error handler      -> ``_fulfill_runner_error``

Built on the fixtures of ``tests/test_fulfill_obligation_branches_1c.py`` and
``tests/test_fulfill_obligation_branches_1b.py``: every test drives the REAL
``fulfill_obligation`` against a real obligation file in a throwaway state dir
(``tmp_path``) through the real launch guard (a real ``flock``-based run
lock). Only the facts these blocks are *about* are patched — the pre-execution
decision, the PR resolution, the review-gate manager, the PR-head lookup and
the takeover walk — exactly as ``tests/test_gate_obligation*.py`` already do
it. Everything the blocks then write is the real writer.

Written and run GREEN on the old code first (base ``8d209937``), so the suite
is the baseline the move must preserve. It asserts the whole outcome dict, the
obligation file on disk, the result file on disk, and the log lines that carry
behaviour (the provider_not_installed removal warning).
"""

from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import (  # noqa: E402
    REASON_GATE_PARKED,
    REASON_GATE_PARKED_TIMEOUT,
    STATUS_FAILED,
    STATUS_FULFILLED,
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    register_obligation,
    update_obligation,
)

GATE = "codex_gate"
SUCCESSOR_GATE = "kimi_gate"
PR_NUMBER = 4321
OWNER_REPO = "Vinix24/vnx-orchestration"
HEAD_SHA = "a1" * 20


# ---------------------------------------------------------------------------
# Fixtures — a real state dir, a real obligation file and a real run lock
# ---------------------------------------------------------------------------


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "obligations").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
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


class _ScriptedManager:
    """The manager ``_build_manager`` returns: no gate runs, a scripted record is written.

    ``payload`` is the result record the run leaves for the declared gate
    (``None``: the run leaves no declared-gate record at all, e.g. because the
    declared gate never produced one and a takeover successor did);
    ``error`` makes ``request_and_execute`` raise instead.
    """

    def __init__(self, state_dir: Path, *, payload: Optional[Dict[str, Any]] = None,
                 error: Optional[BaseException] = None) -> None:
        self.state_dir = Path(state_dir)
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []

    def _request_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "requests" / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"

    def request_and_execute(self, *, pr_number, branch, review_stack, risk_class,
                            changed_files, mode, dispatch_id="", **extra):
        self.calls.append({
            "pr_number": pr_number, "branch": branch, "review_stack": list(review_stack),
            "risk_class": risk_class, "changed_files": list(changed_files), "mode": mode,
            "dispatch_id": dispatch_id, **extra,
        })
        if self.error is not None:
            raise self.error
        if self.payload is not None:
            self._request_path(GATE, pr_number).write_text(
                json.dumps({"gate": GATE, "pr_number": pr_number, "status": "completed"}), encoding="utf-8",
            )
            record = {"gate": GATE, "pr_number": pr_number, "commit_sha": HEAD_SHA, **self.payload}
            self._result_path(GATE, pr_number).write_text(json.dumps(record), encoding="utf-8")
        return {
            "pr_number": pr_number,
            "branch": branch,
            "gates": [],
            "has_required_failure": bool((self.payload or {}).get("has_required_failure")),
        }


def _patch_attempt_gate(monkeypatch, seen: Optional[dict] = None) -> dict:
    """Patch the decision (attempt_gate) and the PR resolution.

    Rule 1 of the refactor-safe skill: patch where the name is LOOKED UP — the
    runner module — and assert the patch was actually hit, so a rename cannot
    leave the test green while the real decision runs.
    """
    seen = {} if seen is None else seen

    def fake_decision(state_dir, dispatch_id, gate, pr_resolution, attempts, result_index, snapshot=None):
        seen.update(
            state_dir=state_dir, dispatch_id=dispatch_id, gate=gate,
            resolution=pr_resolution, attempts=attempts, snapshot=snapshot,
        )
        return {"kind": "attempt_gate"}

    resolution = runner.PrResolution(
        runner.RESOLUTION_RESOLVED, pr_number=PR_NUMBER, owner_repo=OWNER_REPO,
    )
    monkeypatch.setattr(runner, "_pre_execution_decision", fake_decision)
    monkeypatch.setattr(runner, "resolve_pr_number", lambda *a, **k: resolution)
    return seen


def _patch_manager(monkeypatch, manager: _ScriptedManager, *, takeover=None) -> None:
    """Hermetic: no git, no gh, no real gate, no real takeover chain walk."""
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: HEAD_SHA)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo, snapshot=None: None)
    monkeypatch.setattr(
        runner, "_resolve_github_owner_repo", lambda state_dir: OWNER_REPO,
    )
    monkeypatch.setattr(runner, "_find_takeover_successor_evidence", lambda *a, **k: takeover)
    fake_rgm = types.ModuleType("review_gate_manager")
    fake_rgm._compute_changed_files = lambda branch: ["scripts/lib/foo.py"]
    monkeypatch.setitem(sys.modules, "review_gate_manager", fake_rgm)


def _takeover_hit(state_dir: Path, *, status: str) -> tuple:
    """A real successor result record on disk and the walk's answer for it."""
    successor_path = state_dir / "review_gates" / "results" / f"pr-{PR_NUMBER}-{SUCCESSOR_GATE}.json"
    record = {"gate": SUCCESSOR_GATE, "pr_number": PR_NUMBER, "status": status, "commit_sha": HEAD_SHA}
    successor_path.write_text(json.dumps(record), encoding="utf-8")
    return (SUCCESSOR_GATE, successor_path, record, [GATE, SUCCESSOR_GATE])


def _assert_iso_timestamp(value) -> None:
    assert isinstance(value, str) and value.endswith("Z") and "T" in value


# ---------------------------------------------------------------------------
# 1. The takeover booking — a passing and a failing successor record
# ---------------------------------------------------------------------------


def test_takeover_with_a_passing_successor_books_fulfilled(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1d-takeover-pass"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    hit = _takeover_hit(state_dir, status="pass")
    seen = _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload=None)
    _patch_manager(monkeypatch, manager, takeover=hit)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    successor_gate, successor_path = hit[0], hit[1]
    assert seen, "the patched decision was never reached — the branch would run for real"
    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": STATUS_FULFILLED,
        "request_path": str(manager._request_path(GATE, PR_NUMBER)),
        "result_path": str(successor_path),
        "resolved_by_gate": successor_gate,
        "has_required_failure": False,
    }
    stored = _record(path)
    assert stored["status"] == STATUS_FULFILLED
    assert stored["attempts"] == 1
    assert stored["pr_number"] == PR_NUMBER
    assert stored["branch"] == f"dispatch/{dispatch_id}"
    assert stored["request_path"] == str(manager._request_path(GATE, PR_NUMBER))
    assert stored["result_path"] == str(successor_path)
    assert stored["resolved_by_gate"] == successor_gate
    assert stored["fulfilled_by"] == successor_gate
    assert stored["takeover_gate"] == successor_gate
    assert stored["takeover_hops"] == [GATE, SUCCESSOR_GATE]
    assert stored["evidence_result_path"] == str(successor_path)
    assert stored["reason"] == runner.REASON_FULFILLED_BY_TAKEOVER
    assert stored["reason_detail"] == (
        f"{GATE} never produced a decided verdict of its own for PR "
        f"#{PR_NUMBER} -- the review-gate takeover chain ({GATE} -> {SUCCESSOR_GATE}) "
        f"already substituted {SUCCESSOR_GATE} as the reader, and "
        f"{SUCCESSOR_GATE} produced a complete-evidence verdict "
        f"(passed). Booked against {GATE}'s obligation with "
        f"resolved_by_gate={SUCCESSOR_GATE!r} (D2e)."
    )
    _assert_iso_timestamp(stored["resolved_at"])
    _assert_iso_timestamp(stored["last_attempt_at"])


def test_takeover_with_a_failing_successor_books_failed(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1d-takeover-fail"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    hit = _takeover_hit(state_dir, status="fail")
    _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload=None)
    _patch_manager(monkeypatch, manager, takeover=hit)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    successor_gate, successor_path = hit[0], hit[1]
    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": STATUS_FAILED,
        "request_path": str(manager._request_path(GATE, PR_NUMBER)),
        "result_path": str(successor_path),
        "resolved_by_gate": successor_gate,
        "has_required_failure": True,
    }
    stored = _record(path)
    assert stored["status"] == STATUS_FAILED
    assert stored["attempts"] == 1
    assert stored["reason"] == runner.REASON_FAILED_BY_TAKEOVER
    assert stored["reason_detail"] == (
        f"{GATE} never produced a decided verdict of its own for PR "
        f"#{PR_NUMBER} -- the review-gate takeover chain ({GATE} -> {SUCCESSOR_GATE}) "
        f"already substituted {SUCCESSOR_GATE} as the reader, and "
        f"{SUCCESSOR_GATE} produced a complete-evidence verdict "
        f"(status: fail). Booked against {GATE}'s obligation with "
        f"resolved_by_gate={SUCCESSOR_GATE!r} (D2e)."
    )
    assert stored["fulfilled_by"] == successor_gate
    assert stored["takeover_hops"] == [GATE, SUCCESSOR_GATE]
    _assert_iso_timestamp(stored["resolved_at"])


# ---------------------------------------------------------------------------
# 2. The provider_not_installed cleanup — removed only for that exact shape
# ---------------------------------------------------------------------------


def test_provider_not_installed_result_is_removed_and_the_obligation_stays_pending(
    tmp_path, monkeypatch, caplog,
):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1d-provider-missing"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={
        "status": STATUS_NOT_EXECUTABLE, "reason": "provider_not_installed",
    })
    _patch_manager(monkeypatch, manager)

    with caplog.at_level(logging.DEBUG, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    result_file = manager._result_path(GATE, PR_NUMBER)
    assert not result_file.exists(), "the provider_not_installed record must be removed"
    assert outcome["action"] == "pending"
    assert outcome["detail"] == (
        f"{GATE} is parked (provider_not_installed: the provider binary is not "
        "on PATH in this environment yet), not broken — the obligation waits "
        "for it to be re-enabled/installed or for a future run, not permanently refused"
    )
    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == 1
    assert stored["reason"] == REASON_GATE_PARKED
    assert stored["reason_detail"] == outcome["detail"]
    warnings = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "removed provider_not_installed" in r.getMessage()
    ]
    assert warnings == [
        f"gate_obligation_runner: removed provider_not_installed result for "
        f"pr={PR_NUMBER} gate={GATE} — provider availability is an environment "
        "fact, not PR evidence (OI-1469)"
    ]


def test_another_status_keeps_the_result_file(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1d-result-kept"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={"status": "pass"})
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    result_file = manager._result_path(GATE, PR_NUMBER)
    assert result_file.exists(), "only the provider_not_installed shape is removed"
    assert outcome["action"] == STATUS_FULFILLED
    assert _record(path)["status"] == STATUS_FULFILLED


# ---------------------------------------------------------------------------
# 3. The temporary refusal — below, at and at-threshold-with-another-reason
# ---------------------------------------------------------------------------


def test_temporary_refusal_below_the_threshold_stays_pending(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1d-temp-below"
    path = _register(state_dir, dispatch_id)
    _seed_attempts(path, 10)
    record = _record(path)
    _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={
        "status": STATUS_NOT_EXECUTABLE, "reason": "provider_disabled",
    })
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "pending",
        "detail": (
            f"{GATE} is parked (provider_disabled: a config flag has it disabled), "
            "not broken — the obligation waits for it to be re-enabled/installed "
            "or for a future run, not permanently refused"
        ),
    }
    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == 11
    assert stored["reason"] == REASON_GATE_PARKED
    assert stored["result_path"] == str(manager._result_path(GATE, PR_NUMBER))
    assert "resolved_at" not in stored or stored["resolved_at"] is None


def test_temporary_refusal_at_the_threshold_with_a_parked_gate_escalates(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1d-temp-threshold-parked"
    path = _register(state_dir, dispatch_id)
    _seed_attempts(path, 95)
    record = _record(path)
    _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={
        "status": STATUS_NOT_EXECUTABLE, "reason": "provider_disabled",
    })
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    escalation_detail = (
        f"{GATE} stayed temporarily unavailable ({REASON_GATE_PARKED}) for "
        "96 attempts (last status='not_executable', "
        "reason='provider_disabled') — escalating to a loud terminal failure"
    )
    assert outcome["action"] == STATUS_NOT_EXECUTABLE
    assert outcome["detail"] == escalation_detail
    stored = _record(path)
    assert stored["status"] == STATUS_NOT_EXECUTABLE
    assert stored["attempts"] == 96
    assert stored["reason"] == REASON_GATE_PARKED_TIMEOUT
    assert stored["reason_detail"] == escalation_detail
    assert stored["result_path"] == str(manager._result_path(GATE, PR_NUMBER))
    _assert_iso_timestamp(stored["resolved_at"])


def test_temporary_refusal_at_the_threshold_with_the_other_parked_reason_escalates(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1d-temp-threshold-unavailable"
    path = _register(state_dir, dispatch_id)
    _seed_attempts(path, 95)
    record = _record(path)
    _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={
        "status": STATUS_NOT_EXECUTABLE, "reason": "provider_not_installed",
    })
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    escalation_detail = (
        f"{GATE} stayed temporarily unavailable ({REASON_GATE_PARKED}) for "
        "96 attempts (last status='not_executable', "
        "reason='provider_not_installed') — escalating to a loud terminal failure"
    )
    assert outcome["action"] == STATUS_NOT_EXECUTABLE
    assert outcome["detail"] == escalation_detail
    stored = _record(path)
    assert stored["status"] == STATUS_NOT_EXECUTABLE
    assert stored["reason"] == REASON_GATE_PARKED_TIMEOUT
    assert stored["reason_detail"] == escalation_detail


def test_temporary_refusal_with_a_non_parked_reason_uses_the_generic_timeout_suffix(
    tmp_path, monkeypatch,
):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1d-temp-threshold-outage"
    path = _register(state_dir, dispatch_id)
    _seed_attempts(path, 95)
    record = _record(path)
    _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={"status": "unavailable", "reason": "timeout"})
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome["action"] == STATUS_NOT_EXECUTABLE
    stored = _record(path)
    assert stored["status"] == STATUS_NOT_EXECUTABLE
    assert stored["reason"] == "gate_run_unavailable_timeout"
    assert stored["reason_detail"] == (
        f"{GATE} stayed temporarily unavailable (gate_run_unavailable) for "
        "96 attempts (last status='unavailable', "
        "reason='timeout') — escalating to a loud terminal failure"
    )


# ---------------------------------------------------------------------------
# 4. The runner-error handler — a loud record, a booking, and the lock released
# ---------------------------------------------------------------------------


def test_runner_error_books_not_executable_and_releases_the_launch_lock(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1d-runner-error"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, error=RuntimeError("boom"))
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": STATUS_NOT_EXECUTABLE,
        "detail": "RuntimeError: boom",
    }
    stored = _record(path)
    assert stored["status"] == STATUS_NOT_EXECUTABLE
    assert stored["attempts"] == 1
    assert stored["reason"] == "runner_error"
    assert stored["reason_detail"] == "RuntimeError: boom"
    assert stored["result_path"] == str(
        state_dir / "review_gates" / "results" / f"pr-{PR_NUMBER}-{GATE}.json"
    )
    assert Path(stored["result_path"]).exists()
    _assert_iso_timestamp(stored["resolved_at"])

    # The lock must be released by the ``finally``: a SECOND fulfilment attempt
    # acquires it (an unreleased lock would answer ``in_flight`` with a
    # ``run_lock`` field instead of re-checking the booked obligation).
    second_record = _record(path)
    second_manager = _ScriptedManager(state_dir, error=RuntimeError("second"))
    _patch_manager(monkeypatch, second_manager)
    second = runner.fulfill_obligation(state_dir, path, second_record)
    assert "run_lock" not in second, second
    assert second["action"] == STATUS_NOT_EXECUTABLE
    assert second["detail"] == (
        "booked not_executable by another run while this launch waited for the "
        "run lock — not run again"
    )
    assert second_manager.calls == []
