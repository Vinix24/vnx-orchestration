"""delivery — deliver_via_subprocess + phase helpers.

Cross-module helpers (``_inject_skill_context``, ``_write_manifest``, ...)
and module-level globals exposed by the facade (``SubprocessAdapter``,
``WorkerHealthMonitor``) are looked up via ``import subprocess_dispatch as _sd``
at call time so that ``unittest.mock.patch("subprocess_dispatch.X")``
intercepts the call from inside this module.

Wave 4.6 PR-4.6.2: the spawn+stream slice (SubprocessAdapter.deliver() +
read_events_with_timeout()) is now delegated to
``provider_spawns.claude_spawn.spawn_claude()``.  All governance concerns
(rotation detection, path extraction, heartbeat, event archive, report
pipeline trigger) remain here — byte-identical to the pre-refactor path.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from .delivery_runtime import _SubprocessResult, _heartbeat_loop
from .path_utils import _extract_touched_paths_from_event, _normalize_repo_path

logger = logging.getLogger(__name__)

# Side-channel for token_usage from spawn_claude — keyed by dispatch_id.
# Written by deliver_via_subprocess after each spawn; read+popped by
# _dispatch_claude in provider_dispatch.py immediately after deliver_with_recovery.
_dispatch_token_usage: dict = {}


def _build_worker_identity_env(terminal_id: str) -> dict[str, str]:
    """Resolve the orchestrator's identity and project it onto the worker env.

    The worker's ``resolve_identity()`` call will pick these up at the head of
    its resolution chain so receipts written from inside the subprocess are
    attributed back to the correct {operator, project, orchestrator, agent}.
    The ``agent_id`` slot is filled with the worker's terminal label
    (``t1``/``t2``/``t3``) when the orchestrator does not have a fixed
    agent_id of its own — agents are per-terminal, not per-orchestrator.
    Resolution failures are non-fatal: we return an empty mapping so the
    spawn still happens (legacy behaviour).
    """
    import subprocess_dispatch as _sd  # noqa: F401  (kept for facade parity)
    try:
        from vnx_identity import try_resolve_identity, ENV_AGENT
    except Exception:
        return {}
    identity = try_resolve_identity()
    if identity is None:
        return {}
    env = dict(identity.to_env())
    if ENV_AGENT not in env and terminal_id:
        agent_label = terminal_id.lower()
        from vnx_identity import ID_REGEX
        if ID_REGEX.match(agent_label):
            env[ENV_AGENT] = agent_label
    return env


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
    dispatch_paths: "list[str] | None" = None,
    pr_id: "str | None" = None,
) -> str:
    """Append repo map, layered skill context, then permission preamble."""
    import subprocess_dispatch as _sd
    if repo_map:
        instruction = instruction + f"\n\n{repo_map}"
    instruction = _sd._inject_skill_context(
        terminal_id,
        instruction,
        role=role,
        dispatch_metadata={
            "dispatch_id": dispatch_id,
            "model": model,
            "dispatch_paths": dispatch_paths,
            "pr_id": pr_id,
            "pr": pr_id,  # PromptAssembler renders metadata["pr"]; set both keys (CFX-W5-2)
        },
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


def _prepare_dispatch(
    terminal_id: str,
    instruction: str,
    model: str,
    dispatch_id: str,
    role: str | None,
    repo_map: str | None,
    commit_hash_before: str,
    dispatch_paths: "list[str] | None" = None,
    pr_id: "str | None" = None,
) -> tuple[str, "Path | None", "Path | None", str]:
    """Run pre-deliver phases: handover continuation, assembly, cwd, manifest write.

    Returns (instruction, pending_handover, agent_cwd, manifest_path).
    """
    import subprocess_dispatch as _sd
    instruction, pending_handover = _apply_handover_continuation(terminal_id, instruction)
    instruction = _assemble_instruction(
        terminal_id, instruction, role, dispatch_id, model, repo_map,
        dispatch_paths=dispatch_paths, pr_id=pr_id,
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
    dispatch_paths: "list[str] | None" = None,
    pr_id: "str | None" = None,
) -> _SubprocessResult:
    """Deliver a dispatch via spawn_claude() and consume the event stream.

    Composes per-phase helpers in this module: instruction assembly, manifest
    write, heartbeat thread, governance event handlers (rotation + path
    extraction), completion classification, and cleanup.  Returns
    ``_SubprocessResult`` (success, session_id, event_count, manifest_path,
    touched_files).

    The spawn+stream slice (Popen + NDJSON stream reading) is delegated to
    ``provider_spawns.claude_spawn.spawn_claude()``.  All governance concerns
    remain here — byte-identical to the pre-PR-4.6.2 behavior.

    dispatch_paths and pr_id are forwarded to dispatch_metadata so W5 item
    classes (adr_relevant, code_anchor, operator_memory, schema_section,
    prior_round_finding) fire in IntelligenceSelector.select() during assembly.
    """
    import subprocess_dispatch as _sd
    from provider_spawns.claude_spawn import spawn_claude

    chunk_timeout, total_deadline = _apply_runtime_overrides(chunk_timeout, total_deadline)

    instruction, pending_handover, agent_cwd, manifest_path = _prepare_dispatch(
        terminal_id, instruction, model, dispatch_id, role, repo_map, commit_hash_before,
        dispatch_paths=dispatch_paths, pr_id=pr_id,
    )

    resume_session = _load_resume_session(terminal_id)
    extra_env = _build_worker_identity_env(terminal_id)

    # PR-6.5e: per-worker state directory for N-worker isolation.
    from vnx_paths import resolve_worker_state_dir
    worker_state_dir = resolve_worker_state_dir(terminal_id)
    extra_env["VNX_WORKER_STATE_DIR"] = str(worker_state_dir)

    heartbeat_stop, heartbeat_thread = _start_heartbeat(
        terminal_id, dispatch_id, lease_generation, heartbeat_interval,
    )

    # Governance state accumulated by the per-event callback below.
    repo_root = Path(__file__).resolve().parents[3]
    tracker = _sd.HeadlessContextTracker(model_context_limit=200_000)
    touched_files: set[str] = set()
    rotation_triggered_box: list[bool] = [False]  # mutable cell for closure
    spawn_result = None  # initialised here so the finally block can always reference it

    def _governance_on_event(event) -> bool:
        """Per-event governance handler passed to spawn_claude as on_event.

        Extracts touched paths, drives the context-rotation tracker, and
        returns False (stop stream) when the rotation threshold is reached.
        Health-monitor stuck-logging is handled inside spawn_claude itself.
        """
        for raw_path in _extract_touched_paths_from_event(event):
            norm = _normalize_repo_path(raw_path, repo_root)
            if norm:
                touched_files.add(norm)
        tracker.update(event)
        if not rotation_triggered_box[0] and tracker.should_rotate:
            rotation_triggered_box[0] = True
            _sd._write_rotation_handover(terminal_id, dispatch_id, tracker)
            return False  # signal spawn_claude to stop the stream
        return True

    try:
        spawn_result = spawn_claude(
            prompt=instruction,
            model=model,
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            health_monitor=health_monitor,
            on_event=_governance_on_event,
            extra_env=extra_env,
            cwd=agent_cwd,
            resume_session=resume_session,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        )

        if spawn_result.token_usage:
            _dispatch_token_usage[dispatch_id] = spawn_result.token_usage

        # --- completion classification (mirrors _classify_completion) ---
        session_id = spawn_result.session_id
        event_count = spawn_result.events_written
        rotation_triggered = rotation_triggered_box[0]

        if rotation_triggered:
            completed = _sd._promote_manifest(dispatch_id)
            return _SubprocessResult(
                success=False, session_id=session_id, event_count=event_count,
                manifest_path=completed or manifest_path,
                touched_files=frozenset(touched_files),
            )

        if spawn_result.returncode != 0 or spawn_result.timed_out:
            if spawn_result.timed_out:
                logger.warning(
                    "deliver_via_subprocess: timeout-terminated dispatch %s for %s — fail-closed",
                    dispatch_id, terminal_id,
                )
            else:
                logger.warning(
                    "deliver_via_subprocess: subprocess exited %d for %s — fail-closed",
                    spawn_result.returncode, terminal_id,
                )
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

    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=5)
        # Event archive + report-pipeline trigger using the same adapter instance
        # that ran the dispatch (via spawn_result._adapter).  This preserves
        # session_id in the trigger file — byte-identical with the pre-4.6.2 path.
        # Fall back to a fresh adapter if spawn_result is undefined (rare exception path).
        try:
            _cleanup_adapter = (
                spawn_result._adapter
                if spawn_result is not None and spawn_result._adapter is not None
                else _sd.SubprocessAdapter()
            )
            event_store = _cleanup_adapter.event_store
            if event_store is not None:
                try:
                    event_store.clear(terminal_id, archive_dispatch_id=dispatch_id)
                except Exception as _exc:
                    logger.debug(
                        "deliver_via_subprocess: event archive+clear failed: %s", _exc,
                    )
            _cleanup_adapter.trigger_report_pipeline(
                terminal_id, dispatch_id,
                cwd=str(agent_cwd) if agent_cwd is not None else None,
            )
        except Exception as _exc:
            logger.debug(
                "deliver_via_subprocess: cleanup adapter failed: %s", _exc,
            )
