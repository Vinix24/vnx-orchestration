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

Builder runs come here through :func:`record_builder_run` (K2a). Since K4b-1 a
classifier call on the ``deepseek`` and ``haiku`` providers also books its one
line, through :func:`record_classifier_run`: ``role`` ``intelligence``, producer
``classifier``, the call's session id as its ref. A gate seat, a plan-gate seat
or a panel leg on the same lanes (:data:`NOT_BUILDER_ROLES`) keeps the old
``emit_provider_cost`` event until K3/K4a move it. A line in the ledger's spool
is a written line (ADR-043 §6, ``record.landed``). :func:`record_builder_run`
never catches the ledger's errors (``CostLedgerWriteError``, ``CostLedgerRefusal``):
they reach its caller, which must not write its receipt with a cost it does not
have (ADR-005, ADR-043). :func:`record_classifier_run` is the one caller that
catches them, because a classifier call returns its classification whatever the
ledger did, with no amount and the reason in the result.

K2b adds the two lanes that report their own usage instead of leaving a claude
transcript: ``codex`` and ``kimi``, as builder and as gate. They book through
:func:`record_codex_run` / :func:`record_kimi_run` (one function per provider
kind, both behind :func:`record_cli_run`), from a :class:`CliRun` that
:func:`cli_run` builds from whichever usage source the measured end holds: a
builder's own ``token_usage`` dict, a codex gate's ``execution_depth`` (no cache
fields, so the line is ``partial:``), or a governed seat's report, whose
frontmatter usage counts only when the lane stamped ``token_usage_measured:
true`` — a lane that reports nothing (kimi-cli) books
``unknown:kimi_cli_no_usage`` / ``unavailable:kimi_cli_no_usage``. Both providers
bill flat (ChatGPT login, CLI OAuth), so the registry price is the
API-equivalent list price (``billing: subscription``, ``api_equivalent_usd``).
The bookers are fail-open: a refusal is logged with the ref, the provider and
the reason and the run goes on — a gate verdict or a build is never the
ledger's to fail.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import cost_ledger
import provider_costs
from reviewer_roles import REVIEWER_ROLES
from token_harvest import (
    CLAUDE_HARNESS_PROVIDERS,
    LEDGER_TOKEN_FIELDS,
    MessageUsage,
    harvest_session_messages,
)
from unified_report_schema import SchemaViolation, parse_frontmatter

logger = logging.getLogger(__name__)

BUILDER_ROLE = "builder"
# A classifier call on a harness lane (K4b-1): role ``intelligence``, no dispatch, the session id as ref.
INTELLIGENCE_ROLE = "intelligence"
CLASSIFIER_PRODUCER = "classifier"
# A gate run (K2b): the review seat's own model call, booked by the gate that ends it.
GATE_ROLE = "review"

# The native claude lane runs on the Max subscription (marginal cost 0, list
# price in api_equivalent_usd); the deepseek and glm harness lanes run on an
# own API key, so every token is real money.
BILLING_BY_PROVIDER: Mapping[str, str] = {
    "claude": "subscription",
    "deepseek-harness": "metered",
    "glm-harness": "metered",
}

# K2b: the two lanes that report their own usage (codex `exec --json`, kimi
# stream-json + its post-run wire harvest). Both bill flat, so every token also
# has a list price in the registry, booked as ``api_equivalent_usd`` (ADR-043).
CODEX_PROVIDER = "codex"
KIMI_PROVIDER = "kimi"
CLI_BILLING: Mapping[str, str] = {CODEX_PROVIDER: "subscription", KIMI_PROVIDER: "subscription"}
# kimi-cli 1.46.0's stream carries no usage accounting (ADR-043 §8's documented
# exception): a kimi run that reports nothing says so instead of booking a zero.
KIMI_NO_USAGE_REASON = "kimi_cli_no_usage"

# Dispatch roles on these lanes that are not builder runs: review-gate seats
# and plan-gate seats (K3) and deliberation-panel legs (K4a).
NOT_BUILDER_ROLES = frozenset(REVIEWER_ROLES | {"deliberation-panelist"})

SPAWN_REFUSED_SOURCE = "unavailable:spawn_refused"
NO_TRANSCRIPT_REASON = "no_transcript"
NO_USAGE_FIELDS_REASON = "no_usage_fields"


def writes_builder_run(provider: Optional[str], role: Optional[str]) -> bool:
    """True when this run's cost goes to the ledger as a builder run (K2a-2), not the old event."""
    return provider in CLAUDE_HARNESS_PROVIDERS and (role or "") not in NOT_BUILDER_ROLES


