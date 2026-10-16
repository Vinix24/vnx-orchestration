#!/usr/bin/env bash
# t0_rotate_spawn.sh — start the successor T0 after a context rotation.
#
# Called by the rotate skill AFTER `/build-log wrap` wrote daily-log/handoff.md. Contract and
# rationale: docs/operations/T0_CONTEXT_ROTATION.md. The guard that demands the rotation is
# scripts/hooks/t0_context_guard.py; the state both share is scripts/lib/t0_rotation_state.py.
#
# What it does, in order:
#   1. Refuses unless it runs inside tmux and the handoff was written in the last N minutes
#      (default 15). Handing over a stale handoff is the worst case: the successor would
#      confidently resume the wrong state.
#   2. Reads the first numbered next step and an optional `## Actief /goal` section, and checks
#      the wrap proof (operator decisions 2026-10-16 a, b): the `wrap:` block that
#      `t0_rotation_state.py wrap-finish` writes into the handoff frontmatter must bind to this
#      wrap (wrapped_at fresh, the daily log written inside the wrap's window, every nugget
#      heading in the bucket, and the start record it names consumed by exactly this block),
#      in a frontmatter that parses. No proof: exit 6, "run /build-log wrap".
#      Emergency path (decision c): `--skip-wrap-reason "<text>"` skips the wrap proof and the
#      age check, never the first-step check. It needs the predecessor's transcript
#      (`--transcript`, else the pending marker's `transcript_path`) as a readable JSONL file,
#      books a `t0_context_rotation_wrap_skipped` receipt before any window opens (ledger first,
#      ADR-005; a refusing ledger refuses the spawn), and puts a catch-up wrap from that
#      transcript at the head of the prompt. A relative transcript path is made absolute against
#      the caller's folder first, because the successor starts in another folder.
#   3. Opens a new window next to the current one, with the current window's name, and types
#      `claude --model opus` (plus `--remote-control "<name>"` when this session is bridged,
#      and CLAUDE_CONFIG_DIR when set: tmux does not carry a bare export into the pane).
#      The window starts in <root>/.claude/terminals/T0 when that folder has a CLAUDE.md AND its
#      `.claude` resolves (pwd -P) to <root>/.claude; otherwise in the project root, with one log
#      line, and a WARNING naming a non-destructive repair when the T0 folder is the broken one
#      (the spawner prints the repair, it runs nothing). A T0-folder start types
#      `cd -- <folder> && <launch>` and checks the new pane's folder before the prompt is typed.
#      A healthy T0 folder without a `.vnx-data` link still starts there (one log line).
#   4. Waits for the claude footer, then types ONE line of natural language: kill the old
#      window, run the kickoff skill on the handoff, then take up step 1. Enter is always a
#      separate send-keys call.
#   5. Writes the latch that releases the guard's Stop block and a state_mutation receipt.
#   6. With an active /goal: hands the follow-up to the tmux SERVER (`run-shell -b`). The
#      successor kills this window in its first turn, which kills this script too; a job on
#      the server survives that. The follow-up waits until the first turn is over, types the
#      /goal line, and checks the footer shows the goal as active (one retry, then a loud
#      message in the new window).
#
# Usage:
#   t0_rotate_spawn.sh [--handoff PATH] [--max-age-minutes N] [--session-id ID]
#                      [--project-root DIR] [--rc | --no-rc]
#                      [--skip-wrap-reason "<text>" [--transcript PATH]]
#
# Timing knobs (env, seconds): VNX_T0_ROTATE_FOOTER_TIMEOUT (60), VNX_T0_ROTATE_POLL_SECONDS (1),
# VNX_T0_ROTATE_SETTLE_SECONDS (2), VNX_T0_ROTATE_TURN_TIMEOUT (1800),
# VNX_T0_ROTATE_GOAL_CONFIRM_TIMEOUT (20).
# Screen patterns (env, grep -E): VNX_T0_ROTATE_FOOTER_PATTERN ("auto mode"),
# VNX_T0_ROTATE_BUSY_PATTERN ("esc to interrupt"), VNX_T0_ROTATE_GOAL_ACTIVE_PATTERN ("/goal active").
#
# Switch: VNX_T0_ROTATE_START_DIR = t0 (default) | root. The tmux global environment is read first
# (`tmux show-environment -g VNX_T0_ROTATE_START_DIR`), the process environment second; `root`
# forces the project-root start. Set it with `tmux set-environment -g VNX_T0_ROTATE_START_DIR root`,
# remove it with `tmux set-environment -gu VNX_T0_ROTATE_START_DIR`.
#
# Last stdout line: new_window=@N old_window=@N goal_followup=0|1 run_dir=PATH start_dir=PATH
# (start_dir is shell-quoted with %q and is the last field).
#
# Exit codes: 0 spawned, 2 usage / invalid VNX_T0_ROTATE_START_DIR / empty --skip-wrap-reason,
# 3 not in tmux / handoff missing or stale, 4 handoff without a first step, 5 successor did not
# come up or started in another folder than the start folder (the old window is left untouched),
# 6 no valid wrap proof ("run /build-log wrap"), or a skip run without a readable JSONL
# transcript, with a transcript path that cannot be made absolute, or whose receipt the ledger
# refused (no window opened in any of these).

