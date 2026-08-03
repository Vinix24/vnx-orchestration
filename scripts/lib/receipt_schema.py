"""receipt_schema.py — codified receipt-shape contracts (receipt-quality PR-B0).

Two receipt shapes are written into the canonical ledger today, and until now
both were assembled as ad-hoc ``Dict`` literals at the emit sites:

  - ``ReceiptV2`` — the governance-enriched dispatch receipt
    (``schema_version: 2``) emitted by
    ``governance_emit.emit_dispatch_receipt`` (ADR-035, Path 1).
  - ``SynthesizedLaneReceipt`` — the lane-synthesized fallback completion
    receipt emitted by ``dispatch_govern.ensure_receipt`` when the worker
    never produced one (F1 guarantee).

This module codifies each shape ONCE as a typed dataclass so later
receipt-quality PRs (B1-B4) add typed fields instead of dict keys.

Hard contracts:

  - ``to_dict()`` is BYTE-COMPATIBLE with the pre-PR-B0 literal construction:
    same field insertion order (the ledger writer serializes with
    ``json.dumps(..., sort_keys=False)``), same unconditional-vs-conditional
    stamping semantics (``role``/``pr_id``/``report_path``/``events_path``/
    ``cost_usd`` are stamped even when ``None``; ``permission_enforcement``/
    ``mandate_id`` are omitted when falsy; the remaining optional fields are
    omitted only when ``None``).
  - The ``receipt_kind`` closed-set lint (receipt-quality PR-3,
    ``dispatch_identity.validate_receipt_kind``) is folded into each
    contract's ``__post_init__`` — constructing a receipt object with a
    missing/out-of-vocab kind raises ValueError before anything is written.
  - PR-B0 itself added NO new receipt fields and did not touch
    ``IDEMPOTENCY_FIELDS``. Later receipt-quality PRs (B2+) add typed,
    conditionally-stamped fields on top of this contract -- additive only,
    still never touching ``IDEMPOTENCY_FIELDS``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Sibling scripts/lib modules must resolve even when a caller imports this
# module by path without scripts/lib already on sys.path (mirrors
# governance_emit.py / dispatch_identity.py).
_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from dispatch_identity import validate_receipt_kind  # noqa: E402


def _utc_now_iso() -> str:
    """Ledger timestamp format used by both emit paths (``%Y-%m-%dT%H:%M:%SZ``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# OI-817 (kimi_gate nitpick on PR-B0): ``schema_version`` / ``event_type`` are
# part of the v2 contract's identity (ADR-035 §3.2.1/§7.1 r2 — never keyed on
# status; always ``task_complete``), not caller data. ``ReceiptV2.__post_init__``
# forces these constants so a future emit site cannot bypass them by stamping a
# non-v2 receipt or a different event_type.
RECEIPT_V2_SCHEMA_VERSION = 2
RECEIPT_V2_EVENT_TYPE = "task_complete"


@dataclass
class ReceiptV2:
    """The ADR-035 v2 dispatch receipt emitted by ``governance_emit``.

    Field set derived from the actual pre-PR-B0 emit site
    (``governance_emit.emit_dispatch_receipt``) — not from documentation.

    Unconditionally stamped (serialized even when ``None``, so the ledger
    distinguishes "unresolved" (null) from "pre-feature" (field absent)):
    ``role``, ``pr_id``, ``report_path``, ``events_path``, ``cost_usd``.

    Conditionally stamped (omitted from the serialized dict):
    ``permission_enforcement`` and ``mandate_id`` when falsy (matches the
    pre-PR-B0 truthiness guard); ``final_prompt_path``,
    ``final_prompt_sha256``, ``injection_reconstructs``, ``verification`` and
    ``warnings`` when ``None``.
    """

    dispatch_id: str
    terminal_id: str
    provider: str
    model: str
    status: str
    completion_pct: int
    risk: float
    findings: List[Dict[str, Any]]
    duration_seconds: float
    token_usage: Dict[str, int]
    receipt_kind: str
    # Unconditionally stamped, None-able.
    role: Optional[str] = None
    pr_id: Optional[str] = None
    report_path: Optional[str] = None
    events_path: Optional[str] = None
    cost_usd: Optional[float] = None
    # Contract identity — forced to the module constants in __post_init__
    # (OI-817), never caller-overridable.
    event_type: str = RECEIPT_V2_EVENT_TYPE
    schema_version: int = RECEIPT_V2_SCHEMA_VERSION
    timestamp: Optional[str] = None  # None -> stamped with now at construction
    # Conditionally stamped.
    permission_enforcement: Optional[str] = None
    mandate_id: Optional[str] = None
    final_prompt_path: Optional[str] = None
    final_prompt_sha256: Optional[str] = None
    injection_reconstructs: Optional[bool] = None
    verification: Optional[Dict[str, Any]] = None
    warnings: Optional[List[Dict[str, Any]]] = None
    # receipt-quality PR-B2: PreToolUse-hook tool-call signals, aggregated by
    # toolcall_signals.aggregate_toolcall_signals() from the per-dispatch
    # tmux-signal-dir NDJSON log. None (omitted) when no signal log exists for
    # this dispatch -- distinct from "confirmed zero tool calls".
    tool_call_count: Optional[int] = None
    tool_call_failures: Optional[int] = None
    tool_call_retries: Optional[int] = None
    # deadline-passthrough: the deadline that was in effect when this dispatch ran.
    # Stamped when known (omitted when None). When status=timeout and this field is
    # present, an operator can see whether the deadline was 900s (hardcoded default)
    # or a longer spec-staged value like 3600s. Distinct from duration_seconds which
    # records wall-clock time.
    deadline_seconds: Optional[int] = None
    # OI-866 failure classification: stamped when status != "success" (omitted on
    # success so pre-feature receipts stay byte-identical).
    # failure_class is a closed-set category (auth_rejected, empty_completion,
    # timeout, model_error, unknown). failure_reason is the human-readable detail.
    failure_reason: Optional[str] = None
    failure_class: Optional[str] = None
    # Deterministic role-applied control (dispatch-20260801-w10): stamped by the
    # provider/envelope GOVERN paths after the deterministic check that the
    # resolved role source's content actually reached the assembled final prompt.
    # role_tier is one of prompt_assembler | agents | skills | terminal | none.
    # role_applied is False (stamped) when the resolved source content is absent
    # from the prompt; role_not_applied_reason explains why. Conditionally stamped
    # (None omits), so lanes that do not yet compute the verdict keep a
    # byte-identical receipt shape.
    role_applied: Optional[bool] = None
    role_tier: Optional[str] = None
    role_not_applied_reason: Optional[str] = None
    role_source_path: Optional[str] = None
    # dispatch-20260802-model-ssot-en-ketenlink (chain-link): the receipt says
    # WHICH dispatch this one continues. parent_dispatch is the id of the
    # retried/fix-forward/escalated predecessor; tier_from/tier_to record an
    # escalation (a tier change between the predecessor and this dispatch);
    # task_class is the deterministic smart_router class of the work. Each is
    # stamped only when known (None omits — the ledger distinguishes "not a
    # retry" from "retry, tier unknown").
    parent_dispatch: Optional[str] = None
    task_class: Optional[str] = None
    tier_from: Optional[str] = None
    tier_to: Optional[str] = None

    def __post_init__(self) -> None:
        # Receipt-quality PR-3: the closed-set lint lives in the contract now
        # — an invalid kind fails construction, before any write.
        self.receipt_kind = validate_receipt_kind(self.receipt_kind)
        # Same normalization the pre-PR-B0 literal applied at build time.
        self.duration_seconds = round(float(self.duration_seconds), 3)
        # OI-817: schema_version/event_type are contract identity — force the
        # constants regardless of what a caller passed (a future emit site must
        # not be able to bypass them).
        self.schema_version = RECEIPT_V2_SCHEMA_VERSION
        self.event_type = RECEIPT_V2_EVENT_TYPE
        if self.timestamp is None:
            self.timestamp = _utc_now_iso()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the ledger dict, byte-compatible with the pre-PR-B0
        literal construction in ``governance_emit.emit_dispatch_receipt``
        (same insertion order; the writer uses ``sort_keys=False``).
        """
        receipt: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "dispatch_id": self.dispatch_id,
            "terminal_id": self.terminal_id,
            "provider": self.provider,
            "model": self.model,
            "role": self.role,
            "receipt_kind": self.receipt_kind,
            "status": self.status,
            "event_type": self.event_type,
            "completion_pct": self.completion_pct,
            "risk": self.risk,
            "duration_seconds": self.duration_seconds,
            "token_usage": self.token_usage,
            "cost_usd": self.cost_usd,
            "findings": self.findings,
            "pr_id": self.pr_id,
            "report_path": self.report_path,
            "events_path": self.events_path,
            "timestamp": self.timestamp,
        }
        # Conditional stamps — same guards as the pre-PR-B0 emit site:
        # truthiness for permission_enforcement / mandate_id, is-not-None for
        # the rest.
        if self.permission_enforcement:
            receipt["permission_enforcement"] = self.permission_enforcement
        if self.mandate_id:
            receipt["mandate_id"] = self.mandate_id
        if self.final_prompt_path is not None:
            receipt["final_prompt_path"] = self.final_prompt_path
        if self.final_prompt_sha256 is not None:
            receipt["final_prompt_sha256"] = self.final_prompt_sha256
        if self.injection_reconstructs is not None:
            receipt["injection_reconstructs"] = self.injection_reconstructs
        if self.verification is not None:
            receipt["verification"] = self.verification
        if self.warnings is not None:
            receipt["warnings"] = self.warnings
        if self.tool_call_count is not None:
            receipt["tool_call_count"] = self.tool_call_count
        if self.tool_call_failures is not None:
            receipt["tool_call_failures"] = self.tool_call_failures
        if self.tool_call_retries is not None:
            receipt["tool_call_retries"] = self.tool_call_retries
        if self.deadline_seconds is not None:
            receipt["deadline_seconds"] = self.deadline_seconds
        # OI-866: conditionally stamped — omitted on success so pre-feature
        # receipts stay byte-identical.
        if self.failure_reason is not None:
            receipt["failure_reason"] = self.failure_reason
        if self.failure_class is not None:
            receipt["failure_class"] = self.failure_class
        if self.role_applied is not None:
            receipt["role_applied"] = self.role_applied
        if self.role_tier is not None:
            receipt["role_tier"] = self.role_tier
        if self.role_not_applied_reason is not None:
            receipt["role_not_applied_reason"] = self.role_not_applied_reason
        if self.role_source_path is not None:
            receipt["role_source_path"] = self.role_source_path
        # Chain-link stamps (dispatch-20260802-model-ssot-en-ketenlink) —
        # conditional like the other optional fields: a non-retry receipt omits
        # parent_dispatch entirely.
        if self.parent_dispatch is not None:
            receipt["parent_dispatch"] = self.parent_dispatch
        if self.task_class is not None:
            receipt["task_class"] = self.task_class
        if self.tier_from is not None:
            receipt["tier_from"] = self.tier_from
        if self.tier_to is not None:
            receipt["tier_to"] = self.tier_to
        return receipt


@dataclass
class SynthesizedLaneReceipt:
    """The govern-synthesized fallback completion receipt (F1 guarantee)
    emitted by ``dispatch_govern.ensure_receipt`` when the worker never
    produced a receipt before the deadline.

    Field set derived from the actual pre-PR-B0 emit site. Distinct from
    ``ReceiptV2``: this is a ``subprocess_completion`` event with no
    ``schema_version`` stamp, dual ``terminal``/``terminal_id`` keys, and the
    ``tmux_interactive_lane_synthesized`` source marker
    ``dedup_completion_receipts`` relies on to prefer worker-authored
    receipts on readback.
    """

    dispatch_id: str
    terminal_id: str
    model: str
    lane: str
    failure_reason: Optional[str]
    contract_status: str
    permission_enforcement: str
    role: Optional[str] = None
    receipt_kind: str = "dispatch"
    event_type: str = "subprocess_completion"
    status: str = "failed"
    source: str = "tmux_interactive_lane_synthesized"
    synthesized: bool = True
    provider: str = "claude"
    sub_provider: str = "anthropic"
    timestamp: Optional[str] = None  # None -> stamped with now at construction
    # Chain-link fields (dispatch-20260802-model-ssot-en-ketenlink) — see
    # ReceiptV2 for semantics. Conditionally stamped.
    parent_dispatch: Optional[str] = None
    task_class: Optional[str] = None
    tier_from: Optional[str] = None
    tier_to: Optional[str] = None
    # Conditionally stamped (is-not-None).
    worker_permission_enforcement: Optional[str] = None
    report_path: Optional[str] = None

    def __post_init__(self) -> None:
        self.receipt_kind = validate_receipt_kind(self.receipt_kind)
        if self.timestamp is None:
            self.timestamp = _utc_now_iso()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the ledger dict, byte-compatible with the pre-PR-B0
        literal construction in ``dispatch_govern.ensure_receipt``.
        """
        receipt: Dict[str, Any] = {
            "event_type": self.event_type,
            "dispatch_id": self.dispatch_id,
            "terminal": self.terminal_id,
            "terminal_id": self.terminal_id,
            "status": self.status,
            "source": self.source,
            "synthesized": self.synthesized,
            "failure_reason": self.failure_reason,
            "contract_status": self.contract_status,
            "permission_enforcement": self.permission_enforcement,
            "timestamp": self.timestamp,
            "provider": self.provider,
            "sub_provider": self.sub_provider,
            "model": self.model,
            "lane": self.lane,
            "role": self.role,
            "receipt_kind": self.receipt_kind,
        }
        if self.worker_permission_enforcement is not None:
            receipt["worker_permission_enforcement"] = self.worker_permission_enforcement
        if self.report_path is not None:
            receipt["report_path"] = self.report_path
        # Chain-link stamps — conditional like the other optional fields.
        if self.parent_dispatch is not None:
            receipt["parent_dispatch"] = self.parent_dispatch
        if self.task_class is not None:
            receipt["task_class"] = self.task_class
        if self.tier_from is not None:
            receipt["tier_from"] = self.tier_from
        if self.tier_to is not None:
            receipt["tier_to"] = self.tier_to
        return receipt


__all__ = ["ReceiptV2", "SynthesizedLaneReceipt"]
