"""tests/test_t0_rotate_spawn.py — the rotation spawner (dispatch 20260925-t0-context-rotation-enforced).

scripts/t0_rotate_spawn.sh runs for real against a stub ``tmux`` and a stub ``claude`` on PATH.
No real tmux server or claude session is ever touched:

  - stub tmux logs every call (argv) to calls.ndjson and plays a claude screen back on
    ``capture-pane``: a shell before the launch line is submitted, the footer after it, and from
    the delivery on the successor's turns, written into its transcript the way claude 2.1.308
    writes them (a turn ends with a ``system`` / ``turn_duration`` record; a line submitted
    while a turn runs is queued, never a user record). Each capture is one tick of that clock;
    the spinner of a running turn has no ``esc to interrupt`` and the footer stays, as on
    2.1.308. ``/goal active`` shows once a /goal landed (unless told otherwise).
    ``run-shell -b`` executes its command synchronously so the tmux-server follow-up is
    observable in the test.
  - stub claude only records that it was executed; the spawner must never run claude itself,
    it types the launch line into the new pane.

The wrap-proof fixtures (dispatch 20261016b-tg-g5a-wrap-proof) write the handoff frontmatter, the
daily log and the bucket as plain text, without the helper's own symbols, so a run of these tests
on the old spawner fails on what the spawner does and never on a missing name.
"""
from __future__ import annotations

import json
import os
import re
import shlex
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

def submitted(upto=None):
    """Texts followed by a separate Enter keystroke, in order (over the first `upto` calls)."""
    out, pending, eaten = [], None, False
    for c in calls()[:upto]:
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

# ── the successor's turns, as its transcript records them (claude 2.1.308) ──
# The turn of the start argument runs for STUB_TURN_CAPTURES captures and then ends the way
# claude ends one: `end_turn`, `stop_hook_summary`, then `system` / `turn_duration`. A line
# submitted while a turn runs is queued (a queue-operation, never a user record) and stays on
# screen, as in rotation-20261022-170308-53038 and rotation-20261022-191855-76425.
TURN = os.path.join(stub, "turn.json")
SPINNER = {"2.1.308": "✳ Tempering… (52s · ↓ 4.5k tokens · thought for 13s)",
           "legacy": "✻ Working… (esc to interrupt)", "idle": ""}

def load():
    return json.load(open(TURN)) if os.path.exists(TURN) else None

def save(st):
    json.dump(st, open(TURN, "w"))

def append(st, record):
    with open(st["transcript"], "a") as fh:
        fh.write(json.dumps(record) + "\n")

def user(content, origin=None):
    return {"type": "user", "isSidechain": False, "origin": origin,
            "message": {"role": "user", "content": content}}

def start_turn(st, captures):
    st["running"], st["left"] = True, captures

def tick(st):
    """One capture: a running turn does a tool round trip until its captures are used up, then
    ends. True while the screen shows the spinner (STUB_SCREEN_LAG keeps it there longer)."""
    if st["running"] and st["left"] > 0:
        st["left"] -= 1
        append(st, {"type": "assistant", "isSidechain": False,
                    "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use"}]}})
        append(st, user([{"type": "tool_result", "content": "ok"}]))
        return True
    if st["running"]:
        append(st, {"type": "assistant", "isSidechain": False,
                    "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "klaar"}]}})
        append(st, {"type": "system", "subtype": "stop_hook_summary", "isSidechain": False, "hookCount": 1})
        append(st, {"type": "system", "subtype": "turn_duration", "isSidechain": False,
                    "durationMs": 1000, "messageCount": 9})
        st["running"] = False
        st["ends"].append(len(calls()) - 1)
        st["lag"] = int(os.environ.get("STUB_SCREEN_LAG", "0"))
    if st["lag"] > 0:
        st["lag"] -= 1
        return True
    return False

def submit(st, line):
    """A line whose Enter landed. STUB_TURN_BEFORE_GOAL=1,2 starts a turn (a task notification)
    the moment before the first / second /goal line lands."""
    if line.startswith("/goal "):
        st["goals"] += 1
        if str(st["goals"]) in os.environ.get("STUB_TURN_BEFORE_GOAL", "").split(","):
            append(st, user("<task-notification>\n<summary>bouwer klaar</summary>\n</task-notification>",
                            {"kind": "task-notification"}))
            start_turn(st, int(os.environ.get("STUB_BUSY_CAPTURES", "3")))
    if st["running"]:
        append(st, {"type": "queue-operation", "operation": "enqueue",
                    "content": '<pasted_content id="c632">\n' + line})
        st["queued"].append(line)
    elif not line.startswith("/goal "):
        append(st, user(line, {"kind": "human"}))
        start_turn(st, 0)
    elif os.environ.get("STUB_GOAL_SCREEN_ONLY"):
        st["goal_on_screen"] = True
    elif not os.environ.get("STUB_GOAL_NEVER"):
        append(st, user("<command-name>/goal</command-name>\n            <command-message>goal"
                        "</command-message>\n            <command-args>%s</command-args>" % line[6:]))
        st["goal_on_screen"] = True
        start_turn(st, 1)

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
    # What the ledger held the moment the first window opened: ledger first (ADR-005).
    ledger = os.environ.get("VNX_T0_ROTATION_RECEIPTS_FILE", "")
    held = open(ledger).read() if ledger and os.path.isfile(ledger) else ""
    open(os.path.join(stub, "ledger_at_new_window"), "w").write(held)
    print("@7")
elif cmd == "send-keys" and args[-1] == "Enter" and "-l" not in args:
    st = load()
    before, _ = submitted(-1)
    after, _ = submitted()
    if st and len(after) > len(before):
        submit(st, after[-1])
        save(st)
elif cmd == "capture-pane":
    subs, typed = submitted()
    screen = "vincent@mac project % "
    # A T0-folder start first types a plain `cd -- <folder>`; the launch line is a later one.
    launched = any(s and s.startswith(("claude ", "CLAUDE_CONFIG_DIR=")) for s in subs)
    if launched and not os.environ.get("STUB_NO_FOOTER"):
        st = load()
        busy = tick(st) if st else False
        if st:
            save(st)
        spinner = SPINNER[os.environ.get("STUB_SCREEN", "2.1.308")]
        lines = ["> " + q for q in (st["queued"] if st else [])]
        if busy and spinner:
            lines += [spinner, ""]
        lines.append("> " + (typed or ""))
        footer = "  ⏵⏵ auto mode on (shift+tab to cycle)"
        if st and st["goal_on_screen"]:
            footer += "  · /goal active"
        screen = "\n".join(lines + [footer])
    print(screen)
elif cmd == "list-windows":
    print("@1" if os.environ.get("STUB_NEW_WINDOW_GONE") else "@1\n@7")
elif cmd == "run-shell":
    # STUB_TRANSCRIPT=expanded|plain: the successor "received" its start argument, so the delivery
    # job finds a transcript <config dir>/projects/-p/<session uuid>.jsonl, written in the form
    # claude would store it (the slash command expanded, or the plain text).
    mode = os.environ.get("STUB_TRANSCRIPT")
    if mode:
        import shlex
        subs, _ = submitted()
        words = shlex.split(next(s for s in subs if s and "--session-id" in s))
        uuid, message = words[words.index("--session-id") + 1], words[-1]
        content = message
        if mode == "expanded":
            name, _, rest = message.partition(" ")
            content = ("<command-message>%s</command-message>\n<command-name>%s</command-name>\n"
                       "<command-args>%s</command-args>" % (name[1:], name, rest))
        config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ["HOME"], ".claude")
        folder = os.path.join(config, "projects", "-p")
        os.makedirs(folder, exist_ok=True)
        record = {"type": "user", "isSidechain": False, "message": {"role": "user", "content": content}}
        path = os.path.join(folder, uuid + ".jsonl")
        open(path, "w").write(json.dumps(record) + "\n")
        # The kickoff turn starts with the delivery.
        save({"transcript": path, "running": True, "left": int(os.environ.get("STUB_TURN_CAPTURES", "2")),
              "ends": [], "lag": 0, "goals": 0, "queued": [], "goal_on_screen": False})
    # What the spawner had printed on stdout by the time the server job was handed over.
    if os.environ.get("STUB_STDOUT_FILE"):
        open(os.path.join(stub, "stdout_at_run_shell"), "w").write(open(os.environ["STUB_STDOUT_FILE"]).read())
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
        "VNX_T0_ROTATE_DELIVERY_TIMEOUT": "0",
        "VNX_T0_ROTATE_DELIVERY_POLL": "0",
    }
    return {"tmp": tmp_path, "stub": stub, "project": project, "state": state, "env": env}


