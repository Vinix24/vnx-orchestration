"""SessionStart entry: a fresh session continues from its predecessor's handover (loop step 8).

- source "clear": the ready-markers of the project whose process identity (the
  `claude` process, which survives /clear) matches this session's are checked
  (sha256, age, transcript). Exactly one current match is claimed by atomic
  rename, copied to the archive, logged in injections.jsonl and returned as
  additionalContext (or as a pointer to the archive copy above 9,000 characters);
  the claimed marker is removed last.
- no match, and any source other than "compact": handovers not continued yet are
  listed for the user with the `resume` hint, never dropped silently.
- any source: the archive is trimmed to archive_keep.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import context_budget
import handover_injection
import hook_runtime
import rotation_state

INJECTED_NOTICE = "context-rotation: continued from the handover of the previous session (%s)."


def current_matches(project_dir, session_id, identity, settings):
    """(path, marker, handover bytes) for each current ready-marker of this `claude` process.

    A match that is no longer current becomes outdated here. Call under the project lock.
    """
    found = []
    for path, marker in rotation_state.markers_of_kind(project_dir, "ready"):
        if marker["session_id"] == session_id or marker.get("process") != identity:
            continue
        data = rotation_state.read_bytes(marker["handover"])
        reason = rotation_state.outdated_reason(marker, settings, data)
        if reason:
            rotation_state.mark_outdated(project_dir, marker["session_id"], reason, True)
            continue
        found.append((path, marker, data))
    return found


def emit_handover(text, archive):
    hook_runtime.emit(hook_runtime.context_output("SessionStart", text, INJECTED_NOTICE % archive))


def inject_on_clear(env, run_state, project_dir, session_id, settings):
    """True when exactly one handover was claimed and emitted."""
    run_state.at("list markers")
    if not rotation_state.markers_of_kind(project_dir, "ready"):
        return False
    run_state.at("identify process")
    identity = rotation_state.process_identity(env)
    if identity is None:
        return False
    run_state.at("claim handover")
    with rotation_state.project_lock(project_dir):
        found = current_matches(project_dir, session_id, identity, settings)
        if len(found) != 1:
            return False
        path, marker, data = found[0]
        taken = rotation_state.claim(project_dir, path, session_id)
    if taken is None:
        return False
    claimed, claimed_marker = taken
    handover_injection.deliver(
        project_dir, claimed, claimed_marker, data, session_id, "clear", emit_handover, run_state.at)
    return True


def run(payload, env, run_state):
    if payload.get("agent_id"):
        return
    session_id = payload.get("session_id")
    if not rotation_state.is_safe_session_id(session_id):
        return
    project_dir = rotation_state.project_dir_for(payload, env)
    run_state.project_dir = project_dir
    run_state.at("load settings")
    settings = context_budget.load_settings(project_dir)
    if not settings["enabled"]:
        return
    run_state.at("trim archive")
    rotation_state.trim_archive(project_dir, settings["archive_keep"])
    source = payload.get("source")
    if source == "compact":
        return
    if source == "clear" and inject_on_clear(env, run_state, project_dir, session_id, settings):
        return
    run_state.at("list handovers")
    notice = handover_injection.start_notice(project_dir, session_id)
    if notice:
        hook_runtime.emit({"systemMessage": notice})


if __name__ == "__main__":
    hook_runtime.main(run)
