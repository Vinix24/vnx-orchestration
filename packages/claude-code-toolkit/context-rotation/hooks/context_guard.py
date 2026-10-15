"""UserPromptSubmit entry: warns when the session's context is getting full.

An unexpected failure never blocks the prompt: it leaves one line on stderr and one
in rotation/errors.log, and the hook exits 1 (non-blocking; never 2). The only
other effect this hook has is JSON on stdout.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import context_budget
import rotation_state

UNKNOWN_NOTICE_AFTER = 3
UNKNOWN_NOTICE = (
    "context-rotation cannot measure this session's context, so it cannot warn "
    "before the context fills up. It will not repeat this notice."
)


CONTEXT = {"step": "read payload", "project_dir": None}


def step(name):
    CONTEXT["step"] = name


def project_dir_for(payload):
    return os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()


def warn_message(size, settings, project_dir, session_id):
    window = settings["window_tokens"]
    percent = size * 100 / window
    return (
        "Context is %d of %d tokens (%.0f%%). Wrap up the current work and prepare a "
        "handover: write the state of this session (goal, decisions, what is done, "
        "what is next) to %s so a fresh session can continue from it."
        % (size, window, percent, rotation_state.handover_path(project_dir, session_id))
    )


def emit(document):
    sys.stdout.write(json.dumps(document))
    sys.stdout.flush()


def handle_known(size, settings, project_dir, session_id, state):
    if state.get("unknown_count"):
        state["unknown_count"] = 0
        step("save state")
        rotation_state.save_state(project_dir, session_id, state)
    if context_budget.band(size, settings) in ("warn", "force", "hard"):
        # The handover path is named in the instruction below: protect it first.
        step("ignore protection")
        rotation_state.ensure_gitignore(project_dir)
        step("emit warning")
        text = warn_message(size, settings, project_dir, session_id)
        emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}})


def handle_unknown(project_dir, session_id, state):
    state["unknown_count"] = int(state.get("unknown_count") or 0) + 1
    due = state["unknown_count"] >= UNKNOWN_NOTICE_AFTER and not state.get("notice_sent")
    if due:
        state["notice_sent"] = True
        state["notice_sent_at"] = rotation_state.utc_now()
        state["notice_sent_band"] = "unknown"
        state["notice_sent_size"] = None
        state["notice_sent_unknown_count"] = state["unknown_count"]
    step("save state")
    rotation_state.save_state(project_dir, session_id, state)
    if due:
        emit({"systemMessage": UNKNOWN_NOTICE})


def run(payload):
    if payload.get("agent_id"):
        return
    session_id = payload.get("session_id")
    if not rotation_state.is_safe_session_id(session_id):
        return
    project_dir = project_dir_for(payload)
    CONTEXT["project_dir"] = project_dir
    step("load settings")
    settings = context_budget.load_settings(project_dir)
    if not settings["enabled"]:
        return
    step("measure")
    size = context_budget.measure(payload.get("transcript_path"))
    step("load state")
    state = rotation_state.load_state(project_dir, session_id)
    if size is None:
        handle_unknown(project_dir, session_id, state)
    else:
        handle_known(size, settings, project_dir, session_id, state)


def report_failure(error):
    line = "context-rotation: %s failed: %s: %s" % (CONTEXT["step"], type(error).__name__, error)
    line = " ".join(line.split())
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        # stderr may be closed; the log below still gets the line.
        pass
    if CONTEXT["project_dir"]:
        rotation_state.log_error(CONTEXT["project_dir"], "%s %s" % (rotation_state.utc_now(), line))


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        sys.exit(0)  # not our payload: nothing to measure, nothing to report
    try:
        if isinstance(payload, dict):
            run(payload)
    except Exception as error:
        report_failure(error)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
