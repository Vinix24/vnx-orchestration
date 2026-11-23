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
# Background build (OI-3056): the build runs detached through
# scripts/lib/state_rebuild_trigger.py, so the hook returns within 2 s and
# always exits 0. A broken build is logged by the trigger's reaper (APPENDED
# to build_t0_state.err with a UTC timestamp per incident; D1 split rc=1 =
# built but system_health degraded/failed, other non-zero = failed build), not
# on this hook's stderr: by then the hook has long returned. A missing builder
# is still reported here on stderr.
#
# Uses an explicit interpreter: bare `python3` can be relinked out from under
# the hook (OI-852/OI-857); lib_hook_python.sh `vnx_hook_python` resolves it.

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

source "$HOOK_DIR/lib_hook_python.sh"
PY="$(vnx_hook_python "$ENGINE_ROOT")"

# ── Skip the build in a worker session ────────────────────────────────
# Every headless builder is a Claude session in a worktree of this repo and
# so fires this SessionStart hook, but a builder never reads t0_state.json /
# t0_index.json: with several builders at once the parallel builds only load
# the machine. Ask the one classifier (actor_class.derive, ADR-056) for the
# class of THIS session, through the same interpreter and PYTHONPATH the
# builder gets. Only the exact class `worker` skips; t0, operator, automation
# and unknown build as before. The t0-over-dispatch-marker order lives in
# derive(); it is not reimplemented here.
# Fail-open to the current behaviour: if the classification itself fails
# (non-zero exit, no output, import error) the build runs as today.
_ACTOR_CLASS="$(PYTHONPATH="$ENGINE_ROOT/scripts/lib:${PYTHONPATH:-}" \
    "$PY" -c 'from actor_class import derive; print(derive())' 2>/dev/null)" || _ACTOR_CLASS=""
if [ "$_ACTOR_CLASS" = "worker" ]; then
    printf '[vnx] build_t0_state_hook: skipped: worker session %s\n' \
        "${VNX_CURRENT_DISPATCH_ID:-${VNX_DISPATCH_ID:-}}" >&2
    exit 0
fi

# ── Start the build in the background, then return ─────────────────────
# The build takes 101-124 s (measured 2026-11-21), far past any hook budget,
# so it must never run in the foreground: the harness cut the hook off before
# the build finished and nothing was logged. The hook now starts ONE detached
# trigger and returns within 2 s (its settings.json entry carries timeout 10).
#
# The trigger (scripts/lib/state_rebuild_trigger.py) owns the throttle, the
# state-dir resolution and the failure logging: with --wait its process joins
# the reaper, so a failed build still appends an incident to
# build_t0_state.err (rc=1 = built but system_health degraded/failed, any
# other non-zero rc = failed build). That logging lives only there.
# `session_start` is deliberately NOT in CRITICAL_EVENTS: it honours the 30 s
# throttle, so a session start right after another rebuild starts nothing.
# stdin/stdout/stderr are redirected away so the harness does not wait on an
# open pipe; the hook always exits 0.
"$PY" "$ENGINE_ROOT/scripts/lib/state_rebuild_trigger.py" --event session_start --wait \
    </dev/null >/dev/null 2>&1 &
disown 2>/dev/null || true
printf '[vnx] build_t0_state_hook: background rebuild of t0_state.json started (failures are logged to build_t0_state.err)\n' >&2

exit 0
