#!/usr/bin/env python3
"""PreToolUse hook: enforce a worker's role-scoped permission profile.

Purpose: `worker_permissions.match_bash_deny` / `match_file_write_scope`
(scripts/lib/worker_permissions.py) have existed as prose-only guidance —
zero blocking callers. This hook is the first blocking caller: it runs
in-process inside a spawned worker (tmux-lane, per
docs/investigations/spike-worker-scope-hook-feasibility.md) and denies a
Bash command or Write/Edit/MultiEdit path that falls outside the worker's
role profile.

Claude Code hook contract (2.1+):
  stdin  : JSON {tool_name, tool_input, session_id, cwd, transcript_path}
  stdout : {"decision":"block","reason":"..."} to block, empty to allow
  exit   : 0 always — decision is communicated via JSON output, never exit code

Registered (D3) as a worktree-local `.claude/settings.local.json` PreToolUse
hook on matcher "Bash|Write|Edit|MultiEdit", written by
tmux_interactive_dispatch._materialize_worker_scope_hook — only when
VNX_ENFORCE_WORKER_PERMISSIONS is truthy. The shared T0 `.claude/settings.json`
is never touched (OI-188).

Fast-path (nul-cost on T0 / any non-worker session): VNX_WORKER_ROLE unset ->
return immediately, before importing worker_permissions or touching YAML.
T0 and any session the spawner hasn't wired a role into never pays this
hook's cost beyond one os.environ.get().

Flag-gate: VNX_ENFORCE_WORKER_PERMISSIONS not truthy -> allow, no-op. Uses
worker_permissions.worker_permission_enforcement_enabled() (the existing
ADR-012 flag) so this hook's posture always matches the rest of the
enforcement feature — default OFF changes nothing.

Fail-CLOSED once a role signal is present and the flag is on: an unknown/
missing role in worker_permissions.yaml blocks (worker_permissions.role_known()
is an explicit membership check — resolve_worker_profile()/load_permissions()
both return a permissive fallback profile for an unrecognized role, which
would silently allow-all if used as the membership test). Any other internal
error in this path (malformed stdin, YAML load failure, path resolution
error) also blocks — this hook is an enforcement gate, not an opportunistic
detector; an error here must not open the gate.

Known limitation (accepted by design, documented per dispatch spec — NOT
claimed to be airtight): both match_bash_deny and match_file_write_scope are
fnmatch/glob string matches against the raw command / a normalized path.
`cd x && rm -rf y`, `bash -c "..."`, `$(...)`, and backtick command
substitution can all evade detection here. This is a first-order guard
layer; the real isolation boundary remains the per-dispatch git worktree.
See docs/investigations/spike-worker-scope-hook-feasibility.md.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCOPED_TOOLS = frozenset({"Bash", "Write", "Edit", "MultiEdit"})


def _emit_block(reason: str) -> None:
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")


def _audit_event(*, tool_name: str, role: str, reason: str, rule: str, extra: dict | None = None) -> None:
    """Append one JSON line to <data_dir>/events/worker_scope_denied.ndjson.

    Best-effort: any failure here must never crash the hook or change its
    allow/block decision — the decision has already been written to stdout
    by the time this runs.
    """
    try:
        import project_root  # noqa: PLC0415 — sys.path adjusted by caller

        data_dir = project_root.resolve_data_dir(__file__)
        events_dir = data_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        try:
            project_id = project_root.resolve_project_id()
        except RuntimeError:
            project_id = ""
        entry = {
            "event_type": "worker_scope_denied",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "reason": reason,
            "rule": rule,
            "VNX_WORKER_ROLE": role,
            "VNX_CURRENT_DISPATCH_ID": os.environ.get("VNX_CURRENT_DISPATCH_ID", ""),
            "project_id": project_id,
        }
        if extra:
            entry.update(extra)
        with (events_dir / "worker_scope_denied.ndjson").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass  # best-effort: audit sink failure must never affect the decision


def _resolve_repo_root(payload: dict) -> Path:
    """Base directory for normalizing a tool's file_path against file_write_scope globs.

    Prefers the hook payload's own ``cwd`` field (the worktree root the spawner
    launched the pane in, per the spike's E2 finding) over Path.cwd() so
    normalization is correct even if this script is ever invoked from a
    different working directory than the hook payload describes.
    """
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        try:
            return Path(cwd).resolve()
        except OSError:
            pass
    return Path.cwd().resolve()


def _normalize_repo_path(raw_path: str, repo_root: Path) -> str:
    """Normalize a tool's file_path to a repo-root-relative POSIX path.

    Handles a worktree-absolute path (the common case — Claude Code tool
    calls carry absolute file_path values), resolves ``..`` segments and
    symlinks, and returns the path relative to repo_root so it can be
    fnmatch-compared against file_write_scope globs like "scripts/**". A
    path that resolves outside repo_root is returned as its absolute form,
    which correctly fails to match any repo-relative glob (deny-by-default
    for anything outside the worktree).
    """
    p = Path(raw_path)
    if not p.is_absolute():
        p = repo_root / p
    try:
        resolved = p.resolve()
    except OSError:
        resolved = p
    try:
        rel = resolved.relative_to(repo_root)
        return rel.as_posix()
    except ValueError:
        return resolved.as_posix()


def _extract_target_paths(tool_name: str, tool_input: dict) -> list[str]:
    """Every file_path this tool call would write to.

    Write/Edit carry a single top-level file_path. MultiEdit's standard
    schema also carries a single top-level file_path (the `edits` array
    holds old_string/new_string pairs, not per-edit paths) — but each edit
    entry is defensively checked too in case a per-edit file_path is ever
    present, so "check elk MultiEdit-pad" holds under either schema shape.
    """
    paths: list[str] = []
    top = tool_input.get("file_path")
    if isinstance(top, str) and top:
        paths.append(top)
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, dict):
                    p = edit.get("file_path")
                    if isinstance(p, str) and p:
                        paths.append(p)
    return paths


def main() -> None:
    role = os.environ.get("VNX_WORKER_ROLE", "").strip()
    if not role:
        # Fast-path: no worker-role signal (T0, an attached human session, or a
        # lane that hasn't wired VNX_WORKER_ROLE yet) -> allow. No import of
        # worker_permissions or any YAML load happens on this path.
        return

    try:
        lib_dir = Path(__file__).resolve().parent.parent / "lib"
        if str(lib_dir) not in sys.path:
            sys.path.insert(0, str(lib_dir))
        import worker_permissions as wp  # noqa: PLC0415

        if not wp.worker_permission_enforcement_enabled():
            return  # flag-gate OFF: allow, no-op — default posture unchanged

        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}

        tool_name = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}

        if tool_name not in _SCOPED_TOOLS:
            return  # out of scope for this hook -> allow

        if not wp.role_known(role):
            reason = (
                f"worker-scope-enforce: unknown role '{role}' has no profile in "
                "worker_permissions.yaml — fail-closed block"
            )
            _emit_block(reason)
            _audit_event(tool_name=tool_name, role=role, reason=reason, rule="unknown_role")
            return

        profile = wp.resolve_worker_profile(role)

        if tool_name == "Bash":
            command = tool_input.get("command")
            if not isinstance(command, str):
                return
            deny_pattern = wp.match_bash_deny(command, profile)
            if deny_pattern:
                reason = (
                    f"worker-scope-enforce: command matches deny pattern '{deny_pattern}' "
                    f"for role '{role}'"
                )
                _emit_block(reason)
                _audit_event(
                    tool_name=tool_name,
                    role=role,
                    reason=reason,
                    rule="bash_deny",
                    extra={"pattern": deny_pattern},
                )
            return

        # Write / Edit / MultiEdit -> file_write_scope check, per-path.
        repo_root = _resolve_repo_root(payload)
        for raw_path in _extract_target_paths(tool_name, tool_input):
            normalized = _normalize_repo_path(raw_path, repo_root)
            if not wp.match_file_write_scope(normalized, profile):
                reason = (
                    f"worker-scope-enforce: '{normalized}' is outside file_write_scope "
                    f"for role '{role}'"
                )
                _emit_block(reason)
                _audit_event(
                    tool_name=tool_name,
                    role=role,
                    reason=reason,
                    rule="file_write_scope",
                    extra={"path": normalized},
                )
                return
        return

    except Exception as exc:  # noqa: BLE001
        # Fail-CLOSED: once a role signal is present, any internal error blocks
        # rather than silently allowing. Contrast pretooluse_spawn_detector.py's
        # absolute fail-open — that hook opportunistically detects; this one is
        # an enforcement gate, so an error must not open it.
        reason = f"worker-scope-enforce: internal error, fail-closed: {exc}"
        _emit_block(reason)
        try:
            _audit_event(tool_name="", role=role, reason=reason, rule="hook_error")
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
