#!/bin/bash
# Queue Popup Watcher - Enhanced non-intrusive notifications for new dispatches
# Shows alerts without blocking terminals or interrupting typing

set -euo pipefail

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
VNX_DIR="$VNX_HOME"

# Source the singleton enforcer
source "$VNX_DIR/scripts/singleton_enforcer.sh"

# Enforce singleton - will exit if another instance is running
enforce_singleton "queue_popup_watcher"
QUEUE_DIR="$VNX_DISPATCH_DIR/queue"
PENDING_DIR="$VNX_DISPATCH_DIR/pending"
SCRIPTS_DIR="$VNX_DIR/scripts"
STATE_DIR="$VNX_STATE_DIR"
POPUP_SCRIPT="$SCRIPTS_DIR/queue_ui_enhanced.sh"
STALE_PENDING_MINUTES="${VNX_STALE_PENDING_MINUTES:-3}"   # minutes before a pending dispatch is re-offered
CATCHUP_INTERVAL_CYCLES="${VNX_CATCHUP_INTERVAL_CYCLES:-150}"  # poll cycles between catchup sweeps (~5min at 2s/cycle)

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Initialize
mkdir -p "$QUEUE_DIR" "$PENDING_DIR" "$STATE_DIR"

echo -e "${BLUE}Queue Popup Watcher starting...${NC}"
echo "Monitoring: $QUEUE_DIR"
echo "Popup script: $POPUP_SCRIPT"

# Function to count files in directory
count_files() {
    local dir="$1"
    find "$dir" -type f -name "*.md" 2>/dev/null | wc -l | tr -d ' '
}

# Function to check if popup is already running
is_popup_running() {
    local project_session
    project_session="$(resolve_project_tmux_session)"

    # Prefer authoritative signal: popup window exists in target session.
    if [ -n "$project_session" ] && tmux list-windows -t "$project_session" -F '#{window_name}' 2>/dev/null | grep -qx "VNX-Queue"; then
        return 0
    fi

    # Fallback: running popup process scoped to this project path.
    pgrep -f "queue_ui_enhanced.sh.*${PROJECT_ROOT}" > /dev/null 2>&1
}

# Resolve the tmux session belonging to this project.
resolve_project_tmux_session() {
    local session
    local panes_file="$STATE_DIR/panes.json"

    # Prefer currently attached session for this project so popups appear where user is looking.
    session=$(tmux list-panes -a -F '#{session_name} #{session_attached} #{pane_current_path}' 2>/dev/null \
        | awk -v project_root="$PROJECT_ROOT" '$2 == "1" && $3 ~ "^" project_root "/\\.claude/terminals/" { print $1; exit }')
    if [ -n "$session" ] && tmux has-session -t "$session" 2>/dev/null; then
        echo "$session"
        return 0
    fi

    # Prefer authoritative session from panes.json written by launcher.
    if [ -f "$panes_file" ]; then
        session=$(python3 - "$panes_file" <<'PY'
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        data = json.load(fh)
    print((data.get("session") or "").strip())
except Exception:
    print("")
PY
)
        if [ -n "$session" ] && tmux has-session -t "$session" 2>/dev/null; then
            echo "$session"
            return 0
        fi
    fi

    session=$(tmux list-panes -a -F '#{session_name} #{pane_current_path}' 2>/dev/null \
        | awk -v project_root="$PROJECT_ROOT" '$2 ~ "^" project_root "/\\.claude/terminals/" { print $1; exit }')
    echo "$session"
}

# Function to show enhanced non-intrusive notification AND open popup
show_enhanced_notification_async() {
    local message="$1"
    local count="${2:-1}"
    local queue_count

    # 1. Terminal bell for audio notification (multiple beeps)
    echo -e "\a\a\a"  # Triple beep for attention

    # 2. Extended display message with urgency indicators
    local project_session
    project_session="$(resolve_project_tmux_session)"

    if [ "$count" -gt 3 ]; then
        tmux display-message -t "${project_session}:0" -d 5000 "🚨🚨 URGENT: $count NEW DISPATCHES - Opening Queue 🚨🚨" 2>/dev/null || true
    elif [ "$count" -gt 1 ]; then
        tmux display-message -t "${project_session}:0" -d 5000 "🚨 VNX: $count new dispatches - Opening Queue 🚨" 2>/dev/null || true
    else
        tmux display-message -t "${project_session}:0" -d 5000 "📦 VNX: $count new dispatch - Opening Queue" 2>/dev/null || true
    fi

    # 3. Status line flash (configurable duration)
    tmux set-option -t "${project_session}:0" -g status-style "bg=colour196,fg=colour255,bold" 2>/dev/null || true
    sleep 0.5
    tmux set-option -t "${project_session}:0" -g status-style "bg=colour22,fg=colour255" 2>/dev/null || true
    sleep 0.3
    tmux set-option -t "${project_session}:0" -g status-style "bg=colour196,fg=colour255,bold" 2>/dev/null || true
    sleep 0.5
    tmux set-option -t "${project_session}:0" -g status-style "bg=colour22,fg=colour255" 2>/dev/null || true

    # 4. Window title notification (for external visibility)
    if [ "$count" -gt 1 ]; then
        tmux rename-window -t "${project_session}:0" "VNX!($count)" 2>/dev/null || true
    else
        tmux rename-window -t "${project_session}:0" "VNX!(1)" 2>/dev/null || true
    fi

    # 5. AUTO-OPEN POPUP (RESTORED FUNCTIONALITY)
    # Guard: only open popup if queue still has items (avoid empty-popup races).
    queue_count=$(count_files "$QUEUE_DIR")
    if [ "$queue_count" -eq 0 ]; then
        echo "Queue empty at popup time, skipping popup launch"
        return
    fi
    # Check if popup is already running to avoid duplicates
    if ! is_popup_running; then
        echo "Opening queue popup via tmux..."
        if [ -n "$project_session" ]; then
            echo "Creating VNX-Queue window in session: $project_session"
            tmux new-window -t "$project_session" -n "VNX-Queue" "bash '$POPUP_SCRIPT'; tmux kill-window" 2>/dev/null || true
        else
            echo "No project tmux session found for $PROJECT_ROOT - cannot open popup"
        fi
        sleep 1  # Give popup time to open
    else
        echo "Popup already running, skipping duplicate launch"
    fi
}

