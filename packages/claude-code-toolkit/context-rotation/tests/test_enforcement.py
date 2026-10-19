"""The force and hard bands, through the real hook scripts (loop step 3, deliverable B2).

Sizes against the default window of 200,000 tokens: warn from 110,000, force from
130,000, hard from 150,000. A transcript entry from fixtures/b1 that carries usage
measures 76,629 tokens, below warn, so a test that writes the handover sets the
size again afterwards (assistant text is not work, so the handover stays current).
"""
from __future__ import annotations

import pytest

from conftest import (ALLOWED, BELOW, FORCE, HARD, WARN, Session, at, bash, compacted, context_of, decision,
                      goes_ahead, held_back, journal, load_fixture, message_of, output, reason, rotation,
                      settings_file, state_of)

import context_budget


@pytest.fixture
def s(tmp_path, project):
    return Session(tmp_path, project, "sess-a")


# --- UserPromptSubmit -----------------------------------------------------------------

def test_force_asks_for_the_handover_now_and_tells_the_user_once(s):
    at(s, FORCE)
    first = s.prompt("go on")
    assert "Write the handover at %s now" % s.handover in context_of(first)
    assert "in the force band" in context_of(first)
    assert "force band" in message_of(first) and "/clear" in message_of(first)
    assert "the handover goes to %s." % s.handover in message_of(first)
    second = s.prompt("and more")
    assert "Write the handover at %s now" % s.handover in context_of(second)
    assert "systemMessage" not in output(second)


def test_hard_names_what_stays_allowed_and_how_the_user_releases(s):
    at(s, HARD)
    proc = s.prompt("go on")
    assert ALLOWED % s.handover in context_of(proc)
    assert "Type /context-rotation:rotate release" in message_of(proc)


def test_warn_neither_denies_nor_holds_back(s):
    at(s, WARN)
    assert "Wrap up the current work" in context_of(s.prompt("x"))
    assert goes_ahead(bash(s))
    assert goes_ahead(s.stop())
    assert state_of(s) is None


# --- PreToolUse -------------------------------------------------------------------------

@pytest.mark.parametrize("tool,tool_input", [
    ("Read", {"file_path": "/etc/hosts"}), ("Glob", {"pattern": "*"}), ("Grep", {"pattern": "x"}),
    ("Skill", {"skill": "context-rotation:rotate"}), ("AskUserQuestion", {"questions": []}),
    ("TodoWrite", {"todos": []}),
])
def test_hard_lets_reading_asking_the_skill_and_todos_through(s, tool, tool_input):
    at(s, HARD)
    assert goes_ahead(s.pre_tool(tool, tool_input))


def test_hard_allows_the_handover_write_and_denies_every_other_write(s):
    settings_file(s.project, hard_max_denials=20)
    at(s, HARD)
    for tool in ("Write", "Edit"):
        assert goes_ahead(s.pre_tool(tool, {"file_path": str(s.handover), "content": "x"}))
    s.handover.parent.mkdir(parents=True, exist_ok=True)
    s.handover.write_text("a first draft")  # once the handover exists, rewriting it goes ahead too
    for tool in ("Write", "Edit"):
        assert goes_ahead(s.pre_tool(tool, {"file_path": str(s.handover), "content": "x"}))
    other_session = rotation(s.project) / "handover-sess-b.md"
    for path in (s.project / "src" / "main.py", other_session):
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            proc = s.pre_tool(tool, {"file_path": str(path), "content": "x"})
            assert decision(proc) == "deny", (tool, path)


def test_hard_denies_with_why_and_the_path_through_json_and_exit_0(s):
    at(s, HARD)
    proc = bash(s)
    assert proc.returncode == 0
    assert decision(proc) == "deny"
    text = reason(proc)
    assert "hard band" in text and "Write the handover at %s now" % s.handover in text
    assert "Type /context-rotation:rotate release" in message_of(proc)  # the crossing's line, at its first denial
    assert "systemMessage" not in output(bash(s))  # once per crossing


def test_force_denies_nothing_by_default(s):
    at(s, FORCE)
    assert goes_ahead(bash(s))


