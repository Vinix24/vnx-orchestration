"""Tests for scripts/lib/dream_liveness.py (ADR-066, dream PR-2).

Every branch that could turn a silence into a PASS is pinned with the field
missing, None, malformed and unknown: all four stay blocking.
"""
import json
import plistlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dream_liveness as dl
from dream_liveness import ALLOWED, BLOCKED, SEVERITY, assess, classify_night, classify_skip

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PLIST = {"path": "p", "label": "com.vnx.auto-dream", "project_id": "vnx-dev",
         "hour": 3, "minute": 0, "arm_switch": "1"}
P23 = dict(PLIST, hour=23)
OPEN_NOW, MIDNIGHT_OPEN = datetime(2026, 11, 10, 4, 0, tzinfo=UTC), datetime(2026, 11, 10, 1, 30, tzinfo=UTC)
SKIP, OUTSIDE = "dream_cycle_skipped", "outside every scheduled window"


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


def hits(document, fid="blocked_skip"):
    return [f for f in document["findings"] if f["id"] == fid]


def night(document, day):
    return [n for n in document["nights"] if n["night"] == day][0]


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
    errors.add("job_not_loaded")
    warns = {"arm_switch_off", "undated_event", "allowed_skip_streak", "no_output", "review_overdue",
             "template_drift", "log_missing", "log_unreadable"}
    assert {k for k, v in SEVERITY.items() if v == "error"} == errors
    assert warns <= {k for k, v in SEVERITY.items() if v == "warn"}
    assert set(SEVERITY.values()) == {"error", "warn"}


def test_warn_only_document_is_ok():
    d = doc(good_week(), plist=dict(PLIST, arm_switch="0"))
    assert [f["id"] for f in d["findings"]] == ["arm_switch_off"] and d["state"] == "ok"


@pytest.mark.parametrize("reason,found,state", [
    ("probe_not_ok", {"blocked_skip"}, "deviation"), ("insufficient_data", set(), "ok")])
def test_one_skip_is_judged_by_its_class(reason, found, state):
    d = doc(good_week()[:-1] + [ev("dream_cycle_skipped", "2026-11-10", "03:30", reason=reason)])
    assert ids(d) == found and d["state"] == state


def test_outcome_carries_the_class():
    d = doc([ev("dream_cycle_skipped", "2026-11-10", "11:00", reason="scheduler_disabled")])
    assert d["outcome"]["reason"] == "scheduler_disabled" and d["outcome"]["class"] == BLOCKED
    assert doc([])["outcome"] is None


def test_assess_writes_nothing(tmp_path):
    before = sorted(tmp_path.rglob("*"))
    doc(good_week())
    assert sorted(tmp_path.rglob("*")) == before


NO_PROGRESS = [{}, dict.fromkeys(dl._OUTPUT_KEYS, 0), {"merged_count": None}, {"merged_count": "3"},
               {"merged_count": True}, {"merged_count": -1}, {"merged_count": 2.5}]


@pytest.mark.parametrize("counts", NO_PROGRESS)
def test_completed_night_without_real_output_is_no_progress(counts):
    at = datetime(2026, 11, 9, 3, 5, tzinfo=UTC)
    got = classify_night(at.date(), at, at + timedelta(hours=3), [(at, ev("dream_cycle_completed", "2026-11-09", **counts))])
    assert got["progress"] is False and got["terminal"] == "completed"


@pytest.mark.parametrize("key", dl._OUTPUT_KEYS)
def test_any_output_key_counts_as_progress(key):
    assert doc([ev("dream_cycle_completed", "2026-11-09", **{key: 1})])["progress_window"]["completed_with_output"] == 1


@pytest.mark.parametrize("counts", NO_PROGRESS)
def test_seven_completions_without_output_are_a_measured_no_output(counts):
    found = hits(doc([ev("dream_cycle_completed", f"2026-11-{n:02d}", **counts) for n in range(4, 11)]), "no_output")
    assert found and found[0]["measured"] is True and found[0]["severity"] == "warn"


