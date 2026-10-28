"""cost_ledger.py — the cost ledger per run (ADR-043).

Track ``kosten-per-model-in-het-grootboek``, deliverable K2a. The ledger is
``<central store>/events/provider_costs.ndjson``, resolved by
``provider_costs._resolve_costs_path`` (the one resolver). It carries one line
per run in the shape ``provider_cost_run/1``; a run is one start of one model
for one purpose: a dispatch attempt, a gate run, a plan-gate seat, a panel leg,
an intelligence call.

Two entry points, and every writer and reader goes through them:

- :func:`record_run` validates a :class:`RunCost`, prices it through
  ``provider_costs.price_run`` when the caller has tokens but no provider cost
  (a count the price charges that is ``None`` makes the cost
  ``unavailable:usage_unknown``, never a priced zero),
  and writes the line (ADR-005: the line is in the ledger or in the spool when
  this returns; the caller mutates its state after that). The returned record
  says where in ``landed``. A record the plan forbids is refused with a
  :class:`CostLedgerRefusal` subclass and nothing is written.
  :func:`record_next_attempt` is the same writer for a producer that knows its
  dispatch but not the attempt number: it takes the next attempt from the
  ledger and the spool under the same lock as the write.
- :func:`aggregate_runs` is the one aggregation: per ``run_id`` only the last
  line counts (a ``correction`` replaces the earlier contribution), a run
  without a price is listed in ``unavailable`` and never summed as 0, a line in
  the old ``emit_provider_cost`` shape (no ``schema``) is counted in
  ``legacy_lines`` and not summed, and the runs waiting in the spool come back
  as ``spool_pending``, read in the same lock scope as the ledger. A spooled run
  whose ``run_id`` meets another execution in the ledger comes back as
  ``spool_conflicts`` and is in no sum (:func:`_second_executions`).

One lock orders everything (ADR-043 §6): the lock file
``events/provider_costs.lock`` (``provider_costs.ledger_lock``), taken first by
every actor, exclusively by a writer and shared by the aggregation. The
ledger's own ``flock`` stays, nested under it, against a writer of an older
fabric version that does not know the lock file.

The failure contract: when the ledger cannot be opened, locked or given a
boundary, or the append fails, the record goes to its run's spool file
``events/provider_costs.spool/<readable>~<digest>.json`` (:func:`spool_file_for`;
an atomic write of a JSON list of records) before the lock file is released;
when the fsync or the close fails after a whole append, it goes there too. A
run with lines waiting in the spool queues behind them in the spool, so "last
line wins" still means "last written". :class:`CostLedgerWriteError` means the spool failed too, or the lock
file could not be taken: the outcome is unknown and the caller books no new
attempt.

:func:`replay_spool` brings the spool back into the ledger (K2a-3b), one spool
file per exclusive hold of the lock file, bounded in files and seconds, the
ledger ``flock`` and the receipt lock under it bounded as well. A file is
deleted only after the ledger is fsynced, also when nothing was appended, and
every record of it reads back from the ledger as its own complete line with
its content, field by field, so a replay killed halfway leaves the file, and
the next one sees by ``record_id`` and content what already landed; a line
that only carries the ``record_id`` keeps the file (``readback_mismatch``). A
second execution under one ``run_id`` is booked under the next free attempt
with ``renumbered_from`` and a receipt, and a later correction of it follows
it there, also from the writer, which leaves such a correction to the replay;
the renumbered line names the spooled record in ``renumbered_from_record_id``.
A spool file that cannot be read as one run's goes to
``provider_costs.spool/quarantine/``, its receipt booked before the move, and
so does one the plan refuses.

The dispatcher prelude runs the replay every minute (K2a-3c), and after each
replay :func:`report_spool_health` writes the ``cost-ledger`` beacon: degraded
while the spool holds anything, ok once it holds nothing, and an ok one again
once it is a day old. :func:`ledger_files`
names the cost ledger's own files in a store, read-only, for ``vnx doctor``
and the footprint probe.

Boundary first: only a holder of the lock file in exclusive mode ends a last
line without ``\\n`` with ``\\n`` and fsyncs, so a new line always starts on its
own. An append that fails halfway ends its own line at once when the disk has
room, so the old writer cannot glue onto it. No byte is ever removed: the torn
prefix stays and counts as one unreadable line. A reader under the shared lock
changes nothing and reports a torn tail as ``torn_tail``.

The old writer ``provider_costs.emit_provider_cost`` stays for the lanes that
have not moved yet; the builder runs on the claude harness lanes write here
through ``harness_run_cost`` (K2a-2).
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

import provider_costs
from atomic_io import TEMP_PREFIX, TEMP_SUFFIX, atomic_write_text
from vnx_paths import refuse_real_central_store_write_under_test_runner

logger = logging.getLogger(__name__)

RUN_SCHEMA = "provider_cost_run/1"
TOKEN_FIELDS = ("input_uncached", "cache_hit", "cache_write_5m", "cache_write_1h", "output")
ROLES = frozenset({"builder", "review", "plan_gate", "tiebreaker", "panel", "intelligence"})
KINDS = frozenset({"charge", "correction"})
BILLINGS = frozenset({"metered", "subscription"})
SPOOL_DIR_NAME = "provider_costs.spool"
QUARANTINE_DIR_NAME = "quarantine"
# The field of a renumbered line that holds the record_id the record had in the spool (ADR-043 §6, OI-2250).
RENUMBERED_RECORD_ID = "renumbered_from_record_id"
# The field naming the session transcript a line was read from (K6a, OI-2419): the durable
# identity a later backfill matches on before it may supersede this line.
TRANSCRIPT_IDENTITY = "transcript_identity"
# The store's receipt ledger, ``<state dir>/<RECEIPTS_NAME>`` (:func:`_receipts_file`): where the replay books what it changed.
RECEIPTS_NAME = "t0_receipts.ndjson"
# The bounds of one replay call, each overridable per call: (environment variable, default).
REPLAY_BATCH = ("VNX_COST_LEDGER_REPLAY_BATCH", 50)
REPLAY_BUDGET_SEC = ("VNX_COST_LEDGER_REPLAY_BUDGET_SEC", 10.0)
LOCK_WAIT_SEC = ("VNX_COST_LEDGER_LOCK_WAIT_SEC", 5.0)

_PROVIDER_REPORTED = "provider_reported"
_PRICE_TABLE = "price_table@"
_UNAVAILABLE = "unavailable:"
_SEGMENT_KEYS = frozenset({"tokens", "peak", "ts"})
_PRICE_RUN_COUNTS = (
    ("input", ("input_uncached",)), ("output", ("output",)), ("cache_read", ("cache_hit",)),
    ("cache_write", ("cache_write_5m", "cache_write_1h")),
)
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._:@+-]")
# The spool file name: at most this many readable characters, then ``~`` and this many hex digits of the digest.
_SPOOL_READABLE_CHARS = 128
_SPOOL_DIGEST_HEX = 16


class CostLedgerRefusal(ValueError):
    """The writer refused the record. Nothing was written."""


class CostWithoutSource(CostLedgerRefusal):
    """An amount (``cost_usd`` or ``api_equivalent_usd``) without a ``cost_source``."""


class UnavailableWithAmount(CostLedgerRefusal):
    """A ``cost_source`` of ``unavailable:*`` with a non-zero amount."""


class CorrectionWithoutSupersedes(CostLedgerRefusal):
    """A ``correction`` without the ``record_id`` it supersedes or without a reason."""


class IncompleteUsageMarkedComplete(CostLedgerRefusal):
    """``usage_completeness: complete`` while a token count is ``null`` (unknown)."""


class RunIdProjectMismatch(CostLedgerRefusal):
    """The project part of ``run_id`` is not the record's ``project_id`` (ADR-007)."""


class UnknownModel(CostLedgerRefusal):
    """The provider or model is not in the model registry (ADR-036): refused, not guessed."""


class InvalidRecord(CostLedgerRefusal):
    """Any other malformed field: a role, kind, billing, source, token count or timestamp."""


class CostLedgerWriteError(OSError):
    """Neither the ledger nor the spool confirmed the line: the outcome is unknown (ADR-043 §6).

    Raised when the spool failed too, or when the lock file every actor takes
    first could not be opened or locked (without it the spool cannot be used
    either). An ``OSError`` with the errno of its cause, which is
    ``__cause__``. The line may be in the ledger, when a write, fsync or close
    that failed can have put it there, or in the spool, when the spool failed
    in or after replacing the run's file; the message says which. The
    caller's call fails, it mutates no state and it books no new attempt for
    the run (``record_next_attempt`` would make it a second run); the replay, a
    correction or a backfill charge resolves it.
    """


class WrittenRecord(dict):
    """The record as written, and where it landed: ``landed`` is ``ledger``, ``spool`` or ``ledger+spool``.

    The fields are the line's fields, so it compares equal to the line;
    ``landed`` is an attribute, never a field of the line.
    """

    def __init__(self, record: Mapping[str, Any], landed: str):
        super().__init__(record)
        self.landed = landed


class _Unwritten(Exception):
    """A write that ended in :class:`CostLedgerWriteError`: ``cause`` and what it means for the line."""

    def __init__(self, cause: BaseException, outcome: str):
        super().__init__(outcome)
        self.cause = cause
        self.outcome = outcome


class _UnreadableSpool(ValueError):
    """A spool file that is not a non-empty JSON list of ledger records."""


@dataclass(frozen=True)
class RunCost:
    """What a producer knows about one run, before the writer validates and prices it.

    ``tokens`` maps the five fields of ``TOKEN_FIELDS`` to an int or ``None``
    (``None`` is unknown, ``0`` is a confirmed zero; a missing field is unknown).
    ``segments`` splits the run where a price window splits it: each segment has
    its own ``tokens`` and a rate-class selector ``peak`` (bool) or ``ts``; their
    token counts add up to ``tokens``. Leave ``cost_usd``, ``cost_source`` and
    ``api_equivalent_usd`` empty to have the writer price the run from the
    registry; give ``cost_source`` with them when the provider reported the cost
    or none can be known (``unavailable:<reason>``).
    ``transcript_identity`` names the session transcript the run was read from
    (``harness_run_cost.transcript_identity`` builds it): it is written only when
    set, and a later backfill may only supersede a line whose identity it can
    match, so one dispatch's run is never replaced by another's.
    """

    run_id: str
    project_id: str
    role: str
    provider: str
    model: str
    occurred_from: str
    occurred_to: str
    tokens: Mapping[str, Optional[int]]
    usage_completeness: str
    billing: str
    cost_usd: Optional[float] = None
    cost_source: Optional[str] = None
    api_equivalent_usd: Optional[float] = None
    segments: Optional[Sequence[Mapping[str, Any]]] = None
    kind: str = "charge"
    supersedes: Optional[str] = None
    reason: Optional[str] = None
    backfill: bool = False
    transcript_identity: Optional[str] = None


# ---------------------------------------------------------------------------
# run_id
# ---------------------------------------------------------------------------


def split_run_id(run_id: object) -> Tuple[str, str, str, int]:
    """``<project_id>:<producer>:<dispatch_id|gate_run_id>:<attempt>`` -> its four parts.

    The reference may contain ``:``; the project and the producer may not. The
    attempt is a positive integer (a refire is a new attempt, so a new run).
    """
    parts = run_id.split(":") if isinstance(run_id, str) else []
    if len(parts) < 4 or not all(parts):
        raise InvalidRecord(f"run_id {run_id!r} is not <project_id>:<producer>:<ref>:<attempt>")
    attempt = parts[-1]
    if not attempt.isdigit() or int(attempt) < 1:
        raise InvalidRecord(f"run_id {run_id!r} has attempt {attempt!r}; an attempt is an integer >= 1")
    return parts[0], parts[1], ":".join(parts[2:-1]), int(attempt)


def run_id_for(project_id: str, producer: str, ref: str, attempt: int) -> str:
    """Build a ``run_id`` and check that it splits back into the same parts."""
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise InvalidRecord(f"attempt {attempt!r} is not an integer")
    run_id = f"{project_id}:{producer}:{ref}:{attempt}"
    if split_run_id(run_id)[:2] != (project_id, producer):
        raise InvalidRecord(f"project_id {project_id!r} and producer {producer!r} may not contain ':'")
    return run_id


def _renumbered_from_is_valid(record: Mapping[str, Any]) -> bool:
    """True without ``renumbered_from``, or when it names another attempt of the same ``(project_id, producer, ref)``.

    The two fields are optional and only the replay sets them, together
    (ADR-043 §6): ``renumbered_from`` is the ``run_id`` a spooled record had
    before it was booked under another attempt, ``renumbered_from_record_id``
    the ``record_id`` it had there. So a renumbered line names the exact
    record it came from (OI-2250). A line with one of the two and not the
    other, or whose ``renumbered_from`` is anything else, is no run line.
    """
    if "renumbered_from" not in record:
        return RENUMBERED_RECORD_ID not in record
    origin = record.get(RENUMBERED_RECORD_ID)
    if not isinstance(origin, str) or not origin:
        return False
    try:
        before, after = split_run_id(record["renumbered_from"]), split_run_id(record.get("run_id"))
    except InvalidRecord:
        return False
    return before[:3] == after[:3] and before[3] != after[3]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require_member(value: object, allowed: frozenset, name: str) -> None:
    if value not in allowed:
        raise InvalidRecord(f"{name} {value!r} is not one of {sorted(allowed)}")


def _check_identity(run: RunCost) -> None:
    project, _producer, _ref, _attempt = split_run_id(run.run_id)
    if not isinstance(run.project_id, str) or not run.project_id:
        raise InvalidRecord("project_id is empty")
    if project != run.project_id:
        raise RunIdProjectMismatch(
            f"run_id {run.run_id!r} belongs to project {project!r}, the record to {run.project_id!r}"
        )
    _require_member(run.role, ROLES, "role")
    _require_member(run.kind, KINDS, "kind")
    _require_member(run.billing, BILLINGS, "billing")
    if not isinstance(run.backfill, bool):
        raise InvalidRecord(f"backfill {run.backfill!r} is not a bool")


def _check_transcript_identity(identity: object) -> str:
    """A run's transcript identity: a non-empty string, or no line at all (never a blank one).

    A blank identity is refused rather than written: a line that names no
    session cannot be matched by a later backfill, and an unmatched line must
    not become the thing a different session is allowed to supersede.
    """
    if not isinstance(identity, str) or not identity.strip():
        raise InvalidRecord(f"transcript_identity {identity!r} is not a non-empty string")
    return identity


