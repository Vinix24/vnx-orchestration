#!/usr/bin/env python3
"""T0 decision log — passive writer for structured decision records.

Complements t0_decision_summarizer.py (haiku-powered) with a zero-LLM
path: converts already-structured decision events (from decision_executor
or direct callers) into decision log records and appends them to
the decision log under VNX_STATE_DIR.

Two usage modes:

1. Direct write (real-time, called by decision_executor or other code):
   from t0_decision_log import write_decision
   write_decision(record)

2. Batch replay from events file (CLI):
   python3 scripts/lib/t0_decision_log.py
   python3 scripts/lib/t0_decision_log.py --events-file "$VNX_DATA_DIR/events/t0_decisions.ndjson"
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

Cursor tracking: stores the number of processed lines in a
cursor JSON file under VNX_STATE_DIR so repeated runs are
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
# Activation helper: kwargs-based logger for governance call sites
# ---------------------------------------------------------------------------

# decision_type → action mapping for the canonical schema's `action` field.
# Activation call sites use richer decision_type names; we keep the existing
# action vocabulary stable and store the original type in `decision_type`.
_DECISION_TYPE_TO_ACTION: dict[str, str] = {
    "dispatch_created": "dispatch",
    "gate_verdict": "advance_gate",
    "pr_merge": "approve",
    "oi_closed": "close_oi",
}

# Activation call sites whose outcomes are settled at write time (no
# downstream signal to reconcile). Anything else is left pending.
_TERMINAL_DECISION_TYPES: frozenset = frozenset({"oi_closed", "pr_merge"})


def log_decision(
    *,
    decision_type: str,
    dispatch_id: str | None = None,
    terminal: str | None = None,
    role: str | None = None,
    risk_score: float | None = None,
    reasoning: str = "",
    expected_outcome: str | None = None,
    gate: str | None = None,
    verdict: str | None = None,
    blocking_count: int | None = None,
    pr_number: int | None = None,
    dispatches_in_pr: list | None = None,
    oi_id: str | None = None,
    status: str | None = None,
    log_file: Path | None = None,
    timestamp: str | None = None,
) -> bool:
    """Best-effort kwargs-based decision logger for governance call sites.

    Builds a record conforming to the canonical schema (timestamp,
    session_summary_at, action, dispatch_id, track, reasoning,
    open_items_actions, next_expected) and adds activation-specific
    metadata (decision_type, terminal, role, risk_score, gate, verdict,
    blocking_count, pr_number, dispatches_in_pr, oi_id, status,
    expected_outcome, outcome_pending) so reconciliation can resolve
    outcomes later.

    Returns True on success, False on any failure. Never raises — call
    sites are passive sinks that must not break governance flow.
    """
    try:
        action = _DECISION_TYPE_TO_ACTION.get(decision_type, decision_type)
        track = _TERMINAL_TO_TRACK.get((terminal or "").upper())
        record = build_record(
            action=action,
            reasoning=reasoning,
            dispatch_id=dispatch_id,
            track=track,
            timestamp=timestamp,
        )
        record["decision_type"] = decision_type
        if terminal is not None:
            record["terminal"] = terminal
        if role is not None:
            record["role"] = role
        if risk_score is not None:
            record["risk_score"] = risk_score
        if expected_outcome is not None:
            record["expected_outcome"] = expected_outcome
        if gate is not None:
            record["gate"] = gate
        if verdict is not None:
            record["verdict"] = verdict
        if blocking_count is not None:
            record["blocking_count"] = blocking_count
        if pr_number is not None:
            record["pr_number"] = pr_number
        if dispatches_in_pr is not None:
            record["dispatches_in_pr"] = list(dispatches_in_pr)
        if oi_id is not None:
            record["oi_id"] = oi_id
        if status is not None:
            record["status"] = status
        record["outcome_pending"] = decision_type not in _TERMINAL_DECISION_TYPES

        write_decision(record, log_file)
        return True
    except Exception:
        return False


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


def _load_cursor_state(cursor_file: Path) -> dict:
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

    cursor_state = _load_cursor_state(cursor_file)
    cursor = int(cursor_state.get("processed_lines", 0))
    saved_inode = cursor_state.get("inode")

    try:
        current_inode = os.stat(events_file).st_ino
    except OSError:
        current_inode = 0

    # Inode mismatch: source file was replaced (same or greater line count) → reset
    if saved_inode is not None and saved_inode != 0 and current_inode != 0 and current_inode != saved_inode:
        logger.warning(
            "t0_decision_log: source file replaced (inode %d → %d) — resetting cursor to 0",
            saved_inode,
            current_inode,
        )
        cursor = 0

    # Line-count guard: also reset if cursor exceeds current file length
    if cursor > len(all_lines):
        logger.warning(
            "t0_decision_log: cursor %d exceeds file length %d — source file may have been reset; reprocessing from start",
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
                # Partial trailing write — do not advance cursor past it; retry next invocation.
                break
            logger.warning("t0_decision_log: malformed line skipped at position %d", cursor + idx)
            parsed_count += 1
            continue

        record = record_from_executor_event(event)
        if record is None:
            parsed_count += 1
            continue

        if dry_run:
            print(json.dumps(record, indent=2))
        else:
            write_decision(record, log_file)

        written += 1
        parsed_count += 1

    new_cursor = cursor + parsed_count

    if not dry_run:
        upgrade_only = saved_inode is None and new_cursor == cursor
        if new_cursor > cursor or upgrade_only:
            _save_cursor_state(cursor_file, new_cursor, current_inode)
        if new_cursor > cursor:
            logger.info(
                "t0_decision_log: processed %d new events, wrote %d records",
                new_cursor - cursor,
                written,
            )
        elif upgrade_only:
            logger.info(
                "t0_decision_log: upgraded legacy cursor with inode %d (no new events)",
                current_inode,
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