set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"
SCRIPTS_DIR="$(dirname "$SCRIPT_PATH")"
STATE_PY="$SCRIPTS_DIR/lib/t0_rotation_state.py"
PYTHON="${VNX_PYTHON:-python3}"

POLL="${VNX_T0_ROTATE_POLL_SECONDS:-1}"
SETTLE="${VNX_T0_ROTATE_SETTLE_SECONDS:-2}"
FOOTER_TIMEOUT="${VNX_T0_ROTATE_FOOTER_TIMEOUT:-60}"
TURN_TIMEOUT="${VNX_T0_ROTATE_TURN_TIMEOUT:-1800}"
GOAL_CONFIRM_TIMEOUT="${VNX_T0_ROTATE_GOAL_CONFIRM_TIMEOUT:-20}"
FOOTER_PATTERN="${VNX_T0_ROTATE_FOOTER_PATTERN:-auto mode}"
BUSY_PATTERN="${VNX_T0_ROTATE_BUSY_PATTERN:-esc to interrupt}"
GOAL_ACTIVE_PATTERN="${VNX_T0_ROTATE_GOAL_ACTIVE_PATTERN:-/goal active}"

log() { printf '[t0-rotate %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { local code="$1"; shift; log "REFUSED: $*"; exit "$code"; }

# Poll the pane until `grep -E pattern` matches, or the timeout (seconds) passes.
wait_for_screen() {
  local target="$1" pattern="$2" timeout="$3" waited=0
  while :; do
    if tmux capture-pane -p -t "$target" 2>/dev/null | grep -Eq -- "$pattern"; then
      return 0
    fi
    if [ "$waited" -ge "$timeout" ]; then
      return 1
    fi
    sleep "$POLL"
    waited=$((waited + (POLL > 0 ? POLL : 1)))
  done
}

# Type one line into the pane, then Enter as its own keystroke (a combined send-keys misses).
type_line() {
  local target="$1" text="$2"
  tmux send-keys -t "$target" -l "$text"
  sleep "$SETTLE"
  tmux send-keys -t "$target" Enter
}

# ── follow-up mode: runs on the tmux server, after the successor's first turn ──
goal_followup() {
  local target="" goal_file="" state_file=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --target) target="$2"; shift 2 ;;
      --goal-file) goal_file="$2"; shift 2 ;;
      --event-file) state_file="$2"; shift 2 ;;
      *) log "goal-followup: unknown argument $1"; return 2 ;;
    esac
  done
  local goal outcome="confirmed"
  goal="$(cat "$goal_file")"

  # The first turn: it starts when the kickoff prompt lands (busy marker appears) and ends when
  # the footer is back without the busy marker. A turn too short to catch busy is fine: we then
  # proceed on the first idle screen.
  wait_for_screen "$target" "$BUSY_PATTERN" "$FOOTER_TIMEOUT" || log "goal-followup: busy marker never seen; continuing on idle"
  local waited=0 idle_streak=0 screen
  while :; do
    screen="$(tmux capture-pane -p -t "$target" 2>/dev/null || true)"
    if printf '%s' "$screen" | grep -Eq -- "$FOOTER_PATTERN" && ! printf '%s' "$screen" | grep -Eq -- "$BUSY_PATTERN"; then
      idle_streak=$((idle_streak + 1))
      [ "$idle_streak" -ge 2 ] && break
    else
      idle_streak=0
    fi
    if [ "$waited" -ge "$TURN_TIMEOUT" ]; then
      outcome="turn_timeout"
      break
    fi
    sleep "$POLL"
    waited=$((waited + (POLL > 0 ? POLL : 1)))
  done

  if [ "$outcome" = "confirmed" ]; then
    # /goal is a native slash command. Typing it opens the slash autocomplete; with arguments
    # after a space the literal text stays in the box. No Escape: a double Escape in the input
    # box clears it or opens rewind. Verify on screen instead, retry once.
    type_line "$target" "$goal"
    if ! wait_for_screen "$target" "$GOAL_ACTIVE_PATTERN" "$GOAL_CONFIRM_TIMEOUT"; then
      local prefix="${goal:0:40}"
      if tmux capture-pane -p -t "$target" 2>/dev/null | grep -Fq -- "$prefix"; then
        # Still in the input box: the first Enter was taken by the autocomplete.
        tmux send-keys -t "$target" Enter
      else
        type_line "$target" "$goal"
      fi
      wait_for_screen "$target" "$GOAL_ACTIVE_PATTERN" "$GOAL_CONFIRM_TIMEOUT" || outcome="unconfirmed"
    fi
  fi

  if [ "$outcome" != "confirmed" ]; then
    local msg="LET OP: het automatische /goal-vervolg na de context-rotatie is niet bevestigd ($outcome). Meld dit aan de operator en vraag hem deze regel zelf in te typen: $goal"
    tmux display-message -t "$target" "VNX t0-rotate: /goal-vervolg NIET bevestigd ($outcome), zie het bericht in dit venster" || true
    type_line "$target" "$msg"
    log "goal-followup: $outcome"
  fi
  "$PYTHON" "$STATE_PY" event --trigger t0_context_rotation_goal_followup \
    --file "${state_file:-$goal_file}" --field "outcome=$outcome" --field "window=$target" || true
  [ "$outcome" = "confirmed" ]
}

