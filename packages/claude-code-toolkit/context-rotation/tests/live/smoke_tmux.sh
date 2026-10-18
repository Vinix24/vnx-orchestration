#!/bin/sh
# Live smoke of the automatic /clear (loop step 7), run by hand in a throwaway tmux server.
#
# `start` launches a real `claude` on the login of the shell that runs it, so a person
# runs this, never CI and never a build worker. The tmux server is private (socket name
# context-rotation-smoke): nothing outside it is read or typed into, and this script
# types nothing at all. You type into the smoke pane yourself, as the runs below say.
#
#   sh smoke_tmux.sh start <plugin_dir> <project_dir> [window_tokens]
#   sh smoke_tmux.sh attach
#   sh smoke_tmux.sh capture <project_dir> <label>
#   sh smoke_tmux.sh check <project_dir> happy|input|keep-working|background
#   sh smoke_tmux.sh stop
#
# start   refuses a project dir that exists, creates it as a git repo with
#         .claude/context-rotation.json (window_tokens, default 100000, so the guard's
#         notices show early), and starts `claude --plugin-dir <plugin_dir>` in it, in a
#         200x50 pane. Your Claude login is used as it is: CLAUDE_CONFIG_DIR is passed on
#         only when your shell has it set, and never set to a new directory. Evidence goes
#         to <project_dir>.evidence/. Needs tmux 3.0 or later (new-session -e).
# capture saves the pane as <label>.esc.txt (capture-pane -e) and <label>.txt (wrapped lines joined),
#         and where tmux has the cursor as <label>.cursor ("<cursor_x> <cursor_y>"), from one tmux call.
# check   reads .claude/rotation/injections.jsonl, the pane's clear-<n>.json and the
#         captures, prints PASS or FAIL per check, and exits 1 when one fails.
#
# The runs to see, in this order (each starts from an idle, empty input line):
#
# 1 happy: the automatic clear works.
#   Answer the folder trust question, then type /context-rotation:rotate and leave the
#   input line alone. The model writes the handover; at the end of its turn a line says
#   /clear is typed for you. /clear appears, the fresh session starts with "continued
#   from the handover of the previous session", and "Continue from the handover in your
#   context, step 1." is typed and sent once the fresh session's `delivered` line is
#   there. Then: capture happy, check happy. This run is also the live proof of the
#   job's terminal check: the real `claude` (CLAUDE_PID) must have the pane's tty as its
#   controlling terminal and be in its foreground process group. A FAIL whose
#   clear-<n>.json says process_background or process_stopped means that check does not
#   hold for this Claude Code, and no automatic clear will ever happen with it.
#
# 2 input: text in the input line, nothing typed.
#   Type /context-rotation:rotate, and while the model still writes, type hello in the
#   input line without Enter. When the turn ends the status line says "no automatic
#   /clear: there is text in the input line", and the input line still reads hello.
#   Then: capture input, check input. The check also reads where tmux has the cursor:
#   the job removes what it typed only while the cursor stands right after it, so a
#   FAIL there means Claude Code keeps the terminal cursor elsewhere and the job always
#   leaves its text in the input line instead (it says so) rather than removing it.
#
# 3 keep-working: the user keeps working after an aborted clear, and the old handover
#   is never used for a clear. Right after run 2, press Enter on hello and let the
#   model answer. The line under your prompt repeats why no /clear happened, and no
#   /clear follows, in this turn or a later one. Then: capture keep-working,
#   check keep-working.
#
# 4 background: a running background shell, no clear.
#   Type: Run sleep 300 in the background with the Bash tool, then stop. When it has
#   stopped, type /context-rotation:rotate. At the end of that turn the line says this
#   session still runs 1 background shell, so /clear is not typed; nothing is typed.
#   Then: capture background, check background. Stop the sleep afterwards.
#
# Afterwards: stop, then delete the project dir and its .evidence dir.

socket=context-rotation-smoke
session=smoke

usage() {
  cat >&2 <<'USAGE'
usage: sh smoke_tmux.sh start <plugin_dir> <project_dir> [window_tokens]
       sh smoke_tmux.sh attach
       sh smoke_tmux.sh capture <project_dir> <label>
       sh smoke_tmux.sh check <project_dir> happy|input|keep-working|background
       sh smoke_tmux.sh stop
USAGE
  exit 1
}

