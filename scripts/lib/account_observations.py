"""account_observations.py — a calibration is its own record, never a spend (K5b, ADR-043 §7).

A balance measurement lands in ``events/provider_account_observations.ndjson`` beside the cost ledger,
one ``account_observation`` line per provider per measurement, and no spend or token sum reads it.
:func:`observe` takes ``provider_usage``' two probes as they are (no HTTP code is copied) and appends
one record per provider under the ledger's own lock, held exclusively; a failing probe is written as
``unavailable:<reason>``, never skipped and never a zero.

:func:`calibration_delta` reads a window's spend as the drop in the total balance and refuses to
subtract a top-up from it: a rise of the *topped-up* balance makes that window say
``unavailable:topup_in_window`` while the windows around it stay unaffected (each step uses only its
own two observations). :func:`openrouter_coverage` asks whether OpenRouter lists a key's generations:
when it can, a generation no ledger run covers is a missed catch; when it cannot, the ``total_usage``
delta is set against the ledger's OpenRouter sum and the result says that detection has no source.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import cost_ledger
import provider_costs
import provider_usage
from vnx_paths import refuse_real_central_store_write_under_test_runner

SCHEMA = "provider_account_observation/1"
KIND = "account_observation"
OBSERVATIONS_NAME = "provider_account_observations.ndjson"
OBSERVED_PROVIDERS = ("deepseek", "openrouter")
OPENROUTER_GENERATIONS_URL = "https://openrouter.ai/api/v1/generations"
UNAVAILABLE = "unavailable:"
NO_ACCOUNT = UNAVAILABLE + "no_account_endpoint"
# Ledger provider literals whose spend lands on an account a probe measures. `openrouter` has no
# registry section, so no run lands there yet: `litellm:zai` is the lane models.yaml fronts with it.
_PROVIDER_ACCOUNTS = {
    "deepseek": "deepseek", "deepseek-harness": "deepseek", "litellm:deepseek": "deepseek",
    "openrouter": "openrouter", "glm-harness": "openrouter", "litellm:zai": "openrouter",
}
_EPSILON = 1e-9


def observations_path_for(ledger_path: Optional[Path] = None) -> Path:
    """``events/provider_account_observations.ndjson`` beside the cost ledger of the store."""
    ledger = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    return ledger.parent / OBSERVATIONS_NAME


def account_for(ledger_provider: object) -> Optional[str]:
    """The account a ledger provider literal's spend lands on, or ``None`` when no probe measures it."""
    return _PROVIDER_ACCOUNTS.get(ledger_provider) if isinstance(ledger_provider, str) else None


def _amount(value: object) -> Optional[float]:
    """A balance or usage number (a finite int, float or numeric string), or ``None`` (a bool is no amount)."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _moment(value: object) -> Optional[datetime]:
    """An ISO-8601 moment with a timezone, or ``None``: a timestamp without one is never guessed."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _values(provider: str, row: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The probe row's numbers, or why they are not one set; several currencies are never added up."""
    if provider == "deepseek":
        balances = row.get("balances")
        if not isinstance(balances, list) or not balances or not all(isinstance(b, dict) for b in balances):
            return None, UNAVAILABLE + "no_balance_rows"
        if len({balance.get("currency") for balance in balances}) != 1:
            return {"balances": balances}, UNAVAILABLE + "multiple_currencies"
        raw = {key: _amount(balances[0].get(key)) for key in ("total", "topped_up", "granted")}
        if raw["total"] is None or raw["topped_up"] is None:
            return {"balances": balances}, UNAVAILABLE + "balance_not_a_number"
        return dict(raw, currency=balances[0]["currency"]), None
    credits, usage = _amount(row.get("total_credits")), _amount(row.get("total_usage"))
    if credits is None or usage is None:
        return None, UNAVAILABLE + "credits_not_a_number"
    return {"total_credits": credits, "total_usage": usage}, None


def build_observation(provider: str, row: Mapping[str, Any], observed_at: str) -> Dict[str, Any]:
    """One ``account_observation`` of ``provider`` from a probe row: its numbers, or why there are none."""
    record: Dict[str, Any] = {"schema": SCHEMA, "kind": KIND, "provider": provider, "observed_at": observed_at}
    if provider not in OBSERVED_PROVIDERS:
        record["unavailable"] = UNAVAILABLE + "unknown_provider"
    elif not isinstance(row, Mapping) or row.get("available") is not True:
        reason = row.get("reason") if isinstance(row, Mapping) else None
        record["unavailable"] = UNAVAILABLE + (reason if isinstance(reason, str) and reason else "probe_failed")
    else:
        values, unavailable = _values(provider, row)
        if values is not None:
            record["values"] = dict(values)
        if unavailable is not None:
            record["unavailable"] = unavailable
    record["record_id"] = hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
    return record


