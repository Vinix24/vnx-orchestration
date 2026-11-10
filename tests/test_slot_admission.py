"""RS-3: slot admission is pure, safe and ordered (ADR-062 besluit 1, 2, 8)."""

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


def state(holders=(), queue=()):
    return {"holders": {h["lease_id"]: h for h in holders}, "queue": list(queue), "mode": "enforce",
            "fencing_counter": 10}


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
