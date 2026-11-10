"""Tests for scripts/lib/dream_liveness.py (ADR-066, dream PR-2).

Every branch that could turn a silence into a PASS is pinned with the field
missing, None, malformed and unknown: all four stay blocking.
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dream_liveness as dl
from dream_liveness import ALLOWED, BLOCKED, assess, classify_night, classify_skip

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PLIST = {"path": "p", "label": "com.vnx.auto-dream", "project_id": "vnx-dev",
         "hour": 3, "minute": 0, "arm_switch": "1"}


def ev(etype, day, hhmm="03:05", **fields):
    out = {"event_type": etype, "cycle_id": f"c-{day}-{etype}",
           "timestamp": f"{day}T{hhmm}:00+00:00"}
    out.update(fields)
    return out


def doc(events, plist=PLIST, now=NOW):
    return assess({"events": events, "plist": plist}, project_id="vnx-dev", now=now, tz=UTC)


def ids(document):
    return {f["id"] for f in document["findings"]}


def good_week():
    return [ev("dream_cycle_completed", f"2026-11-{d:02d}", merged_count=1) for d in range(3, 11)]


# --- the closed D13 table -------------------------------------------------

@pytest.mark.parametrize("reason,detail,expected", [
    ("insufficient_data", "", (ALLOWED, True)),
    ("incomplete_data", "all receipts stale (newest 400.0h ago)", (ALLOWED, True)),
    ("incomplete_data", "receipts ledger absent: /x", (BLOCKED, True)),
    ("incomplete_data", "receipts ledger empty - none", (BLOCKED, True)),
    ("incomplete_data", "stale but ledger absent", (BLOCKED, True)),
    ("content_restricted_project", "", (ALLOWED, True)),
    ("content_restricted_all_rows", "", (ALLOWED, True)),
    ("scheduler_disabled", "", (BLOCKED, True)),
    ("probe_not_ok", "", (BLOCKED, True)),
    ("provider_switched_off", "", (BLOCKED, True)),
])
def test_every_d13_row(reason, detail, expected):
    assert classify_skip(reason, detail) == expected


@pytest.mark.parametrize("reason,detail", [
    ("incomplete_data", ""), ("incomplete_data", None), ("incomplete_data", 7),
    ("nonsense", "stale"), ("", ""), (None, None), (42, "stale"), (["insufficient_data"], ""),
])
def test_unexplained_or_unknown_reason_is_blocked_and_unknown(reason, detail):
    assert classify_skip(reason, detail) == (BLOCKED, False)


@pytest.mark.parametrize("reason", ["nonsense", None, "", 5])
def test_unknown_skip_reason_is_a_finding(reason):
    d = doc([ev("dream_cycle_skipped", "2026-11-09", reason=reason)])
    assert "unknown_skip_reason" in ids(d)
    assert d["state"] == "deviation"


# --- events ------------------------------------------------------------------

@pytest.mark.parametrize("etype", ["dream_cycle_nonsense", None, "", 3, ["x"]])
def test_unknown_event_type_is_a_finding_and_not_an_attempt(etype):
    d = doc([{"event_type": etype, "timestamp": "2026-11-10T11:00:00+00:00"}])
    assert "unknown_event_type" in ids(d)
    assert d["attempts"]["newest"] is None
    assert "no_attempt_26h" in ids(d)


@pytest.mark.parametrize("stamp", [None, "", "not-a-date", 12345, True, {"a": 1}])
def test_undated_event_is_counted_and_never_an_age(stamp):
    event = ev("dream_cycle_completed", "2026-11-10", merged_count=3)
    if stamp is None:
        del event["timestamp"]
    else:
        event["timestamp"] = stamp
    d = doc([event])
    assert "undated_event" in ids(d)
    assert d["progress_window"]["undated_events"] == 1
    assert d["attempts"]["age_hours"] is None
    assert "no_attempt_26h" in ids(d)


def test_torn_and_non_object_lines_surface_as_undated(tmp_path):
    folder = tmp_path / "events" / "dream"
    folder.mkdir(parents=True)
    (folder / "a.ndjson").write_text('{broken\n[1, 2]\n"x"\n\n' + json.dumps(
        ev("dream_cycle_started", "2026-11-10")) + "\n")
    events = dl.read_events(folder)
    assert len(events) == 4
    d = doc(events)
    assert "unknown_event_type" in ids(d)


def test_read_events_reads_every_file_not_by_date(tmp_path):
    folder = tmp_path / "events" / "dream"
    folder.mkdir(parents=True)
    (folder / "2020-01-01.ndjson").write_text(json.dumps(ev("dream_cycle_started", "2026-11-10")) + "\n")
    (folder / "zzz-other.ndjson").write_text(json.dumps(ev("dream_cycle_skipped", "2026-11-10")) + "\n")
    assert len(dl.read_events(folder)) == 2
    assert dl.read_events(tmp_path / "absent") is None


def test_read_plist_states(tmp_path):
    assert dl.read_plist(tmp_path / "none.plist") is None
    bad = tmp_path / "bad.plist"
    bad.write_text("<<not a plist")
    assert dl.read_plist(bad) == dl.PLIST_UNREADABLE


def test_load_sources_data_root_follows_project_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    folder = tmp_path / ".vnx-data" / "p1" / "events" / "dream"
    folder.mkdir(parents=True)
    (folder / "a.ndjson").write_text(json.dumps(ev("dream_cycle_started", "2026-11-10")) + "\n")
    got = dl.load_sources("p1", plist_path=tmp_path / "none.plist")
    assert len(got["events"]) == 1 and got["plist"] is None


# --- state and sources: fail-closed ------------------------------------------

@pytest.mark.parametrize("events", [None, "x", {"a": 1}, 7])
def test_missing_or_malformed_events_source_is_unmeasured_never_ok(events):
    d = assess({"events": events, "plist": PLIST}, now=NOW, tz=UTC)
    assert d["state"] == "unmeasured" and "events_missing" in ids(d)
    assert d["sources"]["events"] != "ok"


@pytest.mark.parametrize("plist", [None, dl.PLIST_UNREADABLE, "x", 3, [],
                                   {}, {"hour": None, "minute": 0}, {"hour": "3", "minute": 0},
                                   {"hour": 25, "minute": 0}, {"hour": 3, "minute": 60},
                                   {"hour": True, "minute": 0}])
def test_missing_or_malformed_plist_never_ok(plist):
    d = doc(good_week(), plist=plist)
    assert d["state"] != "ok"
    assert ids(d) & {"plist_missing", "plist_unreadable"}
    assert d["nights"] == []


def test_assess_without_any_source_is_unmeasured():
    d = assess({}, now=NOW, tz=UTC)
    assert d["state"] == "unmeasured"
    assert {"events_missing", "plist_missing"} <= ids(d)
    assert d["schema"] == "dream_liveness/1"


@pytest.mark.parametrize("arm", [None, "0", "", 1, "true"])
def test_arm_switch_must_be_exactly_one(arm):
    d = doc(good_week(), plist=dict(PLIST, arm_switch=arm))
    assert "arm_switch_off" in ids(d) and d["state"] != "ok"


def test_empty_events_with_installed_plist_is_a_finding():
    d = doc([])
    assert d["state"] == "deviation"
    assert {"no_attempt_26h", "no_scheduled_attempt", "no_output"} <= ids(d)


def test_progressing_week_is_ok():
    d = doc(good_week())
    assert d["state"] == "ok", d["findings"]
    assert d["progress_window"]["completed_with_output"] == 7
    assert d["findings"] == [] and len(d["nights"]) == 7
    assert d["nights"][-1]["night"] == "2026-11-10"


# --- progress (D3) and attempts (D4) -----------------------------------------

@pytest.mark.parametrize("counts", [
    {}, {"merged_count": 0, "dropped_count": 0, "archived_count": 0, "flagged_count": 0},
    {"merged_count": None}, {"merged_count": "3"}, {"merged_count": True}, {"merged_count": -1},
    {"merged_count": 2.5},
])
def test_completed_night_without_real_output_is_no_progress(counts):
    night = classify_night(date(2026, 11, 9), datetime(2026, 11, 9, 3, tzinfo=UTC),
                           datetime(2026, 11, 9, 6, tzinfo=UTC),
                           [(datetime(2026, 11, 9, 3, 5, tzinfo=UTC),
                             ev("dream_cycle_completed", "2026-11-09", **counts))])
    assert night["progress"] is False and night["terminal"] == "completed"


@pytest.mark.parametrize("key", dl._OUTPUT_KEYS)
def test_any_output_key_counts_as_progress(key):
    d = doc([ev("dream_cycle_completed", "2026-11-09", **{key: 1})])
    assert d["progress_window"]["completed_with_output"] == 1


def test_review_event_after_40_days_of_silence_refreshes_nothing():
    events = [ev("dream_cycle_skipped", "2026-10-01", reason="scheduler_disabled"),
              ev("dream_cycle_approved", "2026-11-10", "11:00"),
              ev("dream_cycle_rejected", "2026-11-10", "11:30")]
    d = doc(events)
    assert d["attempts"]["age_hours"] > 26
    assert "no_attempt_26h" in ids(d)
    assert d["progress_window"]["completed_with_output"] == 0
    assert all(not n["attempt"] for n in d["nights"])


def test_started_without_end_in_a_closed_window_and_in_flight_in_an_open_one():
    closed = doc([ev("dream_cycle_started", "2026-11-09")])
    assert "started_without_end" in ids(closed)
    open_now = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)
    flying = doc([ev("dream_cycle_started", "2026-11-10")], now=open_now)
    assert "started_without_end" not in ids(flying)
    assert flying["in_flight"][0]["cycle_id"] == "c-2026-11-10-dream_cycle_started"
    ended = doc([ev("dream_cycle_started", "2026-11-09", cycle_id="c1"),
                 ev("dream_cycle_completed", "2026-11-09", "03:20", cycle_id="c1")])
    assert "started_without_end" not in ids(ended)


def test_failure_outside_the_window_is_history_only():
    d = doc(good_week() + [ev("dream_cycle_failed", "2026-11-09", "09:00")])
    assert d["history"]["failed"] == 1
    assert "blocked_skip" not in ids(d)


def test_failure_inside_the_window_is_blocked():
    d = doc(good_week() + [ev("dream_cycle_timeout", "2026-11-09", "03:40")])
    assert "blocked_skip" in ids(d)
    assert [n["class"] for n in d["nights"] if n["night"] == "2026-11-09"] == [BLOCKED]


def test_blocked_skip_and_allowed_streak():
    skips = [ev("dream_cycle_skipped", f"2026-11-{d:02d}", reason="insufficient_data")
             for d in (6, 7, 8)]
    d = doc(skips + [ev("dream_cycle_skipped", "2026-11-09", reason="probe_not_ok")])
    assert d["progress_window"]["allowed_skip_streak_max"] == 3
    assert {"allowed_skip_streak", "blocked_skip", "no_output"} <= ids(d)


def test_two_allowed_skips_in_a_row_are_tolerated():
    skips = [ev("dream_cycle_skipped", f"2026-11-{d:02d}", reason="insufficient_data") for d in (9, 10)]
    d = doc([ev("dream_cycle_completed", f"2026-11-0{n}", merged_count=1) for n in range(4, 9)] + skips)
    assert d["state"] == "ok", d["findings"]


def test_outcome_carries_the_class():
    d = doc([ev("dream_cycle_skipped", "2026-11-10", "11:00", reason="scheduler_disabled")])
    assert d["outcome"]["reason"] == "scheduler_disabled" and d["outcome"]["class"] == BLOCKED
    assert doc([])["outcome"] is None


def test_assess_writes_nothing(tmp_path):
    before = sorted(tmp_path.rglob("*"))
    doc(good_week())
    assert sorted(tmp_path.rglob("*")) == before
