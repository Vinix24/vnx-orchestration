"""No tool call writes the plugin's settings files (OI-2156).

Every hook reads `<project>/.claude/context-rotation.json`, else `~/.claude/context-rotation.json`
(context_budget.load_settings), and does nothing at all when the file it reads says
`"enabled": false`. Below the hard band a model could Write that, after which the hard band
enforced nothing (deepseek on #2092). So a Write, Edit, MultiEdit or NotebookEdit that lands on
either settings file, existing or not, is denied the way a write into `.claude/rotation/` is: in
every band, in plan mode, after a release and inside a subagent, judged by where it really lands,
and never counted toward hard_max_denials.

`attempt` plays the part of Claude Code, as in test_storage_writes: it runs the PreToolUse hook and
carries the write out when the hook lets it through, so a test sees what the plugin reads next.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from conftest import (BELOW, FORCE, HARD, PKG, WARN, Session, at, bash, decision, goes_ahead, output, reason,
                      rotation, settings_file, state_of)

sys.path.insert(0, str(PKG / "hooks"))

import context_budget
import context_guard
import hook_runtime

BANDS = [BELOW, WARN, FORCE]
BAND_IDS = ["low", "warn", "force"]
DISABLED = json.dumps({"enabled": False})
USER_SETTINGS = json.dumps({"hard_max_denials": 50})


@pytest.fixture
def s(tmp_path, project):
    return Session(tmp_path, project, "sess-a")


def project_settings(session):
    return session.project / ".claude" / "context-rotation.json"


def home_settings(home):
    return home / ".claude" / "context-rotation.json"


def writer_input(tool, path, text=DISABLED):
    if tool == "Write":
        return {"file_path": str(path), "content": text}
    if tool == "NotebookEdit":
        return {"notebook_path": str(path), "new_source": text}
    return {"file_path": str(path), "old_string": "50", "new_string": "5000"}


def attempt(session, path, tool="Write", payload=None, **extra):
    """The PreToolUse hook for a write of `{"enabled": false}` at `path`, then that write when the hook lets it through.

    The write goes where the kernel takes the path as written, with a leading `~` the home directory.
    """
    tool_input = writer_input(tool, path)
    if payload is None:
        proc = session.pre_tool(tool, tool_input, **extra)
    else:
        proc = session.hook("context_guard", dict(payload, tool_name=tool, tool_input=tool_input))
    if goes_ahead(proc):
        landed = Path(os.path.expanduser(str(path)))
        landed.parent.mkdir(parents=True, exist_ok=True)
        landed.write_text(DISABLED)
    return proc


def keeps_settings(proc, settings):
    """The denial for a write of a settings file: it names that file and says the user changes it."""
    assert decision(proc) == "deny", proc.stdout
    text = reason(proc)
    assert "it writes %s, the context-rotation plugin's settings file" % settings in text
    assert "the user changes it themselves" in text
    return text


def still_enforced(session):
    """The plugin is still on: the hard band denies a shell command."""
    at(session, HARD)
    assert decision(bash(session)) == "deny"


# --- the guard and the reader name the same two files -----------------------------------------

def test_the_guard_takes_the_files_load_settings_reads(tmp_path):
    proj, home = tmp_path / "p", tmp_path / "h"
    paths = context_budget.settings_paths(str(proj), home=str(home))
    assert paths == [str(proj / ".claude" / "context-rotation.json"), str(home / ".claude" / "context-rotation.json")]
    Path(paths[1]).parent.mkdir(parents=True)
    Path(paths[1]).write_text(json.dumps({"window_tokens": 111}))
    assert context_budget.load_settings(str(proj), home=str(home))["window_tokens"] == 111
    Path(paths[0]).parent.mkdir(parents=True)
    Path(paths[0]).write_text(json.dumps({"window_tokens": 222}))
    assert context_budget.load_settings(str(proj), home=str(home))["window_tokens"] == 222


def test_without_a_project_only_the_home_file_is_named(tmp_path):
    home = tmp_path / "h"
    assert context_budget.settings_paths(None, home=str(home)) == [str(home / ".claude" / "context-rotation.json")]
    assert context_budget.settings_paths("", home=str(home)) == [str(home / ".claude" / "context-rotation.json")]


def test_the_home_directory_defaults_to_the_one_load_settings_uses(isolated_env):
    assert context_budget.settings_paths(None) == [str(home_settings(isolated_env))]


# --- deepseek's reproduction: switching the plugin off below the hard band ---------------------

@pytest.mark.parametrize("size", BANDS, ids=BAND_IDS)
def test_disabling_the_plugin_in_the_project_settings_is_denied_and_the_hard_band_still_holds(s, size):
    at(s, size)
    proc = attempt(s, project_settings(s))
    still_enforced(s)
    keeps_settings(proc, project_settings(s))
    assert not project_settings(s).exists()


@pytest.mark.parametrize("size", BANDS, ids=BAND_IDS)
def test_disabling_the_plugin_in_the_home_settings_is_denied_and_the_hard_band_still_holds(s, isolated_env, size):
    at(s, size)
    proc = attempt(s, home_settings(isolated_env))
    still_enforced(s)
    keeps_settings(proc, home_settings(isolated_env))
    assert not home_settings(isolated_env).exists()


@pytest.mark.parametrize("size", BANDS, ids=BAND_IDS)
def test_the_home_settings_written_from_a_tilde_path_are_denied(s, isolated_env, size):
    at(s, size)
    proc = attempt(s, "~/.claude/context-rotation.json")
    still_enforced(s)
    keeps_settings(proc, home_settings(isolated_env))
    assert not home_settings(isolated_env).exists()


@pytest.mark.parametrize("size", BANDS + [HARD], ids=BAND_IDS + ["hard"])
def test_an_existing_settings_file_is_not_changed_by_any_file_tool(s, isolated_env, size):
    settings_file(s.project, hard_max_denials=50)
    home_settings(isolated_env).parent.mkdir()
    home_settings(isolated_env).write_text(USER_SETTINGS)
    stored = project_settings(s).read_bytes()
    at(s, size)
    for path in (project_settings(s), home_settings(isolated_env)):
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            keeps_settings(s.pre_tool(tool, writer_input(tool, path)), path)
    assert project_settings(s).read_bytes() == stored
    assert home_settings(isolated_env).read_text() == USER_SETTINGS


def test_a_settings_file_is_denied_before_its_claude_directory_exists(s, isolated_env):
    at(s, BELOW)
    for path in (project_settings(s), home_settings(isolated_env)):
        keeps_settings(attempt(s, path), path)
        assert not path.parent.exists()  # the check made nothing


def test_neither_plan_mode_nor_a_release_nor_an_unknown_size_opens_the_settings(s):
    at(s, FORCE)
    keeps_settings(attempt(s, project_settings(s), permission_mode="plan"), project_settings(s))
    keeps_settings(attempt(s, project_settings(s), transcript_path=str(s.project / "missing.jsonl")),
                   project_settings(s))
    assert s.expand("release").returncode == 0
    assert state_of(s)["inform_only"] == "release"
    keeps_settings(attempt(s, project_settings(s)), project_settings(s))
    assert not project_settings(s).exists()


def test_a_denial_for_a_settings_file_never_counts_toward_the_cap(s):
    settings_file(s.project, hard_max_denials=2)
    stored = project_settings(s).read_bytes()
    at(s, HARD)
    for _ in range(3):
        keeps_settings(attempt(s, project_settings(s)), project_settings(s))
    assert state_of(s)["denials"] == 0 and state_of(s)["inform_only"] is None
    assert decision(bash(s)) == "deny"  # the cap was not reached by the denials above
    assert project_settings(s).read_bytes() == stored


def test_a_subagent_cannot_switch_the_plugin_off_either(s, isolated_env):
    at(s, FORCE)
    payload = s.payload("12-PreToolUse-inside-subagent")
    for path in (project_settings(s), home_settings(isolated_env)):
        proc = attempt(s, path, payload=payload)
        keeps_settings(proc, path)
        assert "systemMessage" not in output(proc)
    still_enforced(s)


# --- where the write lands, not how the path is written -----------------------------------------

def test_dot_dot_in_the_path_does_not_get_a_write_in(s, isolated_env):
    (s.project / "src").mkdir()
    (isolated_env / "work").mkdir()
    at(s, FORCE)
    keeps_settings(attempt(s, s.project / "src" / ".." / ".claude" / "context-rotation.json"), project_settings(s))
    keeps_settings(attempt(s, isolated_env / "work" / ".." / ".claude" / "context-rotation.json"),
                   home_settings(isolated_env))
    still_enforced(s)


def test_a_symlink_that_leads_to_a_settings_file_does_not_get_a_write_in(s, isolated_env):
    settings_file(s.project, hard_max_denials=50)
    stored = project_settings(s).read_bytes()
    (s.project / "conf.json").symlink_to(project_settings(s))
    (s.project / "cfg").symlink_to(s.project / ".claude")
    (s.project / "home-cfg").symlink_to(isolated_env)  # its .claude is not made yet
    at(s, FORCE)
    keeps_settings(attempt(s, s.project / "conf.json"), project_settings(s))
    keeps_settings(attempt(s, s.project / "cfg" / "context-rotation.json"), project_settings(s))
    keeps_settings(attempt(s, s.project / "home-cfg" / ".claude" / "context-rotation.json"),
                   home_settings(isolated_env))
    assert project_settings(s).read_bytes() == stored
    assert not home_settings(isolated_env).exists()


def test_dot_dot_after_a_symlink_lands_where_the_kernel_takes_it(s):
    rotation(s.project).mkdir(parents=True)
    (s.project / "foo").symlink_to(rotation(s.project))  # foo/.. is .claude for the kernel
    at(s, FORCE)
    keeps_settings(attempt(s, s.project / "foo" / ".." / "context-rotation.json"), project_settings(s))
    assert not project_settings(s).exists()
    still_enforced(s)


def test_the_path_with_dot_dot_collapsed_by_name_counts_too(tmp_path, s):
    """A caller that collapses `..` by name before it opens the path (Node's path.resolve does) lands there."""
    elsewhere = tmp_path / "elsewhere" / "deep"
    elsewhere.mkdir(parents=True)
    (s.project / "away").symlink_to(elsewhere)  # for the kernel away/.. is tmp/elsewhere, outside the project
    at(s, FORCE)
    by_name = s.project / "away" / ".." / ".claude" / "context-rotation.json"
    keeps_settings(s.pre_tool("Write", writer_input("Write", by_name)), project_settings(s))


def test_a_case_variant_of_the_existing_settings_file_is_that_file_where_the_file_system_says_so(s):
    settings_file(s.project, hard_max_denials=50)
    stored = project_settings(s).read_bytes()
    variant = project_settings(s).parent / "CONTEXT-ROTATION.JSON"
    folds_case = variant.exists()  # decided before the attempt can create it
    at(s, FORCE)
    proc = attempt(s, variant)
    if folds_case:  # a case-insensitive file system, the macOS default: the same file
        keeps_settings(proc, project_settings(s))
        assert project_settings(s).read_bytes() == stored
    else:  # another file, which the plugin never reads
        assert goes_ahead(proc)
        assert project_settings(s).read_bytes() == stored


def test_a_case_variant_of_a_settings_file_not_made_yet_is_kept_out(s):
    at(s, BELOW)
    keeps_settings(attempt(s, s.project / ".Claude" / "Context-Rotation.json"), project_settings(s))


# --- the files next to them stay writable --------------------------------------------------------

@pytest.mark.parametrize("name", [
    ".claude/settings.json", ".claude/settings.local.json", ".claude/context-rotation.json.bak",
    ".claude/context-rotation.jsonx", ".claude/old-context-rotation.json", "context-rotation.json",
    ".claude/rotation-notes.md", "src/.claude/context-rotation.json",
])
@pytest.mark.parametrize("size", [BELOW, FORCE], ids=["low", "force"])
def test_a_file_next_to_a_settings_file_goes_ahead(s, isolated_env, name, size):
    settings_file(s.project, hard_max_denials=50)
    at(s, size)
    for root in (s.project, isolated_env):
        for tool in ("Write", "Edit", "MultiEdit"):
            assert goes_ahead(s.pre_tool(tool, writer_input(tool, root / name))), (root, name, tool)


def test_reading_a_settings_file_stays_allowed(s, isolated_env):
    settings_file(s.project, hard_max_denials=50)
    at(s, FORCE)
    for path in (project_settings(s), home_settings(isolated_env)):
        assert goes_ahead(s.pre_tool("Read", {"file_path": str(path)}))


# --- one check that cannot be made hides no other ------------------------------------------------

def test_a_rotation_directory_that_cannot_be_checked_does_not_open_the_settings(tmp_path, s, monkeypatch, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (s.project / ".claude").mkdir()
    rotation(s.project).symlink_to(elsewhere)
    # The gate passed, then the directory was swapped: the rotation check fails, the settings check still decides.
    monkeypatch.setattr(hook_runtime.rotation_state, "storage_problem", lambda project_dir: None)
    at(s, BELOW)
    payload = s.payload("04-PreToolUse-Bash", tool_name="Write", tool_input=writer_input("Write", project_settings(s)))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("CLAUDE_PID", str(s.pid))
    with pytest.raises(SystemExit) as done:
        hook_runtime.main(context_guard.run)
    assert done.value.code == 0
    out = capsys.readouterr().out
    specific = (json.loads(out) if out else {}).get("hookSpecificOutput", {})
    assert specific.get("permissionDecision") == "deny", out
    assert "it writes %s, the context-rotation plugin's settings file" % project_settings(s) in (
        specific["permissionDecisionReason"])