@pytest.mark.parametrize("etype", ["dream_cycle_failed", "dream_cycle_timeout"])
def test_seven_failing_nights_are_never_ok(etype):
    d = doc([ev(etype, f"2026-11-{n:02d}") for n in range(4, 11)])
    assert [f["measured"] for f in hits(d, "no_output")] == [True] and len(hits(d)) == 7
    assert d["state"] != "ok" and d["history"][etype.replace("dream_cycle_", "")] == 7


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
    flying = doc([ev("dream_cycle_started", "2026-11-10")], now=OPEN_NOW)
    assert "started_without_end" not in ids(flying) and len(flying["in_flight"]) == 1
    ended = doc([ev("dream_cycle_started", "2026-11-09", cycle_id="c1"),
                 ev("dream_cycle_completed", "2026-11-09", "03:20", cycle_id="c1")])
    assert "started_without_end" not in ids(ended)


def test_cross_midnight_unmatched_start_belongs_to_the_evening_window():
    start = ev("dream_cycle_started", "2026-11-10", "00:30")
    late = doc([start], plist=P23)
    assert "started_without_end" in ids(late) and late["in_flight"] == []
    flying = doc([start], plist=P23, now=MIDNIGHT_OPEN)
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


@pytest.mark.parametrize("etype", ["dream_cycle_failed", "dream_cycle_timeout"])
@pytest.mark.parametrize("hhmm", ["03:01", "03:40"])
def test_in_window_failure_blocks_the_night_before_or_after_a_good_completion(etype, hhmm):
    d = doc(good_week() + [ev(etype, "2026-11-09", hhmm)])
    assert night(d, "2026-11-09")["class"] == BLOCKED and night(d, "2026-11-09")["progress"] is False
    assert "blocked_skip" in ids(d) and d["state"] != "ok" and d["progress_window"]["completed_with_output"] == 6


@pytest.mark.parametrize("etype", ["dream_cycle_failed", "dream_cycle_timeout"])
def test_seven_blocked_nights_with_completions_are_a_measured_no_output(etype):
    days = [f"2026-11-{n:02d}" for n in range(4, 11)]
    d = doc([ev(etype, x, "03:01") for x in days] + [ev("dream_cycle_completed", x, "03:30", merged_count=2) for x in days])
    assert [f["measured"] for f in hits(d, "no_output")] == [True] and d["progress_window"]["completed_with_output"] == 0


def test_failure_before_the_window_start_does_not_block_the_night():
    d = doc(good_week() + [ev("dream_cycle_failed", "2026-11-09", "02:59")])
    n = night(d, "2026-11-09")
    assert n["class"] is None and n["progress"] is True and "blocked_skip" not in ids(d)


def test_blocked_night_is_one_finding_and_the_newest_is_not_reported_twice():
    skips = [ev("dream_cycle_skipped", f"2026-11-{d:02d}", reason="insufficient_data") for d in (6, 7, 8)]
    d = doc(skips + [ev("dream_cycle_skipped", "2026-11-10", reason="probe_not_ok")])
    assert d["progress_window"]["allowed_skip_streak_max"] == 3
    assert {"allowed_skip_streak", "no_output"} <= ids(d) and len(hits(d)) == 1


def test_two_allowed_skips_in_a_row_are_tolerated():
    skips = [ev("dream_cycle_skipped", f"2026-11-{n:02d}", reason="insufficient_data") for n in (9, 10)]
    d = doc([ev("dream_cycle_completed", f"2026-11-0{n}", merged_count=1) for n in range(4, 9)] + skips)
    assert d["state"] == "ok", d["findings"]


@pytest.mark.parametrize("pid,plist_id,root", [
    (None, None, "p1"), ("../../etc", dl.PLIST_UNREADABLE, "p1"), (7, dl.PLIST_UNREADABLE, "p1"),
    ("", dl.PLIST_UNREADABLE, "p1"), ("vnx-dev", "vnx-dev", "vnx-dev")],
    ids=["no-plist", "traversal", "integer", "empty", "valid"])
