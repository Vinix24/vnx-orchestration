#!/usr/bin/env python3
"""T0 Decision Summarizer — reads T0 stream-json events and summarizes into a
compact decision record via claude -p --model haiku.

Usage:
    python3 scripts/lib/t0_decision_summarizer.py
    python3 scripts/lib/t0_decision_summarizer.py --events-file .vnx-data/events/T0.ndjson
    python3 scripts/lib/t0_decision_summarizer.py --dry-run

BILLING SAFETY: Only calls subprocess.Popen(["claude", ...]). No Anthropic SDK.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def _vnx_data_dir() -> Path:
    """Resolve VNX data directory from environment."""
    return Path(os.environ.get("VNX_DATA_DIR", ".vnx-data"))

DEFAULT_EVENTS_FILE = _vnx_data_dir() / "events" / "T0.ndjson"
DEFAULT_DECISION_LOG = _vnx_data_dir() / "state" / "t0_decision_log.jsonl"

TEXT_EVENT_TYPES = {"text", "result"}

HAIKU_PROMPT = (
    "Summarize this T0 orchestrator output into a structured JSON decision record. "
    "Extract: what action was taken, why, which dispatch/open-items were affected, "
    "and what T0 expects next. Be concise — max 200 words total.\n\n"
    "Output ONLY valid JSON matching this schema:\n"
    "{\n"
    '  "timestamp": "<ISO-8601 now>",\n'
    '  "session_summary_at": "<ISO-8601 now>",\n'
    '  "action": "<dispatch|approve|reject|escalate|wait|close_oi|advance_gate>",\n'
    '  "dispatch_id": "<string or null>",\n'
    '  "track": "<A|B|C or null>",\n'
    '  "reasoning": "<1-2 sentence summary>",\n'
    '  "open_items_actions": [{"action": "<close|add|defer>", "id": "<OI-XXX>", "reason": "<...>"}],\n'
    '  "next_expected": "<what T0 is waiting for next>"\n'
    "}\n\n"
    "T0 output to summarize:\n"
)

FALLBACK_RECORD: dict[str, Any] = {
    "action": "wait",
    "dispatch_id": None,
    "track": None,
    "reasoning": "Haiku summarization failed — raw text preserved in events log.",
    "open_items_actions": [],
    "next_expected": "unknown",
}


# ---------------------------------------------------------------------------
# Event extraction
# ---------------------------------------------------------------------------

def load_events(events_file: Path) -> list[dict[str, Any]]:
    """Read all NDJSON events from the events file."""
    if not events_file.exists():
        raise FileNotFoundError(f"Events file not found: {events_file}")
    events = []
    for line in events_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed NDJSON line: %s — %s", line[:80], exc)
    return events


def extract_text_content(events: list[dict[str, Any]]) -> str:
    """Extract text from text/result type events."""
    parts: list[str] = []
    for event in events:
        event_type = event.get("type", "")
        if event_type not in TEXT_EVENT_TYPES:
            continue
        data = event.get("data", {})
        text = data.get("text", "")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Haiku summarization
# ---------------------------------------------------------------------------

def summarize_with_haiku(text_content: str) -> dict[str, Any]:
    """Call claude -p --model haiku to produce a structured decision record."""
    prompt = HAIKU_PROMPT + text_content

    cmd = [
        "claude",
        "-p",
        "--model", "haiku",
        "--output-format", "json",
        "--dangerously-skip-permissions",
        prompt,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        logger.warning("Haiku summarization timed out")
        return _build_fallback()
    except FileNotFoundError:
        logger.warning("claude CLI not found — cannot run haiku summarization")
        return _build_fallback()

    if proc.returncode != 0:
        logger.warning("Haiku exited %d: %s", proc.returncode, stderr[:200])
        return _build_fallback()

    return _parse_haiku_output(stdout)


def _parse_haiku_output(stdout: str) -> dict[str, Any]:
    """Parse haiku's JSON response and extract the decision record."""
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse haiku outer JSON: %s", exc)
        return _build_fallback()

    # claude --output-format json wraps output in {"result": "<text>", ...}
    result_text = outer.get("result", "")
    if not result_text:
        logger.warning("Empty result field from haiku")
        return _build_fallback()

    # Strip markdown code fences if haiku wrapped the JSON
    cleaned = result_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # Remove first and last fence lines
        inner_lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(inner_lines).strip()

    try:
        record = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse haiku decision JSON: %s — raw: %s", exc, cleaned[:200])
        return _build_fallback()

    # Ensure required fields present
    now = _now_iso()
    record.setdefault("timestamp", now)
    record.setdefault("session_summary_at", now)
    record.setdefault("action", "wait")
    record.setdefault("dispatch_id", None)
    record.setdefault("track", None)
    record.setdefault("reasoning", "")
    record.setdefault("open_items_actions", [])
    record.setdefault("next_expected", "")
    return record


def _build_fallback() -> dict[str, Any]:
    now = _now_iso()
    return {
        "timestamp": now,
        "session_summary_at": now,
        **FALLBACK_RECORD,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Decision log append
# ---------------------------------------------------------------------------

def append_decision_record(record: dict[str, Any], log_file: Path) -> None:
    """Atomically append a decision record to the JSONL log using fcntl locking."""
    log_file.parent.mkdir(parents=True, exist_ok=True)

    with open(log_file, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize T0 stream-json events into a decision record via haiku."
    )
    parser.add_argument(
        "--events-file",
        type=Path,
        default=DEFAULT_EVENTS_FILE,
        help=f"Path to T0 NDJSON events file (default: {DEFAULT_EVENTS_FILE})",
    )
    parser.add_argument(
        "--decision-log",
        type=Path,
        default=DEFAULT_DECISION_LOG,
        help=f"Path to decision log JSONL (default: {DEFAULT_DECISION_LOG})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the decision record without appending to the log",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    # Load events
    try:
        events = load_events(args.events_file)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    if not events:
        logger.warning("Events file is empty: %s", args.events_file)
        return 0

    # Extract text content
    text_content = extract_text_content(events)
    if not text_content.strip():
        logger.warning("No text/result events found in %s", args.events_file)
        return 0

    logger.info("Extracted %d chars from %d events", len(text_content), len(events))

    # Summarize
    record = summarize_with_haiku(text_content)

    if args.dry_run:
        print(json.dumps(record, indent=2))
        return 0

    # Append to log
    append_decision_record(record, args.decision_log)
    logger.info("Decision record appended to %s", args.decision_log)
    logger.info("Action: %s | Dispatch: %s | Track: %s",
                record.get("action"), record.get("dispatch_id"), record.get("track"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
