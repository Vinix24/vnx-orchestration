"""paid_lane_budget.py — daily spend cap for metered-API provider lanes.

The gap this closes: ``DEEPSEEK_API_KEY`` and ``OPENROUTER_API_KEY`` sit in the
environment with no expenditure ceiling anywhere in the fabric. The ONLY cost
cap that existed before this module was ``receipt_classifier.py``'s own
``DEFAULT_DAILY_BUDGET_USD`` — and that covers exactly one consumer (the
receipt classifier's own LLM calls), not the dispatch-lane traffic that
actually spends against these two keys (deepseek-harness, glm-harness,
litellm:deepseek, litellm:zai — glm-harness alone produced 8 real charges on
2026-09-04, ~$0.37 total).

Design choice — deliberately NOT ``receipt_classifier``'s pattern
-------------------------------------------------------------------
``receipt_classifier.py`` tracks spend in a dedicated counter file
(``receipt_classifier_cost.json``), incremented by an explicit ``track_cost()``
call placed at the ONE spot inside that module where its own provider call
returns. That shape fits there because the classifier is the only writer of
that file and the only reader of that number.

This module instead computes today's spend by scanning the receipts ledger
(``t0_receipts.ndjson``) — the SAME append-only file every paid-lane dispatch
already writes its ``cost_usd`` into via ``governance_emit.emit_dispatch_receipt``
(see ``scripts/lib/cost_tracker.py::recent_cost_per_hour`` for existing
precedent: reading the ledger for cost aggregation is an established pattern
here, not a new one).

A second, independently-maintained counter file would be a second place that
has to be kept in sync with the ledger by hand at every call site that spends
money — and every call site would need to remember to call ``track_cost()``.
There are at least two such call sites for paid lanes (the door's provider
lane in ``dispatch_envelope.run_envelope_plan`` and the standalone CLI entry
in ``provider_dispatch.py``'s ``main()``), and history in this fabric shows
that a second record of a fact nobody re-derives from the first is exactly
where drift creeps in when a writer crashes between updating the two. The
receipts ledger is already the fabric's single source of truth for what a
dispatch actually cost (ADR-005/ADR-035); reading it directly means the
budget check can never disagree with the ledger, and needs no write path of
its own to go stale.

The trade-off: an is-this-provider-a-paid-lane classification here is
DELIBERATELY based on which env key a provider's spawn handler draws on
(architecture), never on whether the ledger happens to show nonzero
``cost_usd`` for that provider historically. A lane whose cost computation
hits a pricing-registry miss stamps ``cost_usd: 0.0`` on its receipt even
though real money moved (see ``provider_dispatch._compute_cost``'s "Registry
miss" fallback) — a lane that does not REPORT a cost is not the same as a
lane that HAS no cost, and must not be silently exempted from the cap because
its own receipts happen to read zero.

Call sites (both un-evadable pre-flight points, before any worker spawns):
  - ``dispatch_envelope.run_envelope_plan`` — the door's provider lane
    (``dispatch_cli.py`` calls this directly for every kimi/glm/deepseek
    dispatch fired through ``vnx dispatch``).
  - ``provider_dispatch.main()`` — the standalone CLI entry point used by
    callers that invoke the provider lane directly as a subprocess (e.g.
    ``plan_gate_panel.py``), bypassing the door.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

ENV_DAILY_BUDGET = "VNX_PAID_LANE_DAILY_BUDGET_USD"
ENV_OVERRIDE = "VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET"
ENV_STATE_DIR = "VNX_STATE_DIR"

# Conservative default: today's real paid-lane spend (8 glm-harness runs +
# 1 deepseek-harness run, 2026-09-04) totalled ~$0.37. $5.00/day leaves
# comfortable headroom for legitimate use while bounding a runaway loop to a
# two-digit-dollar mistake instead of an unbounded one. Operator-tunable via
# ENV_DAILY_BUDGET.
DEFAULT_DAILY_BUDGET_USD = 5.00

RECEIPTS_FILE_NAME = "t0_receipts.ndjson"

# provider (or litellm sub_provider) -> the env var whose key it spends
# against. Mirrors provider_dispatch.py's _SUB_PROVIDER_KEY_REQS plus the
# deepseek-harness/glm-harness special cases provider_dispatch._check_constraints
# resolves before matching (deepseek-harness -> sub_provider "deepseek",
# glm-harness -> sub_provider "zai" routed via OpenRouter). Kept as an
# independent literal set here (not imported from provider_dispatch) so this
# module has no reverse dependency on the file it is meant to gate — the two
# are cross-referenced by comment instead, deliberately narrow in scope to the
# two keys named in the gap this module closes (kimi/moonshot use their own
# key and route CLI-OAuth-only; claude/codex/gemini are subscription/OAuth
# lanes with no per-token key here).
_PAID_SUB_PROVIDER_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "zai": "OPENROUTER_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

_PAID_TOP_LEVEL_PROVIDER_KEYS = {
    "deepseek-harness": "DEEPSEEK_API_KEY",
    "glm-harness": "OPENROUTER_API_KEY",
}


class PaidLaneBudgetExceededError(RuntimeError):
    """Raised when a paid-lane dispatch is refused for exceeding the daily cap."""

    def __init__(self, provider: str, spent_usd: float, budget_usd: float) -> None:
        self.provider = provider
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd
        super().__init__(
            f"paid_lane_daily_budget_exceeded: provider={provider} "
            f"spent_today_usd={spent_usd:.6f} daily_budget_usd={budget_usd:.6f} — "
            f"refusing further paid-lane spend today "
            f"(operator override: {ENV_OVERRIDE}=1)"
        )


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def paid_lane_env_key(provider: Optional[str]) -> Optional[str]:
    """Return the env var a `provider` string draws on, or None when it is not
    one of the two metered lanes this module caps.

    Classification is by ARCHITECTURE (which key the spawn handler requires),
    never by observed historical cost — see module docstring.
    """
    if not provider:
        return None
    normalized = provider.strip().lower()
    if normalized in _PAID_TOP_LEVEL_PROVIDER_KEYS:
        return _PAID_TOP_LEVEL_PROVIDER_KEYS[normalized]
    if normalized.startswith("litellm:"):
        parts = normalized.split(":", 2)
        sub_provider = parts[1] if len(parts) > 1 else ""
        if sub_provider in _PAID_SUB_PROVIDER_KEYS:
            return _PAID_SUB_PROVIDER_KEYS[sub_provider]
    return None


def is_paid_lane(provider: Optional[str]) -> bool:
    return paid_lane_env_key(provider) is not None


def get_daily_budget_usd() -> float:
    raw = os.environ.get(ENV_DAILY_BUDGET)
    if raw is None or raw == "":
        return DEFAULT_DAILY_BUDGET_USD
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_BUDGET_USD


def default_state_dir() -> Path:
    """Resolve VNX_STATE_DIR: explicit env var first, else the canonical
    central resolver (vnx_paths.resolve_paths(), VNX_HOME + project-marker
    aware — never a repo-relative or __file__-anchored guess; the
    central-mode path gate in CI rejects that shape, and it would fork state
    away from the real central store in a central install). Callers that
    already hold a resolved state_dir (dispatch_envelope.run_envelope_plan,
    provider_dispatch._resolve_state_dir) should pass it in directly instead
    of relying on this fallback — this exists for standalone/test callers.
    An unresolvable project fails loud (ADR-007: fail-closed, not a guess).
    """
    raw = os.environ.get(ENV_STATE_DIR)
    if raw:
        return Path(raw)
    from vnx_paths import resolve_paths

    return Path(resolve_paths()["VNX_STATE_DIR"])


def _receipt_date_utc(raw_ts) -> Optional[str]:
    """Return the receipt's UTC calendar date as YYYY-MM-DD, or None if the
    timestamp is missing/unparseable. Handles both the ISO-8601 'Z' string
    form every current writer stamps and a bare epoch number (legacy/other
    event shapes seen on the same ledger) so a format quirk on an unrelated
    event type can never masquerade as "no spend today".
    """
    if raw_ts is None:
        return None
    if isinstance(raw_ts, (int, float)):
        try:
            return datetime.fromtimestamp(raw_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw_ts, str):
        text = raw_ts.strip()
        if not text:
            return None
        # Fast path: every current writer stamps "%Y-%m-%dT%H:%M:%SZ" —
        # the date is the first 10 characters.
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10]
        try:
            return datetime.fromisoformat(text.rstrip("Z")).replace(
                tzinfo=timezone.utc
            ).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def spent_today_usd(state_dir: Path) -> float:
    """Sum cost_usd across today's (UTC) receipts for paid-lane providers.

    Reads the append-only receipts ledger directly (see module docstring for
    why this is preferred over a separately-maintained counter). Missing or
    unparseable lines/timestamps are skipped, never treated as an excuse to
    abort the whole scan — a single malformed line must not blind the cap to
    every other real charge on the same day.
    """
    receipts_path = Path(state_dir) / RECEIPTS_FILE_NAME
    if not receipts_path.is_file():
        return 0.0

    today = _today_str()
    total = 0.0
    try:
        with receipts_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    receipt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(receipt, dict):
                    continue
                if not is_paid_lane(receipt.get("provider")):
                    continue
                if _receipt_date_utc(receipt.get("timestamp")) != today:
                    continue
                cost = receipt.get("cost_usd")
                if cost is None:
                    # Reported-zero-because-unknown vs genuinely-free is not
                    # distinguishable from the ledger alone (module docstring)
                    # — count it as 0.0, the best-available reading, rather
                    # than raising or dropping the receipt from the scan.
                    continue
                try:
                    total += float(cost)
                except (TypeError, ValueError):
                    continue
    except OSError as exc:
        logger.warning("paid_lane_budget: failed to read %s: %s", receipts_path, exc)
        return total
    return round(total, 8)


def is_budget_exhausted(provider: Optional[str], state_dir: Optional[Path] = None) -> bool:
    """True when `provider` is a paid lane AND today's cumulative spend for
    paid lanes already meets/exceeds the daily budget. Always False for a
    non-paid-lane provider (mirrors receipt_classifier: a budget of 0 or
    below means "always exhausted", fail-closed).
    """
    if not is_paid_lane(provider):
        return False
    budget = get_daily_budget_usd()
    if budget <= 0:
        return True
    spent = spent_today_usd(state_dir or default_state_dir())
    return spent >= budget


def enforce_daily_budget(provider: Optional[str], state_dir: Optional[Path] = None) -> None:
    """Raise PaidLaneBudgetExceededError when the daily cap is already spent
    for a paid-lane `provider`. No-op for non-paid-lane providers.

    Honors an explicit operator override (ENV_OVERRIDE=1), logged loudly —
    the same escape-hatch convention every other blocking constraint in this
    fabric uses (see provider_constraints.md), never a silent bypass.
    """
    if not is_paid_lane(provider):
        return
    budget = get_daily_budget_usd()
    resolved_dir = state_dir or default_state_dir()
    spent = spent_today_usd(resolved_dir)
    exhausted = budget <= 0 or spent >= budget
    if not exhausted:
        return
    if os.environ.get(ENV_OVERRIDE) == "1":
        logger.warning(
            "paid_lane_budget: OVERRIDDEN provider=%s spent_today_usd=%.6f "
            "daily_budget_usd=%.6f (%s=1)",
            provider, spent, budget, ENV_OVERRIDE,
        )
        return
    raise PaidLaneBudgetExceededError(provider, spent, budget)