DAILY_LOG = "daily-log/2026-10-16.md"
SLUG = "vnx-orchestration"
TRANSCRIPT_LINES = [
    {"type": "user", "message": {"role": "user", "content": "rotate please"}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "on it"}]}},
]


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _wrap_frontmatter(rig, *, started: float, wrapped: float, headings=(), nuggets=None,
                      daily_log: str = DAILY_LOG, bucket: str = None) -> str:
    """The frontmatter `wrap-finish` writes, as text, and the start record it consumed: the
    block names the record's start_id, and the record's consumed_at is the block's wrapped_at."""
    count = len(headings) if nuggets is None else nuggets
    start_id = "5eed" + format(int(started), "x")[-12:]
    rig["state"].mkdir(parents=True, exist_ok=True)
    (rig["state"] / "wrap-start.json").write_text(json.dumps({
        "start_id": start_id, "started_at": _iso(started), "project_root": str(rig["project"]),
        "project_slug": SLUG, "bucket": bucket, "headings": [], "consumed_at": _iso(wrapped),
    }), encoding="utf-8")
    return (
        "---\ncontext: dev\nproject: vnx-orchestration\nwrap:\n"
        f"  start_id: {json.dumps(start_id)}\n"
        f"  started_at: {_iso(started)}\n"
        f"  wrapped_at: {_iso(wrapped)}\n"
        f"  nuggets: {count}\n"
        f"  nugget_headings: {json.dumps(list(headings), ensure_ascii=False)}\n"
        f"  daily_log: {json.dumps(daily_log)}\n"
        f"  bucket: {json.dumps(bucket) if bucket else 'null'}\n"
        "---\n\n"
    )