def test_a_plist_project_id_is_used_only_when_vnx_paths_accepts_it(tmp_path, monkeypatch, pid, plist_id, root):
    monkeypatch.setenv("HOME", str(tmp_path))
    folder, plist, seen = tmp_path / ".vnx-data" / root / "events" / "dream", tmp_path / "p.plist", []
    folder.mkdir(parents=True)
    (folder / "a.ndjson").write_text(json.dumps(ev("dream_cycle_started", "2026-11-10")) + "\n")
    if pid is not None:
        plist.write_bytes(plistlib.dumps({"ProgramArguments": ["dream", "--project-id", pid]}))
    got = dl.read_plist(plist)
    assert (got["project_id"] if isinstance(got, dict) else got) == plist_id, got
    monkeypatch.setattr(dl, "read_events", lambda path, real=dl.read_events: seen.append(path) or real(path))
    src = dl.load_sources("p1", plist_path=plist)
    assert seen == [folder] and len(src["events"]) == 1 and src["project_id"] == root
    pytest.raises(ValueError, dl.load_sources, "../p1", plist_path=tmp_path / "none.plist")


@pytest.mark.parametrize("extra,expected", [
    (ev(SKIP, "2026-11-10", reason="probe_not_ok"), ["night 2026-11-10: probe_not_ok"]),
    (ev("dream_cycle_timeout", "2026-11-10", "03:40"), ["night 2026-11-10: timeout"]),
    (ev("dream_cycle_completed", "2026-11-10", merged_count=1), [])], ids=["skip", "timeout", "completed"])
def test_the_open_window_reports_a_blocked_attempt_at_once_and_nothing_else(extra, expected):
    d = doc(good_week()[:-1] + [extra], now=OPEN_NOW)
    assert [f["detail"] for f in d["findings"]] == expected and d["state"] == ("deviation" if expected else "ok")


def test_open_window_event_leaves_nights_and_progress_window_unchanged():
    base = doc(good_week()[:-1], now=OPEN_NOW)
    blocked = doc(good_week()[:-1] + [ev("dream_cycle_failed", "2026-11-10", "03:10")], now=OPEN_NOW)
    assert (blocked["progress_window"], blocked["nights"]) == (base["progress_window"], base["nights"])
    assert "2026-11-10" not in [n["night"] for n in blocked["nights"]]


def test_open_night_is_reported_exactly_once_after_the_window_closes():
    events = good_week()[:-1] + [ev(SKIP, "2026-11-10", reason="probe_not_ok")]
    assert len(hits(doc(events, now=OPEN_NOW))) == len(hits(doc(events, now=NOW))) == 1