def books_cli_run(provider: Optional[str], role: Optional[str]) -> bool:
    """True when this codex/kimi run books its own ledger line as a BUILDER run (K2b).

    A review-gate seat, plan-gate seat or panel leg is not a builder: its gate
    books the run when the gate ends (K2b's gate half), and the seat keeps the
    old ``emit_provider_cost`` event until K3/K4a move it.
    """
    return provider in CLI_BILLING and (role or "") not in NOT_BUILDER_ROLES


# A plan model that names no model on the redirected harness lanes: the door's
# placeholder, the claude default, or nothing. The lane then runs its own default.
_LANE_PLACEHOLDERS = frozenset({"default", "sonnet", ""})


def lane_model(provider: str, requested: Optional[str]) -> str:
    """The model a lane runs for the model a plan requested.

    ``deepseek-harness`` and ``glm-harness`` resolve a placeholder to their own
    default (``resolve_harness_model``: the ``VNX_*_HARNESS_MODEL`` override,
    then the registry default); ``codex`` and ``kimi`` resolve one through the
    same registry resolvers their spawns use (ADR-036); any other value runs as
    given. The lane spawns with this model, and a spawn refused before the lane
    resolved one is booked with it too. ``claude`` runs what was requested. A
    resolver that cannot name a model returns the value as it came, so the
    ledger refuses an identity it cannot verify instead of guessing one.
    """
    value = (requested or "").strip()
    placeholder = None if value in _LANE_PLACEHOLDERS else value
    if provider == "deepseek-harness":
        from provider_spawns.deepseek_harness_spawn import resolve_harness_model  # noqa: PLC0415
    elif provider == "glm-harness":
        from provider_spawns.glm_harness_spawn import resolve_harness_model  # noqa: PLC0415
    elif provider == CODEX_PROVIDER:
        return _codex_lane_model(placeholder, value)
    elif provider == KIMI_PROVIDER:
        return _kimi_lane_model(placeholder, value)
    else:
        return value
    return resolve_harness_model(placeholder)


def _codex_lane_model(placeholder: Optional[str], value: str) -> str:
    """The codex model a plan's value names: the registry default for a placeholder (OI-2062's one resolver)."""
    from providers import provider_registry  # noqa: PLC0415

    try:
        return provider_registry.resolve_codex_model(explicit=placeholder)
    except provider_registry.CodexModelResolutionError:
        return value


def _kimi_lane_model(placeholder: Optional[str], value: str) -> str:
    """The kimi model a plan's value names, through the lane's own resolver (``kimi-k3`` by default)."""
    from provider_dispatch import _kimi_resolve_requested_key  # noqa: PLC0415

    return _kimi_resolve_requested_key(placeholder) or value


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


def _window_fields(started: datetime, ended: datetime) -> Dict[str, Any]:
    return {"occurred_from": _iso(started), "occurred_to": _iso(ended)}


def _refused_fields(started: datetime, ended: datetime) -> Dict[str, Any]:
    """A spawn the lane refused: five confirmed zeros, ``complete``, no cost, and the reason."""
    return {
        "tokens": dict.fromkeys(LEDGER_TOKEN_FIELDS, 0), "usage_completeness": "complete",
        "cost_usd": 0.0, "cost_source": SPAWN_REFUSED_SOURCE, **_window_fields(started, ended),
    }


def _unknown_fields(started: datetime, ended: datetime, reason: str) -> Dict[str, Any]:
    """A run whose usage nobody reported: five nulls, ``unknown:<reason>``, no amount."""
    return {
        "tokens": dict.fromkeys(LEDGER_TOKEN_FIELDS), "usage_completeness": f"unknown:{reason}",
        "cost_source": f"unavailable:{reason}", **_window_fields(started, ended),
    }


