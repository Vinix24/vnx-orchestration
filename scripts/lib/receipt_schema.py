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

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Sibling scripts/lib modules must resolve even when a caller imports this
# module by path without scripts/lib already on sys.path (mirrors
# governance_emit.py / dispatch_identity.py).
_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from dispatch_identity import validate_receipt_kind  # noqa: E402
from ledger_schema_version import CURRENT_SCHEMA_VERSION  # noqa: E402


def _utc_now_iso() -> str:
    """Ledger timestamp format used by both emit paths (``%Y-%m-%dT%H:%M:%SZ``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# OI-817 (kimi_gate nitpick on PR-B0): ``schema_version`` / ``event_type`` are
# part of the v2 contract's identity (ADR-035 §3.2.1/§7.1 r2 — never keyed on
# status; always ``task_complete``), not caller data. ``ReceiptV2.__post_init__``
# forces these constants so a future emit site cannot bypass them by stamping a
# non-v2 receipt or a different event_type.
# Sourced from ledger_schema_version.CURRENT_SCHEMA_VERSION so the receipt
# shape's stamp and the reader's migration ladder share ONE version constant
# (bump there, not here — see ledger_schema_version.py).
RECEIPT_V2_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
RECEIPT_V2_EVENT_TYPE = "task_complete"

# ReceiptV2 fields stamped only when not None, in ledger insertion order (the
# writer uses sort_keys=False, so this order is part of the byte shape).
# failure_reason/failure_class (OI-866) are omitted on success so pre-feature
# receipts stay byte-identical; a non-retry receipt omits the chain-link
# fields (parent_dispatch, task_class, tier_from, tier_to) entirely.
_STAMPED_WHEN_SET = (
    "final_prompt_path", "final_prompt_sha256", "injection_reconstructs", "verification", "warnings",
    "tool_call_count", "tool_call_failures", "tool_call_retries", "deadline_seconds",
    "failure_reason", "failure_class", "delivery_state",
    "role_applied", "role_tier", "role_not_applied_reason", "role_source_path",
    "parent_dispatch", "task_class", "tier_from", "tier_to",
    "permission_posture", "permission_profile", "permission_allow_pattern_count",
    "model_resolved", "cost_ledger_run_ids",
)


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
    # Price provenance (ADR-040): ``<model>@<version-date>`` for a priced run,
    # ``unavailable:no_price:<model>`` for a missing price, None for a lane that
    # never priced (subscription, no billable usage). Conditionally stamped —
    # omitted when falsy so pre-feature receipts stay byte-identical.
    cost_source: Optional[str] = None
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
    # dispatch 20260816-p10b-provider-observability: delivery-state field.
    # Orthogonal to failure_class — one of the closed set
    # session_ready | submit_failed | deliver_failed | delivery_refused,
    # mirroring the tmux lane's deliver_failed/submit_failed/session_ready
    # sentinels plus a provider-lane-only delivery_refused for pre-flight
    # refusals. Conditionally stamped (None omits) so lanes that do not yet
    # compute it keep a byte-identical receipt shape.
    delivery_state: Optional[str] = None
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
    # OI-864: the ACTUAL spawn-time permission posture, classified from the
    # real launch flags (worker_permissions.classify_permission_posture) —
    # never from re-reading VNX_ENFORCE_WORKER_PERMISSIONS/VNX_WORKER_SCOPED.
    # One of "blanket-skip" | "scoped-allowlist" | "attached-interactive".
    # permission_profile / permission_allow_pattern_count are only meaningful
    # (and only stamped) for "scoped-allowlist". Conditionally stamped (None
    # omits), same as the other optional fields.
    permission_posture: Optional[str] = None
    permission_profile: Optional[str] = None
    permission_allow_pattern_count: Optional[int] = None
    # The model the harness reported it actually ran (init event), normalized to
    # the registry key. ``model`` stays the REQUESTED value (often an alias such
    # as "sonnet"); this field is what the alias resolved to. None omits the
    # field: no init event means no observation, never a guess.
    model_resolved: Optional[str] = None
    # ADR-043 (K2a-2): the run_id of every cost-ledger line this dispatch wrote,
    # one per attempt. The receipt's cost_usd/cost_source come from those lines.
    # None omits the field: the lane does not write the ledger yet.
    cost_ledger_run_ids: Optional[List[str]] = None

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
        # truthiness for cost_source / permission_enforcement / mandate_id,
        # is-not-None for the rest.
        for key in ("cost_source", "permission_enforcement", "mandate_id"):
            value = getattr(self, key)
            if value:
                receipt[key] = value
        for key in _STAMPED_WHEN_SET:
            value = getattr(self, key)
            if value is not None:
                receipt[key] = list(value) if key == "cost_ledger_run_ids" else value
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
    # Default "failed" preserves the pre-PR-B0 byte-identical shape (and the
    # honest no-evidence outcome). The caller (dispatch_govern.ensure_receipt)
    # derives the real status from the worker's DECLARED outcome: "done" for an
    # authored report that does not declare failure, "failed" otherwise — for a
    # declared-failure report or when no valid report exists (OI-1202).
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
    # OI-864: see ReceiptV2 for semantics — same fields, same classification
    # source (worker_permissions.classify_permission_posture from the actual
    # spawn flags), stamped on the lane-synthesized fallback receipt too.
    permission_posture: Optional[str] = None
    permission_profile: Optional[str] = None
    permission_allow_pattern_count: Optional[int] = None

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
        if self.permission_posture is not None:
            receipt["permission_posture"] = self.permission_posture
        if self.permission_profile is not None:
            receipt["permission_profile"] = self.permission_profile
        if self.permission_allow_pattern_count is not None:
            receipt["permission_allow_pattern_count"] = self.permission_allow_pattern_count
        return receipt


# ---------------------------------------------------------------------------
# ADR-045 (audit-spoor-kern K-1): the closed dispatch-lifecycle vocabulary.
#
# A dispatch's ledger life is three line types and nothing else. The list is
# closed on purpose: a reader that meets a fourth type cannot say whether it is
# an outcome, and the whole point of the track is that the ledger answers "did
# this dispatch end, and how" without guessing. The required-field contract
# below is the write-side half — a line missing a field is refused with the
# name of the missing field, never written half-formed.
#
# What each type is NOT is as binding as what it is (ADR-045 §2):
#   - ``dispatch_started`` happens before the work, so it is never an outcome;
#   - ``dispatch_finished`` is the ONE end of one attempt and IS an outcome;
#   - ``report_ingested`` says the report arrived, never that the dispatch
#     ended — a report is not a second end line.
# ---------------------------------------------------------------------------
DISPATCH_STARTED_EVENT_TYPE = "dispatch_started"
DISPATCH_FINISHED_EVENT_TYPE = "dispatch_finished"
REPORT_INGESTED_EVENT_TYPE = "report_ingested"

LIFECYCLE_EVENT_TYPES: Tuple[str, ...] = (
    DISPATCH_STARTED_EVENT_TYPE, DISPATCH_FINISHED_EVENT_TYPE, REPORT_INGESTED_EVENT_TYPE)

# ADR-045 §2: ``end_state`` is a closed set. ``abandoned`` is a ledger fact and
# projects to a ``timed_out`` row (ADR-045 §4), never to a new DB state.
LIFECYCLE_END_STATES = frozenset({"completed", "failed", "timed_out", "abandoned"})

# ADR-045 §9: the mode is fixed per dispatch at hand-off and never re-read from
# the live setting, so a dispatch that started in v3 ends in v3.
LIFECYCLE_MODES = frozenset({"v3", "legacy"})

# ADR-045 §8: an end line carries ``head_sha`` when the work was pushed, and
# otherwise an explicit ``no_push:<reason>``. Absence of both is not "no push".
NO_PUSH_PREFIX = "no_push:"

# The required fields per type (ADR-045 §2 and §8). ``schema_version`` and
# ``writer_sha`` are stamped by the append layer, not supplied by the caller;
# ``status`` on a finished line is derived from ``end_state`` (see
# event_outcome_semantics.end_state_status), never supplied.
# (A finished line's ``head_sha``/``no_push`` pair is checked below, not here.)
LIFECYCLE_REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    DISPATCH_STARTED_EVENT_TYPE: (
        "project_id", "dispatch_id", "attempt", "role", "base_sha", "spec_sha256",
        "requested_by", "lifecycle_mode", "door_pid", "door_started_at", "writer_sha"),
    DISPATCH_FINISHED_EVENT_TYPE: (
        "project_id", "dispatch_id", "attempt", "end_state", "writer_sha"),
    REPORT_INGESTED_EVENT_TYPE: ("project_id", "dispatch_id", "attempt"),
}


class LifecycleReceiptError(ValueError):
    """A lifecycle line that the write side must refuse, with a named reason.

    ``code`` is the machine-readable refusal (what the receipt processor and a
    reviewer grep for), ``field`` names the field at fault. The append layer
    translates this into an ``AppendReceiptError`` carrying the same code, so
    the refusal is visible in the ledger's own error vocabulary instead of a
    bare ValueError.
    """

    def __init__(self, code: str, event_type: str, field: str, detail: str):
        self.code, self.event_type, self.field, self.detail = code, event_type, field, detail
        super().__init__(f"{event_type} refused: {detail} (field {field!r})")


def is_lifecycle_event_type(value: Any) -> bool:
    """True for one of the three ADR-045 lifecycle line types."""
    return str(value or "").strip() in LIFECYCLE_EVENT_TYPES


def _is_present(value: Any) -> bool:
    """Fail-closed presence: absent, None and whitespace-only all read as missing.

    True is not "truthy": ``end_pending: false`` must not pass for a boolean
    marker, which is why the boolean case is handled by its own exact check.
    """
    return value is not None and not (isinstance(value, str) and not value.strip())


def validate_lifecycle_receipt(receipt: Dict[str, Any]) -> None:
    """Enforce ADR-045's required-field contract for the three lifecycle types.

    No-op for every other ``event_type`` (the vocabulary is additive: existing
    types keep their current contract). Raises ``LifecycleReceiptError`` with a
    named ``code`` and ``field``; the append layer turns it into a refusal.
    """
    event_type = str(receipt.get("event_type") or "").strip()
    if event_type not in LIFECYCLE_EVENT_TYPES:
        return

    for field in LIFECYCLE_REQUIRED_FIELDS[event_type]:
        if not _is_present(receipt.get(field)):
            raise LifecycleReceiptError(
                "missing_lifecycle_field", event_type, field,
                f"required field {field!r} is absent or empty")

    attempt = receipt.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise LifecycleReceiptError("invalid_lifecycle_attempt", event_type, "attempt",
                                    f"attempt must be a positive integer, got {attempt!r}")

    if event_type == DISPATCH_STARTED_EVENT_TYPE:
        mode = str(receipt.get("lifecycle_mode") or "").strip()
        if mode not in LIFECYCLE_MODES:
            raise LifecycleReceiptError(
                "invalid_lifecycle_mode", event_type, "lifecycle_mode",
                f"lifecycle_mode must be one of {sorted(LIFECYCLE_MODES)}, got {mode!r}")

    if event_type == DISPATCH_FINISHED_EVENT_TYPE:
        end_state = str(receipt.get("end_state") or "").strip()
        if end_state not in LIFECYCLE_END_STATES:
            raise LifecycleReceiptError(
                "invalid_lifecycle_end_state", event_type, "end_state",
                f"end_state must be one of {sorted(LIFECYCLE_END_STATES)}, got {end_state!r}")
        if not _is_present(receipt.get("head_sha")):
            no_push = receipt.get("no_push")
            if not _is_present(no_push):
                raise LifecycleReceiptError(
                    "missing_lifecycle_push_marker", event_type, "head_sha",
                    "an end line needs head_sha or an explicit no_push:<reason>")
            if not str(no_push).startswith(NO_PUSH_PREFIX) or not str(no_push)[len(NO_PUSH_PREFIX):].strip():
                raise LifecycleReceiptError(
                    "invalid_lifecycle_no_push", event_type, "no_push",
                    f"no_push must read {NO_PUSH_PREFIX}<reason>, got {no_push!r}")

    if event_type == REPORT_INGESTED_EVENT_TYPE:
        # Exactly one link: the end line is either already there
        # (end_outcome_id) or explicitly not yet (end_pending: true). A report
        # line is never rewritten to add the link later (ADR-045 §2).
        linked = _is_present(receipt.get("end_outcome_id"))
        pending = receipt.get("end_pending") is True
        if not linked and not pending:
            raise LifecycleReceiptError(
                "missing_lifecycle_end_link", event_type, "end_pending",
                "a report line needs end_outcome_id or end_pending: true")


def lifecycle_end_key(receipt: Dict[str, Any]) -> str:
    """The identity of one attempt: sha256 over (project_id, dispatch_id, attempt).

    ADR-045 §3 makes this triple the key of the start line, the end line and
    the intent. It is a hash, not a dotted string, for the same reason
    ``outcome_identity.compute_outcome_id`` is: the value is only ever compared
    and stored, never parsed. ``attempt`` is stringified so an int from the DB
    and its JSON round-trip hash identically.
    """
    attempt = receipt.get("attempt")
    parts = (str(receipt.get("project_id") or "").strip(),
             str(receipt.get("dispatch_id") or "").strip(),
             "" if attempt is None else str(attempt).strip())
    payload = json.dumps(parts, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "ReceiptV2", "SynthesizedLaneReceipt",
    "DISPATCH_STARTED_EVENT_TYPE", "DISPATCH_FINISHED_EVENT_TYPE", "REPORT_INGESTED_EVENT_TYPE",
    "LIFECYCLE_EVENT_TYPES", "LIFECYCLE_END_STATES", "LIFECYCLE_MODES",
    "LIFECYCLE_REQUIRED_FIELDS", "NO_PUSH_PREFIX", "LifecycleReceiptError",
    "is_lifecycle_event_type", "validate_lifecycle_receipt", "lifecycle_end_key",
]