if [ "${1:-}" = "--goal-followup" ]; then
  shift
  goal_followup "$@"
  exit $?
fi

# ── spawn mode ──
HANDOFF=""
MAX_AGE_MIN=15
SESSION_ID=""
PROJECT_ROOT_ARG=""
RC_MODE="auto"
SKIP_WRAP=0
SKIP_REASON=""
TRANSCRIPT_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --handoff) HANDOFF="$2"; shift 2 ;;
    --max-age-minutes) MAX_AGE_MIN="$2"; shift 2 ;;
    --session-id) SESSION_ID="$2"; shift 2 ;;
    --project-root) PROJECT_ROOT_ARG="$2"; shift 2 ;;
    --rc) RC_MODE="on"; shift ;;
    --no-rc) RC_MODE="off"; shift ;;
    --skip-wrap-reason)
      [ $# -ge 2 ] || die 2 "--skip-wrap-reason needs a reason"
      SKIP_WRAP=1; SKIP_REASON="$2"; shift 2 ;;
    --transcript)
      [ $# -ge 2 ] || die 2 "--transcript needs a path"
      TRANSCRIPT_ARG="$2"; shift 2 ;;
    -h|--help) sed -n '2,67p' "$SCRIPT_PATH"; exit 0 ;;
    *) die 2 "unknown argument: $1" ;;
  esac
