"""No tool call writes into the plugin's own directory (OI-2151).

The force and hard bands keep their state in `.claude/rotation/state/<session>.enforcement.json`,
and every band message names the handover path, which carries the session id. A model that
wrote that state with `inform_only: release` released its own guard before the hard band
was reached. So a Write, Edit, MultiEdit or NotebookEdit into `.claude/rotation/`, other
than of the session's own handover, is denied in every band, also below warn and inside a
subagent, and such a denial never counts toward hard_max_denials.

A hook only decides. `attempt` plays the part of Claude Code: it runs the PreToolUse hook
and carries the write out when the hook lets it through, so a test sees what lands on disk.
"""
from __future__ import annotations

import errno
import io
import json
import os
import sys
import tempfile

import pytest

from conftest import (BELOW, FORCE, HARD, PKG, WARN, Session, at, bash, context_of, decision, goes_ahead, held_back,
                      output, reason, refused_once, rotation, settings_file, snapshot, state_of)

sys.path.insert(0, str(PKG / "hooks"))

import context_guard
import enforcement_state
import hook_runtime

LEVEL = {BELOW: "low", WARN: "low", FORCE: "force", HARD: "hard"}
BANDS = [BELOW, WARN, FORCE, HARD]
BAND_IDS = ["low", "warn", "force", "hard"]
NOT_RELEASED, RELEASED = '"inform_only": null', '"inform_only": "release"'


@pytest.fixture
def s(tmp_path, project):
    return Session(tmp_path, project, "sess-a")


def state_file(session):
    return rotation(session.project) / "state" / ("%s.enforcement.json" % session.session_id)


def errors_log(project):
    path = rotation(project) / "errors.log"
    return path.read_text() if path.exists() else ""


def plant(session, level):
    """A state the plugin writes, at `level`, in the plugin's format: its bytes."""
    state = enforcement_state.fresh()
    if level != "low":
        state.update(level=level, crossing=1, told=1)
    state_file(session).parent.mkdir(parents=True, exist_ok=True)
    state_file(session).write_text(json.dumps(state, indent=1))
    return state_file(session).read_bytes()


def forged(path):
    """What the model writes to release its own guard: the state at `path` with inform_only release."""
    text = path.read_text() if path.exists() else json.dumps(enforcement_state.fresh(), indent=1)
    return text.replace(NOT_RELEASED, RELEASED)


def tool_input(tool, path, text):
    if tool == "Write":
        return {"file_path": str(path), "content": text}
    return {"file_path": str(path), "old_string": NOT_RELEASED, "new_string": RELEASED}


def carry_out(tool, path, text):
    """The write Claude Code makes once the hook let the call through."""
    if tool == "Write":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    else:
        path.write_text(path.read_text().replace(NOT_RELEASED, RELEASED))


def attempt(session, tool, path, payload=None):
    """The PreToolUse hook for a Write or an Edit of `path`, then the write itself when the hook lets it through."""
    text = forged(path)
    if payload is None:
        proc = session.pre_tool(tool, tool_input(tool, path, text))
    else:
        proc = session.hook("context_guard", dict(payload, tool_name=tool, tool_input=tool_input(tool, path, text)))
    if goes_ahead(proc):
        carry_out(tool, path, text)
    return proc


def keeps_out(proc, session):
    """The denial for a write into the plugin's directory: it names that directory and says the plugin keeps it."""
    assert decision(proc) == "deny", proc.stdout
    text = reason(proc)
    assert "writes into %s, which the context-rotation plugin keeps for its own files" % rotation(session.project) in text
    assert "except a write of this session's handover at %s" % session.handover in text
    return text


# --- every band, also below warn ------------------------------------------------------------

@pytest.mark.parametrize("tool", ["Write", "Edit"])
@pytest.mark.parametrize("size", BANDS, ids=BAND_IDS)
def test_a_write_into_the_state_directory_is_denied_in_every_band_and_changes_nothing(s, size, tool):
    stored = plant(s, LEVEL[size])
    at(s, size)
    proc = attempt(s, tool, state_file(s))
    keeps_out(proc, s)
    assert state_file(s).read_bytes() == stored  # neither the write nor a counted denial changed it


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_a_forged_release_written_at_force_no_longer_lifts_the_hard_band(s, tool):
    at(s, FORCE)
    told = s.prompt("go on")  # every band message names the handover path, and with it the session id
    assert str(s.handover) in context_of(told)
    assert goes_ahead(s.pre_tool("Read", {"file_path": str(state_file(s))}))  # reading the schema stays allowed
    written = attempt(s, tool, state_file(s))
    at(s, HARD)
    assert decision(bash(s)) == "deny"  # the hard band still holds
    assert held_back(s.stop())
    keeps_out(written, s)
    assert state_of(s)["level"] == "hard" and state_of(s)["inform_only"] is None


