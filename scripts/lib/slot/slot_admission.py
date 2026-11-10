"""Slot admission building blocks: free capacity, the safe sequence, top-ups, nested leases, fencing (RS-3).

Pure over the ADR-062 ``state`` and ``policy`` dicts, no IO, no clock (``now`` is passed in).
The caller (RS-2) holds the store transaction and writes the returned ``events`` to the
slotlog. Nothing here mutates its input. A missing key raises ``KeyError`` (RS-2
validates); a value that cannot be right raises ``AdmissionInvalid`` (exit 78). An unknown
liveness is never evidence to free capacity. Split 2 of RS-3 adds the round decision, which
orders top-ups and new requests on top of these blocks."""

from __future__ import annotations

import copy

EXIT_INVALID = 78
ALIVE, GONE, UNKNOWN = "alive", "gone", "unknown"
_COPY = ("project_id", "request_id", "lease_id", "parent_lease", "profile", "slots", "bypassed_n")
_FIELDS = ("ts", "event", "project_id", "uid", "mode", "request_id", "lease_id", "parent_lease",
           "profile", "slots", "wait_s", "hold_s", "swap_used_mb", "compressor_gb", "reasons",
           "bypassed_n")


class AdmissionInvalid(ValueError):
    """A request or state that can never be granted; ``exit_code`` is 78."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
        self.exit_code = EXIT_INVALID


def _int(value, reason: str = "policy_invalid") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdmissionInvalid(reason)
    return value


def _roots(state: dict) -> list[dict]:
    return [h for h in state["holders"].values() if h["parent_lease"] is None]


def _children(holders: dict, lease_id: str) -> list[dict]:
    return [h for h in holders.values() if h["parent_lease"] == lease_id]


def _children_granted(holders: dict, lease_id: str) -> int:
    return sum(_int(c["slots_granted"], "state_corrupt") for c in _children(holders, lease_id))


def _profile(policy: dict, name: str) -> dict:
    profile = policy["profiles"].get(name)
    if not isinstance(profile, dict):
        raise AdmissionInvalid("policy_invalid")
    return profile


def free_slots(state: dict, policy: dict) -> int:
    """``total - reserved - runner_reserve`` minus the granted slots of root leases only."""
    pool = _int(policy["total"]) - _int(policy["reserved"]) - _int(policy["runner_reserve"])
    return pool - sum(_int(h["slots_granted"], "state_corrupt") for h in _roots(state))


def _need(holder: dict) -> int:
    return _int(holder["max_slots"], "state_corrupt") - _int(holder["slots_granted"], "state_corrupt")


def safe_sequence(state: dict, policy: dict) -> list[str] | None:
    """Lease ids in an order where each root holder can get its remaining need, or None.

    Sorted on remaining need, ties on ``admitted_at`` then ``lease_id``; a holder that
    finishes returns everything it was granted. One resource, so the smallest need goes first."""
    available = free_slots(state, policy)
    order = []
    for holder in sorted(_roots(state), key=lambda h: (_need(h), h["admitted_at"], h["lease_id"])):
        if _need(holder) > available:
            return None
        available += holder["slots_granted"]
        order.append(holder["lease_id"])
    return order


def is_safe(state: dict, policy: dict) -> bool:
    return safe_sequence(state, policy) is not None


def check_token(state: dict, lease_id: str, token) -> dict:
    """Fencing for release, heartbeat and top-up: only the lease's current token passes."""
    lease = state["holders"].get(lease_id)
    current = None if lease is None else lease["token"]
    ok = (not isinstance(token, bool) and isinstance(token, int) and current is not None
          and not isinstance(current, bool) and token == current)
    return {"ok": ok, "reason": None if ok else "stale_token", "exit_code": 0 if ok else EXIT_INVALID}


def growth_plan(state: dict, lease_id: str, delta: int) -> list[tuple[str, int]]:
    """``(lease_id, delta)`` from the lease up to the root: what must grow so ``delta`` fits.

    A lease never grows above its ``max_slots`` (``over_profile_ceiling``); a parent grows
    only by what its children's grants no longer fit in its own."""
    holders, plan, seen = state["holders"], [], set()
    while delta > 0:
        if lease_id in seen or lease_id not in holders:
            raise AdmissionInvalid("state_corrupt")
        seen.add(lease_id)
        lease = holders[lease_id]
        if _int(lease["slots_granted"], "state_corrupt") + delta > _int(lease["max_slots"], "state_corrupt"):
            raise AdmissionInvalid("over_profile_ceiling")
        plan.append((lease_id, delta))
        parent = lease["parent_lease"]
        if parent is None:
            break
        used = _children_granted(holders, parent)
        delta, lease_id = max(0, used + delta - holders[parent]["slots_granted"]), parent
    return plan


