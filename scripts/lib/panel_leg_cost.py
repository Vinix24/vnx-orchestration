"""panel_leg_cost.py — one cost-ledger line per deliberation-panel leg (K4a, ADR-043 §1).

A panel run makes several model calls: every stage (diverge, contrarian, verify,
synthesis) on every seat it tries. ADR-043's unit is the run — one start of one model for
one purpose — so each call is one line in ``provider_costs.ndjson``: ``role`` ``panel``, a
``producer`` naming the stage and the seat, the panel run's id as the ``ref``, and a refire
as the next attempt (:func:`cost_ledger.record_next_attempt`), never an overwrite.

The amount is the response's own answer, never a silent zero. A response that reported its
own amount (OpenRouter's ``usage.cost``, relayed in the leg's report as ADR-040's
``provider_reported`` pair or as the provider's ``usage`` mapping) is booked with source
``provider_reported``; tokens only are priced from the register by the writer (this module
never prices); no usable usage books every token ``null`` and ``unavailable:<reason>``
(``no_usage``, ``dispatch_error``), which counts in no sum (ADR-043 §5).

A leg its lane already booked through ``provider_dispatch`` is skipped on positive
evidence only — its receipt's ``cost_ledger_run_ids``; anything else is no evidence, so the
leg is booked rather than left out on a guess. Booking never fails the panel: a refusal or
an ``OSError`` is one WARNING naming the ref, the provider, the stage and the reason.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import cost_ledger
import harness_run_cost
from unified_report_schema import SchemaViolation, parse_frontmatter

logger = logging.getLogger(__name__)

PANEL_ROLE = "panel"
PROVIDER_REPORTED = "provider_reported"
# Why a leg carries no amount. Both are named on the line, never replaced by 0.
NO_USAGE_REASON = "no_usage"
DISPATCH_ERROR_REASON = "dispatch_error"
# codex and kimi bill flat on their own login (K2b's CLI_BILLING, mirrored here so a
# branch off main can book a leg on either lane); the harness lanes are harness_run_cost's.
_FLAT_LOGIN_BILLING: Mapping[str, str] = {"codex": "subscription", "kimi": "subscription"}
# MEASURED (OI-2407): a codex token_usage.input INCLUDES the cached part, so the uncached
# count is the difference. Every claude-harness lane and kimi already report it uncached.
_INPUT_INCLUDES_CACHE = frozenset({"codex"})
# producer is "<panel>-<stage>-<seat>", and a producer may not contain ':' (split_run_id).
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class LegUsage:
    """What one leg's response says about its usage, before the writer prices it."""

    tokens: Mapping[str, Optional[int]]
    reported_cost_usd: Optional[float] = None
    reason: str = NO_USAGE_REASON


def panel_run_id(mode: str) -> str:
    """A fresh panel run id: the ``ref`` every leg of one panel run books under."""
    return f"panel-{_UNSAFE_ID_CHARS.sub('_', mode)}-{uuid.uuid4().hex[:8]}"


def producer_for(stage: str, provider: str) -> str:
    """The leg's identity inside the panel run: ``panel-<stage>-<seat>``, never a ``:``."""
    return f"panel-{_UNSAFE_ID_CHARS.sub('_', stage)}-{_UNSAFE_ID_CHARS.sub('_', provider)}"


def billing_for(provider: str) -> Optional[str]:
    """The lane's billing, or None when the lane is unknown."""
    return harness_run_cost.BILLING_BY_PROVIDER.get(provider) or _FLAT_LOGIN_BILLING.get(provider)


def project_id_for(data_dir: Optional[Path]) -> str:
    """The tenant of the store the panel books into (ADR-007), through the one resolver."""
    state_dir = Path(data_dir) / "state" if data_dir is not None else None
    return harness_run_cost.ledger_project_id(state_dir)


