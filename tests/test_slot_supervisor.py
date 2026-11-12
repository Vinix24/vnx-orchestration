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

    @contextlib.contextmanager
    def transaction(self):
        work = copy.deepcopy(self.state)
        yield work
        self.state = work

class RacingStore(Store):
    """``before`` runs once, ahead of the next transaction: another supervisor committing in the gap."""

    before = None

    def transaction(self):
        hook, self.before = self.before, None
        if hook:
            hook(self)
        return super().transaction()

class OrderedStore(Store):
    def __init__(self, holders, order):
        super().__init__(holders)
        self.order = order

    @contextlib.contextmanager
    def transaction(self):
        with super().transaction() as work:
            yield work
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
        script = self.ps_script
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        return item

ALIVE_TABLE = ps_text((77, 1, 77), (500, 77, 500), (600, 500, 500))
SESSION_ONLY = ps_text((77, 1, 77), (600, 500, 500))
EMPTY = ps_text((77, 1, 77))

def test_start_uses_a_new_session_and_books_identities(sup):
    mod = sup[0]
    store = Store({"L1": lease()})
    world = World(store, mod, [ALIVE_TABLE])
    world.supervisor.start("L1", 1, ["make", "test"])
    argv, kwargs = world.spawned[0]
    assert argv == ["make", "test"] and kwargs["start_new_session"] is True
    assert kwargs["env"][mod.PARENT_ENV] == "L1"
    rec = store.state["holders"]["L1"]
    assert (rec["session_id"], rec["owner_pid"], rec["owner_start_time"]) == ("500", 500, LSTART)
    assert (rec["supervisor_pid"], rec["supervisor_start_time"], rec["parent_lease"]) == (77, LSTART, None)

def test_nested_run_records_parent_lease_and_checks_the_profile(sup):
    mod, _, adm = sup
    store = Store({"P": lease("P", token=3, slots_granted=8), "L1": lease(profile="seo-gate")})
    world = World(store, mod, [ALIVE_TABLE], env={mod.PARENT_ENV: "P"})
    world.supervisor.start("L1", 1, ["x"])
    assert store.state["holders"]["L1"]["parent_lease"] == "P"
    assert [e["event"] for e in world.logs] == ["nested"] and world.logs[0]["parent_lease"] == "P"
    other = Store({"P": lease("P", slots_granted=8), "L1": lease(profile="runner")})
    refused = World(other, mod, [ALIVE_TABLE], env={mod.PARENT_ENV: "P"})
    with pytest.raises(adm.AdmissionInvalid, match="profile_not_allowed_under_parent"):
        refused.supervisor.start("L1", 1, ["x"])
    assert other.state["holders"]["L1"]["parent_lease"] is None and not refused.spawned

def test_killed_supervisor_keeps_the_slots_until_the_session_is_empty(sup):
    mod, live, _ = sup
    store = Store({"L1": lease(session_id="500", supervisor_pid=77, supervisor_start_time=LSTART)})
    gone_supervisor = ps_text((600, 1, 500))
    holders = store.state["holders"]
    assert live.supervisor_gone_tree_alive(holders["L1"], live.snapshot(lambda a: gone_supervisor), holders)
    world = World(store, mod, [gone_supervisor], now=1000 + 86400)   # heartbeat long expired
    assert world.supervisor.try_release("L1", 1) is False and "L1" in store.state["holders"]
    world.ps_script = [EMPTY]
    assert world.supervisor.try_release("L1", 1) is True and "L1" not in store.state["holders"]
    assert [e["event"] for e in world.logs] == ["released"]

def test_workload_exit_waits_for_the_session_and_returns_its_exit_code(sup):
    mod = sup[0]
    store = Store({"L1": lease()})
    world = World(store, mod, [ALIVE_TABLE, SESSION_ONLY, SESSION_ONLY, EMPTY], proc=Proc(polls=2, code=-9))
    assert world.supervisor.run("L1", 1, ["x"]) == -9   # a signal exit comes back unchanged
    assert "L1" not in store.state["holders"] and world.logs[-1]["event"] == "released"
    assert world.now > 1000 + 15 * 2   # it kept waiting while a process of the session lived

def test_released_lease_takes_its_gone_child_leases_with_it(sup):
    store = Store({"L1": lease(session_id="500"), "C": lease("C", parent_lease="L1", session_id="501")})
    world = World(store, sup[0], [EMPTY])
    assert world.supervisor.try_release("L1", 1) is True
    assert store.state["holders"] == {} and [e["event"] for e in world.logs] == ["reaped", "released"]

