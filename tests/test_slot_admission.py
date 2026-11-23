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


def req(rid, kind="new", slots=1, at=0, profile=None, **extra):
    """A queued record; a ``top_up`` carries no ``profile`` unless the test gives one."""
    own = {} if kind == "top_up" and profile is None else {"profile": profile or "big"}
    return {"request_id": rid, "requested_at": at, "kind": kind, "lease_id": None, "parent_lease": None,
            "project_id": "p", "uid": 501, "slots": slots, "bypassed_n": 0, "reasons": [], **own, **extra}


def top(rid, lease_id, slots=1, **extra):
    return req(rid, "top_up", slots=slots, lease_id=lease_id, token=1, **extra)


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
    res = run(state(queue=[req("a", slots=8, at=1), req("b", slots=8, at=2)]))
    assert list(res["state"]["holders"]) == ["a"] and ("admitted", "a") in names(res) and ("waited", "b") in names(res)
    assert [(r["request_id"], r["reasons"]) for r in res["state"]["queue"]] == [("b", ["budget_full"])]
    assert res["state"]["holders"]["a"]["token"] == 11 and res["state"]["fencing_counter"] == 11


def test_codex_example_b_gets_its_6_first_and_a_its_7_after_b_ends():
    st = state([lease("A", 1, 8, at=1), lease("B", 1, 7, at=2)], [top("ta", "A", 7, at=1), top("tb", "B", 6, at=2)])
    assert sa.is_safe(st, policy()) and sa.safe_sequence(st, policy()) == ["B", "A"]
    res = run(st)
    assert [res["state"]["holders"][k]["slots_granted"] for k in "BA"] == [7, 1]
    assert [(r["request_id"], r["reasons"]) for r in res["state"]["queue"]] == [("ta", ["budget_full"])]
    del res["state"]["holders"]["B"]
    final = run(res["state"])
    assert final["state"]["holders"]["A"]["slots_granted"] == 8 and final["state"]["queue"] == []


G, HELD = [lease("g", 1, 4, profile="seo-gate")], {"front": ("wait", ["memory_guard"])}  # a heavy holder, a held front
VERDICTS = [({}, ["memory_metric_unavailable"]), ({"h": ("wait", ["memory_guard"])}, ["memory_guard"]),
            ({"h": ("maybe", [])}, ["memory_guard"])] + [({"h": bad}, ["memory_metric_unavailable"]) for bad in (
                ("wait", None), ("admit",), ("admit", None), (None, []), ("wait", [None]), ("wait", "swap"), "admit")]
WAITS = [  # (holders, queue, run arguments, reasons per request): nothing is granted, the input is untouched
    ([lease("A", 1, 8, at=1), lease("B", 1, 7, at=2)], [top("ta", "A", 6)], {}, {"ta": ["unsafe_state"]}),
    ([lease("h", 6, 6)], [req("big", slots=4, at=1), req("tiny", at=2)], {}, {"big": ["budget_full"], "tiny": ["queue_ahead"]}),
    ([lease("A", 3, 8, at=1), lease("B", 4, 8, at=2)], [req("n")], {"pol": policy(total=9)}, {"n": ["unsafe_state"]}),
    ([lease("a", 1, 4)], [top("t", "a")], {"live": {"a": "gone"}}, {"t": ["owner_gone_tree_alive"]}),
    (G, [top("t", "g", 2)], {}, {"t": ["memory_metric_unavailable"]}),
] + [([], [req("h", profile="seo-gate")], {"mem": mem}, {"h": want}) for mem, want in VERDICTS]


@pytest.mark.parametrize("holders, queue, kw, want", WAITS)
def test_each_record_waits_with_its_reasons_and_nothing_is_granted(holders, queue, kw, want):
    st = state(holders, queue)
    snapshot = copy.deepcopy(st)
    res = run(st, **kw)
    assert {r["request_id"]: r["reasons"] for r in res["state"]["queue"]} == want and st == snapshot
    assert res["state"]["holders"] == st["holders"] and {e["event"] for e in res["events"]} == {"waited"}


L, NESTED = [lease("L", 2, 8, profile="big")], [lease("par", 4, 8), lease("kid", 1, 8, parent="par", profile="seo-gate")]
REFUSED = [  # (holders, queue, (request_id, reason) per refused record): the state stays as it was
    ([], [req("x", "weird"), req("y", slots=9)], [("x", "state_corrupt"), ("y", "over_profile_ceiling")]),
    (G, [top("t", "g", 2, profile="big")], [("t", "policy_invalid")]),
] + [(L, [req(rid, lease_id=lid)], [(rid, "state_corrupt")]) for rid, lid in [("r1", "L"), ("L", None), ("r1", ""), ("r1", 7)]] + [
    (NESTED, [req("n1", "nested", profile="seo-gate", parent_lease="par", lease_id=clash)], [("n1", "state_corrupt")])
    for clash in ("kid", "par")]