def _amount(value: object) -> Optional[float]:
    """A reported amount, or None: missing, malformed or negative is no report."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _count(value: object) -> Optional[int]:
    """A reported count, or None: missing, malformed or negative is not a zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _frontmatter(report_text: object) -> Optional[Mapping[str, Any]]:
    """The leg's frontmatter, or None when the text carries no readable one."""
    try:
        return parse_frontmatter(report_text if isinstance(report_text, str) else "")
    except SchemaViolation:
        return None


def _reported_cost(frontmatter: Optional[Mapping[str, Any]]) -> Optional[float]:
    """The amount the provider itself reported, or None when it reported none.

    Two relayed shapes in the leg's own report: the provider's ``usage`` mapping
    (OpenRouter's ``usage.cost``), and ADR-040's pair. A register price the lane stamped
    itself is NOT a report — the writer prices.
    """
    if not isinstance(frontmatter, Mapping):
        return None
    usage = frontmatter.get("usage")
    if isinstance(usage, Mapping):
        amount = _amount(usage.get("cost"))
        if amount is not None:
            return amount
    if frontmatter.get("cost_source") == PROVIDER_REPORTED:
        return _amount(frontmatter.get("cost_usd"))
    return None


def _reported_tokens(frontmatter: Optional[Mapping[str, Any]],
                     provider: str) -> Optional[Dict[str, Optional[int]]]:
    """The five ledger token fields from the report's ``token_usage``, or None when it reports none.

    ``token_usage_measured: false`` is the lane saying it did not measure the run, so the
    counts beside it are not usage.
    """
    if not isinstance(frontmatter, Mapping) or frontmatter.get("token_usage_measured") is False:
        return None
    usage = frontmatter.get("token_usage")
    if not isinstance(usage, Mapping):
        return None
    # The frontmatter's own names, as unified_report_schema.report_cost_fields writes them.
    total_input, cache_read = _count(usage.get("input")), _count(usage.get("cache_read"))
    written = _count(usage.get("cache_write"))
    tokens: Dict[str, Optional[int]] = {
        "input_uncached": total_input, "cache_hit": cache_read, "output": _count(usage.get("output")),
        # One flat cache-write count under 5m and 0 under 1h, as token_harvest reads it.
        "cache_write_5m": written, "cache_write_1h": 0 if written is not None else None,
    }
    if provider in _INPUT_INCLUDES_CACHE and total_input is not None:
        # Never book the total as uncached (OI-2407), and never a negative: a cache read
        # larger than the input it is part of leaves the split unknown.
        spread = cache_read is not None and cache_read <= total_input
        tokens["input_uncached"] = total_input - cache_read if spread else None
    return tokens if any(count is not None for count in tokens.values()) else None


def leg_usage_from_report(report_text: object, *, provider: str) -> LegUsage:
    """One leg's usage, from the unified report the lane returned for it."""
    frontmatter = _frontmatter(report_text)
    reported = _reported_cost(frontmatter)
    tokens = _reported_tokens(frontmatter, provider)
    if tokens is None:
        return LegUsage(dict.fromkeys(cost_ledger.TOKEN_FIELDS), reported, NO_USAGE_REASON)
    return LegUsage(tokens, reported, "")


def already_booked_run_ids(receipts: Optional[Sequence[Mapping[str, Any]]]) -> List[str]:
    """The cost-ledger run ids a leg already has, from its receipts' ``cost_ledger_run_ids``.

    Positive evidence only (ADR-043 §6): a non-empty list of non-empty strings. Anything
    else is not evidence the leg is booked.
    """
    if not isinstance(receipts, Sequence) or isinstance(receipts, (str, bytes)):
        return []
    booked: List[str] = []
    for receipt in receipts:
        ids = receipt.get("cost_ledger_run_ids") if isinstance(receipt, Mapping) else None
        if isinstance(ids, (str, bytes)) or not isinstance(ids, Sequence):
            continue
        booked.extend(run_id for run_id in ids if isinstance(run_id, str) and run_id.strip())
    return booked


