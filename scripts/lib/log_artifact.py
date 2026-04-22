#!/usr/bin/env python3
"""Log artifact writer for VNX headless CLI runs.

Writes human-readable run logs and raw output capture files to an
artifact directory for operator inspection without file spelunking.

Two artifact types:
  <run_id>.log   — structured log with identity, stdout, stderr, outcome
  <run_id>.out   — raw stdout capture (only written when stdout is non-empty)

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_SAFE_RUN_ID = re.compile(r'^[A-Za-z0-9_\-.]+$')


def _assert_safe_run_id(run_id: str) -> str:
    """Validate run_id is filesystem-safe — raises ValueError if not."""
    if not run_id or not _SAFE_RUN_ID.match(run_id) or ".." in run_id:
        raise ValueError(
            f"invalid run_id: {run_id!r} (must match [A-Za-z0-9_\\-.]+ and not contain ..)"
        )
    return run_id


# ---------------------------------------------------------------------------
# Log artifact
# ---------------------------------------------------------------------------

_LOG_TEMPLATE = """\
══════════════════════════════════════════════════════════════════════════
 VNX HEADLESS RUN LOG
══════════════════════════════════════════════════════════════════════════
 Run ID       : {run_id}
 Dispatch ID  : {dispatch_id}
 Target Type  : {target_type}
 Started At   : {started_at}
 Duration     : {duration_seconds}s
 Exit Code    : {exit_code}
 Failure Class: {failure_class}
──────────────────────────────────────────────────────────────────────────
 STDOUT
──────────────────────────────────────────────────────────────────────────
{stdout}
──────────────────────────────────────────────────────────────────────────
 STDERR
──────────────────────────────────────────────────────────────────────────
{stderr}
──────────────────────────────────────────────────────────────────────────
 RUN OUTCOME
──────────────────────────────────────────────────────────────────────────
 Status       : {status}
 Failure Class: {failure_class}
 Duration     : {duration_seconds}s
══════════════════════════════════════════════════════════════════════════
"""


def write_log_artifact(
    *,
    artifact_dir: Path,
    run_id: str,
    dispatch_id: str,
    target_type: str,
    started_at: str,
    stdout: str,
    stderr: str,
    exit_code: Optional[int],
    duration_seconds: float,
    failure_class: Optional[str] = None,
) -> Path:
    """Write a structured run log to <artifact_dir>/<run_id>.log.

    Always writes (even on failure) so operators can inspect every run.
    Returns the path to the written file.
    """
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    effective_fc = failure_class or ("SUCCESS" if exit_code == 0 else "UNKNOWN")
    status = "SUCCEEDED" if effective_fc == "SUCCESS" else "FAILED"

    content = _LOG_TEMPLATE.format(
        run_id=run_id,
        dispatch_id=dispatch_id,
        target_type=target_type,
        started_at=started_at or "unknown",
        duration_seconds=f"{duration_seconds:.1f}",
        exit_code=exit_code if exit_code is not None else "—",
        failure_class=effective_fc,
        stdout=stdout.strip() if stdout else "(no output)",
        stderr=stderr.strip() if stderr else "(no stderr)",
        status=status,
    )

    log_path = artifact_dir / f"{_assert_safe_run_id(run_id)}.log"
    log_path.write_text(content, encoding="utf-8")
    return log_path


# ---------------------------------------------------------------------------
# Output artifact
# ---------------------------------------------------------------------------

def write_output_artifact(
    *,
    artifact_dir: Path,
    run_id: str,
    stdout: str,
) -> Optional[Path]:
    """Write raw stdout to <artifact_dir>/<run_id>.out.

    Returns the path if stdout is non-empty, None otherwise.
    """
    if not stdout or not stdout.strip():
        return None

    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    out_path = artifact_dir / f"{_assert_safe_run_id(run_id)}.out"
    out_path.write_text(stdout, encoding="utf-8")
    return out_path