def _daily_log(rig, mtime: float, *, flag: str = "true") -> Path:
    path = rig["project"] / DAILY_LOG
    path.write_text(f"---\ndate: 2026-10-16\nhandoff: {flag}\n---\n\n# Daily log\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _bucket(rig, headings) -> Path:
    path = rig["tmp"] / "bucket" / "2026-10.md"
    path.parent.mkdir(exist_ok=True)
    body = "".join(f"{h}\n- **wat:** iets\n\n" for h in headings)
    path.write_text(f"# Build-Log Bucket — 2026-10\n\n---\n\n{body}", encoding="utf-8")
    return path


def _transcript(rig, lines=None) -> Path:
    path = rig["tmp"] / "transcripts" / "predecessor.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in (lines or TRANSCRIPT_LINES)), encoding="utf-8")
    return path


def _handoff(rig, text: str, age_seconds: int = 0, *, wrap: bool = True) -> Path:
    """The handoff. With ``wrap`` it carries the proof of a five-minute wrap that finished
    ``age_seconds`` ago, with its daily log written inside the wrap's window."""
    path = rig["project"] / "daily-log" / "handoff.md"
    if wrap:
        wrapped = time.time() - age_seconds
        _daily_log(rig, wrapped - 60)
        text = _wrap_frontmatter(rig, started=wrapped - 300, wrapped=wrapped) + text
    path.write_text(text, encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def _spawn(rig, *args: str, env_extra: Dict[str, str] = None,
           cwd: Path = None) -> subprocess.CompletedProcess:
    env = {**rig["env"], **(env_extra or {})}
    return subprocess.run(["bash", str(SPAWN), "--project-root", str(rig["project"]), *args],
                          env=env, cwd=cwd or rig["project"], capture_output=True, text=True,
                          timeout=120)


def _calls(rig) -> List[list]:
    path = rig["stub"] / "calls.ndjson"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _typed(rig) -> List[str]:
    return [c[-1] for c in _calls(rig) if c[:1] == ["send-keys"] and "-l" in c]


LAUNCH_TAIL_RE = re.compile(r" --session-id ([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}) "
                            r"/kickoff\\ rotatie\\ (rotation-\S+)$")


def _launch_line(rig) -> str:
    """The typed line that starts claude: the one carrying --session-id."""
    return next(t for t in _typed(rig) if "--session-id" in t)


def _launch(rig) -> str:
    """The typed launch line without its start arguments: what is left is the old launch line."""
    return LAUNCH_TAIL_RE.sub("", _launch_line(rig))


def _run_dir(rig) -> Path:
    return next(rig["state"].glob("rotation-*"))


def _prompt(rig) -> str:
    """The full instruction the kickoff skill reads. It is no longer typed anywhere."""
    return (_run_dir(rig) / "prompt.txt").read_text(encoding="utf-8")


def _only(receipts: list, trigger: str) -> list:
    return [r for r in receipts if r.get("trigger") == trigger]


def _receipts(rig) -> list:
    path = Path(rig["env"]["VNX_T0_ROTATION_RECEIPTS_FILE"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _assert_nothing_opened(rig) -> None:
    calls = _calls(rig)
    assert not any(c[0] == "new-window" for c in calls), calls
    assert _typed(rig) == []
    assert not list(rig["state"].glob("latch-*"))


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


def _turn_ends(rig) -> List[int]:
    """The calls.ndjson index of each capture at which a turn of the successor ended in its
    transcript (the stub's clock)."""
    path = rig["stub"] / "turn.json"
    return json.loads(path.read_text())["ends"] if path.exists() else []


def _typed_at(rig, prefix: str) -> List[int]:
    """The calls.ndjson index of each line typed with ``send-keys -l`` that starts with prefix."""
    return [i for i, c in enumerate(_calls(rig))
            if c[:1] == ["send-keys"] and "-l" in c and c[-1].startswith(prefix)]


def _successor_records(rig) -> List[dict]:
    path = Path(json.loads((rig["stub"] / "turn.json").read_text())["transcript"])
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _goal_outcomes(rig) -> List[dict]:
    return _only(_receipts(rig), "t0_context_rotation_goal_followup")


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
    assert len(_typed(rig)) == 1 and _launch(rig) == "claude --model opus"
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
    assert len(typed) == 1, typed
    assert _launch(rig) == "claude --model opus"
    prompt = _prompt(rig)
    assert "tmux kill-window" not in prompt
    assert "kickoff-skill" in prompt
    assert ("Pak daarna stap 1 op: **#1920 mergen**: lees het rapport, controleer rood/groen, "
            "glm-gate op de kop.") in prompt
    assert "\n" not in prompt
    assert "/goal" not in prompt
    _assert_enter_always_separate(rig)
    assert not (rig["stub"] / "claude_invoked").exists()

    assert rotation_state.is_latched(rig["state"], session_id=SESSION, pane="")
    [receipt] = _only(_receipts(rig), "t0_context_rotation_started")
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
    assert _launch(rig) == ("CLAUDE_CONFIG_DIR=/Users/x/.claude-salesminds "
                              "claude --model opus --remote-control VNX\\ ORCH")


def test_no_rc_overrides_the_bridge(rig):
    _handoff(rig, HANDOFF_BASE)
    assert _spawn(rig, "--no-rc", env_extra={"CLAUDE_CODE_BRIDGE_SESSION_ID": "b"}).returncode == 0
    assert _launch(rig) == "claude --model opus"


def test_active_goal_is_resumed_after_the_first_turn(rig):
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_TRANSCRIPT": "expanded"})
    assert proc.returncode == 0, proc.stderr

    typed = _typed(rig)
    assert "start zelf geen nieuwe /goal" in _prompt(rig)
    assert typed[1] == ("/goal werk alle open blockers af tot de lijst leeg is, hervat na "
                        "context-rotatie; resterend: OI-1850 sluiten; OI-1851 fixen")
    assert len(typed) == 2, typed
    _assert_enter_always_separate(rig)

    calls = _calls(rig)
    # the kickoff turn ended in the successor's transcript before the /goal was typed
    [goal_typed] = _typed_at(rig, "/goal ")
    assert _turn_ends(rig) and _turn_ends(rig)[0] < goal_typed
    assert any(c[0] == "run-shell" and c[1] == "-b" for c in calls)

    outcome = [r for r in _receipts(rig) if r["trigger"] == "t0_context_rotation_goal_followup"]
    assert [r["outcome"] for r in outcome] == ["confirmed"]
    started = [r for r in _receipts(rig) if r["trigger"] == "t0_context_rotation_started"]
    assert started[0]["goal_followup"] is True


def test_unconfirmed_goal_retries_once_then_speaks_up(rig):
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_GOAL_NEVER": "1", "STUB_TRANSCRIPT": "expanded"})
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
    proc = _spawn(rig, env_extra={"STUB_EAT_FIRST_GOAL_ENTER": "1", "STUB_TRANSCRIPT": "expanded"})
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
    assert _launch(rig) == "claude --model opus"
    assert not any(c[0] == "kill-window" for c in _calls(rig))


def test_b1_t0_folder_start_from_the_project_root(rig):
    t0 = _t0_folder(rig)
    _handoff(rig, HANDOFF_BASE)
    root = rig["project"].resolve()
    proc = _spawn(rig, env_extra={"STUB_OLD_PANE_PATH": str(root)})
    assert proc.returncode == 0, proc.stderr
    assert _new_window_dir(rig) == str(t0)
    assert _typed(rig)[0] == f"cd -- {t0}"
    assert _launch(rig) == "claude --model opus"
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
    assert "#1920 mergen" in _prompt(rig)
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
    assert _typed(rig) == [f"cd -- {t0}"], "nothing but the plain cd ran in the wrong folder"
    assert not any("claude --model" in t or "/kickoff" in t for t in _typed(rig))
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
    assert (last.rsplit(" run_dir=", 1)[1].split(" close=", 1)[0]
            == str(next(rig["state"].glob("rotation-*"))))
    assert " close=on_delivery_confirmed start_dir=" in last
    encoded = last.split(" start_dir=", 1)[1]
    decoded = subprocess.run(["bash", "-c", 'eval "printf %s $1"', "_", encoded],
                             capture_output=True, text=True).stdout
    assert decoded == str(t0)


# ── wrap proof (operator decisions 2026-10-16 a, b; dispatch 20261016b-tg-g5a-wrap-proof) ──


def _refusal(proc) -> str:
    return [ln for ln in proc.stderr.splitlines() if "REFUSED" in ln][-1]


def test_a_handoff_without_a_wrap_block_is_refused_before_any_window(rig):
    _handoff(rig, HANDOFF_BASE, wrap=False)
    proc = _spawn(rig)
    assert proc.returncode == 6, proc.stderr
    assert "'wrap_block'" in proc.stderr
    assert _refusal(proc).endswith("run /build-log wrap")
    assert "--skip-wrap-reason" in _refusal(proc)
    _assert_nothing_opened(rig)
    assert _receipts(rig) == []


def test_a_wrap_that_finished_two_hours_ago_is_refused_although_the_file_is_fresh(rig):
    now = time.time()
    _daily_log(rig, now - 2 * 3600 - 120)
    _handoff(rig, _wrap_frontmatter(rig, started=now - 2 * 3600 - 300, wrapped=now - 2 * 3600)
             + HANDOFF_BASE, wrap=False)
    proc = _spawn(rig)
    assert proc.returncode == 6, proc.stderr
    assert "'wrapped_at_age'" in proc.stderr and "120 min ago" in proc.stderr
    assert _refusal(proc).endswith("run /build-log wrap")
    _assert_nothing_opened(rig)


def test_a_daily_log_written_before_the_wrap_started_is_refused(rig):
    now = time.time()
    _daily_log(rig, now - 3 * 3600)
    _handoff(rig, _wrap_frontmatter(rig, started=now - 300, wrapped=now - 10) + HANDOFF_BASE, wrap=False)
    proc = _spawn(rig)
    assert proc.returncode == 6, proc.stderr
    assert "'daily_log_window'" in proc.stderr
    _assert_nothing_opened(rig)


@pytest.mark.parametrize("case", ["count", "missing_heading"])
def test_nuggets_that_do_not_match_the_bucket_are_refused(rig, case):
    now = time.time()
    present = [f"### 2026-10-16 · {SLUG} · gate-loopt-vast", f"### 2026-10-16 · {SLUG} · ledger-eerst"]
    bucket = _bucket(rig, present)
    _daily_log(rig, now - 60)
    if case == "count":
        front = _wrap_frontmatter(rig, started=now - 300, wrapped=now - 10, headings=present, nuggets=3,
                                  bucket=str(bucket))
        check = "'nuggets_count'"
    else:
        front = _wrap_frontmatter(rig, started=now - 300, wrapped=now - 10, bucket=str(bucket),
                                  headings=present[:1] + [f"### 2026-10-16 · {SLUG} · nooit-geschreven"])
        check = "'nugget_in_bucket'"
    _handoff(rig, front + HANDOFF_BASE, wrap=False)
    proc = _spawn(rig)
    assert proc.returncode == 6, proc.stderr
    assert check in proc.stderr
    _assert_nothing_opened(rig)


def test_a_valid_wrap_with_nuggets_in_the_bucket_spawns(rig):
    now = time.time()
    headings = [f"### 2026-10-16 · {SLUG} · gate-loopt-vast"]
    bucket = _bucket(rig, headings + ["### 2026-10-16 · seocrawler-v2 · iets-anders"])
    _daily_log(rig, now - 60)
    _handoff(rig, _wrap_frontmatter(rig, started=now - 300, wrapped=now - 10, headings=headings,
                                    bucket=str(bucket)) + HANDOFF_BASE, wrap=False)
    proc = _spawn(rig)
    assert proc.returncode == 0, proc.stderr
    assert "#1920 mergen" in _prompt(rig)


def test_a_wrap_that_took_twenty_minutes_spawns(rig):
    """The wrap's order is nuggets, handoff, daily log, curation, and the curation waits for the
    operator. A wrap of twenty minutes is a normal wrap, not a stale one."""
    now = time.time()
    _daily_log(rig, now - 5 * 60)
    _handoff(rig, _wrap_frontmatter(rig, started=now - 20 * 60 - 30, wrapped=now - 30) + HANDOFF_BASE,
             wrap=False)
    proc = _spawn(rig)
    assert proc.returncode == 0, proc.stderr


def _state_cli(rig, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "lib" / "t0_rotation_state.py"),
                           *argv], capture_output=True, text=True, env=rig["env"], timeout=60)


def test_a_wrap_through_wrap_start_and_wrap_finish_spawns(rig):
    """The whole chain as the build-log skill runs it, over an old wrap: block with a blank line
    and an unindented comment in it (OI-2116), then the spawner."""
    old_block = ("wrap:\n  started_at: 2026-10-01T08:00:00Z\n\n# oud\n  nuggets: 0\n"
                 "  daily_log: \"daily-log/2026-10-01.md\"\n")
    path = rig["project"] / "daily-log" / "handoff.md"
    path.write_text(f"---\ncontext: dev\n{old_block}branch: main\n---\n{HANDOFF_BASE}", encoding="utf-8")
    start = _state_cli(rig, "wrap-start", "--project-root", str(rig["project"]), "--project-slug", SLUG)
    assert start.returncode == 0, start.stderr
    _daily_log(rig, time.time())
    finish = _state_cli(rig, "wrap-finish", "--project-root", str(rig["project"]), "--daily-log", DAILY_LOG)
    assert finish.returncode == 0, finish.stderr
    assert "2026-10-01" not in path.read_text(encoding="utf-8")

    proc = _spawn(rig)
    assert proc.returncode == 0, proc.stderr
    assert "#1920 mergen" in _prompt(rig)


@pytest.mark.parametrize("case, check", [
    ("unconsumed", "'wrap_start_binding'"),
    ("newer_start", "'wrap_start_binding'"),
    ("frontmatter_does_not_parse", "'wrap_block'"),
])
def test_a_proof_that_does_not_bind_or_parse_is_refused_before_any_window(rig, case, check):
    now = time.time()
    _daily_log(rig, now - 60)
    front = _wrap_frontmatter(rig, started=now - 300, wrapped=now - 10)
    record = rig["state"] / "wrap-start.json"
    if case == "unconsumed":
        data = json.loads(record.read_text(encoding="utf-8"))
        del data["consumed_at"]
        record.write_text(json.dumps(data), encoding="utf-8")
    elif case == "newer_start":
        assert _state_cli(rig, "wrap-start", "--project-root", str(rig["project"]),
                          "--project-slug", SLUG).returncode == 0
    else:
        front = front.replace("context: dev\n", "context: dev\n  verdwaald: 1\n", 1)
    _handoff(rig, front + HANDOFF_BASE, wrap=False)
    proc = _spawn(rig)
    assert proc.returncode == 6, proc.stderr
    assert check in proc.stderr
    _assert_nothing_opened(rig)


# ── emergency path (operator decision 2026-10-16 c) ──────────────────────────


def _skipped(rig) -> list:
    return [r for r in _receipts(rig) if r.get("trigger") == "t0_context_rotation_wrap_skipped"]


def test_skip_wrap_reason_spawns_with_a_receipt_and_the_catch_up_first(rig):
    transcript = _transcript(rig)
    _handoff(rig, HANDOFF_BASE, age_seconds=2 * 3600, wrap=False)
    proc = _spawn(rig, "--skip-wrap-reason", "context op 610K, geen ruimte voor een wrap",
                  "--transcript", str(transcript))
    assert proc.returncode == 0, proc.stderr

    [receipt] = _skipped(rig)
    assert receipt["reason"] == "context op 610K, geen ruimte voor een wrap"
    assert receipt["transcript"] == str(transcript)
    assert receipt["old_window"] == OLD_WIN
    run_dir = next(rig["state"].glob("rotation-*"))
    assert receipt["run_dir"] == str(run_dir)
    held = (rig["stub"] / "ledger_at_new_window").read_text()
    assert "t0_context_rotation_wrap_skipped" in held, "the window opened before the ledger held the skip"

    prompt = _prompt(rig)
    assert prompt.startswith("Eerst een inhaal-wrap")
    assert "kill-window" not in prompt
    assert f"/build-log wrap --from-transcript {transcript}" in prompt
    assert f"transcript-digest --transcript {transcript}" in prompt
    assert "Pak daarna stap 1 op: **#1920 mergen**" in prompt
    assert (run_dir / "prompt.txt").read_text(encoding="utf-8") == prompt
    assert [r["trigger"] for r in _receipts(rig)] == ["t0_context_rotation_wrap_skipped",
                                                      "t0_context_rotation_started",
                                                      "t0_context_rotation_delivery"]


def test_skip_takes_the_transcript_from_the_pending_marker(rig):
    transcript = _transcript(rig)
    rotation_state.write_pending(rig["state"], pane=PANE, session_id=SESSION, tokens=612_000,
                                 level="hard", transcript_path=str(transcript))
    _handoff(rig, HANDOFF_BASE, wrap=False)
    proc = _spawn(rig, "--skip-wrap-reason", "noodrem")
    assert proc.returncode == 0, proc.stderr
    assert str(transcript) in _prompt(rig)
    [receipt] = _skipped(rig)
    assert receipt["transcript"] == str(transcript) and receipt["session_id"] == SESSION


def test_a_skip_without_a_transcript_refuses_before_any_window(rig):
    _handoff(rig, HANDOFF_BASE, wrap=False)
    proc = _spawn(rig, "--skip-wrap-reason", "noodrem")
    assert proc.returncode == 6, proc.stderr
    assert "transcript" in _refusal(proc)
    _assert_nothing_opened(rig)
    assert _receipts(rig) == []


def test_a_pending_marker_of_another_session_gives_no_transcript(rig):
    transcript = _transcript(rig)
    rotation_state.write_pending(rig["state"], pane=PANE, session_id="sess-older", tokens=612_000,
                                 level="hard", transcript_path=str(transcript))
    _handoff(rig, HANDOFF_BASE, wrap=False)
    proc = _spawn(rig, "--skip-wrap-reason", "noodrem", "--session-id", SESSION)
    assert proc.returncode == 6, proc.stderr
    _assert_nothing_opened(rig)


@pytest.mark.parametrize("source", ["explicit", "marker"])
@pytest.mark.parametrize("content", ["# not a transcript\n", "", "[1, 2]\n"])
def test_a_transcript_that_is_not_jsonl_refuses_before_any_window(rig, source, content):
    bad = rig["tmp"] / "bad.jsonl"
    bad.write_text(content, encoding="utf-8")
    args = ["--skip-wrap-reason", "noodrem"]
    if source == "explicit":
        args += ["--transcript", str(bad)]
    else:
        rotation_state.write_pending(rig["state"], pane=PANE, session_id=SESSION, tokens=612_000,
                                     level="hard", transcript_path=str(bad))
    _handoff(rig, HANDOFF_BASE, wrap=False)
    proc = _spawn(rig, *args)
    assert proc.returncode == 6, proc.stderr
    assert "readable JSONL" in proc.stderr
    _assert_nothing_opened(rig)
    assert _skipped(rig) == []


_DIGEST_RE = re.compile(r"via de digest: (python3 \S+ transcript-digest --transcript \S+?)\. Je bent")


def test_a_relative_transcript_is_absolute_in_receipt_prompt_and_digest(rig):
    """codex_gate on 3c2bdd24: `--transcript predecessor.jsonl` passed the check from the caller's
    folder, then the successor, started in the T0 folder, could not find the file."""
    successor_dir = _t0_folder(rig)
    transcript = _transcript(rig)
    caller_dir = transcript.parent
    absolute = os.path.join(os.path.realpath(caller_dir), transcript.name)
    _handoff(rig, HANDOFF_BASE, wrap=False)
    proc = _spawn(rig, "--skip-wrap-reason", "noodrem", "--transcript", transcript.name, cwd=caller_dir)
    assert proc.returncode == 0, proc.stderr
    assert _new_window_dir(rig) == str(successor_dir)

    [receipt] = _skipped(rig)
    assert receipt["transcript"] == absolute
    prompt = _prompt(rig)
    assert f"/build-log wrap --from-transcript {absolute}" in prompt
    digest = _DIGEST_RE.search(prompt)
    assert digest, prompt
    argv = shlex.split(digest.group(1))
    assert argv[-1] == absolute
    read = subprocess.run([sys.executable, *argv[1:]], cwd=successor_dir, env=rig["env"],
                          capture_output=True, text=True, timeout=60)
    assert read.returncode == 0, read.stderr
    assert "rotate please" in read.stdout and "on it" in read.stdout


def test_a_relative_transcript_from_the_pending_marker_is_made_absolute(rig):
    transcript = _transcript(rig)
    rotation_state.write_pending(rig["state"], pane=PANE, session_id=SESSION, tokens=612_000,
                                 level="hard", transcript_path=transcript.name)
    _handoff(rig, HANDOFF_BASE, wrap=False)
    proc = _spawn(rig, "--skip-wrap-reason", "noodrem", cwd=transcript.parent)
    assert proc.returncode == 0, proc.stderr
    absolute = os.path.join(os.path.realpath(transcript.parent), transcript.name)
    [receipt] = _skipped(rig)
    assert receipt["transcript"] == absolute
    assert f"transcript-digest --transcript {absolute}" in _prompt(rig)


def test_a_transcript_that_cannot_be_resolved_refuses_before_any_window(rig):
    """A relative path from a folder that is gone names no file anyone can find again."""
    _transcript(rig)
    _handoff(rig, HANDOFF_BASE, wrap=False)
    gone = rig["tmp"] / "gone"
    gone.mkdir()
    proc = subprocess.run(
        ["bash", "-c", 'cd "$1" && rmdir "$1" && shift && exec bash "$@"', "_", str(gone),
         str(SPAWN), "--project-root", str(rig["project"]), "--skip-wrap-reason", "noodrem",
         "--transcript", "predecessor.jsonl"],
        env=rig["env"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 6, proc.stderr
    assert "cannot resolve" in _refusal(proc)
    _assert_nothing_opened(rig)
    assert _receipts(rig) == []


def test_a_refusing_ledger_refuses_the_skip_spawn(rig):
    transcript = _transcript(rig)
    blocker = rig["tmp"] / "not-a-dir"
    blocker.write_text("a file where the ledger's directory should be", encoding="utf-8")
    _handoff(rig, HANDOFF_BASE, wrap=False)
    proc = _spawn(rig, "--skip-wrap-reason", "noodrem", "--transcript", str(transcript),
                  env_extra={"VNX_T0_ROTATION_RECEIPTS_FILE": str(blocker / "t0_receipts.ndjson")})
    assert proc.returncode == 6, proc.stderr
    assert "ledger" in _refusal(proc)
    _assert_nothing_opened(rig)


@pytest.mark.parametrize("reason", ["", "   "])
def test_an_empty_skip_reason_is_a_usage_error(rig, reason):
    _handoff(rig, HANDOFF_BASE, wrap=False)
    proc = _spawn(rig, "--skip-wrap-reason", reason, "--transcript", str(_transcript(rig)))
    assert proc.returncode == 2, proc.stderr
    _assert_nothing_opened(rig)
    assert _receipts(rig) == []


def test_a_transcript_without_a_skip_is_a_usage_error(rig):
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig, "--transcript", str(_transcript(rig)))
    assert proc.returncode == 2, proc.stderr
    _assert_nothing_opened(rig)


def test_a_skip_never_skips_the_first_step_check(rig):
    _handoff(rig, "# Handoff\n\n## State\nniets\n", wrap=False)
    proc = _spawn(rig, "--skip-wrap-reason", "noodrem", "--transcript", str(_transcript(rig)))
    assert proc.returncode == 4, proc.stderr
    _assert_nothing_opened(rig)
    assert _skipped(rig) == []


def test_skips_lists_the_receipt_the_spawner_booked(rig):
    _handoff(rig, HANDOFF_BASE, wrap=False)
    assert _spawn(rig, "--skip-wrap-reason", "noodrem op 610K",
                  "--transcript", str(_transcript(rig))).returncode == 0
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "lib" / "t0_rotation_state.py"),
                           "skips", "--since", "7"], env=rig["env"], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert 'reason="noodrem op 610K"' in lines[0]
    assert lines[-1].startswith("1 wrap skip(s) in the last 7 day(s)")


# ── the first message is a start argument (dispatch 20261021-rot-prompt-als-argument, OI-2102) ──

UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def _launch_parts(rig):
    match = LAUNCH_TAIL_RE.search(_launch_line(rig))
    assert match, _typed(rig)
    return match.group(1), match.group(2)


def test_the_first_message_is_a_start_argument_and_nothing_is_typed_after_the_footer(rig):
    """Red run on origin/main: the prompt was typed with `send-keys -l` into the TUI input box
    after the footer, and the launch line carried neither --session-id nor the first message."""
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig)
    assert proc.returncode == 0, proc.stderr

    session_uuid, run_id = _launch_parts(rig)
    assert UUID_RE.match(session_uuid)
    assert run_id == _run_dir(rig).name
    assert shlex.split(_launch_line(rig))[-3:] == ["--session-id", session_uuid, f"/kickoff rotatie {run_id}"]
    assert _typed(rig) == [_launch_line(rig)], "something was typed into the TUI after the footer"
    assert not any("Je bent de verse T0" in t for t in _typed(rig))
    _assert_enter_always_separate(rig)


