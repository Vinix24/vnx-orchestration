"""Tests for the per-key producer-freshness sweep (scripts/lib/producer_freshness.py).

Every test in this file is RED against origin/main (the module, config and CLI
do not exist there) and GREEN on the producer-freshness-monitor branch.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import producer_freshness as pf  # noqa: E402

NOW = time.time()
DAY = 86400


def _touch(path: Path, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    ts = NOW - age_seconds
    os.utime(path, (ts, ts))


def _make_review_gates(state_dir: Path) -> None:
    # The 2026-07-31 incident shape: codex_gate requests silent ~6 days,
    # results silent ~3 days; kimi_gate results fresh.
    _touch(state_dir / "review_gates" / "requests" / "pr-1228-codex_gate.json", 6 * DAY)
    _touch(state_dir / "review_gates" / "requests" / "pr-1220-kimi_gate.json", 6 * DAY)
    _touch(state_dir / "review_gates" / "results" / "pr-1228-codex_gate.json", 3 * DAY)
    _touch(state_dir / "review_gates" / "results" / "pr-1233-kimi_gate.json", 0.5 * DAY)


def _make_demand_register(state_dir: Path, events_after_iso_ts: float, n: int) -> None:
    lines = []
    for i in range(n):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(events_after_iso_ts + 3600 * (i + 1)))
        lines.append(json.dumps({"timestamp": ts, "event": "dispatch_started", "dispatch_id": f"DISP-{i:03d}"}))
    (state_dir / "dispatch_register.ndjson").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_runtime_db(state_dir: Path) -> None:
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE dispatches (dispatch_id TEXT, created_at TEXT)")
        # Only the dlv- producer ever wrote, and it stopped 15 days ago.
        for i in range(3):
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - (15 + i) * DAY))
            conn.execute("INSERT INTO dispatches VALUES (?, ?)", (f"dlv-{i:04d}-x", ts))
        conn.commit()
    finally:
        conn.close()


def _make_quality_db(state_dir: Path) -> None:
    db = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE governance_metrics (metric_name TEXT, computed_at TEXT)")
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 45 * DAY))
        fresh = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(NOW - 0.1 * DAY))
        conn.execute("INSERT INTO governance_metrics VALUES ('fpy', ?)", (old,))
        conn.execute("INSERT INTO governance_metrics VALUES ('rework_rate', ?)", (old,))
        conn.execute("INSERT INTO governance_metrics VALUES ('dispatch_count', ?)", (fresh,))
        conn.commit()
    finally:
        conn.close()


def _registry(state_dir: Path) -> list:
    return [
        {
            "name": "review_gate_requests",
            "type": "directory",
            "path": str(state_dir / "review_gates" / "requests"),
            "glob": "*.json",
            "key_regex": r"^pr-[0-9]+-(?P<key>.+)\.json$",
            "timestamp": "mtime",
            "cadence_seconds": DAY,
            "demand": {
                "type": "ndjson_events",
                "path": str(state_dir / "dispatch_register.ndjson"),
                "timestamp_field": "timestamp",
                "label": "dispatch_register events",
            },
        },
        {
            "name": "review_gate_results",
            "type": "directory",
            "path": str(state_dir / "review_gates" / "results"),
            "glob": "*.json",
            "key_regex": r"^pr-[0-9]+-(?P<key>.+)\.json$",
            "timestamp": "mtime",
            "cadence_seconds": DAY,
            "demand": {
                "type": "ndjson_events",
                "path": str(state_dir / "dispatch_register.ndjson"),
                "timestamp_field": "timestamp",
                "label": "dispatch_register events",
            },
        },
        {
            "name": "dispatches_table",
            "type": "sqlite",
            "db": str(state_dir / "runtime_coordination.db"),
            "query": "SELECT dispatch_id, created_at FROM dispatches",
            "key_column": "dispatch_id",
            "key_transform": "prefix",
            "timestamp_column": "created_at",
            "cadence_seconds": 7 * DAY,
            "expected_keys": ["dlv", "DISP"],
        },
        {
            "name": "governance_metrics",
            "type": "sqlite",
            "db": str(state_dir / "quality_intelligence.db"),
            "query": "SELECT metric_name, MAX(computed_at) AS last_ts FROM governance_metrics GROUP BY metric_name",
            "key_column": "metric_name",
            "timestamp_column": "last_ts",
            "cadence_seconds": 3 * DAY,
        },
    ]


@pytest.fixture()
def fake_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _make_review_gates(state_dir)
    _make_demand_register(state_dir, NOW - 3 * DAY, 5)
    _make_runtime_db(state_dir)
    _make_quality_db(state_dir)
    return state_dir


def _findings_by(report: dict, producer: str) -> dict:
    return {f["key"]: f for f in report["findings"] if f["producer"] == producer}


def test_real_config_loads() -> None:
    registry = pf.load_registry(REPO_ROOT / "configs" / "producer_freshness.yaml")
    names = [p["name"] for p in registry]
    assert names == [
        "review_gate_requests",
        "review_gate_results",
        # OI-876/OI-881: declared-gate obligations, grouped per gate key
        "review_gate_obligations",
        # 20260816: aggregate "newest gate result" key with merged-PR demand
        "review_gate_results_freshness",
        "dispatches_table",
        "governance_metrics",
        # OI-896: auto-dream cycles, grouped per cycle status
        "dream_cycles",
    ]


def test_sweep_finds_silent_review_gate_keys(fake_state: Path) -> None:
    report = pf.run_sweep(fake_state, _registry(fake_state), now=NOW)
    requests = _findings_by(report, "review_gate_requests")
    results = _findings_by(report, "review_gate_results")
    # codex_gate is silent beyond its 1-day cadence in BOTH layers.
    assert requests["codex_gate"]["kind"] == "stale"
    assert results["codex_gate"]["kind"] == "stale"
    assert results["codex_gate"]["silence_days"] == pytest.approx(3.0, abs=0.05)
    # kimi_gate results are fresh — per-key grouping must NOT condemn the
    # whole directory just because one key is dead (and vice versa).
    assert "kimi_gate" not in results
    # Demand evidence: dispatches kept flowing while the gate was silent.
    assert results["codex_gate"]["demand"]["events_since_last_seen"] >= 1
    assert report["status"] == "stale"


def test_sweep_finds_dead_sqlite_keys_and_absent_producer(fake_state: Path) -> None:
    report = pf.run_sweep(fake_state, _registry(fake_state), now=NOW)
    dispatches = _findings_by(report, "dispatches_table")
    # The dlv producer is stale (15 days > 7-day cadence)...
    assert dispatches["dlv"]["kind"] == "stale"
    # ...and the dispatch-door producer is entirely ABSENT: a producer that
    # writes nothing can only be caught by an expected-key assertion.
    assert dispatches["DISP"]["kind"] == "missing"
    assert dispatches["DISP"]["expected_key_absent"] is True

    metrics = _findings_by(report, "governance_metrics")
    # Table looks alive (dispatch_count fresh) but fpy/rework_rate are dead —
    # the exact governance_metrics case. Only per-key grouping sees this.
    assert metrics["fpy"]["kind"] == "stale"
    assert metrics["rework_rate"]["kind"] == "stale"
    assert "dispatch_count" not in metrics


def test_unreadable_source_is_a_finding_not_silence(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    spec = {
        "name": "broken_db_producer",
        "type": "sqlite",
        "db": str(state_dir / "does_not_exist.db"),
        "query": "SELECT k, t FROM x",
        "key_column": "k",
        "timestamp_column": "t",
        "cadence_seconds": DAY,
    }
    report = pf.run_sweep(state_dir, [spec], now=NOW)
    assert report["findings_count"] == 1
    assert report["findings"][0]["kind"] == "source_unreadable"
    assert report["status"] == "stale"


def test_report_and_heartbeat_written_every_run(tmp_path: Path) -> None:
    """The heartbeat lands even on a zero-finding run — a sweep that finds
    nothing and writes nothing is indistinguishable from one that never ran."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    fresh_dir = state_dir / "fresh"
    _touch(fresh_dir / "pr-1-codex_gate.json", 60)
    spec = {
        "name": "fresh_producer",
        "type": "directory",
        "path": str(fresh_dir),
        "glob": "*.json",
        "key_regex": r"^pr-[0-9]+-(?P<key>.+)\.json$",
        "timestamp": "mtime",
        "cadence_seconds": DAY,
    }
    report = pf.run_sweep(state_dir, [spec], now=NOW)
    assert report["findings_count"] == 0
    assert report["status"] == "ok"

    report_path = pf.append_report(state_dir, report)
    heartbeat_path = pf.write_heartbeat(state_dir, report)

    records = [json.loads(line) for line in report_path.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["event_type"] == "producer_freshness_sweep"
    assert records[0]["status"] == "ok"

    beacon = json.loads(heartbeat_path.read_text())
    assert beacon["component"] == "producer_freshness_monitor"
    assert beacon["status"] == "ok"
    assert heartbeat_path.parent.name == "health"


def test_report_appends_findings_as_ndjson(fake_state: Path) -> None:
    report = pf.run_sweep(fake_state, _registry(fake_state), now=NOW)
    report_path = pf.append_report(fake_state, report)
    records = [json.loads(line) for line in report_path.read_text().splitlines() if line.strip()]
    kinds = {r["event_type"] for r in records}
    assert "producer_freshness_sweep" in kinds
    assert "producer_freshness_finding" in kinds
    finding_records = [r for r in records if r["event_type"] == "producer_freshness_finding"]
    assert len(finding_records) == report["findings_count"]
    assert all(r["run_id"] == report["run_id"] for r in records)


def test_cli_no_write_touches_nothing_and_reports(fake_state: Path, capsys, monkeypatch) -> None:
    """The acceptance mode against the live store: read-only, findings on stdout."""
    import producer_freshness_monitor as cli  # noqa: PLC0415

    before = {p for p in fake_state.rglob("*")}
    rc = cli.main(
        [
            "--config",
            str(REPO_ROOT / "configs" / "producer_freshness.yaml"),
            "--state-dir",
            str(fake_state),
            "--no-write",
        ]
    )
    after = {p for p in fake_state.rglob("*")}
    assert rc == cli.EXIT_OK  # OI-1039: findings no longer drive exit code
    assert before == after, "--no-write must not create/modify any file"
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "stale"
    # OI-1041: review_gate_requests/results are one_shot — they no longer
    # produce stale findings. The real findings now come from ongoing producers.
    assert any(f["producer"] == "governance_metrics" for f in out["findings"])


# ---------------------------------------------------------------------------
# Auto-dream consolidation (ADR-019 / OI-896) — dream_cycles producer
# ---------------------------------------------------------------------------
# The key is the cycle STATUS, never table level (OI-881). status='completed'
# means a cycle finished consolidation and is waiting on the mandatory T0
# review gate. A completed cycle that sits unreviewed past cadence is stale —
# a review gate skipped by construction. `expected_keys: [completed]` makes a
# scheduler that NEVER ran visible too: absence is asserted, not observed.

_DREAM_CYCLES_SPEC = {
    "name": "dream_cycles",
    "type": "sqlite",
    "query": (
        "SELECT status, MAX(COALESCE(completed_at, started_at)) AS last_ts "
        "FROM dream_cycles GROUP BY status"
    ),
    "key_column": "status",
    "timestamp_column": "last_ts",
    "cadence_seconds": DAY,
    "expected_keys": ["completed"],
}


def _make_dream_db(state_dir: Path, rows: list[tuple[str, str]]) -> None:
    """Create quality_intelligence.db with a dream_cycles table (status, ts)."""
    db = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE dream_cycles ("
            " cycle_id TEXT, project_id TEXT, started_at TEXT, completed_at TEXT,"
            " status TEXT)"
        )
        for status, ts in rows:
            completed = ts if status in ("completed", "reviewed", "rejected") else None
            conn.execute(
                "INSERT INTO dream_cycles (cycle_id, project_id, started_at, completed_at, status)"
                " VALUES (?, 'vnx-dev', ?, ?, ?)",
                (f"dream-{status}-{ts}", ts, completed, status),
            )
        conn.commit()
    finally:
        conn.close()


def _dream_iso(days_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - days_ago * DAY))