def test_force_denies_only_what_force_deny_tools_names(s):
    settings_file(s.project, force_deny_tools=["Bash", "mcp__*", "Read", "Write"], hard_max_denials=20)
    at(s, FORCE)
    assert decision(bash(s)) == "deny"
    assert "force_deny_tools" in reason(bash(s))
    assert decision(s.pre_tool("mcp__tracker__create", {})) == "deny"
    assert goes_ahead(s.pre_tool("WebFetch", {"url": "https://example.org"}))
    assert goes_ahead(s.pre_tool("Read", {"file_path": "/etc/hosts"}))  # what the hard band allows, force never denies
    assert goes_ahead(s.pre_tool("Write", {"file_path": str(s.handover), "content": "x"}))


def test_an_unknown_size_never_denies_or_holds_back(s):
    at(s, HARD)
    assert decision(bash(s)) == "deny"
    before = state_of(s)
    boundary = load_fixture("transcript", "compact_boundary.json")
    del boundary["compactMetadata"]["postTokens"]  # a compaction without its size: unknown
    s.append(boundary)
    assert goes_ahead(bash(s))
    assert goes_ahead(s.stop())
    assert goes_ahead(s.pre_tool("Write", {"file_path": str(s.project / "x.py"), "content": "x"}))
    assert state_of(s) == before


def test_a_missing_transcript_never_denies(s, tmp_path):
    settings_file(s.project, window_tokens=1000)
    assert goes_ahead(bash(s, transcript_path=str(tmp_path / "missing.jsonl")))
    assert goes_ahead(s.stop(transcript_path=str(tmp_path / "missing.jsonl")))
    assert state_of(s) is None


def test_a_hook_with_agent_id_does_nothing_at_hard(s):
    at(s, HARD)
    payloads = [
        s.payload("12-PreToolUse-inside-subagent"),
        s.payload("10-PreToolUse-internal-agent-after-Stop"),  # A0 rule 3: after Stop, in the same turn
        s.payload("03-Stop-idle", agent_id="agent-1", agent_type="general-purpose"),
        s.payload("02-UserPromptSubmit", prompt="x", agent_id="agent-1"),
    ]
    for payload in payloads:
        assert payload["agent_id"]
        proc = s.hook("context_guard", payload)
        assert (proc.returncode, proc.stdout) == (0, ""), payload["hook_event_name"]
    assert not rotation(s.project).exists()


# --- Stop -----------------------------------------------------------------------------------

def test_stop_is_held_back_once_per_crossing(s):
    at(s, FORCE)
    first = s.stop()
    assert held_back(first)
    assert output(first)["reason"].endswith(
        "Write the handover at %s with the rotate skill as your last action, then stop." % s.handover)
    assert "force band" in message_of(first)
    assert goes_ahead(s.stop())
    at(s, HARD)  # force to hard: a crossing into hard
    assert held_back(s.stop())
    assert goes_ahead(s.stop())


def test_no_hold_back_while_stop_hook_active(s):
    at(s, HARD)
    assert goes_ahead(s.stop(stop_hook_active=True))
    assert held_back(s.stop())  # the crossing's hold-back was not used up


def test_falling_back_and_rising_again_is_a_new_crossing(s):
    at(s, HARD)
    assert held_back(s.stop())
    assert "systemMessage" not in output(s.prompt("x"))  # told at the hold-back already
    compacted(s, 20000)
    assert goes_ahead(s.stop())
    assert context_of(s.prompt("y")) is None
    at(s, FORCE)
    again = s.stop()
    assert held_back(again) and "force band" in message_of(again)
    assert [line["event"] for line in journal(s.project)] == [
        "crossing", "stop_block", "told", "fall", "crossing", "stop_block", "told"]
    assert state_of(s)["crossing"] == 2


def test_a_call_the_hard_band_allows_still_records_the_band(s):
    at(s, HARD)
    assert held_back(s.stop())
    compacted(s, 20000)
    assert goes_ahead(s.pre_tool("Read", {"file_path": "/etc/hosts"}))  # the only hook below force
    assert state_of(s)["level"] == "low"
    at(s, HARD)
    assert held_back(s.stop())  # a second crossing into hard, so its Stop is held back once more
    assert goes_ahead(s.stop())
    assert [line["event"] for line in journal(s.project)] == [
        "crossing", "stop_block", "told", "fall", "crossing", "stop_block", "told"]


def test_a_current_handover_is_not_held_back(s):
    at(s, HARD)
    s.prompt("go on")
    assert s.write_handover().returncode == 0
    at(s, HARD)  # assistant text after the write: not work, the handover stays current
    proc = s.stop()
    assert message_of(proc).startswith("Handover saved. Type /clear")
    assert "decision" not in output(proc)
    state = state_of(s)
    assert state is not None, "the crossing into hard was not recorded"
    assert state["level"] == "hard" and state["blocked"] == 0
    s.prompt("one more thing")  # work after the handover: it is outdated, and the stop is held back
    assert held_back(s.stop())


