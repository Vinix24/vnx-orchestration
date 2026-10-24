"""tests/test_t0_rotation_delivery.py — the successor's turns, read from its transcript (dlv-259886a390ec).

``scripts/lib/t0_rotation_delivery.py`` answers the /goal follow-up of a T0 rotation: is the kickoff
turn over, did /goal run, was the typed line queued. The records below carry the field names and
shapes measured on claude 2.1.308 transcripts of the SEOcrawler rotations of 2026-10-22
(rotation-20261022-170308-53038, rotation-20261022-191855-76425) and of earlier follow-ups that did
land (2.1.284, 2.1.285, 2.1.295); the content is made up.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "scripts" / "lib" / "t0_rotation_delivery.py"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import t0_rotation_delivery as delivery

RUN_ID = "rotation-20261022-191855-76425"
GOAL = ("/goal FASE 1, DOEL A: snelle poort en de voorwaarden voor de push, hervat na "
        "context-rotatie; resterend: Punt 1: #803 poort en merge")


def _user(content, **extra):
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": content}, **extra}


KICKOFF = _user("<command-message>kickoff</command-message>\n<command-name>/kickoff</command-name>\n"
                f"<command-args>rotatie {RUN_ID}</command-args>", origin={"kind": "human"})
SKILL_BODY = _user([{"type": "text", "text": "Base directory for this skill: ~/.claude/skills/kickoff"}],
                   isMeta=True)
BOOKKEEPING = [{"type": "mode", "mode": "normal"}, {"type": "permission-mode", "permissionMode": "auto"},
               {"type": "atis-latch", "atis": {}}, {"type": "bridge-session", "bridgeSessionId": "b"},
               {"type": "api-request", "id": "r1"}, {"type": "last-prompt", "lastPrompt": "x"}]
TOOL_ROUND = [{"type": "assistant", "isSidechain": False,
               "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use"}]}},
              {"type": "attachment", "attachment": {"type": "hook_success"}},
              _user([{"type": "tool_result", "content": "ok"}])]
QUEUED_GOAL = [{"type": "queue-operation", "operation": "enqueue", "content": f'<pasted_content id="5356">\n{GOAL}'},
               {"type": "queue-operation", "operation": "remove", "content": f'<pasted_content id="5356">\n{GOAL}'},
               {"type": "attachment", "attachment": {"type": "queued_command",
                                                     "prompt": f'<pasted_content id="5356">\n{GOAL}'}}]
TURN_END = [{"type": "assistant", "isSidechain": False,
             "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "klaar"}]}},
            {"type": "system", "subtype": "stop_hook_summary", "isSidechain": False, "hookCount": 3,
             "preventedContinuation": False},
            {"type": "system", "subtype": "turn_duration", "isSidechain": False, "durationMs": 330187,
             "messageCount": 243}]
GOAL_RAN = [{"type": "attachment", "attachment": {"type": "goal_status", "met": False, "sentinel": True}},
            _user("<command-name>/goal</command-name>\n            <command-message>goal</command-message>\n"
                  f"            <command-args>{GOAL[6:]}</command-args>", origin=None),
            _user(f"<local-command-stdout>Goal set: {GOAL[6:]}</local-command-stdout>"),
            _user([{"type": "text", "text": "A session-scoped Stop hook is now active"}], isMeta=True)]


def _write(tmp_path, records: List[dict], *, tail: str = "") -> Path:
    path = tmp_path / "projects" / "-p" / "0f1e2d3c-4b5a-4697-8877-665544332211.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + tail, encoding="utf-8")
    return path


def _state(tmp_path, records, line: str = GOAL):
    return delivery.turn_state(_write(tmp_path, records), line)


def test_the_kickoff_turn_of_a_2026_10_22_rotation_ends_only_on_turn_duration(tmp_path):
    """The rotation of 19:18: the /goal went in during the kickoff turn and was queued; the turn
    ended minutes later. Neither the queue, nor end_turn, nor stop_hook_summary is the end."""
    head = BOOKKEEPING + [KICKOFF, SKILL_BODY] + BOOKKEEPING + TOOL_ROUND * 3
    during = _state(tmp_path, head)
    assert during.kickoff and not during.idle and during.turns_ended == 0
    queued = _state(tmp_path, head + QUEUED_GOAL + TOOL_ROUND)
    assert not queued.idle and queued.queued and not queued.goal
    before_end = _state(tmp_path, head + QUEUED_GOAL + TOOL_ROUND + TURN_END[:2])
    assert not before_end.idle, "end_turn and stop_hook_summary come before the end"
    ended = _state(tmp_path, head + QUEUED_GOAL + TOOL_ROUND + TURN_END + BOOKKEEPING)
    assert ended.idle and ended.turns_ended == 1 and ended.queued and not ended.goal


def test_a_blocking_stop_hook_keeps_the_turn_running(tmp_path):
    """A /goal that is not met blocks the stop: stop_hook_summary, Stop hook feedback, and the turn
    goes on without a turn_duration."""
    blocked = [TURN_END[0], {"type": "attachment", "attachment": {"type": "goal_status", "met": False}},
               _user("Stop hook feedback:\nnog niet klaar"), TURN_END[1]] + TOOL_ROUND
    assert not _state(tmp_path, [KICKOFF] + TOOL_ROUND + blocked).idle
    assert _state(tmp_path, [KICKOFF] + TOOL_ROUND + blocked + TURN_END).idle


@pytest.mark.parametrize("text", ["[Request interrupted by user]", "[Request interrupted by user for tool use]"])
def test_an_interrupted_turn_counts_as_ended(tmp_path, text):
    state = _state(tmp_path, [KICKOFF] + TOOL_ROUND + [_user([{"type": "text", "text": text}])])
    assert state.idle and state.turns_ended == 1


def test_meta_and_local_command_output_after_the_turn_keep_it_idle(tmp_path):
    after = [_user("<local-command-caveat>Caveat: local</local-command-caveat>", isMeta=True),
             _user("<local-command-stdout>voortgang geopend</local-command-stdout>"),
             {"type": "system", "subtype": "local_command",
              "content": "<command-name>/voortgang</command-name>"},
             {"type": "attachment", "attachment": {"type": "total_tokens_reminder"}}] + BOOKKEEPING
    assert _state(tmp_path, [KICKOFF] + TURN_END + after).idle


@pytest.mark.parametrize("starter", [
    _user("<task-notification>\n<summary>bouwer klaar</summary>\n</task-notification>",
          origin={"kind": "task-notification"}),
    _user("kopieer de goal naar clipboard", origin={"kind": "human"}),
    _user("\n\n<pasted_content id=\"c632\">\n" + GOAL, origin={"kind": "human"}),
])
def test_a_new_turn_after_the_kickoff_turn_is_busy_again(tmp_path, starter):
    state = _state(tmp_path, [KICKOFF] + TURN_END + [starter])
    assert not state.idle and state.turns_ended == 1
    assert not state.goal, "a prompt that carries /goal as text did not run /goal"
    assert _state(tmp_path, [KICKOFF] + TURN_END + [starter] + TOOL_ROUND + TURN_END).idle


def test_a_goal_that_ran_is_the_goal_entry(tmp_path):
    state = _state(tmp_path, [KICKOFF] + TURN_END + GOAL_RAN)
    assert state.goal and not state.idle, "/goal starts a turn of its own"


@pytest.mark.parametrize("record", [
    _user("waarom staat er <command-name>/goal</command-name> in het log?"),
    _user("<command-name>/goals</command-name>\n<command-args>x</command-args>"),
    _user("/goal " + GOAL[6:]),
    _user("<command-name>/goal</command-name>\n<command-args>x</command-args>", isMeta=True),
    _user("<command-name>/goal</command-name>\n<command-args>x</command-args>", isSidechain=True),
])
def test_only_the_command_record_of_goal_counts(tmp_path, record):
    assert not _state(tmp_path, [KICKOFF] + TURN_END + [record]).goal


def test_a_queued_line_is_matched_on_its_start(tmp_path):
    other = [{"type": "queue-operation", "operation": "enqueue", "content": "<task-notification>x"}]
    assert not _state(tmp_path, [KICKOFF] + other).queued
    assert not _state(tmp_path, [KICKOFF] + QUEUED_GOAL, line="").queued
    assert not _state(tmp_path, [KICKOFF] + QUEUED_GOAL[1:]).queued, "only the enqueue counts"
    assert _state(tmp_path, [KICKOFF] + QUEUED_GOAL, line=GOAL + "\n").queued


@pytest.mark.parametrize("records", [[], BOOKKEEPING + [SKILL_BODY] + TOOL_ROUND])
def test_without_the_kickoff_entry_nothing_is_idle(tmp_path, records):
    state = _state(tmp_path, records + TURN_END)
    assert state == delivery.TurnState(kickoff=False, turns_ended=0, idle=False, goal=False, queued=False)


def test_a_missing_transcript_and_a_half_written_line(tmp_path):
    assert not delivery.turn_state(tmp_path / "gone.jsonl").kickoff
    path = _write(tmp_path, [KICKOFF] + TURN_END, tail='{"type": "user", "message": {"con')
    assert delivery.turn_state(path).idle


def _cli(*argv: str) -> int:
    return subprocess.run([sys.executable, str(LIB), *argv], capture_output=True, text=True,
                          timeout=60).returncode


def test_the_cli_answers_with_its_exit_code(tmp_path):
    busy = str(_write(tmp_path, [KICKOFF] + QUEUED_GOAL))
    line = tmp_path / "goal.txt"
    line.write_text(GOAL, encoding="utf-8")
    assert (_cli("idle", "--transcript", busy), _cli("goal", "--transcript", busy)) == (1, 1)
    assert _cli("queued", "--transcript", busy, "--line-file", str(line)) == 0
    done = str(_write(tmp_path, [KICKOFF] + TURN_END + GOAL_RAN[:1] + GOAL_RAN[1:] + TOOL_ROUND + TURN_END))
    assert (_cli("idle", "--transcript", done), _cli("goal", "--transcript", done)) == (0, 0)
    assert _cli("queued", "--transcript", done, "--line-file", str(line)) == 1


@pytest.mark.parametrize("argv", [
    ("queued", "--transcript", "t.jsonl"),
    ("queued", "--transcript", "t.jsonl", "--line-file", "/nonexistent/goal.txt"),
    ("busy", "--transcript", "t.jsonl"),
    ("idle",),
])
def test_the_cli_refuses_a_wrong_call_with_2(argv):
    assert _cli(*argv) == 2