def test_dream_cycles_stale_completed_is_a_finding_per_status_key(tmp_path: Path) -> None:
    """A completed cycle unreviewed past cadence is stale; a fresh reviewed
    cycle must NOT hide it (per sleutel — the OI-881 lesson)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _make_dream_db(
        state_dir,
        [
            ("completed", _dream_iso(3.0)),   # completed 3 days ago, never reviewed
            ("reviewed", _dream_iso(0.1)),     # a review happened just now
        ],
    )
    spec = {**_DREAM_CYCLES_SPEC, "db": str(state_dir / "quality_intelligence.db")}

    section = pf.evaluate_producer(spec, now=NOW)

    assert section["status"] == "stale"
    stale_keys = {f["key"] for f in section["findings"]}
    assert stale_keys == {"completed"}, (
        "only the unreviewed completed stream may be flagged — the fresh "
        "reviewed sibling stays green (per status, not per table)"
    )
    assert section["findings"][0]["kind"] == "stale"
    assert section["findings"][0]["silence_seconds"] >= 3 * DAY


def test_dream_cycles_never_ran_is_expected_key_absent(tmp_path: Path) -> None:
    """A scheduler that never produced a cycle can only be caught by an
    expected-key assertion — grouping existing rows finds nothing."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _make_dream_db(state_dir, [])  # dream_cycles exists but is empty
    spec = {**_DREAM_CYCLES_SPEC, "db": str(state_dir / "quality_intelligence.db")}

    section = pf.evaluate_producer(spec, now=NOW)

    assert section["status"] == "stale"
    assert section["findings"][0]["key"] == "completed"
    assert section["findings"][0]["kind"] == "missing"
    assert section["findings"][0]["expected_key_absent"] is True


