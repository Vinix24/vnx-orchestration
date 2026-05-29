#!/usr/bin/env python3
"""subprocess_dispatch.py — Facade for SubprocessAdapter-based dispatch delivery.

This module is intentionally thin. The implementation lives under
``subprocess_dispatch_internals/`` and is re-exported here so external
callers (dispatch_deliver.sh, headless_dispatch_daemon, claude_adapter,
the ``test_subprocess_*`` test suite, etc.) can keep importing from
``subprocess_dispatch`` unchanged.

BILLING SAFETY: only ``subprocess.Popen(["claude", ...])`` is invoked
downstream — no Anthropic SDK is imported anywhere in this package.

Success-path call order (preserved by deliver_with_recovery):
    _write_receipt(...) -> _update_pattern_confidence(...) -> _capture_dispatch_outcome(...)

Per-dispatch event archival is performed inside delivery's finally block via
``event_store.clear(terminal_id, archive_dispatch_id=dispatch_id)`` (NOT
``event_store.archive(...)`` alone) so the live NDJSON ring-buffer is
truncated before the next dispatch begins writing.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

# OI-1107: Extract Role: header from instruction text when --role is not passed.
_ROLE_HEADER_RE = re.compile(r"^Role:\s*(\S+)", re.MULTILINE)
_ROLE_FALLBACK = "backend-developer"


def _extract_role_from_instruction(instruction: str) -> str | None:
    """Return the role from a 'Role: <name>' header in the instruction, or None."""
    m = _ROLE_HEADER_RE.search(instruction)
    return m.group(1) if m else None

sys.path.insert(0, str(Path(__file__).parent))

from subprocess_adapter import SubprocessAdapter
from headless_context_tracker import HeadlessContextTracker
from worker_health_monitor import WorkerHealthMonitor, HealthStatus, SLOW_THRESHOLD
from cleanup_worker_exit import cleanup_worker_exit

from subprocess_dispatch_internals.delivery import deliver_via_subprocess
from subprocess_dispatch_internals.delivery_runtime import (
    _SubprocessResult,
    _heartbeat_loop,
)
from subprocess_dispatch_internals.git_helpers import (
    _check_commit_since,
    _commit_belongs_to_dispatch,
    _count_lines_changed_since_sha,
    _get_active_worktree,
    _get_commit_hash,
    _get_current_branch,
    _set_active_worktree,
)
from subprocess_dispatch_internals.handover import (
    _build_continuation_prompt,
    _detect_pending_handover,
    _write_rotation_handover,
)
from subprocess_dispatch_internals.manifest import (
    _promote_manifest,
    _write_manifest,
)
from subprocess_dispatch_internals.path_utils import (
    _extract_touched_paths_from_event,
    _get_dirty_files,
    _normalize_repo_path,
    _parse_dirty_files,
)
from subprocess_dispatch_internals.pattern_confidence import (
    _capture_dispatch_outcome,
    _capture_dispatch_parameters,
    _update_pattern_confidence,
)
from subprocess_dispatch_internals.receipt_writer import (
    _auto_commit_changes,
    _auto_stash_changes,
    _ensure_unified_report,
    _write_receipt,
)
from governance_emit import emit_dispatch_receipt, emit_unified_report  # noqa: F401 — re-exported for callers
from subprocess_dispatch_internals.recovery import deliver_with_recovery
from subprocess_dispatch_internals.skill_injection import (
    _build_intelligence_section,
    _inject_permission_profile,
    _inject_skill_context,
    _load_agent_profile,
    _resolve_agent_cwd,
)
from subprocess_dispatch_internals.state_paths import (
    _default_state_dir,
    _dispatch_manifest_dir,
    _resolve_active_dispatch_file,
    _safe_remove_active_dir,
)

__all__ = [
    "_extract_role_from_instruction",
    "_select_dispatch_path",
    "_build_cheap_lane_argv",
    "_execute_cheap_lane_dispatch",
    "SubprocessAdapter",
    "HeadlessContextTracker",
    "WorkerHealthMonitor",
    "HealthStatus",
    "SLOW_THRESHOLD",
    "cleanup_worker_exit",
    "deliver_via_subprocess",
    "deliver_with_recovery",
    "_SubprocessResult",
    "_heartbeat_loop",
    "_build_intelligence_section",
    "_inject_skill_context",
    "_inject_permission_profile",
    "_resolve_agent_cwd",
    "_load_agent_profile",
    "_write_manifest",
    "_promote_manifest",
    "_dispatch_manifest_dir",
    "_safe_remove_active_dir",
    "_default_state_dir",
    "_resolve_active_dispatch_file",
    "_get_commit_hash",
    "_get_current_branch",
    "_check_commit_since",
    "_commit_belongs_to_dispatch",
    "_count_lines_changed_since_sha",
    "_get_dirty_files",
    "_normalize_repo_path",
    "_parse_dirty_files",
    "_extract_touched_paths_from_event",
    "subprocess",
    "_detect_pending_handover",
    "_build_continuation_prompt",
    "_write_rotation_handover",
    "_write_receipt",
    "_ensure_unified_report",
    "_auto_commit_changes",
    "_auto_stash_changes",
    "emit_dispatch_receipt",
    "emit_unified_report",
    "_capture_dispatch_parameters",
    "_capture_dispatch_outcome",
    "_update_pattern_confidence",
    "_get_active_worktree",
    "_set_active_worktree",
]


def _select_dispatch_path(
    task_class: str,
    complexity: str = "medium",
    current_model: str = "sonnet",
    env: "dict[str, str] | None" = None,
    auto_route_applied: bool = False,
) -> "tuple[str | None, str]":
    """Resolve the dispatch lane and effective Claude model from the routing policy.

    Returns ``(cheap_lane_provider, effective_model)`` where:

    - ``cheap_lane_provider`` is the non-Claude lane string (e.g.
      ``"litellm:moonshot:kimi-k2-0905-default"``) when the routing policy
      selects a non-Claude provider, or ``None`` when the dispatch stays on
      Claude.
    - ``effective_model`` is the Claude model to use (``"sonnet"``,
      ``"haiku"``, ``"opus"``), unchanged from ``current_model`` whenever
      a non-Claude lane is selected.

    Short-circuit conditions that return ``(None, current_model)`` unchanged:
    - ``VNX_ROUTING_POLICY_ENABLED`` is absent or not ``"1"`` in *env*.
    - ``task_class`` is empty.
    - ``auto_route_applied`` is ``True`` (smart_router already ran; do not
      override its decision with the coarser routing_policy).
    - Any exception raised by the routing policy (file-not-found, bad YAML,
      unexpected errors) — fail open so dispatch continues on the current
      Claude model.

    Critical regression fix: the old ``__main__`` block fell back to the first
    Claude model in ``fallback_chain`` when the lane was non-Claude.  This
    silently routed the dispatch through Claude instead of the intended provider.
    This function never touches ``fallback_chain`` for non-Claude lanes.
    """
    _env = env if env is not None else dict(os.environ)

    # Guard: feature flag, empty task_class, or smart_router already decided.
    if _env.get("VNX_ROUTING_POLICY_ENABLED") != "1":
        return None, current_model
    if not task_class:
        return None, current_model
    if auto_route_applied:
        return None, current_model

    try:
        from routing_policy import decide_lane, lane_to_claude_model  # noqa: PLC0415
        decision = decide_lane(task_class=task_class, complexity=complexity, env=_env)
        claude_model = lane_to_claude_model(decision.lane)
        if claude_model is not None:
            # Claude lane: override model, no cheap-lane provider.
            return None, claude_model
        # Non-Claude lane: return lane as-is; do NOT fall through to fallback_chain.
        return decision.lane, current_model
    except Exception as exc:  # noqa: BLE001
        import logging as _log_mod  # noqa: PLC0415
        _log_mod.getLogger(__name__).warning(
            "routing_policy: decision failed (%s); falling back to --model=%s",
            exc, current_model,
        )
        return None, current_model


def _build_cheap_lane_argv(
    args: "argparse.Namespace",
    cheap_lane_provider: str,
) -> "list[str]":
    """Build the provider_dispatch argv list for a non-Claude lane dispatch.

    Constructs the full argument list forwarded to ``provider_dispatch.main()``
    when ``routing_policy`` selects a non-Claude (cheap) provider.  Extracted
    as a module-level function so tests can verify the delegation contract
    without spawning real processes.

    Args:
        args:               Parsed ``argparse.Namespace`` from ``__main__``.
        cheap_lane_provider: The lane string returned by ``_select_dispatch_path``
                            (e.g. ``"litellm:moonshot:kimi-k2-0905-default"``).

    Returns:
        List of string arguments suitable for ``provider_dispatch.main(argv)``.
    """
    argv: "list[str]" = [
        "--provider", cheap_lane_provider,
        "--terminal-id", args.terminal_id,
        "--dispatch-id", args.dispatch_id,
        "--instruction", args.instruction,
        "--model", args.model,
        "--role", args.role or _ROLE_FALLBACK,
        "--max-retries", str(args.max_retries),
        "--gate", args.gate,
    ]
    if getattr(args, "no_auto_commit", False):
        argv.append("--no-auto-commit")
    if getattr(args, "dispatch_paths", ""):
        argv.extend(["--dispatch-paths", args.dispatch_paths])
    if getattr(args, "pr_id", None):
        argv.extend(["--pr-id", args.pr_id])
    return argv


def _execute_cheap_lane_dispatch(
    args: "argparse.Namespace",
    cheap_lane_provider: str,
) -> int:
    """Delegate to provider_dispatch when routing policy selects a non-Claude lane.

    This is the single delegation entry-point called from ``__main__`` when
    ``_select_dispatch_path`` returns a non-None ``cheap_lane_provider``.
    ``provider_dispatch`` owns receipt and unified_report emission after the
    spawn completes; ``deliver_with_recovery`` (Claude) is never invoked on
    this path.

    Extracting it as a module-level function makes the delegation contract
    directly testable: callers can mock ``provider_dispatch.main`` and assert
    that ``deliver_with_recovery`` is not called (which is the primary
    regression guard for the cheap-lane feature).

    Args:
        args:               Parsed ``argparse.Namespace`` from ``__main__``.
        cheap_lane_provider: The non-Claude lane string (e.g.
                            ``"litellm:moonshot:kimi-k2-0905-default"``).

    Returns:
        Exit code from ``provider_dispatch.main()``.
    """
    import provider_dispatch as _pd  # noqa: PLC0415
    return _pd.main(_build_cheap_lane_argv(args, cheap_lane_provider))


def _pool_heartbeat_loop(
    terminal_id: str,
    project_id: str,
    db_path: "Path",
    stop_event: "threading.Event",
    interval: float = 15.0,
) -> None:
    """Update terminal_leases.last_heartbeat_at every *interval* seconds."""
    while not stop_event.wait(timeout=interval):
        try:
            from pool_state_repo import PoolStateRepository
            repo = PoolStateRepository(db_path, project_id)
            repo.update_heartbeat_by_terminal(terminal_id, time.time())
        except Exception as exc:
            import logging as _log_mod
            _log_mod.getLogger(__name__).warning("heartbeat update failed: %s", exc)
            pass


if __name__ == "__main__":
    import argparse
    import threading

    parser = argparse.ArgumentParser(description="Deliver dispatch via SubprocessAdapter")
    parser.add_argument("--terminal-id", required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--role", default=None, help="Agent role for skill context inlining")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--no-auto-commit", action="store_true",
        help="Disable auto-commit of uncommitted changes after dispatch",
    )
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
    parser.add_argument(
        "--pr-id",
        default=None,
        help=(
            "PR identifier forwarded to IntelligenceSelector so prior_round_finding "
            "items (codex/gemini gate results) fire in production (CFX-W5-2)."
        ),
    )
    # Wave 7 PR-7.4: cost-routing policy engine (feature-flag gated).
    parser.add_argument(
        "--task-class", default="",
        help="Task class for cost-routing (e.g. refactor, code-review). Requires VNX_ROUTING_POLICY_ENABLED=1.",
    )
    parser.add_argument(
        "--complexity", default="medium", choices=["low", "medium", "high"],
        help="Dispatch complexity for cost-routing. Defaults to medium.",
    )
    parser.add_argument(
        "--auto-route", action="store_true",
        help="Use smart_router to auto-select model (opt-in, default off).",
    )
    parser.add_argument(
        "--no-adr-inject", action="store_true",
        help="Disable Wave-5 ADR context injection (debug/testing only).",
    )
    args = parser.parse_args()

    # Wave-5 ADR injection opt-out: set env var before instruction assembly (INT-2)
    if getattr(args, "no_adr_inject", False):
        os.environ["VNX_NO_ADR_INJECT"] = "1"

    # OI-1107: fall back to Role: header in instruction, then to a documented default.
    if args.role is None:
        args.role = _extract_role_from_instruction(args.instruction) or _ROLE_FALLBACK

    _dispatch_paths: "list[str] | None" = None
    if args.dispatch_paths.strip():
        _dispatch_paths = [p.strip() for p in args.dispatch_paths.split(",") if p.strip()]

    # Wave 7 PR-7.4: consult routing policy when VNX_ROUTING_POLICY_ENABLED=1.
    # Default behavior (flag unset): unchanged — Sonnet as before.
    _effective_model = args.model

    # PR-SR-4: smart_router auto-route (opt-in, takes precedence over routing_policy).
    _auto_route_applied = False
    if getattr(args, "auto_route", False):
        try:
            from smart_router import decide as _smart_route, parse_route_model_id, write_route_decision  # noqa: PLC0415

            _route_decision = _smart_route(
                instruction=args.instruction,
                role=args.role,
                dispatch_paths=_dispatch_paths,
            )
            if _route_decision.primary:
                _r_provider, _r_model = parse_route_model_id(
                    _route_decision.primary.model_id,
                )
                if _r_provider == "claude":
                    _effective_model = _r_model
                    _auto_route_applied = True

            _state_dir = Path(os.environ.get("VNX_STATE_DIR", ".vnx-data/state"))
            write_route_decision(args.dispatch_id, _route_decision, state_dir=_state_dir)
            import logging as _log_mod  # noqa: PLC0415
            _log_mod.getLogger(__name__).info(
                "smart_router: auto-route task_class=%s model=%s",
                _route_decision.task_class, _effective_model,
            )
        except Exception as _route_exc:
            import logging as _log_mod  # noqa: PLC0415
            _log_mod.getLogger(__name__).warning(
                "smart_router: auto-route failed (%s); falling back to --model=%s",
                _route_exc, args.model,
            )

    _cheap_lane_provider, _effective_model = _select_dispatch_path(
        task_class=args.task_class,
        complexity=args.complexity,
        current_model=_effective_model,
        auto_route_applied=_auto_route_applied,
    )
    if _cheap_lane_provider is not None or _effective_model != args.model:
        import logging as _log_mod
        _log_mod.getLogger(__name__).info(
            "dispatch_path: task_class=%s complexity=%s -> cheap_lane=%s effective_model=%s",
            args.task_class, args.complexity, _cheap_lane_provider, _effective_model,
        )

    _state_dir = _default_state_dir()
    _db_path = _state_dir / "runtime_coordination.db"
    _project_id = os.environ.get("VNX_PROJECT_ID", "vnx-dev")

    try:
        from pool_state_repo import PoolStateRepository
        _pid_repo = PoolStateRepository(_db_path, _project_id)
        _pid_repo.store_worker_pid(args.terminal_id, os.getpid())
    except Exception as exc:
        import logging as _log_mod
        _log_mod.getLogger(__name__).warning("PID persistence failed: %s", exc)
        pass

    _hb_stop = threading.Event()
    _hb_thread = threading.Thread(
        target=_pool_heartbeat_loop,
        args=(args.terminal_id, _project_id, _db_path, _hb_stop),
        daemon=True,
    )
    _hb_thread.start()

    if _cheap_lane_provider is not None:
        # Non-Claude lane chosen by routing_policy: stop the heartbeat thread, then
        # delegate execution to provider_dispatch via _execute_cheap_lane_dispatch.
        # provider_dispatch owns receipt + unified_report emission; deliver_with_recovery
        # (which would spawn a Claude process) is never called on this path.
        _hb_stop.set()
        _hb_thread.join(timeout=5)
        sys.exit(_execute_cheap_lane_dispatch(args, _cheap_lane_provider))

    # Scale chunk/total timeouts by --complexity so compute-heavy ("high")
    # dispatches get more headroom and aren't killed by the per-chunk timeout
    # during a long quiet-but-working step (e.g. static analysis). These are the
    # *base* values fed into deliver_with_recovery; apply_runtime_overrides runs
    # downstream so VNX_CHUNK_TIMEOUT / VNX_TOTAL_DEADLINE still take precedence.
    from subprocess_dispatch_internals.runtime_overrides import complexity_timeout_defaults
    _chunk_timeout, _total_deadline = complexity_timeout_defaults(args.complexity)

    # VNX_ISOLATED_WORKTREE=1: create a per-dispatch ephemeral worktree so
    # concurrent workers operate on independent file trees and never share HEAD.
    # Default (unset): no change — proven path runs exactly as before.
    _isolated = os.environ.get("VNX_ISOLATED_WORKTREE") == "1"
    _isolation_wt_path = None
    _isolation_project_root = Path(__file__).resolve().parents[2]
    if _isolated:
        try:
            import logging as _log_mod
            _log_mod.getLogger(__name__).info(
                "VNX_ISOLATED_WORKTREE=1: creating dispatch worktree for %s",
                args.dispatch_id,
            )
            from dispatch_worktree_isolation import (
                create_dispatch_worktree as _create_wt,
                remove_dispatch_worktree as _remove_wt,
            )
            _isolation_wt_path = _create_wt(
                args.dispatch_id,
                project_root=_isolation_project_root,
            )
            _set_active_worktree(_isolation_wt_path)
        except Exception as _wt_exc:
            import logging as _log_mod
            _log_mod.getLogger(__name__).error(
                "VNX_ISOLATED_WORKTREE: worktree creation failed (%s); "
                "falling back to shared repo root",
                _wt_exc,
            )
            _isolated = False
            _isolation_wt_path = None

    try:
        ok = deliver_with_recovery(
            terminal_id=args.terminal_id,
            instruction=args.instruction,
            model=_effective_model,
            dispatch_id=args.dispatch_id,
            role=args.role,
            max_retries=args.max_retries,
            chunk_timeout=_chunk_timeout,
            total_deadline=_total_deadline,
            auto_commit=not args.no_auto_commit,
            gate=args.gate,
            dispatch_paths=_dispatch_paths,
            pr_id=args.pr_id,
        )
    finally:
        _hb_stop.set()
        _hb_thread.join(timeout=5)
        if _isolated and _isolation_wt_path is not None:
            _set_active_worktree(None)
            try:
                _remove_wt(args.dispatch_id, project_root=_isolation_project_root)
            except Exception as _rm_exc:
                import logging as _log_mod
                _log_mod.getLogger(__name__).warning(
                    "VNX_ISOLATED_WORKTREE: worktree cleanup failed: %s", _rm_exc,
                )

    sys.exit(0 if ok else 1)