def test_the_launch_line_keeps_config_dir_remote_control_and_cd_prefix(rig):
    t0 = _t0_folder(rig)
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig, env_extra={"CLAUDE_CODE_BRIDGE_SESSION_ID": "bridge-1",
                                  "CLAUDE_CONFIG_DIR": "/Users/x/.claude-salesminds"})
    assert proc.returncode == 0, proc.stderr
    assert _typed(rig)[0] == f"cd -- {t0}", "the folder step is its own plain line"
    assert _launch(rig) == ("CLAUDE_CONFIG_DIR=/Users/x/.claude-salesminds "
                            "claude --model opus --remote-control VNX\\ ORCH")


def test_a_goal_run_types_only_the_goal_after_the_launch(rig):
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    assert _spawn(rig, env_extra={"STUB_TRANSCRIPT": "expanded"}).returncode == 0
    typed = _typed(rig)
    assert len(typed) == 2 and typed[1].startswith("/goal ")


def test_prompt_txt_keeps_the_goal_sentence_and_the_catch_up_head_without_the_kill_sentence(rig):
    transcript = _transcript(rig)
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION, wrap=False)
    assert _spawn(rig, "--skip-wrap-reason", "noodrem", "--transcript", str(transcript)).returncode == 0
    prompt = _prompt(rig)
    assert prompt.startswith("Eerst een inhaal-wrap")
    assert "start zelf geen nieuwe /goal" in prompt
    assert "kill-window" not in prompt
    assert json.loads((_run_dir(rig) / "continuation.json").read_text())["goal"].startswith("/goal werk alle")