def _iso(moment: datetime) -> str:
    """A ledger timestamp: UTC, ``Z``-suffixed."""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _completeness(tokens: Mapping[str, Optional[int]], reason: str) -> str:
    """``complete``, ``partial:<null fields>`` or ``unknown:<reason>`` — the writer's vocabulary."""
    unknown = sorted(name for name in cost_ledger.TOKEN_FIELDS if tokens[name] is None)
    if not unknown:
        return "complete"
    if len(unknown) == len(cost_ledger.TOKEN_FIELDS):
        return f"unknown:{reason or NO_USAGE_REASON}"
    return "partial:" + ",".join(unknown)


class LegBooker:
    """Books one ledger line per panel leg of one panel run, and never fails the panel."""

    def __init__(self, *, ref: str, project_id: str, ledger_path: Optional[Path] = None) -> None:
        self.ref, self.project_id = ref, project_id
        self.ledger_path = Path(ledger_path) if ledger_path is not None else None
        self.run_ids: List[str] = []

    def book(
        self,
        *,
        stage: str,
        provider: str,
        model: str,
        dispatch_id: str,
        report_text: object,
        started: datetime,
        ended: datetime,
        receipts: Optional[Sequence[Mapping[str, Any]]] = None,
        failure: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Write this leg's line; return it as written, or None when it is skipped or refused.

        ``failure`` names why the dispatch produced no response (a raised dispatch): the
        line then books ``unavailable:<failure>`` rather than read an error as a report.
        """
        booked = already_booked_run_ids(receipts)
        if booked:
            logger.debug("panel_leg_cost: leg %s %s already booked as %s", stage, provider, booked)
            return None
        usage = LegUsage(dict.fromkeys(cost_ledger.TOKEN_FIELDS), None, failure) if failure else \
            leg_usage_from_report(report_text, provider=provider)
        return self._write(stage=stage, provider=provider, model=model, dispatch_id=dispatch_id,
                           usage=usage, started=started, ended=ended)

    def _write(self, *, stage: str, provider: str, model: str, dispatch_id: str,
               usage: LegUsage, started: datetime, ended: datetime) -> Optional[Dict[str, Any]]:
        billing = billing_for(provider)
        if billing is None:
            logger.warning(
                "panel_leg_cost: leg %s (%s %s, stage %s, dispatch %s) not booked: no billing for the lane",
                self.ref, provider, model, stage, dispatch_id,
            )
            return None
        tokens = dict(usage.tokens)
        cost_usd = api_equivalent_usd = cost_source = None
        if usage.reported_cost_usd is not None:
            cost_source = PROVIDER_REPORTED
            # ADR-043 §1: a subscription run costs 0 at the margin; the reported amount is
            # its list price.
            if billing == "subscription":
                api_equivalent_usd = usage.reported_cost_usd
            else:
                cost_usd = usage.reported_cost_usd
        elif all(count is None for count in tokens.values()):
            cost_source = f"unavailable:{usage.reason or NO_USAGE_REASON}"
        producer = producer_for(stage, provider)

        def make_run(run_id: str) -> cost_ledger.RunCost:
            return cost_ledger.RunCost(
                run_id=run_id, project_id=self.project_id, role=PANEL_ROLE,
                provider=provider, model=model, billing=billing,
                occurred_from=_iso(started), occurred_to=_iso(ended),
                tokens=tokens, usage_completeness=_completeness(tokens, usage.reason),
                cost_usd=cost_usd, cost_source=cost_source, api_equivalent_usd=api_equivalent_usd,
            )

        try:
            record = cost_ledger.record_next_attempt(
                self.project_id, producer, self.ref, make_run, ledger_path=self.ledger_path,
            )
        except (cost_ledger.CostLedgerRefusal, OSError) as exc:
            # CostLedgerWriteError IS an OSError: both mean "no confirmed line".
            logger.warning(
                "panel_leg_cost: leg %s (%s %s, stage %s, dispatch %s) not booked: %s: %s",
                self.ref, provider, model, stage, dispatch_id, type(exc).__name__, exc,
            )
            return None
        self.run_ids.append(record["run_id"])
        return record