def nested_plan(state: dict, policy: dict, parent_lease: str, profile: str, slots: int) -> dict:
    """A child of ``slots`` under ``parent_lease``: a deellease, or a top-up of the shortfall."""
    parent = state["holders"].get(parent_lease)
    if parent is None:
        raise AdmissionInvalid("stale_token")
    if profile not in _profile(policy, parent["profile"])["children"]:
        raise AdmissionInvalid("profile_not_allowed_under_parent")
    if _int(slots, "over_profile_ceiling") > _int(_profile(policy, profile)["max_slots"]):
        raise AdmissionInvalid("over_profile_ceiling")
    used = _children_granted(state["holders"], parent_lease)
    shortfall = max(0, used + slots - parent["slots_granted"])
    return {"mode": "top_up" if shortfall else "nested", "shortfall": shortfall,
            "plan": growth_plan(state, parent_lease, shortfall)}


def _event(state: dict, name: str, now: float, src: dict, **extra) -> dict:
    ev = dict.fromkeys(_FIELDS)
    ev.update({k: src[k] for k in _COPY if k in src})
    if ev["slots"] is None and "slots_granted" in src:
        ev["slots"] = src["slots_granted"]  # a lease record stores its capacity as slots_granted
    ev["uid"] = src.get("uid", src.get("owner_uid"))
    ev.update(ts=now, event=name, mode=state["mode"], **extra)
    return ev


def _grown(holders: dict, plan: list[tuple[str, int]]) -> dict:
    out = dict(holders)
    for lease_id, delta in plan:
        out[lease_id] = {**out[lease_id], "slots_granted": out[lease_id]["slots_granted"] + delta}
    return out


def _host_need(holders: dict, plan: list[tuple[str, int]]) -> int:
    return sum(d for lease_id, d in plan if holders[lease_id]["parent_lease"] is None)


def _memory_reasons(policy: dict, rec: dict, memory: dict) -> list[str]:
    """Heavy work needs a positive ``admit``; a missing or unknown verdict waits."""
    if _profile(policy, rec["profile"]).get("heavy") is not True:
        return []
    verdict = memory.get(rec["request_id"])
    if verdict is None:
        return ["memory_metric_unavailable"]
    return [] if verdict[0] == "admit" else list(verdict[1]) or ["memory_guard"]


def _lease_id(rec: dict) -> str:
    """The id a queued record is inserted under (``lease_id``, else ``request_id``): a non-empty string, else ``state_corrupt``."""
    raw = rec.get("lease_id")
    lease_id = rec.get("request_id") if raw is None else raw
    if not isinstance(lease_id, str) or not lease_id:
        raise AdmissionInvalid("state_corrupt")
    return lease_id


def _claim_lease_id(work: dict, rec: dict) -> None:
    """Refuse (``state_corrupt``) a record whose id is invalid or already a key in ``holders``."""
    if _lease_id(rec) in work["holders"]:
        raise AdmissionInvalid("state_corrupt")


def _make_lease(work: dict, policy: dict, rec: dict, now: float, parent: str | None) -> dict:
    work["fencing_counter"] = _int(work["fencing_counter"], "state_corrupt") + 1
    lease = dict(rec.get("owner") or {})
    lease.update(
        lease_id=_lease_id(rec),
        token=work["fencing_counter"], profile=rec["profile"], project_id=rec["project_id"],
        slots_granted=rec["slots"], max_slots=_profile(policy, rec["profile"])["max_slots"],
        owner_uid=rec["uid"], parent_lease=parent, state="active", grandfathered=False,
        admitted_at=now, heartbeat_at=now)
    lease.setdefault("owner_kind", "run")
    lease.setdefault("descendants", [])
    return lease


def _waiting(work: dict, rec: dict, reasons: list[str], now: float, out: list) -> None:
    rec["reasons"] = reasons
    out.append(_event(work, "waited", now, rec, wait_s=now - rec["requested_at"], reasons=reasons))


def _refuse(work: dict, rec: dict, reason: str, now: float, out: list) -> None:
    out.append(_event(work, "refused", now, rec, exit_code=EXIT_INVALID, reasons=[reason]))


