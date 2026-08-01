#!/usr/bin/env bash
# build_t0_state_hook.sh — SessionStart hook that refreshes the T0 state
# projection into the CENTRAL store.
#
# OI-859 direction B: the guard and `vnx status --json` read RUNTIME state, so
# t0_state.json must land where the runtime resolver points (ADR-026 central
# ~/.vnx-data/<project>/state), not a repo-local `.vnx-data`. The old inline
# hook forced the output to a repo-local state path (while every other write
# went central) — the split-brain that left the guard reading an absent file.
#
# Uses an explicit interpreter: bare `python3` can be relinked out from under
# the hook (OI-852/OI-857) — repo venv > pinned homebrew 3.12 > bare python3.
# Never blocks the session: stderr is captured to the central logs dir and the
# hook always exits 0.

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
[ -x "$PY" ] || PY="python3"

# Resolve the CENTRAL state + logs dirs via the same resolver the CLI uses.
# The bash resolver (vnx_paths.sh) applies a worktree override that points at
# the repo-local `.vnx-data`; the python resolver keeps the store central,
# which is what the guard reads — so resolve through python.
PATHS="$("$PY" -c "import sys; sys.path.insert(0, '$ROOT/scripts/lib'); from vnx_paths import ensure_env; p=ensure_env(); print(p['VNX_STATE_DIR']); print(p['VNX_LOGS_DIR'])" 2>/dev/null)"
STATE="$(printf '%s\n' "$PATHS" | sed -n 1p)"
LOG="$(printf '%s\n' "$PATHS" | sed -n 2p)"
[ -n "$STATE" ] || STATE="$ROOT/.vnx-data/state"
[ -n "$LOG" ] || LOG="${STATE%/state}/logs"

mkdir -p "$LOG" 2>/dev/null || true
PYTHONPATH="$ROOT/scripts/lib:${PYTHONPATH:-}" \
    "$PY" "$ROOT/scripts/build_t0_state.py" --output "$STATE/t0_state.json" \
    2>"$LOG/build_t0_state.err"
exit 0
