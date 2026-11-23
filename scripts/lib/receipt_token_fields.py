"""receipt_token_fields.py: the normalized token shape of a dispatch receipt (ADR-071).

``token_usage_normalized`` holds the five ``token_harvest.LEDGER_TOKEN_FIELDS``: the booked
ledger record's own ``tokens``, else the lane's usage through ``harness_run_cost._cli_tokens``
(with ``split_reason`` when ``input_uncached`` stays unknown), five ``None`` when the lane
reported nothing. ``start_context_tokens`` is the context a claude harness builder started with.
``reasoning`` tokens are absent on purpose: no lane reports them today (``codex_spawn`` carries none).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from token_harvest import LEDGER_TOKEN_FIELDS, harvest_session_messages

logger = logging.getLogger(__name__)


def normalized_tokens(
    token_usage: Optional[Mapping[str, Any]], provider: str, ledger_record: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """The ``token_usage_normalized`` value of one receipt (the record's own tokens win, never recomputed)."""
    if ledger_record is not None:
        tokens = ledger_record.get("tokens")
        return {name: tokens.get(name) if isinstance(tokens, Mapping) else None for name in LEDGER_TOKEN_FIELDS}
    from harness_run_cost import _cli_tokens  # local import: harness_run_cost pulls a wide module graph

    tokens, split_reason = _cli_tokens(token_usage, provider)
    normalized: Dict[str, Any] = {name: tokens.get(name) for name in LEDGER_TOKEN_FIELDS}
    if split_reason is not None:
        normalized["split_reason"] = split_reason
    return normalized


def _timestamp(raw: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def start_context_tokens(session_id: Optional[str], *, claude_projects_dir: Optional[Path] = None) -> Optional[int]:
    """Uncached input, cache read and both cache writes of the transcript's earliest assistant message.

    ``None`` when there is no transcript, no message has a timestamp to rank on, or any of the four
    parts is unknown: a partial sum would read as a smaller context than the build started with.
    """
    try:
        messages = harvest_session_messages(session_id, claude_projects_dir=claude_projects_dir)
    except Exception:  # vnx-silent-except: a transcript read must never break receipt emission (logged at debug)
        logger.debug("receipt_token_fields: transcript read failed session=%s", session_id, exc_info=True)
        return None
    ranked = [(stamp, m) for m in messages or [] if (stamp := _timestamp(m.timestamp)) is not None]
    if not ranked:
        return None
    parts = [min(ranked, key=lambda pair: pair[0])[1].tokens.get(name) for name in LEDGER_TOKEN_FIELDS if name != "output"]
    return None if any(part is None for part in parts) else sum(parts)


def receipt_token_fields(
    *, provider: str, role: Optional[str], session_id: Optional[str],
    token_usage: Optional[Mapping[str, Any]], ledger_record: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """The ``emit_dispatch_receipt`` keywords this module owns, ready to splat.

    ``start_context_tokens`` is a candidate only for a claude harness builder run
    (``harness_run_cost.writes_builder_run``); the receipt omits it when its value is ``None``.
    """
    from harness_run_cost import writes_builder_run  # local import, see normalized_tokens

    try:
        normalized = normalized_tokens(token_usage, provider, ledger_record)
    except Exception as exc:  # vnx-silent-except: normalizing must never withhold the receipt; the failure is named on the field
        logger.warning("receipt_token_fields: normalization failed provider=%s: %s: %s", provider, type(exc).__name__, exc)
        normalized = {**dict.fromkeys(LEDGER_TOKEN_FIELDS), "split_reason": f"normalization_failed:{type(exc).__name__}"}
    fields: Dict[str, Any] = {"token_usage_normalized": normalized}
    if writes_builder_run(provider, role):
        fields["start_context_tokens"] = start_context_tokens(session_id)
    return fields


__all__ = ["normalized_tokens", "receipt_token_fields", "start_context_tokens"]
