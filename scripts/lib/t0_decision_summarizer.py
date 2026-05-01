#!/usr/bin/env python3
"""T0 decision summarizer — haiku-powered T0 session decision log writer.

Reads T0's stream-json events from the T0 events file under
VNX_DATA_DIR, extracts text content, summarizes via claude haiku,
and appends a structured decision record to the decision log under
VNX_STATE_DIR.

Decision record schema:
  {
    "timestamp": "ISO-8601",
    "session_summary_at": "ISO-8601",
    "action": "dispatch|approve|reject|escalate|wait|close_oi|advance_gate",
    "dispatch_id": "string or null",
    "track": "A|B|C or null",
    "reasoning": "1-2 sentence summary",
    "open_items_actions": [{"action": "close|add|defer", "id": "OI-XXX", "reason": "..."}],
    "next_expected": "what T0 is waiting for next"
  }

CLI:
  python3 scripts/lib/t0_decision_summarizer.py
  python3 scripts/lib/t0_decision_summarizer.py --events-file "$VNX_DATA_DIR/events/T0.ndjson"
  python3 scripts/lib/t0_decision_summarizer.py --dry-run

Environment:
  VNX_DECISION_SUMMARIZER=1  (default: 0) — enable summarizer
  VNX_DATA_DIR               — override data directory root

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls. CLI-only.
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
# Path resolution
# ---------------------------------------------------------------------------


def _data_dir() -> Path:
    vnx_data = os.environ.get("VNX_DATA_DIR")
    if vnx_data:
        return Path(vnx_data).expanduser().resolve()
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent.parent / ".vnx-data"


DEFAULT_EVENTS_FILE: Path = _data_dir() / "events" / "T0.ndjson"
DEFAULT_DECISION_LOG: Path = _data_dir() / "state" / "t0_decision_log.jsonl"

# Rotate log when it exceeds 1 MB
_ROTATION_BYTES = 1 * 1024 * 1024

# Haiku subprocess timeout in seconds
_HAIKU_TIMEOUT = 120

# ---------------------------------------------------------------------------
# Haiku prompt
# ---------------------------------------------------------------------------

_HAIKU_PROMPT_TEMPLATE = """\
Analyze this T0 orchestrator session output and extract the key governance decision.
Return ONLY a valid JSON object with these exact fields:

{{
  "timestamp": "<ISO-8601 UTC>",
  "session_summary_at": "<ISO-8601 UTC>",
  "action": "<one of: dispatch, approve, reject, escalate, wait, close_oi, advance_gate>",
  "dispatch_id": "<dispatch ID string or null>",
  "track": "<A, B, C, or null>",
  "reasoning": "<1-2 sentence summary of why this action was taken>",
  "open_items_actions": [],
  "next_expected": "<what T0 is waiting for next>"
}}

