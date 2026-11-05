"""R10 (dispatch dlv-38447134aeec): one codex run per PR head, in the obligation runner.

Measured 15-22 October 2026 (``claudedocs/2026-10-22-codex-druk.md``): 272 codex
review sessions on 224 distinct (PR, head) pairs. PR #2151 head ``24412e26`` got
13 runs, one per launchd cycle, each refused as ``gate_execution_degenerate``;
PR #2170 head ``83bad6a0`` got a launchd run while a manual runner was still
reviewing that head. Pinned here, through the real ``fulfill_obligation``/``run``
against real obligation and result files (the provider is never reached: the
manager below writes the result record a run would leave):

- a launch while the run lock of (PR, gate, head) is held elsewhere does not
  run, counts no attempt and leaves the obligation exactly as it was;
- a head that already has a decided verdict is not run again, also when the
  verdict landed after the decision that led to the launch;
- ``--force-rerun --reason`` runs anyway and records its reason;
- three degenerate attempts on one head stop after the second, in the cap
  state; a quota refusal does not count; a new head runs again;
- the runner removes the lock files of dead holders.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import (  # noqa: E402
    STATUS_FULFILLED,
    STATUS_PENDING,
    obligation_path,
    register_obligation,
    update_obligation,
)

PR = 61010
GATE = "codex_gate"
HEAD_A = "a1" * 20
HEAD_B = "b2" * 20
CAP_REASON = "degenerate_retry_cap"

DEGENERATE = {"status": "unavailable", "reason": "gate_execution_degenerate"}
TIMEOUT = {"status": "unavailable", "reason": "timeout"}
STALL = {"status": "unavailable", "reason": "stall"}
QUOTA = {"status": "unavailable", "reason": "lane_exhausted"}
PARTIAL = {"status": "partial_review", "reason": "diff_truncated"}


@pytest.fixture(autouse=True)
def _clean_takeover_chain_env(monkeypatch):
    monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)


class _ScriptedManager:
    """``request_and_execute`` writes the next scripted result record for the declared gate; no gate runs.

    The record carries the head the run was launched on (``head["sha"]`` at
    call time), as ``gate_recorder.stamp_request_identity`` stamps it.
    """

    def __init__(self, state_dir: Path, results: List[Dict[str, Any]], head: Dict[str, str]) -> None:
        self.state_dir = Path(state_dir)
        self.results = list(results)
        self.head = head
        self.calls: List[Dict[str, Any]] = []

    def _request_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "requests" / f"pr-{pr_number}-{gate}.json"

    def _result_path(self, gate: str, pr_number: int) -> Path:
        return self.state_dir / "review_gates" / "results" / f"pr-{pr_number}-{gate}.json"

    def request_and_execute(self, *, pr_number, branch, review_stack, risk_class,
                            changed_files, mode, dispatch_id="", **extra):
        self.calls.append({"review_stack": list(review_stack), "head": self.head["sha"], **extra})
        payload = self.results.pop(0) if len(self.results) > 1 else dict(self.results[0])
        for gate in review_stack:
            self._request_path(gate, pr_number).write_text(
                json.dumps({"gate": gate, "pr_number": pr_number, "status": "completed"}), encoding="utf-8",
            )
            record = {"gate": gate, "pr_number": pr_number, "commit_sha": self.head["sha"], **payload}
            self._result_path(gate, pr_number).write_text(json.dumps(record), encoding="utf-8")
        return {"pr_number": pr_number, "branch": branch, "gates": [], "has_required_failure": False}


def _store(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    return state_dir


def _register(state_dir: Path, dispatch_id: str) -> Path:
    return register_obligation(state_dir, dispatch_id=dispatch_id, gate=GATE, project_id="vnx-dev", pr_number=PR)


def _read(state_dir: Path, dispatch_id: str) -> Dict[str, Any]:
    return json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))


def _patch(monkeypatch, manager: _ScriptedManager, head: Dict[str, str]) -> None:
    """Hermetic: no git, no gh, no real gate. The PR is open and its head is ``head["sha"]``."""
    monkeypatch.setattr(runner, "_build_manager", lambda state_dir: manager)
    monkeypatch.setattr(runner, "_branch_from_github", lambda pr, owner_repo, snapshot=None: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda state_dir: "Vinix24/vnx-orchestration")
    # OI-2459: hermetic — the tick snapshot must never hit real gh here.
    monkeypatch.setattr(runner, "_new_github_snapshot", lambda owner_repo: None)
    monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr_number: head["sha"])
    monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr_number, owner_repo, snapshot=None: "OPEN")
    monkeypatch.setattr(runner, "_list_open_prs", lambda owner_repo: {"prs": [], "suspect": False})
    fake_rgm = types.ModuleType("review_gate_manager")
    fake_rgm._compute_changed_files = lambda branch: ["scripts/lib/foo.py"]
    monkeypatch.setitem(sys.modules, "review_gate_manager", fake_rgm)


def _seed_verdict(state_dir: Path, *, head_sha: str, status: str = "pass") -> Path:
    """A decided, complete-evidence verdict of the declared gate on ``head_sha``, its report on disk."""
    report = state_dir.parent / "unified_reports" / f"codex-gate-pr{PR}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("codex_gate report body", encoding="utf-8")
    result = state_dir / "review_gates" / "results" / f"pr-{PR}-{GATE}.json"
    result.write_text(json.dumps({
        "gate": GATE, "pr_number": PR, "status": status, "contract_hash": "sha256:r10",
        "report_path": str(report), "commit_sha": head_sha,
    }), encoding="utf-8")
    return result


_HOLDER = """
import fcntl, json, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.pwrite(fd, json.dumps({"pid": os.getpid()}).encode("utf-8"), 0)
print("held", flush=True)
sys.stdin.read()
"""


@contextmanager
def _held_by_another_process(lock_path: Path) -> Iterator[int]:
    """Another process holds ``lock_path`` (flock) until the block ends; yields its pid."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "held"
        yield proc.pid
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def _lock_path(state_dir: Path, head_sha: str) -> Path:
    import gate_run_guard

    return gate_run_guard.run_lock_path(state_dir, pr_ref=PR, gate=GATE, head_sha=head_sha)