def test_real_config_sweep_knows_both_new_producers_and_flags_silence(
    tmp_path: Path,
) -> None:
    """End-to-end against the REAL registry: both review_gate_obligations and
    dream_cycles are evaluated, and a simulated silence (an unfulfilled gate
    declaration + an unreviewed completed cycle) yields a finding each."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Obligation: codex_gate declared 3 days ago, never fulfilled.
    (state_dir / "review_gates" / "obligations").mkdir(parents=True)
    (state_dir / "review_gates" / "obligations" / "20260729-dispatch.json").write_text(
        json.dumps(
            {
                "dispatch_id": "20260729-dispatch",
                "gate": "codex_gate",
                "declared_at": _dream_iso(3.0),
                "status": "pending",
            }
        ),
        encoding="utf-8",
    )
    # Dream: a completed cycle unreviewed for 3 days; a fresh reviewed one.
    _make_dream_db(
        state_dir,
        [("completed", _dream_iso(3.0)), ("reviewed", _dream_iso(0.1))],
    )
    # Keep the other sqlite producers readable so the findings under test are
    # not drowned out by source_unreadable noise. Their known stale keys
    # (dlv > 7d cadence, fpy/rework > 3d cadence) are pre-existing producers,
    # not the two under test.
    _make_runtime_db(state_dir)
    _make_quality_db(state_dir)

    registry = pf.load_registry(REPO_ROOT / "configs" / "producer_freshness.yaml")
    report = pf.run_sweep(state_dir, registry, now=NOW)

    producers = {s["producer"] for s in report["producers"]}
    assert {"review_gate_obligations", "dream_cycles"} <= producers

    findings_by = {(f["producer"], f["key"]): f for f in report["findings"]}
    assert findings_by[("review_gate_obligations", "codex_gate")]["kind"] == "stale"
    assert findings_by[("dream_cycles", "completed")]["kind"] == "stale"
    # Per-key: the fresh reviewed dream cycle stays green.
    assert ("dream_cycles", "reviewed") not in findings_by
    assert report["status"] == "stale"


# ---------------------------------------------------------------------------
# OI-1041: one_shot producers — no staleness, only missing expected keys
# ---------------------------------------------------------------------------


def test_one_shot_producer_skips_staleness(tmp_path: Path) -> None:
    """A one_shot producer's keys that exist are never flagged as stale.
    Silence after a one-shot artefact's PR merges is normal, not a failure."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    gates_dir = state_dir / "review_gates" / "requests"
    _touch(gates_dir / "pr-100-codex_gate.json", 10 * DAY)  # way past cadence
    _touch(gates_dir / "pr-101-kimi_gate.json", 10 * DAY)

    spec = {
        "name": "review_gate_requests",
        "kind": "one_shot",
        "type": "directory",
        "path": str(gates_dir),
        "glob": "*.json",
        "key_regex": r"^pr-[0-9]+-(?P<key>.+)\.json$",
        "cadence_seconds": DAY,
    }
    section = pf.evaluate_producer(spec, now=NOW)
    assert section["kind"] == "one_shot"
    assert section["findings"] == [], (
        "one_shot producer must not flag stale: silence after merge is normal"
    )
    assert section["status"] == "ok"


