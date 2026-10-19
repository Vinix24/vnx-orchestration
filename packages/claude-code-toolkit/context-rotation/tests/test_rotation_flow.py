"""Outdated markers, the Stop completion path and UserPromptExpansion, through context_guard.py."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from conftest import (
    VALID_HANDOVER, Session, b1_entry, context_of, message_of, rotation, settings_file, stamp,
)

MANUAL = "Handover saved. Type /clear, then any message, to continue in a fresh context."


@pytest.fixture
def saved(tmp_path, project):
    """A session that wrote a valid handover as its last tool call."""
    s = Session(tmp_path, project, "sess-a")
    s.write_handover()
    assert s.marker("ready").exists()
    return s


def outdated(session):
    return json.loads(session.marker("outdated").read_text(encoding="utf-8"))


# --- the manual completion path (loop step 7) ----------------------------

def test_stop_with_a_current_marker_tells_the_user_to_clear(saved):
    proc = saved.finish_turn()
    assert message_of(proc) == MANUAL
    assert "hookSpecificOutput" not in json.loads(proc.stdout)
    assert saved.marker("ready").exists()


def test_the_message_comes_once_per_marker(saved):
    assert message_of(saved.finish_turn()) == MANUAL
    assert saved.stop().stdout == ""
    assert saved.marker("ready").exists()


def test_a_new_marker_is_announced_again(saved):
    saved.finish_turn()
    saved.write_handover(VALID_HANDOVER + "\nMore.\n")
    assert message_of(saved.finish_turn()) == MANUAL


def test_stop_without_a_handover_is_silent(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    proc = s.finish_turn()
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not rotation(project).exists()


def test_reading_after_the_write_keeps_the_marker(saved):
    tool_id = saved.tool_id()
    assert saved.pre_tool("Read", {"file_path": str(saved.handover)}).stdout == ""
    saved.append(b1_entry("tool_use_read", tool_use_id=tool_id, file_path=saved.handover),
                 b1_entry("tool_result", tool_use_id=tool_id, file_path=saved.handover, tool_output="text"))
    assert message_of(saved.finish_turn()) == MANUAL


def test_bookkeeping_entries_after_the_write_keep_the_marker(saved):
    saved.append(*(b1_entry(name, text="x", prompt="p", command_output="o", skill_body="b", thinking="t",
                            signature="s") for name in (
        "assistant_thinking", "meta_user", "local_command_name", "local_command_stdout", "interrupted",
        "turn_duration", "last_prompt")))
    assert message_of(saved.finish_turn()) == MANUAL


# --- work after the marker makes it outdated (loop step 6) ---------------

def test_a_prompt_makes_it_outdated_and_the_model_is_told(saved):
    proc = saved.prompt("Also rename the module.")
    note = context_of(proc)
    assert "no longer covers the latest work (a prompt after it)" in note
    assert str(saved.handover) in note
    assert not saved.marker("ready").exists()
    assert outdated(saved)["outdated_reason"] == "a prompt after it"
    assert saved.stop().stdout == ""


def test_a_subagent_hand_back_counts_as_a_prompt(saved):
    saved.prompt('<agent-message from="agent-1">\n  report\n</agent-message>')
    assert saved.marker("outdated").exists()


def test_the_clear_prompt_does_not_outdate(saved):
    proc = saved.prompt("/clear")
    assert proc.stdout == ""
    assert saved.marker("ready").exists()


def test_a_tool_call_makes_it_outdated_and_the_model_is_told(saved):
    proc = saved.pre_tool("Bash", {"command": "pytest"})
    assert json.loads(proc.stdout)["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "(a tool call after it)" in context_of(proc)
    assert outdated(saved)["notified"] is True


@pytest.mark.parametrize("tool", ["Read", "Glob", "Grep"])
def test_read_only_tools_keep_it_current(saved, tool):
    assert saved.pre_tool(tool, {"pattern": "x", "file_path": "x"}).stdout == ""
    assert saved.marker("ready").exists()


def test_writing_the_handover_again_keeps_it_current(saved):
    assert saved.pre_tool("Write", {"file_path": str(saved.handover), "content": "x"}).stdout == ""
    assert saved.marker("ready").exists()


def test_writing_another_file_makes_it_outdated(saved, project):
    saved.pre_tool("Write", {"file_path": str(project / "a.py"), "content": "x"})
    assert saved.marker("outdated").exists()


def test_a_tool_call_inside_a_subagent_changes_nothing(saved):
    """Claude Code's internal agents fire a PreToolUse after Stop (A0 rule 12)."""
    proc = saved.pre_tool("Bash", {"command": "true"}, agent_id="agent-1")
    assert proc.stdout == ""
    assert saved.marker("ready").exists()


