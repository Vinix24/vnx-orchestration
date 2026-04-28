"""Ghost receipt detection and gate-event stream routing.

A "ghost receipt" is a receipt whose dispatch_id resolved to the sentinel
value "unknown" after all enrichment attempts (env-var lookup, metadata
extraction, etc.).  These arise from headless gate runners (gemini_review,
codex_gate) that are invoked without a VNX_CURRENT_DISPATCH_ID in scope.

Instead of polluting t0_receipts.ndjson with untraceable events, ghost gate
receipts are redirected to a separate gate_events.ndjson stream where they
can be correlated by gate + PR number rather than dispatch_id.

Usage in append_receipt.py:
    from ghost_receipt_filter import should_route_to_gate_stream, gate_events_file

    if receipts_file is None and should_route_to_gate_stream(receipt):
        receipts_file = str(gate_events_file(state_dir))
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

GATE_EVENTS_FILENAME = "gate_events.ndjson"

_GHOST_SENTINEL_VALUES = {"unknown", "none", "null", ""}

_KNOWN_GATE_NAMES = {
    "gemini_review",
    "codex_gate",
    "claude_github_optional",
    "claude_github_review",
    "pre_merge_gate",
    "review_gate",
}


def is_ghost_dispatch_id(dispatch_id: Optional[str]) -> bool:
    """True when dispatch_id is absent or a sentinel non-value."""
    if dispatch_id is None:
        return True
    return str(dispatch_id).strip().lower() in _GHOST_SENTINEL_VALUES


def is_gate_event(receipt: Dict[str, Any]) -> bool:
    """True when the receipt originated from a headless gate runner.

    Detects by:
    - A non-empty, non-unknown 'gate' field
    - Terminal set to 'HEADLESS' or prefixed with 'HEADLESS'
    - report_file containing the 'HEADLESS' marker
    """
    gate = str(receipt.get("gate") or "").strip().lower()
    if gate and gate not in _GHOST_SENTINEL_VALUES:
        return True

    terminal = str(receipt.get("terminal") or "").strip().upper()
    if terminal.startswith("HEADLESS"):
        return True

    report_file = str(receipt.get("report_file") or receipt.get("report_path") or "")
    if "HEADLESS" in report_file.upper():
        return True

    return False


def should_route_to_gate_stream(receipt: Dict[str, Any]) -> bool:
    """True when this receipt should go to gate_events.ndjson instead of t0_receipts.ndjson.

    Routes when: dispatch_id is a ghost value AND the receipt is a gate event.
    Receipts with a valid dispatch_id always stay in the main stream regardless
    of whether they are gate events.

    For review_gate_request specifically: legacy headless runners that omit
    dispatch_id produce ghost receipts and are redirected here. Callers that
    supply a real dispatch_id (PR-2 fix) bypass this and land in t0_receipts.ndjson.
    """
    dispatch_id = receipt.get("dispatch_id")
    if not is_ghost_dispatch_id(dispatch_id):
        return False
    return is_gate_event(receipt)


def gate_events_file(state_dir: Path) -> Path:
    """Return the canonical gate_events.ndjson path for the given state directory."""
    return state_dir / GATE_EVENTS_FILENAME


__all__ = [
    "GATE_EVENTS_FILENAME",
    "gate_events_file",
    "is_gate_event",
    "is_ghost_dispatch_id",
    "should_route_to_gate_stream",
]
