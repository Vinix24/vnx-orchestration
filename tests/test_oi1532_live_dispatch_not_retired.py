"""tests/test_oi1532_live_dispatch_not_retired.py — OI-1532.

The obligation runner must not retire a LIVE dispatch that has not pushed its
branch yet, and must not retry a genuine wait FOREVER.

Defect, in one function, two branches, both wrong
(``scripts/gate_obligation_runner.py::_pre_execution_decision``):

  * Tak A — ``branch_exists is False`` is too aggressive. ``False`` folds two
    states into one: "branch existed and was deleted" (dispatch dead, retire
    is correct) and "branch never created because the dispatch is STILL
    RUNNING" (retire is wrong). Caught live on ``20260830-124500-sidedoor``:
    ``would_retire`` while the dispatch held an occupancy lock (13 min runtime,
    pid 82207). The fix splits ``False`` three ways using the dispatch's
    occupancy lock: dead (retire), alive (stay pending), liveness-unmeasurable
    (stay pending, visibly — a THIRD state, never a silent default).
  * Tak B — everything that is not ``False`` waits UNBOUNDED. ``stay_pending``
    had no attempt bound. Measured on mission-control: 11 obligations on 776
    attempts (8+ days at the 900s cadence) with nothing alarming. The fix
    reuses ``_STAY_PENDING_ESCALATION_ATTEMPTS`` (== the existing
    ``_UNRESOLVABLE_ESCALATION_ATTEMPTS``) so a wait that never produces a PR
    escalates loudly, never via a second drift-prone bound.

Hard test requirement: a test that is RED on the current tree and GREEN after
the fix. The liveness probe must NOT measure itself — POSIX advisory locks are
per-OPEN-FILE-DESCRIPTION, not per-process, so a probe that takes the lock in
the same process that already holds it would block itself. The live-dispatch
test holds the occupancy lock in a SEPARATE subprocess while the runner's probe
runs in the main process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gate_obligation_runner as runner  # noqa: E402
from gate_obligations import (  # noqa: E402
    STATUS_NOT_EXECUTABLE,
    STATUS_PENDING,
    STATUS_RETIRED,
    REASON_NO_PR_BRANCH_GONE,
    REASON_NO_PR_BRANCH_GONE_LIVE,
    REASON_NO_PR_BRANCH_GONE_UNMEASURED,
    obligation_path,
    register_obligation,
    update_obligation,
)


# ---------------------------------------------------------------------------
# Helpers — mirror test_gate_obligations.py's fixtures so this file is
# self-contained (no cross-file import of private helpers).
# ---------------------------------------------------------------------------


def _make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "vnx-data" / "state"
    (state_dir / "review_gates" / "requests").mkdir(parents=True, exist_ok=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True, exist_ok=True)
    return state_dir


def _read_obligation(state_dir: Path, dispatch_id: str) -> dict:
    return json.loads(obligation_path(state_dir, dispatch_id).read_text(encoding="utf-8"))


def _patch_resolution(monkeypatch, *, branch_exists, dispatch_live=None) -> None:
    """Pin PR resolution so no gh/git calls happen.

    ``dispatch_live`` is left to the REAL ``_dispatch_is_live`` unless the test
    is exercising the subprocess-holder path (which creates a real lock file)
    or explicitly stubbing it. Pinning ``branch_exists`` is enough to drive the
    AWAITING branch; the liveness probe then reads the real occupancy lock.
    """
    monkeypatch.setattr(runner, "_pr_from_dispatch_metadata", lambda sd, did: None)
    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: "Vinix24/vnx-orchestration")
    monkeypatch.setattr(runner, "_pr_from_github", lambda did, owner_repo: None)
    monkeypatch.setattr(runner, "_branch_exists_on_github", lambda did, owner_repo: branch_exists)


# A tiny program that takes an exclusive flock on a given path and holds it
# until its stdin is closed (or it is killed). Written to a temp file and run
# as a SUBPROCESS so the lock's open file description belongs to a different
# process than the runner's probe — the OI-1232 / OI-1532 contract: a probe in
# the same process that holds the lock measures itself.
_HOLD_LOCK_SRC = textwrap.dedent(
    """
    import fcntl, sys, time
    path = sys.argv[1]
    fh = open(path, "a")
    fcntl.flock(fh, fcntl.LOCK_EX)  # blocking: wait until we win it
    # Signal the parent we hold the lock, then hold until stdin EOF.
    sys.stdout.write("HELD\\n")
    sys.stdout.flush()
    try:
        # Block on stdin read; parent closes stdin (or kills us) to release.
        sys.stdin.read()
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    """
)


@pytest.fixture
def hold_occupancy_lock(tmp_path):
    """Return a context manager that holds the dispatch's occupancy lock in a
    SEPARATE subprocess for the duration of the ``with`` block.

    Creates the lock file first (the isolation layer creates it with
    ``open(..., "a")`` before flock-ing), then spawns a holder subprocess.
    The holder prints ``HELD`` once it has the lock so the caller knows the
    probe will see a live holder. On exit the subprocess is terminated and
    reaped; the kernel releases the lock the instant its process exits.
    """
    holder_path = tmp_path / "_hold_lock_src.py"
    holder_path.write_text(_HOLD_LOCK_SRC, encoding="utf-8")

    holders: list[subprocess.Popen] = []

    def _hold(lock_path: Path):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Create the file the way the isolation layer does (open "a"), so the
        # probe's ``lock_path.exists()`` check sees it — a missing file reads
        # as ``None`` (unmeasured), which is NOT what this fixture means to
        # simulate (a live dispatch always created its worktree/lock).
        open(lock_path, "a").close()
        proc = subprocess.Popen(
            [sys.executable, str(holder_path), str(lock_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait for the holder to confirm it has the lock (or report an error).
        ready = proc.stdout.readline()
        if ready != b"HELD\n":
            err = proc.stderr.read().decode("utf-8", "replace")
            proc.kill()
            pytest.fail(f"occupancy holder did not acquire the lock: {err!r} (ready={ready!r})")
        holders.append(proc)
        return proc

    yield _hold

    for proc in holders:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError, BrokenPipeError):
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


# ---------------------------------------------------------------------------
# Tak A — branch_exists is False, three ways.
# ---------------------------------------------------------------------------


class TestTakALiveDispatchNotRetired:
    """A dispatch whose branch is gone on GitHub but is STILL RUNNING (its
    occupancy lock is held by a live process) must NOT be retired.

    RED on unfixed main: ``_pre_execution_decision`` returned ``{"kind":
    "retire"}`` for ``branch_exists is False`` regardless of liveness, so the
    obligation landed STATUS_RETIRED while the dispatch was actively running.
    """

    def test_live_dispatch_stays_pending_not_retired(self, tmp_path, monkeypatch, hold_occupancy_lock):
        state_dir = _make_state_dir(tmp_path)
        dispatch_id = "20260830-oi1532-live-not-pushed"
        register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
        )
        _patch_resolution(monkeypatch, branch_exists=False)
        # Hold the occupancy lock in a SEPARATE subprocess — the runner's probe
        # runs in this process and must see a live holder without measuring
        # itself (POSIX locks are per-open-file-description, not per-process).
        lock_path = runner._occupancy_lock_path(state_dir, dispatch_id)
        holder = hold_occupancy_lock(lock_path)

        try:
            summary = runner.run(state_dir)
        finally:
            # Release the holder so the fixture can clean up.
            if holder.stdin is not None:
                holder.stdin.close()
            holder.wait(timeout=5)

        record = _read_obligation(state_dir, dispatch_id)
        # The live dispatch must NOT be retired.
        assert record["status"] == STATUS_PENDING, (
            "a dispatch that is still running (occupancy lock held by a live "
            "process) must stay pending — it has not pushed its branch yet, "
            "NOT died (OI-1532 Tak A)"
        )
        assert record["status"] != STATUS_RETIRED
        assert record["reason"] == REASON_NO_PR_BRANCH_GONE_LIVE, (
            "the recorded reason must say 'live, not pushed' so the state is "
            "distinct from a generic wait, not mistaken for a normal no-PR-yet"
        )
        assert record["reason_detail"], "reason_detail is required, never empty"
        assert "still running" in record["reason_detail"]
        # The runner's outcome action mirrors the pending state.
        assert summary["pending_after"] == 1
        outcome = summary["outcomes"][0]
        assert outcome["action"] == "pending"

    def test_live_dispatch_liveness_probe_does_not_measure_itself(self, tmp_path, monkeypatch):
        """Regression guard for the POSIX-lock footgun: a probe that holds the
        lock in the SAME process as the check would block itself and read the
        dispatch as live for the wrong reason (its own hold, not a real
        dispatch's). This test takes the lock IN-PROCESS and asserts the probe
        still reads a live holder — proving the probe opens its OWN file
        description rather than re-entering the holder's. If a future change
        made the probe reuse the holder's fd, this test would hang or
        misreport; the separate-subprocess test above is the real proof, this
        is the cheap unit guard on the probe itself."""
        state_dir = _make_state_dir(tmp_path)
        dispatch_id = "20260830-oi1532-probe-self"
        import fcntl

        lock_path = runner._occupancy_lock_path(state_dir, dispatch_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Take the exclusive lock in THIS process.
        fh = open(lock_path, "a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            # The probe opens its OWN fd and asks LOCK_SH | LOCK_NB. An
            # exclusive holder blocks it -> True (live). The probe must NOT
            # reuse this fd (which would make a shared request succeed against
            # our own exclusive lock on the same description — a false negative).
            live = runner._dispatch_is_live(state_dir, dispatch_id)
            assert live is True, (
                "the probe must open its own file description and see the "
                "in-process exclusive holder as live — if this reads False or "
                "None, the probe is re-entering the holder's open file "
                "description and measuring itself (OI-1532 / OI-1232)"
            )
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


class TestTakADeadDispatchStillRetired:
    """The existing, correct behaviour must not regress: a dispatch that is
    dead (branch gone AND no live holder) is still retired."""

    def test_dead_dispatch_retired(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        dispatch_id = "20260830-oi1532-dead-no-pr"
        register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
        )
        _patch_resolution(monkeypatch, branch_exists=False)
        # No occupancy lock file exists -> _dispatch_is_live returns None
        # (unmeasured), NOT False. To simulate a genuinely DEAD dispatch whose
        # worktree existed (lock file present, no holder), stub the probe to
        # False. This is the real shape of a dead dispatch that created a
        # worktree: the file remains after teardown, the kernel released the
        # lock when the holder exited.
        monkeypatch.setattr(runner, "_dispatch_is_live", lambda sd, did: False)

        summary = runner.run(state_dir)

        record = _read_obligation(state_dir, dispatch_id)
        assert record["status"] == STATUS_RETIRED
        assert record["reason"] == REASON_NO_PR_BRANCH_GONE
        assert summary["pending_after"] == 0


class TestTakALivenessUnmeasured:
    """The THIRD state: the branch is gone but liveness could not be measured
    (no occupancy lock file — the dispatch never created a worktree, a
    dry-run, a hand-registered obligation, or a lane without occupancy locks —
    or the probe failed). This is NOT dead and NOT alive. The runner must NOT
    retire (a live dispatch must never be closed on ambiguous evidence) and
    must record the unmeasured state VISIBLY so it is not mistaken for a
    normal wait."""

    def test_no_lock_file_stays_pending_unmeasured(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        dispatch_id = "20260830-oi1532-unmeasured"
        register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
        )
        _patch_resolution(monkeypatch, branch_exists=False)
        # No lock file exists in the temp state dir -> the REAL probe returns
        # None. Do NOT stub _dispatch_is_live: we want to prove the real
        # three-way branch reaches retire_unmeasured on a missing lock file.

        summary = runner.run(state_dir)

        record = _read_obligation(state_dir, dispatch_id)
        assert record["status"] == STATUS_PENDING, (
            "liveness-unmeasurable must NOT retire — a live dispatch must "
            "never be closed on ambiguous evidence (OI-1532 third state)"
        )
        assert record["status"] != STATUS_RETIRED
        assert record["reason"] == REASON_NO_PR_BRANCH_GONE_UNMEASURED, (
            "the recorded reason must mark the state unmeasured so it is "
            "distinct from a generic wait and from a confirmed retirement"
        )
        assert record["reason_detail"], "reason_detail is required"
        assert "could not be measured" in record["reason_detail"]
        assert summary["pending_after"] == 1


# ---------------------------------------------------------------------------
# Tak B — the genuine-wait branch must be bounded.
# ---------------------------------------------------------------------------


class TestTakBBoundedWait:
    """The ``stay_pending`` branch (branch exists / undetermined) used to retry
    forever. It now escalates loudly past ``_STAY_PENDING_ESCALATION_ATTEMPTS``
    (which reuses ``_UNRESOLVABLE_ESCALATION_ATTEMPTS`` — never a second bound)."""

    def test_below_threshold_stays_pending(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        dispatch_id = "20260830-oi1532-wait-below"
        register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
        )
        # branch_exists True -> genuine wait, liveness never probed.
        _patch_resolution(monkeypatch, branch_exists=True)

        summary = runner.run(state_dir)

        record = _read_obligation(state_dir, dispatch_id)
        assert record["status"] == STATUS_PENDING
        assert record["attempts"] == 1
        assert summary["pending_after"] == 1

    def test_at_threshold_escalates_loudly(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        dispatch_id = "20260830-oi1532-wait-over-threshold"
        path = register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
        )
        # Bring the obligation to one attempt BELOW the threshold so this run
        # crosses it (the runner increments attempts by 1 before deciding).
        update_obligation(path, attempts=runner._STAY_PENDING_ESCALATION_ATTEMPTS - 1)
        _patch_resolution(monkeypatch, branch_exists=True)

        summary = runner.run(state_dir)

        record = _read_obligation(state_dir, dispatch_id)
        assert record["status"] == STATUS_NOT_EXECUTABLE, (
            "a genuine wait that has retried past the stay-pending threshold "
            "must escalate to a loud terminal not_executable — never retry "
            "silently forever (OI-1532 Tak B)"
        )
        assert record["reason"] == "stay_pending_timeout"
        assert record["reason_detail"], "reason_detail is required"
        assert "waited" in record["reason_detail"]
        assert summary["pending_after"] == 0

    def test_threshold_reuses_unresolvable_constant(self):
        """The stay-pending bound MUST be the same constant as the unresolvable
        bound — two bounds that drift out of step is the next defect."""
        assert runner._STAY_PENDING_ESCALATION_ATTEMPTS is runner._UNRESOLVABLE_ESCALATION_ATTEMPTS
        assert runner._STAY_PENDING_ESCALATION_ATTEMPTS == runner._UNRESOLVABLE_ESCALATION_ATTEMPTS

    def test_branch_unknown_below_threshold_stays_pending(self, tmp_path, monkeypatch):
        """branch_exists None (gh could not tell) is also a genuine wait under
        the same bound — ambiguous evidence never retires, but it no longer
        waits forever either."""
        state_dir = _make_state_dir(tmp_path)
        dispatch_id = "20260830-oi1532-branch-unknown-below"
        path = register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
        )
        update_obligation(path, attempts=runner._STAY_PENDING_ESCALATION_ATTEMPTS - 2)
        _patch_resolution(monkeypatch, branch_exists=None)

        summary = runner.run(state_dir)

        record = _read_obligation(state_dir, dispatch_id)
        assert record["status"] == STATUS_PENDING
        assert summary["pending_after"] == 1


# ---------------------------------------------------------------------------
# Dry-run parity — the new kinds must forecast the same action a real run takes.
# ---------------------------------------------------------------------------


class TestDryRunParity:
    """The dry run must walk the SAME three-way decision tree as a real run for
    the new OI-1532 branches (OI-1388 defect 2 extended)."""

    def test_live_dispatch_dry_run_forecasts_stay_pending(self, tmp_path, monkeypatch, hold_occupancy_lock):
        real_dir = _make_state_dir(tmp_path / "real")
        dry_dir = _make_state_dir(tmp_path / "dry")
        dispatch_id = "20260830-oi1532-parity-live"
        for state_dir in (real_dir, dry_dir):
            register_obligation(
                state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
            )
        _patch_resolution(monkeypatch, branch_exists=False)
        # Hold the lock in a subprocess for BOTH dirs (the probe reads whichever
        # state_dir it is given). A single holder per dir.
        real_holder = hold_occupancy_lock(runner._occupancy_lock_path(real_dir, dispatch_id))
        dry_holder = hold_occupancy_lock(runner._occupancy_lock_path(dry_dir, dispatch_id))
        try:
            dry_summary = runner.run(dry_dir, write=False)
            real_summary = runner.run(real_dir, write=True)
        finally:
            for h in (dry_holder, real_holder):
                if h.stdin is not None:
                    h.stdin.close()
                h.wait(timeout=5)

        dry_action = dry_summary["outcomes"][0]["action"]
        real_action = real_summary["outcomes"][0]["action"]
        assert dry_action == "would_stay_pending_live"
        assert real_action == "pending"
        # Both agree the dispatch is NOT retired.
        assert dry_action != "would_retire"
        assert real_action != STATUS_RETIRED

    def test_unmeasured_dry_run_forecasts_stay_pending(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        dispatch_id = "20260830-oi1532-parity-unmeasured"
        register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
        )
        _patch_resolution(monkeypatch, branch_exists=False)
        # No lock file -> real probe returns None.

        dry_summary = runner.run(state_dir, write=False)

        assert dry_summary["outcomes"][0]["action"] == "would_stay_pending_unmeasured"
        assert dry_summary["pending_after"] == 1

    def test_over_threshold_dry_run_forecasts_escalation(self, tmp_path, monkeypatch):
        state_dir = _make_state_dir(tmp_path)
        dispatch_id = "20260830-oi1532-parity-escalate"
        path = register_obligation(
            state_dir, dispatch_id=dispatch_id, gate="codex_gate", project_id="vnx-dev",
        )
        update_obligation(path, attempts=runner._STAY_PENDING_ESCALATION_ATTEMPTS - 1)
        _patch_resolution(monkeypatch, branch_exists=True)

        dry_summary = runner.run(state_dir, write=False)

        assert dry_summary["outcomes"][0]["action"] == "would_escalate_stay_pending"
        assert dry_summary["pending_after"] == 0