class TestSecondLaunchWhileInFlight:
    def test_a_launch_while_the_lock_is_held_does_not_run_and_counts_no_attempt(
        self, tmp_path, monkeypatch, caplog,
    ):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-inflight")
        before = obligation_path(state_dir, "dlv-r10-inflight").read_text(encoding="utf-8")
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)
        lock = _lock_path(state_dir, HEAD_A)

        with caplog.at_level(logging.WARNING), _held_by_another_process(lock) as holder_pid:
            summary = runner.run(state_dir)

        assert manager.calls == [], "a head already in flight is never launched a second time"
        outcome = summary["outcomes"][0]
        assert outcome["action"] == "in_flight"
        assert outcome["holder_pid"] == holder_pid
        assert str(holder_pid) in outcome["detail"] and lock.stem in outcome["detail"]
        assert obligation_path(state_dir, "dlv-r10-inflight").read_text(encoding="utf-8") == before, (
            "the obligation is left exactly as it was: no attempt counted, no reason written"
        )
        assert summary["pending_after"] == 1, "a head in flight is still an open obligation"
        refusals = [r for r in caplog.records if lock.stem in r.getMessage()]
        assert len(refusals) == 1 and str(holder_pid) in refusals[0].getMessage(), (
            "one log line names the key and the holder's pid"
        )

    def test_the_lock_is_released_after_the_run_and_its_file_removed(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-release")
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)

        runner.run(state_dir)

        assert len(manager.calls) == 1
        assert not _lock_path(state_dir, HEAD_A).exists(), "a clean release leaves no lock file behind"

    def test_a_force_rerun_never_passes_a_run_in_flight(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-force-inflight")
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)

        with _held_by_another_process(_lock_path(state_dir, HEAD_A)):
            summary = runner.run(
                state_dir, dispatch_prefix="dlv-r10-force-inflight", force_rerun="operator wants a rerun",
            )

        assert manager.calls == []
        assert summary["outcomes"][0]["action"] == "in_flight"


