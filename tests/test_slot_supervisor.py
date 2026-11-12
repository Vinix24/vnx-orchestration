"""Tests for scripts/lib/slot/slot_supervisor.py (RS-6): no real sleep, process or store."""

from __future__ import annotations

import contextlib
import copy
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
LSTART = "Thu Nov 12 10:00:00 2026"
SWAP = "vm.swapusage: total = 5120.00M  used = 100.00M  free = 5020.00M  (encrypted)\n"
VM_STAT = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
           "Pages occupied by compressor:                 1024.\n")
POLICY = {"profiles": {"runner": {"children": ["seo-gate"], "max_slots": 8, "heavy": False},
                       "seo-gate": {"children": [], "max_slots": 8, "heavy": True}}}

@pytest.fixture(scope="module")
def sup():
    sys.path.insert(0, str(LIB))
    try:
        from slot import slot_admission, slot_liveness, slot_supervisor
    finally:
        sys.path.remove(str(LIB))
    return slot_supervisor, slot_liveness, slot_admission

class Store:
    def __init__(self, holders):
        self.state = {"holders": holders, "samples": [], "mode": "observe", "queue": [],
                      "memory_recovery": {"blocked": False, "clean_buckets": []}}

    def read_policy(self):
        return POLICY

    before = order = None   # ``before`` runs once ahead of a transaction: another supervisor committing
    @contextlib.contextmanager
    def transaction(self):
        hook, self.before = self.before, None
        hook and hook(self)
        work = copy.deepcopy(self.state)
        yield work
        self.state = work
        if self.order is not None:
            self.order.append("commit")

def lease(lease_id="L1", token=1, profile="runner", **extra):
    return {"lease_id": lease_id, "token": token, "profile": profile, "project_id": "p", "slots_granted": 1,
            "max_slots": 8, "admitted_at": 1000, "heartbeat_at": 1000, "parent_lease": None, "state": "active",
            "owner_kind": "run", "descendants": [], **extra}

def ps_text(*rows):
    return "\n".join(f"{pid} {ppid} {sess} {LSTART}" for pid, ppid, sess in rows) + "\n"

class Proc:
    def __init__(self, pid=500, polls=0, code=0):
        self.pid, self.returncode, self._polls = pid, code, polls

    def poll(self):
        self._polls -= 1
        return None if self._polls >= 0 else self.returncode

class World:
    """A clock that only moves when the supervisor sleeps, and a scripted process table."""

    def __init__(self, store, sup, ps_script, proc=None, env=None, now=1000.0):
        self.now, self.logs, self.spawned, self.ps_script = now, [], [], list(ps_script)
        self.proc = proc or Proc()
        self.supervisor = sup.Supervisor(
            store, self.logs.append, clock=lambda: self.now, sleep=self.sleep, spawn=self.spawn,
            ps=self.ps, sample_runner=lambda argv: SWAP if argv[0] == "sysctl" else VM_STAT,
            getpid=lambda: 77, env=env or {}, interval=15)

    def sleep(self, seconds):
        self.now += seconds
        if self.now > 1000 + 3600:
            raise AssertionError("supervisor never finished")

    def spawn(self, argv, **kwargs):
        self.spawned.append((argv, kwargs))
        return self.proc

    def ps(self, argv):
        item = self.ps_script.pop(0) if len(self.ps_script) > 1 else self.ps_script[0]
        if isinstance(item, Exception):
            raise item
        return item

ALIVE_TABLE = ps_text((77, 1, 77), (500, 77, 500), (600, 500, 500))
SESSION_ONLY = ps_text((77, 1, 77), (600, 500, 500))
EMPTY = ps_text((77, 1, 77))

def test_start_uses_a_new_session_books_identities_and_refuses_a_second_start(sup):
    mod = sup[0]
    store = Store({"L1": lease()})
    store.order = order = []
    world = World(store, mod, [ALIVE_TABLE])
    world.supervisor.log = lambda ev: order.append(ev["event"])
    world.supervisor.start("L1", 1, ["make", "test"])
    assert order == ["admitted", "commit"]   # the ledger line comes before the state commit
    (argv, kwargs), = world.spawned
    assert argv == ["make", "test"] and kwargs["start_new_session"] is True and kwargs["env"][mod.PARENT_ENV] == "L1"
    rec = store.state["holders"]["L1"]
    assert (rec["session_id"], rec["owner_pid"], rec["owner_start_time"]) == ("500", 500, LSTART)
    assert (rec["supervisor_pid"], rec["supervisor_start_time"], rec["parent_lease"]) == (77, LSTART, None)
    world.proc = Proc(pid=501)
    with pytest.raises(PermissionError, match="owner_mismatch"):   # same lease, same token: refused before the spawn
        world.supervisor.start("L1", 1, ["y"])
    assert order == ["admitted", "commit", "refused"] and store.state["holders"]["L1"] is rec and len(world.spawned) == 1

def test_a_nested_child_that_exactly_fills_the_parent_grant_with_a_sibling_starts(sup):
    store = Store({"P": lease("P", slots_granted=2), "S": lease("S", parent_lease="P"), "L1": lease(profile="seo-gate")})
    world = World(store, sup[0], [ALIVE_TABLE], env={sup[0].PARENT_ENV: "P"})
    world.supervisor.start("L1", 1, ["x"])
    assert store.state["holders"]["L1"]["parent_lease"] == "P" and len(world.spawned) == 1
    assert [(e["event"], e["parent_lease"]) for e in world.logs] == [("nested", "P")]

