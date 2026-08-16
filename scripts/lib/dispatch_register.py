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
import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

import state_writer
from vnx_ids import PROJECT_ID_RE as _PROJECT_ID_RE

try:
    import shadow_verifier as _shadow_verifier
    import shadow_logger as _shadow_logger
except ImportError:
    _shadow_verifier = None  # type: ignore[assignment]
    _shadow_logger = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# NDJSON write path documentation
#
# Two separate NDJSON paths exist in this module — intentional, not a bug:
#
# Path 1 (legacy, fire-and-forget): append_event()
#   Writes to: <state_dir>/dispatch_register.ndjson
#   Semantics: best-effort, never raises, suitable for fire-and-forget hooks
#   Used by: all legacy callers (schema_migration hooks, CLI bash callers, etc.)
#
# Path 2 (transactional, ADR-005): register_proposed_track_dispatch()
#   Writes to: <state_dir.parent>/events/dispatch_register.ndjson
#   Semantics: raises on failure; NDJSON written before SQLite commit
#   Used by: new track-layer operations requiring ledger-first guarantee
#
# DO NOT merge the two paths — legacy callers rely on best-effort semantics;
# new callers must use the transactional path and handle OSError propagation.
# ---------------------------------------------------------------------------

# SQL template identifier used in shadow comparisons (no actual SQL — NDJSON source)
_REGISTER_NDJSON_TEMPLATE = "dispatch_register.ndjson"

_REPO_ROOT = Path(__file__).resolve().parents[2]

VALID_EVENTS = {
    "dispatch_created",         # written to pending/
    "dispatch_promoted",        # moved pending/ → active/
    "dispatch_started",         # worker began
    "dispatch_completed",       # governed success — see event_outcome_semantics.classify_event_outcome
    "dispatch_failed",          # governed failure — see event_outcome_semantics.classify_event_outcome
    # (OI-1148: the event_type + status -> outcome mapping used to be
    # paraphrased here as "task_failed OR task_complete with status=failed OR
    # task_timeout" — inaccurate (e.g. it omitted subprocess_completion, and
    # didn't reflect that task_timeout+status="no_confirmation" is a pending
    # state, not a failure) and, being a comment, could drift silently from
    # the actual classifier in append_receipt_internals/register_emit.py.
    # The one place that now decides this is
    # scripts/lib/event_outcome_semantics.py::classify_event_outcome.)
    "gate_requested",           # review_gate_request
    "gate_passed",              # gate completed with no blocking findings
    "gate_failed",              # gate completed with blocking findings
    "pr_opened",
    "pr_merged",
    "runtime_anomaly_detected",          # RuntimeSupervisor detected a stalled/zombie worker
    "lease_released_on_failure_partial", # lease released but failure_recorded=False — incomplete cleanup
}


def _register_path(state_dir: Optional[Path] = None) -> Path:
    """Resolve dispatch_register.ndjson via canonical vnx_paths resolver.

    ``state_dir``, when given, is authoritative and skips ambient resolution
    entirely — mirrors the override ``read_events`` already accepts (OI-1120
    part 2), so a caller that has already resolved its own state_dir (e.g. the
    dispatch door, which derives it from the staged bundle's physical
    location per ADR-026) writes to the exact same path it reads from,
    instead of risking a second, independent ambient resolution drifting
    from the first.

    Fallback precedence (when state_dir is not given and the canonical
    resolver is unavailable):
    1. VNX_STATE_DIR (if set) — use directly as state dir
    2. VNX_DATA_DIR + state subdir (only when VNX_DATA_DIR_EXPLICIT=1)
    3. Repo-relative .vnx-data/state
    """
    if state_dir is not None:
        return Path(state_dir) / "dispatch_register.ndjson"
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


def _resolve_register_path(state_dir: Optional[Path] = None) -> Path:
    path = _register_path(state_dir=state_dir)
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
    import hashlib as _hashlib
    _primary = dispatch_id or feature_id or str(pr_number)
    record["record_id"] = _hashlib.sha256(
        f'{event}:{_primary}:{record["timestamp"]}'.encode()
    ).hexdigest()[:16]
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


