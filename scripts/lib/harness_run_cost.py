"""harness_run_cost.py — the cost-ledger line of a builder run on a claude harness lane.

Track ``kosten-per-model-in-het-grootboek``, deliverable K2a, PR K2a-2 (ADR-043).
Every builder run on a lane in ``token_harvest.CLAUDE_HARNESS_PROVIDERS``
(``claude``, ``deepseek-harness``, ``glm-harness``) writes exactly one
``cost_ledger`` line per attempt, through :func:`record_builder_run`. The three
call sites are ``envelope_govern._govern`` (the door's headless and provider
lanes), ``subprocess_dispatch_internals.recovery`` (the terminal-pinned
subprocess lane, one line per delivery attempt) and
``provider_dispatch._emit_governance`` (the standalone provider CLI). Each
names its own producer in the ``run_id``.

What the line carries:

- tokens from the session transcript, per assistant message deduplicated on
  ``message.id`` (``token_harvest.harvest_session_messages``). A field a
  message does not carry makes the run's field ``None`` and the run
  ``partial:<fields>``, never a silent 0.
- no transcript, or no assistant usage in it: every token ``None``,
  ``unknown:no_transcript`` and ``unavailable:no_transcript``. Tokens are
  never made up.
- a spawn the lane refused, or that never started (the caller names the
  condition in ``spawn_refused``): five zeros, ``complete``, ``cost_usd`` 0 and
  ``unavailable:spawn_refused``.
- ``segments`` per price window when the model's latest registry price has a
  peak/off-peak split (DeepSeek): one per rate class, from the message
  timestamps, so a run across a window boundary is priced per segment.
- ``billing``: ``subscription`` on the native ``claude`` lane, ``metered`` on
  the two harness lanes that run on an own API key.

Only builder runs move here. A gate seat, a plan-gate seat or a panel leg on
the same lanes (:data:`NOT_BUILDER_ROLES`) keeps the old ``emit_provider_cost``
event until K3/K4a move it. The ledger's errors are never caught here:
``CostLedgerWriteError`` and a ``CostLedgerRefusal`` reach the caller, which
must not write its receipt with a cost it does not have (ADR-005, ADR-043).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import cost_ledger
import provider_costs
from reviewer_roles import REVIEWER_ROLES
from token_harvest import (
    CLAUDE_HARNESS_PROVIDERS,
    LEDGER_TOKEN_FIELDS,
    MessageUsage,
    harvest_session_messages,
)

logger = logging.getLogger(__name__)

BUILDER_ROLE = "builder"

# The native claude lane runs on the Max subscription (marginal cost 0, list
# price in api_equivalent_usd); the deepseek and glm harness lanes run on an
# own API key, so every token is real money.
BILLING_BY_PROVIDER: Mapping[str, str] = {
    "claude": "subscription",
    "deepseek-harness": "metered",
    "glm-harness": "metered",
}

# Dispatch roles on these lanes that are not builder runs: review-gate seats
# and plan-gate seats (K3) and deliberation-panel legs (K4a).
NOT_BUILDER_ROLES = frozenset(REVIEWER_ROLES | {"deliberation-panelist"})

SPAWN_REFUSED_SOURCE = "unavailable:spawn_refused"
NO_TRANSCRIPT_REASON = "no_transcript"
NO_USAGE_FIELDS_REASON = "no_usage_fields"


def writes_builder_run(provider: Optional[str], role: Optional[str]) -> bool:
    """True when this run's cost goes to the ledger as a builder run (K2a-2), not the old event."""
    return provider in CLAUDE_HARNESS_PROVIDERS and (role or "") not in NOT_BUILDER_ROLES


# A plan model that names no model on the redirected harness lanes: the door's
# placeholder, the claude default, or nothing. The lane then runs its own default.
_LANE_PLACEHOLDERS = frozenset({"default", "sonnet", ""})


def lane_model(provider: str, requested: Optional[str]) -> str:
    """The model a harness lane runs for the model a plan requested.

    ``deepseek-harness`` and ``glm-harness`` resolve a placeholder to their own
    default (``resolve_harness_model``: the ``VNX_*_HARNESS_MODEL`` override,
    then the registry default); any other value runs as given. The provider
    adapter spawns with this model, and a spawn the lane refused before it
    resolved one is booked with it too. ``claude`` runs what was requested.
    """
    value = (requested or "").strip()
    if provider == "deepseek-harness":
        from provider_spawns.deepseek_harness_spawn import resolve_harness_model  # noqa: PLC0415
    elif provider == "glm-harness":
        from provider_spawns.glm_harness_spawn import resolve_harness_model  # noqa: PLC0415
    else:
        return value
    return resolve_harness_model(None if value in _LANE_PLACEHOLDERS else value)