def test_one_shot_producer_flags_missing_expected_keys(tmp_path: Path) -> None:
    """A one_shot producer with expected_keys flags keys that NEVER wrote.
    Absence of a mandatory artefact is still a finding, even for one-shot."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    gates_dir = state_dir / "review_gates" / "requests"
    _touch(gates_dir / "pr-100-codex_gate.json", 0.5 * DAY)

    spec = {
        "name": "review_gate_requests",
        "kind": "one_shot",
        "type": "directory",
        "path": str(gates_dir),
        "glob": "*.json",
        "key_regex": r"^pr-[0-9]+-(?P<key>.+)\.json$",
        "cadence_seconds": DAY,
        "expected_keys": ["codex_gate", "gemini_review"],
    }
    section = pf.evaluate_producer(spec, now=NOW)
    assert len(section["findings"]) == 1
    finding = section["findings"][0]
    assert finding["key"] == "gemini_review"
    assert finding["kind"] == "missing"
    assert finding["expected_key_absent"] is True
    # codex_gate exists and is NOT flagged — it's one_shot, not stale
    assert "codex_gate" not in {f["key"] for f in section["findings"]}
    assert section["status"] == "stale"


def test_ongoing_producer_still_flags_staleness(tmp_path: Path) -> None:
    """Default kind='ongoing' preserves the original cadence-based behavior.
    Backward compat: specs without kind are treated as ongoing."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    gates_dir = state_dir / "review_gates" / "requests"
    _touch(gates_dir / "pr-100-codex_gate.json", 10 * DAY)

    spec = {
        "name": "review_gate_requests",
        # no 'kind' field -> defaults to "ongoing"
        "type": "directory",
        "path": str(gates_dir),
        "glob": "*.json",
        "key_regex": r"^pr-[0-9]+-(?P<key>.+)\.json$",
        "cadence_seconds": DAY,
    }
    section = pf.evaluate_producer(spec, now=NOW)
    assert section["kind"] == "ongoing"
    assert len(section["findings"]) == 1
    assert section["findings"][0]["key"] == "codex_gate"
    assert section["findings"][0]["kind"] == "stale"
    assert section["status"] == "stale"


