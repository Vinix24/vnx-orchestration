"""PostToolUse entry for Write|Edit: checks this session's handover (loop step 5).

When the written file is the session's own handover path, the handover is held
against the contract (a numbered first step under "## Next steps", a "## Running"
section, at most handover_max_chars). Valid: the ready-marker is written under the
project lock, with the handover's sha256, the session's process identity, the
transcript position and the write's tool_use_id. The position is that of the
write's own transcript entry, taken before the handover is read, so work that
lands while this hook runs lies after it. Invalid: the model is told exactly what
is missing (M), and an earlier ready-marker becomes outdated. Valid or not, the
write clears the denials the force and hard bands counted toward
hard_max_denials (band_guard.on_handover_write), unless a symlink at the
handover path took it to another file (band_guard.writes_handover).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import band_guard
import handover_contract
import hook_runtime
import rotation_state
import state_fields
import transcript_work

SAVED = (
    "The handover at %s is valid and marked ready. End your turn now without further "
    "tool calls: any further work makes it outdated."
)
NOT_READY = "The handover at %s is not ready yet: %s. Fix it with another Write or Edit of that file."


def run(payload, env, run_state):
    opened = hook_runtime.open_project(payload, env, run_state)
    if opened is None:
        return
    project_dir, session_id, settings = opened
    handover = rotation_state.handover_path(project_dir, session_id)
    if not transcript_work.is_handover_write(payload.get("tool_name"), payload.get("tool_input"), handover):
        return
    session = hook_runtime.Session(project_dir, session_id, settings)
    band_guard.on_handover_write(payload, session, run_state)
    run_state.at("check transcript")
    cursor = transcript_work.write_position(payload.get("transcript_path"), payload.get("tool_use_id"))
    run_state.at("read handover")
    with rotation_state.project_lock(project_dir):  # never while a cleanup has the file set aside
        data = rotation_state.read_bytes(project_dir, handover)
    problems = handover_contract.problems(data, settings["handover_max_chars"])
    if problems:
        run_state.at("mark outdated")
        rotation_state.mark_outdated(project_dir, session_id, state_fields.REWRITTEN_INVALID, True)
        hook_runtime.emit(hook_runtime.context_output("PostToolUse", NOT_READY % (handover, "; ".join(problems))))
        return
    run_state.at("write ready marker")
    marker = rotation_state.new_marker(session_id, handover, data, env, cursor, payload.get("tool_use_id"))
    rotation_state.mark_ready(project_dir, marker)
    hook_runtime.emit(hook_runtime.context_output("PostToolUse", SAVED % handover))


if __name__ == "__main__":
    hook_runtime.main(run)
