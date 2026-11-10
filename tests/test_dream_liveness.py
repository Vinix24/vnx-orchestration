"""Tests for scripts/lib/dream_liveness.py (ADR-066, dream PR-2).

Every branch that could turn a silence into a PASS is pinned with the field
missing, None, malformed and unknown: all four stay blocking.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dream_liveness as dl
from dream_liveness import ALLOWED, BLOCKED, SEVERITY, assess, classify_skip

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
    return assess({"events": events, "plist": plist}, project_id="vnx-dev", now=now)


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
              "no_attempt_26h", "started_without_end"}
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
    d = doc([ev("dream_cycle_skipped", "2026-11-10", "11:00", reason=reason)])
    assert ids(d) == found and d["state"] == state


def test_outcome_carries_the_class():
    d = doc([ev("dream_cycle_skipped", "2026-11-10", "11:00", reason="scheduler_disabled")])
    assert d["outcome"]["reason"] == "scheduler_disabled" and d["outcome"]["class"] == BLOCKED
    assert doc([])["outcome"] is None


def test_assess_writes_nothing(tmp_path):
    before = sorted(tmp_path.rglob("*"))
    doc(good_week())
    assert sorted(tmp_path.rglob("*")) == before
