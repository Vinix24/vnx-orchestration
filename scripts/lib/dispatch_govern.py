"""dispatch_govern — shared GOVERN step for tmux and subprocess Claude lanes.

govern() is the single authority for producing the final unified report body:
  1. If a worker-authored report exists at unified_reports/<dispatch_id>.md
     and passes validate_body: use it as-is (contract_status="authored").
  2. Else synthesize a git-derived body (contract_status="synthesized").
  3. Run validate_body on the final body; authored-but-invalid in shadow mode
     flags contract_status="violated" (does not reject for tmux lane).
  4. Call emit_unified_report with body_override so the body is final before emit.
  5. Stamp contract_status + permission_enforcement in frontmatter.

Synthesis is GIT-DERIVED, never capture-pane.
Body is final BEFORE emit (emit is idempotent-on-exists, governance_emit.py:198).
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GovernSpec:
    dispatch_id: str
    terminal_id: str
    instruction: str
    data_dir: Path
    state_dir: Path
    pr_id: Optional[str] = None
    base_sha: Optional[str] = None
    worktree_path: Optional[Path] = None


@dataclass
class GovernRaw:
    receipt: Optional[dict] = None
    duration_seconds: float = 0.0


@dataclass
class GovernedOutcome:
    report_path: Optional[Path]
    contract_status: str  # "authored" | "synthesized" | "violated"
    body_result: object = None  # BodyResult from validate_body
    permission_enforcement: str = "soft"
    error: Optional[str] = None


def govern(spec: GovernSpec, raw: GovernRaw, lane: str) -> GovernedOutcome:
    """Produce the final unified report for a dispatch, stamped with contract metadata.

    Order:
      a. Check for worker-authored report; validate it.
      b. If absent or placeholder: synthesize from git.
      c. Validate final body (shadow-mode for tmux; violations flagged, not rejected).
      d. Emit via emit_unified_report(body_override=...) so body is final before write.
    """
    from report_body_contract import BodyResult, validate_body  # noqa: PLC0415
    from governance_emit import emit_unified_report  # noqa: PLC0415

    dispatch_id = spec.dispatch_id
    reports_dir = Path(spec.data_dir) / "unified_reports"
    worker_report_path = reports_dir / f"{dispatch_id}.md"

    # Determine enforcement mode: tmux=shadow, subprocess=enforce (when flag on).
    contract_validate = os.environ.get("VNX_CONTRACT_VALIDATE", "shadow").strip().lower()
    enforce = contract_validate == "enforce"

    # -- a. Check for worker-authored report ----------------------------------
    body: Optional[str] = None
    contract_status = "synthesized"

    if worker_report_path.exists():
        try:
            candidate = worker_report_path.read_text(encoding="utf-8")
            bv = validate_body(candidate, pr_id=spec.pr_id)
            if bv.valid and not bv.placeholder:
                # Authored report passes — use it.
                body = candidate
                contract_status = "authored"
            else:
                logger.info(
                    "govern: worker report exists but invalid (missing=%s placeholder=%s)"
                    " for dispatch=%s — synthesizing",
                    bv.missing, bv.placeholder, dispatch_id,
                )
        except OSError as exc:
            logger.warning("govern: could not read worker report for %s: %s", dispatch_id, exc)

    # -- b. Synthesize if no valid authored body found ------------------------
    if body is None:
        body = _synthesize(spec, raw)
        contract_status = "synthesized"

    # -- c. Validate final body (shadow-mode flagging) -----------------------
    final_bv = validate_body(body, pr_id=spec.pr_id)
    if contract_status == "authored" and not final_bv.valid:
        logger.warning(
            "govern: authored body failed re-validation for dispatch=%s "
            "(missing=%s, placeholder=%s) — flagging violated%s",
            dispatch_id, final_bv.missing, final_bv.placeholder,
            " (enforcing)" if enforce else " (shadow)",
        )
        contract_status = "violated"
        if enforce:
            return GovernedOutcome(
                report_path=None,
                contract_status="violated",
                body_result=final_bv,
                permission_enforcement="strict" if enforce else "soft",
                error=f"contract_violated: missing={final_bv.missing}",
            )

    permission_enforcement = "strict" if enforce else "soft"

    # -- d. Emit report with final body ---------------------------------------
    # body_override ensures the final contract body is written instead of the
    # generic "## Response" wrapper.  emit is idempotent-on-exists (line 198),
    # so body MUST be final before we call it.
    #
    # For authored reports the worker already wrote the file; emit_unified_report
    # will detect the existing file and return without overwriting.  We rely on
    # the worker having written a valid body — the validate_body pass above
    # confirmed it.  Only synthesized or violated bodies need body_override.
    frontmatter = {
        "dispatch_id": dispatch_id,
        "terminal_id": spec.terminal_id,
        "lane": lane,
        "contract_status": contract_status,
        "permission_enforcement": permission_enforcement,
    }
    if spec.pr_id:
        frontmatter["pr_id"] = spec.pr_id

    status = (raw.receipt or {}).get("status", "unknown") if raw.receipt else "timeout"

    try:
        report_path = emit_unified_report(
            dispatch_id=dispatch_id,
            terminal_id=spec.terminal_id,
            provider="claude",
            instruction=spec.instruction,
            response_text=f"Lane: {lane}. Status: {status}.",
            findings=[],
            duration_seconds=raw.duration_seconds,
            data_dir=spec.data_dir,
            frontmatter=frontmatter,
            body_override=body if contract_status != "authored" else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("govern: emit_unified_report failed for %s: %s", dispatch_id, exc)
        return GovernedOutcome(
            report_path=None,
            contract_status=contract_status,
            body_result=final_bv,
            permission_enforcement=permission_enforcement,
            error=str(exc),
        )

    return GovernedOutcome(
        report_path=report_path,
        contract_status=contract_status,
        body_result=final_bv,
        permission_enforcement=permission_enforcement,
    )


def _synthesize(spec: GovernSpec, raw: GovernRaw) -> str:
    """Build a git-derived report body. NEVER uses capture-pane.

    Git is authoritative for what changed. Pane-scraping would serialize TUI
    chrome into content that looks authored but is garbage.
    """
    dispatch_id = spec.dispatch_id
    status = (raw.receipt or {}).get("status", "unknown") if raw.receipt else "timeout"

    # -- ## Summary -----------------------------------------------------------
    summary = _git_summary(spec, status)

    # -- ## Changes -----------------------------------------------------------
    changes = _git_changes(spec)

    # -- ## Verification ------------------------------------------------------
    verification = (
        "None — interactive lane (tmux-spawn). "
        "Report synthesized by governance layer; worker did not author a report file."
    )

    # -- ## Open Items --------------------------------------------------------
    open_items = f"Report synthesized by tmux lane; worker did not author unified_reports/{dispatch_id}.md."

    return (
        f"# Dispatch {dispatch_id}\n\n"
        f"- Lane: tmux_interactive\n"
        f"- Status: {status}\n"
        f"- contract_status: synthesized\n\n"
        f"## Summary\n\n{summary}\n\n"
        f"## Changes\n\n{changes}\n\n"
        f"## Verification\n\n{verification}\n\n"
        f"## Open Items\n\n{open_items}\n"
    )


def _git_summary(spec: GovernSpec, status: str) -> str:
    """Return git log subject + body for the worker commit, or a fallback message."""
    cwd = str(spec.worktree_path) if spec.worktree_path else None
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s%n%b"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=cwd,
        )
        msg = result.stdout.strip()
        if msg:
            return f"{msg}\n\nWorker status: {status}. Body synthesized by governance layer (no worker report file)."
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as exc:
        logger.debug("govern._git_summary: git log failed for %s: %s", spec.dispatch_id, exc)

    return (
        f"No commit on branch; worker emitted status={status}. "
        "Body synthesized by lane (no worker report)."
    )


def _git_changes(spec: GovernSpec) -> str:
    """Return git diff --stat between base_sha and HEAD, or a fallback message."""
    cwd = str(spec.worktree_path) if spec.worktree_path else None
    base = spec.base_sha

    if base:
        try:
            result = subprocess.run(
                ["git", "diff", "--stat", f"{base}..HEAD"],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=cwd,
            )
            stat = result.stdout.strip()
            if stat:
                return stat
        except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as exc:
            logger.debug("govern._git_changes: git diff failed for %s: %s", spec.dispatch_id, exc)

    # Fallback: no base_sha or git failed.
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~1..HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=cwd,
        )
        stat = result.stdout.strip()
        if stat:
            return f"(no base_sha; showing HEAD~1..HEAD)\n\n{stat}"
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass

    return "No git diff available — worktree path or base SHA not provided."