def _resolve_central_data_dir(project_id: str) -> Path:
    """Return the central data dir for project_id. Module-level for monkeypatching."""
    from vnx_paths import resolve_central_data_dir
    return resolve_central_data_dir(project_id)


def _project_id_from_state_dir(state_dir: Path) -> str:
    """Extract project_id from state_dir if it matches ~/.vnx-data/<project>/state.

    Returns empty string when state_dir does not follow the central hierarchy,
    ensuring env-based VNX_PROJECT_ID is never consulted when an explicit
    state_dir is provided.
    """
    try:
        resolved = state_dir.resolve()
        vnx_data = (Path.home() / ".vnx-data").resolve()
        if resolved.name == "state" and resolved.parent.parent == vnx_data:
            pid = resolved.parent.name
            if _PROJECT_ID_RE.match(pid):
                return pid
    except OSError as e:
        log.debug("_project_id_from_state_dir: path resolution failed: %s", e)
    return ""


def _merge_dedup_key(event: dict) -> tuple[str, str, str, str, str]:
    return (
        str(event.get("timestamp", "")),
        str(event.get("event", "")),
        str(event.get("dispatch_id", "") or ""),
        str(event.get("pr_number", "") or ""),
        str(event.get("feature_id", "") or ""),
    )


def _write_event_locked(path: Path, record: dict) -> None:
    """Backwards-compatible wrapper around the shared state writer."""
    state_writer.append_locked(path, record)


def _isolation_guard_error_class():
    """Lazy import: TestIsolationGuardError for except-clauses (keeps the
    vnx_paths import off the module import path). Mirrors the same helper in
    append_receipt_internals.payload so both mirrors share one re-raise shape."""
    scripts_lib = str(_REPO_ROOT / "scripts" / "lib")
    if scripts_lib not in sys.path:
        sys.path.insert(0, scripts_lib)
    from vnx_paths import TestIsolationGuardError
    return TestIsolationGuardError


def _refuse_real_store_write_under_pytest(target: Path) -> None:
    """OI-1079 guard seam: refuse an imminent WRITE into the real central
    store (~/.vnx-data) while running under pytest. No-op outside pytest.

    Same guard the receipt mirror carries (append_receipt_internals.payload,
    shipped with #1397). The register mirror lacked it, so an isolated test
    that lost its pin and reached append_event wrote through this path into
    the real ``~/.vnx-data/<project_id>/state/dispatch_register.ndjson`` —
    the exact leak class #1397 closed for t0_receipts.ndjson.
    """
    scripts_lib = str(_REPO_ROOT / "scripts" / "lib")
    if scripts_lib not in sys.path:
        sys.path.insert(0, scripts_lib)
    from vnx_paths import refuse_real_central_store_write_under_pytest as _refuse
    _refuse(target)


