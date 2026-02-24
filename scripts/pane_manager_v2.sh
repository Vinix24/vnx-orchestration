#!/bin/bash
# pane_manager_v2.sh - Self-healing pane discovery system for VNX
# Replaces static pane_config.sh with dynamic discovery

# Base directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
VNX_BASE="$VNX_HOME"

# Cache configuration (project-scoped to avoid cross-project contamination)
_CACHE_SEED="${PROJECT_ROOT:-$VNX_BASE}"
if command -v shasum >/dev/null 2>&1; then
    _CACHE_SUFFIX="$(printf '%s' "$_CACHE_SEED" | shasum | awk '{print $1}' | cut -c1-12)"
elif command -v md5 >/dev/null 2>&1; then
    _CACHE_SUFFIX="$(printf '%s' "$_CACHE_SEED" | md5 -q | cut -c1-12)"
else
    _CACHE_SUFFIX="$(basename "$_CACHE_SEED" | tr -c '[:alnum:]' '_')"
fi
PANE_CACHE="/tmp/vnx_pane_cache_${_CACHE_SUFFIX}"
unset _CACHE_SEED _CACHE_SUFFIX
CACHE_TTL=300  # 5 minutes

# Logging — namespaced to avoid collision with parent scripts that source this file.
# Smart_tap, receipt_processor etc. define their own `log()` which writes to stdout;
# if we used a bare `log()` here, command substitution like $(get_pane_id_smart ...)
# would capture our debug output together with the pane ID, corrupting it.
_pm_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

# Method 1: Discover pane by title
discover_pane_by_title() {
    local terminal="$1"
    local pane_id=""

    # Scope to own session when PROJECT_ROOT is set (prevents cross-project matches)
    local vnx_session=""
    if [ -n "${PROJECT_ROOT:-}" ]; then
        vnx_session="vnx-$(basename "$PROJECT_ROOT")"
    fi

    # Look for panes with matching title (session-scoped when possible)
    if [ -n "$vnx_session" ] && tmux has-session -t "$vnx_session" 2>/dev/null; then
        pane_id=$(tmux list-panes -s -t "$vnx_session" -F "#{pane_id} #{pane_title}" 2>/dev/null | \
            grep -E "(T${terminal#T}|${terminal})" | \
            awk '{print $1}' | \
            head -1)
    fi

    # Fallback: search all sessions
    if [ -z "$pane_id" ]; then
        pane_id=$(tmux list-panes -a -F "#{pane_id} #{pane_title}" 2>/dev/null | \
            grep -E "(T${terminal#T}|${terminal})" | \
            awk '{print $1}' | \
            head -1)
    fi

    if [ -n "$pane_id" ]; then
        _pm_log "Found $terminal by title: $pane_id"
        echo "$pane_id"
        return 0
    fi
    return 1
}

# Method 2: Discover pane by working directory
discover_pane_by_path() {
    local terminal="$1"
    local pane_id=""
    local terminals_root="${PROJECT_ROOT:-}/.claude/terminals"

    # Prefer panes in an attached tmux session first (usually where user is active).
    if [ -n "${PROJECT_ROOT:-}" ]; then
        pane_id=$(tmux list-panes -a -F "#{pane_id} #{session_attached} #{pane_current_path}" 2>/dev/null | \
            awk -v root="$terminals_root" -v t="$terminal" '$2=="1" && $3==root"/"t {print $1; exit}')
    fi

    # Prefer exact project terminal path match to avoid cross-project pane mixups.
    if [ -z "$pane_id" ] && [ -n "${PROJECT_ROOT:-}" ]; then
        pane_id=$(tmux list-panes -a -F "#{pane_id} #{pane_current_path}" 2>/dev/null | \
            awk -v root="$terminals_root" -v t="$terminal" '$2==root"/"t {print $1; exit}')
    fi

    # Fallback: generic terminal path scan (scoped to own session when possible).
    if [ -z "$pane_id" ]; then
        local vnx_session_path=""
        if [ -n "${PROJECT_ROOT:-}" ]; then
            vnx_session_path="vnx-$(basename "$PROJECT_ROOT")"
        fi
        if [ -n "$vnx_session_path" ] && tmux has-session -t "$vnx_session_path" 2>/dev/null; then
            pane_id=$(tmux list-panes -s -t "$vnx_session_path" -F "#{pane_id} #{pane_current_path}" 2>/dev/null | \
                grep -E "/terminals/(T${terminal#T}|${terminal}|T-MANAGER)" | \
                grep -E "(T${terminal#T}|${terminal})" | \
                awk '{print $1}' | \
                head -1)
        fi
        # Only fall back to all-sessions scan if no PROJECT_ROOT is set
        if [ -z "$pane_id" ] && [ -z "${PROJECT_ROOT:-}" ]; then
            pane_id=$(tmux list-panes -a -F "#{pane_id} #{pane_current_path}" 2>/dev/null | \
                grep -E "/terminals/(T${terminal#T}|${terminal}|T-MANAGER)" | \
                grep -E "(T${terminal#T}|${terminal})" | \
                awk '{print $1}' | \
                head -1)
        fi
    fi

    if [ -n "$pane_id" ]; then
        _pm_log "Found $terminal by path: $pane_id"
        echo "$pane_id"
        return 0
    fi
    return 1
}

