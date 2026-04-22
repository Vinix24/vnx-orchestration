#!/usr/bin/env python3
"""T0 decision log — passive writer for structured decision records.

Complements t0_decision_summarizer.py (haiku-powered) with a zero-LLM
path: converts already-structured decision events (from decision_executor
or direct callers) into decision log records and appends them to
.vnx-data/state/t0_decision_log.jsonl.

Two usage modes:

1. Direct write (real-time, called by decision_executor or other code):
   from t0_decision_log import write_decision
   write_decision(record)

2. Batch replay from events file (CLI):
   python3 scripts/lib/t0_decision_log.py
   python3 scripts/lib/t0_decision_log.py --events-file .vnx-data/events/t0_decisions.ndjson
   python3 scripts/lib/t0_decision_log.py --dry-run

Decision record schema (same as summarizer output):
  {
    "timestamp": "ISO-8601",
    "session_summary_at": "ISO-8601",
    "action": "dispatch|approve|reject|escalate|wait|close_oi|advance_gate",
    "dispatch_id": "string or null",
    "track": "A|B|C or null",
    "reasoning": "1-2 sentence summary",
    "open_items_actions": [],
    "next_expected": ""
  }

Cursor tracking: stores the number of processed lines in
.vnx-data/state/t0_decision_log_cursor.json so repeated runs are
idempotent — only unprocessed events are converted.

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
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

_TERMINAL_TO_TRACK: dict[str, str] = {
    "T1": "A",
    "T2": "B",
    "T3": "C",
}

# event_type → action mapping (executor events → decision log schema)
_EVENT_TYPE_TO_ACTION: dict[str, str] = {
    "t0_dispatch": "dispatch",
    "t0_wait": "wait",
    "t0_complete": "close_oi",
    "t0_reject": "reject",
    "t0_escalate": "escalate",
    "t0_unknown_decision": "wait",
}


def _data_dir() -> Path:
    vnx_data = os.environ.get("VNX_DATA_DIR")
    if vnx_data:
        return Path(vnx_data).expanduser().resolve()
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent / ".vnx-data"


DEFAULT_EVENTS_FILE: Path = _data_dir() / "events" / "t0_decisions.ndjson"
DEFAULT_DECISION_LOG: Path = _data_dir() / "state" / "t0_decision_log.jsonl"
DEFAULT_CURSOR_FILE: Path = _data_dir() / "state" / "t0_decision_log_cursor.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Schema builder
# ---------------------------------------------------------------------------


def build_record(
    action: str,
    reasoning: str = "",
    dispatch_id: str | None = None,
    track: str | None = None,
    open_items_actions: list[dict[str, Any]] | None = None,
    next_expected: str = "",
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build a decision log record conforming to the shared schema.

    All callers (direct writers and the batch replay path) funnel through
    here so the record shape is always consistent.
    """
    now = timestamp or _now_iso()
    return {
        "timestamp": now,
        "session_summary_at": now,
        "action": action,
        "dispatch_id": dispatch_id,
        "track": track,
        "reasoning": reasoning,
        "open_items_actions": open_items_actions if open_items_actions is not None else [],
        "next_expected": next_expected,
    }


# ---------------------------------------------------------------------------
# Event conversion
# ---------------------------------------------------------------------------


