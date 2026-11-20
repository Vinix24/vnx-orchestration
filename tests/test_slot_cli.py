"""RS-8: vnx slot acquire, run, status, release and mode on a temp store (never /Users/Shared/vnx-slots).

Rights go through the RS-2 stat seam; processes, ps and the clock are real unless a test pins them."""
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import slot_cli  # imported after the path setup above
from slot.slot_store import SlotStore

ACL = " 0: group:vnxslot allow list,add_file,search,delete_child,file_inherit,directory_inherit"
SWAP = "vm.swapusage: total = 5120.00M  used = 100.00M  free = 5020.00M  (encrypted)\n"
VM_STAT = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages occupied by compressor: 1024.\n"
DAY = 86400
POLICY = {"total": 3, "reserved": 1, "runner_reserve": 1, "max_bypass": 3, "max_transition": 600, "max_queue": 5,
          "mode": "observe", "enforce_activation": {},
          "memory": {"swap_growth_mb": 256, "swap_level_mb": 4096, "compressor_gb": 8},
          "profiles": {"runner": {"slots": 1, "max_slots": 8, "max_wait_s": 30, "heavy": False, "children": ["seo-gate"]},
                       "seo-gate": {"slots": 1, "max_slots": 4, "max_wait_s": 30, "heavy": True, "children": []}}}


def evidence(now):
    return {"calibration": {"measured_at": now - 9 * DAY, "measurement_ref": "rs1.md", "seo_slots": 1},
            "doctor": {a: {"checked_at": now - DAY, "exit_code": 0, "evidence_ref": "log#1"}
                       for a in ("vincentvandeth", "ci-runner")},
            "observe": {"started_at": now - 8 * DAY, "ended_at": now - DAY, "project_ids": ["vnx-dev", "seo"],
                        "evidence_ref": "slotlog"},
            "operator": {"decided_at": now - 100, "decision_ref": "besluit 2026-11-20"}}


@pytest.fixture
def env(tmp_path):
    store, pdir = tmp_path / "store", tmp_path / "policy"
    store.mkdir(), pdir.mkdir()
    (pdir / "host.json").write_text(json.dumps(POLICY))
    uid = os.getuid()
    table = {store: {"uid": uid, "group": "vnxslot", "mode": 0o2770, "acl": ACL},
             pdir: {"uid": uid, "group": "staff", "mode": 0o755, "acl": ""},
             pdir / "host.json": {"uid": uid, "group": "staff", "mode": 0o644, "acl": ""}}
    st = SlotStore(store, pdir / "host.json", stat_fn=lambda p: dict(table[p]))
    st.initialize_state()
    return st


def cli(st, *argv, **seams):
    out = []
    seams.setdefault("sample_runner", lambda a: SWAP if a[0] == "sysctl" else VM_STAT)
    code = slot_cli.main(list(argv), store=st, out=out.append, **seams)
    return code, out


def events(st, name=None):
    lines = [json.loads(x) for f in sorted((st.store_dir / "slotlog").glob("*.ndjson")) for x in f.read_text().splitlines()]
    return [e for e in lines if name is None or e["event"] == name]


def state(st):
    return json.loads((st.store_dir / "state.json").read_text())


def patch_state(st, **fields):
    raw = state(st)
    raw.update(fields)
    (st.store_dir / "state.json").write_text(json.dumps(raw))


def patch_policy(st, **fields):
    raw = json.loads(st.policy_path.read_text())
    raw.update(fields)
    st.policy_path.write_text(json.dumps(raw))


def test_run_holds_the_lease_for_the_workload_session_and_releases_after(env, tmp_path):
    seen = tmp_path / "seen.json"
    probe = ("import fcntl,json,os,sys; fd=os.open(sys.argv[3],os.O_RDWR); fcntl.flock(fd,fcntl.LOCK_EX); "
             "s=json.load(open(sys.argv[1])); "
             "json.dump({'holders': s['holders'], 'sid': os.getsid(0)}, open(sys.argv[2],'w')); sys.exit(3)")
    code, _ = cli(env, "run", "runner", "--project-id", "vnx-dev", "--", sys.executable, "-c", probe,
                  str(env.store_dir / "state.json"), str(seen), str(env.store_dir / "ledger.lock"), interval=0.05)
    assert code == 3   # the workload's own exit code
    during = json.loads(seen.read_text())
    (lease,) = during["holders"].values()
    assert lease["owner_kind"] == "run" and lease["session_id"] == str(during["sid"]) and lease["project_id"] == "vnx-dev"
    assert lease["supervisor_pid"] == os.getpid() and during["sid"] != os.getsid(0)   # its own session
    assert state(env)["holders"] == {} and state(env)["shadow"]["holders"] == {}
    assert [e["event"] for e in events(env)] == ["requested", "would_wait", "admitted", "released"]
    assert all("token" not in e and e["project_id"] == "vnx-dev" for e in events(env))


