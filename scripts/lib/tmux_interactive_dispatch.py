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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys_path_dir = str(Path(__file__).resolve().parent)

if sys_path_dir not in sys.path:
    sys.path.insert(0, sys_path_dir)

logger = logging.getLogger(__name__)

from tmux_worktree import WorktreeAllocateError, WorktreeHandle, allocate, classify, reap  # noqa: E402

# Capability scoping (interim, per WORKER-CAPABILITY-SCOPING-DESIGN.md §4.4/§5):
# detached ephemeral spawns drop --dangerously-skip-permissions for an empty
# ambient MCP + acceptEdits posture + role allow-list. Imported defensively;
# if unavailable the detached branch keeps a minimal scoped fallback (never
# --dangerously-skip-permissions, but without the role-specific allow-list).
try:
    from worker_permissions import (  # noqa: E402
        EMPTY_MCP_CONFIG,
        worker_scoped_enabled,
        build_claude_scope_args as _wp_build_claude_scope_args,
        resolve_worker_profile as _wp_resolve_worker_profile,
    )
    _WP_AVAILABLE = True
except Exception:  # pragma: no cover - sibling import is available in-tree
    EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
    _WP_AVAILABLE = False

    def worker_scoped_enabled() -> bool:  # type: ignore[misc]
        return os.environ.get("VNX_WORKER_SCOPED", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )

    def _wp_build_claude_scope_args(profile, *, permission_mode="acceptEdits", requires_mcp=False, working_tree_only=False):  # type: ignore[misc]
        args = ["--permission-mode", permission_mode]
        if not requires_mcp:
            args += ["--strict-mcp-config", "--mcp-config", EMPTY_MCP_CONFIG]
        args += ["--allowedTools", "Read,Write,Edit,MultiEdit,Bash,Grep,Glob"]
        if working_tree_only:
            args += ["--disallowedTools", "Bash(git commit),Bash(git commit:*),Bash(git push),Bash(git push:*)"]
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

