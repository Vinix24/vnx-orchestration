"""fabric-state-herstel D4a — `_build_recent_receipts` reads `receipt_outcome`.

The reader used a priority table that let an earlier success beat a later
failure, and showed writer-B's `done` next to writer-A's lane status. It now
folds a dispatch through `receipt_outcome.summarize` and shows the outcome's
status. ADR-007: every ledger carries the same dispatch id from a second
project, which must not leak into the row.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

PROJECT = "vnx-dev"
OTHER = "other-project"


def _line(did: str, status: str, ts: str, *, project: str = PROJECT, **kw: Any) -> Dict[str, Any]:
    return {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
            "status": status, "project_id": project, "terminal": "T1", "timestamp": ts, **kw}


def _rows(tmp_path: Path, entries: List[Dict[str, Any]], project_id: str = PROJECT,
          limit: int = 20) -> List[Dict[str, Any]]:
    from build_t0_state import _build_recent_receipts
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "t0_receipts.ndjson").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    os.environ.pop("VNX_USE_CENTRAL_DB", None)
    return _build_recent_receipts(state_dir, project_id=project_id, limit=limit)


def _row(rows: List[Dict[str, Any]], did: str) -> Dict[str, Any]:
    matching = [r for r in rows if r["dispatch_id"] == did]
    assert len(matching) == 1, f"expected one row for {did}, got {matching}"
    return matching[0]


def test_later_failure_after_earlier_success_shows_failure(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [
        _line("d1", "success", "2026-09-29T10:00:00Z"),
        _line("d1", "failure", "2026-09-29T11:00:00Z"),
        _line("d1", "success", "2026-09-29T12:00:00Z", project=OTHER),
    ])
    row = _row(rows, "d1")
    assert row["status"] == "failure"
    assert row["next_action"] == "fix_needed"


def test_retry_success_after_failure_shows_success(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [
        _line("d2", "failure", "2026-09-29T10:00:00Z"),
        _line("d2", "success", "2026-09-29T11:00:00Z"),
        _line("d2", "failure", "2026-09-29T12:00:00Z", project=OTHER),
    ])
    assert _row(rows, "d2")["status"] == "success"


def test_writer_b_done_does_not_overwrite_writer_a_failure(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [
        _line("d3", "failure", "2026-09-29T10:00:00Z"),
        _line("d3", "done", "2026-09-29T10:00:05Z", report_file="d3.md"),
    ])
    row = _row(rows, "d3")
    assert row["status"] == "failure"


def test_contract_invalid_after_success_is_a_reject_row(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [
        _line("d4", "success", "2026-09-29T10:00:00Z"),
        {"event_type": "report_contract_invalid", "dispatch_id": "d4", "status": "contract_invalid",
         "project_id": PROJECT, "timestamp": "2026-09-29T10:05:00Z"},
    ])
    row = _row(rows, "d4")
    assert row["status"] == "contract_invalid"
    assert row["next_action"] == "verify"


def test_temp_dir_and_magicmock_receipts_are_noise(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [
        _line("real", "success", "2026-09-29T10:00:00Z"),
        _line("leak-a", "success", "2026-09-29T10:01:00Z", report_path="/var/folders/xx/leak.md"),
        _line("leak-b", "success", "2026-09-29T10:02:00Z", title="<MagicMock id='1'>"),
        _line("DISP-007", "success", "2026-09-29T10:03:00Z", source="pytest"),
    ])
    assert [r["dispatch_id"] for r in rows] == ["real"]


def test_bookkeeping_and_id_less_lines_are_not_rows(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [
        _line("real", "success", "2026-09-29T10:00:00Z"),
        {"event_type": "review_gate_request", "dispatch_id": "real", "status": "requested",
         "project_id": PROJECT, "timestamp": "2026-09-29T10:01:00Z"},
        {"event_type": "state_mutation", "dispatch_id": "unknown", "project_id": PROJECT,
         "timestamp": "2026-09-29T10:02:00Z"},
        _line("unknown", "success", "2026-09-29T10:03:00Z"),
    ])
    assert [r["dispatch_id"] for r in rows] == ["real"]
    assert rows[0]["event_type"] == "task_complete"


def test_row_keeps_terminal_commit_and_pr_from_the_dispatch_lines(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [
        _line("d5", "success", "2026-09-29T10:00:00Z", commit_hash="abc123", pr_id="77"),
        _line("d5", "done", "2026-09-29T10:00:05Z", report_file="d5.md",
              report_path="/reports/d5.md"),
    ])
    row = _row(rows, "d5")
    assert row["terminal"] == "T1"
    assert row["commit_hash"] == "abc123"
    assert row["pr_id"] == "77"
    assert row["report_evidence_path"] == "/reports/d5.md"


def test_colliding_id_in_other_project_never_creates_or_changes_a_row(tmp_path: Path) -> None:
    rows = _rows(tmp_path, [
        _line("shared", "failure", "2026-09-29T10:00:00Z", project=OTHER),
        _line("only-mine", "success", "2026-09-29T10:01:00Z"),
    ])
    assert [r["dispatch_id"] for r in rows] == ["only-mine"]


def test_rows_are_newest_first_and_limited(tmp_path: Path) -> None:
    entries = [_line(f"d{i:02d}", "success", f"2026-09-29T{i:02d}:00:00Z") for i in range(10)]
    rows = _rows(tmp_path, entries, limit=3)
    assert [r["dispatch_id"] for r in rows] == ["d09", "d08", "d07"]
