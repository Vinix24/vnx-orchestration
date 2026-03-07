#!/bin/bash
# Dispatcher V8 Minimal - Native Skills + Instruction-Only Dispatch
# BREAKING CHANGE: Assumes skills loaded natively at session start
# Only sends: skill activation + instruction + receipt (no template compilation)

set -euo pipefail

# Ensure tmux/jq are available when launched via nohup/setsid
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"
source "$SCRIPT_DIR/lib/dispatch_metadata.sh"

# Configuration
PROJECT_ROOT="${PROJECT_ROOT}"
VNX_DIR="$VNX_HOME"

# Source the singleton enforcer
source "$VNX_DIR/scripts/singleton_enforcer.sh"

# Enforce singleton - will exit if another instance is running
enforce_singleton "dispatcher_v8_minimal"

# Configuration
CLAUDE_DIR="$PROJECT_ROOT/.claude"
DISPATCH_DIR="$VNX_DISPATCH_DIR"
QUEUE_DIR="$DISPATCH_DIR/queue"
PENDING_DIR="$DISPATCH_DIR/pending"
ACTIVE_DIR="$DISPATCH_DIR/active"
COMPLETED_DIR="$DISPATCH_DIR/completed"
REJECTED_DIR="$DISPATCH_DIR/rejected"
STATE_DIR="$VNX_STATE_DIR"
TERMINALS_DIR="$CLAUDE_DIR/terminals"
LOG_FILE="$VNX_LOGS_DIR/dispatcher_v8.log"
PROGRESS_FILE="$STATE_DIR/progress.yaml"
RUN_ID=$(date +%s)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Initialize log (avoid process substitution issues under nohup)
mkdir -p "$(dirname "$LOG_FILE")"
exec >> "$LOG_FILE" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Dispatcher V8 MINIMAL starting..."

# Initialize directories
for dir in "$QUEUE_DIR" "$PENDING_DIR" "$ACTIVE_DIR" "$COMPLETED_DIR" "$REJECTED_DIR"; do
    mkdir -p "$dir"
done

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Structured failure event logging for shell/Python boundary diagnostics.
log_structured_failure() {
    local code="$1"
    local message="$2"
    local details="${3:-}"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"

    local payload
    payload="$(python3 - "$code" "$message" "$details" <<'PY'
import json
import sys

code, message, details = sys.argv[1], sys.argv[2], sys.argv[3]
event = {
    "event": "failure",
    "component": "dispatcher_v8_minimal.sh",
    "code": code,
    "message": message,
}
if details:
    event["details"] = details
print(json.dumps(event, separators=(",", ":")))
PY
)"

    echo "[$ts] $payload"
}

tmux_send_best_effort() {
    local target_pane="$1"
    shift
    if ! tmux send-keys -t "$target_pane" "$@" 2>/dev/null; then
        log_structured_failure "tmux_send_failed" "tmux send-keys failed (best-effort)" "pane=$target_pane args=$*"
        return 1
    fi
    return 0
}

# Map track identifier to canonical terminal id.
track_to_terminal() {
    case "$1" in
        A) echo "T1" ;;
        B) echo "T2" ;;
        C) echo "T3" ;;
        *) echo "" ;;
    esac
}

