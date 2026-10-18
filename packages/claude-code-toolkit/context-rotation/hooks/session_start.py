"""SessionStart entry: a fresh session continues from its predecessor's handover (loop step 8).

- source "clear": the ready-markers of the project whose process identity (the
  `claude` process, which survives /clear) matches this session's are checked
  (sha256, age, transcript). Exactly one current match is claimed: its marker
  moves to the claim name of its generation, which never replaces a file (its
  line in injections.jsonl first). The handover is copied to the archive from the
  bytes the claim read; the original moves into the archive under a name of its
  own (`<time>-<origin>.original`) and never becomes that copy. The injection is
  logged in injections.jsonl and returned as additionalContext (or as a pointer to
  the archive copy above 9,000 characters); once that output is written a
  `delivered` line follows, and the claimed marker is removed last, only after that
  line. An output that cannot be written leaves the claim and no `delivered` line,
  so the automatic /clear types nothing after it; a `delivered` line that cannot be
  written leaves the claim too. The user gets one line naming the archive copy;
  when the pane's automatic /clear typed this clear (auto_clear.typed_by_job) and
  it is the first one for this user, that line also says how to turn auto_clear
  off. A marker that fails the path checks is never used: it is reported once and
  left on disk.
- no match, and any source other than "compact": handovers not continued yet are
  listed for the user with the `resume` hint, never dropped silently.
- any source: the archive is trimmed to archive_keep, and injections.jsonl to a
  bound tied to it (rotation_state.trim_journal).

Nothing happens when the project's storage fails the storage gate
(hook_runtime.open_project). A storage failure (OSError, LockTimeout) while
trimming, claiming, delivering or listing is logged and the hook still exits 0,
also after the handover was emitted; a claim that could not be finished stays for
`resume`.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))

import auto_clear
import handover_injection
import hook_runtime
import rotation_state

INJECTED_NOTICE = "context-rotation: continued from the handover of the previous session (%s)."


def current_matches(project_dir, session_id, identity, settings):
    """(path, marker) for each current ready-marker of this `claude` process.

    A match that is no longer current becomes outdated here. Call under the project lock.
    """
    found = []
    for path, marker in rotation_state.markers_of_kind(project_dir, "ready"):
        if marker["session_id"] == session_id or marker.get("process") != identity:
            continue
        data = rotation_state.read_bytes(project_dir, marker["handover"])
        reason = rotation_state.outdated_reason(marker, settings, data)
        if reason:
            rotation_state.mark_outdated(project_dir, marker["session_id"], reason, True)
            continue
        found.append((path, marker))
    return found


def emit_handover(text, archive, typed_by_job=False):
    """The handover for the model (M) and one line for the user (U); after the first automatic clear, how to stop it."""
    line = INJECTED_NOTICE % archive
    if typed_by_job and auto_clear.first_on_machine():
        line += " " + auto_clear.OFF_HINT
    hook_runtime.emit(hook_runtime.context_output("SessionStart", text, line))


def take_current(env, run_state, project_dir, session_id, settings):
    """The one current handover of this `claude` process, claimed: (claimed path, marker, bytes), or None."""
    run_state.at("list markers")
    if not rotation_state.markers_of_kind(project_dir, "ready"):
        return None
    run_state.at("identify process")
    identity = rotation_state.process_identity(env)
    if identity is None:
        return None
    run_state.at("claim handover")
    with rotation_state.project_lock(project_dir):
        found = current_matches(project_dir, session_id, identity, settings)
        if len(found) != 1:
            return None
        return rotation_state.claim(project_dir, found[0][0], session_id, as_checked=True)


def inject_on_clear(env, run_state, project_dir, session_id, settings):
    """True when exactly one handover was claimed and emitted.

    False after a storage failure on the way: it is logged, nothing was emitted,
    and a claim already taken stays for `resume`.
    """
    try:
        taken = take_current(env, run_state, project_dir, session_id, settings)
    except (OSError, rotation_state.LockTimeout) as error:
        rotation_state.log_failure(project_dir, run_state.step, error)
        return False
    if taken is None:
        return False
    claimed, claimed_marker, data = taken
    typed = auto_clear.typed_by_job(env, project_dir, claimed_marker)

    def emit(text, archive):
        emit_handover(text, archive, typed)

    return handover_injection.deliver(
        project_dir, claimed, claimed_marker, data, session_id, "clear", emit, run_state.at) is not None


def waiting_notice(env, run_state, project_dir, session_id):
    """The list of handovers not continued yet; None when none wait or listing them failed (logged)."""
    run_state.at("list handovers")
    try:
        return handover_injection.start_notice(project_dir, session_id, env)
    except (OSError, rotation_state.LockTimeout) as error:
        rotation_state.log_failure(project_dir, run_state.step, error)
        return None


def run(payload, env, run_state):
    opened = hook_runtime.open_project(payload, env, run_state)
    if opened is None:
        return
    project_dir, session_id, settings = opened
    run_state.at("trim archive")
    rotation_state.trim_archive(project_dir, settings["archive_keep"])
    rotation_state.trim_journal(project_dir, settings["archive_keep"])
    source = payload.get("source")
    if source == "compact":
        return
    if source == "clear" and inject_on_clear(env, run_state, project_dir, session_id, settings):
        return
    notice = waiting_notice(env, run_state, project_dir, session_id)
    if notice:
        hook_runtime.emit({"systemMessage": notice})


if __name__ == "__main__":
    hook_runtime.main(run)
