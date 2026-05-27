#!/usr/bin/env python3
"""tmux_interactive_dispatch.py — interactive-tmux Claude dispatch lane (PR-TMUX-1).

The 15-June escape.  Headless ``claude -p`` moves to API credits on 15 June;
an *interactive* ``claude`` running inside a tmux window stays on the Claude
subscription.  This module turns that validated mechanism into a real dispatch
lane with full governance parity (lease + NDJSON audit + clean receipt).

Two-phase lifecycle (NOT a single blocking close):

  Phase A — :meth:`TmuxInteractiveDispatch.dispatch`
    1. Spawn a FRESH interactive ``claude`` in a DETACHED tmux window
       (interactive, never ``claude -p`` — that is the subscription-safe mode).
    2. Inject the role / CLAUDE.md context the same way subprocess_dispatch
       does (base + role layers + permission preamble).
    3. Acquire the terminal lease (parity with subprocess lease-management).
    4. ``send-keys`` the dispatch instruction.  **Enter is ALWAYS a separate
       keystroke** — without it the message never reaches the worker.
    5. Wait until the worker emits a receipt for this dispatch_id (the worker
       calls ``append_receipt`` directly; the lane appends a completion
       protocol footer instructing it to).  No 300s chunk-timeout — interactive
       sessions do not have the event-stream-silence problem; a wide,
       configurable deadline is used instead.
    6. Leave the window WARM-OPEN and persist the window/pane handle so Phase B
       can find it.  Emit NDJSON audit events at parity with subprocess.
    7. Return the receipt plus the window handle.

  Phase B — :meth:`TmuxInteractiveDispatch.close` (called AFTER T0 review)
    8. Optionally ``send-keys`` one more follow-up instruction into the still
       warm session (context + cwd intact = fix-forward without re-spawn), wait
       for that follow-up receipt, THEN kill the window and release the lease.
    9. Default (no follow-up): just close the window and free the lease.

Modes:
  * ``attach=False`` (default): detached window — autonomous dev worker.
  * ``attach=True``: window surfaced to the operator's terminal (watch + talk).

Wave5 smart-context injection is intentionally NOT wired here — it lands in
PR-TMUX-2.  The ``smart_context`` parameter of :meth:`dispatch` is the single
insertion point: pass the assembled intelligence block and it is layered into
the context exactly like subprocess_dispatch does.  Default ``None`` => no Wave5.

BILLING SAFETY: only ``tmux`` subprocess calls spawn an interactive ``claude``
binary.  No Anthropic SDK is imported anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

sys_path_dir = str(Path(__file__).resolve().parent)
import sys

if sys_path_dir not in sys.path:
    sys.path.insert(0, sys_path_dir)

logger = logging.getLogger(__name__)

# Receipt statuses that mark a dispatch as complete for the purpose of the
# Phase-A / follow-up wait.  A worker emitting any of these (or an event_type
# ending in ``_completion``) is treated as "done driving".
DEFAULT_COMPLETION_STATUSES = frozenset({"done", "completed", "failed", "blocked"})


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
    """Thin wrapper around real ``tmux`` subprocess calls.

    Tests inject a fake with the same surface (``run`` / ``available``) so the
    spawn->drive->receipt->close round-trip exercises this module's real logic
    without a live Claude call.
    """

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
    """Outcome of Phase A (spawn -> drive -> collect, window stays warm)."""

    success: bool
    dispatch_id: str
    terminal_id: str
    session: "str | None" = None
    window_id: "str | None" = None
    pane_id: "str | None" = None
    lease_generation: "int | None" = None
    receipt: "dict | None" = None
    attached: bool = False
    failure_reason: "str | None" = None


@dataclass
class InteractiveCloseResult:
    """Outcome of Phase B (optional follow-up, then close + lease release)."""

    success: bool
    dispatch_id: str
    window_killed: bool = False
    lease_released: bool = False
    follow_up_receipt: "dict | None" = None
    failure_reason: "str | None" = None


# ---------------------------------------------------------------------------
# Launch command builder (overridable)
# ---------------------------------------------------------------------------
def _default_launch_command(
    model: str,
    *,
    skip_permissions: bool = False,
    extra_flags: str = "",
) -> str:
    """Build the interactive ``claude`` launch line (NOT ``claude -p``).

    Mirrors the worker-pane launch in ``scripts/commands/start.sh``: source the
    profile for PATH/MCP, then start an interactive ``claude`` pinned to *model*.
    ``--dangerously-skip-permissions`` is added only when *skip_permissions* is
    set (autonomous detached workers cannot answer permission prompts).

    Raises ValueError if *extra_flags* contains ``-p``, ``--print``, or
    ``--print=…``: those flags convert an interactive session to headless,
    defeating the subscription-safe guarantee of this lane.
    """
    if extra_flags:
        for token in extra_flags.split():
            if token in ("-p", "--print") or token.startswith("--print="):
                raise ValueError(
                    f"extra_flags must not contain -p/--print: "
                    "the interactive lane must stay on the subscription"
                )
    flags = ""
    if skip_permissions:
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
    """Drive a dispatch through an interactive tmux Claude session."""

    def __init__(
        self,
        state_dir: "str | Path",
        *,
        runner: "TmuxCommandRunner | None" = None,
        session_prefix: str = "vnx-int",
        launch_builder: "Callable[..., str] | None" = None,
        project_root: "str | Path | None" = None,
        receipts_file: "str | Path | None" = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._runner = runner or TmuxCommandRunner()
        self._session_prefix = session_prefix
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

    # -- path helpers ------------------------------------------------------
    @staticmethod
    def _resolve_project_root() -> Path:
        """scripts/lib/tmux_interactive_dispatch.py -> repo root (parents[2])."""
        return Path(__file__).resolve().parents[2]

    def _resolve_cwd(self, terminal_id: str) -> Path:
        """Spawn cwd: the terminal dir (skill discovery) if present, else root.

        Matches start.sh, which launches each worker pane in
        ``.claude/terminals/T{n}`` so Claude Code discovers the symlinked
        ``.claude/skills/``.  The dispatch instruction itself tells the worker
        which worktree to ``cd`` into for the actual edits.
        """
        term_dir = self._project_root / ".claude" / "terminals" / terminal_id
        return term_dir if term_dir.is_dir() else self._project_root

    def _handle_path(self, dispatch_id: str) -> Path:
        return self._handle_dir / f"{dispatch_id}.json"

    # -- audit -------------------------------------------------------------
    def _emit_event(
        self,
        event_type: str,
        *,
        dispatch_id: str,
        terminal_id: str,
        reason: "str | None" = None,
        metadata: "dict | None" = None,
    ) -> None:
        """Append a coordination event (NDJSON audit parity). Never raises."""
        meta = {"terminal_id": terminal_id, "lane": "tmux_interactive"}
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
            logger.debug(
                "interactive: emit %s skipped (%s)", event_type, exc
            )

    # -- lease -------------------------------------------------------------
    def _acquire_lease(self, terminal_id: str, dispatch_id: str) -> "int | None":
        """Acquire the terminal lease; return the new generation or None.

        Failure is non-fatal in shadow mode (DB unavailable): we log and return
        None so the dispatch still spawns, mirroring TmuxAdapter.validate_lease.
        """
        try:
            from runtime_coordination import (
                acquire_lease,
                get_connection,
                register_dispatch,
            )

            with get_connection(self._state_dir) as conn:
                # Register the dispatch first (idempotent) so the lease's
                # dispatch_id FK is satisfied — the real flow registers via the
                # queue before leasing; the lane registers on its own behalf.
                register_dispatch(
                    conn,
                    dispatch_id=dispatch_id,
                    terminal_id=terminal_id,
                    actor="tmux_interactive",
                )
                lease = acquire_lease(
                    conn,
                    terminal_id=terminal_id,
                    dispatch_id=dispatch_id,
                    actor="tmux_interactive",
                    reason=f"interactive dispatch {dispatch_id}",
                )
                conn.commit()
                return int(lease.get("generation")) if lease else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "interactive: lease acquire failed for %s/%s: %s",
                terminal_id,
                dispatch_id,
                exc,
            )
            return None

    def _release_lease(
        self, terminal_id: str, generation: "int | None", dispatch_id: str
    ) -> bool:
        """Release the lease. Returns True on release (or already-released)."""
        if generation is None:
            return False
        try:
            from runtime_coordination import get_connection, release_lease

            with get_connection(self._state_dir) as conn:
                release_lease(
                    conn,
                    terminal_id=terminal_id,
                    generation=generation,
                    actor="tmux_interactive",
                    reason=f"interactive close {dispatch_id}",
                )
                conn.commit()
                return True
        except Exception as exc:  # noqa: BLE001 — already idle / generation drift
            logger.warning(
                "interactive: lease release for %s gen=%s treated as no-op: %s",
                terminal_id,
                generation,
                exc,
            )
            return False

    # -- context assembly --------------------------------------------------
    def _assemble_context(
        self,
        terminal_id: str,
        instruction: str,
        role: "str | None",
        dispatch_id: str,
        model: str,
        smart_context: "str | None" = None,
    ) -> str:
        """Layer base + role CLAUDE.md context, exactly like subprocess_dispatch.

        Wave5 smart-context is layered ONLY when *smart_context* is provided
        (the PR-TMUX-2 insertion point).  Default ``None`` => no intelligence
        section, keeping this PR scoped to the core lane.
        """
        intelligence_section = smart_context or ""
        body: "str | None" = None
        try:
            from subprocess_dispatch_internals.skill_injection import (
                _inject_permission_profile,
                _legacy_claude_md_resolution,
                _try_prompt_assembler,
            )

            meta: dict = {
                "role": role or "",
                "terminal": terminal_id,
                "dispatch_id": dispatch_id,
                "model": model,
            }
            body = _try_prompt_assembler(
                terminal_id, instruction, role, meta, intelligence_section
            )
            if body is None:
                body = _legacy_claude_md_resolution(
                    terminal_id, instruction, role, intelligence_section
                )
            body = _inject_permission_profile(terminal_id, role, body)
            return body
        except Exception as exc:  # noqa: BLE001 — assembly is best-effort
            logger.warning(
                "interactive: context assembly fell back to raw instruction (%s)",
                exc,
            )
            return instruction

    def _build_completion_protocol(
        self, dispatch_id: str, terminal_id: str
    ) -> str:
        """Footer instructing the worker to emit a clean receipt directly.

        The interactive worker does not go through the markdown -> receipt
        processor route; it calls ``append_receipt`` directly so a clean
        ``status`` receipt for this dispatch_id lands in the canonical NDJSON,
        which is what Phase A polls on.

        The path to ``append_receipt.py`` is ABSOLUTE so it resolves correctly
        regardless of the worker's cwd (which is ``.claude/terminals/T{n}``
        when skill-discovery dirs are present).
        """
        append_receipt = self._project_root / "scripts" / "append_receipt.py"
        return (
            "\n\n---\n\n## Completion Protocol (interactive lane)\n\n"
            "When you have finished AND committed, emit a completion receipt "
            "directly so the orchestrator can detect completion. Run:\n\n"
            "```bash\n"
            f"python3 {append_receipt} --receipt "
            f"'{{\"event_type\": \"subprocess_completion\", "
            f"\"dispatch_id\": \"{dispatch_id}\", "
            f"\"terminal\": \"{terminal_id}\", "
            "\"status\": \"done\", "
            "\"source\": \"tmux_interactive\"}'\n"
            "```\n\n"
            "Use `\"status\": \"failed\"` instead if you could not complete the "
            "work. Always write your unified report first, then emit the receipt "
            "as the last step.\n"
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
        """Poll capture-pane until a readiness marker appears or timeout.

        Best-effort: interactive prompt detection is fragile, so on timeout we
        proceed anyway (the send-keys below still queues into the input box).
        Returns True if a marker was observed.
        """
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
        """Clear the input, paste the instruction body, submit with Enter.

        The body is delivered via tmux paste-buffer (multi-line safe) and the
        Enter that submits it is sent as a SEPARATE keystroke afterwards.
        """
        # Clear any pending input first.
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
        # tmux returns non-zero when the session is already gone; treat as done.
        logger.debug(
            "interactive: kill-session %s rc=%s (likely already gone): %s",
            session,
            res.returncode,
            res.stderr.strip(),
        )
        return False

    def _attach(self, session: str) -> bool:
        """Surface the session to the operator (best-effort).

        When this process runs inside tmux (``$TMUX`` set) ``switch-client``
        moves the operator's client to the session.  Outside tmux we cannot
        block on ``attach-session`` here, so we record the session and rely on
        the operator running ``tmux attach -t <session>``.
        """
        if os.environ.get("TMUX"):
            return self._runner.run(["switch-client", "-t", session]).returncode == 0
        logger.info(
            "interactive: not inside tmux — attach with: tmux attach -t %s",
            session,
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
        *,
        deadline_seconds: float,
        poll_interval: float,
        completion_statuses: frozenset,
        baseline_count: int = 0,
    ) -> "dict | None":
        """Poll the canonical receipts NDJSON until a NEW completion appears.

        No 300s chunk-timeout: interactive sessions never go event-silent the
        way ``claude -p`` does, so we use a single wide *deadline_seconds*.
        Returns the newest matching receipt beyond *baseline_count*, or None on
        deadline.
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
        """Atomically persist the warm-window handle for Phase B."""
        self._handle_dir.mkdir(parents=True, exist_ok=True)
        path = self._handle_path(dispatch_id)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(handle, indent=2, sort_keys=True), encoding="utf-8")
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
            logger.debug("interactive: handle unlink failed for %s: %s", dispatch_id, exc)

    # ------------------------------------------------------------------
    # Phase A
    # ------------------------------------------------------------------
    def dispatch(
        self,
        terminal_id: str,
        instruction: str,
        dispatch_id: str,
        *,
        role: "str | None" = None,
        model: str = "sonnet",
        attach: bool = False,
        skip_permissions: "bool | None" = None,
        smart_context: "str | None" = None,
        deadline_seconds: float = 3600.0,
        poll_interval: float = 5.0,
        warmup_timeout: float = 30.0,
        warmup_poll_interval: float = 1.0,
        ready_markers: "tuple[str, ...]" = ("for shortcuts", "? for shortcuts", "Welcome to Claude"),
        completion_statuses: frozenset = DEFAULT_COMPLETION_STATUSES,
        extra_flags: str = "",
    ) -> InteractiveDispatchResult:
        """Spawn -> drive -> collect.  Leaves the window WARM-OPEN on success.

        ``skip_permissions`` defaults to ``not attach``: an autonomous detached
        worker cannot answer permission prompts, while an attached (human in the
        loop) session keeps them.  Pass an explicit bool to override.
        """
        if not self._runner.available():
            return InteractiveDispatchResult(
                success=False,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                failure_reason="tmux binary not found in PATH",
            )

        if skip_permissions is None:
            skip_permissions = not attach

        session = _sanitize_session_name(f"{self._session_prefix}-{dispatch_id}")
        cwd = self._resolve_cwd(terminal_id)

        # 1. spawn fresh interactive claude in a detached window
        spawned = self._spawn_session(session, cwd)
        if spawned is None:
            return InteractiveDispatchResult(
                success=False,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                session=session,
                failure_reason="tmux new-session failed",
            )
        pane_id, window_id = spawned
        self._emit_event(
            "interactive_spawn",
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            reason=f"spawned interactive claude in {session}",
            metadata={"session": session, "pane_id": pane_id, "window_id": window_id},
        )

        launch_cmd = self._launch_builder(
            model, skip_permissions=skip_permissions, extra_flags=extra_flags
        )
        if not self._launch_claude(pane_id, launch_cmd):
            self._kill_session(session)
            return InteractiveDispatchResult(
                success=False,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                session=session,
                window_id=window_id,
                pane_id=pane_id,
                failure_reason="failed to launch interactive claude",
            )

        # 2. acquire lease (parity with subprocess)
        lease_generation = self._acquire_lease(terminal_id, dispatch_id)

        # 3. wait for the session to be ready to accept input (best-effort)
        self._wait_ready(
            pane_id,
            ready_markers=ready_markers,
            warmup_timeout=warmup_timeout,
            poll_interval=warmup_poll_interval,
        )

        attached = self._attach(session) if attach else False

        # Snapshot baseline BEFORE delivering the instruction.  Any matching
        # receipt that already exists in the NDJSON (stale / reused dispatch_id)
        # is counted in the baseline so _wait_for_receipt only returns on a
        # FRESH receipt beyond that count — avoids false Phase-A completion.
        baseline = len(self._matching_receipts(dispatch_id, completion_statuses))

        # 4. assemble context (base + role; Wave5 only if smart_context given)
        body = self._assemble_context(
            terminal_id, instruction, role, dispatch_id, model, smart_context
        )
        body = body + self._build_completion_protocol(dispatch_id, terminal_id)

        # 5. send-keys the dispatch instruction (Enter as a separate keystroke)
        self._emit_event(
            "interactive_deliver_start",
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            reason="send-keys dispatch instruction",
            metadata={"session": session, "pane_id": pane_id, "attached": attached},
        )
        if not self._deliver_instruction(pane_id, body):
            self._emit_event(
                "interactive_deliver_failure",
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                reason="send-keys/paste of instruction failed",
                metadata={"session": session, "pane_id": pane_id},
            )
            # Window stays open for inspection; persist handle so Phase B can close.
            self._persist_handle(
                dispatch_id,
                self._build_handle(
                    dispatch_id, terminal_id, session, window_id, pane_id,
                    lease_generation, attached, model, role, "deliver_failed",
                ),
            )
            return InteractiveDispatchResult(
                success=False,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                session=session,
                window_id=window_id,
                pane_id=pane_id,
                lease_generation=lease_generation,
                attached=attached,
                failure_reason="failed to deliver instruction via send-keys",
            )
        self._emit_event(
            "interactive_deliver_success",
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            reason="dispatch instruction delivered",
            metadata={"session": session, "pane_id": pane_id},
        )

        # 6. persist handle BEFORE waiting so Phase B can always find the warm
        #    window even if the receipt wait times out.
        self._persist_handle(
            dispatch_id,
            self._build_handle(
                dispatch_id, terminal_id, session, window_id, pane_id,
                lease_generation, attached, model, role, "awaiting_receipt",
            ),
        )

        # 7. wait for the worker's completion receipt
        receipt = self._wait_for_receipt(
            dispatch_id,
            deadline_seconds=deadline_seconds,
            poll_interval=poll_interval,
            completion_statuses=completion_statuses,
            baseline_count=baseline,
        )

        if receipt is None:
            self._emit_event(
                "interactive_receipt_timeout",
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                reason=f"no completion receipt within {deadline_seconds:.0f}s",
                metadata={"session": session, "pane_id": pane_id},
            )
            # Window intentionally left WARM-OPEN for operator inspection.
            return InteractiveDispatchResult(
                success=False,
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                session=session,
                window_id=window_id,
                pane_id=pane_id,
                lease_generation=lease_generation,
                attached=attached,
                failure_reason="receipt deadline exceeded (window left warm-open)",
            )

        self._emit_event(
            "interactive_receipt_observed",
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            reason=f"receipt status={receipt.get('status')}",
            metadata={"session": session, "status": receipt.get("status")},
        )
        # Refresh the handle with the observed receipt status; window stays warm.
        self._persist_handle(
            dispatch_id,
            self._build_handle(
                dispatch_id, terminal_id, session, window_id, pane_id,
                lease_generation, attached, model, role, "warm_open",
                receipt_status=receipt.get("status"),
            ),
        )
        self._emit_event(
            "interactive_warm_open",
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            reason="window left warm-open pending T0 review",
            metadata={"session": session, "window_id": window_id},
        )

        return InteractiveDispatchResult(
            success=True,
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            session=session,
            window_id=window_id,
            pane_id=pane_id,
            lease_generation=lease_generation,
            receipt=receipt,
            attached=attached,
        )

    @staticmethod
    def _build_handle(
        dispatch_id: str,
        terminal_id: str,
        session: str,
        window_id: str,
        pane_id: str,
        lease_generation: "int | None",
        attached: bool,
        model: str,
        role: "str | None",
        phase: str,
        receipt_status: "str | None" = None,
    ) -> dict:
        handle = {
            "dispatch_id": dispatch_id,
            "terminal_id": terminal_id,
            "session": session,
            "window_id": window_id,
            "pane_id": pane_id,
            "lease_generation": lease_generation,
            "attached": attached,
            "model": model,
            "role": role,
            "phase": phase,
            "updated_at": time.time(),
        }
        if receipt_status is not None:
            handle["receipt_status"] = receipt_status
        return handle

    # ------------------------------------------------------------------
    # Phase B
    # ------------------------------------------------------------------
    def close(
        self,
        dispatch_id: str,
        follow_up_instruction: "str | None" = None,
        *,
        deadline_seconds: float = 3600.0,
        poll_interval: float = 5.0,
        completion_statuses: frozenset = DEFAULT_COMPLETION_STATUSES,
    ) -> InteractiveCloseResult:
        """Close the warm window (Phase B), optionally driving one follow-up.

        Called AFTER T0 review.  With *follow_up_instruction*, one more command
        is sent into the still-warm session (context + cwd intact = fix-forward
        without re-spawn) and its receipt awaited before teardown.  Without it,
        the window is killed and the lease released immediately.

        Idempotent: a missing handle (already closed) returns success.
        """
        handle = self._load_handle(dispatch_id)
        if handle is None:
            logger.info(
                "interactive: no handle for %s — already closed (no-op)", dispatch_id
            )
            return InteractiveCloseResult(
                success=True,
                dispatch_id=dispatch_id,
                window_killed=False,
                lease_released=False,
                failure_reason="no handle (already closed)",
            )

        session = handle.get("session") or ""
        pane_id = handle.get("pane_id") or ""
        terminal_id = handle.get("terminal_id") or ""
        lease_generation = handle.get("lease_generation")

        follow_up_receipt: "dict | None" = None
        follow_up_ok = True

        if follow_up_instruction:
            baseline = len(
                self._matching_receipts(dispatch_id, completion_statuses)
            )
            self._emit_event(
                "interactive_follow_up_deliver",
                dispatch_id=dispatch_id,
                terminal_id=terminal_id,
                reason="send-keys follow-up into warm session",
                metadata={"session": session, "pane_id": pane_id},
            )
            delivered = self._deliver_instruction(
                pane_id,
                follow_up_instruction
                + self._build_completion_protocol(dispatch_id, terminal_id),
            )
            if not delivered:
                follow_up_ok = False
                logger.warning(
                    "interactive: follow-up send-keys failed for %s", dispatch_id
                )
            else:
                follow_up_receipt = self._wait_for_receipt(
                    dispatch_id,
                    deadline_seconds=deadline_seconds,
                    poll_interval=poll_interval,
                    completion_statuses=completion_statuses,
                    baseline_count=baseline,
                )
                if follow_up_receipt is None:
                    follow_up_ok = False
                    self._emit_event(
                        "interactive_follow_up_timeout",
                        dispatch_id=dispatch_id,
                        terminal_id=terminal_id,
                        reason=f"no follow-up receipt within {deadline_seconds:.0f}s",
                        metadata={"session": session},
                    )

        # Teardown always runs (cleanup must happen even on follow-up timeout).
        window_killed = self._kill_session(session) if session else False
        lease_released = self._release_lease(
            terminal_id, lease_generation, dispatch_id
        )
        self._emit_event(
            "interactive_window_closed",
            dispatch_id=dispatch_id,
            terminal_id=terminal_id,
            reason="warm window closed + lease released",
            metadata={
                "session": session,
                "window_killed": window_killed,
                "lease_released": lease_released,
                "had_follow_up": bool(follow_up_instruction),
            },
        )
        self._remove_handle(dispatch_id)

        return InteractiveCloseResult(
            success=follow_up_ok,
            dispatch_id=dispatch_id,
            window_killed=window_killed,
            lease_released=lease_released,
            follow_up_receipt=follow_up_receipt,
            failure_reason=(
                None if follow_up_ok else "follow-up did not complete (window still closed)"
            ),
        )