def test_a_live_child_lease_blocks_the_release(sup):
    store = Store({"L1": lease(session_id="500"), "C": lease("C", parent_lease="L1", session_id="501")})
    world = World(store, sup[0], [ps_text((77, 1, 77), (700, 1, 501))])
    assert world.supervisor.try_release("L1", 1) is False and set(store.state["holders"]) == {"L1", "C"}

@pytest.mark.parametrize("script,session", [([RuntimeError("ps broke")], "500"), (["garbage\n"], "500"),
                                            ([""], "500"), ([EMPTY], None)])
def test_unknown_liveness_releases_nothing(sup, script, session):
    store = Store({"L1": lease(session_id=session)})
    world = World(store, sup[0], script)
    assert world.supervisor.try_release("L1", 1) is False and "L1" in store.state["holders"]
    assert world.logs == []

def test_samples_land_in_the_bucket_of_now_and_a_second_writer_writes_nothing(sup):
    mod = sup[0]
    store = Store({"L1": lease()})
    first = World(store, mod, [ALIVE_TABLE], now=1010.0)
    first.supervisor.tick("L1", 1)
    second = World(store, mod, [ALIVE_TABLE], now=1015.0)   # buckets: 990-1019, then 1020-1049
    assert [s["bucket_start"] for s in store.state["samples"]] == [990]
    second.supervisor.tick("L1", 1)
    assert len(store.state["samples"]) == 1 and store.state["samples"][0]["status"] == "ok"
    second.now = 1025.0
    second.supervisor.tick("L1", 1)
    assert [s["bucket_start"] for s in store.state["samples"]] == [990, 1020]
    assert store.state["holders"]["L1"]["heartbeat_at"] == 1025.0   # the heartbeat carries the token

@pytest.mark.parametrize("token", [2, None, "1", True])
def test_token_mismatch_stops_the_heartbeat_and_never_releases(sup, token):
    store = Store({"L1": lease(session_id="500")})
    world = World(store, sup[0], [EMPTY])
    world.supervisor.tick("L1", token)
    assert world.supervisor.fenced is True and store.state["holders"]["L1"]["heartbeat_at"] == 1000
    assert world.logs[0]["event"] == "refused" and world.logs[0]["reasons"] == ["stale_token"]
    world.supervisor.tick("L1", 1)   # fenced for good: not even the right token heartbeats again
    assert store.state["holders"]["L1"]["heartbeat_at"] == 1000
    assert world.supervisor.try_release("L1", token) is False and "L1" in store.state["holders"]

def test_a_fenced_run_ends_without_taking_the_lease_over(sup):
    store = Store({"L1": lease(token=5)})
    world = World(store, sup[0], [ALIVE_TABLE])
    with pytest.raises(PermissionError, match="stale_token"):
        world.supervisor.run("L1", 1, ["x"])
    assert not world.spawned and "L1" in store.state["holders"]

def test_a_child_committed_before_the_transaction_keeps_both_leases(sup):
    store = RacingStore({"L1": lease(session_id="500")})
    child = lease("C", parent_lease="L1", session_id="501")
    store.before = lambda s: s.state["holders"].update(C=child)
    world = World(store, sup[0], [EMPTY])
    # the child's session is in the process table from the moment its lease exists
    world.ps = lambda argv: ps_text((77, 1, 77), (700, 1, 501)) if "C" in store.state["holders"] else EMPTY
    assert world.supervisor.try_release("L1", 1) is False and set(store.state["holders"]) == {"L1", "C"}
    assert world.logs == []

def test_a_workload_that_ends_before_the_first_tick_still_ends_in_gone(sup):
    store = Store({"L1": lease()})
    world = World(store, sup[0], [EMPTY], proc=Proc(polls=0, code=3))   # not in the first snapshot
    assert world.supervisor.run("L1", 1, ["x"]) == 3 and "L1" not in store.state["holders"]
    assert world.logs[-1]["event"] == "released"

def test_a_root_start_logs_before_the_state_commit(sup):
    order = []
    store = OrderedStore({"L1": lease()}, order)
    world = World(store, sup[0], [ALIVE_TABLE])
    world.supervisor.log = lambda ev: (order.append(ev["event"]), world.logs.append(ev))
    world.supervisor.start("L1", 1, ["x"])
    assert order == ["admitted", "commit"] and world.logs[0]["lease_id"] == "L1" and world.logs[0]["parent_lease"] is None
