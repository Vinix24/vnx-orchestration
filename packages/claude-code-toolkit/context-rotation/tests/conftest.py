"""Shared fixtures: package paths, an isolated HOME, transcript builders and a simulated session."""
from __future__ import annotations

import itertools
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import pytest

PKG = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "a0"
sys.path.insert(0, str(PKG / "lib"))

import auto_clear  # the plugin's lib, on the path just above
import tmux_clear


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """No test may read the real ~/.claude, inherit plugin switches, or reach a real tmux server.

    Without TMUX and TMUX_PANE every hook takes the manual path; a test of the
    automatic path sets them to a pane of its own fake tmux (test_auto_clear).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONTEXT_ROTATION", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
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


# A `claude` stand-in on a pty of its own: argv is <ready file> foreground|background. It makes the
# pty its controlling terminal (it runs as a session leader, so its process group becomes the
# terminal's foreground group), writes the pid a test records, then becomes `sleep`.
STAND_IN = r'''
import fcntl, os, sys, termios, time
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
pid = os.getpid()
if sys.argv[2] == "background":
    pid = os.fork()
    if pid == 0:
        os.setpgid(0, 0)
        os.execvp("sleep", ["sleep", "300"])
    while os.getpgid(pid) != pid:
        time.sleep(0.01)
with open(sys.argv[1] + ".tmp", "w") as handle:
    handle.write("%d" % pid)
os.rename(sys.argv[1] + ".tmp", sys.argv[1])
os.execvp("sleep", ["sleep", "300"])
'''


class StandIn(NamedTuple):
    """A process a test records as the session's `claude`, and its controlling terminal as tmux names it."""

    pid: int
    tty: str


@pytest.fixture
def stand_in(tmp_path):
    """Start `claude` stand-ins on terminals of their own: ptys the test opens, never a real claude.

    `stand_in()` gives the session leader of a new pty, in the terminal's foreground
    process group: it owns the terminal's input. `stand_in("background")` gives a child
    of such a leader in a process group of its own, on the same terminal, while the
    leader's group stays in the foreground. Each runs below this process and is killed
    at the end of the test.
    """
    started = []

    def start(mode="foreground"):
        master, slave = os.openpty()
        tty = os.ttyname(slave)
        ready = tmp_path / ("stand-in-%d" % len(started))
        leader = subprocess.Popen([sys.executable, "-c", STAND_IN, str(ready), mode], stdin=slave, stdout=slave,
                                  stderr=slave, start_new_session=True)
        os.close(slave)
        started.append((leader, master, ready))
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert leader.poll() is None and time.monotonic() < deadline, "the stand-in did not start"
            time.sleep(0.01)
        return StandIn(int(ready.read_text()), tty)

    yield start
    for leader, master, ready in started:
        if ready.exists() and int(ready.read_text()) != leader.pid:
            os.kill(int(ready.read_text()), signal.SIGKILL)  # the leader's child: at worst its zombie, never gone
        leader.kill()
        leader.wait()
        os.close(master)


@pytest.fixture
def claude(stand_in):
    """The session's `claude`: a stand-in that owns the input of its terminal, which the fake pane names."""
    return stand_in()


# --- B2: sizes, the enforcement state, and what a hook decided ----------------

BELOW, WARN, FORCE, HARD = 1000, 120000, 140000, 170000
ALLOWED = "every tool except Read, Glob, Grep, Skill, AskUserQuestion, TodoWrite and a Write or Edit of %s is denied"


def at(session, total):
    """The session's context now measures `total` tokens."""
    entry = assistant_entry(total)
    entry["uuid"] = "size-%05d" % next(_IDS)
    session.append(entry)


def compacted(session, post_tokens):
    """A compaction: the context measures `post_tokens` after it (A0 rule 2)."""
    entry = load_fixture("transcript", "compact_boundary.json")
    entry["compactMetadata"]["postTokens"] = post_tokens
    entry["uuid"] = "compact-%05d" % next(_IDS)
    session.append(entry)


def state_of(session):
    path = rotation(session.project) / "state" / ("%s.enforcement.json" % session.session_id)
    return json.loads(path.read_text()) if path.exists() else None


def journal(project):
    path = rotation(project) / "enforcement.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def decision(proc):
    return output(proc).get("hookSpecificOutput", {}).get("permissionDecision")


