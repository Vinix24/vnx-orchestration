#!/usr/bin/env bash
# VNX SessionStart auto-close tick
#
# Fires on Claude Code's SessionStart hook for the interactive T0/operator
# session. Runs `objective reconcile` so tracks whose PRs merged since the last
# session close automatically, keeping the horizon in sync with git reality
# without a long-running supervisor.
#
# WHY a session hook and not a cron/launchd agent: `gh` auth is keyring-backed
# on this fleet (no GH_TOKEN), so a headless launchd context cannot authenticate
# the `gh pr view` calls reconcile depends on — every PR would come back
# `unverified` and nothing would close. The interactive session HAS keychain
# access, so this is the reliable context. Session-start is also the moment a
# synced horizon matters most (kickoff).
#
# SAFETY — auto-close is ON BY DEFAULT (operator directive 2026-07-10). reconcile
# only ever closes tracks whose linked PRs are verified MERGED on GitHub (provenance
# chain), so applying by default keeps the horizon in sync with git reality without
# waiting for the trust streak. Opt out with VNX_AUTO_CLOSE=0 → advisory CHECK (zero
# writes). The reconcile-streak is still computed + logged for observability, but no
# longer gates the flip. reconcile's own two-stage close (CONFIRMED → stale_candidate
# → closed) remains the conservative safeguard against a single mis-verify.
#
# Scoped to the interactive session: fires ONLY when VNX_DISPATCH_ID is UNSET.
# A tmux-spawn worker (VNX_DISPATCH_ID set) drains stdin and exits 0 — no-op.
#
# Detached (nohup + background) so it never blocks session start. Always exit 0.

# Drain the hook's stdin JSON so the caller never blocks.
cat >/dev/null 2>&1 || true

# ── Guard: skip tmux-spawn workers; only the interactive session ticks ───────
if [ -n "${VNX_DISPATCH_ID:-}" ]; then
    exit 0
fi

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo .)"
CLI="$ROOT/scripts/planning_cli.py"
LOG_DIR="$ROOT/.vnx-data/logs"
LOG="$LOG_DIR/objective_reconcile.log"

# No CLI, nothing to do.
[ -f "$CLI" ] || exit 0
mkdir -p "$LOG_DIR" 2>/dev/null || true

# Run the whole tick detached so session start never waits on gh network calls.
(
    # Resolve project_id the same way the CLI does (git remote / .vnx-project-id),
    # falling back to the reconcile default. Empty is fine — the CLI resolves it.
    PID="${VNX_PROJECT_ID:-}"
    PID_ARGS=()
    [ -n "$PID" ] && PID_ARGS=(--project-id "$PID")

    # Auto-close ON BY DEFAULT (operator directive 2026-07-10): apply unless the operator
    # opts out with VNX_AUTO_CLOSE=0. reconcile only closes tracks whose PRs are verified
    # MERGED, so this keeps the horizon in sync with git without waiting for the trust streak.
    if [ "${VNX_AUTO_CLOSE:-1}" = "0" ]; then
        MODE="check"
    else
        MODE="apply"
    fi

    # Streak is still computed + logged for observability (no longer gates the flip).
    STREAK_MET="?"
    if python3 "$CLI" objective reconcile-streak "${PID_ARGS[@]}" --json 2>/dev/null \
        | python3 -c 'import sys,json;
d=json.load(sys.stdin);
sys.exit(0 if d.get("flip_criterion_met") else 1)' 2>/dev/null; then
        STREAK_MET="yes"
    else
        STREAK_MET="no"
    fi

    STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "[$STAMP] session-reconcile tick: mode=$MODE streak_met=$STREAK_MET" >>"$LOG" 2>&1

    if [ "$MODE" = "apply" ]; then
        python3 "$CLI" objective reconcile "${PID_ARGS[@]}" --apply --repo-root "$ROOT" >>"$LOG" 2>&1
    else
        python3 "$CLI" objective reconcile "${PID_ARGS[@]}" --repo-root "$ROOT" >>"$LOG" 2>&1
    fi
) </dev/null >/dev/null 2>&1 &

exit 0
