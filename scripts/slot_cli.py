#!/usr/bin/env python3
"""vnx slot — host capacity slots (ADR-062). RS-2 shipped ``doctor``; RS-8 wires the rest.

``acquire``, ``run`` and ``release`` connect the store (RS-2),
admission (RS-3), liveness (RS-4), memory (RS-5), supervisor (RS-6) and shadow queue (RS-7);
every slot event goes to the slotlog. Exit codes: 0 done (observe admits at once), 75 temporary
capacity refusal (``queue_full``, ``wait_timeout``), 78 invalid store, policy, state, request,
owner or token, 2 a usage error. ``run`` returns the workload's own exit code once
the workload started. A corrupt state or policy is 78, never mode ``off``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from slot import slot_memory, slot_shadow
from slot import slot_liveness as liveness
from slot.slot_admission import (AdmissionInvalid, StateCorrupt, _event, _grown, _make_lease,
                                 check_token, decide_round, nested_plan)
from slot.slot_log import SlotLog
from slot.slot_store import SlotError, SlotStore
from slot.slot_supervisor import PARENT_ENV, StartRefused, Supervisor, _children_of

EXIT_TEMPFAIL, EXIT_INVALID, EXIT_USAGE = 75, 78, 2
POLL_SECONDS = 2
USAGE = """usage: vnx slot doctor
       vnx slot acquire <profile> --project-id <id> --owner-pid <pid>
       vnx slot run <profile> --project-id <id> -- <command> [args...]
       vnx slot release <lease-id:token> [--project-id <id>]"""
_WAIT, _OFF = object(), object()


class Refused(SlotError):
    """A refusal already written to the slotlog; ``exit_code`` is 75 or 78."""

    def __init__(self, code: str, detail: str = "", exit_code: int = EXIT_INVALID) -> None:
        super().__init__(code, detail)
        self.exit_code = exit_code


@dataclass
class Ctx:
    store: SlotStore
    log: Callable[[Dict[str, Any]], None]
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    ps: Callable = liveness._run_ps
    sample_runner: Callable = liveness._run_ps
    spawn: Callable = subprocess.Popen
    env: Dict[str, str] = field(default_factory=lambda: dict(os.environ))
    interval: float = 15
    poll: float = POLL_SECONDS
    out: Callable[[str], None] = print


def _refuse(ctx: Ctx, state: dict, src: dict, reason: str, code: int = EXIT_INVALID, detail: str = "") -> None:
    ctx.log(_event(state, "refused", ctx.clock(), src, reasons=[reason], exit_code=code))
    raise Refused(reason, detail, code)


def _refuse_now(ctx: Ctx, src: dict, reason: str, detail: str = "") -> None:
    """A refusal before any state change: logged under the lock with the effective mode, nothing written."""
    with ctx.store.transaction() as state:
        _refuse(ctx, state, src, reason, detail=detail)



def _mode_changed(ctx: Ctx, state: dict, now: float, project_id: Optional[str], to: str) -> None:
    ev = _event(state, "mode_changed", now, {"project_id": project_id, "uid": os.getuid()},
                from_mode=state["mode"], to_mode=to, transition_s=None, reasons=[])
    state["mode"] = ev["mode"] = to
    ctx.log(ev)


def _sync_mode(ctx: Ctx, state: dict, policy: dict, now: float, project_id: Optional[str]) -> None:
    """Follow the policy mode for ``off`` and ``observe``. ``enforce`` is only entered through the
    activation gate of ``mode set enforce`` (pending first), never by a read of the policy."""
    want = policy["mode"]
    if want in ("off", "observe") and state["mode"] != want:
        _mode_changed(ctx, state, now, project_id, want)



def _tree_verdict(state: dict, lease_id: str, table) -> str:
    """GONE only when the lease and every child lease are positively gone; acquire leases learn descendants."""
    holders, verdicts = state["holders"], []
    for key in [lease_id, *_children_of(holders, lease_id)]:
        verdict, found = liveness.lease_liveness(holders[key], table, holders)
        if holders[key].get("owner_kind") == "acquire":
            holders[key]["descendants"] = found
        verdicts.append(verdict)
    if all(v == liveness.GONE for v in verdicts):
        return liveness.GONE
    return liveness.ALIVE if liveness.ALIVE in verdicts else liveness.UNKNOWN


def _remove(ctx: Ctx, state: dict, lease_id: str, now: float, event: str) -> None:
    holders = state["holders"]
    for key in _children_of(holders, lease_id):
        child = holders.pop(key)
        ctx.log(_event(state, "reaped", now, child, hold_s=now - child["admitted_at"], reasons=[]))
    lease = holders.pop(lease_id)
    ctx.log(_event(state, event, now, lease, hold_s=now - lease["admitted_at"], reasons=[]))


def _sync_shadow(state: dict, now: float) -> None:
    """Shadow holders and queue entries of leases that no longer exist leave the shadow (with their hold)."""
    live = set(state["holders"]) | {r.get("lease_id") for r in state["queue"]}
    for key in [k for k in state["shadow"]["holders"] if k not in live]:
        if key in state["shadow"]["holders"]:
            state["shadow"] = slot_shadow.shadow_release(state, key, now)["shadow"]
    state["shadow"]["queue"] = [r for r in state["shadow"]["queue"] if r.get("lease_id") in live]


def _reap(ctx: Ctx, state: dict, now: float):
    """Free unsupervised root leases whose whole tree is positively gone: acquire leases, and run leases
    whose supervisor is gone (a run lease with a live supervisor is released by that supervisor)."""
    table = liveness.snapshot(ctx.ps)
    holders = state["holders"]
    for key in [k for k, h in holders.items() if h.get("parent_lease") is None]:
        lease = holders.get(key)
        if lease is None:
            continue
        kind = lease.get("owner_kind")
        sup_gone = kind == "run" and liveness.process_alive(
            table, lease.get("supervisor_pid"), lease.get("supervisor_start_time")) == liveness.GONE
        if not (kind == "acquire" or sup_gone):
            continue
        # a run lease whose supervisor died before it started a session never had a workload
        never_started = sup_gone and lease.get("session_id") is None and not _children_of(holders, key)
        if never_started or _tree_verdict(state, key, table) == liveness.GONE:
            _remove(ctx, state, key, now, "released" if lease.get("state") == "releasing" else "reaped")
    _sync_shadow(state, now)
    return table



@dataclass
class _Pending:
    rec: Optional[dict] = None
    queued: bool = False
    last_reasons: Optional[List[str]] = None


def _memory(ctx: Ctx, state: dict, policy: dict, profile: str, now: float):
    if policy["profiles"][profile].get("heavy") is not True:
        return slot_memory.ADMIT, []
    if slot_memory.needs_own_sample(state, now):
        slot_memory.record_sample(state, slot_memory.take_sample(ctx.sample_runner, now), now)
    return slot_memory.memory_verdict(state, policy, profile, now)


def _parent(ctx: Ctx, state: dict) -> Optional[str]:
    """The caller's session names the parent (besluit 6); ``VNX_SLOT_LEASE`` is only a hint."""
    try:
        mine = str(os.getsid(0))
    except OSError:
        mine = None
    by_session = [k for k, h in state["holders"].items()
                  if h.get("owner_kind") in ("run", "runner") and mine and h.get("session_id") == mine]
    if len(by_session) == 1:
        return by_session[0]
    hint = ctx.env.get(PARENT_ENV) or None
    return hint if hint in state["holders"] else None


