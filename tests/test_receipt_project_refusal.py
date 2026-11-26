"""AG-1b: exercise the real append and converter processes on an unnamed store."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "other"
    (root / "state").mkdir(parents=True)
    (root / "unified_reports").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.update(VNX_HOME=str(ROOT), PROJECT_ROOT=str(tmp_path),
               VNX_DATA_DIR=str(root), VNX_STATE_DIR=str(root / "state"),
               VNX_DATA_DIR_EXPLICIT="1", VNX_DATA_HOME=str(tmp_path / "account"))
    return root, env


@pytest.mark.parametrize("project", [None, "", "../other", "/other", "other"])
def test_append_refuses_guessed_project(store, project):
    root, env = store
    if project is not None:
        env["VNX_PROJECT_ID"] = project
    ledger = root / "state/t0_receipts.ndjson"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/append_receipt.py"),
         "--receipts-file", str(ledger)], env=env, capture_output=True, text=True,
        input=json.dumps({"timestamp": "2026-11-25T20:00:00Z",
                          "event_type": "task_complete", "dispatch_id": "ag-1b-test",
                          "terminal": "T1", "status": "success", "receipt_kind": "test"}),
    )
    if project == "other":
        assert result.returncode == 0, result.stderr
        assert json.loads(ledger.read_text())["project_id"] == "other"
        return
    assert result.returncode != 0, ledger.read_text() if ledger.exists() else result.stderr
    assert "project_id_unresolved" in result.stderr
    assert not ledger.exists()


@pytest.mark.parametrize("source", ["env", "marker", "layout"])
def test_converter_refusal_then_retry_exactly_once(store, source):
    root, env = store
    report = root / "unified_reports/ag-1b-test.md"
    report.write_text("**Dispatch-ID**: ag-1b-test\n**Model**: gpt-6-astra\n"
                      "**Provider**: codex\n**Status**: failure\n\n"
                      "## Summary\nA failed dispatch.\n## Changes\nNone.\n"
                      "## Verification\nNo tests run.\n## Open Items\nNone.\n")
    original = report.read_bytes()
    command = [sys.executable, str(ROOT / "scripts/lib/report_to_receipt_converter.py"),
               "--state-dir", str(root / "state"), str(report.parent)]
    dry = subprocess.run(command + ["--dry-run"], env=env, capture_output=True, text=True)
    assert "project_id_unresolved" in dry.stderr
    assert not (root / "state/skip_metrics.ndjson").exists()
    assert not (root / "health/report_to_receipt_converter.json").exists()
    first = subprocess.run(command, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    health = json.loads((root / "health/report_to_receipt_converter.json").read_text())
    assert health["details"]["error_count"] == 1, health
    assert health["details"]["skipped_non_dispatch_count"] == 0
    assert "project_id_unresolved" in str(health["details"]["errors"])
    assert "ERROR" in first.stderr and str(report) in first.stderr
    assert report.read_bytes() == original
    assert not (root / "state/t0_receipts.ndjson").exists()
    for name in ("report_to_receipt_processed.txt", "processed_receipts.txt"):
        path = root / "state" / name
        assert not path.exists() or not path.read_text().strip()
    counter = root / "state/skip_metrics.ndjson"
    events = [json.loads(line) for line in counter.read_text().splitlines()]
    assert len(events) == 1
    assert events[0]["context"] == "receipt_processor"
    assert events[0]["reason"] == events[0]["event_type"] == "project_id_unresolved"
    assert events[0]["report"] == str(report)

    if source == "env":
        env["VNX_PROJECT_ID"] = "other"
    elif source == "marker":
        (root / ".vnx-project-id").write_text("other\n")
    else:
        destination = root.parent / ".vnx-data/other"
        destination.parent.mkdir()
        root.rename(destination)
        command = [arg.replace(str(root), str(destination)) for arg in command]
        env = {k: v.replace(str(root), str(destination)) for k, v in env.items()}
        root = destination
    for _ in range(2):
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    lines = (root / "state/t0_receipts.ndjson").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["project_id"] == "other"
    assert (root / "unified_reports/ag-1b-test.md").read_bytes() == original
    assert len((root / "state/skip_metrics.ndjson").read_text().splitlines()) == 1


def test_confidence_lookup_refuses_unknown_tenant_and_scopes_exactly(tmp_path):
    from append_receipt_internals.payload import _confidence_event_recorded
    from project_scope import TenantUnresolved

    db = tmp_path / "quality_intelligence.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE confidence_events (dispatch_id, outcome, project_id)")
        conn.executemany("INSERT INTO confidence_events VALUES (?, ?, ?)",
                         [("ag-1b", "success", "neighbour"),
                          ("ag-1b-longer", "success", "other")])
    with pytest.raises(TenantUnresolved):
        _confidence_event_recorded(db, "ag-1b", "success")
    (tmp_path / ".vnx-project-id").write_text("other\n")
    assert not _confidence_event_recorded(db, "ag-1b", "success")
    assert _confidence_event_recorded(db, "ag-1b-longer", "success")
