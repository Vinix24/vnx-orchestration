"""Slot admission building blocks: free capacity, the safe sequence, top-ups, nested leases, fencing (RS-3).

Pure over the ADR-062 ``state`` and ``policy`` dicts, no IO, no clock (``now`` is passed in).
The caller (RS-2) holds the store transaction and writes the returned ``events`` to the
slotlog. Nothing here mutates its input. A missing key raises ``KeyError`` (RS-2
validates); a value that cannot be right raises ``AdmissionInvalid`` (exit 78). An unknown
liveness is never evidence to free capacity. Split 2 of RS-3 adds the round decision, which
orders top-ups and new requests on top of these blocks."""

from __future__ import annotations

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
        used = sum(c["slots_granted"] for c in _children(holders, parent))
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
    used = sum(c["slots_granted"] for c in _children(state["holders"], parent_lease))
    shortfall = max(0, used + slots - parent["slots_granted"])
    return {"mode": "top_up" if shortfall else "nested", "shortfall": shortfall,
            "plan": growth_plan(state, parent_lease, shortfall)}


def _event(state: dict, name: str, now: float, src: dict, **extra) -> dict:
    ev = dict.fromkeys(_FIELDS)
    ev.update({k: src[k] for k in _COPY if k in src})
    ev["uid"] = src.get("uid", src.get("owner_uid"))
    ev.update(ts=now, event=name, mode=state["mode"], **extra)
    return ev


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