def test_observe_admits_at_once_and_logs_would_wait_from_the_shadow(env):
    profiles = copy.deepcopy(POLICY["profiles"])
    profiles["runner"]["max_slots"] = 1
    patch_policy(env, profiles=profiles)
    first = cli(env, "acquire", "runner", "--project-id", "a", "--owner-pid", str(os.getpid()))
    second = cli(env, "acquire", "runner", "--project-id", "b", "--owner-pid", str(os.getpid()))
    assert first[0] == second[0] == 0 and len(state(env)["holders"]) == 2   # pool 1, both admitted
    one, two = events(env, "would_wait")
    assert (one["would_wait"], one["estimated_wait_s"], one["reasons"]) == (False, 0, [])
    assert (two["would_wait"], two["estimated_wait_s"], two["reasons"], two["estimate"]) == (True, None, ["budget_full"], True)
    lease_id, token = second[1][0].split(":")
    assert state(env)["holders"][lease_id]["token"] == int(token)


@pytest.mark.parametrize("content", ["{", "[]", json.dumps({"holders": {}})])
@pytest.mark.parametrize("argv", [("status",), ("acquire", "runner", "--project-id", "p", "--owner-pid", "1"),
                                  ("release", "L-x:1"), ("mode", "show")])
def test_corrupt_state_refuses_78_never_off(env, content, argv):
    (env.store_dir / "state.json").write_text(content)
    assert cli(env, *argv)[0] == 78


def test_corrupt_state_never_starts_the_workload(env, tmp_path):
    (env.store_dir / "state.json").write_text("{")
    marker = tmp_path / "ran"
    code, _ = cli(env, "run", "runner", "--project-id", "p", "--", "touch", str(marker))
    assert code == 78 and not marker.exists()


def test_policy_off_runs_without_a_lease(env, tmp_path):
    patch_policy(env, mode="off")
    marker = tmp_path / "ran"
    assert cli(env, "run", "runner", "--project-id", "p", "--", "touch", str(marker))[0] == 0
    assert marker.exists() and state(env)["holders"] == {} and state(env)["mode"] == "off"


def test_acquire_without_owner_pid_refuses_78(env):
    assert cli(env, "acquire", "runner", "--project-id", "p")[0] == 78
    (ev,) = events(env, "refused")
    assert ev["reasons"] == ["owner_pid_required"] and ev["exit_code"] == 78 and ev["project_id"] == "p"


def test_unknown_profile_is_policy_invalid(env):
    assert cli(env, "acquire", "nope", "--project-id", "p", "--owner-pid", str(os.getpid()))[0] == 78
    assert events(env, "refused")[0]["reasons"] == ["policy_invalid"]


def test_release_with_a_wrong_token_is_stale_and_a_live_owner_only_marks_releasing(env):
    owner = subprocess.Popen(["sleep", "30"])
    try:
        _, out = cli(env, "acquire", "runner", "--project-id", "p", "--owner-pid", str(owner.pid))
        lease_id, token = out[0].split(":")
        for bad in (f"{lease_id}:{int(token) + 1}", f"{lease_id}:x", lease_id, f"L-other:{token}"):
            assert cli(env, "release", bad)[0] == 78
        assert {tuple(e["reasons"]) for e in events(env, "refused")} == {("stale_token",)}
        assert cli(env, "release", f"{lease_id}:{token}") == (0, ["releasing"])
        assert state(env)["holders"][lease_id]["state"] == "releasing"
    finally:
        owner.kill()
        owner.wait()
    assert cli(env, "release", f"{lease_id}:{token}") == (0, ["released"])
    assert state(env)["holders"] == {} and events(env, "released")[0]["hold_s"] >= 0


