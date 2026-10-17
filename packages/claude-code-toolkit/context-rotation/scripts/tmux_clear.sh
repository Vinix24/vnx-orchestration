#!/bin/sh
# The detached job of the automatic /clear (loop step 7).
#
# Usage: tmux_clear.sh <pane> <session_id> <generation> <job_id>
#
# The Stop hook starts it with `tmux run-shell -b` (lib/auto_clear.py), with ids it
# checked against fixed patterns; the project path is in the job file, never on the
# command line. This shim runs lib/tmux_clear.py, which does the checks and the typing.
# It types nothing itself and never exits 2:
#   - CLAUDE_CONTEXT_ROTATION=off (exactly "off"): exit 0, nothing else happens.
#   - a pane id other than "%" and digits: exit 1, nothing is shown in any pane.
#   - python3 or the job missing: one line in that pane's status line, exit 1.
#   - the job's own exit status 2 is mapped to 1; every other status passes through.

if [ "${CLAUDE_CONTEXT_ROTATION}" = "off" ]; then
  exit 0
fi

pane="$1"
case "$pane" in
  "" | % | %*[!0-9]*)
    exit 1
    ;;
  %*)
    ;;
  *)
    exit 1
    ;;
esac

here=$(dirname "$0")
job="$here/../lib/tmux_clear.py"
missing="context-rotation: no automatic /clear: python3 or the plugin's job is missing. Type /clear, then any message, to continue in a fresh context."

if [ ! -f "$job" ] || ! command -v python3 >/dev/null 2>&1; then
  tmux display-message -t "$pane" "$missing" >/dev/null 2>&1
  exit 1
fi

python3 "$job" "$@"
status=$?
if [ "$status" -eq 2 ]; then
  exit 1
fi
exit "$status"