def test_a_state_written_below_warn_before_the_directory_exists_is_denied(s):
    at(s, BELOW)
    proc = attempt(s, "Write", state_file(s))
    keeps_out(proc, s)
    assert not rotation(s.project).exists()  # the check made nothing
    at(s, HARD)
    assert decision(bash(s)) == "deny"


def test_a_denial_for_the_plugin_directory_never_counts_toward_the_cap(s):
    settings_file(s.project, hard_max_denials=2)
    at(s, FORCE)
    s.prompt("go on")
    for _ in range(3):
        keeps_out(attempt(s, "Write", state_file(s)), s)
    assert state_of(s)["denials"] == 0 and state_of(s)["inform_only"] is None
    at(s, HARD)
    assert decision(bash(s)) == "deny"  # the cap was not reached by the denials above


def test_neither_a_release_nor_plan_mode_nor_an_unknown_size_opens_the_directory(s):
    stored = plant(s, "hard")
    at(s, HARD)
    write = {"file_path": str(state_file(s)), "content": forged(state_file(s))}
    keeps_out(s.pre_tool("Write", write, permission_mode="plan"), s)
    keeps_out(s.pre_tool("Write", write, transcript_path=str(s.project / "missing.jsonl")), s)
    assert state_file(s).read_bytes() == stored
    assert s.expand("release").returncode == 0
    assert state_of(s)["inform_only"] == "release"
    released = state_file(s).read_bytes()
    keeps_out(attempt(s, "Edit", state_file(s)), s)
    assert state_file(s).read_bytes() == released


def test_a_path_from_the_home_directory_is_kept_out(tmp_path, isolated_env):
    s = Session(tmp_path, isolated_env / "proj", "sess-a")
    s.project.mkdir()
    stored = plant(s, "force")
    at(s, FORCE)
    home_relative = "~/proj/.claude/rotation/state/%s" % state_file(s).name
    keeps_out(s.pre_tool("Write", {"file_path": home_relative, "content": forged(state_file(s))}), s)
    assert state_file(s).read_bytes() == stored


@pytest.mark.parametrize("size", BANDS, ids=BAND_IDS)
def test_any_file_of_the_plugin_is_kept_out_not_just_the_state(s, size):
    settings_file(s.project, hard_max_denials=50)
    at(s, size)
    place = rotation(s.project)
    for path in (place / "handover-sess-b.md", place / "ready-sess-a.json", place / "enforcement.jsonl",
                 place / "errors.log", place / ".gitignore", place / "archive" / "20261017T000000000000Z-sess-a.md",
                 place / "state" / "new" / "x.json", place / "state" / "sess-a.json"):
        for tool in ("Write", "Edit", "MultiEdit"):
            keeps_out(s.pre_tool(tool, {"file_path": str(path), "content": "x"}), s)
        keeps_out(s.pre_tool("NotebookEdit", {"notebook_path": str(path), "new_source": "x"}), s)


# --- the session's own handover stays writable ----------------------------------------------

@pytest.mark.parametrize("size", BANDS, ids=BAND_IDS)
def test_the_sessions_own_handover_stays_writable(s, size):
    at(s, size)
    for tool in ("Write", "Edit"):
        assert goes_ahead(s.pre_tool(tool, {"file_path": str(s.handover), "content": "x"})), tool
    s.handover.parent.mkdir(parents=True, exist_ok=True)
    s.handover.write_text("a first draft")
    for tool in ("Write", "Edit"):
        assert goes_ahead(s.pre_tool(tool, {"file_path": str(s.handover), "content": "x"})), tool


def test_a_multiedit_of_the_own_handover_is_not_kept_out_below_hard(s):
    at(s, FORCE)
    assert goes_ahead(s.pre_tool("MultiEdit", {"file_path": str(s.handover), "edits": []}))


