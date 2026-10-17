"""Shared fixtures: package paths, an isolated HOME, transcript builders and a simulated session."""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "a0"
sys.path.insert(0, str(PKG / "lib"))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """No test may read the real ~/.claude or inherit plugin switches."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONTEXT_ROTATION", raising=False)
    return home


def load_fixture(*parts):
    return json.loads((FIXTURES.joinpath(*parts)).read_text(encoding="utf-8"))


def assistant_entry(total, model="claude-sonnet-5-5", sidechain=False):
    entry = load_fixture("transcript", "assistant_entry.json")
    usage = entry["message"]["usage"]
    usage["input_tokens"] = total
    usage["cache_read_input_tokens"] = 0
    usage["cache_creation_input_tokens"] = 0
    entry["message"]["model"] = model
    entry["isSidechain"] = sidechain
    return entry


def write_transcript(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


def payload_for(project, transcript, session_id="sess-1", **extra):
    payload = load_fixture("payloads", "02-UserPromptSubmit.json")["payload"]
    payload.update(
        session_id=session_id, transcript_path=str(transcript), cwd=str(project)
    )
    payload.update(extra)
    return payload


# --- B1: transcripts, hooks and a simulated session ----------------------

B1 = Path(__file__).resolve().parent / "fixtures" / "b1"
HOOKS = PKG / "hooks"
_IDS = itertools.count(1)

VALID_HANDOVER = """# Handover

## Goal
Finish the parser so the import command reads every sample file.

## Current state
The tokenizer is done and tested. The parser fails on nested lists.

## Running
none

## Next steps
1. Run the parser tests and read the first failure.
2. Fix nested list handling in the parser.

## Files touched
- src/tokenizer.py: new tokenizer

## Open risks
The sample files were not all checked.
"""


def fill(value, mapping):
    """Replace placeholders in every string of a fixture."""
    if isinstance(value, str):
        for key, replacement in mapping.items():
            value = value.replace(key, replacement)
        return value
    if isinstance(value, list):
        return [fill(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: fill(item, mapping) for key, item in value.items()}
    return value


def stamp(offset_seconds=0.0):
    moment = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def b1_entry(name, **values):
    """A transcript entry from fixtures/b1 with its placeholders filled."""
    raw = json.loads((B1 / "transcript" / (name + ".json")).read_text(encoding="utf-8"))
    mapping = {"<uuid>": "entry-%05d" % next(_IDS), "<timestamp>": values.pop("timestamp", None) or stamp()}
    mapping.update(("<%s>" % key, str(value)) for key, value in values.items())
    return fill(raw, mapping)


def append_transcript(path, entries):
    with open(path, "a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def run_hook(entry, payload, pid=None, env_extra=None, drop=()):
    env = dict(os.environ)
    env["CLAUDE_PID"] = str(pid or os.getpid())
    env.update(env_extra or {})
    for name in drop:
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, str(HOOKS / (entry + ".py"))],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60,
    )


def event(fixture, project, transcript, session_id, **extra):
    payload = load_fixture("payloads", fixture + ".json")["payload"]
    payload.update(session_id=session_id, transcript_path=str(transcript), cwd=str(project))
    payload.update(extra)
    return payload


def output(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout else {}


def context_of(proc):
    return output(proc).get("hookSpecificOutput", {}).get("additionalContext")


def message_of(proc):
    return output(proc).get("systemMessage")


def rotation(project):
    return project / ".claude" / "rotation"


def settings_file(project, **values):
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    (project / ".claude" / "context-rotation.json").write_text(json.dumps(values), encoding="utf-8")


class Session:
    """One simulated Claude Code session: its transcript and its hook calls."""

    def __init__(self, tmp_path, project, session_id, pid=None):
        self.project = project
        self.session_id = session_id
        self.pid = pid or os.getpid()
        self.transcript = tmp_path / "transcripts" / ("%s.jsonl" % session_id)
        first = assistant_entry(1000)
        first["uuid"] = "entry-%05d" % next(_IDS)
        write_transcript(self.transcript, [first])
        self.handover = rotation(project) / ("handover-%s.md" % session_id)
        self._tools = itertools.count(1)

    def tool_id(self):
        return "tool-%s-%d" % (self.session_id, next(self._tools))

    def append(self, *entries):
        append_transcript(self.transcript, entries)

    def marker(self, kind):
        return rotation(self.project) / ("%s-%s.json" % (kind, self.session_id))

    def payload(self, fixture, **extra):
        return event(fixture, self.project, self.transcript, self.session_id, **extra)

    def hook(self, entry, payload, **kwargs):
        return run_hook(entry, payload, pid=self.pid, **kwargs)

    def write_handover(self, text=VALID_HANDOVER, tool="Write"):
        """The Write tool call, the file on disk, the PostToolUse hook, then the result."""
        tool_id = self.tool_id()
        name = "tool_use_write" if tool == "Write" else "tool_use_edit"
        self.append(b1_entry(name, tool_use_id=tool_id, handover_path=self.handover, handover_text="text"))
        self.handover.parent.mkdir(parents=True, exist_ok=True)
        self.handover.write_text(text, encoding="utf-8")
        proc = self.hook("handover_detector", self.payload(
            "07-PostToolUse-Write", tool_name=tool, tool_use_id=tool_id,
            tool_input={"file_path": str(self.handover), "content": text}))
        self.append(
            b1_entry("tool_result", tool_use_id=tool_id, file_path=self.handover, tool_output="written"),
            b1_entry("hook_additional_context", tool_use_id=tool_id, hook_text="checked"),
        )
        return proc

    def stop(self, **extra):
        return self.hook("context_guard", self.payload("03-Stop-idle", **extra))

    def finish_turn(self):
        self.append(b1_entry("assistant_text", assistant_text="Saved."), b1_entry("stop_hook_summary", text="x"))
        return self.stop()

    def prompt(self, text):
        return self.hook("context_guard", self.payload("02-UserPromptSubmit", prompt=text))

    def pre_tool(self, tool_name, tool_input, **extra):
        payload = self.payload("04-PreToolUse-Bash", tool_name=tool_name, tool_input=tool_input, **extra)
        return self.hook("context_guard", payload)

    def expand(self, args, command="context-rotation:rotate"):
        payload = self.payload(
            "09-UserPromptExpansion-plugin-skill", command_name=command, command_args=args,
            prompt="/%s %s" % (command, args))
        return self.hook("context_guard", payload)

    def resume(self, number):
        """`resume`, then `resume <number>`: a number picks from the list last shown in this session."""
        self.expand("resume")
        return self.expand("resume %d" % number)


def start(project, session_id, source="clear", pid=None, **kwargs):
    """SessionStart of a session (the successor after /clear, or a new one)."""
    fixture = {"clear": "17-SessionStart-clear", "startup": "01-SessionStart-startup",
               "compact": "16-SessionStart-compact"}.get(source, "01-SessionStart-startup")
    payload = event(fixture, project, project / "none.jsonl", session_id, source=source)
    return run_hook("session_start", payload, pid=pid, **kwargs)


@pytest.fixture
def other_pid():
    """A live process that is not this one: another `claude`."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()