# Check if terminal has an active conflicting claim/lock.
terminal_lock_allows_dispatch() {
    local terminal_id="$1"
    local dispatch_id="$2"
    local state_file="$STATE_DIR/terminal_state.json"

    if [ ! -f "$state_file" ]; then
        return 0
    fi

    local check_output
    set +e
    check_output=$(python3 - "$state_file" "$terminal_id" "$dispatch_id" <<'PY'
import json
import sys
from datetime import datetime, timezone

state_file, terminal_id, dispatch_id = sys.argv[1], sys.argv[2], sys.argv[3]

def parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

try:
    with open(state_file, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    print("BLOCK:terminal_state_unreadable")
    sys.exit(0)

record = ((payload.get("terminals") or {}).get(terminal_id) or {})
if not isinstance(record, dict) or not record:
    print("ALLOW:no_record")
    sys.exit(0)

now = datetime.now(timezone.utc)
status = str(record.get("status") or "").strip().lower()
claimed_by = str(record.get("claimed_by") or "").strip()
lease_expires_at = parse_iso(record.get("lease_expires_at"))
last_activity = parse_iso(record.get("last_activity"))

claim_active = bool(claimed_by) and (lease_expires_at is None or lease_expires_at > now)
if claim_active and claimed_by != dispatch_id:
    print(f"BLOCK:active_claim:{claimed_by}")
    sys.exit(0)

# Only block by claimed status when the claim is still active.
# Expired claims should not prevent new dispatches.
if status in {"working", "blocked"} and claim_active and claimed_by and claimed_by != dispatch_id:
    print(f"BLOCK:status_claimed:{claimed_by}:{status}")
    sys.exit(0)

if status in {"working", "blocked"} and not claimed_by and last_activity is not None:
    age_seconds = max(0, int((now - last_activity).total_seconds()))
    if age_seconds <= 900:
        print(f"BLOCK:recent_{status}_without_claim:{age_seconds}s")
        sys.exit(0)

print("ALLOW:clear")
PY
)
    local rc=$?
    set -e

    if [ $rc -ne 0 ]; then
        log "V8 LOCK: check_failed terminal=$terminal_id dispatch=$dispatch_id rc=$rc"
        return 1
    fi

    if [[ "$check_output" == BLOCK:* ]]; then
        log "V8 LOCK: blocked terminal=$terminal_id dispatch=$dispatch_id reason=${check_output#BLOCK:}"
        return 1
    fi

    return 0
}

acquire_terminal_claim() {
    local terminal_id="$1"
    local dispatch_id="$2"
    local now_iso
    now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if ! python3 "$VNX_DIR/scripts/terminal_state_shadow.py" \
        --state-dir "$STATE_DIR" \
        --terminal-id "$terminal_id" \
        --status working \
        --claimed-by "$dispatch_id" \
        --claimed-at "$now_iso" \
        --last-activity "$now_iso" \
        --lease-seconds "${VNX_DISPATCH_LEASE_SECONDS:-600}" >/dev/null 2>&1; then
        log "V8 LOCK: acquire_failed terminal=$terminal_id dispatch=$dispatch_id"
        return 1
    fi

    log "V8 LOCK: acquired terminal=$terminal_id dispatch=$dispatch_id"
    return 0
}

release_terminal_claim() {
    local terminal_id="$1"
    local dispatch_id="$2"
    local now_iso
    now_iso="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if ! python3 "$VNX_DIR/scripts/terminal_state_shadow.py" \
        --state-dir "$STATE_DIR" \
        --terminal-id "$terminal_id" \
        --status idle \
        --last-activity "$now_iso" \
        --clear-claim >/dev/null 2>&1; then
        log "V8 LOCK: release_failed terminal=$terminal_id dispatch=$dispatch_id"
        return 1
    fi

    log "V8 LOCK: released terminal=$terminal_id dispatch=$dispatch_id"
    return 0
}

# Source smart pane manager for self-healing pane discovery
source "$VNX_DIR/scripts/pane_manager_v2.sh"

# Function to get pane IDs from state
get_pane_ids() {
    # Use unified pane configuration
    if ! T0_PANE=$(get_pane_id "t0" "$STATE_DIR/panes.json"); then
        T0_PANE=""
        log_structured_failure "pane_lookup_failed" "Failed to resolve T0 pane id" "pane_file=$STATE_DIR/panes.json"
    fi
    if ! T1_PANE=$(get_pane_id "T1" "$STATE_DIR/panes.json"); then
        T1_PANE=""
        log_structured_failure "pane_lookup_failed" "Failed to resolve T1 pane id" "pane_file=$STATE_DIR/panes.json"
    fi
    if ! T2_PANE=$(get_pane_id "T2" "$STATE_DIR/panes.json"); then
        T2_PANE=""
        log_structured_failure "pane_lookup_failed" "Failed to resolve T2 pane id" "pane_file=$STATE_DIR/panes.json"
    fi
    if ! T3_PANE=$(get_pane_id "T3" "$STATE_DIR/panes.json"); then
        T3_PANE=""
        log_structured_failure "pane_lookup_failed" "Failed to resolve T3 pane id" "pane_file=$STATE_DIR/panes.json"
    fi

    return 0
}

# ===== METADATA EXTRACTION FUNCTIONS (from V7) =====

# Function to extract track from dispatch
extract_track() {
    vnx_dispatch_extract_track "$1"
}

# Function to extract cognition level
extract_cognition() {
    vnx_dispatch_extract_cognition "$1"
}

# Function to extract priority
extract_priority() {
    vnx_dispatch_extract_priority "$1"
}

# Function to extract agent role from dispatch (handles malformed role strings)
extract_agent_role() {
    vnx_dispatch_extract_agent_role "$1"
}

# Function to normalize role for flexible matching
normalize_role() {
    vnx_dispatch_normalize_role "$1"
}

# Function to extract phase from dispatch
extract_phase() {
    vnx_dispatch_extract_phase "$1"
}

# Function to extract new gate from dispatch
extract_new_gate() {
    vnx_dispatch_extract_new_gate "$1"
}

# Function to extract task_id from dispatch filename or content
extract_task_id() {
    vnx_dispatch_extract_task_id "$1" "$2"
}

# SPRINT 2: Function to extract PR-ID from dispatch
extract_pr_id() {
    vnx_dispatch_extract_pr_id "$1"
}

# ===== MODE CONTROL FUNCTIONS (from V7 Track 2b) =====

# Terminal provider resolution (Claude Code vs Codex CLI)
get_terminal_provider() {
    local terminal_id="$1"  # T0|T1|T2|T3

    # 1) Explicit env var override (e.g., VNX_T1_PROVIDER=codex_cli)
    local env_key="VNX_${terminal_id}_PROVIDER"
    local env_provider="${!env_key:-}"
    if [ -n "$env_provider" ]; then
        echo "$env_provider" | tr '[:upper:]' '[:lower:]'
        return 0
    fi

    # 2) panes.json provider field (optional)
    if command -v jq >/dev/null 2>&1 && [ -f "$STATE_DIR/panes.json" ]; then
        local provider
        local terminal_lower
        terminal_lower=$(echo "$terminal_id" | tr '[:upper:]' '[:lower:]')
        if ! provider=$(jq -r ".${terminal_id}.provider // .${terminal_lower}.provider // empty" "$STATE_DIR/panes.json" 2>/dev/null); then
            provider=""
            log_structured_failure "pane_provider_lookup_failed" "Failed to resolve terminal provider from panes.json" "terminal=$terminal_id"
        fi
        if [ -n "$provider" ] && [ "$provider" != "null" ]; then
            echo "$provider" | tr '[:upper:]' '[:lower:]'
            return 0
        fi
    fi

    # Default
    echo "claude_code"
}

get_context_reset_command() {
    local provider="$1"
    case "$provider" in
        codex_cli|codex)
            echo "/new"
            ;;
        *)
            echo "/clear"
            ;;
    esac
}

# Function to extract Mode field from dispatch
extract_mode() {
    local mode
    mode=$(vnx_dispatch_extract_mode "$1")

    if [ "$mode" = "planning" ]; then
        log "V8: Planning mode detected - will activate Opus and @planner skill"
    fi

    echo "$mode"
}

# Function to extract ClearContext field
extract_clear_context() {
    vnx_dispatch_extract_clear_context "$1"
}

# Function to extract ForceNormalMode field
extract_force_normal_mode() {
    vnx_dispatch_extract_force_normal_mode "$1"
}

# Function to extract Requires-Model field (for model switching)
extract_requires_model() {
    vnx_dispatch_extract_requires_model "$1"
}

# Function to extract Requires-Provider field
extract_requires_provider() {
    vnx_dispatch_extract_requires_provider "$1"
}

