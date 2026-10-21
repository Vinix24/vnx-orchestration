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
  and appends the line under the ledger's ``flock`` (ADR-005: the line is in the
  ledger when this returns; the caller mutates its state after that). A record
  the plan forbids is refused with a :class:`CostLedgerRefusal` subclass and
  nothing is written. An append that fails raises :class:`CostLedgerWriteError`:
  the call fails, never silently. :func:`record_next_attempt` is the same
  writer for a producer that knows its dispatch but not the attempt number: it
  takes the next attempt from the ledger under the same lock as the append.
- :func:`aggregate_runs` is the one aggregation: per ``run_id`` only the last
  line counts (a ``correction`` replaces the earlier contribution), a run
  without a price is listed in ``unavailable`` and never summed as 0, and a line
  in the old ``emit_provider_cost`` shape (no ``schema``) is counted in
  ``legacy_lines`` and not summed.

Boundary first: every append first ends a last line without ``\\n`` with
``\\n`` and fsyncs, so a new line always starts on its own. An append that fails
halfway ends its own line at once when the disk has room, so the old writer
cannot glue onto it. No byte is ever removed: the torn prefix stays and counts
as one unreadable line.

The recovery path for a failed append, ADR-043 §6, lands in the second K2a PR
(verdict ``cap-pr2141-73c685f656862925ef20ef81d2a09090b9b08337``). The old
writer ``provider_costs.emit_provider_cost`` stays for the lanes that have not
moved yet; the builder runs on the claude harness lanes write here through
``harness_run_cost`` (K2a-2).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import provider_costs
from vnx_paths import refuse_real_central_store_write_under_test_runner

logger = logging.getLogger(__name__)

RUN_SCHEMA = "provider_cost_run/1"
TOKEN_FIELDS = ("input_uncached", "cache_hit", "cache_write_5m", "cache_write_1h", "output")
ROLES = frozenset({"builder", "review", "plan_gate", "tiebreaker", "panel", "intelligence"})
KINDS = frozenset({"charge", "correction"})
BILLINGS = frozenset({"metered", "subscription"})

_PROVIDER_REPORTED = "provider_reported"
_PRICE_TABLE = "price_table@"
_UNAVAILABLE = "unavailable:"
_SEGMENT_KEYS = frozenset({"tokens", "peak", "ts"})
_PRICE_RUN_COUNTS = (
    ("input", ("input_uncached",)), ("output", ("output",)), ("cache_read", ("cache_hit",)),
    ("cache_write", ("cache_write_5m", "cache_write_1h")),
)


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
    """The line was not appended: the ledger could not be opened, locked, written or closed.

    An ``OSError`` with the errno of its cause, which is ``__cause__``. Nothing
    is kept anywhere else, so the caller's call fails and it mutates no state.
    A write or a close that failed halfway can have left the line on disk: a
    whole line counts as the run's line, a torn one as one unreadable line that
    the next append ends first.
    """


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
    record["record_id"] = _record_id(record)
    return record


