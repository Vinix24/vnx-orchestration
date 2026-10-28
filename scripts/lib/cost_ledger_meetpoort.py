"""cost_ledger_meetpoort.py — the measurement gate of the daily ceiling (K5c, ADR-043 §8).

``paid_lane_budget`` reads ``cost_usd`` from the receipts; the ceiling may read
the cost ledger instead, but only once that ledger measures the lanes that
spend. A hand-flipped switch would let it read a ledger that still misses whole
producer families, and a lower number is never a safety feature. So the gate is
a pure function over the ledger, evaluated per call: its answer *is* the
evidence (no state file, nothing to flip).

Three things make it pass, all measured:

1. **The rollout.** Each of ``builder``, ``review``, ``plan_gate``, ``panel`` and
   ``intelligence`` (the K2a, K2b, K3, K4a and K4b producers) has at least one
   dated line; ``tiebreaker`` is not required, it runs too rarely to mark a
   rollout. A role with no line, or none carrying an ``occurred_from`` date, is
   in ``missing_roles``: its rollout is not measured.
2. **Seven full days.** The role that arrived last arrived at least seven full
   UTC days before ``today``, so every producer covers the window below.
3. **A clean window.** The seven full UTC days before ``today`` hold no
   ``billing: metered`` line with an ``unavailable:*`` ``cost_source``, except
   the exceptions below; offenders are listed per (provider, cost_source).

The exceptions, each a constant with its reason: ``KIMI_NO_USAGE_SOURCE``
(``unavailable:kimi_cli_no_usage`` — the kimi CLI has no usage source; narrow on
purpose, any other ``unavailable:*`` on a kimi line is a capture gap) and
``SPAWN_REFUSED_SOURCE`` (a refused spawn made no model call: its zero is
measured, not missing). An incomplete read (a run waiting in the spool, an
unreadable line, a torn tail) shuts the gate, and so does any error reading it
(:func:`read_failure`): the caller keeps to the receipts then.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Mapping, Tuple

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import cost_ledger  # noqa: E402
from harness_run_cost import KIMI_NO_USAGE_REASON, SPAWN_REFUSED_SOURCE  # noqa: E402

# The roles whose presence proves the ledger measures the lanes that spend
# (K2a builder, K2b review, K3 plan_gate, K4a panel, K4b intelligence);
# `tiebreaker` is absent on purpose: it runs too rarely to mark a rollout.
# The window is this many full UTC days before `today`, and the lead the last
# rolled-out role needs so that window is covered by every producer.
REQUIRED_ROLES: Tuple[str, ...] = ("builder", "review", "plan_gate", "panel", "intelligence")
WINDOW_DAYS = 7

UNKNOWN_COST_PREFIX = "unavailable:"
KIMI_PROVIDER = "kimi"
# `unavailable:kimi_cli_no_usage` — the kimi CLI has no usage source (ADR-043 §8).
KIMI_NO_USAGE_SOURCE = f"{UNKNOWN_COST_PREFIX}{KIMI_NO_USAGE_REASON}"

_UNKNOWN_NAME = "(unknown)"


@dataclass(frozen=True)
class Offender:
    """One ``(provider, cost_source)`` pair that keeps the gate shut, with the runs that carry it."""

    provider: str
    cost_source: str
    count: int


@dataclass(frozen=True)
class Meetpoort:
    """The gate's answer for one day: ``passed``, every cause in ``reason``, and the evidence behind it."""

    passed: bool
    reason: str
    window: Tuple[str, str]
    missing_roles: Tuple[str, ...]
    offenders: Tuple[Offender, ...]


def window_for(today: date) -> Tuple[str, str]:
    """The seven full UTC days before ``today`` as ``(first, last)`` ISO dates, both included."""
    return ((today - timedelta(days=WINDOW_DAYS)).isoformat(), (today - timedelta(days=1)).isoformat())


def read_failure(today: date, why: object) -> Meetpoort:
    """A shut gate for a ledger that could not be read or aggregated: the caller keeps to the receipts."""
    return Meetpoort(False, f"the cost ledger could not be read: {why}", window_for(today), (), ())


def evaluate(ledger_path: Path, today: date) -> Meetpoort:
    """Read the ledger once and answer whether the ceiling may read it from now on.

    A ledger that does not exist is no evidence of a rollout and shuts the gate
    unread (a store without one is not opened for writing either); every other
    error is raised, never taken for an empty ledger (:func:`read_failure`).
    """
    path = Path(ledger_path)
    if not path.exists():
        return Meetpoort(False, f"no cost ledger at {path}", window_for(today), REQUIRED_ROLES, ())
    return verdict(cost_ledger.aggregate_runs(path), today)


def verdict(totals: cost_ledger.LedgerTotals, today: date) -> Meetpoort:
    """The gate over one read: :func:`evaluate`'s answer once the ledger is read (so a caller holding a read has one)."""
    window = window_for(today)
    if totals.incomplete:
        return Meetpoort(False, f"the ledger read is incomplete: {totals.incomplete}", window, (), ())
    first_days: Dict[str, str] = {}
    offenders: Dict[Tuple[str, str], int] = {}
    for record in totals.runs.values():
        role = record.get("role")
        if isinstance(role, str):
            day = cost_ledger.utc_day(record.get("occurred_from"))
            if day is not None and (role not in first_days or day < first_days[role]):
                first_days[role] = day
        if _offends(record, window):
            provider = record.get("provider")
            provider = provider if isinstance(provider, str) and provider else _UNKNOWN_NAME
            key = (provider, str(record.get("cost_source")))
            offenders[key] = offenders.get(key, 0) + 1

    missing = tuple(role for role in REQUIRED_ROLES if role not in first_days)
    reasons = []
    if missing:
        reasons.append("no dated line for role(s) " + ", ".join(missing))
    # "Seven full days before today" is the first day of the window: a role whose
    # first dated line is that day still covers the whole window.
    latest = max((role for role in REQUIRED_ROLES if role in first_days), key=first_days.__getitem__, default=None)
    if latest is not None and first_days[latest] > window[0]:
        reasons.append(
            f"role {latest} has its first dated line on {first_days[latest]}, less than "
            f"{WINDOW_DAYS} full UTC days before {today.isoformat()}"
        )
    listed = tuple(Offender(provider, source, count) for (provider, source), count in sorted(offenders.items()))
    if listed:
        reasons.append(
            "metered unavailable line(s) in the window: "
            + ", ".join(f"{offender.provider} {offender.cost_source} x{offender.count}" for offender in listed)
        )
    if reasons:
        return Meetpoort(False, "; ".join(reasons), window, missing, listed)
    return Meetpoort(
        True,
        f"all of {', '.join(REQUIRED_ROLES)} rolled out at least {WINDOW_DAYS} days ago; "
        f"the window {window[0]}..{window[1]} is clean",
        window, (), (),
    )


def _offends(record: Mapping[str, object], window: Tuple[str, str]) -> bool:
    """True for a metered ``unavailable:*`` line in the window, the two ADR-043 §8 exceptions aside."""
    if record.get("billing") != "metered":
        return False
    source = record.get("cost_source")
    if not isinstance(source, str) or not source.startswith(UNKNOWN_COST_PREFIX):
        return False
    day = cost_ledger.utc_day(record.get("occurred_from"))
    if day is None or day < window[0] or day > window[1]:
        return False
    if source == SPAWN_REFUSED_SOURCE:  # a refused spawn made no model call: a measured zero
        return False
    return not (record.get("provider") == KIMI_PROVIDER and source == KIMI_NO_USAGE_SOURCE)
