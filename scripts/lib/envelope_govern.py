"""envelope_govern.py — GOVERN-seam entry point for the dispatch_envelope
module family.

Leaf module for its own body, but unlike envelope_prepare.py and
envelope_govern_support.py it is NOT dependency-free: ``_govern`` is the
caller that binds six symbols imported from sibling envelope_* modules
(never from dispatch_envelope itself — the facade imports FROM here, never
the reverse). Moved unchanged from dispatch_envelope.py as PR-4 of the
dispatch-monolith-split (dispatch-monolith-split, PR-4 of 6) — see
dispatch_envelope.py's module docstring for the split's seam order.

Binding note (the rule this whole split obeys): a name-string coupling
(``patch("dispatch_envelope.X")``) resolves against the globals of the
module the CALLER lives in, not the new home of the symbol X. ``_govern`` IS
the caller for ``_archive_dispatch_events`` / ``_clear_dispatch_events`` /
``_receipt_exists_for_dispatch`` / ``_verify_role_application`` /
``_verification_from_report`` / ``_resolve_fix_forward_diff`` — moving
``_govern`` here re-targets every coupling to those six symbols onto
``envelope_govern.<name>``. dispatch_envelope.py's own ``_govern(...)``
call sites stay in the facade, so ``patch("dispatch_envelope._govern")``
keeps binding unchanged.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dispatch_identity import _IDENTITY_UNRESOLVED  # single canonical sentinel (dispatch-20260804-190000)
from envelope_types import EnvelopeGovernError, EnvelopeSpec, _AdapterResult
from envelope_prepare import _verify_role_application
from envelope_govern_support import (
    _archive_dispatch_events,
    _clear_dispatch_events,
    _receipt_exists_for_dispatch,
    _resolve_fix_forward_diff,
    _verification_from_report,
)

logger = logging.getLogger(__name__)

# OI-1017/OI-1048 L2: greppable marker for observable body-contract violations
# on the envelope lanes. The receipt status is NOT changed (binding is a
# separate, later PR). Consumers filter on the warning code or grep the log
# for this prefix to distinguish observable from binding violations.
_CONTRACT_OBSERVE_MARKER = "VNX_CONTRACT_OBSERVE_VIOLATION"


# ---------------------------------------------------------------------------
# GOVERN
# ---------------------------------------------------------------------------


def _govern(
    spec: EnvelopeSpec,
    adapter_result: _AdapterResult,
    start_time: datetime,
    end_time: datetime,
    phantom_diff: Optional[str] = None,
    integrity: Optional[Any] = None,
    base_ref: str = "origin/main",
) -> tuple:
    """Emit unified_report then dispatch receipt. Returns (report_path, receipt_path).

    Fail-closed contract: raises EnvelopeGovernError when receipt_path is None or
    absent on disk after emit — never silently loses a receipt.
    Report is emitted first so the receipt can carry the linkage (ADR-005 ordering).

    Idempotent dedup: when the receipt NDJSON already contains a line for this
    dispatch_id (written by deliver_with_recovery's internal close-out as a safety
    net), the GOVERN receipt write is skipped.  This avoids double-emit during the
    migration period where both legacy and envelope paths may run.
    """
    from governance_emit import emit_dispatch_receipt, emit_unified_report  # noqa: PLC0415

    duration = (end_time - start_time).total_seconds()

    # OI-882: the envelope previously hardcoded cost_usd=None even though
    # adapter_result.token_usage carried real tokens and wave7_models.yaml has
    # the per-provider prices. Compute the estimate the same way
    # provider_dispatch._emit_governance does, using the actual model the spawn
    # resolved (falling back to the spec model). Non-fatal: an unresolvable
    # price leaves cost_usd None instead of failing the receipt.
    cost_usd: Optional[float] = None
    _cost_model = getattr(adapter_result, "model", None) or spec.model
    try:
        from provider_dispatch import _compute_cost  # noqa: PLC0415
        cost_usd = _compute_cost(spec.provider, _cost_model, adapter_result.token_usage)
    except Exception as _cost_exc:  # noqa: BLE001 — cost must never break receipt emission
        logger.debug(
            "envelope._govern: cost compute failed dispatch=%s provider=%s (non-fatal): %s",
            spec.dispatch_id, spec.provider, _cost_exc,
        )

    # OI-878/OI-902: end-of-dispatch event archive. Archive the live event stream
    # under THIS dispatch's id BEFORE the receipt so the receipt can carry
    # events_path (parity with provider_dispatch._emit_governance); the clear
    # runs in the finally below AFTER the receipt write. Without this the
    # envelope path never rotated the ring buffer at end-of-dispatch — only the
    # NEXT dispatch's write-side boundary guard did, so the LAST dispatch in a
    # series leaked its events into the live file.
    events_path, _events_archive_ok = _archive_dispatch_events(spec.terminal_id, spec.dispatch_id)

    # REPORT first — idempotent: worker-written file is preserved, not overwritten.
    # OI-903: on failure/timeout, a killed worker's partial report is preserved
    # under a .partial.md sidecar so the canonical report stays contract-compliant
    # while the partial output remains retrievable.
    report_path: Optional[Path] = None
    try:
        report_path = emit_unified_report(
            dispatch_id=spec.dispatch_id,
            terminal_id=spec.terminal_id,
            provider=spec.provider,
            instruction=spec.instruction,
            response_text=adapter_result.completion_text,
            findings=[],
            duration_seconds=duration,
            data_dir=spec.data_dir,
            preserve_partial=adapter_result.status != "success",
        )
    except Exception as exc:
        logger.error(
            "envelope._govern: report emit failed dispatch=%s: %s — proceeding to receipt",
            spec.dispatch_id,
            exc,
        )

    # OI-1017/OI-1048 L2: observable body-contract validation on envelope lanes.
    # The tmux lane already binds via dispatch_govern._govern_impl(); the headless
    # and provider lanes go through here without a validate_body() call. This is
    # the shared envelope entry point — one fix covers both non-binding lanes.
    # Observable mode: the receipt status is NOT changed (binding is a separate,
    # later PR). The violation is logged with a greppable marker and a filterable
    # warning code appended to the receipt's warnings[] array so the follow-up PR
    # only needs to flip the switch from observe to enforce.
    _contract_warnings: List[Dict[str, Any]] = []
    if report_path is not None:
        try:
            from report_body_contract import validate_body  # noqa: PLC0415
            _report_text = report_path.read_text(encoding="utf-8", errors="replace")
            _body_result = validate_body(_report_text)
            if not _body_result.valid:
                _violation_msg = (
                    f"{_CONTRACT_OBSERVE_MARKER}: report body contract "
                    f"violated — missing sections: {', '.join(_body_result.missing)}"
                )
                logger.warning(
                    "envelope._govern: %s dispatch=%s",
                    _violation_msg, spec.dispatch_id,
                )
                _contract_warnings.append({
                    "code": "report_contract_violated",
                    "severity": "warn",
                    "message": _violation_msg,
                })
        except OSError:
            pass  # can't read report file — skip validation, don't break receipt

    # RECEIPT second — fail-closed, with idempotent dedup. The end-of-dispatch
    # event clear runs in finally so the live stream is truncated even when the
    # fail-closed receipt emit raises EnvelopeGovernError.
    receipt_path: Optional[Path] = None
    try:
        ndjson_path = spec.state_dir / "t0_receipts.ndjson"
        if _receipt_exists_for_dispatch(ndjson_path, spec.dispatch_id):
            logger.info(
                "envelope._govern: receipt already exists for dispatch=%s — skipping (idempotent dedup)",
                spec.dispatch_id,
            )
            receipt_path = ndjson_path
        else:
            try:
                # receipt-quality PR-1 + W7 fix: resolve dispatch identity
                # (role) just before the emit. The shared resolver prefers the
                # genuinely-set spec role (never the fake sentinel ""
                # default, OI-981), falls back to dispatch_metadata, then stamps
                # identity_unresolved. FAIL-OPEN — a resolver error must never
                # break receipt emission.
                try:
                    from dispatch_identity import resolve_effective_role  # noqa: PLC0415
                    _project_id = getattr(spec, "project_id", None)
                    if not _project_id:
                        from dispatch_cli import _resolve_project_id  # noqa: PLC0415
                        _project_id = _resolve_project_id()
                    _role = resolve_effective_role(
                        spec.role, spec.dispatch_id, _project_id, state_dir=spec.state_dir,
                    )
                except Exception:  # noqa: BLE001 — identity join is fail-open
                    logger.debug(
                        "envelope._govern: role resolution failed open dispatch=%s",
                        spec.dispatch_id,
                        exc_info=True,
                    )
                    _role = _IDENTITY_UNRESOLVED

                # Deterministic role-applied control (dispatch-20260801-w10 +
                # OI-983): did the resolved role source actually reach the enriched
                # prompt (spec.instruction)? Verified against the SAME resolved
                # _role that is stamped on the receipt so role_applied is truthful.
                # FAIL-OPEN — a verification error must never break receipt
                # emission; the fields simply stay None (omitted).
                _role_app = _verify_role_application(
                    spec.instruction, spec.terminal_id, _role,
                )

                # receipt-quality PR-B2 fix-forward (Finding C): aggregate
                # PreToolUse-hook tool-call signals for this dispatch
                # (toolcall_signals.py), mirroring provider_dispatch._emit_
                # governance's wiring so the claude/subprocess-adapter lane also
                # populates these fields when VNX_TMUX_SIGNAL_DIR is set.
                # FAIL-OPEN — an aggregation error or absent signal log must
                # never break receipt emission; each field simply stays None
                # (omitted by ReceiptV2).
                _toolcall_signals: Dict[str, int] = {}
                try:
                    _signal_dir = os.environ.get("VNX_TMUX_SIGNAL_DIR")
                    if _signal_dir:
                        from toolcall_signals import aggregate_toolcall_signals  # noqa: PLC0415
                        _toolcall_signals = aggregate_toolcall_signals(_signal_dir) or {}
                except Exception:  # noqa: BLE001 — observability signal must never break receipt emission
                    logger.debug(
                        "envelope._govern: toolcall signal aggregation failed dispatch=%s (non-fatal)",
                        spec.dispatch_id,
                        exc_info=True,
                    )
                    _toolcall_signals = {}

                # ADR-005: emit cost event BEFORE receipt write. provider_dispatch
                # and recovery raise on failure (fail-loud); the envelope is the
                # third receipt path and matches them, but wraps in try/except so a
                # cost-log failure never breaks the fail-closed receipt contract.
                try:
                    from provider_costs import emit_provider_cost  # noqa: PLC0415
                    from project_scope import resolve_stamp_project_id, TenantUnresolved  # noqa: PLC0415
                    _cost_pid = ""
                    try:
                        _cost_pid = resolve_stamp_project_id(
                            db_path=str(spec.state_dir / "quality_intelligence.db")
                        )
                    except TenantUnresolved:
                        pass  # emit falls back to env; cost-audit must not lose the event
                    emit_provider_cost(
                        provider=spec.provider,
                        model=_cost_model,
                        input_tokens=(
                            adapter_result.token_usage.get("input")
                            if adapter_result.token_usage else None
                        ),
                        output_tokens=(
                            adapter_result.token_usage.get("output")
                            if adapter_result.token_usage else None
                        ),
                        cost_usd_estimate=cost_usd,
                        dispatch_id=spec.dispatch_id,
                        project_id=_cost_pid,
                    )
                except Exception as _cost_event_exc:  # noqa: BLE001 — cost event must not break receipt
                    logger.warning(
                        "envelope._govern: cost event emit failed dispatch=%s (non-fatal): %s",
                        spec.dispatch_id, _cost_event_exc,
                    )

                # OI-866: classify failure so the receipt carries a distinguishable
                # failure_reason + failure_class instead of a silent
                # "(no error captured)" log line.
                _classification: Dict[str, Optional[str]] = {"failure_class": None, "failure_reason": None}
                if adapter_result.status != "success":
                    try:
                        from failure_classification import classify_failure  # noqa: PLC0415
                        _classification = classify_failure(
                            status=adapter_result.status,
                            error=adapter_result.error,
                            completion_text=adapter_result.completion_text,
                            timed_out=adapter_result.timed_out,
                            provider=spec.provider,
                            duration_seconds=duration,
                            returncode=adapter_result.returncode,
                        )
                    except Exception:  # noqa: BLE001 — classification is best-effort
                        logger.debug(
                            "envelope._govern: failure classification failed dispatch=%s (non-fatal)",
                            spec.dispatch_id,
                            exc_info=True,
                        )

                receipt_path = emit_dispatch_receipt(
                    dispatch_id=spec.dispatch_id,
                    terminal_id=spec.terminal_id,
                    provider=spec.provider,
                    model=_cost_model,
                    pr_id=spec.pr_id,
                    status=adapter_result.status,
                    completion_pct=100 if adapter_result.status == "success" else 0,
                    risk=0.0,
                    findings=[],
                    duration_seconds=duration,
                    token_usage=adapter_result.token_usage,
                    cost_usd=cost_usd,
                    state_dir=spec.state_dir,
                    report_path=str(report_path) if report_path else None,
                    events_path=events_path,
                    final_prompt_path=getattr(integrity, "final_prompt_path", None),
                    final_prompt_sha256=getattr(integrity, "final_prompt_sha256", None),
                    injection_reconstructs=(
                        getattr(integrity, "injection_reconstructs", None)
                        if integrity is not None
                        else None
                    ),
                    # ADR-035 §3.1.1: envelope sub-path — the report is already
                    # on disk (emit_unified_report ran above), so extract
                    # verification{} from it via the shared regex extractor.
                    verification=_verification_from_report(report_path),
                    role=_role,
                    receipt_kind="dispatch",
                    role_applied=(
                        getattr(_role_app, "role_applied", None) if _role_app is not None else None
                    ),
                    role_tier=(
                        getattr(_role_app, "tier", None) if _role_app is not None else None
                    ),
                    role_not_applied_reason=(
                        getattr(_role_app, "reason", None) if _role_app is not None else None
                    ),
                    role_source_path=(
                        getattr(_role_app, "source_path", None) if _role_app is not None else None
                    ),
                    session_id=adapter_result.session_id,
                    tool_call_count=_toolcall_signals.get("tool_call_count"),
                    tool_call_failures=_toolcall_signals.get("tool_call_failures"),
                    tool_call_retries=_toolcall_signals.get("tool_call_retries"),
                    deadline_seconds=spec.deadline_seconds,
                    failure_reason=_classification.get("failure_reason"),
                    failure_class=_classification.get("failure_class"),
                    # Chain-link (dispatch-20260802-model-ssot-en-ketenlink):
                    # the door's values flow plan -> EnvelopeSpec -> receipt.
                    parent_dispatch=spec.parent_dispatch,
                    task_class=spec.task_class,
                    tier_from=spec.tier_from,
                    tier_to=spec.tier_to,
                    # OI-1017/OI-1048 L2: observable body-contract warnings.
                    # None when the report passes or couldn't be read; a list
                    # with a filterable warning code when violated.  The receipt
                    # status field is NOT affected (the violation is observable,
                    # not yet binding — a separate PR makes it binding).
                    warnings=_contract_warnings or None,
                )
            except Exception as exc:
                raise EnvelopeGovernError(
                    f"envelope._govern: receipt emit raised for dispatch={spec.dispatch_id}: {exc}"
                ) from exc

            if receipt_path is None:
                raise EnvelopeGovernError(
                    f"envelope._govern: receipt_path is None after emit "
                    f"(fail-closed) dispatch={spec.dispatch_id}"
                )
            if not receipt_path.exists():
                raise EnvelopeGovernError(
                    f"envelope._govern: receipt file absent on disk after emit "
                    f"path={receipt_path} dispatch={spec.dispatch_id} (fail-closed)"
                )
    finally:
        # OI-878/OI-902: truncate the live event stream now that the archive
        # (top of _govern) and the receipt write are complete. Best-effort.
        # OI-918: only when the archive step actually succeeded (or had nothing
        # to archive) — clearing after a FAILED archive would destroy exactly
        # the events we wanted to preserve. On an archive failure the live file
        # is left in place for the next dispatch's write-side boundary guard
        # (#1276) to rotate, which is the second line of defence it exists for.
        if _events_archive_ok:
            _clear_dispatch_events(spec.terminal_id, spec.dispatch_id)
        else:
            logger.warning(
                "envelope: end-dispatch clear skipped terminal=%s dispatch=%s — "
                "archive failed; live file left for the next dispatch's boundary guard",
                spec.terminal_id, spec.dispatch_id,
            )

    if adapter_result.status != "success":
        # Fail-loud: a failure/timeout/empty-completion receipt must never be silent.
        # dispatch_cli returns only an integer exit code for the provider lane, so this
        # log line is often the only place the raw error surfaces — print it in full.
        logger.error(
            "envelope._govern: dispatch=%s provider=%s status=%s report=%s receipt=%s "
            "completion_len=%d error=%s",
            spec.dispatch_id,
            spec.provider,
            adapter_result.status,
            report_path,
            receipt_path,
            len(adapter_result.completion_text or ""),
            adapter_result.error or "(no error captured)",
        )
    else:
        logger.info(
            "envelope._govern: dispatch=%s status=%s report=%s receipt=%s",
            spec.dispatch_id,
            adapter_result.status,
            report_path,
            receipt_path,
        )
    # P0.2: inline phantom-guard (provider lanes — the kimi/glm/deepseek text-only fabrication
    # vector). A delivery worker that reports success with no worktree/branch diff is rejected via
    # a corrective failed receipt. worktree_path is unavailable on EnvelopeSpec, so the guard derives
    # the dispatch/<id> branch (isolated dispatches) or abstains (never false-rejects). Non-fatal.
    try:
        from phantom_guard import record_phantom_if_any  # noqa: PLC0415
        _tok = adapter_result.token_usage or {}
        # Fix-forward: an empty own-worktree/dispatch-branch diff is falsely read as phantom when
        # the dispatch targets an existing PR (spec.pr_id) and pushed its commit onto THAT branch
        # instead — resolve the pushed branch and use its diff when the own diff is empty.
        _effective_diff = _resolve_fix_forward_diff(spec, phantom_diff, base_ref=base_ref)
        record_phantom_if_any(
            dispatch_id=spec.dispatch_id,
            role=spec.role,
            status=adapter_result.status,
            token_usage=(int(_tok.get("input", 0) or 0) + int(_tok.get("output", 0) or 0)) or None,
            worktree_path=None,
            base_sha=None,
            worktree_diff=_effective_diff,  # F1: pre-captured before the worktree teardown
            receipts_file=str(spec.state_dir / "t0_receipts.ndjson"),
            state_dir=spec.state_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "envelope._govern: phantom-guard check failed (non-fatal) dispatch=%s: %s",
            spec.dispatch_id, exc,
        )
        try:
            from phantom_guard import record_guard_error  # noqa: PLC0415
            record_guard_error(
                dispatch_id=spec.dispatch_id,
                receipts_file=str(spec.state_dir / "t0_receipts.ndjson"),
                error=exc,
            )
        except Exception:  # noqa: BLE001 — the guard-error audit signal must never make _govern fatal
            logger.error("envelope._govern: guard-error audit signal itself failed dispatch=%s", spec.dispatch_id)
    return report_path, receipt_path
