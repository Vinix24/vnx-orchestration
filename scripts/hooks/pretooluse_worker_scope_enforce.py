#!/usr/bin/env python3
"""pretooluse_worker_scope_enforce.py — PoC PreToolUse hook: worker-scope enforcement.

Feasibility spike for OI-788 (dispatch 20260724-worker-scope-feasibility-spike).
Proves that ``match_bash_deny`` / ``match_file_write_scope`` (scripts/lib/worker_permissions.py)
can be wired to an actual PreToolUse hook that blocks a tool call, instead of
remaining prose-only preamble text. Reuses the existing matchers verbatim —
no reimplementation of matching logic.

Claude Code hook contract (2.1+):
  stdin  : JSON {tool_name, tool_input, session_id, cwd, transcript_path}
  stdout : {"decision":"block","reason":"..."} to block, empty to allow
  exit   : 0 always — decision is communicated via JSON output, never exit code

Gate: VNX_ENFORCE_WORKER_PERMISSIONS (see worker_permissions.
worker_permission_enforcement_enabled(); default OFF). Unset/falsy → this hook
is a pure no-op: every branch below returns ("allow", None) before any
matcher runs. This mirrors the flag that already gates the coarse
--allowedTools/--disallowedTools launch-time posture (ADR-012) — this hook is
the fine-grained (per-command, per-path glob) enforcement layer on top of it.

Role resolution: VNX_WORKER_ROLE env var. KNOWN GAP (see
docs/investigations/spike-worker-scope-hook-feasibility.md, section E3): no
spawner currently exports this into the worker's tmux pane, so in production
today this always falls back to worker_permissions.default_code_worker_profile()
via resolve_worker_profile(None). The fallback is intentionally still a real,
functional profile (not an open-allow) so the hook degrades to a role-agnostic
baseline instead of doing nothing.

Fail-open by construction: any missing dependency, malformed payload, or
unexpected exception results in an "allow" decision. This hook must never be
the reason a legitimate tool call is refused.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

try:
    from worker_permissions import (  # noqa: E402
        match_bash_deny,
        match_file_write_scope,
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


def evaluate(payload: dict) -> tuple[str, str | None]:
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
        cwd = payload.get("cwd")
        cwd = cwd if isinstance(cwd, str) else ""
        rel_path = _relative_to_cwd(file_path, cwd)
        if not match_file_write_scope(rel_path, profile):
            return (
                "block",
                f"worker-scope: write target '{rel_path}' is outside file_write_scope "
                f"for role '{profile.role}'",
            )
        return "allow", None

    return "allow", None


def _emit_audit(tool_name: object, decision: str, reason: str | None) -> None:
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
    except Exception:  # noqa: BLE001 - audit trail must never block or crash the hook
        pass


def main() -> None:
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(payload, dict):
            return

        decision, reason = evaluate(payload)

        if decision == "block":
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
            _emit_audit(payload.get("tool_name"), decision, reason)
    except Exception:  # noqa: BLE001 - absolute fail-open, never crash the hook
        return


if __name__ == "__main__":
    main()