done
[[ "$MAX_AGE_MIN" =~ ^[0-9]+$ ]] || die 2 "--max-age-minutes must be a whole number, got '$MAX_AGE_MIN'"
if [ "$SKIP_WRAP" -eq 1 ] && [ -z "${SKIP_REASON//[[:space:]]/}" ]; then
  die 2 "--skip-wrap-reason needs a reason; an empty one is refused (the reason goes into the ledger)"
fi
if [ -n "$TRANSCRIPT_ARG" ] && [ "$SKIP_WRAP" -eq 0 ]; then
  die 2 "--transcript only applies with --skip-wrap-reason"
fi

# A relative transcript path names a file in the folder this was called from, but the successor
# starts in another one (the T0 folder). Prefix the physical folder (`pwd -P`: the folder the
# kernel resolves the relative path against, so a `..` keeps its meaning); an absolute path stays
# byte for byte. Fails when that folder is gone.
absolute_path() {
  local here
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *)
      here="$(pwd -P 2>/dev/null)" && [ -n "$here" ] || return 1
      printf '%s/%s\n' "$here" "$1" ;;
  esac
}

# Resolved now, before anything else runs: the check, the receipt and the prompt all see the path
# the caller meant.
if [ -n "$TRANSCRIPT_ARG" ]; then
  transcript_abs="$(absolute_path "$TRANSCRIPT_ARG")" \
    || die 6 "cannot resolve --transcript $TRANSCRIPT_ARG to an absolute path: the folder it is relative to is gone; nothing opened"
  TRANSCRIPT_ARG="$transcript_abs"
fi

[ -n "${TMUX:-}" ] && [ -n "${TMUX_PANE:-}" ] || die 3 "not running inside tmux (TMUX/TMUX_PANE unset); rotation needs a tmux window to hand over"

# The switch is validated before any window is opened. A set tmux global wins over the process
# value; an unset or removed global (`unknown variable`, or `-NAME`) falls through to it.
START_MODE="${VNX_T0_ROTATE_START_DIR:-}"
if tmux_global="$(tmux show-environment -g VNX_T0_ROTATE_START_DIR 2>/dev/null)"; then
  case "$tmux_global" in
    VNX_T0_ROTATE_START_DIR=?*) START_MODE="${tmux_global#VNX_T0_ROTATE_START_DIR=}" ;;
  esac
fi
START_MODE="${START_MODE:-t0}"
case "$START_MODE" in
  t0|root) ;;
  *) die 2 "VNX_T0_ROTATE_START_DIR='$START_MODE' is invalid; valid values are 't0' (default) and 'root'" ;;
esac

if [ -n "$PROJECT_ROOT_ARG" ]; then
  PROJECT_ROOT="$(cd "$PROJECT_ROOT_ARG" && pwd -P)"
else
  # Resolve from the cwd, not from this file: in a consumer project this script lives in the
  # central install, and the project is where T0 runs.
  # shellcheck source=lib/vnx_resolve_root.sh
  source "$SCRIPTS_DIR/lib/vnx_resolve_root.sh"
  vnx_resolve_project_root "" || die 3 "cannot resolve the project root from $(pwd)"
  PROJECT_ROOT="$VNX_PROJECT_ROOT"
fi
HANDOFF="${HANDOFF:-$PROJECT_ROOT/daily-log/handoff.md}"

[ -f "$HANDOFF" ] || die 3 "handoff not found: $HANDOFF (run /build-log wrap first)"
# A skip run hands over without a fresh wrap by definition, so only a normal run checks the age.
if [ "$SKIP_WRAP" -eq 0 ]; then
  # Age via python, not stat: `stat -f %m` is BSD, but GNU stat reads -f as "filesystem status"
  # and prints a block on stdout before failing, which then lands in the arithmetic (`File:
  # unbound variable` under set -u on Linux).
  age="$("$PYTHON" -c 'import os, sys, time; print(int(time.time() - os.stat(sys.argv[1]).st_mtime))' "$HANDOFF")" \
    || die 3 "cannot read the modification time of $HANDOFF"
  if [ "$age" -gt $((MAX_AGE_MIN * 60)) ]; then
    die 3 "handoff $HANDOFF is $((age / 60)) min old (limit $MAX_AGE_MIN); run /build-log wrap again so the successor gets the current state"
  fi