def reason(proc):
    return output(proc)["hookSpecificOutput"]["permissionDecisionReason"]


def held_back(proc):
    return output(proc).get("decision") == "block"


def bash(session, **extra):
    return session.pre_tool("Bash", {"command": "make test"}, **extra)


def goes_ahead(proc):
    assert proc.returncode == 0, proc.stderr
    return "permissionDecision" not in proc.stdout and '"decision"' not in proc.stdout


def snapshot(root):
    """Every entry below `root` with its bytes (None for a directory or a symlink), so a change shows."""
    found = {}
    for directory, dirs, files in os.walk(str(root)):
        for name in dirs + files:
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, str(root))
            if os.path.islink(path) or os.path.isdir(path):
                found[relative] = None
            else:
                with open(path, "rb") as handle:
                    found[relative] = handle.read()
    return found


def gate_lines(proc):
    return [line for line in proc.stderr.splitlines() if line.startswith("context-rotation:")]


def refused_once(proc, place):
    assert proc.returncode == 0, proc.stderr
    lines = gate_lines(proc)
    assert len(lines) == 1, proc.stderr
    assert "%s is a symlink" % place in lines[0]


# --- C: a tmux that plays one pane from the A0 captures ---------------------------

PANES = FIXTURES / "panes"
PROMPT = "❯ "  # the input line's prompt (A0 rule 6)
IDLE = "04-idle-empty-auto.esc.txt"
AFTER_CLEAR = "07-after-clear-auto.esc.txt"


def pane_text(name):
    return (PANES / name).read_text(encoding="utf-8")


def with_input(screen, typed):
    """The screen with `typed` in its input line: right after the one prompt with a no-break space."""
    assert screen.count(PROMPT) <= 1
    return screen.replace(PROMPT, PROMPT + typed, 1)


FORMAT = re.compile(r"#\{([a-z_]+)\}")


def commands_of(args):
    """The commands of one tmux call: its arguments split at each ";" (a command sequence)."""
    commands, current = [], []
    for arg in args:
        if arg == ";":
            commands.append(current)
            current = []
        else:
            current.append(arg)
    return commands + [current]


def input_row(screen):
    """The row of the input line: the one line that holds the prompt with its no-break space (with_input)."""
    return next((number for number, line in enumerate(screen.split("\n")) if PROMPT in line), 0)


class FakeTmux:
    """Stands in for auto_clear.run_tmux: records every call and plays one pane.

    The pane shows an A0 capture, with what was typed into its input line. The cursor
    sits `back` characters before the end of that text: a literal goes in there and a
    BSpace removes the character before it, as in Claude Code's input line. Enter
    submits the line: `submitted` keeps it, `on_enter` callbacks get it, and after
    /clear the pane shows the capture taken right after a /clear. A call with a
    command sequence (`a ; b`) runs its commands in order and stops at the first that
    fails, as tmux does; `on_capture` runs once before a call that captures, so what
    it changes shows in every command of that call. `pane_tty` is the pane's terminal:
    a pty of a stand-in `claude` (stand_in), whose first process runs below this one.
    """

    def __init__(self, pane="%7", pane_pid=None, screen=IDLE, pane_tty=""):
        self.pane = pane
        self.pane_pid = os.getpid() if pane_pid is None else pane_pid
        self.pane_tty = pane_tty
        self.base = pane_text(screen)
        self.typed = ""
        self.back = 0
        self.echo = True
        self.info = None
        self.fields = {}
        self.calls = []
        self.fail = {}
        self.submitted = []
        self.on_enter = []
        self.on_capture = None

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        commands = commands_of(args)
        if self.on_capture and any(command[0] == "capture-pane" for command in commands):
            self.on_capture(self)
        status, out = 0, ""
        for command in commands:
            status, text = self.one(command)
            out += text
            if status != 0:
                break
        return status, out

    def one(self, args):
        failure = self.fail.get("pane-info" if args[0] == "display-message" and "-p" in args else args[0])
        if isinstance(failure, BaseException):
            raise failure
        if isinstance(failure, int):
            return failure, ""
        if args[0] == "capture-pane":
            return 0, self.screen()
        if args[0] == "display-message" and "-p" in args:
            return 0, self.expand(args[-1]) + "\n"
        if args[0] == "send-keys":
            self.keys(args[3:])
        return 0, ""

    def screen(self):
        return with_input(self.base, self.typed)

    def expand(self, form):
        """display-message -p: the format with each #{name} filled in; `info` replaces the whole line."""
        if self.info is not None:
            return self.info
        values = {"pane_id": self.pane, "pane_pid": str(self.pane_pid), "pane_dead": "0", "pane_in_mode": "0",
                  "pane_synchronized": "0", "cursor_x": str(len(PROMPT) + len(self.typed) - self.back),
                  "cursor_y": str(input_row(self.screen())), "pane_tty": self.pane_tty}
        values.update(self.fields)
        return FORMAT.sub(lambda match: values[match.group(1)], form)

    def keys(self, keys):
        at = len(self.typed) - self.back
        if keys[:1] == ["-l"]:
            if self.echo:
                self.typed = self.typed[:at] + keys[1] + self.typed[at:]
            return
        for key in keys:
            at = len(self.typed) - self.back
            if key == "BSpace" and at > 0:
                self.typed = self.typed[:at - 1] + self.typed[at:]
            elif key == "Enter":
                submitted, self.typed, self.back = self.typed, "", 0
                self.submitted.append(submitted)
                if submitted == "/clear":
                    self.base = pane_text(AFTER_CLEAR)
                for callback in self.on_enter:
                    callback(submitted)

    def sent(self):
        """The keys of every send-keys call, in order."""
        return [call[3:] for call in self.calls if call[0] == "send-keys"]

    def shown(self):
        """The lines display-message showed to the user."""
        return [call[-1] for call in self.calls if call[0] == "display-message" and "-p" not in call]