def _front_harmed(work: dict, policy: dict, grown: dict) -> bool:
    """True when the front new request is admissible now but would not be after ``grown``."""
    news = [r for r in work["queue"] if r["kind"] == "new"]
    if not news:
        return False
    front = min(news, key=lambda r: r["requested_at"])
    try:
        return _can_admit(work, policy, front, work["holders"]) and not _can_admit(work, policy, front, grown)
    except AdmissionInvalid:
        return False  # unevaluable front is not harmed; _admit_new refuses it with its own reason


def _can_admit(work: dict, policy: dict, rec: dict, holders: dict) -> bool:
    stub = {"lease_id": "\0candidate", "slots_granted": (slots := _int(rec["slots"], "over_profile_ceiling")), "parent_lease": None,
            "max_slots": _profile(policy, rec["profile"])["max_slots"], "admitted_at": float("inf")}
    trial = {**work, "holders": {**holders, stub["lease_id"]: stub}}
    return free_slots({**work, "holders": holders}, policy) >= slots and is_safe(trial, policy)


def _top_up_step(work: dict, policy: dict, rec: dict, ctx: dict) -> bool:
    """Grant, refuse or hold one ``top_up`` or ``nested`` record. True once it leaves the queue."""
    now, out, liveness = ctx["now"], ctx["events"], ctx["liveness"]
    try:
        if rec["kind"] == "top_up":
            if not check_token(work, rec["lease_id"], rec.get("token"))["ok"]:
                raise AdmissionInvalid("stale_token")
            target, plan = rec["lease_id"], growth_plan(work, rec["lease_id"], _int(rec["slots"], "over_profile_ceiling"))
        else:
            _claim_lease_id(work, rec)
            target = rec["parent_lease"]
            plan = nested_plan(work, policy, target, rec["profile"], rec["slots"])["plan"]
    except AdmissionInvalid as exc:
        _refuse(work, rec, exc.reason, now, out)
        return True
    grown = _grown(work["holders"], plan)
    need = _host_need(work["holders"], plan)
    reasons = _memory_reasons(policy, rec, ctx["memory"])
    if liveness.get(target) == GONE:
        reasons.append("owner_gone_tree_alive")
    if need > free_slots(work, policy):
        reasons.append("budget_full")
    elif need and not is_safe({**work, "holders": grown}, policy):
        reasons.append("unsafe_state")
    elif need and _front_harmed(work, policy, grown) and _passed_limit(work, policy):
        reasons.append("queue_ahead")
    if reasons:
        _waiting(work, rec, reasons, now, out)
        return False
    for lease_id, delta in plan:
        out.append(_event(work, "topped_up", now, work["holders"][lease_id], slots=delta,
                          request_id=rec["request_id"],
                          slots_granted=grown[lease_id]["slots_granted"],
                          grandfathered=grown[lease_id]["grandfathered"] is True))
    work["holders"] = grown
    if rec["kind"] == "nested":
        child = _make_lease(work, policy, rec, now, target)
        work["holders"][child["lease_id"]] = child
        out.append(_event(work, "nested", now, child, request_id=rec["request_id"],
                          wait_s=now - rec["requested_at"], reasons=[]))
    if need:
        for waiting in work["queue"]:
            if waiting["kind"] == "new":
                # ADR-062: the counter may not go above max_bypass, so it saturates there.
                waiting["bypassed_n"] = min(_int(waiting["bypassed_n"], "state_corrupt") + 1,
                                            _int(policy["max_bypass"]))
    return True


def _passed_limit(work: dict, policy: dict) -> bool:
    """The front request was already passed ``max_bypass`` times: no further top-up goes first.

    ADR-062 caps the counter at ``max_bypass``, so the guard blocks at ``bypassed_n >= max_bypass``
    (T0 decision). It only matters when the front request is harmed (see ``_front_harmed``)."""
    news = [r for r in work["queue"] if r["kind"] == "new"]
    front = min(news, key=lambda r: r["requested_at"])
    return _int(front["bypassed_n"], "state_corrupt") >= _int(policy["max_bypass"])


def _top_up_order(work: dict, policy: dict, recs: list[dict]) -> list[dict]:
    """Top-up requests in safe-sequence order of the root they grow (queue order on ties)."""
    sequence = safe_sequence(work, policy) or []

    def position(rec: dict) -> int:
        lease_id = rec["lease_id"] if rec["kind"] == "top_up" else rec["parent_lease"]
        while lease_id in work["holders"] and work["holders"][lease_id]["parent_lease"] is not None:
            lease_id = work["holders"][lease_id]["parent_lease"]
        return sequence.index(lease_id) if lease_id in sequence else len(sequence)

    return sorted(recs, key=position)


