"""recovery — deliver_with_recovery + retry/success/failure phase helpers.

Internal helper references go through ``import subprocess_dispatch as _sd`` so
that ``unittest.mock.patch("subprocess_dispatch.X")`` intercepts the call.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

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


def _read_dispatch_path_manifest(dispatch_id: str) -> "list[str] | None":
    """Read CFX-1 dispatch path manifest; returns None when absent."""
    import subprocess_dispatch as _sd
    try:
        from dispatch_paths import read_manifest as _read_manifest
        manifest_paths = _read_manifest(_sd._default_state_dir(), dispatch_id)
    except Exception as _exc:
        logger.debug("dispatch_paths manifest read failed: %s", _exc)
        return None
    if manifest_paths is not None:
        logger.info(
            "deliver_with_recovery: dispatch %s declared %d manifest path(s): %s",
            dispatch_id, len(manifest_paths), manifest_paths,
        )
    return manifest_paths


def _resolve_committed(
    dispatch_id: str, commit_hash_before: str, commit_hash_after: str,
) -> bool:
    """Determine whether the new HEAD is owned by THIS dispatch (shared-worktree-safe).

    Requires both: HEAD moved AND new commit message references the dispatch_id.
    """
    import subprocess_dispatch as _sd
    head_moved = bool(
        commit_hash_before and commit_hash_after and commit_hash_before != commit_hash_after
    )
    return head_moved and _sd._commit_belongs_to_dispatch(commit_hash_after, dispatch_id)


def _maybe_auto_commit(
    auto_commit: bool,
    commit_missing: bool,
    committed: bool,
    *,
    dispatch_id: str,
    terminal_id: str,
    gate: str,
    pre_dispatch_dirty,
    touched_files,
    manifest_paths,
    commit_hash_after: str,
) -> tuple[bool, bool, str]:
    """Run auto_commit on the success path and refresh post-commit state."""
    import subprocess_dispatch as _sd
    if not (auto_commit and commit_missing and not committed):
        return committed, commit_missing, commit_hash_after
    committed = _sd._auto_commit_changes(
        dispatch_id, terminal_id, gate=gate,
        pre_dispatch_dirty=pre_dispatch_dirty,
        dispatch_touched_files=touched_files,
        manifest_paths=manifest_paths,
    )
    if committed:
        commit_missing = False
        commit_hash_after = _sd._get_commit_hash()
    return committed, commit_missing, commit_hash_after


def _resolve_active_dispatch_file(dispatch_id: str):
    """Forward to the facade-exposed helper."""
    import subprocess_dispatch as _sd
    return _sd._resolve_active_dispatch_file(dispatch_id)


def _handle_success(
    *,
    dispatch_id: str,
    terminal_id: str,
    attempt: int,
    sub_result,
    monitor,
    auto_commit: bool,
    gate: str,
    pre_dispatch_dirty,
    manifest_paths,
    commit_hash_before: str,
    dispatch_start_ts: str,
    pre_sha: str,
    lease_generation: "int | None",
) -> None:
    """Run the success branch: write receipt, feedback, outcome capture, cleanup."""
    import subprocess_dispatch as _sd
    monitor.mark_completed()
    commit_hash_after = _sd._get_commit_hash()
    commit_missing = _sd._check_commit_since(dispatch_start_ts, dispatch_id=dispatch_id)
    committed = _resolve_committed(dispatch_id, commit_hash_before, commit_hash_after)
    committed, commit_missing, commit_hash_after = _maybe_auto_commit(
        auto_commit, commit_missing, committed,
        dispatch_id=dispatch_id, terminal_id=terminal_id, gate=gate,
        pre_dispatch_dirty=pre_dispatch_dirty,
        touched_files=sub_result.touched_files,
        manifest_paths=manifest_paths,
        commit_hash_after=commit_hash_after,
    )
    _sd._write_receipt(
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
    quality_db = _sd._default_state_dir() / "quality_intelligence.db"
    patt_updated = _sd._update_pattern_confidence(dispatch_id, "success", quality_db)
    logger.debug(
        "Feedback boost: dispatch=%s patterns_updated=%d", dispatch_id, patt_updated,
    )
    _sd._capture_dispatch_outcome(
        dispatch_id=dispatch_id,
        success=True,
        start_ts=dispatch_start_ts,
        committed=committed,
        pre_sha=pre_sha,
        manifest_paths=manifest_paths,
    )
    _sd.cleanup_worker_exit(
        terminal_id=terminal_id,
        dispatch_id=dispatch_id,
        exit_status="success",
        lease_generation=lease_generation,
        dispatch_file=_resolve_active_dispatch_file(dispatch_id),
    )


def _handle_final_failure(
    *,
    dispatch_id: str,
    terminal_id: str,
    attempt: int,
    sub_result,
    monitor,
    auto_commit: bool,
    pre_dispatch_dirty,
    manifest_paths,
    commit_hash_before: str,
    dispatch_start_ts: str,
    pre_sha: str,
    max_retries: int,
    lease_generation: "int | None",
) -> None:
    """Run the budget-exhausted failure branch: stash, receipt, decay, cleanup."""
    import subprocess_dispatch as _sd
    monitor.mark_completed()
    if auto_commit:
        _sd._auto_stash_changes(
            dispatch_id, terminal_id,
            pre_dispatch_dirty=pre_dispatch_dirty,
            dispatch_touched_files=sub_result.touched_files,
            manifest_paths=manifest_paths,
        )
    _sd._write_receipt(
        dispatch_id, terminal_id, "failed",
        event_count=sub_result.event_count,
        session_id=sub_result.session_id,
        attempt=attempt,
        failure_reason=f"Exhausted {max_retries} retries",
        commit_hash_before=commit_hash_before,
        manifest_path=sub_result.manifest_path,
        stuck_event_count=monitor.stuck_count,
    )
    quality_db = _sd._default_state_dir() / "quality_intelligence.db"
    patt_updated = _sd._update_pattern_confidence(dispatch_id, "failure", quality_db)
    logger.debug(
        "Feedback decay: dispatch=%s patterns_updated=%d", dispatch_id, patt_updated,
    )
    _sd._capture_dispatch_outcome(
        dispatch_id=dispatch_id,
        success=False,
        start_ts=dispatch_start_ts,
        committed=False,
        pre_sha=pre_sha,
        manifest_paths=manifest_paths,
    )
    _sd.cleanup_worker_exit(
        terminal_id=terminal_id,
        dispatch_id=dispatch_id,
        exit_status="failure",
        lease_generation=lease_generation,
        dispatch_file=_resolve_active_dispatch_file(dispatch_id),
    )


def _init_recovery_state(
    dispatch_id: str,
    instruction: str,
    terminal_id: str,
    model: str,
    role: "str | None",
    repo_map: "str | None",
) -> tuple[str, str, "frozenset | set | list", "list[str] | None"]:
    """Capture dispatch_start_ts, commit_hash, pre-dispatch dirty files, manifest paths.

    Also runs ``_capture_dispatch_parameters`` for pattern-confidence intelligence.
    """
    import subprocess_dispatch as _sd
    dispatch_start_ts = datetime.now(timezone.utc).isoformat()
    commit_hash_before = _sd._get_commit_hash()
    repo_cwd = Path(__file__).resolve().parents[3]
    pre_dispatch_dirty = _sd._get_dirty_files(repo_cwd)
    manifest_paths = _read_dispatch_path_manifest(dispatch_id)
    _sd._capture_dispatch_parameters(
        dispatch_id=dispatch_id,
        instruction=instruction,
        terminal_id=terminal_id,
        model=model,
        role=role,
        repo_map=repo_map,
    )
    return dispatch_start_ts, commit_hash_before, pre_dispatch_dirty, manifest_paths


def _attempt_delivery(
    *,
    terminal_id: str,
    instruction: str,
    model: str,
    dispatch_id: str,
    role: "str | None",
    monitor,
    lease_generation: "int | None",
    heartbeat_interval: float,
    chunk_timeout: float,
    total_deadline: float,
    commit_hash_before: str,
):
    """Single delivery attempt — proxies to ``deliver_via_subprocess``."""
    import subprocess_dispatch as _sd
    return _sd.deliver_via_subprocess(
        terminal_id, instruction, model, dispatch_id,
        role=role,
        lease_generation=lease_generation,
        heartbeat_interval=heartbeat_interval,
        chunk_timeout=chunk_timeout,
        total_deadline=total_deadline,
        health_monitor=monitor,
        commit_hash_before=commit_hash_before,
    )


def _backoff_or_fail(
    *,
    attempt: int,
    max_retries: int,
    sub_result,
    monitor,
    dispatch_id: str,
    terminal_id: str,
    auto_commit: bool,
    pre_dispatch_dirty,
    manifest_paths,
    commit_hash_before: str,
    dispatch_start_ts: str,
    pre_sha: str,
    lease_generation: "int | None",
) -> None:
    """Sleep with exponential backoff, or run the final-failure branch when exhausted."""
    if attempt < max_retries:
        backoff = 30 * (2 ** attempt)  # 30s, 60s, 120s
        logger.warning(
            "Delivery failed for %s, retry %d/%d in %ds",
            dispatch_id, attempt + 1, max_retries, backoff,
        )
        time.sleep(backoff)
    else:
        _handle_final_failure(
            dispatch_id=dispatch_id, terminal_id=terminal_id, attempt=attempt,
            sub_result=sub_result, monitor=monitor,
            auto_commit=auto_commit,
            pre_dispatch_dirty=pre_dispatch_dirty,
            manifest_paths=manifest_paths,
            commit_hash_before=commit_hash_before,
            dispatch_start_ts=dispatch_start_ts,
            pre_sha=pre_sha,
            max_retries=max_retries,
            lease_generation=lease_generation,
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
    """Deliver with automatic retry; success -> "done" receipt, final fail -> "failed".

    Retries use exponential backoff (30s, 60s, 120s).
    """
    import subprocess_dispatch as _sd
    chunk_timeout, total_deadline = _apply_runtime_overrides(chunk_timeout, total_deadline)

    dispatch_start_ts, commit_hash_before, pre_dispatch_dirty, manifest_paths = (
        _init_recovery_state(dispatch_id, instruction, terminal_id, model, role, repo_map)
    )
    pre_sha = commit_hash_before
    monitor = _sd.WorkerHealthMonitor(terminal_id, dispatch_id)

    for attempt in range(max_retries + 1):
        sub_result = _attempt_delivery(
            terminal_id=terminal_id, instruction=instruction, model=model,
            dispatch_id=dispatch_id, role=role, monitor=monitor,
            lease_generation=lease_generation,
            heartbeat_interval=heartbeat_interval,
            chunk_timeout=chunk_timeout, total_deadline=total_deadline,
            commit_hash_before=commit_hash_before,
        )
        if sub_result.success:
            _handle_success(
                dispatch_id=dispatch_id, terminal_id=terminal_id, attempt=attempt,
                sub_result=sub_result, monitor=monitor,
                auto_commit=auto_commit, gate=gate,
                pre_dispatch_dirty=pre_dispatch_dirty,
                manifest_paths=manifest_paths,
                commit_hash_before=commit_hash_before,
                dispatch_start_ts=dispatch_start_ts,
                pre_sha=pre_sha,
                lease_generation=lease_generation,
            )
            return True
        _backoff_or_fail(
            attempt=attempt, max_retries=max_retries,
            sub_result=sub_result, monitor=monitor,
            dispatch_id=dispatch_id, terminal_id=terminal_id,
            auto_commit=auto_commit, pre_dispatch_dirty=pre_dispatch_dirty,
            manifest_paths=manifest_paths,
            commit_hash_before=commit_hash_before,
            dispatch_start_ts=dispatch_start_ts, pre_sha=pre_sha,
            lease_generation=lease_generation,
        )

    return False
