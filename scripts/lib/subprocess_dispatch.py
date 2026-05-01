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
    "_capture_dispatch_parameters",
    "_capture_dispatch_outcome",
    "_update_pattern_confidence",
]


if __name__ == "__main__":
    import argparse

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
    args = parser.parse_args()

    # OI-1107: fall back to Role: header in instruction, then to a documented default.
    if args.role is None:
        args.role = _extract_role_from_instruction(args.instruction) or _ROLE_FALLBACK

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