smoke_tmux() {
  tmux -L "$socket" "$@"
}

evidence_of() {
  printf '%s.evidence' "$1"
}

refuse_hash() {
  case "$1" in
    *"#"*)
      echo "smoke: $2 holds a '#', which tmux would read as a format; pick another path" >&2
      exit 1
      ;;
  esac
}

start() {
  [ "$#" -ge 2 ] || usage
  plugin=$1
  project=$2
  window=${3:-100000}
  case "$window" in
    "" | *[!0-9]*)
      echo "smoke: window_tokens must be a number" >&2
      exit 1
      ;;
  esac
  [ -f "$plugin/.claude-plugin/plugin.json" ] || {
    echo "smoke: $plugin is not the plugin dir (no .claude-plugin/plugin.json)" >&2
    exit 1
  }
  [ ! -e "$project" ] || {
    echo "smoke: $project exists; give a project dir that does not exist yet" >&2
    exit 1
  }
  refuse_hash "$plugin" "the plugin dir"
  refuse_hash "$project" "the project dir"
  if smoke_tmux has-session -t "$session" 2>/dev/null; then
    echo "smoke: the smoke server already runs; sh $0 stop first" >&2
    exit 1
  fi
  command -v claude >/dev/null 2>&1 || {
    echo "smoke: claude is not on PATH" >&2
    exit 1
  }
  plugin=$(cd "$plugin" && pwd)
  mkdir -p "$project/.claude" || exit 1
  project=$(cd "$project" && pwd)
  evidence=$(evidence_of "$project")
  mkdir -p "$evidence" || exit 1
  printf '%s\n' "$plugin" > "$evidence/plugin_dir"
  git -C "$project" init -q || exit 1
  printf '{"window_tokens": %s, "ack_timeout_seconds": 30}\n' "$window" > "$project/.claude/context-rotation.json"
  set -- -e "SMOKE_PLUGIN_DIR=$plugin"
  if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
    set -- "$@" -e "CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR"
  fi
  # The pane's own shell expands $SMOKE_PLUGIN_DIR, so no path is part of the command line.
  # shellcheck disable=SC2016
  smoke_tmux -f /dev/null new-session -d -s "$session" -x 200 -y 50 -c "$project" "$@" \
    'exec claude --plugin-dir "$SMOKE_PLUGIN_DIR"' || exit 1
  echo "smoke: claude runs in the private server; attach with: sh $0 attach"
  echo "smoke: evidence goes to $evidence"
}

capture() {
  [ "$#" -eq 2 ] || usage
  case "$2" in
    "" | *[!A-Za-z0-9_-]*)
      echo "smoke: a label is letters, digits, - and _" >&2
      exit 1
      ;;
  esac
  evidence=$(evidence_of "$1")
  [ -d "$evidence" ] || {
    echo "smoke: no evidence dir $evidence; was it started with this project dir?" >&2
    exit 1
  }
  # One call, so the cursor and the screen are of the same moment, as the job reads them.
  smoke_tmux display-message -p -t "$session" '#{cursor_x} #{cursor_y}' \; \
    capture-pane -p -e -t "$session" > "$evidence/$2.look" || exit 1
  head -n 1 "$evidence/$2.look" > "$evidence/$2.cursor" || exit 1
  tail -n +2 "$evidence/$2.look" > "$evidence/$2.esc.txt" || exit 1
  rm -f "$evidence/$2.look"
  smoke_tmux capture-pane -p -J -t "$session" > "$evidence/$2.txt" || exit 1
  echo "smoke: saved $evidence/$2.esc.txt, $evidence/$2.cursor and $evidence/$2.txt"
}

check() {
  [ "$#" -eq 2 ] || usage
  case "$2" in
    happy | input | keep-working | background) ;;
    *) usage ;;
  esac
  evidence=$(evidence_of "$1")
  [ -f "$evidence/plugin_dir" ] || {
    echo "smoke: no $evidence/plugin_dir; was it started with this project dir?" >&2
    exit 1
  }
  plugin=$(cat "$evidence/plugin_dir")
  python3 - "$plugin" "$1" "$evidence" "$2" <<'PYTHON'
import glob
import json
import os
import sys

