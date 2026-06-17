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
  6. Call ensure_receipt() to guarantee a completion receipt exists for every
     subscription-session dispatch — appending a lane-synthesized receipt if
     the worker never emitted one (gap #4 / VNX_RECEIPT_FALLBACK default-on).

govern() NEVER raises — any unhandled error emits a minimal honest synthesized
body and returns GovernedOutcome(contract_status="synthesized", error=str(e)).

Synthesis is GIT-DERIVED, never capture-pane.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# scripts/ dir resolved from this file's location (scripts/lib/dispatch_govern.py → scripts/).
# Used to ensure append_receipt (which registers the facade) is importable at runtime,
# since the tmux dispatch.sh sets PYTHONPATH to scripts/lib only (not scripts/).
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)


# ---------------------------------------------------------------------------
# Receipt dedup — authored > synthesized, newest timestamp wins within tier
# ---------------------------------------------------------------------------

_AUTHORITATIVE_STATUSES = frozenset({"done", "failed"})


def _receipt_authority(receipt: dict) -> int:
    """Return authority rank: 1 = authoritative (done/failed), 0 = unknown/other."""
    return 1 if (receipt.get("status") or "") in _AUTHORITATIVE_STATUSES else 0


def dedup_completion_receipts(receipts: list) -> "dict | None":
    """Pick the preferred receipt from multiple completion receipts for one dispatch.

    Preference order (highest to lowest):
      1. Authoritative status (done/failed) > unknown/other
      2. Non-synthesized (worker-authored) > synthesized within the same authority tier
      3. Newest ISO 8601 timestamp within the same authority+authored tier
    Fallback: last entry in list order.

    Never raises.
    """
    if not receipts:
        return None
    if len(receipts) == 1:
        return receipts[0]

    # Tier 1: authoritative status outranks unknown
    authoritative = [r for r in receipts if _receipt_authority(r)]
    pool = authoritative if authoritative else receipts

    # Tier 2: authored (non-synthesized) outranks synthesized within the pool
    authored = [r for r in pool if not r.get("synthesized")]
    pool = authored if authored else pool

    # Tier 3: newest timestamp
    try:
        return max(pool, key=lambda r: str(r.get("timestamp") or ""))
    except Exception:  # noqa: BLE001
        return pool[-1]


# ---------------------------------------------------------------------------
# Receipt fallback — F1 lane-synthesized receipt guarantee
# ---------------------------------------------------------------------------

def ensure_receipt(
    spec: "GovernSpec",
    raw: "GovernRaw",
    lane: str,
    *,
    report_path: Optional[Path],
    contract_status: str,
    permission_enforcement: str,
) -> None:
    """Append a lane-synthesized completion receipt when the worker never emitted one.

    Gate: VNX_RECEIPT_FALLBACK (default "1"). Set to "0" to disable.
    Fires only when raw.receipt is None — i.e. the worker never produced a receipt
    before the deadline. Never raises — best-effort audit trail.

    The synthesized receipt uses source="tmux_interactive_lane_synthesized" so
    dedup_completion_receipts() can distinguish it from a worker-authored one.
    If the worker later emits its own receipt, the authored one wins on readback.
    """
    if os.environ.get("VNX_RECEIPT_FALLBACK", "1").strip() == "0":
        return
    if raw.receipt is not None:
        return

    receipts_file = spec.state_dir / "t0_receipts.ndjson"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    synthesized_receipt: dict = {
        "event_type": "subprocess_completion",
        "dispatch_id": spec.dispatch_id,
        "terminal": spec.terminal_id,
        "terminal_id": spec.terminal_id,
        "status": "failed",
        "source": "tmux_interactive_lane_synthesized",
        "synthesized": True,
        "failure_reason": raw.failure_reason,
        "contract_status": contract_status,
        "permission_enforcement": permission_enforcement,
        "timestamp": ts,
        "provider": "claude",
        "sub_provider": "anthropic",
        "model": spec.model or "unknown",
        "lane": lane,
    }
    if report_path is not None:
        synthesized_receipt["report_path"] = str(report_path)

    try:
        if _SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, _SCRIPTS_DIR)
        from append_receipt import append_receipt_payload  # noqa: PLC0415
        append_receipt_payload(
            synthesized_receipt,
            receipts_file=str(receipts_file),
            cache_window_seconds=300,
        )
        logger.info(
            "ensure_receipt: appended lane-synthesized receipt for dispatch=%s receipts_file=%s",
            spec.dispatch_id, receipts_file,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ensure_receipt: failed to append synthesized receipt for dispatch=%s: %s",
            spec.dispatch_id, exc,
        )


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
    model: Optional[str] = None


@dataclass
class GovernRaw:
    receipt: Optional[dict] = None
    duration_seconds: float = 0.0
    # Reason recorded on a lane-synthesized fallback receipt (raw.receipt is None).
    # Defaults to the deadline case; abort paths (ready_timeout / submit_failed /
    # no_progress) pass their own reason so the audit trail does not mislabel them.
    failure_reason: str = "tmux_receipt_deadline_exceeded"
    # Lane-parsed token counts {input, output, cache_read} for lanes with no usage API
    # (e.g. the claude tmux subscription lane, parsed from the pane TUI counter). Used as
    # a fallback for the report frontmatter when the worker receipt carries no token_usage.
    token_usage: Optional[dict] = None


