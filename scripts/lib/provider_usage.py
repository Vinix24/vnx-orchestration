"""provider_usage.py — the usage and balance of every provider, read-only (``vnx providers usage``).

One source per provider, each measured on its own: a failing source never hides or zeroes
another. Nothing here makes a model call. The HTTP probes read a balance endpoint only, which
is why ``_http_get_json`` sits on ``provider_switch.EXEMPT`` (layer 1 covers launches, not
balance probes). A missing number is never written as 0 and never left out silently: a row is
either ``available`` with its numbers or ``available: false`` with a ``reason``.

No secret leaves this module: an API key goes into one request header and nowhere else, and
every reason string is built from an error class or status code, never from a message that
could quote the request.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import load_deepseek_key as _keys
import provider_reachability as _reach

SCHEMA = "vnx.providers.usage/1"
HTTP_TIMEOUT_SECONDS = 5
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
CLAUDE_REASON = "alleen in een Claude Code-sessie meetbaar"
KIMI_FULL = "vol tot reset"
PROVIDER_ORDER = ("claude", "codex", "kimi", "deepseek", "openrouter")

_TAIL_CHUNK = 64 * 1024
_TAIL_SCAN_LIMIT = 32 * 1024 * 1024
_RESPONSE_LIMIT = 1024 * 1024
_KIMI_LIMIT_RE = re.compile(r"\b429\b|weekly|7-day|usage limit", re.IGNORECASE)
_OPENROUTER_KEY_LINE_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?OPENROUTER_API_KEY=(.*)$")

Row = Dict[str, Any]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _unavailable(reason: str) -> Row:
    return {"available": False, "reason": reason}


# ---------------------------------------------------------------------------
# codex: the last rate_limits line of the newest rollout
# ---------------------------------------------------------------------------

def _codex_sessions_dir(env: Dict[str, str], home: Path) -> Path:
    base = env.get("CODEX_HOME") or ""
    return (Path(base).expanduser() if base else home / ".codex") / "sessions"


def _newest_rollout(sessions: Path) -> Optional[Path]:
    newest: Optional[Tuple[float, Path]] = None
    for path in sessions.glob("**/rollout-*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue  # vnx-silent-except: a rollout that vanished between glob and stat is not a candidate
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return newest[1] if newest else None


def _lines_from_end(path: Path):
    """Yield the lines of ``path`` last to first, reading a chunk at a time, up to the scan limit."""
    with path.open("rb") as handle:
        position = handle.seek(0, os.SEEK_END)
        floor = max(0, position - _TAIL_SCAN_LIMIT)
        carry = b""
        while position > floor:
            step = min(_TAIL_CHUNK, position - floor)
            position -= step
            handle.seek(position)
            parts = (handle.read(step) + carry).split(b"\n")
            carry = parts[0]
            yield from reversed(parts[1:])
        if floor == 0 and carry:
            yield carry


def _rate_limits_of(raw: bytes) -> Optional[Tuple[dict, dict]]:
    """``(line, rate_limits)`` for a line that carries a rate_limits object, else None."""
    if b"rate_limits" not in raw:
        return None
    try:
        line = json.loads(raw)
    except ValueError:
        return None  # vnx-silent-except: a malformed line is skipped by contract
    payload = line.get("payload") if isinstance(line, dict) else None
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    return (line, limits) if isinstance(limits, dict) else None


def _age_seconds(timestamp: Any, now: float) -> Optional[int]:
    if not isinstance(timestamp, str):
        return None
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, int(now - moment.timestamp()))


def _codex_row(line: dict, limits: dict, rollout: Path, now: float) -> Row:
    primary = limits.get("primary")
    if not isinstance(primary, dict) or not isinstance(primary.get("used_percent"), (int, float)):
        return _unavailable("rate_limits zonder primary.used_percent in " + rollout.name)
    resets_at = primary.get("resets_at")
    row: Row = {
        "available": True,
        "used_percent": primary["used_percent"],
        "window_minutes": primary.get("window_minutes"),
        "resets_at": _iso(resets_at) if isinstance(resets_at, (int, float)) else None,
        "plan_type": limits.get("plan_type"),
        "age_seconds": _age_seconds(line.get("timestamp"), now),
        "source": rollout.name,
    }
    if limits.get("secondary") is not None:
        row["secondary"] = limits["secondary"]
    return row


def probe_codex(env: Dict[str, str], home: Path, now: float) -> Row:
    sessions = _codex_sessions_dir(env, home)
    if not sessions.is_dir():
        return _unavailable("geen codex-sessiemap: " + str(sessions))
    rollout = _newest_rollout(sessions)
    if rollout is None:
        return _unavailable("geen rollout-bestand in " + str(sessions))
    for raw in _lines_from_end(rollout):
        found = _rate_limits_of(raw)
        if found:
            return _codex_row(found[0], found[1], rollout, now)
    return _unavailable("geen rate_limits-regel in " + rollout.name)


# ---------------------------------------------------------------------------
# kimi: reachability records only, never a call with the kimi login
# ---------------------------------------------------------------------------

def _kimi_records(home: Path) -> List[Tuple[float, str, _reach.Reachability]]:
    found = []
    for state in sorted((home / ".vnx-data").glob("*/state")):
        # now=0.0 keeps an expired record readable: its age is reported, not hidden.
        record = _reach.get("kimi", state_dir=state, now=0.0)
        if record.measured_at is not None and record.state is not _reach.ReachabilityState.UNMEASURED:
            found.append((record.measured_at, state.parent.name, record))
    return found


def _kimi_status(record: _reach.Reachability) -> str:
    if record.is_reachable:
        return "bereikbaar"
    if _KIMI_LIMIT_RE.search(record.detail or ""):
        return KIMI_FULL
    return "onbereikbaar: " + (record.reason or "onbekend")


def probe_kimi(home: Path, now: float) -> Row:
    records = _kimi_records(home)
    if not records:
        return _unavailable("geen meting")
    measured_at, store, record = max(records, key=lambda item: item[0])
    return {
        "available": True,
        "status": _kimi_status(record),
        "reason": record.reason,
        "measured_at": _iso(measured_at),
        "age_seconds": max(0, int(now - measured_at)),
        "store": store,
        "resets_at": "onbekend",
    }


# ---------------------------------------------------------------------------
# deepseek and openrouter: balance probes
# ---------------------------------------------------------------------------

def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "[redacted]") if secret else text


def _http_reason(exc: urllib.error.HTTPError) -> str:
    meaning = {401: "sleutel geweigerd", 402: "onvoldoende saldo", 403: "geen toegang", 429: "te veel aanvragen"}
    return f"http {exc.code}: {meaning[exc.code]}" if exc.code in meaning else f"http {exc.code}"


def _http_get_json(url: str, key: str) -> Tuple[Optional[Any], str]:
    """GET ``url`` with a bearer key. Returns ``(body, "")`` or ``(None, reason)``. Balance probe, no model call."""
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + key, "Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(_RESPONSE_LIMIT)
    except urllib.error.HTTPError as exc:
        return None, _http_reason(exc)
    except TimeoutError:
        return None, f"timeout na {HTTP_TIMEOUT_SECONDS}s"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return None, f"timeout na {HTTP_TIMEOUT_SECONDS}s"
        return None, "netwerkfout: " + type(exc.reason).__name__
    except OSError as exc:
        return None, "netwerkfout: " + type(exc).__name__
    try:
        return json.loads(_redact(raw.decode("utf-8", "replace"), key)), ""
    except ValueError:
        return None, "antwoord is geen JSON"


def probe_deepseek(env: Dict[str, str], http_get: Callable[[str, str], Tuple[Optional[Any], str]]) -> Row:
    key = _keys.resolve_deepseek_api_key(env=env)
    if not key:
        return _unavailable("geen DEEPSEEK_API_KEY in de omgeving of het provider-env-bestand")
    body, reason = http_get(DEEPSEEK_BALANCE_URL, key)
    if not isinstance(body, dict):
        return _unavailable(reason or "onverwacht antwoord")
    rows = [
        {"currency": info.get("currency"), "total": info.get("total_balance"),
         "granted": info.get("granted_balance"), "topped_up": info.get("topped_up_balance")}
        for info in (body.get("balance_infos") or []) if isinstance(info, dict)
    ]
    return {"available": True, "is_available": body.get("is_available"), "balances": rows}


def _openrouter_key_from_file(env_file: Optional[Path] = None) -> str:
    """Parse OPENROUTER_API_KEY out of the provider env file; the file is never sourced."""
    path = env_file or _keys.provider_env_file_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    for line in reversed(lines):
        match = _OPENROUTER_KEY_LINE_RE.match(line)
        if match:
            return _keys._strip_quotes(match.group(1)).strip()
    return ""


def resolve_openrouter_api_key(env: Dict[str, str]) -> str:
    """The environment first, then the provider env file."""
    return (env.get(OPENROUTER_API_KEY_ENV) or "").strip() or _openrouter_key_from_file()


def probe_openrouter(env: Dict[str, str], http_get: Callable[[str, str], Tuple[Optional[Any], str]]) -> Row:
    key = resolve_openrouter_api_key(env)
    if not key:
        return _unavailable("geen OPENROUTER_API_KEY in de omgeving of het provider-env-bestand")
    body, reason = http_get(OPENROUTER_CREDITS_URL, key)
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return _unavailable(reason or "onverwacht antwoord")
    credits, usage = data.get("total_credits"), data.get("total_usage")
    if not isinstance(credits, (int, float)) or not isinstance(usage, (int, float)):
        return _unavailable("antwoord mist total_credits of total_usage")
    return {"available": True, "total_credits": credits, "total_usage": usage,
            "remaining": round(credits - usage, 6)}


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _guarded(name: str, probe: Callable[[], Row]) -> Row:
    try:
        return probe()
    except Exception as exc:  # vnx-silent-except: one source failing is its own row with the error class, never another row's problem
        return _unavailable(f"onverwachte fout in de {name}-meting: {type(exc).__name__}")


def build_usage(*, env: Optional[Dict[str, str]] = None, home: Optional[Path] = None,
                now: Optional[float] = None,
                http_get: Callable[[str, str], Tuple[Optional[Any], str]] = _http_get_json) -> Dict[str, Any]:
    """Build the usage report, one independent row per provider.

    Shape (schema ``vnx.providers.usage/1``)::

        {"schema": "vnx.providers.usage/1", "measured_at": "<ISO>",
         "providers": {"claude": ..., "codex": ..., "kimi": ..., "deepseek": ..., "openrouter": ...}}

    Every row has ``available`` (bool) and, when false, a ``reason``. When true:

    - codex: ``used_percent``, ``window_minutes``, ``resets_at`` (ISO), ``plan_type``,
      ``age_seconds``, ``source``, and ``secondary`` when the rollout carries one.
    - kimi: ``status`` (``bereikbaar``, ``vol tot reset`` or ``onbereikbaar: <reason>``), ``reason``,
      ``measured_at``, ``age_seconds``, ``store``, ``resets_at`` (always ``onbekend``). Never a percentage.
    - deepseek: ``is_available`` and ``balances``, one ``{currency, total, granted, topped_up}`` per
      row the API returned, as given.
    - openrouter: ``total_credits``, ``total_usage``, ``remaining``.
    - claude: always unavailable here; the mod reads it from ``$.session.usage()``.
    """
    env = dict(os.environ) if env is None else env
    home = Path.home() if home is None else home
    moment = now if now is not None else datetime.now(timezone.utc).timestamp()
    rows = {
        "claude": lambda: _unavailable(CLAUDE_REASON),
        "codex": lambda: probe_codex(env, home, moment),
        "kimi": lambda: probe_kimi(home, moment),
        "deepseek": lambda: probe_deepseek(env, http_get),
        "openrouter": lambda: probe_openrouter(env, http_get),
    }
    return {"schema": SCHEMA, "measured_at": _iso(moment),
            "providers": {name: _guarded(name, rows[name]) for name in PROVIDER_ORDER}}


def _detail(name: str, row: Row) -> str:
    if name == "codex":
        return (f"{row['used_percent']}% van {row['window_minutes']} min, reset {row['resets_at']}, "
                f"plan {row['plan_type']}, {row['age_seconds']}s oud")
    if name == "kimi":
        return f"{row['status']}, gemeten {row['measured_at']} ({row['store']}), reset onbekend"
    if name == "deepseek":
        balances = "; ".join(f"{b['currency']} {b['total']} (granted {b['granted']}, topped-up {b['topped_up']})"
                             for b in row["balances"]) or "geen saldo-rijen"
        return f"is_available={row['is_available']}, {balances}"
    return f"{row['remaining']} over ({row['total_credits']} gekocht, {row['total_usage']} gebruikt)"


def render_usage_text(report: Dict[str, Any]) -> str:
    """One line per source."""
    lines = []
    for name, row in report["providers"].items():
        body = _detail(name, row) if row["available"] else "niet beschikbaar: " + row["reason"]
        lines.append(f"{name:<11}{body}")
    return "\n".join(lines)
