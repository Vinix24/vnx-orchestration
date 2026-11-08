#!/usr/bin/env python3
"""Tests for `backfill_pr_merged_receipts.py --report-only` (deur D7a).

The audit compares merged PRs with both pr_merged ledgers, proves its window by
pagination, tells door evidence from backfill, and keeps three outcomes apart:
exit 0 (complete read, no gaps), exit 1 (gaps), exit 2 (unmeasurable).
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import backfill_pr_merged_receipts as bf

NOW = datetime.datetime(2026, 11, 8, 12, 0, tzinfo=datetime.timezone.utc)
SINCE = "2026-11-01"


def _ts(days_ago: float) -> str:
    return (NOW - datetime.timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _pr(number: int, merged_days_ago, updated_days_ago=None, title="") -> Dict[str, Any]:
    merged = None if merged_days_ago is None else _ts(merged_days_ago)
    updated = _ts(merged_days_ago if updated_days_ago is None else updated_days_ago)
    return {"number": number, "title": title or f"PR {number}", "merged_at": merged,
            "updated_at": updated, "head": {"ref": f"feat/{number}"}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    state = data / "state"
    events = data / "events"
    state.mkdir(parents=True)
    events.mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    receipts = state / "t0_receipts.ndjson"
    receipts.write_text("")
    return {"data": data, "state": state, "receipts": receipts, "events": events / "pr_merged.ndjson"}


def _write(path: Path, *records: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _door(number: int) -> Dict[str, Any]:
    return {"event_type": "pr_merged", "source": "pr_merge", "pr_number": number}


def _backfill(number: int) -> Dict[str, Any]:
    return {"event_type": "pr_merged", "source": "backfill_pr_merged_receipts",
            "pr_number": number, "backfilled": True}


def _serve(monkeypatch, pages: Dict[int, List[Dict[str, Any]]]):
    """Stub gh: page N returns pages[N]; asking for an unknown page is a test bug."""
    calls: List[int] = []

    def fake(page: int):
        calls.append(page)
        return pages[page]

    monkeypatch.setattr(bf, "_gh_api_pulls_page", fake)
    return calls


@pytest.fixture(autouse=True)
def _no_real_writes(monkeypatch):
    emitted: List[Any] = []
    monkeypatch.setattr(bf, "_emit_backfill_receipt", lambda *a, **k: emitted.append((a, k)))
    return emitted


# --- the red-run contract: exit 1 names the gap, exit 2 means unmeasurable -------------

def test_gap_is_exit_1_names_the_pr_and_two_means_unmeasurable(env, monkeypatch, capsys):
    _write(env["receipts"], _door(10))
    _serve(monkeypatch, {1: [_pr(10, 2), _pr(11, 3, title="the missing one")]})
    code = bf.main(["--report-only", "--since", SINCE, "--no-write"])
    out = capsys.readouterr().out
    assert code == 1
    assert "PR #11" in out and "the missing one" in out
    assert "1 van 2" in out

    def boom(page):
        raise bf.AuditUnmeasurable("gh: not authenticated")

    monkeypatch.setattr(bf, "_gh_api_pulls_page", boom)
    code = bf.main(["--report-only", "--since", SINCE, "--no-write"])
    out = capsys.readouterr().out
    assert code == 2
    assert "UNMEASURABLE" in out and "not authenticated" in out
    assert "unrecognized" not in out


# --- window: pagination stop condition ------------------------------------------------

def test_stop_condition_catches_merge_on_page_two_by_updated_at(env, monkeypatch):
    # PR 7 was created long before the window but merged inside it: only the
    # updated_at order puts it on page 2 behind 100 fresher rows.
    page1 = [_pr(1000 + i, 1.0) for i in range(100)]
    page2 = [_pr(7, 3.0, updated_days_ago=3.0), _pr(6, 30.0)]
    calls = _serve(monkeypatch, {1: page1, 2: page2})
    merged = bf.list_merged_in_window(bf._audit_since(SINCE))
    numbers = {p["number"] for p in merged}
    assert 7 in numbers and 6 not in numbers
    assert len(numbers) == 101
    assert calls == [1, 2]


def test_stop_condition_stops_before_reading_older_pages(env, monkeypatch):
    page1 = [_pr(2000 + i, 1.0) for i in range(99)] + [_pr(5, 40.0)]
    calls = _serve(monkeypatch, {1: page1})
    merged = bf.list_merged_in_window(bf._audit_since(SINCE))
    assert calls == [1]
    assert {p["number"] for p in merged} == {2000 + i for i in range(99)}


def test_unmerged_closed_prs_are_not_merges(env, monkeypatch):
    _serve(monkeypatch, {1: [_pr(3, None, updated_days_ago=1.0)]})
    assert bf.list_merged_in_window(bf._audit_since(SINCE)) == []


def test_page_limit_is_unknown_exit_2(env, monkeypatch, capsys):
    pages = {n: [_pr(n * 1000 + i, 1.0) for i in range(100)] for n in range(1, bf.AUDIT_PAGE_LIMIT + 1)}
    calls = _serve(monkeypatch, pages)
    code = bf.main(["--report-only", "--since", SINCE, "--no-write"])
    assert code == 2
    assert "page limit" in capsys.readouterr().out
    assert calls[-1] == bf.AUDIT_PAGE_LIMIT


@pytest.mark.parametrize("bad_row", [
    "not-a-dict",
    {"number": "12", "updated_at": _ts(1), "merged_at": _ts(1)},
    {"number": 12, "merged_at": _ts(1)},
    {"number": 12, "updated_at": "garbage", "merged_at": _ts(1)},
    {"number": 12, "updated_at": _ts(1), "merged_at": 5},
    {"number": True, "updated_at": _ts(1), "merged_at": _ts(1)},
])
def test_malformed_page_row_is_exit_2(env, monkeypatch, bad_row):
    _serve(monkeypatch, {1: [bad_row]})
    assert bf.main(["--report-only", "--since", SINCE, "--no-write"]) == 2


def test_page_that_is_not_a_list_is_exit_2(env, monkeypatch):
    _serve(monkeypatch, {1: {"message": "rate limited"}})
    assert bf.main(["--report-only", "--since", SINCE, "--no-write"]) == 2


def test_bad_since_is_exit_2(env, monkeypatch):
    _serve(monkeypatch, {1: []})
    assert bf.main(["--report-only", "--since", "yesterday", "--no-write"]) == 2


def test_default_window_is_seven_days(env):
    delta = datetime.datetime.now(datetime.timezone.utc) - bf._audit_since(None)
    assert abs(delta - datetime.timedelta(days=7)) < datetime.timedelta(minutes=1)


# --- gh failures are exit 2, never 0 ---------------------------------------------------

class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.mark.parametrize("behaviour", ["missing", "rc", "json", "timeout"])
def test_gh_failure_is_exit_2(env, monkeypatch, capsys, behaviour):
    def fake_run(*a, **k):
        if behaviour == "missing":
            raise FileNotFoundError("gh")
        if behaviour == "timeout":
            raise subprocess.TimeoutExpired("gh", 60)
        if behaviour == "rc":
            return _Done(rc=1, err="HTTP 403 rate limit")
        return _Done(out="{not json")

    monkeypatch.setattr(bf.subprocess, "run", fake_run)
    code = bf.main(["--report-only", "--since", SINCE, "--no-write"])
    assert code == 2
    assert "UNMEASURABLE" in capsys.readouterr().out


def test_gh_is_called_with_the_updated_desc_endpoint(env, monkeypatch):
    seen: List[List[str]] = []

    def fake_run(args, **k):
        seen.append(args)
        return _Done(out="[]")

    monkeypatch.setattr(bf.subprocess, "run", fake_run)
    assert bf.main(["--report-only", "--since", SINCE, "--no-write"]) == 0
    assert seen[0][:2] == ["gh", "api"]
    assert "sort=updated&direction=desc&per_page=100&page=1" in seen[0][2]
    assert "state=closed" in seen[0][2]


# --- provenance ---------------------------------------------------------------------

def test_backfilled_line_is_not_door_evidence(env, monkeypatch):
    _write(env["receipts"], _door(1))
    _write(env["events"], _backfill(2))
    _serve(monkeypatch, {1: [_pr(1, 1), _pr(2, 2), _pr(3, 3)]})
    report = bf.run_report_only(since=SINCE)
    assert [p["pr_number"] for p in report["door"]] == [1]
    assert [p["pr_number"] for p in report["backfilled"]] == [2]
    assert [p["pr_number"] for p in report["missing"]] == [3]
    assert report["exit_code"] == 1


@pytest.mark.parametrize("rec", [
    {"event_type": "pr_merged", "source": "pr_merge", "pr_number": 4, "backfilled": True},
    {"event_type": "pr_merged", "source": "pr_merge", "pr_number": 4, "backfilled": "false"},
    {"event_type": "pr_merged", "source": "pr_merge", "pr_number": 4, "backfilled": None},
    {"event_type": "pr_merged", "source": "something_else", "pr_number": 4},
    {"event_type": "pr_merged", "pr_number": 4},
])
def test_door_needs_exact_source_and_no_backfill_marker(env, monkeypatch, rec):
    _write(env["receipts"], rec)
    _serve(monkeypatch, {1: [_pr(4, 1)]})
    report = bf.run_report_only(since=SINCE)
    assert report["door"] == [] and len(report["backfilled"]) == 1


def test_door_beats_backfill_for_the_same_pr(env, monkeypatch):
    _write(env["receipts"], _door(5))
    _write(env["events"], _backfill(5))
    _serve(monkeypatch, {1: [_pr(5, 1)]})
    report = bf.run_report_only(since=SINCE)
    assert len(report["door"]) == 1 and report["backfilled"] == []


def test_both_ledger_files_are_read(env, monkeypatch):
    _write(env["receipts"], _door(1))
    _write(env["events"], _door(2))
    _serve(monkeypatch, {1: [_pr(1, 1), _pr(2, 1)]})
    report = bf.run_report_only(since=SINCE)
    assert len(report["door"]) == 2 and report["missing"] == []
    assert len(report["ledgers"]) == 2


def test_other_event_types_do_not_count(env, monkeypatch):
    _write(env["receipts"], {"event_type": "pr_merge_refused", "source": "pr_merge", "pr_number": 8})
    _serve(monkeypatch, {1: [_pr(8, 1)]})
    assert bf.run_report_only(since=SINCE)["exit_code"] == 1


def test_missing_receipts_ledger_is_exit_2(env, monkeypatch):
    env["receipts"].unlink()
    _serve(monkeypatch, {1: [_pr(1, 1)]})
    assert bf.run_report_only(since=SINCE)["exit_code"] == 2


def test_unreadable_ledger_is_exit_2(env, monkeypatch):
    env["receipts"].write_bytes(b"\xff\xfe\x00 not utf8 \xff")
    _serve(monkeypatch, {1: [_pr(1, 1)]})
    report = bf.run_report_only(since=SINCE)
    assert report["exit_code"] == 2 and "unreadable" in report["reason"]


def test_malformed_ledger_line_never_vouches_for_a_pr(env, monkeypatch):
    env["receipts"].write_text("{broken\n" + json.dumps(_door(1)) + "\n")
    _serve(monkeypatch, {1: [_pr(1, 1), _pr(2, 1)]})
    report = bf.run_report_only(since=SINCE)
    assert report["malformed_ledger_lines"] == 1
    assert [p["pr_number"] for p in report["missing"]] == [2]


# --- three outcomes ------------------------------------------------------------------

def test_zero_of_zero_is_exit_0(env, monkeypatch, capsys):
    _serve(monkeypatch, {1: []})
    code = bf.main(["--report-only", "--since", SINCE, "--no-write"])
    assert code == 0
    assert "0 van 0" in capsys.readouterr().out


def test_all_covered_is_exit_0(env, monkeypatch, capsys):
    _write(env["receipts"], _door(1))
    _write(env["events"], _backfill(2))
    _serve(monkeypatch, {1: [_pr(1, 1), _pr(2, 1)]})
    assert bf.main(["--report-only", "--since", SINCE, "--no-write", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["outcome"] == "clean" and data["total"] == 2
    assert {"door", "backfilled", "missing"} <= set(data)


def test_json_output_for_gaps(env, monkeypatch, capsys):
    _serve(monkeypatch, {1: [_pr(9, 1)]})
    assert bf.main(["--report-only", "--since", SINCE, "--no-write", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["outcome"] == "gaps" and data["missing"][0]["pr_number"] == 9


# --- never heals ---------------------------------------------------------------------

def test_report_only_never_reaches_emit_backfill_receipt(env, monkeypatch, _no_real_writes):
    _serve(monkeypatch, {1: [_pr(9, 1)]})
    monkeypatch.setattr(bf, "backfill", lambda **k: pytest.fail("backfill() must not run"))
    bf.main(["--report-only", "--since", SINCE, "--no-write"])
    bf.main(["--report-only", "--since", SINCE])
    assert _no_real_writes == []
    assert not env["events"].exists()


# --- open items and beacon -----------------------------------------------------------

@pytest.fixture()
def items_store(tmp_path, monkeypatch):
    import open_items_manager as oim
    state = tmp_path / "oi_state"
    state.mkdir()
    monkeypatch.setattr(oim, "STATE_DIR", state)
    monkeypatch.setattr(oim, "OPEN_ITEMS_FILE", state / "open_items.json")
    monkeypatch.setattr(oim, "DIGEST_FILE", state / "open_items_digest.json")
    monkeypatch.setattr(oim, "MARKDOWN_FILE", state / "open_items.md")
    monkeypatch.setattr(oim, "AUDIT_LOG", state / "open_items_audit.jsonl")
    return oim


def test_same_gap_twice_is_one_open_item(env, monkeypatch, items_store):
    _serve(monkeypatch, {1: [_pr(9, 1)]})
    assert bf.main(["--report-only", "--since", SINCE]) == 1
    assert bf.main(["--report-only", "--since", SINCE]) == 1
    items = items_store.load_items()["items"]
    keys = [i.get("dedup_key") for i in items]
    assert len(items) == 1
    assert keys[0].startswith("door-audit:") and keys[0].endswith(":9")


def test_one_open_item_per_missing_pr(env, monkeypatch, items_store):
    _serve(monkeypatch, {1: [_pr(9, 1), _pr(10, 1)]})
    bf.main(["--report-only", "--since", SINCE])
    assert len(items_store.load_items()["items"]) == 2


def test_beacon_written_per_outcome(env, monkeypatch, items_store):
    beacon = env["data"] / "health" / "door_audit.json"
    _serve(monkeypatch, {1: []})
    assert bf.main(["--report-only", "--since", SINCE]) == 0
    assert json.loads(beacon.read_text())["status"] == "ok"
    _serve(monkeypatch, {1: [_pr(9, 1)]})
    assert bf.main(["--report-only", "--since", SINCE]) == 1
    payload = json.loads(beacon.read_text())
    assert payload["status"] == "fail" and payload["details"]["missing"] == [9]


def test_no_write_leaves_no_trace(env, monkeypatch, items_store):
    _serve(monkeypatch, {1: [_pr(9, 1)]})
    assert bf.main(["--report-only", "--since", SINCE, "--no-write"]) == 1
    assert not (env["data"] / "health").exists()
    assert items_store.load_items()["items"] == []


def test_unknown_outcome_writes_failing_beacon(env, monkeypatch, items_store):
    def boom(page):
        raise bf.AuditUnmeasurable("rate limited")

    monkeypatch.setattr(bf, "_gh_api_pulls_page", boom)
    assert bf.main(["--report-only", "--since", SINCE]) == 2
    payload = json.loads((env["data"] / "health" / "door_audit.json").read_text())
    assert payload["status"] == "fail" and payload["details"]["outcome"] == "unknown"


def test_open_item_failure_keeps_gap_exit_1(env, monkeypatch, capsys):
    def broken(pr, project_id):
        raise OSError("disk full")

    monkeypatch.setattr(bf, "_file_open_item", broken)
    _serve(monkeypatch, {1: [_pr(9, 1)]})
    assert bf.main(["--report-only", "--since", SINCE, "--json"]) == 1
    assert "disk full" in json.loads(capsys.readouterr().out)["write_errors"][0]


def test_beacon_failure_on_clean_read_is_exit_2(env, monkeypatch):
    import health_beacon

    def broken(self, *a, **k):
        raise OSError("read-only fs")

    monkeypatch.setattr(health_beacon.HealthBeacon, "heartbeat_strict", broken)
    _serve(monkeypatch, {1: []})
    assert bf.main(["--report-only", "--since", SINCE]) == 2