def _new_request(ctx: Ctx, state: dict, policy: dict, profile: str, project_id: str, owner: dict, now: float) -> dict:
    parent = _parent(ctx, state)
    rec = {"request_id": f"r-{uuid.uuid4().hex}", "lease_id": f"L-{uuid.uuid4().hex}", "requested_at": now,
           "kind": "nested" if parent else "new", "parent_lease": parent, "profile": profile,
           "project_id": project_id, "uid": os.getuid(), "owner": owner, "bypassed_n": 0, "reasons": []}
    prof = policy["profiles"].get(profile)
    if not isinstance(prof, dict):
        _refuse(ctx, state, rec, "policy_invalid", detail=f"unknown profile {profile!r}")
    rec.update(slots=prof["slots"], max_slots=prof["max_slots"], deadline_at=now + prof["max_wait_s"])
    ctx.log(_event(state, "requested", now, rec, reasons=[]))
    return rec


def _grant_nested(ctx: Ctx, state: dict, policy: dict, rec: dict, now: float) -> dict:
    try:
        plan = nested_plan(state, policy, rec["parent_lease"], rec["profile"], rec["slots"])["plan"]
    except StateCorrupt:
        raise
    except AdmissionInvalid as exc:
        _refuse(ctx, state, rec, exc.reason)
    grown = _grown(state["holders"], plan)
    for key, delta in plan:
        ctx.log(_event(state, "topped_up", now, state["holders"][key], slots=delta, request_id=rec["request_id"],
                       reasons=[], slots_granted=grown[key]["slots_granted"],
                       grandfathered=grown[key].get("grandfathered") is True))
    state["holders"] = grown
    child = _make_lease(state, policy, rec, now, rec["parent_lease"])
    state["holders"][child["lease_id"]] = child
    ctx.log(_event(state, "nested", now, child, request_id=rec["request_id"],
                   wait_s=now - rec["requested_at"], reasons=[]))
    return child


