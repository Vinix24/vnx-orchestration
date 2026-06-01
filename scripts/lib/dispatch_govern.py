"""dispatch_govern — shared GOVERN step for tmux and subprocess Claude lanes.

govern() is the single authority for producing the final unified report body:
  1. If a worker-authored report exists at unified_reports/<dispatch_id>.md
     and passes validate_body: use it as-is (contract_status="authored").
  2. Else synthesize a git-derived body (contract_status="synthesized").
  3. Run validate_body on the final body; violations flagged for both authored
     and synthesized bodies. authored+enforce returns early; synthesized always
     shadows (flags, does not reject).
  4. Call emit_unified_report with body_override + overwrite=True (for non-authored)
     so a stale placeholder file is replaced, not kept.
  5. Stamp contract_status + permission_enforcement in frontmatter.

govern() NEVER raises — any unhandled error emits a minimal honest synthesized
body and returns GovernedOutcome(contract_status="synthesized", error=str(e)).

Synthesis is GIT-DERIVED, never capture-pane.
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

    Never raises — any unhandled internal error emits a minimal honest synthesized
    body with contract_status="synthesized" and error set.

    Order:
      a. Check for worker-authored report; validate it.
      b. If absent or placeholder: synthesize from git.
      c. Validate final body (shadow-mode for both lanes; authored+enforce may reject).
      d. Emit via emit_unified_report(body_override=..., overwrite=True for non-authored)
         so a stale placeholder file is replaced rather than kept by idempotency.
    """
    dispatch_id = spec.dispatch_id
    try:
        return _govern_impl(spec, raw, lane)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "govern: unhandled error dispatch=%s lane=%s: %s — emitting error body",
            dispatch_id, lane, exc,
        )
        return _govern_error_fallback(spec, raw, lane, exc)


def _govern_error_fallback(
    spec: GovernSpec, raw: GovernRaw, lane: str, exc: Exception
) -> GovernedOutcome:
    """Emit a minimal honest body when _govern_impl hit an unrecoverable error."""
    from governance_emit import emit_unified_report  # noqa: PLC0415

    dispatch_id = spec.dispatch_id
    error_body = (
        f"# Dispatch {dispatch_id}\n\n"
        f"- Lane: {lane}\n"
        f"- contract_status: synthesized\n\n"
        f"## Summary\n\n"
        f"Governance error during dispatch close-out. "
        f"Report synthesized by error handler. Error: {exc}\n\n"
        f"## Changes\n\nNot available — governance error during synthesis.\n\n"
        f"## Verification\n\nNone — error path.\n\n"
        f"## Open Items\n\nGovernance error: {exc}\n"
    )
    try:
        rp = emit_unified_report(
            dispatch_id=dispatch_id,
            terminal_id=spec.terminal_id,
            provider="claude",
            instruction=spec.instruction,
            response_text=f"Lane: {lane}. Error.",
            findings=[],
            duration_seconds=raw.duration_seconds,
            data_dir=spec.data_dir,
            body_override=error_body,
            overwrite=True,
        )
    except Exception:  # noqa: BLE001
        rp = None
    return GovernedOutcome(
        report_path=rp,
        contract_status="synthesized",
        error=str(exc),
    )


def _govern_impl(spec: GovernSpec, raw: GovernRaw, lane: str) -> GovernedOutcome:
    """Core govern logic. Called by govern(); any exception is caught there."""
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

    # -- c. Validate final body — applies to BOTH authored and synthesized ----
    final_bv = validate_body(body, pr_id=spec.pr_id)
    if not final_bv.valid:
        logger.warning(
            "govern: %s body failed validation for dispatch=%s "
            "(missing=%s, placeholder=%s) — flagging violated%s",
            contract_status, dispatch_id, final_bv.missing, final_bv.placeholder,
            " (enforcing)" if (enforce and contract_status == "authored") else " (shadow)",
        )
        # authored+enforce: return early; synthesized or soft: stamp and continue.
        if enforce and contract_status == "authored":
            return GovernedOutcome(
                report_path=None,
                contract_status="violated",
                body_result=final_bv,
                permission_enforcement="strict",
                error=f"contract_violated: missing={final_bv.missing}",
            )
        contract_status = "violated"

    permission_enforcement = "strict" if enforce else "soft"

    # -- d. Emit report with final body ---------------------------------------
    # authored: idempotent (no overwrite) — worker already wrote the valid file.
    # synthesized/violated: overwrite=True so a stale placeholder is replaced.
    is_authored = contract_status == "authored"
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
            body_override=body if not is_authored else None,
            overwrite=not is_authored,
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

    body = (
        f"# Dispatch {dispatch_id}\n\n"
        f"- Lane: tmux_interactive\n"
        f"- Status: {status}\n"
        f"- contract_status: synthesized\n\n"
        f"## Summary\n\n{summary}\n\n"
        f"## Changes\n\n{changes}\n\n"
        f"## Verification\n\n{verification}\n\n"
        f"## Open Items\n\n{open_items}\n"
    )

    if spec.pr_id:
        body += f"\n## PR\n\nPR #{spec.pr_id} (synthesized — see branch)\n"

    return body


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
