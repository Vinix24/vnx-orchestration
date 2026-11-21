"""morning round D1b: the morning pause at the door (stop condition, part A) and in pr_merge (part B)."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "scripts", ROOT / "scripts" / "lib"):
    sys.path.insert(0, str(_p))

import dispatch_cli
import morning_pause_gate as gate
import morning_snapshot as ag
import operator_queue as oq
import pr_merge
import stop_conditions as sc
from test_pr_merge_door_permit import door as merge_door  # fixture

P, DAY = "proj-a", "2026-11-19"
IN_WINDOW = datetime(2026, 11, 19, 6, 30, tzinfo=timezone.utc)  # 07:30 Europe/Amsterdam
AFTER = datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)  # 10:00 local
CONFIG = {"VNX_MORNING_PAUSE_ENABLED": "true", "VNX_MORNING_PAUSE_START": "07:00",
          "VNX_MORNING_PAUSE_DEADLINE": "09:00", "VNX_MORNING_PAUSE_TZ": "Europe/Amsterdam"}


def snapshot(sd, *, decide=False):
    oq.open_point(P, bron=1, ref="a", vraag="q a", sluit_bij="x", opened_by="t", state_dir=sd)
    ag.write_snapshot(P, DAY, state_dir=sd, now="2026-11-19T05:00:00Z")
    if decide:
        oq.close_point(P, oq.point_id(P, 1, "a", 1), uitkomst="beantwoord", bewijs="e", state_dir=sd)


def events(sd):
    path = sd / ag.EVENTS_FILENAME
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def seams(tmp_path, monkeypatch):
    """Injected getter and clock for the door check; never the live registry."""
    state = {"cfg": dict(CONFIG), "now": IN_WINDOW, "sd": tmp_path}
    monkeypatch.setattr(gate, "registry_getter", lambda pid, sd: state["cfg"].__getitem__)
    monkeypatch.setattr("vnx_paths.project_id_from_state_dir", lambda sd: P)
    return state


def door(seams):
    return sc.check_morning_pause(seams["sd"], now=seams["now"], get=seams["cfg"].__getitem__)


def test_off_reads_nothing_else(tmp_path):
    def only_enabled(key):
        assert key == "VNX_MORNING_PAUSE_ENABLED", f"read {key}"
        return "false"
    sd = tmp_path / "missing"
    v = gate.morning_pause_verdict(P, IN_WINDOW, state_dir=sd, get=only_enabled)
    assert (v.paused, v.reason) == (False, "disabled") and not sd.exists()
    assert sc.check_morning_pause(sd, now=IN_WINDOW, get=only_enabled).status == sc.CheckStatus.CLEAR


def test_open_point_triggers_door_and_books_one_refused_line(seams):
    snapshot(seams["sd"])
    res = door(seams)
    assert res.status == sc.CheckStatus.TRIGGERED and res.evidence["reason"] == "open_points"
    dispatch_cli._book_morning_pause([res], state_dir=seams["sd"], dispatch_id="dlv-x", override_reason="")
    assert [(e["kind"], e["surface"], e["ref"]) for e in events(seams["sd"])] == [
        ("refused", "door", "dlv-x")]


@pytest.mark.parametrize("decide,now", [(True, IN_WINDOW), (False, AFTER)])
def test_all_decided_and_after_deadline_are_clear(seams, decide, now):
    snapshot(seams["sd"], decide=decide)
    seams["now"] = now
    assert door(seams).status == sc.CheckStatus.CLEAR


@pytest.mark.parametrize("key,bad,reason", [
    ("VNX_MORNING_PAUSE_START", "7am", "morning_pause_config_invalid: VNX_MORNING_PAUSE_START"),
    ("VNX_MORNING_PAUSE_TZ", "Mars/Base", "morning_pause_config_invalid: VNX_MORNING_PAUSE_TZ"),
    ("VNX_MORNING_PAUSE_ENABLED", "maybe", "morning_pause_config_invalid: VNX_MORNING_PAUSE_ENABLED")])
def test_bad_config_is_paused(seams, key, bad, reason):
    if key:
        seams["cfg"][key] = bad
    res = door(seams)
    assert res.status == sc.CheckStatus.TRIGGERED and res.evidence["reason"].startswith(reason)


def test_no_snapshot_today_is_not_started_and_books_nothing(seams):
    assert door(seams).status == sc.CheckStatus.CLEAR and events(seams["sd"]) == []


def test_unreadable_registry_is_paused(seams):
    def unreadable(key):
        raise ValueError(f"{key}: config unreadable")
    v = gate.morning_pause_verdict(P, IN_WINDOW, state_dir=seams["sd"], get=unreadable)
    assert v.paused and v.reason.startswith("morning_pause_config_invalid")


def test_door_blocks_then_override_passes_and_books_lines(seams, monkeypatch):
    snapshot(seams["sd"])
    real = sc.check_morning_pause
    monkeypatch.setattr(sc, "check_morning_pause", lambda sd: real(sd, now=seams["now"], get=seams["cfg"].__getitem__))
    for name in ("check_main_ci_red", "check_gh_auth_dead"):
        monkeypatch.setattr(sc, name, lambda *a, **k: sc._clear("x", "ok"))
    blocked = dispatch_cli._check_stop_conditions_verdict(state_dir=seams["sd"], dispatch_id="dlv-x")
    assert blocked.severity == "blocking" and "morning_pause" in blocked.message
    passed = dispatch_cli._check_stop_conditions_verdict(
        state_dir=seams["sd"], dispatch_id="dlv-x", override_reason="revert_or_fix_forward")
    assert passed.override_applied
    assert [(e["kind"], e["surface"]) for e in events(seams["sd"])] == [("refused", "door"), ("override", "door")]


def test_failing_append_keeps_the_refusal_and_shows_the_error(seams, monkeypatch, capsys):
    snapshot(seams["sd"])
    monkeypatch.setattr(gate, "append_event", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    res = door(seams)
    dispatch_cli._book_morning_pause([res], state_dir=seams["sd"], dispatch_id="dlv-x", override_reason="")
    assert res.status == sc.CheckStatus.TRIGGERED and "OSError: disk full" in capsys.readouterr().err
    v = gate.MorningPauseVerdict(True, "x", DAY)
    assert gate.record_event("refused", v, project_id=None, state_dir=seams["sd"], surface="door", ref="r", reason="x")


# --- part B: the pr_merge preflight ---------------------------------------------------------


@pytest.fixture
def merge_seams(seams, monkeypatch):
    monkeypatch.setattr(pr_merge, "active_store", lambda: (seams["sd"], P))
    monkeypatch.setattr(pr_merge, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: seams["now"])}))
    return seams


def merge(override=None):
    return pr_merge._run_pause_gate(argparse.Namespace(pr=7, dispatch_id="dlv-x", override_morning_pause=override))


def test_merge_is_refused_in_the_window_with_one_refused_line(merge_seams):
    snapshot(merge_seams["sd"])
    assert merge()["verdict"] == "NO-GO"
    (line,) = events(merge_seams["sd"])
    assert (line["kind"], line["surface"], line["ref"]) == ("refused", "merge", "PR #7 dlv-x")


@pytest.mark.parametrize("decide,now", [(True, IN_WINDOW), (False, AFTER)])
def test_merge_is_go_when_decided_or_after_the_deadline(merge_seams, decide, now):
    snapshot(merge_seams["sd"], decide=decide)
    merge_seams["now"] = now
    assert merge()["verdict"] == "GO" and events(merge_seams["sd"]) == []


@pytest.mark.parametrize("reason", ["revert_or_fix_forward", "operator says go"])
def test_merge_override_with_a_reason_is_go_with_one_override_line(merge_seams, reason):
    snapshot(merge_seams["sd"])
    g = merge(reason)
    assert g["verdict"] == "GO" and g["overridden"] and g["override_reason"] == reason
    (line,) = events(merge_seams["sd"])
    assert (line["kind"], line["surface"], line["reason"]) == ("override", "merge", reason)


@pytest.mark.parametrize("empty", ["", "   "])
def test_merge_empty_override_is_refused_in_english(merge_seams, empty):
    snapshot(merge_seams["sd"])
    g = merge(empty)
    assert g["verdict"] == "NO-GO" and not g["overridden"]
    assert "override refused: --override-morning-pause needs a non-empty reason" in g["message"]
    assert [e["kind"] for e in events(merge_seams["sd"])] == ["refused"]


def test_a_raising_gate_is_a_no_go_naming_the_type(merge_seams, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("store gone")
    monkeypatch.setattr(gate, "morning_pause_verdict", boom)
    g = merge()
    assert g["verdict"] == "NO-GO" and "morning_pause_unreadable: RuntimeError" in g["message"]


def test_a_failing_append_keeps_the_refusal_and_shows_the_error(merge_seams, monkeypatch, capsys):
    snapshot(merge_seams["sd"])
    monkeypatch.setattr(gate, "append_event", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert merge()["verdict"] == "NO-GO" and "OSError: disk full" in capsys.readouterr().err


def test_an_override_whose_line_cannot_be_written_is_a_no_go_naming_the_type(merge_seams, monkeypatch, capsys):
    snapshot(merge_seams["sd"])
    monkeypatch.setattr(gate, "append_event", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    g = merge("operator says go")
    assert g["verdict"] == "NO-GO" and not g["overridden"] and g["override_reason"] is None
    assert "morning_pause_override_unrecorded: OSError" in g["message"]
    assert "OSError: disk full" in capsys.readouterr().err
    assert events(merge_seams["sd"]) == []


def test_main_lands_the_refusal_in_the_preflight_ledger(merge_door, merge_seams):
    from test_pr_merge_door_permit import _refusal, _run
    snapshot(merge_seams["sd"])
    assert _run() == pr_merge.EXIT_ERROR
    gates = {g["gate"]: g for g in _refusal(merge_door)["preflight_gates"]}
    assert gates["morning_pause"]["verdict"] == "NO-GO" and gates["door"]["verdict"] == "not_evaluated"


BEFORE = datetime(2026, 11, 19, 5, 59, tzinfo=timezone.utc)  # 06:59 Europe/Amsterdam


@pytest.mark.parametrize("override", [None, "operator says go"])
def test_pause_is_rechecked_after_the_main_lock(merge_door, merge_seams, monkeypatch, override):
    """Approved at 06:59, the lock wait carries the merge to 07:30: the recheck refuses before the permit."""
    import contextlib
    from test_pr_merge_door_permit import _refusal, _run
    snapshot(merge_seams["sd"])
    merge_seams["now"] = BEFORE

    @contextlib.contextmanager
    def slow_lock(args, enabled):
        merge_seams["now"] = IN_WINDOW
        yield 600.0
    monkeypatch.setattr(pr_merge, "_main_admission_lock", slow_lock)
    extra = ("--override-morning-pause", override) if override else ()
    if override:  # an override first recorded at the recheck is one line, and a GO
        assert _run(*extra) == pr_merge.EXIT_OK
        assert [e["kind"] for e in events(merge_seams["sd"])] == ["override"]
        return
    assert _run() == pr_merge.EXIT_ERROR
    assert not merge_door.receipts("pr_merged") and not merge_door.of("POST")
    gates = {g["gate"]: g for g in _refusal(merge_door)["preflight_gates"]}
    assert gates["morning_pause"]["verdict"] == "NO-GO" and gates["door"]["verdict"] == "not_evaluated"
    assert [e["kind"] for e in events(merge_seams["sd"])] == ["refused"]


def test_an_override_recorded_at_the_first_check_is_not_recorded_again(merge_door, merge_seams):
    from test_pr_merge_door_permit import _run
    snapshot(merge_seams["sd"])
    assert _run("--override-morning-pause", "operator says go") == pr_merge.EXIT_OK
    assert [e["kind"] for e in events(merge_seams["sd"])] == ["override"]