def test_a_released_lease_whose_owner_died_is_reaped_by_the_next_request(env):
    owner = subprocess.Popen(["sleep", "30"])
    _, out = cli(env, "acquire", "runner", "--project-id", "p", "--owner-pid", str(owner.pid))
    owner.kill()
    owner.wait()
    cli(env, "acquire", "runner", "--project-id", "q", "--owner-pid", str(os.getpid()))
    assert out[0].split(":")[0] not in state(env)["holders"] and len(events(env, "reaped")) == 1


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _enforce_full(env):
    profiles = copy.deepcopy(POLICY["profiles"])
    profiles["runner"]["max_slots"] = 1   # a ceiling of 8 on pool 1 is never safe for the banker's test
    patch_policy(env, mode="enforce", profiles=profiles)
    patch_state(env, mode="enforce")
    assert cli(env, "acquire", "runner", "--project-id", "a", "--owner-pid", str(os.getpid()))[0] == 0


def test_a_bounded_wait_ends_in_75_and_leaves_the_queue(env):
    _enforce_full(env)
    clock = Clock()
    code, _ = cli(env, "acquire", "runner", "--project-id", "b", "--owner-pid", str(os.getpid()),
                  clock=clock, sleep=clock.sleep)
    assert code == 75 and state(env)["queue"] == [] and len(state(env)["holders"]) == 1
    refused = events(env, "refused")[-1]
    assert refused["reasons"][0] == "wait_timeout" and refused["exit_code"] == 75 and refused["wait_s"] >= 30
    assert "budget_full" in events(env, "waited")[0]["reasons"]


def test_a_full_queue_refuses_75(env):
    _enforce_full(env)
    patch_policy(env, max_queue=1)
    raw = state(env)
    patch_state(env, queue=[{**copy.deepcopy(raw["holders"][next(iter(raw["holders"]))]), "request_id": "r-0",
                             "kind": "new", "slots": 1, "requested_at": 1.0, "bypassed_n": 0, "reasons": [],
                             "uid": os.getuid(), "lease_id": "L-0", "owner": {}}])
    assert cli(env, "acquire", "runner", "--project-id", "b", "--owner-pid", str(os.getpid()))[0] == 75
    assert events(env, "refused")[-1]["reasons"] == ["queue_full"]


def test_mode_set_enforce_without_evidence_refuses_and_leaves_the_policy(env):
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 78
    assert json.loads(env.policy_path.read_text())["mode"] == "observe"
    assert events(env, "refused")[0]["reasons"] == ["activation_incomplete"]


def _heavy_log(env, now, projects):
    log = slot_cli.SlotLog(env.store_dir)
    for i, project in enumerate(projects):
        log.write({"ts": now - 5 * DAY + i, "event": "requested", "project_id": project, "profile": "seo-gate"})


@pytest.mark.parametrize("break_it", [
    lambda e: e["doctor"]["ci-runner"].update(exit_code=True),
    lambda e: e["doctor"]["ci-runner"].update(exit_code="0"),
    lambda e: e["doctor"].pop("ci-runner"),
    lambda e: e["calibration"].update(seo_slots=2),
    lambda e: e["observe"].update(started_at=e["observe"]["ended_at"] - 6 * DAY),
    lambda e: e["observe"].update(project_ids=["vnx-dev"]),
    lambda e: e["operator"].update(decided_at=None),
    lambda e: e["operator"].update(decided_at=e["observe"]["ended_at"] - 1),
    lambda e: e.pop("calibration"),
])
def test_each_missing_activation_condition_blocks(env, break_it):
    now = time.time()
    proof = evidence(now)
    break_it(proof)
    patch_policy(env, enforce_activation=proof)
    _heavy_log(env, now, ["vnx-dev", "seo"])
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 78
    assert json.loads(env.policy_path.read_text())["mode"] == "observe"


def test_heavy_requests_of_only_one_project_in_the_slotlog_block(env):
    now = time.time()
    patch_policy(env, enforce_activation=evidence(now))
    _heavy_log(env, now, ["vnx-dev", "vnx-dev"])
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 78


def test_a_corrupt_slotlog_line_blocks_activation(env):
    now = time.time()
    patch_policy(env, enforce_activation=evidence(now))
    _heavy_log(env, now, ["vnx-dev", "seo"])
    with open(next((env.store_dir / "slotlog").glob("*.ndjson")), "a") as fh:
        fh.write("{not json\n")
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 78
    assert json.loads(env.policy_path.read_text())["mode"] == "observe"


