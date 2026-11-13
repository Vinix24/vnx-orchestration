"""receipt_token_fields.py — the normalized token shape of a dispatch receipt.

Track ``herwerk-per-model-en-reviewer`` (ADR-071). The raw ``token_usage`` of a
receipt differs per lane and stays as it is. Beside it a receipt carries:

- ``token_usage_normalized``: the five fields of ``token_harvest.LEDGER_TOKEN_FIELDS``
  (``input_uncached``, ``cache_hit``, ``cache_write_5m``, ``cache_write_1h``,
  ``output``). A run that booked a cost-ledger line carries the line's own
  ``tokens``, never a second computation. A run without one is normalized from
  its lane's ``token_usage`` through ``harness_run_cost._cli_tokens``; when that
  leaves ``input_uncached`` unknown, ``split_reason`` names why. A lane that
  reported nothing carries five ``None``, never zeros.
- ``start_context_tokens``: the size of the context a claude harness builder
  starts with, read from the first assistant message of its transcript.

``reasoning`` tokens are absent on purpose: no lane reports them today
(``codex_spawn`` carries none), so a field for them would be invented.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from token_harvest import LEDGER_TOKEN_FIELDS, MessageUsage, harvest_session_messages

logger = logging.getLogger(__name__)


def _from_ledger_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    """The record's own ``tokens``; five ``None`` when the record carries none (unknown, never zero)."""
    tokens = record.get("tokens")
    if not isinstance(tokens, Mapping):
        return dict.fromkeys(LEDGER_TOKEN_FIELDS)
    return {name: tokens.get(name) for name in LEDGER_TOKEN_FIELDS}


def normalized_tokens(
    token_usage: Optional[Mapping[str, Any]], provider: str, ledger_record: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """The ``token_usage_normalized`` value of one receipt."""
    if ledger_record is not None:
        return _from_ledger_record(ledger_record)
    from harness_run_cost import _cli_tokens  # local import: harness_run_cost pulls a wide module graph

    tokens, split_reason = _cli_tokens(token_usage, provider)
    normalized: Dict[str, Any] = {name: tokens.get(name) for name in LEDGER_TOKEN_FIELDS}
    if split_reason is not None:
        normalized["split_reason"] = split_reason
    return normalized


def _timestamp(message: MessageUsage) -> Optional[datetime]:
    if not isinstance(message.timestamp, str):
        return None
    try:
        return datetime.fromisoformat(message.timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def start_context_tokens(
    session_id: Optional[str], *, claude_projects_dir: Optional[Path] = None,
) -> Optional[int]:
    """Uncached input, cache read and both cache writes of the transcript's earliest assistant message.

    ``None`` when there is no transcript, no message carries a parseable
    timestamp to rank on, or any of the four parts is unknown: a partial sum
    would read as a smaller context than the build started with.
    """
    try:
        messages = harvest_session_messages(session_id, claude_projects_dir=claude_projects_dir)
    except Exception:  # vnx-silent-except: a transcript read must never break receipt emission (logged at debug)
        logger.debug("receipt_token_fields: transcript read failed session=%s", session_id, exc_info=True)
        return None
    ranked = [(stamp, message) for message in messages or [] if (stamp := _timestamp(message)) is not None]
    if not ranked:
        return None
    first = min(ranked, key=lambda pair: pair[0])[1]
    parts = [first.tokens.get(name) for name in LEDGER_TOKEN_FIELDS if name != "output"]
    if any(part is None for part in parts):
        return None
    return sum(parts)


def receipt_token_fields(
    *, provider: str, role: Optional[str], session_id: Optional[str],
    token_usage: Optional[Mapping[str, Any]], ledger_record: Optional[Mapping[str, Any]],
    claude_projects_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """The ``emit_dispatch_receipt`` keywords this module owns, ready to splat.

    ``start_context_tokens`` is only a candidate for a claude harness builder
    run (``harness_run_cost.writes_builder_run``); every other lane leaves it
    out. A candidate whose value is ``None`` is left out too, and the receipt
    omits a field whose value is ``None``.
    """
    from harness_run_cost import writes_builder_run

    try:
        normalized = normalized_tokens(token_usage, provider, ledger_record)
    except Exception as exc:  # vnx-silent-except: normalizing must never withhold the receipt; the failure is named on the field
        logger.warning("receipt_token_fields: normalization failed provider=%s: %s: %s", provider, type(exc).__name__, exc)
        normalized = {**dict.fromkeys(LEDGER_TOKEN_FIELDS), "split_reason": f"normalization_failed:{type(exc).__name__}"}
    fields: Dict[str, Any] = {"token_usage_normalized": normalized}
    if writes_builder_run(provider, role):
        fields["start_context_tokens"] = start_context_tokens(session_id, claude_projects_dir=claude_projects_dir)
    return fields


__all__ = ["normalized_tokens", "receipt_token_fields", "start_context_tokens"]
