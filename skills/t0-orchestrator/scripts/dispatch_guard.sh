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
#   2 = WAIT (degraded, busy terminal, or active/pending queue)
#   1 = error (missing state)

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
  2 = WAIT (degraded, busy terminal, or active/pending queue)
  1 = error (missing state)
USAGE
}

# Resolve the vnx CLI to run. Prefer the repo's own bin/vnx (schema
# vnx_status/1.0, ships system_health), then an explicit VNX_BIN, then `vnx` on
# PATH for consumers without a local bin/vnx. The co-located bin must win over
# an inherited VNX_BIN so the guard always reads the schema its checks expect.
_resolve_vnx() {
    local bin="$REPO_ROOT/bin/vnx"
    if [ ! -x "$bin" ]; then
        bin="${VNX_BIN:-}"
    fi
    if [ -z "$bin" ] || [ ! -x "$bin" ]; then
        bin="$(command -v vnx || true)"
    fi
    printf '%s' "$bin"
}

# Fetch the runtime snapshot. Empty stdout / non-zero exit = the CLI produced no
# snapshot (state not built or CLI missing) -> callers fail closed (exit 1).
_fetch_status_json() {
    local bin
    bin="$(_resolve_vnx)"
    [ -n "$bin" ] || return 1
    "$bin" status --json 2>/dev/null
}

# Best-effort pool info for the human view. Never gates the decision.
_fetch_pool_json() {
    local bin
    bin="$(_resolve_vnx)"
    [ -n "$bin" ] || return 1
    "$bin" pool status --json --project-dir "$REPO_ROOT" 2>/dev/null
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

    # Check 2: busy terminal (working/blocked) → WAIT.
    local busy_count
    busy_count=$(jq -r '[.terminals | to_entries[] | select(.value.status == "working" or .value.status == "blocked")] | length' <<<"$status_json")
    if [ "$busy_count" -gt 0 ]; then
        _GUARD_REASON="busy: terminals=${busy_count}"
        return 2
    fi

    # Check 3: active/pending dispatch queue → WAIT.
    local pending_count active_count
    pending_count=$(jq -r '.queues.pending_count // 0' <<<"$status_json")
    active_count=$(jq -r '.queues.active_count // 0' <<<"$status_json")
    if [ "$pending_count" -gt 0 ] || [ "$active_count" -gt 0 ]; then
        _GUARD_REASON="queue: pending=${pending_count} active=${active_count}"
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
        echo "WAIT: ${_GUARD_REASON:-terminals or queue are not idle}"
    fi

    echo "Queue: pending=$(jq -r '.queues.pending_count // 0' <<<"$_STATUS_JSON") active=$(jq -r '.queues.active_count // 0' <<<"$_STATUS_JSON") conflicts=$(jq -r '.queues.conflict_count // 0' <<<"$_STATUS_JSON")"
    echo "Terminals:"
    jq -r '.terminals | to_entries[] | "\(.key)=\(.value.status)(\(.value.status_age_seconds // 0)s)"' <<<"$_STATUS_JSON"

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
        --argjson state "$(jq -c '{queues: .queues, terminals: .terminals, system_health: .system_health}' <<<"$_STATUS_JSON")" \
        '{decision: $decision, reason: $reason, queues: $state.queues, terminals: $state.terminals, system_health: $state.system_health}'
}

main() {
    local mode="${1:-human}"

    if ! _STATUS_JSON="$(_fetch_status_json)"; then
        echo "Missing state: unable to read 'vnx status --json'" >&2
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
