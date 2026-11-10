"""Tests for scripts/lib/dream_liveness.py (ADR-066, dream PR-2).

Every branch that could turn a silence into a PASS is pinned with the field
missing, None, malformed and unknown: all four stay blocking.
"""
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dream_liveness as dl
from dream_liveness import ALLOWED, BLOCKED, SEVERITY, assess, classify_night, classify_skip

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
    assert "unknown_skip_reason" in ids(d) and d["state"] == "deviation"


@pytest.mark.parametrize("etype", ["dream_cycle_nonsense", None, "", 3, ["x"]])
def test_unknown_event_type_is_a_finding_and_not_an_attempt(etype):
    d = doc([{"event_type": etype, "timestamp": "2026-11-10T11:00:00+00:00"}])
    assert {"unknown_event_type", "no_attempt_26h"} <= ids(d) and d["attempts"]["newest"] is None


@pytest.mark.parametrize("stamp", [None, "", "not-a-date", 12345, True, {"a": 1}])
def test_undated_event_is_counted_and_never_an_age(stamp):
    event = ev("dream_cycle_completed", "2026-11-10", merged_count=3)
    event.pop("timestamp")
    if stamp is not None:
        event["timestamp"] = stamp
    d = doc([event])
    assert "undated_event" in ids(d)
    assert d["attempts"]["age_hours"] is None
    assert "no_attempt_26h" in ids(d)


def test_undated_unknown_skip_reason_still_gives_the_error_next_to_a_fresh_completion():
    skipped = ev("dream_cycle_skipped", "2026-11-09", reason="nonsense")
    del skipped["timestamp"]
    d = doc([ev("dream_cycle_completed", "2026-11-10", "11:00", merged_count=1), skipped])
    assert {"unknown_skip_reason", "undated_event"} <= ids(d)
    assert d["state"] == "deviation"


def test_sole_future_completion_is_undated_and_no_attempt():
    d = doc([ev("dream_cycle_completed", "2027-11-10", merged_count=1)])
    assert {"undated_event", "no_attempt_26h"} <= ids(d) and d["state"] == "deviation"
    assert d["attempts"]["newest"] is None and d["attempts"]["age_hours"] is None


def test_future_event_never_becomes_the_newest_attempt():
    valid = ev("dream_cycle_completed", "2026-11-10", "11:00", merged_count=1)
    d = doc([valid, ev("dream_cycle_completed", "2026-11-11", merged_count=1)])
    assert d["attempts"]["newest"]["cycle_id"] == valid["cycle_id"]
    assert "undated_event" in ids(d) and "no_attempt_26h" not in ids(d)
    assert d["attempts"]["age_hours"] == 1.0


def test_torn_and_non_object_lines_surface_as_undated(tmp_path):
    folder = tmp_path / "events" / "dream"
    folder.mkdir(parents=True)
    (folder / "a.ndjson").write_text('{broken\n[1, 2]\n"x"\n\n' + json.dumps(
        ev("dream_cycle_started", "2026-11-10")) + "\n")
    events = dl.read_events(folder)
    assert len(events) == 4 and "unknown_event_type" in ids(doc(events))


def test_read_events_reads_every_file_not_by_date(tmp_path):
    folder = tmp_path / "events" / "dream"
    folder.mkdir(parents=True)
    (folder / "2020-01-01.ndjson").write_text(json.dumps(ev("dream_cycle_started", "2026-11-10")) + "\n")
    (folder / "zzz-other.ndjson").write_text(json.dumps(ev("dream_cycle_skipped", "2026-11-10")) + "\n")
    assert len(dl.read_events(folder)) == 2 and dl.read_events(tmp_path / "absent") is None


def test_read_plist_states(tmp_path):
    assert dl.read_plist(tmp_path / "none.plist") is None
    (tmp_path / "bad.plist").write_text("<<not a plist")
    assert dl.read_plist(tmp_path / "bad.plist") == dl.PLIST_UNREADABLE


