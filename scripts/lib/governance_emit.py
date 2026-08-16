"""governance_emit.py — Shared governance receipt + unified report emitter (Wave 7 PR-7.6).

Used by both subprocess_dispatch.py (claude path) and provider_dispatch.py (multi-provider
path) so every dispatch writes a governance-enriched receipt and unified report.

ADR-005: NDJSON audit completeness. ADR-016: unified events.
ADR-035 §7.1: the receipt append itself is delegated to
``append_receipt_internals.idempotency._write_receipt_under_lock`` — the same
lock-file, hash-chain-stamping, validated append primitive Path 2
(``append_receipt_payload``) uses. This module no longer opens/locks/writes
``t0_receipts.ndjson`` itself.

Hard rules (PRD provider-governance-unification):
  - Provider field MUST match _PROVIDER_RE — raises ValueError on mismatch.
  - Receipt write MUST NOT silently fail — raises RuntimeError on write/validation failure.
  - Unified report uses tmp + os.replace for atomic write.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure the sibling scripts/lib modules resolve even when a caller imports
# governance_emit by path without scripts/lib already on sys.path.
_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from append_receipt_internals.common import AppendReceiptError
from append_receipt_internals.idempotency import (
    _cache_file_for,
    _compute_idempotency_key,
    _write_receipt_under_lock,
)
from append_receipt_internals.receipt_finalize import (
    classify_receipt_v2_warnings,
    commit_receipt_v2_fields,
)
from append_receipt_internals.validation import _validate_receipt
from receipt_schema import ReceiptV2
from report_body_contract import validate_body
from token_harvest import CLAUDE_HARNESS_PROVIDERS

logger = logging.getLogger(__name__)

# Matches append_receipt_internals.payload.append_receipt_payload's default so
# a receipt emitted via either write path dedups against the same window.
_RECEIPT_CACHE_WINDOW_SECONDS = 300

# The third (model-alias) segment allows "/" — the openrouter-arbitrary lane passes
# raw OpenRouter "vendor/model" paths as the alias (e.g. litellm:openrouter:openai/gpt-4o-mini).
_PROVIDER_RE = re.compile(
    r"^(claude|codex|gemini|kimi|deepseek-harness|glm-harness|litellm(:[a-z][a-z0-9_-]*(:[a-z][a-z0-9_./-]*)?)?|local-gemma)$"
)


def _validate_provider(provider: str) -> None:
    """Raise ValueError when provider doesn't match required pattern."""
    if not _PROVIDER_RE.match(provider or ""):
        raise ValueError(
            f"Invalid provider {provider!r}. "
            "Must match ^(claude|codex|gemini|kimi|deepseek-harness|glm-harness|litellm(:[a-z][a-z0-9_-]*(:[a-z][a-z0-9_./-]*)?)?|local-gemma)$"
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
    report_path: Optional[str] = None,
    events_path: Optional[str] = None,
    permission_enforcement: Optional[str] = None,
    mandate_id: Optional[str] = None,
    final_prompt_path: Optional[str] = None,
    final_prompt_sha256: Optional[str] = None,
    injection_reconstructs: Optional[bool] = None,
    verification: Optional[Dict[str, Any]] = None,
    warnings: Optional[List[Dict[str, Any]]] = None,
    role: Optional[str] = None,
    receipt_kind: Optional[str] = None,
    session_id: Optional[str] = None,
    tool_call_count: Optional[int] = None,
    tool_call_failures: Optional[int] = None,
    tool_call_retries: Optional[int] = None,
    deadline_seconds: Optional[int] = None,
    failure_reason: Optional[str] = None,
    failure_class: Optional[str] = None,
    delivery_state: Optional[str] = None,
    role_applied: Optional[bool] = None,
    role_tier: Optional[str] = None,
    role_not_applied_reason: Optional[str] = None,
    role_source_path: Optional[str] = None,
    parent_dispatch: Optional[str] = None,
    task_class: Optional[str] = None,
    tier_from: Optional[str] = None,
    tier_to: Optional[str] = None,
    permission_posture: Optional[str] = None,
    permission_profile: Optional[str] = None,
    permission_allow_pattern_count: Optional[int] = None,
) -> Path:
    """Atomic-append to t0_receipts.ndjson via the shared append primitive
    (ADR-035 §7.1) — same lock file, hash-chain stamping, and validator Path 2
    uses.

    The receipt shape itself is codified in ``receipt_schema.ReceiptV2``
    (receipt-quality PR-B0) — construction validates ``receipt_kind`` and
    ``to_dict()`` serializes byte-compatibly with the historical literal.

    Returns the receipt file path on success.

    ``report_path`` links the receipt to its emitted unified report. The path is
    deterministic (``unified_reports/<dispatch_id>.md``) so the caller can supply
    it even when the report is written after the receipt.

    ``events_path`` links the receipt to the archived NDJSON event stream for this
    dispatch (``events/archive/{terminal}/{dispatch_id}.ndjson``).  Null when the
    dispatch lane produces no event stream (tmux, claude subprocess) or when the
    archive step was skipped.  Turns dispatch→stream linkage from convention
    (matching dispatch_id in the filename) into an explicit data pointer.

    ``permission_enforcement``: when provided (e.g. "enforced"), stamps the
    receipt with the ADR-012 worker-permission enforcement mode. Only set when
    ``VNX_ENFORCE_WORKER_PERMISSIONS`` is active so flag-off receipts remain
    byte-identical to the pre-feature shape.

    ``final_prompt_path`` / ``final_prompt_sha256`` / ``injection_reconstructs``:
    the input-side audit pointer (final_prompt_integrity). ``final_prompt_path``
    points at the persisted assembled prompt, ``final_prompt_sha256`` pins its
    bytes, and ``injection_reconstructs`` records whether the raw instruction +
    recorded intelligence injections literally reconstruct that body. Each is only
    stamped when provided so lanes that do not yet compute integrity keep a
    byte-identical receipt shape.

    ``verification``: ADR-035 §3.1.1 — the v2 ``verification{}`` object. The
    envelope sub-path (``dispatch_envelope.py``) threads the report already on
    disk through ``report_parser.py::extract_validation`` and passes the
    result here; the multi-provider sub-path (``provider_dispatch.py``) passes
    ``{"method": "pending-report", ...}`` explicitly, since ``report_path`` is
    not yet a real file at call time on that sub-path. Only stamped when
    provided, so callers that do not yet compute it keep a byte-identical
    receipt shape.

    ``role`` / ``receipt_kind``: receipt-quality track — dispatch identity
    propagated from ``dispatch_metadata`` (via
    ``dispatch_identity.resolve_dispatch_role``) by the caller. ``role`` is
    stamped unconditionally (like ``status``): ``None`` when the caller could
    not resolve a real role, so the ledger records "identity was not
    resolvable" instead of silently omitting the field. ``receipt_kind`` is
    REQUIRED as of PR-3 (emit-time lint warn -> raise): it must be a member
    of the ``dispatch_identity.RECEIPT_KINDS`` closed set, stamped from the
    emitter's own knowledge; a missing or out-of-vocab value raises
    ValueError before anything is written.

    ``session_id``: receipt-quality PR-B1 — when the caller-supplied
    ``token_usage`` is empty or explicitly marked ``unavailable`` (the
    subscription/harness lanes have no live usage API) and ``provider`` is a
    member of ``token_harvest.CLAUDE_HARNESS_PROVIDERS`` (claude,
    deepseek-harness, glm-harness — every lane that runs through the Claude
    Code harness), the local Claude Code session transcript
    (``~/.claude/projects/*/<session_id>.jsonl``) is harvested via
    ``token_harvest.harvest_session_tokens`` and used instead. Fail-open: any
    harvest problem (no session_id, no transcript, kimi/other providers)
    leaves ``token_usage`` exactly as the caller supplied it.

    ``warnings``: ADR-035 §6.1 — raw ``{code, severity, message}`` entries.
    Classified (side-effect-free) via ``classify_receipt_v2_warnings``
    before the receipt is validated, then committed (open-items promotion,
    counter increment) via ``commit_receipt_v2_fields`` only once the
    append primitive confirms the receipt will actually be written
    (fix-r1) — exactly like Path 2 (``append_receipt_payload``). Only
    stamped when provided.

    ``tool_call_count`` / ``tool_call_failures`` / ``tool_call_retries``:
    receipt-quality PR-B2 — PreToolUse-hook signal counts for this dispatch,
    aggregated by the caller via ``toolcall_signals.aggregate_toolcall_
    signals``. Only stamped when provided (None omits the field — no signal
    log means no observation, not "confirmed zero calls").

    ``failure_reason`` / ``failure_class``: OI-866 failure classification —
    stamped when the dispatch failed so the receipt carries a distinguishable
    reason instead of a silent "(no error captured)" log line. Conditionally
    stamped (omitted on success).

    ``delivery_state``: dispatch 20260816-p10b-provider-observability — one of
    ``session_ready`` | ``submit_failed`` | ``deliver_failed`` |
    ``delivery_refused``, making delivery and reporting separately observable
    on the receipt (mirrors the tmux lane's sentinels). Conditionally stamped
    (None omits) so lanes that do not yet compute it keep a byte-identical
    receipt shape.

    ``permission_posture`` / ``permission_profile`` / ``permission_allow_
    pattern_count``: OI-864 — the ACTUAL spawn-time permission posture
    (``"blanket-skip"`` | ``"scoped-allowlist"`` | ``"attached-interactive"``),
    classified by the caller from the real launch flags/argv (see
    ``worker_permissions.classify_permission_posture``), never re-derived from
    env vars in this function. ``permission_profile`` / ``_allow_pattern_
    count`` are only meaningful for ``"scoped-allowlist"``. Conditionally
    stamped (None omits).

    Raises:
        ValueError: provider field doesn't match required pattern, or
            receipt_kind missing / outside the closed set (PR-3 lint raise)
        RuntimeError: write failed
    """
    _validate_provider(provider)

    # dispatch-20260802-model-ssot-en-ketenlink: model identity is normalized to
    # the canonical wave7_models.yaml key here, at the receipt boundary — the
    # same model can no longer land in the ledger under several spellings
    # (deepseek/deepseek-v4-pro vs deepseek-v4-pro, kimi-code/k3 vs kimi-k3,
    # claude-sonnet-5 vs sonnet-5). Unmapped strings pass through unchanged and
    # are caught by the fail-closed model validator downstream.
    try:
        from providers.model_normalizer import normalize_model_name  # noqa: PLC0415
        model = normalize_model_name(model)
    except Exception:  # noqa: BLE001 — a normalizer failure must never block receipt emission
        logger.debug("emit_dispatch_receipt: model normalization failed dispatch=%s", dispatch_id, exc_info=True)

    # Chain-link fallback: the caller may not know the fields (the tmux worker
    # path writes its own receipt via append_receipt_payload and inherits them
    # from the door's env); the door exports them so every lane lands the same
    # value. Explicit kwargs win over env.
    parent_dispatch = parent_dispatch or os.environ.get("VNX_PARENT_DISPATCH") or None
    task_class = task_class or os.environ.get("VNX_TASK_CLASS") or None
    tier_from = tier_from or os.environ.get("VNX_TIER_FROM") or None
    tier_to = tier_to or os.environ.get("VNX_TIER_TO") or None

    # Receipt-quality PR-B1: backfill token_usage for the claude-harness lanes
    # from the local Claude Code session transcript when the caller has nothing
    # usable (no live usage API on the subscription lane). Only ever tightens
    # the data — a caller-supplied real token_usage is never overwritten, and
    # any harvest failure (no session_id, no transcript, non-harness providers)
    # leaves token_usage exactly as passed in.
    if provider in CLAUDE_HARNESS_PROVIDERS and session_id and (not token_usage or token_usage.get("unavailable")):
        try:
            from token_harvest import harvest_session_tokens  # noqa: PLC0415
            harvested = harvest_session_tokens(session_id)
            if harvested and not harvested.get("unavailable"):
                token_usage = harvested
        except Exception as exc:  # noqa: BLE001 — harvesting must never break receipt emission
            logger.debug(
                "emit_dispatch_receipt: token harvest failed for dispatch=%s session_id=%s: %s",
                dispatch_id, session_id, exc,
            )

    # Receipt-quality PR-B0: the v2 receipt shape is codified in
    # receipt_schema.ReceiptV2 — the contract validates receipt_kind
    # (receipt-quality PR-3 closed-set lint, warn -> raise: hard-fail before
    # any write), normalizes duration_seconds, stamps schema_version /
    # event_type=task_complete (ADR-035 §3.2.1/§7.1 r2 BLOCKING-1: never
    # keyed on status) / timestamp, and serializes byte-compatibly with the
    # pre-PR-B0 literal (same field order, same conditional-stamp guards).
    # role stays stamped unconditionally (receipt-quality PR-1): None marks
    # "identity was not resolvable" vs a pre-feature absent field.
    receipt = ReceiptV2(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        provider=provider,
        model=model,
        status=status,
        completion_pct=completion_pct,
        risk=risk,
        findings=findings,
        duration_seconds=duration_seconds,
        token_usage=token_usage,
        cost_usd=cost_usd,
        pr_id=pr_id,
        role=role,
        receipt_kind=receipt_kind,
        report_path=report_path,
        events_path=events_path,
        permission_enforcement=permission_enforcement,
        mandate_id=mandate_id,
        final_prompt_path=final_prompt_path,
        final_prompt_sha256=final_prompt_sha256,
        injection_reconstructs=injection_reconstructs,
        verification=verification,
        warnings=warnings,
        tool_call_count=tool_call_count,
        tool_call_failures=tool_call_failures,
        tool_call_retries=tool_call_retries,
        deadline_seconds=deadline_seconds,
        failure_reason=failure_reason,
        failure_class=failure_class,
        delivery_state=delivery_state,
        role_applied=role_applied,
        role_tier=role_tier,
        role_not_applied_reason=role_not_applied_reason,
        role_source_path=role_source_path,
        parent_dispatch=parent_dispatch,
        task_class=task_class,
        tier_from=tier_from,
        tier_to=tier_to,
        permission_posture=permission_posture,
        permission_profile=permission_profile,
        permission_allow_pattern_count=permission_allow_pattern_count,
    ).to_dict()

    receipt_path = Path(state_dir) / "t0_receipts.ndjson"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        # ADR-035 §9 PR-4 (fix-r1): pure classification of warnings[] (no
        # side effects) before the shared validator sees it — the same
        # classify step append_receipt_payload (Path 2) calls. The matching
        # side-effect commit (open-items promotion, counter increment) is
        # deferred to commit_receipt_v2_fields, passed below as
        # pre_write_hook so it only fires once _write_receipt_under_lock
        # confirms this receipt is not a duplicate and will actually be
        # written.
        classify_receipt_v2_warnings(receipt)
        event_name = _validate_receipt(receipt)
        idempotency_key = _compute_idempotency_key(receipt, event_name)
        cache_path = _cache_file_for(receipt_path)
        _write_receipt_under_lock(
            receipt,
            receipt_path,
            cache_path,
            idempotency_key,
            _RECEIPT_CACHE_WINDOW_SECONDS,
            pre_write_hook=commit_receipt_v2_fields,
        )
    except AppendReceiptError as exc:
        raise RuntimeError(
            f"governance_emit: receipt write failed for dispatch={dispatch_id}: {exc}"
        ) from exc

    logger.info(
        "governance_emit: receipt written dispatch=%s provider=%s status=%s",
        dispatch_id, provider, status,
    )
    return receipt_path


