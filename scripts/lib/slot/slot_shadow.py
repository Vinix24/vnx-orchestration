"""The observe-mode shadow queue: what ``enforce`` would have done, never a real reservation (RS-7).

Pure over the ADR-062 ``state`` and ``policy`` dicts (decisions 5 and 8), no IO, no clock (``now`` is
passed in). ``shadow_admit`` runs one request through RS-3 ``decide_round`` on ``state["shadow"]``
(``holders`` and ``queue``) and never touches the real ``holders`` or ``queue``. The wait is an estimate
built from real hold times only: a shadow release records the hold it saw, the estimate takes the median
per profile, and without such a record it is ``None``, never a made-up 0."""

from __future__ import annotations

import copy
from statistics import median

from slot.slot_admission import AdmissionInvalid, decide_round, free_slots, is_safe
from slot.slot_memory import MEMORY_REASONS

ADMITTED_EVENTS = ("admitted", "topped_up", "nested")
HISTORY_KEEP = 50


def _history(shadow: dict) -> list[dict]:
    """The recorded shadow hold times: a missing list is empty history, so every estimate stays unknown."""
    return shadow.get("hold_history") or []


def _typical_hold(shadow: dict, profile: str) -> float | None:
    """Median real hold of ``profile`` from the shadow history; None when no valid hold is recorded."""
    holds = [h["hold_s"] for h in _history(shadow)
             if isinstance(h, dict) and h.get("profile") == profile and not isinstance(h.get("hold_s"), bool)
             and isinstance(h.get("hold_s"), (int, float)) and h["hold_s"] >= 0]
    return median(holds) if holds else None


def _estimate(shadow: dict, policy: dict, rec: dict, reasons: list[str], now: float) -> float | None:
    """Seconds until enough shadow holders have finished for ``rec`` to fit, or None when unknown.

    Only a new request without a memory reason has an estimate: memory recovery is not a hold time.
    Each root holder is expected to end at ``admitted_at`` plus the typical hold of its profile; one
    holder with no known typical hold makes the whole estimate unknown."""
    if rec["kind"] != "new" or MEMORY_REASONS & set(reasons):
        return None
    holders = shadow["holders"]
    ahead = sum(r["slots"] for r in shadow["queue"] if r["kind"] == "new" and r["requested_at"] < rec["requested_at"])
    ends = []
    for lease in holders.values():
        hold = _typical_hold(shadow, lease["profile"])
        if lease["parent_lease"] is None:
            if hold is None:
                return None
            ends.append((max(0.0, lease["admitted_at"] + hold - now), lease["lease_id"]))
    candidate = {"lease_id": "\0candidate", "slots_granted": rec["slots"], "parent_lease": None,
                 "max_slots": policy["profiles"][rec["profile"]]["max_slots"], "admitted_at": float("inf")}
    trial = {**shadow, "holders": dict(holders)}
    for remaining, lease_id in sorted(ends):
        trial["holders"].pop(lease_id)
        with_candidate = {**trial, "holders": {**trial["holders"], candidate["lease_id"]: candidate}}
        if free_slots(trial, policy) >= ahead + rec["slots"] and is_safe(with_candidate, policy):
            return remaining
    return None


def _request(request: dict, now: float) -> dict:
    rec = {"kind": "new", "lease_id": None, "parent_lease": None, "requested_at": now, "bypassed_n": 0,
           "reasons": []}
    rec.update(request)
    return rec


def shadow_admit(state: dict, policy: dict, request: dict, liveness: dict, memory: dict, now: float) -> dict:
    """Run ``request`` through the ``enforce`` rules on the shadow; return the new state and one record.

    ``memory`` and ``liveness`` are the ``decide_round`` inputs. Returns ``{"state": state with the
    updated shadow, "record": {...}}``; ``state`` is a new dict whose real ``holders`` and ``queue`` are
    the untouched originals. A request ``enforce`` would refuse (a defect of the request) raises
    ``AdmissionInvalid`` with that reason, and a request id already in the shadow is ``state_corrupt``."""
    shadow = state["shadow"]
    rec = _request(request, now)
    taken = [r["request_id"] for r in shadow["queue"]] + list(shadow["holders"])
    if rec["request_id"] in taken:
        raise AdmissionInvalid("state_corrupt")
    sim = {"mode": state["mode"], "fencing_counter": state["fencing_counter"],
           "holders": shadow["holders"], "queue": [*shadow["queue"], rec]}
    result = decide_round(sim, policy, liveness, memory, now)
    mine = [e for e in result["events"] if e["request_id"] == rec["request_id"]]
    refused = [e for e in mine if e["event"] == "refused"]
    if refused:
        raise AdmissionInvalid(refused[-1]["reasons"][0])
    admitted = any(e["event"] in ADMITTED_EVENTS for e in mine)
    updated = {**shadow, "holders": result["state"]["holders"], "queue": result["state"]["queue"]}
    waits = [e for e in mine if e["event"] == "waited"]
    reasons = [] if admitted or not waits else list(waits[-1]["reasons"])
    waiting = next((r for r in updated["queue"] if r["request_id"] == rec["request_id"]), None)
    if not admitted and waiting is None:
        raise AdmissionInvalid("state_corrupt")  # neither admitted, refused nor queued: the round lost it
    wait_s = 0 if admitted else _estimate(updated, policy, waiting, reasons, now)
    record = {"event": "would_wait", "request_id": rec["request_id"], "would_wait": not admitted,
              "reasons": reasons, "estimated_wait_s": wait_s, "estimate": True}
    return {"state": {**state, "shadow": updated}, "record": record}


def shadow_release(state: dict, lease_id: str, now: float) -> dict:
    """Remove ``lease_id`` and its child leases from the shadow only; record the real hold of the root.

    An unknown lease id is ``stale_token`` (``AdmissionInvalid``): a release is never silently a no-op.
    The real ``holders`` and ``queue`` are returned untouched."""
    shadow = copy.deepcopy(state["shadow"])
    lease = shadow["holders"].get(lease_id)
    if lease is None:
        raise AdmissionInvalid("stale_token")
    gone, grew = {lease_id}, True
    while grew:
        children = {i for i, h in shadow["holders"].items() if h["parent_lease"] in gone} - gone
        grew = bool(children)
        gone |= children
    for lease_key in gone:
        del shadow["holders"][lease_key]
    if lease["parent_lease"] is None:
        shadow["hold_history"] = [*_history(shadow), {"profile": lease["profile"], "hold_s": now - lease["admitted_at"]}][-HISTORY_KEEP:]
    return {**state, "shadow": shadow}
