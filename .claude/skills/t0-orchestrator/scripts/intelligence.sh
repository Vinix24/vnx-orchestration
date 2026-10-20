#!/bin/bash
# Intelligence queries for T0 orchestration
# Live state monitoring and orchestration pattern queries

INTEL_SCRIPT="scripts/gather_intelligence.py"

# Bind to the repo this script ships in, not the caller's cwd.
_INTEL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_INTEL_REPO_ROOT="$(git -C "$_INTEL_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$(cd "$_INTEL_DIR/../../../.." && pwd)")"

# Central state/dispatch dirs (ADR-026), via the shared resolver when the repo
# carries it, else the environment. Never a path relative to the cwd.
STATE_DIR="${VNX_STATE_DIR:-}"
DISPATCH_DIR="${VNX_DISPATCH_DIR:-}"
if [ -f "$_INTEL_REPO_ROOT/scripts/lib/vnx_resolve_root.sh" ]; then
    # shellcheck disable=SC1091
    source "$_INTEL_REPO_ROOT/scripts/lib/vnx_resolve_root.sh"
    vnx_resolve_project_root "$_INTEL_DIR/intelligence.sh" >/dev/null 2>&1 || true
    vnx_resolve_state_dir >/dev/null 2>&1 || true
    vnx_resolve_dispatch_dir >/dev/null 2>&1 || true
    STATE_DIR="${VNX_STATE_DIR:-$STATE_DIR}"
    DISPATCH_DIR="${VNX_DISPATCH_DIR:-$DISPATCH_DIR}"
fi

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
    if [ -f "$STATE_DIR/t0_recommendations.json" ]; then
        cat "$STATE_DIR/t0_recommendations.json" | jq -r '.recommendations[] | "[\(.priority)] \(.action): \(.reason)"'
    else
        echo "No recommendations available"
    fi
}

# Get recent receipts
recent_receipts() {
    if [ -f "$STATE_DIR/t0_receipts.ndjson" ]; then
        tail -n 5 "$STATE_DIR/t0_receipts.ndjson" | jq -r '"[\(.timestamp)] \(.track) \(.status): \(.dispatch_id // "unknown")"'
    else
        echo "No recent receipts"
    fi
}

# Check active dispatches
active_dispatches() {
    ls -la "$DISPATCH_DIR/active/" 2>/dev/null | grep -E "\.dispatch$" | awk '{print $9}' | sed 's/\.dispatch$//'
}

# Check queued dispatches
queued_dispatches() {
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