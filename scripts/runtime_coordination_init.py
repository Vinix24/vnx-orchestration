#!/usr/bin/env python3
"""
VNX Runtime Coordination Database Initialization

Idempotent initialization of the runtime coordination schema.
Safe to run multiple times — uses CREATE TABLE IF NOT EXISTS and
INSERT OR IGNORE throughout; never drops or truncates existing data.

Usage:
    python scripts/runtime_coordination_init.py
    python scripts/runtime_coordination_init.py --state-dir /custom/path
    python scripts/runtime_coordination_init.py --verify-only
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_paths import ensure_env  # noqa: E402
from runtime_coordination import (  # noqa: E402
    DISPATCH_STATES,
    LEASE_STATES,
    db_path_from_state_dir,
    get_connection,
    init_schema,
)


class Colors:
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    RESET  = "\033[0m"


def log(level: str, message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = {"INFO": Colors.BLUE, "SUCCESS": Colors.GREEN,
             "WARNING": Colors.YELLOW, "ERROR": Colors.RED}.get(level, Colors.RESET)
    print(f"[{ts}] {color}[{level}]{Colors.RESET} {message}")


def verify_schema(state_dir: str) -> bool:
    """Verify all expected tables, indexes, and seed data exist."""
    db = db_path_from_state_dir(state_dir)
    if not db.exists():
        log("ERROR", f"Database not found: {db}")
        return False

    required_tables = {
        "runtime_schema_version",
        "dispatches",
        "dispatch_attempts",
        "terminal_leases",
        "coordination_events",
    }

    required_indexes = {
        "idx_dispatch_state",
        "idx_dispatch_terminal",
        "idx_dispatch_created",
        "idx_attempt_dispatch",
        "idx_attempt_state",
        "idx_attempt_terminal",
        "idx_lease_state",
        "idx_lease_dispatch",
        "idx_event_entity",
        "idx_event_type",
        "idx_event_occurred",
    }

    with get_connection(state_dir) as conn:
        actual_tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing_tables = required_tables - actual_tables
        if missing_tables:
            log("ERROR", f"Missing tables: {sorted(missing_tables)}")
            return False
        log("SUCCESS", f"All {len(required_tables)} tables present")

        actual_indexes = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        missing_indexes = required_indexes - actual_indexes
        if missing_indexes:
            log("WARNING", f"Missing indexes: {sorted(missing_indexes)}")
            # Non-fatal: indexes can be recreated
        else:
            log("SUCCESS", f"All {len(required_indexes)} indexes present")

        # Verify terminal lease seed rows
        count = conn.execute("SELECT COUNT(*) FROM terminal_leases").fetchone()[0]
        if count < 3:
            log("WARNING", f"Expected 3 terminal lease rows (T1/T2/T3), found {count}")
        else:
            log("SUCCESS", f"Terminal lease rows: {count}")

        # Verify schema version
        version_row = conn.execute(
            "SELECT version, description FROM runtime_schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if version_row:
            log("SUCCESS", f"Schema version: {version_row[0]} — {version_row[1]}")
        else:
            log("WARNING", "No schema version record found")

    return True


def print_state_documentation() -> None:
    """Print canonical state documentation to stdout."""
    print(f"\n{Colors.BLUE}{'─'*60}")
    print("Canonical Dispatch States")
    print(f"{'─'*60}{Colors.RESET}")
    state_docs = {
        "queued":          "Registered, not yet assigned to a terminal",
        "claimed":         "Terminal lease acquired, delivery not started",
        "delivering":      "Bundle is being sent to terminal",
        "accepted":        "Terminal has ACKed receipt",
        "running":         "Worker is actively executing",
        "completed":       "Worker reported success (T0 authority unchanged)",
        "timed_out":       "No ACK or heartbeat within deadline",
        "failed_delivery": "Transport failure (tmux error, pane gone)",
        "expired":         "Exceeded max attempt window",
        "recovered":       "Reconciler moved from failed state",
    }
    for state in sorted(DISPATCH_STATES):
        doc = state_docs.get(state, "")
        print(f"  {state:<20} {doc}")

    print(f"\n{Colors.BLUE}{'─'*60}")
    print("Canonical Lease States")
    print(f"{'─'*60}{Colors.RESET}")
    lease_docs = {
        "idle":       "Available for assignment",
        "leased":     "Owned by a dispatch, heartbeat expected",
        "expired":    "TTL elapsed without heartbeat; awaiting recovery",
        "recovering": "Reconciler is recovering this lease",
        "released":   "Explicitly released (transient → idle)",
    }
    for state in sorted(LEASE_STATES):
        doc = lease_docs.get(state, "")
        print(f"  {state:<20} {doc}")

    print(f"\n{Colors.BLUE}{'─'*60}")
    print("Projection Notes")
    print(f"{'─'*60}{Colors.RESET}")
    print("  terminal_state.json  Derived projection of terminal_leases table.")
    print("                       NOT a source of truth for lease or dispatch state.")
    print("                       Regenerate via runtime_coordination.project_terminal_state().")
    print("  panes.json           tmux adapter mapping (pane IDs → terminal IDs).")
    print("                       NOT an ownership record. Pane remap does not")
    print("                       change dispatch or lease state.")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize VNX runtime coordination database")
    parser.add_argument("--state-dir", default=None, help="Override VNX state directory")
    parser.add_argument("--verify-only", action="store_true", help="Verify without initializing")
    parser.add_argument("--print-states", action="store_true", help="Print canonical state documentation")
    args = parser.parse_args()

    print(f"\n{Colors.BLUE}{'='*60}")
    print("VNX Runtime Coordination — Database Init")
    print(f"{'='*60}{Colors.RESET}\n")

    paths = ensure_env()
    state_dir = args.state_dir or paths["VNX_STATE_DIR"]
    db = db_path_from_state_dir(state_dir)

    log("INFO", f"State dir: {state_dir}")
    log("INFO", f"Database:  {db}")

    if args.print_states:
        print_state_documentation()

    if args.verify_only:
        ok = verify_schema(state_dir)
        return 0 if ok else 1

    # Initialize schema
    log("INFO", "Initializing runtime coordination schema...")
    try:
        init_schema(state_dir)
        log("SUCCESS", "Schema initialized (idempotent)")
    except FileNotFoundError as exc:
        log("ERROR", str(exc))
        return 1
    except Exception as exc:
        log("ERROR", f"Schema init failed: {exc}")
        return 1

    # Verify
    log("INFO", "Verifying schema...")
    if not verify_schema(state_dir):
        log("ERROR", "Schema verification failed")
        return 1

    # Size report
    if db.exists():
        size_kb = db.stat().st_size / 1024
        log("SUCCESS", f"Database ready — {size_kb:.1f} KB")

    print_state_documentation()

    # Save init report
    report = {
        "initialized_at": datetime.now().isoformat(),
        "state_dir": str(state_dir),
        "db_path": str(db),
        "db_size_bytes": db.stat().st_size if db.exists() else 0,
        "status": "ok",
    }
    report_path = Path(state_dir) / "runtime_coordination_init_report.json"
    report_path.write_text(json.dumps(report, indent=2))
    log("INFO", f"Init report: {report_path}")

    print(f"\n{Colors.GREEN}Runtime coordination database ready.{Colors.RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
