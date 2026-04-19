#!/usr/bin/env bash
# dispatch_guard.sh
# Read-only pre-dispatch guard for T0.
# Examples:
#   bash .claude/skills/t0-orchestrator/scripts/dispatch_guard.sh
#   bash .claude/skills/t0-orchestrator/scripts/dispatch_guard.sh json
#
# Exit codes:
#   0 = GO (safe to dispatch)
#   2 = WAIT (degraded, divergence, busy terminal, or active/pending queue)
#   1 = error (missing state)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
T0_BRIEF="$REPO_ROOT/.vnx-data/state/t0_brief.json"
TERMINAL_STATE="$REPO_ROOT/.vnx-data/state/terminal_state.json"

# Reason string set by evaluate_state() when it returns non-zero.
# Consumed by print_human() and print_json() for operator-visible diagnostics.
_GUARD_REASON=""

usage() {
    cat <<'USAGE'
Usage:
  dispatch_guard.sh         Human-readable go/no-go decision
  dispatch_guard.sh json    JSON go/no-go decision

Exit codes:
  0 = GO (safe to dispatch)
  2 = WAIT (degraded, divergence, busy terminal, or active/pending queue)
  1 = error (missing state)
USAGE
}

require_state() {
    if [[ ! -f "$T0_BRIEF" ]]; then
        echo "Missing file: $T0_BRIEF" >&2
        exit 1
    fi
}

evaluate_state() {
    _GUARD_REASON=""
    local busy_count pending_count active_count conflict_count

    # Check 1 (postmortem §4.2): brief.system_health.status == degraded → WAIT
    # Ignoring this flag was part of the 2026-04-16 60h freeze.
    local degraded_status
    degraded_status=$(jq -r '.system_health.status // "healthy"' "$T0_BRIEF")
    if [[ "$degraded_status" == "degraded" ]]; then
        local reasons
        reasons=$(jq -r '.system_health.warnings // [] | join(",")' "$T0_BRIEF" 2>/dev/null || echo "")
        _GUARD_REASON="brief_degraded: ${reasons:-no_details}"
        return 2
    fi

    # Check 2 (postmortem §4.2): brief vs terminal_state divergence → WAIT
    # Same freeze: brief said working (stale), state said idle, guard said GO.
    if [[ -f "$TERMINAL_STATE" ]]; then
        local t brief_status state_status
        for t in T1 T2 T3; do
            brief_status=$(jq -r ".terminals.${t}.status // \"unknown\"" "$T0_BRIEF")
            state_status=$(jq -r ".terminals.${t}.status // \"unknown\"" "$TERMINAL_STATE")
            if [[ "$brief_status" != "unknown" && "$state_status" != "unknown" && "$brief_status" != "$state_status" ]]; then
                _GUARD_REASON="state_divergence: ${t} brief=${brief_status} state=${state_status}"
                return 2
            fi
        done
    fi

    # Existing checks: busy terminal / queue / conflicts
    busy_count=$(jq -r '[.terminals | to_entries[] | select(.value.status == "working" or .value.status == "blocked")] | length' "$T0_BRIEF")
    pending_count=$(jq -r '.queues.pending // 0' "$T0_BRIEF")
    active_count=$(jq -r '.queues.active // 0' "$T0_BRIEF")
    conflict_count=$(jq -r '.queues.conflicts // 0' "$T0_BRIEF")

    if (( busy_count > 0 || pending_count > 0 || active_count > 0 )); then
        _GUARD_REASON="busy: terminals=${busy_count} pending=${pending_count} active=${active_count}"
        return 2
    fi

    if (( conflict_count > 0 )); then
        _GUARD_REASON="conflicts: ${conflict_count}"
        return 2
    fi

    return 0
}

print_human() {
    local queue_line terminal_lines

    queue_line=$(jq -r '.queues | "pending=\(.pending // 0) active=\(.active // 0) conflicts=\(.conflicts // 0)"' "$T0_BRIEF")
    terminal_lines=$(jq -r '.terminals | to_entries[] | "\(.key)=\(.value.status)(\(.value.status_age_seconds // 0)s)"' "$T0_BRIEF")

    if evaluate_state; then
        echo "GO: safe to dispatch"
    else
        echo "WAIT: ${_GUARD_REASON:-terminals or queue are not idle}"
    fi

    echo "Queue: $queue_line"
    echo "Terminals:"
    echo "$terminal_lines"
}

print_json() {
    local decision
    if evaluate_state; then
        decision="GO"
    else
        decision="WAIT"
    fi

    jq -n \
        --arg decision "$decision" \
        --arg reason "$_GUARD_REASON" \
        --argjson queues "$(jq '.queues' "$T0_BRIEF")" \
        --argjson terminals "$(jq '.terminals' "$T0_BRIEF")" \
        '{decision: $decision, reason: $reason, queues: $queues, terminals: $terminals}'
}

main() {
    local mode="${1:-human}"

    require_state

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

    evaluate_state
}

main "$@"
