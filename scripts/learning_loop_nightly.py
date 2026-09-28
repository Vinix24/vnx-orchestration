#!/usr/bin/env python3
"""Nightly learning-loop phase (shadow by default), with a beacon per run.

Runs ``LearningLoop.daily_learning_cycle()``. Without ``VNX_LEARNING_LOOP_PERSIST=1``
the cycle is a shadow run: it computes patterns and proposals, writes one report
(``learning_loop_shadow_report.json``) and persists nothing. Every run, also a
failed one, writes ``learning_loop_nightly_beacon.json`` under the state dir.

Exit code: 0 on a finished run, 1 when the run failed (the beacon then says
``status=failed`` and carries the reason).
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from learning_loop_beacon import (  # noqa: E402
    STATUS_FAILED,
    STATUS_OK,
    atomic_write_json,
    shadow_report_path,
    utc_now_iso,
    write_beacon,
)
from vnx_paths import (  # noqa: E402
    ensure_env,
    refuse_real_central_store_write_under_test_runner,
    resolve_project_id,
)


def _counts(report: Dict[str, Any]) -> Dict[str, int]:
    stats = report.get("statistics") or {}
    shadow = report.get("shadow") or {}
    return {
        "receipts_read": int(stats.get("receipts_read", 0)),
        "receipts_after_cutoff": int(stats.get("receipts_after_cutoff", 0)),
        "patterns": int(stats.get("failure_patterns", 0)),
        "patterns_used": int(stats.get("patterns_used", 0)),
        "patterns_ignored": int(stats.get("patterns_ignored", 0)),
        "proposals": (
            int(stats.get("proposal_count", 0))
            + len(shadow.get("archival_candidates") or [])
            + int(shadow.get("supersede_candidates", 0))
        ),
        "skipped": int(stats.get("receipts_skipped", 0)),
        "open_items_read": int(stats.get("open_items_read", 0)),
        "open_item_signals": int(stats.get("open_item_signals", 0)),
    }


def run_phase(state_dir: Optional[Path] = None) -> int:
    from learning_loop import LearningLoop, persist_enabled, resolve_receipt_cutoff

    started = utc_now_iso()
    beacon: Dict[str, Any] = {
        "component": "learning_loop_nightly",
        "project_id": resolve_project_id(),
        "started_at": started,
        "finished_at": None,
        "mode": "persist" if persist_enabled() else "shadow",
        "cutoff": None,
        "counts": {},
        "status": STATUS_FAILED,
        "error": None,
    }
    if state_dir is None:
        state_dir = Path(ensure_env()["VNX_STATE_DIR"]).expanduser().resolve()
    refuse_real_central_store_write_under_test_runner(state_dir)

    exit_code = 1
    try:
        beacon["cutoff"] = resolve_receipt_cutoff().isoformat()
        report = LearningLoop().daily_learning_cycle()
        beacon["counts"] = _counts(report)
        beacon["cutoff"] = report.get("cutoff", beacon["cutoff"])
        run_status = report.get("status")
        beacon["status"] = run_status if run_status in ("dormant", "degraded") else STATUS_OK
        if beacon["mode"] == "shadow":
            path = shadow_report_path(state_dir)
            atomic_write_json(path, {"generated_at": utc_now_iso(), "report": report})
            beacon["report_path"] = str(path)
        exit_code = 0
    except Exception as exc:  # the beacon must record whatever broke the run
        beacon["status"] = STATUS_FAILED
        beacon["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        beacon["finished_at"] = utc_now_iso()
        write_beacon(state_dir, beacon)

    if exit_code:
        print(
            f"WARNING: learning loop phase FAILED ({beacon['error']}); "
            "beacon status=failed",
            file=sys.stderr,
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(run_phase())