def _observe_grant(ctx: Ctx, state: dict, policy: dict, rec: dict, now: float) -> dict:
    """Admit at once; the shadow queue logs what enforce would have done (``would_wait``)."""
    shadow_rec = {k: rec[k] for k in ("request_id", "lease_id", "profile", "project_id", "uid", "slots",
                                      "max_slots", "owner", "requested_at", "deadline_at", "bypassed_n")}
    if rec["parent_lease"] in state["shadow"]["holders"]:
        shadow_rec.update(kind="nested", parent_lease=rec["parent_lease"])
    memory = {rec["request_id"]: _memory(ctx, state, policy, rec["profile"], now)}
    try:
        result = slot_shadow.shadow_admit(state, policy, shadow_rec, {}, memory, now)
    except StateCorrupt:
        raise
    except AdmissionInvalid as exc:
        _refuse(ctx, state, rec, exc.reason)
    state["shadow"] = result["state"]["shadow"]
    record = {k: v for k, v in result["record"].items() if k not in ("event", "request_id")}
    ctx.log(_event(state, "would_wait", now, rec, **record))
    if rec["parent_lease"] is not None:
        return _grant_nested(ctx, state, policy, rec, now)
    lease = _make_lease(state, policy, rec, now, None)
    state["holders"][lease["lease_id"]] = lease
    ctx.log(_event(state, "admitted", now, lease, request_id=rec["request_id"], wait_s=now - rec["requested_at"],
                   reasons=[], slots_granted=lease["slots_granted"], max_slots=lease["max_slots"], bypassed_n=0))
    return lease


def _enforce_step(ctx: Ctx, state: dict, policy: dict, req: _Pending, now: float, table):
    rec, rid = req.rec, req.rec["request_id"]
    if not req.queued:
        if len(state["queue"]) >= policy["max_queue"]:
            _refuse(ctx, state, rec, "queue_full", EXIT_TEMPFAIL)
        state["queue"].append(rec)
        req.queued = True
    elif not any(r["request_id"] == rid for r in state["queue"]):
        if rec["lease_id"] in state["holders"]:
            return state["holders"][rec["lease_id"]]   # another client's round admitted it
        _refuse(ctx, state, rec, "state_corrupt", detail="request left the queue without a lease")
    if state["mode"] == "enforce_pending":
        reasons, mine = ["enforce_pending"], rec
    else:
        heavy = {r["profile"] for r in state["queue"]
                 if policy["profiles"].get(r["profile"], {}).get("heavy") is True}
        verdicts = {p: _memory(ctx, state, policy, p, now) for p in heavy}
        memory = {r["request_id"]: verdicts[r["profile"]] for r in state["queue"] if r["profile"] in verdicts}
        live = {k: liveness.lease_liveness(h, table, state["holders"])[0] for k, h in state["holders"].items()}
        result = decide_round(state, policy, live, memory, now)
        state.clear()
        state.update(result["state"])
        for ev in result["events"]:
            if ev["event"] != "waited":
                ctx.log(ev)
        mine = next((r for r in state["queue"] if r["request_id"] == rid), None)
        if mine is None and rec["lease_id"] in state["holders"]:
            return state["holders"][rec["lease_id"]]
        refused = [e for e in result["events"] if e["event"] == "refused" and e["request_id"] == rid]
        if refused or mine is None:
            raise Refused(refused[-1]["reasons"][0] if refused else "state_corrupt")
        reasons = list(mine["reasons"])
    if now >= rec["deadline_at"]:
        state["queue"] = [r for r in state["queue"] if r["request_id"] != rid]
        ctx.log(_event(state, "refused", now, mine, reasons=["wait_timeout", *reasons], exit_code=EXIT_TEMPFAIL,
                       wait_s=now - rec["requested_at"]))
        return Refused("wait_timeout", ", ".join(reasons), EXIT_TEMPFAIL)   # raised after the commit
    if reasons != req.last_reasons:
        req.last_reasons = reasons
        ctx.log(_event(state, "waited", now, mine, wait_s=now - rec["requested_at"], reasons=reasons))
    return _WAIT