def test_a_tool_call_in_the_transcript_after_the_marker_outdates_at_stop(saved):
    """A parallel call whose PreToolUse ran before the marker existed."""
    tool_id = saved.tool_id()
    saved.append(b1_entry("tool_use_bash", tool_use_id=tool_id, command="make", description="build"),
                 b1_entry("tool_result", tool_use_id=tool_id, file_path="x", tool_output="ok"))
    proc = saved.finish_turn()
    assert proc.stdout == ""
    assert outdated(saved)["outdated_reason"] == "work after it (a tool call)"
    assert outdated(saved)["notified"] is False


def test_the_result_of_a_parallel_call_outdates_at_stop(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    bash_id = s.tool_id()
    s.append(b1_entry("tool_use_bash", tool_use_id=bash_id, command="make", description="build"))
    s.write_handover()
    s.append(b1_entry("tool_result", tool_use_id=bash_id, file_path="x", tool_output="built"))
    s.finish_turn()
    assert outdated(s)["outdated_reason"] == "work after it (the result of a tool call)"


def test_the_result_of_a_parallel_read_keeps_it_current(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    read_id = s.tool_id()
    s.append(b1_entry("tool_use_read", tool_use_id=read_id, file_path="a.py"))
    s.write_handover()
    s.append(b1_entry("tool_result", tool_use_id=read_id, file_path="a.py", tool_output="text"))
    assert message_of(s.finish_turn()) == MANUAL


@pytest.mark.parametrize("name,why", [
    ("user_prompt", "a prompt"),
    ("user_prompt_blocks", "a prompt"),
    ("task_notification", "a prompt"),
    ("queued_command", "a queued prompt"),
])
def test_prompts_in_the_transcript_after_the_marker_outdate_at_stop(saved, name, why):
    saved.append(b1_entry(name, prompt="next", task_id="t1"))
    saved.finish_turn()
    assert outdated(saved)["outdated_reason"] == "work after it (%s)" % why


def test_an_outdated_notice_from_stop_reaches_the_model_once(saved):
    saved.append(b1_entry("queued_command", prompt="next"))
    saved.finish_turn()
    first = context_of(saved.pre_tool("Bash", {"command": "ls"}))
    assert "a queued prompt" in first
    assert saved.pre_tool("Bash", {"command": "ls"}).stdout == ""


def test_an_over_age_marker_is_outdated_at_stop(saved):
    marker = json.loads(saved.marker("ready").read_text())
    old = datetime.now(timezone.utc) - timedelta(minutes=61)
    marker["written_at"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    saved.marker("ready").write_text(json.dumps(marker))
    saved.finish_turn()
    assert outdated(saved)["outdated_reason"] == "it is older than handover_max_age_minutes allows"


def test_a_handover_changed_outside_the_tools_is_checked_again(saved):
    saved.handover.write_text("broken now")
    proc = saved.finish_turn()
    assert proc.stdout == ""
    assert outdated(saved)["outdated_reason"] == "the handover file changed after it was checked"


def test_a_truncated_transcript_does_not_count_as_current(saved):
    data = saved.transcript.read_bytes()
    saved.transcript.write_bytes(data[: len(data) // 3])
    saved.stop()
    assert outdated(saved)["outdated_reason"] == "the transcript no longer matches the recorded position"


# --- the Stop fallback: a handover written by another route --------------

def write_by_shell(session, text=VALID_HANDOVER):
    """A handover written with a shell command: no PostToolUse for Write or Edit."""
    tool_id = session.tool_id()
    session.append(b1_entry("tool_use_bash", tool_use_id=tool_id, command="cat > handover", description="x",
                            timestamp=stamp(-5)))
    session.handover.parent.mkdir(parents=True, exist_ok=True)
    session.handover.write_text(text, encoding="utf-8")
    written = time.time() - 4
    os.utime(session.handover, (written, written))
    session.append(b1_entry("tool_result", tool_use_id=tool_id, file_path="x", tool_output="", timestamp=stamp(-3)))
    return tool_id


def test_stop_gives_a_shell_written_handover_its_marker(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    write_by_shell(s)
    s.append(b1_entry("assistant_text", assistant_text="Saved.", timestamp=stamp(-2)))
    assert message_of(s.stop()) == MANUAL
    marker = json.loads(s.marker("ready").read_text())
    assert marker["tool_use_id"] is None
    assert marker["transcript"]["size"] == s.transcript.stat().st_size


def test_stop_fallback_refuses_when_work_followed_the_write(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    write_by_shell(s)
    s.append(b1_entry("tool_use_bash", tool_use_id=s.tool_id(), command="make", description="x", timestamp=stamp(-1)))
    assert s.stop().stdout == ""
    assert not s.marker("ready").exists()


def test_stop_fallback_refuses_an_invalid_handover(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    write_by_shell(s, "just notes")
    assert s.stop().stdout == ""
    assert not s.marker("ready").exists()


def test_stop_fallback_never_revives_an_outdated_handover(saved):
    saved.prompt("one more change")
    os.utime(saved.handover, None)
    assert saved.finish_turn().stdout == ""
    assert not saved.marker("ready").exists()


# --- UserPromptExpansion -------------------------------------------------

def test_typing_rotate_puts_the_handover_path_in_context(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    proc = s.expand("")
    spec = json.loads(proc.stdout)["hookSpecificOutput"]
    assert spec["hookEventName"] == "UserPromptExpansion"
    assert str(s.handover) in spec["additionalContext"]
    assert "8000 characters" in spec["additionalContext"]
    assert (rotation(project) / ".gitignore").read_text() == "*\n"


def test_write_is_the_same_as_no_argument(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    assert context_of(s.expand("write")) == context_of(s.expand(""))


def test_another_command_is_ignored(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    assert s.expand("", command="other-plugin:rotate").stdout == ""
    assert not rotation(project).exists()


def test_release_is_an_action(tmp_path, project):
    """B2 records the release; its effect on the force and hard bands is in test_enforcement.py."""
    s = Session(tmp_path, project, "sess-a")
    assert message_of(s.expand("release")).startswith("context-rotation: released for this session.")


def test_a_project_skill_with_the_same_name_is_ignored(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    payload = s.payload("09-UserPromptExpansion-plugin-skill", command_name="context-rotation:rotate",
                        command_args="", command_source="projectSettings")
    assert s.hook("context_guard", payload).stdout == ""


def test_enabled_false_ignores_every_event(tmp_path, project):
    s = Session(tmp_path, project, "sess-a")
    s.write_handover()
    settings_file(project, enabled=False)
    for proc in (s.prompt("x"), s.pre_tool("Bash", {"command": "x"}), s.finish_turn(), s.expand("")):
        assert (proc.returncode, proc.stdout) == (0, "")
    assert s.marker("ready").exists()


def test_warn_band_and_outdated_note_share_one_context(saved):
    saved.append(b1_entry("assistant_text", assistant_text="x"))
    entry = json.loads(saved.transcript.read_text().splitlines()[-1])
    entry["message"]["usage"].update(input_tokens=130000, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    saved.append(entry)
    text = context_of(saved.prompt("go on"))
    assert "no longer covers the latest work" in text
    assert "Context is at 130000 tokens, in the force band (threshold 130000 tokens)" in text
