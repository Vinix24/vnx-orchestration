"""The shell side of the automatic /clear: scripts/tmux_clear.sh, and the run-shell line through tmux and sh.

The chain runs for real: the Stop hook as a process writes the job file and asks a fake
tmux binary to run the line; the line goes through tmux's expansion (emulated, and once
through a private tmux server of the test's own when tmux is installed) and `sh -c`; the
job runs as a process against the fake binary, which plays pane %7 and starts the fresh
session's SessionStart on Enter after /clear. Nothing is ever typed into a real pane.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

import pytest

from conftest import HOOKS, PKG, Session, b1_entry, clear_events, event, rotation

import tmux_clear

SHELLS = ["/bin/sh", "dash", "bash"]
SCRIPT = PKG / "scripts" / "tmux_clear.sh"
HOSTILE = "a b'c$(x)#{pane_id}"


def shell_path(name):
    path = shutil.which(name) if not name.startswith("/") else (name if os.path.exists(name) else None)
    if path is None:
        pytest.skip("shell %s not present on this machine" % name)
    return path


@pytest.fixture(params=SHELLS)
def shell(request):
    return shell_path(request.param)


def run_shim(shell, script, args, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([shell, str(script)] + list(args), capture_output=True, text=True, env=env, timeout=60)


def fake_root(tmp_path, job_body=None):
    root = tmp_path / "plugin"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(str(SCRIPT), str(root / "scripts" / "tmux_clear.sh"))
    if job_body is not None:
        (root / "lib").mkdir()
        (root / "lib" / "tmux_clear.py").write_text(job_body, encoding="utf-8")
    return root / "scripts" / "tmux_clear.sh"


ARGS = ["%7", "sess-a", "0" * 16, "a" * 32]


# --- the shim ----------------------------------------------------------------------------

def test_the_shim_passes_the_syntax_check_of_every_shell(shell):
    assert subprocess.run([shell, "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_the_off_switch_runs_nothing(shell, tmux_bin, tmp_path):
    script = fake_root(tmp_path, "import sys\nopen(sys.argv[0] + '.ran', 'w').close()\n")
    proc = run_shim(shell, script, ARGS, {"CLAUDE_CONTEXT_ROTATION": "off"})
    assert proc.returncode == 0 and tmux_bin.calls() == []
    assert not (script.parent.parent / "lib" / "tmux_clear.py.ran").exists()


@pytest.mark.parametrize("pane", ["", "%", "%7a", "7", "%7;touch x", "%7 8", "%-1", "%7\n"])
def test_a_pane_outside_its_pattern_is_refused_before_anything_runs(pane, shell, tmux_bin, tmp_path):
    script = fake_root(tmp_path, "import sys\nopen(sys.argv[0] + '.ran', 'w').close()\n")
    proc = run_shim(shell, script, [pane] + ARGS[1:])
    assert proc.returncode == 1 and tmux_bin.calls() == []
    assert not (script.parent.parent / "lib" / "tmux_clear.py.ran").exists()


def test_the_ids_reach_the_job_unchanged(shell, tmux_bin, tmp_path):
    script = fake_root(tmp_path, "import json, sys\nopen(sys.argv[0] + '.args', 'w').write(json.dumps(sys.argv[1:]))\n")
    assert run_shim(shell, script, ARGS).returncode == 0
    assert json.loads((script.parent.parent / "lib" / "tmux_clear.py.args").read_text()) == ARGS


@pytest.mark.parametrize("status, expected", [(0, 0), (1, 1), (2, 1), (3, 3)])
def test_the_job_never_makes_the_shim_exit_two(status, expected, shell, tmux_bin, tmp_path):
    script = fake_root(tmp_path, "import sys\nsys.exit(%d)\n" % status)
    assert run_shim(shell, script, ARGS).returncode == expected


def test_a_missing_job_tells_the_pane_and_types_nothing(shell, tmux_bin, tmp_path):
    script = fake_root(tmp_path)
    proc = run_shim(shell, script, ARGS)
    assert proc.returncode == 1
    (call,) = tmux_bin.calls()
    assert call[:3] == ["display-message", "-t", "%7"] and "Type /clear" in call[3]


def test_no_python3_tells_the_pane_and_types_nothing(shell, tmux_bin, tmp_path):
    script = fake_root(tmp_path, "raise SystemExit(0)\n")
    proc = run_shim(shell, script, ARGS, {"PATH": str(tmux_bin.bin)})
    assert proc.returncode == 1
    assert [call[0] for call in tmux_bin.calls()] == ["display-message"]


# --- the whole chain with a hostile project dir and plugin dir ----------------------------------

def hostile_chain(tmp_path, tmux_bin, claude):
    """A project and a plugin copy whose paths hold a space, a quote, $(...) and #{...}: (session, Stop line).

    The session's `claude` is the stand-in, and the fake pane's terminal is its pty.
    """
    project = tmp_path / HOSTILE
    project.mkdir()
    plugin = tmp_path / ("plug in'$(x)#{pane_id}")
    shutil.copytree(str(PKG), str(plugin), ignore=shutil.ignore_patterns("__pycache__", "tests"))
    canary = tmp_path / "canary"
    (tmux_bin.bin / "x").write_text("#!/bin/sh\ntouch '%s'\n" % canary, encoding="utf-8")
    (tmux_bin.bin / "x").chmod(0o755)
    tmux_bin.set("info", "%s %d 0 0 0 %s" % (tmux_bin.pane, os.getpid(), claude.tty))
    session = Session(tmp_path, project, "sess-a", pid=claude.pid)
    session.write_handover()
    session.append(b1_entry("assistant_text", assistant_text="Saved."), b1_entry("stop_hook_summary", text="x"))
    payload = event("03-Stop-idle", project, session.transcript, "sess-a")
    env = dict(os.environ, CLAUDE_PID=str(claude.pid))
    stop = subprocess.run([sys.executable, str(plugin / "hooks" / "context_guard.py")], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=60)
    assert stop.returncode == 0, stop.stderr
    (call,) = tmux_bin.calls("run-shell")
    assert str(project) not in call[-1] and HOSTILE not in call[-1]
    successor = event("17-SessionStart-clear", project, project / "none.jsonl", "sess-b")
    tmux_bin.set("on-clear", json.dumps({"argv": [sys.executable, str(HOOKS / "session_start.py")],
                                         "input": json.dumps(successor),
                                         "env": {"CLAUDE_PID": str(claude.pid)}}))
    return session, call[-1], canary


def assert_cleared(session, tmux_bin, canary):
    keys = [call[3:] for call in tmux_bin.calls("send-keys")]
    assert keys == [["-l", "/clear"], ["Enter"], ["-l", tmux_clear.CONTINUE], ["Enter"]]
    assert {call[2] for call in tmux_bin.calls("send-keys")} == {"%7"}
    assert clear_events(session.project) == ["clear_started", "clear_entered", "clear_acknowledged",
                                             "clear_continued"]
    assert len(list((rotation(session.project) / "archive").glob("*.md"))) == 1
    assert not canary.exists(), "a $(...) in a path ran"


def test_a_hostile_project_dir_survives_tmux_expansion_and_sh(tmp_path, tmux_bin, claude):
    session, line, canary = hostile_chain(tmp_path, tmux_bin, claude)
    assert "#" not in line.replace("##", ""), "a single # would start a tmux format"
    expanded = line.replace("##", "#")  # what tmux makes of it (measured: test below with a real server)
    proc = subprocess.run(["/bin/sh", "-c", expanded], capture_output=True, text=True, timeout=120)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert_cleared(session, tmux_bin, canary)


@pytest.fixture
def real_tmux():
    found = shutil.which("tmux", path=os.defpath + os.pathsep + "/opt/homebrew/bin:/usr/local/bin:/usr/bin")
    if found is None:
        pytest.skip("tmux is not installed")
    return found


def test_a_hostile_project_dir_survives_a_real_tmux_run_shell(tmp_path, tmux_bin, real_tmux, claude):
    """A private tmux server of this test runs the line; the job it starts talks to the fake binary only."""
    session, line, canary = hostile_chain(tmp_path, tmux_bin, claude)
    socket_dir = tempfile.mkdtemp(prefix="crt", dir="/tmp")
    socket = os.path.join(socket_dir, "s")
    env = dict(os.environ)
    server = [real_tmux, "-S", socket, "-f", "/dev/null"]
    try:
        subprocess.run(server + ["new-session", "-d", "-s", "smoke", "-x", "80", "-y", "20", "sleep 300"],
                       env=env, check=True, timeout=30)
        proc = subprocess.run(server + ["run-shell", "-t", "smoke", line], env=env, capture_output=True, text=True,
                              timeout=120)
        mode = subprocess.run(server + ["display-message", "-p", "-t", "smoke", "#{pane_in_mode}"], env=env,
                              capture_output=True, text=True, timeout=30).stdout.strip()
    finally:
        subprocess.run(server + ["kill-server"], env=env, capture_output=True, timeout=30)
        shutil.rmtree(socket_dir)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert mode == "0", "the run-shell job put the pane into view mode"
    assert_cleared(session, tmux_bin, canary)


def test_the_job_is_the_script_the_line_names(session, tmux_bin):
    session.write_handover()
    session.finish_turn()
    words = shlex.split(tmux_bin.calls("run-shell")[0][-1].replace("##", "#"))
    assert words[0] == "sh" and os.path.samefile(words[1], str(SCRIPT))