fi

OLD_WIN="$(tmux display-message -p -t "$TMUX_PANE" '#{window_id}')"
OLD_NAME="$(tmux display-message -p -t "$TMUX_PANE" '#{window_name}')"
[ -n "$OLD_WIN" ] || die 3 "cannot read the current tmux window id"

STATE_DIR="$("$PYTHON" "$STATE_PY" state-dir --project-root "$PROJECT_ROOT")"
RUN_DIR="$STATE_DIR/rotation-$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$RUN_DIR"

# The predecessor's transcript of a skip run: --transcript (already absolute), else the pending
# marker the guard wrote for this pane, made absolute the same way. compose refuses (6) when it is
# not a readable JSONL file.
TRANSCRIPT=""
if [ "$SKIP_WRAP" -eq 1 ]; then
  if [ -n "$TRANSCRIPT_ARG" ]; then
    TRANSCRIPT="$TRANSCRIPT_ARG"
  else
    pending_args=(pending-transcript --project-root "$PROJECT_ROOT" --pane "$TMUX_PANE")
    [ -n "$SESSION_ID" ] && pending_args+=(--session-id "$SESSION_ID")
    TRANSCRIPT="$("$PYTHON" "$STATE_PY" "${pending_args[@]}")" || TRANSCRIPT=""
    if [ -n "$TRANSCRIPT" ]; then
      transcript_abs="$(absolute_path "$TRANSCRIPT")" \
        || die 6 "cannot resolve the pending marker's transcript $TRANSCRIPT to an absolute path: the folder it is relative to is gone; nothing opened"
      TRANSCRIPT="$transcript_abs"
    fi
  fi
  [ -n "$TRANSCRIPT" ] || die 6 "--skip-wrap-reason needs the predecessor's transcript for the catch-up wrap: no --transcript and no transcript_path in the pending marker of pane $TMUX_PANE; nothing opened"
fi

compose_args=(compose --handoff "$HANDOFF" --old-window "$OLD_WIN" --out-dir "$RUN_DIR"
              --max-age-minutes "$MAX_AGE_MIN" --project-root "$PROJECT_ROOT")
[ "$SKIP_WRAP" -eq 1 ] && compose_args+=(--skip-wrap-reason "$SKIP_REASON" --transcript "$TRANSCRIPT")
set +e
"$PYTHON" "$STATE_PY" "${compose_args[@]}"
rc=$?
set -e
case "$rc" in
  0) ;;
  6)
    if [ "$SKIP_WRAP" -eq 1 ]; then
      die 6 "the transcript $TRANSCRIPT is not a readable JSONL file (see above); nothing opened"
    fi
    die 6 "handoff $HANDOFF carries no valid wrap proof (see above); nothing opened. Emergency path: --skip-wrap-reason \"<why>\"; run /build-log wrap" ;;
  *) die "$rc" "handoff $HANDOFF does not meet the contract (docs/operations/T0_CONTEXT_ROTATION.md)" ;;
esac

# Ledger first (ADR-005): the skip is on record before any window opens, or nothing opens.
if [ "$SKIP_WRAP" -eq 1 ]; then
  skip_args=(record-skip --project-root "$PROJECT_ROOT" --pane "$TMUX_PANE" --reason "$SKIP_REASON"
             --transcript "$TRANSCRIPT" --handoff "$HANDOFF" --run-dir "$RUN_DIR" --old-window "$OLD_WIN")
  [ -n "$SESSION_ID" ] && skip_args+=(--session-id "$SESSION_ID")
  "$PYTHON" "$STATE_PY" "${skip_args[@]}" \
    || die 6 "the ledger refused the t0_context_rotation_wrap_skipped receipt; an unrecorded skip does not spawn, nothing opened"
  log "wrap skipped (reason: $SKIP_REASON), receipt booked; the successor first runs the catch-up wrap from $TRANSCRIPT"