def record_from_executor_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a decision_executor event into a decision log record.

    Returns None for unrecognised event types so callers can skip them.

    Handles the following event_type values emitted by decision_executor:
      t0_dispatch, t0_wait, t0_complete, t0_reject, t0_escalate,
      t0_unknown_decision
    """
    event_type = event.get("event_type", "")
    action = _EVENT_TYPE_TO_ACTION.get(event_type)
    if action is None:
        return None

    timestamp = event.get("timestamp") or _now_iso()

    if event_type == "t0_dispatch":
        dispatch_id = event.get("dispatch_id")
        dispatch_target = str(event.get("dispatch_target", "")).upper()
        track = _TERMINAL_TO_TRACK.get(dispatch_target)
        trigger_reason = event.get("trigger_reason", "")
        reasoning = f"Dispatched to {dispatch_target}" + (f" — {trigger_reason}" if trigger_reason else "")
        next_expected = f"Receipt from {dispatch_target}" if dispatch_target else ""
        return build_record(
            action=action,
            reasoning=reasoning,
            dispatch_id=dispatch_id,
            track=track,
            next_expected=next_expected,
            timestamp=timestamp,
        )

    if event_type == "t0_escalate":
        reason = event.get("reason", "")
        return build_record(
            action=action,
            reasoning=reason or "Escalation triggered",
            next_expected="Operator review",
            timestamp=timestamp,
        )

    # wait / complete / reject / unknown — reason field is the human text
    reason = event.get("reason", "")
    if event_type == "t0_unknown_decision":
        raw = event.get("raw_decision", {})
        reason = f"Unknown decision type: {raw.get('decision', '?')}"
    return build_record(
        action=action,
        reasoning=reason,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# File I/O — write
# ---------------------------------------------------------------------------


def write_decision(record: dict[str, Any], log_file: Path | None = None) -> None:
    """Append a decision record to the JSONL log with exclusive file locking.

    This is the primary real-time API used by decision_executor or any
    code that has already built a decision record dict.
    """
    path = log_file or DEFAULT_DECISION_LOG
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


def load_cursor(cursor_file: Path) -> int:
    """Return the number of lines already processed (0 if cursor absent)."""
    if not cursor_file.exists():
        return 0
    try:
        data = json.loads(cursor_file.read_text(encoding="utf-8"))
        return int(data.get("processed_lines", 0))
    except Exception:
        return 0


def save_cursor(cursor_file: Path, processed_lines: int) -> None:
    """Persist the cursor so repeated runs skip already-processed events."""
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    cursor_file.write_text(
        json.dumps({"processed_lines": processed_lines}, indent=2) + "\n",
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
    """Read unprocessed events from events_file and append decision records.

    Args:
        events_file:  Path to t0_decisions.ndjson (written by decision_executor).
        log_file:     Destination JSONL decision log.
        cursor_file:  Cursor file tracking how many lines have been processed.
        dry_run:      Print records to stdout, do not write to log or cursor.

    Returns:
        Number of new records written (or that would be written in dry-run).
    """
    if not events_file.exists():
        logger.info("t0_decision_log: events file not found: %s", events_file)
        return 0

    all_lines = events_file.read_text(encoding="utf-8").splitlines()
    cursor = load_cursor(cursor_file)
    unprocessed = all_lines[cursor:]

    written = 0
    new_cursor = cursor

    for raw_line in unprocessed:
        new_cursor += 1
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            logger.warning("t0_decision_log: skipping malformed line at position %d", new_cursor)
            continue

        record = record_from_executor_event(event)
        if record is None:
            continue

        if dry_run:
            print(json.dumps(record, indent=2))
        else:
            write_decision(record, log_file)

        written += 1

    if not dry_run and new_cursor > cursor:
        save_cursor(cursor_file, new_cursor)
        logger.info(
            "t0_decision_log: processed %d new events, wrote %d records",
            new_cursor - cursor,
            written,
        )

    return written


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns:
        0 on success (including no-new-events case — no work to do)
        1 on unrecoverable errors
    """
    parser = argparse.ArgumentParser(
        description="Passive decision log writer — converts t0_decisions events to JSONL records."
    )
    parser.add_argument(
        "--events-file",
        type=Path,
        default=DEFAULT_EVENTS_FILE,
        help="Path to t0_decisions NDJSON events file",
    )
    parser.add_argument(
        "--decision-log",
        type=Path,
        default=DEFAULT_DECISION_LOG,
        help="Path to decision log JSONL file",
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
        args.decision_log,
        args.cursor_file,
        dry_run=args.dry_run,
    )

    if written == 0:
        logger.info("t0_decision_log: no new events to process")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