@pytest.mark.parametrize("size", [BELOW, FORCE], ids=["low", "force"])
def test_the_handover_written_from_the_home_directory_goes_ahead(tmp_path, isolated_env, size):
    s = Session(tmp_path, isolated_env / "proj", "sess-a")
    s.project.mkdir()
    at(s, size)
    home_relative = "~/proj/.claude/rotation/%s" % s.handover.name
    for tool in ("Write", "Edit"):
        assert goes_ahead(s.pre_tool(tool, {"file_path": home_relative, "content": "x"})), tool  # not made yet
    s.handover.parent.mkdir(parents=True, exist_ok=True)
    s.handover.write_text("a first draft")
    for tool in ("Write", "Edit"):
        assert goes_ahead(s.pre_tool(tool, {"file_path": home_relative, "content": "x"})), tool


def test_a_case_variant_of_the_existing_handover_is_that_handover_where_the_file_system_says_so(s):
    s.handover.parent.mkdir(parents=True)
    s.handover.write_text("a first draft")
    at(s, BELOW)
    variant = s.handover.parent / s.handover.name.upper()
    proc = s.pre_tool("Write", {"file_path": str(variant), "content": "x"})
    if variant.exists():  # a case-insensitive file system, the macOS default: the same file
        assert goes_ahead(proc)
    else:  # another file in the plugin's directory
        keeps_out(proc, s)


def test_a_case_variant_of_a_handover_not_written_yet_is_kept_out(s):
    rotation(s.project).mkdir(parents=True)
    at(s, BELOW)
    keeps_out(s.pre_tool("Write", {"file_path": str(rotation(s.project) / s.handover.name.upper()), "content": "x"}), s)


def test_a_form_of_the_path_that_is_the_handover_does_not_exempt_a_form_that_is_not(s):
    (rotation(s.project) / "state" / "sub").mkdir(parents=True)
    (rotation(s.project) / "foo").symlink_to(rotation(s.project) / "state" / "sub")
    at(s, FORCE)
    path = rotation(s.project) / "foo" / ".." / s.handover.name  # by name the handover, for the kernel state/<that name>
    keeps_out(attempt(s, "Write", path), s)
    assert not (rotation(s.project) / "state" / s.handover.name).exists()


@pytest.mark.parametrize("path", [
    ".claude/rotation-notes.md", ".claude/settings.json", ".claude/rotationx/state/sess-a.enforcement.json",
    "rotation/state/sess-a.enforcement.json", "src/rotation/state.py",
])
def test_a_file_outside_the_plugin_directory_goes_ahead(s, path):
    rotation(s.project).mkdir(parents=True)
    at(s, FORCE)
    for tool in ("Write", "Edit", "MultiEdit"):
        assert goes_ahead(s.pre_tool(tool, {"file_path": str(s.project / path), "content": "x"})), tool


def test_other_tools_are_not_checked_here(s):
    at(s, FORCE)
    assert goes_ahead(s.pre_tool("Read", {"file_path": str(state_file(s))}))
    assert goes_ahead(s.pre_tool("Bash", {"command": "cat > %s" % state_file(s)}))  # the plugin parses no shell


# --- where it lands, not how the path is written -----------------------------------------------

def test_dot_dot_in_the_path_does_not_get_a_write_in(s):
    stored = plant(s, "force")
    (s.project / "src").mkdir()
    at(s, FORCE)
    sneaky = s.project / "src" / ".." / ".claude" / "rotation" / "state" / ("%s.enforcement.json" % s.session_id)
    keeps_out(attempt(s, "Write", sneaky), s)
    assert state_file(s).read_bytes() == stored


def test_a_symlink_elsewhere_that_leads_in_does_not_get_a_write_in(s):
    stored = plant(s, "force")
    (s.project / "notes").symlink_to(state_file(s).parent)
    (s.project / "state.json").symlink_to(state_file(s))
    at(s, FORCE)
    for path in (s.project / "notes" / state_file(s).name, s.project / "state.json"):
        for tool in ("Write", "Edit"):
            keeps_out(attempt(s, tool, path), s)
    assert state_file(s).read_bytes() == stored


def test_a_symlink_inside_that_leads_out_is_kept_out_too(s):
    target = s.project / "src" / "main.py"
    target.parent.mkdir()
    target.write_text(NOT_RELEASED)
    rotation(s.project).mkdir(parents=True)
    (rotation(s.project) / "out.md").symlink_to(target)
    at(s, FORCE)
    (rotation(s.project) / "linked").symlink_to(target.parent)  # a directory inside that leads out
    for tool in ("Write", "Edit"):
        keeps_out(attempt(s, tool, rotation(s.project) / "out.md"), s)
        keeps_out(attempt(s, tool, rotation(s.project) / "linked" / "main.py"), s)
    assert target.read_text() == NOT_RELEASED


