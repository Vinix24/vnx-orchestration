"""tests/live/smoke_tmux.sh: its refusals and its `check`, on evidence the real job writes.

`start` with valid arguments starts a real claude in a real tmux server, so no test
calls it that way: only the refusals that exit before anything starts run here.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from conftest import IDLE, PKG, PROMPT, load_fixture, pane_text, start, started, with_input

import auto_clear
import tmux_clear

SMOKE = PKG / "tests" / "live" / "smoke_tmux.sh"


def smoke(*args):
    return subprocess.run(["sh", str(SMOKE)] + [str(a) for a in args], capture_output=True, text=True, timeout=60)


def evidence(project):
    path = project.parent / (project.name + ".evidence")
    path.mkdir(exist_ok=True)
    (path / "plugin_dir").write_text(str(PKG) + "\n")
    return path


def test_the_script_passes_the_syntax_check_of_sh():
    assert subprocess.run(["sh", "-n", str(SMOKE)]).returncode == 0


@pytest.mark.parametrize("args, says", [
    ([], "usage:"),
    (["start", "/no/such/plugin", "/tmp/never"], "is not the plugin dir"),
    (["start", PKG, PKG], "exists"),
    (["start", PKG, "/tmp/never#{pane_id}"], "holds a '#'"),
    (["start", PKG, "/tmp/never", "lots"], "window_tokens must be a number"),
    (["capture", "/tmp/never", "a b"], "a label is"),
    (["check", "/tmp/never", "everything"], "usage:"),
])
def test_the_refusals_start_nothing(args, says):
    proc = smoke(*args)
    assert proc.returncode == 1 and says in proc.stderr


def test_check_passes_on_a_clear_that_went_all_the_way(session, fake, tmux_bin, project, clock):
    fake.on_enter.append(
        lambda line: start(project, "sess-b", source="clear", pid=session.pid) if line == "/clear" else None)
    tmux_clear.main(started(session, tmux_bin))
    evidence(project)
    proc = smoke("check", project, "happy")
    assert proc.returncode == 0, proc.stdout
    assert "FAIL" not in proc.stdout and proc.stdout.count("PASS") == 5


def test_check_fails_where_the_run_did_not_happen(session, fake, tmux_bin, project, clock):
    fake.typed = "hello"
    tmux_clear.main(started(session, tmux_bin))
    evidence(project)
    proc = smoke("check", project, "happy")
    assert proc.returncode == 1 and "FAIL started, /clear entered" in proc.stdout


def captured(project, fake, label):
    """What `capture` saves of the pane the fake plays: its screen and where it has the cursor."""
    path = evidence(project)
    (path / (label + ".esc.txt")).write_text(fake.screen(), encoding="utf-8")
    (path / (label + ".cursor")).write_text(fake.expand("#{cursor_x} #{cursor_y}") + "\n", encoding="utf-8")


def test_check_passes_on_text_in_the_input_line_and_the_work_after_it(session, fake, tmux_bin, project, clock):
    fake.typed = "hello"
    tmux_clear.main(started(session, tmux_bin))
    captured(project, fake, "input")
    proc = smoke("check", project, "input")
    assert proc.returncode == 0, proc.stdout
    assert "PASS the input line still reads hello" in proc.stdout
    assert "PASS the cursor stands right after the typed text" in proc.stdout
    session.prompt("hello")
    proc = smoke("check", project, "keep-working")
    assert proc.returncode == 0, proc.stdout


@pytest.mark.parametrize("cursor", ["moved", "absent"])
def test_check_fails_when_the_cursor_is_not_after_the_typed_text(cursor, session, fake, tmux_bin, project, clock):
    fake.typed = "hello"
    tmux_clear.main(started(session, tmux_bin))
    fake.back = 2
    captured(project, fake, "input")
    if cursor == "absent":
        (evidence(project) / "input.cursor").unlink()
    proc = smoke("check", project, "input")
    assert proc.returncode == 1 and "FAIL the cursor stands right after the typed text" in proc.stdout


def test_capture_saves_the_screen_and_the_cursor_of_one_moment(tmux_bin, project):
    tmux_bin.set("typed", "hello")
    path = evidence(project)
    proc = smoke("capture", project, "input")
    assert proc.returncode == 0, proc.stderr
    screen = with_input(pane_text(IDLE), "hello")
    assert (path / "input.esc.txt").read_text(encoding="utf-8") == screen
    row = next(number for number, line in enumerate(screen.split("\n")) if PROMPT in line)
    assert (path / "input.cursor").read_text() == "%d %d\n" % (len(PROMPT) + len("hello"), row)
    assert sorted(p.name for p in path.iterdir()) == ["input.cursor", "input.esc.txt", "input.txt", "plugin_dir"]
    (look,) = [call for call in tmux_bin.calls() if "display-message" in call]
    assert look[:2] == ["-L", "context-rotation-smoke"] and ";" in look and "capture-pane" in look


def test_check_passes_on_a_background_shell(session, tmux_bin, project):
    session.write_handover()
    tasks = load_fixture("payloads", "13-Stop-with-background-tasks.json")["payload"]["background_tasks"]
    proc = session.stop(background_tasks=[task for task in tasks if task["type"] == "shell"])
    assert auto_clear.BACKGROUND % "1 background shell" in proc.stdout
    (evidence(project) / "background.txt").write_text("  ⎿  Stop says: %s\n" % (
        auto_clear.BACKGROUND % "1 background shell"), encoding="utf-8")
    proc = smoke("check", project, "background")
    assert proc.returncode == 0, proc.stdout


def test_check_without_any_handover_fails(project):
    evidence(project)
    proc = smoke("check", project, "happy")
    assert proc.returncode == 1 and "FAIL a handover was marked ready" in proc.stdout
    assert not os.path.exists(str(project / ".claude"))