@pytest.mark.parametrize("extra,plist,now,expected", [
    ([ev(SKIP, "2026-11-09", "03:30", reason="probe_not_ok")], PLIST, NOW, ["night 2026-11-09: probe_not_ok"]),
    ([ev(SKIP, "2026-11-10", reason="probe_not_ok")], PLIST, OPEN_NOW, ["night 2026-11-10: probe_not_ok"]),
    ([ev(SKIP, "2026-11-10", "09:00", reason="probe_not_ok")], PLIST, NOW,
     [f"2026-11-10T09:00:00+00:00: probe_not_ok {OUTSIDE}"]),
    ([ev(SKIP, "2026-11-09", "03:10", reason="probe_not_ok"), ev("dream_cycle_failed", "2026-11-09", "03:20")],
     PLIST, NOW, ["night 2026-11-09: failed"]),
    ([ev(SKIP, "2026-11-10", "08:00", reason="scheduler_disabled"),
      ev(SKIP, "2026-11-10", "09:00", reason="provider_switched_off")], PLIST, NOW,
     [f"2026-11-10T08:00:00+00:00: scheduler_disabled {OUTSIDE}",
      f"2026-11-10T09:00:00+00:00: provider_switched_off {OUTSIDE}"]),
    ([ev(SKIP, "2026-11-09", "03:30", reason="probe_not_ok"), ev("dream_cycle_timeout", "2026-11-10", "03:40")],
     PLIST, OPEN_NOW, ["night 2026-11-09: probe_not_ok", "night 2026-11-10: timeout"]),
    ([ev(SKIP, "2026-11-09", "03:01", reason="probe_not_ok")], PLIST, NOW, ["night 2026-11-09: probe_not_ok"]),
    ([ev(SKIP, "2026-11-10", "09:00", reason="nonsense")], PLIST, NOW, [f"2026-11-10T09:00:00+00:00: nonsense {OUTSIDE}"]),
    ([ev(SKIP, "2026-11-10", "09:00", reason="insufficient_data")], PLIST, NOW, []),
    ([ev(SKIP, "2026-10-01", "09:00", reason="probe_not_ok")], PLIST, NOW, []),
    ([ev(SKIP, "2026-11-10", "00:30", reason="probe_not_ok")], P23, NOW, ["night 2026-11-09: probe_not_ok"]),
    ([ev(SKIP, "2026-11-10", "00:30", reason="probe_not_ok")], P23, MIDNIGHT_OPEN, ["night 2026-11-09: probe_not_ok"]),
], ids=["closed-night", "open-window", "outside", "two-in-one-night", "two-outside", "closed-plus-open",
        "blocked-then-completed", "unknown-outside", "allowed-outside", "before-horizon", "cross-midnight-closed",
        "cross-midnight-open"])
def test_every_blocked_attempt_is_reported_exactly_once(extra, plist, now, expected):
    d = doc((good_week()[:-1] if now == OPEN_NOW else good_week()) + extra, plist=plist, now=now)
    assert [f["detail"] for f in hits(d)] == expected
    assert d["state"] == ("deviation" if expected else "ok"), d["findings"]


@pytest.mark.parametrize("plist,state", [(None, "missing"), (dl.PLIST_UNREADABLE, "unreadable"),
                                         ({"hour": 25}, "unreadable")])
def test_blocked_skip_without_a_usable_plist_is_a_measured_finding(plist, state):
    d = doc([ev(SKIP, "2026-11-10", "10:00", reason="probe_not_ok")], plist=plist)
    assert d["state"] == "deviation" and len(hits(d)) == 1 and d["nights"] == [] and d["progress_window"] is None
    assert f"plist {state}" in hits(d)[0]["detail"] and "2026-11-10T10:00:00+00:00: probe_not_ok" in hits(d)[0]["detail"]


def test_without_a_plist_failures_and_skips_before_the_horizon_stay_history():
    old = ev(SKIP, "2026-11-03", "11:59", reason="probe_not_ok")
    d = doc([old, ev("dream_cycle_failed", "2026-11-10", "10:00")], plist=None)
    assert hits(d) == [] and d["history"]["failed"] == 1 and d["state"] == "unmeasured"
    edge = hits(doc([old, ev(SKIP, "2026-11-03", "12:00", reason="nonsense"),
                     ev(SKIP, "2026-11-09", "09:00", reason="scheduler_disabled")], plist=None))
    assert len(edge) == 1 and edge[0]["detail"].endswith(
        "2 blocked skip(s) since 2026-11-03T12:00:00+00:00, newest 2026-11-09T09:00:00+00:00: scheduler_disabled")


@pytest.mark.parametrize("reason", ["probe_not_ok", "insufficient_data"])
def test_skip_outside_every_window_leaves_nights_and_progress_window_unchanged(reason):
    base = doc(good_week())
    d = doc(good_week() + [ev(SKIP, "2026-11-10", "09:00", reason=reason)])
    assert d["nights"] == base["nights"] and d["progress_window"] == base["progress_window"]
    assert d["outcome"]["class"] == classify_skip(reason, None)[0]


