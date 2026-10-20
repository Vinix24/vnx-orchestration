#!/bin/bash
# Intelligence queries for T0 orchestration
# Live state monitoring and orchestration pattern queries

INTEL_SCRIPT="scripts/gather_intelligence.py"

# Bind to the repo this script ships in, not the caller's cwd.
_INTEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_INTEL_REPO_ROOT="$(git -C "$_INTEL_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$(cd "$_INTEL_DIR/../../../.." && pwd)")"

# Central state/dispatch dirs (ADR-026). An explicit VNX_STATE_DIR / VNX_DISPATCH_DIR
# wins and is never overwritten. Otherwise the central resolver (vnx_paths.py) runs with
# the repo root as cwd and its KEY=VALUE lines are parsed, never eval'd. Unresolvable
# leaves both empty; the functions that need them refuse (_require_dirs).
_intel_resolver() {
    local candidate
    for candidate in \
        "$_INTEL_REPO_ROOT/scripts/lib/vnx_paths.py" \
        "${VNX_HOME:-}/scripts/lib/vnx_paths.py" \
        "${HOME:-}/.vnx-system/current/scripts/lib/vnx_paths.py"; do
        if [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

_intel_resolve_dirs() {
    local resolver line
    STATE_DIR="${VNX_STATE_DIR:-}"
    DISPATCH_DIR="${VNX_DISPATCH_DIR:-}"
    if [ -n "$STATE_DIR" ] && [ -n "$DISPATCH_DIR" ]; then
        return 0
    fi
    resolver="$(_intel_resolver)" || return 0
    while IFS= read -r line; do
        case "$line" in
            VNX_STATE_DIR=*) [ -n "$STATE_DIR" ] || STATE_DIR="${line#VNX_STATE_DIR=}" ;;
            VNX_DISPATCH_DIR=*) [ -n "$DISPATCH_DIR" ] || DISPATCH_DIR="${line#VNX_DISPATCH_DIR=}" ;;
        esac
    done < <(cd "$_INTEL_REPO_ROOT" && python3 "$resolver" 2>/dev/null || true)
    return 0
}
_intel_resolve_dirs

_require_state_dir() {
    [ -n "$STATE_DIR" ] || { echo "Error: VNX state dir unresolved"; return 1; }
}

_require_dispatch_dir() {
    [ -n "$DISPATCH_DIR" ] || { echo "Error: VNX state dir unresolved"; return 1; }
}

# ============================================
# LIVE STATE QUERIES
# ============================================

# Runtime snapshot: the same `vnx status --json` the dispatch guard reads, run
# bound to the repo root. t0_brief.json is a presentation cache; nothing here reads it.
_runtime_status() {
    local bin="$_INTEL_REPO_ROOT/bin/vnx"
    [ -x "$bin" ] || bin="${VNX_BIN:-$(command -v vnx || true)}"
    [ -n "$bin" ] && [ -x "$bin" ] || return 1
    (cd "$_INTEL_REPO_ROOT" && "$bin" status --json 2>/dev/null)
}

# Dispatches in the air (live, starting, stale, unmeasured). `vnx status --json`
# carries no per-terminal rows any more, so this replaces check_terminals.
check_live_work() {
    local status
    status="$(_runtime_status)" || { echo "Error: vnx status --json unavailable"; return 1; }
    jq -r '(.live_work // {}) as $w | ("live", "starting", "stale", "unmeasured") as $b | ($w[$b] // [])[] | "\($b) \(.dispatch_id // "?") state=\(.state // "?") age=\(.age_seconds // 0)s"' <<<"$status"
}

# Check queue status
check_queue() {
    local status
    status="$(_runtime_status)" || { echo "Error: vnx status --json unavailable"; return 1; }
    jq -r '.queues | "pending=\(.pending_count // 0) conflicts=\(.conflict_count // 0)"' <<<"$status"
}

# Check open items digest (deliverables)
check_open_items() {
    python3 "scripts/open_items_manager.py" digest 2>/dev/null || echo "No open items data"
}

# Get orchestration recommendations
check_recommendations() {
    _require_state_dir || return 1
    if [ -f "$STATE_DIR/t0_recommendations.json" ]; then
        cat "$STATE_DIR/t0_recommendations.json" | jq -r '.recommendations[] | "[\(.priority)] \(.action): \(.reason)"'
    else
        echo "No recommendations available"
    fi
}