# Scan pending/ for dispatches older than STALE_PENDING_MINUTES and touch them.
# This resets mtime so the dispatcher sees them as new entries (seen-key is dispatch-id:mtime).
# Prevents dispatches from getting permanently stuck when a terminal was blocked or the
# dispatcher missed the file on first scan.
_stale_pending_catchup() {
    local now
    now=$(date +%s)
    local stale_threshold=$(( STALE_PENDING_MINUTES * 60 ))
    local found=0
    while IFS= read -r -d '' f; do
        local mtime
        mtime=$(stat -f%m "$f" 2>/dev/null || stat -c%Y "$f" 2>/dev/null || echo 0)
        local age_secs=$(( now - mtime ))
        if [ "$age_secs" -ge "$stale_threshold" ]; then
            touch "$f"
            echo "[catchup] Re-offered stale pending dispatch: $(basename "$f") (age: $((age_secs/60))m)"
            found=$(( found + 1 ))
        fi
    done < <(find "$PENDING_DIR" -name "*.md" -type f -print0 2>/dev/null)
    [ "$found" -gt 0 ] && echo "[catchup] Re-offered $found stale dispatch(es) in pending/"
}

# Track last count to detect changes
LAST_COUNT=$(count_files "$QUEUE_DIR")

# Main monitoring loop
echo -e "${BLUE}Watching for new dispatches...${NC}"
echo "Press Ctrl+C to stop"
echo ""

# Startup catchup: re-offer any stale pending dispatches from before this process started
_stale_pending_catchup

# If queue already has items on watcher startup, raise popup immediately.
# Wait briefly for tmux session to be fully registered (race condition at startup).
if [ "$LAST_COUNT" -gt 0 ]; then
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}✓ $LAST_COUNT queued dispatch(es) on startup${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    # Retry popup with delay — tmux session may not be resolvable yet at startup
    for _attempt in 1 2 3; do
        if [ -n "$(resolve_project_tmux_session)" ]; then
            show_enhanced_notification_async "$LAST_COUNT queued dispatch(es) on startup" "$LAST_COUNT"
            break
        fi
        echo "Waiting for tmux session to register (attempt $_attempt/3)..."
        sleep 3
    done
fi

_catchup_cycle=0
while true; do
    # Count files in queue
    CURRENT_COUNT=$(count_files "$QUEUE_DIR")
    
    # Check if new files appeared
    if [ "$CURRENT_COUNT" -gt "$LAST_COUNT" ]; then
        NEW_FILES=$((CURRENT_COUNT - LAST_COUNT))
        echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${GREEN}✓ $NEW_FILES new dispatch(es) detected!${NC}"
        echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        
        # Debounce: ensure queue still has items before opening popup
        sleep 1
        CURRENT_COUNT=$(count_files "$QUEUE_DIR")
        if [ "$CURRENT_COUNT" -gt 0 ]; then
            # Show enhanced non-intrusive notification instead of auto-launching popup
            show_enhanced_notification_async "$NEW_FILES new dispatch(es) detected" "$NEW_FILES"
        else
            echo "Queue emptied during debounce; skipping popup"
        fi
        
        LAST_COUNT=$CURRENT_COUNT
    elif [ "$CURRENT_COUNT" -lt "$LAST_COUNT" ]; then
        # Files were processed/removed
        LAST_COUNT=$CURRENT_COUNT
        if [ "$CURRENT_COUNT" -eq 0 ]; then
            echo -e "${BLUE}Queue empty${NC}"
        else
            echo -e "${BLUE}$CURRENT_COUNT dispatch(es) remaining in queue${NC}"
        fi
    fi
    
    # Also check pending directory for direct dispatches
    PENDING_COUNT=$(count_files "$PENDING_DIR")
    if [ "$PENDING_COUNT" -gt 0 ]; then
        echo -e "${YELLOW}Note: $PENDING_COUNT dispatch(es) in pending (auto-processing)${NC}"
    fi
    
    # Periodic stale-pending catchup sweep
    _catchup_cycle=$(( _catchup_cycle + 1 ))
    if [ $(( _catchup_cycle % CATCHUP_INTERVAL_CYCLES )) -eq 0 ]; then
        _stale_pending_catchup
    fi

    # Wait before next check
    sleep 2
done
