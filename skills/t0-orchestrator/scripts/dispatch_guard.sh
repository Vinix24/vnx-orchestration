#!/usr/bin/env bash
# dispatch_guard.sh
# Read-only pre-dispatch guard for T0.
# Direction B (OI-859): reads RUNTIME state via the vnx CLI (vnx status --json,
# vnx pool status --json) instead of the repo-local t0_brief.json presentation
# cache. The brief stays a presentation layer; no decision reads it.
#
# Examples:
#   bash skills/t0-orchestrator/scripts/dispatch_guard.sh
#   bash skills/t0-orchestrator/scripts/dispatch_guard.sh json
#
# Exit codes:
#   0 = GO (safe to dispatch)
#   2 = WAIT (degraded, live work in the air, pending queue, or conflicts)
#   1 = error (missing state)
#
# D5 (fabric-state-herstel): "is there work in the air" comes from
# .live_work (dispatches table + occupancy flock), not from .terminals or
# .queues.active_count, which the status snapshot no longer carries. Every
# live, starting, stale or unmeasured row is WAIT; a missing or unavailable
# live_work is Missing state, never GO.

set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
    echo "error: dispatch_guard.sh requires jq" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Robust root resolution: git top-level works from nested worktrees. The legacy
# four-parent walk overshoots by one level in a worktree topology
# (docs/operations/RUNTIME_LIVENESS.md §5).
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$(cd "$SCRIPT_DIR/../../../.." && pwd)")"

# Reason string set by evaluate_state() when it returns non-zero.
# Consumed by print_human() and print_json() for operator-visible diagnostics.
_GUARD_REASON=""
_STATUS_JSON=""

usage() {
    cat <<'USAGE'
Usage:
  dispatch_guard.sh         Human-readable go/no-go decision
  dispatch_guard.sh json    JSON go/no-go decision

Exit codes:
  0 = GO (safe to dispatch)
  2 = WAIT (degraded, live work in the air, pending queue, or conflicts)
  1 = error (missing state)
USAGE
}

# Schema the guard's checks are written against. A CLI that answers anything
# else (the pip package's `active_dispatches`/`agents` shape) is refused.
_EXPECTED_SCHEMA="vnx_status/1.0"

# Resolve the vnx CLI to run, first executable wins: the repo's own bin/vnx, the
# install named by VNX_HOME, the central install ~/.vnx-system/current, an explicit
# VNX_BIN, then `vnx` on PATH. A consumer has no bin/vnx, and the `vnx` on its PATH
# may be the pip package, which does not speak vnx_status/1.0; the installs come
# before it so a consumer reads the schema the checks expect.
_resolve_vnx() {
    local candidate
    for candidate in \
        "$REPO_ROOT/bin/vnx" \
        "${VNX_HOME:+$VNX_HOME/bin/vnx}" \
        "${HOME:+$HOME/.vnx-system/current/bin/vnx}" \
        "${VNX_BIN:-}"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    command -v vnx || true
}

# Run the resolved CLI bound to REPO_ROOT. `vnx status` has no --project-dir: it
# resolves the project from the cwd and from VNX_PROJECT_ROOT, so both are pinned.
# An unbound call answers for whatever directory (or inherited root) the caller has.
_vnx_in_repo() {
    local bin="$1"
    shift
    (cd "$REPO_ROOT" && VNX_PROJECT_ROOT="$REPO_ROOT" "$bin" "$@" 2>/dev/null)
}

# Fetch the runtime snapshot. Empty stdout / non-zero exit = the CLI produced no
# snapshot (state not built or CLI missing) -> callers fail closed (exit 1).
_fetch_status_json() {
    local bin
    bin="$(_resolve_vnx)"
    [ -n "$bin" ] || return 1
    _vnx_in_repo "$bin" status --json
}

# Best-effort pool info for the human view. Never gates the decision.
_fetch_pool_json() {
    local bin
    bin="$(_resolve_vnx)"
    [ -n "$bin" ] || return 1
    _vnx_in_repo "$bin" pool status --json --project-dir "$REPO_ROOT"
}

_LIVE_BUCKETS_JQ='[.live_work.live, .live_work.starting, .live_work.stale, .live_work.unmeasured]'

# "live=N starting=N stale=N unmeasured=N", counted from the row lists so a
# stale .counts block can never under-report.
_live_counts() {
    jq -r '.live_work | "live=\(.live | length) starting=\(.starting | length) stale=\(.stale | length) unmeasured=\(.unmeasured | length)"' <<<"$1"
}