# --- recovery: plan mode, the denial cap, release -------------------------------------------

def test_plan_mode_informs_and_denies_nothing(s):
    at(s, HARD)
    assert goes_ahead(bash(s, permission_mode="plan"))
    assert goes_ahead(s.stop(permission_mode="plan"))
    prompt = s.hook("context_guard", s.payload("02-UserPromptSubmit", prompt="x", permission_mode="plan"))
    assert "Plan mode is on, so nothing is denied" in context_of(prompt)
    assert decision(bash(s)) == "deny"  # outside plan mode the same session is enforced
    assert held_back(s.stop())


def test_after_the_denial_cap_the_session_only_informs_and_the_user_is_told_once(s):
    settings_file(s.project, hard_max_denials=2)
    at(s, HARD)
    assert decision(bash(s)) == "deny"
    second = bash(s)
    assert decision(second) == "deny"
    assert "2 tool calls were denied in this session without a handover write" in message_of(second)
    assert "2 tool calls" not in reason(second)  # the model is not told the count
    for _ in range(3):
        assert bash(s).stdout == ""
    assert goes_ahead(s.stop())
    assert "Nothing is denied or held back in this session" in context_of(s.prompt("x"))
    assert state_of(s)["inform_only"] == "denial_cap"


def test_a_handover_write_clears_the_denials(s):
    settings_file(s.project, hard_max_denials=2)
    at(s, HARD)
    assert decision(bash(s)) == "deny"
    assert state_of(s)["denials"] == 1
    assert s.write_handover().returncode == 0
    assert state_of(s)["denials"] == 0
    at(s, HARD)
    assert decision(bash(s)) == "deny"
    assert decision(bash(s)) == "deny"
    assert bash(s).stdout.count("permissionDecision") == 0
    assert [line["event"] for line in journal(s.project)].count("reset") == 1


def test_release_typed_by_the_user_lifts_the_force_and_hard_bands(s):
    at(s, HARD)
    assert decision(bash(s)) == "deny"
    proc = s.expand("release")
    assert message_of(proc).startswith("context-rotation: released for this session.")
    assert "the context-rotation guard is released for this session" in context_of(proc)
    assert str(s.handover) in context_of(proc)
    assert goes_ahead(bash(s))
    assert goes_ahead(s.stop())
    assert "the user released the guard for this session" in context_of(s.prompt("x"))
    assert state_of(s)["inform_only"] == "release"
    assert journal(s.project)[-1]["event"] == "inform_only" and journal(s.project)[-1]["reason"] == "release"


def test_a_release_the_model_invokes_changes_nothing(s):
    at(s, HARD)
    assert goes_ahead(s.pre_tool("Skill", {"skill": "context-rotation:rotate", "args": "release"}))
    rotation(s.project).mkdir(parents=True, exist_ok=True)
    (rotation(s.project) / "release-sess-a").write_text("release")  # the fallback file A0 made unnecessary
    assert s.prompt("/context-rotation:rotate release").returncode == 0  # a prompt alone is no expansion
    project_skill = s.payload("09-UserPromptExpansion-plugin-skill", command_name="context-rotation:rotate",
                              command_args="release", command_source="projectSettings")
    assert s.hook("context_guard", project_skill).stdout == ""
    assert decision(bash(s)) == "deny"
    assert held_back(s.stop())
    assert state_of(s)["inform_only"] is None


def test_release_in_any_band_lasts_for_the_session(s):
    at(s, BELOW)
    assert message_of(s.expand("release")).startswith("context-rotation: released for this session.")
    at(s, HARD)
    assert goes_ahead(bash(s))
    assert goes_ahead(s.stop())


# --- settings ---------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["Bash", [1], [""], ["a\0b"], {"Bash": True}])
def test_a_bad_force_deny_tools_falls_back_to_none(project, value, capsys):
    settings_file(project, force_deny_tools=value)
    assert context_budget.load_settings(str(project)).get("force_deny_tools") == ()
    assert "force_deny_tools" in capsys.readouterr().err


def test_force_deny_tools_is_read_as_given(project):
    settings_file(project, force_deny_tools=["Bash", "mcp__*"])
    assert context_budget.load_settings(str(project)).get("force_deny_tools") == ("Bash", "mcp__*")
