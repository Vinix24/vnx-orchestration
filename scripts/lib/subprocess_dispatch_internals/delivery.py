"""delivery — deliver_via_subprocess + phase helpers.

Cross-module helpers (``_inject_skill_context``, ``_write_manifest``, ...)
and module-level globals exposed by the facade (``SubprocessAdapter``,
``WorkerHealthMonitor``) are looked up via ``import subprocess_dispatch as _sd``
at call time so that ``unittest.mock.patch("subprocess_dispatch.X")``
intercepts the call from inside this module.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from .delivery_runtime import _SubprocessResult, _heartbeat_loop
from .path_utils import _extract_touched_paths_from_event, _normalize_repo_path

logger = logging.getLogger(__name__)


def _apply_runtime_overrides(chunk_timeout: float, total_deadline: float) -> tuple[float, float]:
    """Honor VNX_CHUNK_TIMEOUT / VNX_TOTAL_DEADLINE env overrides."""
    try:
        chunk_timeout = float(os.environ["VNX_CHUNK_TIMEOUT"])
    except (KeyError, ValueError):
        pass
    try:
        total_deadline = float(os.environ["VNX_TOTAL_DEADLINE"])
    except (KeyError, ValueError):
        pass
    return chunk_timeout, total_deadline


def _apply_handover_continuation(
    terminal_id: str, instruction: str,
) -> tuple[str, "Path | None"]:
    """Detect pending rotation handover and wrap the instruction for continuation."""
    import subprocess_dispatch as _sd
    handover_dir = _sd._default_state_dir().parent / "rotation_handovers"
    pending = _sd._detect_pending_handover(terminal_id, handover_dir)
    if pending is not None:
        logger.info(
            "deliver_via_subprocess: pending handover found for %s: %s",
            terminal_id, pending,
        )
        instruction = _sd._build_continuation_prompt(pending, instruction)
    return instruction, pending


def _assemble_instruction(
    terminal_id: str,
    instruction: str,
    role: str | None,
    dispatch_id: str,
    model: str,
    repo_map: str | None,
) -> str:
    """Append repo map, layered skill context, then permission preamble."""
    import subprocess_dispatch as _sd
    if repo_map:
        instruction = instruction + f"\n\n{repo_map}"
    instruction = _sd._inject_skill_context(
        terminal_id,
        instruction,
        role=role,
        dispatch_metadata={"dispatch_id": dispatch_id, "model": model},
    )
    return _sd._inject_permission_profile(terminal_id, role, instruction)


def _resolve_agent_cwd_and_log_profile(role: str | None) -> "Path | None":
    """Resolve agents/{role}/ cwd and log governance_profile from config.yaml."""
    import subprocess_dispatch as _sd
    agent_cwd = _sd._resolve_agent_cwd(role)
    if agent_cwd is not None:
        config_path = agent_cwd / "config.yaml"
        if config_path.exists():
            profile = _sd._load_agent_profile(config_path)
            logger.info("Agent %s using governance profile: %s", role, profile)
    return agent_cwd


def _load_resume_session(terminal_id: str) -> "str | None":
    """Load prior session ID for --resume when VNX_SESSION_RESUME=1."""
    if os.environ.get("VNX_SESSION_RESUME", "0") != "1":
        return None
    try:
        from session_store import SessionStore as _SessionStore
        resume_session = _SessionStore().load(terminal_id)
        if resume_session:
            logger.info(
                "deliver_via_subprocess: resuming %s with session_id=%s",
                terminal_id, resume_session,
            )
        return resume_session
    except Exception as _exc:
        logger.debug("deliver_via_subprocess: session load failed: %s", _exc)
        return None


def _save_resume_session(terminal_id: str, session_id: str, dispatch_id: str) -> None:
    """Persist session_id for next dispatch's --resume (success path only)."""
    if not session_id or os.environ.get("VNX_SESSION_RESUME", "0") != "1":
        return
    try:
        from session_store import SessionStore as _SessionStore
        _SessionStore().save(terminal_id, session_id, dispatch_id=dispatch_id)
    except Exception as _exc:
        logger.debug("deliver_via_subprocess: session save failed: %s", _exc)


def _start_heartbeat(
    terminal_id: str,
    dispatch_id: str,
    lease_generation: "int | None",
    heartbeat_interval: float,
) -> tuple["threading.Event | None", "threading.Thread | None"]:
    """Start background lease-renewal thread when lease_generation is known."""
    if lease_generation is None:
        return None, None
    import subprocess_dispatch as _sd
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(terminal_id, dispatch_id, lease_generation, stop_event, _sd._default_state_dir()),
        kwargs={"interval": heartbeat_interval},
        daemon=True,
    )
    thread.start()
    return stop_event, thread


class _StreamState:
    """Mutable state accumulated by the event loop, kept accessible if it raises."""
    __slots__ = ("event_count", "touched_files", "rotation_triggered", "last_stuck_log")

    def __init__(self) -> None:
        self.event_count = 0
        self.touched_files: set[str] = set()
        self.rotation_triggered = False
        self.last_stuck_log = 0.0


