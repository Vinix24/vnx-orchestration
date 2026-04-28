"""Dispatch lifecycle register — append-only NDJSON writer for dispatch state changes.

Source of truth for feature/PR queue state. Read by build_t0_state.py.
File location: $VNX_STATE_DIR/dispatch_register.ndjson
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]

VALID_EVENTS = {
    "dispatch_created",
    "dispatch_promoted",
    "dispatch_started",
    "dispatch_completed",
    "dispatch_failed",
    "gate_requested",
    "gate_passed",
    "gate_failed",
    "pr_opened",
    "pr_merged",
}


def _register_path() -> Path:
    data_dir = Path(os.environ.get("VNX_DATA_DIR", str(_REPO_ROOT / ".vnx-data")))
    return data_dir / "state" / "dispatch_register.ndjson"


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def append_event(
    event: str,
    *,
    dispatch_id: str = "",
    pr_number: Optional[int] = None,
    feature_id: str = "",
    terminal: str = "",
    gate: str = "",
    extra: Optional[dict] = None,
) -> bool:
    """Append a lifecycle event. Best-effort, never raises."""
    if event not in VALID_EVENTS:
        return False

    # Coerce pr_number to int if passed as string from CLI
    if pr_number is not None and not isinstance(pr_number, int):
        try:
            pr_number = int(pr_number)
        except (ValueError, TypeError):
            pr_number = None

    record: dict = {"timestamp": _utc_now_iso(), "event": event}
    if dispatch_id:
        record["dispatch_id"] = dispatch_id
    if pr_number is not None:
        record["pr_number"] = pr_number
    if feature_id:
        record["feature_id"] = feature_id
    if terminal:
        record["terminal"] = terminal
    if gate:
        record["gate"] = gate
    if extra:
        record["extra"] = extra

    try:
        path = _register_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        return True
    except Exception:
        return False


def read_events(*, since_iso: Optional[str] = None) -> list:
    """Read all events, optionally filtered by timestamp."""
    path = _register_path()
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if since_iso and rec.get("timestamp", "") < since_iso:
                continue
            events.append(rec)
        except json.JSONDecodeError:
            continue
    return events


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3 or sys.argv[1] != "append":
        sys.exit(1)

    event = sys.argv[2]
    kwargs: dict = {}
    for arg in sys.argv[3:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            kwargs[k] = v

    sys.exit(0 if append_event(event, **kwargs) else 1)