def ledger_project_id(state_dir: Optional[Path]) -> str:
    """The project of the run: from the store, else ``VNX_PROJECT_ID``, else ``vnx-dev``.

    The same order ``emit_provider_cost`` used for the old event (ADR-007:
    the store decides; the cost record is never skipped for want of a tenant).
    """
    from project_scope import TenantUnresolved, resolve_stamp_project_id  # noqa: PLC0415

    if state_dir is not None:
        try:
            return resolve_stamp_project_id(db_path=Path(state_dir) / "quality_intelligence.db")
        except TenantUnresolved as exc:
            logger.warning("harness_run_cost: project_id unresolved from the store, using the env: %s", exc)
    return os.environ.get("VNX_PROJECT_ID") or "vnx-dev"


@dataclass(frozen=True)
class BuilderRun:
    """What the runner knows about one attempt: who ran, when, and the session it left.

    ``spawn_refused`` names the condition under which the lane refused the
    spawn or the spawn never started; ``None`` when it ran (or may have run).
    """

    provider: str
    model: str
    started: datetime
    ended: datetime
    session_id: Optional[str] = None
    spawn_refused: Optional[str] = None


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parsed(timestamp: Optional[str]) -> Optional[datetime]:
    """A transcript timestamp as an aware datetime, or ``None`` when it is absent, malformed or naive."""
    if not isinstance(timestamp, str):
        return None
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def run_tokens(messages: Sequence[MessageUsage]) -> Dict[str, Optional[int]]:
    """The run's five counts: the sum per field, ``None`` for a field any message lacks."""
    totals: Dict[str, Optional[int]] = {}
    for name in LEDGER_TOKEN_FIELDS:
        values = [message.tokens.get(name) for message in messages]
        totals[name] = None if any(value is None for value in values) else sum(values)
    return totals


def usage_completeness(tokens: Mapping[str, Optional[int]]) -> str:
    unknown = sorted(name for name in LEDGER_TOKEN_FIELDS if tokens[name] is None)
    if not unknown:
        return "complete"
    if len(unknown) == len(LEDGER_TOKEN_FIELDS):
        return f"unknown:{NO_USAGE_FIELDS_REASON}"
    return "partial:" + ",".join(unknown)


def _has_peak_split(provider: str, model: str) -> bool:
    """Whether the model's latest registry price has an off-peak rate, the split ``price_run`` applies."""
    from providers import provider_registry  # noqa: PLC0415

    registry_key = provider_costs.registry_key_for_provider(provider)
    versions = provider_registry.resolve_price_versions(registry_key, model) if registry_key else None
    latest = versions[-1] if versions else None
    return (
        latest is not None
        and latest.off_peak_input_per_mtok is not None
        and latest.off_peak_output_per_mtok is not None
    )


def _is_peak(timestamp: Optional[str]) -> bool:
    """The rate class of a message. Without a readable timestamp: peak, the upper bound."""
    moment = _parsed(timestamp)
    return True if moment is None else provider_costs._is_peak(moment)


def price_segments(
    provider: str, model: str, messages: Sequence[MessageUsage], tokens: Mapping[str, Optional[int]],
) -> Optional[List[Dict[str, Any]]]:
    """One segment per rate class when the price has a peak/off-peak split, else ``None``.

    Each message counts in the class of its own timestamp. A field the run
    does not know is ``None`` in every segment, so the segments add up to the
    run field by field, which the writer checks. Peak comes first; ``ts`` is
    the first readable timestamp of the class.
    """
    if not _has_peak_split(provider, model):
        return None
    classes: Dict[bool, Dict[str, Any]] = {}
    for message in messages:
        peak = _is_peak(message.timestamp)
        segment = classes.setdefault(peak, {"counts": dict.fromkeys(LEDGER_TOKEN_FIELDS, 0), "ts": None})
        for name in LEDGER_TOKEN_FIELDS:
            if tokens[name] is not None:
                segment["counts"][name] += message.tokens[name]
        if segment["ts"] is None and _parsed(message.timestamp) is not None:
            segment["ts"] = message.timestamp
    segments: List[Dict[str, Any]] = []
    for peak in sorted(classes, reverse=True):
        counts = classes[peak]["counts"]
        segment = {"tokens": {name: (counts[name] if tokens[name] is not None else None)
                              for name in LEDGER_TOKEN_FIELDS}, "peak": peak}
        if classes[peak]["ts"] is not None:
            segment["ts"] = classes[peak]["ts"]
        segments.append(segment)
    return segments


def _window(messages: Sequence[MessageUsage], run: BuilderRun) -> Tuple[str, str]:
    """First and last message timestamp, or the dispatch start and end when no message has one."""
    stamped = [(moment, message.timestamp) for message in messages
               if (moment := _parsed(message.timestamp)) is not None]
    if not stamped:
        return _iso(run.started), _iso(run.ended)
    return min(stamped)[1], max(stamped)[1]


