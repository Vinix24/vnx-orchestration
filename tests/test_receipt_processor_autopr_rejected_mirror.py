"""Tests for OI-626: receipt_processor.sh must mirror the autopr_rejected
outcome_status downgrade the python lanes already apply.

dispatch_govern.dedup_completion_receipts' Tier-0 override treats an
autopr_rejected corrective receipt (pr_enforcement.py: branch pushed but no PR
found/created) the same way it treats a phantom_rejected one — final,
overriding the worker's own claim. receipt_processor.sh:185's inline python
snippet (_psr_update_dispatch_outcome) previously only checked
phantom_rejected when downgrading the persisted quality_intelligence.db
outcome_status, so an autopr-rejected dispatch stayed 'done' in the shell-
processed intelligence DB while dedup_completion_receipts resolved it as
rejected — an inconsistent audit trail across the two paths.

Exercises the REAL _psr_update_dispatch_outcome by sourcing receipt_processor.sh
in _RP_LIB_MODE=1 (same pattern as test_receipt_processor_bootstrap.py), not a
reimplementation of its logic.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RP_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "receipt_processor.sh"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
from dispatch_govern import dedup_completion_receipts  # noqa: E402


def _init_dispatch_metadata_db(db_path: Path, dispatch_id: str) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            terminal TEXT NOT NULL,
            track TEXT NOT NULL,
            outcome_status TEXT,
            outcome_report_path TEXT,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO dispatch_metadata (dispatch_id, project_id, terminal, track, outcome_status) "
        "VALUES (?, 'vnx-dev', 'T1', 'A', NULL)",
        (dispatch_id,),
    )
    conn.commit()
    conn.close()


def _read_outcome_status(db_path: Path, dispatch_id: str) -> "str | None":
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT outcome_status FROM dispatch_metadata WHERE dispatch_id = ?",
        (dispatch_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _run_update_dispatch_outcome(
    dispatch_id: str,
    status: str,
    receipts: "list[dict] | None" = None,
    event_type: str = "task_complete",
) -> "tuple[int, str, str | None]":
    """Source the real receipt_processor.sh and invoke _psr_update_dispatch_outcome.

    Returns (returncode, stderr, final_outcome_status_from_db).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        state = tmp / "state"
        data_dir = tmp / "data"
        unified = tmp / "unified"
        headless = tmp / "headless"
        pids = tmp / "pids"
        locks = tmp / "locks"
        for d in (state, data_dir, unified, headless, pids, locks):
            d.mkdir(parents=True)

        db_path = state / "quality_intelligence.db"
        _init_dispatch_metadata_db(db_path, dispatch_id)

        if receipts:
            ledger = state / "t0_receipts.ndjson"
            with ledger.open("w", encoding="utf-8") as f:
                for r in receipts:
                    f.write(json.dumps(r) + "\n")

        timestamp = "2026-07-16T12:00:00Z"

        bash_cmd = f"""
set -e
export _RP_LIB_MODE=1
export VNX_DATA_DIR="{data_dir}"
export VNX_DATA_DIR_EXPLICIT=1
export VNX_STATE_DIR="{state}"
export VNX_PIDS_DIR="{pids}"
export VNX_LOCKS_DIR="{locks}"
export VNX_REPORTS_DIR="{unified}"
export VNX_HEADLESS_REPORTS_DIR="{headless}"
source "{RP_SCRIPT}" || {{ echo "FATAL: source {RP_SCRIPT} failed" >&2; exit 1; }}

_psr_update_dispatch_outcome "{dispatch_id}" "{event_type}" "{status}" "" "{timestamp}"
"""
        result = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)
        final_status = _read_outcome_status(db_path, dispatch_id)
        return result.returncode, result.stderr, final_status


# ---------------------------------------------------------------------------
# receipt_processor.sh shell-lane behavior
# ---------------------------------------------------------------------------

def test_autopr_rejected_receipt_downgrades_outcome_to_failed():
    """A worker-claimed 'done' status must be downgraded to 'failed' in the
    intelligence DB when a corrective autopr_rejected receipt exists for the
    same dispatch — mirroring the python-lane Tier-0 override."""
    dispatch_id = "20260716-autopr-rejected-test"
    rc, stderr, final_status = _run_update_dispatch_outcome(
        dispatch_id,
        "done",
        receipts=[
            {
                "event_type": "subprocess_completion",
                "dispatch_id": dispatch_id,
                "status": "failed",
                "autopr_rejected": True,
                "autopr_reason": "gh pr create failed for branch dispatch/20260716-autopr-rejected-test",
                "source": "pr_enforcement",
                "timestamp": "2026-07-16T11:59:00Z",
            }
        ],
    )
    assert rc == 0, f"_psr_update_dispatch_outcome failed rc={rc}:\n{stderr}"
    assert final_status == "failed", (
        f"Expected outcome_status downgraded to 'failed' via autopr_rejected mirror, "
        f"got {final_status!r}. stderr:\n{stderr}"
    )


