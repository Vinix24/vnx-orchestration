#!/usr/bin/env python3
"""tmux_interactive_dispatch.py — single-shot ephemeral leaseless tmux dispatch lane.

Each dispatch() call spawns a fresh unique tmux session, drives it with an
interactive claude worker, waits for the completion receipt, and tears it down.
No reuse, no warm-open, no leases, no fixed terminal identities.

This lane runs Claude workers on the SUBSCRIPTION (the 15-June billing escape).
Interactive ``claude`` (never ``claude -p``) stays on the subscription.

BILLING SAFETY: only ``tmux`` subprocess calls spawn an interactive ``claude``
binary. No Anthropic SDK is imported anywhere in this module.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys_path_dir = str(Path(__file__).resolve().parent)

if sys_path_dir not in sys.path:
    sys.path.insert(0, sys_path_dir)

logger = logging.getLogger(__name__)

from tmux_worktree import WorktreeAllocateError, WorktreeHandle, allocate, classify, reap  # noqa: E402
from pr_enforcement import PrEnforcementResult  # noqa: E402
from worker_pane_classifier import classify_worker_pane  # noqa: E402
from vnx_paths import _resolve_vnx_home  # noqa: E402

# Work-started gate outcome codes (OI-863): a worker blocked on a permission
# prompt is ALIVE and recoverable — one relayed answer saves it — so it must not
# be fast-aborted as no_progress.  The gate returns one of these.
WORK_START_WORKING = "working"
WORK_START_AWAITING_PERMISSION = "awaiting_permission"
WORK_START_NO_PROGRESS = "no_progress"

# Capability scoping (interim, per WORKER-CAPABILITY-SCOPING-DESIGN.md §4.4/§5):
# detached ephemeral spawns run SCOPED by default (empty ambient MCP +
# acceptEdits + role allow-list) since the 14-08 flip (mcp-scoped-default). The
# old blanket --dangerously-skip-permissions default rested on the worktree
# argument — worktree isolation bounds the FILESYSTEM, not the NETWORK, and an
# MCP server talks to a service outside the checkout. Blanket skip is now the
# explicit opt-out, and it takes BOTH opt-outs because both predicates default
# ON (scoped since 14-08, ADR-012 enforcement since 15-08):
# VNX_WORKER_BLANKET_SKIP=1 (or falsy VNX_WORKER_SCOPED) AND
# VNX_WORKER_ENFORCEMENT_SKIP=1 (or falsy VNX_ENFORCE_WORKER_PERMISSIONS).
#
# OI-1099: the decision predicates worker_scoped_enabled /
# worker_permission_enforcement_enabled resolve in ONE place — the canonical
# worker_permissions module. They are NOT re-defined here as a second default
# that could silently diverge from the real predicate on an import fault. On an
# import fault the inline fallback below still fails CLOSED into the scoped
# posture (never blanket-skip), so a missing sibling import can never silently
# re-open the wider blast radius.
try:
    from worker_permissions import (  # noqa: E402
        EMPTY_MCP_CONFIG,
        worker_scoped_enabled,
        worker_permission_enforcement_enabled,
        build_claude_scope_args as _wp_build_claude_scope_args,
        resolve_worker_profile as _wp_resolve_worker_profile,
        classify_permission_posture,
    )
    _WP_AVAILABLE = True
except Exception:  # pragma: no cover - sibling import is available in-tree
    EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
    _WP_AVAILABLE = False
    # Import-fault fallback (OI-1099 + 14-08/15-08 flips): fail CLOSED into the
    # scoped posture — never silently re-open the wider blanket-skip blast radius.
    # These inline predicates return the current fleet defaults (scoped ON,
    # ADR-012 enforcement ON since 15-08) so the normal code path below still
    # assembles a scoped, enforcing spawn on an import fault; only the canonical
    # worker_permissions module owns the real, env-driven decision. "Fail-closed"
    # now means enforcement ON: an import fault must not silently drop the
    # fine-grained file-write boundary, so this stub mirrors the 15-08 flip.
    def worker_scoped_enabled():  # type: ignore[misc]
        return True

    def worker_permission_enforcement_enabled():  # type: ignore[misc]
        return True

    def classify_permission_posture(argv, role=None):  # type: ignore[misc]
        # OI-864 fallback: classify from the actual argv tokens, never by
        # re-reading env vars. Mirrors worker_permissions.classify_permission_posture.
        if "--dangerously-skip-permissions" in argv:
            return {"permission_posture": "blanket-skip"}
        if "--permission-mode" in argv or "--allowedTools" in argv:
            allow_count = 0
            if "--allowedTools" in argv:
                idx = argv.index("--allowedTools")
                if idx + 1 < len(argv):
                    allow_count = len([p for p in argv[idx + 1].split(",") if p.strip()])
            return {
                "permission_posture": "scoped-allowlist",
                "permission_profile": role or "code-worker",
                "permission_allow_pattern_count": allow_count,
            }
        return {"permission_posture": "attached-interactive"}

    def _wp_build_claude_scope_args(profile, *, permission_mode="acceptEdits", requires_mcp=False, working_tree_only=False):  # type: ignore[misc]
        args = ["--permission-mode", permission_mode]
        if not requires_mcp:
            # --mcp-config is variadic; the boolean --strict-mcp-config must
            # terminate it (same order as build_claude_scope_args).
            args += ["--mcp-config", EMPTY_MCP_CONFIG, "--strict-mcp-config"]
        # Kept in parity with worker_permissions.DEFAULT_CODE_WORKER_TOOLS (OI-104):
        # explicit Bash(<cmd>:*) build-toolchain coverage alongside bare "Bash".
        args += [
            "--allowedTools",
            "Read,Write,Edit,MultiEdit,Bash,Grep,Glob,"
            "Bash(git:*),Bash(gh:*),Bash(python3:*),Bash(pytest:*),"
            "Bash(pip:*),Bash(rm:*),Bash(chmod:*),Bash(mkdir:*)",
        ]
        disallowed = []
        if not requires_mcp:
            # Parity with worker_permissions.MCP_NAMESPACE_DENY: the empty
            # --mcp-config above does not reach extension bridges
            # (claude-in-chrome), so deny the whole mcp__ namespace explicitly.
            disallowed.append("mcp__*")
        if working_tree_only:
            disallowed += ["Bash(git commit)", "Bash(git commit:*)", "Bash(git push)", "Bash(git push:*)"]
        if disallowed:
            args += ["--disallowedTools", ",".join(disallowed)]
        return args

    def _wp_resolve_worker_profile(role):  # type: ignore[misc]
        return None

DEFAULT_COMPLETION_STATUSES = frozenset({"done", "completed", "failed", "blocked", "task_complete", "success"})

# P0-1: all four contract headings must be present for the report backstop to fire.
_REQUIRED_REPORT_HEADINGS = frozenset({
    "## Summary", "## Changes", "## Verification", "## Open Items"
})

# Receipt dedup — prefer worker-authored over lane-synthesized.
# Imported defensively; fallback returns the last receipt by list order.
try:
    from dispatch_govern import dedup_completion_receipts as _dedup_receipts  # noqa: E402
except Exception:  # pragma: no cover - sibling import is available in-tree
    def _dedup_receipts(receipts):  # type: ignore[misc]
        return receipts[-1] if receipts else None

# Shared dispatch_metadata writer — keeps the leaseless tmux lane provider-aware.
# Imported defensively; if unavailable the lane degrades to its prior behaviour.
try:
    from dispatch_metadata_db import upsert_dispatch_provider_row as _upsert_dispatch_metadata  # noqa: E402
except Exception:  # pragma: no cover - sibling import is available in-tree
    _upsert_dispatch_metadata = None  # type: ignore[misc]

# Only simple identifiers are valid model names (no whitespace or shell metacharacters).
_SAFE_MODEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Terminal → track mapping shared with headless provider lanes. The leaseless
# tmux lane borrows it so T1/T2/T3 dispatches land in tracks A/B/C.
_TERMINAL_TRACK = {"T1": "A", "T2": "B", "T3": "C"}

# The metadata stamp fires on every claude-lane dispatch (not flag-gated), so a
# contended sqlite3 default (5s) can now stall every dispatch behind a lock.
# Short-circuit fast: this is a best-effort write the sweep re-derives later.
_METADATA_STAMP_LOCK_TIMEOUT_SECONDS = 0.5

# Allowlist for extra_flags tokens: long flags (--foo, --foo=bar, --foo=bar.baz-qux),
# short flags (-f), and short flags with values (-f val treated as two tokens).
# Rejects shell metacharacters, subshell expansion, backticks, semicolons, etc.
_SAFE_FLAG_RE = re.compile(
    r"^(?:"
    r"--[a-zA-Z][a-zA-Z0-9_-]+"            # --long-flag
    r"(?:=[a-zA-Z0-9][a-zA-Z0-9._:/@-]*)?" # optional =value (simple chars only)
    r"|"
    r"-[a-zA-Z]"                             # -f
    r"|"
    r"[a-zA-Z0-9][a-zA-Z0-9._:/@-]*"       # bare value token (for -f value pairs)
    r")$"
)


def _assert_no_headless_flags(launch_cmd: str) -> None:
    """Raise ValueError if the assembled launch command contains -p/--print/--print=…

    Applied to the FINAL command regardless of how it was built (default builder,
    custom launch_builder, or model interpolation) before _launch_claude is called.
    """
    try:
        tokens = shlex.split(launch_cmd)
    except ValueError:
        tokens = launch_cmd.split()
    for token in tokens:
        if token in ("-p", "--print") or token.startswith("--print="):
            raise ValueError(
                f"headless flag {token!r} detected in assembled launch command; "
                "this lane must use interactive claude (subscription), not headless"
            )


# ---------------------------------------------------------------------------
# tmux transport — injectable so tests never spawn a real claude/tmux
# ---------------------------------------------------------------------------
@dataclass
class TmuxResult:
    """Result of a single ``tmux`` invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class TmuxCommandRunner:
    """Thin wrapper around real ``tmux`` subprocess calls."""

    def run(
        self,
        args: list[str],
        *,
        timeout: int = 10,
        input_text: "str | None" = None,
    ) -> TmuxResult:
        proc = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
        return TmuxResult(proc.returncode, proc.stdout, proc.stderr)

    def available(self) -> bool:
        return shutil.which("tmux") is not None