def _step(ctx: Ctx, state: dict, policy: dict, req: _Pending, now: float, table):
    mode = state["mode"]
    if mode in ("off", "observe") and req.queued:
        state["queue"] = [r for r in state["queue"] if r["request_id"] != req.rec["request_id"]]
        req.queued = False
    if mode == "off":
        return _OFF
    if mode == "observe":
        return _observe_grant(ctx, state, policy, req.rec, now)
    return _enforce_step(ctx, state, policy, req, now, table)


def _admit(ctx: Ctx, profile: str, project_id: str, owner: dict):
    """Poll under the control lock until admitted (lease dict), refused (raises) or off (None)."""
    req = _Pending()
    while True:
        policy = ctx.store.read_policy()
        with ctx.store.transaction() as state:
            now = ctx.clock()
            _sync_mode(ctx, state, policy, now, project_id)
            table = _reap(ctx, state, now)
            if req.rec is None:
                req.rec = _new_request(ctx, state, policy, profile, project_id, owner, now)
            result = _step(ctx, state, policy, req, now, table)
        if isinstance(result, Refused):
            raise result
        if result is _OFF:
            return None
        if result is not _WAIT:
            return result
        ctx.sleep(ctx.poll)



def _parse(argv: List[str], *specs) -> Optional[argparse.Namespace]:
    parser = argparse.ArgumentParser(prog="vnx slot", add_help=False, usage=USAGE)
    for args, kwargs in specs:
        parser.add_argument(*args, **kwargs)
    try:
        ns = parser.parse_args(argv)
    except SystemExit:
        return None
    project_id = getattr(ns, "project_id", None)
    if project_id is not None and not project_id.strip():
        return None   # ADR-007: an explicit project id, never an empty one
    return ns


PROJECT = (("--project-id",), {"required": True})


def _row(table, pid: int):
    return next((r for r in table or [] if r["pid"] == pid), None)


def cmd_acquire(ctx: Ctx, argv: List[str]) -> int:
    ns = _parse(argv, (("profile",), {}), PROJECT, (("--owner-pid",), {"type": int, "default": None}))
    if ns is None:
        return EXIT_USAGE
    src = {"project_id": ns.project_id, "profile": ns.profile, "uid": os.getuid()}
    try:
        pid = liveness.validate_acquire_owner(ns.owner_pid)
    except liveness.OwnerPidRequired:
        _refuse_now(ctx, src, "owner_pid_required")
    row = _row(liveness.snapshot(ctx.ps), pid)
    if row is None:
        _refuse_now(ctx, src, "owner_mismatch", detail=f"owner pid {pid} not found alive")
    owner = {"owner_kind": "acquire", "owner_pid": pid, "owner_start_time": row["start_time"],
             "session_id": row["session"], "supervisor_pid": None, "supervisor_start_time": None,
             "runner": None, "descendants": []}
    lease = _admit(ctx, ns.profile, ns.project_id, owner)
    ctx.out("off" if lease is None else f"{lease['lease_id']}:{lease['token']}")
    return 0


def _drop_unstarted(ctx: Ctx, lease: dict) -> None:
    with ctx.store.transaction() as state:
        held = state["holders"].get(lease["lease_id"])
        if held is not None and held["token"] == lease["token"] and held.get("session_id") is None:
            _remove(ctx, state, lease["lease_id"], ctx.clock(), "released")
            _sync_shadow(state, ctx.clock())