FAKE_TMUX_PY = r'''
import json, os, re, subprocess, sys

state = os.environ["FAKE_TMUX_DIR"]
PROMPT = "❯ "
FIELDS = ("pane_id", "pane_pid", "pane_dead", "pane_in_mode", "pane_synchronized", "pane_tty")


def path(name):
    return os.path.join(state, name)


def read(name, default=""):
    try:
        with open(path(name), encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return default


def write(name, text):
    with open(path(name), "w", encoding="utf-8") as handle:
        handle.write(text)


def screen():
    return read("screen").replace(PROMPT, PROMPT + read("typed"), 1)


def expand(form):
    """The format with each #{name} filled in: the pane's fields from `info`, the cursor after the typed text."""
    values = dict(zip(FIELDS, read("info").split()))
    lines = screen().split("\n")
    values["cursor_y"] = str(next((n for n, line in enumerate(lines) if PROMPT in line), 0))
    values["cursor_x"] = str(len(PROMPT) + len(read("typed")))
    return re.sub(r"#\{([a-z_]+)\}", lambda match: values.get(match.group(1), ""), form)


def send(keys):
    typed = read("typed")
    if keys[:1] == ["-l"]:
        typed += keys[1]
    else:
        for key in keys:
            if key == "BSpace":
                typed = typed[:-1]
            elif key == "Enter":
                if typed == "/clear" and read("after-clear"):
                    write("screen", read("after-clear"))
                    spec = read("on-clear")
                    if spec:
                        spec = json.loads(spec)
                        subprocess.run(spec["argv"], input=spec["input"], env=dict(os.environ, **spec["env"]),
                                       capture_output=True, text=True, timeout=60)
                typed = ""
    write("typed", typed)


def run(args):
    """One command of the call: its exit status (a status-<command> file makes it fail)."""
    name = args[0] if args else ""
    status = read("status-" + name).strip()
    if status:
        return int(status)
    if name == "capture-pane":
        sys.stdout.write(screen())
    elif name == "display-message" and "-p" in args:
        sys.stdout.write(expand(args[-1]) + "\n")
    elif name == "send-keys":
        send(args[3:])
    return 0


args = sys.argv[1:]
with open(path("calls.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
if args[:1] == ["-L"]:  # a named server (the live smoke's): this fake plays one pane for every server
    args = args[2:]
commands, current = [], []
for arg in args:
    if arg == ";":
        commands.append(current)
        current = []
    else:
        current.append(arg)
for command in commands + [current]:
    status = run(command)
    if status:
        sys.exit(status)
'''