# ---------------------------------------------------------------------------
# Deterministic worker liveness (OI-1130 follow-up)
# ---------------------------------------------------------------------------
def _process_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` liveness probe. True unless *pid* is provably gone.

    ``ProcessLookupError`` means the pid no longer exists in the OS process
    table -> dead. ``PermissionError`` means the pid exists but is owned by a
    different user -> still alive, just not signalable by us. Any other
    ``OSError`` is unexpected and propagates so the caller's fail-open guard
    classifies the result as unknown rather than guessing dead.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class WorkerLiveness:
    """Verdict of the tmux-lane deterministic liveness probe.

    ``alive`` is TRI-STATE: ``True``/``False`` are deterministic verdicts;
    ``None`` means the probe could not establish liveness (a tmux error, an
    unparseable pane_pid, an unexpected exception) and the caller MUST fail
    open — "cannot measure" is never read as "dead" (mirrors the fail-closed
    check's own fail-open guard in ``pre_merge_gate.py``, #1468).
    """

    alive: "bool | None"
    reason: str


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class InteractiveDispatchResult:
    """Outcome of a single-shot ephemeral dispatch (spawn -> drive -> receipt -> teardown)."""

    success: bool
    dispatch_id: str
    session: "str | None" = None
    label: "str | None" = None
    window_id: "str | None" = None
    pane_id: "str | None" = None
    receipt: "dict | None" = None
    failure_reason: "str | None" = None
    duration_seconds: float = 0.0
    worktree_state: "str | None" = None
    worktree_path: "str | None" = None


# ---------------------------------------------------------------------------
# Launch command builder (overridable)
# ---------------------------------------------------------------------------
def _default_launch_command(
    model: str,
    *,
    skip_permissions: bool = False,
    extra_flags: str = "",
    role: "str | None" = None,
    requires_mcp: bool = False,
    working_tree_only: bool = False,
    session_uuid: "str | None" = None,
) -> str:
    """Build the interactive ``claude`` launch line (NOT ``claude -p``).

    Raises ValueError if *model* contains whitespace or shell metacharacters, or
    if *extra_flags* contains ``-p``, ``--print``, or ``--print=…``: those flags
    convert an interactive session to headless, defeating the subscription-safe
    guarantee of this lane.

    ``role``: when provided, selects the permission profile whose tool allow-list
    is included as ``--allowedTools`` so detached headless workers proceed without
    stalling on tool-use prompts (``acceptEdits`` alone only auto-approves file edits).

    ``requires_mcp``: when True, the ``--mcp-config {} --strict-mcp-config`` pair
    is omitted so the worker keeps its normal ambient MCP config.

    ``session_uuid``: when provided, ``--session-id <uuid>`` is injected after
    ``--model`` so the worker's transcript is deterministically joinable. The uuid
    is shlex-quoted; a None/empty value appends nothing (fail-open).
    """
    if not _SAFE_MODEL_RE.match(model):
        raise ValueError(
            f"model {model!r} must be a simple identifier (e.g. 'sonnet', "
            f"'claude-opus-4-8'); whitespace and shell metacharacters are not allowed"
        )
    if extra_flags:
        try:
            flag_tokens = shlex.split(extra_flags)
        except ValueError as exc:
            raise ValueError(
                f"extra_flags could not be parsed by shlex.split: {exc}"
            ) from exc
        safe_tokens: list[str] = []
        for token in flag_tokens:
            if token in ("-p", "--print") or token.startswith("--print="):
                raise ValueError(
                    "extra_flags must not contain -p/--print: "
                    "the interactive lane must stay on the subscription"
                )
            if not _SAFE_FLAG_RE.match(token):
                raise ValueError(
                    f"extra_flags token {token!r} contains disallowed characters; "
                    "only plain flag forms (--flag, --flag=value, -f, bare-value) are accepted"
                )
            safe_tokens.append(shlex.quote(token))
        extra_flags = " ".join(safe_tokens)
    flags = ""
    if skip_permissions:
        # Detached/autonomous run (no TTY to answer prompts). Default: the scoped
        # posture (role allow-list + empty ambient MCP) since the 14-08 flip —
        # worktree isolation bounds the filesystem, not the network, and an MCP
        # server talks to a service outside the checkout. The else branch is the
        # explicit opt-out, which requires BOTH predicates off since enforcement
        # also defaulted ON on 15-08: VNX_WORKER_BLANKET_SKIP=1 / falsy
        # VNX_WORKER_SCOPED AND VNX_WORKER_ENFORCEMENT_SKIP=1 / falsy
        # VNX_ENFORCE_WORKER_PERMISSIONS.
        if worker_scoped_enabled() or worker_permission_enforcement_enabled():
            profile = _wp_resolve_worker_profile(role)
            scope_args = _wp_build_claude_scope_args(
                profile,
                requires_mcp=requires_mcp,
                working_tree_only=working_tree_only,
            )
            flags = " " + " ".join(shlex.quote(a) for a in scope_args)
        else:
            flags = " --dangerously-skip-permissions"
    if extra_flags:
        flags = f"{flags} {extra_flags}".rstrip()
    session_arg = ""
    if session_uuid:
        session_arg = f" --session-id {shlex.quote(session_uuid)}"
    return f"source ~/.zshrc 2>/dev/null; claude --model {model}{session_arg}{flags}"


def _sanitize_session_name(raw: str) -> str:
    """tmux session names may not contain '.' or ':'. Map them to '-'."""
    return "".join("-" if c in ".:" else c for c in raw)


# ---------------------------------------------------------------------------
# Worker-scope PreToolUse enforcement hook wiring (spike E1/E2; default ON since 15-08)
# ---------------------------------------------------------------------------

# Command anchors the hook at the FABRIC install root, never the worktree or
# consumer root. The worker-scope enforcement script ships only with the fabric;
# a dispatch worktree created from a consumer repo has no scripts/hooks/, so the
# old git-rev-parse command failed with a silent "No such file or directory" per
# tool call and the guard did nothing (OI-1089 finding 1). The command bakes in
# the fabric-absolute path (resolved via vnx_paths._resolve_vnx_home) and fails
# LOUD when the artifact is missing while still exiting 0, so a broken install is
# visible but never blocks a tool call. The path travels to the inner shell via
# the VNX_WORKER_SCOPE_HOOK environment variable (shlex.quoted at the export),
# never through bash -c string interpolation, so shell metacharacters in the
# fabric path cannot alter the executed command (PR #1413).
_WORKER_SCOPE_HOOK_MATCHER = "Bash|Write|Edit|MultiEdit"


def _worker_scope_hook_command() -> str:
    """The PreToolUse hook command, anchored at the fabric install root.

    The hook path is embedded at registration time so the command needs no
    git/consumer-relative lookup at fire time. It is handed to the inner shell
    via an environment variable rather than interpolated into the bash -c body:
    a fabric path containing shell metacharacters (space, ``;``, ``$(...)``) is
    passed literally and can never change what the command executes. The value
    is shlex.quoted at the export assignment so it survives the outer shell
    parse intact. The fail-loud guard keeps the hook non-blocking (exit 0) but
    makes a missing artifact unmistakable.
    """
    hook = _resolve_vnx_home() / "scripts" / "hooks" / "pretooluse_worker_scope_enforce.sh"
    quoted_hook = shlex.quote(str(hook))
    return (
        "export VNX_WORKER_SCOPE_HOOK={quoted_hook}; "
        "bash -c 'if [ -f \"$VNX_WORKER_SCOPE_HOOK\" ]; then "
        "exec bash \"$VNX_WORKER_SCOPE_HOOK\"; else "
        "printf \"[vnx] worker-scope hook artifact MISSING at %s; "
        "worker-scope guard is NOT enforcing\\n\" \"$VNX_WORKER_SCOPE_HOOK\" >&2; "
        "exit 0; fi'"
    ).format(quoted_hook=quoted_hook)


def _worker_scope_hook_entry() -> dict:
    """The settings.json PreToolUse entry registering the worker-scope hook."""
    return {
        "matcher": _WORKER_SCOPE_HOOK_MATCHER,
        "hooks": [
            {
                "type": "command",
                "command": _worker_scope_hook_command(),
                "timeout": 5000,
            }
        ],
    }


def _permissions_from_file(settings_file: Path) -> "dict | None":
    """Return the ``permissions`` block of *settings_file*, or None.

    Fail-open: an unreadable or malformed main-checkout settings file must not
    abort the hook write — the hook is fail-open anyway, so a corrupt source
    degrades to "nothing to merge" with a warning.
    """
    if not settings_file.exists():
        return None
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "interactive: worker-scope hook settings: could not read %s for "
            "permissions merge; skipping",
            settings_file,
        )
        return None
    if not isinstance(data, dict):
        return None
    permissions = data.get("permissions")
    return copy.deepcopy(permissions) if isinstance(permissions, dict) else None


def _merge_permissions_into(target: dict, source: dict) -> None:
    """Deep-merge *source* into *target* in place (source supplements target).

    Rule lists (allow/deny/ask) are UNIONed target-first; nested dicts recurse;
    any other value is taken from *source* (local overrides tracked). Used to
    fold the gitignored settings.local.json permissions over the tracked
    settings.json permissions in the main checkout.
    """
    for key, value in source.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
            continue
        existing = target[key]
        if isinstance(existing, list) and isinstance(value, list):
            for item in value:
                if item not in existing:
                    existing.append(item)
        elif isinstance(existing, dict) and isinstance(value, dict):
            _merge_permissions_into(existing, value)
        else:
            target[key] = copy.deepcopy(value)


def _read_main_checkout_permissions(main_checkout_root: Path) -> "dict | None":
    """Collect the main checkout's project ``permissions`` block (OI-1161).

    A fresh dispatch worktree receives the tracked ``.claude/settings.json`` via
    ``git worktree add`` but NOT the gitignored ``.claude/settings.local.json``.
    Both are folded together (local supplements tracked) so the worktree worker
    keeps every permission it would have had in the main checkout. Returns None
    when neither file carries a ``permissions`` block, so the write degrades to
    hook-only.
    """
    merged: "dict | None" = None
    for filename in ("settings.json", "settings.local.json"):
        perms = _permissions_from_file(main_checkout_root / ".claude" / filename)
        if perms is None:
            continue
        if merged is None:
            merged = copy.deepcopy(perms)
        else:
            _merge_permissions_into(merged, perms)
    return merged


def _write_worker_scope_hook_settings(
    worktree_root: Path, main_checkout_root: "Path | None" = None
) -> Path:
    """Register the worker-scope PreToolUse enforcement hook in a dispatch worktree.

    Writes/merges ``.claude/settings.local.json`` (gitignored; spike-proven to be
    honored via cwd-based discovery and to live-reload — see
    docs/investigations/spike-worker-scope-hook-feasibility.md E1/E2) rather than
    the git-tracked ``.claude/settings.json``, so the worktree's git status stays
    clean and the worker can never accidentally commit the registration.

    The hook itself is gated on ``VNX_ENFORCE_WORKER_PERMISSIONS`` (default ON
    since 15-08), so registering it unconditionally binds on every scoped spawn
    unless the dispatch explicitly opts out.

    Idempotent: an identical hook command is never registered twice. Existing
    unrelated keys and hook entries in the file are preserved.

    OI-1161: also carries over the ``permissions`` block from
    *main_checkout_root* (None → hook-only, the pre-fix behaviour) so a worktree
    worker keeps the project permissions it would have in the main checkout. The
    hook configuration written here wins: main-checkout permissions only fill in
    when the worktree file does not already define ``permissions``, and a key
    present in both is surfaced via a warning rather than silently resolved.

    Returns the settings file path written.
    """
    settings_path = worktree_root / ".claude" / "settings.local.json"
    data: dict = {}
    if settings_path.exists():
        # A corrupt existing file must not be silently clobbered — surface it to
        # the caller (which treats the whole write as best-effort).
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(
                f"worker-scope hook settings: {settings_path} is not a JSON object"
            )

    hooks = data.setdefault("hooks", {})
    pre_tool_use = hooks.setdefault("PreToolUse", [])

    already_registered = any(
        isinstance(entry, dict)
        and any(
            isinstance(hook, dict)
            and hook.get("command") == _worker_scope_hook_command()
            for hook in entry.get("hooks", [])
        )
        for entry in pre_tool_use
    )
    if not already_registered:
        pre_tool_use.append(_worker_scope_hook_entry())

    # OI-1161: fill in the main checkout's permissions (hook wins on conflict).
    if main_checkout_root is not None:
        main_permissions = _read_main_checkout_permissions(main_checkout_root)
        if main_permissions is not None:
            if "permissions" in data:
                logger.warning(
                    "interactive: worker-scope hook settings: 'permissions' exists "
                    "in both the worktree settings.local.json and the main "
                    "checkout; keeping the worktree value (hook wins) and NOT "
                    "merging the main-checkout permissions"
                )
            else:
                data["permissions"] = main_permissions

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = settings_path.with_suffix(settings_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, settings_path)
    return settings_path


# ---------------------------------------------------------------------------
# Core lane
# ---------------------------------------------------------------------------
class TmuxInteractiveDispatch:
    """Drive a dispatch through a single-shot ephemeral interactive tmux Claude session."""

    def __init__(
        self,
        state_dir: "str | Path",
        *,
        runner: "TmuxCommandRunner | None" = None,
        launch_builder: "Callable[..., str] | None" = None,
        project_root: "str | Path | None" = None,
        receipts_file: "str | Path | None" = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._runner = runner or TmuxCommandRunner()
        self._launch_builder = launch_builder or _default_launch_command
        self._project_root = (
            Path(project_root) if project_root else self._resolve_project_root()
        )
        self._handle_dir = self._state_dir / "tmux_interactive"
        self._receipts_file = (
            Path(receipts_file)
            if receipts_file
            else self._state_dir / "t0_receipts.ndjson"
        )
        # P0-1: mtime stability cache for report backstop.
        self._report_mtime_cache: dict[str, float] = {}
        # OI-863: dispatch ids for which the awaiting-permission detection event
        # has already been emitted (one event per dispatch, not one per poll).
        self._awaiting_permission_emitted: set[str] = set()
        # OI-1130: dispatch ids for which the "silent but confirmed alive"
        # thinking-worker event has already been emitted (one per dispatch).
        self._thinking_silent_emitted: set[str] = set()

    # -- OI-863 pane classification ----------------------------------------
    def _classify_pane(self, pane_id: str):
        """Capture *pane_id* and classify it (never raises).

        Returns a ``WorkerPaneState``; a failed capture classifies as unknown
        so a tmux error never takes down the lane.
        """
        cap = self._runner.run(["capture-pane", "-t", pane_id, "-p"])
        content = cap.stdout if cap.returncode == 0 else ""
        return classify_worker_pane(content)

    # -- OI-1130 deterministic liveness -------------------------------------
    def _check_worker_liveness(self, pane_id: str, session: str) -> WorkerLiveness:
        """Deterministic liveness probe for the worker behind *pane_id*/*session*.

        Distinguishes a worker that is silently THINKING (the pane log has
        stalled but the process is still there) from one that is genuinely
        DEAD (its tmux session/pane — and the OS pid behind it — are gone).
        The pane-log heartbeat alone cannot make this distinction: it only
        ever sees SILENCE, never the process itself, so a slow-thinking
        worker and a dead one look identical to it (OI-1130: 4 of 5 deep-
        thinking dispatches were killed on exactly this confusion).

        Scope (deliberate — see the dispatch report for the full rationale):
        this checks whether the worker's tmux home (session, pane, and the
        shell pid tmux spawned it with) still exists. That is the dominant
        real death mode for an ephemeral single-shot tmux dispatch — a tmux
        server crash, an externally killed session, an OOM/SIGKILL that took
        the whole process group with it. It deliberately does NOT walk the
        process tree looking for a child ``claude`` process that exited while
        the wrapping shell survives: that would need either a full-system
        ``ps`` scan on every poll interval (explicitly too expensive — this
        probe must stay cheap enough to run every poll) or a second pane-TEXT
        heuristic, which is exactly the kind of guess this check exists to
        replace. That residual gap stays bounded by the unchanged
        ``deadline_seconds``.

        Fail-open: ANY ambiguity (a tmux error, an unparseable pane_pid, a
        surprising probe exception) returns ``alive=None`` — never ``False``.
        "Cannot measure" must never be read as "dead" (mirrors the fail-open
        guard #1468 put in ``pre_merge_gate.py`` for the same principle).
        """
        try:
            has = self._runner.run(["has-session", "-t", session])
        except Exception as exc:  # noqa: BLE001 — probe must never raise into the poll loop
            return WorkerLiveness(
                alive=None, reason=f"has_session_probe_error:{exc.__class__.__name__}"
            )
        if has.returncode != 0:
            return WorkerLiveness(alive=False, reason="tmux_session_gone")

        try:
            res = self._runner.run(
                ["display-message", "-p", "-t", pane_id, "#{pane_dead}\t#{pane_pid}"]
            )
        except Exception as exc:  # noqa: BLE001
            return WorkerLiveness(
                alive=None, reason=f"pane_probe_error:{exc.__class__.__name__}"
            )
        if res.returncode != 0 or not (res.stdout or "").strip():
            return WorkerLiveness(alive=False, reason="tmux_pane_gone")

        parts = res.stdout.strip().split("\t")
        if len(parts) != 2:
            return WorkerLiveness(alive=None, reason="tmux_pane_info_unparseable")
        dead_flag, pid_str = parts[0].strip(), parts[1].strip()
        if dead_flag == "1":
            return WorkerLiveness(alive=False, reason="tmux_pane_dead_flag")
        if not pid_str.isdigit() or int(pid_str) <= 0:
            return WorkerLiveness(alive=None, reason="tmux_pane_pid_unparseable")

        pid = int(pid_str)
        try:
            alive = _process_alive(pid)
        except Exception as exc:  # noqa: BLE001 — fail open, never guess dead on a probe crash
            return WorkerLiveness(
                alive=None, reason=f"pid_probe_error:{exc.__class__.__name__}"
            )
        if alive:
            return WorkerLiveness(alive=True, reason="pane_pid_alive")
        return WorkerLiveness(alive=False, reason="pane_pid_gone")

    def _emit_awaiting_permission(
        self,
        dispatch_id: str,
        label: str,
        pane_id: str,
        reason: str,
    ) -> None:
        """Emit ``interactive_awaiting_permission`` at most once per dispatch.

        The event is the detection surface T0 uses to answer the prompt (one
        keystroke) instead of letting the worker burn its whole deadline.

        OI-1007 escalation bridge: alongside the event, a durable escalation
        record is written via ``worker_permission_relay.write_escalation`` so the
        prompt is ALSO surfaced to ``vnx permission escalations`` / ``vnx
        permission approve`` — independent of the flag-gated relay, which may not
        be running on this lane. The reason ``awaiting_permission`` records that
        the lane's own pane detection raised it, not a relay tick.
        """
        if dispatch_id in self._awaiting_permission_emitted:
            return
        self._awaiting_permission_emitted.add(dispatch_id)
        self._emit_event(
            "interactive_awaiting_permission",
            dispatch_id=dispatch_id,
            label=label,
            reason=reason,
            metadata={"pane_id": pane_id},
        )
        try:
            from worker_permission_relay import (  # noqa: PLC0415
                parse_pending_command,
                write_escalation,
            )
            cap = self._runner.run(["capture-pane", "-t", pane_id, "-p"])
            content = cap.stdout if cap.returncode == 0 else ""
            command = parse_pending_command(content)
            if command:
                write_escalation(
                    dispatch_id,
                    command,
                    "awaiting_permission",
                    state_dir=self._state_dir,
                )
                logger.info(
                    "interactive: awaiting-permission escalation written "
                    "dispatch=%s cmd=%r",
                    dispatch_id,
                    command,
                )
        except Exception as exc:  # noqa: BLE001 — the bridge is best-effort; never raise
            logger.debug(
                "interactive: awaiting-permission escalation bridge failed "
                "for %s (%s)",
                dispatch_id,
                exc,
            )

    @staticmethod
    def _resolve_project_root() -> Path:
        """scripts/lib/tmux_interactive_dispatch.py -> repo root (parents[2])."""
        return Path(__file__).resolve().parents[2]

    def _handle_path(self, dispatch_id: str) -> Path:
        return self._handle_dir / f"{dispatch_id}.json"

    # -- audit -------------------------------------------------------------
    def _emit_event(
        self,
        event_type: str,
        *,
        dispatch_id: str,
        label: str,
        reason: "str | None" = None,
        metadata: "dict | None" = None,
    ) -> None:
        """Append a coordination event (NDJSON audit parity). Never raises."""
        meta = {"label": label, "lane": "tmux_interactive"}
        if metadata:
            meta.update(metadata)
        try:
            from runtime_coordination import _append_event, get_connection

            with get_connection(self._state_dir) as conn:
                _append_event(
                    conn,
                    event_type=event_type,
                    entity_type="dispatch",
                    entity_id=dispatch_id,
                    actor="tmux_interactive",
                    reason=reason,
                    metadata=meta,
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.debug(
                "interactive: failed to emit %s for %s: %s",
                event_type,
                dispatch_id,
                exc,
            )
        except Exception as exc:  # noqa: BLE001 — DB unavailable in shadow mode
            logger.debug("interactive: emit %s skipped (%s)", event_type, exc)

    # -- context assembly --------------------------------------------------
    def _assemble_context(
        self,
        *,
        role: "str | None",
        smart_context: "str | None" = None,
        terminal_id: "str | None" = None,
        dispatch_id: "str | None" = None,
        instruction: str = "",
        dispatch_paths: "list[str] | None" = None,
        pr_id: "str | None" = None,
    ) -> str:
        """Build enriched dispatch body: skill body + intelligence + instruction.

        When VNX_SHARED_PREPARE=1, delegates to dispatch_prepare.prepare() so both
        Claude lanes share identical enrichment (permission preamble + worker-rules
        footer + report-contract directive + trailer sentinel). The default ("0")
        path runs skill + intelligence enrichment and still appends the
        report-contract directive (gap #3b) so every dispatch — on either path —
        tells the worker the required report sections and stays governed.

        Reuses subprocess lane enrichers (_inject_skill_context) so the tmux-spawn
        worker receives the same skill body + intelligence treatment as a headless
        subprocess worker. Falls back to a legacy role label + instruction on failure.
        Always includes *instruction* in the returned string.
        """
        if os.environ.get("VNX_BENCH_EQUAL_CONTEXT") == "1":
            return instruction

        if os.environ.get("VNX_SHARED_PREPARE", "0").strip().lower() in (
            "1", "true", "yes", "on"
        ):
            try:
                from dispatch_prepare import prepare  # noqa: PLC0415
                body = prepare(
                    terminal_id=terminal_id,
                    instruction=instruction,
                    role=role,
                    dispatch_id=dispatch_id or "",
                    dispatch_paths=dispatch_paths,
                    pr_id=pr_id,
                )
                if smart_context:
                    body = f"{smart_context}\n\n{body}"
                return body
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_assemble_context: dispatch_prepare.prepare() failed (%s); "
                    "falling back to standard enrichment",
                    exc,
                )

        dispatch_metadata: dict = {}
        if dispatch_id:
            dispatch_metadata["dispatch_id"] = dispatch_id
        if dispatch_paths:
            dispatch_metadata["dispatch_paths"] = dispatch_paths
        if pr_id:
            dispatch_metadata["pr_id"] = pr_id

        enriched: "str | None" = None
        try:
            from subprocess_dispatch_internals.skill_injection import _inject_skill_context  # noqa: PLC0415
            enriched = _inject_skill_context(
                terminal_id or "",
                instruction,
                role,
                dispatch_metadata,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_assemble_context: skill injection failed (%s); falling back to role label",
                exc,
            )

        if enriched is None:
            # Fallback: legacy role label + instruction
            if role:
                header = f"## Role\n\nYou are operating as a **{role}** worker."
            else:
                header = (
                    "## Worker Preamble\n\n"
                    "You are a VNX headless worker executing a dispatch instruction."
                )
            parts: list[str] = [header]
            if smart_context:
                parts.append(smart_context)
            parts.append(instruction)
            enriched = "\n\n".join(parts)
        elif smart_context:
            enriched = f"{smart_context}\n\n{enriched}"

        # Report-contract directive on the fallback path too (gap #3b parity with
        # dispatch_prepare.prepare()). Without VNX_SHARED_PREPARE the worker would
        # otherwise never be told the required report sections, so its report fails
        # the body contract -> no governed receipt -> ungoverned dispatch. Honors the
        # same VNX_REPORT_CONTRACT_DIRECTIVE gate (default on) as prepare().
        return self._append_report_contract_directive(
            enriched, dispatch_id=dispatch_id, pr_id=pr_id
        )

    @staticmethod
    def _append_report_contract_directive(
        body: str,
        *,
        dispatch_id: "str | None",
        pr_id: "str | None" = None,
    ) -> str:
        """Append the report-body-contract directive when enabled.

        Mirrors dispatch_prepare.prepare()'s step 5: same VNX_REPORT_CONTRACT_DIRECTIVE
        gate (default on). Ensures the tmux fallback path (no VNX_SHARED_PREPARE) still
        enumerates the required report sections so the worker's report passes the body
        contract and the dispatch stays governed.
        """
        if os.environ.get("VNX_REPORT_CONTRACT_DIRECTIVE", "1").strip().lower() in (
            "0", "false", "no", "off"
        ):
            return body
        # Import unguarded — same fail-closed semantics as dispatch_prepare.prepare():
        # report_body_contract is a core lib present in every tree. If it cannot be
        # imported the runtime is broken; surface that loudly rather than silently
        # shipping an ungoverned (directive-less) body, which is the exact failure
        # this directive exists to prevent.
        import report_body_contract as _rbc  # noqa: PLC0415
        return body + "\n\n" + _rbc.build_directive(dispatch_id or "", pr_id=pr_id)

    def _record_final_prompt_integrity(
        self,
        *,
        dispatch_id: str,
        final_prompt: str,
        raw_instruction: str,
    ):
        """Persist the assembled final prompt + verify raw+injections reconstruct it.

        ``final_prompt`` is the enriched body (skill + intelligence + instruction +
        report-contract directive) BEFORE this lane's deterministic delivery wrappers
        (completion protocol, scope guard, trailer). The integrity fields are baked
        into the worker's completion-protocol receipt so the audit chain closes at the
        input side.

        Best-effort: the strict (fail-closed) reconstruction raise propagates so an
        opted-in operator gets fail-closed delivery; every other error is swallowed so
        the audit-closure step can never itself break a dispatch. Returns the
        FinalPromptIntegrity or None.
        """
        try:
            from final_prompt_integrity import (  # noqa: PLC0415
                InjectionReconstructError,
                record_final_prompt_integrity,
            )
        except ImportError:
            return None
        try:
            return record_final_prompt_integrity(
                dispatch_id=dispatch_id,
                final_prompt=final_prompt,
                raw_instruction=raw_instruction,
                data_dir=self._state_dir.parent,
                state_dir=self._state_dir,
            )
        except InjectionReconstructError:
            raise
        except Exception as exc:  # noqa: BLE001 — audit closure must never break a dispatch
            logger.error(
                "interactive: final-prompt integrity failed (non-fatal) dispatch=%s: %s",
                dispatch_id,
                exc,
            )
            return None

    def _stamp_dispatch_metadata(
        self,
        dispatch_id: str,
        terminal_id: str,
        *,
        model: "str | None" = None,
        role: "str | None" = None,
        pr_id: "str | None" = None,
        outcome_status: "str | None" = None,
        report_path: "str | Path | None" = None,
        session_id: "str | None" = None,
    ) -> None:
        """Best-effort dispatch_metadata row for the leaseless tmux lane.

        Every claude-lane dispatch gets a row — this used to be flag-gated by
        ``VNX_TMUX_SESSION_ID`` (default OFF), which meant regular build
        dispatches never wrote one and the merge-side pr_id backfill
        (``receipt_provenance._link_pr_to_dispatch_metadata``) had no row to
        fill in for them. Uses the shared writer's COALESCE/INSERT-OR-IGNORE
        pattern so re-calls are idempotent and never clobber a richer
        provider-lane row.

        ``session_id``: the pre-assigned worker session UUID (F1.1), stamped only
        when the column exists and a non-empty value is provided.

        Fail-open: any error is logged at DEBUG and swallowed; the write must
        never block, delay, or fail the dispatch. The sqlite lock-wait is
        bounded to ``_METADATA_STAMP_LOCK_TIMEOUT_SECONDS`` for the same
        reason — a contended DB must not stall the dispatch for the driver's
        default 5s.
        """
        if _upsert_dispatch_metadata is None:
            logger.debug(
                "interactive: dispatch_metadata writer unavailable for dispatch=%s",
                dispatch_id,
            )
            return

        try:
            # All setup inside the guard: the metadata stamp must be fully fail-open,
            # including db_path/track computation (gate finding) — never propagate to dispatch.
            db_path = self._state_dir / "quality_intelligence.db"
            track = _TERMINAL_TRACK.get(terminal_id, "headless")
            _upsert_dispatch_metadata(
                db_path,
                dispatch_id=dispatch_id,
                terminal=terminal_id,
                provider="claude",
                model=model,
                track=track,
                role=role,
                gate=None,
                pr_id=pr_id,
                outcome_status=outcome_status,
                report_path=str(report_path) if report_path else None,
                session_id=session_id,
                timeout=_METADATA_STAMP_LOCK_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — metadata stamp is best-effort; must never block dispatch
            logger.debug(
                "interactive: dispatch_metadata stamp failed for dispatch=%s (non-fatal): %s",
                dispatch_id,
                exc,
            )

    def _govern_report(
        self,
        dispatch_id: str,
        terminal_id: str,
        instruction: str,
        receipt: "dict | None",
        duration_seconds: float,
        *,
        pr_id: "str | None" = None,
        base_sha: "str | None" = None,
        worktree_path: "Path | None" = None,
        model: "str | None" = None,
        failure_reason: str = "tmux_receipt_deadline_exceeded",
        token_usage: "dict | None" = None,
        role: "str | None" = None,
        session_id: "str | None" = None,
        permission_posture: "dict | None" = None,
    ) -> "Path | None":
        """Emit governance unified_report via the shared govern() step.

        The tmux lane always routes through govern() — no VNX_SHARED_GOVERN gate
        applies here. govern() is guaranteed not to raise; on any internal error
        it emits an honest minimal synthesized body with contract_status="synthesized".

        Returns the emitted report path on success, None on critical import failure.
        A None return is an audit-trail gap and must be surfaced by the caller.

        ``role`` is forwarded to GovernSpec so govern() can apply role-specific
        validation logic (e.g. plan-reviewer bodies skip standard heading validation).

        ``session_id``: pre-assigned worker session UUID (F1.1), threaded through to
        the dispatch_metadata stamp.

        ``permission_posture`` (OI-864): forwarded to GovernSpec so a
        lane-synthesized fallback receipt (worker never emitted its own,
        ``ensure_receipt``) still carries the real spawn-time posture instead
        of omitting it or re-deriving it from env vars a second time.
        """
        try:
            from dispatch_govern import GovernRaw, GovernSpec, govern  # noqa: PLC0415
        except ImportError as exc:
            logger.error(
                "interactive: dispatch_govern import failed for dispatch=%s: %s",
                dispatch_id, exc,
            )
            return None

        spec = GovernSpec(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            instruction=instruction,
            data_dir=self._state_dir.parent,
            state_dir=self._state_dir,
            pr_id=pr_id,
            base_sha=base_sha,
            worktree_path=worktree_path,
            model=model,
            role=role,
            permission_posture=permission_posture,
        )
        raw = GovernRaw(
            receipt=receipt,
            duration_seconds=duration_seconds,
            failure_reason=failure_reason,
            token_usage=token_usage,
        )
        outcome = govern(spec, raw, lane="tmux_interactive")
        self._stamp_dispatch_metadata(
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            model=model,
            role=role,
            pr_id=pr_id,
            outcome_status=outcome.contract_status,
            report_path=outcome.report_path,
            session_id=session_id,
        )
        if outcome.report_path:
            logger.info(
                "interactive: govern() emitted report dispatch=%s "
                "contract_status=%s path=%s",
                dispatch_id, outcome.contract_status, outcome.report_path,
            )
        else:
            logger.warning(
                "interactive: govern() returned no report_path for dispatch=%s "
                "contract_status=%s error=%s",
                dispatch_id, outcome.contract_status, outcome.error,
            )
        return outcome.report_path

    # Pane TUI cumulative token counter, e.g. "(18s · ↓ 739 tokens)" (output / down) and
    # "(↑ 12 tokens)" (input / up). Best-effort: the subscription lane has no usage API.
    _PANE_OUTPUT_TOKENS_RE = re.compile(r"[↓⬇]\s*([\d,]+)\s*tokens?\b", re.IGNORECASE)
    _PANE_INPUT_TOKENS_RE = re.compile(r"[↑⬆]\s*([\d,]+)\s*tokens?\b", re.IGNORECASE)
    _ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

    def _parse_token_usage_from_log(self, raw_log: "Path | None") -> "dict | None":
        """Best-effort token counts for the claude subscription lane (no usage API).

        The interactive TUI prints a cumulative counter like "(18s · ↓ 739 tokens)".
        pipe-pane records every redraw, so the MAX ↓/↑ value across the captured log is
        the final output/input token total. Returns {"input","output","cache_read"} or
        None when nothing parseable was seen (then the report frontmatter stays at 0 and
        the scorer reports tokens/sec as n/a rather than a fabricated number).
        """
        if raw_log is None:
            return None
        try:
            text = Path(raw_log).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        text = self._ANSI_RE.sub("", text)

        def _max_tokens(rx: "re.Pattern") -> int:
            vals = [int(m.replace(",", "")) for m in rx.findall(text) if m]
            return max(vals) if vals else 0

        out = _max_tokens(self._PANE_OUTPUT_TOKENS_RE)
        inp = _max_tokens(self._PANE_INPUT_TOKENS_RE)
        if out == 0 and inp == 0:
            return None
        return {"input": inp, "output": out, "cache_read": 0}

    def _fail_loud_on_empty_extraction(
        self,
        *,
        dispatch_id: str,
        receipt: "dict | None",
        role: "str | None",
        worktree_path: "Path | None",
        base_sha: "str | None",
        pane_tokens: "dict | None" = None,
    ) -> "dict | None":
        """Reject a worker's self-reported completion BEFORE govern() synthesizes a
        report from it, when the worktree shows no evidence of work.

        The tmux-lane twin of dispatch_envelope._fail_loud_on_empty_success — that
        guard catches an empty PROVIDER completion at EXECUTE time, before GOVERN
        ever sees it. Until this hook, the tmux lane's equivalent check
        (phantom_guard) ran only INSIDE govern(), AFTER the unified report was
        already synthesized and emitted with status="done" — the correction landed
        as a second, later receipt (audit-honest, per phantom_guard's design), but
        the report FILE itself kept claiming success. Running the identical
        rejection here, before _govern_report(), means the report synthesized from
        this receipt already reflects "failed" — no "done" report is ever emitted
        for an empty extraction (dispatch 20260711-164747-provider-hardening).

        Delegates to phantom_guard.record_phantom_if_any for BOTH the decision and
        the corrective-receipt append (the same ndjson-visible signal the govern()
        -time backstop produces), so the two chokepoints share one implementation
        and never diverge. By the time govern()'s own post-hoc check runs, the
        receipt it sees already carries status="failed" — its guard_at_govern
        short-circuits on the non-completion status, so no second corrective
        receipt is appended.

        Never raises: on any internal error the receipt is returned unchanged
        (fail-open, matching the guard's own abstain contract).
        """
        if receipt is None:
            return receipt
        try:
            from phantom_guard import record_phantom_if_any  # noqa: PLC0415
            _tok = pane_tokens or {}
            verdict = record_phantom_if_any(
                dispatch_id=dispatch_id,
                role=role,
                status=receipt.get("status"),
                token_usage=(
                    int(_tok.get("input", 0) or 0) + int(_tok.get("output", 0) or 0)
                ) or None,
                worktree_path=worktree_path,
                base_sha=base_sha,
                receipts_file=str(self._receipts_file),
                state_dir=self._state_dir,
            )
        except Exception as exc:  # noqa: BLE001 — never block a real completion on a guard error
            logger.error(
                "interactive: fail-loud-on-empty-extraction guard errored dispatch=%s: %s",
                dispatch_id, exc,
            )
            return receipt
        if not verdict.is_phantom:
            return receipt
        logger.warning(
            "interactive: fail-loud-on-empty-extraction REJECTED dispatch=%s — %s",
            dispatch_id, verdict.reason,
        )
        self._emit_event(
            "interactive_empty_extraction",
            dispatch_id=dispatch_id,
            label=str(receipt.get("terminal") or receipt.get("terminal_id") or ""),
            reason=verdict.reason,
            metadata={"original_status": receipt.get("status")},
        )
        downgraded = dict(receipt)
        downgraded["status"] = "failed"
        downgraded["phantom_rejected"] = True
        downgraded["phantom_reason"] = verdict.reason
        return downgraded

    def _enforce_pr_exists(
        self,
        *,
        dispatch_id: str,
        label: str,
        worktree_handle: "WorktreeHandle",
        worktree_state: str,
    ) -> "PrEnforcementResult":
        """Lane-context adapter over pr_enforcement.enforce_pr_exists (lane-fix B).

        ``worktree_state`` is the caller's already-computed classify(worktree_handle)
        verdict — passed in rather than reclassified here so classify() runs exactly
        once per dispatch (its result is memoized into _wt_classification and reused
        by _teardown's reap() call).

        When the branch was pushed to origin, ensures an open PR exists — creating
        one via gh_pr_ensure when it does not. Emits an audit event either way; a
        creation failure is ALSO recorded as a receipt-visible corrective receipt by
        pr_enforcement itself.

        Never raises: mirrors the fail-open contract of the phantom-guard hook —
        an internal error here must not crash a real, successful dispatch.
        """
        try:
            from pr_enforcement import enforce_pr_exists  # noqa: PLC0415

            # OI-1127: hand the worktree path through so a "dirty" verdict is
            # split into substantive vs scratch and substantive work is
            # salvaged (OI-1119) — without wt_path this lane kept the old
            # lose-everything behaviour while the envelope lane was already
            # fixed. Verified to still exist at this moment rather than
            # assumed: this runs before _teardown's reap(), but a vanished
            # directory (external cleanup, crash-restart) must degrade to the
            # back-compat path, not feed a dead path into git.
            _wt_path = worktree_handle.path if worktree_handle.path.is_dir() else None
            result = enforce_pr_exists(
                dispatch_id=dispatch_id,
                branch=worktree_handle.branch,
                worktree_state=worktree_state,
                repo_root=self._project_root,
                receipts_file=self._receipts_file,
                wt_path=_wt_path,
                pr_title=f"dispatch({dispatch_id}): auto-created by VNX tmux-spawn lane",
                pr_body=(
                    f"Auto-created by VNX tmux-spawn build-dispatch completion "
                    f"(dispatch `{dispatch_id}`) — the worker pushed this branch "
                    "but did not open a PR itself. Please review before merging."
                ),
            )
        except Exception as exc:  # noqa: BLE001 — never block a real completion on this guard
            logger.error(
                "interactive: PR-enforcement guard errored dispatch=%s: %s", dispatch_id, exc,
            )
            return PrEnforcementResult(applicable=False, ok=True, reason=f"guard error: {exc}")

        if not result.applicable:
            return result

        if result.ok:
            self._emit_event(
                "interactive_autopr",
                dispatch_id=dispatch_id,
                label=label,
                reason="created" if result.created else "already_existed",
                metadata={"pr_number": result.pr_number, "created": result.created},
            )
        else:
            logger.warning(
                "interactive: PR-enforcement REJECTED dispatch=%s — %s",
                dispatch_id, result.reason,
            )
            self._emit_event(
                "interactive_autopr_failed",
                dispatch_id=dispatch_id,
                label=label,
                reason=result.reason,
            )
        return result

    def _build_completion_protocol(
        self,
        dispatch_id: str,
        label: str,
        model: str = "",
        *,
        integrity=None,
        permission_posture: "dict | None" = None,
    ) -> str:
        """Footer instructing the worker to emit a clean receipt directly.

        The path to ``append_receipt.py`` is ABSOLUTE so it resolves correctly
        regardless of the worker's cwd.

        Receipt-truth design (sweep H3):
        - ``status`` is NOT pre-baked: the worker picks one of two ready-made
          commands (done/failed) so the status reflects actual outcome.
        - ``timestamp`` is NOT generated at body-assembly time: each command uses
          shell ``$(date -u +%Y-%m-%dT%H:%M:%SZ)`` so the timestamp records the
          execution moment, not when the dispatch was launched.

        ``permission_posture`` (OI-864): the dict returned by
        ``classify_permission_posture()`` from the ACTUAL flags assembled for
        this spawn's ``launch_cmd`` — never re-derived from env vars here.
        Baked in as ``permission_posture`` / ``permission_profile`` /
        ``permission_allow_pattern_count`` alongside the other spawn
        properties (model/lane/terminal) this receipt already carries. Only
        stamped when provided, so callers that do not compute it (e.g. direct
        unit tests of this method) keep a byte-identical receipt shape.
        """
        append_receipt = self._project_root / "scripts" / "append_receipt.py"
        # report_path is deterministic — include it so the receipt->report linkage
        # is established even when the report is written after the receipt.
        report_path = str(
            self._state_dir.parent / "unified_reports" / f"{dispatch_id}.md"
        )

        state_dir_q = shlex.quote(str(self._state_dir))
        data_dir_q = shlex.quote(str(self._state_dir.parent))
        append_receipt_q = shlex.quote(str(append_receipt))
        receipts_file_q = shlex.quote(str(self._receipts_file))

        def _make_receipt_json(status: str) -> str:
            """Return a double-quoted JSON argument with shell-evaluated timestamp.

            The receipt dict is built in Python (for correct key/value escaping)
            with a sentinel timestamp placeholder. The placeholder is then replaced
            with a shell ``$(date ...)`` substitution *outside* the single-quote
            boundary so that bash evaluates it at execution time.

            The outer double-quotes let ``$_VNX_TS`` expand; inner double-quotes
            inside the JSON are escaped with backslash.
            """
            _TS_SENTINEL = "__VNX_TS_PLACEHOLDER__"
            receipt_dict = {
                "event_type": "subprocess_completion",
                "receipt_kind": "dispatch",
                "dispatch_id": dispatch_id,
                "terminal": label,
                "terminal_id": label,
                "status": status,
                "source": "tmux_interactive",
                "timestamp": _TS_SENTINEL,
                "report_path": report_path,
                "provider": "claude",
                "sub_provider": "anthropic",
                "model": model,
                "lane": "tmux_interactive",
            }
            # Audit marker for the ADR-012 worker-permission enforcement mode.
            # Only emitted when the flag is ON so flag-off receipts are byte-identical.
            if worker_permission_enforcement_enabled():
                receipt_dict["permission_enforcement"] = "enforced"
            # OI-864: the actual spawn-time permission posture, derived from the
            # real launch flags (see classify_permission_posture). Distinct from
            # (and more precise than) permission_enforcement above, which only
            # ever says "enforced" or omits itself — it cannot show blanket-skip
            # vs scoped-allowlist, and re-reads the env independently of what
            # flags this spawn actually used.
            if permission_posture:
                receipt_dict["permission_posture"] = permission_posture.get(
                    "permission_posture"
                )
                if permission_posture.get("permission_profile") is not None:
                    receipt_dict["permission_profile"] = permission_posture[
                        "permission_profile"
                    ]
                if permission_posture.get("permission_allow_pattern_count") is not None:
                    receipt_dict["permission_allow_pattern_count"] = permission_posture[
                        "permission_allow_pattern_count"
                    ]
            # Input-side audit closure (final_prompt_integrity). Only baked in when
            # integrity was computed so the receipt shape stays byte-identical
            # otherwise. The worker echoes these back verbatim in its receipt.
            if integrity is not None:
                if getattr(integrity, "final_prompt_path", None) is not None:
                    receipt_dict["final_prompt_path"] = integrity.final_prompt_path
                receipt_dict["final_prompt_sha256"] = integrity.final_prompt_sha256
                receipt_dict["injection_reconstructs"] = bool(
                    integrity.injection_reconstructs
                )
            json_str = json.dumps(receipt_dict)
            # Escape inner double-quotes for the shell double-quoted argument.
            escaped = json_str.replace('"', '\\"')
            # Replace the sentinel with the shell variable reference.
            escaped = escaped.replace(_TS_SENTINEL, "$_VNX_TS")
            return f'"{escaped}"'

        env_prefix = (
            f"VNX_STATE_DIR={state_dir_q} VNX_DATA_DIR={data_dir_q} VNX_DATA_DIR_EXPLICIT=1"
        )
        py_cmd = (
            f"python3 {append_receipt_q} "
            f"--receipts-file {receipts_file_q} --receipt"
        )

        done_receipt_arg = _make_receipt_json("done")
        failed_receipt_arg = _make_receipt_json("failed")

        done_cmd = (
            f"_VNX_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
            f"{env_prefix} {py_cmd} {done_receipt_arg}"
        )
        failed_cmd = (
            f"_VNX_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
            f"{env_prefix} {py_cmd} {failed_receipt_arg}"
        )

        return (
            "\n\n---\n\n## Completion Protocol (interactive lane)\n\n"
            "When you have finished AND committed your work, emit a completion "
            "receipt so the orchestrator can detect completion. "
            "**Choose the correct command — do not copy blindly:**\n\n"
            "**If work completed successfully:**\n\n"
            "```bash\n"
            f"{done_cmd}\n"
            "```\n\n"
            "**If you could NOT complete the work (error, blocker, or partial):**\n\n"
            "```bash\n"
            f"{failed_cmd}\n"
            "```\n\n"
            "Always write your unified report first, then emit the receipt "
            "as the very last step.\n"
        )

    def _scope_note(self, dispatch_paths: "list[str] | str | None") -> str:
        """Generate a scope-guard block instructing the worker to stay within paths."""
        if not dispatch_paths:
            return ""
        if isinstance(dispatch_paths, str):
            paths = [dispatch_paths]
        else:
            paths = list(dispatch_paths)
        paths_str = "\n".join(f"  - `{p}`" for p in paths)
        return (
            "\n\n---\n\n## Scope Guard\n\n"
            "**Edit ONLY within these paths.** Do not touch files outside this scope:\n\n"
            f"{paths_str}\n"
        )

    # -- tmux primitives ---------------------------------------------------
    def _spawn_session(
        self,
        session: str,
        cwd: Path,
        dispatch_id: str = "",
        session_uuid: "str | None" = None,
        role: "str | None" = None,
        dispatch_paths: "list[str] | None" = None,
    ) -> "tuple[str, str] | None":
        """Create a detached session; return (pane_id, window_id) or None.

        When ``dispatch_id`` is provided it is exported into the pane environment as
        ``VNX_CURRENT_DISPATCH_ID`` so the worker's ``git commit`` carries a provenance
        trace token (read by the prepare-commit-msg hook / trace_token_validator), closing
        the dispatch->commit link in the provenance_registry.

        When ``session_uuid`` is provided it is exported as ``VNX_CLAUDE_SESSION_ID``
        so the SessionStart hook can verify the pre-assigned id took (F1.1).

        When ``role`` is provided it is exported as ``VNX_WORKER_ROLE`` so the
        worker-scope PreToolUse enforcement hook
        (scripts/hooks/pretooluse_worker_scope_enforce.py) can resolve which
        role's permission profile to enforce (spike E3 gap).

        When ``dispatch_paths`` is provided it is JSON-encoded and exported as
        ``VNX_DISPATCH_PATHS`` (OI-1196) so the same hook can narrow the
        role's file_write_scope to this dispatch's declared paths — never
        wider than the role, only sharper. Before this, ``dispatch_paths``
        only reached the worker as prose (``_scope_note()``) and
        ``dispatch_metadata``; it had no enforcement channel at all.
        """
        args = ["new-session", "-d", "-s", session, "-c", str(cwd)]
        if dispatch_id:
            args += ["-e", f"VNX_CURRENT_DISPATCH_ID={dispatch_id}"]
        if session_uuid:
            args += ["-e", f"VNX_CLAUDE_SESSION_ID={session_uuid}"]
        if dispatch_paths:
            args += ["-e", f"VNX_DISPATCH_PATHS={json.dumps(list(dispatch_paths))}"]
        if role:
            args += ["-e", f"VNX_WORKER_ROLE={role}"]
        args += ["-P", "-F", "#{pane_id}"]
        res = self._runner.run(args)
        if res.returncode != 0:
            logger.warning(
                "interactive: new-session %s failed: %s", session, res.stderr.strip()
            )
            return None
        pane_id = res.stdout.strip()
        win = self._runner.run(
            ["display-message", "-p", "-t", pane_id, "#{window_id}"]
        )
        window_id = win.stdout.strip() if win.returncode == 0 else ""
        return pane_id, window_id

    def _launch_claude(self, pane_id: str, launch_cmd: str) -> bool:
        """Send the interactive-claude launch line to the pane, then submit."""
        rc = self._runner.run(
            ["send-keys", "-t", pane_id, "-l", launch_cmd]
        ).returncode
        if rc != 0:
            return False
        # Enter ALWAYS as a separate keystroke.
        return self._runner.run(["send-keys", "-t", pane_id, "Enter"]).returncode == 0

    def _verify_session_id(
        self,
        signal_dir: "Path | None",
        session_uuid: "str | None",
        *,
        dispatch_id: str,
        label: str,
    ) -> None:
        """Compare the hook-reported session id to the pre-assigned uuid (F1.1).

        Fires only when ``session_uuid`` is set (``VNX_TMUX_SESSION_ID`` flag ON).
        A missing or empty ``session_id`` sentinel is fail-open: older CLIs may not
        report the id. Only a non-empty DIFFERENT value emits a ``session_id_mismatch``
        audit event. Never raises and never blocks the dispatch.
        """
        if not session_uuid:
            return
        if signal_dir is None:
            return
        sid_path = signal_dir / "session_id"
        try:
            if not sid_path.exists():
                return
            hook_session_id = sid_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.debug(
                "interactive: failed to read hook session_id for %s: %s",
                dispatch_id,
                exc,
            )
            return
        if not hook_session_id:
            return
        if hook_session_id != session_uuid:
            self._emit_event(
                "session_id_mismatch",
                dispatch_id=dispatch_id,
                label=label,
                reason="Claude CLI session_id differs from pre-assigned VNX session_uuid",
                metadata={
                    "pre_assigned_session_id": session_uuid,
                    "hook_session_id": hook_session_id,
                },
            )

    def _wait_ready(
        self,
        pane_id: str,
        *,
        ready_markers: "tuple[str, ...]",
        warmup_timeout: float,
        poll_interval: float,
        signal_dir: "Path | None" = None,
    ) -> bool:
        """Wait until the worker session is ready, or timeout.

        PRIMARY signal: the SessionStart hook sentinel ("<signal_dir>/session_ready").
        It is checked first on every poll and confirms readiness via the stable hook
        contract — version-agnostic, independent of any TUI banner wording. The
        capture-pane marker scan is only a tolerant fallback for when the sentinel is
        unavailable.
        """
        ready_sentinel = (signal_dir / "session_ready") if signal_dir else None
        deadline = time.monotonic() + warmup_timeout
        while time.monotonic() < deadline:
            # PRIMARY: SessionStart hook fired → session is ready.
            if ready_sentinel is not None:
                try:
                    if ready_sentinel.exists():
                        logger.info(
                            "interactive: readiness via SessionStart sentinel for %s",
                            pane_id,
                        )
                        return True
                except OSError:
                    pass
            # FALLBACK: tolerant TUI marker scan.
            cap = self._runner.run(["capture-pane", "-t", pane_id, "-p"])
            content = cap.stdout if cap.returncode == 0 else ""
            if content and any(m in content for m in ready_markers):
                return True
            time.sleep(poll_interval)
        logger.info(
            "interactive: readiness marker not seen for %s before %.0fs warmup "
            "(STRICT=1 will abort; STRICT=0 will proceed)",
            pane_id,
            warmup_timeout,
        )
        return False

    def _verify_pane_identity(self, pane_id: str, session: str) -> bool:
        """OI-1126 defense-in-depth: assert *pane_id* still belongs to *session*.

        Pane addressing was measured stable (a captured pane_id never drifts to a
        different session within a live tmux server — 0 mismatches across repeated
        concurrent-spawn trials), but this check is the difference between "delivery
        to the wrong pane is unlikely" and "delivery to the wrong pane is impossible":
        called immediately before every send-keys/paste that carries dispatch content,
        so a future addressing regression fails loud here instead of silently
        delivering into a sibling dispatch's pane.
        """
        check = self._runner.run(["display-message", "-p", "-t", pane_id, "#{session_name}"])
        return check.returncode == 0 and check.stdout.strip() == session

    def _deliver_instruction(self, pane_id: str, body: str, dispatch_id: str) -> bool:
        """Clear input, paste the instruction body, settle, then submit with Enter."""
        self._runner.run(["send-keys", "-t", pane_id, "C-u"])
        if not self._paste(pane_id, body, dispatch_id):
            return False
        # Settle after bracketed paste so the pane has fully received the content.
        # Scale with body size: +1s per 50k chars, capped at +2s. Large bodies need
        # more time for the terminal to process bracketed-paste under load.
        _base = float(os.environ.get("VNX_TMUX_PASTE_SETTLE_SECONDS", "0.75"))
        _extra = min(len(body) / 50000.0, 2.0)
        _settle = _base + _extra
        if _settle > 0:
            time.sleep(_settle)
        # Enter ALWAYS as a separate keystroke.
        return self._runner.run(["send-keys", "-t", pane_id, "Enter"]).returncode == 0

    # Sentinel appended to every dispatch body (END_OF_INSTRUCTION_SENTINEL from dispatch_prepare).
    _END_SENTINEL = "<!-- VNX-END-OF-INSTRUCTION -->"
    # ── Tolerant TUI fallback heuristics — NOT the primary signal ──────────────
    # Hooks are PRIMARY (SessionStart ready / UserPromptSubmit submit) and the
    # receipt is AUTHORITATIVE for completion. The pane-scrape below is only a
    # backstop for when the hook sentinels are unavailable, and MUST stay
    # version-robust: never hard-depend on a Claude Code version's exact wording.
    #
    # Evidence: claude 2.1.160 dropped "esc to interrupt" in favour of a
    # random-gerund spinner + token-counter, e.g. "✢ Smooshing… (18s · ↓ 739 tokens)".
    # So the working-detector matches the STRUCTURAL token-counter shape
    # ("(<n>s · ↓/↑ <n> tokens)"), OR a tolerant set of legacy literals.
    _WORKING_TOKEN_RE = re.compile(r"\(\s*\d+\s*s\b[^)\n]*tokens?\b", re.IGNORECASE)
    _WORKING_LITERALS = ("esc to interrupt", "to interrupt")
    # Bottom N lines of the pane constitute the "input region" for staged-paste detection.
    _INPUT_REGION_LINES = 10
    # Leading chars of the body used as a fingerprint to detect it in the input region.
    _BODY_FINGERPRINT_LEN = 80

    def _looks_working(self, content: str) -> bool:
        """Version-robust "Claude is actively working" detector (tolerant fallback).

        Returns True if the pane content shows the structural token-counter the TUI
        prints while a turn is running ("(18s · ↓ 739 tokens)", "(3s · ↑ 12 tokens)")
        OR any tolerant legacy literal ("esc to interrupt", "to interrupt"). This is
        only a backstop to the hook sentinels — never a hard dependency on a specific
        Claude Code version's wording.
        """
        if not content:
            return False
        if self._WORKING_TOKEN_RE.search(content):
            return True
        lowered = content.lower()
        return any(lit in lowered for lit in self._WORKING_LITERALS)

    def _verify_submit(self, pane_id: str, body: str, *, signal_dir: "Path | None" = None) -> bool:
        """Confirm the instruction was actually submitted, not left staged in the input box.

        Submitted = working indicator present anywhere in the pane, OR the input region
        (bottom ``_INPUT_REGION_LINES`` lines) contains none of the staged-paste signals.
        Still staged = no working indicator AND the input region still shows the paste
        (bracketed-paste annotation, END_SENTINEL, or leading body text).

        Scoping the check to the input region avoids the echo false-positive: after a
        real submit, Claude echoes the body into the conversation scrollback, which would
        contain the sentinel — that is NOT the same as the body still being staged.

        Works on both paths: VNX_SHARED_PREPARE=1 (body contains sentinel) and the legacy
        default (no sentinel in body, detected via bracketed-paste annotation or fingerprint).

        PRIMARY signal: the UserPromptSubmit hook sentinel ("<signal_dir>/prompt_received").
        When present, submission is confirmed via the stable hook contract and the
        TUI-scrape fallback below is skipped entirely. The pane scrape is only a backstop.

        Uses a bounded guarded-retry loop: up to VNX_TMUX_SUBMIT_MAX_RETRIES (default 3)
        additional Enter keystrokes, each preceded by a _still_staged() check to prevent
        stray Enters landing in a running session. Each retry polls for up to
        VNX_TMUX_SUBMIT_RETRY_DELAY seconds. The entire loop is bounded by
        VNX_TMUX_SUBMIT_VERIFY_TIMEOUT. Returns False only when still staged after all
        retries are exhausted within the deadline.
        """
        max_retries = int(os.environ.get("VNX_TMUX_SUBMIT_MAX_RETRIES", "3"))
        retry_delay = float(os.environ.get("VNX_TMUX_SUBMIT_RETRY_DELAY", "0.75"))
        verify_timeout = float(os.environ.get("VNX_TMUX_SUBMIT_VERIFY_TIMEOUT", "5"))
        body_fingerprint = body.strip()[:self._BODY_FINGERPRINT_LEN]
        _prompt_sentinel = (signal_dir / "prompt_received") if signal_dir else None

        def _sentinel_submitted() -> bool:
            try:
                return _prompt_sentinel is not None and _prompt_sentinel.exists()
            except OSError:
                return False

        def _still_staged() -> bool:
            # PRIMARY: UserPromptSubmit hook fired → instruction was submitted.
            if _sentinel_submitted():
                return False
            cap = self._runner.run(["capture-pane", "-t", pane_id, "-p"])
            content = cap.stdout if cap.returncode == 0 else ""
            # Working indicator anywhere in pane → Claude is running → submitted.
            if self._looks_working(content):
                return False
            # Scope staged-paste check to the input region (bottom lines only).
            # Sentinel/instruction text in the scrollback (upper area) means Claude already
            # echoed the submitted content — do not treat that as "still staged".
            lines = content.splitlines()
            input_region = "\n".join(lines[-self._INPUT_REGION_LINES:]) if lines else ""
            if "[Pasted text" in input_region:
                return True   # bracketed-paste staging annotation still visible
            if self._END_SENTINEL in input_region:
                return True   # sentinel still in input buffer (not just scrollback echo)
            if body_fingerprint and body_fingerprint in input_region:
                return True   # leading body text still in input buffer
            return False      # unknown state; assume submitted (conservative)

        # Fast path: already submitted (first Enter from _deliver_instruction worked).
        if _sentinel_submitted():
            logger.info(
                "interactive: submission confirmed via UserPromptSubmit sentinel for %s",
                pane_id,
            )
            return True
        if not _still_staged():
            return True

        deadline = time.monotonic() + verify_timeout

        for _attempt in range(max_retries):
            if time.monotonic() >= deadline:
                break

            # Guard: only send Enter when we confirmed it's still staged right now.
            # Prevents stray Enters landing in an already-running session.
            if not _still_staged():
                return True

            self._runner.run(["send-keys", "-t", pane_id, "Enter"])

            # Poll for up to retry_delay seconds (or until overall deadline).
            poll_deadline = min(time.monotonic() + retry_delay, deadline)
            while time.monotonic() < poll_deadline:
                if not _still_staged():
                    return True
                time.sleep(0.1)

        # Final check after all retries exhausted.
        if not _still_staged():
            return True

        logger.warning(
            "interactive: submit-verify timeout for pane %s after %d retries (%.1fs deadline): "
            "paste still staged in input region",
            pane_id, max_retries, verify_timeout,
        )
        return False

    def _await_work_started(
        self,
        pane_id: str,
        dispatch_id: str,
        *,
        signal_dir: "Path | None",
        baseline_count: int,
        baseline_pending_ids: "frozenset[str]",
        completion_statuses: "frozenset[str]",
        label: str,
    ) -> str:
        """Confirm the worker actually STARTED working after a verified submit.

        ``_verify_submit`` confirms the input box is no longer staged, but under
        subscription load a submit can clear the box without the worker progressing —
        Claude idles at the prompt and never writes a receipt. Without this gate the
        lane would then wait the FULL ``deadline_seconds`` for a receipt that never
        comes (the recurring warmup-miss/no-progress hang in DISPATCH_RULES §7).

        This bounded watchdog polls for a real work signal — the UserPromptSubmit
        sentinel, a version-robust working indicator (``_looks_working``), or a fresh
        receipt — and re-nudges ONCE with a guarded Enter if the instruction is still
        staged (never a stray Enter into a running session).

        Returns one of WORK_START_*:

        * ``WORK_START_WORKING`` — work observed (or gate disabled); proceed to the
          receipt wait.
        * ``WORK_START_AWAITING_PERMISSION`` — no work within the window AND the pane
          shows a permission prompt (OI-863).  The worker is ALIVE and one keystroke
          saves it; the caller must NOT fast-abort it as no_progress.
        * ``WORK_START_NO_PROGRESS`` — no work and no permission prompt; the caller can
          FAST-ABORT (retryable in seconds) instead of burning the full deadline.

        Gate is on by default; set ``VNX_TMUX_WORK_START_GATE=0`` to restore the prior
        proceed-straight-to-receipt-wait behavior.

        A worker blocked on a permission prompt is now detected (not a silent
        no-progress); an attended/permissioned session should still enable the
        permission relay (``VNX_PERMISSION_RELAY=1``) so the prompt is ANSWERED rather
        than merely detected.
        """
        if os.environ.get("VNX_TMUX_WORK_START_GATE", "1").strip().lower() in (
            "0", "false", "no", "off"
        ):
            return WORK_START_WORKING

        timeout = float(os.environ.get("VNX_TMUX_WORK_START_TIMEOUT", "120"))
        poll = float(os.environ.get("VNX_TMUX_WORK_START_POLL", "3"))
        prompt_sentinel = (signal_dir / "prompt_received") if signal_dir else None

        def _work_observed() -> bool:
            # (a) UserPromptSubmit hook fired → worker accepted the prompt and is running.
            try:
                if prompt_sentinel is not None and prompt_sentinel.exists():
                    return True
            except OSError:
                pass
            # (b) version-robust working indicator anywhere in the pane.
            cap = self._runner.run(["capture-pane", "-t", pane_id, "-p"])
            content = cap.stdout if cap.returncode == 0 else ""
            if content and self._looks_working(content):
                return True
            # (c) a receipt already appeared for this dispatch (a fast worker that
            # finished before the first poll, or any progress receipt).
            canonical, pending = self._matching_receipts_split(
                dispatch_id, completion_statuses
            )
            if len(canonical) > baseline_count:
                return True
            # Filter empty _pending_file values: a pending receipt without a file would
            # otherwise contribute "" and read as fresh progress unless "" is already in
            # the baseline (making the gate falsely lenient).
            if frozenset(
                f for f in (r.get("_pending_file", "") for r in pending) if f
            ) - baseline_pending_ids:
                return True
            return False

        def _still_staged() -> bool:
            cap = self._runner.run(["capture-pane", "-t", pane_id, "-p"])
            content = cap.stdout if cap.returncode == 0 else ""
            lines = content.splitlines()
            region = "\n".join(lines[-self._INPUT_REGION_LINES:]) if lines else ""
            return ("[Pasted text" in region) or (self._END_SENTINEL in region)

        deadline = time.monotonic() + timeout
        nudged = False
        while time.monotonic() < deadline:
            if _work_observed():
                return WORK_START_WORKING
            # Guarded hand-deliver: re-submit ONCE, and only while the instruction is
            # still staged AND not working as of this instant (both re-checked right
            # before the send) — never a stray Enter into a session that just started.
            if not nudged and _still_staged() and not _work_observed():
                self._emit_event(
                    "interactive_hand_deliver",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason="no work observed after submit; re-submitting staged instruction",
                    metadata={"pane_id": pane_id},
                )
                self._runner.run(["send-keys", "-t", pane_id, "Enter"])
                nudged = True
            time.sleep(poll)
        # Gate window elapsed without work.  Classify the pane: a permission prompt is
        # a RECOVERABLE state — one relayed answer saves the worker — so it must NOT
        # fast-abort as no_progress (OI-863).
        final = self._classify_pane(pane_id)
        if final.is_awaiting_permission:
            self._emit_awaiting_permission(
                dispatch_id, label, pane_id,
                "permission prompt at work-start gate; not fast-aborting",
            )
            return WORK_START_AWAITING_PERMISSION
        return WORK_START_NO_PROGRESS

    def _paste(self, pane_id: str, content: str, dispatch_id: str, max_inline: int = 50000) -> bool:
        """Load *content* into a PER-DISPATCH NAMED tmux buffer and paste it into the pane.

        OI-1126: ``load-buffer``/``paste-buffer`` without ``-b <name>`` operate on the
        tmux SERVER's single shared "most recent buffer" slot, not on data scoped to
        this call. Two dispatches racing to load+paste concurrently (fired close
        together, e.g. via the door) can have dispatch A's ``paste-buffer`` retrieve
        dispatch B's just-loaded content instead of its own — a TOCTOU race on shared
        global state. Measured: 0/25 crossings with a 2-dispatch, 2s-staggered load, but
        15/15 (100%) with 4 dispatches racing with no stagger at all — see the dispatch
        report for the full reproduction. This was NOT a pane-addressing bug: pane_id
        always stayed correctly bound to its own session (also measured, 0 mismatches
        across 3x4-way spawn-only trials); the race was purely in the shared buffer.
        A dispatch-id-scoped buffer name makes load+paste immune to any concurrently
        running sibling dispatch, because no two dispatches ever touch the same buffer.
        """
        buffer_name = f"vnx-paste-{_sanitize_session_name(dispatch_id)}"
        try:
            if len(content) > max_inline:
                tmp_path = ""
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".vnx_int_buf", delete=False, encoding="utf-8"
                    ) as fh:
                        fh.write(content)
                        tmp_path = fh.name
                    rc = self._runner.run(["load-buffer", "-b", buffer_name, tmp_path]).returncode
                finally:
                    if tmp_path:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
            else:
                rc = self._runner.run(
                    ["load-buffer", "-b", buffer_name, "-"], input_text=content
                ).returncode
            if rc != 0:
                logger.warning("interactive: load-buffer failed for %s", pane_id)
                return False
            return self._runner.run(
                ["paste-buffer", "-b", buffer_name, "-t", pane_id]
            ).returncode == 0
        finally:
            # Best-effort cleanup: a long-running fleet must not accumulate one
            # named buffer per dispatch forever. Failure here never affects the
            # paste outcome already computed above.
            self._runner.run(["delete-buffer", "-b", buffer_name])

    # -- worker-permission relay (governance, flag-gated) ------------------
    def _maybe_start_permission_relay(self, session: str, dispatch_id: str):
        """Start the worker-permission relay thread when VNX_PERMISSION_RELAY=1.

        Governance relay (see scripts/lib/worker_permission_relay.py): a DETACHED
        worker has no human to answer a Claude Code permission prompt, so it would
        silently hang. The relay polls the pane on a short interval and, per the
        operator-controlled auto-accept WINDOW + CATASTROPHIC hard-list, either
        auto-approves a routine prompt (send-keys "1" + a SEPARATE Enter to the
        EXPLICIT session) or writes an escalation record for the operator (T0) to
        surface in chat. Default off = no behavior change. Returns a RelayHandle
        or None (disabled / unavailable). Never raises.
        """
        flag = os.environ.get("VNX_PERMISSION_RELAY", "0").strip().lower()
        if flag in ("0", "false", "no", "off", ""):
            return None
        try:
            from worker_permission_relay import RelayHandle, run_relay_loop  # noqa: PLC0415

            interval = float(os.environ.get("VNX_PERMISSION_RELAY_INTERVAL", "3"))
            stop_event = threading.Event()
            thread = threading.Thread(
                target=run_relay_loop,
                args=(session, dispatch_id, self._runner, stop_event),
                kwargs={"state_dir": self._state_dir, "interval": interval},
                name=f"perm-relay-{dispatch_id}",
                daemon=True,
            )
            thread.start()
            logger.info(
                "interactive: permission relay started dispatch=%s session=%s interval=%.1fs",
                dispatch_id, session, interval,
            )
            return RelayHandle(thread=thread, stop_event=stop_event)
        except Exception as exc:  # noqa: BLE001 — relay is best-effort, never blocks dispatch
            logger.debug("interactive: permission relay start skipped (%s)", exc)
            return None

    def _kill_session(self, session: str) -> bool:
        """Kill the dispatch session. Idempotent — absent session is success."""
        res = self._runner.run(["kill-session", "-t", session])
        if res.returncode == 0:
            return True
        logger.debug(
            "interactive: kill-session %s rc=%s (likely already gone): %s",
            session,
            res.returncode,
            res.stderr.strip(),
        )
        return False

    def _attach(self, session: str) -> bool:
        """Surface the session to the operator (best-effort)."""
        if os.environ.get("TMUX"):
            return self._runner.run(["switch-client", "-t", session]).returncode == 0
        logger.info(
            "interactive: not inside tmux — attach with: tmux attach -t %s", session
        )
        return False

    # -- capture (pipe-pane) -----------------------------------------------
    def _start_pipe_pane(self, pane_id: str, dispatch_id: str) -> "Path | None":
        """Wire tmux pipe-pane to stream pane output to a raw log file.

        Gate-controlled by VNX_TMUX_CAPTURE (default "1").
        Creates the log directory if needed.
        Returns the raw log Path on success, None if disabled or on failure.
        Never raises.
        """
        capture_flag = os.environ.get("VNX_TMUX_CAPTURE", "1").strip().lower()
        if capture_flag in ("0", "false", "no", "off"):
            return None
        try:
            # Sanitize dispatch_id: reject any id that contains path separators,
            # '..' components, or characters outside [A-Za-z0-9._-].  shlex.quote
            # stops shell meta-chars but '../' still escapes the log directory.
            if not re.match(r'^[A-Za-z0-9._-]+$', dispatch_id):
                logger.warning(
                    "interactive: capture skipped — unsafe dispatch_id %r",
                    dispatch_id,
                )
                return None
            log_dir = self._state_dir.parent / "logs" / "conversations"
            log_dir.mkdir(parents=True, exist_ok=True)
            raw_log = (log_dir / f"{dispatch_id}.log").resolve()
            # Path containment guard: resolved path must stay under log_dir.
            try:
                raw_log.relative_to(log_dir.resolve())
            except ValueError:
                logger.warning(
                    "interactive: capture skipped — log path %s escaped %s",
                    raw_log,
                    log_dir,
                )
                return None
            # pipe-pane receives a single shell-command string; use shlex.quote
            # so paths with spaces or special characters are safe.
            shell_cmd = f"cat >> {shlex.quote(str(raw_log))}"
            res = self._runner.run(["pipe-pane", "-o", "-t", pane_id, shell_cmd])
            if res.returncode != 0:
                logger.warning(
                    "interactive: pipe-pane wiring failed pane=%s: %s",
                    pane_id,
                    res.stderr.strip(),
                )
                return None
            logger.debug("interactive: pipe-pane wired dispatch=%s log=%s", dispatch_id, raw_log)
            return raw_log
        except Exception as exc:  # noqa: BLE001
            logger.debug("interactive: _start_pipe_pane failed (%s)", exc)
            return None

    def _run_capture_normalizer(
        self,
        raw_log: "Path",
        terminal_id: str,
        dispatch_id: str,
        model: str,
    ) -> None:
        """Normalize the raw pipe-pane log into EventStore CanonicalEvents.

        Best-effort — never raises. Errors are logged at DEBUG level.
        Called at teardown after kill-session so the log is fully flushed.
        """
        try:
            from tmux_conversation_normalizer import normalize_conversation  # noqa: PLC0415
            from event_store import EventStore  # noqa: PLC0415
            event_store = EventStore()
            count = normalize_conversation(raw_log, event_store, terminal_id, dispatch_id, model)
            if count:
                logger.info(
                    "interactive: capture normalized %d events dispatch=%s terminal=%s",
                    count,
                    dispatch_id,
                    terminal_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "interactive: capture normalizer skipped dispatch=%s (%s)",
                dispatch_id,
                exc,
            )

    # -- receipt polling ---------------------------------------------------
    def _matching_receipts_split(
        self,
        dispatch_id: str,
        completion_statuses: frozenset,
    ) -> "tuple[list[dict], list[dict]]":
        """Return (canonical, pending) completion receipts separately.

        Signal 1 (canonical): t0_receipts.ndjson — append-only, position-stable.
        Signal 2 (pending): receipts/pending/*.json — MUTABLE (processor moves files).

        Keeping sources separate lets _wait_for_receipt apply the correct guard:
        position-based for signal 1, identity-set-based for signal 2 (P0-2).
        Each pending receipt carries ``_pending_file`` (filename) as its identity key.
        """
        canonical: list[dict] = []
        pending_out: list[dict] = []

        # Signal 1: canonical ndjson
        if self._receipts_file.exists():
            try:
                with self._receipts_file.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or dispatch_id not in line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(rec, dict):
                            continue
                        if rec.get("dispatch_id") != dispatch_id:
                            continue
                        status = (rec.get("status") or "").lower()
                        event_type = (rec.get("event_type") or "")
                        if status in completion_statuses or event_type.endswith("_completion"):
                            canonical.append(rec)
            except OSError as exc:
                logger.debug("interactive: receipts read failed: %s", exc)

        # Signal 2: raw pending receipts (P1-2: also accept event_type ending in _completion)
        pending_dir = self._state_dir / "receipts" / "pending"
        if pending_dir.is_dir():
            try:
                for pending_file in sorted(pending_dir.glob("*.json")):
                    try:
                        rec = json.loads(pending_file.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("dispatch_id") != dispatch_id:
                        continue
                    status = (rec.get("status") or "").lower()
                    event_type = (rec.get("event_type") or "")
                    if status in completion_statuses or event_type.endswith("_completion"):
                        rec.setdefault("_signal", "raw_pending")
                        rec["_pending_file"] = pending_file.name
                        pending_out.append(rec)
            except OSError as exc:
                logger.debug("interactive: pending receipts scan failed: %s", exc)

        return canonical, pending_out

    def _matching_receipts(
        self,
        dispatch_id: str,
        completion_statuses: frozenset,
    ) -> list[dict]:
        """Return parsed completion receipts for *dispatch_id* (signals 1+2), in file order.

        Signal 3 (report backstop) is handled separately in _wait_for_receipt.
        For per-source baseline guards use _matching_receipts_split directly.
        """
        canonical, pending = self._matching_receipts_split(dispatch_id, completion_statuses)
        return canonical + pending

    def _is_report_backstop_active(self, dispatch_id: str) -> bool:
        """Return True when the report is finished AND mtime-stable (P0-1).

        Requires ALL FOUR contract headings (## Summary, ## Changes, ## Verification,
        ## Open Items) AND an unchanged mtime across two consecutive calls.  A mid-task
        draft typically lacks the later headings; a report still being written keeps
        changing its mtime, so neither half-written state trips this signal.
        """
        report_path = self._state_dir.parent / "unified_reports" / f"{dispatch_id}.md"
        try:
            if not report_path.exists():
                self._report_mtime_cache.pop(dispatch_id, None)
                return False
            content = report_path.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                return False
            # All four contract headings must be present (not a mid-task draft).
            if not all(h in content for h in _REQUIRED_REPORT_HEADINGS):
                self._report_mtime_cache.pop(dispatch_id, None)
                return False
            # Mtime stability: record current mtime; require one prior stable observation.
            current_mtime = report_path.stat().st_mtime
            last_mtime = self._report_mtime_cache.get(dispatch_id)
            self._report_mtime_cache[dispatch_id] = current_mtime
            return last_mtime is not None and current_mtime == last_mtime
        except OSError:
            return False

    def _wait_for_receipt(
        self,
        dispatch_id: str,
        deadline_seconds: float,
        poll_interval: float,
        completion_statuses: frozenset,
        *,
        baseline_count: int = 0,
        baseline_pending_ids: "frozenset[str] | None" = None,
        baseline_backstop: "bool | None" = None,
        pane_id: "str | None" = None,
        label: "str | None" = None,
        raw_log_path: "Path | None" = None,
        session: str = "",
    ) -> "dict | None":
        """Poll signals 1–3 until a NEW completion appears beyond the baseline.

        *baseline_count* guards signal 1 (canonical ndjson count before delivery).
        *baseline_pending_ids* guards signal 2 (filenames of pending receipts at baseline).
          When None, falls back to legacy combined position-based baseline (backward compat).
        *baseline_backstop* guards signal 3 (True if report was backstop-active at baseline).
          When None, captured at call time (conservative default).

        *pane_id* (optional): when provided, the loop classifies the pane on each
          poll and emits ``interactive_awaiting_permission`` the first time the
          worker is seen blocked on a permission prompt (OI-863) — so a worker
          that hangs on a prompt MID-RUN is surfaced long before the deadline,
          instead of burning the full ``deadline_seconds`` invisible.

        *raw_log_path* (optional): when provided and the file exists, a
          FileProgressHeartbeat monitors the pipe-pane log for growth.  If the log
          stops growing for longer than the configured silence threshold, the
          worker is killed as stuck (OI-944, OI-1007).  When absent or None
          (e.g. VNX_TMUX_CAPTURE=0), the heartbeat is blind — the pane-content
          fallback (permission-prompt detection) is the only guard.

        *session*: tmux session name, required when raw_log_path is provided so
          the heartbeat can kill the session on silence timeout. Also required
          (together with *pane_id*) for the OI-1130 deterministic liveness probe
          below; when either is missing the probe reports "unknown" every poll
          and every decision falls open to the pre-OI-1130 heartbeat-only
          behavior.

        OI-1130 deterministic liveness (see ``_check_worker_liveness``): each
        poll, independent of heartbeat silence, the worker's tmux session/pane
        is probed for a confirmed-dead verdict. A confirmed-dead worker is
        killed immediately (reason ``worker_process_gone``) without waiting out
        the silence threshold. A confirmed-ALIVE worker is never killed on
        silence alone — silence plus alive means "thinking", not "stuck" — only
        ``deadline_seconds`` still bounds that wait. An "unknown" verdict (fail
        open) reproduces the exact pre-OI-1130 heartbeat-silence-kill behavior.

        Priority: signal 1 (canonical) > signal 2 (pending) > signal 3 (backstop).
        Returns the best matching receipt, or None on deadline.
        """
        deadline = time.monotonic() + deadline_seconds
        # Stale guard for signal 3: report must NOT have been backstop-active at baseline.
        # When baseline_backstop is None (direct caller), prime the mtime cache first so that
        # any pre-existing complete+stable report is immediately identified as stale.
        if baseline_backstop is None:
            self._is_report_backstop_active(dispatch_id)  # prime
            _bl_backstop: bool = self._is_report_backstop_active(dispatch_id)
        else:
            _bl_backstop = baseline_backstop

        # OI-944 / OI-1007: worker heartbeat — monitor the pipe-pane log for
        # growth.  If the log stops growing for longer than the configured silence
        # threshold, the worker is stuck and must be killed.
        _heartbeat = None
        if raw_log_path is not None and raw_log_path.exists():
            try:
                from worker_heartbeat import FileProgressHeartbeat  # noqa: PLC0415
                _heartbeat = FileProgressHeartbeat(
                    raw_log_path, dispatch_id,
                )
            except Exception as _hb_exc:
                logger.debug(
                    "interactive: heartbeat init failed for %s: %s",
                    dispatch_id, _hb_exc,
                )

        while True:
            if baseline_pending_ids is not None:
                # P0-2: per-source baseline — correct guard for each mutable source
                canonical, pending = self._matching_receipts_split(dispatch_id, completion_statuses)
                new_canonical = canonical[baseline_count:]
                new_pending = [
                    r for r in pending
                    if r.get("_pending_file") not in baseline_pending_ids
                ]
            else:
                # Legacy: combined position-based (backward compat for call sites that
                # do not pass baseline_pending_ids, e.g. direct test calls)
                all_matches = self._matching_receipts(dispatch_id, completion_statuses)
                new_canonical = all_matches[baseline_count:]
                new_pending = []

            # Signal 3: report backstop — last resort, only when newly active
            new_backstop: list[dict] = []
            if not _bl_backstop and self._is_report_backstop_active(dispatch_id):
                new_backstop = [{
                    "dispatch_id": dispatch_id,
                    "status": "done",
                    "event_type": "subprocess_completion",
                    "source": "report_backstop",
                    "_signal": "report_backstop",
                    "report_path": str(
                        self._state_dir.parent / "unified_reports" / f"{dispatch_id}.md"
                    ),
                }]

            # P1-1: signal 1 is authoritative; signal 2 only when no new canonical;
            # signal 3 (backstop) only when neither 1 nor 2 has a new receipt.
            if new_canonical:
                candidates = new_canonical
            elif new_pending:
                candidates = new_pending
            elif new_backstop:
                candidates = new_backstop
            else:
                if time.monotonic() >= deadline:
                    return None
                # OI-863: mid-wait permission-prompt detection.  A detached worker
                # blocked on a prompt produces no receipt; surface it the moment the
                # pane betrays it (at most once per dispatch) instead of waiting the
                # full deadline invisible.
                _awaiting_permission = False
                if pane_id is not None:
                    _pane_state = self._classify_pane(pane_id)
                    _awaiting_permission = _pane_state.is_awaiting_permission
                    if _awaiting_permission and label is not None:
                        self._emit_awaiting_permission(
                            dispatch_id, label, pane_id,
                            "permission prompt during receipt wait",
                        )
                # Heartbeat verdict, computed at most once per poll (skipped while
                # awaiting a permission prompt — see the OI-863 note at the kill
                # site below). Reused by both the OI-1130 liveness cross-check and
                # the legacy silence-kill branch so FileProgressHeartbeat.check()
                # (which mutates its own growth timer) is never called twice in
                # the same iteration.
                _hb_verdict = None
                if _heartbeat is not None and not _awaiting_permission:
                    _hb_verdict = _heartbeat.check()

                # OI-1130 follow-up: deterministic liveness probe.  Runs every
                # poll, independent of heartbeat silence, so a genuinely DEAD
                # worker is caught within one poll interval instead of waiting
                # out the full silence threshold.  See _check_worker_liveness
                # for the fail-open contract and the deliberate scope boundary.
                _liveness = (
                    self._check_worker_liveness(pane_id, session)
                    if pane_id is not None and session
                    else WorkerLiveness(alive=None, reason="no_pane_or_session")
                )

                if _liveness.alive is False:
                    if _hb_verdict is not None and not _hb_verdict.is_silent:
                        # "groeit + dood" is structurally impossible (a growing
                        # pane log implies a live writer) — the process death
                        # verdict still wins, but the contradiction is logged as
                        # an anomaly worth investigating.
                        logger.warning(
                            "interactive: liveness check found worker gone for %s "
                            "(reason=%s) while the pane log was STILL GROWING — "
                            "anomaly, but process death wins; killing anyway",
                            dispatch_id, _liveness.reason,
                        )
                    else:
                        logger.warning(
                            "interactive: deterministic liveness check found worker "
                            "gone for %s (reason=%s) — killing without waiting out "
                            "the silence threshold",
                            dispatch_id, _liveness.reason,
                        )
                    # Write a failure report so the audit trail records a
                    # confirmed-dead kill under its OWN reason — distinct from a
                    # heartbeat-silence guess (see worker_heartbeat module docs).
                    try:
                        from worker_heartbeat import build_process_gone_failure_report  # noqa: PLC0415
                        _pg_report = build_process_gone_failure_report(
                            dispatch_id,
                            liveness_reason=_liveness.reason,
                            model=model,
                            terminal_id=label or "",
                        )
                        _reports_dir = self._state_dir.parent / "unified_reports"
                        _reports_dir.mkdir(parents=True, exist_ok=True)
                        _report_path = _reports_dir / f"{dispatch_id}.md"
                        _report_path.write_text(_pg_report, encoding="utf-8")
                        logger.info(
                            "interactive: worker_process_gone failure report "
                            "written to %s",
                            _report_path,
                        )
                    except Exception as _pg_write_exc:
                        logger.warning(
                            "interactive: worker_process_gone failure report "
                            "write failed for %s: %s",
                            dispatch_id,
                            _pg_write_exc,
                        )
                    return None

                # OI-944 / OI-1007 / OI-1130: heartbeat silence check.  If the
                # pipe-pane log has stopped growing for longer than the silence
                # threshold AND the liveness probe could not confirm the worker
                # is alive (fail-open: unknown, not "alive"), the worker is
                # treated as stuck — kill it and write a terminal failure exactly
                # as before.  When the liveness probe DID confirm the process is
                # alive, a silent log means the worker is THINKING, not stuck
                # (OI-1130 regression pin: 4 of 5 deep-thinking dispatches were
                # killed on exactly this false signal) — surface it once instead
                # of killing, and let deadline_seconds remain the only bound.
                # EXCEPTION (OI-863): a recoverable awaiting_permission worker is
                # NOT a silent/stalled worker — its log has legitimately stopped
                # growing while it waits on ONE keystroke. Killing it would discard
                # a rescueable dispatch, so the heartbeat is skipped while the pane
                # shows a permission prompt (which escalates instead).
                if _hb_verdict is not None and _hb_verdict.is_silent:
                    if _liveness.alive is True:
                        if dispatch_id not in self._thinking_silent_emitted:
                            self._thinking_silent_emitted.add(dispatch_id)
                            logger.info(
                                "interactive: worker %s silent for %.0fs (threshold="
                                "%.0fs) but the liveness check confirms it is alive "
                                "— NOT killing; deadline_seconds remains the only "
                                "bound (OI-1130)",
                                dispatch_id,
                                _hb_verdict.silence_seconds,
                                _hb_verdict.threshold_seconds,
                            )
                            self._emit_event(
                                "interactive_worker_thinking_silent",
                                dispatch_id=dispatch_id,
                                label=label or "",
                                reason="pane log silent but process confirmed alive",
                                metadata={
                                    "silence_seconds": _hb_verdict.silence_seconds,
                                    "silence_threshold_seconds": _hb_verdict.threshold_seconds,
                                },
                            )
                    else:
                        if _liveness.alive is None:
                            logger.debug(
                                "interactive: liveness undeterminable for %s "
                                "(reason=%s) — falling back to the pre-OI-1130 "
                                "heartbeat-silence-kill behavior",
                                dispatch_id, _liveness.reason,
                            )
                        logger.warning(
                            "interactive: heartbeat silence detected for %s "
                            "(%.0fs silent, threshold=%.0fs) — killing worker",
                            dispatch_id,
                            _hb_verdict.silence_seconds,
                            _hb_verdict.threshold_seconds,
                        )
                        # Write a failure report so the audit trail records
                        # the heartbeat kill with the reason and timing.
                        try:
                            from worker_heartbeat import build_heartbeat_failure_report  # noqa: PLC0415
                            _hb_report = build_heartbeat_failure_report(
                                dispatch_id=dispatch_id,
                                verdict=_hb_verdict,
                                model=model,
                                terminal_id=label or "",
                            )
                            _reports_dir = (
                                self._state_dir.parent / "unified_reports"
                            )
                            _reports_dir.mkdir(parents=True, exist_ok=True)
                            _report_path = _reports_dir / f"{dispatch_id}.md"
                            _report_path.write_text(_hb_report, encoding="utf-8")
                            logger.info(
                                "interactive: heartbeat failure report written to %s",
                                _report_path,
                            )
                        except Exception as _hb_write_exc:
                            logger.warning(
                                "interactive: heartbeat failure report write "
                                "failed for %s: %s",
                                dispatch_id,
                                _hb_write_exc,
                            )
                        return None
                time.sleep(poll_interval)
                continue

            preferred = _dedup_receipts(candidates)
            receipt = preferred if preferred is not None else candidates[-1]
            logger.info(
                "interactive: completion detected dispatch=%s signal=%s status=%s",
                dispatch_id,
                (receipt.get("_signal") or receipt.get("source") or "canonical"),
                receipt.get("status"),
            )
            return receipt

    # -- handle persistence ------------------------------------------------
    def _persist_handle(self, dispatch_id: str, handle: dict) -> None:
        """Atomically persist the crash-recovery handle."""
        self._handle_dir.mkdir(parents=True, exist_ok=True)
        path = self._handle_path(dispatch_id)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(
                json.dumps(handle, indent=2, sort_keys=True), encoding="utf-8"
            )
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _remove_handle(self, dispatch_id: str) -> None:
        path = self._handle_path(dispatch_id)
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.debug(
                "interactive: handle unlink failed for %s: %s", dispatch_id, exc
            )

    # ------------------------------------------------------------------
    # Single-shot ephemeral dispatch
    # ------------------------------------------------------------------
    def dispatch(
        self,
        instruction: str,
        dispatch_id: str,
        *,
        role: "str | None" = None,
        model: str = "sonnet",
        worker_label: "str | None" = None,
        skip_permissions: "bool | None" = None,
        smart_context: "str | None" = None,
        deadline_seconds: float = 3600.0,
        poll_interval: float = 5.0,
        warmup_timeout: float = 30.0,
        warmup_poll_interval: float = 1.0,
        ready_markers: "tuple[str, ...]" = (
            # Tolerant TUI-fallback markers only — the SessionStart hook sentinel is
            # the primary readiness signal. Deliberately NOT pinned to a version
            # banner ("Claude Code vX.Y.Z" was removed: it breaks on every bump).
            "for shortcuts",
            "? for shortcuts",
            "Welcome to Claude",
            "❯",                # input prompt glyph (idle, ready for input)
        ),
        completion_statuses: frozenset = DEFAULT_COMPLETION_STATUSES,
        dispatch_paths: "list[str] | str | None" = None,
        extra_flags: str = "",
        attach: bool = False,
        isolated_worktree: bool = True,
        base_ref: str = "origin/main",
        requires_mcp: bool = False,
        working_tree_only: bool = False,
    ) -> InteractiveDispatchResult:
        """Spawn -> drive -> collect -> teardown. Single-shot; no warm-open.

        ``skip_permissions`` defaults to ``not attach``: an autonomous detached
        worker cannot answer permission prompts, while an attached (human in the
        loop) session keeps them.  Pass an explicit bool to override.

        ``requires_mcp``: when True, the worker keeps its normal ambient MCP config
        instead of the default force-empty posture (forwarded to the launch builder).
        """
        if not self._runner.available():
            return InteractiveDispatchResult(
                success=False,
                dispatch_id=dispatch_id,
                failure_reason="tmux binary not found in PATH",
            )

        if skip_permissions is None:
            skip_permissions = not attach

        # D2.2 scoping precondition (fail-closed): a working-tree-only dispatch's
        # commit/push deny only binds in the scoped detached spawn (the path where
        # _wp_build_claude_scope_args is invoked). Both the scoped posture and the
        # ADR-012 enforcement default ON (14-08 and 15-08 respectively), and either
        # predicate alone forces the scoped spawn that carries the deny — so only
        # opting out of BOTH (VNX_WORKER_BLANKET_SKIP=1 / falsy VNX_WORKER_SCOPED
        # AND VNX_WORKER_ENFORCEMENT_SKIP=1 / falsy VNX_ENFORCE_WORKER_PERMISSIONS)
        # — or an attached session — leaves the worker unscoped; reject those so an
        # unscoped working-tree-only worker can never silently reach git commit/push.
        if working_tree_only and not (
            skip_permissions
            and (worker_scoped_enabled() or worker_permission_enforcement_enabled())
        ):
            return InteractiveDispatchResult(
                success=False,
                dispatch_id=dispatch_id,
                failure_reason=(
                    "working_tree_only requires a scoped detached spawn "
                    "(both the scoped posture and ADR-012 enforcement default ON; "
                    "refusing the full opt-out path — VNX_WORKER_BLANKET_SKIP=1 / "
                    "falsy VNX_WORKER_SCOPED AND VNX_WORKER_ENFORCEMENT_SKIP=1 / "
                    "falsy VNX_ENFORCE_WORKER_PERMISSIONS — where the commit/push "
                    "deny would not bind)"
                ),
            )

        label = worker_label or dispatch_id
        session = _sanitize_session_name(f"vnx-{dispatch_id}")
        cwd = self._project_root
        start_time = time.monotonic()

        # Belt-and-suspenders: validate model before any session creation.
        if not _SAFE_MODEL_RE.match(model):
            raise ValueError(
                f"model {model!r} must be a simple identifier (e.g. 'sonnet', "
                f"'claude-opus-4-8'); whitespace and shell metacharacters are not allowed"
            )

        # F1.1: pre-assign the worker's Claude session id when the session-linkage
        # flag is on. uuid4 is always valid; a None value keeps the launch line and
        # pane env byte-for-byte unchanged when the flag is off.
        session_uuid: "str | None" = None
        if os.environ.get("VNX_TMUX_SESSION_ID", "").strip().lower() in ("1", "true"):
            session_uuid = str(uuid.uuid4())

        # Worktree isolation: allocate before session creation so a failed add
        # never spawns a tmux session with an uncontrolled cwd.
        worktree_handle: "WorktreeHandle | None" = None
        _wt_state: "list[str | None]" = [None]
        # Set by the pre-govern auto-PR-enforcement check (success path only) so
        # _teardown's classify() call is memoized rather than re-run — classify()
        # is read-only/idempotent but callers (e.g. tests) assert it runs exactly
        # once per dispatch.
        _wt_classification: "list[str | None]" = [None]
        _raw_log: "list[Path | None]" = [None]
        # Worker-permission relay handle (flag-gated); stopped in _teardown.
        _relay_handle: "list[object | None]" = [None]

        if isolated_worktree:
            try:
                worktree_handle = allocate(
                    dispatch_id=dispatch_id,
                    base_ref=base_ref,
                    repo_root=self._project_root,
                )
                cwd = worktree_handle.path
                if os.environ.get("VNX_BENCH_SEED_MATERIALIZE") == "1":
                    from benchmark_worker_isolation import materialize_benchmark_seed  # noqa: PLC0415

                    cwd = materialize_benchmark_seed(cwd, dispatch_paths)
            except WorktreeAllocateError as exc:
                self._emit_event(
                    "interactive_worktree_add_failed",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason=str(exc),
                )
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    label=label,
                    failure_reason=f"worktree_add_failed: {exc}",
                    duration_seconds=time.monotonic() - start_time,
                )
            except RuntimeError as exc:
                if worktree_handle is not None:
                    try:
                        reap(worktree_handle, classify(worktree_handle))
                    except Exception as cleanup_exc:  # noqa: BLE001
                        logger.warning(
                            "interactive: failed to clean materialization-error "
                            "worktree for %s: %s",
                            dispatch_id,
                            cleanup_exc,
                        )
                self._emit_event(
                    "interactive_benchmark_seed_materialization_failed",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason=str(exc),
                )
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    label=label,
                    failure_reason=f"benchmark_seed_materialization_failed: {exc}",
                    duration_seconds=time.monotonic() - start_time,
                    worktree_path=str(worktree_handle.path) if worktree_handle else None,
                )

            if worktree_handle is not None:
                # Worker-scope PreToolUse enforcement hook (gated by
                # VNX_ENFORCE_WORKER_PERMISSIONS, default ON since 15-08;
                # spike E1/E2): register the hook in the fresh worktree BEFORE
                # the tmux session spawns so cwd-based settings discovery has it
                # from the first tool call. Best-effort: the hook is fail-open
                # anyway, so a failed write must never abort the dispatch.
                try:
                    # OI-1161: pass the main checkout root so the worktree's
                    # settings.local.json also carries the project permissions a
                    # worker would have had in the main checkout.
                    _write_worker_scope_hook_settings(Path(cwd), self._project_root)
                    # OI-804 (ADR-005 audit gap): a successful state mutation —
                    # the settings.local.json registration — emits a coordination
                    # event so the write lands in the audit trail. Best-effort
                    # like the write itself: an emit failure must never abort the
                    # dispatch.
                    self._emit_event(
                        "hook_settings_written",
                        dispatch_id=dispatch_id,
                        label=label,
                        reason="worker-scope PreToolUse hook registered in worktree",
                        metadata={
                            "settings_path": str(
                                Path(cwd) / ".claude" / "settings.local.json"
                            )
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - hook wiring is best-effort
                    logger.warning(
                        "interactive: worker-scope hook settings write failed "
                        "for %s: %s",
                        dispatch_id,
                        exc,
                    )

        # Per-dispatch hook signal dir: the SessionStart / UserPromptSubmit / Stop
        # hooks (guarded by VNX_TMUX_SIGNAL_DIR + VNX_DISPATCH_ID) drop sentinels
        # here. Best-effort: if mkdtemp fails the lane silently degrades to the
        # TUI-marker fallback (sentinel checks just never fire).
        signal_dir: "Path | None" = None
        try:
            signal_dir = Path(tempfile.mkdtemp(prefix="vnx-tmux-sig-"))
        except OSError as exc:
            logger.debug("interactive: signal dir mkdtemp failed (%s); TUI fallback only", exc)

        # Idempotency guard: teardown runs exactly once across all exit paths.
        _torn_down = False

        def _teardown(status: str) -> None:
            nonlocal _torn_down
            if _torn_down:
                return
            _torn_down = True
            # Stop the worker-permission relay thread (flag-gated; may be None).
            if _relay_handle[0] is not None:
                try:
                    _relay_handle[0].stop_event.set()
                    _relay_handle[0].thread.join(timeout=5)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("interactive: relay stop failed (%s)", exc)
            if signal_dir is not None:
                try:
                    shutil.rmtree(signal_dir, ignore_errors=True)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("interactive: signal dir cleanup failed (%s)", exc)
            try:
                self._kill_session(session)
            except Exception as exc:  # noqa: BLE001
                logger.debug("interactive: teardown kill-session %s: %s", session, exc)
            # Normalize captured conversation into EventStore (best-effort, after kill
            # so pipe-pane has flushed its final bytes to the log).
            if _raw_log[0] is not None:
                try:
                    self._run_capture_normalizer(_raw_log[0], label, dispatch_id, model)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("interactive: teardown normalizer dispatch=%s: %s", dispatch_id, exc)
            if worktree_handle is not None:
                try:
                    cls = (
                        _wt_classification[0]
                        if _wt_classification[0] is not None
                        else classify(worktree_handle)
                    )
                    reap_result = reap(worktree_handle, cls)
                    _wt_state[0] = cls
                    self._emit_event(
                        "interactive_teardown_worktree",
                        dispatch_id=dispatch_id,
                        label=label,
                        metadata={
                            "worktree_state": cls,
                            "branch_kept_local": reap_result.branch_kept_local,
                            "branch_kept_remote": reap_result.branch_kept_remote,
                            "preserved_path": str(reap_result.preserved_path)
                            if reap_result.preserved_path
                            else None,
                        },
                    )
                    if cls == "dirty":
                        self._emit_event(
                            "interactive_teardown_preserved",
                            dispatch_id=dispatch_id,
                            label=label,
                            metadata={"preserved_path": str(reap_result.preserved_path)},
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "interactive: worktree reap failed for %s: %s", dispatch_id, exc
                    )
            try:
                self._remove_handle(dispatch_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("interactive: teardown remove-handle %s: %s", dispatch_id, exc)
            try:
                self._emit_event(
                    "interactive_exit",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason=f"status={status}",
                    metadata={"session": session},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("interactive: teardown emit %s: %s", dispatch_id, exc)

        # FIX 2: Global teardown envelope starts before _spawn_session so any
        # exception after a session may exist still triggers teardown.
        pane_id: "str | None" = None
        window_id: "str | None" = None
        try:
            # 1. Spawn detached session
            _spawn_dispatch_paths: "list[str] | None"
            if isinstance(dispatch_paths, str):
                _spawn_dispatch_paths = [dispatch_paths]
            elif dispatch_paths:
                _spawn_dispatch_paths = list(dispatch_paths)
            else:
                _spawn_dispatch_paths = None
            spawned = self._spawn_session(
                session,
                cwd,
                dispatch_id,
                session_uuid=session_uuid,
                role=role,
                dispatch_paths=_spawn_dispatch_paths,
            )
            if spawned is None:
                self._emit_event(
                    "interactive_spawn_failed",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason="tmux new-session failed",
                )
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    failure_reason="tmux new-session failed",
                    duration_seconds=time.monotonic() - start_time,
                )

            pane_id, window_id = spawned

            # 2. Persist handle for crash-recovery / operator tmux attach
            self._persist_handle(
                dispatch_id,
                {
                    "dispatch_id": dispatch_id,
                    "label": label,
                    "session": session,
                    "pane_id": pane_id,
                    "window_id": window_id,
                    "started_at": time.time(),
                    "worktree_path": str(worktree_handle.path) if worktree_handle else None,
                    "branch": worktree_handle.branch if worktree_handle else None,
                    "base_sha": worktree_handle.base_sha if worktree_handle else None,
                },
            )
            self._emit_event(
                "interactive_spawn",
                dispatch_id=dispatch_id,
                label=label,
                reason=f"spawned interactive claude in {session}",
                metadata={"session": session, "pane_id": pane_id, "window_id": window_id},
            )

            # 2b. Clear/archive EventStore terminal stream for this dispatch (mirror
            # subprocess adapter behavior) so normalized events form a clean stream.
            try:
                from event_store import EventStore  # noqa: PLC0415
                _pre_es = EventStore()
                _prev_ev = _pre_es.last_event(label)
                _prev_did = (_prev_ev or {}).get("dispatch_id") or None
                _pre_es.clear(label, archive_dispatch_id=_prev_did)
            except Exception as _pre_exc:  # noqa: BLE001
                logger.debug("interactive: pre-capture EventStore clear failed (%s)", _pre_exc)

            # Wire pipe-pane capture (before claude launch so the full session is logged).
            _raw_log[0] = self._start_pipe_pane(pane_id, dispatch_id)

            # 3. Build launch command
            launch_cmd = self._launch_builder(
                model,
                skip_permissions=skip_permissions,
                extra_flags=extra_flags,
                role=role,
                requires_mcp=requires_mcp,
                working_tree_only=working_tree_only,
                session_uuid=session_uuid,
            )

            # FIX 1: Final-command guard — bites regardless of how the command
            # was built (default builder, custom launch_builder, model injection).
            try:
                _assert_no_headless_flags(launch_cmd)
            except ValueError:
                self._emit_event(
                    "interactive_launch_failed",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason="headless flag detected in launch command",
                    metadata={"session": session},
                )
                _teardown("headless_flag_blocked")
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    window_id=window_id,
                    pane_id=pane_id,
                    failure_reason="headless_flag_blocked",
                    duration_seconds=time.monotonic() - start_time,
                )

            # OI-864: classify the permission posture from the REAL flags this
            # spawn is about to launch with — not by re-reading
            # VNX_ENFORCE_WORKER_PERMISSIONS/VNX_WORKER_SCOPED a second time (two
            # independent env reads can diverge from what the builder actually
            # assembled). Computed once here from the guard-validated launch_cmd
            # and threaded to both the completion-protocol receipt (worker-authored
            # path) and every govern()/ensure_receipt call (synthesized fallback
            # path) below, so both receipt paths agree on the same single source
            # of truth.
            try:
                _launch_tokens = shlex.split(launch_cmd)
            except ValueError:
                _launch_tokens = launch_cmd.split()
            permission_posture = classify_permission_posture(_launch_tokens, role)

            # Inject hook-signal env so the worker's SessionStart/UserPromptSubmit/Stop
            # hooks fire (they are guarded by these two vars). Prepended as an export
            # so it applies across the whole compound command (source …; claude …),
            # not just the first segment. Validated after the headless guard so the
            # guard only ever inspects the pure builder output.
            if signal_dir is not None:
                launch_cmd = (
                    f"export VNX_TMUX_SIGNAL_DIR={shlex.quote(str(signal_dir))} "
                    f"VNX_DISPATCH_ID={shlex.quote(dispatch_id)}; {launch_cmd}"
                )

            # OI-1126: verify the captured pane_id still belongs to THIS dispatch's
            # session immediately before the first content is delivered to it. Cheap
            # (~one tmux round-trip) defense-in-depth on top of the per-dispatch named
            # buffer fix below — makes delivery to a sibling dispatch's pane impossible
            # to do silently, not merely unlikely.
            if not self._verify_pane_identity(pane_id, session):
                self._emit_event(
                    "interactive_pane_identity_mismatch",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason="pane_id no longer bound to this dispatch's session before launch",
                    metadata={"session": session, "pane_id": pane_id},
                )
                _teardown("pane_identity_mismatch")
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    window_id=window_id,
                    pane_id=pane_id,
                    failure_reason="pane_identity_mismatch_before_launch",
                    duration_seconds=time.monotonic() - start_time,
                )

            if not self._launch_claude(pane_id, launch_cmd):
                self._emit_event(
                    "interactive_launch_failed",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason="send-keys for claude launch failed",
                    metadata={"session": session},
                )
                _teardown("launch_failed")
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    window_id=window_id,
                    pane_id=pane_id,
                    failure_reason="failed to launch interactive claude",
                    duration_seconds=time.monotonic() - start_time,
                )

            # 4. Wait for readiness; STRICT mode aborts if not ready before warmup_timeout.
            _ready = self._wait_ready(
                pane_id,
                ready_markers=ready_markers,
                warmup_timeout=warmup_timeout,
                poll_interval=warmup_poll_interval,
                signal_dir=signal_dir,
            )
            # F1.1: verify the pre-assigned session id actually took (hook-reported id
            # must match). Fail-open: only emit an audit event, never block dispatch.
            self._verify_session_id(
                signal_dir,
                session_uuid,
                dispatch_id=dispatch_id,
                label=label,
            )
            _strict = os.environ.get("VNX_TMUX_READY_STRICT", "1").strip().lower() not in (
                "0", "false", "no", "off"
            )
            if not _ready and _strict:
                self._emit_event(
                    "interactive_ready_timeout",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason="no readiness marker before warmup_timeout; VNX_TMUX_READY_STRICT=1",
                    metadata={"session": session, "pane_id": pane_id},
                )
                self._govern_report(
                    dispatch_id=dispatch_id,
                    terminal_id=label,
                    instruction=instruction,
                    receipt=None,
                    duration_seconds=time.monotonic() - start_time,
                    base_sha=worktree_handle.base_sha if worktree_handle else None,
                    worktree_path=worktree_handle.path if worktree_handle else None,
                    model=model,
                    failure_reason="interactive_ready_timeout",
                    role=role,
                    session_id=session_uuid,
                    permission_posture=permission_posture,
                )
                _teardown("ready_timeout")
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    window_id=window_id,
                    pane_id=pane_id,
                    failure_reason="interactive_ready_timeout",
                    duration_seconds=time.monotonic() - start_time,
                )

            # OI-877: record the worker's process group(s) so teardown can find
            # dispatch processes that escape the worktree (their repo-root
            # resolves to the main checkout).  Capture at readiness — AFTER the
            # SessionStart hooks have fired, so any hook-spawned background
            # process is already a member of the captured groups and stays
            # re-findable at teardown even after reparenting (PPID 1).  The
            # pane shell's own group is excluded: only the worker's groups are
            # recorded, never the dispatcher's.  Best-effort: a failed capture
            # degrades to worktree-scan-only teardown.
            if _ready and worktree_handle is not None:
                try:
                    from dispatch_process_registry import (  # noqa: PLC0415
                        collect_descendant_pgids,
                        record_dispatch_pgids,
                    )
                    _pane_pid_res = self._runner.run(
                        ["display-message", "-p", "-t", pane_id, "#{pane_pid}"]
                    )
                    _pane_pid_str = (
                        (_pane_pid_res.stdout or "").strip()
                        if _pane_pid_res.returncode == 0
                        else ""
                    )
                    if _pane_pid_str.isdigit():
                        _pane_pid = int(_pane_pid_str)
                        _worker_pgids = collect_descendant_pgids(_pane_pid)
                        try:
                            _worker_pgids.discard(os.getpgid(_pane_pid))
                        except (ProcessLookupError, PermissionError):
                            pass
                        if _worker_pgids:
                            record_dispatch_pgids(
                                dispatch_id,
                                sorted(_worker_pgids),
                                repo_root=self._project_root,
                            )
                except Exception as _pgid_exc:
                    logger.warning(
                        "interactive: dispatch pgid capture failed for %s: %s",
                        dispatch_id,
                        _pgid_exc,
                    )

            if attach:
                self._attach(session)

            # 5. Baseline snapshot BEFORE delivery (F3: stale-receipt guard)
            # P0-2: capture per-source baselines so pending file removals don't drop completions.
            _bl_canonical, _bl_pending = self._matching_receipts_split(dispatch_id, completion_statuses)
            baseline = len(_bl_canonical)
            baseline_pending_ids: frozenset[str] = frozenset(
                r.get("_pending_file", "") for r in _bl_pending
            )
            # P0-1: double-call to prime the mtime cache — any pre-existing complete+stable report
            # is immediately identified as stale (same mtime across both rapid calls → True → blocked).
            self._is_report_backstop_active(dispatch_id)
            baseline_backstop = self._is_report_backstop_active(dispatch_id)

            # 6. Assemble body (skill body + intelligence + instruction via enrichers)
            _context_body = self._assemble_context(
                role=role,
                smart_context=smart_context,
                terminal_id=label,
                dispatch_id=dispatch_id,
                instruction=instruction,
                dispatch_paths=dispatch_paths,
            )
            # 6b. Input-side audit closure: persist the enriched final prompt (the
            # body BEFORE the lane's deterministic delivery wrappers) + verify the
            # raw instruction and recorded intelligence injections reconstruct it.
            # The result is baked into the worker's completion-protocol receipt.
            _integrity = self._record_final_prompt_integrity(
                dispatch_id=dispatch_id,
                final_prompt=_context_body,
                raw_instruction=instruction,
            )

            if os.environ.get("VNX_BENCH_EQUAL_CONTEXT") == "1":
                body = _context_body
            elif os.environ.get("VNX_SHARED_PREPARE", "0").strip().lower() in (
                "1", "true", "yes", "on"
            ):
                # prepare() already includes scope-note; add completion-protocol
                # then trailer as the ABSOLUTE LAST content.
                try:
                    from dispatch_prepare import END_OF_INSTRUCTION_SENTINEL as _TRAILER  # noqa: PLC0415
                except ImportError:
                    _TRAILER = "<!-- VNX-END-OF-INSTRUCTION -->"
                body = (
                    _context_body
                    + self._build_completion_protocol(
                        dispatch_id, label, model=model, integrity=_integrity,
                        permission_posture=permission_posture,
                    )
                    + f"\n\n{_TRAILER}\n"
                )
            else:
                # Legacy path: scope-note + completion-protocol, no trailer.
                body = (
                    _context_body
                    + self._scope_note(dispatch_paths)
                    + self._build_completion_protocol(
                        dispatch_id, label, model=model, integrity=_integrity,
                        permission_posture=permission_posture,
                    )
                )

            # 7. Deliver instruction
            self._emit_event(
                "interactive_deliver_start",
                dispatch_id=dispatch_id,
                label=label,
                reason="send-keys dispatch instruction",
                metadata={"session": session, "pane_id": pane_id},
            )
            # OI-1126: re-verify identity right before the instruction payload itself
            # goes out — this is the delivery the reproduction showed was actually at
            # risk (via the shared tmux paste buffer, now fixed below in _paste()).
            if not self._verify_pane_identity(pane_id, session):
                self._emit_event(
                    "interactive_pane_identity_mismatch",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason="pane_id no longer bound to this dispatch's session before instruction delivery",
                    metadata={"session": session, "pane_id": pane_id},
                )
                _teardown("pane_identity_mismatch")
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    window_id=window_id,
                    pane_id=pane_id,
                    failure_reason="pane_identity_mismatch_before_deliver",
                    duration_seconds=time.monotonic() - start_time,
                )

            if not self._deliver_instruction(pane_id, body, dispatch_id):
                self._emit_event(
                    "interactive_deliver_failed",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason="send-keys/paste of instruction failed",
                    metadata={"session": session},
                )
                _teardown("deliver_failed")
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    window_id=window_id,
                    pane_id=pane_id,
                    failure_reason="failed to deliver instruction via send-keys",
                    duration_seconds=time.monotonic() - start_time,
                )

            # 7b. Verify the instruction was actually submitted (not left staged).
            if not self._verify_submit(pane_id, body, signal_dir=signal_dir):
                self._emit_event(
                    "interactive_submit_failed",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason="instruction still staged after paste-enter-verify-retry-timeout",
                    metadata={"session": session, "pane_id": pane_id},
                )
                self._govern_report(
                    dispatch_id=dispatch_id,
                    terminal_id=label,
                    instruction=instruction,
                    receipt=None,
                    duration_seconds=time.monotonic() - start_time,
                    base_sha=worktree_handle.base_sha if worktree_handle else None,
                    worktree_path=worktree_handle.path if worktree_handle else None,
                    model=model,
                    failure_reason="submit_failed",
                    role=role,
                    session_id=session_uuid,
                    permission_posture=permission_posture,
                )
                _teardown("submit_failed")
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    window_id=window_id,
                    pane_id=pane_id,
                    failure_reason="submit_failed",
                    duration_seconds=time.monotonic() - start_time,
                    worktree_state=_wt_state[0],
                )

            # 7b-alt. OI-1126 worker-side detectability: the UserPromptSubmit hook
            # (tmux_signal_prompt_received.sh) independently compares the dispatch_id
            # embedded in the delivered prompt's completion-protocol JSON against
            # VNX_DISPATCH_ID (set race-free at spawn/launch time, never via the shared
            # paste buffer) and drops a "dispatch_id_mismatch" sentinel when they
            # disagree. The worker's own pane is the last line of defence against a
            # delivery bug anywhere upstream of it — fail loud immediately rather than
            # let the dispatch run out its deadline against content that isn't its own.
            if signal_dir is not None:
                _mismatch_sentinel = signal_dir / "dispatch_id_mismatch"
                if _mismatch_sentinel.exists():
                    _mismatch_detail = ""
                    try:
                        _mismatch_detail = _mismatch_sentinel.read_text(encoding="utf-8").strip()
                    except OSError:
                        pass
                    self._emit_event(
                        "interactive_dispatch_id_mismatch",
                        dispatch_id=dispatch_id,
                        label=label,
                        reason=_mismatch_detail or "worker-side dispatch id mismatch detected",
                        metadata={"session": session, "pane_id": pane_id},
                    )
                    self._govern_report(
                        dispatch_id=dispatch_id,
                        terminal_id=label,
                        instruction=instruction,
                        receipt=None,
                        duration_seconds=time.monotonic() - start_time,
                        base_sha=worktree_handle.base_sha if worktree_handle else None,
                        worktree_path=worktree_handle.path if worktree_handle else None,
                        model=model,
                        failure_reason="dispatch_id_mismatch_detected",
                        role=role,
                        session_id=session_uuid,
                        permission_posture=permission_posture,
                    )
                    _teardown("dispatch_id_mismatch_detected")
                    return InteractiveDispatchResult(
                        success=False,
                        dispatch_id=dispatch_id,
                        session=session,
                        label=label,
                        window_id=window_id,
                        pane_id=pane_id,
                        failure_reason="dispatch_id_mismatch_detected",
                        duration_seconds=time.monotonic() - start_time,
                        worktree_state=_wt_state[0],
                    )

            # 7b-bis. Confirm the worker actually STARTED working. A verified submit can
            # still clear the input box without the worker progressing under subscription
            # load; without this gate the lane would wait the full deadline for a receipt
            # that never comes (the warmup-miss/no-progress hang in DISPATCH_RULES §7).
            # Fast-abort (retryable in seconds) instead of burning deadline_seconds.
            # baseline / baseline_pending_ids are the PRE-DELIVERY snapshot (step 5),
            # so a pre-existing/stale receipt is never miscounted as fresh progress.
            _work_start = self._await_work_started(
                pane_id,
                dispatch_id,
                signal_dir=signal_dir,
                baseline_count=baseline,
                baseline_pending_ids=baseline_pending_ids,
                completion_statuses=completion_statuses,
                label=label,
            )
            if _work_start == WORK_START_AWAITING_PERMISSION:
                # OI-863: the worker is ALIVE, blocked on one permission prompt.  Do
                # NOT fast-abort — a single relayed answer saves it.  Fall through to
                # the relay (answers it when a window is open) and the receipt wait
                # (whose deadline path re-classifies the pane if nothing resolves it).
                logger.warning(
                    "interactive: dispatch %s worker awaiting permission at work-start; "
                    "proceeding to relay+wait instead of fast-aborting",
                    dispatch_id,
                )
            if _work_start == WORK_START_NO_PROGRESS:
                # Capture the pane tail so operators can see WHY no work was observed
                # (idle prompt, permission prompt, error) and tune the heuristic.
                _pane_tail = ""
                try:
                    _cap = self._runner.run(["capture-pane", "-t", pane_id, "-p"])
                    if _cap.returncode == 0 and _cap.stdout:
                        _pane_tail = _cap.stdout[-600:]
                except Exception as _cap_exc:  # noqa: BLE001
                    logger.debug("interactive: no-progress pane capture failed (%s)", _cap_exc)
                self._emit_event(
                    "interactive_no_progress",
                    dispatch_id=dispatch_id,
                    label=label,
                    reason="worker never started working after submit; fast-abort before deadline",
                    metadata={"session": session, "pane_id": pane_id, "pane_tail": _pane_tail},
                )
                self._govern_report(
                    dispatch_id=dispatch_id,
                    terminal_id=label,
                    instruction=instruction,
                    receipt=None,
                    duration_seconds=time.monotonic() - start_time,
                    base_sha=worktree_handle.base_sha if worktree_handle else None,
                    worktree_path=worktree_handle.path if worktree_handle else None,
                    model=model,
                    failure_reason="interactive_no_progress",
                    role=role,
                    session_id=session_uuid,
                    permission_posture=permission_posture,
                )
                _teardown("no_progress")
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    window_id=window_id,
                    pane_id=pane_id,
                    failure_reason="interactive_no_progress",
                    duration_seconds=time.monotonic() - start_time,
                    worktree_state=_wt_state[0],
                )

            # 7c. Start the governance permission relay (flag-gated, default off)
            # so a permission prompt raised DURING the run is auto-approved (open
            # window) or escalated to the operator instead of silently hanging.
            _relay_handle[0] = self._maybe_start_permission_relay(session, dispatch_id)

            # 8. Wait for receipt
            receipt = self._wait_for_receipt(
                dispatch_id,
                deadline_seconds,
                poll_interval,
                completion_statuses,
                baseline_count=baseline,
                baseline_pending_ids=baseline_pending_ids,
                baseline_backstop=baseline_backstop,
                pane_id=pane_id,
                label=label,
                raw_log_path=_raw_log[0],
                session=session,
            )

            if receipt is None:
                # OI-863: classify the pane at deadline.  A worker STILL blocked on a
                # permission prompt is not a plain deadline — record it distinctly as
                # awaiting_permission so the audit trail shows a recoverable state,
                # not a silent hang.
                _deadline_state = self._classify_pane(pane_id)
                _deadline_reason = (
                    "tmux_awaiting_permission"
                    if _deadline_state.is_awaiting_permission
                    else "tmux_receipt_deadline_exceeded"
                )
                if _deadline_state.is_awaiting_permission:
                    self._emit_awaiting_permission(
                        dispatch_id, label, pane_id,
                        "still awaiting permission at receipt deadline",
                    )
                self._govern_report(
                    dispatch_id=dispatch_id,
                    terminal_id=label,
                    instruction=instruction,
                    receipt=None,
                    duration_seconds=time.monotonic() - start_time,
                    base_sha=worktree_handle.base_sha if worktree_handle else None,
                    worktree_path=worktree_handle.path if worktree_handle else None,
                    model=model,
                    failure_reason=_deadline_reason,
                    role=role,
                    session_id=session_uuid,
                    permission_posture=permission_posture,
                )
                _teardown("timeout")
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    window_id=window_id,
                    pane_id=pane_id,
                    failure_reason=(
                        "awaiting_permission"
                        if _deadline_state.is_awaiting_permission
                        else "receipt deadline exceeded"
                    ),
                    duration_seconds=time.monotonic() - start_time,
                    worktree_state=_wt_state[0],
                    worktree_path=str(worktree_handle.path) if worktree_handle else None,
                )

            self._emit_event(
                "interactive_receipt_observed",
                dispatch_id=dispatch_id,
                label=label,
                reason=f"receipt status={receipt.get('status')}",
                metadata={"session": session, "status": receipt.get("status")},
            )
            # Best-effort token usage parsed from the pane TUI counter (no usage API on
            # the subscription lane) — used as a frontmatter fallback AND as
            # corroborating detail for the fail-loud check below.
            _pane_tokens = self._parse_token_usage_from_log(_raw_log[0])
            # P0.3: fail loud on an empty extraction BEFORE trusting the worker's own
            # "done" claim — see _fail_loud_on_empty_extraction docstring.
            receipt = self._fail_loud_on_empty_extraction(
                dispatch_id=dispatch_id,
                receipt=receipt,
                role=role,
                worktree_path=worktree_handle.path if worktree_handle else None,
                base_sha=worktree_handle.base_sha if worktree_handle else None,
                pane_tokens=_pane_tokens,
            )
            worker_succeeded = receipt is not None and receipt.get("status") not in (
                "failed",
                "blocked",
            )
            # Auto-PR enforcement (rij-7, lane-matrix): a build worker that committed
            # its dispatch branch but never pushed it, OR pushed it but never ran
            # `gh pr create`, leaves T0 to salvage the work by hand every time. The
            # per-state decision lives in pr_enforcement.enforce_pr_exists (the ONE
            # binding site — never duplicated): `committed` → push then PR, `pushed`
            # → PR, `clean` → not applicable, `dirty` → split into substantive
            # (salvaged + loud, OI-1119/OI-1127) vs scratch. Runs BEFORE govern() so a push
            # or creation failure is reflected in the governed report and
            # InteractiveDispatchResult.success — never a silent "done" with work
            # stranded locally or on origin.
            _autopr_failure_reason: "str | None" = None
            if worker_succeeded and worktree_handle is not None:
                _wt_classification[0] = classify(worktree_handle)
                autopr_result = self._enforce_pr_exists(
                    dispatch_id=dispatch_id,
                    label=label,
                    worktree_handle=worktree_handle,
                    worktree_state=_wt_classification[0],
                )
                if not autopr_result.ok:
                    worker_succeeded = False
                    _autopr_failure_reason = autopr_result.reason
                    receipt = dict(receipt)
                    receipt["status"] = "failed"
                    receipt["autopr_rejected"] = True
                    receipt["autopr_reason"] = autopr_result.reason
            emitted_report = self._govern_report(
                dispatch_id=dispatch_id,
                terminal_id=label,
                instruction=instruction,
                receipt=receipt,
                duration_seconds=time.monotonic() - start_time,
                base_sha=worktree_handle.base_sha if worktree_handle else None,
                worktree_path=worktree_handle.path if worktree_handle else None,
                model=model,
                token_usage=_pane_tokens,
                role=role,
                session_id=session_uuid,
                permission_posture=permission_posture,
                **(
                    {
                        "failure_reason": (
                            f"dispatch_branch_no_pr "
                            f"(state={_wt_classification[0]}): {_autopr_failure_reason}"
                        ),
                    }
                    if _autopr_failure_reason
                    else {}
                ),
            )
            # A governed-completion path (worker OK) with no linked report is an
            # audit-trail gap — do not report success with an unlinked report.
            if worker_succeeded and emitted_report is None:
                logger.warning(
                    "interactive: governed dispatch %s succeeded but unified_report "
                    "emit failed — marking degraded",
                    dispatch_id,
                )
            success = worker_succeeded and emitted_report is not None
            _teardown("success" if success else "worker_status_failed")
            return InteractiveDispatchResult(
                success=success,
                dispatch_id=dispatch_id,
                session=session,
                label=label,
                window_id=window_id,
                pane_id=pane_id,
                receipt=receipt,
                failure_reason=(
                    None if success else (
                        "unified_report_emit_failed"
                        if worker_succeeded
                        else (
                            f"pushed_branch_no_pr: {_autopr_failure_reason}"
                            if _autopr_failure_reason
                            else f"worker_status: {receipt.get('status')}"
                        )
                    )
                ),
                duration_seconds=time.monotonic() - start_time,
                worktree_state=_wt_state[0],
                worktree_path=str(worktree_handle.path) if worktree_handle else None,
            )

        except Exception as _exc:  # noqa: BLE001
            # Unexpected error (e.g. _persist_handle raises): convert to failure
            # result so the caller always gets a structured outcome.
            logger.warning(
                "interactive: unexpected error in dispatch %s: %s", dispatch_id, _exc
            )
            _teardown("unexpected_error")
            return InteractiveDispatchResult(
                success=False,
                dispatch_id=dispatch_id,
                session=session,
                label=label,
                window_id=window_id,
                pane_id=pane_id,
                failure_reason="unexpected_error",
                duration_seconds=time.monotonic() - start_time,
                worktree_state=_wt_state[0],
                worktree_path=str(worktree_handle.path) if worktree_handle else None,
            )
        finally:
            # No-op if teardown already ran; catches any remaining exit path.
            _teardown("exception")