def test_mode_set_enforce_with_evidence_activates_an_empty_host(env):
    now = time.time()
    patch_policy(env, enforce_activation=evidence(now))
    _heavy_log(env, now, ["vnx-dev", "seo"])
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 0
    assert json.loads(env.policy_path.read_text())["mode"] == "enforce"
    assert state(env)["mode"] == "enforce" and state(env)["transition"]["outcome"] == "activated"
    assert [(e["from_mode"], e["to_mode"]) for e in events(env, "mode_changed")] == [
        ("observe", "enforce_pending"), ("enforce_pending", "enforce")]


def test_enforce_drains_grandfathered_leases_and_times_out_to_observe(env):
    now = time.time()
    assert cli(env, "acquire", "runner", "--project-id", "a", "--owner-pid", str(os.getpid()))[0] == 0
    patch_policy(env, enforce_activation=evidence(now))
    _heavy_log(env, now, ["vnx-dev", "seo"])
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 0
    (held,) = state(env)["holders"].values()   # need 7 against pool 1: unsafe, so it drains
    assert state(env)["mode"] == "enforce_pending" and held["grandfathered"] is True and held["state"] == "grandfathered"
    clock = Clock(now + 601)
    assert cli(env, "acquire", "runner", "--project-id", "b", "--owner-pid", str(os.getpid()),
               clock=clock, sleep=clock.sleep)[0] == 0   # fell back to observe: admitted at once
    last = events(env, "mode_changed")[-1]
    assert (last["to_mode"], last["reasons"], last["transition_s"] >= 600) == ("observe", ["transition_timeout"], True)
    assert state(env)["transition"]["outcome"] == "timed_out" and state(env)["mode"] == "observe"


def test_mode_set_by_someone_else_than_the_policy_owner_refuses(env, monkeypatch):
    other = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: other)   # the policy file belongs to the real uid
    assert cli(env, "mode", "set", "off", "--project-id", "p")[0] == 78
    assert json.loads(env.policy_path.read_text())["mode"] == "observe"


def test_status_shows_holders_without_tokens(env):
    cli(env, "acquire", "runner", "--project-id", "a", "--owner-pid", str(os.getpid()))
    code, out = cli(env, "status")
    shown = json.loads(out[0])
    assert code == 0 and shown["free"] == 0 and shown["pool"] == 1 and "token" not in out[0]
    assert shown["holders"][0]["project_id"] == "a"


def test_usage_errors_exit_2(env):
    for argv in ([], ["bogus"], ["run", "runner", "--project-id", "p"], ["run", "runner", "--project-id", "p", "--"],
                 ["acquire", "runner"], ["acquire", "runner", "--project-id", " ", "--owner-pid", "1"]):
        assert cli(env, *argv)[0] == 2


def test_slotlog_loops_on_a_short_write_and_strips_the_token(env, monkeypatch):
    from slot import slot_log
    real = os.write
    monkeypatch.setattr(slot_log.os, "write", lambda fd, data: real(fd, bytes(data[:3])))
    slot_log.SlotLog(env.store_dir).write({"ts": 1_800_000_000, "event": "released", "project_id": "p", "token": 9})
    (line,) = events(env)
    assert line["event"] == "released" and "token" not in line and set(slot_log._FIELDS) <= set(line)


@pytest.mark.parametrize("bad", [{"hold_s": float("nan")}, {"wait_s": float("inf")}, {"ts": None}])
def test_slotlog_refuses_a_line_the_reader_cannot_accept(env, bad):
    from slot import slot_log
    with pytest.raises(slot_cli.SlotError, match="store_invalid"):
        slot_log.SlotLog(env.store_dir).write({"ts": 1_800_000_000, "event": "released", "project_id": "p", **bad})
    assert not (env.store_dir / "slotlog").exists() or events(env) == []


def test_slotlog_write_failure_surfaces(env, monkeypatch):
    from slot import slot_log
    monkeypatch.setattr(slot_log.os, "write", lambda fd, data: 0)
    with pytest.raises(slot_cli.SlotError, match="wrote nothing"):
        slot_log.SlotLog(env.store_dir).write({"ts": 1_800_000_000, "event": "released", "project_id": "p"})