T0 SESSION OUTPUT:
{content}
"""

# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def load_events(events_file: Path) -> list[dict[str, Any]]:
    """Load NDJSON events from file.

    Raises:
        FileNotFoundError: if events_file does not exist.
    """
    if not events_file.exists():
        raise FileNotFoundError(f"Events file not found: {events_file}")

    events: list[dict[str, Any]] = []
    for line in events_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("t0_decision_summarizer: skipping malformed line in %s", events_file)
    return events


def extract_text_content(events: list[dict[str, Any]]) -> str:
    """Extract and join text from type=text and type=result events."""
    parts: list[str] = []
    for event in events:
        if event.get("type") not in ("text", "result"):
            continue
        data = event.get("data", {})
        text = data.get("text", "") if isinstance(data, dict) else ""
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_fallback(reason: str = "Haiku summarization failed") -> dict[str, Any]:
    """Build a fallback decision record when summarization cannot complete."""
    now = _now_iso()
    return {
        "timestamp": now,
        "session_summary_at": now,
        "action": "wait",
        "dispatch_id": None,
        "track": None,
        "reasoning": f"{reason} — operator review required",
        "open_items_actions": [],
        "next_expected": "",
    }


def _strip_code_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences from a string."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    inner_lines = []
    for i, line in enumerate(lines):
        if i == 0 and line.startswith("```"):
            continue
        if i == len(lines) - 1 and line.strip() == "```":
            continue
        inner_lines.append(line)
    return "\n".join(inner_lines)


def _apply_record_defaults(record: dict[str, Any]) -> None:
    """Fill missing fields in a decision record with safe defaults."""
    now = _now_iso()
    record.setdefault("timestamp", now)
    record.setdefault("session_summary_at", now)
    record.setdefault("action", "wait")
    record.setdefault("dispatch_id", None)
    record.setdefault("track", None)
    record.setdefault("reasoning", "")
    record.setdefault("open_items_actions", [])
    record.setdefault("next_expected", "")


def _parse_haiku_output(stdout: str) -> dict[str, Any]:
    """Parse haiku --output-format json response into a decision record.

    Falls back gracefully on any parse failure.
    """
    try:
        outer = json.loads(stdout)
    except json.JSONDecodeError:
        return _build_fallback("Haiku summarization failed: invalid JSON response")

    result_str = outer.get("result", "")
    if not result_str:
        return _build_fallback("Haiku summarization failed: empty result")

    try:
        record = json.loads(_strip_code_fences(result_str))
    except json.JSONDecodeError:
        return _build_fallback("Haiku summarization failed: invalid inner JSON")

    _apply_record_defaults(record)
    return record


def summarize_with_haiku(content: str) -> dict[str, Any]:
    """Invoke claude haiku to summarize session content into a decision record."""
    prompt = _HAIKU_PROMPT_TEMPLATE.format(content=content)
    cmd = [
        "claude",
        "-p",
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "--model", "haiku",
        prompt,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=_HAIKU_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return _build_fallback("Haiku summarization failed: timeout")

        if proc.returncode != 0:
            return _build_fallback("Haiku summarization failed: non-zero exit")

        return _parse_haiku_output(stdout)

    except FileNotFoundError:
        return _build_fallback("Haiku summarization failed: claude not found")


def append_decision_record(record: dict[str, Any], log_file: Path) -> None:
    """Append a decision record as a JSONL line with exclusive file locking."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(log_file, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _rotate_if_needed(log_file: Path) -> None:
    """Archive and truncate log file when it exceeds the rotation threshold.

    Holds an exclusive lock across the entire copy+truncate sequence to prevent
    concurrent writers from losing records between the archive copy and the
    in-place truncation.
    """
    if not log_file.exists() or log_file.stat().st_size < _ROTATION_BYTES:
        return

    archive_dir = log_file.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive_path = archive_dir / f"t0_decision_log_{ts}.jsonl"

    with open(log_file, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            if os.fstat(f.fileno()).st_size < _ROTATION_BYTES:
                return
            content = f.read()
            archive_path.write_text(content, encoding="utf-8")
            f.seek(0)
            f.truncate()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    logger.info("t0_decision_summarizer: rotated log to %s", archive_path)


# ---------------------------------------------------------------------------
# Assembler integration — query interface for context assembly
# ---------------------------------------------------------------------------


def load_recent_decisions(
    log_file: Path | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return the N most recent decision records from the log.

    Used by context assemblers to include recent T0 decision history
    in T0 session prompts. Returns an empty list if the log doesn't exist.
    """
    path = log_file or DEFAULT_DECISION_LOG
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


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Summarize T0 session events into a structured decision log entry."
    )
    parser.add_argument(
        "--events-file",
        type=Path,
        default=DEFAULT_EVENTS_FILE,
        help="Path to T0 NDJSON events file",
    )
    parser.add_argument(
        "--decision-log",
        type=Path,
        default=DEFAULT_DECISION_LOG,
        help="Path to decision log JSONL file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print decision record to stdout without writing to log",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns:
        0 on success (including empty/no-text events — no work to do)
        1 on fatal errors (events file not found)
    """
    args = _build_arg_parser().parse_args(argv)

    try:
        events = load_events(args.events_file)
    except FileNotFoundError as exc:
        logger.error("t0_decision_summarizer: %s", exc)
        return 1

    content = extract_text_content(events)
    if not content:
        logger.info("t0_decision_summarizer: no text events — nothing to summarize")
        return 0

    record = summarize_with_haiku(content)

    if args.dry_run:
        print(json.dumps(record, indent=2))
        return 0

    _rotate_if_needed(args.decision_log)
    append_decision_record(record, args.decision_log)
    logger.info("t0_decision_summarizer: appended record to %s", args.decision_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