def test_a_symlink_at_the_handover_path_does_not_open_the_state(s):
    stored = plant(s, "force")
    s.handover.symlink_to(state_file(s))
    at(s, FORCE)
    for tool in ("Write", "Edit"):
        keeps_out(attempt(s, tool, s.handover), s)
    assert state_file(s).read_bytes() == stored


def test_through_a_symlinked_claude_directory_both_paths_are_kept_out(tmp_path, s):
    real = tmp_path / "claude-real"
    real.mkdir()
    (s.project / ".claude").symlink_to(real)  # the storage gate allows a symlinked .claude
    stored = plant(s, "force")
    at(s, FORCE)
    for path in (state_file(s), real / "rotation" / "state" / state_file(s).name):
        keeps_out(attempt(s, "Write", path), s)
    assert state_file(s).read_bytes() == stored


def case_insensitive(directory):
    """Whether `directory`'s file system folds case, probed with a fresh name that nothing else creates."""
    probe = tempfile.mkdtemp(prefix="casefold-", dir=directory)
    try:
        head, name = os.path.split(probe)
        return os.path.exists(os.path.join(head, name.upper()))
    finally:
        os.rmdir(probe)


def test_a_name_in_another_case_is_kept_out_where_it_is_the_same_directory(s):
    stored = plant(s, "force")
    at(s, FORCE)
    other_case = s.project / ".claude" / "ROTATION" / "state" / state_file(s).name
    folds_case = case_insensitive(rotation(s.project))  # decided before the attempt can create ROTATION
    proc = attempt(s, "Write", other_case)
    if folds_case:
        keeps_out(proc, s)
        assert state_file(s).read_bytes() == stored
    else:  # another directory, which the plugin never reads
        assert goes_ahead(proc)


def test_a_name_in_another_case_is_kept_out_before_the_directory_exists(s):
    at(s, BELOW)
    other_case = s.project / ".claude" / "Rotation" / "state" / state_file(s).name
    keeps_out(attempt(s, "Write", other_case), s)  # on a case-insensitive file system it would become the directory


# --- `..` after a symlink: where the kernel takes the write ---------------------------------

def forged_release_is_kept_out(s, path, tools=("Write", "Edit")):
    """A write of the session's state by `path` is denied, the state is unchanged, and the hard band still holds."""
    stored = plant(s, "force")
    at(s, FORCE)
    for tool in tools:
        keeps_out(attempt(s, tool, path), s)
    assert state_file(s).read_bytes() == stored
    at(s, HARD)
    assert decision(bash(s)) == "deny"


def test_dot_dot_after_a_symlink_into_the_state_directory_is_kept_out(s):
    rotation(s.project).mkdir(parents=True)
    (s.project / "foo").symlink_to(rotation(s.project) / "state")  # foo/.. is .claude/rotation for the kernel
    forged_release_is_kept_out(s, s.project / "foo" / ".." / "state" / state_file(s).name)


def test_dot_dot_after_a_symlink_to_a_sibling_directory_is_kept_out(s):
    (s.project / ".claude" / "other").mkdir(parents=True)
    (s.project / "alias").symlink_to(s.project / ".claude" / "other")  # alias/.. is .claude for the kernel
    forged_release_is_kept_out(s, s.project / "alias" / ".." / "rotation" / "state" / state_file(s).name, ("Edit",))


def test_a_chain_of_two_symlinks_with_dot_dot_between_them_is_kept_out(s):
    (s.project / "x" / "y").mkdir(parents=True)
    (s.project / "one").symlink_to(os.path.join("x", "y"))
    (s.project / "x" / "two").symlink_to(os.path.join("..", ".claude", "rotation"))
    # one -> x/y, its .. is x, and x/two -> .claude/rotation: by name the path is proj/two/state/...
    forged_release_is_kept_out(s, s.project / "one" / ".." / "two" / "state" / state_file(s).name)


def test_the_path_with_dot_dot_collapsed_by_name_counts_too(tmp_path, s):
    """A caller that collapses `..` by name before it opens the path (Node's path.resolve does) lands there."""
    elsewhere = tmp_path / "elsewhere" / "deep"
    elsewhere.mkdir(parents=True)
    (s.project / "away").symlink_to(elsewhere)  # for the kernel away/.. is tmp/elsewhere, outside the project
    plant(s, "force")
    at(s, FORCE)
    by_name = s.project / "away" / ".." / ".claude" / "rotation" / "state" / state_file(s).name
    keeps_out(s.pre_tool("Write", {"file_path": str(by_name), "content": "x"}), s)


