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
# OI-1073: this hook ships as an INSTALL ARTEFACT, not a fabric source path.
# It is registered in consumer repos via the install templates
# (templates/settings_vnx_keys.json.tmpl + templates/init/*/settings.json.j2)
# as an engine-relative path, and the wheel ships it at
#   <site-packages>/vnx_orchestration/scripts/hooks/build_t0_state_hook.sh
# The builder is resolved through the INSTALLED ENGINE (the same tree this
# hook lives in), never through a repo-relative `scripts/` path — a consumer
# (pip install) has no `scripts/` next to its project root, so a
# `$PROJECT_ROOT/scripts/build_t0_state.py` reference would be a dead link
# and the projection would silently never refresh (the bug this fixes).
#
# Failure visibility (OI-1073 defect 2): a broken build must be VISIBLE, not
# silent. The hook writes a one-line failure to its OWN stderr, which the
# harness surfaces on the session that fired it, while still exiting 0 so a
# stale state file never blocks a session from starting. The full builder
# traceback still goes to the central logs dir for diagnosis.
#
# D1: that central log (build_t0_state.err) used to be overwritten on every
# run (`2>"$ERR_LOG"`), so it could only ever show the LAST failure — no way
# to tell "failed once" from "fails every session". Failures are now
# APPENDED with a UTC timestamp per incident. D1 also distinguishes the
# builder's two non-zero exit codes: rc=1 means the build succeeded and
# wrote a fresh t0_state.json but system_health itself is degraded/failed
# (a fabric-health judgment); any other non-zero rc means the build raised
# before a state file could be produced (the true "not refreshed" case).
#
# Uses an explicit interpreter: bare `python3` can be relinked out from under
# the hook (OI-852/OI-857) — repo venv > pinned homebrew 3.12 > bare python3.

set -u

# ── Resolve the INSTALLED ENGINE root (this hook's own tree) ───────────
# This hook lives at <engine>/scripts/hooks/build_t0_state_hook.sh, so the
# engine root is two levels up from the script's real location (symlink-
# resolved). That is the same tree the `vnx` binary resolves its scripts/
# from, so the builder reached here is the one the running engine ships —
# never a repo-relative `scripts/` path that may not exist in a consumer.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ENGINE_ROOT="$(cd "$HOOK_DIR/../.." && pwd -P)"

# Sanity guard: refuse to run a stale/missing builder rather than silently
# doing nothing. If the engine tree is somehow broken the failure must be
# visible (OI-1073 defect 2), not swallowed.
if [ ! -f "$ENGINE_ROOT/scripts/build_t0_state.py" ]; then
    printf '[vnx] build_t0_state_hook: builder not found under engine %s (t0_state.json not refreshed)\n' \
        "$ENGINE_ROOT" >&2
    exit 0
fi

PY="$ENGINE_ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
[ -x "$PY" ] || PY="python3"

# Resolve the CENTRAL state + logs dirs via the same resolver the CLI uses.
# The bash resolver (vnx_paths.sh) applies a worktree override that points at
# the repo-local `.vnx-data`; the python resolver keeps the store central,
# which is what the guard reads — so resolve through python.
PATHS="$("$PY" -c "import sys; sys.path.insert(0, '$ENGINE_ROOT/scripts/lib'); from vnx_paths import ensure_env; p=ensure_env(); print(p['VNX_STATE_DIR']); print(p['VNX_LOGS_DIR'])" 2>/dev/null)"
STATE="$(printf '%s\n' "$PATHS" | sed -n 1p)"
LOG="$(printf '%s\n' "$PATHS" | sed -n 2p)"
[ -n "$STATE" ] || STATE="$ENGINE_ROOT/.vnx-data/state"
[ -n "$LOG" ]   || LOG="${STATE%/state}/logs"

mkdir -p "$LOG" 2>/dev/null || true

# ── Run the builder: visible failure, non-blocking exit ───────────────
# Capture the builder's stderr to a per-run scratch file, then APPEND only
# the failure incidents to the central log with an incident timestamp (D1:
# this used to be a plain `2>"$ERR_LOG"`, i.e. overwrite-on-every-run — the
# one artefact that could show HOW OFTEN this fails ever showed just the
# last occurrence, 1 line / 102 bytes no matter how many times it happened).
# A concise line still goes to the hook's OWN stderr so it lands on the
# session that fired the hook. The hook still exits 0: a stale/broken state
# file must never block a session.
#
# D1 also splits build_t0_state.py main()'s two non-zero outcomes: rc=1
# means the build SUCCEEDED and t0_state.json WAS written, but
# system_health.status itself is degraded/failed -- a fabric-health
# judgment, not a refresh failure. Any other non-zero rc means the build
# raised before a state file could be produced. Reporting both as "t0_state
# not refreshed" would turn this line into a loud falsehood the moment a
# healthy build routinely carries a degraded system_health.
ERR_LOG="$LOG/build_t0_state.err"
_RUN_STDERR="$LOG/.build_t0_state.err.tmp.$$"
BUILD_RC=0
PYTHONPATH="$ENGINE_ROOT/scripts/lib:${PYTHONPATH:-}" \
    "$PY" "$ENGINE_ROOT/scripts/build_t0_state.py" --output "$STATE/t0_state.json" \
    2>"$_RUN_STDERR" || BUILD_RC=$?

if [ "$BUILD_RC" -ne 0 ]; then
    if [ -s "$_RUN_STDERR" ]; then
        {
            printf '===== %s (rc=%s) =====\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$BUILD_RC"
            cat "$_RUN_STDERR"
        } >> "$ERR_LOG"
    fi
    if [ "$BUILD_RC" -eq 1 ]; then
        # Build OK, state file IS fresh -- rc=1 here is a fabric-health
        # signal (system_health.status degraded/failed), not a stale read.
        printf '[vnx] build_t0_state_hook: build OK, t0_state.json IS refreshed, but system_health is degraded/failed (rc=%s; full output: %s)\n' \
            "$BUILD_RC" "$ERR_LOG" >&2
    else
        # Surface the failure on the session's own output. Keep it to the
        # last line of the builder's stderr so it is concise but carries the
        # real cause (the full traceback stays in $ERR_LOG for follow-up).
        _last_err="$(tail -n 1 "$_RUN_STDERR" 2>/dev/null | tr -d '\r')"
        if [ -n "$_last_err" ]; then
            printf '[vnx] build_t0_state_hook FAILED (rc=%s): t0_state.json not refreshed. %s (full output: %s)\n' \
                "$BUILD_RC" "$_last_err" "$ERR_LOG" >&2
        else
            printf '[vnx] build_t0_state_hook FAILED (rc=%s): t0_state.json not refreshed (full output: %s)\n' \
                "$BUILD_RC" "$ERR_LOG" >&2
        fi
    fi
fi
rm -f "$_RUN_STDERR" 2>/dev/null || true

exit 0