def _no_transcript_fields(run: BuilderRun) -> Dict[str, Any]:
    return _unknown_fields(run.started, run.ended, NO_TRANSCRIPT_REASON)


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
        return _refused_fields(run.started, run.ended)
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
    role: str = BUILDER_ROLE,
    ledger_path: Optional[Path] = None,
    claude_projects_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Write the ledger line of one run on a harness lane; return the record as written.

    The ``run_id`` is ``<project_id>:<producer>:<dispatch_id>:<attempt>``, the
    attempt the next one the ledger holds for that producer and dispatch
    (``cost_ledger.record_next_attempt``), so a refire is a new run. ``role``
    defaults to :data:`BUILDER_ROLE`, so every K2a call site books a builder
    run; K4b-1 passes :data:`INTELLIGENCE_ROLE` for a classifier call. It must
    be one of ``cost_ledger.ROLES``: an unknown role is refused here, before
    anything is written. Raises what the ledger raises; nothing here catches
    it.
    """
    if run.provider not in BILLING_BY_PROVIDER:
        raise cost_ledger.InvalidRecord(f"provider {run.provider!r} is not a claude harness lane")
    if role not in cost_ledger.ROLES:
        raise cost_ledger.InvalidRecord(
            f"role {role!r} is not a ledger role ({', '.join(sorted(cost_ledger.ROLES))})"
        )
    fields = run_fields(run, claude_projects_dir=claude_projects_dir)
    if fields.get("cost_source") is not None:
        logger.info(
            "harness_run_cost: dispatch=%s provider=%s booked as %s (%s)",
            dispatch_id, run.provider, fields["cost_source"], run.spawn_refused or "no transcript",
        )

    def make_run(run_id: str) -> cost_ledger.RunCost:
        return cost_ledger.RunCost(
            run_id=run_id, project_id=project_id, role=role, provider=run.provider,
            model=run.model, billing=BILLING_BY_PROVIDER[run.provider], **fields,
        )

    return cost_ledger.record_next_attempt(project_id, producer, dispatch_id, make_run, ledger_path=ledger_path)


def record_classifier_run(
    *, provider: str, model: str, session_id: str, started: datetime, ended: datetime,
    project_id: Optional[str] = None, spawn_refused: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Book one classifier call on ``provider`` as an ``intelligence`` run.

    ``(record, None)`` when the line landed, in the ledger or its spool, and
    ``(None, reason)`` when the ledger refused the record or could not write it
    (a WARNING names the session id and the reason). A classifier call always
    returns its classification, so the ledger's errors never reach it and no
    amount is invented. The run has no dispatch: the session id is its ref.
    """
    run = BuilderRun(
        provider=provider, model=model, started=started, ended=ended,
        session_id=session_id, spawn_refused=spawn_refused,
    )
    try:
        record = record_builder_run(
            run, project_id=project_id or ledger_project_id(None),
            producer=CLASSIFIER_PRODUCER, dispatch_id=session_id, role=INTELLIGENCE_ROLE,
        )
    except (cost_ledger.CostLedgerRefusal, cost_ledger.CostLedgerWriteError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning("harness_run_cost: classifier session %s not booked: %s", session_id, reason)
        return None, reason
    return record, None


# ---------------------------------------------------------------------------
# K2b: the codex and kimi lanes, as builder and as gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CliRun:
    """A codex/kimi run as it reports itself: its ``token_usage`` (``None`` = reported nothing), or a refusal."""

    provider: str
    model: str
    started: datetime
    ended: datetime
    token_usage: Optional[Mapping[str, Any]] = None
    spawn_refused: Optional[str] = None


def _token_count(value: object) -> Optional[int]:
    """A reported count, or ``None`` — a missing, negative or non-integer count is not a zero."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _first_count(usage: Mapping[str, Any], *names: str) -> Optional[int]:
    """The first of *names* that carries a count: the raw shape first, then the receipt-normalized one."""
    for name in names:
        counted = _token_count(usage.get(name))
        if counted is not None:
            return counted
    return None


def _cli_tokens(usage: Optional[Mapping[str, Any]]) -> Dict[str, Optional[int]]:
    """The five ledger fields of a lane's own ``token_usage``; five ``None`` when it reported nothing.

    Neither lane splits cache writes, so a flat ``cache_creation_tokens`` books
    under 5m and 0 under 1h (as ``token_harvest`` does) and a lane reporting none
    leaves both ``None``, which makes the line ``partial:``. Both dict shapes
    reach here as in ``provider_dispatch._extract_token_usage``, whose
    ``unavailable: True`` marker for "reported nothing" is honoured as five
    ``None``, so a placeholder 0 is never read as a measured zero.
    """
    if not isinstance(usage, Mapping) or usage.get("unavailable") is True:
        return dict.fromkeys(LEDGER_TOKEN_FIELDS)
    written = _token_count(usage.get("cache_creation_tokens"))
    return {
        "input_uncached": _first_count(usage, "input_tokens", "input"),
        "cache_hit": _first_count(usage, "cache_read_tokens", "cache_read", "cache_hit"),
        "cache_write_5m": written,
        "cache_write_1h": 0 if written is not None else None,
        "output": _first_count(usage, "output_tokens", "output"),
    }


def cli_run(
    *, provider: str, model: str, started: datetime, ended: datetime,
    token_usage: Optional[Mapping[str, Any]] = None, depth: Any = None,
    report_text: Optional[str] = None, spawn_refused: Optional[str] = None,
) -> CliRun:
    """One codex/kimi run, from whichever usage source its measured end holds (K2b).

    ``token_usage`` is a builder's own dict (codex's final ``token_count``,
    kimi's ``wire.jsonl`` harvest); ``depth`` the codex gate's
    ``ExecutionDepth`` — input and output only, hence ``partial:`` on the cache
    fields; ``report_text`` a governed-lane seat's report, read only when its
    frontmatter stamped ``token_usage_measured`` (OI-1291; kimi-cli 1.46.0's
    stream carries no usage at all). No source, or one that measured nothing,
    stays ``None`` and books the honest unknown, never a zero.
    """
    if isinstance(token_usage, Mapping):
        usage: Optional[Mapping[str, Any]] = token_usage
    elif depth is not None:
        usage = _depth_usage(depth)
    elif report_text is not None:
        usage = _report_usage(report_text)
    else:
        usage = None
    return CliRun(provider=provider, model=model, started=started, ended=ended,
                  token_usage=usage, spawn_refused=spawn_refused)


def _depth_usage(depth: Any) -> Optional[Dict[str, int]]:
    """The gate's parsed depth as usage, or ``None``: ``ExecutionDepth`` uses 0 for "nothing" and "not measured" alike."""
    if not getattr(depth, "parsed", False):
        return None
    counted = {
        "input_tokens": int(getattr(depth, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(depth, "output_tokens", 0) or 0),
    }
    return counted if any(counted.values()) else None


def _report_usage(report_text: str) -> Optional[Mapping[str, Any]]:
    """The frontmatter's ``token_usage`` in the lane's own key shape, or ``None`` when it is not a measurement."""
    try:
        frontmatter = parse_frontmatter(report_text or "")
    except SchemaViolation:
        return None
    usage = frontmatter.get("token_usage")
    if not frontmatter.get("token_usage_measured") or not isinstance(usage, Mapping):
        return None
    measured: Dict[str, Any] = {
        "input_tokens": usage.get("input"),
        "output_tokens": usage.get("output"),
        "cache_read_tokens": usage.get("cache_read"),
    }
    if usage.get("cache_write") is not None:
        measured["cache_creation_tokens"] = usage.get("cache_write")
    if not any(_token_count(value) for value in measured.values()):
        # An UNMEASURED report carries the all-zero shape (kimi stamps zeros
        # beside ``token_usage_measured: false``); a measured-looking line of
        # zeros would price a real run at nothing.
        return None
    return measured


def _no_usage_reason(provider: str) -> str:
    """kimi-cli's documented no-accounting shape gets its own reason; any other lane the generic one."""
    return KIMI_NO_USAGE_REASON if provider == KIMI_PROVIDER else NO_USAGE_FIELDS_REASON


def cli_run_fields(run: CliRun) -> Dict[str, Any]:
    """The usage part of a CLI run's :class:`cost_ledger.RunCost`: the refusal, the numbers, or the unknown."""
    if isinstance(run.spawn_refused, str) and run.spawn_refused:
        return _refused_fields(run.started, run.ended)
    tokens = _cli_tokens(run.token_usage)
    if all(tokens[name] is None for name in LEDGER_TOKEN_FIELDS):
        return _unknown_fields(run.started, run.ended, _no_usage_reason(run.provider))
    return {"tokens": tokens, "usage_completeness": usage_completeness(tokens),
            **_window_fields(run.started, run.ended)}


def _book_cli_run(
    run: CliRun, provider: str, *, project_id: str, producer: str, ref: str,
    role: str, ledger_path: Optional[Path],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Write one codex/kimi line, and never let the ledger fail the run (K2b).

    ``(record, None)`` when the line landed, in the ledger or its spool, and
    ``(None, reason)`` when it was refused; either way the caller goes on,
    because a gate verdict and a build are never the ledger's to fail. Never
    silent: each refusal is a WARNING naming the ref (dispatch id or gate run
    id), the provider, the role and the reason. The opposite of
    :func:`record_builder_run`, which propagates.

    ``OSError`` is caught beside the ledger's own errors because
    :func:`cost_ledger.record_next_attempt` deliberately propagates the
    registry/ledger read error unchanged (a ``PermissionError`` on a store the
    process may not read): booking runs before a gate records its verdict, so an
    escaping ``OSError`` would cost the verdict itself. ``BaseException`` stays
    uncaught — a ``KeyboardInterrupt`` is not a booking failure.
    """
    if run.provider != provider:
        raise cost_ledger.InvalidRecord(f"a {provider} run cannot book provider {run.provider!r}")
    if role not in cost_ledger.ROLES:
        raise cost_ledger.InvalidRecord(f"role {role!r} is not a ledger role ({', '.join(sorted(cost_ledger.ROLES))})")
    fields = cli_run_fields(run)

    def make_run(run_id: str) -> cost_ledger.RunCost:
        return cost_ledger.RunCost(
            run_id=run_id, project_id=project_id, role=role, provider=provider,
            model=run.model, billing=CLI_BILLING[provider], **fields,
        )

    try:
        record = cost_ledger.record_next_attempt(project_id, producer, ref, make_run, ledger_path=ledger_path)
    except (cost_ledger.CostLedgerRefusal, cost_ledger.CostLedgerWriteError, OSError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "harness_run_cost: %s %s run %s not booked (%s): %s",
            provider, role, ref, fields.get("cost_source") or fields["usage_completeness"], reason,
        )
        return None, reason
    return record, None


def record_codex_run(
    run: CliRun, *, project_id: str, producer: str, ref: str, role: str = GATE_ROLE,
    ledger_path: Optional[Path] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Book one codex run, as its gate or its builder, fail-open (K2b).

    Codex bills flat on the ChatGPT login, so the line is ``billing: subscription``
    and the registry price lands in ``api_equivalent_usd`` (ADR-043). ``ref`` is
    the dispatch id, or the gate run id for a gate attempt.
    """
    return _book_cli_run(run, CODEX_PROVIDER, project_id=project_id, producer=producer, ref=ref,
                         role=role, ledger_path=ledger_path)


def record_kimi_run(
    run: CliRun, *, project_id: str, producer: str, ref: str, role: str = GATE_ROLE,
    ledger_path: Optional[Path] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Book one kimi run, as its gate or its builder, fail-open (K2b).

    Same contract as :func:`record_codex_run` (kimi bills flat on CLI OAuth); a
    run the CLI reported no usage for books ``unavailable:kimi_cli_no_usage``.
    """
    return _book_cli_run(run, KIMI_PROVIDER, project_id=project_id, producer=producer, ref=ref,
                         role=role, ledger_path=ledger_path)


CLI_BOOKERS: Mapping[str, Callable[..., Tuple[Optional[Dict[str, Any]], Optional[str]]]] = {
    CODEX_PROVIDER: record_codex_run,
    KIMI_PROVIDER: record_kimi_run,
}


def book_cli_builder(
    *, provider: str, model_requested: Optional[str], started: datetime, ended: datetime,
    token_usage: Optional[Mapping[str, Any]] = None, spawn_refused: Optional[str] = None,
    project_id: str, producer: str, ref: str, ledger_path: Optional[Path],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Book a builder run on a codex/kimi lane from the usage its own spawn reported (K2b).

    One of the two measured ends of a CLI builder run: the provider lane
    (``provider_dispatch._book_dispatch_cost``) and the envelope lane
    (``envelope_govern._book_ledger_run``) both end holding the run's
    ``token_usage``, and both book through here so the model identity comes from
    :func:`lane_model`'s registry resolution (ADR-036) in one place.
    """
    run = cli_run(provider=provider, model=lane_model(provider, model_requested), started=started,
                  ended=ended, token_usage=token_usage, spawn_refused=spawn_refused)
    return record_cli_run(run, project_id=project_id, producer=producer, ref=ref,
                          role=BUILDER_ROLE, ledger_path=ledger_path)


def record_cli_run(
    run: CliRun, *, project_id: str, producer: str, ref: str, role: str = GATE_ROLE,
    ledger_path: Optional[Path] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The one door the measured ends call: the provider's own booker, fail-open either way."""
    booker = CLI_BOOKERS.get(run.provider)
    if booker is None:
        raise cost_ledger.InvalidRecord(f"provider {run.provider!r} books no CLI run (codex, kimi)")
    return booker(run, project_id=project_id, producer=producer, ref=ref, role=role, ledger_path=ledger_path)


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
    "BILLING_BY_PROVIDER", "BUILDER_ROLE", "BuilderRun", "CLASSIFIER_PRODUCER", "CLI_BILLING", "CliRun",
    "CODEX_PROVIDER", "GATE_ROLE", "INTELLIGENCE_ROLE", "KIMI_NO_USAGE_REASON", "KIMI_PROVIDER",
    "NOT_BUILDER_ROLES", "SPAWN_REFUSED_SOURCE", "book_cli_builder", "books_cli_run", "cli_run",
    "cli_run_fields",
    "lane_model", "ledger_project_id", "price_segments",
    "receipt_cost", "record_builder_run", "record_classifier_run", "record_cli_run", "record_codex_run",
    "record_kimi_run", "run_fields", "run_tokens", "usage_completeness", "writes_builder_run",
]