# Function to configure terminal mode based on dispatch fields
configure_terminal_mode() {
    local target_pane="$1"
    local dispatch_file="$2"

    local terminal_id
    terminal_id=$(get_terminal_from_pane "$target_pane" "$STATE_DIR/panes.json" 2>/dev/null || echo "UNKNOWN")
    local provider
    provider=$(get_terminal_provider "$terminal_id")

    # Extract mode control fields
    local mode=$(extract_mode "$dispatch_file")
    local clear_context=$(extract_clear_context "$dispatch_file")
    local requires_model=$(extract_requires_model "$dispatch_file")
    local force_normal=$(extract_force_normal_mode "$dispatch_file")
    local requires_provider
    requires_provider=$(extract_requires_provider "$dispatch_file")

    # Provider mismatch warning
    if [[ -n "$requires_provider" ]] && [[ "$requires_provider" != "$provider" ]]; then
        log "V8 MODE_CONTROL: WARNING — Requires-Provider=$requires_provider but terminal=$terminal_id runs $provider"
    fi

    # Log configuration for debugging
    log "V8 MODE_CONTROL: Config - terminal=$terminal_id provider=$provider mode=$mode clear=$clear_context model=$requires_model force=$force_normal"

    # Step 1: Force normal mode if requested (to handle persistence issue)
    if [[ "$force_normal" == "true" && "$provider" == "claude_code" ]]; then
        log "V8 MODE_CONTROL: Forcing normal mode first (safety reset)..."
        # Cycle through modes to ensure we're in normal
        if ! tmux_send_best_effort "$target_pane" Tab; then
            log "V8 MODE_CONTROL: best-effort Tab reset failed (continuing)"
        fi  # Exit thinking if active
        sleep 0.5
        if ! tmux_send_best_effort "$target_pane" -l $'\e[Z'; then
            log "V8 MODE_CONTROL: best-effort Shift+Tab reset failed (continuing)"
        fi  # Exit plan if active
        sleep 0.5
        if ! tmux_send_best_effort "$target_pane" -l $'\e[Z'; then
            log "V8 MODE_CONTROL: best-effort Shift+Tab cycle failed (continuing)"
        fi  # Cycle once more
        sleep 0.5
        if ! tmux_send_best_effort "$target_pane" -l $'\e[Z'; then
            log "V8 MODE_CONTROL: best-effort Shift+Tab normalization failed (continuing)"
        fi  # Back to normal
        sleep 1
    fi

    # Step 2: Clear context if requested
    if [[ "$clear_context" == "true" ]]; then
        local reset_cmd
        reset_cmd=$(get_context_reset_command "$provider")
        log "V8 MODE_CONTROL: Clearing context via $reset_cmd ..."

        # Pre-clear: ensure input line is empty before typing command
        # Without this, leftover characters in the input buffer cause
        # /new to become " /new" (leading space) which Codex doesn't
        # recognize as a command (white text instead of blue).
        # NOTE: Do NOT use C-c here — it can kill the CLI process entirely,
        # leaving a bare zsh shell where dispatch content gets executed as
        # shell commands. C-u alone safely clears the input line.
        tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
        sleep 0.3

        if ! tmux_send_best_effort "$target_pane" -l "$reset_cmd"; then
            log_structured_failure "context_reset_failed" "Failed to send context reset command" "pane=$target_pane provider=$provider"
            return 1
        fi
        sleep 1  # Allow CLI to fully render typed command before submitting
        if ! tmux_send_best_effort "$target_pane" Enter; then
            log_structured_failure "context_reset_submit_failed" "Failed to submit context reset command" "pane=$target_pane provider=$provider"
            return 1
        fi
        # Provider-aware delay: Gemini /clear needs more time to reset UI than
        # Claude /clear or Codex /new (different rendering cycle + history wipe).
        case "$provider" in
            gemini_cli|gemini) sleep 6 ;;
            codex_cli|codex)   sleep 4 ;;
            *)                 sleep 3 ;;
        esac
    fi

    # Step 3: Switch model if specified (only for providers that support /model)
    if [[ -n "$requires_model" ]] && [[ "$requires_model" != "" ]]; then
        if [[ "$provider" == "claude_code" || "$provider" == "codex_cli" || "$provider" == "codex" ]]; then
            log "V8 MODE_CONTROL: Switching to model: $requires_model (provider=$provider)"
            # Pre-clear input line (C-u only — C-c would kill the CLI process)
            tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
            sleep 0.3
            if ! tmux_send_best_effort "$target_pane" -l "/model $requires_model"; then
                log_structured_failure "model_switch_failed" "Failed to send model switch command" "pane=$target_pane model=$requires_model"
                return 1
            fi
            sleep 1  # Allow CLI to render command before submitting
            if ! tmux_send_best_effort "$target_pane" Enter; then
                log_structured_failure "model_switch_submit_failed" "Failed to submit model switch command" "pane=$target_pane model=$requires_model"
                return 1
            fi
            sleep 4  # Critical delay for model switch to complete
        elif [[ "$provider" == "gemini_cli" || "$provider" == "gemini" ]]; then
            log "V8 MODE_CONTROL: Gemini does not support runtime model switching — Requires-Model=$requires_model ignored"
        else
            log "V8 MODE_CONTROL: Unknown provider '$provider' — Requires-Model=$requires_model ignored"
        fi
    fi

    # Step 4: Activate requested mode
    if [[ "$provider" != "claude_code" ]]; then
        if [[ "$provider" == "codex_cli" || "$provider" == "codex" ]]; then
            if [[ "$mode" == "planning" ]]; then
                log "V8 MODE_CONTROL: Codex planning mode via /plan"
                # Pre-clear input line (C-u only — C-c would kill the CLI process)
                tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
                sleep 0.3
                if ! tmux_send_best_effort "$target_pane" -l "/plan"; then
                    log_structured_failure "plan_mode_activation_failed" "Failed to send /plan command" "pane=$target_pane provider=$provider"
                    return 1
                fi
                sleep 1  # Allow CLI to render command before submitting
                if ! tmux_send_best_effort "$target_pane" Enter; then
                    log_structured_failure "plan_mode_submit_failed" "Failed to submit /plan command" "pane=$target_pane provider=$provider"
                    return 1
                fi
                sleep 2  # Wait for plan mode activation
            else
                log "V8 MODE_CONTROL: Codex - skipping unsupported mode: $mode"
            fi
        elif [[ "$provider" == "gemini_cli" || "$provider" == "gemini" ]]; then
            log "V8 MODE_CONTROL: Gemini - no mode toggles available (mode=$mode skipped)"
        else
            log "V8 MODE_CONTROL: Unknown provider '$provider' - skipping mode: $mode"
        fi
        log "V8 MODE_CONTROL: Configuration complete"
        return 0
    fi

    case "$mode" in
        planning)
            # Planning mode with Opus model and @planner skill
            log "V8 MODE_CONTROL: Activating PLANNING mode with Opus model..."

            # First, ensure we're using Opus for planning
            log "V8: Switching to Opus model for planning mode"
            # Pre-clear input line (C-u only — C-c would kill the CLI process)
            tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
            sleep 0.3
            if ! tmux_send_best_effort "$target_pane" -l "/model opus"; then
                log_structured_failure "planning_model_switch_failed" "Failed to switch to Opus for planning mode" "pane=$target_pane"
                return 1
            fi
            sleep 1  # Allow CLI to render command before submitting
            if ! tmux_send_best_effort "$target_pane" Enter; then
                log_structured_failure "planning_model_submit_failed" "Failed to submit Opus switch for planning mode" "pane=$target_pane"
                return 1
            fi
            sleep 4  # Critical delay for model switch to complete

            # Then activate PLAN mode
            log "V8 MODE_CONTROL: Activating PLAN mode (⏸)..."
            if ! tmux_send_best_effort "$target_pane" -l $'\e[Z'; then
                log_structured_failure "planning_mode_toggle_failed" "Failed first Shift+Tab for planning mode" "pane=$target_pane"
                return 1
            fi
            sleep 0.5
            if ! tmux_send_best_effort "$target_pane" -l $'\e[Z'; then
                log_structured_failure "planning_mode_toggle_failed" "Failed second Shift+Tab for planning mode" "pane=$target_pane"
                return 1
            fi
            sleep 2
            log "V8 MODE_CONTROL: Plan mode activated - look for ⏸ indicator"
            ;;
        thinking)
            log "V8 MODE_CONTROL: Activating THINKING mode (✽)..."
            if ! tmux_send_best_effort "$target_pane" Tab; then
                log_structured_failure "thinking_mode_toggle_failed" "Failed Tab toggle for thinking mode" "pane=$target_pane"
                return 1
            fi
            sleep 2
            log "V8 MODE_CONTROL: Thinking mode activated - look for ✽ indicator"
            ;;
        none|normal)
            log "V8 MODE_CONTROL: Staying in NORMAL mode"
            ;;
        *)
            log "V8 MODE_CONTROL: Unknown mode: $mode (ignoring)"
            ;;
    esac

    log "V8 MODE_CONTROL: Configuration complete"
}