@pytest.fixture(autouse=True)
def _no_real_subprocess(monkeypatch):
    """Every plutil and launchctl call goes through the seam; here it answers a loaded job and no plutil."""
    calls = []

    def fake(argv, timeout=dl.SUBPROCESS_TIMEOUT):
        calls.append(argv)
        if argv[0] == "plutil":
            raise FileNotFoundError("plutil")
        assert argv[:2] == ["launchctl", "print"], argv
        return 0, "state = not running\n\tlast exit code = 0\n"

    monkeypatch.setattr(dl, "_run", fake)
    return calls


def logged(events):
    return {"name": "launchd.out.log", "lines": {e["cycle_id"]: f"launchd.out.log:{i}" for i, e in enumerate(events, 1)}}


UNSET = object()


def full(events, log=UNSET, job=None, plist=PLIST):
    sources = {"events": events, "plist": dict(plist, drift=[]), "log": logged(events) if log is UNSET else log,
               "job": {"loaded": True, "last_exit": 0} if job is None else job}
    return assess(sources, project_id="vnx-dev", now=NOW, tz=UTC)


def test_proven_week_with_loaded_job_and_no_drift_is_ok():
    d = full(good_week())
    assert d["state"] == "ok" and d["findings"] == [] and d["sources"]["log"] == "ok"
    assert d["job"]["loaded"] is True and d["job"]["last_exit"] == 0 and d["attempts"]["newest"]["log"]


def test_loaded_job_without_attempt_is_not_pass():
    d = full([], log={"name": "launchd.out.log", "lines": {}})
    assert d["state"] == "deviation" and {"no_attempt_26h", "no_scheduled_attempt"} <= ids(d)


def test_attempt_without_log_line_is_not_proven():
    started = ev("dream_cycle_started", "2026-11-10", "03:05")
    d = full(good_week()[:-1] + [started], log=logged(good_week()[:-1]))
    assert d["nights"][-1]["attempt"] is False and "no_scheduled_attempt" in ids(d)


def test_review_events_do_not_refresh_attempts():
    old = [ev("dream_cycle_completed", "2026-09-30", merged_count=1)]
    review = ev("dream_cycle_approved", "2026-11-10", "11:00")
    d = full(old + [review], log=logged(old + [review]))
    assert d["attempts"]["age_hours"] > 26 and "no_attempt_26h" in ids(d)
    assert d["attempts"]["newest"]["cycle_id"] == old[0]["cycle_id"]


@pytest.mark.parametrize("log,fid,state", [
    ("missing", "log_missing", "missing"), (dl.LOG_UNREADABLE, "log_unreadable", dl.LOG_UNREADABLE),
    (None, "log_missing", "missing"), ("junk", "log_missing", "missing"), ({"lines": "x"}, "log_missing", "missing")])
def test_log_that_cannot_be_read_proves_nothing(log, fid, state):
    d = full(good_week(), log=log)
    assert fid in ids(d) and d["sources"]["log"] == state and d["state"] != "ok"
    assert "no_attempt_26h" in ids(d) and hits(d, fid)[0]["measured"] is False


@pytest.mark.parametrize("job,measured", [({"loaded": False, "last_exit": None}, True), (None, False),
                                          ({"loaded": None}, True), ({}, True), ("loaded", False)])
def test_job_not_loaded_stays_blocking(job, measured):
    d = full(good_week(), job=job) if job is not None else assess(
        {"events": good_week(), "plist": dict(PLIST, drift=[]), "log": logged(good_week()), "job": None},
        now=NOW, tz=UTC)
    found = hits(d, "job_not_loaded")
    assert len(found) == 1 and found[0]["measured"] is measured and d["state"] != "ok"


@pytest.mark.parametrize("drift", [["Label"], ["StandardOutPath", "StartCalendarInterval"], None, "Label"])
def test_template_drift_is_a_finding_and_unreadable_is_unmeasured(drift):
    d = assess({"events": good_week(), "plist": dict(PLIST, drift=drift), "log": logged(good_week()),
                "job": {"loaded": True, "last_exit": 0}}, now=NOW, tz=UTC)
    found = hits(d, "template_drift")
    assert len(found) == 1 and found[0]["measured"] is isinstance(drift, list)
    assert d["state"] == ("ok" if isinstance(drift, list) else "unmeasured")  # drift is a warn; unread is unmeasured


