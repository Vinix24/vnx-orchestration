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
from slot import slot_activation
from slot.slot_store import SlotStore

ACL = " 0: group:vnxslot allow list,add_file,search,delete_child,file_inherit,directory_inherit"
SWAP = "vm.swapusage: total = 5120.00M  used = 100.00M  free = 5020.00M  (encrypted)\n"
VM_STAT = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages occupied by compressor: 1024.\n"
DAY = 86400
DOCTOR_ACCOUNTS = ["vincentvandeth", "ci-runner"]
POLICY = {"total": 3, "reserved": 1, "runner_reserve": 1, "max_bypass": 3, "max_transition": 600, "max_queue": 5,
          "mode": "observe", "enforce_activation": {"doctor_accounts": list(DOCTOR_ACCOUNTS)},
          "memory": {"swap_growth_mb": 256, "swap_level_mb": 4096, "compressor_gb": 8},
          "profiles": {"runner": {"slots": 1, "max_slots": 8, "max_wait_s": 30, "heavy": False, "children": ["seo-gate"]},
                       "seo-gate": {"slots": 1, "max_slots": 4, "max_wait_s": 30, "heavy": True, "children": []}}}


def evidence(now):
    return {"doctor_accounts": list(DOCTOR_ACCOUNTS),
            "calibration": {"measured_at": now - 9 * DAY, "measurement_ref": "rs1.md", "seo_slots": 1},
            "doctor": {a: {"checked_at": now - DAY, "exit_code": 0, "evidence_ref": "log#1"}
                       for a in DOCTOR_ACCOUNTS},
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


_OBSERVE = object()


def _heavy_log(env, now, projects, mode=_OBSERVE, at=None):
    log = slot_cli.SlotLog(env.store_dir)
    for i, project in enumerate(projects):
        line = {"ts": (now - 5 * DAY if at is None else at) + i, "event": "requested", "project_id": project,
                "profile": "seo-gate"}
        line["mode"] = "observe" if mode is _OBSERVE else mode
        log.write(line)


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


_MISSING = object()


@pytest.mark.parametrize("accounts", [
    pytest.param(_MISSING, id="missing-key"),
    pytest.param(None, id="null"),
    pytest.param("vincentvandeth", id="not-a-list"),
    pytest.param([], id="empty"),
    pytest.param([""], id="empty-string"),
    pytest.param(["   "], id="blank"),
    pytest.param([1], id="non-string"),
    pytest.param(["vincentvandeth", "vincentvandeth"], id="duplicate"),
    pytest.param(["vincentvandeth", "ci-runner", None], id="non-string-among-names"),
])
def test_an_unreadable_doctor_accounts_list_blocks_with_that_one_condition(env, accounts):
    """The accounts come from the policy (ADR-062); every unreadable shape is one blocking condition."""
    now = time.time()
    proof = evidence(now)
    if accounts is _MISSING:
        proof.pop("doctor_accounts")
    else:
        proof["doctor_accounts"] = accounts
    patch_policy(env, enforce_activation=proof)
    _heavy_log(env, now, ["vnx-dev", "seo"])
    policy = json.loads(env.policy_path.read_text())
    assert slot_activation.missing(policy, slot_cli.read_events(env.store_dir), now) == [
        "doctor: no valid doctor_accounts in the policy"]   # calibration, observe and operator all pass
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 78
    assert json.loads(env.policy_path.read_text())["mode"] == "observe"


@pytest.mark.parametrize("policy", [
    pytest.param({}, id="no-enforce-activation"),
    pytest.param({"enforce_activation": {}}, id="no-doctor-accounts"),
    pytest.param({"enforce_activation": {"doctor_accounts": []}}, id="empty"),
])
def test_an_unreadable_doctor_accounts_list_checks_no_account(policy):
    """``missing`` refuses a policy without an ``enforce_activation`` object before it gets here, so
    the raw condition is pinned on the helper: even a positive record cannot rescue the list."""
    now = time.time()
    rec = {"vincentvandeth": {"checked_at": now - DAY, "exit_code": 0, "evidence_ref": "log#1"},
           "ci-runner": {"checked_at": now - DAY, "exit_code": 1, "evidence_ref": "log#1"}}
    assert slot_activation._doctor(policy, rec, now) == ["doctor: no valid doctor_accounts in the policy"]


def test_a_valid_doctor_accounts_list_of_one_passes_when_that_account_has_evidence(env):
    now = time.time()
    proof = evidence(now)
    proof["doctor_accounts"] = ["vincentvandeth"]
    proof["doctor"] = {"vincentvandeth": proof["doctor"]["vincentvandeth"]}   # ci-runner is not named
    patch_policy(env, enforce_activation=proof)
    _heavy_log(env, now, ["vnx-dev", "seo"])
    policy = json.loads(env.policy_path.read_text())
    assert slot_activation.missing(policy, slot_cli.read_events(env.store_dir), now) == []
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 0
    assert json.loads(env.policy_path.read_text())["mode"] == "enforce"


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


@pytest.mark.parametrize("mode", ["off", "enforce", "enforce_pending", None, "OBSERVE", 1])
def test_heavy_requests_not_recorded_in_observe_mode_do_not_prove_observe(env, mode):
    now = time.time()
    patch_policy(env, enforce_activation=evidence(now))
    _heavy_log(env, now, ["vnx-dev", "seo"], mode=mode)   # the slotlog writes null for a line without a mode
    code = cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0]
    assert code == 78 and json.loads(env.policy_path.read_text())["mode"] == "observe"
    assert events(env, "mode_changed") == []


