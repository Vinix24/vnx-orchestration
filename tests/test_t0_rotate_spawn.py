"""tests/test_t0_rotate_spawn.py — the rotation spawner (dispatch 20260925-t0-context-rotation-enforced).

scripts/t0_rotate_spawn.sh runs for real against a stub ``tmux`` and a stub ``claude`` on PATH.
No real tmux server or claude session is ever touched:

  - stub tmux logs every call (argv) to calls.ndjson and plays a claude screen back on
    ``capture-pane``: a shell before the launch line is submitted, the footer after it, a busy
    marker for the first two captures after the kickoff prompt, and ``/goal active`` once a
    /goal line was submitted (unless told to never accept it). ``run-shell -b`` executes its
    command synchronously so the tmux-server follow-up is observable in the test.
  - stub claude only records that it was executed; the spawner must never run claude itself,
    it types the launch line into the new pane.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPAWN = REPO_ROOT / "scripts" / "t0_rotate_spawn.sh"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import t0_rotation_state as rotation_state

OLD_WIN = "@1"
NEW_WIN = "@7"
WINDOW_NAME = "VNX ORCH"
PANE = "%9"
SESSION = "sess-rot-1"

STUB_TMUX = r'''#!/usr/bin/env python3
import json, os, re, subprocess, sys
stub = os.environ["STUB_DIR"]
args = sys.argv[1:]
with open(os.path.join(stub, "calls.ndjson"), "a") as fh:
    fh.write(json.dumps(args) + "\n")

def calls():
    with open(os.path.join(stub, "calls.ndjson")) as fh:
        return [json.loads(l) for l in fh if l.strip()]

def submitted():
    """Texts followed by a separate Enter keystroke, in order."""
    out, pending, eaten = [], None, False
    for c in calls():
        if c[:1] != ["send-keys"]:
            continue
        if "-l" in c:
            pending = c[-1]
        elif c[-1] == "Enter":
            if (os.environ.get("STUB_EAT_FIRST_GOAL_ENTER") and not eaten
                    and pending and pending.startswith("/goal ")):
                eaten = True  # the slash autocomplete takes the first Enter
                continue
            out.append(pending)
            pending = None
    return out, pending

cmd = args[0] if args else ""
def pane_path(target):
    """Folder of a pane: the old pane is configurable, the new window defaults to the -c folder of
    new-window and is overridable (STUB_NEW_PANE_PATH) for the wrong-folder case."""
    if target == "%9":
        return os.environ.get("STUB_OLD_PANE_PATH", "")
    if os.environ.get("STUB_NEW_PANE_PATH"):
        return os.environ["STUB_NEW_PANE_PATH"]
    path = os.path.join(stub, "new_window_cwd")
    return open(path).read() if os.path.exists(path) else ""

if cmd == "display-message" and "-p" in args:
    fmt = args[-1]
    target = args[args.index("-t") + 1] if "-t" in args else ""
    if fmt == "#{pane_current_path}":
        print(pane_path(target))
    else:
        print({"#{window_id}": "@1", "#{window_name}": "VNX ORCH"}.get(fmt, ""))
elif cmd == "show-environment":
    # Only ever asked for ONE variable by name, in the three real forms.
    name = args[-1]
    state = os.environ.get("STUB_TMUX_GLOBAL", "unset")
    if args[:2] != ["show-environment", "-g"] or name.startswith("-") or len(args) != 3:
        sys.stderr.write("stub: show-environment must name exactly one variable\n")
        sys.exit(64)
    if state == "unset":
        sys.stderr.write("unknown variable: %s\n" % name)
        sys.exit(1)
    print("-" + name if state == "removed" else "%s=%s" % (name, state))
elif cmd == "new-window":
    if "-c" in args:
        open(os.path.join(stub, "new_window_cwd"), "w").write(args[args.index("-c") + 1])
    print("@7")
elif cmd == "capture-pane":
    subs, typed = submitted()
    counter = os.path.join(stub, "captures_after_kickoff")
    screen = "vincent@mac project % "
    # A T0-folder start types `cd -- <folder> && <launch>`; recognise the launch behind the prefix.
    bare = [re.sub(r"^cd -- (?:\\.|\S)+ && ", "", s) for s in subs if s]
    launched = any(s.startswith(("claude ", "CLAUDE_CONFIG_DIR=")) for s in bare)
    if launched and not os.environ.get("STUB_NO_FOOTER"):
        screen = "> \n  ⏵⏵ auto mode on (shift+tab to cycle)"
        if any(s and s.startswith("Je bent de verse T0") for s in subs):
            n = int(open(counter).read()) if os.path.exists(counter) else 0
            open(counter, "w").write(str(n + 1))
            if n < 2:
                screen = "✻ Working… (esc to interrupt)\n  ⏵⏵ auto mode on"
        goal_subs = [s for s in subs if s and s.startswith("/goal ")]
        if goal_subs and not os.environ.get("STUB_GOAL_NEVER"):
            screen += "  · /goal active"
        elif typed and typed.startswith("/goal "):
            screen = "> " + typed + "\n  ⏵⏵ auto mode on"
    print(screen)
elif cmd == "run-shell":
    # Synchronous here so the test can observe it; like real tmux, -b reports 0 to the caller
    # whatever the background job ends with (its outcome lands in the receipt instead).
    rc = subprocess.run(["bash", "-c", args[-1]]).returncode
    sys.exit(0 if "-b" in args else rc)
'''

STUB_CLAUDE = '''#!/usr/bin/env bash
echo "$@" >> "$STUB_DIR/claude_invoked"
'''

# The two `stat` families the spawner meets. GNU coreutils reads -f as "filesystem status", so
# `stat -f %m FILE` treats BOTH operands as files: `%m` fails, FILE prints its filesystem block
# ("  File: ...") on stdout, and the status is 1. That block is what reached the spawner's
# arithmetic on the ubuntu CI runner (`File: unbound variable`). BSD stat (macOS) takes
# `-f FORMAT` and has no -c.
STUB_STAT_GNU = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args[:1] == ["-c"]:
    if args[1] == "%Y":
        print(int(os.stat(args[2]).st_mtime))
        sys.exit(0)
    sys.exit(1)
if args[:1] == ["-f"]:
    rc = 0
    for operand in args[1:]:
        if os.path.exists(operand):
            print('  File: "%s"\n    ID: 0        Namelen: 255     Type: ext2/ext3\n'
                  'Block size: 4096       Fundamental block size: 4096' % operand)
        else:
            sys.stderr.write("stat: cannot read file system information for '%s': "
                             "No such file or directory\n" % operand)
            rc = 1
    sys.exit(rc)
sys.exit(1)
'''

STUB_STAT_BSD = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args[:1] == ["-f"] and args[1] == "%m":
    print(int(os.stat(args[2]).st_mtime))
    sys.exit(0)
sys.stderr.write("stat: illegal option -- %s\n" % args[0].lstrip("-")[:1])
sys.exit(1)
'''

HANDOFF_BASE = """# Handoff — vnx-orchestration — 25 september