def test_the_delivery_check_is_a_script_file_run_by_the_tmux_server(rig):
    _handoff(rig, HANDOFF_BASE)
    assert _spawn(rig).returncode == 0
    jobs = [c for c in _calls(rig) if c[0] == "run-shell"]
    [job] = jobs
    job_file = _run_dir(rig) / "delivery_job.sh"
    assert job[1] == "-b" and str(job_file) in job[-1]
    assert "#{" not in job[-1], "run-shell expands #{...} formats in an inline command"
    body = job_file.read_text(encoding="utf-8")
    session_uuid, run_id = _launch_parts(rig)
    assert session_uuid in body and "t0_rotation_delivery_job.py" in body and f"/kickoff rotatie {run_id}" in body.replace("\\", "")
    assert subprocess.run(["bash", "-n", str(job_file)]).returncode == 0


def test_the_config_dir_of_the_transcript_search_follows_claude_config_dir(rig):
    _handoff(rig, HANDOFF_BASE)
    assert _spawn(rig, env_extra={"CLAUDE_CONFIG_DIR": "/Users/x/.claude-salesminds"}).returncode == 0
    body = (_run_dir(rig) / "delivery_job.sh").read_text(encoding="utf-8")
    assert "--config-dir /Users/x/.claude-salesminds" in body


def test_without_a_transcript_in_time_the_old_window_stays_open_and_both_windows_say_so(rig):
    _handoff(rig, HANDOFF_BASE)
    assert _spawn(rig).returncode == 0
    calls = _calls(rig)
    assert not any(c[0] == "kill-window" for c in calls)
    notes = [c for c in calls if c[0] == "display-message" and "-p" not in c]
    assert sorted(c[c.index("-t") + 1] for c in notes) == sorted([NEW_WIN, OLD_WIN])
    delivery = json.loads((_run_dir(rig) / "delivery.json").read_text(encoding="utf-8"))
    assert delivery["outcome"] == "timeout" and delivery["transcript"] is None
    [receipt] = _only(_receipts(rig), "t0_context_rotation_delivery")
    assert receipt["outcome"] == "timeout"


# ── the delivery job against a fake transcript ───────────────────────────────

DELIVERY = REPO_ROOT / "scripts" / "t0_rotation_delivery_job.py"
JOB_UUID = "0f1e2d3c-4b5a-4697-8877-665544332211"
JOB_RUN_ID = "rotation-20261021-063814-84445"
KICKOFF_RECORD = {
    "type": "user", "isSidechain": False, "origin": {"kind": "human"}, "userType": "external",
    "message": {"role": "user", "content": (
        "<command-message>kickoff</command-message>\n<command-name>/kickoff</command-name>\n"
        f"<command-args>rotatie {JOB_RUN_ID}</command-args>")},
}
NOISE = [
    {"type": "mode", "mode": "normal"},
    {"type": "file-history-snapshot", "snapshot": {}},
    {"type": "queue-operation", "operation": "enqueue", "content": "/kickoff"},
    {"type": "user", "isMeta": True, "message": {"role": "user",
     "content": "<local-command-caveat>Caveat: generated while running local commands.</local-command-caveat>"}},
    {"type": "user", "message": {"role": "user",
     "content": "<local-command-stdout></local-command-stdout>"}},
    {"type": "user", "message": {"role": "user", "content": "<system-reminder>SessionStart hook</system-reminder>"}},
    {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},
]