@pytest.mark.parametrize("offset", [-8 * DAY - 10, -DAY + 10, 3600])
def test_observe_requests_outside_the_interval_do_not_prove_observe(env, offset):
    now = time.time()   # the interval is [now - 8 days, now - 1 day]
    patch_policy(env, enforce_activation=evidence(now))
    _heavy_log(env, now, ["vnx-dev", "seo"], at=now + offset)
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 78
    assert json.loads(env.policy_path.read_text())["mode"] == "observe"


@pytest.mark.parametrize("change", [{"to_mode": "off"}, {"to_mode": "enforce"}, {"to_mode": None}, {"ts": None}])
def test_leaving_observe_inside_the_interval_blocks(env, change):
    now = time.time()
    patch_policy(env, enforce_activation=evidence(now))
    _heavy_log(env, now, ["vnx-dev", "seo"])
    line = {"ts": now - 3 * DAY, "event": "mode_changed", "from_mode": "observe", "to_mode": "off", **change}
    with open(next((env.store_dir / "slotlog").glob("*.ndjson")), "a") as fh:
        fh.write(json.dumps(line) + "\n")
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 78
    assert json.loads(env.policy_path.read_text())["mode"] == "observe"


def test_observe_requests_inside_the_interval_prove_observe(env):
    now = time.time()
    patch_policy(env, enforce_activation=evidence(now))
    _heavy_log(env, now, ["vnx-dev", "seo"], at=now - 8 * DAY)   # the interval start itself counts
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 0


def test_mode_change_whose_transaction_fails_leaves_policy_and_slotlog_unchanged(env, monkeypatch):
    now = time.time()
    patch_policy(env, enforce_activation=evidence(now))
    _heavy_log(env, now, ["vnx-dev", "seo"])
    before = len(events(env))

    def broken(state):
        raise slot_cli.SlotError("state_corrupt", "injected")

    monkeypatch.setattr(slot_cli, "validate_state", broken)
    assert cli(env, "mode", "set", "enforce", "--project-id", "vnx-dev")[0] == 78
    assert json.loads(env.policy_path.read_text())["mode"] == "observe"
    assert len(events(env)) == before and state(env)["mode"] == "observe"