fi
PROMPT="$(cat "$RUN_DIR/prompt.txt")"
HAS_GOAL=0
[ -f "$RUN_DIR/goal.txt" ] && HAS_GOAL=1

# The start folder. T0 folder only when it carries a CLAUDE.md and its `.claude` is the project's
# `.claude`; every other case is the project-root start of old, with one log line.
phys() { [ -n "$1" ] || return 0; (cd -- "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"; }
T0_DIR="$PROJECT_ROOT/.claude/terminals/T0"
START_DIR="$PROJECT_ROOT"
START_IN_T0=0
if [ "$START_MODE" = "root" ]; then
  log "start folder: project root $PROJECT_ROOT (VNX_T0_ROTATE_START_DIR=root)"
elif [ ! -f "$T0_DIR/CLAUDE.md" ]; then
  log "start folder: project root $PROJECT_ROOT (no $T0_DIR/CLAUDE.md: this project has no T0 folder)"
else
  t0_link="$T0_DIR/.claude"
  root_claude="$PROJECT_ROOT/.claude"
  if [ -d "$t0_link" ] && [ "$(phys "$t0_link")" = "$(phys "$root_claude")" ]; then
    START_DIR="$T0_DIR"
    START_IN_T0=1
    [ -e "$T0_DIR/.vnx-data" ] || log "start folder: $T0_DIR has no .vnx-data link; starting there anyway (the T0 hooks resolve the data dir from the project root)"
  elif [ -L "$t0_link" ]; then
    log "WARNING: $t0_link does not resolve to $root_claude; starting in the project root $PROJECT_ROOT. Repair (not run): ln -sfn $root_claude $t0_link"
  elif [ -d "$t0_link" ]; then
    log "WARNING: $t0_link is a plain directory, not a link to $root_claude; starting in the project root $PROJECT_ROOT. Repair (not run, only with no session running in $T0_DIR): mv $t0_link $t0_link.replaced-$(date +%Y%m%d) && ln -s $root_claude $t0_link"
  else
    log "WARNING: $t0_link is missing; starting in the project root $PROJECT_ROOT. Repair (not run): ln -s $root_claude $t0_link"
  fi
fi
if [ "$START_IN_T0" -eq 1 ]; then
  old_path="$(phys "$(tmux display-message -p -t "$TMUX_PANE" '#{pane_current_path}' 2>/dev/null || true)")"
  if [ "$old_path" != "$(phys "$START_DIR")" ]; then
    log "start folder correction: the old session runs in $old_path, the successor starts in the T0 folder $START_DIR"
  fi
fi

launch="claude --model opus"
case "$RC_MODE" in
  on) launch="$launch --remote-control $(printf '%q' "$OLD_NAME")" ;;
  auto) [ -n "${CLAUDE_CODE_BRIDGE_SESSION_ID:-}" ] && launch="$launch --remote-control $(printf '%q' "$OLD_NAME")" ;;
esac
if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
  launch="CLAUDE_CONFIG_DIR=$(printf '%q' "$CLAUDE_CONFIG_DIR") $launch"
fi
# `cd --` first, so a shell rc that changes directory cannot move the session out of the T0 folder.
[ "$START_IN_T0" -eq 1 ] && launch="cd -- $(printf '%q' "$START_DIR") && $launch"

NEW_WIN="$(tmux new-window -a -t "$OLD_WIN" -c "$START_DIR" -n "$OLD_NAME" -P -F '#{window_id}')"
[ -n "$NEW_WIN" ] || die 5 "tmux new-window returned no window id"
log "old_window=$OLD_WIN new_window=$NEW_WIN name=$OLD_NAME goal_followup=$HAS_GOAL"

type_line "$NEW_WIN" "$launch"
if ! wait_for_screen "$NEW_WIN" "$FOOTER_PATTERN" "$FOOTER_TIMEOUT"; then
  die 5 "claude did not show its footer in $NEW_WIN within ${FOOTER_TIMEOUT}s; nothing sent, old window $OLD_WIN left running"