# --- a file at the plugin's directory, or at `.claude` above it --------------------------------

def writer_input(tool, path):
    field = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {field: path, "content": "x"}


@pytest.mark.parametrize("made, name", [
    ((), ".claude"), ((), ".claude/rotation"), ((".claude",), ".claude/rotation"),
], ids=["claude-nothing-made", "rotation-nothing-made", "rotation-claude-made"])
def test_a_file_written_at_the_plugin_directory_below_warn_is_denied_and_the_guard_keeps_working(s, made, name):
    """A regular file there made the storage gate refuse the project, so no band was enforced after it."""
    for part in made:
        (s.project / part).mkdir()
    at(s, BELOW)
    keeps_out(attempt(s, "Write", s.project / name), s)
    assert not (s.project / name).is_file()
    at(s, HARD)
    assert decision(bash(s)) == "deny"


@pytest.mark.parametrize("size", BANDS, ids=BAND_IDS)
def test_a_write_of_the_plugin_directory_or_of_claude_itself_is_denied_in_every_band(s, size):
    rotation(s.project).mkdir(parents=True)
    at(s, size)
    for name in (".claude", ".claude/", ".claude/rotation", ".claude/rotation/", ".claude/./rotation",
                 ".claude/rotation/state/.."):
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            keeps_out(s.pre_tool(tool, writer_input(tool, os.path.join(str(s.project), name))), s)
    assert rotation(s.project).is_dir()


# --- a symlinked rotation directory is not followed ------------------------------------------

def test_a_symlinked_rotation_directory_is_not_followed(tmp_path, s):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "state").mkdir(parents=True)
    (elsewhere / "state" / state_file(s).name).write_text(json.dumps(enforcement_state.fresh(), indent=1))
    (s.project / ".claude").mkdir()
    rotation(s.project).symlink_to(elsewhere)
    before = snapshot(elsewhere)
    at(s, FORCE)
    for path in (state_file(s), elsewhere / "state" / state_file(s).name, elsewhere / "notes.md"):
        proc = s.pre_tool("Write", {"file_path": str(path), "content": "x"})
        assert proc.stdout == "", proc.stdout  # the plugin uses nothing there, and decides nothing on it
        refused_once(proc, rotation(s.project))
    assert snapshot(elsewhere) == before