@pytest.mark.parametrize("fields,parent_slots,reason,code", [
    ({}, 8, "profile_not_allowed_under_parent", 78), ({"profile": "seo-gate", "slots_granted": 2}, 1, "budget_full", 75),
    ({"session_id": ""}, 0, "owner_mismatch", 78), ({"owner_pid": 9}, 0, "owner_mismatch", 78),
    ({"owner_kind": "acquire"}, 0, "owner_mismatch", 78), ({"owner_kind": None}, 0, "owner_mismatch", 78),
    ({"parent_lease": "P"}, 0, "owner_mismatch", 78)])
def test_a_start_that_may_not_run_is_refused_before_the_spawn(sup, fields, parent_slots, reason, code):
    store = Store({"P": lease("P", slots_granted=parent_slots or 1), "L1": lease(**fields)})
    world, state = World(store, sup[0], [ALIVE_TABLE], env={sup[0].PARENT_ENV: "P"} if parent_slots else {}), store.state
    with pytest.raises(PermissionError, match=reason):   # budget_full: the plan says top_up and nobody granted it
        world.supervisor.start("L1", 1, ["x"])
    assert store.state is state and not world.spawned   # raised inside the transaction: nothing committed
    assert [(e["event"], e["reasons"], e["exit_code"]) for e in world.logs] == [("refused", [reason], code)]

def test_killed_supervisor_keeps_the_slots_until_the_session_is_empty(sup):
    mod, live, _ = sup
    store = Store({"L1": lease(session_id="500", supervisor_pid=77, supervisor_start_time=LSTART),
                   "C": lease("C", parent_lease="L1", session_id="501")})
    gone_supervisor = ps_text((600, 1, 500), (601, 1, 501))
    holders = store.state["holders"]
    assert live.supervisor_gone_tree_alive(holders["L1"], live.snapshot(lambda a: gone_supervisor), holders)
    world = World(store, mod, [gone_supervisor], now=1000 + 86400)   # heartbeat long expired
    assert world.supervisor.try_release("L1", 1) is False and "L1" in store.state["holders"]
    world.ps_script = [EMPTY]
    assert world.supervisor.try_release("L1", 1) is True and store.state["holders"] == {}
    assert [e["event"] for e in world.logs] == ["reaped", "released"]   # the gone child lease goes with it

def test_workload_exit_waits_for_the_session_and_returns_its_exit_code(sup):
    mod = sup[0]
    store = Store({"L1": lease()})
    world = World(store, mod, [ALIVE_TABLE, SESSION_ONLY, SESSION_ONLY, EMPTY], proc=Proc(polls=2, code=-9))
    assert world.supervisor.run("L1", 1, ["x"]) == -9   # a signal exit comes back unchanged
    assert "L1" not in store.state["holders"] and world.logs[-1]["event"] == "released"
    assert world.now > 1000 + 15 * 2   # it kept waiting while a process of the session lived

@pytest.mark.parametrize("script,session", [([RuntimeError("ps broke")], "500"), (["garbage\n"], "500"),
                                            ([""], "500"), ([EMPTY], None)])
def test_unknown_liveness_releases_nothing(sup, script, session):
    store = Store({"L1": lease(session_id=session)})
    world = World(store, sup[0], script)
    assert world.supervisor.try_release("L1", 1) is False and "L1" in store.state["holders"]
    assert world.logs == []

def test_samples_land_in_the_bucket_of_now_and_a_second_writer_writes_nothing(sup):
    store = Store({"L1": lease()})
    first = World(store, sup[0], [ALIVE_TABLE], now=1010.0)
    first.supervisor.tick("L1", 1)
    second = World(store, sup[0], [ALIVE_TABLE], now=1015.0)   # buckets: 990-1019, then 1020-1049
    second.supervisor.tick("L1", 1)
    second.now = 1025.0
    second.supervisor.tick("L1", 1)
    assert [s["bucket_start"] for s in store.state["samples"]] == [990, 1020]
    assert store.state["holders"]["L1"]["heartbeat_at"] == 1025.0   # the heartbeat carries the token

@pytest.mark.parametrize("token", [2, None, "1", True])
def test_token_mismatch_fences_every_later_call_even_with_the_current_token(sup, token):
    store = Store({"L1": lease(session_id="500")})
    world = World(store, sup[0], [EMPTY])
    world.supervisor.tick("L1", token)
    world.supervisor.tick("L1", 1)   # fenced for good: the current token neither heartbeats, releases nor starts
    state = store.state
    assert state["holders"]["L1"]["heartbeat_at"] == 1000 and world.supervisor.try_release("L1", 1) is False
    with pytest.raises(PermissionError, match="stale_token"):
        world.supervisor.start("L1", 1, ["x"])
    assert not world.spawned and store.state is state and world.supervisor.fenced is True   # no write-back either
    assert [(e["event"], e["reasons"]) for e in world.logs] == [("refused", ["stale_token"])]   # one ledger line

def test_a_child_committed_before_the_transaction_keeps_both_leases(sup):
    store = Store({"L1": lease(session_id="500")})
    store.before = lambda s: s.state["holders"].update(C=lease("C", parent_lease="L1", session_id="501"))
    world = World(store, sup[0], [EMPTY])
    world.supervisor.ps = lambda argv: ps_text((77, 1, 77), (700, 1, 501)) if "C" in store.state["holders"] else EMPTY
    assert world.supervisor.try_release("L1", 1) is False and set(store.state["holders"]) == {"L1", "C"}

def test_a_workload_that_ends_before_the_first_tick_still_ends_in_gone(sup):
    store = Store({"L1": lease()})
    world = World(store, sup[0], [EMPTY], proc=Proc(polls=0, code=3))   # not in the first snapshot
    assert world.supervisor.run("L1", 1, ["x"]) == 3 and "L1" not in store.state["holders"]
