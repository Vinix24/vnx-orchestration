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
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys_path_dir = str(Path(__file__).resolve().parent)
import sys

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

    def _wp_build_claude_scope_args(profile, *, permission_mode="acceptEdits", requires_mcp=False):  # type: ignore[misc]
        args = ["--permission-mode", permission_mode]
        if not requires_mcp:
            args += ["--strict-mcp-config", "--mcp-config", EMPTY_MCP_CONFIG]
        args += ["--allowedTools", "Read,Write,Edit,MultiEdit,Bash,Grep,Glob"]
        return args

    def _wp_resolve_worker_profile(role):  # type: ignore[misc]
        return None

DEFAULT_COMPLETION_STATUSES = frozenset({"done", "completed", "failed", "blocked"})

# Only simple identifiers are valid model names (no whitespace or shell metacharacters).
_SAFE_MODEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


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
            f"'claude-opus-4-7'); whitespace and shell metacharacters are not allowed"
        )
    if extra_flags:
        for token in extra_flags.split():
            if token in ("-p", "--print") or token.startswith("--print="):
                raise ValueError(
                    "extra_flags must not contain -p/--print: "
                    "the interactive lane must stay on the subscription"
                )
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
        footer + report-contract directive + trailer sentinel). Default ("0") is
        byte-identical to pre-T1 behavior.

        Reuses subprocess lane enrichers (_inject_skill_context) so the tmux-spawn
        worker receives the same skill body + intelligence treatment as a headless
        subprocess worker. Falls back to a legacy role label + instruction on failure.
        Always includes *instruction* in the returned string.
        """
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
            return "\n\n".join(parts)

        if smart_context:
            enriched = f"{smart_context}\n\n{enriched}"
        return enriched

    def _emit_unified_report(
        self,
        dispatch_id: str,
        terminal_id: str,
        instruction: str,
        receipt: "dict | None",
        duration_seconds: float,
    ) -> "Path | None":
        """Emit governance unified_report for audit parity with subprocess lane.

        Returns the emitted report path on success, None on failure.
        A None return on a governed-completion path (worker succeeded) is an
        audit-trail gap and must be surfaced by the caller.
        """
        try:
            from governance_emit import emit_unified_report  # noqa: PLC0415
            status = (receipt or {}).get("status", "done")
            data_dir = self._state_dir.parent
            report_path = emit_unified_report(
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                provider="claude",
                instruction=instruction,
                response_text=(
                    f"Interactive tmux dispatch (lane: tmux_interactive). Status: {status}."
                ),
                findings=[],
                duration_seconds=duration_seconds,
                data_dir=data_dir,
            )
            logger.info(
                "interactive: unified_report emitted dispatch=%s status=%s path=%s",
                dispatch_id,
                status,
                report_path,
            )
            return report_path
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "interactive: unified_report emission failed for %s: %s",
                dispatch_id,
                exc,
            )
            return None

    def _build_completion_protocol(self, dispatch_id: str, label: str) -> str:
        """Footer instructing the worker to emit a clean receipt directly.

        The path to ``append_receipt.py`` is ABSOLUTE so it resolves correctly
        regardless of the worker's cwd.
        """
        append_receipt = self._project_root / "scripts" / "append_receipt.py"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # report_path is deterministic — include it so the receipt->report linkage
        # is established even when the report is written after the receipt.
        report_path = str(
            self._state_dir.parent / "unified_reports" / f"{dispatch_id}.md"
        )
        receipt = {
            "event_type": "subprocess_completion",
            "dispatch_id": dispatch_id,
            "terminal": label,
            "status": "done",
            "source": "tmux_interactive",
            "timestamp": ts,
            "report_path": report_path,
        }
        state_dir = shlex.quote(str(self._state_dir))
        data_dir = shlex.quote(str(self._state_dir.parent))
        append_receipt_arg = shlex.quote(str(append_receipt))
        receipts_file = shlex.quote(str(self._receipts_file))
        receipt_arg = shlex.quote(json.dumps(receipt))
        return (
            "\n\n---\n\n## Completion Protocol (interactive lane)\n\n"
            "When you have finished AND committed, emit a completion receipt "
            "directly so the orchestrator can detect completion. Run:\n\n"
            "```bash\n"
            f"VNX_STATE_DIR={state_dir} VNX_DATA_DIR={data_dir} "
            f"python3 {append_receipt_arg} --receipts-file {receipts_file} --receipt "
            f"{receipt_arg}\n"
            "```\n\n"
            "Use `\"status\": \"failed\"` instead if you could not complete the "
            "work. Always write your unified report first, then emit the receipt "
            "as the last step.\n"
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
    ) -> bool:
        """Poll capture-pane until a readiness marker appears or timeout."""
        deadline = time.monotonic() + warmup_timeout
        while time.monotonic() < deadline:
            cap = self._runner.run(["capture-pane", "-t", pane_id, "-p"])
            content = cap.stdout if cap.returncode == 0 else ""
            if content and any(m in content for m in ready_markers):
                return True
            time.sleep(poll_interval)
        logger.info(
            "interactive: readiness marker not seen for %s before %.0fs warmup; "
            "proceeding (input is queued into the box regardless)",
            pane_id,
            warmup_timeout,
        )
        return False

    def _deliver_instruction(self, pane_id: str, body: str) -> bool:
        """Clear input, paste the instruction body, submit with Enter."""
        self._runner.run(["send-keys", "-t", pane_id, "C-u"])
        if not self._paste(pane_id, body):
            return False
        # Enter ALWAYS as a separate keystroke.
        return self._runner.run(["send-keys", "-t", pane_id, "Enter"]).returncode == 0

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

    # -- receipt polling ---------------------------------------------------
    def _matching_receipts(
        self,
        dispatch_id: str,
        completion_statuses: frozenset,
    ) -> list[dict]:
        """Return parsed completion receipts for *dispatch_id*, in file order."""
        if not self._receipts_file.exists():
            return []
        out: list[dict] = []
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
                    if status in completion_statuses or event_type.endswith(
                        "_completion"
                    ):
                        out.append(rec)
        except OSError as exc:
            logger.debug("interactive: receipts read failed: %s", exc)
        return out

    def _wait_for_receipt(
        self,
        dispatch_id: str,
        deadline_seconds: float,
        poll_interval: float,
        completion_statuses: frozenset,
        *,
        baseline_count: int = 0,
    ) -> "dict | None":
        """Poll the canonical receipts NDJSON until a NEW completion appears.

        Only counts receipts beyond *baseline_count* (F3: stale-receipt guard).
        Returns the newest matching receipt beyond baseline, or None on deadline.
        """
        deadline = time.monotonic() + deadline_seconds
        while True:
            matches = self._matching_receipts(dispatch_id, completion_statuses)
            if len(matches) > baseline_count:
                return matches[-1]
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_interval)

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

    def _load_handle(self, dispatch_id: str) -> "dict | None":
        path = self._handle_path(dispatch_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "interactive: handle read failed for %s: %s", dispatch_id, exc
            )
            return None

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
            "for shortcuts",
            "? for shortcuts",
            "Welcome to Claude",
        ),
        completion_statuses: frozenset = DEFAULT_COMPLETION_STATUSES,
        dispatch_paths: "list[str] | str | None" = None,
        extra_flags: str = "",
        attach: bool = False,
        isolated_worktree: bool = True,
        base_ref: str = "origin/main",
        requires_mcp: bool = False,
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

        label = worker_label or dispatch_id
        session = _sanitize_session_name(f"vnx-{dispatch_id}")
        cwd = self._project_root
        start_time = time.monotonic()

        # Belt-and-suspenders: validate model before any session creation.
        if not _SAFE_MODEL_RE.match(model):
            raise ValueError(
                f"model {model!r} must be a simple identifier (e.g. 'sonnet', "
                f"'claude-opus-4-7'); whitespace and shell metacharacters are not allowed"
            )

        # Worktree isolation: allocate before session creation so a failed add
        # never spawns a tmux session with an uncontrolled cwd.
        worktree_handle: "WorktreeHandle | None" = None
        _wt_state: "list[str | None]" = [None]

        if isolated_worktree:
            try:
                worktree_handle = allocate(
                    dispatch_id=dispatch_id,
                    base_ref=base_ref,
                    repo_root=self._project_root,
                )
                cwd = worktree_handle.path
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

        # Idempotency guard: teardown runs exactly once across all exit paths.
        _torn_down = False

        def _teardown(status: str) -> None:
            nonlocal _torn_down
            if _torn_down:
                return
            _torn_down = True
            try:
                self._kill_session(session)
            except Exception as exc:  # noqa: BLE001
                logger.debug("interactive: teardown kill-session %s: %s", session, exc)
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

            # 3. Build launch command
            launch_cmd = self._launch_builder(
                model,
                skip_permissions=skip_permissions,
                extra_flags=extra_flags,
                role=role,
                requires_mcp=requires_mcp,
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

            # 4. Wait for readiness (best-effort)
            self._wait_ready(
                pane_id,
                ready_markers=ready_markers,
                warmup_timeout=warmup_timeout,
                poll_interval=warmup_poll_interval,
            )

            if attach:
                self._attach(session)

            # 5. Baseline snapshot BEFORE delivery (F3: stale-receipt guard)
            baseline = len(self._matching_receipts(dispatch_id, completion_statuses))

            # 6. Assemble body (skill body + intelligence + instruction via enrichers)
            body = (
                self._assemble_context(
                    role=role,
                    smart_context=smart_context,
                    terminal_id=label,
                    dispatch_id=dispatch_id,
                    instruction=instruction,
                    dispatch_paths=dispatch_paths,
                )
                + self._scope_note(dispatch_paths)
                + self._build_completion_protocol(dispatch_id, label)
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

            # 8. Wait for receipt
            receipt = self._wait_for_receipt(
                dispatch_id,
                deadline_seconds,
                poll_interval,
                completion_statuses,
                baseline_count=baseline,
            )

            if receipt is None:
                self._emit_unified_report(
                    dispatch_id=dispatch_id,
                    terminal_id=label,
                    instruction=instruction,
                    receipt=None,
                    duration_seconds=time.monotonic() - start_time,
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
            emitted_report = self._emit_unified_report(
                dispatch_id=dispatch_id,
                terminal_id=label,
                instruction=instruction,
                receipt=receipt,
                duration_seconds=time.monotonic() - start_time,
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

    args = parser.parse_args(argv)
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
    )
    print(json.dumps(result.__dict__, default=str))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