plugin, project, evidence, run = sys.argv[1:5]
sys.path.insert(0, os.path.join(plugin, "lib"))
import context_budget
import pane_screen

rotation = os.path.join(project, ".claude", "rotation")
journal = os.path.join(rotation, "injections.jsonl")
lines = []
if os.path.exists(journal):
    with open(journal, encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]
failed = []


def verdict(ok, text):
    print("%s %s" % ("PASS" if ok else "FAIL", text))
    if not ok:
        failed.append(text)


ready = [line for line in lines if line["event"] == "ready"]
verdict(bool(ready), "a handover was marked ready")
if not ready:
    sys.exit(1)
generation = ready[-1]["generation"]
events = [line for line in lines if line.get("generation") == generation]
names = [line["event"] for line in events]
print("newest handover: generation %s of session %s" % (generation, ready[-1]["origin_session_id"]))
print("its lines: %s" % ", ".join(names))


def screen_of(label):
    path = os.path.join(evidence, label + ".esc.txt")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return pane_screen.read(handle.read(), context_budget.load_settings(project))


def cursor_of(label):
    path = os.path.join(evidence, label + ".cursor")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        fields = handle.read().split()
    return tuple(int(field) for field in fields) if len(fields) == 2 and all(f.isdigit() for f in fields) else None


def ordered(*wanted):
    position = -1
    for name in wanted:
        if name not in names[position + 1:]:
            return False
        position = names.index(name, position + 1)
    return True


if run == "happy":
    verdict(ordered("clear_started", "clear_entered", "injected", "delivered", "clear_acknowledged",
                    "clear_continued"),
            "started, /clear entered, injected, delivered, acknowledged and continued, in that order")
    injected = [line for line in events if line["event"] == "injected"]
    verdict(bool(injected) and injected[-1]["via"] == "clear", "the injection came through /clear")
    if injected:
        verdict(os.path.exists(injected[-1]["archive"]), "the archive copy %s exists" % injected[-1]["archive"])
        print("fresh session: %s; its transcript is <config dir>/projects/*/%s.jsonl"
              % (injected[-1]["session_id"], injected[-1]["session_id"]))
    verdict("clear_aborted" not in names, "the job did not stop")
elif run == "input":
    aborted = [line for line in events if line["event"] == "clear_aborted"]
    verdict(bool(aborted) and aborted[-1]["abort_reason"] == "input", "the job stopped on text in the input line")
    verdict("clear_entered" not in names, "/clear was never entered")
    screen = screen_of("input")
    typed = screen.typed if screen else None
    verdict(typed == "hello", "the input line still reads hello (it reads %r)" % (typed,))
    cursor = cursor_of("input")
    after = (screen.end, screen.row) if screen else None
    verdict(cursor is not None and cursor == after,
            "the cursor stands right after the typed text (cursor %r, text ends at %r)" % (cursor, after))
elif run == "keep-working":
    verdict("clear_aborted" in names and "clear_entered" not in names, "the clear stopped and was never entered")
    verdict(ordered("clear_aborted", "clear_told"), "the next prompt repeated why")
    verdict(ordered("clear_aborted", "outdated"), "the prompt made the handover outdated")
    verdict("injected" not in names, "the old handover was never injected")
else:
    verdict("clear_started" not in names, "no clear started for the handover")
    path = os.path.join(evidence, "background.txt")
    shown = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            shown = handle.read()
    verdict("still runs 1 background shell" in shown, "the pane says the background shell still runs")
for name in sorted(glob.glob(os.path.join(rotation, "clear-*.json"))):
    with open(name, encoding="utf-8") as handle:
        print("%s: %s" % (os.path.basename(name), handle.read().strip()))
sys.exit(1 if failed else 0)
PYTHON
}

case "${1:-}" in
  start)
    shift
    start "$@"
    ;;
  attach)
    smoke_tmux has-session -t "$session" 2>/dev/null || {
      echo "smoke: the smoke server does not run; sh $0 start first" >&2
      exit 1
    }
    exec tmux -L "$socket" attach -t "$session"
    ;;
  capture)
    shift
    capture "$@"
    ;;
  check)
    shift
    check "$@"
    ;;
  stop)
    smoke_tmux kill-server
    ;;
  *)
    usage
    ;;
esac