class TestNoRunOnAHeadWithAVerdict:
    def test_a_head_with_a_decided_verdict_is_booked_not_run(self, tmp_path, monkeypatch):
        """The rescue that already existed before R10 (item 10 of the runner docstring): pinned here."""
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-verdict")
        result = _seed_verdict(state_dir, head_sha=HEAD_A)
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)

        summary = runner.run(state_dir)

        assert manager.calls == []
        record = _read(state_dir, "dlv-r10-verdict")
        assert record["status"] == STATUS_FULFILLED
        assert record["result_path"] == str(result)
        assert summary["pending_after"] == 0

    def test_a_verdict_that_landed_after_the_decision_is_booked_under_the_lock(self, tmp_path, monkeypatch):
        """The concurrent case: the decision saw no verdict, another run booked one before this launch."""
        state_dir = _store(tmp_path)
        path = _register(state_dir, "dlv-r10-landed")
        record = _read(state_dir, "dlv-r10-landed")
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)
        index_before_the_verdict = runner._index_gate_results(state_dir)
        _seed_verdict(state_dir, head_sha=HEAD_A)

        outcome = runner.fulfill_obligation(state_dir, path, record, result_index=index_before_the_verdict)

        assert manager.calls == [], "a verdict on this head that landed meanwhile is the answer, not a second run"
        assert outcome["action"] == STATUS_FULFILLED
        assert _read(state_dir, "dlv-r10-landed")["status"] == STATUS_FULFILLED

    def test_an_obligation_another_run_booked_meanwhile_is_not_run_again(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        path = _register(state_dir, "dlv-r10-booked")
        stale_record = _read(state_dir, "dlv-r10-booked")
        update_obligation(path, status=STATUS_FULFILLED, reason="fulfilled_by_other_runner")
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)

        outcome = runner.fulfill_obligation(state_dir, path, stale_record)

        assert manager.calls == []
        assert outcome["action"] == STATUS_FULFILLED
        assert _read(state_dir, "dlv-r10-booked")["reason"] == "fulfilled_by_other_runner"


class TestForceRerun:
    def test_the_force_flag_runs_over_a_verdict_and_records_its_reason(self, tmp_path, monkeypatch, caplog):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-force")
        _seed_verdict(state_dir, head_sha=HEAD_A)
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)
        reason = "codex config fixed, re-review the same head"

        with caplog.at_level(logging.WARNING):
            runner.run(state_dir, dispatch_prefix="dlv-r10-force", force_rerun=reason)

        assert len(manager.calls) == 1, "an operator force-rerun runs the gate"
        assert manager.calls[0]["force_rerun"] == reason, "the reason travels to the request and the result"
        forced = _read(state_dir, "dlv-r10-force")["force_rerun"]
        assert forced["reason"] == reason and forced["head_sha"] == HEAD_A
        assert any(reason in r.getMessage() for r in caplog.records), "the reason lands in the log"

    def test_the_force_flag_runs_past_the_retry_cap(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-force-cap")
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)
        runner.run(state_dir)
        runner.run(state_dir)
        assert _read(state_dir, "dlv-r10-force-cap")["reason"] == CAP_REASON

        runner.run(state_dir, dispatch_prefix="dlv-r10-force-cap", force_rerun="one more try after the fix")

        assert len(manager.calls) == 3
        assert _read(state_dir, "dlv-r10-force-cap")["head_runs"]["reviewer_runs"] == 3

    def test_the_force_flag_does_not_rerun_a_merged_pr(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-force-merged")
        _seed_verdict(state_dir, head_sha=HEAD_A)
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)
        monkeypatch.setattr(runner, "_pr_state_from_github", lambda pr_number, owner_repo, snapshot=None: "MERGED")

        runner.run(state_dir, dispatch_prefix="dlv-r10-force-merged", force_rerun="rerun")

        assert manager.calls == []
        assert _read(state_dir, "dlv-r10-force-merged")["status"] == STATUS_FULFILLED