# Method 3: Discover pane by window name
discover_pane_by_window() {
    local terminal="$1"
    local window_index

    # Map terminal to expected window position
    case "$terminal" in
        T0) window_index=0 ;;
        T1) window_index=1 ;;
        T2) window_index=2 ;;
        T3) window_index=3 ;;
        *) return 1 ;;
    esac

    # Derive session name from PROJECT_ROOT (matches bin/vnx: "vnx-$(basename $PROJECT_ROOT)")
    # This prevents cross-project pane discovery when multiple VNX sessions exist.
    local vnx_session="vnx"
    if [ -n "${PROJECT_ROOT:-}" ]; then
        vnx_session="vnx-$(basename "$PROJECT_ROOT")"
    fi

    # Try to find by window:pane notation (session-scoped)
    local pane_id=$(tmux list-panes -t "$vnx_session:$window_index.0" -F "#{pane_id}" 2>/dev/null | head -1)

    if [ -n "$pane_id" ]; then
        _pm_log "Found $terminal by window position: $pane_id"
        echo "$pane_id"
        return 0
    fi
    return 1
}

# Method 4: Interactive discovery
discover_pane_interactive() {
    local terminal="$1"

    _pm_log "MANUAL DISCOVERY NEEDED for $terminal"
    _pm_log "Please identify the pane for $terminal:"

    # List all panes with details
    tmux list-panes -a -F "Pane: #{pane_id} | Path: #{pane_current_path} | Title: #{pane_title}" >&2

    # Store in a marker file for manual resolution
    echo "MANUAL_REQUIRED" > "$PANE_CACHE/${terminal}.manual"

    return 1
}

# Main discovery function with caching
get_pane_id_smart() {
    local terminal="$1"

    # Normalize terminal name
    terminal=$(echo "$terminal" | tr '[:lower:]' '[:upper:]')

    # Validate terminal name
    case "$terminal" in
        T0|T1|T2|T3) ;;
        *)
            _pm_log "ERROR: Invalid terminal name: $terminal"
            return 1
            ;;
    esac

    # Check cache first
    local cache_file="$PANE_CACHE/${terminal}.pane"
    if [ -f "$cache_file" ]; then
        local cache_age=$(( $(date +%s) - $(stat -f%m "$cache_file" 2>/dev/null || stat -c%Y "$cache_file" 2>/dev/null || echo 0) ))
        if [ "$cache_age" -lt "$CACHE_TTL" ]; then
            local cached_pane=$(cat "$cache_file")
            # Verify pane still exists
            if tmux list-panes -a -F "#{pane_id}" 2>/dev/null | grep -q "^${cached_pane}$"; then
                # If an attached-session pane exists for this terminal path, prefer it.
                # This avoids routing to stale panes in detached duplicate sessions.
                local attached_preferred=""
                if [ -n "${PROJECT_ROOT:-}" ]; then
                    local terminal_path="${PROJECT_ROOT}/.claude/terminals/${terminal}"
                    attached_preferred=$(tmux list-panes -a -F "#{pane_id} #{session_attached} #{pane_current_path}" 2>/dev/null | \
                        awk -v p="$terminal_path" '$2=="1" && $3==p {print $1; exit}')
                fi
                if [ -n "$attached_preferred" ] && [ "$attached_preferred" != "$cached_pane" ]; then
                    _pm_log "Cached pane $cached_pane for $terminal is stale; using attached pane $attached_preferred"
                    echo "$attached_preferred" > "$cache_file"
                    echo "$attached_preferred"
                    return 0
                fi
                echo "$cached_pane"
                return 0
            else
                _pm_log "Cached pane $cached_pane no longer exists, rediscovering..."
                rm -f "$cache_file"
            fi
        fi
    fi

    # Create cache directory if needed
    mkdir -p "$PANE_CACHE"

    # Try discovery methods in order
    local pane_id=""

    # Method 1: By path (project-aware, most reliable)
    if [ -z "$pane_id" ]; then
        pane_id=$(discover_pane_by_path "$terminal")
    fi

    # Method 2: By title
    if [ -z "$pane_id" ]; then
        pane_id=$(discover_pane_by_title "$terminal")
    fi

    # Method 3: By window position
    if [ -z "$pane_id" ]; then
        pane_id=$(discover_pane_by_window "$terminal")
    fi

    # Method 4: Interactive (last resort)
    if [ -z "$pane_id" ]; then
        discover_pane_interactive "$terminal"
        return 1
    fi

    # Cache the successful discovery
    echo "$pane_id" > "$cache_file"
    _pm_log "Cached pane $pane_id for $terminal"

    echo "$pane_id"
    return 0
}