def test_the_finding_ids_are_the_closed_adr_set():
    adr = (Path(__file__).resolve().parent.parent / "docs/governance/decisions/ADR-066-dream-liveness-watch.md")
    line = [l for l in adr.read_text(encoding="utf-8").splitlines() if l.startswith("- **`findings`**")][0]
    import re
    named = set(re.findall(r"`([a-z0-9_]+)`", line.split("verzameling ids", 1)[1].split("Een id buiten")[0]))
    assert named - {"severity", "measured", "detail"} == dl.FINDING_IDS and set(SEVERITY) <= dl.FINDING_IDS
    pytest.raises(ValueError, dl._finding, "made_up", "x")


PLUTIL_JSON = json.dumps({"Label": "com.vnx.auto-dream", "ProgramArguments": ["vnx", "dream", "run", "--project-id", "vnx-dev"],
                          "StartCalendarInterval": {"Hour": 3, "Minute": 0}, "StandardOutPath": "/x/launchd.out.log"})


def test_read_plist_prefers_plutil_and_falls_back_to_plistlib(tmp_path, monkeypatch, _no_real_subprocess):
    path = tmp_path / "a.plist"
    path.write_bytes(plistlib.dumps({"Label": "from-plistlib"}))
    assert dl.read_plist(path)["label"] == "from-plistlib"  # fixture: no plutil
    monkeypatch.setattr(dl, "_run", lambda argv, timeout=10: _no_real_subprocess.append(argv) or (0, PLUTIL_JSON))
    got = dl.read_plist(path)
    assert got["label"] == "com.vnx.auto-dream" and got["stdout_path"] == "/x/launchd.out.log"
    assert _no_real_subprocess[-1] == ["plutil", "-convert", "json", "-o", "-", str(path)]


@pytest.mark.parametrize("answer", [(1, ""), (0, "not json"), (0, "[1]"), (0, ""), subprocess.TimeoutExpired("plutil", 10)])
def test_plutil_that_refuses_times_out_or_returns_junk_is_unreadable(tmp_path, monkeypatch, answer):
    path = tmp_path / "a.plist"
    path.write_bytes(plistlib.dumps({"Label": "x"}))
    monkeypatch.setattr(dl, "_run", lambda argv, timeout=10: (_ for _ in ()).throw(answer) if isinstance(answer, Exception) else answer)
    assert dl.read_plist(path) == dl.PLIST_UNREADABLE


@pytest.mark.parametrize("answer,expected", [
    ((0, "x\n\tlast exit code = 78\n"), {"loaded": True, "last_exit": 78}),
    ((113, "Could not find service"), {"loaded": False, "last_exit": None})])
def test_read_job_asks_launchctl_print_for_the_gui_domain(monkeypatch, answer, expected):
    seen = []
    monkeypatch.setattr(dl, "_run", lambda argv, timeout=10: seen.append(argv) or answer)
    assert dl.read_job(uid=501) == expected and seen == [["launchctl", "print", "gui/501/com.vnx.auto-dream"]]


@pytest.mark.parametrize("exc", [FileNotFoundError, OSError, lambda: subprocess.TimeoutExpired("launchctl", 10)])
def test_read_job_that_cannot_ask_is_none(monkeypatch, exc):
    monkeypatch.setattr(dl, "_run", lambda argv, timeout=10: (_ for _ in ()).throw(exc()))
    assert dl.read_job(uid=501) is None