def _consume_event_stream(
    adapter,
    terminal_id: str,
    dispatch_id: str,
    chunk_timeout: float,
    total_deadline: float,
    health_monitor,
    tracker,
    repo_root: Path,
    state: "_StreamState",
) -> None:
    """Drive the event loop, mutating ``state`` so partial values survive exceptions."""
    import subprocess_dispatch as _sd
    from worker_health_monitor import HealthStatus, SLOW_THRESHOLD
    import time as _time

    for event in adapter.read_events_with_timeout(
        terminal_id, chunk_timeout=chunk_timeout, total_deadline=total_deadline,
    ):
        state.event_count += 1
        for raw_path in _extract_touched_paths_from_event(event):
            norm = _normalize_repo_path(raw_path, repo_root)
            if norm:
                state.touched_files.add(norm)
        tracker.update(event)
        if not state.rotation_triggered and tracker.should_rotate:
            state.rotation_triggered = True
            _sd._write_rotation_handover(terminal_id, dispatch_id, tracker)
            try:
                adapter.stop(terminal_id)
            except Exception as _exc:
                logger.debug(
                    "deliver_via_subprocess: adapter.stop after rotation failed: %s", _exc,
                )
            break
        if health_monitor is not None:
            health_monitor.update(event)
            now = _time.monotonic()
            if now - state.last_stuck_log >= SLOW_THRESHOLD:
                h = health_monitor.health_status()
                if h.status == HealthStatus.STUCK:
                    health_monitor.log_stuck_event()
                    state.last_stuck_log = now


def _wire_event_store_into_health(adapter, health_monitor) -> None:
    """Wire event_store into health_monitor so STUCK events persist to NDJSON."""
    if health_monitor is None or health_monitor._event_store is not None:
        return
    es = adapter.event_store
    if es is not None:
        health_monitor._event_store = es


def _mark_handover_processed(pending_handover: "Path | None") -> None:
    """Rename the handover file with .processed suffix after successful delivery."""
    if pending_handover is None or not pending_handover.exists():
        return
    processed = pending_handover.with_suffix(pending_handover.suffix + ".processed")
    try:
        pending_handover.rename(processed)
        logger.info(
            "deliver_via_subprocess: handover marked processed: %s", processed,
        )
    except Exception as exc:
        logger.warning(
            "deliver_via_subprocess: failed to mark handover processed: %s", exc,
        )


def _classify_completion(
    adapter,
    terminal_id: str,
    dispatch_id: str,
    session_id: "str | None",
    event_count: int,
    touched_files: set,
    manifest_path: "str | None",
    rotation_triggered: bool,
    pending_handover: "Path | None",
) -> _SubprocessResult:
    """Apply fail-closed checks and return the final _SubprocessResult."""
    import subprocess_dispatch as _sd
    if rotation_triggered:
        completed = _sd._promote_manifest(dispatch_id)
        return _SubprocessResult(
            success=False, session_id=session_id, event_count=event_count,
            manifest_path=completed or manifest_path,
            touched_files=frozenset(touched_files),
        )
    obs = adapter.observe(terminal_id)
    returncode = obs.transport_state.get("returncode")
    if returncode is None:
        # Fallback: stop() caches returncode before removing the process (OI-1120)
        returncode = getattr(adapter, "_returncode_cache", {}).get(terminal_id)
    if returncode is not None and returncode != 0:
        logger.warning(
            "deliver_via_subprocess: subprocess exited %d for %s — fail-closed",
            returncode, terminal_id,
        )
        # OI-1319: do NOT promote to dead_letter here; the deliver_with_recovery
        # retry loop may succeed on a subsequent attempt.  dead_letter promotion
        # happens in _handle_final_failure() only after all retries are exhausted,
        # preventing dual-bucketing (manifest in both dead_letter/ and completed/).
        return _SubprocessResult(
            success=False, session_id=session_id, event_count=event_count,
            manifest_path=manifest_path,
            touched_files=frozenset(touched_files),
        )
    if adapter.was_timed_out(terminal_id):
        logger.warning(
            "deliver_via_subprocess: timeout-terminated dispatch %s for %s — fail-closed",
            dispatch_id, terminal_id,
        )
        # OI-1319: same as above — defer dead_letter promotion to _handle_final_failure().
        return _SubprocessResult(
            success=False, session_id=session_id, event_count=event_count,
            manifest_path=manifest_path,
            touched_files=frozenset(touched_files),
        )
    completed = _sd._promote_manifest(dispatch_id, stage="completed")
    _save_resume_session(terminal_id, session_id or "", dispatch_id)
    _mark_handover_processed(pending_handover)
    return _SubprocessResult(
        success=True, session_id=session_id, event_count=event_count,
        manifest_path=completed or manifest_path,
        touched_files=frozenset(touched_files),
    )