def _refused_fields(run: BuilderRun) -> Dict[str, Any]:
    return {
        "tokens": dict.fromkeys(LEDGER_TOKEN_FIELDS, 0), "usage_completeness": "complete",
        "cost_usd": 0.0, "cost_source": SPAWN_REFUSED_SOURCE,
        "occurred_from": _iso(run.started), "occurred_to": _iso(run.ended),
    }


def _no_transcript_fields(run: BuilderRun) -> Dict[str, Any]:
    return {
        "tokens": dict.fromkeys(LEDGER_TOKEN_FIELDS), "usage_completeness": f"unknown:{NO_TRANSCRIPT_REASON}",
        "cost_source": f"unavailable:{NO_TRANSCRIPT_REASON}",
        "occurred_from": _iso(run.started), "occurred_to": _iso(run.ended),
    }


def _transcript_fields(run: BuilderRun, messages: Sequence[MessageUsage]) -> Dict[str, Any]:
    tokens = run_tokens(messages)
    occurred_from, occurred_to = _window(messages, run)
    return {
        "tokens": tokens, "usage_completeness": usage_completeness(tokens),
        "segments": price_segments(run.provider, run.model, messages, tokens),
        "occurred_from": occurred_from, "occurred_to": occurred_to,
    }


def _session(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def run_fields(run: BuilderRun, *, claude_projects_dir: Optional[Path] = None) -> Dict[str, Any]:
    """The usage part of the run's :class:`cost_ledger.RunCost`, from the refusal or the transcript."""
    refused = run.spawn_refused if isinstance(run.spawn_refused, str) and run.spawn_refused else None
    if refused is not None:
        return _refused_fields(run)
    messages = harvest_session_messages(_session(run.session_id), claude_projects_dir=claude_projects_dir)
    if messages is None:
        return _no_transcript_fields(run)
    return _transcript_fields(run, messages)


def record_builder_run(
    run: BuilderRun,
    *,
    project_id: str,
    producer: str,
    dispatch_id: str,
    ledger_path: Optional[Path] = None,
    claude_projects_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Write the ledger line of one builder attempt; return the record as written.

    The ``run_id`` is ``<project_id>:<producer>:<dispatch_id>:<attempt>``, the
    attempt the next one the ledger holds for that producer and dispatch
    (``cost_ledger.record_next_attempt``), so a refire is a new run. Raises
    what the ledger raises; nothing here catches it.
    """
    if run.provider not in BILLING_BY_PROVIDER:
        raise cost_ledger.InvalidRecord(f"provider {run.provider!r} is not a claude harness lane")
    fields = run_fields(run, claude_projects_dir=claude_projects_dir)
    if fields.get("cost_source") is not None:
        logger.info(
            "harness_run_cost: dispatch=%s provider=%s booked as %s (%s)",
            dispatch_id, run.provider, fields["cost_source"], run.spawn_refused or "no transcript",
        )

    def make_run(run_id: str) -> cost_ledger.RunCost:
        return cost_ledger.RunCost(
            run_id=run_id, project_id=project_id, role=BUILDER_ROLE, provider=run.provider,
            model=run.model, billing=BILLING_BY_PROVIDER[run.provider], **fields,
        )

    return cost_ledger.record_next_attempt(project_id, producer, dispatch_id, make_run, ledger_path=ledger_path)


def receipt_cost(
    records: Sequence[Mapping[str, Any]], *, ledger_path: Optional[Path] = None,
) -> Tuple[Optional[float], Optional[str], List[str]]:
    """``(cost_usd, cost_source, run_ids)`` for the receipt that summarises these ledger lines.

    One line: its own ``cost_usd`` and ``cost_source``, the record the writer
    returned. Several (the attempts of one subprocess-lane dispatch): the sum
    ``cost_ledger.aggregate_runs`` gives over exactly those runs; a run without
    a price makes the receipt's cost ``None`` with an ``unavailable:`` source,
    never a lower total.
    """
    run_ids = [str(record["run_id"]) for record in records]
    if not records:
        return None, None, []
    if len(records) == 1:
        return records[0]["cost_usd"], records[0]["cost_source"], run_ids
    wanted = set(run_ids)
    totals = cost_ledger.aggregate_runs(ledger_path, select=lambda record: record.get("run_id") in wanted)
    if totals.unavailable or len(totals.runs) != len(wanted):
        unpriced = len(wanted) - len(totals.runs) + len(totals.unavailable)
        return None, f"unavailable:runs_without_price:{unpriced}/{len(wanted)}", run_ids
    sources = sorted({str(record.get("cost_source")) for record in totals.runs.values()})
    return totals.cost_usd, "+".join(sources), run_ids


__all__ = [
    "BILLING_BY_PROVIDER", "BUILDER_ROLE", "BuilderRun", "NOT_BUILDER_ROLES", "SPAWN_REFUSED_SOURCE",
    "lane_model", "ledger_project_id", "price_segments", "receipt_cost", "record_builder_run", "run_fields",
    "run_tokens", "usage_completeness", "writes_builder_run",
]
