#!/usr/bin/env bash
# Regression test for cross-project pane discovery leak.
# When PROJECT_ROOT is set and no pane matches in the own tmux session,
# discover_pane_by_title must NOT fall back to a global tmux scan.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$SCRIPT_DIR/scripts/pane_manager_v2.sh"

fail=0
pass=0

_assert() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "ok   — $name"
        pass=$((pass+1))
    else
        echo "FAIL — $name (expected='$expected' actual='$actual')"
        fail=$((fail+1))
    fi
}

# Stub tmux: returns panes from a fake foreign session only (no own session).
tmux() {
    case "$*" in
        "has-session -t vnx-dummy-project"*) return 1 ;;
        "list-panes -s -t vnx-dummy-project"*) return 0 ;;
        "list-panes -a -F #{pane_id} #{pane_title}")
            printf '%s\n' "%99 T0" ;;
        "list-panes -a -F #{pane_id} #{pane_current_path}")
            printf '%s\n' "%99 /Users/foreign/project/.claude/terminals/T0" ;;
        "list-panes -a -F"*session_attached*)
            printf '%s\n' "%99 1 /Users/foreign/project/.claude/terminals/T0" ;;
        *) return 0 ;;
    esac
}
export -f tmux

# Case 1: PROJECT_ROOT set, no own-session pane → must return empty (no cross-project leak)
export PROJECT_ROOT="/Users/me/my-project"
result=$(discover_pane_by_title T0 2>/dev/null || echo "")
_assert "title: PROJECT_ROOT set + no own pane → empty" "" "$result"

# Case 2: PROJECT_ROOT unset → legacy global fallback allowed (backward compat)
unset PROJECT_ROOT
result=$(discover_pane_by_title T0 2>/dev/null || echo "")
_assert "title: PROJECT_ROOT unset → global fallback OK" "%99" "$result"

echo "---"
echo "$pass passed, $fail failed"
exit $fail
