"""tests/test_fulfill_obligation_attempt_gate_1e.py — golf 2, G2-1e.

Characterisation of the last four blocks G2-1e moves out of the ``attempt_gate``
path of ``fulfill_obligation`` into private helpers:

  * the declared MISMATCH hold      -> ``_fulfill_declared_sha_mismatch``
  * the declared UNKNOWN_SHA hold   -> ``_fulfill_declared_unknown_sha``
  * the head-moved hold             -> ``_fulfill_result_head_moved``
  * the final booking               -> ``_fulfill_book_declared_result``

Built on the fixtures of ``tests/test_fulfill_obligation_attempt_gate_1d.py``:
every test drives the REAL ``fulfill_obligation`` against a real obligation file
in a throwaway state dir (``tmp_path``) through the real launch guard (a real
``flock``-based run lock). Only the facts these blocks are *about* are patched —
the pre-execution decision, the PR resolution, the review-gate manager, the
PR-head lookup and the takeover walk. Everything the blocks write is the real
writer.

Written and run GREEN on the old code first (base ``80309e17``), so the suite is
the baseline the move must preserve. It asserts the whole outcome dict, the
obligation file on disk, and the log lines that carry behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import types
from pathlib import Path
from typing import Any, Callable, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import (  # noqa: E402
    REASON_FAILED_BY_GATE_VERDICT,
    STATUS_FAILED,
    STATUS_FULFILLED,
    STATUS_PENDING,
    register_obligation,
)

GATE = "codex_gate"
PR_NUMBER = 4321
OWNER_REPO = "Vinix24/vnx-orchestration"
HEAD_SHA = "a1" * 20
OLD_HEAD = "b2" * 20
NEW_HEAD = "c3" * 20
CONTRACT_HASH = "d4" * 32


# ---------------------------------------------------------------------------
# Fixtures — a real state dir, a real obligation file and a real run lock
# ---------------------------------------------------------------------------


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "obligations").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "reports").mkdir(parents=True, exist_ok=True)
    return state_dir


def _register(state_dir: Path, dispatch_id: str, *, pr_number: int | None = PR_NUMBER) -> Path:
    path = register_obligation(
        state_dir, dispatch_id=dispatch_id, gate=GATE, project_id="vnx-dev", pr_number=pr_number,
    )
    assert path is not None
    return path


def _record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _report_file(state_dir: Path, name: str = "codex-gate-report.md") -> str:
    """A real report file the result record can point at."""
    report = state_dir / "reports" / name
    report.write_text("# gate report\n\npassed\n", encoding="utf-8")
    return str(report)


class _ScriptedManager:
    """The manager ``_build_manager`` returns: no gate runs, a scripted record is written.

    ``payload`` is the result record the run leaves for the declared gate.
    ``error`` makes ``request_and_execute`` raise instead. ``on_run`` runs just
    before the call returns (used to move the PR head mid-run). ``age_seconds``
    backdates the written result file's mtime (the fast-fulfilment tripwire).
    """

    def __init__(self, state_dir: Path, *, payload: Optional[Dict[str, Any]] = None,
                 error: Optional[BaseException] = None,
                 on_run: Optional[Callable[[], None]] = None,
                 age_seconds: Optional[float] = None) -> None:
        self.state_dir = Path(state_dir)
        self.payload = payload
        self.error = error
        self.on_run = on_run
        self.age_seconds = age_seconds
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
            result_path = self._result_path(GATE, pr_number)
            result_path.write_text(json.dumps(record), encoding="utf-8")
            if self.age_seconds is not None:
                stamp = time.time() - self.age_seconds
                os.utime(result_path, (stamp, stamp))
        if self.on_run is not None:
            self.on_run()
        return {
            "pr_number": pr_number,
            "branch": branch,
            "gates": [],
            "has_required_failure": bool((self.payload or {}).get("has_required_failure")),
        }


def _patch_attempt_gate(monkeypatch) -> dict:
    """Patch the decision (attempt_gate) and the PR resolution.

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
        return {"kind": "attempt_gate"}

    resolution = runner.PrResolution(
        runner.RESOLUTION_RESOLVED, pr_number=PR_NUMBER, owner_repo=OWNER_REPO,
    )
    monkeypatch.setattr(runner, "_pre_execution_decision", fake_decision)
    monkeypatch.setattr(runner, "resolve_pr_number", lambda *a, **k: resolution)
    return seen


def _patch_manager(monkeypatch, manager: _ScriptedManager, *,
                   takeover=None, head_sha_fn: Optional[Callable[[Optional[int]], str]] = None) -> None:
    """Hermetic: no git, no gh, no real gate, no real takeover chain walk."""
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(
        runner, "_get_pr_head_sha_for_gate",
        head_sha_fn if head_sha_fn is not None else (lambda pr_number: HEAD_SHA),
    )
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo, snapshot=None: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: OWNER_REPO)
    monkeypatch.setattr(runner, "_find_takeover_successor_evidence", lambda *a, **k: takeover)
    fake_rgm = types.ModuleType("review_gate_manager")
    fake_rgm._compute_changed_files = lambda branch: ["scripts/lib/foo.py"]
    monkeypatch.setitem(sys.modules, "review_gate_manager", fake_rgm)