def _job(rig, records, *, extra_env: Dict[str, str] = None, timeout: str = "0", extra_args=()):
    config = rig["tmp"] / "config"
    project_dir = config / "projects" / "-some-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    if records is not None:
        (project_dir / f"{JOB_UUID}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    run_dir = rig["tmp"] / "run" / JOB_RUN_ID
    run_dir.mkdir(parents=True)
    proc = subprocess.run(
        [sys.executable, str(DELIVERY), "--run-dir", str(run_dir), "--session-uuid", JOB_UUID,
         "--config-dir", str(config), "--expected", f"/kickoff rotatie {JOB_RUN_ID}",
         "--old-window", OLD_WIN, "--new-window", NEW_WIN, "--tmux", str(rig["tmp"] / "bin" / "tmux"),
         "--timeout", timeout, "--poll", "0.05", *extra_args],
        env={**rig["env"], **(extra_env or {})}, capture_output=True, text=True, timeout=60)
    delivery = run_dir / "delivery.json"
    return proc, (json.loads(delivery.read_text(encoding="utf-8")) if delivery.exists() else None), run_dir


def _kills(rig):
    return [c for c in _calls(rig) if c[0] == "kill-window"]


def test_a_confirmed_delivery_kills_the_old_window(rig):
    proc, delivery, run_dir = _job(rig, NOISE + [KICKOFF_RECORD])
    assert proc.returncode == 0, proc.stderr
    assert delivery["outcome"] == "confirmed"
    assert delivery["expected"] == f"/kickoff rotatie {JOB_RUN_ID}"
    assert delivery["found"] == f"/kickoff rotatie {JOB_RUN_ID}"
    assert delivery["transcript"].endswith(f"{JOB_UUID}.jsonl")
    assert _kills(rig) == [["kill-window", "-t", OLD_WIN]]
    assert not any(c[0] == "display-message" for c in _calls(rig))
    assert not list(run_dir.glob("*.tmp"))
    [receipt] = _only(_receipts(rig), "t0_context_rotation_delivery")
    assert receipt["outcome"] == "confirmed" and receipt["run_id"] == JOB_RUN_ID


def test_a_plain_text_first_message_is_confirmed_too(rig):
    plain = {"type": "user", "message": {"role": "user", "content": f"/kickoff rotatie {JOB_RUN_ID}"}}
    proc, delivery, _ = _job(rig, NOISE + [plain])
    assert proc.returncode == 0 and delivery["outcome"] == "confirmed"
    assert _kills(rig) == [["kill-window", "-t", OLD_WIN]]


def test_a_confirmed_delivery_leaves_the_old_window_when_the_new_one_is_gone(rig):
    proc, delivery, _ = _job(rig, [KICKOFF_RECORD], extra_env={"STUB_NEW_WINDOW_GONE": "1"})
    assert delivery["outcome"] == "confirmed" and delivery["old_window_close"] == "skipped_new_window_gone"
    assert _kills(rig) == []


def test_a_mismatch_keeps_both_windows_and_says_so_in_both(rig):
    tail = {"type": "user", "message": {"role": "user", "content": "e-dir ~/.vnx-data/vnx-dev/state`"}}
    proc, delivery, _ = _job(rig, NOISE + [tail, KICKOFF_RECORD])
    assert proc.returncode == 1
    assert delivery["outcome"] == "mismatch" and delivery["found"] == "e-dir ~/.vnx-data/vnx-dev/state`"
    assert _kills(rig) == []
    notes = [c for c in _calls(rig) if c[0] == "display-message"]
    assert sorted(c[c.index("-t") + 1] for c in notes) == sorted([NEW_WIN, OLD_WIN])
    assert all("mismatch" in c[-1] for c in notes)
    assert not any(c[0] == "send-keys" for c in _calls(rig))
    [receipt] = _only(_receipts(rig), "t0_context_rotation_delivery")
    assert receipt["outcome"] == "mismatch"


def test_the_found_text_is_cut_at_200_characters(rig):
    long = {"type": "user", "message": {"role": "user", "content": "x" * 500}}
    _, delivery, _ = _job(rig, [long])
    assert delivery["outcome"] == "mismatch" and delivery["found"] == "x" * 200


@pytest.mark.parametrize("records", [None, NOISE])
def test_a_timeout_keeps_both_windows(rig, records):
    """No transcript at all, or a transcript without a real user record: the same outcome."""
    proc, delivery, _ = _job(rig, records)
    assert proc.returncode == 1 and delivery["outcome"] == "timeout"
    assert delivery["found"] == ""
    assert _kills(rig) == []
    notes = [c for c in _calls(rig) if c[0] == "display-message"]
    assert sorted(c[c.index("-t") + 1] for c in notes) == sorted([NEW_WIN, OLD_WIN])
    assert not any(c[0] == "send-keys" for c in _calls(rig))


def test_the_job_waits_for_a_transcript_that_appears_late(rig):
    config = rig["tmp"] / "config" / "projects" / "-p"
    config.mkdir(parents=True)
    target = config / f"{JOB_UUID}.jsonl"
    writer = subprocess.Popen([sys.executable, "-c",
                               "import sys,time,json; time.sleep(0.6); "
                               "open(sys.argv[1],'w').write(json.dumps(json.loads(sys.argv[2]))+'\\n')",
                               str(target), json.dumps(KICKOFF_RECORD)])
    try:
        proc, delivery, _ = _job(rig, None, timeout="20")
    finally:
        writer.wait(timeout=30)
    assert proc.returncode == 0 and delivery["outcome"] == "confirmed"


# ── round 2 on #2144 (dispatch 20261021-rot-prompt-r2, OI-2218) ───────────────

def _blocked_ledger(rig) -> Dict[str, str]:
    """A receipts path whose directory is a file: every append to it fails."""
    blocker = rig["tmp"] / "ledger-blocker"
    blocker.write_text("a file where the ledger's directory should be", encoding="utf-8")
    return {"VNX_T0_ROTATION_RECEIPTS_FILE": str(blocker / "t0_receipts.ndjson")}


def _notes(rig) -> List[list]:
    return [c for c in _calls(rig) if c[0] == "display-message" and "-p" not in c]


def test_a_delivery_receipt_the_ledger_refuses_closes_nothing_and_says_so_in_both_windows(rig):
    """Red run on e7ccb56c: delivery.json was written, the failed append ignored, kill-window sent."""
    proc, delivery, run_dir = _job(rig, NOISE + [KICKOFF_RECORD], extra_env=_blocked_ledger(rig))
    assert proc.returncode == 3, proc.stderr
    assert delivery is None and not (run_dir / "delivery.json").exists()
    assert _kills(rig) == []
    assert sorted(c[c.index("-t") + 1] for c in _notes(rig)) == sorted([NEW_WIN, OLD_WIN])
    assert all("receipt" in c[-1] and "NOT booked" in c[-1] for c in _notes(rig))
    assert not any(c[0] == "send-keys" for c in _calls(rig))


def test_a_receipt_append_that_times_out_leaves_both_windows_open(rig, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import t0_rotation_delivery_job as job

    told: List[tuple] = []
    real_run = subprocess.run

    def run(argv, **kwargs):
        if str(job.STATE_PY) in argv:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        told.append(tuple(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(job.subprocess, "run", run)
    config = rig["tmp"] / "config" / "projects" / "-p"
    config.mkdir(parents=True)
    (config / f"{JOB_UUID}.jsonl").write_text(json.dumps(KICKOFF_RECORD) + "\n", encoding="utf-8")
    run_dir = rig["tmp"] / "run" / JOB_RUN_ID
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("STUB_DIR", rig["env"]["STUB_DIR"])
    rc = job.main(["--run-dir", str(run_dir), "--session-uuid", JOB_UUID,
                   "--config-dir", str(rig["tmp"] / "config"), "--expected",
                   f"/kickoff rotatie {JOB_RUN_ID}", "--old-window", OLD_WIN, "--new-window",
                   NEW_WIN, "--tmux", str(rig["tmp"] / "bin" / "tmux"), "--timeout", "0"])
    assert rc == job.EXIT_RECEIPT_FAILED
    assert not (run_dir / "delivery.json").exists()
    assert _kills(rig) == []
    assert sorted(c[c.index("-t") + 1] for c in _notes(rig)) == sorted([NEW_WIN, OLD_WIN])
    assert all("did not finish" in c[-1] for c in _notes(rig))


def test_a_goal_run_with_a_refused_receipt_types_no_goal_and_closes_nothing(rig):
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_TRANSCRIPT": "expanded", **_blocked_ledger(rig)})
    assert proc.returncode == 0, proc.stderr
    assert len(_typed(rig)) == 1, _typed(rig)
    assert _kills(rig) == []
    assert all("receipt" in c[-1] for c in _notes(rig)) and len(_notes(rig)) == 2


def test_the_goal_followup_comes_after_a_confirmed_delivery_and_before_the_old_window_closes(rig):
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    assert _spawn(rig, env_extra={"STUB_TRANSCRIPT": "expanded"}).returncode == 0
    calls = _calls(rig)
    goal_enter = max(i for i, c in enumerate(calls) if c[:1] == ["send-keys"] and c[-1] == "Enter")
    kill_old = next(i for i, c in enumerate(calls) if c == ["kill-window", "-t", OLD_WIN])
    assert goal_enter < kill_old
    assert len([c for c in calls if c[0] == "run-shell"]) == 1, "one server job, not two"
    delivery = json.loads((_run_dir(rig) / "delivery.json").read_text(encoding="utf-8"))
    assert delivery["outcome"] == "confirmed"


@pytest.mark.parametrize("mode", [None, "plain"])
def test_a_goal_run_without_a_confirmed_delivery_types_no_goal(rig, mode):
    """Red run on e7ccb56c: the follow-up ran beside the delivery job and typed /goal after a
    timeout. A delivery timeout (no transcript) and a mismatch both leave the goal to the operator."""
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    extra = {"STUB_TRANSCRIPT": mode} if mode else {}
    assert _spawn(rig, env_extra=extra).returncode == 0
    if mode == "plain":
        assert _kills(rig) != [], "the plain form is a confirmed delivery"
        return
    assert len(_typed(rig)) == 1 and not any(t.startswith("/goal") for t in _typed(rig))
    assert _kills(rig) == []
    assert sorted(c[c.index("-t") + 1] for c in _notes(rig)) == sorted([NEW_WIN, OLD_WIN])
    assert all("nothing was typed" in c[-1] for c in _notes(rig))
    assert not (_run_dir(rig) / "goal_followup.log").exists() or \
        not (_run_dir(rig) / "goal_followup.log").read_text()


def test_the_last_stdout_line_is_printed_before_the_server_job_starts(rig):
    _handoff(rig, HANDOFF_BASE)
    out = rig["tmp"] / "spawner.stdout"
    with out.open("w") as handle:
        proc = subprocess.run(["bash", str(SPAWN), "--project-root", str(rig["project"])],
                              env={**rig["env"], "STUB_STDOUT_FILE": str(out)}, cwd=rig["project"],
                              stdout=handle, stderr=subprocess.PIPE, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    seen = (rig["stub"] / "stdout_at_run_shell").read_text(encoding="utf-8")
    assert seen.startswith("new_window=") and "close=on_delivery_confirmed" in seen
    assert seen == out.read_text(encoding="utf-8")


def test_the_wrong_folder_path_never_carries_the_first_message(rig):
    """Red run on e7ccb56c: the launch line, with the first message, was typed before the folder
    check, so the kickoff ran in the wrong folder until exit 5."""
    t0 = _t0_folder(rig)
    _handoff(rig, HANDOFF_BASE)
    wrong = rig["tmp"] / "elsewhere"
    wrong.mkdir()
    proc = _spawn(rig, env_extra={"STUB_NEW_PANE_PATH": str(wrong)})
    assert proc.returncode == 5
    assert not any("--session-id" in t or "/kickoff" in t for t in _typed(rig)), _typed(rig)
    assert _typed(rig) == [f"cd -- {t0}"]


def test_the_launch_line_follows_the_folder_check_in_order(rig):
    t0 = _t0_folder(rig)
    _handoff(rig, HANDOFF_BASE)
    assert _spawn(rig).returncode == 0
    calls = _calls(rig)
    cd_idx = next(i for i, c in enumerate(calls) if c[:1] == ["send-keys"] and c[-1] == f"cd -- {t0}")
    check_idx = next(i for i, c in enumerate(calls) if i > cd_idx and c[:1] == ["display-message"]
                     and c[-1] == "#{pane_current_path}" and NEW_WIN in c)
    launch_idx = next(i for i, c in enumerate(calls) if c[:1] == ["send-keys"] and "--session-id" in c[-1])
    assert cd_idx < check_idx < launch_idx


def test_a_session_id_that_cannot_be_generated_is_exit_5_and_opens_nothing(rig):
    """The documented code, not the exit status of the failed command substitution."""
    wrapper = rig["tmp"] / "py-no-uuid"
    wrapper.write_text(f'#!/usr/bin/env bash\ncase "$*" in *uuid4*) exit 7;; esac\n'
                       f'exec {sys.executable} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    _handoff(rig, HANDOFF_BASE)
    proc = _spawn(rig, env_extra={"VNX_PYTHON": str(wrapper)})
    assert proc.returncode == 5, proc.stderr
    assert "session id" in _refusal(proc)
    assert not any(c[0] == "new-window" for c in _calls(rig))


# ── the start argument in both forms the transcript can hold ─────────────────

def _lib_delivery():
    import t0_rotation_delivery as delivery
    return delivery


@pytest.mark.parametrize("stored", [
    "<command-message>kickoff</command-message>\n<command-name>/kickoff</command-name>\n"
    "<command-args>rotatie rotation-1-2</command-args>",
    "/kickoff rotatie rotation-1-2",
])
def test_both_forms_of_the_start_argument_are_the_expected_first_message(stored):
    delivery = _lib_delivery()
    assert delivery.normalise(stored) == "/kickoff rotatie rotation-1-2"
    assert delivery.judge("/kickoff rotatie rotation-1-2", delivery.normalise(stored)) == "confirmed"


def test_a_different_command_or_other_args_is_a_mismatch_in_either_form():
    delivery = _lib_delivery()
    other = ("<command-message>kickoff</command-message>\n<command-name>/kickoff</command-name>\n"
             "<command-args>rotatie rotation-9-9</command-args>")
    assert delivery.judge("/kickoff rotatie rotation-1-2", delivery.normalise(other)) == "mismatch"
    assert delivery.judge("/kickoff rotatie rotation-1-2", delivery.normalise("/kickoff")) == "mismatch"
    assert delivery.judge("/kickoff rotatie rotation-1-2", None) == "timeout"


@pytest.mark.parametrize("mode", ["expanded", "plain"])
def test_the_spawner_flow_confirms_each_form_of_the_start_argument(rig, mode):
    _handoff(rig, HANDOFF_BASE)
    assert _spawn(rig, env_extra={"STUB_TRANSCRIPT": mode}).returncode == 0
    delivery = json.loads((_run_dir(rig) / "delivery.json").read_text(encoding="utf-8"))
    assert delivery["outcome"] == "confirmed"
    assert _kills(rig) == [["kill-window", "-t", OLD_WIN]]


# ── the /goal follow-up reads the successor's transcript (dispatch dlv-259886a390ec) ──
# Measured 2026-10-22 on claude 2.1.308: a running turn shows `✳ Tempering… (52s · ↓ 4.5k tokens
# · thought for 13s)` with the footer still on screen and no `esc to interrupt`. In
# rotation-20261022-170308-53038 and rotation-20261022-191855-76425 the follow-up read that as
# idle and typed /goal into the kickoff turn; the transcript queued it and holds no /goal entry.

GOAL_LINE = ("/goal werk alle open blockers af tot de lijst leeg is, hervat na context-rotatie; "
             "resterend: OI-1850 sluiten; OI-1851 fixen")


def _goal_entries(rig) -> List[dict]:
    return [r for r in _successor_records(rig) if r.get("type") == "user"
            and isinstance(r["message"]["content"], str)
            and r["message"]["content"].startswith("<command-name>/goal</command-name>")]


@pytest.mark.parametrize("screen", ["2.1.308", "idle"])
def test_the_goal_waits_for_the_end_of_the_kickoff_turn_in_the_transcript(rig, screen):
    """Red run on origin/main: the follow-up waited for `esc to interrupt`, never saw it, read the
    2.1.308 busy screen (spinner plus footer) as idle and typed /goal into the kickoff turn."""
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_TRANSCRIPT": "expanded", "STUB_TURN_CAPTURES": "12",
                                  "STUB_SCREEN": screen})
    assert proc.returncode == 0, proc.stderr
    ends, goal_typed = _turn_ends(rig), _typed_at(rig, "/goal ")
    assert ends, "the kickoff turn never ended in the transcript"
    assert goal_typed and goal_typed[0] > ends[0], (
        f"/goal typed at call {goal_typed[:1]}, the kickoff turn ended at call {ends[0]}")
    assert len(goal_typed) == 1 and len(_goal_entries(rig)) == 1
    assert [r["outcome"] for r in _goal_outcomes(rig)] == ["confirmed"]


def test_goal_active_on_screen_without_a_transcript_entry_is_unconfirmed(rig):
    """The screen is a second opinion: `/goal active` without a /goal user entry after the kickoff
    entry is no arrival (on origin/main the screen alone confirmed it)."""
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_TRANSCRIPT": "expanded", "STUB_GOAL_SCREEN_ONLY": "1"})
    assert proc.returncode == 0, proc.stderr
    assert _goal_entries(rig) == []
    [outcome] = _goal_outcomes(rig)
    assert outcome["outcome"] == "unconfirmed" and outcome["screen_goal_active"] == "1"
    assert len(_typed_at(rig, "/goal ")) == 2
    assert _typed(rig)[-1].startswith("LET OP: het automatische /goal-vervolg")
    _assert_enter_always_separate(rig)


def test_the_retry_waits_for_an_idle_turn_and_types_a_queued_line_again(rig):
    """A task notification starts a turn just before the /goal lands: the line is queued (on
    screen, no /goal entry). The retry waits until that turn ended in the transcript and types the
    line again; Enter alone would land in an empty input box."""
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_TRANSCRIPT": "expanded", "STUB_TURN_BEFORE_GOAL": "1",
                                  "STUB_BUSY_CAPTURES": "8"})
    assert proc.returncode == 0, proc.stderr
    ends, goal_typed = _turn_ends(rig), _typed_at(rig, "/goal ")
    assert len(goal_typed) == 2, _typed(rig)
    assert len(ends) >= 2 and goal_typed[0] < ends[1] < goal_typed[1], (goal_typed, ends)
    assert len(_goal_entries(rig)) == 1
    assert [r["outcome"] for r in _goal_outcomes(rig)] == ["confirmed"]
    _assert_enter_always_separate(rig)


def test_the_fallback_waits_for_an_idle_turn(rig):
    """The /goal never lands and a turn starts just before the retry lands: the LET OP line goes
    in only after that turn ended in the transcript."""
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_TRANSCRIPT": "expanded", "STUB_GOAL_NEVER": "1",
                                  "STUB_TURN_BEFORE_GOAL": "2", "STUB_BUSY_CAPTURES": "8"})
    assert proc.returncode == 0, proc.stderr
    ends, goal_typed, let_op = _turn_ends(rig), _typed_at(rig, "/goal "), _typed_at(rig, "LET OP: ")
    assert len(goal_typed) == 2 and len(let_op) == 1, _typed(rig)
    assert len(ends) >= 2 and goal_typed[1] < ends[1] < let_op[0], (goal_typed, ends, let_op)
    [outcome] = _goal_outcomes(rig)
    assert outcome["outcome"] == "unconfirmed" and outcome["fallback"] == "typed"
    assert any(c[c.index("-t") + 1] == NEW_WIN and "unconfirmed" in c[-1] for c in _notes(rig))


