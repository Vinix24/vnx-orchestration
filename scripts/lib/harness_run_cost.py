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
transcript: ``codex`` and ``kimi``, as builder AND as gate. They book through
:func:`record_codex_run` / :func:`record_kimi_run` (one function per provider
kind, both behind :func:`record_cli_run`), from a :class:`CliRun` that
:func:`cli_run` builds from whichever usage source the measured end holds: a
builder's own ``token_usage`` dict, a codex gate's ``execution_depth`` (input,
output and — when the stream carried it — the cached part of the input), or a
governed seat's report, whose frontmatter usage counts only when the lane
stamped ``token_usage_measured: true`` — a lane that reports nothing (kimi-cli)
books ``unknown:kimi_cli_no_usage`` / ``unavailable:kimi_cli_no_usage``. The
usage is then normalised per provider (``_codex_tokens``,
``_kimi_tokens``) before it is mapped onto the ledger fields: codex's input
count includes the cached part and kimi's does not, so one shared mapping would
double-count every cached codex token (OI-2407). A provider whose split was
never measured, or a report that carries no cached count, books
``input_uncached`` as ``None`` with the reason written down — never the total,
never a negative. Both providers bill flat (ChatGPT login, CLI OAuth), so the
registry price is the API-equivalent list price (``billing: subscription``,
``api_equivalent_usd``). The bookers are fail-open: a refusal is logged with
the ref, the provider and the reason and the run goes on — a gate verdict or a
build is never the ledger's to fail. A gate seat on either lane is booked by
the gate that ends it (``gate_runner`` for codex, ``governed_lane`` for kimi),
under role ``review``; that seat books no builder line, so one run is one line.
"""

from __future__ import annotations

import logging
import os
import re
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

# The reasons an input count could not be split into its uncached and cached
# parts (OI-2407). A provider this module has no measurement for never defaults
# to "the whole input is uncached": the field stays None, the line says
# ``partial:`` and the booking writes the reason down. ``INVALID`` is the
# contradiction case — a cached count larger than the input it is part of — and
# books no negative.
INPUT_CACHE_SPLIT_UNMEASURED = "input_cache_split_unmeasured"
INPUT_CACHE_SPLIT_INVALID = "input_cache_split_invalid"

# Dispatch roles on these lanes that are not builder runs: review-gate seats
# and plan-gate seats (K3) and deliberation-panel legs (K4a).
NOT_BUILDER_ROLES = frozenset(REVIEWER_ROLES | {"deliberation-panelist"})

SPAWN_REFUSED_REASON = "spawn_refused"
SPAWN_REFUSED_SOURCE = f"unavailable:{SPAWN_REFUSED_REASON}"
NO_TRANSCRIPT_REASON = "no_transcript"
NO_USAGE_FIELDS_REASON = "no_usage_fields"

# K3-1a: the three ends an attempt that is not booked at its lane's measured end
# can have. ``refused`` is an attempt the door provably did not start (zero
# tokens, ``complete``, as ADR-043 defines a refused spawn); ``ran`` is an
# attempt that started but whose usage nobody reported (unknown, never zero);
# ``never_started`` is an attempt that never reached the door at all, which is
# not a run and books nothing.
ATTEMPT_REFUSED = "refused"
ATTEMPT_RAN = "ran"
ATTEMPT_NEVER_STARTED = "never_started"
ATTEMPT_OUTCOMES = frozenset({ATTEMPT_REFUSED, ATTEMPT_RAN, ATTEMPT_NEVER_STARTED})

# The lane billing of every provider K3 books an attempt for: the claude harness
# lanes, the two own-key harness lanes, and the two flat CLI lanes K2b measures.
LANE_BILLING: Mapping[str, str] = {**BILLING_BY_PROVIDER, **CLI_BILLING}

# The door's own refusal markers, in the order the reason is preferred: the
# specific budget refusal outranks the generic delivery refusal it is carried in.
_DOOR_REFUSAL_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("paid_lane_daily_budget_exceeded", "paid_lane_daily_budget_exceeded"),
    ("delivery_refused", "delivery_refused"),
    ("spawn_refused", SPAWN_REFUSED_REASON),
    ("lane_exhausted", "lane_exhausted"),
)
_REJECT_CODE_RE = re.compile(r"REJECT \[([A-Za-z0-9][A-Za-z0-9._-]{0,63})\]")
_REASON_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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
    """The project of the run: the STORE's own, else ``VNX_PROJECT_ID``, else ``vnx-dev``.

    The store decides first (ADR-007): a store under ``.vnx-data/<project>/state``
    names its project by its own layout
    (``project_id_migration.project_id_from_store_path``), and that answer wins
    over the ambient ``VNX_PROJECT_ID`` — a line written into tenant-b's ledger
    belongs to tenant-b, whatever tenant the writing process was started for
    (K3 round 3). Only a store whose path names no project falls through to the
    marker/env resolution and, when even that resolves nothing, to the env and
    ``vnx-dev``: a cost record is never skipped for want of a tenant.
    """
    from project_id_migration import project_id_from_store_path  # noqa: PLC0415
    from project_scope import TenantUnresolved, resolve_stamp_project_id  # noqa: PLC0415

    if state_dir is not None:
        named = project_id_from_store_path(Path(state_dir) / "quality_intelligence.db")
        if named:
            return named
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


def transcript_identity(provider: str, session_id: str) -> str:
    """The ledger's transcript identity ``"<provider>:<session_id>"`` (K6a, OI-2419).

    The one definition of the format: a harness run's line names the session
    transcript it was read from with this string, and a later backfill matches
    that string before it may supersede the line. Keep the reader on this
    function rather than re-deriving the shape.
    """
    return f"{provider}:{session_id}"


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

    A run that knows its ``session_id`` names that session on its line as
    ``cost_ledger.TRANSCRIPT_IDENTITY`` (:func:`transcript_identity`), so the
    line can be matched to its transcript without a guess (K6a). Existing
    ledger lines are never rewritten and are not migrated: a line written
    before this field existed keeps its ``record_id``, because an absent field
    does not enter the hash, and it receives an identity only through the
    backfill correction a later K6b-2 supersedes it with.
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
    # ``_session`` drops a blank id: an identity naming no session would be refused by the
    # ledger, and the run's usage must still be booked, so a blank id is no identity at all.
    session = _session(run.session_id)
    identity = transcript_identity(run.provider, session) if session is not None else None

    def make_run(run_id: str) -> cost_ledger.RunCost:
        return cost_ledger.RunCost(
            run_id=run_id, project_id=project_id, role=role, provider=run.provider,
            model=run.model, billing=BILLING_BY_PROVIDER[run.provider],
            transcript_identity=identity, **fields,
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
# K2b-a: the codex and kimi lanes, as builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CliRun:
    """A codex/kimi run as it reports itself: its ``token_usage`` (``None`` = reported nothing), or a refusal.

    ``execution_id`` (K3-2a) is the run's identity, minted by the gate that
    started it (``gate_status.new_execution_id``) before the model call and
    carried onto its result record. It lands on the ledger line so one
    execution is one line, whatever the result slot later does with the
    record (K3-2b). ``None`` books a line without an id, which is what a
    caller that knows no run identity (a builder leg outside the gate) does.
    """

    provider: str
    model: str
    started: datetime
    ended: datetime
    token_usage: Optional[Mapping[str, Any]] = None
    spawn_refused: Optional[str] = None
    execution_id: Optional[str] = None


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


def _written_fields(usage: Mapping[str, Any]) -> Dict[str, Optional[int]]:
    """The cache-write pair both lanes share: one flat count under 5m and 0 under 1h (as ``token_harvest``)."""
    written = _token_count(usage.get("cache_creation_tokens"))
    return {"cache_write_5m": written, "cache_write_1h": 0 if written is not None else None}


def _codex_tokens(usage: Mapping[str, Any]) -> Tuple[Dict[str, Optional[int]], Optional[str]]:
    """The five ledger fields of a codex ``token_usage``, and the reason a split stayed unknown.

    MEASURED SOURCE — a codex input count INCLUDES the cached part. One
    ``turn.completed`` in ``tests/fixtures/gate_verdict/
    pr-1869-codex_gate-bare-verdict.ndjson`` (line 7) reports
    ``input_tokens: 40939`` beside ``cached_input_tokens: 33536`` for the same
    call, and ``codex_spawn._normalize_token_count`` carries that pair through
    as ``input_tokens``/``cache_read_tokens``. The uncached part is therefore
    ``input_tokens - cache_read_tokens``; booking the total as
    ``input_uncached`` prices every cached token twice (OI-2407).

    A codex report with no cached count at all leaves the split unmeasured:
    ``input_uncached`` stays ``None`` with ``input_cache_split_unmeasured``, and
    a cached count larger than the input it is part of books no negative but
    ``None`` with ``input_cache_split_invalid``.
    """
    cache_read = _first_count(usage, "cached_input_tokens", "cache_read_tokens", "cache_read", "cache_hit")
    fields: Dict[str, Optional[int]] = {
        "input_uncached": None,
        "cache_hit": cache_read,
        "output": _first_count(usage, "output_tokens", "output"),
        **_written_fields(usage),
    }
    total_input = _first_count(usage, "input_tokens", "input")
    if total_input is None:
        # No input count to split: the null field itself says so, no split reason.
        return fields, None
    if cache_read is None:
        return fields, INPUT_CACHE_SPLIT_UNMEASURED
    if cache_read > total_input:
        return fields, INPUT_CACHE_SPLIT_INVALID
    fields["input_uncached"] = total_input - cache_read
    return fields, None


def _kimi_tokens(usage: Mapping[str, Any]) -> Tuple[Dict[str, Optional[int]], Optional[str]]:
    """The five ledger fields of a kimi ``token_usage`` (no split reason: kimi needs none).

    MEASURED SOURCE — a kimi input count EXCLUDES the cached part. The
    ``wire.jsonl`` ``StatusUpdate`` fixture
    (``tests/test_kimi_token_capture.py::_status_update``) records
    ``input_other`` next to ``input_cache_read``, and
    ``kimi_spawn._parse_wire_token_usage`` sums ``input_other`` into
    ``input_tokens`` while keeping the cache read as its own cumulative count.
    So the count as reported IS the uncached part, and a cache read larger than
    it is normal rather than contradictory (a step can read more context from
    cache than it writes as new tokens), so this provider never needs the
    negative-count guard.
    """
    return {
        "input_uncached": _first_count(usage, "input_tokens", "input"),
        "cache_hit": _first_count(usage, "cache_read_tokens", "cache_read", "cache_hit"),
        "output": _first_count(usage, "output_tokens", "output"),
        **_written_fields(usage),
    }, None


def _unmeasured_provider_tokens(usage: Mapping[str, Any]) -> Tuple[Dict[str, Optional[int]], Optional[str]]:
    """A provider whose input/cache split this module has never measured: no default "input means uncached".

    codex and kimi are the measured ones. Any other provider's cache count (when
    it reports one) is booked as reported, but ``input_uncached`` stays ``None``
    with ``input_cache_split_unmeasured``: guessing that its input excludes the
    cache is exactly the double count OI-2407 removes.
    """
    return {
        "input_uncached": None,
        "cache_hit": _first_count(
            usage, "cache_read_input_tokens", "cached_input_tokens", "cache_read_tokens", "cache_read", "cache_hit",
        ),
        "output": _first_count(usage, "output_tokens", "output"),
        **_written_fields(usage),
    }, INPUT_CACHE_SPLIT_UNMEASURED


# One normaliser per provider (OI-2407 acceptance 2); a provider not listed here
# gets the unmeasured default, never a guess. Both dict shapes reach the
# normalisers as in ``provider_dispatch._extract_token_usage`` — the spawn's own
# shape (``input_tokens``/``cache_read_tokens``) and the receipt-normalized one
# (``input``/``cache_read``/``cache_hit``) — so each reads both.
_CLI_TOKEN_NORMALIZERS: Mapping[
    str, Callable[[Mapping[str, Any]], Tuple[Dict[str, Optional[int]], Optional[str]]]
] = {
    CODEX_PROVIDER: _codex_tokens,
    KIMI_PROVIDER: _kimi_tokens,
}


def _cli_tokens(
    usage: Optional[Mapping[str, Any]], provider: str,
) -> Tuple[Dict[str, Optional[int]], Optional[str]]:
    """The five ledger fields of a lane's own ``token_usage``, normalised for *provider*.

    Returns the fields and the reason ``input_uncached`` stayed ``None`` because
    the provider's input/cache split could not be applied (``None`` when there
    is nothing to say). Five ``None`` when the lane reported nothing — both
    ``provider_dispatch._extract_token_usage`` and the spawns mark "reported
    nothing" with ``unavailable: True``, which is honoured so a placeholder 0
    is never read as a measured zero.
    """
    if not isinstance(usage, Mapping) or usage.get("unavailable") is True:
        return dict.fromkeys(LEDGER_TOKEN_FIELDS), None
    normalizer = _CLI_TOKEN_NORMALIZERS.get(provider, _unmeasured_provider_tokens)
    return normalizer(usage)


def cli_run(
    *, provider: str, model: str, started: datetime, ended: datetime,
    token_usage: Optional[Mapping[str, Any]] = None, depth: Any = None,
    report_text: Optional[str] = None, spawn_refused: Optional[str] = None,
    execution_id: Optional[str] = None,
) -> CliRun:
    """One codex/kimi run, from whichever usage source its measured end holds (K2b).

    ``token_usage`` is a builder's own dict (codex's final ``token_count``,
    kimi's ``wire.jsonl`` harvest); ``depth`` the codex gate's
    ``ExecutionDepth`` — input and output only, hence ``partial:`` on the cache
    fields; ``report_text`` a governed-lane seat's report, read only when its
    frontmatter stamped ``token_usage_measured`` (OI-1291; kimi-cli 1.46.0's
    stream carries no usage at all). No source, or one that measured nothing,
    stays ``None`` and books the honest unknown, never a zero. ``spawn_refused``
    names an attempt the lane refused before anything ran. ``execution_id``
    (K3-2a) is the gate run's own identity; it is written on the line as-is,
    never re-minted here.
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
                  token_usage=usage, spawn_refused=spawn_refused, execution_id=execution_id)


