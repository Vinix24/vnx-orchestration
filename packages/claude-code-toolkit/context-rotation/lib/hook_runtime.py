"""What every hook entry shares: the project it acts in, emitting JSON, failing without blocking.

Every entry opens its project through open_project, the one place where the
storage gate (rotation_state.storage_problem) is passed before anything under
`.claude/rotation` is read or changed. An unexpected failure never blocks the
session: it leaves one line on stderr and one in rotation/errors.log, and the hook
exits 1 (non-blocking; never 2). A deliberate effect is JSON on stdout, never an
exit code. Standard library only.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict, Optional, Tuple

import context_budget
import rotation_state


class HookRun:
    """The step a hook is at, so a failure names where it happened."""

    def __init__(self) -> None:
        self.step = "read payload"
        self.project_dir: Optional[str] = None

    def at(self, name: str) -> None:
        self.step = name


def open_project(payload: Dict[str, Any], env: Dict[str, str],
                 run: HookRun) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """(project dir, session id, settings) for a hook that may act; None when it must do nothing.

    Nothing happens inside a subagent, for an unsafe session id or with the plugin
    disabled. Nor when the project's rotation storage fails the storage gate: then
    the reason goes to stderr once and the hook exits 0, without errors.log, which
    lives in the directory that failed the check. Failures are logged in the
    project only once the gate has passed.
    """
    if payload.get("agent_id"):
        return None
    session_id = payload.get("session_id")
    if not rotation_state.is_safe_session_id(session_id):
        return None
    project_dir = rotation_state.project_dir_for(payload, env)
    run.at("load settings")
    settings = context_budget.load_settings(project_dir)
    if not settings["enabled"]:
        return None
    run.at("check storage")
    problem = rotation_state.storage_problem(project_dir)
    if problem:
        rotation_state.stderr_line("context-rotation: %s; nothing is read or changed in this project" % problem)
        return None
    run.project_dir = project_dir
    return project_dir, session_id, settings


def emit(document: Optional[Dict[str, Any]]) -> None:
    """Write one hook output document; None (what context_output gives for no text) writes nothing."""
    if document is None:
        return
    sys.stdout.write(json.dumps(document))
    sys.stdout.flush()


def context_output(event: str, text: Optional[str], system_message: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The output document for model context (M) and a user line (U); None when both are empty."""
    document: Dict[str, Any] = {}
    if text:
        document["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": text}
    if system_message:
        document["systemMessage"] = system_message
    return document or None


def report_failure(run: HookRun, error: BaseException) -> None:
    line = "context-rotation: %s failed: %s: %s" % (run.step, type(error).__name__, error)
    line = " ".join(line.split())
    rotation_state.stderr_line(line)
    if run.project_dir:
        rotation_state.log_error(run.project_dir, "%s %s" % (rotation_state.utc_now(), line))


def main(handler: Callable[[Dict[str, Any], Dict[str, str], HookRun], None]) -> None:
    """Read the payload from stdin, run the handler, and exit 0, or 1 on a failure."""
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        sys.exit(0)  # not our payload: nothing to act on, nothing to report
    run = HookRun()
    try:
        if isinstance(payload, dict):
            handler(payload, dict(os.environ), run)
    except Exception as error:
        report_failure(run, error)
        sys.exit(1)
    sys.exit(0)
