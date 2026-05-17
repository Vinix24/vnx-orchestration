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
    _get_commit_hash,
    _get_current_branch,
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
]


def _pool_heartbeat_loop(
    terminal_id: str,
    project_id: str,
    db_path: "Path",
    stop_event: "threading.Event",
    interval: float = 15.0,
) -> None:
    """Update terminal_leases.last_heartbeat_at every *interval* seconds."""
    import threading as _thr
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
    args = parser.parse_args()

    # OI-1107: fall back to Role: header in instruction, then to a documented default.
    if args.role is None:
        args.role = _extract_role_from_instruction(args.instruction) or _ROLE_FALLBACK

    _dispatch_paths: "list[str] | None" = None
    if args.dispatch_paths.strip():
        _dispatch_paths = [p.strip() for p in args.dispatch_paths.split(",") if p.strip()]

    # Wave 7 PR-7.4: consult routing policy when VNX_ROUTING_POLICY_ENABLED=1.
    # Default behavior (flag unset): unchanged — Sonnet as before.
    _effective_model = args.model
    if os.environ.get("VNX_ROUTING_POLICY_ENABLED") == "1" and args.task_class:
        try:
            from routing_policy import decide_lane, lane_to_claude_model
            _decision = decide_lane(
                task_class=args.task_class,
                complexity=args.complexity,
            )
            _claude_model = lane_to_claude_model(_decision.lane)
            if _claude_model is not None:
                # Claude lane: override model directly.
                _effective_model = _claude_model
            else:
                # LiteLLM lane: log routing intent; full LiteLLM path wired separately.
                # Fallback to first Claude lane in the fallback_chain, or keep current model.
                _fallback_claude = next(
                    (lane_to_claude_model(fb) for fb in _decision.fallback_chain
                     if lane_to_claude_model(fb) is not None),
                    None,
                )
                if _fallback_claude is not None:
                    _effective_model = _fallback_claude
            import logging as _log_mod
            _log_mod.getLogger(__name__).info(
                "routing_policy: task_class=%s complexity=%s -> lane=%s "
                "(rule=%s) effective_model=%s",
                args.task_class, args.complexity,
                _decision.lane, _decision.rule_name, _effective_model,
            )
        except Exception as _routing_exc:
            import logging as _log_mod
            _log_mod.getLogger(__name__).warning(
                "routing_policy: decision failed (%s); falling back to --model=%s",
                _routing_exc, args.model,
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

    try:
        ok = deliver_with_recovery(
            terminal_id=args.terminal_id,
            instruction=args.instruction,
            model=_effective_model,
            dispatch_id=args.dispatch_id,
            role=args.role,
            max_retries=args.max_retries,
            auto_commit=not args.no_auto_commit,
            gate=args.gate,
            dispatch_paths=_dispatch_paths,
            pr_id=args.pr_id,
        )
    finally:
        _hb_stop.set()
        _hb_thread.join(timeout=5)

    sys.exit(0 if ok else 1)