def _assert_iso_timestamp(value) -> None:
    assert isinstance(value, str) and value.endswith("Z") and "T" in value


# ---------------------------------------------------------------------------
# 1. The declared MISMATCH hold
# ---------------------------------------------------------------------------


def test_declared_mismatch_stays_pending(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1e-declared-mismatch"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    other_sha = "e5" * 20
    seen = _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={
        "status": "pass",
        "commit_sha": other_sha,
        "contract_hash": CONTRACT_HASH,
        "report_path": _report_file(state_dir),
    })
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    declared_detail = (
        f"result records commit {other_sha[:8]} but the PR head is "
        f"{HEAD_SHA[:8]} — this verdict is about other code"
    )
    assert seen, "the patched decision was never reached — the branch would run for real"
    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "pending",
        "detail": declared_detail,
    }
    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == 1
    assert stored["reason"] == "stale_evidence_sha_mismatch"
    assert stored["reason_detail"] == (
        f"{GATE}'s own result for PR #{PR_NUMBER} is decided but about a "
        f"DIFFERENT commit than the PR head ({declared_detail}) and no "
        "takeover successor produced current evidence either — staying "
        "pending, never booking a verdict about other code (OI-1571 tak 3)"
    )
    assert stored["request_path"] == str(manager._request_path(GATE, PR_NUMBER))
    assert stored["result_path"] == str(manager._result_path(GATE, PR_NUMBER))
    _assert_iso_timestamp(stored["last_attempt_at"])
    assert manager.calls, "the gate run itself must still happen"


# ---------------------------------------------------------------------------
# 2. The declared UNKNOWN_SHA hold
# ---------------------------------------------------------------------------


def test_declared_unknown_sha_stays_pending(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1e-declared-unknown"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    seen = _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={
        "status": "pass",
        "commit_sha": "",
        "contract_hash": CONTRACT_HASH,
        "report_path": _report_file(state_dir),
    })
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    declared_detail = (
        "sha binding unknown — head_sha or the record's commit_sha is missing; "
        "cannot verify whether this evidence belongs to the current head"
    )
    assert seen
    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "pending",
        "detail": declared_detail,
    }
    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == 1
    assert stored["reason"] == "sha_binding_unverifiable"
    assert stored["reason_detail"] == (
        f"{GATE}'s own result for PR #{PR_NUMBER} is decided but its sha "
        f"binding to the PR head could not be verified ({declared_detail}) "
        "and no takeover successor produced verifiable evidence either — "
        "suspending judgement (OI-1571 tak 3) rather than silently accepting "
        "or refusing it"
    )
    assert stored["result_path"] == str(manager._result_path(GATE, PR_NUMBER))
    _assert_iso_timestamp(stored["last_attempt_at"])


# ---------------------------------------------------------------------------
# 3. The head-moved hold — the head is re-resolved after the run
# ---------------------------------------------------------------------------


def test_head_moved_stays_pending_with_all_three_shas(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1e-head-moved"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    seen = _patch_attempt_gate(monkeypatch)
    live = {"head": OLD_HEAD}
    manager = _ScriptedManager(
        state_dir,
        payload={"status": "completed", "commit_sha": OLD_HEAD},
        on_run=lambda: live.update(head=NEW_HEAD),
    )
    _patch_manager(monkeypatch, manager, head_sha_fn=lambda pr_number: live["head"])

    outcome = runner.fulfill_obligation(state_dir, path, record)

    head_moved_detail = (
        f"{GATE} result for PR #{PR_NUMBER} records commit {OLD_HEAD[:8]} but "
        f"the PR head was re-resolved as {NEW_HEAD[:8]} after the gate ran (it "
        f"was {OLD_HEAD[:8]} before) — the head moved while the gate ran, so "
        "this verdict is about superseded code and is never booked"
    )
    assert seen
    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": "pending",
        "detail": head_moved_detail,
    }
    stored = _record(path)
    assert stored["status"] == STATUS_PENDING
    assert stored["attempts"] == 1
    assert stored["reason"] == "stale_evidence_sha_mismatch"
    assert stored["reason_detail"] == (
        f"{head_moved_detail} and no takeover successor produced current "
        "evidence either — staying pending, never booking a verdict about "
        "other code (defect fixed 2026-09-30)"
    )
    assert stored["result_path"] == str(manager._result_path(GATE, PR_NUMBER))
    _assert_iso_timestamp(stored["last_attempt_at"])