# Setup function to initialize pane titles (run once during setup)
setup_pane_titles() {
    _pm_log "Setting up pane titles for better discovery..."

    # Get current pane IDs (if possible)
    local t0_pane=$(get_pane_id_smart "T0" 2>/dev/null)
    local t1_pane=$(get_pane_id_smart "T1" 2>/dev/null)
    local t2_pane=$(get_pane_id_smart "T2" 2>/dev/null)
    local t3_pane=$(get_pane_id_smart "T3" 2>/dev/null)

    # Set pane titles and persistent @vnx_label (immune to OSC escape sequence overrides)
    [ -n "$t0_pane" ] && tmux select-pane -t "$t0_pane" -T "T0" 2>/dev/null && tmux set-option -p -t "$t0_pane" @vnx_label "T0" 2>/dev/null && _pm_log "Set title for T0"
    [ -n "$t1_pane" ] && tmux select-pane -t "$t1_pane" -T "TRACK A" 2>/dev/null && tmux set-option -p -t "$t1_pane" @vnx_label "TRACK A" 2>/dev/null && _pm_log "Set title for TRACK A"
    [ -n "$t2_pane" ] && tmux select-pane -t "$t2_pane" -T "TRACK B" 2>/dev/null && tmux set-option -p -t "$t2_pane" @vnx_label "TRACK B" 2>/dev/null && _pm_log "Set title for TRACK B"
    [ -n "$t3_pane" ] && tmux select-pane -t "$t3_pane" -T "TRACK C" 2>/dev/null && tmux set-option -p -t "$t3_pane" @vnx_label "TRACK C" 2>/dev/null && _pm_log "Set title for TRACK C"

    _pm_log "Pane title setup complete"
}

# Health check function
check_pane_health() {
    local all_healthy=true

    for terminal in T0 T1 T2 T3; do
        if get_pane_id_smart "$terminal" >/dev/null 2>&1; then
            _pm_log "✓ $terminal is healthy"
        else
            _pm_log "✗ $terminal is not reachable"
            all_healthy=false
        fi
    done

    if $all_healthy; then
        _pm_log "All panes healthy"
        return 0
    else
        _pm_log "Some panes need attention"
        return 1
    fi
}

# Clear cache function
clear_cache() {
    _pm_log "Clearing pane cache..."
    rm -rf "$PANE_CACHE"
    mkdir -p "$PANE_CACHE"
    _pm_log "Cache cleared"
}

# Backward compatibility wrapper (drop-in replacement for get_pane_id)
get_pane_id() {
    local terminal="$1"
    local panes_file="${2:-$VNX_STATE_DIR/panes.json}"

    # Prefer panes.json if available (VNX_HYBRID_FINAL.sh writes deterministic pane IDs each launch)
    if [ -f "$panes_file" ] && command -v jq >/dev/null 2>&1; then
        local normalized_terminal normalized_terminal_lower
        normalized_terminal=$(echo "$terminal" | tr '[:lower:]' '[:upper:]')
        normalized_terminal_lower=$(echo "$normalized_terminal" | tr '[:upper:]' '[:lower:]')

        # Support both {"t0": {...}} and {"T0": {...}} shapes
        local pane_id
        pane_id=$(jq -r ".${normalized_terminal}.pane_id // .${normalized_terminal_lower}.pane_id // empty" "$panes_file" 2>/dev/null)

        if [ -n "$pane_id" ] && tmux list-panes -a -F "#{pane_id}" 2>/dev/null | grep -q "^${pane_id}$"; then
            # If panes.json declares a session, ensure pane belongs to that session.
            local expected_session actual_session
            expected_session=$(jq -r '.session // empty' "$panes_file" 2>/dev/null || true)
            if [ -n "$expected_session" ]; then
                actual_session=$(tmux list-panes -a -F "#{pane_id} #{session_name}" 2>/dev/null | \
                    awk -v p="$pane_id" '$1==p {print $2; exit}')
                if [ "$actual_session" != "$expected_session" ]; then
                    _pm_log "Pane $pane_id for $terminal belongs to session '$actual_session' (expected '$expected_session'), rediscovering"
                    pane_id=""
                fi
            fi
            # If an attached-session pane exists for this terminal path, prefer it over panes.json.
            local attached_preferred=""
            if [ -n "${PROJECT_ROOT:-}" ]; then
                local terminal_path="${PROJECT_ROOT}/.claude/terminals/${normalized_terminal}"
                attached_preferred=$(tmux list-panes -a -F "#{pane_id} #{session_attached} #{pane_current_path}" 2>/dev/null | \
                    awk -v p="$terminal_path" '$2=="1" && $3==p {print $1; exit}')
            fi
            if [ -n "$attached_preferred" ] && [ "$attached_preferred" != "$pane_id" ]; then
                _pm_log "Overriding panes.json pane $pane_id for $normalized_terminal with attached pane $attached_preferred"
                pane_id="$attached_preferred"
            fi
            if [ -n "$pane_id" ]; then
                echo "$pane_id"
                return 0
            fi
        fi
    fi

    # Fallback to dynamic discovery
    get_pane_id_smart "$terminal"
}

