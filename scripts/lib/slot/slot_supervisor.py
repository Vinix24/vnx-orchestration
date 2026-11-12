"""Supervisor of one ``vnx slot run`` workload (RS-6, ADR-062 besluiten 2, 9, 11 and 13).

Starts the workload in its own session, books the identities on the lease, keeps the
heartbeat, takes the host-wide memory samples and releases only on positive evidence:
the session is ``GONE`` and every child lease is ``GONE``. Nothing here frees a lease
because a heartbeat expired or a supervisor died. Everything impure is injected.

``store`` needs ``read_policy()`` and ``transaction()``, a context manager that yields the
ADR-062 state dict and writes it back under the control lock when the block ends normally.
"""

from __future__ import annotations

import os
import subprocess
import time

from . import slot_liveness as liveness
from . import slot_memory
from .slot_admission import _event, check_token, nested_plan

PARENT_ENV = "VNX_SLOT_LEASE"
HEARTBEAT_SECONDS = 15

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

    def _fence(self, state: dict, lease_id: str, token, verdict: dict) -> bool:
        """True when ``token`` is the lease's current one; otherwise log it and stop for good."""
        if verdict["ok"]:
            return True
        self.fenced = True
        self.log(_event(state, "refused", self.clock(), state["holders"].get(lease_id) or {"lease_id": lease_id},
                        reasons=[verdict["reason"]], exit_code=verdict["exit_code"]))
        return False

    def start(self, lease_id: str, token: int, argv: list[str]):
        """Start the workload in a new session and book session, supervisor and workload on the lease."""
        parent = self.env.get(PARENT_ENV) or None
        policy = self.store.read_policy()
        with self.store.transaction() as state:
            if not self._fence(state, lease_id, token, check_token(state, lease_id, token)):
                raise PermissionError("stale_token")
            lease = state["holders"][lease_id]
            if parent is not None:   # raises AdmissionInvalid for an unknown parent or a forbidden child profile
                probe = {**state, "holders": {k: v for k, v in state["holders"].items() if k != lease_id}}
                nested_plan(probe, policy, parent, lease["profile"], lease["slots_granted"])
            proc = self.spawn(argv, start_new_session=True, env={**self.env, PARENT_ENV: lease_id})
            table = liveness.snapshot(self.ps)
            work, mine = (next((r for r in table or [] if r["pid"] == p), None) for p in (proc.pid, self.getpid()))
            lease.update(
                owner_kind="run", parent_lease=parent, owner_uid=os.getuid(),
                # start_new_session makes the child its own session leader: sid == pid, known without a
                # snapshot, so a workload that ends before the first read still ends in GONE.
                session_id=str(proc.pid), owner_pid=proc.pid,
                owner_start_time=work["start_time"] if work else None,   # unknown when it already exited
                supervisor_pid=self.getpid(), supervisor_start_time=mine["start_time"] if mine else None)
            # These identities drive the release decisions: the ledger line comes before their commit.
            self.log(_event(state, "nested" if parent is not None else "admitted", self.clock(), lease,
                            reasons=[], max_slots=lease["max_slots"]))
        return proc

    def tick(self, lease_id: str, token: int) -> None:
        """Heartbeat with the fencing token (never after a refusal) and the bucket's sample."""
        now = self.clock()
        with self.store.transaction() as state:
            if not self.fenced and self._fence(state, lease_id, token, check_token(state, lease_id, token)):
                state["holders"][lease_id]["heartbeat_at"] = now
            if slot_memory.needs_own_sample(state, now):
                slot_memory.record_sample(state, slot_memory.take_sample(self.sample_runner, now), now)

    def try_release(self, lease_id: str, token: int) -> bool:
        """Release the lease and its child leases only when liveness says GONE; UNKNOWN never does."""
        with self.store.transaction() as state:
            if not self._fence(state, lease_id, token, check_token(state, lease_id, token)):
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
