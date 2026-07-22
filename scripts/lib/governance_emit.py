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
from datetime import datetime, timezone
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
) -> Path:
    """Atomic-append to t0_receipts.ndjson via the shared append primitive
    (ADR-035 §7.1) — same lock file, hash-chain stamping, and validator Path 2
    uses.

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

    ``warnings``: ADR-035 §6.1 — raw ``{code, severity, message}`` entries.
    Classified (side-effect-free) via ``classify_receipt_v2_warnings``
    before the receipt is validated, then committed (open-items promotion,
    counter increment) via ``commit_receipt_v2_fields`` only once the
    append primitive confirms the receipt will actually be written
    (fix-r1) — exactly like Path 2 (``append_receipt_payload``). Only
    stamped when provided.

    Raises:
        ValueError: provider field doesn't match required pattern
        RuntimeError: write failed
    """
    _validate_provider(provider)

    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    recorded_ts = now_ts

    receipt: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "terminal_id": terminal_id,
        "provider": provider,
        "model": model,
        "status": status,
        # ADR-035 §3.2.1/§7.1 (r2 BLOCKING-1): stamped unconditionally, never
        # keyed on status — task_complete marks "reached a terminal outcome",
        # status carries which outcome (matches outcome_signals.py's
        # task_complete-plus-status convention). This is the one field Path 1
        # must add to survive contact with the shared validator below, which
        # it has never faced before this PR.
        "event_type": "task_complete",
        "completion_pct": completion_pct,
        "risk": risk,
        "duration_seconds": round(float(duration_seconds), 3),
        "token_usage": token_usage,
        "cost_usd": cost_usd,
        "findings": findings,
        "pr_id": pr_id,
        "report_path": report_path,
        "events_path": events_path,
        "timestamp": now_ts,
        "recorded_at": recorded_ts,
    }
    if permission_enforcement:
        receipt["permission_enforcement"] = permission_enforcement
    if mandate_id:
        receipt["mandate_id"] = mandate_id
    if final_prompt_path is not None:
        receipt["final_prompt_path"] = final_prompt_path
    if final_prompt_sha256 is not None:
        receipt["final_prompt_sha256"] = final_prompt_sha256
    if injection_reconstructs is not None:
        receipt["injection_reconstructs"] = injection_reconstructs
    if verification is not None:
        receipt["verification"] = verification
    if warnings is not None:
        receipt["warnings"] = warnings

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