# ---------------------------------------------------------------------------
# CLI — single-shot dispatch entry point
# ---------------------------------------------------------------------------
def _resolve_state_dir() -> Path:
    """Resolve the lane's runtime state dir, honoring the explicit override and
    otherwise anchoring on the INVOCATION project root — never the lane code's
    on-disk location.

    In central-install mode the lane code lives under
    ``~/.vnx-system/versions/<v>/scripts/lib/``. Deriving the state root from
    ``__file__`` collapsed every plan-gate receipt/report into the version-dir's
    local ``.vnx-data`` (OI-900) — a tree ``vnx update`` later pruned (OI-912),
    destroying the audit trail.

    Resolution order:
      1. ``VNX_DATA_DIR_EXPLICIT=1`` + ``VNX_DATA_DIR`` — explicit override
         (test isolation / CI / worktree isolation), same two-key contract as
         ``project_root.resolve_state_dir``.
      2. The invocation project root (VNX_PROJECT_ROOT shim > CWD git > lane
         repo — the same chain as ``_resolve_invocation_project_root``) is the
         operator's project, so its ``.vnx-data/state`` is the correct runtime
         root and matches what ``append_receipt`` resolves from that context.
    """
    explicit_flag = os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1"
    explicit_val = os.environ.get("VNX_DATA_DIR", "")
    if explicit_flag and explicit_val:
        return Path(explicit_val).expanduser().resolve() / "state"
    from vnx_paths import resolve_state_dir as resolve_vnx_state_dir
    return resolve_vnx_state_dir(_resolve_invocation_project_root())


