"""transcript_work: positions and what counts as work after them."""
from __future__ import annotations

import json
import time

import pytest

import transcript_work as tw
from conftest import b1_entry, stamp, write_transcript

HANDOVER = "/project/.claude/rotation/handover-s.md"


def entry(name, **values):
    defaults = dict(tool_use_id="t1", handover_path=HANDOVER, handover_text="x", file_path="f", tool_output="o",
                    command="c", description="d", prompt="p", assistant_text="a", thinking="t", signature="s",
                    skill_body="b", command_output="o", text="x", hook_text="h", task_id="k")
    defaults.update(values)
    return b1_entry(name, **defaults)


@pytest.mark.parametrize("name", [
    "assistant_text", "assistant_thinking", "meta_user", "local_command_stdout", "local_command_name",
    "interrupted", "hook_additional_context", "stop_hook_summary", "turn_duration", "last_prompt",
    "tool_use_write", "tool_use_edit", "tool_use_read",
])
def test_not_work(name):
    assert tw.entry_work(entry(name), HANDOVER, set()) is None


@pytest.mark.parametrize("name,why", [
    ("tool_use_bash", "a Bash call"),
    ("user_prompt", "a prompt"),
    ("user_prompt_blocks", "a prompt"),
    ("task_notification", "a prompt"),
    ("queued_command", "a queued prompt"),
])
def test_work(name, why):
    assert tw.entry_work(entry(name), HANDOVER, set()) == why


def test_a_write_elsewhere_is_work():
    assert tw.entry_work(entry("tool_use_write", handover_path="/elsewhere.md"), HANDOVER, set()) == "a Write call"


def test_a_sidechain_entry_is_never_work():
    sub = entry("tool_use_bash")
    sub["isSidechain"] = True
    assert tw.entry_work(sub, HANDOVER, set()) is None


def test_an_unknown_assistant_block_is_work():
    odd = entry("assistant_text")
    odd["message"]["content"] = [{"type": "server_tool_use", "name": "web_search"}]
    assert tw.entry_work(odd, HANDOVER, set()) == "an assistant server_tool_use block"


def test_results_are_judged_by_their_call():
    allowed = set()
    assert tw.entry_work(entry("tool_use_read", tool_use_id="r1"), HANDOVER, allowed) is None
    assert allowed == {"r1"}
    assert tw.entry_work(entry("tool_result", tool_use_id="r1"), HANDOVER, allowed, lambda _id: None) is None
    found = tw.entry_work(entry("tool_result", tool_use_id="b1"), HANDOVER, allowed, lambda _id: None)
    assert found == "the result of an earlier tool call"
    assert tw.entry_work(entry("tool_result", tool_use_id="b1"), HANDOVER, allowed) is None


def test_position_stops_at_the_last_complete_line(tmp_path):
    path = write_transcript(tmp_path / "t.jsonl", [entry("assistant_text"), entry("assistant_text")])
    last_uuid = json.loads(path.read_text().splitlines()[-1])["uuid"]
    with open(path, "a") as handle:
        handle.write('{"type": "assist')
    pos = tw.position(str(path))
    assert pos["size"] == path.stat().st_size - len('{"type": "assist')
    assert pos["last_uuid"] == last_uuid
    assert tw.position_holds(pos)


def test_position_of_a_missing_transcript_is_none(tmp_path):
    assert tw.position(str(tmp_path / "gone.jsonl")) is None
    assert tw.position(None) is None


def test_a_rewritten_transcript_does_not_hold(tmp_path):
    path = write_transcript(tmp_path / "t.jsonl", [entry("assistant_text"), entry("assistant_text")])
    pos = tw.position(str(path))
    write_transcript(path, [entry("assistant_text"), entry("assistant_text")])
    assert pos["size"] == path.stat().st_size
    assert not tw.position_holds(pos)


def test_stale_reason_names_the_first_work(tmp_path):
    path = write_transcript(tmp_path / "t.jsonl", [entry("tool_use_write", tool_use_id="w1")])
    pos = tw.position(str(path))
    with open(path, "a") as handle:
        for item in (entry("tool_result", tool_use_id="w1"), entry("assistant_text"), entry("tool_use_bash")):
            handle.write(json.dumps(item) + "\n")
    assert tw.stale_reason(pos, HANDOVER, "w1") == "work after it (a Bash call)"
    assert tw.stale_reason(None, HANDOVER, "w1") == "its transcript position is unknown"


def test_a_partial_last_line_is_left_for_later(tmp_path):
    path = write_transcript(tmp_path / "t.jsonl", [entry("assistant_text")])
    pos = tw.position(str(path))
    with open(path, "a") as handle:
        handle.write(json.dumps(entry("tool_use_bash"))[:-5])
    assert tw.stale_reason(pos, HANDOVER, None) is None


def test_work_since_judges_only_calls_and_prompts_after_the_time(tmp_path):
    written = time.time()
    path = write_transcript(tmp_path / "t.jsonl", [
        entry("tool_use_bash", timestamp=stamp(-10)),
        entry("tool_result", tool_use_id="unknown", timestamp=stamp(1)),
        entry("assistant_text", timestamp=stamp(2)),
    ])
    assert tw.work_since(str(path), written, HANDOVER) is None
    with open(path, "a") as handle:
        handle.write(json.dumps(entry("tool_use_bash", timestamp=stamp(3))) + "\n")
    assert tw.work_since(str(path), written, HANDOVER) == "a Bash call"


def test_work_since_without_a_transcript_counts_as_work(tmp_path):
    assert tw.work_since(None, 0.0, HANDOVER) == "no transcript to check"
    assert tw.work_since(str(tmp_path / "gone.jsonl"), 0.0, HANDOVER) == "the transcript cannot be read"