def test_a_turn_that_never_ends_books_turn_timeout_and_types_nothing(rig):
    """VNX_T0_ROTATE_TURN_TIMEOUT bounds the wait. The session never was idle, so neither the
    /goal nor the LET OP line is typed; the status line and the receipt name the outcome."""
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_TRANSCRIPT": "expanded", "STUB_TURN_CAPTURES": "1000",
                                  "VNX_T0_ROTATE_TURN_TIMEOUT": "6"})
    assert proc.returncode == 0, proc.stderr
    assert _turn_ends(rig) == []
    assert len(_typed(rig)) == 1, _typed(rig)
    [outcome] = _goal_outcomes(rig)
    assert outcome["outcome"] == "turn_timeout" and outcome["fallback"] == "not_typed"
    notes = [c for c in _notes(rig) if c[c.index("-t") + 1] == NEW_WIN]
    assert notes and "turn_timeout" in notes[-1][-1]
    assert _kills(rig) == [["kill-window", "-t", OLD_WIN]], "the job closes the old window after the follow-up"


@pytest.mark.parametrize("screen", ["2.1.308", "legacy"])
def test_a_spinner_on_screen_holds_the_goal_after_the_transcript_turn_end(rig, screen):
    """The second opinion: the default busy pattern knows the 2.1.308 spinner (no `esc to
    interrupt`) and the older one; while the screen shows it, the transcript's idle waits."""
    _handoff(rig, HANDOFF_BASE + GOAL_SECTION)
    proc = _spawn(rig, env_extra={"STUB_TRANSCRIPT": "expanded", "STUB_SCREEN": screen,
                                  "STUB_SCREEN_LAG": "5"})
    assert proc.returncode == 0, proc.stderr
    calls = _calls(rig)
    end = _turn_ends(rig)[0]
    [goal_typed] = _typed_at(rig, "/goal ")
    # the capture that saw the end, four more with the spinner, then two idle ones
    assert len([c for c in calls[end:goal_typed] if c[0] == "capture-pane"]) >= 7
    assert [r["outcome"] for r in _goal_outcomes(rig)] == ["confirmed"]