# ---------------------------------------------------------------------------
# OI-1039: exit 0 on sweep with findings — health file carries the signal
# ---------------------------------------------------------------------------


def test_cli_exits_zero_even_with_findings(fake_state: Path, capsys, monkeypatch) -> None:
    """OI-1039: the sweep always exits 0 when it ran successfully. Findings
    are reported via the health file + NDJSON, not via exit code. launchd
    interprets any non-zero exit as permanent failure."""
    import producer_freshness_monitor as cli  # noqa: PLC0415

    rc = cli.main(
        [
            "--config",
            str(REPO_ROOT / "configs" / "producer_freshness.yaml"),
            "--state-dir",
            str(fake_state),
            "--no-write",
        ]
    )
    assert rc == cli.EXIT_OK, (
        "exit 0 always, even with findings — they're in the health file"
    )
    out = json.loads(capsys.readouterr().out)
    assert out["findings_count"] > 0, "sweep with real registry must find stale producers"


def test_cli_no_write_persists_findings_in_report(fake_state: Path) -> None:
    """The health file + NDJSON report still carries findings even when exit
    code is always 0. The signal is not lost — it moved channels."""
    import producer_freshness_monitor as cli  # noqa: PLC0415

    rc = cli.main(
        [
            "--config",
            str(REPO_ROOT / "configs" / "producer_freshness.yaml"),
            "--state-dir",
            str(fake_state),
        ]
    )
    assert rc == cli.EXIT_OK
    # The report file must exist and contain the sweep + findings.
    report_path = fake_state / pf.REPORT_FILENAME
    assert report_path.exists(), "NDJSON report must be written"
    records = [json.loads(line) for line in report_path.read_text().splitlines() if line.strip()]
    sweep = [r for r in records if r["event_type"] == "producer_freshness_sweep"]
    assert len(sweep) == 1
    assert sweep[0]["findings_count"] > 0, "findings still land via NDJSON"
    # Heartbeat also written.
    heartbeat = fake_state.parent / "health" / "producer_freshness_monitor.json"
    assert heartbeat.exists(), "heartbeat must be written on every run"


# ---------------------------------------------------------------------------
# 20260816: "PRs merged since the last gate result" — scan_newest + event filter
# ---------------------------------------------------------------------------


