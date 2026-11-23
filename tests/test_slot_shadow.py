"""RS-7: the observe shadow queue simulates enforce admission and never frees real capacity (ADR-062 besluit 5, 13)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from slot import slot_shadow as ss
from slot.slot_admission import AdmissionInvalid


def policy():
    prof = lambda m, heavy=False: {"slots": 1, "max_slots": m, "children": [], "heavy": heavy, "max_wait_s": 60}
    return {"total": 8, "reserved": 0, "runner_reserve": 0, "max_bypass": 3,
            "profiles": {"big": prof(8), "small": prof(2), "heavy": prof(2, heavy=True)}}


def lease(lease_id, granted, max_slots, at=0, profile="big", parent=None):
    return {"lease_id": lease_id, "token": 1, "profile": profile, "project_id": "p", "slots_granted": granted,
            "max_slots": max_slots, "parent_lease": parent, "admitted_at": at, "owner_uid": 501,
            "state": "active", "grandfathered": False}


def req(rid, slots=1, at=0, profile="big"):
    return {"request_id": rid, "requested_at": at, "kind": "new", "project_id": "p", "uid": 501,
            "slots": slots, "profile": profile}


def state(shadow_holders=(), shadow_queue=(), history=None):
    shadow = {"holders": {h["lease_id"]: h for h in shadow_holders}, "queue": list(shadow_queue)}
    if history is not None:
        shadow["hold_history"] = history
    return {"holders": {"real": lease("real", 2, 4)}, "queue": [req("real-q", at=1)], "mode": "observe",
            "fencing_counter": 10, "shadow": shadow}


def admit(st, request, mem=None, now=100):
    return ss.shadow_admit(st, policy(), request, {}, mem or {}, now)


def test_free_budget_is_admitted_with_no_wait():
    out = admit(state(), req("a", slots=2))
    assert out["record"] == {"event": "would_wait", "request_id": "a", "would_wait": False, "reasons": [],
                             "estimated_wait_s": 0, "estimate": True}
    assert "a" in out["state"]["shadow"]["holders"]


def test_full_budget_waits_with_budget_full():
    out = admit(state([lease("h", 8, 8)]), req("a"))
    assert out["record"]["would_wait"] is True and out["record"]["reasons"] == ["budget_full"]
    assert [r["request_id"] for r in out["state"]["shadow"]["queue"]] == ["a"]


def test_earlier_waiting_request_gives_queue_ahead():
    st = state([lease("h", 8, 8)], [req("first", at=50)])
    out = admit(st, req("second", at=60), now=100)
    assert out["record"]["would_wait"] is True and out["record"]["reasons"] == ["queue_ahead"]


def test_state_failing_the_safe_sequence_gives_unsafe_state():
    st = state([lease("a", 2, 8, at=1), lease("b", 2, 8, at=2)])  # 4 free, each needs 6 more: no safe order
    assert ss.is_safe(st["shadow"] | {"mode": "observe"}, policy()) is False
    out = admit(st, req("c"))
    assert out["record"]["would_wait"] is True and out["record"]["reasons"] == ["unsafe_state"]


def test_estimate_is_null_without_hold_time_and_always_labelled():
    out = admit(state([lease("h", 8, 8)]), req("a"))
    assert out["record"]["estimated_wait_s"] is None and out["record"]["estimate"] is True


def test_estimate_comes_from_real_shadow_hold_times():
    released = ss.shadow_release(state([lease("old", 8, 8, at=0)]), "old", 60)  # a real hold of 60 s
    st = {**released, "shadow": {**released["shadow"], "holders": {"h": lease("h", 8, 8, at=100)}}}
    out = admit(st, req("a"), now=130)
    assert out["record"]["reasons"] == ["budget_full"]
    assert out["record"]["estimated_wait_s"] == 30 and out["record"]["estimate"] is True


def test_memory_guard_shows_up_as_reason_and_has_no_estimate():
    out = admit(state(), req("a", slots=1, profile="heavy"), mem={"a": ("wait", ["memory_guard"])})
    assert out["record"]["would_wait"] is True and out["record"]["reasons"] == ["memory_guard"]
    assert out["record"]["estimated_wait_s"] is None


def test_missing_memory_verdict_for_heavy_is_metric_unavailable():
    out = admit(state(), req("a", profile="heavy"))
    assert out["record"]["reasons"] == ["memory_metric_unavailable"]


def test_real_holders_and_queue_are_byte_for_byte_unchanged():
    st = state([lease("h", 8, 8)])
    before = json.dumps({k: st[k] for k in ("holders", "queue", "fencing_counter", "mode")}, sort_keys=True)
    out = admit(st, req("a"))
    out2 = ss.shadow_release(out["state"], "h", 120)
    for result in (out["state"], out2):
        assert json.dumps({k: result[k] for k in ("holders", "queue", "fencing_counter", "mode")}, sort_keys=True) == before
    assert st["shadow"]["queue"] == [] and list(st["shadow"]["holders"]) == ["h"]


def test_shadow_release_frees_shadow_capacity_only():
    st = state([lease("h", 8, 8, at=10)])
    freed = ss.shadow_release(st, "h", 70)
    assert freed["shadow"]["holders"] == {} and "real" in freed["holders"]
    assert freed["shadow"]["hold_history"] == [{"profile": "big", "hold_s": 60}]
    assert admit(freed, req("a", slots=8))["record"]["would_wait"] is False


def test_shadow_release_removes_child_leases_too():
    st = state([lease("par", 4, 8), lease("kid", 2, 4, parent="par")])
    assert ss.shadow_release(st, "par", 5)["shadow"]["holders"] == {}


def test_release_of_unknown_lease_is_stale_token():
    with pytest.raises(AdmissionInvalid) as exc:
        ss.shadow_release(state(), "nope", 5)
    assert exc.value.reason == "stale_token"


def test_request_enforce_would_refuse_raises_with_its_reason():
    with pytest.raises(AdmissionInvalid) as exc:
        admit(state(), req("a", slots=9))
    assert exc.value.reason == "over_profile_ceiling" and exc.value.exit_code == 78


def test_duplicate_request_id_is_state_corrupt():
    with pytest.raises(AdmissionInvalid) as exc:
        admit(state([lease("a", 1, 2)]), req("a"))
    assert exc.value.reason == "state_corrupt"


@pytest.mark.parametrize("bad", [None, "x", True, -1, float("nan")])
def test_malformed_hold_history_never_yields_an_estimate(bad):
    st = state([lease("h", 8, 8)], history=[{"profile": "big", "hold_s": bad}])
    assert admit(st, req("a"))["record"]["estimated_wait_s"] is None


def test_missing_shadow_key_raises():
    st = state()
    del st["shadow"]
    with pytest.raises(KeyError):
        admit(st, req("a"))