def _depth_usage(depth: Any) -> Optional[Dict[str, Any]]:
    """The gate's parsed depth as usage, or ``None``: ``ExecutionDepth`` uses 0 for "nothing" and "not measured" alike.

    The cached part travels only when the gate's stream reported it (OI-2407):
    ``ExecutionDepth.cache_read_tokens`` is then an int and the codex normaliser
    subtracts it. Without it the key is absent, which the normaliser reads as an
    unmeasured split — the gate's total input is never booked as uncached.
    """
    if not getattr(depth, "parsed", False):
        return None
    counted: Dict[str, Any] = {
        "input_tokens": _token_count(getattr(depth, "input_tokens", None)) or 0,
        "output_tokens": _token_count(getattr(depth, "output_tokens", None)) or 0,
    }
    cached = _token_count(getattr(depth, "cache_read_tokens", None))
    if cached is not None:
        counted["cached_input_tokens"] = cached
    return counted if counted["input_tokens"] or counted["output_tokens"] else None


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


def _cli_fields(run: CliRun) -> Tuple[Dict[str, Any], Optional[str]]:
    """The usage part of a CLI run's :class:`cost_ledger.RunCost`, and the split reason when there is one."""
    if isinstance(run.spawn_refused, str) and run.spawn_refused:
        return _refused_fields(run.started, run.ended), None
    tokens, split_reason = _cli_tokens(run.token_usage, run.provider)
    if all(tokens[name] is None for name in LEDGER_TOKEN_FIELDS):
        return _unknown_fields(run.started, run.ended, _no_usage_reason(run.provider)), None
    return {"tokens": tokens, "usage_completeness": usage_completeness(tokens),
            **_window_fields(run.started, run.ended)}, split_reason