@dataclass
class GovernedOutcome:
    report_path: Optional[Path]
    contract_status: str  # "authored" | "synthesized" | "violated"
    body_result: object = None  # BodyResult from validate_body
    permission_enforcement: str = "soft"
    error: Optional[str] = None


def _has_yaml_frontmatter(text: str) -> bool:
    """Return True if text begins with a YAML frontmatter block (--- ... ---)."""
    stripped = text.lstrip("\n")
    return stripped.startswith("---\n") or stripped.startswith("---\r\n")


def _split_yaml_frontmatter(text: str) -> "tuple[dict, str]":
    """Split text at YAML frontmatter boundary.

    Returns (frontmatter_dict, body_text) where body_text is the content
    after the closing --- delimiter with leading blank lines stripped.
    If no valid frontmatter found, returns ({}, original text).
    Best-effort: never raises.
    """
    try:
        import yaml  # noqa: PLC0415
        stripped = text.lstrip("\n")
        if not stripped.startswith("---\n") and not stripped.startswith("---\r\n"):
            return {}, text
        lines = stripped.split("\n")
        # lines[0] == "---"; find closing ---
        close_line: Optional[int] = None
        for i, line in enumerate(lines[1:], 1):
            if line.rstrip("\r") == "---":
                close_line = i
                break
        if close_line is None:
            return {}, text
        fm_text = "\n".join(lines[1:close_line])
        body_lines = lines[close_line + 1:]
        while body_lines and not body_lines[0].strip():
            body_lines = body_lines[1:]
        body = "\n".join(body_lines)
        fm = yaml.safe_load(fm_text)
        if not isinstance(fm, dict):
            return {}, text
        return fm, body
    except Exception:  # noqa: BLE001
        return {}, text


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
    ensure_receipt(
        spec, raw, lane,
        report_path=rp,
        contract_status="synthesized",
        permission_enforcement="soft",
    )
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
    # authored: force-write with frontmatter so ALL reports are schema-uniform.
    #   The worker body is preserved; the frontmatter block is prepended.
    #   If the worker already wrote frontmatter (defensive), merge — govern's
    #   required fields take precedence.
    # synthesized/violated: overwrite=True so a stale placeholder is replaced.
    is_authored = contract_status == "authored"

    # Build schema-complete frontmatter (unified_report_v1.json requires 14 fields).
    # Partial frontmatter raises SchemaViolation under VNX_SCHEMA_STRICT=1 and
    # blocks the atomic write, leaving a stale placeholder on disk.
    receipt_data = raw.receipt or {}
    _model = (receipt_data.get("model") or "unknown")
    _exit_code = int(receipt_data.get("exit_code", 0) or 0)
    _raw_token = receipt_data.get("token_usage") or raw.token_usage or {}
    _token_usage = {
        "input": int(_raw_token.get("input") or 0),
        "output": int(_raw_token.get("output") or 0),
        "cache_read": int(
            _raw_token.get("cache_read") or _raw_token.get("cache_hit") or 0
        ),
    }
    _cost_usd = float(receipt_data.get("cost_usd") or 0.0)
    frontmatter = {
        "schema_version": 1,
        "dispatch_id": dispatch_id,
        "provider": "claude",
        "sub_provider": "anthropic",
        "model": _model,
        "terminal_id": spec.terminal_id,
        "pool_id": "interactive",
        "role": "backend-developer",
        "task_class": "implementation",
        "pr_id": spec.pr_id or "none",
        "duration_seconds": float(raw.duration_seconds),
        "exit_code": _exit_code,
        "token_usage": _token_usage,
        "cost_usd": _cost_usd,
        "route_decision": {
            "strategy": "synthesized",
            "selected_provider": "claude",
            "selected_model": _model,
            "reason": (
                f"tmux interactive lane — govern synthesis (terminal={spec.terminal_id})"
            ),
        },
        "lane": lane,
        "contract_status": contract_status,
        "permission_enforcement": permission_enforcement,
    }

    status = (raw.receipt or {}).get("status", "unknown") if raw.receipt else "timeout"

    # Determine the exact body and frontmatter to write.
    # Authored path: detect if worker already wrote frontmatter (defensive merge);
    # otherwise use the worker body as-is with our govern frontmatter prepended.
    if is_authored and _has_yaml_frontmatter(body):
        existing_fm, body_only = _split_yaml_frontmatter(body)
        # Govern's required fields take precedence to guarantee schema-uniform output.
        emit_frontmatter: dict = {**existing_fm, **frontmatter}
        emit_body: str = body_only
    else:
        emit_frontmatter = frontmatter
        emit_body = body

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
            frontmatter=emit_frontmatter,
            body_override=emit_body,
            overwrite=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("govern: emit_unified_report failed for %s: %s", dispatch_id, exc)
        ensure_receipt(
            spec, raw, lane,
            report_path=None,
            contract_status=contract_status,
            permission_enforcement=permission_enforcement,
        )
        return GovernedOutcome(
            report_path=None,
            contract_status=contract_status,
            body_result=final_bv,
            permission_enforcement=permission_enforcement,
            error=str(exc),
        )

    ensure_receipt(
        spec, raw, lane,
        report_path=report_path,
        contract_status=contract_status,
        permission_enforcement=permission_enforcement,
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
