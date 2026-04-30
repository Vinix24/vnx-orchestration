#!/usr/bin/env python3
"""subprocess_dispatch.py — Thin helper for routing dispatch delivery via SubprocessAdapter.

Called from dispatch_deliver.sh when VNX_ADAPTER_T{n}=subprocess is set.

BILLING SAFETY: Only calls subprocess.Popen(["claude", ...]). No Anthropic SDK.

Delivery helpers are split across sub-modules for maintainability:
  dispatch_context.py   — context/permission/intelligence injection
  dispatch_git_ops.py   — git utilities, auto-commit, auto-stash
  dispatch_manifest.py  — manifest lifecycle, receipts, telemetry
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))

from subprocess_adapter import SubprocessAdapter
from headless_context_tracker import HeadlessContextTracker
from worker_health_monitor import WorkerHealthMonitor, HealthStatus, SLOW_THRESHOLD
from cleanup_worker_exit import cleanup_worker_exit

# Sub-module imports — re-exported here for backward compatibility so callers
# that do `from subprocess_dispatch import _write_receipt` etc. keep working.
from dispatch_context import (
    _default_state_dir,
    _resolve_active_dispatch_file,
    _inject_permission_profile,
    _build_intelligence_section,
    _inject_skill_context,
    _resolve_agent_cwd,
    _load_agent_profile,
)
from dispatch_git_ops import (
    _FILE_WRITING_TOOLS,
    _normalize_repo_path,
    _extract_touched_paths_from_event,
    _parse_dirty_files,
    _get_dirty_files,
    _get_commit_hash,
    _count_lines_changed_since_sha,
    _get_current_branch,
    _check_commit_since,
    _commit_belongs_to_dispatch,
    _auto_commit_changes,
    _auto_stash_changes,
)
from dispatch_manifest import (
    _dispatch_manifest_dir,
    _write_manifest,
    _safe_remove_active_dir,
    _promote_manifest,
    _heartbeat_loop,
    _detect_pending_handover,
    _build_continuation_prompt,
    _write_rotation_handover,
    _write_receipt,
    _capture_dispatch_parameters,
    _capture_dispatch_outcome,
    _update_pattern_confidence,
)

logger = logging.getLogger(__name__)


class _SubprocessResult(NamedTuple):
    """Return value from deliver_via_subprocess() carrying stats back to the caller."""
    success: bool
    session_id: str | None
    event_count: int
    manifest_path: str | None
    # Repo-relative paths the worker explicitly wrote/edited via structured tool
    # calls (Write/Edit/MultiEdit/NotebookEdit) during this dispatch.  Used by
    # _auto_commit_changes / _auto_stash_changes to scope staging to *this*
    # worker's writes, even in shared worktrees where concurrent terminals or
    # the operator may produce additional dirty files during the dispatch
    # window.  Empty frozenset() when no structured file writes occurred.
    touched_files: frozenset[str] = frozenset()


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
    health_monitor: "WorkerHealthMonitor | None" = None,
    commit_hash_before: str = "",
) -> "_SubprocessResult":
    """Deliver a dispatch instruction to terminal_id via SubprocessAdapter.

    Blocks until the subprocess exits, consuming all stream events.
    Events are persisted to EventStore via read_events_with_timeout() internally.

    If role is provided, _inject_skill_context() resolves the matching
    CLAUDE.md via 3-tier lookup and SubprocessAdapter uses the agent dir
    as cwd when available.

    If repo_map is provided (pre-formatted repo map string), it is appended
    to the instruction before skill context injection.  This is the direct-
    caller path; dispatches routed through DispatchDaemon receive repo maps
    via DispatchEnricher instead.

    If lease_generation is provided, a background heartbeat thread renews the
    lease every heartbeat_interval seconds to prevent TTL expiry during long tasks.

    If health_monitor is provided, each streamed event is fed into it.

    Returns _SubprocessResult(success, session_id, event_count, manifest_path).
    """
    # Allow runtime override via env vars so operators can tune without code changes.
    try:
        chunk_timeout = float(os.environ["VNX_CHUNK_TIMEOUT"])
    except (KeyError, ValueError):
        pass
    try:
        total_deadline = float(os.environ["VNX_TOTAL_DEADLINE"])
    except (KeyError, ValueError):
        pass

    # Detect pending handover and wrap instruction for seamless continuation.
    # Runs on every attempt (including retries) so continuation is always fresh.
    handover_dir = _default_state_dir().parent / "rotation_handovers"
    pending_handover = _detect_pending_handover(terminal_id, handover_dir)
    if pending_handover is not None:
        logger.info(
            "deliver_via_subprocess: pending handover found for %s: %s",
            terminal_id, pending_handover,
        )
        instruction = _build_continuation_prompt(pending_handover, instruction)

    # Append repo map to instruction before skill context wrapping
    if repo_map:
        instruction = instruction + f"\n\n{repo_map}"

    # Compose layered user message (L1 base + L2 role + L3 dispatch payload)
    instruction = _inject_skill_context(
        terminal_id,
        instruction,
        role=role,
        dispatch_metadata={
            "dispatch_id": dispatch_id,
            "model": model,
        },
    )

    # Inject per-terminal permission profile preamble
    instruction = _inject_permission_profile(terminal_id, role, instruction)

    # Resolve agent cwd: agents/{role}/ dir takes precedence when it exists
    agent_cwd = _resolve_agent_cwd(role)

    # Load and log governance profile from agent config.yaml
    if agent_cwd is not None:
        config_path = agent_cwd / "config.yaml"
        if config_path.exists():
            profile = _load_agent_profile(config_path)
            logger.info("Agent %s using governance profile: %s", role, profile)

    # Write dispatch manifest before launching subprocess
    manifest_path = _write_manifest(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        model=model,
        role=role,
        instruction=instruction,
        commit_hash_before=commit_hash_before,
        branch=_get_current_branch(),
    )

    # Load prior session ID for --resume (opt-in via VNX_SESSION_RESUME=1)
    resume_session: str | None = None
    if os.environ.get("VNX_SESSION_RESUME", "0") == "1":
        try:
            from session_store import SessionStore as _SessionStore
            resume_session = _SessionStore().load(terminal_id)
            if resume_session:
                logger.info(
                    "deliver_via_subprocess: resuming %s with session_id=%s",
                    terminal_id, resume_session,
                )
        except Exception as _exc:
            logger.debug("deliver_via_subprocess: session load failed: %s", _exc)

    adapter = SubprocessAdapter()
    result = adapter.deliver(
        terminal_id,
        dispatch_id,
        instruction=instruction,
        model=model,
        cwd=agent_cwd,
        resume_session=resume_session,
    )
    if not result.success:
        return _SubprocessResult(
            success=False,
            session_id=None,
            event_count=0,
            manifest_path=manifest_path,
            touched_files=frozenset(),
        )

    # Resolve repo root once for path normalization; agent_cwd may point into
    # a sub-directory but the repo root anchors all git status output.
    _repo_root = Path(__file__).resolve().parents[2]
    _touched_files: set[str] = set()

    # Wire event_store into health_monitor so STUCK events are persisted to NDJSON
    if health_monitor is not None and health_monitor._event_store is None:
        _es = adapter._get_event_store()
        if _es is not None:
            health_monitor._event_store = _es

    # Start heartbeat thread if lease generation is known
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None

    if lease_generation is not None:
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(terminal_id, dispatch_id, lease_generation, heartbeat_stop, _default_state_dir()),
            kwargs={"interval": heartbeat_interval},
            daemon=True,
        )
        heartbeat_thread.start()

    tracker = HeadlessContextTracker(model_context_limit=200_000)
    event_count = 0
    _last_stuck_log_time = 0.0
    rotation_triggered = False
    try:
        for _event in adapter.read_events_with_timeout(
            terminal_id,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        ):
            event_count += 1
            # Track files written by this dispatch's structured tool calls so
            # auto-commit / auto-stash can scope to *this* worker's writes,
            # not whatever else became dirty in a shared worktree.
            for raw_path in _extract_touched_paths_from_event(_event):
                norm = _normalize_repo_path(raw_path, _repo_root)
                if norm:
                    _touched_files.add(norm)
            # Update the context tracker.  ``_event`` is a StreamEvent
            # (dataclass with ``.type``/``.data`` attrs); HeadlessContextTracker
            # accepts either a StreamEvent-like object or a plain dict thanks
            # to its internal normalisation, so this call is type-safe even
            # though the unit tests pass plain dicts.
            tracker.update(_event)
            # Detect rotation as soon as the threshold is crossed and write
            # the handover exactly once — checking *inside* the loop avoids
            # the prior bug where a brief late-stream context spike on a
            # normally-completing dispatch flipped the post-loop
            # ``should_rotate`` check and returned success=False.  Once
            # rotation fires we stop the subprocess so the worker can resume
            # via the handover on the next dispatch.
            if not rotation_triggered and tracker.should_rotate:
                rotation_triggered = True
                _write_rotation_handover(terminal_id, dispatch_id, tracker)
                try:
                    adapter.stop(terminal_id)
                except Exception as _exc:
                    logger.debug(
                        "deliver_via_subprocess: adapter.stop after rotation failed: %s",
                        _exc,
                    )
                break
            if health_monitor is not None:
                health_monitor.update(_event)
                # Log stuck warning at most once per SLOW_THRESHOLD window
                import time as _time
                _now = _time.monotonic()
                if _now - _last_stuck_log_time >= SLOW_THRESHOLD:
                    h = health_monitor.health_status()
                    if h.status == HealthStatus.STUCK:
                        health_monitor.log_stuck_event()
                        _last_stuck_log_time = _now

        session_id = adapter.get_session_id(terminal_id)

        # Rotation is a graceful stop: surface as success=False so
        # deliver_with_recovery's auto-stash protects partial work and the
        # next dispatch (or the immediate retry) sees the handover.  We
        # *only* take this branch when rotation was actually triggered
        # mid-stream — never based on the post-loop tracker state alone.
        if rotation_triggered:
            completed_manifest = _promote_manifest(dispatch_id)
            return _SubprocessResult(
                success=False,
                session_id=session_id,
                event_count=event_count,
                manifest_path=completed_manifest or manifest_path,
                touched_files=frozenset(_touched_files),
            )

        # Fail-closed: non-zero exit code means failure even when events were parsed.
        # Manifest promotion is deferred until after all fail-closed checks so
        # failed dispatches are routed to dead_letter/ rather than completed/.
        obs = adapter.observe(terminal_id)
        returncode = obs.transport_state.get("returncode")
        if returncode is not None and returncode != 0:
            logger.warning(
                "deliver_via_subprocess: subprocess exited %d for %s — fail-closed",
                returncode,
                terminal_id,
            )
            dead_manifest = _promote_manifest(dispatch_id, stage="dead_letter")
            return _SubprocessResult(
                success=False,
                session_id=session_id,
                event_count=event_count,
                manifest_path=dead_manifest or manifest_path,
                touched_files=frozenset(_touched_files),
            )
        # Fail-closed: timeout-terminated dispatches must not be classified as success.
        # stop() removes the process from _processes so returncode above is None,
        # but was_timed_out() tracks the authoritative kill-by-timeout signal.
        if adapter.was_timed_out(terminal_id):
            logger.warning(
                "deliver_via_subprocess: timeout-terminated dispatch %s for %s — fail-closed",
                dispatch_id,
                terminal_id,
            )
            dead_manifest = _promote_manifest(dispatch_id, stage="dead_letter")
            return _SubprocessResult(
                success=False,
                session_id=session_id,
                event_count=event_count,
                manifest_path=dead_manifest or manifest_path,
                touched_files=frozenset(_touched_files),
            )

        # All fail-closed checks passed — promote manifest to completed/.
        completed_manifest = _promote_manifest(dispatch_id, stage="completed")

        # Only persist session_id once all fail-closed checks pass, so the next
        # dispatch (with VNX_SESSION_RESUME=1) cannot resume a failed or
        # timeout-killed conversation.
        if session_id and os.environ.get("VNX_SESSION_RESUME", "0") == "1":
            try:
                from session_store import SessionStore as _SessionStore
                _SessionStore().save(terminal_id, session_id, dispatch_id=dispatch_id)
            except Exception as _exc:
                logger.debug("deliver_via_subprocess: session save failed: %s", _exc)

        # Mark handover as processed after successful delivery (not rotation)
        if pending_handover is not None and pending_handover.exists():
            processed_path = pending_handover.with_suffix(pending_handover.suffix + ".processed")
            try:
                pending_handover.rename(processed_path)
                logger.info(
                    "deliver_via_subprocess: handover marked processed: %s",
                    processed_path,
                )
            except Exception as exc:
                logger.warning(
                    "deliver_via_subprocess: failed to mark handover processed: %s", exc
                )

        return _SubprocessResult(
            success=True,
            session_id=session_id,
            event_count=event_count,
            manifest_path=completed_manifest or manifest_path,
            touched_files=frozenset(_touched_files),
        )
    except Exception:
        logger.exception("deliver_via_subprocess failed for %s", terminal_id)
        return _SubprocessResult(
            success=False,
            session_id=None,
            event_count=event_count,
            manifest_path=manifest_path,
            touched_files=frozenset(_touched_files),
        )
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=5)
        # Archive events for this dispatch and clear the live file immediately so
        # the next dispatch starts with an empty file (guarantees 100% archival).
        event_store = adapter._get_event_store()
        if event_store is not None:
            try:
                event_store.clear(terminal_id, archive_dispatch_id=dispatch_id)
            except Exception as _exc:
                logger.debug("deliver_via_subprocess: event archive+clear failed: %s", _exc)
        # Trigger auto-report pipeline on completion (gated by VNX_AUTO_REPORT=1)
        adapter.trigger_report_pipeline(
            terminal_id,
            dispatch_id,
            cwd=str(agent_cwd) if agent_cwd is not None else None,
        )


def deliver_with_recovery(
    terminal_id: str,
    instruction: str,
    model: str,
    dispatch_id: str,
    *,
    role: str | None = None,
    repo_map: str | None = None,
    max_retries: int = 3,
    lease_generation: int | None = None,
    heartbeat_interval: float = 300.0,
    chunk_timeout: float = 300.0,
    total_deadline: float = 900.0,
    auto_commit: bool = True,
    gate: str = "",
) -> bool:
    """Deliver with automatic retry on failure.

    On success, writes a receipt with status="done".
    On final failure (budget exhausted), writes a receipt with status="failed".
    Retries use exponential backoff: 30s, 60s, 120s.

    auto_commit: if True (default), auto-commit uncommitted changes on success,
                 auto-stash on failure.  Pass False to disable.
    gate: gate tag used in the auto-commit message (e.g. "f52-pr3").
    repo_map: optional repo map string, forwarded to parameter tracker only.

    Returns True on success, False on failure.
    """
    # Allow runtime override via env vars so operators can tune without code changes.
    try:
        chunk_timeout = float(os.environ["VNX_CHUNK_TIMEOUT"])
    except (KeyError, ValueError):
        pass
    try:
        total_deadline = float(os.environ["VNX_TOTAL_DEADLINE"])
    except (KeyError, ValueError):
        pass

    dispatch_start_ts = datetime.now(timezone.utc).isoformat()
    commit_hash_before = _get_commit_hash()
    _dispatch_pre_sha = commit_hash_before
    # Snapshot dirty files before dispatch so auto-commit/stash can scope to
    # changes introduced by this dispatch only (not pre-existing dirty state).
    _repo_cwd = Path(__file__).resolve().parents[2]
    pre_dispatch_dirty = _get_dirty_files(_repo_cwd)

    # Read dispatch path manifest (CFX-1).  Workers declare allowed mutation
    # paths via dispatch_paths.write_manifest before delivery; auto-commit /
    # auto-stash will then refuse to touch files outside this scope, even when
    # dirty.  None means "no manifest declared" — legacy pre_dispatch_dirty
    # scoping applies (with a deprecation warning logged inside the helpers).
    try:
        from dispatch_paths import read_manifest as _read_manifest
        manifest_paths = _read_manifest(_default_state_dir(), dispatch_id)
    except Exception as _exc:
        logger.debug("dispatch_paths manifest read failed: %s", _exc)
        manifest_paths = None
    if manifest_paths is not None:
        logger.info(
            "deliver_with_recovery: dispatch %s declared %d manifest path(s): %s",
            dispatch_id, len(manifest_paths), manifest_paths,
        )

    # Capture dispatch parameters before execution
    _capture_dispatch_parameters(
        dispatch_id=dispatch_id,
        instruction=instruction,
        terminal_id=terminal_id,
        model=model,
        role=role,
        repo_map=repo_map,
    )

    # Create health monitor for this dispatch
    monitor = WorkerHealthMonitor(terminal_id, dispatch_id)

    for attempt in range(max_retries + 1):
        sub_result = deliver_via_subprocess(
            terminal_id,
            instruction,
            model,
            dispatch_id,
            role=role,
            lease_generation=lease_generation,
            heartbeat_interval=heartbeat_interval,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
            health_monitor=monitor,
            commit_hash_before=commit_hash_before,
        )
        if sub_result.success:
            monitor.mark_completed()
            commit_hash_after = _get_commit_hash()
            commit_missing = _check_commit_since(dispatch_start_ts, dispatch_id=dispatch_id)

            # Post-dispatch commit enforcement.
            #
            # ``committed`` must mean "this dispatch produced a commit", not
            # "HEAD moved during the dispatch window" — in a shared worktree
            # another terminal can commit concurrently, which would otherwise
            # be mis-attributed here.  We require BOTH (a) HEAD changed and
            # (b) the new commit message references this dispatch_id.
            head_moved = bool(
                commit_hash_before and commit_hash_after and commit_hash_before != commit_hash_after
            )
            committed = head_moved and _commit_belongs_to_dispatch(
                commit_hash_after, dispatch_id
            )
            if auto_commit and commit_missing and not committed:
                committed = _auto_commit_changes(
                    dispatch_id, terminal_id, gate=gate,
                    pre_dispatch_dirty=pre_dispatch_dirty,
                    dispatch_touched_files=sub_result.touched_files,
                    manifest_paths=manifest_paths,
                )
                if committed:
                    commit_missing = False
                    commit_hash_after = _get_commit_hash()

            _write_receipt(
                dispatch_id, terminal_id, "done",
                event_count=sub_result.event_count,
                session_id=sub_result.session_id,
                attempt=attempt,
                commit_missing=commit_missing,
                committed=committed,
                commit_hash_before=commit_hash_before,
                commit_hash_after=commit_hash_after,
                manifest_path=sub_result.manifest_path,
                stuck_event_count=monitor.stuck_count,
            )

            # Feedback loop: boost pattern confidence for successful dispatch
            # update_confidence_from_outcome is handled by append_receipt_payload (VNX-R4)
            _quality_db = _default_state_dir() / "quality_intelligence.db"
            _patt_updated = _update_pattern_confidence(dispatch_id, "success", _quality_db)
            logger.debug(
                "Feedback boost: dispatch=%s patterns_updated=%d", dispatch_id, _patt_updated
            )

            # Capture outcome after receipt is written.  pre_sha drives a
            # HEAD-comparison line count that ignores parallel-dispatch commits.
            _capture_dispatch_outcome(
                dispatch_id=dispatch_id,
                success=True,
                start_ts=dispatch_start_ts,
                committed=committed,
                pre_sha=_dispatch_pre_sha,
                manifest_paths=manifest_paths,
            )

            # Single-owner post-exit cleanup (SUP-PR1).  Idempotent — the bash
            # caller's rc_release_lease may have already released the lease,
            # which is fine; this records the worker state transition and
            # dispatch_register audit event regardless.
            cleanup_worker_exit(
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                exit_status="success",
                lease_generation=lease_generation,
                dispatch_file=_resolve_active_dispatch_file(dispatch_id),
            )
            return True

        if attempt < max_retries:
            backoff = 30 * (2 ** attempt)  # 30s, 60s, 120s
            logger.warning(
                "Delivery failed for %s, retry %d/%d in %ds",
                dispatch_id, attempt + 1, max_retries, backoff,
            )
            time.sleep(backoff)
        else:
            monitor.mark_completed()
            # Stash uncommitted changes from failed dispatch
            if auto_commit:
                _auto_stash_changes(
                    dispatch_id,
                    terminal_id,
                    pre_dispatch_dirty=pre_dispatch_dirty,
                    dispatch_touched_files=sub_result.touched_files,
                    manifest_paths=manifest_paths,
                )
            _write_receipt(
                dispatch_id, terminal_id, "failed",
                event_count=sub_result.event_count,
                session_id=sub_result.session_id,
                attempt=attempt,
                failure_reason=f"Exhausted {max_retries} retries",
                commit_hash_before=commit_hash_before,
                manifest_path=sub_result.manifest_path,
                stuck_event_count=monitor.stuck_count,
            )

            # Feedback loop: decay pattern confidence for failed dispatch
            # update_confidence_from_outcome is handled by append_receipt_payload (VNX-R4)
            _quality_db = _default_state_dir() / "quality_intelligence.db"
            _patt_updated = _update_pattern_confidence(dispatch_id, "failure", _quality_db)
            logger.debug(
                "Feedback decay: dispatch=%s patterns_updated=%d", dispatch_id, _patt_updated
            )

            # Capture failed outcome.  Pass pre_sha so lines_changed reflects
            # only this dispatch's diff, not parallel work.
            _capture_dispatch_outcome(
                dispatch_id=dispatch_id,
                success=False,
                start_ts=dispatch_start_ts,
                committed=False,
                pre_sha=_dispatch_pre_sha,
                manifest_paths=manifest_paths,
            )

            # Single-owner post-exit cleanup (SUP-PR1).  Routes failure-path
            # disposition (move to rejected/failure/, transition worker to
            # exited_bad, audit) through the same helper as the bash caller.
            cleanup_worker_exit(
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                exit_status="failure",
                lease_generation=lease_generation,
                dispatch_file=_resolve_active_dispatch_file(dispatch_id),
            )

    return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deliver dispatch via SubprocessAdapter")
    parser.add_argument("--terminal-id", required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--role", default=None, help="Agent role for skill context inlining")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--no-auto-commit", action="store_true",
                        help="Disable auto-commit of uncommitted changes after dispatch")
    parser.add_argument("--gate", default="", help="Gate tag for auto-commit message")
    parser.add_argument(
        "--dispatch-paths",
        default="",
        help=(
            "Comma-separated list of paths this dispatch is allowed to mutate "
            "(CFX-1).  Auto-commit/stash will refuse to touch files outside "
            "this scope.  When omitted, legacy pre_dispatch_dirty scoping is used."
        ),
    )
    args = parser.parse_args()

    if args.dispatch_paths.strip():
        from dispatch_paths import write_manifest as _write_dispatch_paths_manifest
        _allowed = [p.strip() for p in args.dispatch_paths.split(",") if p.strip()]
        _write_dispatch_paths_manifest(
            _default_state_dir(), args.dispatch_id, _allowed,
        )

    ok = deliver_with_recovery(
        terminal_id=args.terminal_id,
        instruction=args.instruction,
        model=args.model,
        dispatch_id=args.dispatch_id,
        role=args.role,
        max_retries=args.max_retries,
        auto_commit=not args.no_auto_commit,
        gate=args.gate,
    )
    sys.exit(0 if ok else 1)