def cmd_run(ctx: Ctx, argv: List[str]) -> int:
    if "--" not in argv:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    cut = argv.index("--")
    ns, command = _parse(argv[:cut], (("profile",), {}), PROJECT), argv[cut + 1:]
    if ns is None or not command:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    me = _row(liveness.snapshot(ctx.ps), os.getpid())
    owner = {"owner_kind": "run", "session_id": None, "owner_pid": None, "owner_start_time": None,
             "supervisor_pid": os.getpid(), "supervisor_start_time": me["start_time"] if me else None,
             "runner": None, "descendants": []}
    lease = _admit(ctx, ns.profile, ns.project_id, owner)
    if lease is None:   # the operator chose off: no admission, no bookkeeping
        return ctx.spawn(command).wait()
    env = {k: v for k, v in ctx.env.items() if k != PARENT_ENV}
    if lease["parent_lease"] is not None:
        env[PARENT_ENV] = lease["parent_lease"]
    booked = {"done": False}

    def log(ev: Dict[str, Any]) -> None:   # admission already logged admitted/nested with its wait
        if not booked["done"] and ev["event"] in ("admitted", "nested") and ev["lease_id"] == lease["lease_id"]:
            booked["done"] = True
            return
        ctx.log(ev)

    sup = Supervisor(ctx.store, log, clock=ctx.clock, sleep=ctx.sleep, spawn=ctx.spawn, ps=ctx.ps,
                     sample_runner=ctx.sample_runner, env=env, interval=ctx.interval)
    try:
        return sup.run(lease["lease_id"], lease["token"], command)
    except StartRefused as exc:
        _drop_unstarted(ctx, lease)
        print(f"vnx slot run: refused: {exc.reason}", file=sys.stderr)
        return exc.exit_code
    except OSError as exc:   # the command could not be started: no workload, so no lease
        _drop_unstarted(ctx, lease)
        print(f"vnx slot run: command not started: {exc}", file=sys.stderr)
        return EXIT_INVALID


def cmd_release(ctx: Ctx, argv: List[str]) -> int:
    ns = _parse(argv, (("ref",), {}), (("--project-id",), {"default": None}))
    if ns is None:
        return EXIT_USAGE
    lease_id, sep, raw = ns.ref.rpartition(":")
    token = int(raw) if raw.isdigit() else None
    with ctx.store.transaction() as state:
        now, lease = ctx.clock(), state["holders"].get(lease_id)
        src = lease or {"lease_id": lease_id or None, "project_id": ns.project_id, "uid": os.getuid()}
        if not sep or not check_token(state, lease_id, token)["ok"]:
            _refuse(ctx, state, src, "stale_token")
        try:
            session = str(os.getsid(0))
        except OSError:
            session = None
        if os.getuid() != lease.get("owner_uid") and (session is None or session != lease.get("session_id")):
            _refuse(ctx, state, src, "owner_mismatch")
        table = liveness.snapshot(ctx.ps)
        if _tree_verdict(state, lease_id, table) == liveness.GONE:
            _remove(ctx, state, lease_id, now, "released")
            _sync_shadow(state, now)
            outcome = "released"
        else:
            if liveness.supervisor_gone_tree_alive(lease, table, state["holders"]):
                ctx.log(_event(state, "lease_held", now, lease, reasons=["owner_gone_tree_alive"]))
            lease["state"], outcome = "releasing", "releasing"
    ctx.out(outcome)
    return 0


COMMANDS = {"acquire": cmd_acquire, "run": cmd_run, "release": cmd_release}


def main(argv: list, **seams: Any) -> int:
    """``seams`` (tests): ``store``, ``log`` and any ``Ctx`` field; the default store is the host store."""
    store = seams.pop("store", None) or SlotStore()
    if argv[:1] == ["doctor"]:
        code, reasons = store.doctor()
        for reason in reasons:
            print(reason)
        if code == 0:
            print("slot doctor: ok")
        return code
    command = COMMANDS.get(argv[0]) if argv else None
    if command is None:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    ctx = Ctx(store=store, log=seams.pop("log", None) or SlotLog(store.store_dir).write, **seams)
    try:
        return command(ctx, argv[1:])
    except SlotError as exc:
        print(f"vnx slot {argv[0]}: {exc}", file=sys.stderr)
        return exc.exit_code
    except AdmissionInvalid as exc:
        print(f"vnx slot {argv[0]}: {exc.reason}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
