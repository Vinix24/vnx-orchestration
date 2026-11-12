"""``report --check`` and ``report --metric`` (doorloop PR-14, ADR-063 besluit 2, 3, 6, 8).

Every run is a ``subprocess`` with ``--store`` under ``tmp_path``, ``--now`` fixed and a fake
``launchctl`` through ``VNX_DAILY_METRICS_LAUNCHCTL``. On ``origin/main`` every test fails.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from daily_metrics.metric import REQUIRED

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "daily_metrics.py"
PID = "p"
NOW = "2026-11-12T09:00:00Z"           # newest settled day: 2026-11-11
NEWEST = date(2026, 11, 11)
WINDOW = [NEWEST - timedelta(days=offset) for offset in range(6, -1, -1)]
OMIT = object()


def _scalar(value=1.0, samples=(0.5, 1.5), reason=None):
    return {"kind": "scalar", "value": value, "source": "t", "baseline_7d": None,
            "unknown_reason": reason, "sample_size": len(samples), "details": {"samples": list(samples)}}


def _doc(day, *, collected=None, backfilled=False, status="final", project_id=PID):
    """A final, fresh day with all ten ids: collected at 03:10Z the next day."""
    metrics = {metric_id: _scalar() for metric_id in REQUIRED}
    metrics["operator_wait_hours"] = _scalar(None, (), "no_request_timestamp_in_approval_ledger")
    metrics["gate_duration_seconds_by_seat"] = {"kind": "keyed", "source": "t", "details": {},
                                                "entries": {"codex_gate": _scalar(300.0, (300.0,))}}
    stamp = collected or f"{(day + timedelta(days=1)).isoformat()}T03:10:00Z"
    return {"schema": 1, "project_id": project_id, "day": day.isoformat(), "collected_at": stamp,
            "backfilled": backfilled, "status": status, "metrics": metrics, "details": {}}


def _docs(days=WINDOW):
    return {day: _doc(day) for day in days}


def _write(tmp_path, docs):
    """One base file per day key; a field set to ``OMIT`` is left out of the file."""
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    for day, doc in docs.items():
        body = {name: value for name, value in doc.items() if value is not OMIT}
        (tmp_path / "store" / f"{day.isoformat()}.json").write_text(json.dumps(body), encoding="utf-8")


def _report(tmp_path, *args, launchctl_exit=113):
    fake = tmp_path / f"launchctl-{launchctl_exit}"
    fake.write_text(f"#!/bin/sh\nexit {launchctl_exit}\n", encoding="utf-8")
    fake.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key != "VNX_PROJECT_ID"}
    env["VNX_DAILY_METRICS_LAUNCHCTL"] = str(fake)
    proc = subprocess.run(
        [sys.executable, str(CLI), "report", *args, "--project-id", PID, "--now", NOW,
         "--store", str(tmp_path / "store")],
        env=env, capture_output=True, text=True, cwd=str(REPO_ROOT))
    return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else None), proc.stderr


def test_seven_final_fresh_days_exit_0(tmp_path):
    _write(tmp_path, _docs())
    code, out, err = _report(tmp_path, "--check")
    assert (code, out["status"], out["unmet"], out["fresh_days"]) == (0, "ok", [], 7), err
    assert out["max_gap_hours"] <= 48 and out["job"]["loaded"] is False


def test_backfilled_day_breaks_the_run(tmp_path):
    docs = _docs()
    docs[WINDOW[3]] = _doc(WINDOW[3], collected="2026-11-12T03:10:00Z", backfilled=True)
    _write(tmp_path, docs)
    code, out, _ = _report(tmp_path, "--check")
    assert code == 1 and "fresh_days" in out["unmet"]
    assert out["fresh_days"] == 3 and out["days"][WINDOW[3].isoformat()] == "backfilled"


@pytest.mark.parametrize("field, value", [
    ("status", OMIT), ("status", None), ("status", "incomplete"), ("status", "Final"),
    ("backfilled", OMIT), ("backfilled", None), ("backfilled", "false"), ("backfilled", 0),
    ("collected_at", OMIT), ("collected_at", None), ("collected_at", "yesterday"),
    ("collected_at", "2026-11-12T03:10:00"), ("collected_at", "2026-11-11T12:00:00Z"),
    ("collected_at", "2026-11-12T10:00:00Z"), ("project_id", "other"), ("day", OMIT), ("day", "2026-11-10"),
])
def test_newest_day_is_fresh_only_on_positive_evidence(tmp_path, field, value):
    docs = _docs()
    docs[NEWEST][field] = value
    _write(tmp_path, docs)
    code, out, _ = _report(tmp_path, "--check")
    assert code == 1 and "fresh_days" in out["unmet"] and out["fresh_days"] == 0


def test_loaded_job_with_newest_day_three_days_old_is_stale(tmp_path):
    _write(tmp_path, _docs([day - timedelta(days=2) for day in WINDOW]))  # newest: 2026-11-09
    code, out, _ = _report(tmp_path, "--check", launchctl_exit=0)
    assert (code, out["status"], out["job"]["loaded"]) == (1, "stale", True)
    assert {"fresh_days", "max_gap_hours"} <= set(out["unmet"])
    code, out, _ = _report(tmp_path, "--check", launchctl_exit=113)
    assert (code, out["status"], out["job"]["loaded"]) == (1, "unmet", False)


def test_day_with_nine_of_ten_ids_exit_1(tmp_path):
    docs = _docs()
    del docs[WINDOW[2]]["metrics"]["merges"]
    _write(tmp_path, docs)
    code, out, _ = _report(tmp_path, "--check")
    assert code == 1 and out["unmet"] == ["missing_ids"]
    assert out["missing_ids"] == {WINDOW[2].isoformat(): ["merges"]}


KEYED_EMPTY = [{"kind": "keyed", "source": "t", "entries": {}}, {"kind": "keyed", "source": "t"},
               {"kind": "keyed", "source": "t", "entries": None}, {"kind": "keyed", "entries": []}, None,
               "codex_gate"]


@pytest.mark.parametrize("metric, record, condition", [
    *(("gate_duration_seconds_by_seat", record, "keyed_without_entries") for record in KEYED_EMPTY),
    ("merges", _scalar(0, (), "x"), "zero_for_unknown"), ("merges", _scalar(0.0, (), "x"), "zero_for_unknown"),
    ("merges", _scalar(None, (), None), "unknown_without_reason"),
    ("merges", _scalar(None, (), " "), "unknown_without_reason"),
    ("merges", _scalar("1", ()), "malformed_records"), ("merges", _scalar(True, ()), "malformed_records"),
    ("merges", [], "malformed_records"),
])
def test_keyed_record_without_entries_and_other_record_breaches_exit_1(tmp_path, metric, record, condition):
    docs = _docs()
    docs[WINDOW[4]]["metrics"][metric] = record
    _write(tmp_path, docs)
    code, out, _ = _report(tmp_path, "--check")
    assert code == 1 and out["unmet"] == [condition] and list(out[condition]) == [WINDOW[4].isoformat()]


def _approved(evidence):
    event = {"event": "approved", "proposal_id": "abc123abc123", "approved_by": "operator"}
    return json.dumps(event if evidence is OMIT else {**event, "evidence": evidence}) + "\n"


@pytest.mark.parametrize("ledger, unmet", [
    *((_approved(evidence), ["approval_without_tty"]) for evidence in
      (OMIT, None, {}, {"tty": None}, {"tty": False}, {"tty": "true"}, {"tty": 1}, "tty")),
    ("{not json\n", ["ledger_unreadable"]), (_approved({"tty": True, "user": "u"}), []),
])
def test_approved_event_without_tty_evidence_exit_1(tmp_path, ledger, unmet):
    _write(tmp_path, _docs())
    (tmp_path / "store" / "proposals.ndjson").write_text(ledger, encoding="utf-8")
    code, out, _ = _report(tmp_path, "--check")
    assert (code, out["unmet"]) == (1 if unmet else 0, unmet)
    assert out["approvals_without_tty"] == (["abc123abc123"] if unmet == ["approval_without_tty"] else
                                            None if unmet else [])


@pytest.mark.parametrize("make_dir", [False, True])
def test_empty_or_absent_store_exit_1(tmp_path, make_dir):
    if make_dir:
        (tmp_path / "store").mkdir()
    code, out, _ = _report(tmp_path, "--check")
    assert code == 1 and out["status"] == "unmet"
    assert {"store_empty", "fresh_days", "max_gap_hours"} <= set(out["unmet"])


def test_report_metric_last_7_json_pools_the_samples(tmp_path):
    docs = _docs()
    docs[WINDOW[0]]["status"] = "incomplete"
    _write(tmp_path, docs)
    code, out, err = _report(tmp_path, "--metric", "pr_open_to_merge_median_hours", "--last", "7", "--json")
    assert code == 0, err
    assert out["window"] == {"from": WINDOW[0].isoformat(), "to": NEWEST.isoformat()}
    assert (out["pooled_value"], out["sample_size"], out["unknown_reason"]) == (1.0, 12, None)
    assert out["excluded_days"] == {WINDOW[0].isoformat(): "not_final"}
    code, out, _ = _report(tmp_path, "--metric", "gate_duration_seconds_by_seat:codex_gate", "--json")
    assert (code, out["pooled_value"], out["sample_size"]) == (0, 300.0, 6)


@pytest.mark.parametrize("metric", ["operator_wait_hours", "merges"])
def test_report_metric_without_a_measured_value_is_unknown_never_0(tmp_path, metric):
    if metric == "operator_wait_hours":
        _write(tmp_path, _docs())
    code, out, _ = _report(tmp_path, "--metric", metric, "--last", "7", "--json")
    assert code == 0 and out["pooled_value"] is None and out["sample_size"] == 0
    assert out["unknown_reason"] == "no_measured_value_in_window" and out["days_with_value"] == 0


@pytest.mark.parametrize("args", [
    ["--metric", "nope"], ["--metric", "gate_duration_seconds_by_seat"],
    ["--metric", "merges:codex_gate"], ["--metric", "merges", "--last", "0"], [],
])
def test_report_usage_errors_exit_2(tmp_path, args):
    code, out, err = _report(tmp_path, *args)
    assert (code, out) == (2, None) and "invalid choice: 'report'" not in err and err.strip()