# ---------------------------------------------------------------------------
# 4. The final booking of the declared gate's own result
# ---------------------------------------------------------------------------


def test_a_passing_record_on_the_current_head_books_fulfilled(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1e-pass"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    seen = _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={
        "status": "pass",
        "contract_hash": CONTRACT_HASH,
        "report_path": _report_file(state_dir),
    })
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    result_file = manager._result_path(GATE, PR_NUMBER)
    assert seen
    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": STATUS_FULFILLED,
        "request_path": str(manager._request_path(GATE, PR_NUMBER)),
        "result_path": str(result_file),
        "has_required_failure": False,
    }
    stored = _record(path)
    assert stored["status"] == STATUS_FULFILLED
    assert stored["attempts"] == 1
    assert stored["reason"] is None
    assert stored["reason_detail"] is None
    assert stored["fulfilled_by"] == GATE
    assert stored["takeover_gate"] is None
    assert stored["evidence_result_path"] == str(result_file)
    assert stored["result_path"] == str(result_file)
    _assert_iso_timestamp(stored["resolved_at"])


def test_a_rejection_with_blocking_findings_books_failed_by_verdict(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1e-rejected"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    seen = _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={
        "status": "completed",
        "contract_hash": CONTRACT_HASH,
        "report_path": _report_file(state_dir),
        "blocking_findings": [{"id": "B1", "detail": "off-by-one"}],
    })
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    result_file = manager._result_path(GATE, PR_NUMBER)
    assert seen
    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": STATUS_FAILED,
        "request_path": str(manager._request_path(GATE, PR_NUMBER)),
        "result_path": str(result_file),
        "has_required_failure": False,
    }
    stored = _record(path)
    assert stored["status"] == STATUS_FAILED
    assert stored["reason"] == REASON_FAILED_BY_GATE_VERDICT
    assert stored["reason_detail"] == "1 blocking finding(s)"
    assert stored["fulfilled_by"] == GATE
    assert stored["takeover_gate"] is None
    assert stored["evidence_result_path"] == str(result_file)


def test_a_run_with_has_required_failure_books_required_failure(tmp_path, monkeypatch):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1e-required-failure"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    seen = _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={
        "status": "failed",
        "has_required_failure": True,
        "contract_hash": CONTRACT_HASH,
        "report_path": _report_file(state_dir),
    })
    _patch_manager(monkeypatch, manager)

    outcome = runner.fulfill_obligation(state_dir, path, record)

    result_file = manager._result_path(GATE, PR_NUMBER)
    assert seen
    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": STATUS_FAILED,
        "request_path": str(manager._request_path(GATE, PR_NUMBER)),
        "result_path": str(result_file),
        "has_required_failure": True,
    }
    stored = _record(path)
    assert stored["status"] == STATUS_FAILED
    assert stored["reason"] == "required_failure"
    assert stored["reason_detail"] == "result status: failed"
    assert stored["fulfilled_by"] == GATE
    assert stored["evidence_result_path"] == str(result_file)


# ---------------------------------------------------------------------------
# 5. The fast-fulfilment tripwire — a stale-before-this-attempt evidence file
# ---------------------------------------------------------------------------


def test_a_stale_evidence_file_stamps_the_fast_fulfillment_warning(
    tmp_path, monkeypatch, caplog,
):
    state_dir = _make_state_dir(tmp_path)
    dispatch_id = "dlv-g2-1e-fast-warning"
    path = _register(state_dir, dispatch_id)
    record = _record(path)
    seen = _patch_attempt_gate(monkeypatch)
    manager = _ScriptedManager(state_dir, payload={
        "status": "pass",
        "contract_hash": CONTRACT_HASH,
        "report_path": _report_file(state_dir),
    }, age_seconds=600)
    _patch_manager(monkeypatch, manager)

    with caplog.at_level(logging.WARNING, logger="gate_obligation_runner"):
        outcome = runner.fulfill_obligation(state_dir, path, record)

    result_file = manager._result_path(GATE, PR_NUMBER)
    assert seen
    warning = outcome.pop("fast_fulfillment_warning")
    assert warning.startswith(
        f"{GATE} fulfilment for PR #{PR_NUMBER} (dispatch {dispatch_id}) used "
        "evidence whose file was last written "
    )
    assert warning.endswith(
        "s before this attempt resolved — not provably a fresh run this cycle "
        "(OI-1569 Klaar item 8)"
    )
    assert outcome == {
        "dispatch_id": dispatch_id,
        "gate": GATE,
        "action": STATUS_FULFILLED,
        "request_path": str(manager._request_path(GATE, PR_NUMBER)),
        "result_path": str(result_file),
        "has_required_failure": False,
    }
    assert _record(path)["status"] == STATUS_FULFILLED
    warnings = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "not provably a fresh run" in r.getMessage()
    ]
    assert warnings == [f"gate_obligation_runner: {warning}"]