def test_scan_newest_returns_newest_mtime(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _touch(results_dir / "pr-1-codex_gate.json", 3 * DAY)
    _touch(results_dir / "pr-2-kimi_gate.json", 0.5 * DAY)

    spec = {"path": str(results_dir), "glob": "*.json", "key": "results"}
    seen = pf.scan_newest(spec, now=NOW)

    assert seen == {"results": seen["results"]}
    assert NOW - seen["results"] < DAY, "newest key must be the 0.5-day-old file, not the 3-day-old one"


def test_scan_newest_empty_dir_returns_empty(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    results_dir.mkdir()

    spec = {"path": str(results_dir), "glob": "*.json", "key": "results"}
    assert pf.scan_newest(spec, now=NOW) == {}


def test_count_demand_events_filters_by_event_value(tmp_path: Path) -> None:
    register = tmp_path / "dispatch_register.ndjson"
    lines = [
        {"timestamp": _dream_iso(1.0), "event": "pr_merged"},
        {"timestamp": _dream_iso(0.9), "event": "pr_merged"},
        {"timestamp": _dream_iso(0.8), "event": "dispatch_started"},
        {"timestamp": _dream_iso(0.7), "event": "pr_merged"},
    ]
    register.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")

    spec = {
        "cadence_seconds": DAY,
        "demand": {
            "type": "ndjson_events",
            "path": str(register),
            "timestamp_field": "timestamp",
            "event_field": "event",
            "event_value": "pr_merged",
            "label": "merged PRs",
        },
    }
    count = pf.count_demand_events(spec, since_ts=NOW - 2 * DAY, now=NOW)

    assert count == 3, "only pr_merged events count; dispatch_started is filtered out"


def test_real_config_review_gate_results_freshness_producer() -> None:
    registry = pf.load_registry(REPO_ROOT / "configs" / "producer_freshness.yaml")
    by_name = {p["name"]: p for p in registry}
    producer = by_name["review_gate_results_freshness"]
    assert producer["type"] == "newest"
    assert producer["key"] == "results"
    assert producer["expected_keys"] == ["results"]
    assert producer["demand"]["event_field"] == "event"
    assert producer["demand"]["event_value"] == "pr_merged"


def test_review_gate_results_freshness_flags_stale_with_merged_pr_demand(tmp_path: Path) -> None:
    """The 2026-08-12..16 shape: an old gate result + merged PRs since = a
    stale finding whose demand evidence counts the merged PRs, not every
    dispatch_register event."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    results_dir = state_dir / "review_gates" / "results"
    _touch(results_dir / "pr-1-codex_gate.json", 3 * DAY)

    register = state_dir / "dispatch_register.ndjson"
    lines = [
        {"timestamp": _dream_iso(1.0), "event": "pr_merged"},
        {"timestamp": _dream_iso(0.9), "event": "pr_merged"},
        {"timestamp": _dream_iso(0.5), "event": "dispatch_started"},
    ]
    register.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")

    spec = {
        "name": "review_gate_results_freshness",
        "type": "newest",
        "path": str(results_dir),
        "glob": "*.json",
        "key": "results",
        "cadence_seconds": DAY,
        "expected_keys": ["results"],
        "demand": {
            "type": "ndjson_events",
            "path": str(register),
            "timestamp_field": "timestamp",
            "event_field": "event",
            "event_value": "pr_merged",
            "label": "merged PRs",
        },
    }
    section = pf.evaluate_producer(spec, now=NOW)

    assert section["status"] == "stale"
    assert len(section["findings"]) == 1
    finding = section["findings"][0]
    assert finding["key"] == "results"
    assert finding["kind"] == "stale"
    assert finding["demand"]["source"] == "merged PRs"
    assert finding["demand"]["events_since_last_seen"] == 2, (
        "only pr_merged events newer than the 3-day-old result count"
    )


def test_review_gate_results_freshness_empty_dir_is_missing(tmp_path: Path) -> None:
    """An empty results dir is an asserted absence: the 'results' key never
    wrote, so the finding is 'missing', not a silent green."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    results_dir = state_dir / "review_gates" / "results"
    results_dir.mkdir(parents=True)

    spec = {
        "name": "review_gate_results_freshness",
        "type": "newest",
        "path": str(results_dir),
        "glob": "*.json",
        "key": "results",
        "cadence_seconds": DAY,
        "expected_keys": ["results"],
    }
    section = pf.evaluate_producer(spec, now=NOW)

    assert section["status"] == "stale"
    assert section["findings"][0]["key"] == "results"
    assert section["findings"][0]["kind"] == "missing"
    assert section["findings"][0]["expected_key_absent"] is True
