"""governance_emit.py — Shared governance receipt + unified report emitter (Wave 7 PR-7.6).

Used by both subprocess_dispatch.py (claude path) and provider_dispatch.py (multi-provider
path) so every dispatch writes a governance-enriched receipt and unified report.

ADR-005: NDJSON audit completeness. ADR-016: unified events.

Hard rules (PRD provider-governance-unification):
  - Provider field MUST match _PROVIDER_RE — raises ValueError on mismatch.
  - Receipt write MUST NOT silently fail — raises RuntimeError on OSError.
  - NDJSON append uses fcntl.flock(LOCK_EX) for concurrent safety.
  - Unified report uses tmp + os.replace for atomic write.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROVIDER_RE = re.compile(r"^(claude|codex|gemini|kimi|litellm:[a-z][a-z0-9_-]*)$")


def _validate_provider(provider: str) -> None:
    """Raise ValueError when provider doesn't match required pattern."""
    if not _PROVIDER_RE.match(provider or ""):
        raise ValueError(
            f"Invalid provider {provider!r}. "
            "Must match ^(claude|codex|gemini|kimi|litellm:[a-z][a-z0-9_-]*)$"
        )


def emit_dispatch_receipt(
    dispatch_id: str,
    terminal_id: str,
    provider: str,
    model: str,
    pr_id: Optional[str],
    status: str,
    completion_pct: int,
    risk: float,
    findings: List[Dict[str, Any]],
    duration_seconds: float,
    token_usage: Dict[str, int],
    cost_usd: Optional[float],
    state_dir: Path,
) -> Path:
    """Atomic-append to t0_receipts.ndjson. fcntl.flock for concurrent safety.

    Returns the receipt file path on success.

    Raises:
        ValueError: provider field doesn't match required pattern
        RuntimeError: write failed
    """
    _validate_provider(provider)

    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    recorded_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    receipt: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "terminal_id": terminal_id,
        "provider": provider,
        "model": model,
        "status": status,
        "completion_pct": completion_pct,
        "risk": risk,
        "duration_seconds": round(float(duration_seconds), 3),
        "token_usage": token_usage,
        "cost_usd": cost_usd,
        "findings": findings,
        "pr_id": pr_id,
        "timestamp": now_ts,
        "recorded_at": recorded_ts,
    }

    receipt_path = Path(state_dir) / "t0_receipts.ndjson"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(receipt, separators=(",", ":")) + "\n"

    try:
        with open(receipt_path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError as exc:
        raise RuntimeError(
            f"governance_emit: receipt write failed for dispatch={dispatch_id}: {exc}"
        ) from exc

    logger.info(
        "governance_emit: receipt written dispatch=%s provider=%s status=%s",
        dispatch_id, provider, status,
    )
    return receipt_path


def emit_unified_report(
    dispatch_id: str,
    terminal_id: str,
    provider: str,
    instruction: str,
    response_text: str,
    findings: List[Dict[str, Any]],
    duration_seconds: float,
    data_dir: Path,
) -> Path:
    """Atomic write to unified_reports/<dispatch_id>.md. Returns path.

    Idempotent: returns the existing path without modifying it when the report
    already exists (worker may have written a richer report).

    Raises:
        RuntimeError: write failed
    """
    reports_dir = Path(data_dir) / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / f"{dispatch_id}.md"
    if report_path.exists():
        return report_path

    if findings:
        findings_lines = "\n".join(
            f"- [{f.get('severity', 'info').upper()}] {f.get('message', str(f))}"
            for f in findings
        )
    else:
        findings_lines = "None"

    content = (
        f"# Dispatch {dispatch_id}\n\n"
        f"- Provider: {provider}\n"
        f"- Terminal: {terminal_id}\n"
        f"- Duration: {duration_seconds:.1f}s\n\n"
        f"## Instruction\n\n{instruction or '(not captured)'}\n\n"
        f"## Response\n\n{response_text or '(no response captured)'}\n\n"
        f"## Findings\n\n{findings_lines}\n"
    )

    tmp_path = report_path.with_suffix(".md.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, report_path)
    except OSError as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"governance_emit: unified report write failed for dispatch={dispatch_id}: {exc}"
        ) from exc

    logger.info(
        "governance_emit: unified report written dispatch=%s provider=%s path=%s",
        dispatch_id, provider, report_path,
    )
    return report_path