def _mirror_event_to_central(record: dict, primary_path: Path, project_id: str) -> None:
    """Best-effort mirror of a register event to the central path. Never raises.

    Phase 6 P4: path resolution stays in this module (so test monkey-patches
    on ``_resolve_central_data_dir`` keep working). The locked append is
    delegated to ``scripts.lib.dual_writer.append_record_locked`` so all
    dual-write sites (receipts + register events) share one fcntl/atomicity
    implementation.
    P5 cutover guard: skips when primary_path resolves to the central file.

    OI-1079: raises ``TestIsolationGuardError`` when the resolved central
    target is the real central store and the process runs under pytest —
    that is an isolation violation, not a routine mirror failure, so the
    caller (append_event) must re-raise it rather than swallow it as the
    generic best-effort ``except`` otherwise would. Mirrors the receipt
    seam shipped with #1397.
    """
    try:
        central_base = _resolve_central_data_dir(project_id)
        central_path = central_base / "state" / "dispatch_register.ndjson"
        if central_path.resolve() == primary_path.resolve():
            return
        _refuse_real_store_write_under_pytest(central_path)
        try:
            from dual_writer import append_record_locked
            append_record_locked(central_path, record)
        except Exception:
            central_path.parent.mkdir(parents=True, exist_ok=True)
            _write_event_locked(central_path, record)
    except _isolation_guard_error_class():
        # OI-1079: a test-isolation violation must fail the test, not be
        # swallowed as a debug log line.
        raise
    except (ImportError, OSError) as e:
        log.debug("Mirror to central register failed: %s", e)


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
    state_dir: Optional[Path] = None,
) -> bool:
    """Append a lifecycle event. Returns True on success, False on any failure.

    Best-effort: never raises (except ``TestIsolationGuardError`` — see
    below). Intended for use as a fire-and-forget hook where caller flow
    must not break on register write failure.

    Optional ``operator_id`` / ``project_id`` / ``orchestrator_id`` /
    ``agent_id`` arguments stamp a four-tuple identity onto the event.
    When omitted, the helper falls back to ``vnx_identity.try_resolve_identity``;
    if resolution fails the event is written without those fields (legacy
    behaviour). Existing callers that pass none of these arguments continue
    to work unchanged.

    ``state_dir``, when given, overrides ambient path resolution for the
    primary write (see ``_register_path``) — the same override ``read_events``
    already accepts.
    """
    return _append_event_core(
        event,
        dispatch_id=dispatch_id,
        pr_number=pr_number,
        feature_id=feature_id,
        terminal=terminal,
        gate=gate,
        extra=extra,
        operator_id=operator_id,
        project_id=project_id,
        orchestrator_id=orchestrator_id,
        agent_id=agent_id,
        state_dir=state_dir,
        dedup=False,
    )


def _append_event_core(
    event: str,
    *,
    dispatch_id: str,
    pr_number: Optional[int],
    feature_id: str,
    terminal: str,
    gate: str,
    extra: Optional[dict],
    operator_id: Optional[str],
    project_id: Optional[str],
    orchestrator_id: Optional[str],
    agent_id: Optional[str],
    state_dir: Optional[Path],
    dedup: bool,
) -> bool:
    """Shared body of ``append_event`` / ``append_event_idempotent``.

    ``dedup=True`` performs the duplicate check INSIDE the write's critical
    section (``state_writer.append_locked`` with ``skip_if``): check and
    append share one sentinel + data-file ``LOCK_EX`` hold, so two
    concurrent callers can never both pass the check (OI-1129). On a
    duplicate hit the mirrors (decision log, central) are skipped — the
    equivalent event already went through them when it was first written.
    """
    if event not in VALID_EVENTS:
        return False
    # Require at least one identifying field — register is canonical source, must be queryable
    if not dispatch_id and pr_number is None and not feature_id:
        return False

    # Resolve identity per-field so partial callers still receive missing values.
    if not (operator_id and project_id and orchestrator_id and agent_id):
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
    try:
        primary_path = _resolve_register_path(state_dir=state_dir)
        if dedup:
            event_identity = _event_identity(record)
            appended = state_writer.append_locked(
                primary_path,
                record,
                skip_if=lambda content: _content_has_identity(content, event_identity),
            )
            if not appended:
                # Equivalent event already in the register: present-after-call
                # is the contract, and the first write already fanned out to
                # the mirrors.
                return True
        else:
            _write_event_locked(primary_path, record)
        _mirror_to_decision_log(event, record, extra=extra)
        # Phase 6 P3 dual-write: mirror to central when project_id is known
        if project_id:
            _mirror_event_to_central(record, primary_path, project_id)
        return True
    except _isolation_guard_error_class():
        # OI-1079: never swallow a test-isolation violation as a generic
        # best-effort failure — the mirror seam must fail the test so the
        # leak is visible, not silently return False.
        raise
    except Exception:
        return False


def _event_identity(event: dict) -> tuple[str, str, str, str]:
    """The non-timestamp portion of ``_merge_dedup_key``.

    Two records sharing this identity describe the same real-world
    occurrence regardless of when each was written — the timestamp is the
    only field ``_merge_dedup_key`` includes that a retry naturally varies.
    """
    return _merge_dedup_key(event)[1:]