# Reverse lookup: Get terminal name from pane ID
get_terminal_from_pane() {
    local pane_id="$1"
    local panes_file="${2:-$VNX_STATE_DIR/panes.json}"

    # Validate pane_id format
    if [ -z "$pane_id" ]; then
        _pm_log "ERROR: Empty pane_id provided to get_terminal_from_pane"
        return 1
    fi

    # Method 1: Check panes.json if available
    if [ -f "$panes_file" ] && command -v jq >/dev/null 2>&1; then
        local terminal
        terminal=$(jq -r "to_entries[] | select(.value.pane_id == \"$pane_id\") | .key" "$panes_file" 2>/dev/null | tr '[:lower:]' '[:upper:]')

        if [ -n "$terminal" ]; then
            echo "$terminal"
            return 0
        fi
    fi

    # Method 2: Check by working directory
    local terminal_path
    terminal_path=$(tmux list-panes -a -F "#{pane_id} #{pane_current_path}" 2>/dev/null | \
        grep "^${pane_id} " | \
        awk '{print $2}')

    if [ -n "$terminal_path" ]; then
        # Extract terminal name from path (e.g., /path/to/terminals/T1 -> T1)
        if echo "$terminal_path" | grep -qE "/terminals/(T[0-3]|T-MANAGER)"; then
            terminal=$(echo "$terminal_path" | grep -oE "(T[0-3]|T-MANAGER)" | head -1)
            if [ -n "$terminal" ]; then
                echo "$terminal"
                return 0
            fi
        fi
    fi

    # Method 3: Check by pane title
    local pane_title
    pane_title=$(tmux list-panes -a -F "#{pane_id} #{pane_title}" 2>/dev/null | \
        grep "^${pane_id} " | \
        awk '{print $2}')

    if [ -n "$pane_title" ]; then
        # Extract terminal from title (e.g., "T1-WORKER" -> T1)
        if echo "$pane_title" | grep -qE "T[0-3]"; then
            terminal=$(echo "$pane_title" | grep -oE "T[0-3]" | head -1)
            if [ -n "$terminal" ]; then
                echo "$terminal"
                return 0
            fi
        fi
    fi

    # Failed to identify terminal
    _pm_log "WARNING: Could not identify terminal for pane $pane_id"
    return 1
}

# Export functions for use in other scripts
export -f _pm_log
export -f get_pane_id_smart
export -f get_pane_id
export -f get_terminal_from_pane
export -f setup_pane_titles
export -f check_pane_health

# CLI interface
if [ "${BASH_SOURCE[0]}" == "${0}" ]; then
    case "${1:-}" in
        setup)
            setup_pane_titles
            ;;
        health)
            check_pane_health
            ;;
        clear-cache)
            clear_cache
            ;;
        get)
            if [ -z "${2:-}" ]; then
                echo "Usage: $0 get <terminal>"
                exit 1
            fi
            get_pane_id_smart "$2"
            ;;
        *)
            echo "VNX Pane Manager v2 - Self-healing pane discovery"
            echo ""
            echo "Usage: $0 [command] [args]"
            echo ""
            echo "Commands:"
            echo "  get <terminal>  - Get pane ID for terminal (T0/T1/T2/T3)"
            echo "  setup          - Setup pane titles for better discovery"
            echo "  health         - Check health of all panes"
            echo "  clear-cache    - Clear the pane cache"
            echo ""
            echo "Or source this file to use functions in other scripts"
            ;;
    esac
fi
