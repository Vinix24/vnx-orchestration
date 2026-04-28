"""Dispatch lifecycle register — append-only NDJSON log of dispatch state changes.

File: $VNX_STATE_DIR/dispatch_register.ndjson
Source of truth for feature/PR queue state (consumed by build_t0_state.py).
"""
from __future__ import annotations
import datetime as _dt, json, os, fcntl, sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]

VALID_EVENTS = {
    "dispatch_created",     # written to pending/
    "dispatch_promoted",    # moved pending/ → active/
    "dispatch_started",     # worker began
    "dispatch_completed",   # successful task_complete
    "dispatch_failed",      # task_failed OR task_complete with status=failed OR task_timeout
    "gate_requested",       # review_gate_request
    "gate_passed",          # gate completed with no blocking findings
    "gate_failed",          # gate completed with blocking findings
    "pr_opened",
    "pr_merged",
}


def _register_path() -> Path:
    """Resolve dispatch_register.ndjson location via canonical vnx_paths."""
    try:
        scripts_lib = str(_REPO_ROOT / "scripts" / "lib")
        if scripts_lib not in sys.path:
            sys.path.insert(0, scripts_lib)
        from vnx_paths import resolve_paths
        state_dir = resolve_paths()["VNX_STATE_DIR"]
        return Path(state_dir) / "dispatch_register.ndjson"
    except Exception:
        # Fallback: only honor VNX_DATA_DIR if explicitly enabled (mirrors canonical contract)
        if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1" and os.environ.get("VNX_DATA_DIR"):
            data_dir = Path(os.environ["VNX_DATA_DIR"])
        else:
            data_dir = _REPO_ROOT / ".vnx-data"
        return data_dir / "state" / "dispatch_register.ndjson"


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with microsecond precision (avoids same-second collisions)."""
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
    """Append a lifecycle event. Returns True on success, False on any failure.

    Best-effort: never raises. Intended for use as a fire-and-forget hook
    where caller flow must not break on register write failure.
    """
    if event not in VALID_EVENTS:
        return False
    record = {
        "timestamp": _utc_now_iso(),
        "event": event,
    }
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
    if extra and isinstance(extra, dict):
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


def read_events(*, since_iso: Optional[str] = None) -> list[dict]:
    """Read all events; takes shared lock to avoid partial-write reads."""
    path = _register_path()
    if not path.exists():
        return []
    events = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                content = fh.read()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        for line in content.splitlines():
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
    except Exception:
        return []
    return events


# CLI for bash callers
def _cli(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "append":
        print("Usage: dispatch_register.py append <event> [key=value ...] [extra.key=value ...]", flush=True)
        return 2
    event = argv[2]
    kwargs: dict = {}
    extra: dict = {}
    for arg in argv[3:]:
        if "=" not in arg:
            continue
        k, v = arg.split("=", 1)
        if k.startswith("extra."):
            extra_key = k[len("extra."):]
            if extra_key:
                extra[extra_key] = v
        elif k == "pr_number":
            try:
                kwargs[k] = int(v)
            except ValueError:
                continue
        elif k in ("dispatch_id", "feature_id", "terminal", "gate"):
            kwargs[k] = v
    if extra:
        kwargs["extra"] = extra
    return 0 if append_event(event, **kwargs) else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
