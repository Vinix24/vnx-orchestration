#!/usr/bin/env python3
"""Standalone CQS updater for dispatch_metadata.

Called from receipt_processor_v4.sh after dispatch outcome is recorded.
Reads receipt and session data, computes CQS, and updates the database.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env
from cqs_calculator import calculate_cqs


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Update CQS for a dispatch")
    parser.add_argument("--dispatch-id", required=True)
    args = parser.parse_args()

    paths = ensure_env()
    db_path = Path(paths["VNX_STATE_DIR"]) / "quality_intelligence.db"
    if not db_path.exists():
        return 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        "SELECT * FROM dispatch_metadata WHERE dispatch_id = ?", (args.dispatch_id,)
    ).fetchone()
    if not row:
        conn.close()
        return 0

    row_keys = set(row.keys())

    def _field(key, default=None):
        return row[key] if key in row_keys else default

    def _parse_json_field(value: str | None) -> dict | list | None:
        if not value:
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None

    receipt: dict = {
        "status": row["outcome_status"],
        "report_path": row["outcome_report_path"],
        "role": row["role"],
        "gate": row["gate"],
        "pr_id": row["pr_id"],
        "open_items_created": _field("open_items_created") or 0,
        "open_items_resolved": _field("open_items_resolved") or 0,
        "target_open_items": _parse_json_field(_field("target_open_items")),
    }
    qa = _parse_json_field(_field("quality_advisory_json"))
    if qa is not None:
        receipt["quality_advisory"] = qa

    session = None
    sa_row = conn.execute(
        "SELECT * FROM session_analytics WHERE dispatch_id = ?", (args.dispatch_id,)
    ).fetchone()
    if sa_row:
        session = dict(sa_row)

    result = calculate_cqs(receipt, session, db_path, args.dispatch_id)

    conn.execute(
        "UPDATE dispatch_metadata SET cqs = ?, normalized_status = ?, cqs_components = ? WHERE dispatch_id = ?",
        (result["cqs"], result["normalized_status"], json.dumps(result["components"]), args.dispatch_id),
    )
    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