def _cleanup_dispatch_resources(
    adapter,
    terminal_id: str,
    dispatch_id: str,
    agent_cwd: "Path | None",
    heartbeat_stop: "threading.Event | None",
    heartbeat_thread: "threading.Thread | None",
) -> None:
    """Stop heartbeat, archive+clear events, trigger report pipeline."""
    if heartbeat_stop is not None:
        heartbeat_stop.set()
    if heartbeat_thread is not None:
        heartbeat_thread.join(timeout=5)
    event_store = adapter.event_store
    if event_store is not None:
        try:
            event_store.clear(terminal_id, archive_dispatch_id=dispatch_id)
        except Exception as _exc:
            logger.debug("deliver_via_subprocess: event archive+clear failed: %s", _exc)
    adapter.trigger_report_pipeline(
        terminal_id, dispatch_id,
        cwd=str(agent_cwd) if agent_cwd is not None else None,
    )


def _prepare_dispatch(
    terminal_id: str,
    instruction: str,
    model: str,
    dispatch_id: str,
    role: str | None,
    repo_map: str | None,
    commit_hash_before: str,
) -> tuple[str, "Path | None", "Path | None", str]:
    """Run pre-deliver phases: handover continuation, assembly, cwd, manifest write.

    Returns (instruction, pending_handover, agent_cwd, manifest_path).
    """
    import subprocess_dispatch as _sd
    instruction, pending_handover = _apply_handover_continuation(terminal_id, instruction)
    instruction = _assemble_instruction(
        terminal_id, instruction, role, dispatch_id, model, repo_map,
    )
    agent_cwd = _resolve_agent_cwd_and_log_profile(role)
    manifest_path = _sd._write_manifest(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        model=model,
        role=role,
        instruction=instruction,
        commit_hash_before=commit_hash_before,
        branch=_sd._get_current_branch(),
    )
    return instruction, pending_handover, agent_cwd, manifest_path


def _run_event_loop(
    adapter,
    terminal_id: str,
    dispatch_id: str,
    chunk_timeout: float,
    total_deadline: float,
    health_monitor,
    pending_handover: "Path | None",
    manifest_path: "str | None",
) -> _SubprocessResult:
    """Drive the event consumption + completion classification body."""
    import subprocess_dispatch as _sd
    repo_root = Path(__file__).resolve().parents[3]
    tracker = _sd.HeadlessContextTracker(model_context_limit=200_000)
    state = _StreamState()
    try:
        _consume_event_stream(
            adapter, terminal_id, dispatch_id,
            chunk_timeout, total_deadline,
            health_monitor, tracker, repo_root, state,
        )
        session_id = adapter.get_session_id(terminal_id)
        return _classify_completion(
            adapter, terminal_id, dispatch_id, session_id,
            state.event_count, state.touched_files, manifest_path,
            state.rotation_triggered, pending_handover,
        )
    except Exception:
        logger.exception("deliver_via_subprocess failed for %s", terminal_id)
        return _SubprocessResult(
            success=False, session_id=None, event_count=state.event_count,
            manifest_path=manifest_path,
            touched_files=frozenset(state.touched_files),
        )


def deliver_via_subprocess(
    terminal_id: str,
    instruction: str,
    model: str,
    dispatch_id: str,
    *,
    role: str | None = None,
    repo_map: str | None = None,
    lease_generation: int | None = None,
    heartbeat_interval: float = 300.0,
    chunk_timeout: float = 300.0,
    total_deadline: float = 900.0,
    health_monitor=None,
    commit_hash_before: str = "",
) -> _SubprocessResult:
    """Deliver a dispatch via SubprocessAdapter and consume the event stream.

    Composes per-phase helpers in this module: instruction assembly, manifest
    write, heartbeat thread, event consumption, completion classification, and
    cleanup.  Returns ``_SubprocessResult`` (success, session_id, event_count,
    manifest_path, touched_files).
    """
    import subprocess_dispatch as _sd
    chunk_timeout, total_deadline = _apply_runtime_overrides(chunk_timeout, total_deadline)

    instruction, pending_handover, agent_cwd, manifest_path = _prepare_dispatch(
        terminal_id, instruction, model, dispatch_id, role, repo_map, commit_hash_before,
    )

    resume_session = _load_resume_session(terminal_id)
    adapter = _sd.SubprocessAdapter()
    result = adapter.deliver(
        terminal_id, dispatch_id,
        instruction=instruction, model=model,
        cwd=agent_cwd, resume_session=resume_session,
    )
    if not result.success:
        return _SubprocessResult(
            success=False, session_id=None, event_count=0,
            manifest_path=manifest_path, touched_files=frozenset(),
        )

    _wire_event_store_into_health(adapter, health_monitor)
    heartbeat_stop, heartbeat_thread = _start_heartbeat(
        terminal_id, dispatch_id, lease_generation, heartbeat_interval,
    )
    try:
        return _run_event_loop(
            adapter, terminal_id, dispatch_id,
            chunk_timeout, total_deadline,
            health_monitor, pending_handover, manifest_path,
        )
    finally:
        _cleanup_dispatch_resources(
            adapter, terminal_id, dispatch_id, agent_cwd,
            heartbeat_stop, heartbeat_thread,
        )