class TmuxBinary:
    """A fake `tmux` executable on PATH for hooks and jobs that run as processes; its state is files in `dir`."""

    def __init__(self, root, pane="%7"):
        self.dir = root / "fake-tmux"
        self.bin = root / "fake-bin"
        self.dir.mkdir()
        self.bin.mkdir()
        self.pane = pane
        (self.dir / "fake_tmux.py").write_text(FAKE_TMUX_PY, encoding="utf-8")
        executable = self.bin / "tmux"
        executable.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, self.dir / "fake_tmux.py"),
                              encoding="utf-8")
        executable.chmod(0o755)
        self.set("screen", pane_text(IDLE))
        self.set("after-clear", pane_text(AFTER_CLEAR))
        self.set("info", "%s %d 0 0 0" % (pane, os.getpid()))

    def set(self, name, text):
        (self.dir / name).write_text(text, encoding="utf-8")

    def calls(self, name=None):
        path = self.dir / "calls.jsonl"
        found = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        return [call for call in found if name is None or call[0] == name]

    def env(self):
        return {"FAKE_TMUX_DIR": str(self.dir), "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                "TMUX": str(self.dir / "no-server,1,0"), "TMUX_PANE": self.pane}


@pytest.fixture
def tmux_bin(tmp_path, monkeypatch):
    """Inside a fake tmux: every hook of the test runs in pane %7 of a server that does not exist."""
    fake = TmuxBinary(tmp_path)
    for key, value in fake.env().items():
        monkeypatch.setenv(key, value)
    return fake


# What the job reads in each A0 capture: None where it may type, else why not (state_fields.CLEAR_ABORTS).
# The job reads `capture-pane -e` (.esc.txt); without -e a placeholder or a suggestion looks typed, so a
# plain capture could only ever make it type less.
PANE_EXPECTED = {
    "01-idle-placeholder-auto.esc.txt": None,
    "01-idle-placeholder-auto.txt": "input",
    "02-idle-typed-text-auto.txt": "input",
    "03-busy-auto.esc.txt": "busy",
    "03-busy-auto.txt": "busy",
    "04-idle-empty-auto.esc.txt": None,
    "04-idle-empty-auto.txt": None,
    "05-idle-suggestion-auto.esc.txt": None,
    "05-idle-suggestion-auto.txt": "input",
    "06-idle-typed-text-auto.esc.txt": "input",
    "06-idle-typed-text-auto.txt": "input",
    "07-after-clear-auto.esc.txt": None,
    "07-after-clear-auto.txt": None,
    "08-idle-empty-mode2.esc.txt": None,
    "08-idle-empty-mode2.txt": None,
    "09-idle-typed-mode2.esc.txt": "input",
    "09-idle-typed-mode2.txt": "input",
    "10-busy-mode2.esc.txt": "busy",
    "10-busy-mode2.txt": "busy",
    "11-permission-dialog-mode2.esc.txt": "no_input_box",
    "11-permission-dialog-mode2.txt": "no_input_box",
}


def journal_lines(project):
    path = rotation(project) / "injections.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def clear_events(project):
    return [line["event"] for line in journal_lines(project) if line["event"].startswith("clear_")]


class Clock:
    """monotonic and sleep for the job: a sleep moves the time on, nothing waits."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    found = Clock()
    monkeypatch.setattr(tmux_clear, "monotonic", found.monotonic)
    monkeypatch.setattr(tmux_clear, "sleep", found.sleep)
    return found


@pytest.fixture
def fake(monkeypatch, tmux_bin, claude):
    """The tmux the job talks to in-process; the hooks of the test run in pane %7 of the fake binary (tmux_bin).

    The pane's terminal is the stand-in `claude`'s pty, so that `claude` owns its input.
    """
    recorder = FakeTmux(pane_tty=claude.tty)
    monkeypatch.setattr(auto_clear, "run_tmux", recorder)
    return recorder


@pytest.fixture
def session(tmp_path, project, tmux_bin, claude):
    """Session sess-a, run by the stand-in `claude`: the process its hooks record and the job checks."""
    return Session(tmp_path, project, "sess-a", pid=claude.pid)


def started(session, tmux_bin):
    """Write the handover and end the turn: the Stop hook starts the job. The job's four arguments."""
    session.write_handover()
    assert message_of(session.finish_turn()) == auto_clear.STARTED
    line = tmux_bin.calls("run-shell")[-1][-1]
    return shlex.split(line.replace("##", "#"))[2:6]
