"""Tests for scripts/lib/slot/slot_liveness.py (RS-4): recorded ps text, no live process."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))
from slot import slot_liveness as sl

T1, T2 = "Mon 9 Nov 10:00:00 2026", "Mon 9 Nov 10:05:00 2026"


def ps(*rows):
    """rows: (pid, ppid, session, lstart) -> recorded ps -A text."""
    return "\n".join(f"{p:>5} {pp:>5} {s:>5} {t}" for p, pp, s, t in rows) + "\n"


def table(*rows):
    return sl.parse_process_table(ps(*rows))


def run_lease(lease_id, session, parent=None):
    return {"lease_id": lease_id, "owner_kind": "run", "session_id": session, "parent_lease": parent}


def verdict(lease, tbl, holders=None):
    return sl.lease_liveness(lease, tbl, holders or {lease["lease_id"]: lease})[0]


def test_session_with_dead_leader_and_live_child_is_alive():
    tbl = table((200, 1, "77", T2))  # leader 100 is gone, its child keeps the session
    assert verdict(run_lease("a", "77"), tbl) == sl.ALIVE


def test_empty_session_is_gone():
    assert verdict(run_lease("a", "77"), table((5, 1, "9", T1))) == sl.GONE


def test_parent_alive_through_child_lease_in_another_session():
    parent, child = run_lease("p", "10"), run_lease("c", "20", "p")
    tbl = table((300, 1, "20", T1))
    assert verdict(parent, tbl, {"p": parent, "c": child}) == sl.ALIVE


def test_chain_where_only_grandchild_lives_keeps_all_alive():
    p, c, g = run_lease("p", "1"), run_lease("c", "2", "p"), run_lease("g", "3", "c")
    holders, tbl = {"p": p, "c": c, "g": g}, table((400, 1, "3", T1))
    assert [verdict(x, tbl, holders) for x in (p, c, g)] == [sl.ALIVE] * 3


def test_parent_gone_when_session_and_all_children_gone():
    p, c = run_lease("p", "1"), run_lease("c", "2", "p")
    assert verdict(p, table((9, 1, "8", T1)), {"p": p, "c": c}) == sl.GONE


def test_unknown_child_makes_parent_unknown_not_gone():
    p, c = run_lease("p", "1"), run_lease("c", None, "p")
    assert verdict(p, table((9, 1, "8", T1)), {"p": p, "c": c}) == sl.UNKNOWN


def test_reused_pid_with_other_start_time_is_gone():
    tbl = table((100, 1, "5", T2))
    assert sl.process_alive(tbl, 100, T1) == sl.GONE
    assert sl.process_alive(tbl, 100, T2) == sl.ALIVE
    lease = {"lease_id": "a", "owner_kind": "acquire", "owner_pid": 100, "owner_start_time": T1}
    assert verdict(lease, tbl) == sl.GONE


def test_start_time_padding_is_normalised():
    assert sl.process_alive(table((7, 1, "5", "Mon  9 Nov 10:00:00 2026")), 7, T1) == sl.ALIVE


def test_acquire_owner_exited_stays_alive_through_remembered_descendant():
    lease = {"lease_id": "a", "owner_kind": "acquire", "owner_pid": 100, "owner_start_time": T1, "descendants": []}
    first, kids = sl.lease_liveness(lease, table((100, 1, "5", T1), (101, 100, "5", T2), (102, 101, "5", T2)), {})
    assert first == sl.ALIVE and {d["pid"] for d in kids} == {101, 102}
    lease["descendants"] = kids
    again, kids2 = sl.lease_liveness(lease, table((102, 1, "5", T2)), {})  # owner and 101 gone
    assert again == sl.ALIVE and kids2 == kids
    assert sl.lease_liveness(lease, table((9, 1, "5", T1)), {})[0] == sl.GONE


def test_acquire_missing_owner_identity_is_unknown():
    assert verdict({"lease_id": "a", "owner_kind": "acquire"}, table((9, 1, "5", T1))) == sl.UNKNOWN


def test_raising_runner_gives_unknown_for_every_lease():
    def boom(argv):
        raise OSError("ps failed")

    tbl = sl.snapshot(boom)
    acq = {"lease_id": "a", "owner_kind": "acquire", "owner_pid": 1, "owner_start_time": T1}
    assert tbl is None
    assert [verdict(x, tbl) for x in (run_lease("r", "1"), acq)] == [sl.UNKNOWN] * 2
    assert sl.process_alive(tbl, 1, T1) == sl.UNKNOWN


@pytest.mark.parametrize("text", ["", "garbage\n", "1 0 0\n"])
def test_unparseable_text_gives_none(text):
    assert sl.snapshot(lambda argv: text) is None


def test_snapshot_parses_recorded_text_and_passes_ps_argv():
    seen = []
    tbl = sl.snapshot(lambda argv: seen.append(argv) or ps((1, 0, "0", T1)))
    assert seen == [sl.PS_ARGV] and tbl == [{"pid": 1, "ppid": 0, "session": "0", "start_time": T1}]


def test_supervisor_gone_tree_alive():
    lease = {**run_lease("a", "77"), "supervisor_pid": 50, "supervisor_start_time": T1}
    live = table((200, 1, "77", T2))
    assert sl.supervisor_gone_tree_alive(lease, live, {"a": lease}) is True
    assert sl.supervisor_gone_tree_alive(lease, live + table((50, 1, "3", T1)), {"a": lease}) is False
    assert sl.supervisor_gone_tree_alive(lease, table((9, 1, "3", T1)), {"a": lease}) is False
    assert sl.supervisor_gone_tree_alive(lease, None, {"a": lease}) is False


@pytest.mark.parametrize("bad", [None, 0, -4, "12", 1.5, True])
def test_acquire_without_valid_owner_pid_is_78(bad):
    with pytest.raises(sl.OwnerPidRequired) as exc:
        sl.validate_acquire_owner(bad)
    assert exc.value.exit_code == 78 and exc.value.code == "owner_pid_required"


def test_valid_owner_pid_passes():
    assert sl.validate_acquire_owner(4242) == 4242