fi
sleep "$SETTLE"
if [ "$START_IN_T0" -eq 1 ]; then
  new_path="$(phys "$(tmux display-message -p -t "$NEW_WIN" '#{pane_current_path}' 2>/dev/null || true)")"
  if [ "$new_path" != "$(phys "$START_DIR")" ]; then
    tmux kill-window -t "$NEW_WIN" || log "could not close $NEW_WIN; close it by hand"
    die 5 "successor started in $new_path instead of the T0 folder $START_DIR; window $NEW_WIN closed, nothing sent, old window $OLD_WIN left running (VNX_T0_ROTATE_START_DIR=root starts in the project root)"
  fi
fi
type_line "$NEW_WIN" "$PROMPT"

latch_args=(latch --project-root "$PROJECT_ROOT" --pane "$TMUX_PANE" --old-window "$OLD_WIN"
            --new-window "$NEW_WIN" --handoff "$HANDOFF")
[ -n "$SESSION_ID" ] && latch_args+=(--session-id "$SESSION_ID")
[ "$HAS_GOAL" -eq 1 ] && latch_args+=(--goal-followup)
"$PYTHON" "$STATE_PY" "${latch_args[@]}" >/dev/null || log "latch/receipt write failed; the guard may nag this session once more"

if [ "$HAS_GOAL" -eq 1 ]; then
  followup="VNX_T0_ROTATE_POLL_SECONDS=$POLL VNX_T0_ROTATE_SETTLE_SECONDS=$SETTLE"
  followup="$followup VNX_T0_ROTATE_FOOTER_TIMEOUT=$FOOTER_TIMEOUT VNX_T0_ROTATE_TURN_TIMEOUT=$TURN_TIMEOUT"
  followup="$followup VNX_T0_ROTATE_GOAL_CONFIRM_TIMEOUT=$GOAL_CONFIRM_TIMEOUT"
  followup="$followup VNX_T0_ROTATE_FOOTER_PATTERN=$(printf '%q' "$FOOTER_PATTERN")"
  followup="$followup VNX_T0_ROTATE_BUSY_PATTERN=$(printf '%q' "$BUSY_PATTERN")"
  followup="$followup VNX_T0_ROTATE_GOAL_ACTIVE_PATTERN=$(printf '%q' "$GOAL_ACTIVE_PATTERN")"
  followup="$followup VNX_PYTHON=$(printf '%q' "$PYTHON")"
  [ -n "${VNX_T0_ROTATION_STATE_DIR:-}" ] && followup="$followup VNX_T0_ROTATION_STATE_DIR=$(printf '%q' "$VNX_T0_ROTATION_STATE_DIR")"
  [ -n "${VNX_T0_ROTATION_RECEIPTS_FILE:-}" ] && followup="$followup VNX_T0_ROTATION_RECEIPTS_FILE=$(printf '%q' "$VNX_T0_ROTATION_RECEIPTS_FILE")"
  followup="$followup bash $(printf '%q' "$SCRIPT_PATH") --goal-followup --target $(printf '%q' "$NEW_WIN")"
  followup="$followup --goal-file $(printf '%q' "$RUN_DIR/goal.txt") --event-file $(printf '%q' "$RUN_DIR/goal.txt")"
  followup="$followup >>$(printf '%q' "$RUN_DIR/goal_followup.log") 2>&1"
  if tmux run-shell -b "$followup"; then
    log "goal follow-up handed to the tmux server (log: $RUN_DIR/goal_followup.log)"
  else
    log "WARNING: tmux refused the goal follow-up; type it yourself in $NEW_WIN: $(cat "$RUN_DIR/goal.txt")"
  fi
fi

printf 'new_window=%s old_window=%s goal_followup=%s run_dir=%s start_dir=%q\n' "$NEW_WIN" "$OLD_WIN" "$HAS_GOAL" "$RUN_DIR" "$START_DIR"