@pytest.mark.parametrize("holders, queue, want", REFUSED)
def test_a_defect_of_the_record_is_refused_78_and_leaves_the_state_as_it_was(holders, queue, want):
    st = state(holders, queue)
    res = run(st)
    assert [(e["event"], e["request_id"], e["reasons"], e["exit_code"]) for e in res["events"]] == [
        ("refused", rid, [reason], 78) for rid, reason in want]
    assert res["state"]["holders"] == st["holders"] and res["state"]["queue"] == []
    assert res["state"]["fencing_counter"] == st["fencing_counter"]


def test_a_top_up_is_checked_on_the_holders_profile_and_brings_only_a_slot_count():
    for profile in (None, "seo-gate"):
        res = run(state(G, [top("t", "g", 2, profile=profile)]), mem={"t": ("admit", [])})
        assert names(res) == [("topped_up", "g")] and res["state"]["holders"]["g"]["slots_granted"] == 3
    assert "h" in run(state(queue=[req("h", profile="seo-gate")]), mem={"h": ("admit", [])})["state"]["holders"]


KID, NEST = lease("kid", 1, 4, parent="par", profile="seo-gate"), req("n", "nested", profile="seo-gate", parent_lease="par")
SIB = {**KID, "lease_id": "sib", "slots_granted": True}
CORRUPT = {  # a defect of a held lease raises, from a building block and from every step of decide_round
    "nested_plan-sibling": lambda: sa.nested_plan(state([lease("par", 1, 8), SIB]), policy(), "par", "seo-gate", 2),
    "growth_plan-sibling": lambda: sa.growth_plan(state([lease("par", 1, 8), KID, SIB]), "kid", 1),
    "nested-sibling": lambda: run(state([lease("par", 4, 8), SIB], [NEST])),
    "top_up-sibling": lambda: run(state([lease("par", 1, 8), KID, SIB], [top("t", "kid", profile="seo-gate")])),
    "root-lease": lambda: run(state([lease("par", True, 8)], [top("t", "par")])),
    "parent-grant": lambda: run(state([lease("par", 4, 8), lease("mid", True, 3, parent="par", profile="mid"),
                                       {**KID, "parent_lease": "mid", "slots_granted": 0}], [top("t", "kid")])),
    "parent-profile": lambda: run(state([lease("par", 4, 8, profile="ghost")], [NEST])),
    "holder-profile": lambda: run(state([lease("g", 1, 4, profile="ghost")], [top("t", "g")])),
    "parent-cycle": lambda: run(state([lease("a", 1, 4, parent="b"), lease("b", 1, 4, parent="a")], [top("t", "a")])),
}


@pytest.mark.parametrize("call", list(CORRUPT.values()), ids=list(CORRUPT))
def test_a_defect_of_a_held_lease_raises_state_corrupt_and_refuses_nothing(call):
    with pytest.raises(sa.AdmissionInvalid) as exc:
        call()
    assert exc.value.reason == "state_corrupt" and exc.value.exit_code == 78


def test_bypassed_n_counts_and_max_bypass_stops_a_top_up():
    held, st = HELD, state([lease("A", 2, 8, at=1)], [req("front", slots=2, at=1, profile="hsmall"), top("t1", "A")])
    first = run(st, policy(max_bypass=1), mem=held)
    assert first["state"]["holders"]["A"]["slots_granted"] == 3 and [r["bypassed_n"] for r in first["state"]["queue"]] == [1]
    first["state"]["queue"].append(top("t2", "A", 4))
    second = run(first["state"], policy(max_bypass=1), mem=held)
    waiting = {r["request_id"]: r for r in second["state"]["queue"]}
    assert second["state"]["holders"]["A"]["slots_granted"] == 3 and waiting["t2"]["reasons"] == ["queue_ahead"]
    assert waiting["front"]["bypassed_n"] == 1 and any(e["event"] == "waited" and e["bypassed_n"] == 1 for e in second["events"])
    free_run = run(first["state"], policy(max_bypass=5), mem=held)
    assert free_run["state"]["holders"]["A"]["slots_granted"] == 7 and [r["bypassed_n"] for r in free_run["state"]["queue"]] == [2]