def test_phantom_rejected_receipt_still_downgrades_outcome_to_failed():
    """Regression guard: the pre-existing phantom_rejected mirror must keep working
    after extending the condition to also cover autopr_rejected."""
    dispatch_id = "20260716-phantom-rejected-test"
    rc, stderr, final_status = _run_update_dispatch_outcome(
        dispatch_id,
        "done",
        receipts=[
            {
                "event_type": "subprocess_completion",
                "dispatch_id": dispatch_id,
                "status": "failed",
                "phantom_rejected": True,
                "source": "phantom_guard",
                "timestamp": "2026-07-16T11:59:00Z",
            }
        ],
    )
    assert rc == 0, f"_psr_update_dispatch_outcome failed rc={rc}:\n{stderr}"
    assert final_status == "failed"


def test_no_corrective_receipt_keeps_worker_claimed_status():
    """No phantom_rejected / autopr_rejected receipt on the ledger for this
    dispatch → the worker-claimed status is persisted as-is."""
    dispatch_id = "20260716-no-override-test"
    rc, stderr, final_status = _run_update_dispatch_outcome(
        dispatch_id,
        "done",
        receipts=[
            {
                "event_type": "subprocess_completion",
                "dispatch_id": dispatch_id,
                "status": "done",
                "source": "tmux_interactive",
                "timestamp": "2026-07-16T11:59:00Z",
            }
        ],
    )
    assert rc == 0, f"_psr_update_dispatch_outcome failed rc={rc}:\n{stderr}"
    assert final_status == "done"


def test_autopr_rejected_false_does_not_trigger_downgrade():
    """An explicit autopr_rejected=False on an unrelated receipt must not
    trigger the override (only autopr_rejected is True counts)."""
    dispatch_id = "20260716-autopr-false-test"
    rc, stderr, final_status = _run_update_dispatch_outcome(
        dispatch_id,
        "done",
        receipts=[
            {
                "event_type": "subprocess_completion",
                "dispatch_id": dispatch_id,
                "status": "done",
                "autopr_rejected": False,
                "source": "tmux_interactive",
                "timestamp": "2026-07-16T11:59:00Z",
            }
        ],
    )
    assert rc == 0, f"_psr_update_dispatch_outcome failed rc={rc}:\n{stderr}"
    assert final_status == "done"


def test_other_dispatch_autopr_rejection_does_not_leak():
    """An autopr_rejected receipt for a DIFFERENT dispatch_id must not affect
    this one (dispatch_id scoping)."""
    dispatch_id = "20260716-scoped-test"
    rc, stderr, final_status = _run_update_dispatch_outcome(
        dispatch_id,
        "done",
        receipts=[
            {
                "event_type": "subprocess_completion",
                "dispatch_id": "some-other-dispatch",
                "status": "failed",
                "autopr_rejected": True,
                "source": "pr_enforcement",
                "timestamp": "2026-07-16T11:59:00Z",
            }
        ],
    )
    assert rc == 0, f"_psr_update_dispatch_outcome failed rc={rc}:\n{stderr}"
    assert final_status == "done"


# ---------------------------------------------------------------------------
# Parity — the shell lane and the python lane (dispatch_govern) must classify
# the exact same autopr_rejected receipt set identically.
# ---------------------------------------------------------------------------

def test_shell_and_python_lane_agree_on_autopr_rejected_classification():
    """Same receipt set fed to both the shell processor's outcome-downgrade
    check and dispatch_govern.dedup_completion_receipts (the python lane) must
    agree that the dispatch is rejected/failed, not done."""
    dispatch_id = "20260716-parity-test"
    receipts = [
        {
            "event_type": "subprocess_completion",
            "dispatch_id": dispatch_id,
            "status": "done",
            "source": "tmux_interactive",
            "timestamp": "2026-07-16T11:58:00Z",
        },
        {
            "event_type": "subprocess_completion",
            "dispatch_id": dispatch_id,
            "status": "failed",
            "autopr_rejected": True,
            "source": "pr_enforcement",
            "timestamp": "2026-07-16T11:59:00Z",
        },
    ]

    # Python lane
    preferred = dedup_completion_receipts(
        [r for r in receipts if r["dispatch_id"] == dispatch_id]
    )
    assert preferred is not None
    assert preferred["status"] == "failed", "python lane must resolve as failed"

    # Shell lane
    rc, stderr, final_status = _run_update_dispatch_outcome(
        dispatch_id, "done", receipts=receipts,
    )
    assert rc == 0, f"_psr_update_dispatch_outcome failed rc={rc}:\n{stderr}"
    assert final_status == "failed", "shell lane must resolve as failed to match the python lane"
    assert final_status == preferred["status"], (
        f"shell lane ({final_status!r}) and python lane ({preferred['status']!r}) "
        "must classify the same autopr_rejected receipt identically"
    )
