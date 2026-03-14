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

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Dismiss marker: when user closes popup with Q, this file is touched.
# Popup only auto-opens when new items arrive AFTER the dismiss timestamp.
DISMISS_MARKER="$STATE_DIR/.queue_popup_dismissed"

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

    # 5. AUTO-OPEN POPUP
    # Skip if user recently dismissed the popup (dismiss marker is newer than oldest queue item)
    local dismissed=false
    if [ -f "$DISMISS_MARKER" ]; then
        local dismiss_ts oldest_queue_ts
        dismiss_ts=$(stat -f%m "$DISMISS_MARKER" 2>/dev/null || stat -c%Y "$DISMISS_MARKER" 2>/dev/null || echo 0)
        oldest_queue_ts=$(find "$QUEUE_DIR" -type f -name "*.md" -exec stat -f%m {} \; 2>/dev/null | sort -n | head -1 || echo 0)
        # If dismiss is newer than all queue items, user already saw and dismissed these
        if [ -n "$oldest_queue_ts" ] && [ "$dismiss_ts" -ge "$oldest_queue_ts" ]; then
            dismissed=true
        fi
    fi

    # Check if popup is already running to avoid duplicates
    if [ "$dismissed" = true ]; then
        echo "Popup dismissed by user, skipping auto-open (Ctrl+G to reopen)"
    elif ! is_popup_running; then
        echo "Opening queue popup via tmux..."
        if [ -n "$project_session" ]; then
            echo "Creating VNX-Queue window in session: $project_session"
            tmux new-window -t "$project_session" -n "VNX-Queue" "bash '$POPUP_SCRIPT'; touch '$DISMISS_MARKER'; tmux kill-window" 2>/dev/null || true
        else
            echo "No project tmux session found for $PROJECT_ROOT - cannot open popup"
        fi
        sleep 1  # Give popup time to open
    else
        echo "Popup already running, skipping duplicate launch"
    fi
}

# Track last count to detect changes
LAST_COUNT=$(count_files "$QUEUE_DIR")

# Main monitoring loop
echo -e "${BLUE}Watching for new dispatches...${NC}"
echo "Press Ctrl+C to stop"
echo ""

# If queue already has items on watcher startup, raise popup — but only if not
# previously dismissed by the user. This prevents popup spam on watcher restarts.
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

while true; do
    # Count files in queue
    CURRENT_COUNT=$(count_files "$QUEUE_DIR")
    
    # Check if new files appeared
    if [ "$CURRENT_COUNT" -gt "$LAST_COUNT" ]; then
        NEW_FILES=$((CURRENT_COUNT - LAST_COUNT))
        echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${GREEN}✓ $NEW_FILES new dispatch(es) detected!${NC}"
        echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

        # Clear dismiss marker — genuinely new dispatch(es) arrived
        rm -f "$DISMISS_MARKER"

        # Show enhanced non-intrusive notification instead of auto-launching popup
        show_enhanced_notification_async "$NEW_FILES new dispatch(es) detected" "$NEW_FILES"

        # Auto-accept: move queue files directly to pending when VNX_AUTO_ACCEPT=1
        if [ "${VNX_AUTO_ACCEPT:-0}" = "1" ]; then
            find "$QUEUE_DIR" -name "*.md" -type f | while read -r qfile; do
                mv "$qfile" "$PENDING_DIR/$(basename "$qfile")"
                log "Auto-accepted (VNX_AUTO_ACCEPT=1): $(basename "$qfile")"
            done
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
    
    # Wait before next check
    sleep 2
done