# Only simple identifiers are valid model names (no whitespace or shell metacharacters).
_SAFE_MODEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

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
) -> str:
    """Build the interactive ``claude`` launch line (NOT ``claude -p``).

    Raises ValueError if *model* contains whitespace or shell metacharacters, or
    if *extra_flags* contains ``-p``, ``--print``, or ``--print=…``: those flags
    convert an interactive session to headless, defeating the subscription-safe
    guarantee of this lane.

    ``role``: when provided, selects the permission profile whose tool allow-list
    is included as ``--allowedTools`` so detached headless workers proceed without
    stalling on tool-use prompts (``acceptEdits`` alone only auto-approves file edits).

    ``requires_mcp``: when True, ``--strict-mcp-config --mcp-config {}`` is omitted
    so the worker keeps its normal ambient MCP config.
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
        # Detached/autonomous run (no TTY to answer prompts). Default: scope the
        # spawn — role allow-list + optional empty-MCP — instead of the blanket
        # skip-permissions blast radius. VNX_WORKER_SCOPED=0 restores the legacy
        # flag for emergency rollback.
        if worker_scoped_enabled():
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
    return f"source ~/.zshrc 2>/dev/null; claude --model {model}{flags}"


def _sanitize_session_name(raw: str) -> str:
    """tmux session names may not contain '.' or ':'. Map them to '-'."""
    return "".join("-" if c in ".:" else c for c in raw)


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
    ) -> "Path | None":
        """Emit governance unified_report via the shared govern() step.

        The tmux lane always routes through govern() — no VNX_SHARED_GOVERN gate
        applies here. govern() is guaranteed not to raise; on any internal error
        it emits an honest minimal synthesized body with contract_status="synthesized".

        Returns the emitted report path on success, None on critical import failure.
        A None return is an audit-trail gap and must be surfaced by the caller.

        ``role`` is forwarded to GovernSpec so govern() can apply role-specific
        validation logic (e.g. plan-reviewer bodies skip standard heading validation).
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
        )
        raw = GovernRaw(
            receipt=receipt,
            duration_seconds=duration_seconds,
            failure_reason=failure_reason,
            token_usage=token_usage,
        )
        outcome = govern(spec, raw, lane="tmux_interactive")
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

    def _build_completion_protocol(
        self, dispatch_id: str, label: str, model: str = "unknown"
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
    def _spawn_session(self, session: str, cwd: Path) -> "tuple[str, str] | None":
        """Create a detached session; return (pane_id, window_id) or None."""
        res = self._runner.run(
            [
                "new-session",
                "-d",
                "-s",
                session,
                "-c",
                str(cwd),
                "-P",
                "-F",
                "#{pane_id}",
            ]
        )
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

    def _deliver_instruction(self, pane_id: str, body: str) -> bool:
        """Clear input, paste the instruction body, settle, then submit with Enter."""
        self._runner.run(["send-keys", "-t", pane_id, "C-u"])
        if not self._paste(pane_id, body):
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
    ) -> bool:
        """Confirm the worker actually STARTED working after a verified submit.

        ``_verify_submit`` confirms the input box is no longer staged, but under
        subscription load a submit can clear the box without the worker progressing —
        Claude idles at the prompt and never writes a receipt. Without this gate the
        lane would then wait the FULL ``deadline_seconds`` for a receipt that never
        comes (the recurring warmup-miss/no-progress hang in DISPATCH_RULES §7).

        This bounded watchdog polls for a real work signal — the UserPromptSubmit
        sentinel, a version-robust working indicator (``_looks_working``), or a fresh
        receipt — and re-nudges ONCE with a guarded Enter if the instruction is still
        staged (never a stray Enter into a running session). It returns True as soon as
        work is observed; False if no work appears within the window, so the caller can
        FAST-ABORT (retryable in seconds) instead of burning the full deadline.

        Gate is on by default; set ``VNX_TMUX_WORK_START_GATE=0`` to restore the prior
        proceed-straight-to-receipt-wait behavior.

        Known limitation: a worker blocked on an interactive permission prompt shows no
        working indicator, so for an attended/permissioned session enable the permission
        relay (``VNX_PERMISSION_RELAY=1``) so the prompt is answered before the window
        elapses. Autonomous (skip-permissions) workers — the benchmark and headless lanes
        — are unaffected.
        """
        if os.environ.get("VNX_TMUX_WORK_START_GATE", "1").strip().lower() in (
            "0", "false", "no", "off"
        ):
            return True

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
                return True
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
        return _work_observed()

    def _paste(self, pane_id: str, content: str, max_inline: int = 50000) -> bool:
        """Load *content* into a tmux buffer and paste it into the pane."""
        if len(content) > max_inline:
            tmp_path = ""
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".vnx_int_buf", delete=False, encoding="utf-8"
                ) as fh:
                    fh.write(content)
                    tmp_path = fh.name
                rc = self._runner.run(["load-buffer", tmp_path]).returncode
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        else:
            rc = self._runner.run(["load-buffer", "-"], input_text=content).returncode
        if rc != 0:
            logger.warning("interactive: load-buffer failed for %s", pane_id)
            return False
        return self._runner.run(["paste-buffer", "-t", pane_id]).returncode == 0

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
    ) -> "dict | None":
        """Poll signals 1–3 until a NEW completion appears beyond the baseline.

        *baseline_count* guards signal 1 (canonical ndjson count before delivery).
        *baseline_pending_ids* guards signal 2 (filenames of pending receipts at baseline).
          When None, falls back to legacy combined position-based baseline (backward compat).
        *baseline_backstop* guards signal 3 (True if report was backstop-active at baseline).
          When None, captured at call time (conservative default).

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
        # _wp_build_claude_scope_args is invoked). Reject every other path —
        # attached, --dangerously-skip-permissions, or VNX_WORKER_SCOPED=0 — so an
        # unscoped working-tree-only worker can never silently reach git commit/push.
        if working_tree_only and not (skip_permissions and worker_scoped_enabled()):
            return InteractiveDispatchResult(
                success=False,
                dispatch_id=dispatch_id,
                failure_reason=(
                    "working_tree_only requires a scoped detached spawn "
                    "(skip_permissions + VNX_WORKER_SCOPED!=0); refusing unscoped "
                    "dispatch where the commit/push deny would not bind"
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

        # Worktree isolation: allocate before session creation so a failed add
        # never spawns a tmux session with an uncontrolled cwd.
        worktree_handle: "WorktreeHandle | None" = None
        _wt_state: "list[str | None]" = [None]
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
                    cls = classify(worktree_handle)
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
            spawned = self._spawn_session(session, cwd)
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
                    + self._build_completion_protocol(dispatch_id, label, model=model)
                    + f"\n\n{_TRAILER}\n"
                )
            else:
                # Legacy path: scope-note + completion-protocol, no trailer.
                body = (
                    _context_body
                    + self._scope_note(dispatch_paths)
                    + self._build_completion_protocol(dispatch_id, label, model=model)
                )

            # 7. Deliver instruction
            self._emit_event(
                "interactive_deliver_start",
                dispatch_id=dispatch_id,
                label=label,
                reason="send-keys dispatch instruction",
                metadata={"session": session, "pane_id": pane_id},
            )
            if not self._deliver_instruction(pane_id, body):
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

            # 7b-bis. Confirm the worker actually STARTED working. A verified submit can
            # still clear the input box without the worker progressing under subscription
            # load; without this gate the lane would wait the full deadline for a receipt
            # that never comes (the warmup-miss/no-progress hang in DISPATCH_RULES §7).
            # Fast-abort (retryable in seconds) instead of burning deadline_seconds.
            # baseline / baseline_pending_ids are the PRE-DELIVERY snapshot (step 5),
            # so a pre-existing/stale receipt is never miscounted as fresh progress.
            if not self._await_work_started(
                pane_id,
                dispatch_id,
                signal_dir=signal_dir,
                baseline_count=baseline,
                baseline_pending_ids=baseline_pending_ids,
                completion_statuses=completion_statuses,
                label=label,
            ):
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
            )

            if receipt is None:
                self._govern_report(
                    dispatch_id=dispatch_id,
                    terminal_id=label,
                    instruction=instruction,
                    receipt=None,
                    duration_seconds=time.monotonic() - start_time,
                    base_sha=worktree_handle.base_sha if worktree_handle else None,
                    worktree_path=worktree_handle.path if worktree_handle else None,
                    model=model,
                    role=role,
                )
                _teardown("timeout")
                return InteractiveDispatchResult(
                    success=False,
                    dispatch_id=dispatch_id,
                    session=session,
                    label=label,
                    window_id=window_id,
                    pane_id=pane_id,
                    failure_reason="receipt deadline exceeded",
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
            worker_succeeded = receipt is not None and receipt.get("status") not in (
                "failed",
                "blocked",
            )
            # Best-effort token usage parsed from the pane TUI counter (no usage API on
            # the subscription lane) — used only as a frontmatter fallback when the
            # worker receipt carries none.
            _pane_tokens = self._parse_token_usage_from_log(_raw_log[0])
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
                        else f"worker_status: {receipt.get('status')}"
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
    """Delegate to canonical project_root resolver; ensures lane and append_receipt share the same state dir."""
    from project_root import resolve_state_dir
    return resolve_state_dir(caller_file=__file__)


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
    from staging_validator import validate_staging_path as _validate_staging  # noqa: PLC0415
    _validate_staging(
        getattr(args, "from_staging_id", None),
        getattr(args, "allow_unstaged", False),
        getattr(args, "reason", None),
    )

    lane = TmuxInteractiveDispatch(_resolve_state_dir())

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
    )
    print(json.dumps(result.__dict__, default=str))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