def test_mode_changed_is_written_before_the_policy_is_rewritten(env, monkeypatch):
    seen = []
    real = slot_cli._write_policy_mode

    def spy(store, policy, mode):
        seen.append([e["to_mode"] for e in events(env, "mode_changed")])
        real(store, policy, mode)

    monkeypatch.setattr(slot_cli, "_write_policy_mode", spy)
    assert cli(env, "mode", "set", "off", "--project-id", "vnx-dev")[0] == 0
    assert seen == [["off"]] and json.loads(env.policy_path.read_text())["mode"] == "off"


def test_a_failed_policy_rewrite_keeps_the_state(env, monkeypatch):
    def fail(store, policy, mode):
        raise slot_cli.SlotError("store_invalid", "injected")

    monkeypatch.setattr(slot_cli, "_write_policy_mode", fail)
    assert cli(env, "mode", "set", "off", "--project-id", "vnx-dev")[0] == 78
    assert json.loads(env.policy_path.read_text())["mode"] == "observe" and state(env)["mode"] == "observe"
    assert [e["event"] for e in events(env)] == ["mode_changed", "refused"]   # the record, then its undoing
    assert events(env, "refused")[0]["mode"] == "observe" and events(env, "refused")[0]["reasons"] == ["store_invalid"]


def test_a_policy_change_without_an_execution_change_is_still_recorded(env):
    patch_policy(env, mode="enforce")   # a timed-out transition: policy enforce, execution observe
    assert cli(env, "mode", "set", "observe", "--project-id", "vnx-dev")[0] == 0
    (ev,) = events(env, "mode_changed")
    assert (ev["from_mode"], ev["to_mode"], ev["reasons"]) == ("observe", "observe", ["policy_mode_set"])


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


def _queued_new(request_id, lease_id, requested_at):
    """Another client's request already in the queue (same shape ``_new_request`` writes)."""
    return {"request_id": request_id, "lease_id": lease_id, "requested_at": requested_at, "kind": "new",
            "parent_lease": None, "profile": "runner", "project_id": "other", "uid": os.getuid(),
            "owner": {"owner_kind": "acquire", "owner_pid": os.getpid(), "owner_start_time": None,
                      "session_id": None, "supervisor_pid": None, "supervisor_start_time": None,
                      "runner": None, "descendants": []},
            "bypassed_n": 0, "reasons": [], "slots": 1, "max_slots": 2, "deadline_at": requested_at + 30}


def test_a_refusal_in_a_round_that_admits_another_commits_that_admission(env):
    profiles = copy.deepcopy(POLICY["profiles"])
    profiles["runner"]["max_slots"] = 2
    patch_policy(env, mode="enforce", total=6, profiles=profiles)
    patch_state(env, mode="enforce")
    _, out = cli(env, "acquire", "runner", "--project-id", "a", "--owner-pid", str(os.getpid()))
    parent = out[0].split(":")[0]
    before = state(env)["fencing_counter"]
    patch_state(env, queue=[_queued_new("r-other", "L-other", 1.0)])
    # a runner child under a runner parent is not allowed: refused in the same round that admits L-other
    code, _ = cli(env, "acquire", "runner", "--project-id", "b", "--owner-pid", str(os.getpid()),
                  env={"VNX_SLOT_LEASE": parent})
    after = state(env)
    assert code == 78
    assert "L-other" in after["holders"] and after["fencing_counter"] == before + 1
    assert after["queue"] == []   # neither the admitted nor the refused request stays queued
    admitted = [e["lease_id"] for e in events(env, "admitted")]
    assert admitted.count("L-other") == 1 and set(admitted) == set(after["holders"])
    assert events(env, "refused")[-1]["reasons"] == ["profile_not_allowed_under_parent"]


