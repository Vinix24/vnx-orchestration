"""cost_ledger.py — the cost ledger per run (ADR-043).

Track ``kosten-per-model-in-het-grootboek``, deliverable K2a. The ledger is
``<central store>/events/provider_costs.ndjson``, resolved by
``provider_costs._resolve_costs_path`` (the one resolver). It carries one line
per run in the shape ``provider_cost_run/1``; a run is one start of one model
for one purpose: a dispatch attempt, a gate run, a plan-gate seat, a panel leg,
an intelligence call.

Three entry points, and every writer and reader goes through them:

- :func:`record_run` validates a :class:`RunCost`, prices it through
  ``provider_costs.price_run`` when the caller has tokens but no provider cost,
  and appends the line under the ledger's ``flock`` (ADR-005: the line is in the
  ledger, or in the spool, when this returns; the caller mutates its state after
  that). A record the plan forbids is refused with a :class:`CostLedgerRefusal`
  subclass and nothing is written.
- :func:`aggregate_runs` is the one aggregation: per ``run_id`` only the last
  line counts (a ``correction`` replaces the earlier contribution), a run
  without a price is listed in ``unavailable`` and never summed as 0, a line in
  the old ``emit_provider_cost`` shape (no ``schema``) is counted in
  ``legacy_lines`` and not summed, and the number of runs waiting in the spool
  comes back as ``spool_pending`` ("onvolledig: N runs wachten op replay").
- :func:`replay_spool` brings spooled records into the ledger.

When the append fails, the same record goes to
``events/provider_costs.spool/<run_id>.json`` (an atomic write of a JSON list of
records) and the ``cost-ledger`` health beacon (``<data_dir>/health/
cost-ledger.json``) reports degraded. A failing spool write raises
:class:`CostLedgerSpoolError`: never silent. While a record of a run waits in the
spool, a later record of the same run queues behind it in the spool rather than
landing in the ledger first, so "last line wins" still means "last written".

Every append, in :func:`record_run` and in the replay, first ends a torn last
line (an append that failed halfway) with ``\\n``, under the ledger lock, so a
new line always starts on its own. No byte is ever removed: the torn prefix
stays and counts as one unreadable line. The replay writes each record once per
``run_id`` + ``record_id``, fsyncs, and reads the ledger back. It removes a
spool file only when every record in it stands there as its own line, equal to
what was spooled, and the newest record of each of its runs is that run's last
line. A file that does not read back stays, and the replay raises
:class:`CostLedgerReplayError`.

The old writer ``provider_costs.emit_provider_cost`` and its callers are
untouched; the harness lanes move onto :func:`record_run` in K2a-2.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import provider_costs
from atomic_io import atomic_write_text, slot_lock
from vnx_paths import refuse_real_central_store_write_under_test_runner

logger = logging.getLogger(__name__)

RUN_SCHEMA = "provider_cost_run/1"
TOKEN_FIELDS = ("input_uncached", "cache_hit", "cache_write_5m", "cache_write_1h", "output")
ROLES = frozenset({"builder", "review", "plan_gate", "tiebreaker", "panel", "intelligence"})
KINDS = frozenset({"charge", "correction"})
BILLINGS = frozenset({"metered", "subscription"})
SPOOL_DIR_NAME = "provider_costs.spool"

_HEALTH_COMPONENT = "cost-ledger"
_PROVIDER_REPORTED = "provider_reported"
_PRICE_TABLE = "price_table@"
_UNAVAILABLE = "unavailable:"
_SEGMENT_KEYS = frozenset({"tokens", "peak", "ts"})
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._:@+-]")


class CostLedgerRefusal(ValueError):
    """The writer refused the record. Nothing was written, not to the ledger and not to the spool."""


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


class CostLedgerSpoolError(RuntimeError):
    """The record could not be spooled. The run has no line anywhere: the caller's call fails."""


class CostLedgerReplayError(OSError):
    """A replayed record did not read back from the ledger as its own line. Its spool file stays.

    An ``OSError``, so every caller that treats a failed replay as one treats
    this as one too: ``vnx costs replay`` exits 2, the prelude tick logs it.
    ``result`` is what the replay did, ``result.unconfirmed`` the files kept.
    """

    def __init__(self, message: str, result: "ReplayResult"):
        super().__init__(message)
        self.result = result


class _UnreadableSpool(Exception):
    """A spool file that is not a JSON list of ledger records."""


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