def test_the_job_hands_the_transcript_of_the_delivery_check_to_the_followup(rig):
    """The follow-up reads the transcript the delivery check read (delivery.json), never a search."""
    argv_file = rig["tmp"] / "followup-argv.txt"
    recorder = rig["tmp"] / "followup-recorder.sh"
    recorder.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {shlex.quote(str(argv_file))}\n',
                        encoding="utf-8")
    goal = rig["tmp"] / "goal.txt"
    goal.write_text(GOAL_LINE, encoding="utf-8")
    proc, delivery, _ = _job(rig, NOISE + [KICKOFF_RECORD],
                             extra_args=("--goal-file", str(goal), "--followup-script", str(recorder)))
    assert proc.returncode == 0, proc.stderr
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert argv[0] == "--goal-followup"
    assert argv[argv.index("--transcript") + 1] == delivery["transcript"]
    assert _kills(rig) == [["kill-window", "-t", OLD_WIN]]


def test_the_followup_without_a_transcript_types_nothing(rig):
    goal = rig["tmp"] / "goal.txt"
    goal.write_text(GOAL_LINE, encoding="utf-8")
    proc = subprocess.run(["bash", str(SPAWN), "--goal-followup", "--target", NEW_WIN,
                           "--goal-file", str(goal), "--event-file", str(goal)],
                          env=rig["env"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2, proc.stderr
    assert "--transcript" in proc.stderr
    assert not any(c[0] == "send-keys" for c in _calls(rig))
    assert _goal_outcomes(rig) == []
