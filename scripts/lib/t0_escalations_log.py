#!/usr/bin/env python3
"""T0 escalations log — passive JSONL writer for structured escalation records.

Two write paths:

1. Real-time adapter (called directly from governance_escalation and decision_executor):
   from t0_escalations_log import write_escalation, build_record
   write_escalation(build_record(...))

2. Batch replay from executor events file (CLI):
   python3 scripts/lib/t0_escalations_log.py
   python3 scripts/lib/t0_escalations_log.py --events-file "$VNX_DATA_DIR/events/t0_decisions.ndjson"
   python3 scripts/lib/t0_escalations_log.py --dry-run

Escalation record schema:
  {
    "timestamp":           "ISO-8601",
    "entity_type":         "string or null",
    "entity_id":           "string or null",
    "escalation_level":    "info|review_required|hold|escalate",
    "from_level":          "string or null",
    "trigger_category":    "string or null",
    "trigger_description": "string or null",
    "actor":               "string",
    "source":              "executor|governance"
  }

The batch-replay path reads t0_decisions.ndjson for t0_escalate events only,
applying inode-based cursor tracking identical to t0_decision_log.py so runs
are idempotent and incremental.

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls. CLI-only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _data_dir() -> Path:
    vnx_data = os.environ.get("VNX_DATA_DIR")
    if vnx_data:
        return Path(vnx_data).expanduser().resolve()
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent / ".vnx-data"


DEFAULT_EVENTS_FILE: Path = _data_dir() / "events" / "t0_decisions.ndjson"
DEFAULT_ESCALATION_LOG: Path = _data_dir() / "state" / "t0_escalations.jsonl"
DEFAULT_CURSOR_FILE: Path = _data_dir() / "state" / "t0_escalations_cursor.json"

VALID_ESCALATION_LEVELS = frozenset({
    "info",
    "review_required",
    "hold",
    "escalate",
})

VALID_SOURCES = frozenset({"executor", "governance"})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Schema builder
# ---------------------------------------------------------------------------


def build_record(
    escalation_level: str,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    from_level: str | None = None,
    trigger_category: str | None = None,
    trigger_description: str | None = None,
    actor: str = "runtime",
    source: str = "executor",
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build an escalation log record conforming to the shared schema.

    All callers (direct writers and batch replay) funnel through here so the
    record shape is always consistent.
    """
    return {
        "timestamp": timestamp or _now_iso(),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "escalation_level": escalation_level,
        "from_level": from_level,
        "trigger_category": trigger_category,
        "trigger_description": trigger_description,
        "actor": actor,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Event conversion — executor path
# ---------------------------------------------------------------------------


def record_from_executor_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a t0_escalate event into an escalation log record.

    Returns None for non-escalation events so callers can skip them.
    Only processes event_type == 't0_escalate'.
    """
    if event.get("event_type") != "t0_escalate":
        return None

    reason = str(event.get("reason", "Escalation triggered"))
    timestamp = event.get("timestamp") or _now_iso()

    return build_record(
        "escalate",
        trigger_description=reason,
        actor="t0",
        source="executor",
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Record builder — governance path
# ---------------------------------------------------------------------------


def record_from_governance_transition(
    *,
    entity_type: str,
    entity_id: str,
    from_level: str,
    new_level: str,
    actor: str,
    trigger_category: str | None = None,
    trigger_description: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build an escalation log record from a governance state transition.

    Called directly from governance_escalation.transition_escalation() so the
    JSONL log captures rich entity+trigger data without a DB query.
    """
    return build_record(
        new_level,
        entity_type=entity_type,
        entity_id=entity_id,
        from_level=from_level,
        trigger_category=trigger_category,
        trigger_description=trigger_description,
        actor=actor,
        source="governance",
        timestamp=timestamp or _now_iso(),
    )


# ---------------------------------------------------------------------------
# File I/O — write
# ---------------------------------------------------------------------------


def write_escalation(record: dict[str, Any], log_file: Path | None = None) -> None:
    """Append an escalation record to the JSONL log with exclusive file locking."""
    path = log_file or DEFAULT_ESCALATION_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Cursor tracking
# ---------------------------------------------------------------------------


def _load_cursor_state(cursor_file: Path) -> dict[str, Any]:
    """Return full cursor state dict (processed_lines, inode).

    Backward-compatible: old cursor files without 'inode' return inode=None.
    """
    if not cursor_file.exists():
        return {"processed_lines": 0}
    try:
        return json.loads(cursor_file.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_lines": 0}


def _save_cursor_state(cursor_file: Path, processed_lines: int, inode: int) -> None:
    """Persist cursor with inode so source-identity changes can be detected."""
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    state = {"processed_lines": processed_lines, "inode": inode}
    cursor_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def load_cursor(cursor_file: Path) -> int:
    """Return the number of lines already processed (0 if cursor absent)."""
    return int(_load_cursor_state(cursor_file).get("processed_lines", 0))


def save_cursor(cursor_file: Path, processed_lines: int) -> None:
    """Persist cursor (without inode — legacy compat shim)."""
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    cursor_file.write_text(
        json.dumps({"processed_lines": processed_lines}) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Batch replay
# ---------------------------------------------------------------------------


def process_events_file(
    events_file: Path,
    log_file: Path,
    cursor_file: Path,
    *,
    dry_run: bool = False,
) -> int:
    """Read unprocessed t0_escalate events and append escalation records.

    Args:
        events_file:  Path to t0_decisions.ndjson (written by decision_executor).
        log_file:     Destination JSONL escalation log.
        cursor_file:  Cursor file tracking how many lines have been processed.
        dry_run:      Print records to stdout, do not write to log or cursor.

    Returns:
        Number of new escalation records written (or that would be written in dry-run).
    """
    if not events_file.exists():
        logger.info("t0_escalations_log: events file not found: %s", events_file)
        return 0

    all_lines = events_file.read_text(encoding="utf-8").splitlines()

    cursor_state = _load_cursor_state(cursor_file)
    cursor = int(cursor_state.get("processed_lines", 0))
    saved_inode = cursor_state.get("inode")

    try:
        current_inode = os.stat(events_file).st_ino
    except OSError:
        current_inode = 0

    # Inode mismatch: source file was replaced → reset cursor
    if saved_inode is not None and saved_inode != 0 and current_inode != 0 and current_inode != saved_inode:
        logger.warning(
            "t0_escalations_log: source file replaced (inode %d → %d) — resetting cursor to 0",
            saved_inode,
            current_inode,
        )
        cursor = 0

    # Line-count guard: reset if cursor exceeds current file length
    if cursor > len(all_lines):
        logger.warning(
            "t0_escalations_log: cursor %d exceeds file length %d — resetting to 0",
            cursor,
            len(all_lines),
        )
        cursor = 0

    unprocessed = all_lines[cursor:]

    written = 0
    parsed_count = 0

    for idx, raw_line in enumerate(unprocessed):
        raw_line = raw_line.strip()
        if not raw_line:
            parsed_count += 1
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            is_last = idx == len(unprocessed) - 1
            if is_last:
                # Partial trailing write — do not advance cursor past it
                break
            logger.warning(
                "t0_escalations_log: malformed line skipped at position %d",
                cursor + idx,
            )
            parsed_count += 1
            continue

        record = record_from_executor_event(event)
        if record is None:
            parsed_count += 1
            continue

        if dry_run:
            print(json.dumps(record, indent=2))
        else:
            write_escalation(record, log_file)

        written += 1
        parsed_count += 1

    new_cursor = cursor + parsed_count

    if not dry_run:
        upgrade_only = saved_inode is None and new_cursor == cursor
        if new_cursor > cursor or upgrade_only:
            _save_cursor_state(cursor_file, new_cursor, current_inode)
        if new_cursor > cursor:
            logger.info(
                "t0_escalations_log: processed %d new events, wrote %d escalation records",
                new_cursor - cursor,
                written,
            )
        elif upgrade_only:
            logger.info(
                "t0_escalations_log: upgraded legacy cursor with inode %d (no new events)",
                current_inode,
            )

    return written


# ---------------------------------------------------------------------------
# Query interface — context assembly
# ---------------------------------------------------------------------------


def load_recent_escalations(
    log_file: Path | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return the N most recent escalation records from the log.

    Used by context assemblers to surface active escalations in T0 prompts.
    Returns an empty list if the log doesn't exist.
    """
    path = log_file or DEFAULT_ESCALATION_LOG
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return records[-limit:] if len(records) > limit else records


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns:
        0 on success (including no-new-events case)
        1 on unrecoverable errors
    """
    parser = argparse.ArgumentParser(
        description="Passive escalation log writer — converts t0_escalate events to JSONL records."
    )
    parser.add_argument(
        "--events-file",
        type=Path,
        default=DEFAULT_EVENTS_FILE,
        help="Path to t0_decisions NDJSON events file",
    )
    parser.add_argument(
        "--escalation-log",
        type=Path,
        default=DEFAULT_ESCALATION_LOG,
        help="Path to escalation log JSONL file",
    )
    parser.add_argument(
        "--cursor-file",
        type=Path,
        default=DEFAULT_CURSOR_FILE,
        help="Path to cursor tracking file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print records to stdout without writing to log or advancing cursor",
    )
    args = parser.parse_args(argv)

    written = process_events_file(
        args.events_file,
        args.escalation_log,
        args.cursor_file,
        dry_run=args.dry_run,
    )

    if written == 0:
        logger.info("t0_escalations_log: no new escalation events to process")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