def _price_segment(segment: Mapping[str, Any]) -> Dict[str, Any]:
    tokens = segment["tokens"]
    priced: Dict[str, Any] = {
        "input": tokens["input_uncached"] or 0,
        "output": tokens["output"] or 0,
        "cache_read": tokens["cache_hit"] or 0,
        "cache_write": (tokens["cache_write_5m"] or 0) + (tokens["cache_write_1h"] or 0),
    }
    for key in ("peak", "ts"):
        if key in segment:
            priced[key] = segment[key]
    return priced


def _price(run: RunCost, model: str, tokens: Mapping[str, Optional[int]], segments) -> Tuple:
    """Price a run from the registry. Unknown usage is unavailable, never a zero."""
    if all(tokens[name] is None for name in TOKEN_FIELDS):
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
# The spool and the health beacon
# ---------------------------------------------------------------------------


def spool_dir_for(ledger_path: Path) -> Path:
    return Path(ledger_path).parent / SPOOL_DIR_NAME


def spool_file_for(spool_dir: Path, run_id: str) -> Path:
    name = _UNSAFE_FILENAME_CHARS.sub("_", run_id)
    if name.startswith("."):
        name = "_" + name[1:]
    return spool_dir / f"{name}.json"


def _spool_files(spool_dir: Path) -> List[Path]:
    if not spool_dir.is_dir():
        return []
    return sorted(path for path in spool_dir.glob("*.json") if not path.name.startswith("."))


def _is_run_record(record: object) -> bool:
    return (
        isinstance(record, dict)
        and record.get("schema") == RUN_SCHEMA
        and isinstance(record.get("run_id"), str) and bool(record["run_id"])
        and isinstance(record.get("record_id"), str) and bool(record["record_id"])
    )