# ===== END MODE CONTROL FUNCTIONS =====

# Function to determine which executor (terminal) to use
determine_executor() {
    local track="$1"
    local cognition="$2"
    local requires_mcp="${3:-false}"

    # MCP-aware routing: redirect to T3 if task needs full MCP capabilities
    if [ "$requires_mcp" = "true" ] && [ "$track" != "C" ]; then
        log "V8 MCP routing: Track $track → T3 (requires MCP)"
        if [ -n "${T3_PANE:-}" ]; then
            echo "$T3_PANE"
        else
            echo "$(get_pane_id "T3" "$STATE_DIR/panes.json")"
        fi
        return 0
    fi

    # Track-based routing with automatic model switching per dispatch
    case "$track" in
        A) echo "${T1_PANE:-$(get_pane_id "T1" "$STATE_DIR/panes.json")}" ;;  # T1 (Track A)
        B) echo "${T2_PANE:-$(get_pane_id "T2" "$STATE_DIR/panes.json")}" ;;  # T2 (Track B)
        C) echo "${T3_PANE:-$(get_pane_id "T3" "$STATE_DIR/panes.json")}" ;;  # T3 (Track C)
        *) echo "${T1_PANE:-$(get_pane_id "T1" "$STATE_DIR/panes.json")}" ;;  # Default to T1
    esac
}

# ===== INSTRUCTION EXTRACTION (V8 Core) =====