# Empty output = live_work usable. Otherwise the reason it is not.
_live_work_problem() {
    jq -r '
        if (.live_work | type) != "object" or (.live_work | length) == 0 then
            "live_work missing"
        elif .live_work.available != true then
            "live_work unavailable: " + (.live_work.reason // "no reason given" | tostring)
        elif ([.live_work.live, .live_work.starting, .live_work.stale, .live_work.unmeasured]
              | all(type == "array") | not) then
            "live_work lacks a live/starting/stale/unmeasured list"
        else
            empty
        end
    ' <<<"$1"
}

evaluate_state() {
    _GUARD_REASON=""
    local status_json="$1"

    # Check 1 (postmortem §4.2): system_health.status == degraded → WAIT.
    # Ignoring this flag was part of the 2026-04-16 60h freeze.
    local sh_status
    sh_status=$(jq -r '.system_health.status // "healthy"' <<<"$status_json")
    if [ "$sh_status" = "degraded" ] || [ "$sh_status" = "failed" ]; then
        _GUARD_REASON="system_degraded: ${sh_status}"
        return 2
    fi

    # Check 2: any in-flight dispatch row → WAIT. unmeasured means the lock
    # probe raised: not measurable, so not GO. main() has already refused a
    # snapshot whose live_work is missing, unavailable or lacks a bucket.
    local live_total
    live_total=$(jq -r "$_LIVE_BUCKETS_JQ | map(length) | add" <<<"$status_json")
    if [ "$live_total" -gt 0 ]; then
        _GUARD_REASON="busy: live_work $(_live_counts "$status_json")"
        return 2
    fi

    # Check 3: pending dispatch queue → WAIT.
    local pending_count
    pending_count=$(jq -r '.queues.pending_count // 0' <<<"$status_json")
    if [ "$pending_count" -gt 0 ]; then
        _GUARD_REASON="queue: pending=${pending_count}"
        return 2
    fi

    # Check 4: conflicts → WAIT.
    local conflict_count
    conflict_count=$(jq -r '.queues.conflict_count // 0' <<<"$status_json")
    if [ "$conflict_count" -gt 0 ]; then
        _GUARD_REASON="conflicts: ${conflict_count}"
        return 2
    fi

    return 0
}

print_human() {
    if evaluate_state "$_STATUS_JSON"; then
        echo "GO: safe to dispatch"
    else
        echo "WAIT: ${_GUARD_REASON:-live work or queue are not idle}"
    fi

    echo "Queue: pending=$(jq -r '.queues.pending_count // 0' <<<"$_STATUS_JSON") conflicts=$(jq -r '.queues.conflict_count // 0' <<<"$_STATUS_JSON")"
    echo "Live work: $(_live_counts "$_STATUS_JSON")"
    jq -r '.live_work | ("live", "starting", "stale", "unmeasured") as $b | .[$b][] | "  \($b) \(.dispatch_id // "?") state=\(.state // "?") lock=\(.lock // "?") age=\(.age_seconds // 0)s"' <<<"$_STATUS_JSON"

    local pool
    pool="$(_fetch_pool_json)" || true
    if [ -n "$pool" ]; then
        echo "Pool: current=$(jq -r '.current // "n/a"' <<<"$pool") queue_depth=$(jq -r '.queue_depth // "n/a"' <<<"$pool")"
    fi
}

print_json() {
    local decision
    if evaluate_state "$_STATUS_JSON"; then
        decision="GO"
    else
        decision="WAIT"
    fi

    jq -n \
        --arg decision "$decision" \
        --arg reason "$_GUARD_REASON" \
        --argjson state "$(jq -c '{queues: .queues, live_work: .live_work, system_health: .system_health}' <<<"$_STATUS_JSON")" \
        '{decision: $decision, reason: $reason, queues: $state.queues, live_work: $state.live_work, system_health: $state.system_health}'
}

main() {
    local mode="${1:-human}"

    if ! _STATUS_JSON="$(_fetch_status_json)"; then
        echo "Missing state: unable to read 'vnx status --json'" >&2
        exit 1
    fi

    # Fail closed on a CLI that speaks another schema, and say which CLI and which
    # schema: "live_work missing" would hide that the wrong CLI answered.
    local schema
    schema=$(jq -r '.schema? // "none"' <<<"$_STATUS_JSON" 2>/dev/null || echo "unparseable")
    if [ "$schema" != "$_EXPECTED_SCHEMA" ]; then
        echo "Missing state: $(_resolve_vnx) answered schema '${schema}', the guard needs '${_EXPECTED_SCHEMA}' (vnx status --json)" >&2
        exit 1
    fi

    # Fail closed when the runtime snapshot is not available or exposes no
    # system_health (we cannot confirm the fleet is healthy → error, not GO).
    local t0_avail sh_status
    t0_avail=$(jq -r '.t0_state_available // false' <<<"$_STATUS_JSON")
    if [ "$t0_avail" != "true" ]; then
        echo "Missing state: t0_state.json unavailable via 'vnx status --json'" >&2
        exit 1
    fi
    sh_status=$(jq -r '.system_health.status? // "unavailable"' <<<"$_STATUS_JSON")
    if [ "$sh_status" = "unavailable" ]; then
        echo "Missing state: system_health unavailable via 'vnx status --json'" >&2
        exit 1
    fi
    local live_problem
    live_problem="$(_live_work_problem "$_STATUS_JSON")"
    if [ -n "$live_problem" ]; then
        echo "Missing state: ${live_problem} via 'vnx status --json'" >&2
        exit 1
    fi

    case "$mode" in
        human)
            print_human
            ;;
        json)
            print_json
            ;;
        help|--help|-h)
            usage
            ;;
        *)
            echo "Unknown mode: $mode" >&2
            usage
            exit 1
            ;;
    esac

    evaluate_state "$_STATUS_JSON"
}

main "$@"