def _resolve_invocation_project_root() -> Path:
    """Resolve the PROJECT repo root from the INVOCATION CONTEXT, not the lane
    code's on-disk location.

    In central-install mode the lane code lives under
    ``~/.vnx-system/current/scripts/lib/``, so ``Path(__file__).parents[2]``
    resolves to the shared VNX keystone, NOT the operator's project. The worker
    must be spawned in the project (``cwd`` / worktree ``repo_root``), so resolve
    from the invocation context instead:

      1. ``VNX_PROJECT_ROOT`` env var (exported by the central-install shim) when
         it points at a real directory.
      2. The invoking CWD's git top-level.
      3. Last resort: the lane code's own repo root (``parents[2]``) — correct for
         the embedded/dev-checkout layout where code and project coincide.

    Deliberately does NOT pass ``caller_file=__file__`` to the shared resolver:
    that would re-introduce the keystone bug via its physical-location candidate.
    """
    raw = os.environ.get("VNX_PROJECT_ROOT", "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            return candidate.resolve()

    try:
        out = subprocess.check_output(
            ["git", "-C", str(Path.cwd()), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return Path(out).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass

    return TmuxInteractiveDispatch._resolve_project_root()


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Interactive-tmux Claude dispatch lane — single-shot ephemeral"
    )
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--role", default=None)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--worker-label", default=None)
    parser.add_argument("--deadline-seconds", type=float, default=3600.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--warmup-timeout", type=float, default=30.0)
    parser.add_argument("--dispatch-paths", nargs="*", default=None)
    parser.add_argument("--skip-permissions", action="store_true", default=None)
    parser.add_argument("--extra-flags", default="")
    parser.add_argument("--attach", action="store_true")
    parser.add_argument(
        "--working-tree-only", action="store_true", default=False,
        dest="working_tree_only",
        help="Plan-review/plan-write dispatch: deny git commit/push (scoped spawn "
             "required; the dispatch is rejected if it would run unscoped).",
    )
    wt_group = parser.add_mutually_exclusive_group()
    wt_group.add_argument(
        "--isolated-worktree",
        dest="isolated_worktree",
        action="store_true",
        default=True,
        help="(default) spawn worker in an ephemeral isolated git worktree",
    )
    wt_group.add_argument(
        "--shared-worktree",
        dest="isolated_worktree",
        action="store_false",
        help="spawn worker in the main repo checkout (opt-out of isolation)",
    )
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument(
        "--requires-mcp", action="store_true", default=False, dest="requires_mcp",
        help="Preserve ambient MCP config for this dispatch instead of the default "
             "force-empty scoped posture (spec requires_mcp: true / Requires-MCP: true).",
    )
    # ADR-006: staging→pending→promote gate enforcement.
    parser.add_argument(
        "--from-staging-id", default=None, dest="from_staging_id",
        help="Dispatch ID that exists in .vnx-data/dispatches/pending/ or /staging/.",
    )
    parser.add_argument(
        "--allow-unstaged", action="store_true", default=False,
        help="Bypass staging gate (requires --reason for audit trail).",
    )
    parser.add_argument(
        "--reason", default=None,
        help="Audit reason required when --allow-unstaged is set.",
    )

    args = parser.parse_args(argv)

    # ADR-006: staging→pending→promote gate — must pass before any dispatch work.
    # OI-627: dispatch_id=args.dispatch_id cross-checks the id actually executed
    # against the staged id — a caller cannot stage under the real id and then
    # run (and stamp the commit trailer) under a different one.
    from staging_validator import validate_staging_path as _validate_staging  # noqa: PLC0415
    _validate_staging(
        getattr(args, "from_staging_id", None),
        getattr(args, "allow_unstaged", False),
        getattr(args, "reason", None),
        dispatch_id=args.dispatch_id,
    )

    lane = TmuxInteractiveDispatch(
        _resolve_state_dir(), project_root=_resolve_invocation_project_root()
    )

    result = lane.dispatch(
        args.instruction,
        args.dispatch_id,
        role=args.role,
        model=args.model,
        worker_label=args.worker_label,
        deadline_seconds=args.deadline_seconds,
        poll_interval=args.poll_interval,
        warmup_timeout=args.warmup_timeout,
        dispatch_paths=args.dispatch_paths,
        skip_permissions=args.skip_permissions if args.skip_permissions else None,
        extra_flags=args.extra_flags,
        attach=args.attach,
        isolated_worktree=args.isolated_worktree,
        base_ref=args.base_ref,
        working_tree_only=args.working_tree_only,
        requires_mcp=args.requires_mcp,
    )
    print(json.dumps(result.__dict__, default=str))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
