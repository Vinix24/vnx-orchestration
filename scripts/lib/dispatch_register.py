"""Dispatch lifecycle register — append-only NDJSON log of dispatch state changes.

File: $VNX_STATE_DIR/dispatch_register.ndjson

Current consumers:
- build_t0_state.py: exposes raw events list as dispatch_register_events (PR-4b2)

Future consumers (separate PRs):
- append_receipt.py + gate_recorder.py + dispatch_lifecycle.sh: hook callers (PR-4b3, PR-4b4)
- build_t0_state.py: full register-canonical pr_progress aggregation (PR-4c)
"""
from __future__ import annotations
import datetime as _dt
import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]

VALID_EVENTS = {
    "dispatch_created",         # written to pending/
    "dispatch_promoted",        # moved pending/ → active/
    "dispatch_started",         # worker began
    "dispatch_completed",       # successful task_complete
    "dispatch_failed",          # task_failed OR task_complete with status=failed OR task_timeout
    "gate_requested",           # review_gate_request
    "gate_passed",              # gate completed with no blocking findings
    "gate_failed",              # gate completed with blocking findings
    "pr_opened",
    "pr_merged",
    "runtime_anomaly_detected",          # RuntimeSupervisor detected a stalled/zombie worker
    "lease_released_on_failure_partial", # lease released but failure_recorded=False — incomplete cleanup
}