def in_process(monkeypatch, session, payload):
    """context_guard as Claude Code runs it, in this process: its exit status."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("CLAUDE_PID", str(session.pid))
    with pytest.raises(SystemExit) as done:
        hook_runtime.main(context_guard.run)
    return done.value.code


def printed(capsys):
    out = capsys.readouterr().out
    return json.loads(out) if out else {}


def test_a_rotation_directory_swapped_for_a_symlink_after_the_gate_is_not_followed(tmp_path, s, monkeypatch, capsys):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "state").mkdir(parents=True)
    (s.project / ".claude").mkdir()
    rotation(s.project).symlink_to(elsewhere)
    monkeypatch.setattr(hook_runtime.rotation_state, "storage_problem", lambda project_dir: None)  # passed, then swapped
    at(s, BELOW)
    payload = s.payload("04-PreToolUse-Bash", tool_name="Write",
                        tool_input={"file_path": str(elsewhere / "state" / state_file(s).name), "content": "x"})
    assert in_process(monkeypatch, s, payload) == 0
    captured = capsys.readouterr()
    assert "permissionDecision" not in captured.out  # a failed check never denies
    assert "check write of the plugin's own files failed: StorageRefused" in captured.err  # no log in refused storage
    assert list((elsewhere / "state").iterdir()) == []


# --- a failed check never denies -----------------------------------------------------------------

def failing_stat(monkeypatch, inside):
    """Every os.stat of a path inside `inside` fails with EIO."""
    real_stat = os.stat
    prefix = os.path.realpath(str(inside))

    def stat(path, *args, **kwargs):
        if isinstance(path, str) and os.path.realpath(path).startswith(prefix):
            raise OSError(errno.EIO, "injected stat failure", path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)


@pytest.mark.parametrize("size", [BELOW, FORCE], ids=["low", "force"])
def test_an_oserror_in_the_check_lets_the_call_through_and_is_logged(s, monkeypatch, capsys, size):
    stored = plant(s, LEVEL[size])
    at(s, size)
    failing_stat(monkeypatch, rotation(s.project))
    payload = s.payload("04-PreToolUse-Bash", tool_name="Write",
                        tool_input={"file_path": str(state_file(s)), "content": forged(state_file(s))})
    assert in_process(monkeypatch, s, payload) == 0
    assert printed(capsys).get("hookSpecificOutput", {}).get("permissionDecision") is None
    logged = errors_log(s.project)
    assert "check write of the plugin's own files failed" in logged and "injected stat failure" in logged
    assert state_file(s).read_bytes() == stored


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches every directory")
def test_a_directory_that_cannot_be_searched_lets_the_call_through_and_is_logged(s):
    locked = s.project / "locked"
    (locked / "inner").mkdir(parents=True)
    os.chmod(str(locked), 0)
    try:
        at(s, FORCE)
        proc = s.pre_tool("Write", {"file_path": str(locked / "inner" / "x.json"), "content": "x"})
    finally:
        os.chmod(str(locked), 0o700)
    assert goes_ahead(proc)
    assert "check write of the plugin's own files failed" in errors_log(s.project)


# --- one spelling that cannot be judged hides no denial (codex on #2092) ------------------------

LONG = "x" * 300  # longer than any file system allows a name to be: a stat of a path through it fails


def through_a_long_name(project, *parts):
    """`<project>/<LONG>/../<parts>`: the kernel fails on it, a writer that collapses `..` first does not."""
    return os.path.join(str(project), LONG, "..", *parts)


def test_a_spelling_that_cannot_be_judged_does_not_hide_a_write_into_the_state(s):
    stored = plant(s, "force")
    at(s, FORCE)
    path = through_a_long_name(s.project, ".claude", "rotation", "state", state_file(s).name)
    proc = s.pre_tool("Write", tool_input("Write", path, forged(state_file(s))))
    if goes_ahead(proc):  # a writer that collapses `..` by name first (Node's path.resolve) lands in the state
        carry_out("Write", state_file(s), forged(state_file(s)))
    assert state_file(s).read_bytes() == stored
    at(s, HARD)
    assert decision(bash(s)) == "deny"  # the hard band still holds
    keeps_out(proc, s)


def test_a_spelling_that_cannot_be_judged_and_lands_outside_goes_ahead_and_is_logged(s):
    rotation(s.project).mkdir(parents=True)
    at(s, FORCE)
    proc = s.pre_tool("Write", {"file_path": through_a_long_name(s.project, "src", "main.py"), "content": "x"})
    assert goes_ahead(proc)
    logged = errors_log(s.project)
    assert "check write of the plugin's own files failed" in logged and "File name too long" in logged


@pytest.mark.parametrize("size", [BELOW, FORCE, HARD], ids=["low", "force", "hard"])
def test_the_handover_under_a_spelling_that_cannot_be_judged_still_goes_ahead(s, size):
    rotation(s.project).mkdir(parents=True)
    at(s, size)
    path = through_a_long_name(s.project, ".claude", "rotation", s.handover.name)
    for tool in ("Write", "Edit"):
        assert goes_ahead(s.pre_tool(tool, {"file_path": path, "content": "x"})), tool
    assert "File name too long" in errors_log(s.project)


# --- inside a subagent ---------------------------------------------------------------------------

def subagent(session):
    return session.payload("12-PreToolUse-inside-subagent")


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_a_subagent_cannot_write_the_state_either(s, tool):
    stored = plant(s, "force")
    at(s, FORCE)
    proc = attempt(s, tool, state_file(s), payload=subagent(s))
    keeps_out(proc, s)
    assert "systemMessage" not in output(proc)
    assert state_file(s).read_bytes() == stored
    at(s, HARD)
    assert decision(bash(s)) == "deny"


def test_inside_a_subagent_nothing_else_is_checked(s):
    at(s, HARD)
    payload = subagent(s)
    calls = [("Bash", {"command": "make test"}), ("Write", {"file_path": str(s.project / "x.py"), "content": "x"}),
             ("Write", {"file_path": str(s.handover), "content": "x"}), ("Read", {"file_path": str(state_file(s))})]
    for tool, tool_input_ in calls:
        proc = s.hook("context_guard", dict(payload, tool_name=tool, tool_input=tool_input_))
        assert (proc.returncode, proc.stdout) == (0, ""), tool
    assert not rotation(s.project).exists()  # no band, marker or state inside a subagent