def test_release_of_a_live_lease_logs_the_request_before_releasing(env):
    owner = subprocess.Popen(["sleep", "30"])
    try:
        _, out = cli(env, "acquire", "runner", "--project-id", "p", "--owner-pid", str(owner.pid))
        lease_id, _ = out[0].split(":")
        assert cli(env, "release", out[0]) == (0, ["releasing"])
        assert cli(env, "release", out[0]) == (0, ["releasing"])   # a repeat changes nothing, logs nothing
    finally:
        owner.kill()
        owner.wait()
    (ev,) = events(env, "release_requested")
    assert ev["lease_id"] == lease_id and ev["project_id"] == "p" and ev["reasons"] == [] and "token" not in ev
    assert state(env)["holders"][lease_id]["state"] == "releasing"


def test_release_requested_not_logged_means_releasing_not_committed(env, monkeypatch):
    owner = subprocess.Popen(["sleep", "30"])
    try:
        _, out = cli(env, "acquire", "runner", "--project-id", "p", "--owner-pid", str(owner.pid))
        lease_id = out[0].split(":")[0]
        real = slot_cli.SlotLog.write

        def failing(self, ev):
            if ev["event"] == "release_requested":
                raise slot_cli.SlotError("store_invalid", "slotlog write failed")
            real(self, ev)
        monkeypatch.setattr(slot_cli.SlotLog, "write", failing)
        assert cli(env, "release", out[0])[0] == 78
    finally:
        owner.kill()
        owner.wait()
    assert state(env)["holders"][lease_id]["state"] == "active"


@pytest.mark.parametrize("make", ["missing", "not_executable"])
def test_off_mode_start_failure_is_structured_78(env, tmp_path, capsys, make):
    patch_policy(env, mode="off")
    target = tmp_path / "cmd"
    if make == "not_executable":
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o644)
    code, _ = cli(env, "run", "runner", "--project-id", "p", "--", str(target))
    err = capsys.readouterr().err
    assert code == 78 and "vnx slot run: command not started:" in err and "Traceback" not in err
    assert state(env)["holders"] == {}


def test_a_lease_hint_from_another_session_is_ignored_by_name(env):
    other = subprocess.Popen(["sleep", "30"], start_new_session=True)   # a lease owned in another session
    try:
        _, out = cli(env, "acquire", "runner", "--project-id", "a", "--owner-pid", str(other.pid))
        foreign = out[0].split(":")[0]
        assert state(env)["holders"][foreign]["session_id"] != str(os.getsid(0))
        code, out = cli(env, "acquire", "seo-gate", "--project-id", "b", "--owner-pid", str(os.getpid()),
                        env={"VNX_SLOT_LEASE": foreign})
        assert code == 0
        mine = state(env)["holders"][out[0].split(":")[0]]
        assert mine["parent_lease"] is None   # the foreign lease's budget is not consumed
        ignored = events(env, "parent_hint_ignored")
        assert [(e["lease_id"], e["project_id"], e["reasons"]) for e in ignored] == [
            (foreign, "b", ["hint_session_mismatch"])]
    finally:
        other.kill()
        other.wait()


def test_a_lease_hint_for_an_unknown_lease_is_ignored_by_name(env):
    code, _ = cli(env, "acquire", "runner", "--project-id", "a", "--owner-pid", str(os.getpid()),
                  env={"VNX_SLOT_LEASE": "L-gone"})
    assert code == 0
    assert [e["reasons"] for e in events(env, "parent_hint_ignored")] == [["hint_unknown_lease"]]


def test_slotlog_sets_the_mode_only_on_the_file_it_creates(env, monkeypatch):
    from slot import slot_log
    calls = []
    monkeypatch.setattr(slot_log.os, "fchmod", lambda fd, mode: calls.append(mode))
    log = slot_log.SlotLog(env.store_dir)
    log.write({"ts": 1_800_000_000, "event": "released", "project_id": "p"})
    assert calls == [0o660]   # created here: group write is set despite the umask
    # an existing monthly log (another user's file) is appended to without fchmod, which would raise EPERM
    log.write({"ts": 1_800_000_001, "event": "released", "project_id": "p"})
    assert calls == [0o660]
    assert len(events(env, "released")) == 2
