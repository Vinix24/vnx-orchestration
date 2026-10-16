"""What every hook entry shares: reading the payload, emitting JSON, failing without blocking.

An unexpected failure never blocks the session: it leaves one line on stderr and
one in rotation/errors.log, and the hook exits 1 (non-blocking; never 2). A
deliberate effect is JSON on stdout, never an exit code. Standard library only.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict, Optional

import rotation_state


class HookRun:
    """The step a hook is at, so a failure names where it happened."""

    def __init__(self) -> None:
        self.step = "read payload"
        self.project_dir: Optional[str] = None

    def at(self, name: str) -> None:
        self.step = name


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
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        # stderr may be closed; the log below still gets the line.
        pass
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
