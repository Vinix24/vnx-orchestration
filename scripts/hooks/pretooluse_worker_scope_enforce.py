#!/usr/bin/env python3
"""pretooluse_worker_scope_enforce.py — PreToolUse hook: worker-scope enforcement.

Dispatch 20260724-worker-scope-enforce-hook. Fine-grained enforcement layer on
top of the coarse ADR-012 ``--allowedTools``/``--disallowedTools`` launch-time
posture: enforces ``match_bash_deny`` and ``match_file_write_scope`` from the
worker permission SSOT (``.vnx/worker_permissions.yaml`` via
``scripts/lib/worker_permissions.py``) on every Bash / Write / Edit / MultiEdit
tool call. Reuses the existing matchers verbatim — no reimplementation of
matching logic.

OI-1196: the file_write_scope check additionally narrows to this dispatch's
own declared paths, when any were declared. ``VNX_DISPATCH_PATHS`` (a JSON
list, exported by ``TmuxInteractiveDispatch._spawn_session`` from the
dispatch's ``--dispatch-paths``) is resolved via
``worker_permissions.resolve_dispatch_write_scope`` and passed to
``match_file_write_scope`` as an additional, ANDed constraint — a dispatch
can only narrow the role's scope, never widen it.

Feasibility proven by docs/investigations/spike-worker-scope-hook-feasibility.md
(E1-E4): PreToolUse hooks fire under --dangerously-skip-permissions, worktree-
local settings are honored via cwd-based discovery, and config live-reloads.

Claude Code hook contract (2.1+):
  stdin  : JSON {tool_name, tool_input, session_id, cwd, transcript_path}
  stdout : {"decision":"block","reason":"..."} to block, empty to allow
  exit   : 0 always — decision is communicated via JSON output, never exit code

Gate: VNX_ENFORCE_WORKER_PERMISSIONS (see worker_permissions.
worker_permission_enforcement_enabled(); default ON since 15-08). An explicit
opt-out (VNX_WORKER_ENFORCEMENT_SKIP=1 or a falsy
VNX_ENFORCE_WORKER_PERMISSIONS) → this hook is a pure no-op: every branch below
returns ("allow", None) before any matcher runs. This mirrors the flag that
already gates the coarse launch-time posture — this hook is the fine-grained
(per-command, per-path glob) layer on top of it.

Role resolution: VNX_WORKER_ROLE env var, exported into the worker's tmux pane
by TmuxInteractiveDispatch._spawn_session() (E3 gap, closed in the same
dispatch). When unset, resolve_worker_profile(None) falls back to the
role-agnostic default_code_worker_profile(), which carries no bash_deny_patterns
or file_write_scope — i.e. the hook never invents restrictions the SSOT does
not declare for the resolved profile.

Audit: every block decision appends one ``worker_scope_block`` event to
$VNX_DATA_DIR/events/worker_scope_block.ndjson via atomic_io.audit_event_append
(same audit channel as the rest of the fabric).

Fail-open by construction: any missing dependency, malformed payload, or
unexpected exception results in an "allow" decision. This hook must never be
the reason a legitimate tool call is refused.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("pretooluse_worker_scope_enforce")
if not logger.handlers:
    # Hook contract: stdout carries only the decision JSON — logs go to stderr.
    _stderr_handler = logging.StreamHandler(sys.stderr)
    _stderr_handler.setFormatter(
        logging.Formatter("%(name)s: %(levelname)s: %(message)s")
    )
    logger.addHandler(_stderr_handler)
    logger.setLevel(logging.WARNING)

_SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

try:
    from worker_permissions import (  # noqa: E402
        match_bash_deny,
        match_file_write_scope,
        resolve_dispatch_write_scope,
        resolve_worker_profile,
        worker_permission_enforcement_enabled,
    )
    import project_root  # noqa: E402
    from atomic_io import audit_event_append  # noqa: E402

    _DEPS_AVAILABLE = True
except Exception:  # noqa: BLE001 - hook must never crash the tool call on import
    _DEPS_AVAILABLE = False

_WRITE_LIKE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})


def _relative_to_cwd(file_path: str, cwd: str) -> str:
    """Best-effort: turn an absolute file_path into a path relative to cwd.

    file_write_scope globs (e.g. "scripts/**") are project-relative. The hook
    payload's cwd is the worker's working directory (the per-dispatch worktree
    root for tmux/subprocess-spawned workers). A path that resolves outside
    cwd (relpath starting with "..") is left absolute — match_file_write_scope
    will then correctly fail to match any project-relative scope glob.
    """
    if not file_path or not os.path.isabs(file_path) or not cwd:
        return file_path
    try:
        rel = os.path.relpath(file_path, cwd)
    except ValueError:
        return file_path
    return file_path if rel.startswith("..") else rel


def _is_within_report_dir(file_path: str) -> bool:
    """Return True when *file_path* targets the unified report directory.

    Workers must always be able to write their completion report to
    $VNX_DATA_DIR/unified_reports/<dispatch_id>.md regardless of
    file_write_scope. This exemption is scoped narrowly: only the
    unified_reports/ directory under the resolved data dir, not
    any other location under VNX_DATA_DIR.
    """
    if not file_path or not _DEPS_AVAILABLE:
        return False
    try:
        data_dir = project_root.resolve_data_dir(__file__)
        reports_dir = (data_dir / "unified_reports").resolve()
        target = Path(file_path).resolve()
        # Must be a direct child of reports_dir (not a subdirectory or sibling).
        try:
            target.relative_to(reports_dir)
        except ValueError:
            return False
        return True
    except Exception:
        return False


def _resolve_dispatch_write_scope_from_env() -> "list[str] | None":
    """Read ``VNX_DISPATCH_PATHS`` (JSON list, exported by
    ``TmuxInteractiveDispatch._spawn_session``) and resolve it to a
    write-scope narrowing via :func:`resolve_dispatch_write_scope` (OI-1196).

    Fails open to ``None`` (no dispatch-level narrowing) on any parse
    failure or missing var. This is safe, not a silent hole: ``None`` only
    removes the EXTRA per-dispatch narrowing — the role's file_write_scope
    check in :func:`match_file_write_scope` still applies unconditionally,
    so a malformed/missing env var can never widen a worker past its role
    scope. It can only fail to apply a tightening the dispatch asked for.
    """
    if not _DEPS_AVAILABLE:
        return None
    raw = os.environ.get("VNX_DISPATCH_PATHS")
    if not raw:
        return None
    try:
        paths = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return None
    return resolve_dispatch_write_scope(paths)


def evaluate(payload: dict) -> "tuple[str, str | None]":
    """Return (decision, reason) for one PreToolUse payload. decision is 'allow' or 'block'."""
    if not _DEPS_AVAILABLE:
        return "allow", None
    if not worker_permission_enforcement_enabled():
        return "allow", None

    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return "allow", None

    role = os.environ.get("VNX_WORKER_ROLE") or None
    profile = resolve_worker_profile(role)

    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command:
            return "allow", None
        pattern = match_bash_deny(command, profile)
        if pattern:
            return (
                "block",
                f"worker-scope: Bash command matches bash_deny_patterns entry "
                f"'{pattern}' for role '{profile.role}'",
            )
        return "allow", None

    if tool_name in _WRITE_LIKE_TOOLS:
        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return "allow", None
        # Report obligation: a worker must always be able to write its
        # completion report to $VNX_DATA_DIR/unified_reports/ regardless
        # of file_write_scope. This exemption is scoped narrowly to the
        # unified_reports directory only.
        if _is_within_report_dir(file_path):
            return "allow", None
        cwd = payload.get("cwd")
        cwd = cwd if isinstance(cwd, str) else ""
        rel_path = _relative_to_cwd(file_path, cwd)
        dispatch_write_scope = _resolve_dispatch_write_scope_from_env()
        if not match_file_write_scope(rel_path, profile, dispatch_write_scope):
            scope_desc = (
                "file_write_scope narrowed by this dispatch's declared paths"
                if dispatch_write_scope is not None
                else "file_write_scope"
            )
            return (
                "block",
                f"worker-scope: write target '{rel_path}' is outside {scope_desc} "
                f"for role '{profile.role}'",
            )
        return "allow", None

    return "allow", None


def _emit_audit(tool_name: object, decision: str, reason: "str | None") -> None:
    """Append one audit event for a block decision. Fail-open — never raises."""
    if not _DEPS_AVAILABLE:
        return
    try:
        data_dir = project_root.resolve_data_dir(__file__)
        events_dir = data_dir / "events"
        audit_event_append(
            events_dir,
            "worker_scope_block",
            {
                "tool_name": tool_name,
                "decision": decision,
                "reason": reason,
                "role": os.environ.get("VNX_WORKER_ROLE") or "(unset)",
                "dispatch_id": os.environ.get("VNX_CURRENT_DISPATCH_ID")
                or os.environ.get("VNX_DISPATCH_ID")
                or "(unset)",
            },
        )
    except Exception:  # audit trail must never block or crash the hook
        logger.warning(
            "worker-scope audit append failed for tool %r (decision=%s); "
            "the %s audit event was NOT written",
            tool_name,
            decision,
            "worker_scope_block",
            exc_info=True,
        )


def main() -> None:
    tool_name_ctx: object = None
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        tool_name_ctx = payload.get("tool_name")

        decision, reason = evaluate(payload)

        if decision == "block":
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
            _emit_audit(tool_name_ctx, decision, reason)
    except Exception:  # absolute fail-open, never crash the hook
        logger.exception(
            "worker-scope hook failed unexpectedly for tool %r; failing open "
            "with an implicit allow decision",
            tool_name_ctx,
        )
        return


if __name__ == "__main__":
    main()
