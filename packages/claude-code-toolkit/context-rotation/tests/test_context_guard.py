"""The UserPromptSubmit guard, run as the real script."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import PKG, assistant_entry, load_fixture, payload_for, write_transcript

GUARD = PKG / "hooks" / "context_guard.py"


def run_guard(payload, env_extra=None, raw=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=30,
    )
    return proc


def state_dir(project):
    return project / ".claude" / "rotation" / "state"


def transcript_with(tmp_path, total, name="t.jsonl"):
    return write_transcript(tmp_path / name, [assistant_entry(total)])


def unknown_transcript(tmp_path):
    return write_transcript(tmp_path / "u.jsonl", [{"type": "user"}])


def test_agent_id_is_silent_and_writes_no_state(tmp_path, project):
    t = transcript_with(tmp_path, 190000)
    proc = run_guard(payload_for(project, t, agent_id="agent-9", agent_type="x"))
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not (project / ".claude").exists()


def test_below_warn_is_silent(tmp_path, project):
    proc = run_guard(payload_for(project, transcript_with(tmp_path, 1000)))
    assert (proc.returncode, proc.stdout) == (0, "")


def test_warn_outputs_additional_context_with_handover_path(tmp_path, project):
    proc = run_guard(payload_for(project, transcript_with(tmp_path, 120000), session_id="abc_1-2"))
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "UserPromptSubmit"
    text = spec["additionalContext"]
    handover = str(project / ".claude" / "rotation" / "handover-abc_1-2.md")
    assert handover in text
    assert "120000" in text or "120,000" in text
    assert "200000" in text or "200,000" in text
    assert "60%" in text


def test_force_and_hard_get_the_same_message_shape(tmp_path, project):
    for total in (140000, 170000):
        out = json.loads(run_guard(payload_for(project, transcript_with(tmp_path, total))).stdout)
        assert "additionalContext" in out["hookSpecificOutput"]


def test_project_dir_env_wins_over_cwd(tmp_path, project):
    other = tmp_path / "other"
    other.mkdir()
    payload = payload_for(other, transcript_with(tmp_path, 150000))
    out = json.loads(run_guard(payload, {"CLAUDE_PROJECT_DIR": str(project)}).stdout)
    assert str(project / ".claude" / "rotation") in out["hookSpecificOutput"]["additionalContext"]


def test_enabled_false_is_silent(tmp_path, project):
    (project / ".claude").mkdir()
    (project / ".claude" / "context-rotation.json").write_text('{"enabled": false}')
    proc = run_guard(payload_for(project, transcript_with(tmp_path, 190000)))
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not state_dir(project).exists()


def test_three_unknowns_give_exactly_one_system_message(tmp_path, project):
    t = unknown_transcript(tmp_path)
    outputs = [run_guard(payload_for(project, t)).stdout for _ in range(5)]
    assert outputs[0] == outputs[1] == ""
    msg = json.loads(outputs[2])
    assert msg["systemMessage"]
    assert outputs[3] == outputs[4] == ""


def test_known_measurement_resets_the_unknown_count(tmp_path, project):
    unknown = unknown_transcript(tmp_path)
    known = transcript_with(tmp_path, 1000)
    run_guard(payload_for(project, unknown))
    run_guard(payload_for(project, unknown))
    run_guard(payload_for(project, known))
    assert run_guard(payload_for(project, unknown)).stdout == ""
    assert run_guard(payload_for(project, unknown)).stdout == ""
    assert "systemMessage" in json.loads(run_guard(payload_for(project, unknown)).stdout)


def test_notice_is_not_repeated_after_a_reset(tmp_path, project):
    unknown = unknown_transcript(tmp_path)
    known = transcript_with(tmp_path, 1000)
    for _ in range(3):
        run_guard(payload_for(project, unknown))
    run_guard(payload_for(project, known))
    outs = [run_guard(payload_for(project, unknown)).stdout for _ in range(4)]
    assert outs == [""] * 4


def test_sessions_count_separately(tmp_path, project):
    unknown = unknown_transcript(tmp_path)
    for _ in range(2):
        run_guard(payload_for(project, unknown, session_id="a"))
    assert run_guard(payload_for(project, unknown, session_id="b")).stdout == ""
    assert "systemMessage" in json.loads(run_guard(payload_for(project, unknown, session_id="a")).stdout)


def test_state_file_is_json_under_state_dir(tmp_path, project):
    run_guard(payload_for(project, unknown_transcript(tmp_path), session_id="s-1"))
    data = json.loads((state_dir(project) / "s-1.json").read_text())
    assert data["unknown_count"] == 1
    assert list(state_dir(project).glob("*.tmp")) == []


def test_gitignore_written_once_and_not_overwritten(tmp_path, project):
    unknown = unknown_transcript(tmp_path)
    run_guard(payload_for(project, unknown))
    gi = project / ".claude" / "rotation" / ".gitignore"
    assert gi.read_text() == "*\n"
    gi.write_text("custom\n")
    run_guard(payload_for(project, unknown))
    assert gi.read_text() == "custom\n"


@pytest.mark.parametrize("sid", ["../evil", "a/b", "a.b", "", "x y", "..", "é"])
def test_unsafe_session_id_is_refused(tmp_path, project, sid):
    proc = run_guard(payload_for(project, unknown_transcript(tmp_path), session_id=sid))
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not (project / ".claude").exists()
    assert not (tmp_path / "evil.json").exists()


def test_missing_session_id_is_refused(tmp_path, project):
    payload = payload_for(project, transcript_with(tmp_path, 190000))
    del payload["session_id"]
    proc = run_guard(payload)
    assert (proc.returncode, proc.stdout) == (0, "")


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null", "42"])
def test_garbage_stdin_exits_zero_silently(raw):
    proc = run_guard(None, raw=raw)
    assert (proc.returncode, proc.stdout) == (0, "")


def test_unwritable_state_never_raises(tmp_path, project):
    (project / ".claude").write_text("a file where the directory should be")
    proc = run_guard(payload_for(project, unknown_transcript(tmp_path)))
    assert (proc.returncode, proc.stdout) == (0, "")


def test_missing_transcript_path_counts_as_unknown(tmp_path, project):
    payload = payload_for(project, "")
    del payload["transcript_path"]
    for _ in range(2):
        run_guard(payload)
    assert "systemMessage" in json.loads(run_guard(payload).stdout)
