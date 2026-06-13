#!/bin/bash
# Intelligence queries for T0 orchestration
# Live state monitoring and orchestration pattern queries

INTEL_SCRIPT="scripts/gather_intelligence.py"
STATE_DIR=".vnx-data/state"
DISPATCH_DIR=".vnx-data/dispatches"

# ============================================
# LIVE STATE QUERIES
# ============================================

# Check terminal states
check_terminals() {
    if [ -f "$STATE_DIR/t0_brief.json" ]; then
        cat "$STATE_DIR/t0_brief.json" | jq -r '.terminals | to_entries[] | "\(.key)=\(.value.status)(\(.value.status_age_seconds // 0)s)"'
    else
        echo "Error: t0_brief.json not found"
    fi
}

# Check queue status
check_queue() {
    if [ -f "$STATE_DIR/t0_brief.json" ]; then
        cat "$STATE_DIR/t0_brief.json" | jq -r '.queues | "pending=\(.pending) active=\(.active) conflicts=\(.conflicts)"'
    else
        echo "Error: t0_brief.json not found"
    fi
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

# Check for blocked terminals
check_blocked() {
    if [ -f "$STATE_DIR/t0_brief.json" ]; then
        cat "$STATE_DIR/t0_brief.json" | jq -r '.terminals | to_entries[] | select(.value.status == "blocked") | "\(.key) blocked: \(.value.status_age_seconds // 0)s"'
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

# Should we dispatch?
can_dispatch() {
    local terminals=$(check_terminals | grep -c "working\|blocked")
    local queue=$(check_queue | grep -oE "active=([0-9]+)" | cut -d= -f2)

    if [ "$terminals" -eq 0 ] && [ "$queue" -eq 0 ]; then
        echo "YES - All terminals idle and queue empty"
    else
        echo "NO - Terminals busy or queue active"
    fi
}

# Get dispatch decision
dispatch_decision() {
    echo "=== Orchestration Decision ==="
    echo "Terminals: $(check_terminals | tr '\n' ' ')"
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
echo "  check_terminals     - Show all terminal states"
echo "  check_queue        - Show queue status"
echo "  check_open_items   - Show open items digest (deliverables)"
echo "  check_blocked      - Show blocked terminals"
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