def _admit_new(work: dict, policy: dict, ctx: dict) -> list[dict]:
    """Admit new requests strictly in arrival order: only the front one may go, if still safe."""
    now, out, gone = ctx["now"], ctx["events"], []
    news = sorted((r for r in work["queue"] if r["kind"] == "new"), key=lambda r: r["requested_at"])
    for index, rec in enumerate(news):
        try:
            _claim_lease_id(work, rec)
            ceiling = _int(_profile(policy, rec["profile"])["max_slots"])
            if _int(rec["slots"], "over_profile_ceiling") > ceiling:
                raise AdmissionInvalid("over_profile_ceiling")
        except AdmissionInvalid as exc:
            _refuse(work, rec, exc.reason, now, out)
            gone.append(rec)
            continue
        reasons = _memory_reasons(policy, rec, ctx["memory"])
        if free_slots(work, policy) < rec["slots"]:
            reasons.append("budget_full")
        elif not is_safe(work, policy) or not _can_admit(work, policy, rec, work["holders"]):
            reasons.append("unsafe_state")
        if reasons:
            _waiting(work, rec, reasons, now, out)
            for behind in news[index + 1:]:
                _waiting(work, behind, ["queue_ahead"], now, out)
            break
        lease = _make_lease(work, policy, rec, now, None)
        work["holders"][lease["lease_id"]] = lease
        out.append(_event(work, "admitted", now, lease, request_id=rec["request_id"],
                          wait_s=now - rec["requested_at"], reasons=[],
                          slots_granted=lease["slots_granted"], max_slots=lease["max_slots"],
                          bypassed_n=rec["bypassed_n"]))
        gone.append(rec)
    return gone


def decide_round(state: dict, policy: dict, liveness: dict, memory: dict, now: float) -> dict:
    """One round under the store lock: top-ups first (safe order), then the front new request.

    ``liveness`` maps lease_id to alive/gone/unknown (a top-up for a lease whose owner is positively
    gone waits with ``owner_gone_tree_alive``: a T0 decision, conservative; ``alive`` and ``unknown``
    do not block); ``memory`` maps request_id to the RS-5 ``(verdict, reasons)``.
    Returns ``{"state": new state, "events": slotlog events}``; the input is untouched."""
    work = copy.deepcopy(state)
    ctx = {"now": now, "events": [], "liveness": liveness, "memory": memory}
    leaving, kinds = [], {"new", "top_up", "nested"}
    for rec in [r for r in work["queue"] if r["kind"] not in kinds]:
        _refuse(work, rec, "state_corrupt", now, ctx["events"])
        leaving.append(rec)
    topups = [r for r in work["queue"] if r["kind"] in ("top_up", "nested")]
    for rec in _top_up_order(work, policy, topups):
        if _top_up_step(work, policy, rec, ctx):
            leaving.append(rec)
    leaving += _admit_new(work, policy, ctx)
    work["queue"] = [r for r in work["queue"] if not any(r is gone for gone in leaving)]
    return {"state": work, "events": ctx["events"]}


def cleanup_decision(state: dict, liveness: dict, now: float) -> dict:
    """Which leases may be freed: ``gone`` and every child lease freed. Children go first.

    ``now`` is required: the ``reaped`` event carries ``ts`` and ``hold_s`` (``now`` minus
    ``admitted_at``), which the caller cannot know.

    ``alive`` keeps a lease (also one in ``releasing``); ``unknown`` or a missing verdict
    keeps it and lists it under ``unknown``; ``gone`` with a child still held is
    ``lease_held`` with ``owner_gone_tree_alive``."""
    holders, freed, held, unknown, events = state["holders"], [], [], [], []
    memo: dict[str, bool] = {}

    def settle(lease_id: str) -> bool:
        if lease_id in memo:
            return memo[lease_id]
        memo[lease_id] = False
        children_freed = [settle(c["lease_id"]) for c in _children(holders, lease_id)]
        verdict = liveness.get(lease_id, UNKNOWN)
        lease = holders[lease_id]
        if verdict == GONE and all(children_freed):
            memo[lease_id] = True
            freed.append(lease_id)
            events.append(_event(state, "reaped", now, lease, hold_s=now - lease["admitted_at"], reasons=[]))
        elif verdict == GONE:
            held.append(lease_id)
            events.append(_event(state, "lease_held", now, lease, reasons=["owner_gone_tree_alive"]))
        elif verdict != ALIVE:
            unknown.append(lease_id)
        return memo[lease_id]

    for lease_id in holders:
        settle(lease_id)
    return {"reap": freed, "held": held, "unknown": unknown, "events": events}