def _validate_report_via_shell(report_path: Path, dispatch_id: str) -> None:
    """Shell fallback: invoke verify_report_schema.sh when Python jsonschema unavailable."""
    import subprocess

    script = Path(__file__).resolve().parent.parent / "guardrails" / "verify_report_schema.sh"
    if not script.exists():
        logger.debug("governance_emit: shell validator %s not found, skipping", script)
        return
    try:
        result = subprocess.run(
            ["bash", str(script), str(report_path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            msg = (result.stdout or result.stderr or "unknown error").strip()
            if os.environ.get("VNX_SCHEMA_STRICT") == "1":
                raise ValueError(f"schema validation failed (shell): {msg}")
            logger.warning(
                "governance_emit: schema violation via shell (shadow-mode) dispatch=%s: %s",
                dispatch_id, msg,
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("governance_emit: shell validator error dispatch=%s: %s", dispatch_id, exc)


def _validate_report_frontmatter(content: str, dispatch_id: str, report_path: Optional[Path] = None) -> None:
    """Validate unified-report frontmatter via UnifiedReportValidator (PR-D5-E/F).

    Uses Python jsonschema when available (UnifiedReportValidator class), falls
    back to shell wrapper (verify_report_schema.sh) when jsonschema is missing.
    Shadow-mode by default (log violations). Raises only when VNX_SCHEMA_STRICT=1.
    """
    try:
        from unified_report_schema import UnifiedReportValidator, SchemaViolation
    except ImportError:
        if report_path is not None:
            _validate_report_via_shell(report_path, dispatch_id)
        else:
            logger.debug("governance_emit: unified_report_schema not available, skipping validation")
        return

    validator = UnifiedReportValidator()
    result = validator.validate(content)
    if not result.valid:
        violation_msg = result.errors[0] if result.errors else "unknown schema violation"
        if os.environ.get("VNX_SCHEMA_STRICT") == "1":
            raise SchemaViolation(violation_msg)
        logger.warning(
            "governance_emit: schema violation (shadow-mode) dispatch=%s: %s",
            dispatch_id, violation_msg,
        )


def emit_unified_report(
    dispatch_id: str,
    terminal_id: str,
    provider: str,
    instruction: str,
    response_text: str,
    findings: List[Dict[str, Any]],
    duration_seconds: float,
    data_dir: Path,
    *,
    frontmatter: Optional[Dict[str, Any]] = None,
    body_override: Optional[str] = None,
    overwrite: bool = False,
    preserve_partial: bool = False,
) -> Path:
    """Atomic write to unified_reports/<dispatch_id>.md. Returns path.

    Idempotent: returns the existing path without modifying it when the report
    already exists (worker may have written a richer report).

    When *body_override* is provided, that exact markdown string is written as
    the report body instead of the generic ## Response wrapper.  The govern()
    function uses this to write the final contract body before emit so the body
    is always finalized before the file is created (idempotency line 198).

    When *overwrite* is True, force-writes the file even when it already exists.
    govern() passes overwrite=True for synthesized/violated bodies to replace
    stale placeholder files that would otherwise block idempotent early-return.

    When *preserve_partial* is True (failure/timeout emitters) and the existing
    report fails the report-body contract, the partial body is preserved under
    ``<dispatch_id>.partial.md`` and the fresh structured report is written in its
    place. OI-903: a worker SIGTERM'd mid-report leaves a partial file that would
    otherwise block the idempotent early-return AND satisfy nothing — the
    preserved sidecar makes the partial output retrievable while the canonical
    report stays contract-compliant. An existing report that PASSES the contract
    is never touched (idempotent early-return still applies).

    When *frontmatter* is provided, prepends a YAML frontmatter block and
    validates against unified_report_v1 schema.  Default is shadow-mode (log
    violations, do not raise).  Set VNX_SCHEMA_STRICT=1 to raise on violation.

    Raises:
        RuntimeError: write failed
    """
    reports_dir = Path(data_dir) / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / f"{dispatch_id}.md"
    if report_path.exists() and not overwrite:
        if preserve_partial:
            try:
                existing = report_path.read_text(encoding="utf-8", errors="replace")
            except OSError as _read_exc:
                logger.warning(
                    "governance_emit: could not read existing report %s for partial check (%s); "
                    "returning as-is",
                    report_path, _read_exc,
                )
                return report_path
            if not validate_body(existing).valid:
                # Killed-worker artifact: preserve the partial output under a
                # .partial.md sidecar so the work is retrievable, then let the
                # fresh structured report (written below) take the canonical path.
                partial_path = reports_dir / f"{dispatch_id}.partial.md"
                try:
                    os.replace(report_path, partial_path)
                    logger.info(
                        "governance_emit: preserved partial report dispatch=%s at %s",
                        dispatch_id, partial_path,
                    )
                except OSError as _mv_exc:
                    logger.warning(
                        "governance_emit: could not preserve partial report %s as %s (%s); "
                        "returning as-is",
                        report_path, partial_path, _mv_exc,
                    )
                    return report_path
            else:
                return report_path
        else:
            return report_path

    if body_override is not None:
        body = body_override
    else:
        if findings:
            findings_lines = "\n".join(
                f"- [{f.get('severity', 'info').upper()}] {f.get('message', str(f))}"
                for f in findings
            )
        else:
            findings_lines = "None"

        body = (
            f"# Dispatch {dispatch_id}\n\n"
            f"- Provider: {provider}\n"
            f"- Terminal: {terminal_id}\n"
            f"- Duration: {duration_seconds:.1f}s\n\n"
            f"## Instruction\n\n{instruction or '(not captured)'}\n\n"
            f"## Response\n\n{response_text or '(no response captured)'}\n\n"
            f"## Findings\n\n{findings_lines}\n"
        )

    if frontmatter:
        import yaml
        frontmatter_yaml = yaml.dump(
            frontmatter, default_flow_style=False, sort_keys=False,
            allow_unicode=True,
        )
        content = f"---\n{frontmatter_yaml}---\n\n{body}"
        _validate_report_frontmatter(content, dispatch_id, report_path=report_path)
    else:
        content = body

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