def _register_path() -> Path:
    """Resolve dispatch_register.ndjson via canonical vnx_paths resolver.

    Fallback precedence (when canonical resolver unavailable):
    1. VNX_STATE_DIR (if set) — use directly as state dir
    2. VNX_DATA_DIR + state subdir (only when VNX_DATA_DIR_EXPLICIT=1)
    3. Repo-relative .vnx-data/state
    """
    try:
        scripts_lib = str(_REPO_ROOT / "scripts" / "lib")
        if scripts_lib not in sys.path:
            sys.path.insert(0, scripts_lib)
        from vnx_paths import resolve_paths
        state_dir = resolve_paths()["VNX_STATE_DIR"]
        return Path(state_dir) / "dispatch_register.ndjson"
    except Exception:
        # Fallback chain mirrors canonical contract
        state_dir_env = os.environ.get("VNX_STATE_DIR")
        if state_dir_env:
            state_dir = Path(state_dir_env)
        elif os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1" and os.environ.get("VNX_DATA_DIR"):
            state_dir = Path(os.environ["VNX_DATA_DIR"]) / "state"
        else:
            state_dir = _REPO_ROOT / ".vnx-data" / "state"
        return state_dir / "dispatch_register.ndjson"


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with microsecond precision (avoids same-second collisions)."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(ts: str) -> Optional[_dt.datetime]:
    """Parse ISO-8601 UTC timestamp tolerating both microsecond and second
    precision and a trailing ``Z`` suffix. Returns ``None`` on failure.

    Why: read_events compares record timestamps to ``since_iso`` cutoffs.
    Lexicographic compare silently drops same-second events when the writer
    uses microsecond precision (``…00.123456Z``) and the caller passes a
    coarser cutoff (``…00Z``) — ``.`` (0x2E) sorts before ``Z`` (0x5A).
    """
    if not ts:
        return None
    s = ts
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _resolve_register_path() -> Path:
    path = _register_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _build_event_record(
    event: str,
    dispatch_id: str,
    pr_number: Optional[int],
    feature_id: str,
    terminal: str,
    gate: str,
    extra: Optional[dict],
    operator_id: Optional[str] = None,
    project_id: Optional[str] = None,
    orchestrator_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> dict:
    record: dict = {
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
    if operator_id:
        record["operator_id"] = operator_id
    if project_id:
        record["project_id"] = project_id
    if orchestrator_id:
        record["orchestrator_id"] = orchestrator_id
    if agent_id:
        record["agent_id"] = agent_id
    if extra and isinstance(extra, dict):
        record["extra"] = extra
    return record


def _resolve_identity_for_register() -> dict:
    """Best-effort identity resolution for register events. Never raises."""
    try:
        scripts_lib = str(_REPO_ROOT / "scripts" / "lib")
        if scripts_lib not in sys.path:
            sys.path.insert(0, scripts_lib)
        from vnx_identity import try_resolve_identity
    except Exception:
        return {}
    identity = try_resolve_identity()
    if identity is None:
        return {}
    return {
        "operator_id": identity.operator_id,
        "project_id": identity.project_id,
        "orchestrator_id": identity.orchestrator_id,
        "agent_id": identity.agent_id,
    }


def _write_event_locked(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def _central_register_path(project_id: str) -> Optional[Path]:
    """Resolve ~/.vnx-data/<project_id>/state/dispatch_register.ndjson.

    Phase 6 P3: second write target for dual-write. Returns None on any error.
    """
    try:
        from vnx_paths import resolve_central_data_dir
        state_dir = resolve_central_data_dir(project_id) / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "dispatch_register.ndjson"
    except Exception:
        return None


def _write_central_register(
    record: dict,
    project_id: Optional[str],
    *,
    _primary_path: Optional[Path] = None,
) -> None:
    """Best-effort mirror of a register event to the central path. Never raises.

    Skips the write when the resolved central path is the same file as
    _primary_path — prevents double-logging at P5 cutover when
    _resolve_register_path() already points to the central store.
    """
    if not project_id:
        project_id = os.environ.get("VNX_PROJECT_ID") or None
    if not project_id:
        return
    try:
        path = _central_register_path(project_id)
        if path is not None:
            if _primary_path is not None and path.resolve() == _primary_path.resolve():
                return
            _write_event_locked(path, record)
    except Exception:
        pass


def append_event(
    event: str,
    *,
    dispatch_id: str = "",
    pr_number: Optional[int] = None,
    feature_id: str = "",
    terminal: str = "",
    gate: str = "",
    extra: Optional[dict] = None,
    operator_id: Optional[str] = None,
    project_id: Optional[str] = None,
    orchestrator_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> bool:
    """Append a lifecycle event. Returns True on success, False on any failure.

    Best-effort: never raises. Intended for use as a fire-and-forget hook
    where caller flow must not break on register write failure.

    Optional ``operator_id`` / ``project_id`` / ``orchestrator_id`` /
    ``agent_id`` arguments stamp a four-tuple identity onto the event.
    When omitted, the helper falls back to ``vnx_identity.try_resolve_identity``;
    if resolution fails the event is written without those fields (legacy
    behaviour). Existing callers that pass none of these arguments continue
    to work unchanged.
    """
    if event not in VALID_EVENTS:
        return False
    # Require at least one identifying field — register is canonical source, must be queryable
    if not dispatch_id and pr_number is None and not feature_id:
        return False

    if not (operator_id or project_id or orchestrator_id or agent_id):
        identity = _resolve_identity_for_register()
        operator_id = operator_id or identity.get("operator_id")
        project_id = project_id or identity.get("project_id")
        orchestrator_id = orchestrator_id or identity.get("orchestrator_id")
        agent_id = agent_id or identity.get("agent_id")

    record = _build_event_record(
        event, dispatch_id, pr_number, feature_id, terminal, gate, extra,
        operator_id=operator_id,
        project_id=project_id,
        orchestrator_id=orchestrator_id,
        agent_id=agent_id,
    )
    primary_path = _resolve_register_path()
    try:
        _write_event_locked(primary_path, record)
        _mirror_to_decision_log(event, record, extra=extra)
    except Exception:
        return False
    # Phase 6 P3: best-effort dual-write to central per-project path.
    # _primary_path guard prevents double-logging at P5 cutover when primary
    # already resolves to the central store.
    try:
        _write_central_register(record, project_id, _primary_path=primary_path)
    except Exception:
        pass
    return True


def _log_dispatch_created(log_fn, record: dict, extra_dict: dict) -> None:
    log_fn(
        decision_type="dispatch_created",
        dispatch_id=record.get("dispatch_id"),
        terminal=record.get("terminal"),
        role=extra_dict.get("role"),
        risk_score=extra_dict.get("risk_score"),
        reasoning=extra_dict.get("reasoning", ""),
        expected_outcome=extra_dict.get("expected_outcome"),
        timestamp=record.get("timestamp"),
    )


def _log_gate_verdict(log_fn, event: str, record: dict, extra_dict: dict) -> None:
    verdict = "passed" if event == "gate_passed" else "failed"
    log_fn(
        decision_type="gate_verdict",
        dispatch_id=record.get("dispatch_id"),
        pr_number=record.get("pr_number"),
        gate=record.get("gate") or None,
        verdict=verdict,
        blocking_count=extra_dict.get("blocking_count"),
        reasoning=extra_dict.get("reasoning", ""),
        timestamp=record.get("timestamp"),
    )


def _log_pr_merged(log_fn, record: dict, extra_dict: dict) -> None:
    log_fn(
        decision_type="pr_merge",
        pr_number=record.get("pr_number"),
        dispatches_in_pr=extra_dict.get("dispatches_in_pr"),
        reasoning=extra_dict.get("reasoning", ""),
        timestamp=record.get("timestamp"),
    )


def _mirror_to_decision_log(event: str, record: dict, *, extra: Optional[dict] = None) -> None:
    """Best-effort fan-out to the T0 decision log for governance-relevant events.

    Captures dispatch_created, gate_passed, gate_failed, pr_merged so T0
    has structured introspection on its own decisions. Never raises — a
    decision-log write failure must not break dispatch_register.
    """
    try:
        from t0_decision_log import log_decision
    except Exception:
        return
    extra_dict = extra if isinstance(extra, dict) else {}
    if event == "dispatch_created":
        _log_dispatch_created(log_decision, record, extra_dict)
    elif event in ("gate_passed", "gate_failed"):
        _log_gate_verdict(log_decision, event, record, extra_dict)
    elif event == "pr_merged":
        _log_pr_merged(log_decision, record, extra_dict)
    # Other lifecycle events (dispatch_promoted, dispatch_started,
    # dispatch_completed, etc.) are recorded in the register but are
    # outcome signals rather than T0 decisions; reconciliation reads
    # them to resolve pending decisions.


def _read_register_locked(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        try:
            return fh.read()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_events(*, since_iso: Optional[str] = None, state_dir: Optional[Path] = None) -> list[dict]:
    """Read all events; takes shared lock to avoid partial-write reads. Honors optional state_dir override."""
    if state_dir is not None:
        path = Path(state_dir) / "dispatch_register.ndjson"
    else:
        path = _register_path()
    if not path.exists():
        return []
    events = []
    cutoff_dt = _parse_iso(since_iso) if since_iso else None
    # If the caller provided a since_iso we could not parse, fall back to the
    # legacy lexicographic compare so behaviour stays predictable rather than
    # silently disabling the filter.
    cutoff_lex = since_iso if (since_iso and cutoff_dt is None) else None
    try:
        content = _read_register_locked(path)
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff_dt is not None:
                rec_ts = rec.get("timestamp", "")
                rec_dt = _parse_iso(rec_ts)
                if rec_dt is None or rec_dt < cutoff_dt:
                    continue
            elif cutoff_lex is not None:
                if rec.get("timestamp", "") < cutoff_lex:
                    continue
            events.append(rec)
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