def _content_has_identity(content: bytes, identity: tuple[str, str, str, str]) -> bool:
    """True when raw register NDJSON ``content`` holds an event with ``identity``.

    Total by construction — undecodable bytes, unparseable lines, and
    non-dict JSON are skipped, never raised on — because this runs as the
    ``skip_if`` predicate inside ``state_writer.append_locked``'s critical
    section: a scan failure must degrade to "no duplicate found" (append
    proceeds; better a possible duplicate than a lost event), the same
    direction the old pre-check fell.
    """
    for line in content.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            existing = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(existing, dict) and _event_identity(existing) == identity:
            return True
    return False


def append_event_idempotent(
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
    state_dir: Optional[Path] = None,
) -> bool:
    """``append_event``, but a no-op when an equivalent event already exists.

    OI-1120 part 2: a retry or fix-forward that fires the same dispatch_id
    twice through the door must not create a second ``dispatch_created``
    record — that would give ``report_to_receipt_converter._is_known_dispatch``
    two rows to reconcile per id instead of one. Reuses the identity
    ``_merge_dedup_key`` already defines for read-side merge-dedup
    (event, dispatch_id, pr_number, feature_id), evaluated at write time
    instead of read time, rather than inventing a second dedup scheme.

    OI-1129: the duplicate check runs INSIDE the write's critical section
    (``state_writer.append_locked`` with ``skip_if`` — the same
    check-inside-the-lock discipline the receipt writer uses in
    ``append_receipt_internals.idempotency._write_receipt_under_lock``), not
    as a separate ``read_events`` pre-check. The original read-then-write
    pair let two concurrent callers both miss the pre-check and both append.

    Returns True when the event is present after the call — whether it was
    already there or newly written — False only on a genuine write failure.
    """
    return _append_event_core(
        event,
        dispatch_id=dispatch_id,
        pr_number=pr_number,
        feature_id=feature_id,
        terminal=terminal,
        gate=gate,
        extra=extra,
        operator_id=operator_id,
        project_id=project_id,
        orchestrator_id=orchestrator_id,
        agent_id=agent_id,
        state_dir=state_dir,
        dedup=True,
    )


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


def _read_register_locked_per_project(path: Path) -> str:
    """Read raw NDJSON content from a register file under shared lock."""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        try:
            return fh.read()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_register_locked_central(project_id: str) -> str:
    """Read raw NDJSON content from the central register for project_id.

    Returns empty string when the central path does not exist or any error occurs.
    """
    try:
        central_base = _resolve_central_data_dir(project_id)
        central_path = central_base / "state" / "dispatch_register.ndjson"
        if not central_path.exists():
            return ""
        return _read_register_locked_per_project(central_path)
    except Exception:
        return ""


def _read_register_locked(path: Path) -> str:
    """3-state dispatcher for register reads (Wave 1 VNX_USE_CENTRAL_DB).

    | VNX_USE_CENTRAL_DB | Behaviour |
    |--------------------|-----------|
    | unset (default)    | per-project read only — zero behaviour change |
    | shadow             | per-project authoritative; central read compared via metric 4 |
    | 1                  | central read only |
    """
    flag = os.environ.get("VNX_USE_CENTRAL_DB", "")
    if flag == "":
        return _read_register_locked_per_project(path)
    project_id = _project_id_from_state_dir(path.parent) or os.environ.get("VNX_PROJECT_ID", "")
    if flag == "1":
        if project_id:
            return _read_register_locked_central(project_id)
        return _read_register_locked_per_project(path)
    # flag == "shadow": per-project authoritative; central observed-only
    legacy_content = _read_register_locked_per_project(path)
    if project_id and _shadow_verifier is not None:
        try:
            central_content = _read_register_locked_central(project_id)
            legacy_events = [
                json.loads(ln) for ln in legacy_content.splitlines() if ln.strip()
            ]
            central_events = [
                json.loads(ln) for ln in central_content.splitlines() if ln.strip()
            ]
            cmp = _shadow_verifier.compare(
                legacy_events,
                central_events,
                project_id=project_id,
                read_site="_read_register_locked",
                sql_template=_REGISTER_NDJSON_TEMPLATE,
                metric_id=4,
                table="dispatch_register",
            )
            if cmp.divergences and _shadow_logger is not None:
                _shadow_logger.write_comparison_result(
                    cmp, project_id, "_read_register_locked"
                )
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
            log.debug("Shadow comparison failed in _read_register_locked: %s", e)
    return legacy_content