# Function to extract instruction content from dispatch
extract_instruction_content() {
    local dispatch_file="$1"

    # Extract content between "Instruction:" and "[[DONE]]"
    local content
    content=$(awk '/^Instruction:/{flag=1; next} /^\[\[DONE\]\]/{flag=0} flag' "$dispatch_file")
    if [ -n "$content" ]; then
        echo "$content"
        return 0
    fi

    # Fallback: use everything after YAML frontmatter, excluding [[TARGET:...]] markers.
    content=$(awk '
        BEGIN { in_frontmatter = 0; saw_frontmatter = 0 }
        /^---$/ {
            if (saw_frontmatter == 0) { saw_frontmatter = 1; in_frontmatter = 1; next }
            if (in_frontmatter == 1) { in_frontmatter = 0; next }
        }
        in_frontmatter == 1 { next }
        { print }
    ' "$dispatch_file" | sed '/^\[\[TARGET:/d')

    if [ -n "$content" ]; then
        echo "$content"
        return 0
    fi

    return 1
}

extract_context_files() {
    local dispatch_file="$1"

    # Extract Context: line(s) - simpler approach: grab Context line + next non-blank line before Instruction
    local context
    context=$(awk '
        /^Context:/ {
            sub(/^Context: */, "")
            context = $0
            in_context = 1
            next
        }
        in_context == 1 && /^$/ {
            next
        }
        in_context == 1 && /^Instruction:/ {
            in_context = 0
        }
        in_context == 1 && /^\[\[@/ {
            context = context " " $0
            next
        }
        in_context == 1 {
            in_context = 0
        }
        END {
            if (context) print context
        }
    ' "$dispatch_file" | tr ' ' '\n' | grep '^\[\[@' )

    if [ -n "$context" ]; then
        echo "$context"
        return 0
    fi

    # Fallback: YAML frontmatter context_files list.
    awk '
        BEGIN { in_frontmatter = 0; saw_frontmatter = 0; in_list = 0 }
        /^---$/ {
            if (saw_frontmatter == 0) { saw_frontmatter = 1; in_frontmatter = 1; next }
            if (in_frontmatter == 1) { in_frontmatter = 0; in_list = 0; next }
        }
        in_frontmatter == 1 && /^context_files:/ { in_list = 1; next }
        in_frontmatter == 1 && in_list == 1 {
            if ($0 ~ /^ *-/) { sub(/^ *- */, ""); print; next }
            if ($0 ~ /^[a-zA-Z_]+:/) { in_list = 0 }
        }
    ' "$dispatch_file"
}

# ===== RECEIPT GENERATION (from V7) =====

# Function to generate receipt footer
generate_receipt_footer() {
    local dispatch_file="$1"
    local track="$2"
    local phase="$3"
    local gate="$4"
    local task_id="$5"
    local cmd_id="$6"
    local dispatch_id="$7"

    # Extract PR-ID from dispatch file
    local footer_pr_id
    footer_pr_id=$(extract_pr_id "$dispatch_file" 2>/dev/null)
    local dispatch_id_for_footer="$dispatch_id"
    if [ -z "$dispatch_id_for_footer" ]; then
        dispatch_id_for_footer=$(vnx_dispatch_extract_dispatch_id "$dispatch_file" 2>/dev/null)
    fi

    # Generate inline receipt footer — no external template dependency
    cat <<RECEIPT_EOF

---
# Task Completion Guidelines

## Report Metadata (REQUIRED — include this section in your report)

Your report MUST include this metadata block exactly as shown below. The receipt processor parses these fields to track progress and deliver receipts to T0.

\`\`\`
**Dispatch ID**: ${dispatch_id_for_footer:-unknown}
**PR**: ${footer_pr_id:-unknown}
**Track**: ${track}
**Gate**: ${gate}
**Status**: success
\`\`\`

## Before Completing

1. Stage and commit ALL code changes from this task:
   - Conventional commit: \`feat|fix|test|refactor(<scope>): <description>\`
   - Include in commit body: \`Dispatch-ID: ${dispatch_id_for_footer:-unknown}\`
   - Do NOT commit VNX infrastructure or state directories
2. Then write your report below

## Expected Outputs

When completing your task, create a markdown report with:

- **Implementation Summary**: What was done, key decisions made
- **Files Modified**: List of changed/created files with brief descriptions
- **Testing Evidence**: Test results, validation performed
- **Open Items**: Issues discovered outside dispatch scope (blocker/warn/info)

**Report Format**: Structured markdown with clear sections and evidence-based findings.

Write your report to: \`.vnx-data/unified_reports/\`
Filename: \`$(date +%Y%m%d-%H%M%S)-${track}-<short-title>.md\`

---
*VNX V8 - Native Skills + Instruction-Only Dispatch*
RECEIPT_EOF
}

# ===== SKILL ACTIVATION MAPPING (V8 Core) =====

# Function to map dispatch role to skill name
map_role_to_skill() {
    local role="$1"

    # Map dispatch roles to native skill names
    case "$role" in
        "debugging-specialist"|"debugging_specialist")
            echo "debugger"
            ;;
        "developer")
            echo "backend-developer"
            ;;
        "senior-developer")
            echo "reviewer"
            ;;
        "performance-engineer"|"perf-engineer")
            echo "performance-profiler"
            ;;
        "integration-specialist")
            echo "api-developer"
            ;;
        "refactoring-expert")
            echo "python-optimizer"
            ;;
        "planner"|"architect"|"backend-developer"|"api-developer"|"frontend-developer"|"test-engineer"|"security-engineer"|"quality-engineer"|"reviewer"|"debugger"|"data-analyst"|"supabase-expert"|"performance-profiler"|"excel-reporter"|"python-optimizer"|"monitoring-specialist"|"vnx-manager"|"t0-orchestrator")
            # Already native skill names - pass through
            echo "$role"
            ;;
        *)
            # Unknown role - pass through (log to stderr to avoid corrupting subshell capture)
            log "V8 WARNING: Unknown role '$role' - using as-is (may fail skill activation)" >&2
            echo "$role"
            ;;
    esac
}

# ===== V8 CORE DISPATCH FUNCTION =====

# Function to send dispatch with skill activation
dispatch_with_skill_activation() {
    local dispatch_file="$1"
    local track="$2"
    local agent_role="$3"
    local intelligence_data="${4:-}"  # Optional intelligence JSON
    local dispatch_id="${5:-}"

    if [ -z "$dispatch_id" ]; then
        dispatch_id="$(basename "$dispatch_file" .md)"
    fi

    # Determine target terminal pane (MCP-aware routing)
    local requires_mcp
    requires_mcp=$(vnx_dispatch_extract_requires_mcp "$dispatch_file")
    local target_pane
    if ! target_pane=$(determine_executor "$track" "normal" "$requires_mcp"); then
        log "V8 ERROR: Failed to determine target terminal"
        return 1
    fi

    log "V8 DISPATCH: Routing to terminal $target_pane (Track: $track, Role: $agent_role)"

    # Configure terminal mode (clear, model switch, mode activation)
    if ! configure_terminal_mode "$target_pane" "$dispatch_file"; then
        log_structured_failure "mode_configuration_failed" "Terminal mode configuration failed" "pane=$target_pane dispatch=$(basename "$dispatch_file")"
        return 1
    fi

    # CRITICAL: Add delay after mode configuration to ensure commands complete
    # This matches V7 behavior where mode changes need time to settle
    sleep 2

    # Pre-clear: ensure terminal input line is empty before skill activation
    # NOTE: Do NOT use C-c here — it kills the CLI process, leaving a bare
    # zsh shell where dispatch content gets executed as shell commands.
    # C-u alone safely clears the readline input buffer.
    tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
    sleep 0.5

    # Map role to skill name
    local skill_name=$(map_role_to_skill "$agent_role")
    if [ -z "$skill_name" ]; then
        log "V8 WARNING: Empty skill name for role '$agent_role' (waiting for edit)"
        if ! grep -q "\[SKILL_INVALID\]" "$dispatch_file"; then
            echo -e "\n\n[SKILL_INVALID] Skill for role '$agent_role' not found. Update Role and remove this marker to retry.\n" >> "$dispatch_file"
        fi
        return 1
    fi

    # Validate skill against skills.yaml before dispatching
    if ! python3 "$VNX_DIR/scripts/validate_skill.py" "$skill_name" >/dev/null 2>&1; then
        log "V8 WARNING: Skill '@${skill_name}' not found in skills.yaml (waiting for edit)"
        if ! grep -q "\[SKILL_INVALID\]" "$dispatch_file"; then
            echo -e "\n\n[SKILL_INVALID] Skill '@${skill_name}' not found in skills.yaml. Update Role and remove this marker to retry.\n" >> "$dispatch_file"
        fi
        return 1
    fi

    log "V8 SKILL: Activating skill @$skill_name for role $agent_role"

    # Extract instruction content
    local instruction_content
    if ! instruction_content=$(extract_instruction_content "$dispatch_file"); then
        log "V8 ERROR: Failed to extract instruction content"
        return 1
    fi

    if [ -z "$instruction_content" ]; then
        log "V8 ERROR: No instruction content found in dispatch"
        return 1
    fi

    # Extract context files (Workflow + Context lines with @ references)
    local context_files=$(extract_context_files "$dispatch_file")
    if [ -n "$context_files" ]; then
        log "V8 CONTEXT: Extracted context files from dispatch"
    fi

    # Extract metadata for receipt
    local phase=$(extract_phase "$dispatch_file")
    local gate=$(extract_new_gate "$dispatch_file")
    local task_id=$(extract_task_id "$dispatch_file" "$track")
    local cmd_id=$(uuidgen 2>/dev/null || echo "$(date +%s)-$$" | sha256sum | cut -c1-16)

    # Fallback to planning if no gate specified
    if [ -z "$gate" ]; then
        log "V8: No gate specified, defaulting to 'planning'"
        gate="planning"
    fi

    # Generate receipt footer
    local receipt_footer
    if ! receipt_footer=$(generate_receipt_footer "$dispatch_file" "$track" "$phase" "$gate" "$task_id" "$cmd_id" "$dispatch_id"); then
        log "V8 WARNING: Failed to generate receipt footer, continuing without"
        receipt_footer=""
    fi

    # Format intelligence data if provided
    local intelligence_section=""
    if [ -n "$intelligence_data" ]; then
        # Extract pattern summaries (title + description, max 5 patterns)
        local pattern_summaries=$(echo "$intelligence_data" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    patterns = data.get("suggested_patterns", [])[:5]  # Top 5 patterns
    if patterns:
        print("### 🧠 Relevant Patterns\n")
        for p in patterns:
            title = p.get("title", "Unknown")
            desc = p.get("description", "")[:100]
            rel = p.get("relevance_score", 0)
            fp = p.get("file_path", "")
            lr = p.get("line_range", "")
            loc = f" @ `{fp}:{lr}`" if fp and lr else ""
            print(f"- **{title}** (relevance: {rel:.2f}): {desc}{loc}")
except (json.JSONDecodeError, TypeError) as exc:
    print(f"[NON_CRITICAL] pattern_summary_parse_failed: {exc}", file=sys.stderr)
' 2>/dev/null)

        # Extract prevention rules
        local prevention_summaries=$(echo "$intelligence_data" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    rules = data.get("prevention_rules", [])[:3]  # Top 3 rules
    if rules:
        print("\n### ⚠️ Prevention Rules\n")
        for r in rules:
            print(f"- {r}")
except (json.JSONDecodeError, TypeError) as exc:
    print(f"[NON_CRITICAL] prevention_summary_parse_failed: {exc}", file=sys.stderr)
' 2>/dev/null)

        # Combine intelligence sections if they exist
        if [ -n "$pattern_summaries" ] || [ -n "$prevention_summaries" ]; then
            intelligence_section="

---
## Intelligence Context

$pattern_summaries$prevention_summaries

---
"
        fi
    fi

    # Build context section if files were specified
    local context_section=""
    if [ -n "$context_files" ]; then
        context_section="

---
## Context Files

Read the following files for context before starting:

$context_files

---
"
    fi

    # Resolve terminal_id and provider early (needed for skill command format)
    local terminal_id
    if ! terminal_id="$(get_terminal_from_pane "$target_pane" 2>/dev/null)"; then
        terminal_id=""
        log_structured_failure "terminal_resolution_failed" "Failed to resolve terminal id from pane" "pane=$target_pane"
    fi
    if [ -z "$terminal_id" ] || [ "$terminal_id" = "UNKNOWN" ]; then
        terminal_id="$(track_to_terminal "$track")"
    fi

    if [ -z "$terminal_id" ]; then
        log "V8 LOCK: unable to resolve terminal for track=$track dispatch=$dispatch_id"
        return 1
    fi

    local provider
    provider=$(get_terminal_provider "$terminal_id")

    # Extract PR-ID early so it can be included in the prompt
    local pr_id=$(extract_pr_id "$dispatch_file")

    # BUILD COMPLETE PROMPT: skill activation + context + intelligence + instruction + receipt
    # V8.1: Hybrid dispatch - skill via send-keys, instruction via paste-buffer
    # Provider-aware skill invocation:
    #   Claude Code: /skill-name  (slash command)
    #   Codex CLI:   $skill-name  (dollar-sign mention)
    #   Gemini CLI:  @skill-name  (at-sign prefix, also auto-activates on description match)
    local skill_command
    local extra_skills_hint
    case "$provider" in
        codex_cli|codex)
            skill_command="\$${skill_name} "
            extra_skills_hint="Use additional skills as needed (\$test-engineer, \$reviewer, \$debugger) to deliver production-quality results."
            ;;
        gemini_cli|gemini)
            skill_command="@${skill_name} "
            extra_skills_hint="Use additional skills as needed (@test-engineer, @reviewer, @debugger) to deliver production-quality results."
            ;;
        *)
            skill_command="/${skill_name} "
            extra_skills_hint="Use additional skills as needed (/test-engineer, /reviewer, /debugger) to deliver production-quality results."
            ;;
    esac

    log "V8 SKILL_FORMAT: provider=$provider command='${skill_command}'"

    # Build dispatch header so workers know what they're working on
    local dispatch_header="## Dispatch Assignment
| Field | Value |
|-------|-------|
| **PR** | ${pr_id:-unknown} |
| **Dispatch-ID** | ${dispatch_id} |
| **Track** | ${track} |
| **Gate** | ${gate} |
"

    local complete_prompt="${dispatch_header}
Apply your specialized expertise to this task.

**Critical Success Factors:**
- Maintain high code quality standards and best practices
- Ensure comprehensive test coverage where applicable
- Follow established project patterns and conventions
- Validate all changes against requirements
- Document significant design decisions

$extra_skills_hint
$context_section$intelligence_section
$instruction_content

$receipt_footer"

    if ! terminal_lock_allows_dispatch "$terminal_id" "$dispatch_id"; then
        return 1
    fi

    if ! acquire_terminal_claim "$terminal_id" "$dispatch_id"; then
        return 1
    fi

    # V8 CORE: Hybrid dispatch - skill via send-keys, instruction via paste-buffer
    log "V8 DISPATCH: Activating skill '${skill_command}' + pasting instruction"

    # Step 1: Type skill command via send-keys (triggers skill activation)
    # Use -l (literal) for providers that use $ prefix to prevent tmux key interpretation
    if ! tmux_send_best_effort "$target_pane" -l "$skill_command"; then
        if ! release_terminal_claim "$terminal_id" "$dispatch_id"; then
            log_structured_failure "claim_release_failed" "Failed to release claim after skill send failure" "terminal=$terminal_id dispatch=$dispatch_id"
        fi
        log "V8 ERROR: Failed to send skill command to terminal $target_pane"
        return 1
    fi

    # Allow CLI to render the skill command before pasting instruction
    sleep 0.5

    # Step 2: Load instruction into buffer and paste after typed skill command
    if ! echo "$complete_prompt" | tmux load-buffer -; then
        if ! release_terminal_claim "$terminal_id" "$dispatch_id"; then
            log_structured_failure "claim_release_failed" "Failed to release claim after tmux load failure" "terminal=$terminal_id dispatch=$dispatch_id"
        fi
        log "V8 ERROR: Failed to load prompt to tmux buffer"
        return 1
    fi

    if ! tmux paste-buffer -t "$target_pane"; then
        if ! release_terminal_claim "$terminal_id" "$dispatch_id"; then
            log_structured_failure "claim_release_failed" "Failed to release claim after tmux paste failure" "terminal=$terminal_id dispatch=$dispatch_id"
        fi
        log "V8 ERROR: Failed to paste prompt to terminal $target_pane"
        return 1
    fi

    # Add delay before Enter to ensure content is fully pasted and rendered
    sleep 1

    if ! tmux send-keys -t "$target_pane" Enter; then
        if ! release_terminal_claim "$terminal_id" "$dispatch_id"; then
            log_structured_failure "claim_release_failed" "Failed to release claim after Enter failure" "terminal=$terminal_id dispatch=$dispatch_id"
        fi
        log "V8 ERROR: Failed to send Enter to terminal $target_pane"
        return 1
    fi

    log "V8 DISPATCH: Successfully sent dispatch to $target_pane"

    # Update progress_state.yaml (from V7)
    local filename
    filename=$(basename "$dispatch_file")

    if [ -f "$VNX_DIR/scripts/update_progress_state.py" ]; then
        log "V8 PROGRESS_STATE: Updating Track $track → gate=$gate, status=working, dispatch_id=$dispatch_id"

        if python3 "$VNX_DIR/scripts/update_progress_state.py" \
            --track "$track" \
            --gate "$gate" \
            --status working \
            --dispatch-id "$dispatch_id" \
            --updated-by dispatcher 2>&1; then
            log "V8 PROGRESS_STATE: ✅ Successfully updated progress_state.yaml for Track $track"
        else
            log "V8 PROGRESS_STATE: ⚠️  Failed to update progress_state.yaml (non-fatal)"
        fi
    else
        log "V8 PROGRESS_STATE: ⚠️  update_progress_state.py not found (non-fatal)"
    fi

    # Notify heartbeat ACK monitor (from V7)
    # pr_id already extracted earlier for inclusion in dispatch prompt
    python3 "$VNX_DIR/scripts/notify_dispatch.py" "$dispatch_id" "$terminal_id" "$dispatch_id" "$pr_id" 2>/dev/null || {
        log "V8 WARNING: Failed to notify heartbeat ACK monitor (non-fatal)"
    }

    # Log dispatch metadata to quality_intelligence.db (non-fatal)
    local _dm_cognition _dm_priority _dm_pattern_count _dm_rule_count _dm_instr_chars
    _dm_cognition=$(vnx_dispatch_extract_cognition "$dispatch_file" 2>/dev/null || echo "normal")
    _dm_priority=$(vnx_dispatch_extract_priority "$dispatch_file" 2>/dev/null || echo "P1")
    _dm_pattern_count=$(echo "$intelligence_data" | grep -o '"pattern_count":[0-9]*' | grep -o '[0-9]*' || echo "0")
    _dm_rule_count=$(echo "$intelligence_data" | grep -o '"prevention_rule_count":[0-9]*' | grep -o '[0-9]*' || echo "0")
    _dm_instr_chars=${#instruction_content}
    # Extract OI-NNN references from dispatch instructions for target tracking
    local _dm_target_oi=""
    _dm_target_oi=$(echo "$instruction_content" | grep -oE 'OI-[0-9]{3,}' | sort -u | paste -sd ',' - 2>/dev/null || echo "")
    python3 "$VNX_DIR/scripts/log_dispatch_metadata.py" \
        --dispatch-id "$dispatch_id" \
        --terminal "$terminal_id" \
        --track "$track" \
        --role "$agent_role" \
        --skill-name "$agent_role" \
        --gate "$gate" \
        --cognition "$_dm_cognition" \
        --priority "$_dm_priority" \
        --pr-id "${pr_id:-}" \
        --pattern-count "${_dm_pattern_count:-0}" \
        --prevention-rule-count "${_dm_rule_count:-0}" \
        --intelligence-json "$intelligence_data" \
        --instruction-char-count "${_dm_instr_chars:-0}" \
        --target-open-items "${_dm_target_oi:-}" 2>/dev/null || {
        log "V8 WARNING: Failed to log dispatch metadata (non-fatal)"
    }

    # Move dispatch to active (receipt_processor moves to completed on task_complete)
    mv "$dispatch_file" "$ACTIVE_DIR/$filename"

    log "V8 DISPATCH: Activated - moved to $ACTIVE_DIR/$filename"
    return 0
}

# ===== INTELLIGENCE INTEGRATION (V7.4) =====

# Function to process pending dispatches with intelligence
process_dispatches() {
    local count=0

    # Clean up stuck files in active directory (older than 1 hour)
    while IFS= read -r stuck_file; do
        if [ -f "$stuck_file" ]; then
            log "V8: Moving stuck file to completed: $(basename "$stuck_file")"
            if ! mv "$stuck_file" "$COMPLETED_DIR/" 2>/dev/null; then
                log_structured_failure "stuck_file_move_failed" "Failed to move stuck file to completed" "file=$stuck_file"
            fi
        fi
    done < <(find "$ACTIVE_DIR" -name "*.md" -type f -mmin +60 2>/dev/null || :)

    for dispatch in "$PENDING_DIR"/*.md; do
        [ -f "$dispatch" ] || continue

        local agent_role=$(extract_agent_role "$dispatch")
        log "V8: Processing dispatch: $(basename "$dispatch") (Role: $agent_role)"

        # Skip dispatches waiting for manual skill fix
        if grep -q "\[SKILL_INVALID\]" "$dispatch"; then
            log "V8 WARNING: Dispatch $(basename "$dispatch") blocked due to invalid skill (waiting for edit)"
            continue
        fi

        # V7.4 INTELLIGENCE: Validate agent if specified
        if [ -n "$agent_role" ] && [ "$agent_role" != "none" ] && [ "$agent_role" != "None" ]; then
            # Validate agent using intelligence gatherer
            local validation_rc=0
            set +e
            validation_result=$(python3 "$VNX_DIR/scripts/gather_intelligence.py" validate "$agent_role" 2>&1)
            validation_rc=$?
            set -e

            if [ "$validation_rc" -ne 0 ]; then
                log_structured_failure "agent_validation_dependency_failed" "Agent validation command failed; dispatch blocked" "role=$agent_role rc=$validation_rc"
                if ! grep -q "\[DEPENDENCY_ERROR\]" "$dispatch"; then
                    echo -e "\n\n[DEPENDENCY_ERROR] gather_intelligence validate failed (rc=$validation_rc). Resolve runtime dependency and retry.\n" >> "$dispatch"
                fi
                continue
            fi

            # Check if validation failed
            if echo "$validation_result" | grep -q '"valid": false'; then
                log "V8 ERROR: Agent validation failed for '$agent_role'"
                log "Validation result: $validation_result"

                # Extract suggested agent
                suggested=$(echo "$validation_result" | grep -o '"suggestion": "[^"]*"' | cut -d'"' -f4)
                log "Suggested agent: $suggested"

                # Mark for manual fix (do not reject)
                if ! grep -q "\[SKILL_INVALID\]" "$dispatch"; then
                    echo -e "\n\n[SKILL_INVALID] Skill '$agent_role' not found. Suggested: '$suggested'. Update Role and remove this marker to retry.\n" >> "$dispatch"
                fi
                continue
            else
                log "V8: Agent validated: $agent_role"
            fi
        fi

        # Extract metadata
        local track=$(extract_track "$dispatch")
        local cognition=$(extract_cognition "$dispatch")
        local priority=$(extract_priority "$dispatch")
        local gate=$(extract_new_gate "$dispatch")
        local dispatch_id
        dispatch_id="$(basename "$dispatch" .md)"

        if [ -z "$track" ]; then
            log "V8 WARNING: No track found in dispatch, skipping"
            mv "$dispatch" "$REJECTED_DIR/"
            continue
        fi

        # Never send to T0
        if [ "$track" = "0" ] || [ "$track" = "T0" ]; then
            log "V8 ERROR: Attempting to dispatch to T0 - BLOCKED"
            mv "$dispatch" "$REJECTED_DIR/"
            continue
        fi

        local target_terminal
        target_terminal="$(track_to_terminal "$track")"
        if [ -z "$target_terminal" ]; then
            log "V8 ERROR: Invalid track '$track' for dispatch $(basename "$dispatch")"
            mv "$dispatch" "$REJECTED_DIR/"
            continue
        fi

        if ! terminal_lock_allows_dispatch "$target_terminal" "$dispatch_id"; then
            log "V8 LOCK: deferring $(basename "$dispatch") until terminal $target_terminal is unlocked"
            continue
        fi

        # V7.4 INTELLIGENCE: Gather intelligence for dispatch
        local intel_result=""
        if [ -f "$VNX_DIR/scripts/gather_intelligence.py" ]; then
            log "V8 INTELLIGENCE: Gathering intelligence for dispatch"

            # Extract task description for intelligence gathering
            local task_description=$(extract_instruction_content "$dispatch")

            # Gather intelligence (convert track letter to terminal ID)
            local terminal
            terminal=$(track_to_terminal "$track")
            local intel_rc=0
            set +e
            intel_result=$(python3 "$VNX_DIR/scripts/gather_intelligence.py" gather "$task_description" "$terminal" "$agent_role" "$gate" 2>&1)
            intel_rc=$?
            set -e

            if [ "$intel_rc" -ne 0 ]; then
                log_structured_failure "intelligence_gather_failed" "Intelligence gather command failed; dispatch blocked" "dispatch=$dispatch_id terminal=$terminal rc=$intel_rc"
                if ! grep -q "\[DEPENDENCY_ERROR\]" "$dispatch"; then
                    echo -e "\n\n[DEPENDENCY_ERROR] gather_intelligence gather failed (rc=$intel_rc). Resolve runtime dependency and retry.\n" >> "$dispatch"
                fi
                continue
            fi

            # Parse JSON results for logging
            local pattern_count=$(echo "$intel_result" | grep '"pattern_count":' | grep -o '[0-9]*' | head -1 || echo "0")
            local prevention_rules=$(echo "$intel_result" | grep '"prevention_rule_count":' | grep -o '[0-9]*' | head -1 || echo "0")

            log "V8 INTELLIGENCE: Gathered $pattern_count patterns, $prevention_rules rules → injecting into prompt"

            # Intelligence is now passed to dispatch_with_skill_activation
            # and injected directly into the terminal prompt (not the dispatch file)
        fi

        # Send dispatch with skill activation + intelligence injection (V8 core)
        if ! dispatch_with_skill_activation "$dispatch" "$track" "$agent_role" "$intel_result" "$dispatch_id"; then
            if grep -q "\[SKILL_INVALID\]" "$dispatch"; then
                log "V8 WARNING: Dispatch blocked due to invalid skill (waiting for edit): $(basename "$dispatch")"
                continue
            fi
            log "V8 ERROR: Dispatch failed for $(basename "$dispatch")"
            if [ -f "$dispatch" ]; then
                echo -e "\n\n[REJECTED: Dispatch failed during execution]\n" >> "$dispatch"
                mv "$dispatch" "$REJECTED_DIR/"
            fi
            continue
        fi

        ((count++))

        # Small delay between dispatches
        sleep 1
    done

    if [ $count -gt 0 ]; then
        log "V8: Processed $count dispatches"
    fi
}

# Main loop
log "Dispatcher V8 MINIMAL ready. Monitoring $PENDING_DIR for dispatches..."
log "V8 Features: Native skills + instruction-only dispatch (~200 tokens vs 1500 in V7) + multi-provider skill format"
log "V8 Maintains: Mode control, model switching, intelligence v7.4, receipt tracking"
log "Track routing: A→T1(%1), B→T2(%2), C→T3(%3)"

# Get initial pane IDs (non-fatal)
if ! get_pane_ids; then
    log_structured_failure "pane_refresh_failed" "Initial pane ID refresh failed" "phase=startup"
fi

while true; do
    # Update pane IDs periodically (non-fatal)
    if ! get_pane_ids; then
        log_structured_failure "pane_refresh_failed" "Periodic pane ID refresh failed" "phase=loop"
    fi

    # Process any pending dispatches
    process_dispatches

    # Wait before next check
    sleep 2
done
