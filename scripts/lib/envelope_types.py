"""envelope_types.py — public types for the dispatch_envelope module family.

Leaf module: no imports from sibling envelope_* modules or from
dispatch_envelope itself (the facade imports FROM here, never the reverse).
Moved unchanged from dispatch_envelope.py as PR-1 of the dispatch-monolith-split
(dispatch-monolith-split, PR-1 of 6) — see dispatch_envelope.py's module
docstring for the split's seam order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class EnvelopeSpec:
    """Normalized dispatch parameters passed through PREPARE -> ROUTE -> EXECUTE -> GOVERN."""

    dispatch_id: str
    terminal_id: str
    provider: str
    model: str
    instruction: str
    role: Optional[str]
    pr_id: Optional[str]
    state_dir: Path
    data_dir: Path
    deadline_seconds: int = 900
    # Chain-link (dispatch-20260802-model-ssot-en-ketenlink): threaded from the
    # plan onto the receipt so the provider lane stamps the same values the
    # door computed.
    parent_dispatch: Optional[str] = None
    task_class: Optional[str] = None
    tier_from: Optional[str] = None
    tier_to: Optional[str] = None
    # OI-1137: explicit work-ref — the branch a fix-forward dispatch delivers onto,
    # so the phantom-guard can weigh the pushed branch diff when the own worktree
    # reads empty. Optional; None for a normal dispatch.
    work_ref: Optional[str] = None


@dataclass
class EnvelopeResult:
    """Outcome from a complete envelope run."""

    status: str           # "success" | "failure" | "timeout"
    returncode: int
    report_path: Optional[Path]
    receipt_path: Optional[Path]
    completion_text: str = ""
    error: Optional[str] = None


class EnvelopeGovernError(RuntimeError):
    """Raised when GOVERN cannot emit or confirm a receipt (fail-closed contract)."""


# ---------------------------------------------------------------------------
# Internal adapter result
# ---------------------------------------------------------------------------


@dataclass
class _AdapterResult:
    returncode: int
    completion_text: str
    status: str           # "success" | "failure" | "timeout"
    token_usage: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None
    timed_out: bool = False
    event_writer_failures: int = 0
    # receipt-quality PR-B1: claude session_id (from the init event), threaded
    # through to emit_dispatch_receipt so it can backfill token_usage from the
    # local transcript when the spawn itself reported none. None for adapters
    # with no session concept (e.g. codex).
    session_id: Optional[str] = None
    # Actual model the spawn resolved and executed (e.g. deepseek-harness
    # resolves "default"/"sonnet" -> "deepseek-v4-pro"). Used for cost
    # computation in _govern so the receipt's cost_usd prices the model that
    # actually ran, not a placeholder from the dispatch spec. None when the
    # adapter did not resolve a distinct model (caller falls back to spec.model).
    model: Optional[str] = None