def _read_events_from_path(path: Path, since_iso: Optional[str]) -> list[dict]:
    """Read events from a single NDJSON path with optional timestamp filter.

    Uses _read_register_locked_per_project directly so that shadow comparison
    at this level does not interfere with higher-level _query_recent_dispatches
    shadow logging (each level logs independently via its own dispatcher).
    """
    if not path.exists():
        return []
    cutoff_dt = _parse_iso(since_iso) if since_iso else None
    cutoff_lex = since_iso if (since_iso and cutoff_dt is None) else None
    events: list[dict] = []
    try:
        content = _read_register_locked_per_project(path)
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


def read_events(*, since_iso: Optional[str] = None, state_dir: Optional[Path] = None) -> list[dict]:
    """Read all events; merge-reads from central when project_id is derivable.

    Primary path: ``state_dir/dispatch_register.ndjson`` (or ambient VNX_STATE_DIR).
    Central path: derived from state_dir hierarchy when explicit; from VNX_PROJECT_ID
        env only when state_dir is not provided. This prevents env bleed-through when
        an explicit state_dir override is supplied.
    Deduplication: on (timestamp, event, dispatch_id, pr_number, feature_id)
        — central record wins.
    P5 cutover guard: central is skipped when it resolves to the same file as primary.
    """
    primary_path = (Path(state_dir) / "dispatch_register.ndjson") if state_dir is not None else _register_path()
    primary_events = _read_events_from_path(primary_path, since_iso)

    # Derive project_id from state_dir structure when explicit (ignore env);
    # fall back to env only when state_dir was not provided by the caller.
    if state_dir is not None:
        project_id = _project_id_from_state_dir(Path(state_dir))
    else:
        project_id = os.environ.get("VNX_PROJECT_ID", "").strip()

    central_events: list[dict] = []
    if project_id:
        try:
            central_base = _resolve_central_data_dir(project_id)
            central_path = central_base / "state" / "dispatch_register.ndjson"
            # P5 cutover guard: skip if central == primary
            primary_resolved = primary_path.resolve() if primary_path.exists() else None
            if central_path.exists() and (
                primary_resolved is None or central_path.resolve() != primary_resolved
            ):
                central_events = _read_events_from_path(central_path, since_iso)
        except (ImportError, OSError) as e:
            log.debug("Central register read skipped in read_events: %s", e)

    if not central_events:
        return primary_events

    # Merge: deduplicate on (timestamp, event, dispatch_id, pr_number, feature_id);
    # central wins on collision.
    merged: dict = {}
    for ev in primary_events:
        key = _merge_dedup_key(ev)
        merged[key] = ev
    for ev in central_events:
        key = _merge_dedup_key(ev)
        merged[key] = ev  # central overwrites primary on same key
    # OI-949: sort datetime-aware to avoid lexicographic misordering of
    # mixed-precision timestamps (e.g. "…00.123456Z" sorts before "…00Z"
    # under str-collation because "." (0x2E) < "Z" (0x5A)).
    # Fallback: unparseable timestamps land on _dt.datetime.min (year 1) so
    # they sort before any real event rather than interleaving spuriously.
    return sorted(
        merged.values(),
        key=lambda e: (_parse_iso(e.get("timestamp", "")) or _dt.datetime.min, e.get("timestamp", "")),
    )


# ---------------------------------------------------------------------------
# Shadow-aware recent-dispatch query (Wave 1, 3-state VNX_USE_CENTRAL_DB)
# ---------------------------------------------------------------------------


def _query_recent_dispatches_per_project(
    path: Path,
    since_iso: Optional[str] = None,
) -> list[dict]:
    """Return dispatch register events from the per-project NDJSON path."""
    return _read_events_from_path(path, since_iso)