class TestRetryCapPerHead:
    def test_three_degenerate_attempts_on_one_head_stop_after_the_second(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-cap")
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)

        first = runner.run(state_dir)["outcomes"][0]
        second = runner.run(state_dir)["outcomes"][0]
        after_second = _read(state_dir, "dlv-r10-cap")
        third = runner.run(state_dir)

        assert len(manager.calls) == 2, "the first run and one retry, then no more runs on this head"
        assert first["action"] == "pending"
        assert second["action"] == "retry_capped"
        assert after_second["status"] == STATUS_PENDING, "at the cap the merge door still sees no review"
        assert after_second["reason"] == CAP_REASON
        assert after_second["head_runs"]["head_sha"] == HEAD_A
        assert after_second["head_runs"]["reviewer_runs"] == 2
        assert HEAD_A[:8] in after_second["reason_detail"] and "--force-rerun" in after_second["reason_detail"]
        assert third["outcomes"][0]["action"] == "retry_capped"
        assert CAP_REASON in third["outcomes"][0]["detail"], "the cap is visible in the runner output"
        assert third["pending_after"] == 1
        assert _read(state_dir, "dlv-r10-cap")["attempts"] == 2, "a capped launch counts no attempt"

    @pytest.mark.parametrize("result", [TIMEOUT, STALL, PARTIAL], ids=["timeout", "stall", "partial_review"])
    def test_a_timeout_a_stall_and_a_partial_review_count_as_reviewer_runs(self, tmp_path, monkeypatch, result):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-count")
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [result], head)
        _patch(monkeypatch, manager, head)

        for _ in range(3):
            runner.run(state_dir)

        assert len(manager.calls) == 2
        assert _read(state_dir, "dlv-r10-count")["reason"] == CAP_REASON

    def test_a_quota_refusal_does_not_count(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-quota")
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [QUOTA, DEGENERATE, QUOTA, DEGENERATE, DEGENERATE], head)
        _patch(monkeypatch, manager, head)

        for _ in range(5):
            runner.run(state_dir)

        assert len(manager.calls) == 4, "two quota refusals ran no reviewer: two degenerate runs remain"
        record = _read(state_dir, "dlv-r10-quota")
        assert record["head_runs"]["reviewer_runs"] == 2
        assert record["reason"] == CAP_REASON

    def test_a_new_head_runs_again(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-newhead")
        head = {"sha": HEAD_A}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)
        for _ in range(3):
            runner.run(state_dir)
        assert len(manager.calls) == 2

        head["sha"] = HEAD_B
        outcome = runner.run(state_dir)["outcomes"][0]

        assert len(manager.calls) == 3 and manager.calls[-1]["head"] == HEAD_B
        assert outcome["action"] == "pending"
        record = _read(state_dir, "dlv-r10-newhead")
        assert record["head_runs"]["head_sha"] == HEAD_B
        assert record["head_runs"]["reviewer_runs"] == 1, "the count starts again on the new head"
        assert record["reason"] != CAP_REASON

    def test_an_unknown_head_is_never_capped(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        _register(state_dir, "dlv-r10-nohead")
        head = {"sha": ""}
        manager = _ScriptedManager(state_dir, [DEGENERATE], head)
        _patch(monkeypatch, manager, head)

        for _ in range(3):
            runner.run(state_dir)

        assert len(manager.calls) == 3, "a count per head needs a head; the run lock still applies"
        assert "head_runs" not in _read(state_dir, "dlv-r10-nohead")


class TestLockFilesDoNotAccumulate:
    def test_the_runner_removes_a_dead_holders_lock_file_and_keeps_a_live_one(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        head = {"sha": HEAD_A}
        _patch(monkeypatch, _ScriptedManager(state_dir, [DEGENERATE], head), head)
        dead = _lock_path(state_dir, HEAD_A)
        dead.parent.mkdir(parents=True, exist_ok=True)
        dead.write_text('{"pid": 999999}', encoding="utf-8")
        live = _lock_path(state_dir, HEAD_B)

        with _held_by_another_process(live):
            summary = runner.run(state_dir)
            assert live.exists(), "a lock someone holds is never removed"

        assert not dead.exists()
        assert summary["swept_run_locks"] == [dead.name]


def _rc(argv: List[str]) -> int:
    try:
        return runner.main(argv)
    except SystemExit as exc:
        return int(exc.code or 0)


class TestForceRerunFlags:
    @pytest.mark.parametrize("argv", [
        ["--force-rerun", "--dispatch-prefix", "dlv-x"],
        ["--force-rerun", "--reason", "  ", "--dispatch-prefix", "dlv-x"],
        ["--force-rerun", "--reason", "why"],
        ["--force-rerun", "--reason", "why", "--dispatch-prefix", "dlv-x", "--no-write"],
        ["--reason", "why", "--dispatch-prefix", "dlv-x"],
    ], ids=["no-reason", "blank-reason", "no-prefix", "dry-run", "reason-without-force"])
    def test_an_incomplete_force_rerun_is_refused(self, tmp_path, monkeypatch, argv):
        state_dir = _store(tmp_path)
        called = []
        monkeypatch.setattr(runner, "run", lambda *a, **kw: called.append(kw) or {})

        assert _rc(["--state-dir", str(state_dir), *argv]) == 2
        assert called == []

    def test_a_complete_force_rerun_reaches_the_run_with_its_reason(self, tmp_path, monkeypatch):
        state_dir = _store(tmp_path)
        captured: Dict[str, Any] = {}

        def fake_run(state_dir_arg, **kwargs):
            captured.update(kwargs)
            return {"outcomes": [], "obligations_seen": 0, "pending_after": 0}

        monkeypatch.setattr(runner, "run", fake_run)

        rc = _rc(["--state-dir", str(state_dir), "--force-rerun", "--reason", "re-review",
                  "--dispatch-prefix", "dlv-x"])

        assert rc == 0
        assert captured["force_rerun"] == "re-review"
        assert captured["dispatch_prefix"] == "dlv-x"
