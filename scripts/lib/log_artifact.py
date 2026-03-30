#!/usr/bin/env python3
"""
VNX Log Artifact Writer — Persist headless run output as durable log artifacts.

Contract reference: docs/HEADLESS_RUN_CONTRACT.md Section 5.3

Every headless run produces a log artifact containing:
  1. Header: run_id, dispatch_id, target_type, started_at
  2. Stdout: complete captured stdout
  3. Stderr: complete captured stderr (clearly delimited)
  4. Footer: exit_code, failure_class (if applicable), duration_seconds, completed_at

Format: plain text with clear section delimiters. Human-readable without tooling.

Governance:
  A-R3: Logs must be persisted as artifacts, not just streamed to stdout
  O-6:  Operator can read full output of a completed run via log_artifact_path
  O-7:  Operator can read stderr of a failed run via log_artifact_path
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Section delimiters
# ---------------------------------------------------------------------------

HEADER_DELIM = "=" * 72
SECTION_DELIM = "-" * 72


# ---------------------------------------------------------------------------
# Log artifact writer
# ---------------------------------------------------------------------------

def write_log_artifact(
    *,
    artifact_dir: Path,
    run_id: str,
    dispatch_id: str,
    target_type: str,
    started_at: str,
    stdout: str,
    stderr: str,
    exit_code: Optional[int] = None,
    failure_class: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    completed_at: Optional[str] = None,
) -> Path:
    """Write a structured log artifact for a headless run.

    Returns the path to the written artifact file.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{run_id}.log"
    artifact_path = artifact_dir / filename

    if completed_at is None:
        completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    lines = []

    # Header
    lines.append(HEADER_DELIM)
    lines.append("VNX HEADLESS RUN LOG")
    lines.append(HEADER_DELIM)
    lines.append(f"run_id:       {run_id}")
    lines.append(f"dispatch_id:  {dispatch_id}")
    lines.append(f"target_type:  {target_type}")
    lines.append(f"started_at:   {started_at}")
    lines.append(HEADER_DELIM)
    lines.append("")

    # Stdout section
    lines.append(SECTION_DELIM)
    lines.append("STDOUT")
    lines.append(SECTION_DELIM)
    lines.append(stdout if stdout else "(no stdout)")
    lines.append("")

    # Stderr section
    lines.append(SECTION_DELIM)
    lines.append("STDERR")
    lines.append(SECTION_DELIM)
    lines.append(stderr if stderr else "(no stderr)")
    lines.append("")

    # Footer
    lines.append(HEADER_DELIM)
    lines.append("RUN OUTCOME")
    lines.append(HEADER_DELIM)
    lines.append(f"exit_code:        {exit_code if exit_code is not None else 'N/A'}")
    lines.append(f"failure_class:    {failure_class or 'N/A'}")
    lines.append(f"duration_seconds: {duration_seconds if duration_seconds is not None else 'N/A'}")
    lines.append(f"completed_at:     {completed_at}")
    lines.append(HEADER_DELIM)

    artifact_path.write_text("\n".join(lines), encoding="utf-8")
    return artifact_path


def write_output_artifact(
    *,
    artifact_dir: Path,
    run_id: str,
    stdout: str,
) -> Optional[Path]:
    """Write the structured output (stdout only) to a separate artifact.

    Returns the path, or None if stdout is empty.
    """
    if not stdout.strip():
        return None

    artifact_dir.mkdir(parents=True, exist_ok=True)
    output_path = artifact_dir / f"{run_id}.output.txt"
    output_path.write_text(stdout, encoding="utf-8")
    return output_path