def _query_recent_dispatches_central(
    project_id: str,
    since_iso: Optional[str] = None,
) -> list[dict]:
    """Return dispatch register events from the central NDJSON path, filtered to project_id.

    Metric 1 safety: only rows whose project_id matches are returned.
    Rows with a missing project_id field are included under the assumption they
    belong to the requesting project (pre-identity-stamp legacy events); callers
    that need strict isolation should apply _compare_metric_1_wrong_project_rows.
    """
    try:
        central_base = _resolve_central_data_dir(project_id)
        central_path = central_base / "state" / "dispatch_register.ndjson"
        if not central_path.exists():
            return []
        events = _read_events_from_path(central_path, since_iso)
        return [
            e for e in events
            if (e.get("project_id") or project_id) == project_id
        ]
    except Exception:
        return []


def _query_recent_dispatches(
    path: Path,
    project_id: str,
    since_iso: Optional[str] = None,
) -> list[dict]:
    """3-state dispatcher for recent-dispatch reads with shadow comparison.

    | VNX_USE_CENTRAL_DB | Behaviour |
    |--------------------|-----------|
    | unset (default)    | per-project read only — zero behaviour change |
    | shadow             | per-project authoritative; central compared via metric 1 + metric 4 |
    | 1                  | central read only (project_id-scoped) |
    """
    flag = os.environ.get("VNX_USE_CENTRAL_DB", "")
    if flag == "":
        return _query_recent_dispatches_per_project(path, since_iso)
    if flag == "1":
        if project_id:
            return _query_recent_dispatches_central(project_id, since_iso)
        return _query_recent_dispatches_per_project(path, since_iso)
    # flag == "shadow": per-project authoritative; central observed-only
    legacy = _query_recent_dispatches_per_project(path, since_iso)
    if not project_id or _shadow_verifier is None:
        return legacy
    try:
        central = _query_recent_dispatches_central(project_id, since_iso)
        # metric 1: wrong-project rows
        cmp1 = _shadow_verifier.compare(
            legacy,
            central,
            project_id=project_id,
            read_site="_query_recent_dispatches",
            sql_template=_REGISTER_NDJSON_TEMPLATE,
            metric_id=1,
        )
        if cmp1.divergences and _shadow_logger is not None:
            _shadow_logger.write_comparison_result(
                cmp1, project_id, "_query_recent_dispatches"
            )
        # metric 4: count + checksum
        cmp4 = _shadow_verifier.compare(
            legacy,
            central,
            project_id=project_id,
            read_site="_query_recent_dispatches",
            sql_template=_REGISTER_NDJSON_TEMPLATE,
            metric_id=4,
            table="dispatch_register",
        )
        if cmp4.divergences and _shadow_logger is not None:
            _shadow_logger.write_comparison_result(
                cmp4, project_id, "_query_recent_dispatches"
            )
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
        log.debug("Shadow comparison failed in _query_recent_dispatches: %s", e)
    return legacy


def register_proposed_track_dispatch(
    state_dir: "str | Path",
    dispatch_id: str,
    terminal_id: str,
    track_id: str,
    pr_ref: str,
    *,
    project_id: str = "vnx-dev",
) -> None:
    """Insert a proposed dispatch row + emit NDJSON audit event (ADR-005 compliant).

    NDJSON write precedes the SQLite commit. If the audit write fails it raises
    and no DB row is created. Raises sqlite3.Error on DB failure.

    ``project_id`` is written into the dispatches row (defaults to ``vnx-dev``
    for backwards compatibility with callers that do not supply it).
    """
    import sqlite3 as _sqlite3

    # Build audit record before any I/O
    record = _build_event_record(
        "dispatch_created", dispatch_id, None,
        pr_ref or "", terminal_id or "", "", None,
    )

    # Write NDJSON first — ADR-005 requires ledger before DB; events/ not state/
    _write_event_locked(Path(state_dir).parent / "events" / "dispatch_register.ndjson", record)

    db_path = Path(state_dir) / "runtime_coordination.db"
    conn = _sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute(
            """
            INSERT INTO dispatches (dispatch_id, project_id, state, terminal_id, track, pr_ref)
            VALUES (?, ?, 'proposed', ?, ?, ?)
            """,
            (dispatch_id, project_id, terminal_id, track_id, pr_ref),
        )
        conn.commit()
    finally:
        conn.close()


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