def test_read_log_collects_dream_run_lines(tmp_path):
    log = tmp_path / "launchd.out.log"
    log.write_bytes(b"noise\ndream run: cycle=dream-1 project=vnx-dev\n\xff\xfe garbage\ndream run: cycle=dream-2 project=x\n")
    got = dl.read_log(str(log))
    assert got == {"name": "launchd.out.log", "lines": {"dream-1": "launchd.out.log:2", "dream-2": "launchd.out.log:4"}}
    assert dl.read_log(None) is None and dl.read_log(str(tmp_path / "no.log")) is None


def _installed(tmp_path, **override):
    body = {"Label": "com.vnx.auto-dream", "ProgramArguments": ["/bin/vnx", "dream", "run", "--project-id", "vnx-dev"],
            "StartCalendarInterval": {"Hour": 3, "Minute": 0},
            "StandardOutPath": str(tmp_path / "events" / "dream" / "launchd.out.log")}
    body.update(override)
    path = tmp_path / "p.plist"
    path.write_bytes(plistlib.dumps(body))
    return dl.read_plist(path)


def test_an_installed_plist_equal_to_the_template_has_no_drift(tmp_path):
    assert dl.template_drift(_installed(tmp_path), tmp_path / "events" / "dream" / "launchd.out.log") == []


@pytest.mark.parametrize("override,field", [
    ({"Label": "com.other"}, "Label"),
    ({"ProgramArguments": ["/bin/vnx", "dream", "run"]}, "ProgramArguments"),
    ({"ProgramArguments": ["", "dream", "run", "--project-id", "vnx-dev"]}, "ProgramArguments"),
    ({"StartCalendarInterval": {"Hour": 4, "Minute": 0}}, "StartCalendarInterval"),
    ({"StandardOutPath": "/elsewhere/launchd.out.log"}, "StandardOutPath")])
def test_each_compared_field_reports_drift(tmp_path, override, field):
    got = dl.template_drift(_installed(tmp_path, **override), tmp_path / "events" / "dream" / "launchd.out.log")
    assert got == [field]


def test_a_missing_template_is_unmeasured(tmp_path, monkeypatch):
    plist = _installed(tmp_path)
    monkeypatch.setattr(dl, "_TEMPLATE", tmp_path / "gone.j2")
    assert dl.template_drift(plist, tmp_path / "x" / "launchd.out.log") is None


def test_load_sources_reads_job_log_and_drift_for_an_installed_plist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    plist, root = tmp_path / "p.plist", tmp_path / "data"
    (root / "events" / "dream").mkdir(parents=True)
    log = root / "events" / "dream" / "launchd.out.log"
    log.write_text("dream run: cycle=c1 project=vnx-dev\n")
    plist.write_bytes(plistlib.dumps({"Label": "com.vnx.auto-dream", "StandardOutPath": str(log),
                                      "ProgramArguments": ["vnx", "dream", "run", "--project-id", "vnx-dev"],
                                      "StartCalendarInterval": {"Hour": 3, "Minute": 0}}))
    src = dl.load_sources("vnx-dev", data_root=root, plist_path=plist)
    assert src["plist"]["drift"] == [] and src["job"] == {"loaded": True, "last_exit": 0}
    assert src["log"]["lines"] == {"c1": "launchd.out.log:1"}


def test_the_seam_runs_a_real_subprocess_and_guards_launchctl(monkeypatch):
    monkeypatch.undo()  # the seam itself, not the autouse fake
    guard = []
    monkeypatch.setattr(dl, "refuse_real_launchctl_under_test_runner", lambda: guard.append(1))
    ran = []

    def fake_subprocess(argv, **kw):
        ran.append((argv, kw["timeout"]))
        return type("P", (), {"returncode": 3, "stdout": "out"})()

    monkeypatch.setattr(dl.subprocess, "run", fake_subprocess)
    assert dl._run(["launchctl", "print", "x"], 4) == (3, "out") and guard == [1]
    assert dl._run(["plutil", "-p", "x"]) == (3, "out") and guard == [1]
    assert ran == [(["launchctl", "print", "x"], 4), (["plutil", "-p", "x"], dl.SUBPROCESS_TIMEOUT)]