@pytest.mark.parametrize("events", [None, "x", {"a": 1}, 7])
def test_missing_or_malformed_events_source_is_unmeasured_never_ok(events):
    d = assess({"events": events, "plist": PLIST}, now=NOW)
    assert d["state"] == "unmeasured" and "events_missing" in ids(d)
    assert d["sources"]["events"] != "ok"


@pytest.mark.parametrize("plist", [None, dl.PLIST_UNREADABLE, "x", 3, [],
                                   {}, {"hour": None, "minute": 0}, {"hour": "3", "minute": 0},
                                   {"hour": 25, "minute": 0}, {"hour": 3, "minute": 60},
                                   {"hour": True, "minute": 0}])
def test_missing_or_malformed_plist_never_ok(plist):
    d = doc(good_week(), plist=plist)
    assert d["state"] != "ok" and ids(d) & {"plist_missing", "plist_unreadable"}


def test_assess_without_any_source_is_unmeasured():
    d = assess({}, now=NOW)
    assert d["state"] == "unmeasured" and {"events_missing", "plist_missing"} <= ids(d)
    assert d["schema"] == "dream_liveness/1"


@pytest.mark.parametrize("arm", [None, "0", "", 1, "true"])
def test_arm_switch_must_be_exactly_one(arm):
    d = doc(good_week(), plist=dict(PLIST, arm_switch=arm))
    assert "arm_switch_off" in ids(d)
    assert [f["severity"] for f in d["findings"] if f["id"] == "arm_switch_off"] == ["warn"]


def test_empty_events_with_installed_plist_is_a_finding():
    assert doc([])["state"] == "deviation" and "no_attempt_26h" in ids(doc([]))


def test_progressing_week_is_ok():
    d = doc(good_week())
    assert d["state"] == "ok" and d["findings"] == []
    assert d["progress_window"]["completed_with_output"] == 7 and len(d["nights"]) == 7
    assert d["sources"] == {"events": "ok", "plist": "ok"}


def test_review_event_after_40_days_of_silence_refreshes_nothing():
    events = [ev("dream_cycle_skipped", "2026-10-01", reason="scheduler_disabled"),
              ev("dream_cycle_approved", "2026-11-10", "11:00"),
              ev("dream_cycle_rejected", "2026-11-10", "11:30")]
    d = doc(events)
    assert d["attempts"]["age_hours"] > 26
    assert "no_attempt_26h" in ids(d)


def test_severity_table_is_exactly_the_decided_one():
    errors = {"blocked_skip", "unknown_event_type", "unknown_skip_reason",
              "no_attempt_26h", "started_without_end", "no_scheduled_attempt"}
    warns = {"arm_switch_off", "undated_event", "allowed_skip_streak", "no_output", "review_overdue"}
    assert {k for k, v in SEVERITY.items() if v == "error"} == errors
    assert warns <= {k for k, v in SEVERITY.items() if v == "warn"}
    assert set(SEVERITY.values()) == {"error", "warn"}


def test_warn_only_document_is_ok():
    d = doc(good_week(), plist=dict(PLIST, arm_switch="0"))
    assert [f["id"] for f in d["findings"]] == ["arm_switch_off"] and d["state"] == "ok"


@pytest.mark.parametrize("reason,found,state", [
    ("probe_not_ok", {"blocked_skip"}, "deviation"), ("insufficient_data", set(), "ok")])
def test_one_skip_is_judged_by_its_class(reason, found, state):
    week = [ev("dream_cycle_completed", f"2026-11-{n:02d}", merged_count=1) for n in range(3, 10)]
    d = doc(week + [ev("dream_cycle_skipped", "2026-11-10", "03:30", reason=reason)])
    assert ids(d) == found and d["state"] == state


def test_outcome_carries_the_class():
    d = doc([ev("dream_cycle_skipped", "2026-11-10", "11:00", reason="scheduler_disabled")])
    assert d["outcome"]["reason"] == "scheduler_disabled" and d["outcome"]["class"] == BLOCKED
    assert doc([])["outcome"] is None


def test_assess_writes_nothing(tmp_path):
    before = sorted(tmp_path.rglob("*"))
    doc(good_week())
    assert sorted(tmp_path.rglob("*")) == before


