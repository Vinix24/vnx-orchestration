#!/usr/bin/env python3
"""One-time intelligence backfill from historical governance data.

Reads t0_receipts.ndjson and receipts/processed/*.json to populate
the quality_intelligence.db tables (success_patterns, antipatterns,
dispatch_metadata) that were empty after the initial schema creation.

Safe to re-run — uses upsert logic from intelligence_persist.py.

Usage:
    python3 scripts/intelligence_backfill.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Resolve paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from governance_signal_extractor import collect_governance_signals
from intelligence_persist import persist_signals_to_db

STATE_DIR = PROJECT_ROOT / ".vnx-data" / "state"
DB_PATH = STATE_DIR / "quality_intelligence.db"
RECEIPTS_NDJSON = STATE_DIR / "t0_receipts.ndjson"
PROCESSED_DIR = PROJECT_ROOT / ".vnx-data" / "receipts" / "processed"


def load_gate_results() -> list[dict]:
    """Extract gate result records from t0_receipts.ndjson."""
    results: list[dict] = []
    if not RECEIPTS_NDJSON.exists():
        return results

    gate_event_map = {
        "task_complete": "pass",
        "task_success": "pass",
        "gate_pass": "pass",
        "task_failed": "fail",
        "gate_fail": "fail",
        "gate_failure": "fail",
        "task_timeout": "fail",
    }

    with open(RECEIPTS_NDJSON, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                receipt = json.loads(raw)
            except json.JSONDecodeError:
                continue

            gate = receipt.get("gate") or receipt.get("gate_id", "")
            event_type = receipt.get("event_type", "")

            if event_type == "review_gate_result":
                if not gate:
                    continue
                embedded_status = receipt.get("status", "")
                if embedded_status in ("pass", "passed", "success", "approve"):
                    mapped_status = "pass"
                elif embedded_status in ("fail", "failed"):
                    mapped_status = "fail"
                else:
                    continue
                results.append({
                    "gate_id": gate,
                    "status": mapped_status,
                    "feature_id": receipt.get("feature_id", ""),
                    "pr_id": receipt.get("pr", receipt.get("pr_id", "")),
                    "dispatch_id": receipt.get("dispatch_id", ""),
                    "reason": receipt.get("error", receipt.get("reason", receipt.get("summary", ""))),
                })
                continue

            if not gate or event_type not in gate_event_map:
                continue

            results.append({
                "gate_id": gate,
                "status": gate_event_map[event_type],
                "feature_id": receipt.get("feature_id", ""),
                "pr_id": receipt.get("pr", receipt.get("pr_id", "")),
                "dispatch_id": receipt.get("dispatch_id", ""),
                "reason": receipt.get("error", receipt.get("reason", "")),
            })

    return results


def load_queue_anomalies() -> list[dict]:
    """Extract queue anomaly records from t0_receipts.ndjson."""
    anomalies: list[dict] = []
    if not RECEIPTS_NDJSON.exists():
        return anomalies

    anomaly_types = frozenset({
        "delivery_failure", "reconcile_error", "ack_timeout",
        "dead_letter", "queue_stall", "task_timeout",
    })

    with open(RECEIPTS_NDJSON, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                receipt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            et = receipt.get("event_type", "")
            if et in anomaly_types:
                # Remap task_timeout → ack_timeout so the extractor recognizes it
                if et == "task_timeout":
                    receipt = dict(receipt)
                    receipt["event_type"] = "ack_timeout"
                    receipt["reason"] = receipt.get("recommendation", receipt.get("reason", "heartbeat ack timeout"))
                anomalies.append(receipt)

    return anomalies


def backfill_dispatch_metadata() -> int:
    """Populate dispatch_metadata from receipts/processed/*.json."""
    if not PROCESSED_DIR.exists() or not DB_PATH.exists():
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0

    try:
        for receipt_file in sorted(PROCESSED_DIR.glob("*.json")):
            try:
                receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            dispatch_id = receipt.get("dispatch_id", "")
            if not dispatch_id or dispatch_id == "unknown":
                continue

            terminal = receipt.get("terminal", "")
            track = receipt.get("track", "")
            gate = receipt.get("gate", "")
            status = receipt.get("status", "")
            timestamp = receipt.get("timestamp", now)
            title = receipt.get("title", "")

            outcome = None
            if status in ("success", "pass", "passed"):
                outcome = "success"
            elif status in ("fail", "failed", "failure"):
                outcome = "failure"

            existing = conn.execute(
                "SELECT id FROM dispatch_metadata WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()

            if existing:
                if outcome:
                    conn.execute(
                        "UPDATE dispatch_metadata SET outcome_status = COALESCE(outcome_status, ?), "
                        "completed_at = COALESCE(completed_at, ?) WHERE dispatch_id = ?",
                        (outcome, timestamp, dispatch_id),
                    )
                continue

            conn.execute(
                "INSERT INTO dispatch_metadata "
                "(dispatch_id, terminal, track, gate, dispatched_at, completed_at, outcome_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (dispatch_id, terminal, track, gate, timestamp,
                 timestamp if outcome else None, outcome),
            )
            inserted += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return inserted


def verify_counts() -> dict[str, int]:
    """Return row counts for the three target tables."""
    conn = sqlite3.connect(str(DB_PATH))
    counts = {}
    for table in ("success_patterns", "antipatterns", "dispatch_metadata"):
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        counts[table] = row[0] if row else 0
    conn.close()
    return counts


def main() -> None:
    print("=" * 60)
    print("VNX Intelligence Backfill")
    print("=" * 60)

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        sys.exit(1)

    # Step 1: Extract signals from t0_receipts.ndjson
    print("\n[1/4] Loading gate results and queue anomalies from t0_receipts.ndjson...")
    gate_results = load_gate_results()
    queue_anomalies = load_queue_anomalies()
    print(f"      Gate results: {len(gate_results)}")
    print(f"      Queue anomalies: {len(queue_anomalies)}")

    # Step 2: Collect governance signals and persist
    print("\n[2/4] Collecting governance signals and persisting to DB...")
    signals = collect_governance_signals(
        gate_results=gate_results or None,
        queue_anomalies=queue_anomalies or None,
        normalize_families=False,  # Keep raw signals so persist can classify them
        max_signals=500,  # Higher limit for backfill
    )
    print(f"      Signals extracted: {len(signals)}")

    if signals:
        result = persist_signals_to_db(signals, DB_PATH)
        print(f"      Patterns upserted: {result['patterns_upserted']}")
        print(f"      Antipatterns upserted: {result['antipatterns_upserted']}")
        print(f"      Metadata updated: {result['metadata_updated']}")
    else:
        print("      No signals extracted — check t0_receipts.ndjson content")

    # Step 3: Backfill dispatch_metadata from processed receipts
    print("\n[3/4] Backfilling dispatch_metadata from receipts/processed/...")
    dispatch_count = backfill_dispatch_metadata()
    print(f"      Dispatch records inserted: {dispatch_count}")

    # Step 4: Verify
    print("\n[4/4] Verification:")
    counts = verify_counts()
    all_ok = True
    for table, count in counts.items():
        status = "OK" if count > 0 else "EMPTY"
        if count == 0:
            all_ok = False
        print(f"      {table}: {count} rows [{status}]")

    print("\n" + "=" * 60)
    if all_ok:
        print("Backfill complete — all tables populated.")
    else:
        print("WARNING: Some tables are still empty. Check data sources.")
    print("=" * 60)


if __name__ == "__main__":
    main()