def _guarded(name: str, probe: Callable[[], Mapping[str, Any]]) -> Mapping[str, Any]:
    try:
        return probe()
    except Exception as exc:  # vnx-silent-except: a failing probe is its own record with the error class, never a skip
        return {"available": False, "reason": f"onverwachte fout in de {name}-meting: {type(exc).__name__}"}


def _append(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    """Append ``records`` as whole lines under ``path``'s own lock file (ADR-043 §6's style) and fsync it."""
    refuse_real_central_store_write_under_test_runner(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(dict(record), separators=(",", ":")) + "\n" for record in records)
    with provider_costs.ledger_lock(path, fcntl.LOCK_EX):
        with path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def observe(
    *, providers: Sequence[str] = OBSERVED_PROVIDERS, env: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
    http_get: Callable[[str, str], Tuple[Optional[Any], str]] = provider_usage._http_get_json,
    ledger_path: Optional[Path] = None, observations_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Measure every provider and append one record each; return the records as written."""
    environment = dict(os.environ) if env is None else dict(env)
    stamp = datetime.fromtimestamp(now if now is not None else datetime.now(timezone.utc).timestamp(),
                                   tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    probes = {"deepseek": lambda: provider_usage.probe_deepseek(environment, http_get),
              "openrouter": lambda: provider_usage.probe_openrouter(environment, http_get)}
    records = [build_observation(name, _guarded(name, probes[name]) if name in probes
                                 else {"available": False, "reason": "geen meting"}, stamp)
               for name in providers]
    _append(Path(observations_path) if observations_path is not None else observations_path_for(ledger_path), records)
    return records


def read_observations(
    observations_path: Optional[Path] = None, *, ledger_path: Optional[Path] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """``(records, unreadable lines)``; no file is an empty one, and an unreadable line is counted, never skipped."""
    target = Path(observations_path) if observations_path is not None else observations_path_for(ledger_path)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], 0
    except OSError:
        return [], 1
    records, unreadable = [], 0
    for line in filter(None, (raw.strip() for raw in lines)):
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        keyed = all(isinstance((record or {}).get(key), str) for key in ("provider", "observed_at"))
        if keyed and record.get("schema") == SCHEMA and record.get("kind") == KIND:
            records.append(record)
        else:
            unreadable += 1
    return records, unreadable


def _step(provider: str, earlier: Mapping[str, Any], later: Mapping[str, Any]):
    """``(spend, None)`` for one step, or ``(None, unavailable)`` when it cannot separate spend from a top-up."""
    for record in (earlier, later):
        if isinstance(record.get("unavailable"), str):
            return None, record["unavailable"]
    first, second = earlier.get("values"), later.get("values")
    if not isinstance(first, Mapping) or not isinstance(second, Mapping):
        return None, UNAVAILABLE + "observation_without_values"
    keys = ("total_usage",) if provider == "openrouter" else ("total", "topped_up")
    numbers = [_amount(values.get(key)) for values in (first, second) for key in keys]
    if any(number is None for number in numbers):
        return None, UNAVAILABLE + "observation_without_values"
    if provider == "openrouter":
        spent = numbers[1] - numbers[0]
        return (None, UNAVAILABLE + "usage_decreased") if spent < -_EPSILON else (max(spent, 0.0), None)
    if numbers[3] - numbers[1] > _EPSILON:
        return None, UNAVAILABLE + "topup_in_window"
    drop = numbers[0] - numbers[2]
    return (None, UNAVAILABLE + "balance_rose_without_topup") if drop < -_EPSILON else (max(drop, 0.0), None)


def calibration_delta(
    provider: str, since: Optional[str] = None, until: Optional[str] = None, *,
    observations_path: Optional[Path] = None, ledger_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """The spend of ``[since, until]`` for ``provider``: the drop in the balance, never a top-up.

    Bounds are ISO-8601 or ``YYYY-MM-DD`` (``until`` of a bare date covers that day, ``None`` is open).
    """
    def no(reason: str, count: int = 0) -> Dict[str, Any]:
        return {"provider": provider, "since": since, "until": until, "observations": count,
                "spend_usd": None, "unavailable": reason}

    records, unreadable = read_observations(observations_path, ledger_path=ledger_path)
    if unreadable:
        return no(UNAVAILABLE + f"observations_unreadable: {unreadable} lines")
    start, end = _moment(since), _moment(until)
    if (since is not None and start is None) or (until is not None and end is None):
        return no(UNAVAILABLE + "bad_window")
    if end is not None and len(str(until).strip()) == 10:
        end += timedelta(days=1) - timedelta(microseconds=1)
    window = [(_moment(record["observed_at"]), record) for record in records if record["provider"] == provider]
    if any(moment is None for moment, _record in window):
        return no(UNAVAILABLE + "observations_unreadable: observed_at")
    window = sorted((item for item in window
                     if (start is None or item[0] >= start) and (end is None or item[0] <= end)),
                    key=lambda item: item[0])
    if len(window) < 2:
        return no(UNAVAILABLE + "no_observations", len(window))
    spend = 0.0
    for (_before, earlier), (_after, later) in zip(window, window[1:]):
        amount, reason = _step(provider, earlier, later)
        if reason is not None:
            return no(reason, len(window))
        spend += amount
    return {"provider": provider, "since": since, "until": until, "observations": len(window),
            "spend_usd": round(spend, 8), "unavailable": None}


def deviation_pct(ledger_cost: Optional[float], spend: Optional[float]) -> Optional[float]:
    """How far the ledger's metered sum is off the measured spend, in percent of that spend."""
    return None if spend is None or ledger_cost is None or abs(spend) <= _EPSILON \
        else round((ledger_cost - spend) / spend * 100.0, 2)


def _generations(env: Mapping[str, str], http_get, since: Optional[str], until: Optional[str]):
    """``(generation ids, None)`` when the API lists them, else ``(None, unavailable)``."""
    key = provider_usage.resolve_openrouter_api_key(dict(env))
    if not key:
        return None, UNAVAILABLE + "no_generation_source: no OPENROUTER_API_KEY"
    query = urllib.parse.urlencode({name: value for name, value in (("start", since), ("end", until)) if value})
    body, reason = http_get(OPENROUTER_GENERATIONS_URL + ("?" + query if query else ""), key)
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, list):
        return None, UNAVAILABLE + "no_generation_source: " + (reason or "onverwacht antwoord")
    return [item for item in data if isinstance(item, Mapping) and isinstance(item.get("id"), str)], None


def openrouter_coverage(
    since: Optional[str] = None, until: Optional[str] = None, *,
    env: Optional[Mapping[str, str]] = None,
    http_get: Callable[[str, str], Tuple[Optional[Any], str]] = provider_usage._http_get_json,
    observations_path: Optional[Path] = None, ledger_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """OpenRouter spend the ledger misses: per generation when the API lists them, else by the usage delta."""
    ledger = Path(ledger_path) if ledger_path is not None else provider_costs._resolve_costs_path()
    first, last = (str(since)[:10] if since else None), (str(until)[:10] if until else None)
    runs = [record for record in cost_ledger.aggregate_runs(ledger).runs.values()
            if _PROVIDER_ACCOUNTS.get(record.get("provider")) == "openrouter"
            and (day := cost_ledger.utc_day(record.get("occurred_from"))) is not None
            and (first is None or day >= first) and (last is None or day <= last)]
    amounts = [_amount(record.get("cost_usd")) for record in runs]
    windows = [(_moment(run.get("occurred_from")), _moment(run.get("occurred_to"))) for run in runs]
    delta = calibration_delta("openrouter", since, until, observations_path=observations_path,
                              ledger_path=ledger)
    generations, why = _generations(dict(os.environ) if env is None else env, http_get, since, until)
    missed = () if generations is None else tuple(item["id"] for item in generations if not any(
        start is not None and end is not None and start <= _moment(item.get("created_at")) <= end
        for start, end in windows))
    return {"since": since, "until": until, "source": "usage_delta" if generations is None else "generations",
            "missed_catches": missed, "ledger_runs": len(runs), "usage_delta_usd": delta["spend_usd"],
            "ledger_usd": None if None in amounts else round(sum(amounts), 8), "unavailable": why}