NO_PROGRESS = [{}, {"merged_count": 0, "dropped_count": 0, "archived_count": 0, "flagged_count": 0},
               {"merged_count": None}, {"merged_count": "3"}, {"merged_count": True},
               {"merged_count": -1}, {"merged_count": 2.5}]


@pytest.mark.parametrize("counts", NO_PROGRESS)
def test_completed_night_without_real_output_is_no_progress(counts):
    night = classify_night(date(2026, 11, 9), datetime(2026, 11, 9, 3, tzinfo=UTC),
                           datetime(2026, 11, 9, 6, tzinfo=UTC),
                           [(datetime(2026, 11, 9, 3, 5, tzinfo=UTC),
                             ev("dream_cycle_completed", "2026-11-09", **counts))])
    assert night["progress"] is False and night["terminal"] == "completed"


@pytest.mark.parametrize("key", dl._OUTPUT_KEYS)
def test_any_output_key_counts_as_progress(key):
    assert doc([ev("dream_cycle_completed", "2026-11-09", **{key: 1})])["progress_window"][
        "completed_with_output"] == 1


@pytest.mark.parametrize("counts", NO_PROGRESS)
def test_seven_completions_without_output_are_a_measured_no_output(counts):
    d = doc([ev("dream_cycle_completed", f"2026-11-{n:02d}", **counts) for n in range(4, 11)])
    found = [f for f in d["findings"] if f["id"] == "no_output"]
    assert found and found[0]["measured"] is True and found[0]["severity"] == "warn"


@pytest.mark.parametrize("etype", ["dream_cycle_failed", "dream_cycle_timeout"])
def test_seven_failing_nights_are_never_ok(etype):
    d = doc([ev(etype, f"2026-11-{n:02d}") for n in range(4, 11)])
    found = [f for f in d["findings"] if f["id"] == "no_output"]
    assert found and found[0]["measured"] is True
    assert d["state"] != "ok" and len([f for f in d["findings"] if f["id"] == "blocked_skip"]) == 7
    assert d["history"][etype.replace("dream_cycle_", "")] == 7


def test_four_of_seven_is_no_output_and_five_is_not():
    four = [ev("dream_cycle_completed", f"2026-11-{n:02d}", merged_count=1) for n in range(4, 8)]
    five = four + [ev("dream_cycle_completed", "2026-11-08", merged_count=1)]
    assert "no_output" in ids(doc(four))
    assert "no_output" not in ids(doc(five + [ev("dream_cycle_completed", n, merged_count=0)
                                              for n in ("2026-11-09", "2026-11-10")]))


def test_review_event_never_counts_as_a_night_attempt():
    d = doc([ev("dream_cycle_approved", "2026-11-09"), ev("dream_cycle_rejected", "2026-11-10", "03:10")])
    assert all(not n["attempt"] for n in d["nights"]) and "no_scheduled_attempt" in ids(d)


def test_started_without_end_in_a_closed_window_and_in_flight_in_an_open_one():
    assert "started_without_end" in ids(doc([ev("dream_cycle_started", "2026-11-09")]))
    flying = doc([ev("dream_cycle_started", "2026-11-10")], now=datetime(2026, 11, 10, 4, 0, tzinfo=UTC))
    assert "started_without_end" not in ids(flying) and len(flying["in_flight"]) == 1
    ended = doc([ev("dream_cycle_started", "2026-11-09", cycle_id="c1"),
                 ev("dream_cycle_completed", "2026-11-09", "03:20", cycle_id="c1")])
    assert "started_without_end" not in ids(ended)


P23 = dict(PLIST, hour=23)


def test_cross_midnight_unmatched_start_belongs_to_the_evening_window():
    start = ev("dream_cycle_started", "2026-11-10", "00:30")
    late = doc([start], plist=P23)
    assert "started_without_end" in ids(late) and late["in_flight"] == []
    flying = doc([start], plist=P23, now=datetime(2026, 11, 10, 1, 30, tzinfo=UTC))
    assert "started_without_end" not in ids(flying) and len(flying["in_flight"]) == 1


