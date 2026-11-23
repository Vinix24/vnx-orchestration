"""Supervisor of one ``vnx slot run`` workload (RS-6, ADR-062 besluiten 2, 9, 11 and 13).

Runs the workload in its own session, books the identities on the lease, keeps the heartbeat
and the memory samples, and releases only when the session and every child lease are ``GONE``;
an expired heartbeat or a dead supervisor frees nothing. Everything impure is injected.
``store`` needs ``read_policy()`` and ``transaction()``, which yields the ADR-062 state under
the control lock and writes it back when the block ends normally.
"""

from __future__ import annotations

import os
import subprocess
import time

from . import slot_liveness as liveness
from . import slot_memory
from .slot_admission import AdmissionInvalid, StateCorrupt, _event, check_token, nested_plan

PARENT_ENV = "VNX_SLOT_LEASE"
HEARTBEAT_SECONDS = 15
EXIT_TEMPFAIL, EXIT_INVALID = 75, 78

class StartRefused(PermissionError):
    """``start`` spawned nothing and changed no lease field; ``reason`` and ``exit_code`` are ADR-062's."""
    def __init__(self, reason: str, exit_code: int = EXIT_INVALID):
        super().__init__(reason)
        self.reason, self.exit_code = reason, exit_code

def _children_of(holders: dict, lease_id: str) -> list[str]:
    """Every lease below ``lease_id``, recursively."""
    found, frontier = [], [lease_id]
    while frontier:
        parent = frontier.pop()
        for key, rec in holders.items():
            if rec.get("parent_lease") == parent and key not in found:
                found.append(key)
                frontier.append(key)
    return found

class Supervisor:
    def __init__(self, store, log, *, clock=time.time, sleep=time.sleep, spawn=subprocess.Popen,
                 ps=liveness._run_ps, sample_runner=liveness._run_ps,
                 getpid=os.getpid, env=None, interval=HEARTBEAT_SECONDS):
        self.store, self.log, self.clock, self.sleep, self.spawn = store, log, clock, sleep, spawn
        self.ps, self.sample_runner, self.getpid = ps, sample_runner, getpid
        self.env, self.interval, self.fenced = env if env is not None else os.environ, interval, False

    def _fence(self, state: dict, lease_id: str, token) -> bool:
        """True when ``token`` is the lease's current one; the first refusal is logged and fences every later call."""
        if self.fenced:
            return False
        verdict = check_token(state, lease_id, token)
        if verdict["ok"]:
            return True
        self.fenced = True
        self.log(_event(state, "refused", self.clock(), state["holders"].get(lease_id) or {"lease_id": lease_id},
                        reasons=[verdict["reason"]], exit_code=verdict["exit_code"]))
        return False

    @staticmethod
    def _refusal(state: dict, policy: dict, lease_id: str, lease: dict, parent) -> tuple[str, int] | None:
        """Why the lease may not start a workload, or None. Only an unbooked run lease and a ``nested`` plan
        pass: the supervisor grants no capacity, so a plan that still needs a top-up is ``budget_full``."""
        if (lease.get("owner_kind") != "run" or lease.get("session_id") is not None
                or lease.get("owner_pid") is not None or lease.get("parent_lease") not in (None, parent)):
            return "owner_mismatch", EXIT_INVALID   # a booked or foreign owner: a second start would orphan it
        if parent is None:
            return None
        probe = {**state, "holders": {k: v for k, v in state["holders"].items() if k != lease_id}}
        try:
            plan = nested_plan(probe, policy, parent, lease["profile"], lease["slots_granted"])
        except StateCorrupt:
            raise   # a defect of a held lease, never a refusal
        except AdmissionInvalid as exc:
            return exc.reason, exc.exit_code
        return None if plan["mode"] == "nested" and plan["shortfall"] == 0 else ("budget_full", EXIT_TEMPFAIL)

    def start(self, lease_id: str, token: int, argv: list[str]):
        """Start the workload in a new session and book it on the lease; a refusal raises before the spawn."""
        parent = self.env.get(PARENT_ENV) or None
        policy = self.store.read_policy()
        with self.store.transaction() as state:
            if not self._fence(state, lease_id, token):
                raise StartRefused("stale_token")
            lease = state["holders"][lease_id]
            if (refusal := self._refusal(state, policy, lease_id, lease, parent)) is not None:
                self.log(_event(state, "refused", self.clock(), lease, reasons=[refusal[0]], exit_code=refusal[1]))
                raise StartRefused(*refusal)   # raised inside the transaction: no lease field is committed
            proc = self.spawn(argv, start_new_session=True, env={**self.env, PARENT_ENV: lease_id})
            table = liveness.snapshot(self.ps)
            work, mine = (next((r for r in table or [] if r["pid"] == p), None) for p in (proc.pid, self.getpid()))
            lease.update(
                owner_kind="run", parent_lease=parent, owner_uid=os.getuid(),
                # a new session's id is the child's pid: known even when it exits before the first snapshot
                session_id=str(proc.pid), owner_pid=proc.pid,
                owner_start_time=work["start_time"] if work else None,
                supervisor_pid=self.getpid(), supervisor_start_time=mine["start_time"] if mine else None)
            # the identities drive release decisions: the ledger line comes before their commit
            self.log(_event(state, "nested" if parent is not None else "admitted", self.clock(), lease, reasons=[]))
        return proc

    def tick(self, lease_id: str, token: int) -> None:
        """Heartbeat with the fencing token (never after a refusal) and the bucket's sample."""
        now = self.clock()
        with self.store.transaction() as state:
            if self._fence(state, lease_id, token):
                state["holders"][lease_id]["heartbeat_at"] = now
            if slot_memory.needs_own_sample(state, now):
                slot_memory.record_sample(state, slot_memory.take_sample(self.sample_runner, now), now)

    def try_release(self, lease_id: str, token: int) -> bool:
        """Release the lease and its child leases only when liveness says GONE; UNKNOWN never does."""
        if self.fenced:   # fenced for good: not even a write-back of the same state
            return False
        with self.store.transaction() as state:
            if not self._fence(state, lease_id, token):
                return False
            # Under the lock: a child committed before this point is in the table, so it cannot read GONE.
            table = liveness.snapshot(self.ps)
            holders = state["holders"]
            verdict, _ = liveness.lease_liveness(holders[lease_id], table, holders)
            if verdict != liveness.GONE:
                return False
            lease = holders[lease_id]
            for key in _children_of(holders, lease_id):
                self.log(_event(state, "reaped", self.clock(), holders.pop(key)))
            self.log(_event(state, "released", self.clock(), lease,
                            hold_s=self.clock() - lease["admitted_at"]))
            del holders[lease_id]
            return True

    def run(self, lease_id: str, token: int, argv: list[str]) -> int:
        """Run ``argv`` under the lease; the return value is the workload's own exit code."""
        proc = self.start(lease_id, token, argv)
        while proc.poll() is None:
            self.tick(lease_id, token)
            self.sleep(self.interval)
        while not self.fenced:
            self.tick(lease_id, token)
            if self.try_release(lease_id, token):
                break
            self.sleep(self.interval)
        return proc.returncode