def _read_spool_file(path: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _UnreadableSpool(f"{path.name}: {exc}") from exc
    if not isinstance(data, list) or not data or not all(_is_run_record(record) for record in data):
        raise _UnreadableSpool(f"{path.name}: not a non-empty list of {RUN_SCHEMA} records")
    return data


def _spool_record(spool_dir: Path, record: Mapping[str, Any], cause: Optional[BaseException] = None) -> None:
    """Add ``record`` to its run's spool file, behind what already waits there. Never silent."""
    try:
        refuse_real_central_store_write_under_test_runner(spool_dir)
        spool_dir.mkdir(parents=True, exist_ok=True)
        with slot_lock(spool_dir):
            target = spool_file_for(spool_dir, record["run_id"])
            queued = _read_spool_file(target) if target.exists() else []
            atomic_write_text(target, json.dumps([*queued, record], separators=(",", ":")))
    except (OSError, _UnreadableSpool) as exc:
        first = f" after the ledger append failed ({cause})" if cause is not None else ""
        raise CostLedgerSpoolError(
            f"cost record {record['record_id']} of run {record['run_id']} could not be spooled "
            f"to {spool_dir}{first}: {exc}"
        ) from exc


def _beacon_path(ledger_path: Path) -> Path:
    return Path(ledger_path).parent.parent / "health" / f"{_HEALTH_COMPONENT}.json"


def _report_health(
    ledger_path: Path, reason: str, unreadable: Sequence[str] = (), unconfirmed: Sequence[str] = (),
) -> None:
    """Write the ``cost-ledger`` beacon: degraded while the spool is not empty.

    The beacon vocabulary is the probe framework's: ``degraded`` is written as
    ``PROBE_TO_BEACON["degraded"]``. Best-effort, like every beacon write: the
    spool, not the beacon, is what keeps the record.
    """
    from effectiveness_probe import PROBE_TO_BEACON
    from health_beacon import HealthBeacon

    spool_dir = spool_dir_for(ledger_path)
    pending = len(_spool_files(spool_dir))
    state = "degraded" if pending or unreadable or unconfirmed else "ok"
    details = {
        "state": state, "spool_pending": pending, "unreadable": list(unreadable),
        "unconfirmed": list(unconfirmed), "reason": reason, "spool_dir": str(spool_dir),
        "ledger": str(ledger_path),
    }
    try:
        beacon = HealthBeacon(Path(ledger_path).parent.parent, _HEALTH_COMPONENT, expected_interval_seconds=None)
        beacon.heartbeat(status=PROBE_TO_BEACON[state], details=details)
    except OSError as exc:
        logger.warning("cost_ledger: could not write the %s beacon (%s): %s", _HEALTH_COMPONENT, state, exc)


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def _end_a_torn_tail(fh) -> None:
    """End a last line without ``\\n`` (a torn append), so the next line starts on its own.

    The caller holds the ledger lock and opened the ledger for reading too.
    Append-only: the torn bytes stay and become one unreadable line.
    """
    fh.flush()
    size = os.fstat(fh.fileno()).st_size
    if size and os.pread(fh.fileno(), 1, size - 1) != b"\n":
        fh.write("\n")


def _append_record(fh, record: Mapping[str, Any]) -> None:
    """Append ``record`` as its own line, on a valid NDJSON boundary. The caller holds the ledger lock."""
    _end_a_torn_tail(fh)
    fh.write(_line(record))


def _append_unless_queued(ledger_path: Path, spool_dir: Path, record: Mapping[str, Any]) -> bool:
    """Append under the ledger lock; False when the record queued behind its run's spooled ones."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            if spool_file_for(spool_dir, record["run_id"]).exists():
                _spool_record(spool_dir, record)
                return False
            _append_record(fh, record)
            fh.flush()
            return True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def record_run(run: RunCost, *, ledger_path: Optional[Path] = None) -> Dict[str, Any]:
    """Validate, price and write one run's line; return the record as written.

    ``ledger_path`` defaults to ``provider_costs._resolve_costs_path()``.
    Raises a :class:`CostLedgerRefusal` (nothing written) for a record the plan
    forbids, and :class:`CostLedgerSpoolError` when neither the ledger nor the
    spool took the line.
    """
    record = build_record(run)
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    refuse_real_central_store_write_under_test_runner(path)
    spool_dir = spool_dir_for(path)
    try:
        landed = _append_unless_queued(path, spool_dir, record)
    except OSError as exc:
        _spool_record(spool_dir, record, cause=exc)
        logger.warning("cost_ledger: append to %s failed, run %s spooled: %s", path, record["run_id"], exc)
        _report_health(path, reason=f"ledger append failed: {exc}")
        return record
    if not landed:
        _report_health(path, reason=f"run {record['run_id']} has an earlier record waiting in the spool")
    return record


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayResult:
    """What one replay did. ``pending`` counts the spool files left afterwards.

    ``unconfirmed`` names the spool files kept because their records did not
    read back from the ledger; a replay with any raises :class:`CostLedgerReplayError`.
    """

    written: int = 0
    already_present: int = 0
    removed: Tuple[str, ...] = ()
    unreadable: Tuple[str, ...] = ()
    pending: int = 0
    unconfirmed: Tuple[str, ...] = ()


def _canonical(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _read_ledger(fh) -> List[Dict[str, Any]]:
    fh.seek(0)
    return _parse_lines(fh.read())[0]


def _landed_keys(records: Sequence[Mapping[str, Any]]) -> set:
    return {(record["run_id"], record["record_id"]) for record in records if _is_run_record(record)}


def _write_missing(fh, records: Sequence[Mapping[str, Any]], landed: set) -> Tuple[int, int]:
    """Append the records whose ``run_id`` + ``record_id`` is not in the ledger: ``(written, already)``."""
    written = already = 0
    for record in records:
        key = (record["run_id"], record["record_id"])
        if key in landed:
            already += 1
            continue
        _append_record(fh, record)
        landed.add(key)
        written += 1
    return written, already


def _read_back(fh) -> Tuple[set, Dict[str, str]]:
    """The ledger as the aggregation reads it: every run record as canonical JSON, and each run's last line."""
    lines = [(record["run_id"], _canonical(record)) for record in _read_ledger(fh)]
    return {line for _run_id, line in lines}, dict(lines)


def _reads_back(spooled: Sequence[Mapping[str, Any]], present: set, last: Mapping[str, str]) -> bool:
    """Every spooled record is its own ledger line, equal to the spool, and each run's newest is its last line.

    ``present`` and ``last`` come from :func:`_read_back`, the aggregation's
    reading, so a record that reads back here is a run the aggregation counts.
    """
    newest = {record["run_id"]: _canonical(record) for record in spooled}
    return all(_canonical(record) in present for record in spooled) and all(
        last.get(run_id) == line for run_id, line in newest.items()
    )


def _replay_files(fh, spool_dir: Path) -> ReplayResult:
    landed = _landed_keys(_read_ledger(fh))
    written = already = 0
    replayed: List[Tuple[Path, List[Dict[str, Any]]]] = []
    unreadable: List[str] = []
    for target in _spool_files(spool_dir):
        try:
            records = _read_spool_file(target)
        except _UnreadableSpool as exc:
            logger.warning("cost_ledger: unreadable spool file left in place: %s", exc)
            unreadable.append(target.name)
            continue
        added, present = _write_missing(fh, records, landed)
        written += added
        already += present
        replayed.append((target, records))
    fh.flush()
    os.fsync(fh.fileno())
    present, last = _read_back(fh)
    removed: List[str] = []
    unconfirmed: List[str] = []
    for target, records in replayed:
        if _reads_back(records, present, last):
            target.unlink()
            removed.append(target.name)
        else:
            logger.warning("cost_ledger: %s did not read back from the ledger; the spool file stays", target.name)
            unconfirmed.append(target.name)
    pending = len(_spool_files(spool_dir))
    return ReplayResult(written, already, tuple(removed), tuple(unreadable), pending, tuple(unconfirmed))


def _clear_a_stale_degraded(ledger_path: Path) -> None:
    """An empty spool with a beacon still on degraded (a file set aside by hand): report ok."""
    try:
        beacon = json.loads(_beacon_path(ledger_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(beacon, dict) and beacon.get("status") != "ok":
        _report_health(ledger_path, reason="spool empty")


def replay_spool(ledger_path: Optional[Path] = None) -> ReplayResult:
    """Move every readable spool file into the ledger, once per ``run_id`` + ``record_id``.

    Holds the ledger lock and then the spool lock (the writer's order), so no
    record lands between the replay's read of the ledger and its append. Each
    append starts on a valid NDJSON boundary. After the appends are flushed and
    fsynced, still under the lock, the replay reads the ledger back; a spool
    file is removed only when its records read back (:func:`_reads_back`). An
    unreadable spool file stays and is named in the result. Raises ``OSError``
    when the ledger cannot be opened or written, and
    :class:`CostLedgerReplayError` when a record does not read back; in both
    cases the spool stays.
    """
    path = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    spool_dir = spool_dir_for(path)
    if not _spool_files(spool_dir):
        _clear_a_stale_degraded(path)
        return ReplayResult()
    refuse_real_central_store_write_under_test_runner(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8", errors="replace") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            with slot_lock(spool_dir):
                result = _replay_files(fh, spool_dir)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    if not result.unconfirmed:
        _report_health(path, reason="replay", unreadable=result.unreadable)
        return result
    reason = f"replay: {', '.join(result.unconfirmed)} did not read back from {path} as their own lines"
    _report_health(path, reason=reason, unreadable=result.unreadable, unconfirmed=result.unconfirmed)
    raise CostLedgerReplayError(f"{len(result.unconfirmed)} spool file(s) kept: {reason}", result)


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
    carry another schema version or have no ``run_id``. ``spool_pending`` is the
    number of runs waiting for a replay.
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

    @property
    def incomplete(self) -> Optional[str]:
        """The line a summing reader shows while runs wait in the spool, else ``None``."""
        if not self.spool_pending:
            return None
        return f"onvolledig: {self.spool_pending} runs wachten op replay"


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


def _last_line_per_run(ledger_path: Path) -> Tuple[Dict[str, Dict[str, Any]], int, int]:
    if not ledger_path.is_file():
        return {}, 0, 0
    with ledger_path.open("r", encoding="utf-8", errors="replace") as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        try:
            text = fh.read()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    records, legacy, unreadable = _parse_lines(text)
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
    run out of the selection takes the whole run out.
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
        unreadable_lines=unreadable, spool_pending=len(_spool_files(spool_dir_for(path))),
    )


__all__ = [
    "RUN_SCHEMA", "TOKEN_FIELDS", "ROLES", "KINDS", "BILLINGS", "SPOOL_DIR_NAME",
    "CostLedgerRefusal", "CostWithoutSource", "UnavailableWithAmount", "CorrectionWithoutSupersedes",
    "IncompleteUsageMarkedComplete", "RunIdProjectMismatch", "UnknownModel", "InvalidRecord",
    "CostLedgerSpoolError", "CostLedgerReplayError", "RunCost", "LedgerTotals", "ReplayResult",
    "split_run_id", "run_id_for", "build_record", "record_run", "replay_spool", "aggregate_runs",
    "spool_dir_for", "spool_file_for",
]