def test_start_outside_every_window_keeps_the_calendar_day_window():
    start = ev("dream_cycle_started", "2026-11-10", "10:00")
    same_day = doc([start], plist=P23)
    assert "started_without_end" not in ids(same_day) and len(same_day["in_flight"]) == 1
    after = doc([start], plist=P23, now=datetime(2026, 11, 11, 3, 0, tzinfo=UTC))
    assert "started_without_end" in ids(after) and after["in_flight"] == []


def test_failure_outside_the_window_is_history_only():
    d = doc(good_week() + [ev("dream_cycle_failed", "2026-11-09", "09:00")])
    assert d["history"]["failed"] == 1 and "blocked_skip" not in ids(d)


def test_failure_inside_the_window_is_blocked():
    d = doc(good_week() + [ev("dream_cycle_timeout", "2026-11-09", "03:40")])
    assert "blocked_skip" in ids(d)
    assert [n["class"] for n in d["nights"] if n["night"] == "2026-11-09"] == [BLOCKED]


def _night(d, day):
    return [n for n in d["nights"] if n["night"] == day][0]


@pytest.mark.parametrize("etype", ["dream_cycle_failed", "dream_cycle_timeout"])
def test_in_window_failure_blocks_the_night_even_before_a_good_completion(etype):
    d = doc(good_week() + [ev(etype, "2026-11-09", "03:01")])
    n = _night(d, "2026-11-09")
    assert n["class"] == BLOCKED and n["progress"] is False
    assert "blocked_skip" in ids(d) and d["state"] != "ok"


def test_timeout_at_0340_leaves_no_progress():
    d = doc(good_week() + [ev("dream_cycle_timeout", "2026-11-09", "03:40")])
    assert _night(d, "2026-11-09")["progress"] is False
    assert d["progress_window"]["completed_with_output"] == 6


@pytest.mark.parametrize("etype", ["dream_cycle_failed", "dream_cycle_timeout"])
def test_seven_blocked_nights_with_completions_are_a_measured_no_output(etype):
    events = []
    for n in range(4, 11):
        day = f"2026-11-{n:02d}"
        events += [ev(etype, day, "03:01"), ev("dream_cycle_completed", day, "03:30", merged_count=2)]
    d = doc(events)
    found = [f for f in d["findings"] if f["id"] == "no_output"]
    assert found and found[0]["measured"] is True
    assert d["progress_window"]["completed_with_output"] == 0


def test_failure_before_the_window_start_does_not_block_the_night():
    d = doc(good_week() + [ev("dream_cycle_failed", "2026-11-09", "02:59")])
    n = _night(d, "2026-11-09")
    assert n["class"] is None and n["progress"] is True and "blocked_skip" not in ids(d)


def test_blocked_night_is_one_finding_and_the_newest_is_not_reported_twice():
    skips = [ev("dream_cycle_skipped", f"2026-11-{d:02d}", reason="insufficient_data") for d in (6, 7, 8)]
    d = doc(skips + [ev("dream_cycle_skipped", "2026-11-10", reason="probe_not_ok")])
    assert d["progress_window"]["allowed_skip_streak_max"] == 3
    assert {"allowed_skip_streak", "no_output"} <= ids(d)
    assert [f["id"] for f in d["findings"]].count("blocked_skip") == 1


def test_two_allowed_skips_in_a_row_are_tolerated():
    skips = [ev("dream_cycle_skipped", f"2026-11-{n:02d}", reason="insufficient_data") for n in (9, 10)]
    d = doc([ev("dream_cycle_completed", f"2026-11-0{n}", merged_count=1) for n in range(4, 9)] + skips)
    assert d["state"] == "ok", d["findings"]


def test_load_sources_data_root_follows_project_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    folder = tmp_path / ".vnx-data" / "p1" / "events" / "dream"
    folder.mkdir(parents=True)
    (folder / "a.ndjson").write_text(json.dumps(ev("dream_cycle_started", "2026-11-10")) + "\n")
    got = dl.load_sources("p1", plist_path=tmp_path / "none.plist")
    assert len(got["events"]) == 1 and got["plist"] is None