def _book_cli_run(
    run: CliRun, provider: str, *, project_id: str, producer: str, ref: str,
    role: str, ledger_path: Optional[Path],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Write one codex/kimi line, and never let the ledger fail the run (K2b).

    ``(record, None)`` when the line landed, in the ledger or its spool, and
    ``(None, reason)`` when it was refused; either way the caller goes on,
    because a build is never the ledger's to fail. Never silent: each refusal is
    a WARNING naming the ref (dispatch id or gate run id), the provider, the
    role and the reason. The opposite of :func:`record_builder_run`, which
    propagates.

    ``OSError`` is caught beside the ledger's own errors because
    :func:`cost_ledger.record_next_attempt` deliberately propagates the
    registry/ledger read error unchanged (a ``PermissionError`` on a store the
    process may not read): an escaping ``OSError`` would cost the caller its
    receipt. ``BaseException`` stays uncaught — a ``KeyboardInterrupt`` is not a
    booking failure.
    """
    if run.provider != provider:
        raise cost_ledger.InvalidRecord(f"a {provider} run cannot book provider {run.provider!r}")
    if role not in cost_ledger.ROLES:
        raise cost_ledger.InvalidRecord(f"role {role!r} is not a ledger role ({', '.join(sorted(cost_ledger.ROLES))})")
    fields, split_reason = _cli_fields(run)
    if split_reason:
        # OI-2407: the field is None and the line says partial:, so the reason is
        # the only place the split's absence is explained. Never silent.
        logger.warning(
            "harness_run_cost: %s %s run %s books input_uncached as unknown (%s)",
            run.provider, role, ref, split_reason,
        )

    def make_run(run_id: str) -> cost_ledger.RunCost:
        return cost_ledger.RunCost(
            run_id=run_id, project_id=project_id, role=role, provider=provider,
            model=run.model, billing=CLI_BILLING[provider],
            # K3-2a: the gate run's own id travels onto the line verbatim.
            execution_id=run.execution_id, **fields,
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
    """Book one kimi run, fail-open (K2b).

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


# ---------------------------------------------------------------------------
# K3-1a: one line per explicit execution identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptRun:
    """One attempt whose END knows that it ended: a gate outcome, a plan-gate seat, a tiebreaker (K3).

    K2b books a codex/kimi run where the lane reports its own usage. Every other
    attempt ends somewhere else, and only that place knows the run is over: this
    says WHO ran (provider and model from the runner, never from the model's
    report text), WHICH execution it was, WHEN, and WHETHER it ran.

    ``execution_id`` is REQUIRED and is the line's identity: one ``uuid4().hex``
    minted where the run is spawned (K3-2a), handed to the booking unchanged.
    The same id books one line however often the record is committed; no slot
    counter is consulted, so a counter that rewinds can neither duplicate a line
    nor swallow one (the K3 round-4 failure, OI-2444). ``reason`` names why a
    refused run was refused or why a started run has no usage.
    """

    provider: str
    model: str
    execution_id: str
    started: datetime
    ended: datetime
    outcome: str = ATTEMPT_RAN
    reason: str = ""
    token_usage: Optional[Mapping[str, Any]] = None


def attempt_reason(value: object, fallback: str) -> str:
    """A reason that can ride ``unknown:<reason>``: a bounded token, or *fallback*.

    The ledger writes the reason into ``usage_completeness``/``cost_source``; a
    door's free-text error message is not a reason but a paragraph, so it is
    never pasted in. An unrecognisable value becomes the caller's own fallback.
    """
    text = str(value or "").strip()
    return text if _REASON_TOKEN_RE.match(text) else fallback


def door_refusal_reason(text: object) -> Optional[str]:
    """The door's refusal code when *text* says a spawn provably never started, else ``None``.

    A refusal at the door (``delivery_refused``, the daily budget, an exhausted
    takeover lane) raises out of the dispatcher with the door's own words in the
    message. That attempt never started, so its line is a confirmed zero. Any
    other raised dispatch is an attempt that DID start and lost its answer: the
    caller books it unknown, never zero.
    """
    message = str(text or "")
    for marker, reason in _DOOR_REFUSAL_MARKERS:
        if marker in message:
            return reason
    refused = _REJECT_CODE_RE.search(message)
    return refused.group(1) if refused else None


def _attempt_fields(run: AttemptRun) -> Tuple[Dict[str, Any], Optional[str]]:
    """The usage part of an attempt's :class:`cost_ledger.RunCost`, and its split reason when there is one.

    ``refused`` is a door that provably never started the run: five confirmed
    zeros, ``complete`` usage, amount 0 and ``unavailable:<reason>`` (ADR-043).
    ``ran`` is a run whose usage nobody reported: five nulls and
    ``unknown:<reason>``, never a zero. ``never_started`` never reaches here —
    :func:`record_attempt_run` books nothing for it.
    """
    if run.outcome == ATTEMPT_REFUSED:
        reason = attempt_reason(run.reason, SPAWN_REFUSED_REASON)
        return {
            "tokens": dict.fromkeys(LEDGER_TOKEN_FIELDS, 0), "usage_completeness": "complete",
            "cost_usd": 0.0, "cost_source": f"unavailable:{reason}", **_window_fields(run.started, run.ended),
        }, None
    tokens, split_reason = _cli_tokens(run.token_usage, run.provider)
    if all(tokens[name] is None for name in LEDGER_TOKEN_FIELDS):
        return _unknown_fields(
            run.started, run.ended, attempt_reason(run.reason, NO_USAGE_FIELDS_REASON),
        ), None
    return {"tokens": tokens, "usage_completeness": usage_completeness(tokens),
            **_window_fields(run.started, run.ended)}, split_reason


def record_attempt_run(
    run: AttemptRun, *, project_id: str, producer: str, ref: str, role: str,
    ledger_path: Optional[Path] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Book one attempt on its execution identity, never letting the ledger fail the run that ended (K3-1a).

    Same fail-open contract as K2b's :func:`_book_cli_run` — ``(record, None)``
    when the line landed (in the ledger or its spool), ``(None, reason)`` when
    it was refused, and never an exception that could cost the caller its gate
    verdict, receipt or request. Every refusal is one WARNING naming the
    producer, the ref, the provider and the reason.

    The identity is ``(project_id, producer, run.execution_id)`` — nothing else
    (:func:`cost_ledger.record_once`). Committing the same execution twice, even
    with a slot counter that moved in between, returns the line the first commit
    booked; two executions of one ref are two lines. ``ref`` is log context, not
    identity. A missing, empty or non-hex ``execution_id`` books nothing — never
    a generated fallback. ``never_started`` books nothing and returns
    ``(None, None)``: an attempt that never reached the door is not a run.
    ``BaseException`` stays uncaught.
    """
    if run.outcome not in ATTEMPT_OUTCOMES:
        logger.warning(
            "harness_run_cost: %s attempt %s (%s) not booked: outcome %r is not one of %s",
            producer, ref, role, run.outcome, ", ".join(sorted(ATTEMPT_OUTCOMES)),
        )
        return None, f"invalid_outcome:{run.outcome}"
    if run.outcome == ATTEMPT_NEVER_STARTED:
        logger.debug(
            "harness_run_cost: %s attempt %s (%s) never started; nothing booked", producer, ref, role,
        )
        return None, None
    if role not in cost_ledger.ROLES:
        logger.warning(
            "harness_run_cost: %s attempt %s not booked: role %r is not a ledger role",
            producer, ref, role,
        )
        return None, f"invalid_role:{role}"
    billing = LANE_BILLING.get(run.provider)
    if billing is None:
        # Never guess a billing class: a provider this module cannot place
        # (e.g. a path binary or a deterministic gate) is not a model run.
        logger.warning(
            "harness_run_cost: %s attempt %s not booked: provider %r is not a model lane",
            producer, ref, run.provider,
        )
        return None, f"unknown_provider:{run.provider}"
    fields, split_reason = _attempt_fields(run)
    if split_reason:
        logger.warning(
            "harness_run_cost: %s %s attempt %s books input_uncached as unknown (%s)",
            run.provider, role, ref, split_reason,
        )

    def make_run(run_id: str) -> cost_ledger.RunCost:
        return cost_ledger.RunCost(
            run_id=run_id, project_id=project_id, role=role, provider=run.provider,
            model=run.model, billing=billing, **fields,
        )

    try:
        record = cost_ledger.record_once(
            project_id, producer, run.execution_id, make_run, ledger_path=ledger_path,
        )
    except (cost_ledger.CostLedgerRefusal, OSError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "harness_run_cost: %s %s attempt %s not booked (%s): %s",
            run.provider, role, ref, fields.get("cost_source") or fields["usage_completeness"], reason,
        )
        return None, reason
    return record, None


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
    "ATTEMPT_NEVER_STARTED", "ATTEMPT_OUTCOMES", "ATTEMPT_RAN", "ATTEMPT_REFUSED", "AttemptRun",
    "BILLING_BY_PROVIDER", "BUILDER_ROLE", "BuilderRun", "CLASSIFIER_PRODUCER", "CLI_BILLING", "CliRun",
    "CODEX_PROVIDER", "GATE_ROLE", "INPUT_CACHE_SPLIT_INVALID", "INPUT_CACHE_SPLIT_UNMEASURED",
    "INTELLIGENCE_ROLE", "KIMI_NO_USAGE_REASON", "KIMI_PROVIDER", "LANE_BILLING",
    "NOT_BUILDER_ROLES", "SPAWN_REFUSED_REASON", "SPAWN_REFUSED_SOURCE", "attempt_reason",
    "book_cli_builder", "books_cli_run", "cli_run", "door_refusal_reason",
    "lane_model", "ledger_project_id", "price_segments",
    "receipt_cost", "record_attempt_run", "record_builder_run", "record_classifier_run", "record_cli_run",
    "record_codex_run", "record_kimi_run", "run_fields", "run_tokens", "transcript_identity",
    "usage_completeness", "writes_builder_run",
]