def _check_kind(run: RunCost) -> None:
    if run.kind == "correction":
        for name in ("supersedes", "reason"):
            value = getattr(run, name)
            if not isinstance(value, str) or not value.strip():
                raise CorrectionWithoutSupersedes(
                    f"correction of run {run.run_id!r} has no {name}: a correction names the record "
                    "it supersedes and why"
                )
    elif run.supersedes is not None or run.reason is not None:
        raise InvalidRecord(f"charge of run {run.run_id!r} carries supersedes/reason; only a correction does")


def _normalize_tokens(raw: object, where: str) -> Dict[str, Optional[int]]:
    if not isinstance(raw, Mapping):
        raise InvalidRecord(f"{where} is not a mapping of {list(TOKEN_FIELDS)}")
    extra = sorted(set(raw) - set(TOKEN_FIELDS))
    if extra:
        raise InvalidRecord(f"{where} has unknown fields {extra}; the fields are {list(TOKEN_FIELDS)}")
    tokens: Dict[str, Optional[int]] = {}
    for name in TOKEN_FIELDS:
        value = raw.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise InvalidRecord(f"{where}.{name} {value!r} is not a count (an int >= 0, or null)")
        tokens[name] = value
    return tokens


def _check_completeness(completeness: object, tokens: Mapping[str, Optional[int]]) -> None:
    unknown = sorted(name for name in TOKEN_FIELDS if tokens[name] is None)
    if completeness == "complete":
        if unknown:
            raise IncompleteUsageMarkedComplete(f"usage_completeness is complete while {unknown} are null")
        return
    label, _sep, detail = completeness.partition(":") if isinstance(completeness, str) else ("", "", "")
    if label == "partial" and detail:
        if sorted(set(detail.split(","))) != unknown or not unknown:
            raise InvalidRecord(f"{completeness!r} must name exactly the null token fields {unknown}")
    elif label == "unknown" and detail:
        if len(unknown) != len(TOKEN_FIELDS):
            raise InvalidRecord(f"{completeness!r} with known token counts; name the null ones as partial:")
    else:
        raise InvalidRecord(f"usage_completeness {completeness!r} is not complete, partial:<fields> or unknown:<reason>")


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidRecord(f"{name} {value!r} is not an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidRecord(f"{name} {value!r} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise InvalidRecord(f"{name} {value!r} carries no timezone")
    return parsed


def _check_window(occurred_from: object, occurred_to: object) -> None:
    if _parse_utc(occurred_from, "occurred_from") > _parse_utc(occurred_to, "occurred_to"):
        raise InvalidRecord(f"the run ends ({occurred_to}) before it starts ({occurred_from})")


def _normalize_segment(raw: object) -> Dict[str, Any]:
    if not isinstance(raw, Mapping) or "tokens" not in raw or set(raw) - _SEGMENT_KEYS:
        raise InvalidRecord(f"segment {raw!r} is not {{tokens, peak?, ts?}}")
    segment: Dict[str, Any] = {"tokens": _normalize_tokens(raw["tokens"], "segment tokens")}
    if "peak" in raw:
        if not isinstance(raw["peak"], bool):
            raise InvalidRecord(f"segment peak {raw['peak']!r} is not a bool")
        segment["peak"] = raw["peak"]
    if "ts" in raw:
        _parse_utc(raw["ts"], "segment ts")
        segment["ts"] = raw["ts"]
    return segment


def _normalize_segments(raw: object, tokens: Mapping[str, Optional[int]]) -> Optional[List[Dict[str, Any]]]:
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or not raw:
        raise InvalidRecord("segments is not a non-empty list")
    segments = [_normalize_segment(item) for item in raw]
    for name in TOKEN_FIELDS:
        values = [segment["tokens"][name] for segment in segments]
        if tokens[name] is None:
            consistent = all(value is None for value in values)
        else:
            consistent = None not in values and sum(values) == tokens[name]
        if not consistent:
            raise InvalidRecord(f"segments give {name} {values}, the run {tokens[name]}")
    return segments


def _resolve_model(provider: object, model: object) -> str:
    from providers import provider_registry

    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise InvalidRecord("provider and model are required strings")
    registry_key = provider_costs.registry_key_for_provider(provider)
    if registry_key is None:
        raise UnknownModel(f"provider {provider!r} has no section in the model registry")
    try:
        return provider_registry.resolve_model_identity(registry_key, model)
    except provider_registry.RegistryLookupError as exc:
        raise UnknownModel(str(exc)) from exc


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def _check_amount(value: object, name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise InvalidRecord(f"{name} {value!r} is not a finite amount >= 0")
    return float(value)


def _check_source(source: str) -> None:
    if source == _PROVIDER_REPORTED:
        return
    for prefix in (_PRICE_TABLE, _UNAVAILABLE):
        if source.startswith(prefix) and source[len(prefix):].strip():
            return
    raise InvalidRecord(
        f"cost_source {source!r} is not provider_reported, price_table@<version> or unavailable:<reason>"
    )


def _billable_fields(provider: str, model: str) -> Tuple[str, ...]:
    """The token fields the model's price charges: input and output always, a cache field when its rate is set.

    The rate is read from the latest price version, the one ``price_run`` applies.
    """
    from providers import provider_registry

    versions = provider_registry.resolve_price_versions(provider_costs.registry_key_for_provider(provider), model)
    latest = versions[-1] if versions else None
    fields = ["input_uncached", "output"]
    if latest is not None and latest.cache_read_per_mtok:
        fields.append("cache_hit")
    if latest is not None and latest.cache_write_per_mtok:
        fields.extend(("cache_write_5m", "cache_write_1h"))
    return tuple(fields)


def _price_segment(segment: Mapping[str, Any]) -> Dict[str, Any]:
    """The segment in ``price_run``'s shape. An unknown count is left out, never sent as 0.

    :func:`_price` only gets here when every count the price charges is known,
    so a count left out is one the price does not charge.
    """
    tokens = segment["tokens"]
    priced: Dict[str, Any] = {
        key: sum(tokens[name] for name in names)
        for key, names in _PRICE_RUN_COUNTS
        if all(tokens[name] is not None for name in names)
    }
    for key in ("peak", "ts"):
        if key in segment:
            priced[key] = segment[key]
    return priced


def _price(run: RunCost, model: str, tokens: Mapping[str, Optional[int]], segments) -> Tuple:
    """Price a run from the registry. An unknown count the price charges makes it unavailable, never a zero."""
    if any(tokens[name] is None for name in _billable_fields(run.provider, model)):
        amount, source = None, f"{_UNAVAILABLE}usage_unknown"
    else:
        priced = [_price_segment(segment) for segment in (segments or [{"tokens": tokens}])]
        amount, raw_source = provider_costs.price_run(run.provider, model, priced)
        source = raw_source if amount is None else _PRICE_TABLE + (raw_source.rsplit("@", 1)[1] or "undated")
    if run.billing == "metered":
        return amount, source, None
    return 0.0, source, amount


def _given_cost(run: RunCost, cost: Optional[float], api: Optional[float]) -> Tuple:
    source = run.cost_source
    _check_source(source)
    unavailable = source.startswith(_UNAVAILABLE)
    if unavailable and (cost or api):
        raise UnavailableWithAmount(f"cost_source {source!r} with an amount ({cost!r}, {api!r}): unavailable is no price")
    if run.billing == "metered":
        if api is not None:
            raise InvalidRecord("a metered run books its money in cost_usd; api_equivalent_usd is for subscriptions")
        if not unavailable and cost is None:
            raise InvalidRecord(f"cost_source {source!r} without cost_usd")
        return cost, source, None
    if cost:
        raise InvalidRecord("a subscription run costs 0 at the margin; its list price goes in api_equivalent_usd")
    if not unavailable and api is None:
        raise InvalidRecord(f"cost_source {source!r} without api_equivalent_usd")
    return 0.0, source, api


def _resolve_cost(run: RunCost, model: str, tokens, segments) -> Tuple:
    """``(cost_usd, cost_source, api_equivalent_usd)`` from what the caller gave, or from the registry."""
    cost = _check_amount(run.cost_usd, "cost_usd")
    api = _check_amount(run.api_equivalent_usd, "api_equivalent_usd")
    if run.cost_source is None or run.cost_source == "":
        if cost is not None or api is not None:
            raise CostWithoutSource(f"run {run.run_id!r} has an amount without a cost_source")
        return _price(run, model, tokens, segments)
    if not isinstance(run.cost_source, str):
        raise InvalidRecord(f"cost_source {run.cost_source!r} is not a string")
    return _given_cost(run, cost, api)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_id(record: Mapping[str, Any]) -> str:
    body = {key: value for key, value in record.items() if key != "record_id"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def build_record(run: RunCost) -> Dict[str, Any]:
    """Validate and price ``run`` into a ledger record. Raises a :class:`CostLedgerRefusal`."""
    _check_identity(run)
    _check_kind(run)
    tokens = _normalize_tokens(run.tokens, "tokens")
    _check_completeness(run.usage_completeness, tokens)
    segments = _normalize_segments(run.segments, tokens)
    _check_window(run.occurred_from, run.occurred_to)
    model = _resolve_model(run.provider, run.model)
    cost_usd, cost_source, api_equivalent = _resolve_cost(run, model, tokens, segments)

    record: Dict[str, Any] = {
        "schema": RUN_SCHEMA, "record_id": "", "run_id": run.run_id,
        "project_id": run.project_id, "kind": run.kind,
    }
    if run.kind == "correction":
        record.update(supersedes=run.supersedes, reason=run.reason)
    record.update(
        occurred_from=run.occurred_from, occurred_to=run.occurred_to, recorded_at=_now_iso(),
        role=run.role, provider=run.provider, model=model, tokens=tokens,
        usage_completeness=run.usage_completeness,
    )
    if segments is not None:
        record["segments"] = segments
    record.update(cost_usd=cost_usd, cost_source=cost_source, billing=run.billing)
    if run.billing == "subscription":
        record["api_equivalent_usd"] = api_equivalent
    if run.backfill:
        record["backfill"] = True
    if run.transcript_identity is not None:
        record[TRANSCRIPT_IDENTITY] = _check_transcript_identity(run.transcript_identity)
    record["record_id"] = _record_id(record)
    return record


def _line(record: Mapping[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------
# The spool
# ---------------------------------------------------------------------------


def spool_dir_for(ledger_path: Path) -> Path:
    """``provider_costs.spool/`` next to the ledger."""
    return Path(ledger_path).parent / SPOOL_DIR_NAME


def _spool_name(run_id: str) -> str:
    """The spool file name of ``run_id``, ``<readable>~<digest>.json``. See :func:`spool_file_for`."""
    readable = _UNSAFE_FILENAME_CHARS.sub("_", run_id)[:_SPOOL_READABLE_CHARS]
    if readable.startswith("."):
        readable = "_" + readable[1:]
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:_SPOOL_DIGEST_HEX]
    return f"{readable}~{digest}.json"


def spool_file_for(spool_dir: Path, run_id: str) -> Path:
    """The spool file of one run: a JSON list of its waiting records, in order.

    The name is ``<readable>~<digest>.json``: ``<readable>`` is the run id with
    every character outside ``[A-Za-z0-9._:@+-]`` and a leading dot made ``_``,
    cut at 128 characters; ``<digest>`` is the first 16 hex characters of the
    sha256 of the exact run id in UTF-8. The digest keeps two run ids that
    sanitise alike (``review/a`` and ``review_a``) in two files; ``~`` is never
    part of ``<readable>``. A reader takes the run from the records, never from
    the name (:func:`_read_spool_file`).
    """
    return Path(spool_dir) / _spool_name(run_id)


def _spool_names(spool_dir: Path) -> List[str]:
    """The names of the spool files, one per waiting run, sorted. A missing spool is an empty one.

    ``os.scandir`` and not a glob: a glob takes a directory it cannot read for
    an empty one, and an unreadable spool is never empty (blocker 6 of #2141).
    Every other ``OSError`` of the listing is raised. Only the spool's own
    level is listed: the ``quarantine/`` directory is no spool file, and what
    sits in it waits for no replay and counts for no attempt.
    """
    try:
        with os.scandir(spool_dir) as entries:
            return sorted(
                entry.name for entry in entries if entry.name.endswith(".json") and not entry.name.startswith(".")
            )
    except FileNotFoundError:
        return []


def _is_run_record(record: object) -> bool:
    """True for a spooled record the replay can book as it stands; any other record makes its file unreadable.

    The identity holds as :func:`build_record` makes it: a ``run_id`` that
    :func:`split_run_id` accepts, the ``project_id`` of that ``run_id``, a
    ``record_id``, a ``kind``, a correction's ``supersedes`` as a string and
    a valid ``renumbered_from``. A record without it could pass into the
    replay and fail there, after a renumbered line had landed (OI-2250); a
    file that holds one goes to quarantine instead.
    """
    if not (
        isinstance(record, dict)
        and record.get("schema") == RUN_SCHEMA
        and isinstance(record.get("record_id"), str) and bool(record["record_id"])
        and isinstance(record.get("kind"), str) and record["kind"] in KINDS
        and _renumbered_from_is_valid(record)
    ):
        return False
    if record["kind"] == "correction" and not isinstance(record.get("supersedes"), str):
        return False
    try:
        project = split_run_id(record.get("run_id"))[0]
    except InvalidRecord:
        return False
    return record.get("project_id") == project


def _read_spool_file(path: Path, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """The records waiting in one spool file, in order. Raises :class:`_UnreadableSpool`, or the read's ``OSError``.

    Every record carries the run id the file belongs to: ``run_id`` when the
    caller names it, else the run id of its records, and the file has that
    run's :func:`spool_file_for` name. A record of another run makes the whole
    file unreadable, so it is never counted as this run's line.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise _UnreadableSpool(f"{path.name}: {exc}") from exc
    if not isinstance(data, list) or not data or not all(_is_run_record(record) for record in data):
        raise _UnreadableSpool(f"{path.name}: not a non-empty list of {RUN_SCHEMA} records")
    owner = data[0]["run_id"] if run_id is None else run_id
    if path.name != _spool_name(owner) or any(record["run_id"] != owner for record in data):
        found = sorted({record["run_id"] for record in data})
        raise _UnreadableSpool(f"{path.name}: holds records of {found}, not only of the run the file belongs to")
    return data


def _exists(path: Path) -> bool:
    """True when ``path`` exists. Only a missing path, or one under a non-directory, is False; any other error raises.

    Not ``Path.exists``: which errors that swallows differs per Python version,
    and a spool that cannot be checked is never one without the file.
    """
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _what_is_known(in_the_ledger: bool, replacing: bool) -> str:
    """Where a line whose spool failed may be: the ledger when its write had started, the spool once its replace had."""
    if in_the_ledger and replacing:
        return "the outcome is unknown, the line may be in the ledger and may be in the spool"
    if in_the_ledger:
        return "the outcome is unknown, the line may be in the ledger"
    if replacing:
        return "the outcome is unknown, the line may be in the spool"
    return "the line is in neither the ledger nor the spool"


def _spool(spool_dir: Path, record: Mapping[str, Any], why: str, *, in_the_ledger: bool) -> None:
    """Add ``record`` to its run's spool file, behind what waits there. ``why`` is what sent it there.

    The caller holds the lock file exclusively, so reading and replacing the
    run's file is one step for every other actor. The file is replaced
    atomically, and the spool directory and the directory above it are
    fsynced, so a spool created here survives a crash too. A spool that fails
    raises :class:`_Unwritten`; ``in_the_ledger`` says whether a write to the
    ledger had started, so whether the line may be there. A failure in or after
    the replace may leave the whole record in the spool, unconfirmed: the
    message then says the line may be in the spool, never that it is absent.
    """
    refuse_real_central_store_write_under_test_runner(spool_dir)
    target = spool_file_for(spool_dir, record["run_id"])
    replacing = False
    try:
        spool_dir.mkdir(parents=True, exist_ok=True)
        queued = _read_spool_file(target, record["run_id"]) if _exists(target) else []
        replacing = True
        atomic_write_text(target, json.dumps([*queued, dict(record)], separators=(",", ":")))
        _fsync_dir(spool_dir)
        _fsync_dir(spool_dir.parent)
    except (OSError, _UnreadableSpool) as exc:
        raise _Unwritten(exc, f"{why}, and the spool {spool_dir} failed too ({exc}): "
                              f"{_what_is_known(in_the_ledger, replacing)}; book no new attempt for it") from exc
    logger.warning("cost_ledger: run %s went to the spool %s: %s", record["run_id"], spool_dir, why)


def _waits_in_the_spool(spool_dir: Path, record: Mapping[str, Any]) -> bool:
    """True when the record's run has lines waiting in the spool. A spool that cannot be checked raises.

    The run's file must read as its own (:func:`_read_spool_file`): a file
    that is not a list of this run's records cannot say what waits there.
    """
    target = spool_file_for(spool_dir, record["run_id"])
    try:
        if not _exists(target):
            return False
        _read_spool_file(target, record["run_id"])
        return True
    except (OSError, _UnreadableSpool) as exc:
        raise _Unwritten(
            exc, f"the spool {spool_dir} could not be checked for earlier lines of the run ({exc}): "
                 "the line was not written",
        ) from exc


def _second_executions(
    ledger_records: Sequence[Mapping[str, Any]], spooled_runs: Sequence[Sequence[Mapping[str, Any]]],
) -> List[str]:
    """The run ids whose spool file holds a second execution of a run the ledger holds too (ADR-043 §6).

    ``spooled_runs`` holds the records of each spool file as
    :func:`_read_spool_file` reads them. A spooled record continues the run in
    the ledger when its ``record_id`` is in the ledger as well
    (``ledger+spool``), or when it is a correction whose ``supersedes`` names a
    record of that run in the ledger or one that continues it earlier in the
    file. A record a replay renumbered out of the run counts as one of the
    run's here, as its spooled and as its renumbered ``record_id``: it landed,
    and a correction of it follows it to the renumbered run
    (:func:`_followed_line`). Any other spooled record of a run the ledger holds is a second
    execution under the same ``run_id``: a writer of fabric f2bef053 counts the
    attempts in the ledger only, so it books an attempt again while that
    attempt waits in the spool. A run without a line in the ledger has a
    backlog and no conflict, and so has a run whose ledger lines all came from
    its spool file (``ledger+spool``, or a replay that did not get to delete
    the file).
    """
    landed: Dict[str, set] = {}
    for record in ledger_records:
        ids = landed.setdefault(record["run_id"], set())
        if isinstance(record.get("record_id"), str):
            ids.add(record["record_id"])
    renumbered_out: Dict[str, set] = {}
    for origin, line in _renumbered_origins(ledger_records).items():
        renumbered_out.setdefault(line["renumbered_from"], set()).update((origin, str(line.get("record_id"))))
    conflicts: List[str] = []
    for spooled in spooled_runs:
        run_id = spooled[0]["run_id"]
        if not landed.get(run_id, set()) - {record["record_id"] for record in spooled}:
            continue
        chain = landed[run_id] | renumbered_out.get(run_id, set())
        for record in spooled:
            if record["record_id"] not in chain and not (
                record.get("kind") == "correction" and record.get("supersedes") in chain
            ):
                conflicts.append(run_id)
                break
            chain.add(record["record_id"])
    return conflicts


def _renumbered_origins(ledger_records: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    """``{the record_id a renumbered line had in the spool: that line}``, so a spooled record is known to have landed.

    Read from the line, never derived back: every renumbered line carries the
    spooled ``record_id`` in ``renumbered_from_record_id`` (:func:`_renumber`).
    Deriving it back cannot work for a correction whose ``supersedes`` named
    the renumbered line itself: the renumbering keeps that ``supersedes``, so
    no undoing tells which of the two it named in the spool, and the
    correction of that correction then lost its way (OI-2250).
    """
    return {record[RENUMBERED_RECORD_ID]: record for record in ledger_records if "renumbered_from" in record}


def _lines_by_record_id(ledger_records: Sequence[Mapping[str, Any]]) -> Dict[str, List[Mapping[str, Any]]]:
    """``{record_id: every ledger line that carries it, in ledger order}``."""
    lines: Dict[str, List[Mapping[str, Any]]] = {}
    for record in ledger_records:
        if isinstance(record.get("record_id"), str):
            lines.setdefault(record["record_id"], []).append(record)
    return lines


_ABSENT = object()


def _canonical(value: object) -> str:
    """One field value as the record id hashes it, so ``1`` and ``1.0`` or ``true`` and ``1`` differ; absent is its own."""
    return "\x00absent" if value is _ABSENT else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _differing_fields(expected: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]) -> List[str]:
    """The fields in which any of ``lines`` differs from ``expected``, sorted; empty when every line holds it exactly."""
    return sorted({
        key for line in lines for key in set(expected) | set(line)
        if _canonical(expected.get(key, _ABSENT)) != _canonical(line.get(key, _ABSENT))
    })


def _renumber(record: Mapping[str, Any], run_id: str, supersedes: Optional[str] = None) -> Dict[str, Any]:
    """The spooled ``record`` as a line of ``run_id``, with where it came from and a fresh ``record_id``.

    ``renumbered_from`` is the spooled ``run_id``, ``renumbered_from_record_id``
    the spooled ``record_id``. ``supersedes``, when given, replaces what the
    correction names: the renumbered line of the record it named
    (:func:`_followed_line`).
    """
    line = dict(record, run_id=run_id, renumbered_from=record["run_id"])
    line[RENUMBERED_RECORD_ID] = record["record_id"]
    if supersedes is not None:
        line["supersedes"] = supersedes
    line["record_id"] = _record_id(line)
    return line


def _landed_as(
    record: Mapping[str, Any], by_id: Mapping[str, List[Mapping[str, Any]]], origins: Mapping[str, Mapping[str, Any]],
) -> Optional[Tuple[Dict[str, Any], List[Mapping[str, Any]]]]:
    """``(the line the spooled record is in the ledger as, the ledger lines that carry that line's id)``, or ``None``.

    Found by ``record_id``: as itself, or as the renumbered line that names it
    in ``renumbered_from_record_id`` (:func:`_renumbered_origins`). Found is not landed: whether
    those lines hold what the record holds is :func:`_differing_fields`. For a
    renumbered record the line is the one the replay makes of it under that
    run id, its ``record_id`` included, so a renumbered line whose own
    ``record_id`` is off differs too.
    """
    record_id = record["record_id"]
    if record_id in origins:
        found = origins[record_id]
        line = _renumber(record, found["run_id"], found.get("supersedes") if "supersedes" in record else None)
        return line, by_id.get(found.get("record_id")) or [found]
    if record_id in by_id:
        return dict(record), by_id[record_id]
    return None


def _has_landed(
    record: Mapping[str, Any], by_id: Mapping[str, List[Mapping[str, Any]]], origins: Mapping[str, Mapping[str, Any]],
) -> bool:
    """True when the spooled record is in the ledger with its content, as itself or as its renumbered line."""
    landed = _landed_as(record, by_id, origins)
    return landed is not None and not _differing_fields(*landed)


def _followed_line(
    record: Mapping[str, Any], renamed: Mapping[str, Mapping[str, Any]], origins: Mapping[str, Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """The renumbered line a correction follows, or ``None``: the line of the record its ``supersedes`` names.

    A replay renumbers a second execution out of its run (ADR-043 §6); a
    correction of one of its records belongs to the renumbered run, not to
    the run id it was written under, where it would replace the other
    execution's line. ``supersedes`` names the record as it was in the spool
    (renumbered in this file, ``renamed``, or by an earlier replay,
    ``origins``) or the renumbered line itself. Only a line renumbered from
    the correction's own run id is followed.
    """
    if record.get("kind") != "correction" or not isinstance(record.get("supersedes"), str):
        return None
    named = record["supersedes"]
    line = renamed.get(named) or origins.get(named) or next(
        (line for line in (*renamed.values(), *origins.values()) if line.get("record_id") == named), None)
    if line is None or line.get("renumbered_from") != record["run_id"]:
        return None
    return line


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def _end_a_torn_tail(fh) -> None:
    """End a last line without ``\\n`` (a torn append) and fsync, so the next line starts on its own.

    Only a holder of the lock file in exclusive mode calls this, with the
    ledger lock held and the ledger open for reading too. Append-only: the
    torn bytes stay and become one line, readable when the JSON was whole and
    only its ``\\n`` was missing, unreadable otherwise.
    """
    fh.flush()
    size = os.fstat(fh.fileno()).st_size
    if size and os.pread(fh.fileno(), 1, size - 1) != b"\n":
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def _append_record(fh, record: Mapping[str, Any]) -> None:
    """Append ``record`` as one line and flush it. The caller holds both locks and ended the boundary first.

    When the append fails halfway, the line it left open is ended at once, so
    ``emit_provider_cost`` cannot glue its next line onto it. On a disk that is
    still full that fails too; the next exclusive holder ends it first. The
    append's own error is what the caller gets.
    """
    try:
        fh.write(_line(record))
        fh.flush()
    except OSError:
        try:
            _end_a_torn_tail(fh)
        except OSError as exc:
            logger.warning("cost_ledger: the failed append left an open last line in %s: %s", fh.name, exc)
        raise


def _open_locked(ledger_path: Path):
    """The ledger, opened for append and read and locked exclusively.

    The lock is released by closing the file, never by ``LOCK_UN``: a close
    flushes what a failed write left buffered, and those bytes must reach the
    ledger while the lock is still held.
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    fh = ledger_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
    except OSError:
        fh.close()
        raise
    return fh


def _try_open_locked(ledger_path: Path) -> Tuple[Any, Optional[OSError]]:
    """``(ledger, None)`` with the ledger open and locked, or ``(None, error)`` when it could not be."""
    try:
        return _open_locked(ledger_path), None
    except OSError as exc:
        return None, exc


def _close_the_ledger(fh) -> Optional[OSError]:
    """Close the ledger, which flushes under its lock and then releases it. The close's error is returned."""
    try:
        fh.close()
    except OSError as exc:
        logger.warning("cost_ledger: closing %s failed: %s", fh.name, exc)
        return exc
    return None


def _why_only_the_replay_books_it(fh, record: Mapping[str, Any]) -> Optional[str]:
    """Why a correction goes to the spool for the replay to book, or ``None`` for a record the writer appends itself.

    A correction of a record a replay renumbered out of its run belongs to the
    renumbered run (:func:`_followed_line`). Appended under the run id it was
    written with, it would be the last line of the other execution of that
    run and replace its amount. The replay books it under the renumbered run
    id, with its receipt (:func:`_plan_the_file`). A ledger that cannot be
    read to tell sends a correction there as well: the replay reads it again.
    """
    if record.get("kind") != "correction":
        return None
    try:
        origins = _renumbered_origins(_whole_records(fh))
    except OSError as exc:
        return f"the ledger could not be read to see whether the correction follows a renumbered line ({exc})"
    if _followed_line(record, {}, origins) is None:
        return None
    return "the correction names a record a replay renumbered: the replay books it under that line's run"


def _land(fh, opened: Optional[OSError], spool_dir: Path, record: Mapping[str, Any]) -> str:
    """Write ``record`` in its run's order and return where it landed. The caller holds the lock file exclusively.

    ``fh`` is the ledger, open and locked, or ``None`` with ``opened`` the error
    that kept it closed. The failure contract of ADR-043 §6: no ledger, no
    boundary, earlier lines of the run in the spool or a failed append sends
    the record to the spool (``spool``; after a failed append the ledger may
    hold a fragment of it, or all of it once its line is ended); a failed fsync
    after a whole append sends it there as well (``ledger+spool``). So does a
    correction of a record a replay renumbered (:func:`_why_only_the_replay_books_it`). A spool
    that fails too raises :class:`_Unwritten`.
    """
    if fh is None:
        _spool(spool_dir, record, f"the ledger could not be opened or locked ({opened})", in_the_ledger=False)
        return "spool"
    try:
        _end_a_torn_tail(fh)
    except OSError as exc:
        _spool(spool_dir, record, f"the ledger's open last line could not be ended ({exc})", in_the_ledger=False)
        return "spool"
    if _waits_in_the_spool(spool_dir, record):
        _spool(spool_dir, record, "earlier lines of the run wait in the spool", in_the_ledger=False)
        return "spool"
    for_the_replay = _why_only_the_replay_books_it(fh, record)
    if for_the_replay is not None:
        _spool(spool_dir, record, for_the_replay, in_the_ledger=False)
        return "spool"
    try:
        _append_record(fh, record)
    except OSError as exc:
        _spool(spool_dir, record, f"the ledger append failed ({exc})", in_the_ledger=True)
        return "spool"
    try:
        os.fsync(fh.fileno())
    except OSError as exc:
        _spool(spool_dir, record, f"the ledger fsync failed after the whole line was written ({exc})",
               in_the_ledger=True)
        return "ledger+spool"
    return "ledger"


def _write_in_order(
    ledger_path: Path, record_for: Callable[[Any], Dict[str, Any]], built: List[Dict[str, Any]],
) -> str:
    """Write the record ``record_for`` returns in its run's order; return where it landed.

    The lock file first, exclusively, held from the first read to the last
    write, and the ledger opened and ``flock``ed under it. ``record_for`` gets
    the open ledger, or ``None`` when it could not be opened or locked, and
    returns the record; it lands in ``built`` before the write, so an error can
    name it. The ledger is closed, which releases its ``flock``, before the
    lock file is; a close that fails after the record landed in the ledger
    sends it to the spool as well. A lock file that cannot be taken raises
    :class:`_Unwritten` with nothing written.
    """
    spool_dir = spool_dir_for(ledger_path)
    with ExitStack() as held:
        try:
            held.enter_context(provider_costs.ledger_lock(ledger_path, fcntl.LOCK_EX))
        except OSError as exc:
            raise _Unwritten(
                exc, f"the lock file {provider_costs.ledger_lock_path(ledger_path)} could not be taken ({exc}): "
                     "nothing was written, and without that lock the spool cannot be used either",
            ) from exc
        fh, opened = _try_open_locked(ledger_path)
        landed = ""
        try:
            record = record_for(fh)
            built.append(record)
            landed = _land(fh, opened, spool_dir, record)
        finally:
            closed = _close_the_ledger(fh) if fh is not None else None
        if closed is not None and landed == "ledger":
            _spool(spool_dir, record, f"closing the ledger failed after the whole line was written ({closed})",
                   in_the_ledger=True)
            landed = "ledger+spool"
    return landed


class _RecordNotBuilt(Exception):
    """An ``OSError`` raised while building the record (the registry read), not while writing it."""


def _write(ledger_path: Path, record_for: Callable[[Any], Dict[str, Any]], unbuilt: str) -> WrittenRecord:
    """:func:`_write_in_order`, with its failures turned into the caller's errors. ``unbuilt`` names a record not built."""
    built: List[Dict[str, Any]] = []
    try:
        landed = _write_in_order(ledger_path, record_for, built)
    except _RecordNotBuilt as exc:
        raise exc.__cause__ from None
    except _Unwritten as failure:
        what = f"cost record {built[-1]['record_id']} of run {built[-1]['run_id']}" if built else unbuilt
        message = f"{what}, ledger {ledger_path}: {failure.outcome}"
        code = getattr(failure.cause, "errno", None)
        error = CostLedgerWriteError(message) if code is None else CostLedgerWriteError(code, message)
        raise error from failure.cause
    return WrittenRecord(built[-1], landed)


def record_run(run: RunCost, *, ledger_path: Optional[Path] = None) -> WrittenRecord:
    """Validate, price and write one run's line; return the record as written, with ``landed``.

    ``ledger_path`` defaults to ``provider_costs._resolve_costs_path()``.
    Raises a :class:`CostLedgerRefusal` (nothing written) for a record the plan
    forbids. The line lands in the ledger, or in the spool when the ledger
    fails (ADR-043 §6, the failure contract in :func:`_land`).
    :class:`CostLedgerWriteError` when the spool failed too or the lock file
    could not be taken: the outcome is unknown, so the caller fails its call,
    mutates no state and books no new attempt (ADR-005).
    """
    record = build_record(run)
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    refuse_real_central_store_write_under_test_runner(path)
    return _write(path, lambda _fh: record, f"cost record {record['record_id']} of run {record['run_id']}")


def _read_under_the_lock(fh) -> str:
    """The whole ledger text through the locked handle, an undecodable byte replaced."""
    fh.flush()
    fd = fh.fileno()
    size = os.fstat(fd).st_size
    chunks: List[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(fd, size - offset, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _read_without_the_lock(ledger_path: Path) -> str:
    """The ledger text read plainly; no ledger file is an empty ledger."""
    try:
        with ledger_path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def _attempt_of(run_id: str, key: Tuple[str, str, str]) -> int:
    """The attempt of ``run_id`` when it is a run of ``key``, the exact ``(project_id, producer, ref)``; else 0."""
    try:
        project, producer, ref, attempt = split_run_id(run_id)
    except InvalidRecord:
        return 0
    return attempt if (project, producer, ref) == key else 0


def _next_attempt(ledger_path: Path, fh, project_id: str, producer: str, ref: str) -> int:
    """One more than the highest attempt of ``<project_id>:<producer>:<ref>`` in the ledger or the spool.

    The caller holds the lock file exclusively, so no writer of this version
    lands a line or adds one to the spool while this counts. A ledger that
    could not be opened or locked is read without its ``flock``, and the lock
    file does not keep out a writer of an earlier fabric version, which takes
    only the ledger ``flock``: a 1.7.2 ``emit_provider_cost`` appends old-shape
    lines, which carry no attempt, but a K2a-2 ``record_run`` or
    ``record_next_attempt`` (fabric f2bef053) counts the ledger only, so it can
    book an attempt again that waits in the spool. Such a run is in the ledger
    and in the spool as two executions (:func:`_second_executions`): the count
    here passes both and logs a warning naming the run, and the aggregation
    reports it as ``spool_conflicts`` (ADR-043 §6). A spool file counts by the
    ``run_id`` of its records, never by its name, compared on the exact
    ``(project_id, producer, ref)`` as in the ledger. Counting that fails, an
    unreadable spool file included, raises :class:`_Unwritten` with nothing
    written: an attempt guessed too low would make a second run of an existing
    one.
    """
    spool_dir = spool_dir_for(ledger_path)
    try:
        text = _read_under_the_lock(fh) if fh is not None else _read_without_the_lock(ledger_path)
        spooled = [_read_spool_file(spool_dir / name) for name in _spool_names(spool_dir)]
    except (OSError, _UnreadableSpool) as exc:
        raise _Unwritten(exc, f"the attempts already booked could not be counted ({exc}): nothing was written") from exc
    key = (project_id, producer, ref)
    records = _parse_lines(text)[0]
    for run_id in _second_executions(records, spooled):
        if _attempt_of(run_id, key):
            logger.warning(
                "cost_ledger: run %s is in the ledger and in the spool as two executions: a writer of fabric "
                "f2bef053 booked the attempt again while it waited in the spool (ADR-043 §6, spool_conflicts); "
                "the next attempt passes both", run_id,
            )
    attempts = [_attempt_of(record["run_id"], key) for record in [*records, *(r for run in spooled for r in run)]]
    return max(attempts, default=0) + 1


class _BookedRun(Exception):
    """Carries the line :func:`record_attempt_once` found for the run it was asked to book."""

    def __init__(self, record: Dict[str, Any]):
        super().__init__(record.get("run_id"))
        self.record = record


def _booked_lines(ledger_path: Path, fh) -> List[Dict[str, Any]]:
    """Every line in the ledger and in the spool that waits for it, under the caller's lock.

    The spool is read as :func:`_next_attempt` reads it, by the ``run_id`` of its
    records: a run waiting there was written. A ledger that cannot be read
    raises :class:`_Unwritten` with nothing written — an unreadable ledger is
    evidence of nothing, so it must never read as "not booked yet".
    """
    spool_dir = spool_dir_for(ledger_path)
    try:
        text = _read_under_the_lock(fh) if fh is not None else _read_without_the_lock(ledger_path)
        spooled = [_read_spool_file(spool_dir / name) for name in _spool_names(spool_dir)]
    except (OSError, _UnreadableSpool) as exc:
        raise _Unwritten(exc, f"the runs already booked could not be read ({exc}): nothing was written") from exc
    return [*_parse_lines(text)[0], *(record for run in spooled for record in run)]


def _booked_this_run(
    lines: Sequence[Mapping[str, Any]], key: tuple, window: tuple
) -> Optional[Dict[str, Any]]:
    """The line of ``key``'s ref that spans ``window`` — the run the caller commits — or ``None``.

    This is the identity a run has when its slot took nothing: the window it
    spans (``occurred_from``/``occurred_to``, the record's own timestamps). A
    producer that commits one record twice — an attempt whose first slot write
    was refused and that is now committed again — presents that same window
    both times, so the second commit finds the first's line; the next real run
    of that ref begins on another record and spans another window. A line that
    states no window names no run and is never taken for this one, and an
    unreadable ``run_id`` is no line of this ref either: both cases book, so a
    run that happened is never read as already booked.
    """
    started, ended = window
    if not started or not ended:
        return None
    for line in lines:
        try:
            parts = split_run_id(line.get("run_id"))
        except InvalidRecord:
            continue
        if parts[:3] != key:
            continue
        if (line.get("occurred_from"), line.get("occurred_to")) == (started, ended):
            return dict(line)
    return None


def record_attempt_once(
    project_id: str,
    producer: str,
    ref: str,
    attempt: Optional[int],
    make_run: Callable[[str], RunCost],
    *,
    ledger_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Write one run's line at the attempt its EXECUTION holds, unless that line is there already.

    One execution, one line, whether or not the slot took the run's record: the
    booking no longer depends on the write to the result slot succeeding.

    ``attempt`` is the number the run's slot holds — the caller read it off the
    record the slot already carries for this run, so the number NAMES the
    execution. When the ledger holds that ``run_id`` already, the run is booked:
    the line is returned and nothing is written, which is what makes a record
    committed twice one line.

    ``attempt=None`` is a run whose execution the slot has no number for (a
    record the guard refused never took an ``attempt_seq``). Its identity is
    then the window it spans, and :func:`_booked_this_run` says whether that run
    is booked already; when it is not, the run takes the ref's next free
    attempt. A re-commit of a refused record therefore books nothing the second
    time, while a later run of the same ref — another window — books its own
    line rather than being read as booked.

    The lookup, the count and the write run under the same exclusive lock, so
    two processes booking one execution cannot interleave. Returns the line
    already booked (nothing written) or the record as written. ``make_run`` is
    called with the ``run_id`` to price. Refusals and
    :class:`CostLedgerWriteError` are those of :func:`record_run`.
    """
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    refuse_real_central_store_write_under_test_runner(path)
    key = (project_id, producer, ref)

    def build(number: int) -> Dict[str, Any]:
        try:
            return build_record(make_run(run_id_for(project_id, producer, ref, number)))
        except OSError as exc:
            raise _RecordNotBuilt(str(exc)) from exc

    def record_for(fh) -> Dict[str, Any]:
        candidate = build(attempt if attempt is not None else _next_attempt(
            path, fh, project_id, producer, ref,
        ))
        lines = _booked_lines(path, fh)
        if attempt is not None:
            booked = next(
                (dict(line) for line in lines if line.get("run_id") == candidate["run_id"]), None,
            )
        else:
            booked = _booked_this_run(
                lines, key, (candidate.get("occurred_from"), candidate.get("occurred_to")),
            )
        if booked is not None:
            raise _BookedRun(booked)
        return candidate

    described = f"{producer} {ref}" if attempt is None else run_id_for(project_id, producer, ref, attempt)
    try:
        return _write(path, record_for, f"the run {described}")
    except _BookedRun as booked:
        logger.debug("cost_ledger: %s is booked already; nothing written", described)
        return booked.record


def record_next_attempt(
    project_id: str,
    producer: str,
    ref: str,
    make_run: Callable[[str], RunCost],
    *,
    ledger_path: Optional[Path] = None,
) -> WrittenRecord:
    """:func:`record_run` for a producer that knows its run but not the run's attempt number.

    The attempt is one more than the highest attempt the ledger or the spool
    holds for ``<project_id>:<producer>:<ref>``, counted under the same
    exclusive lock as the write, so two attempts of this version never get the
    same ``run_id``: a refire of a dispatch is the next attempt, whichever
    process fires it, also while an earlier attempt waits in the spool, and
    also past a second execution a writer of fabric f2bef053 booked under an
    attempt that waits there (:func:`_next_attempt`). ``make_run`` gets that
    ``run_id`` and returns the :class:`RunCost`. Refusals and
    :class:`CostLedgerWriteError` are those of :func:`record_run`; attempts
    that cannot be counted raise :class:`CostLedgerWriteError` with nothing
    written.
    """
    run_id_for(project_id, producer, ref, 1)
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    refuse_real_central_store_write_under_test_runner(path)

    def record_for(fh) -> Dict[str, Any]:
        attempt = _next_attempt(path, fh, project_id, producer, ref)
        try:
            return build_record(make_run(run_id_for(project_id, producer, ref, attempt)))
        except OSError as exc:
            raise _RecordNotBuilt(str(exc)) from exc

    return _write(path, record_for, f"the next attempt of {project_id}:{producer}:{ref}")


# ---------------------------------------------------------------------------
# The aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerTotals:
    """The one aggregate every reader uses (receipts, report, ceiling).

    ``runs`` maps each ``run_id`` to its effective record: the last line of
    that run. ``cost_usd`` and ``api_equivalent_usd`` sum the priced runs only;
    ``unavailable`` lists the runs without a price, which are in no sum.
    ``tokens`` sums the known counts per field, ``tokens_unknown`` counts per
    field the runs whose count is ``null``. ``legacy_lines`` counts old-shape
    lines (no ``schema``), ``unreadable_lines`` lines that are not JSON objects,
    carry another schema version or have no ``run_id``, a torn tail included.
    ``spool_pending`` is the number of runs waiting in the spool when the
    ledger was read, in no sum; a spool file whose every record is in the
    ledger already with its content (``ledger+spool``, or renumbered by a
    replay that did not get to delete it) waits for nothing and is not
    counted, and one with a record whose ``record_id`` is there with other
    content still counts.
    ``spool_unreadable`` says the spool or its quarantine could not be listed
    or a spool file could not be read as one run's, so ``spool_pending`` is a
    lower bound; ``spool_conflicts`` is the number of those waiting runs whose
    ``run_id`` meets another execution in the ledger
    (:func:`_second_executions`): such a run counts with its ledger lines
    only, and its spooled amount is in no sum until the replay renumbers it.
    ``spool_quarantined`` is the number of spool files the replay moved to
    quarantine: what they hold is in no sum. ``torn_tail`` says the ledger
    ends on a line without ``\\n``. ``unlocked_read`` says the lock file did
    not exist and could not be created, so the ledger and the spool were read
    without it. :attr:`incomplete` is ``None`` only for a complete total.
    """

    runs: Mapping[str, Mapping[str, Any]]
    cost_usd: float
    api_equivalent_usd: float
    tokens: Mapping[str, int]
    tokens_unknown: Mapping[str, int]
    unavailable: Tuple[str, ...]
    legacy_lines: int
    unreadable_lines: int
    spool_pending: int
    spool_unreadable: bool
    spool_conflicts: int
    spool_quarantined: int
    torn_tail: bool
    unlocked_read: bool

    @property
    def incomplete(self) -> Optional[str]:
        """Why the total is not complete, the line a summing reader shows next to it, or ``None``."""
        if self.unlocked_read:
            return "onvolledig: gelezen zonder lockbestand, want dat bestaat niet en is niet aan te maken"
        if self.spool_unreadable:
            return "onvolledig: de spool is niet te lezen"
        if self.spool_conflicts:
            return (f"onvolledig: {_counted(self.spool_conflicts, 'run in de spool botst', 'runs in de spool botsen')}"
                    " met een andere uitvoering in het grootboek")
        if self.spool_quarantined:
            return (f"onvolledig: {_counted(self.spool_quarantined, 'spoolbestand staat', 'spoolbestanden staan')}"
                    " in quarantaine")
        if self.spool_pending:
            return f"onvolledig: {_counted(self.spool_pending, 'run wacht', 'runs wachten')} op replay"
        if self.torn_tail:
            return "onvolledig: het grootboek eindigt op een afgebroken regel"
        if self.unreadable_lines:
            unreadable = _counted(self.unreadable_lines, "onleesbare regel", "onleesbare regels")
            return f"onvolledig: {unreadable} in het grootboek"
        return None

    @property
    def complete_runs(self) -> int:
        """The runs whose ``usage_completeness`` is exactly ``complete``; a missing or other value never counts."""
        return sum(1 for record in self.runs.values() if record.get("usage_completeness") == "complete")

    @property
    def unknown_token_runs(self) -> int:
        """The runs with at least one unknown token count (a ``null`` or a missing field)."""
        return sum(1 for record in self.runs.values() if _has_unknown_token(record))


def _counted(count: int, one: str, many: str) -> str:
    """``count`` and the noun phrase in the singular for one, else in the plural."""
    return f"{count} {one if count == 1 else many}"


def _parse_lines(text: str) -> Tuple[List[Dict[str, Any]], int, int]:
    """``(run records in ledger order, legacy_lines, unreadable_lines)``: the one reading of the ledger text."""
    records: List[Dict[str, Any]] = []
    legacy = unreadable = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if isinstance(record, dict) and "schema" not in record:
            legacy += 1
        elif isinstance(record, dict) and record["schema"] == RUN_SCHEMA and isinstance(record.get("run_id"), str) \
                and record["run_id"] and _renumbered_from_is_valid(record):
            records.append(record)
        else:
            unreadable += 1
    return records, legacy, unreadable


def _split_torn_tail(text: str) -> Tuple[str, bool]:
    """``(the whole lines, torn)``: a last line without ``\\n`` is a torn tail, cut off here.

    A reader changes nothing: the tail counts as one unreadable line, whole JSON
    or not, until an exclusive holder of the lock file ends it.
    """
    if not text or text.endswith("\n"):
        return text, False
    return text[: text.rfind("\n") + 1], True


def _read_the_ledger(ledger_path: Path, deadline: Optional[float] = None) -> str:
    """The ledger text, read under a shared ``flock`` so no append of an old-version writer is read half written.

    No ledger file is an empty ledger. Any other error to open or read it is
    raised: an unreadable ledger is never an empty total. With ``deadline``
    the ``flock`` is tried until then and :class:`_LockBusy` is raised when an
    older writer kept it.
    """
    try:
        fh = ledger_path.open("r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    with fh:
        if deadline is None:
            fcntl.flock(fh, fcntl.LOCK_SH)
        elif not _flock_until(fh, deadline, fcntl.LOCK_SH):
            raise _LockBusy(f"the ledger {ledger_path} stayed flocked by an older writer until the lock wait ran out")
        return fh.read()


def _count_the_quarantine(spool_dir: Path) -> Tuple[int, bool]:
    """``(spool files in quarantine, unlistable)``: a missing quarantine holds none, an unlistable one is unreadable."""
    try:
        with os.scandir(spool_dir / QUARANTINE_DIR_NAME) as entries:
            return sum(1 for entry in entries if not entry.name.startswith(".")), False
    except (FileNotFoundError, NotADirectoryError):
        return 0, False
    except OSError as exc:
        logger.warning("cost_ledger: the spool quarantine in %s cannot be listed, the total is incomplete: %s",
                       spool_dir, exc)
        return 0, True


def _count_the_spool(spool_dir: Path) -> Tuple[List[List[Dict[str, Any]]], bool, int]:
    """``(spooled runs, spool_unreadable, spool_quarantined)``: the records of each spool file that reads as one run's.

    One list of records per spool file, so :func:`aggregate_runs` can tell
    the waiting runs from the ones that landed, and :func:`_second_executions`
    compares them with the ledger. A spool that cannot be listed, a spool file
    that cannot be read and one that holds a record of another run
    (:func:`_read_spool_file`) make the spool unreadable, never empty; such a
    file counts for no run. The quarantine is counted beside it.
    """
    quarantined, unlistable = _count_the_quarantine(spool_dir)
    try:
        names = _spool_names(spool_dir)
    except OSError as exc:
        logger.warning("cost_ledger: the spool %s cannot be listed, the total is incomplete: %s", spool_dir, exc)
        return [], True, quarantined
    spooled: List[List[Dict[str, Any]]] = []
    unreadable = unlistable
    for name in names:
        try:
            spooled.append(_read_spool_file(spool_dir / name))
        except (OSError, _UnreadableSpool) as exc:
            logger.warning("cost_ledger: the spool file %s cannot be read, the total is incomplete: %s", name, exc)
            unreadable = True
    return spooled, unreadable, quarantined


def _create_the_lock_file(lock_path: Path) -> Optional[int]:
    """The lock file created and opened, or ``None`` when it cannot be created (a store a reader may not write).

    Never under a test runner in the real central store: that raises, as
    every write surface does.
    """
    refuse_real_central_store_write_under_test_runner(lock_path)
    try:
        return os.open(lock_path, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
    except OSError as exc:
        logger.warning("cost_ledger: the lock file %s does not exist and cannot be created (%s): the ledger is read "
                       "without it and the total is incomplete", lock_path, exc)
        return None


@contextmanager
def _shared_lock(ledger_path: Path, deadline: Optional[float] = None) -> Iterator[bool]:
    """Hold the lock file shared and yield ``True``; yield ``False`` when it does not exist and cannot be created.

    An existing lock file is opened read-only, so a reader needs no write
    access to the store (OI-2241). Only a missing one is created, as every
    actor would; when that fails too, the read goes on without the lock and
    the total says so (``unlocked_read``). Without ``deadline`` the wait is
    blocking; with one (a ``time.monotonic()`` value) the lock is tried with
    ``LOCK_NB`` until then and :class:`_LockBusy` is raised when it stayed
    taken exclusively, so a reader that must not hang behind a writer (the
    beacon of the tick) never does.
    """
    lock_path = provider_costs.ledger_lock_path(ledger_path)
    try:
        fd: Optional[int] = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        fd = _create_the_lock_file(lock_path)
    if fd is None:
        yield False
        return
    try:
        if deadline is None:
            fcntl.flock(fd, fcntl.LOCK_SH)
        elif not _flock_until(fd, deadline, fcntl.LOCK_SH):
            raise _LockBusy(f"the lock file {lock_path} stayed taken exclusively until the lock wait ran out")
        yield True
    finally:
        os.close(fd)


_LedgerRead = Tuple[str, List[List[Dict[str, Any]]], bool, int, bool]


@contextmanager
def _ledger_and_spool(ledger_path: Path, deadline: Optional[float] = None) -> Iterator[_LedgerRead]:
    """``(ledger text, spooled runs, spool_unreadable, spool_quarantined, unlocked_read)`` at one moment.

    One scope, so a total and its backlog agree: the lock file shared, then
    the ledger's own shared ``flock``. Every writer holds the lock file
    exclusively from its decision to its last write, so no line lands in the
    ledger or enters the spool between the two reads (blocker 4 of #2141).
    The lock file stays held until the ``with`` block ends, so a caller that
    acts on the read (the beacon, :func:`report_spool_health`) acts before any
    writer changes what it read. Nothing is changed, a torn tail included. No
    events directory means nothing was ever written: empty, and nothing is
    created. A spool that cannot be read is reported unreadable, never empty
    (blocker 6 of #2141, :func:`_count_the_spool`). A lock file that is
    missing and cannot be created is read past (:func:`_shared_lock`). With
    ``deadline`` both waits, the lock file and the ledger ``flock``, end there
    with :class:`_LockBusy`; without it they block, as for the aggregation.
    """
    try:
        os.stat(ledger_path.parent)
    except FileNotFoundError:
        yield "", [], False, 0, False
        return
    with _shared_lock(ledger_path, deadline) as locked:
        text = _read_the_ledger(ledger_path, deadline)
        spooled, unreadable, quarantined = _count_the_spool(spool_dir_for(ledger_path))
        yield text, spooled, unreadable, quarantined, not locked


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _priced_amounts(record: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    """``(cost_usd, api_equivalent_usd)`` of a priced run, or ``None`` when it has no price."""
    source = record.get("cost_source")
    if not isinstance(source, str) or not (source == _PROVIDER_REPORTED or source.startswith(_PRICE_TABLE)):
        return None
    cost = _number(record.get("cost_usd"))
    api = _number(record.get("api_equivalent_usd"))
    required = api if record.get("billing") == "subscription" else cost
    if required is None:
        return None
    return cost or 0.0, api or 0.0


def aggregate_runs(
    ledger_path: Optional[Path] = None,
    *,
    select: Optional[Callable[[Mapping[str, Any]], bool]] = None,
) -> LedgerTotals:
    """Sum the ledger per run: the last line of each ``run_id`` counts, nothing else.

    ``select`` filters on that effective record, so a correction that moves a
    run out of the selection takes the whole run out. The ledger is read and
    the spool counted in one scope under the shared lock file
    (:func:`_ledger_and_spool`); a total that misses spooled runs always comes
    with them in ``spool_pending``, and with ``incomplete`` set. A spool file
    whose records all landed already, each with its content
    (:func:`_has_landed`), waits for nothing and is not counted; one whose
    ``record_id`` is in the ledger with other content still waits. A
    spooled run that is a second execution of a run in the ledger is counted in
    ``spool_conflicts`` as well; the run's total stays its ledger lines.
    """
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    with _ledger_and_spool(path) as read:
        pass
    return _totals(read, select)


GROUP_KEYS = ("model", "role", "day", "provider")
UNKNOWN_GROUP = "(unknown)"


@dataclass(frozen=True)
class GroupedTotals:
    """The ledger per group from one read: ``totals`` is the whole selection and carries the read-level counts.

    Every entry of ``groups`` is a :class:`LedgerTotals` over that group's
    effective runs; its read-level fields are zero because the spool and the
    line counts belong to the read and are counted once, on ``totals``.
    """

    key: str
    groups: Mapping[str, LedgerTotals]
    totals: LedgerTotals


def aggregate_groups(
    key: str,
    ledger_path: Optional[Path] = None,
    *,
    select: Optional[Callable[[Mapping[str, Any]], bool]] = None,
) -> GroupedTotals:
    """Sum the ledger per group (``model``, ``role``, ``day`` or ``provider``), in one read.

    The ledger is read and the spool counted once (:func:`_ledger_and_spool`);
    ``totals`` is what :func:`aggregate_runs` returns for that read, and every
    group is a :class:`LedgerTotals` over its effective runs, summed by the same
    :func:`_sum_runs`. ``day`` is the UTC date of ``occurred_from``, never
    ``recorded_at``; a record whose key field is missing or not a non-empty
    string is grouped in ``(unknown)``, never dropped. ``select`` filters on the
    effective record, so a correction that moves a run out of the selection takes
    the whole run out. An unknown ``key`` raises ``ValueError`` before anything
    is read.
    """
    if key not in GROUP_KEYS:
        raise ValueError(f"group key {key!r} is not one of {list(GROUP_KEYS)}")
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    with _ledger_and_spool(path) as read:
        pass
    totals = _totals(read, select)
    return GroupedTotals(key=key, groups=_groups_of(key, totals.runs), totals=totals)


def totals_for_runs(runs: Mapping[str, Mapping[str, Any]]) -> LedgerTotals:
    """A :class:`LedgerTotals` over these effective records, summed as :func:`aggregate_runs` does.

    For a caller that already holds one read's records and needs the same sums
    over a subset of them (the report's calibration window); the read-level
    counts are zero, they belong to the read, never to a subset of it.
    """
    return _totals_with(runs)


def _groups_of(key: str, runs: Mapping[str, Mapping[str, Any]]) -> Dict[str, LedgerTotals]:
    """``{group: LedgerTotals}`` for these effective runs; the read-level counts stay on the result's totals."""
    grouped: Dict[str, Dict[str, Mapping[str, Any]]] = {}
    for run_id, record in runs.items():
        grouped.setdefault(_group_of(record, key), {})[run_id] = record
    return {name: _totals_with(grouped[name]) for name in sorted(grouped)}


def _group_of(record: Mapping[str, Any], key: str) -> str:
    """The group name of an effective record: its key field, the UTC day, or ``(unknown)``."""
    value = utc_day(record.get("occurred_from")) if key == "day" else record.get(key)
    return value if isinstance(value, str) and value else UNKNOWN_GROUP


def utc_day(value: object) -> Optional[str]:
    """The UTC date (``YYYY-MM-DD``) of an ISO-8601 timestamp, or ``None`` when it is not one.

    A missing value, a non-string, one that does not parse and one without a
    timezone give ``None``: a day is never guessed from local time.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).date().isoformat() if parsed.tzinfo else None


def _is_count(value: object) -> bool:
    """True for a token count: an ``int`` that is not a ``bool``."""
    return isinstance(value, int) and not isinstance(value, bool)


def _has_unknown_token(record: Mapping[str, Any]) -> bool:
    """True when the run has at least one token count that is not an int (``null`` or missing)."""
    counts = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
    return any(not _is_count(counts.get(name)) for name in TOKEN_FIELDS)


def _sum_runs(
    runs: Mapping[str, Mapping[str, Any]],
) -> Tuple[float, float, Dict[str, int], Dict[str, int], Tuple[str, ...]]:
    """``(cost_usd, api_equivalent_usd, tokens, tokens_unknown, unavailable)`` of these effective runs.

    The one summing loop: a run without a price is in no sum (it is listed in
    ``unavailable``) and an unknown token count is counted, never summed as 0.
    """
    cost = api = 0.0
    tokens = dict.fromkeys(TOKEN_FIELDS, 0)
    unknown = dict.fromkeys(TOKEN_FIELDS, 0)
    unavailable: List[str] = []
    for run_id, record in runs.items():
        counts = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
        for name in TOKEN_FIELDS:
            value = counts.get(name)
            if _is_count(value):
                tokens[name] += value
            else:
                unknown[name] += 1
        amounts = _priced_amounts(record)
        if amounts is None:
            unavailable.append(run_id)
            continue
        cost += amounts[0]
        api += amounts[1]
    return round(cost, 8), round(api, 8), tokens, unknown, tuple(unavailable)


def _totals_with(runs: Mapping[str, Mapping[str, Any]]) -> LedgerTotals:
    """A :class:`LedgerTotals` over these runs, the read-level counts zero (:func:`_totals` sets them)."""
    cost, api, tokens, unknown, unavailable = _sum_runs(runs)
    return LedgerTotals(
        runs=runs, cost_usd=cost, api_equivalent_usd=api, tokens=tokens, tokens_unknown=unknown,
        unavailable=unavailable, legacy_lines=0, unreadable_lines=0, spool_pending=0, spool_unreadable=False,
        spool_conflicts=0, spool_quarantined=0, torn_tail=False, unlocked_read=False,
    )


def _totals(read: _LedgerRead, select: Optional[Callable[[Mapping[str, Any]], bool]]) -> LedgerTotals:
    """The :class:`LedgerTotals` of one read of the ledger and the spool (:func:`_ledger_and_spool`)."""
    text, spooled, spool_unreadable, quarantined, unlocked = read
    whole, torn = _split_torn_tail(text)
    records, legacy, unreadable = _parse_lines(whole)
    by_id, origins = _lines_by_record_id(records), _renumbered_origins(records)
    waiting = [run for run in spooled if not all(_has_landed(record, by_id, origins) for record in run)]
    last = {record["run_id"]: record for record in records}
    runs = {run_id: record for run_id, record in last.items() if select is None or select(record)}
    return replace(
        _totals_with(runs), legacy_lines=legacy, unreadable_lines=unreadable + int(torn), torn_tail=torn,
        spool_pending=len(waiting), spool_unreadable=spool_unreadable,
        spool_conflicts=len(_second_executions(records, waiting)), spool_quarantined=quarantined,
        unlocked_read=unlocked,
    )




# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------

RENUMBERED_EVENT = "cost_ledger_renumbered"
QUARANTINED_EVENT = "cost_ledger_spool_quarantined"
_LOCK_POLL_SEC = 0.05


@dataclass
class ReplayResult:
    """What one :func:`replay_spool` call did; ``stopped`` says why it ended with files left, else ``None``.

    ``stopped`` is ``lock_busy`` (the lock file, under it the ledger
    ``flock`` a writer of an older fabric version held, or the receipt
    ledger's append lock was not obtained within the bounds; the lock file is
    released, the file it was on stays and nothing more was written),
    ``batch`` or ``budget`` (the bound of one call), ``spool_unreadable`` (the
    spool directory could not be listed), ``write_failed`` (a ledger write,
    its fsync, its read-back, a quarantine or a receipt failed; the file it
    was on stays) or ``readback_mismatch`` (a record of a file is in the ledger
    under its ``record_id`` with other content; the file stays, nothing more
    is written for it, and the call goes on with the next file).
    ``detail`` says what happened. ``files_mismatched`` counts the files kept
    for a mismatch. ``files_remaining`` counts the files this call listed, or
    met as files a renumbering waited on, that are still in the spool.
    """

    files_replayed: int = 0
    records_written: int = 0
    records_already_landed: int = 0
    records_renumbered: int = 0
    files_quarantined: int = 0
    files_mismatched: int = 0
    files_remaining: int = 0
    stopped: Optional[str] = None
    detail: Optional[str] = None

    @property
    def exit_code(self) -> int:
        """0 done or bounded; 1 an unreadable spool directory, a write failure or a mismatch; 3 a lock busy."""
        if self.stopped == "lock_busy":
            return 3
        failed = self.stopped in ("spool_unreadable", "write_failed", "readback_mismatch")
        return 1 if failed or self.files_mismatched else 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class _ReplayError(Exception):
    """A spool file the replay could not finish: the fsync failed, the read-back missed a line, or a receipt failed.

    The file stays.
    """


class _LockBusy(Exception):
    """A bounded lock wait ran out: under the lock file the ledger ``flock`` or the receipt lock, or the beacon's.

    In the replay nothing more is written for the file and it stays;
    :func:`_replay_under_the_lock` releases the lock file. The beacon
    (:func:`report_spool_health`) is then not written this time.
    """


class _ReadbackMismatch(Exception):
    """A record of a spool file reads back from the ledger as a line with its ``record_id`` and other content.

    Found before the file's records are written (a line that only claims to
    be the record) or after (a line that came back changed). The file stays,
    nothing more is written for it, and the replay goes on with the next file
    (``readback_mismatch``, :func:`_drain`).
    """

    def __init__(self, run_id: str, record_id: str, fields: Sequence[str]):
        super().__init__(f"record {record_id} of run {run_id} reads back from the ledger with other content in "
                         f"the fields {list(fields)}: the spool file stays and nothing more is written for it")
        self.run_id, self.record_id, self.fields = run_id, record_id, list(fields)


class _RenumberingWaits(Exception):
    """A renumbering that cannot count the attempts yet: ``names`` are the spool files that cannot be read.

    Raised while the file is planned, so before anything of it is written. The
    replay quarantines those files first, each one unit of its bounds, and
    then tries the file again (:func:`_drain`).
    """

    def __init__(self, names: Sequence[str]):
        super().__init__(f"the renumbering waits on the spool files that cannot be read: {list(names)}")
        self.names = list(names)


@dataclass(frozen=True)
class _Bounds:
    """The bounds of one replay call: ``batch`` files in ``budget`` seconds from ``started``, ``wait`` seconds per lock."""

    batch: int
    budget: float
    wait: float
    started: float

    def reached(self, handled: int) -> Optional[str]:
        """``batch`` or ``budget`` when the call may start no next file after ``handled`` files, else ``None``."""
        if handled >= self.batch:
            return "batch"
        if time.monotonic() - self.started >= self.budget:
            return "budget"
        return None

    def ledger_deadline(self, asked: float) -> float:
        """Until when a lock of one file is tried: what is left of the wait begun at ``asked``, and of the budget.

        The lock file, and under it the ledger ``flock`` and the receipt
        ledger's append lock: no wait may outlast the call's budget (OI-2250),
        and the two under the lock file share what is left of the first wait,
        because the replay holds the lock file meanwhile.
        """
        return min(asked + self.wait, self.started + self.budget)


@dataclass
class _Plan:
    """What the replay does with one spool file: append ``writes`` in order; ``landed`` records were in the ledger.

    ``renumbered`` holds the lines of the file's second execution and of the
    corrections that followed a renumbered line, written now or by an earlier
    replay that did not get to delete the file. ``expected`` holds, per
    spooled record in order, the ledger line it must read back as.
    """

    writes: List[Dict[str, Any]]
    landed: int
    renumbered: List[Mapping[str, Any]]
    expected: List[Dict[str, Any]]


def _setting(given: Optional[float], spec: Tuple[str, float], cast: Callable[[str], float]) -> float:
    """``given``, else the environment variable of ``spec``, else its default.

    An environment value that is no number >= 0 is logged and replaced by the default.
    """
    if given is not None:
        return given
    name, default = spec
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        value = -1
    if not value >= 0:
        logger.warning("cost_ledger: %s=%r is not a number >= 0, the default %s applies", name, raw, default)
        return default
    return value


def _flock_until(target: Any, deadline: float, operation: int = fcntl.LOCK_EX) -> bool:
    """``flock`` ``target``, a descriptor or a file, with ``operation`` and ``LOCK_NB``, retried until ``deadline``.

    ``operation`` is ``LOCK_EX`` (the default) or ``LOCK_SH``. ``True`` when
    held, ``False`` when it stayed taken. One try is always made, also at or
    past the deadline. Any other ``OSError`` is raised.
    """
    while True:
        try:
            fcntl.flock(target, operation | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(_LOCK_POLL_SEC, left))


def _lock_within(ledger_path: Path, deadline: float) -> Optional[int]:
    """The lock file held exclusively, as an open descriptor, or ``None`` when it stayed taken until ``deadline``.

    ``LOCK_NB``, retried until the ``time.monotonic()`` value ``deadline``
    (:meth:`_Bounds.ledger_deadline`: the lock wait, clamped to what is left
    of the budget), so a replay never queues behind a writer for longer than
    either (OI-2250). The caller closes the descriptor, which releases the
    lock.
    """
    fd = os.open(provider_costs.ledger_lock_path(ledger_path), os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        held = _flock_until(fd, deadline)
    except OSError:
        os.close(fd)
        raise
    if held:
        return fd
    os.close(fd)
    return None


def _open_locked_until(ledger_path: Path, deadline: float):
    """The ledger opened for append and read and ``flock``ed exclusively, or ``None`` when it stayed taken until ``deadline``.

    The replay's :func:`_open_locked`. A writer of an older fabric version
    takes the ledger ``flock`` and never the lock file, so a blocking
    ``flock`` here would keep the lock file, and every current writer behind
    it, for as long as that writer holds the ledger. Released by closing the
    file, as there.
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    fh = ledger_path.open("a+", encoding="utf-8")
    try:
        held = _flock_until(fh, deadline)
    except OSError:
        fh.close()
        raise
    if held:
        return fh
    fh.close()
    return None


def _receipts_file(ledger_path: Path) -> Path:
    """The receipt ledger of the store the ledger is in: ``t0_receipts.ndjson`` in that store's state dir.

    For the ledger of the project's own store (the data dir
    ``vnx_paths.resolve_paths`` resolves) the state dir is the resolved
    ``VNX_STATE_DIR``, so the replay's receipts land beside every other
    ``state_mutation`` receipt of the project, also when ``VNX_STATE_DIR``
    points away from ``<data_dir>/state`` (OI-2250). A ledger of another
    store has no state dir the resolver knows: there it is ``<data_dir>/state``.
    A store that cannot be resolved raises :class:`_ReplayError`: a receipt is
    never booked in a ledger that is only a guess.
    """
    from vnx_paths import resolve_paths

    data_dir = Path(ledger_path).parent.parent
    try:
        paths = resolve_paths()
    except (OSError, ValueError) as exc:
        raise _ReplayError(f"the project's state dir cannot be resolved ({exc}): no receipt was booked") from exc
    if os.path.realpath(paths["VNX_DATA_DIR"]) == os.path.realpath(data_dir):
        return Path(paths["VNX_STATE_DIR"]) / RECEIPTS_NAME
    return data_dir / "state" / RECEIPTS_NAME


def _emit_receipt(ledger_path: Path, event_type: str, deadline: float, **fields: Any) -> None:
    """One ``state_mutation`` receipt in the store's receipt ledger, through the canonical append helper.

    ``governance_receipts`` imports ``append_receipt`` from ``scripts/``, so
    that directory goes on the path first. ``section`` must be in ``fields``:
    it is one of the appender's idempotency fields, and without it two
    receipts of one event in the same second share one key and the second is
    dropped as a duplicate. The replay holds the lock file meanwhile, so the
    append lock is tried until ``deadline`` only (a ``time.monotonic()``
    value, :meth:`_Bounds.ledger_deadline`): still taken then raises
    :class:`_LockBusy` with nothing written. A receipt that is not written
    for any other reason raises :class:`_ReplayError`.
    """
    scripts_dir = str(Path(__file__).resolve().parent.parent)
    if scripts_dir not in sys.path:
        sys.path.append(scripts_dir)
    from governance_receipts import AppendReceiptError, emit_governance_receipt

    try:
        emit_governance_receipt(event_type, receipt_kind="state_mutation", status="success",
                                source="cost_ledger_replay", receipts_file=str(_receipts_file(ledger_path)),
                                lock_timeout=max(0.0, deadline - time.monotonic()), ledger=str(ledger_path), **fields)
    except AppendReceiptError as exc:
        if exc.code == "lock_timeout":
            raise _LockBusy(f"the receipt ledger's append lock stayed taken until the lock wait or the budget ran "
                            f"out: the {event_type} receipt was not written and the spool file stays") from exc
        raise _ReplayError(f"the {event_type} receipt was not written ({exc})") from exc
    except (OSError, ValueError) as exc:
        raise _ReplayError(f"the {event_type} receipt was not written ({exc})") from exc


def _booked(
    ledger_path: Path, event_type: str, match: Callable[[Mapping[str, Any]], bool], what: str,
) -> List[Dict[str, Any]]:
    """The receipts of ``event_type`` in the store's receipt ledger that ``match`` accepts, in ledger order.

    A receipt ledger that cannot be read raises :class:`_ReplayError`: without
    knowing, a receipt is neither written twice nor left out. ``what`` names
    the receipt in that error.
    """
    receipts = _receipts_file(ledger_path)
    found: List[Dict[str, Any]] = []
    try:
        with receipts.open("r", encoding="utf-8", errors="replace") as fh:
            for text in fh:
                if event_type not in text:
                    continue
                try:
                    receipt = json.loads(text)
                except ValueError:
                    continue
                if isinstance(receipt, dict) and receipt.get("event_type") == event_type and match(receipt):
                    found.append(receipt)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise _ReplayError(f"the receipt ledger {receipts} cannot be read ({exc}): the {event_type} receipt "
                           f"of {what} was not written") from exc
    return found


def _renumbering_booked(ledger_path: Path, record_id: str) -> bool:
    """True when the receipt ledger holds the renumbering receipt of the line ``record_id`` already."""
    return bool(_booked(ledger_path, RENUMBERED_EVENT, lambda receipt: receipt.get("record_id") == record_id,
                        record_id))


def _content_digest(path: Path) -> str:
    """``sha256:<hex>`` of the spool file's bytes, or ``unreadable:<errno>`` when they cannot be read.

    With the file name, the key of a quarantine decision (:func:`_quarantine`).
    A file that is gone raises ``FileNotFoundError``.
    """
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        raise
    except OSError as exc:
        return f"unreadable:{errno.errorcode.get(exc.errno, exc.errno)}"


def _is_quarantine_name(name: str, spool_name: str) -> bool:
    """True when ``name`` is one the quarantine gives the spool file ``spool_name``: itself, or with a ``.<n>`` suffix."""
    suffix = name[len(spool_name) + 1:]
    return name == spool_name or (name.startswith(spool_name + ".") and suffix.isdigit())


def _quarantine_target(ledger_path: Path, path: Path, digest: str) -> Tuple[Path, bool]:
    """``(target, booked)``: where the spool file ``path`` goes in quarantine, and whether that decision is booked.

    A booked decision for this file name and content digest whose target is
    still free is a move that did not happen: it is finished there. Otherwise
    the file gets its own name, or the first free ``.<n>`` suffix: never a name
    that is in quarantine, and never one a booked decision for this file name
    holds. A target is always in this store's quarantine, whatever path a
    receipt names.
    """
    target_dir = path.parent / QUARANTINE_DIR_NAME
    held: Set[str] = set()
    for receipt in _booked(ledger_path, QUARANTINED_EVENT, lambda r: r.get("spool_file") == path.name, path.name):
        name = Path(str(receipt.get("quarantined_as") or "")).name
        if not _is_quarantine_name(name, path.name):
            continue
        held.add(name)
        if receipt.get("content_digest") == digest and not _exists(target_dir / name):
            return target_dir / name, True
    target, suffix = target_dir / path.name, 0
    while target.name in held or _exists(target):
        suffix += 1
        target = target_dir / f"{path.name}.{suffix}"
    return target, False


def _quarantine(ledger_path: Path, path: Path, why: str, result: ReplayResult, deadline: float) -> None:
    """Move the spool file ``path`` to ``quarantine/`` (never delete it): first the decision booked, then the move (OI-2243).

    The caller holds the lock file exclusively. The receipt goes into the
    receipt ledger before the rename (ADR-005): a receipt that fails moves
    nothing, the file stays in the spool and the call fails; one whose lock
    stays taken until ``deadline`` moves nothing either (:class:`_LockBusy`). A rename that
    fails after its receipt leaves the decision booked; the next replay finds
    it by the file name and the content digest (:func:`_quarantine_target`)
    and only finishes the move, without a second receipt. A name already in
    quarantine is never overwritten. A file that is gone is skipped.
    """
    try:
        digest = _content_digest(path)
    except FileNotFoundError:
        return
    target, booked = _quarantine_target(ledger_path, path, digest)
    if not booked:
        _emit_receipt(ledger_path, QUARANTINED_EVENT, deadline, spool_file=path.name, content_digest=digest,
                      quarantined_as=str(target), reason=why, section=target.name)
    target.parent.mkdir(exist_ok=True)
    os.rename(path, target)
    _fsync_dir(target.parent)
    _fsync_dir(path.parent)
    result.files_quarantined += 1
    logger.warning("cost_ledger: the spool file %s went to quarantine as %s: %s", path.name, target, why)


def _unreadable_spool_files(spool_dir: Path) -> List[str]:
    """The names of the spool files that cannot be read as one run's. A spool that cannot be listed raises."""
    unreadable: List[str] = []
    for name in _spool_names(spool_dir):
        try:
            _read_spool_file(spool_dir / name)
        except FileNotFoundError:
            continue
        except (OSError, _UnreadableSpool):
            unreadable.append(name)
    return unreadable


def _the_landed_line(
    record: Mapping[str, Any], line: Dict[str, Any], lines: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """``line``, what the spooled ``record`` landed as, when every ledger line with that id holds it exactly.

    A line that has the ``record_id`` and other content raises
    :class:`_ReadbackMismatch`: the ``record_id`` alone never proves a record
    landed, and the spool file is then the only copy of what it holds.
    """
    fields = _differing_fields(line, lines)
    if fields:
        raise _ReadbackMismatch(record["run_id"], record["record_id"], fields)
    return line


def _plan_the_file(
    ledger_records: Sequence[Mapping[str, Any]], spooled: Sequence[Mapping[str, Any]], next_run_id: Callable[[], str],
) -> _Plan:
    """What to write for one spool file, from what the ledger holds (ADR-043 §6).

    A record already in the ledger, as itself or as its renumbered line, is
    not written again, and it must be there with its content: a line with its
    ``record_id`` and other fields raises :class:`_ReadbackMismatch` before
    anything is written (:func:`_the_landed_line`). A correction of a record a
    replay renumbered, in this file or in an earlier call, follows it: it goes
    under that line's run id, its ``supersedes`` naming that line
    (:func:`_followed_line`), and never gets a fresh attempt. A record that
    continues the run in the ledger (a backlog, or a correction of a record
    of that execution) is written as it is. Any other record is a second
    execution under the same ``run_id``: it is never merged into the run, it
    goes under the run id ``next_run_id`` gives (asked once, and never when
    the file's records went under a renumbered run id already, by an earlier
    replay or as a correction that followed one) as :func:`_renumber` makes
    it. The run has a backlog when none of its ledger lines came from
    elsewhere than this file.
    """
    run_id = spooled[0]["run_id"]
    by_id, origins = _lines_by_record_id(ledger_records), _renumbered_origins(ledger_records)
    chain = {record.get("record_id") for record in ledger_records if record["run_id"] == run_id}
    backlog = not chain - {record["record_id"] for record in spooled}
    plan, renamed, second = _Plan([], 0, [], []), {}, None
    for record in spooled:
        landed = _landed_as(record, by_id, origins)
        followed = None if landed is not None else _followed_line(record, renamed, origins)
        if landed is not None:
            line = _the_landed_line(record, *landed)
            plan.landed += 1
        elif followed is not None:
            line = _renumber(record, followed["run_id"], followed["record_id"])
        elif backlog or (record.get("kind") == "correction" and record.get("supersedes") in chain):
            line = dict(record)
        else:
            second = second or next_run_id()
            line = _renumber(record, second)
        if landed is None:
            plan.writes.append(line)
        if line["run_id"] != run_id:
            second = second or line["run_id"]
            renamed[record["record_id"]] = line
            plan.renumbered.append(line)
        else:
            chain.add(record["record_id"])
        plan.expected.append(line)
    return plan


def _whole_records(fh) -> List[Dict[str, Any]]:
    """The run records of the ledger's whole lines, read through the locked handle."""
    return _parse_lines(_split_torn_tail(_read_under_the_lock(fh))[0])[0]


def _fsync_the_ledger(fh, ledger_path: Path, run_id: str) -> None:
    """fsync the ledger and its directory; a failure raises :class:`_ReplayError`, so the spool file stays."""
    try:
        os.fsync(fh.fileno())
        _fsync_dir(ledger_path.parent)
    except OSError as exc:
        raise _ReplayError(f"the ledger {ledger_path} could not be fsynced ({exc}): run {run_id} keeps its spool "
                           "file, the only durable copy of its records until a fsync succeeds") from exc


def _write_the_plan(fh, ledger_path: Path, spooled: Sequence[Mapping[str, Any]], result: ReplayResult) -> _Plan:
    """Restore the boundary, write what the ledger misses of one spool file, fsync, and read it back.

    The caller holds the lock file exclusively and the ledger ``flock``ed in
    ``fh``. The fsync is made always, also when every record had landed
    already: such a record can be in the ledger unsynced (its writer's fsync
    failed, ``ledger+spool``, or a replay was killed after its append), and
    until a fsync succeeds the spool file is its only durable copy. Read back
    is every record of the file, written now or landed before: each must be a
    complete line equal to what the plan expects, field by field
    (``expected``, for a renumbered record the line the replay made of it). A
    failed fsync and a record that does not read back as its own complete
    line raise :class:`_ReplayError`; a line with its ``record_id`` and other
    content raises :class:`_ReadbackMismatch`, before the writes when the
    ledger held it already; a failed write raises its ``OSError``. Either way
    the spool file stays and the next replay compares again what landed. A
    renumbering that would count the attempts past a spool file that cannot be
    read raises :class:`_RenumberingWaits` instead, before anything is written.
    """
    _end_a_torn_tail(fh)
    project_id, producer, ref, _attempt = split_run_id(spooled[0]["run_id"])

    def next_run_id() -> str:
        unreadable = _unreadable_spool_files(spool_dir_for(ledger_path))
        if unreadable:
            raise _RenumberingWaits(unreadable)
        return run_id_for(project_id, producer, ref, _next_attempt(ledger_path, fh, project_id, producer, ref))

    plan = _plan_the_file(_whole_records(fh), spooled, next_run_id)
    for line in plan.writes:
        _append_record(fh, line)
    _fsync_the_ledger(fh, ledger_path, spooled[0]["run_id"])
    result.records_written += len(plan.writes)
    result.records_renumbered += sum(1 for line in plan.writes if "renumbered_from" in line)
    result.records_already_landed += plan.landed
    read_back = _lines_by_record_id(_whole_records(fh))
    missing = [line["record_id"] for line in plan.expected if line["record_id"] not in read_back]
    if missing:
        raise _ReplayError(f"records {missing} of run {spooled[0]['run_id']} did not read back from the ledger as "
                           "their own complete lines; the spool file stays")
    for line in plan.expected:
        fields = _differing_fields(line, read_back[line["record_id"]])
        if fields:
            raise _ReadbackMismatch(line["run_id"], line["record_id"], fields)
    return plan


def _book_the_renumbering(ledger_path: Path, renumbered: Sequence[Mapping[str, Any]], deadline: float) -> None:
    """One receipt per renumbered line, unless the receipt ledger holds it already; each tried until ``deadline``."""
    for line in renumbered:
        if not _renumbering_booked(ledger_path, line["record_id"]):
            _emit_receipt(ledger_path, RENUMBERED_EVENT, deadline, project_id=line["project_id"],
                          run_id=line["run_id"], renumbered_from=line["renumbered_from"],
                          record_id=line["record_id"], section=line["record_id"])
            logger.warning("cost_ledger: %s %s of run %s in the spool was booked under run %s (ADR-043 §6)",
                           line.get("kind"), line["record_id"], line["renumbered_from"], line["run_id"])


def _replay_file(ledger_path: Path, path: Path, deadline: float, result: ReplayResult) -> Optional[str]:
    """Bring one spool file back into the ledger and delete it, or quarantine it. The caller holds the lock file.

    ``None`` when the file is done. When the ledger ``flock`` stays taken until
    ``deadline``, nothing is written for the file and the reason is returned;
    a receipt whose lock stays taken until then raises :class:`_LockBusy`, and
    the file stays. The file is deleted only after the ledger is fsynced,
    every record of it reads back with its content and every renumbered line
    has its receipt. A file the plan refuses (a :class:`CostLedgerRefusal`,
    raised before anything of it is written) goes to quarantine, so no spool
    content ends the replay in a traceback (OI-2250).
    """
    try:
        spooled = _read_spool_file(path)
    except FileNotFoundError:
        return None
    except (OSError, _UnreadableSpool) as exc:
        _quarantine(ledger_path, path, str(exc), result, deadline)
        return None
    fh = _open_locked_until(ledger_path, deadline)
    if fh is None:
        return (f"the ledger {ledger_path} stayed flocked by a process that does not take the lock file (a writer "
                "of an older fabric version) until the lock wait or the budget ran out")
    refused: Optional[CostLedgerRefusal] = None
    try:
        plan = _write_the_plan(fh, ledger_path, spooled, result)
    except CostLedgerRefusal as exc:
        refused = exc
    finally:
        closed = _close_the_ledger(fh)
    if closed is not None:
        raise closed
    if refused is not None:
        _quarantine(ledger_path, path, f"a record of the file is no run record the replay can book ({refused})",
                    result, deadline)
        return None
    _book_the_renumbering(ledger_path, plan.renumbered, deadline)
    os.unlink(path)
    _fsync_dir(path.parent)
    result.files_replayed += 1
    return None


def _replay_under_the_lock(ledger_path: Path, path: Path, bounds: _Bounds, result: ReplayResult) -> Optional[str]:
    """:func:`_replay_file` with the lock file held exclusively and released after it.

    ``None`` when done, else why a lock was busy: the lock file, or under it
    the ledger ``flock`` or the receipt ledger's append lock, was not
    obtained within what was left of the wait and the budget
    (:meth:`_Bounds.ledger_deadline`, one deadline for all three, OI-2250).
    Nothing more is written for the file then, it stays, and the lock file is
    released before this returns, so a current writer can go on.
    """
    deadline = bounds.ledger_deadline(time.monotonic())
    fd = _lock_within(ledger_path, deadline)
    if fd is None:
        return "the lock file stayed taken until the lock wait or the budget ran out"
    try:
        return _replay_file(ledger_path, path, deadline, result)
    except _LockBusy as busy:
        return str(busy)
    finally:
        os.close(fd)


def _drain(ledger_path: Path, names: List[str], bounds: _Bounds, result: ReplayResult) -> None:
    """Replay or quarantine the spool files ``names`` in order, the lock file taken and released per file.

    Every file replayed or quarantined is one unit of the batch and the
    budget. A file whose renumbering waits on spool files that cannot be read
    (:class:`_RenumberingWaits`) wrote nothing and is no unit: the files it
    waits on go first, each one a unit, and then the file once more. When the
    bounds stop the call before that, the renumbering waits for a later call;
    the attempts are never counted past a file that cannot be read. A file met
    that way that was not listed is added to ``names``. A file whose records
    do not read back with their content (:class:`_ReadbackMismatch`) stays, is
    one unit, and the call goes on with the next file, so one such file never
    holds back the files after it; ``stopped`` then says ``readback_mismatch``
    unless a lock or a failure ended the call.
    """
    spool_dir = spool_dir_for(ledger_path)
    queue, handled, deferred = list(names), 0, set()
    while queue:
        stop = bounds.reached(handled)
        if stop:
            result.stopped = result.stopped or stop
            return
        name = queue.pop(0)
        try:
            busy = _replay_under_the_lock(ledger_path, spool_dir / name, bounds, result)
        except _ReadbackMismatch as mismatch:
            logger.warning("cost_ledger: the spool file %s stays: %s", name, mismatch)
            result.files_mismatched += 1
            if result.stopped is None:
                result.stopped, result.detail = "readback_mismatch", str(mismatch)
            handled += 1
            continue
        except _RenumberingWaits as waits:
            logger.warning("cost_ledger: the spool file %s waits, %s", name, waits)
            if name not in deferred:
                deferred.add(name)
                ahead = [other for other in waits.names if other != name]
                queue = [*ahead, name, *(other for other in queue if other not in ahead)]
                names.extend(other for other in ahead if other not in names)
            continue
        if busy is not None:
            result.stopped, result.detail = "lock_busy", busy
            return
        handled += 1


def replay_spool(
    ledger_path: Path,
    *,
    batch: Optional[int] = None,
    budget_sec: Optional[float] = None,
    lock_wait_sec: Optional[float] = None,
) -> ReplayResult:
    """Bring the spool back into the ledger, one spool file per exclusive hold of the lock file (ADR-043 §6).

    At most ``batch`` files in ``budget_sec`` seconds; what remains waits for
    the next call. Defaults: ``VNX_COST_LEDGER_REPLAY_BATCH`` (50),
    ``VNX_COST_LEDGER_REPLAY_BUDGET_SEC`` (10), ``VNX_COST_LEDGER_LOCK_WAIT_SEC``
    (5). Per file: the lock file with ``LOCK_NB`` until the wait runs out,
    then the ledger ``flock`` with ``LOCK_NB`` until what is left of the wait
    and the budget runs out (else ``lock_busy``, the lock file released and
    nothing more written), the boundary restored, the records the ledger
    misses appended, fsync (always), every record read back and compared
    field by field, the receipts (their append lock bounded by the same
    deadline, else ``lock_busy``), and only then the file deleted
    (:func:`_replay_file`). A record whose ``record_id`` is in the ledger
    with other content keeps its file (``readback_mismatch``, exit 1). A file that is not one run's goes to quarantine,
    its receipt booked before the move, as one unit of the same bounds
    (:func:`_drain`). A spool directory that cannot be listed is
    ``spool_unreadable`` and never quarantined.
    """
    path = Path(ledger_path)
    refuse_real_central_store_write_under_test_runner(path)
    batch = int(_setting(batch, REPLAY_BATCH, int))
    budget = _setting(budget_sec, REPLAY_BUDGET_SEC, float)
    wait = _setting(lock_wait_sec, LOCK_WAIT_SEC, float)
    spool_dir = spool_dir_for(path)
    result = ReplayResult()
    try:
        names = _spool_names(spool_dir)
    except OSError as exc:
        logger.warning("cost_ledger: the spool %s cannot be listed, nothing was replayed: %s", spool_dir, exc)
        result.stopped, result.detail = "spool_unreadable", f"the spool {spool_dir} cannot be listed: {exc}"
        return result
    try:
        _drain(path, names, _Bounds(batch, budget, wait, time.monotonic()), result)
    except (OSError, _Unwritten, _ReplayError) as exc:
        logger.warning("cost_ledger: the replay of %s stopped on a failure: %s", spool_dir, exc)
        result.stopped, result.detail = "write_failed", str(exc)
    result.files_remaining = sum(1 for name in names if os.path.lexists(spool_dir / name))
    return result


# ---------------------------------------------------------------------------
# The beacon and the cost ledger's own files (K2a-3c)
# ---------------------------------------------------------------------------

BEACON_COMPONENT = "cost-ledger"
# An ``ok`` beacon older than this is written again on an empty spool. The beacon is event-driven, and the reader
# calls an event-driven beacon older than 7 days ``unknown`` (``health_beacon._EVENT_DRIVEN_MAX_AGE_SECONDS``): a
# running tick keeps it fresh, so only a tick that stopped running ages it that far.
BEACON_REFRESH_SECONDS = 24 * 3600


def _beacon_path(ledger_path: Path) -> Path:
    """``<data_dir>/health/cost-ledger.json`` of the store the ledger is in."""
    return Path(ledger_path).parent.parent / "health" / f"{BEACON_COMPONENT}.json"


def _spool_holds_nothing(spool_dir: Path) -> bool:
    """True when the spool lists no spool file and its quarantine nothing; a spool that cannot be listed holds something."""
    try:
        if _spool_names(spool_dir):
            return False
    except OSError:
        return False
    quarantined, unlistable = _count_the_quarantine(spool_dir)
    return not quarantined and not unlistable


def _the_beacon_is_fresh_and_ok_or_absent(ledger_path: Path) -> bool:
    """True when there is no beacon, or one that says ``ok`` and was written at most ``BEACON_REFRESH_SECONDS`` ago.

    A beacon that cannot be read, says something else, carries no
    ``last_run_ts`` (the reader calls that one ``corrupt``) or is an ``ok``
    one past the refresh age is to be written again.
    """
    try:
        beacon = json.loads(_beacon_path(ledger_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    if not isinstance(beacon, dict) or beacon.get("status") != "ok":
        return False
    written = _number(beacon.get("last_run_ts"))
    return written is not None and time.time() - written <= BEACON_REFRESH_SECONDS


def _spool_health(totals: LedgerTotals, spool_files: int) -> Dict[str, Any]:
    """The beacon's details: ``state`` is ``degraded`` while the spool holds a file, an unreadable one or quarantine."""
    degraded = bool(spool_files or totals.spool_unreadable or totals.spool_quarantined)
    details: Dict[str, Any] = {
        "state": "degraded" if degraded else "ok", "spool_files": spool_files,
        "spool_pending": totals.spool_pending, "spool_unreadable": totals.spool_unreadable,
        "spool_quarantined": totals.spool_quarantined, "spool_conflicts": totals.spool_conflicts,
    }
    if degraded:
        unreadable = ", and it cannot all be read" if totals.spool_unreadable else ""
        details["reason"] = (
            f"the cost ledger's spool holds {_counted(spool_files, 'file', 'files')} "
            f"({_counted(totals.spool_pending, 'run', 'runs')} waiting, "
            f"{totals.spool_conflicts} in conflict) and {totals.spool_quarantined} in quarantine{unreadable}: "
            "the tick replays what waits, a quarantined file needs a person"
        )
    return details


def report_spool_health(ledger_path: Optional[Path] = None) -> Optional[str]:
    """Write the ``cost-ledger`` beacon from the spool as it is now (ADR-043 §6); the state written, or ``None``.

    ``degraded`` while the spool holds a file, a file that cannot be read or
    a file in quarantine; ``ok`` once it holds none. In the beacon file the
    state is the probe framework's (``PROBE_TO_BEACON``: ``degraded`` is
    written as ``stale``), the details carry ``state`` and the counts of
    :class:`LedgerTotals`. The beacon is event-driven
    (``expected_interval_seconds=None``): with an empty spool and no beacon,
    or one that says ``ok`` and is at most ``BEACON_REFRESH_SECONDS`` (24
    hours) old, nothing is written and no lock is taken. An ``ok`` beacon
    older than that is written again, so a tick that keeps running keeps it
    fresh and the reader's 7-day backstop (``unknown``) only catches a tick
    that stopped. Otherwise the spool is counted and the beacon written in
    one scope under the lock file held shared (:func:`_ledger_and_spool`), so
    it describes the spool as no writer has changed it since. That lock, the
    ledger ``flock`` and the beacon's own lock are tried until one deadline,
    ``VNX_COST_LEDGER_LOCK_WAIT_SEC`` (5) after the call began, as the
    replay's: no wait under the shared lock outlasts it, so a cost writer is
    never kept out longer. When one stays taken, the beacon is not written
    this time, a warning says so and ``None`` is returned, so neither the tick
    nor ``vnx costs replay`` by hand hangs on it. A beacon that cannot be
    written for another reason raises its ``OSError``; the spool, not the
    beacon, is what keeps a record.
    """
    from effectiveness_probe import PROBE_TO_BEACON
    from health_beacon import BeaconLockTimeout, HealthBeacon

    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    if _spool_holds_nothing(spool_dir_for(path)) and _the_beacon_is_fresh_and_ok_or_absent(path):
        return None
    deadline = time.monotonic() + _setting(None, LOCK_WAIT_SEC, float)
    try:
        with _ledger_and_spool(path, deadline) as read:
            details = _spool_health(_totals(read, None), len(read[1]))
            beacon = HealthBeacon(path.parent.parent, BEACON_COMPONENT, expected_interval_seconds=None)
            beacon.heartbeat_strict(status=PROBE_TO_BEACON[details["state"]], details=details, lock_deadline=deadline)
    except (_LockBusy, BeaconLockTimeout) as busy:
        logger.warning("cost_ledger: the %s beacon was not written this time: %s", BEACON_COMPONENT, busy)
        return None
    return details["state"]


@dataclass(frozen=True)
class LedgerFiles:
    """The cost ledger's own files in one store: what ``vnx doctor`` and the footprint probe know (ADR-043 §6).

    In ``events/``: the ledger (``provider_costs.ndjson``), its lock file
    (``provider_costs.lock``, created by the first actor and never removed)
    and the spool (``provider_costs.spool/``). In the spool: spool files
    (``*.json``), the temporary file an interrupted atomic spool write left
    (``tmp*.tmp``) and ``quarantine/``. ``strays`` names every other entry of
    the spool: no replay reads it and no total counts it. ``unlistable``
    names a directory that could not be listed. ``bytes`` is the size of all
    of them together.
    """

    ledger: bool
    lock_file: bool
    spool_files: int
    spool_temp_files: int
    quarantined: int
    bytes: int
    strays: Tuple[str, ...]
    unlistable: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "strays": list(self.strays), "unlistable": list(self.unlistable)}

    def findings(self) -> List[str]:
        """What needs a person, empty when nothing does: the one reading both ``vnx doctor`` entrances show."""
        findings: List[str] = []
        if self.spool_files:
            findings.append(f"{_counted(self.spool_files, 'spool file waits', 'spool files wait')} for the replay")
        if self.quarantined:
            findings.append(f"{_counted(self.quarantined, 'spool file is', 'spool files are')} in quarantine "
                            "and in no total")
        if self.strays:
            findings.append(f"{SPOOL_DIR_NAME}/ holds entries that are none of its own: {', '.join(self.strays)}")
        if self.unlistable:
            findings.append(f"cannot be listed: {', '.join(self.unlistable)}")
        return findings

    def summary(self) -> str:
        """The known files in one line: ledger, lock file, spool, quarantine, interrupted writes, bytes."""
        present = [name for name, there in (("ledger", self.ledger), ("lock file", self.lock_file)) if there]
        known = ", ".join(present) if present else "no ledger yet"
        temp = (f", {_counted(self.spool_temp_files, 'interrupted spool write', 'interrupted spool writes')}"
                if self.spool_temp_files else "")
        return (f"{known}; spool {_counted(self.spool_files, 'file', 'files')}, quarantine {self.quarantined}{temp}; "
                f"{_counted(self.bytes, 'byte', 'bytes')}")


def _file_size(path: Path) -> Optional[int]:
    """The size of a file, or ``None`` when it is not there; a stat that fails otherwise counts as 0 bytes."""
    try:
        return os.stat(path).st_size
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return 0


def _entry_bytes(entry: "os.DirEntry[str]") -> int:
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


def _quarantine_files(quarantine: Path, unlistable: List[str]) -> Tuple[int, int]:
    """``(files, bytes)`` in quarantine; the dot-files are no quarantined spool file, as in the aggregation."""
    try:
        with os.scandir(quarantine) as entries:
            kept = [entry for entry in entries if not entry.name.startswith(".")]
    except OSError:
        unlistable.append(str(quarantine))
        return 0, 0
    return len(kept), sum(_entry_bytes(entry) for entry in kept)


def _sort_the_spool(spool_dir: Path, unlistable: List[str]) -> Tuple[Dict[str, int], List[str]]:
    """``({kind: count, "bytes": n}, strays)`` for the entries of the spool directory."""
    counts = dict.fromkeys(("spool_files", "spool_temp_files", "quarantined", "bytes"), 0)
    strays: List[str] = []
    try:
        with os.scandir(spool_dir) as listed:
            entries = list(listed)
    except FileNotFoundError:
        return counts, strays
    except OSError:
        unlistable.append(str(spool_dir))
        return counts, strays
    for entry in sorted(entries, key=lambda item: item.name):
        name, is_file = entry.name, entry.is_file(follow_symlinks=False)
        if name == QUARANTINE_DIR_NAME and entry.is_dir(follow_symlinks=False):
            files, size = _quarantine_files(Path(entry.path), unlistable)
            counts["quarantined"] += files
        elif is_file and name.endswith(".json") and not name.startswith("."):
            counts["spool_files"], size = counts["spool_files"] + 1, _entry_bytes(entry)
        elif is_file and name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX):
            counts["spool_temp_files"], size = counts["spool_temp_files"] + 1, _entry_bytes(entry)
        else:
            strays.append(name)
            size = 0
        counts["bytes"] += size
    return counts, strays


def ledger_files(ledger_path: Optional[Path] = None) -> LedgerFiles:
    """The cost ledger's own files in the store of ``ledger_path`` (:class:`LedgerFiles`).

    Read-only and without a lock: nothing is created, the lock file
    included, so ``vnx doctor`` and the footprint probe can look at a store
    they may not write.
    """
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    unlistable: List[str] = []
    ledger_size = _file_size(path)
    lock_size = _file_size(provider_costs.ledger_lock_path(path))
    counts, strays = _sort_the_spool(spool_dir_for(path), unlistable)
    return LedgerFiles(
        ledger=ledger_size is not None, lock_file=lock_size is not None,
        spool_files=counts["spool_files"], spool_temp_files=counts["spool_temp_files"],
        quarantined=counts["quarantined"], bytes=(ledger_size or 0) + (lock_size or 0) + counts["bytes"],
        strays=tuple(strays), unlistable=tuple(unlistable),
    )


__all__ = [
    "RUN_SCHEMA", "TOKEN_FIELDS", "ROLES", "KINDS", "BILLINGS", "SPOOL_DIR_NAME", "QUARANTINE_DIR_NAME",
    "RENUMBERED_EVENT", "QUARANTINED_EVENT", "RENUMBERED_RECORD_ID", "TRANSCRIPT_IDENTITY",
    "BEACON_COMPONENT", "BEACON_REFRESH_SECONDS",
    "CostLedgerRefusal", "CostWithoutSource", "UnavailableWithAmount", "CorrectionWithoutSupersedes",
    "IncompleteUsageMarkedComplete", "RunIdProjectMismatch", "UnknownModel", "InvalidRecord",
    "CostLedgerWriteError", "RunCost", "WrittenRecord", "LedgerTotals", "GroupedTotals", "ReplayResult",
    "LedgerFiles", "GROUP_KEYS", "UNKNOWN_GROUP",
    "split_run_id", "run_id_for", "build_record", "record_run", "record_next_attempt", "record_attempt_once",
    "aggregate_runs",
    "aggregate_groups", "utc_day",
    "replay_spool", "spool_dir_for", "spool_file_for", "report_spool_health", "ledger_files",
]