# Get recent receipts
recent_receipts() {
    _require_state_dir || return 1
    if [ -f "$STATE_DIR/t0_receipts.ndjson" ]; then
        tail -n 5 "$STATE_DIR/t0_receipts.ndjson" | jq -r '"[\(.timestamp)] \(.track) \(.status): \(.dispatch_id // "unknown")"'
    else
        echo "No recent receipts"
    fi
}

# Check active dispatches
active_dispatches() {
    _require_dispatch_dir || return 1
    ls -la "$DISPATCH_DIR/active/" 2>/dev/null | grep -E "\.dispatch$" | awk '{print $9}' | sed 's/\.dispatch$//'
}

# Check queued dispatches
queued_dispatches() {
    _require_dispatch_dir || return 1
    ls -la "$DISPATCH_DIR/queue/" 2>/dev/null | grep -E "\.dispatch$" | awk '{print $9}' | sed 's/\.dispatch$//'
}

# ============================================
# PATTERN QUERIES
# ============================================

# Orchestration patterns
orchestration_patterns() {
    python3 "$INTEL_SCRIPT" patterns "orchestration $1"
}

# Agent selection patterns
agent_patterns() {
    python3 "$INTEL_SCRIPT" patterns "agent selection $1"
}

# Open items / deliverable patterns
deliverable_patterns() {
    python3 "$INTEL_SCRIPT" patterns "open items deliverables $1"
}

# Mode selection patterns
mode_patterns() {
    python3 "$INTEL_SCRIPT" patterns "mode selection $1"
}

# Track selection patterns
track_patterns() {
    python3 "$INTEL_SCRIPT" patterns "track selection $1"
}

# ============================================
# TAG QUERIES
# ============================================

# Tag-based search
tags() {
    python3 "$INTEL_SCRIPT" tags "$1"
}

# Orchestration tags
orchestration_tags() {
    python3 "$INTEL_SCRIPT" tags "orchestration dispatch management"
}

# ============================================
# KEYWORD QUERIES
# ============================================

# Keyword search
keywords() {
    python3 "$INTEL_SCRIPT" keywords "$1"
}

# ============================================
# PREVENTION RULES
# ============================================

# Check prevention rules
prevention() {
    python3 "$INTEL_SCRIPT" prevention
}

# ============================================
# DECISION HELPERS
# ============================================

# Should we dispatch? Delegates to dispatch_guard.sh: 0 -> YES, 2 -> NO, else UNKNOWN.
can_dispatch() {
    local out rc=0 reason
    out="$(bash "$_INTEL_DIR/dispatch_guard.sh" 2>&1)" || rc=$?
    reason="$(printf '%s\n' "$out" | head -n 1)"
    case "$rc" in
        0) echo "YES - ${reason#GO: }" ;;
        2) echo "NO - ${reason#WAIT: }" ;;
        *) echo "UNKNOWN - ${reason:-dispatch guard gave no answer}" ;;
    esac
}

# Get dispatch decision
dispatch_decision() {
    echo "=== Orchestration Decision ==="
    echo "Live work: $(check_live_work | tr '\n' ';')"
    echo "Queue: $(check_queue)"
    echo "Open Items: $(check_open_items)"
    echo "Can Dispatch: $(can_dispatch)"
    echo "================================"
}

# ============================================
# USAGE HELP
# ============================================

echo "T0 Orchestration Intelligence Queries"
echo "======================================"
echo ""
echo "Live State Monitoring:"
echo "  check_live_work    - Show dispatches in the air"
echo "  check_queue        - Show queue status"
echo "  check_open_items   - Show open items digest (deliverables)"
echo "  recent_receipts    - Show last 5 receipts"
echo "  active_dispatches  - List active dispatches"
echo "  queued_dispatches  - List queued dispatches"
echo ""
echo "Pattern Queries:"
echo "  orchestration_patterns 'dispatch timing'"
echo "  agent_patterns 'debugging specialist'"
echo "  deliverable_patterns 'quality gate tracking'"
echo "  mode_patterns 'when to use thinking mode'"
echo "  track_patterns 'parallel execution'"
echo ""
echo "Decision Helpers:"
echo "  can_dispatch       - Check if safe to dispatch"
echo "  dispatch_decision  - Full decision summary"
echo "  check_recommendations - Get AI recommendations"
echo ""
echo "Prevention & Tags:"
echo "  prevention         - Show anti-patterns to avoid"
echo "  orchestration_tags - Search orchestration patterns by tags"