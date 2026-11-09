"""Receipt-processor liveness, read from its heartbeat file in five states.

The processor writes ``$VNX_STATE_DIR/receipt_processor_heartbeat.json``
(contract: ADR-055). This module is the reader. It answers one question that
the PID-only ``daemon_liveness`` cannot: is the process that holds the PID
still making its rounds, or has it lost its work loop and merely kept the PID?

States, decided in exactly this order:

========  ==========================================================  =========
state     when                                                        overall
========  ==========================================================  =========
absent    no heartbeat file                                           unknown
unknown   unreadable, not JSON, unknown ``schema``, a bad ``pid``, a  unknown
          time field that does not parse, or ``updated_at`` more
          than ``CLOCK_SKEW_SECONDS`` in the future
dead      ``os.kill(pid, 0)`` raises ``ProcessLookupError``           fail
stalled   process exists and ``now - updated_at > WINDOW_SECONDS``     fail
fresh     process exists and ``now - updated_at <= WINDOW_SECONDS``    ok
========  ==========================================================  =========

``PermissionError`` from ``os.kill`` means the process exists under another
owner, so it counts as alive. Freshness comes only from the ``updated_at``
field inside the file, never from an mtime.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

HEARTBEAT_FILENAME = "receipt_processor_heartbeat.json"
SCHEMA_VERSION = 1
# One constant. Three times the measured p99 cycle (314 s, n = 171, 2026-11-08
# 21:29Z to 2026-11-09 08:51Z, PID 75922), by the ADR-055 section 5 rule. A later
# measurement replaces it the same way; it is not derived at run time.
WINDOW_SECONDS = 942
CLOCK_SKEW_SECONDS = 30
_TIME_FIELDS = ("started_at", "updated_at", "last_progress_at")

_OVERALL = {
    "fresh": "ok",
    "stalled": "fail",
    "dead": "fail",
    "absent": "unknown",
    "unknown": "unknown",
}


def _parse_time(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; a naive value is read as UTC. ``None`` on any failure."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _result(state: str, reason: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"state": state, "overall": _OVERALL[state], "reason": reason}
    out.update(extra)
    return out


def _load_heartbeat(path: Path) -> Any:
    """Return the decoded JSON, or an ``(error message,)`` tuple when unreadable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return (f"heartbeat unreadable: {exc.__class__.__name__}",)
    except UnicodeError:
        return ("heartbeat is not valid UTF-8",)
    try:
        return json.loads(raw)
    except ValueError:
        return ("heartbeat is not valid JSON",)


def _validate(data: Any) -> Optional[str]:
    """Reason the heartbeat cannot be trusted, or ``None`` when it is well-formed."""
    if not isinstance(data, dict):
        return "heartbeat is not a JSON object"
    schema = data.get("schema")
    if isinstance(schema, bool) or schema != SCHEMA_VERSION:
        return f"heartbeat schema {schema!r} is not {SCHEMA_VERSION}"
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return f"heartbeat pid {pid!r} is not a positive integer"
    for field in _TIME_FIELDS:
        if _parse_time(data.get(field)) is None:
            return f"heartbeat field {field} is missing or does not parse"
    return None


def _pid_exists(pid: int) -> bool:
    """True when the process exists. A permission error still proves existence."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_processor_liveness(
    state_dir: Path,
    *,
    now: Optional[datetime] = None,
    window_seconds: int = WINDOW_SECONDS,
) -> Dict[str, Any]:
    """Classify the receipt processor from ``<state_dir>/receipt_processor_heartbeat.json``.

    ``state_dir`` is always explicit so tests and migrations can point it
    anywhere. Never raises on a bad file: every failure to read evidence maps
    to ``absent`` or ``unknown``, which both stay non-ok.
    """
    path = Path(state_dir) / HEARTBEAT_FILENAME
    if not path.exists():
        return _result("absent", "no heartbeat file")
    data = _load_heartbeat(path)
    if isinstance(data, tuple):
        return _result("unknown", data[0])
    problem = _validate(data)
    if problem is not None:
        return _result("unknown", problem)
    current = now if now is not None else datetime.now(timezone.utc)
    age = (current - _parse_time(data["updated_at"])).total_seconds()  # type: ignore[operator]
    if age < -CLOCK_SKEW_SECONDS:
        return _result("unknown", f"updated_at is {-age:.0f}s in the future (clock skew)", pid=data["pid"])
    pid = data["pid"]
    if not _pid_exists(pid):
        return _result("dead", f"pid {pid} no longer exists", pid=pid, age_seconds=round(age, 1))
    if age > window_seconds:
        return _result(
            "stalled", f"pid {pid} alive but no heartbeat for {age:.0f}s (window {window_seconds}s)",
            pid=pid, age_seconds=round(age, 1),
        )
    return _result("fresh", "heartbeat within window", pid=pid, age_seconds=round(max(age, 0.0), 1))


__all__ = [
    "CLOCK_SKEW_SECONDS",
    "HEARTBEAT_FILENAME",
    "WINDOW_SECONDS",
    "read_processor_liveness",
]
