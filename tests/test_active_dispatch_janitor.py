"""Regression tests for active_dispatch_janitor (Codex round-2 finding 2).

The dispatcher's pre-supervisor `_cleanup_stuck_dispatches` heuristic moved
any *.md file in active/ older than 60 minutes to completed/ purely on
mtime. Active dispatch files are only moved into active/ at delivery time
and not refreshed afterwards, so any legitimate task running longer than an
hour was silently misclassified as completed and hidden from T0 state.

These tests pin the new contract:
1. Receipt evidence is required to promote a dispatch from active/ → completed/.
2. A receiptless dispatch is NEVER moved on age alone.
3. A receiptless dispatch older than --stale-hours is reported as orphan
   (so callers / operators can intervene) but the file stays in active/.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import active_dispatch_janitor as janitor  # noqa: E402
from active_dispatch_janitor import (  # noqa: E402
    ReconcileResult,
    build_receipt_index,
    reconcile_active,
)

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "active_dispatch_janitor.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def layout(tmp_path: Path):
    active = tmp_path / "active"
    completed = tmp_path / "completed"
    receipts_processed = tmp_path / "receipts" / "processed"
    active.mkdir()
    completed.mkdir()
    receipts_processed.mkdir(parents=True)
    return tmp_path, active, completed, receipts_processed


def _write_active(active: Path, dispatch_id: str, age_hours: float = 0.0) -> Path:
    f = active / f"{dispatch_id}.md"
    f.write_text(f"# {dispatch_id}\n", encoding="utf-8")
    if age_hours > 0:
        ts = time.time() - age_hours * 3600.0
        os.utime(f, (ts, ts))
    return f


def _write_receipt(receipts_processed: Path, dispatch_id: str, name: str = "r.json") -> Path:
    f = receipts_processed / name
    f.write_text(json.dumps({"dispatch_id": dispatch_id, "event_type": "task_complete"}), encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# build_receipt_index
# ---------------------------------------------------------------------------

class TestReceiptIndex:
    def test_returns_empty_when_dir_missing(self, tmp_path):
        assert build_receipt_index(tmp_path / "nope") == frozenset()

    def test_indexes_dispatch_ids(self, layout):
        _, _, _, processed = layout
        _write_receipt(processed, "d-1", name="1234-T1-1.json")
        _write_receipt(processed, "d-2", name="5678-T2-9.json")
        idx = build_receipt_index(processed)
        assert idx == frozenset({"d-1", "d-2"})

    def test_skips_unknown_and_empty_ids(self, layout):
        _, _, _, processed = layout
        _write_receipt(processed, "unknown", name="a.json")
        _write_receipt(processed, "", name="b.json")
        _write_receipt(processed, "real-1", name="c.json")
        assert build_receipt_index(processed) == frozenset({"real-1"})

    def test_skips_malformed_json(self, layout):
        _, _, _, processed = layout
        (processed / "bad.json").write_text("not json", encoding="utf-8")
        _write_receipt(processed, "good", name="good.json")
        assert build_receipt_index(processed) == frozenset({"good"})

    def test_skips_non_dict_payload(self, layout):
        _, _, _, processed = layout
        (processed / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
        _write_receipt(processed, "ok", name="ok.json")
        assert build_receipt_index(processed) == frozenset({"ok"})


# ---------------------------------------------------------------------------
# reconcile_active core contract
# ---------------------------------------------------------------------------

class TestReconcileActive:
    def test_receipt_promotes_to_completed(self, layout):
        _, active, completed, processed = layout
        f = _write_active(active, "d-receipt", age_hours=0.1)
        _write_receipt(processed, "d-receipt")
        results = reconcile_active(active, completed, processed)
        assert results == [ReconcileResult("d-receipt", "completed", "receipt found")]
        assert not f.exists()
        assert (completed / "d-receipt.md").exists()

    def test_long_running_no_receipt_is_not_completed(self, layout):
        """Codex round-2 finding 2: legit long-running task must NOT be moved.

        File is 6 hours old (default mtime heuristic would have completed it
        after 1 hour). With no receipt, the new janitor must leave it in active/.
        """
        _, active, completed, processed = layout
        f = _write_active(active, "d-long", age_hours=6.0)
        results = reconcile_active(active, completed, processed, stale_hours=24.0)
        assert len(results) == 1
        assert results[0].action == "skipped", f"long-running task misclassified: {results[0]}"
        assert f.exists(), "long-running active dispatch must remain in active/"
        assert not (completed / "d-long.md").exists()

    def test_orphan_age_threshold_reports_but_does_not_move(self, layout):
        _, active, completed, processed = layout
        f = _write_active(active, "d-orphan", age_hours=48.0)
        results = reconcile_active(active, completed, processed, stale_hours=24.0)
        assert len(results) == 1
        assert results[0].action == "orphan"
        assert f.exists(), "orphan must stay in active/ for higher-level decision"
        assert not (completed / "d-orphan.md").exists()

    def test_fresh_no_receipt_is_skipped(self, layout):
        _, active, completed, processed = layout
        _write_active(active, "d-fresh", age_hours=0.05)
        results = reconcile_active(active, completed, processed, stale_hours=24.0)
        assert len(results) == 1
        assert results[0].action == "skipped"

    def test_mixed_population(self, layout):
        _, active, completed, processed = layout
        _write_active(active, "with-receipt", age_hours=0.5)
        _write_active(active, "still-running", age_hours=2.0)
        _write_active(active, "stale-orphan", age_hours=48.0)
        _write_receipt(processed, "with-receipt", name="a.json")
        results = reconcile_active(active, completed, processed, stale_hours=24.0)
        actions = {r.dispatch_id: r.action for r in results}
        assert actions == {
            "with-receipt": "completed",
            "still-running": "skipped",
            "stale-orphan": "orphan",
        }
        assert (completed / "with-receipt.md").exists()
        assert (active / "still-running.md").exists()
        assert (active / "stale-orphan.md").exists()

    def test_ignores_non_md_and_subdirs(self, layout):
        _, active, completed, processed = layout
        (active / "junk.txt").write_text("nope", encoding="utf-8")
        (active / "subdir").mkdir()
        results = reconcile_active(active, completed, processed)
        assert results == []

    def test_active_dir_missing_is_safe(self, tmp_path):
        results = reconcile_active(
            tmp_path / "no-active",
            tmp_path / "completed",
            tmp_path / "receipts" / "processed",
        )
        assert results == []

    def test_filename_with_suffix_after_dispatch_id_does_not_match_receipt(self, layout):
        """Dispatch ID must match the full basename minus .md.

        Guards against accidental fuzzy matches if the dispatcher ever wrote
        ``<dispatch_id>-something.md`` files into active/.
        """
        _, active, completed, processed = layout
        _write_active(active, "d-1-suffix", age_hours=0.1)
        _write_receipt(processed, "d-1")
        results = reconcile_active(active, completed, processed)
        assert results[0].action == "skipped"
        assert (active / "d-1-suffix.md").exists()


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

class TestCli:
    def test_cli_json_output_completed(self, layout):
        _, active, completed, processed = layout
        _write_active(active, "d-cli", age_hours=0.1)
        _write_receipt(processed, "d-cli")
        result = subprocess.run(
            [
                sys.executable,
                str(_MODULE_PATH),
                "--active-dir", str(active),
                "--completed-dir", str(completed),
                "--receipts-processed-dir", str(processed),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout.strip())
        assert payload == [{"dispatch_id": "d-cli", "action": "completed", "reason": "receipt found"}]

    def test_cli_no_results_returns_zero(self, layout):
        _, active, completed, processed = layout
        result = subprocess.run(
            [
                sys.executable,
                str(_MODULE_PATH),
                "--active-dir", str(active),
                "--completed-dir", str(completed),
                "--receipts-processed-dir", str(processed),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_cli_orphan_logs_but_returns_zero(self, layout):
        _, active, completed, processed = layout
        _write_active(active, "d-orphan", age_hours=48.0)
        result = subprocess.run(
            [
                sys.executable,
                str(_MODULE_PATH),
                "--active-dir", str(active),
                "--completed-dir", str(completed),
                "--receipts-processed-dir", str(processed),
                "--stale-hours", "24",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "ORPHAN" in result.stdout
        assert (active / "d-orphan.md").exists()