@pytest.mark.parametrize("kind", ["top_up", "nested"])
@pytest.mark.parametrize("bad", [{"profile": "nope"}, {"slots": True}, {"slots": -1}, {"slots": "3"}])
def test_malformed_front_new_request_never_crashes_a_top_up_round(kind, bad):
    ex = {"lease_id": "a", "token": 1} if kind == "top_up" else {"lease_id": "c", "parent_lease": "a", "profile": "seo-gate"}
    top_, m = req("t", kind, slots=3 if kind == "top_up" else 2, at=1, **ex), req("m", at=0, **bad)
    mk = lambda *q: state([lease("a", 1, 4), lease("b", 3, 3)], list(q))
    got = lambda res, rid: [(e["event"], e["reasons"]) for e in res["events"] if e["request_id"] == rid]
    assert got(run(mk(m, top_)), "m") == got(run(mk(m)), "m") == [("refused", got(run(mk(m)), "m")[0][1])]
    assert got(run(mk(m, top_)), "t") == got(run(mk(top_)), "t")


def test_bypassed_n_never_exceeds_max_bypass_over_a_round_sequence():
    held, st = HELD, state([lease("A", 1, 8, at=1)], [req("front", slots=2, at=1, profile="hsmall")])
    for number in range(6):
        st["queue"].append(top(f"t{number}", "A", at=2 + number))
        st = run(st, policy(max_bypass=2), mem=held)["state"]
        front = next(r for r in st["queue"] if r["request_id"] == "front")
        assert front["bypassed_n"] <= 2
    assert front["bypassed_n"] == 2


@pytest.mark.parametrize("kid, want", [("gone", [("reaped", "kid", 2), ("reaped", "par", 3)]),
                                       ("alive", [("lease_held", "par", 3)])])
def test_cleanup_events_carry_the_granted_slots_and_keep_request_id_null(kid, want):
    st = state([lease("par", 3, 8), lease("kid", 2, 4, parent="par")])
    events = sa.cleanup_decision(st, {"par": "gone", "kid": kid}, 50)["events"]
    assert [(e["event"], e["lease_id"], e["slots"]) for e in events] == want and all(e["request_id"] is None for e in events)


def test_refused_duplicate_at_the_front_does_not_block_the_next_new_request():
    st = state([lease("L", 2, 2, profile="small")],
               [req("dup", at=1, profile="small", lease_id="L"), req("ok", at=2, profile="small")])
    res = run(st)
    assert names(res) == [("refused", "L"), ("admitted", "ok")] and res["state"]["queue"] == []
    assert (res["events"][0]["reasons"], res["events"][0]["exit_code"]) == (["state_corrupt"], 78)
    assert res["state"]["holders"]["L"] == st["holders"]["L"]
    assert res["state"]["fencing_counter"] == st["fencing_counter"] + 1


def test_admitted_and_top_up_events_carry_the_request_id_of_the_queued_record():
    events = run(state(queue=[req("req-1", slots=2, lease_id="L-1")]))["events"]
    assert [(e["request_id"], e["lease_id"]) for e in events if e["event"] == "admitted"] == [("req-1", "L-1")]
    topped = [e for e in run(state([lease("A", 1, 8, at=1)], [top("req-ta", "A", 3)]))["events"] if e["event"] == "topped_up"]
    assert [(e["request_id"], e["lease_id"], e["reasons"]) for e in topped] == [("req-ta", "A", [])]


def test_nested_and_every_multi_level_top_up_event_carry_the_triggering_request_id():
    st = state([lease("root", 1, 8), lease("mid", 1, 3, parent="root", profile="mid")],
               [req("req-n", "nested", slots=3, profile="seo-gate", parent_lease="mid", lease_id="kid")])
    events = run(st, mem={"req-n": ("admit", [])})["events"]
    topped, nested = [[e for e in events if e["event"] == k] for k in ("topped_up", "nested")]
    assert {e["lease_id"] for e in topped} == {"root", "mid"}
    assert [(e["request_id"], e["lease_id"]) for e in nested] == [("req-n", "kid")]
    assert all(e["request_id"] == "req-n" and e["reasons"] == [] for e in topped)


def test_top_up_with_an_unknown_profile_is_refused_alone_and_the_round_goes_on():
    mk = lambda *q: state([lease("a", 1, 4), lease("b", 1, 4)], list(q))
    res, alone = run(mk(top("bad", "a", 2, at=1, profile="nope"), top("ok", "b", 2, at=2))), run(mk(top("ok", "b", 2, at=2)))
    assert [(e["event"], e["reasons"]) for e in res["events"] if e["request_id"] == "bad"] == [("refused", ["policy_invalid"])]
    assert res["events"][1:] == alone["events"]
    assert res["state"]["queue"] == [] and res["state"]["holders"] == alone["state"]["holders"]

