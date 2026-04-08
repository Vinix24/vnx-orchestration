#!/usr/bin/env python3
"""Deterministic Extraction Module — PR-2 (F37 Auto-Report Pipeline).

Extracts structured data from git history, pytest output, and subprocess
event streams. No LLM calls — pure parsing.

Public API:
    extract_git_provenance(repo_path)   → GitProvenance
    parse_pytest_output(text)           → Optional[TestResults]
    extract_pytest_from_events(path)    → Optional[TestResults]
    aggregate_event_metrics(path)       → EventMetrics
    run_extraction(...)                 → ExtractionResult

All functions return empty/default objects on missing input — never raise.

BILLING SAFETY: No Anthropic SDK imports. Subprocess calls to git only.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Import PR-0 contract schemas
import sys as _sys
_LIB = str(Path(__file__).resolve().parent)
if _LIB not in _sys.path:
    _sys.path.insert(0, _LIB)

from auto_report_contract import (
    EventMetrics,
    ExtractionResult,
    GitProvenance,
    TestResults,
)


# ─── Git Extraction ──────────────────────────────────────────────────────────

def extract_git_provenance(repo_path: Optional[Path] = None) -> GitProvenance:
    """Extract git state from the repository.

    Runs git log and git show --stat to capture the most recent commit's
    metadata and file-change summary. Falls back to an empty GitProvenance
    on any failure (missing git, no commits, detached HEAD, etc.).

    Args:
        repo_path: Repository root. Defaults to current working directory.

    Returns:
        GitProvenance with commit hash, message, branch, files changed,
        insertions, deletions, and dirty flag.
    """
    cwd = str(repo_path) if repo_path else None

    try:
        commit_hash, commit_message = _git_last_commit(cwd)
        if not commit_hash:
            return GitProvenance()

        branch = _git_branch(cwd)
        files_changed, insertions, deletions = _git_diff_stat(cwd)
        is_dirty = _git_is_dirty(cwd)

        return GitProvenance(
            commit_hash=commit_hash,
            commit_message=commit_message,
            branch=branch,
            files_changed=tuple(files_changed),
            insertions=insertions,
            deletions=deletions,
            is_dirty=is_dirty,
        )
    except Exception as exc:
        logger.debug("extract_git_provenance: unexpected error: %s", exc)
        return GitProvenance()


def _run_git(args: List[str], cwd: Optional[str]) -> Tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=15,
        )
        return result.returncode, result.stdout, result.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("_run_git %s: %s", args, exc)
        return 1, "", str(exc)


def _git_last_commit(cwd: Optional[str]) -> Tuple[str, str]:
    """Return (commit_hash, commit_message) for HEAD. Empty strings on failure."""
    rc, stdout, _ = _run_git(["log", "--oneline", "-1"], cwd)
    if rc != 0 or not stdout.strip():
        return "", ""
    parts = stdout.strip().split(None, 1)
    commit_hash = parts[0]
    commit_message = parts[1] if len(parts) > 1 else ""
    return commit_hash, commit_message


def _git_branch(cwd: Optional[str]) -> str:
    """Return current branch name. Empty string on failure."""
    rc, stdout, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return stdout.strip() if rc == 0 else ""


def _git_diff_stat(cwd: Optional[str]) -> Tuple[List[str], int, int]:
    """Return (files_changed, insertions, deletions) from git show --stat HEAD.

    Uses --format= to suppress the commit header, leaving only the stat block.
    """
    rc, stdout, _ = _run_git(["show", "--stat", "--format=", "HEAD"], cwd)
    if rc != 0:
        return [], 0, 0
    return _parse_diff_stat(stdout)


def _git_is_dirty(cwd: Optional[str]) -> bool:
    """Return True if working tree has uncommitted changes."""
    rc, stdout, _ = _run_git(["status", "--porcelain"], cwd)
    return bool(stdout.strip()) if rc == 0 else False


def _parse_diff_stat(stat_output: str) -> Tuple[List[str], int, int]:
    """Parse git diff/show --stat output into (files, insertions, deletions).

    Handles:
    - Standard file lines:   `` path/to/file.py | 10 +++++-----``
    - Binary file lines:     `` image.png | Bin 0 -> 1234 bytes``
    - Summary line:          `` 2 files changed, 10 insertions(+), 5 deletions(-)``
    - Rename lines:          `` old.py => new.py | 5 +++++``
    """
    files: List[str] = []
    insertions = 0
    deletions = 0

    for raw_line in stat_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Summary line (must come before file-line check to avoid false match)
        if re.search(r"\d+ files? changed", line):
            ins_m = re.search(r"(\d+) insertion", line)
            del_m = re.search(r"(\d+) deletion", line)
            if ins_m:
                insertions = int(ins_m.group(1))
            if del_m:
                deletions = int(del_m.group(1))
            continue

        # File stat line: "path/to/file.py | 10 +++++-----"
        if "|" in line:
            parts = line.split("|", 1)
            file_part = parts[0].strip()
            stat_part = parts[1].strip() if len(parts) > 1 else ""
            # Skip binary files ("Bin N -> M bytes"), empty names, and diff markers
            is_binary = stat_part.startswith("Bin ") or "Bin " in stat_part[:10]
            if file_part and not is_binary and not file_part.startswith(("---", "+++")):
                # Handle rename notation: "old.py => new.py" → take new name
                if "=>" in file_part:
                    # "old.py => new.py" or "{old => new}/suffix.py"
                    arrow_match = re.search(r"=>\s*(.+?)$", file_part)
                    if arrow_match:
                        file_part = arrow_match.group(1).strip().rstrip("}")
                files.append(file_part)

    return files, insertions, deletions


# ─── Pytest Output Parsing ───────────────────────────────────────────────────

# Matches the terminal summary line from any pytest output format:
#   "5 passed in 1.23s"
#   "3 failed, 2 passed in 0.50s"
#   "1 error in 0.05s"
#   "5 passed, 2 skipped, 1 xfailed in 2.10s"
_PYTEST_SUMMARY_RE = re.compile(
    r"(?:=+\s*)?((?:\d+\s+\w+(?:,\s*)?)+)\s+in\s+(\d+\.?\d*)\s*s",
    re.IGNORECASE,
)

# Individual count patterns within the summary segment
_COUNT_PATTERNS = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "errors": re.compile(r"(\d+)\s+error"),
    "skipped": re.compile(r"(\d+)\s+skipped"),
}

# Coverage percentage: "TOTAL ... 63%" or "coverage: 63%"
_COVERAGE_RE = re.compile(r"(?:TOTAL\s+\d+\s+\d+\s+(\d+)%|coverage[:\s]+(\d+)%)", re.IGNORECASE)


def parse_pytest_output(text: str) -> Optional[TestResults]:
    """Parse pytest stdout/stderr and extract test counts and duration.

    Handles standard, verbose, and short (dot) pytest output formats.
    Returns None when no pytest summary line is detected (not a pytest run).
    Returns a zero-count TestResults when pytest ran but found nothing.

    Args:
        text: Raw pytest output string.

    Returns:
        TestResults on success, None if text contains no pytest output.
    """
    if not text or not _looks_like_pytest(text):
        return None

    # Find the last summary line (most specific match wins)
    summary_match = None
    for m in _PYTEST_SUMMARY_RE.finditer(text):
        summary_match = m

    if summary_match is None:
        # Pytest ran but produced no counted summary (e.g. collection error)
        return TestResults(raw_output=text[:500])

    summary_segment = summary_match.group(1)
    duration_str = summary_match.group(2)

    passed = _extract_count(summary_segment, _COUNT_PATTERNS["passed"])
    failed = _extract_count(summary_segment, _COUNT_PATTERNS["failed"])
    errors = _extract_count(summary_segment, _COUNT_PATTERNS["errors"])
    skipped = _extract_count(summary_segment, _COUNT_PATTERNS["skipped"])

    try:
        duration = float(duration_str)
    except ValueError:
        duration = 0.0

    return TestResults(
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        duration_seconds=duration,
        raw_output=text[:500],
    )


def _looks_like_pytest(text: str) -> bool:
    """Return True if text contains recognisable pytest markers."""
    markers = ("passed", "failed", "error", "skipped", "pytest", "collected")
    lower = text.lower()
    return any(m in lower for m in markers)


def _extract_count(segment: str, pattern: re.Pattern) -> int:
    """Extract integer count from a regex match, 0 if not found."""
    m = pattern.search(segment)
    return int(m.group(1)) if m else 0


# ─── Event Stream Aggregation ────────────────────────────────────────────────

def aggregate_event_metrics(
    events_path: Path,
    dispatch_id: str = "",
) -> EventMetrics:
    """Aggregate metrics from a terminal's NDJSON event file.

    Reads .vnx-data/events/T{N}.ndjson (or an archived copy). Counts
    tool_use, text, thinking, and error events. Computes session duration
    from first-to-last timestamp. Extracts model from the init event.

    Filters to dispatch_id when provided (empty string = all events).

    Args:
        events_path: Path to the NDJSON event file.
        dispatch_id: Optional dispatch ID filter.

    Returns:
        EventMetrics with aggregated counts and timing. Empty on any failure.
    """
    if not events_path or not Path(events_path).exists():
        return EventMetrics()

    tool_use_count = 0
    text_block_count = 0
    thinking_block_count = 0
    error_count = 0
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    model_used = ""

    try:
        with open(events_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    logger.debug("aggregate_event_metrics: skipping corrupt line")
                    continue

                # Apply dispatch_id filter
                if dispatch_id and event.get("dispatch_id", "") not in ("", dispatch_id):
                    continue

                event_type = event.get("type", "")
                timestamp = event.get("timestamp", "")

                if timestamp:
                    if first_ts is None:
                        first_ts = timestamp
                    last_ts = timestamp

                if event_type == "tool_use":
                    tool_use_count += 1
                elif event_type == "text":
                    text_block_count += 1
                elif event_type == "thinking":
                    thinking_block_count += 1
                elif event_type == "error":
                    error_count += 1
                elif event_type == "init":
                    data = event.get("data", {})
                    if isinstance(data, dict) and data.get("model"):
                        model_used = data["model"]

    except OSError as exc:
        logger.debug("aggregate_event_metrics: cannot read %s: %s", events_path, exc)
        return EventMetrics()

    duration = _compute_duration_seconds(first_ts, last_ts)

    return EventMetrics(
        tool_use_count=tool_use_count,
        text_block_count=text_block_count,
        thinking_block_count=thinking_block_count,
        error_count=error_count,
        session_duration_seconds=duration,
        model_used=model_used,
    )


def extract_pytest_from_events(
    events_path: Path,
    dispatch_id: str = "",
) -> Optional[TestResults]:
    """Scan tool_result events in the NDJSON file for pytest output.

    Workers run pytest via Bash tool. The output appears in tool_result
    events as the content field. This function scans all tool_result events
    and returns the TestResults from the last recognisable pytest run.

    Args:
        events_path: Path to the NDJSON event file.
        dispatch_id: Optional dispatch ID filter.

    Returns:
        TestResults from the last pytest invocation found, or None.
    """
    if not events_path or not Path(events_path).exists():
        return None

    last_result: Optional[TestResults] = None

    try:
        with open(events_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                if dispatch_id and event.get("dispatch_id", "") not in ("", dispatch_id):
                    continue

                if event.get("type") != "tool_result":
                    continue

                data = event.get("data", {})
                if not isinstance(data, dict):
                    continue

                content = data.get("content", "")
                if not isinstance(content, str):
                    continue

                parsed = parse_pytest_output(content)
                if parsed is not None:
                    last_result = parsed

    except OSError as exc:
        logger.debug("extract_pytest_from_events: cannot read %s: %s", events_path, exc)

    return last_result


def _compute_duration_seconds(first_ts: Optional[str], last_ts: Optional[str]) -> int:
    """Compute integer seconds between two ISO 8601 timestamps.

    Returns 0 on any parse failure or when timestamps are identical.
    """
    if not first_ts or not last_ts or first_ts == last_ts:
        return 0
    try:
        fmt = "%Y-%m-%dT%H:%M:%S.%f+00:00"
        # Try multiple ISO formats
        for ts_fmt in (
            "%Y-%m-%dT%H:%M:%S.%f+00:00",
            "%Y-%m-%dT%H:%M:%S+00:00",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
        ):
            try:
                t0 = datetime.strptime(first_ts[:26].replace("Z", "+00:00"), ts_fmt)
                t1 = datetime.strptime(last_ts[:26].replace("Z", "+00:00"), ts_fmt)
                return max(0, int((t1 - t0).total_seconds()))
            except ValueError:
                continue
        # Fallback: fromisoformat
        t0 = datetime.fromisoformat(first_ts)
        t1 = datetime.fromisoformat(last_ts)
        return max(0, int((t1 - t0).total_seconds()))
    except Exception as exc:
        logger.debug("_compute_duration_seconds: %s", exc)
        return 0


# ─── Exit Summary Extraction ─────────────────────────────────────────────────

def extract_exit_summary(events_path: Optional[Path], dispatch_id: str = "") -> str:
    """Extract the last text block from the event stream as exit summary.

    Workers emit a final text block summarising their work. This function
    returns the content of the last ``text`` event, truncated to 200 chars.

    Returns empty string when no text events are found.
    """
    if not events_path or not Path(events_path).exists():
        return ""

    last_text = ""

    try:
        with open(events_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                if dispatch_id and event.get("dispatch_id", "") not in ("", dispatch_id):
                    continue

                if event.get("type") == "text":
                    data = event.get("data", {})
                    if isinstance(data, dict):
                        text = data.get("text", "")
                        if isinstance(text, str) and text.strip():
                            last_text = text.strip()

    except OSError as exc:
        logger.debug("extract_exit_summary: cannot read %s: %s", events_path, exc)

    return last_text[:200]


# ─── Main Entry Point ─────────────────────────────────────────────────────────

def run_extraction(
    dispatch_id: str,
    terminal: str,
    track: str,
    gate: str,
    repo_path: Optional[Path] = None,
    events_path: Optional[Path] = None,
    exit_summary: str = "",
) -> ExtractionResult:
    """Compose all extractors into a single ExtractionResult.

    This is the primary entry point for the stop hook. Each extractor is
    called independently; failures in one do not affect others.

    Args:
        dispatch_id: Active dispatch identifier.
        terminal:    Worker terminal (T1, T2, T3).
        track:       Track letter (A, B, C).
        gate:        Quality gate name.
        repo_path:   Repository root for git extraction. Defaults to cwd.
        events_path: Path to NDJSON event file. Auto-resolved from
                     VNX_DATA_DIR when omitted.
        exit_summary: Override for exit summary (skips event scan).

    Returns:
        ExtractionResult populated with all available data.
    """
    # Resolve events path from env when not provided
    if events_path is None:
        import os
        vnx_data = os.environ.get("VNX_DATA_DIR")
        if vnx_data:
            events_path = Path(vnx_data) / "events" / f"{terminal}.ndjson"

    git_provenance = extract_git_provenance(repo_path)

    # Pytest: prefer events scan over direct git extraction
    test_results = extract_pytest_from_events(events_path, dispatch_id)

    event_metrics = aggregate_event_metrics(events_path, dispatch_id)

    # Exit summary: use override, then event scan, then empty
    if not exit_summary:
        exit_summary = extract_exit_summary(events_path, dispatch_id)

    extracted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return ExtractionResult(
        dispatch_id=dispatch_id,
        terminal=terminal,
        track=track,
        gate=gate,
        git=git_provenance,
        tests=test_results,
        events=event_metrics,
        exit_summary=exit_summary,
        extracted_at=extracted_at,
    )