# ---------------------------------------------------------------------------
# CLI — operator / T0 entry point (dispatch + close subcommands)
# ---------------------------------------------------------------------------
def _resolve_state_dir() -> Path:
    """VNX_STATE_DIR, else VNX_DATA_DIR/state, else <root>/.vnx-data/state."""
    env = os.environ.get("VNX_STATE_DIR", "").strip()
    if env:
        return Path(env)
    data_dir = os.environ.get("VNX_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / "state"
    return Path(__file__).resolve().parents[2] / ".vnx-data" / "state"


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Interactive-tmux Claude dispatch lane (PR-TMUX-1)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pa = sub.add_parser("dispatch", help="Phase A: spawn -> drive -> collect (warm-open)")
    pa.add_argument("--terminal-id", required=True)
    pa.add_argument("--dispatch-id", required=True)
    pa.add_argument("--instruction", required=True)
    pa.add_argument("--role", default=None)
    pa.add_argument("--model", default="sonnet")
    pa.add_argument("--attach", action="store_true")
    pa.add_argument("--deadline-seconds", type=float, default=3600.0)
    pa.add_argument("--poll-interval", type=float, default=5.0)
    pa.add_argument("--warmup-timeout", type=float, default=30.0)

    pb = sub.add_parser("close", help="Phase B: optional follow-up, then close + release")
    pb.add_argument("--dispatch-id", required=True)
    pb.add_argument("--follow-up", default=None, help="Optional follow-up instruction")
    pb.add_argument("--deadline-seconds", type=float, default=3600.0)
    pb.add_argument("--poll-interval", type=float, default=5.0)

    args = parser.parse_args(argv)
    lane = TmuxInteractiveDispatch(_resolve_state_dir())

    if args.command == "dispatch":
        result = lane.dispatch(
            args.terminal_id,
            args.instruction,
            args.dispatch_id,
            role=args.role,
            model=args.model,
            attach=args.attach,
            deadline_seconds=args.deadline_seconds,
            poll_interval=args.poll_interval,
            warmup_timeout=args.warmup_timeout,
        )
        print(json.dumps(result.__dict__, default=str))
        return 0 if result.success else 1

    result = lane.close(
        args.dispatch_id,
        follow_up_instruction=args.follow_up,
        deadline_seconds=args.deadline_seconds,
        poll_interval=args.poll_interval,
    )
    print(json.dumps(result.__dict__, default=str))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