## Waar we middenin zitten
Iets.

## Next steps (om koud op te pakken)

1. **#1920 mergen**: lees het rapport, controleer rood/groen,
   glm-gate op de kop.
2. **v1.6.6 knippen**: release-dispatch.

## Wacht op de operator
- niets
"""

GOAL_SECTION = """
## Actief /goal
Directive: `/goal werk alle open blockers af tot de lijst leeg is`
Resterend:
- OI-1850 sluiten
- OI-1851 fixen
"""


@pytest.fixture
def rig(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "tmux").write_text(STUB_TMUX)
    (bindir / "claude").write_text(STUB_CLAUDE)
    for name in ("tmux", "claude"):
        (bindir / name).chmod(0o755)
    project = tmp_path / "project"
    (project / "daily-log").mkdir(parents=True)
    state = tmp_path / "state"
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path / "home"),
        "STUB_DIR": str(stub),
        "TMUX": "/tmp/stub-tmux,1,0",
        "TMUX_PANE": PANE,
        "VNX_PYTHON": sys.executable,
        "VNX_T0_ROTATION_STATE_DIR": str(state),
        "VNX_T0_ROTATION_RECEIPTS_FILE": str(tmp_path / "receipts" / "t0_receipts.ndjson"),
        "VNX_T0_ROTATE_POLL_SECONDS": "0",
        "VNX_T0_ROTATE_SETTLE_SECONDS": "0",
        "VNX_T0_ROTATE_FOOTER_TIMEOUT": "5",
        "VNX_T0_ROTATE_TURN_TIMEOUT": "20",
        "VNX_T0_ROTATE_GOAL_CONFIRM_TIMEOUT": "3",
    }
    return {"tmp": tmp_path, "stub": stub, "project": project, "state": state, "env": env}


def _handoff(rig, text: str, age_seconds: int = 0) -> Path:
    path = rig["project"] / "daily-log" / "handoff.md"
    path.write_text(text, encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def _spawn(rig, *args: str, env_extra: Dict[str, str] = None) -> subprocess.CompletedProcess:
    env = {**rig["env"], **(env_extra or {})}
    return subprocess.run(["bash", str(SPAWN), "--project-root", str(rig["project"]), *args],
                          env=env, cwd=rig["project"], capture_output=True, text=True, timeout=120)


def _calls(rig) -> List[list]:
    path = rig["stub"] / "calls.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _typed(rig) -> List[str]:
    return [c[-1] for c in _calls(rig) if c[:1] == ["send-keys"] and "-l" in c]


def _receipts(rig) -> list:
    path = Path(rig["env"]["VNX_T0_ROTATION_RECEIPTS_FILE"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _assert_enter_always_separate(rig) -> None:
    sends = [c for c in _calls(rig) if c[:1] == ["send-keys"]]
    assert sends, "nothing was sent"
    for idx, call in enumerate(sends):
        if "-l" in call:
            assert "Enter" not in call[:-1], call
            nxt = sends[idx + 1]
            assert nxt == ["send-keys", "-t", call[call.index("-t") + 1], "Enter"], (call, nxt)
        else:
            assert call[-1] == "Enter" and len(call) == 4, call


# ── refusals ─────────────────────────────────────────────────────────────────


def test_refuses_a_stale_handoff(rig):
    _handoff(rig, HANDOFF_BASE, age_seconds=20 * 60)
    proc = _spawn(rig)
    assert proc.returncode == 3
    assert "min old" in proc.stderr
    assert not any(c[0] == "new-window" for c in _calls(rig))


def test_max_age_is_configurable(rig):
    _handoff(rig, HANDOFF_BASE, age_seconds=20 * 60)
    assert _spawn(rig, "--max-age-minutes", "30").returncode == 0


@pytest.mark.parametrize("flavor", ["gnu", "bsd"])
def test_handoff_age_does_not_depend_on_the_platform_stat(rig, flavor):
    """The age check ran `stat -f %m ... || stat -c %Y ...`; on GNU stat the first call prints a
    filesystem block and only then fails, so the block became the mtime and `set -u` aborted the
    spawner with rc 1 on every Linux runner. Whichever stat is on PATH, a fresh handoff spawns
    and a stale one is refused with the age in the message."""
    stat = rig["tmp"] / "bin" / "stat"
    stat.write_text(STUB_STAT_GNU if flavor == "gnu" else STUB_STAT_BSD)
    stat.chmod(0o755)

    _handoff(rig, HANDOFF_BASE)
    fresh = _spawn(rig)
    assert fresh.returncode == 0, fresh.stderr
    assert "unbound variable" not in fresh.stderr

    _handoff(rig, HANDOFF_BASE, age_seconds=20 * 60)
    stale = _spawn(rig)
    assert stale.returncode == 3, stale.stderr
    assert "20 min old" in stale.stderr


def test_refuses_a_missing_handoff(rig):
    proc = _spawn(rig)
    assert proc.returncode == 3
    assert "handoff not found" in proc.stderr


def test_refuses_outside_tmux(rig):
    _handoff(rig, HANDOFF_BASE)
    env = {k: v for k, v in rig["env"].items() if k not in ("TMUX", "TMUX_PANE")}
    proc = subprocess.run(["bash", str(SPAWN), "--project-root", str(rig["project"])], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 3
    assert _calls(rig) == []


def test_refuses_a_handoff_without_next_steps(rig):
    _handoff(rig, "# Handoff\n\n## State\nniets\n")
    proc = _spawn(rig)
    assert proc.returncode == 4
    assert not any(c[0] == "new-window" for c in _calls(rig))


def test_successor_without_footer_gets_nothing_typed(rig):
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig, env_extra={"STUB_NO_FOOTER": "1"})
    assert proc.returncode == 5
    assert _typed(rig) == ["claude --model opus"]
    assert not rig["state"].joinpath(f"latch-pane-{PANE.replace('%', '_')}.json").exists()


# ── the handover ─────────────────────────────────────────────────────────────


def test_spawns_successor_with_first_step_and_no_goal(rig):
    rotation_state.write_pending(rig["state"], pane=PANE, session_id=SESSION, tokens=512_000,
                                 level="force", transcript_path="/t.jsonl")
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig)
    assert proc.returncode == 0, proc.stderr

    calls = _calls(rig)
    new_window = next(c for c in calls if c[0] == "new-window")
    assert new_window[new_window.index("-n") + 1] == WINDOW_NAME
    assert new_window[new_window.index("-t") + 1] == OLD_WIN
    assert new_window[new_window.index("-c") + 1] == str(rig["project"].resolve())

    typed = _typed(rig)
    assert typed[0] == "claude --model opus"
    prompt = typed[1]
    assert f"tmux kill-window -t {OLD_WIN}" in prompt
    assert "kickoff-skill" in prompt
    assert ("Pak daarna stap 1 op: **#1920 mergen**: lees het rapport, controleer rood/groen, "
            "glm-gate op de kop.") in prompt
    assert "\n" not in prompt
    assert "/goal" not in prompt
    assert len(typed) == 2, typed
    assert not any(c[0] == "run-shell" for c in calls)
    _assert_enter_always_separate(rig)
    assert not (rig["stub"] / "claude_invoked").exists()

    assert rotation_state.is_latched(rig["state"], session_id=SESSION, pane="")
    [receipt] = _receipts(rig)
    assert receipt["trigger"] == "t0_context_rotation_started"
    assert receipt["receipt_kind"] == "state_mutation"
    assert receipt["session_id"] == SESSION
    assert receipt["old_window"] == OLD_WIN and receipt["new_window"] == NEW_WIN


def test_explicit_session_id_wins(rig):
    _handoff(rig, HANDOFF_BASE)
    assert _spawn(rig, "--session-id", "explicit-1").returncode == 0
    assert rotation_state.is_latched(rig["state"], session_id="explicit-1", pane="")
    assert rotation_state.is_latched(rig["state"], session_id="", pane=PANE)


def test_remote_control_follows_the_bridge_and_config_dir_is_carried(rig):
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig, env_extra={"CLAUDE_CODE_BRIDGE_SESSION_ID": "bridge-1",
                                  "CLAUDE_CONFIG_DIR": "/Users/x/.claude-salesminds"})
    assert proc.returncode == 0, proc.stderr
    assert _typed(rig)[0] == ("CLAUDE_CONFIG_DIR=/Users/x/.claude-salesminds "
                              "claude --model opus --remote-control VNX\\ ORCH")


def test_no_rc_overrides_the_bridge(rig):
    _handoff(rig, HANDOFF_BASE)
    assert _spawn(rig, "--no-rc", env_extra={"CLAUDE_CODE_BRIDGE_SESSION_ID": "b"}).returncode == 0
    assert _typed(rig)[0] == "claude --model opus"


def test_active_goal_is_resumed_after_the_first_turn(rig):
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig)
    assert proc.returncode == 0, proc.stderr

    typed = _typed(rig)
    assert "start zelf geen nieuwe /goal" in typed[1]
    assert typed[2] == ("/goal werk alle open blockers af tot de lijst leeg is, hervat na "
                        "context-rotatie; resterend: OI-1850 sluiten; OI-1851 fixen")
    assert len(typed) == 3, typed
    _assert_enter_always_separate(rig)

    calls = _calls(rig)
    kickoff_enter = max(i for i, c in enumerate(calls) if c[:1] == ["send-keys"] and "-l" in c
                        and c[-1].startswith("Je bent")) + 1
    goal_typed = next(i for i, c in enumerate(calls) if c[:1] == ["send-keys"] and "-l" in c
                      and c[-1].startswith("/goal "))
    # the turn was watched: busy captures, then idle, before the /goal was typed
    captures_between = [c for c in calls[kickoff_enter:goal_typed] if c[0] == "capture-pane"]
    assert len(captures_between) >= 4
    assert any(c[0] == "run-shell" and c[1] == "-b" for c in calls)

    outcome = [r for r in _receipts(rig) if r["trigger"] == "t0_context_rotation_goal_followup"]
    assert [r["outcome"] for r in outcome] == ["confirmed"]
    started = [r for r in _receipts(rig) if r["trigger"] == "t0_context_rotation_started"]
    assert started[0]["goal_followup"] is True


def test_unconfirmed_goal_retries_once_then_speaks_up(rig):
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_GOAL_NEVER": "1"})
    assert proc.returncode == 0, proc.stderr

    typed = _typed(rig)
    goal_lines = [t for t in typed if t.startswith("/goal ")]
    enters = [c for c in _calls(rig) if c[:1] == ["send-keys"] and c[-1] == "Enter"]
    # the first submit did not show the goal as active: one retry, never more
    assert len(goal_lines) == 2, typed
    assert len(enters) == len(typed)
    assert typed[-1].startswith("LET OP: het automatische /goal-vervolg")
    assert "/goal werk alle open blockers af" in typed[-1]
    assert any(c[0] == "display-message" and "-p" not in c for c in _calls(rig))
    outcome = [r for r in _receipts(rig) if r["trigger"] == "t0_context_rotation_goal_followup"]
    assert [r["outcome"] for r in outcome] == ["unconfirmed"]
    _assert_enter_always_separate(rig)


def test_goal_enter_taken_by_autocomplete_is_retried_with_enter_only(rig):
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_EAT_FIRST_GOAL_ENTER": "1"})
    assert proc.returncode == 0, proc.stderr
    typed = _typed(rig)
    assert len([t for t in typed if t.startswith("/goal ")]) == 1, typed
    calls = [c for c in _calls(rig) if c[:1] == ["send-keys"]]
    goal_idx = next(i for i, c in enumerate(calls) if "-l" in c and c[-1].startswith("/goal "))
    assert calls[goal_idx + 1:] == [["send-keys", "-t", NEW_WIN, "Enter"]] * 2
    outcome = [r for r in _receipts(rig) if r["trigger"] == "t0_context_rotation_goal_followup"]
    assert [r["outcome"] for r in outcome] == ["confirmed"]


# ── handoff parsing ──────────────────────────────────────────────────────────


def test_parser_reads_the_real_handoff_shape(tmp_path):
    path = tmp_path / "h.md"
    path.write_text(HANDOFF_BASE, encoding="utf-8")
    parsed = rotation_state.parse_handoff(path)
    assert parsed.first_step == ("**#1920 mergen**: lees het rapport, controleer rood/groen, "
                                 "glm-gate op de kop.")
    assert parsed.goal is None


@pytest.mark.parametrize("section,directive,remaining", [
    (GOAL_SECTION, "werk alle open blockers af tot de lijst leeg is", ["OI-1850 sluiten", "OI-1851 fixen"]),
    ("\n## Actief /goal\n**Directive**: zet de vloot op 1.6.5\n**Resterend**: SEOcrawler; sales-copilot\n",
     "zet de vloot op 1.6.5", ["SEOcrawler", "sales-copilot"]),
    ("\n## Actief /goal\n/goal ruim de worktrees op\n", "ruim de worktrees op", []),
])
def test_parser_reads_goal_sections(tmp_path, section, directive, remaining):
    path = tmp_path / "h.md"
    path.write_text(HANDOFF_BASE + section, encoding="utf-8")
    goal = rotation_state.parse_handoff(path).goal
    assert goal.directive == directive
    assert goal.remaining == remaining


@pytest.mark.parametrize("section", ["\n## Actief /goal\ngeen\n", "\n## Actief /goal\n\n",
                                     "\n## Actief /goal\nwe waren ergens mee bezig\n"])
def test_parser_treats_an_empty_or_directiveless_goal_as_none(tmp_path, section):
    path = tmp_path / "h.md"
    path.write_text(HANDOFF_BASE + section, encoding="utf-8")
    assert rotation_state.parse_handoff(path).goal is None


def test_goal_text_stays_within_the_limit():
    goal = rotation_state.ActiveGoal(directive="d" * 100, remaining=["t" * 3000, "u" * 2000])
    text = rotation_state.goal_followup_text(goal, "daily-log/handoff.md")
    assert len(text) <= rotation_state.GOAL_MAX_CHARS
    assert text.startswith("/goal " + "d" * 100)
    assert "zie ## Actief /goal in daily-log/handoff.md" in text
    huge = rotation_state.ActiveGoal(directive="d" * 5000)
    text = rotation_state.goal_followup_text(huge, "daily-log/handoff.md")
    assert len(text) <= rotation_state.GOAL_MAX_CHARS
    assert "daily-log/handoff.md" in text


def test_spawn_script_passes_bash_syntax_check():
    assert subprocess.run(["bash", "-n", str(SPAWN)], capture_output=True).returncode == 0


# ── start folder (dispatch 20261004-t0-rotatie-spawner-startmap) ─────────────


def _t0_folder(rig, *, vnx_data: bool = True) -> Path:
    """A healthy T0 folder inside the rig project: CLAUDE.md plus .claude -> <root>/.claude."""
    root = rig["project"].resolve()
    t0 = root / ".claude" / "terminals" / "T0"
    t0.mkdir(parents=True)
    (t0 / "CLAUDE.md").write_text("@role-orchestrator.md\n", encoding="utf-8")
    (t0 / ".claude").symlink_to(root / ".claude")
    if vnx_data:
        (t0 / ".vnx-data").symlink_to(rig["tmp"] / "state")
    return t0


def _new_window_dir(rig) -> str:
    call = next(c for c in _calls(rig) if c[0] == "new-window")
    return call[call.index("-c") + 1]


def _root_start_asserted(rig, proc) -> None:
    assert proc.returncode == 0, proc.stderr
    assert _new_window_dir(rig) == str(rig["project"].resolve())
    assert _typed(rig)[0] == "claude --model opus"
    assert not any(c[0] == "kill-window" for c in _calls(rig))


def test_b1_t0_folder_start_from_the_project_root(rig):
    t0 = _t0_folder(rig)
    _handoff(rig, HANDOFF_BASE)
    root = rig["project"].resolve()
    proc = _spawn(rig, env_extra={"STUB_OLD_PANE_PATH": str(root)})
    assert proc.returncode == 0, proc.stderr
    assert _new_window_dir(rig) == str(t0)
    assert _typed(rig)[0] == f"cd -- {t0} && claude --model opus"
    correction = [ln for ln in proc.stderr.splitlines() if "start folder correction" in ln]
    assert len(correction) == 1 and str(root) in correction[0] and str(t0) in correction[0]
    _assert_enter_always_separate(rig)


def test_b2_started_from_the_t0_folder_reads_the_root_handoff_and_logs_no_correction(rig):
    t0 = _t0_folder(rig)
    root = rig["project"].resolve()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    _handoff(rig, HANDOFF_BASE)
    env = {**rig["env"], "STUB_OLD_PANE_PATH": str(t0)}
    proc = subprocess.run(["bash", str(SPAWN)], env=env, cwd=t0, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert _new_window_dir(rig) == str(t0)
    assert "start folder correction" not in proc.stderr
    assert "#1920 mergen" in _typed(rig)[1]
    assert (root / "daily-log" / "handoff.md").is_file()
    assert not (t0 / "daily-log").exists()


def test_b3_no_t0_claude_md_starts_in_the_project_root(rig):
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig)
    _root_start_asserted(rig, proc)
    lines = [ln for ln in proc.stderr.splitlines() if "no T0 folder" in ln]
    assert len(lines) == 1
    assert "WARNING" not in proc.stderr and "Repair" not in proc.stderr


def test_b4_plain_directory_in_place_of_the_link_warns_with_a_safe_repair(rig):
    t0 = _t0_folder(rig)
    (t0 / ".claude").unlink()
    (t0 / ".claude").mkdir()
    (t0 / ".claude" / "keep.txt").write_text("mine", encoding="utf-8")
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig)
    _root_start_asserted(rig, proc)
    root = rig["project"].resolve()
    warning = next(ln for ln in proc.stderr.splitlines() if "WARNING" in ln)
    assert f"mv {t0}/.claude {t0}/.claude.replaced-" in warning
    assert f"&& ln -s {root}/.claude {t0}/.claude" in warning
    assert "vnx start" not in proc.stderr
    assert (t0 / ".claude").is_dir() and not (t0 / ".claude").is_symlink()
    assert (t0 / ".claude" / "keep.txt").read_text() == "mine"


def test_b5_link_to_another_directory_warns_with_ln_sfn(rig):
    t0 = _t0_folder(rig)
    other = rig["tmp"] / "other"
    other.mkdir()
    (t0 / ".claude").unlink()
    (t0 / ".claude").symlink_to(other)
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig)
    _root_start_asserted(rig, proc)
    root = rig["project"].resolve()
    warning = next(ln for ln in proc.stderr.splitlines() if "WARNING" in ln)
    assert f"ln -sfn {root}/.claude {t0}/.claude" in warning
    assert "vnx start" not in proc.stderr
    assert (t0 / ".claude").is_symlink() and (t0 / ".claude").resolve() == other.resolve()


def test_b6_missing_vnx_data_link_still_starts_in_the_t0_folder(rig):
    t0 = _t0_folder(rig, vnx_data=False)
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig, env_extra={"STUB_OLD_PANE_PATH": str(t0)})
    assert proc.returncode == 0, proc.stderr
    assert _new_window_dir(rig) == str(t0)
    assert len([ln for ln in proc.stderr.splitlines() if ".vnx-data link" in ln]) == 1
    assert "WARNING" not in proc.stderr


@pytest.mark.parametrize("tmux_global, process, expect_t0", [
    ("root", None, False),
    ("unset", "root", False),
    ("removed", "root", False),
    ("root", "t0", False),
    ("t0", "root", True),
    ("unset", None, True),
])
def test_b7_the_switch_prefers_the_tmux_global(rig, tmux_global, process, expect_t0):
    t0 = _t0_folder(rig)
    _handoff(rig, HANDOFF_BASE)
    extra = {"STUB_TMUX_GLOBAL": tmux_global, "STUB_OLD_PANE_PATH": str(t0)}
    if process is not None:
        extra["VNX_T0_ROTATE_START_DIR"] = process
    proc = _spawn(rig, env_extra=extra)
    assert proc.returncode == 0, proc.stderr
    assert _new_window_dir(rig) == (str(t0) if expect_t0 else str(rig["project"].resolve()))
    assert "WARNING" not in proc.stderr


@pytest.mark.parametrize("tmux_global, process", [("bogus", None), ("unset", "bogus"), ("t0", "bogus")])
def test_b7_an_invalid_switch_value_is_refused_before_a_window_opens(rig, tmux_global, process):
    _t0_folder(rig)
    _handoff(rig, HANDOFF_BASE)
    extra = {"STUB_TMUX_GLOBAL": tmux_global}
    if process is not None:
        extra["VNX_T0_ROTATE_START_DIR"] = process
    proc = _spawn(rig, env_extra=extra)
    if (tmux_global, process) == ("t0", "bogus"):
        # a set global wins, so the bad process value is never consulted
        assert proc.returncode == 0, proc.stderr
        return
    assert proc.returncode == 2
    assert "bogus" in proc.stderr and "'t0'" in proc.stderr and "'root'" in proc.stderr
    assert not any(c[0] == "new-window" for c in _calls(rig))


def test_b8_a_successor_in_the_wrong_folder_is_closed_and_nothing_is_typed(rig):
    t0 = _t0_folder(rig)
    _handoff(rig, HANDOFF_BASE)
    wrong = rig["tmp"] / "elsewhere"
    wrong.mkdir()
    proc = _spawn(rig, env_extra={"STUB_NEW_PANE_PATH": str(wrong), "STUB_OLD_PANE_PATH": str(t0)})
    assert proc.returncode == 5
    assert str(wrong.resolve()) in proc.stderr and str(t0) in proc.stderr
    assert ["kill-window", "-t", NEW_WIN] in _calls(rig)
    assert _typed(rig) == [f"cd -- {t0} && claude --model opus"]
    assert not any(c[:2] == ["kill-window", "-t"] and c[2] == OLD_WIN for c in _calls(rig))
    assert not list(rig["state"].glob("latch-*"))
    assert _receipts(rig) == []


def test_b9_the_last_line_keeps_the_four_fields_the_rotate_skill_reads(rig):
    """The skill's own expressions, as written, on a state dir without spaces."""
    _t0_folder(rig)
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig)
    assert proc.returncode == 0, proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    pipeline = ("printf '%s\n' \"$LAST\" | grep -E '^new_window=@[0-9]+ old_window=@[0-9]+' "
                "| sed -E 's/{expr}/\\1/'")
    got = {}
    for key, expr in {"new": ".*new_window=(@[0-9]+).*", "old": ".*old_window=(@[0-9]+).*",
                      "run": ".*run_dir=([^ ]+).*"}.items():
        out = subprocess.run(["bash", "-c", pipeline.format(expr=expr)], capture_output=True,
                             text=True, env={**os.environ, "LAST": last})
        got[key] = out.stdout.strip()
    assert got["new"] == NEW_WIN and got["old"] == OLD_WIN
    assert got["run"].startswith(str(rig["state"])) and " " not in got["run"]
    assert re.search(r" start_dir=\S+$", last)


def test_b9_start_dir_is_the_last_field_and_decodes_with_a_space_in_the_root(rig):
    rig["project"] = rig["tmp"] / "my project"
    (rig["project"] / "daily-log").mkdir(parents=True)
    t0 = _t0_folder(rig)
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig)
    assert proc.returncode == 0, proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    assert last.rsplit(" run_dir=", 1)[1].split(" start_dir=", 1)[0] == str(next(rig["state"].glob("rotation-*")))
    encoded = last.split(" start_dir=", 1)[1]
    decoded = subprocess.run(["bash", "-c", 'eval "printf %s $1"', "_", encoded],
                             capture_output=True, text=True).stdout
    assert decoded == str(t0)