def _line(record: Mapping[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def _end_a_torn_tail(fh) -> None:
    """End a last line without ``\\n`` (a torn append) and fsync, so the next line starts on its own.

    The caller holds the ledger lock and opened the ledger for reading too.
    Append-only: the torn bytes stay and become one line, readable when the
    JSON was whole and only its ``\\n`` was missing, unreadable otherwise.
    """
    fh.flush()
    size = os.fstat(fh.fileno()).st_size
    if size and os.pread(fh.fileno(), 1, size - 1) != b"\n":
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def _append_record(fh, record: Mapping[str, Any]) -> None:
    """Append ``record`` as its own line, on a valid NDJSON boundary. The caller holds the ledger lock.

    When the append fails halfway, the line it left open is ended at once, so
    ``emit_provider_cost`` cannot glue its next line onto it. On a disk that is
    still full that fails too; the next append of this module ends it first.
    The append's own error is what the caller gets.
    """
    _end_a_torn_tail(fh)
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


def _close_the_ledger(fh) -> Optional[OSError]:
    """Close the ledger, which flushes under its lock and then releases it. The close's error is returned."""
    try:
        fh.close()
    except OSError as exc:
        logger.warning("cost_ledger: closing %s failed: %s", fh.name, exc)
        return exc
    return None


def _append_under_the_lock(ledger_path: Path, record: Mapping[str, Any]) -> None:
    """Append ``record`` under the ledger lock, then close the ledger, which releases the lock.

    Raises the ``OSError`` of whatever failed: the open, the lock, the append
    or the close. A failed append's error is the one raised; the close after it
    only logs its own. A close that fails after a whole append raises too: the
    flush it did is not confirmed.
    """
    fh = _open_locked(ledger_path)
    try:
        _append_record(fh, record)
    finally:
        closed = _close_the_ledger(fh)
    if closed is not None:
        raise closed


def _write_error(ledger_path: Path, record: Mapping[str, Any], cause: OSError) -> CostLedgerWriteError:
    return _write_error_for(ledger_path, f"cost record {record['record_id']} of run {record['run_id']}", cause)


def _write_error_for(ledger_path: Path, what: str, cause: OSError) -> CostLedgerWriteError:
    message = f"{what} was not appended to {ledger_path}: {cause}"
    if cause.errno is None:
        return CostLedgerWriteError(message)
    return CostLedgerWriteError(cause.errno, message)


def record_run(run: RunCost, *, ledger_path: Optional[Path] = None) -> Dict[str, Any]:
    """Validate, price and append one run's line; return the record as written.

    ``ledger_path`` defaults to ``provider_costs._resolve_costs_path()``.
    Raises a :class:`CostLedgerRefusal` (nothing written) for a record the plan
    forbids, and :class:`CostLedgerWriteError` when the append fails: the
    ledger could not be opened, locked, written or closed. That error is the
    caller's: nothing keeps the line elsewhere, so the caller treats the call as
    failed and mutates no state (ADR-005).
    """
    record = build_record(run)
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    refuse_real_central_store_write_under_test_runner(path)
    try:
        _append_under_the_lock(path, record)
    except OSError as exc:
        raise _write_error(path, record, exc) from exc
    return record


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


def _next_attempt(text: str, project_id: str, producer: str, ref: str) -> int:
    """One more than the highest attempt the ledger text holds for this project, producer and ref."""
    highest = 0
    for record in _parse_lines(text)[0]:
        try:
            project, run_producer, run_ref, attempt = split_run_id(record["run_id"])
        except InvalidRecord:
            continue
        if (project, run_producer, run_ref) == (project_id, producer, ref):
            highest = max(highest, attempt)
    return highest + 1


class _RecordNotBuilt(Exception):
    """An ``OSError`` raised while building the record (the registry read), not while writing it."""


def _append_next_under_the_lock(
    ledger_path: Path, record_for: Callable[[str], Dict[str, Any]], built: List[Dict[str, Any]],
) -> None:
    """Read the ledger, build the record from it and append it, all under one exclusive lock.

    ``record_for`` gets the ledger text and returns the record; it lands in
    ``built`` before the append, so a failed append can name it. The ``OSError``
    of the open, lock, read, append or close is raised as itself; one from
    ``record_for`` is wrapped in :class:`_RecordNotBuilt`, so the caller does not
    report it as a failed write. A refusal from ``record_for`` propagates with
    nothing written.
    """
    fh = _open_locked(ledger_path)
    try:
        text = _read_under_the_lock(fh)
        try:
            record = record_for(text)
        except OSError as exc:
            raise _RecordNotBuilt(str(exc)) from exc
        built.append(record)
        _append_record(fh, record)
    finally:
        closed = _close_the_ledger(fh)
    if closed is not None:
        raise closed


def record_next_attempt(
    project_id: str,
    producer: str,
    ref: str,
    make_run: Callable[[str], RunCost],
    *,
    ledger_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """:func:`record_run` for a producer that knows its run but not the run's attempt number.

    The attempt is one more than the highest attempt the ledger holds for
    ``<project_id>:<producer>:<ref>``, read under the same exclusive lock as
    the append, so two attempts never get the same ``run_id``: a refire of a
    dispatch is the next attempt, whichever process fires it. ``make_run``
    gets that ``run_id`` and returns the :class:`RunCost`. Refusals and
    :class:`CostLedgerWriteError` are those of :func:`record_run`.
    """
    run_id_for(project_id, producer, ref, 1)
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    refuse_real_central_store_write_under_test_runner(path)
    built: List[Dict[str, Any]] = []

    def record_for(text: str) -> Dict[str, Any]:
        attempt = _next_attempt(text, project_id, producer, ref)
        return build_record(make_run(run_id_for(project_id, producer, ref, attempt)))

    try:
        _append_next_under_the_lock(path, record_for, built)
    except _RecordNotBuilt as exc:
        raise exc.__cause__ from None
    except OSError as exc:
        what = f"cost record {built[-1]['record_id']} of run {built[-1]['run_id']}" if built else (
            f"the next attempt of {project_id}:{producer}:{ref}"
        )
        raise _write_error_for(path, what, exc) from exc
    return built[-1]


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
    carry another schema version or have no ``run_id``.
    """

    runs: Mapping[str, Mapping[str, Any]]
    cost_usd: float
    api_equivalent_usd: float
    tokens: Mapping[str, int]
    tokens_unknown: Mapping[str, int]
    unavailable: Tuple[str, ...]
    legacy_lines: int
    unreadable_lines: int


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
                and record["run_id"]:
            records.append(record)
        else:
            unreadable += 1
    return records, legacy, unreadable


def _read_the_ledger(ledger_path: Path) -> str:
    """The ledger text, read under a shared ``flock`` so no append is read half written.

    No ledger file is an empty ledger. Any other error to open or read it is
    raised: an unreadable ledger is never an empty total.
    """
    try:
        fh = ledger_path.open("r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    with fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        return fh.read()


def _last_line_per_run(ledger_path: Path) -> Tuple[Dict[str, Dict[str, Any]], int, int]:
    """``(last line per run, legacy_lines, unreadable_lines)``."""
    records, legacy, unreadable = _parse_lines(_read_the_ledger(ledger_path))
    return {record["run_id"]: record for record in records}, legacy, unreadable


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
    run out of the selection takes the whole run out. The ledger is read under
    a shared ``flock`` (:func:`_read_the_ledger`).
    """
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    last, legacy, unreadable = _last_line_per_run(path)
    runs = {run_id: record for run_id, record in last.items() if select is None or select(record)}
    cost = api = 0.0
    tokens = dict.fromkeys(TOKEN_FIELDS, 0)
    unknown = dict.fromkeys(TOKEN_FIELDS, 0)
    unavailable: List[str] = []
    for run_id, record in runs.items():
        counts = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
        for name in TOKEN_FIELDS:
            value = counts.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                tokens[name] += value
            else:
                unknown[name] += 1
        amounts = _priced_amounts(record)
        if amounts is None:
            unavailable.append(run_id)
            continue
        cost += amounts[0]
        api += amounts[1]
    return LedgerTotals(
        runs=runs, cost_usd=round(cost, 8), api_equivalent_usd=round(api, 8), tokens=tokens,
        tokens_unknown=unknown, unavailable=tuple(unavailable), legacy_lines=legacy,
        unreadable_lines=unreadable,
    )


__all__ = [
    "RUN_SCHEMA", "TOKEN_FIELDS", "ROLES", "KINDS", "BILLINGS",
    "CostLedgerRefusal", "CostWithoutSource", "UnavailableWithAmount", "CorrectionWithoutSupersedes",
    "IncompleteUsageMarkedComplete", "RunIdProjectMismatch", "UnknownModel", "InvalidRecord",
    "CostLedgerWriteError", "RunCost", "LedgerTotals",
    "split_run_id", "run_id_for", "build_record", "record_run", "record_next_attempt", "aggregate_runs",
]
