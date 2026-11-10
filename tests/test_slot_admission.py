"""RS-3: slot admission is pure, safe and ordered (ADR-062 besluit 1, 2, 8)."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from slot import slot_admission as sa  # noqa: E402


def policy(total=8, max_bypass=3, **over):
    prof = lambda m, kids=(), heavy=False: {"slots": 1, "max_slots": m, "children": list(kids),
                                            "heavy": heavy, "max_wait_s": 60}
    base = {"total": total, "reserved": 0, "runner_reserve": 0, "max_bypass": max_bypass,
            "profiles": {"runner": prof(8, ["seo-gate", "mid"]), "mid": prof(8, ["seo-gate"]),
                         "seo-gate": prof(8, heavy=True), "big": prof(8), "small": prof(2), "hsmall": prof(2, heavy=True)}}
    return {**base, **over}


def lease(lease_id, granted, max_slots, parent=None, at=0, profile="runner", token=1):
    return {"lease_id": lease_id, "token": token, "profile": profile, "project_id": "p",
            "slots_granted": granted, "max_slots": max_slots, "parent_lease": parent,
            "admitted_at": at, "owner_uid": 501, "state": "active", "grandfathered": False}


def req(rid, kind="new", slots=1, at=0, profile="big", **extra):
    return {"request_id": rid, "requested_at": at, "kind": kind, "lease_id": None,
            "parent_lease": None, "profile": profile, "project_id": "p", "uid": 501,
            "slots": slots, "bypassed_n": 0, "reasons": [], **extra}


def state(holders=(), queue=()):
    return {"holders": {h["lease_id"]: h for h in holders}, "queue": list(queue), "mode": "enforce",
            "fencing_counter": 10}


def run(st, pol=None, live=None, mem=None, now=100):
    return sa.decide_round(st, pol or policy(), live or {}, mem or {}, now)


def names(result):
    return [(e["event"], e["lease_id"] or e["request_id"]) for e in result["events"]]


def test_child_of_8_under_parent_of_1_tops_up_7_and_parent_keeps_its_1():
    st = state([lease("par", 1, 8)])
    plan = sa.nested_plan(st, policy(), "par", "seo-gate", 8)
    assert plan["mode"] == "top_up" and plan["shortfall"] == 7 and plan["plan"] == [("par", 7)]
    assert st["holders"]["par"]["slots_granted"] == 1


def test_top_up_above_max_slots_is_78():
    st = state([lease("a", 2, 4)])
    with pytest.raises(sa.AdmissionInvalid) as exc:
        sa.growth_plan(st, "a", 3)
    assert exc.value.reason == "over_profile_ceiling" and exc.value.exit_code == 78


def test_codex_example_safe_sequence_puts_b_before_a():
    st = state([lease("A", 1, 8, at=1), lease("B", 1, 7, at=2)])
    assert sa.is_safe(st, policy()) and sa.safe_sequence(st, policy()) == ["B", "A"]


def test_nested_lease_within_parent_budget_needs_no_top_up_and_keeps_free_slots():
    st = state([lease("par", 4, 8)])
    plan = sa.nested_plan(st, policy(), "par", "seo-gate", 3)
    assert plan == {"mode": "nested", "shortfall": 0, "plan": []}
    assert sa.free_slots(st, policy()) == 4


def test_child_profile_not_allowed_under_parent_is_78():
    st = state([lease("par", 4, 8)])
    with pytest.raises(sa.AdmissionInvalid) as exc:
        sa.nested_plan(st, policy(), "par", "small", 1)
    assert exc.value.reason == "profile_not_allowed_under_parent" and exc.value.exit_code == 78


def test_intermediate_parent_bounds_its_children():
    holders = [lease("root", 4, 8), lease("mid", 2, 3, parent="root", profile="mid")]
    ok = sa.growth_plan(state(holders), "mid", 1)
    assert ok == [("mid", 1)]
    with pytest.raises(sa.AdmissionInvalid):
        sa.growth_plan(state(holders), "mid", 2)
    low = sa.nested_plan(state([lease("root", 3, 8), lease("mid", 1, 3, parent="root", profile="mid")]),
                         policy(), "mid", "seo-gate", 3)
    assert low["plan"] == [("mid", 2)]
    tight = sa.nested_plan(state([lease("root", 2, 8), lease("mid", 1, 3, parent="root", profile="mid")]),
                           policy(), "mid", "seo-gate", 3)
    assert tight["plan"] == [("mid", 2), ("root", 1)]


def test_wrong_token_is_stale_token_78_and_counter_is_never_reused():
    st = state([lease("a", 1, 2, token=7)])
    assert sa.check_token(st, "a", 7) == {"ok": True, "reason": None, "exit_code": 0}
    for bad in (6, None, True, "7"):
        got = sa.check_token(st, "a", bad)
        assert got["reason"] == "stale_token" and got["exit_code"] == 78 and not got["ok"]
    assert sa.check_token(st, "gone", 7)["reason"] == "stale_token"


def test_unknown_liveness_never_frees_a_lease():
    st = state([lease("a", 2, 4), lease("b", 2, 4), lease("c", 2, 4)])
    got = sa.cleanup_decision(st, {"a": "unknown", "b": "alive"}, 100)
    assert got["reap"] == [] and got["events"] == []
    assert sorted(got["unknown"]) == ["a", "c"]
    assert sa.cleanup_decision(st, {"a": "garbage"}, 100)["reap"] == []


def test_parent_with_live_child_in_another_session_is_never_reaped():
    st = state([lease("par", 8, 8, at=40), lease("kid", 4, 4, parent="par")])
    got = sa.cleanup_decision(st, {"par": "gone", "kid": "alive"}, 100)
    assert got["reap"] == [] and got["held"] == ["par"]
    assert got["events"][0]["event"] == "lease_held"
    assert got["events"][0]["reasons"] == ["owner_gone_tree_alive"]
    done = sa.cleanup_decision(st, {"par": "gone", "kid": "gone"}, 100)
    assert done["reap"] == ["kid", "par"] and done["events"][1]["hold_s"] == 60
    assert sa.cleanup_decision(st, {"par": "gone", "kid": "unknown"}, 100)["reap"] == []


def test_releasing_lease_keeps_slots_while_alive():
    st = state([lease("a", 2, 4)])
    st["holders"]["a"]["state"] = "releasing"
    assert sa.cleanup_decision(st, {"a": "alive"}, 100)["reap"] == []
    assert sa.cleanup_decision(st, {"a": "gone"}, 100)["reap"] == ["a"]


def test_free_slots_counts_roots_only_and_reserves():
    st = state([lease("par", 8, 8), lease("kid", 8, 8, parent="par")])
    assert sa.free_slots(st, policy(total=10, reserved=1, runner_reserve=1)) == 0
    with pytest.raises(sa.AdmissionInvalid):
        sa.free_slots(state([lease("x", True, 2)]), policy())


def test_double_admission_pool_8_two_requests_of_8_one_waits():
    st = state(queue=[req("a", slots=8, at=1), req("b", slots=8, at=2)])
    res = run(st)
    assert list(res["state"]["holders"]) == ["a"]
    assert [r["request_id"] for r in res["state"]["queue"]] == ["b"]
    assert res["state"]["queue"][0]["reasons"] == ["budget_full"]
    assert ("admitted", "a") in names(res) and ("waited", "b") in names(res)
    assert res["state"]["holders"]["a"]["token"] == 11 and res["state"]["fencing_counter"] == 11


def test_codex_example_b_gets_its_6_first_and_a_its_7_after_b_ends():
    holders = [lease("A", 1, 8, at=1), lease("B", 1, 7, at=2)]
    queue = [req("ta", "top_up", slots=7, lease_id="A", token=1, at=1),
             req("tb", "top_up", slots=6, lease_id="B", token=1, at=2)]
    st = state(holders, queue)
    assert sa.is_safe(st, policy()) and sa.safe_sequence(st, policy()) == ["B", "A"]
    res = run(st)
    assert res["state"]["holders"]["B"]["slots_granted"] == 7
    assert res["state"]["holders"]["A"]["slots_granted"] == 1
    assert res["state"]["queue"][0]["request_id"] == "ta"
    assert res["state"]["queue"][0]["reasons"] == ["budget_full"]
    after = copy.deepcopy(res["state"])
    del after["holders"]["B"]
    final = run(after)
    assert final["state"]["holders"]["A"]["slots_granted"] == 8 and final["state"]["queue"] == []


def test_unsafe_top_up_waits_with_unsafe_state():
    st = state([lease("A", 1, 8, at=1), lease("B", 1, 7, at=2)],
               [req("ta", "top_up", slots=6, lease_id="A", token=1)])
    res = run(st)
    assert res["state"]["holders"]["A"]["slots_granted"] == 1
    assert res["state"]["queue"][0]["reasons"] == ["unsafe_state"]


def test_new_request_never_passes_an_earlier_one():
    st = state([lease("h", 6, 6)], [req("big", slots=4, at=1), req("tiny", slots=1, at=2)])
    res = run(st)
    assert list(res["state"]["holders"]) == ["h"]
    by_id = {r["request_id"]: r["reasons"] for r in res["state"]["queue"]}
    assert by_id == {"big": ["budget_full"], "tiny": ["queue_ahead"]}


def test_already_unsafe_state_admits_nothing_new():
    st = state([lease("A", 4, 8, at=1), lease("B", 4, 8, at=2)], [req("n", slots=1)])
    st["holders"]["A"]["slots_granted"] = 3
    assert not sa.is_safe(st, policy(total=8))
    res = run(st, policy(total=9))
    assert "n" not in res["state"]["holders"] and res["state"]["queue"][0]["reasons"] == ["unsafe_state"]


def test_bypassed_n_counts_and_max_bypass_stops_a_top_up():
    held = {"front": ("wait", ["memory_guard"])}
    st = state([lease("A", 2, 8, at=1)], [req("front", slots=2, at=1, profile="hsmall"),
                                          req("t1", "top_up", slots=1, lease_id="A", token=1)])
    first = run(st, policy(max_bypass=1), mem=held)
    assert first["state"]["holders"]["A"]["slots_granted"] == 3
    assert [r["bypassed_n"] for r in first["state"]["queue"]] == [1]
    again = copy.deepcopy(first["state"])
    again["queue"].append(req("t2", "top_up", slots=4, lease_id="A", token=1))
    second = run(again, policy(max_bypass=1), mem=held)
    assert second["state"]["holders"]["A"]["slots_granted"] == 3
    waiting = {r["request_id"]: r for r in second["state"]["queue"]}
    assert waiting["t2"]["reasons"] == ["queue_ahead"] and waiting["front"]["bypassed_n"] == 1
    assert any(e["event"] == "waited" and e["bypassed_n"] == 1 for e in second["events"])
    free_run = run(again, policy(max_bypass=5), mem=held)
    assert free_run["state"]["holders"]["A"]["slots_granted"] == 7
    assert [r["bypassed_n"] for r in free_run["state"]["queue"]] == [2]


def test_heavy_request_waits_without_a_positive_memory_verdict():
    for mem in ({}, {"h": ("wait", ["memory_guard"])}, {"h": ("maybe", [])}):
        res = run(state(queue=[req("h", profile="seo-gate")]), mem=mem)
        assert "h" not in res["state"]["holders"] and res["state"]["queue"][0]["reasons"]
    ok = run(state(queue=[req("h", profile="seo-gate")]), mem={"h": ("admit", [])})
    assert "h" in ok["state"]["holders"]


def test_top_up_for_owner_positively_gone_waits_and_input_is_untouched():
    st = state([lease("a", 1, 4)], [req("t", "top_up", slots=1, lease_id="a", token=1)])
    snapshot = copy.deepcopy(st)
    res = run(st, live={"a": "gone"})
    assert res["state"]["queue"][0]["reasons"] == ["owner_gone_tree_alive"]
    assert st == snapshot


def test_unknown_queue_kind_and_oversized_new_request_are_refused():
    res = run(state(queue=[req("x", "weird"), req("y", slots=9)]))
    assert [(e["event"], e["reasons"]) for e in res["events"]] == [
        ("refused", ["state_corrupt"]), ("refused", ["over_profile_ceiling"])]
    assert res["state"]["queue"] == []


def test_bypassed_n_never_exceeds_max_bypass_over_a_round_sequence():
    held = {"front": ("wait", ["memory_guard"])}
    pol = policy(max_bypass=2)
    st = state([lease("A", 1, 8, at=1)], [req("front", slots=2, at=1, profile="hsmall")])
    for number in range(6):
        st["queue"].append(req(f"t{number}", "top_up", slots=1, lease_id="A", token=1, at=2 + number))
        st = run(st, pol, mem=held)["state"]
        front = next(r for r in st["queue"] if r["request_id"] == "front")
        assert front["bypassed_n"] <= 2
    assert front["bypassed_n"] == 2


def test_reaped_and_lease_held_events_carry_the_granted_slots():
    st = state([lease("par", 3, 8), lease("kid", 2, 4, parent="par")])
    res = sa.cleanup_decision(st, {"par": "gone", "kid": "gone"}, now=50)
    by_event = {e["lease_id"]: e for e in res["events"] if e["event"] == "reaped"}
    assert by_event["kid"]["slots"] == 2 and by_event["par"]["slots"] == 3
    held = sa.cleanup_decision(st, {"par": "gone", "kid": "alive"}, now=50)
    lease_held = [e for e in held["events"] if e["event"] == "lease_held"]
    assert lease_held[0]["slots"] == 3 and lease_held[0]["slots"] is not None


def test_corrupt_sibling_slots_granted_raises_instead_of_skewing_the_delta():
    st = state([lease("par", 1, 8), lease("kid", True, 4, parent="par")])
    with pytest.raises(sa.AdmissionInvalid) as nested:
        sa.nested_plan(st, policy(), "par", "seo-gate", 2)
    assert nested.value.reason == "state_corrupt" and nested.value.exit_code == 78
    st["holders"]["kid"]["slots_granted"] = 1
    st["holders"]["sib"] = lease("sib", True, 4, parent="par")
    with pytest.raises(sa.AdmissionInvalid):
        sa.growth_plan(st, "kid", 1)
