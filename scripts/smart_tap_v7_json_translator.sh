#!/bin/bash
# Smart Tap V7 with JSON Translation Layer
# Supports both Markdown (legacy) and JSON (new) dispatch formats
# Translates JSON to Markdown for popup display and terminal delivery
# Uses bulletproof singleton enforcer with flock

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"

# Configuration first
VNX_DIR="$VNX_HOME"

# Source the singleton enforcer
source "$VNX_DIR/scripts/singleton_enforcer.sh"

# Enforce singleton - will exit if another instance is running
enforce_singleton "smart_tap_multi"

# Configuration (after singleton enforcer)
VNX_DIR="$VNX_HOME"
DISPATCH_DIR="$VNX_DISPATCH_DIR"
QUEUE_DIR="$VNX_DISPATCH_DIR/queue"
STATE_DIR="$VNX_STATE_DIR"
LOG_FILE="$VNX_LOGS_DIR/tap.log"
PID_DIR="$VNX_PIDS_DIR"
PID_FILE="$PID_DIR/smart_tap.pid"

# Singleton is now handled by enforce_singleton function above
# No need for manual PID file handling
echo "[SMART_TAP] Process management handled by singleton enforcer"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Initialize
mkdir -p "$QUEUE_DIR" "$STATE_DIR" "$(dirname "$LOG_FILE")"

# Source smart pane manager for self-healing pane discovery
source "$VNX_DIR/scripts/pane_manager_v2.sh"

# Get T0 pane ID from dynamic discovery (prefers attached session).
# Do not pin to panes.json here, because that can drift to an unattached session.
T0_PANE=$(get_pane_id_smart "T0")

echo -e "${GREEN}Smart Tap (Multi-Block) starting...${NC}"
echo "Monitoring T0 pane: $T0_PANE"
echo "Queue directory: $QUEUE_DIR"

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
    echo "[$(date '+%H:%M:%S')] $1"
}

# Function to detect if content is JSON format
is_json() {
    local content="$1"
    # Check if content starts with { and contains expected JSON fields
    echo "$content" | grep -q '^\s*{\s*"dispatch_format":\s*"json"' 2>/dev/null
}

# Function to translate JSON dispatch to Markdown
json_to_markdown() {
    local json_file="$1"
    
    # Use jq if available, otherwise fallback to basic parsing
    if command -v jq &> /dev/null; then
        # Validate JSON first
        if ! jq empty "$json_file" 2>/dev/null; then
            echo "ERROR: Invalid JSON format"
            return 1
        fi
        
        # Extract fields from JSON - Use the tested simpler format
        local track=$(jq -r '.metadata.track // "unknown"' "$json_file")
        local phase=$(jq -r '.metadata.phase // ""' "$json_file")
        local gate=$(jq -r '.metadata.gate // ""' "$json_file")
        local priority=$(jq -r '.metadata.priority // "normal"' "$json_file")
        local title=$(jq -r '.content.title // ""' "$json_file")
        local objective=$(jq -r '.content.objective // ""' "$json_file")
        local instructions=$(jq -r '.content.instructions // ""' "$json_file")
        local context=$(jq -r '.content.context // ""' "$json_file")
        local cognition=$(jq -r '.metadata.cognition // "normal"' "$json_file")
        
        # Build Markdown format - Same as tested format
        echo "[[TARGET:$track]]"
        [ -n "$phase" ] && [ "$phase" != "null" ] && echo "Phase: $phase"
        [ -n "$gate" ] && [ "$gate" != "null" ] && echo "Gate: $gate"
        [ -n "$title" ] && [ "$title" != "null" ] && echo "Doel: $title"
        echo "Priority: $priority; Cognition: $cognition"
        [ -n "$objective" ] && [ "$objective" != "null" ] && echo "Objective: $objective"
        [ -n "$context" ] && [ "$context" != "null" ] && echo "Context: $context"
        [ -n "$instructions" ] && [ "$instructions" != "null" ] && echo "Instructions: $instructions"
        echo "[[DONE]]"
    else
        # Fallback: Basic parsing without jq
        # Extract key fields using grep and sed
        local track=$(grep -o '"track"[[:space:]]*:[[:space:]]*"[^"]*"' "$json_file" | sed 's/.*"\([^"]*\)"$/\1/')
        local dispatch_id=$(grep -o '"dispatch_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$json_file" | sed 's/.*"\([^"]*\)"$/\1/')
        local timestamp=$(grep -o '"timestamp"[[:space:]]*:[[:space:]]*"[^"]*"' "$json_file" | sed 's/.*"\([^"]*\)"$/\1/')
        local title=$(grep -o '"title"[[:space:]]*:[[:space:]]*"[^"]*"' "$json_file" | sed 's/.*"\([^"]*\)"$/\1/')
        local instructions=$(grep -o '"instructions"[[:space:]]*:[[:space:]]*"[^"]*"' "$json_file" | sed 's/.*"\([^"]*\)"$/\1/')
        
        # Construct basic Markdown
        cat <<EOF
[[TARGET:${track}]]
[[DISPATCH_ID:${dispatch_id}]]
[[TIMESTAMP:${timestamp}]]

---

## Task: ${title}

### Instructions
${instructions}

[[DONE]]
EOF
    fi
}

# Function to translate Markdown dispatch to JSON
markdown_to_json() {
    local markdown_content="$1"
    local temp_file="/tmp/md_to_json_${RANDOM}.tmp"
    echo "$markdown_content" > "$temp_file"

    # Extract fields from Markdown using pattern matching
    local track=$(echo "$markdown_content" | sed -n 's/.*\[\[TARGET:\([^]]*\)\]].*/\1/p' | head -1)

    # Extract Role - handle both separate line and inline formats
    local role=$(echo "$markdown_content" | sed -n 's/^Role: *\([^ ]*\).*/\1/p' | head -1)

    # Extract Workflow - handle both separate line and inline formats
    # First try: Workflow at start of line
    local workflow=$(echo "$markdown_content" | sed -n 's/^Workflow: *\(.*\)/\1/p' | head -1)
    # Second try: Workflow after Role on same line
    if [ -z "$workflow" ]; then
        workflow=$(echo "$markdown_content" | sed -n 's/^Role:.*Workflow: *\(.*\)/\1/p' | head -1)
    fi
    local context=$(echo "$markdown_content" | sed -n 's/^Context: *\(.*\)/\1/p' | head -1)
    local previous_gate=$(echo "$markdown_content" | sed -n 's/^Previous Gate: *\(.*\)/\1/p' | head -1)
    local gate=$(echo "$markdown_content" | sed -n 's/^Gate: *\(.*\)/\1/p' | head -1)
    local priority_line=$(echo "$markdown_content" | sed -n 's/^Priority: *\(.*\)/\1/p' | head -1)

    # Parse priority and cognition from "Priority: P0; Cognition: deep" format
    local priority=$(echo "$priority_line" | sed 's/;.*$//' | sed 's/Priority: *//')
    local cognition=$(echo "$priority_line" | sed -n 's/.*Cognition: *\([^;]*\).*/\1/p')

    # Extract instruction content (everything between "Instruction:" and "[[DONE]]")
    local instructions=$(echo "$markdown_content" | sed -n '/^Instruction:/,/\[\[DONE\]\]/p' | sed '1d' | sed '$d' | sed 's/^- //' | tr '\n' ' ')

    # Generate dispatch ID and timestamp
    local dispatch_id=$(date +%Y%m%d-%H%M%S)-$(uuidgen | cut -c1-8 | tr '[:upper:]' '[:lower:]')
    local timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

    # Construct JSON format for state management
    cat <<EOF
{
  "dispatch_format": "json",
  "dispatch_id": "$dispatch_id",
  "timestamp": "$timestamp",
  "metadata": {
    "track": "$track",
    "role": "$role",
    "workflow": "$workflow",
    "previous_gate": "$previous_gate",
    "gate": "$gate",
    "priority": "$priority",
    "cognition": "$cognition"
  },
  "content": {
    "context": "$context",
    "instructions": "$instructions"
  },
  "state": {
    "status": "queued",
    "created_at": "$timestamp",
    "processed": false
  }
}
EOF

    rm -f "$temp_file"
}

# Function to handle format detection and basic cleaning (no conversion needed)
process_dispatch_content() {
    local content="$1"
    local temp_file="/tmp/dispatch_${RANDOM}.tmp"

    echo "$content" > "$temp_file"

    # Just return the content as-is - conversion happens in save_to_queue
    rm -f "$temp_file"
    echo "$content"
}

# Function to extract ALL manager blocks from captured content
extract_all_manager_blocks() {
    local content="$1"
    local temp_file="/tmp/block_extract_$$.txt"
    
    # Write content to temp file for processing
    echo "$content" > "$temp_file"
    
    # Find ALL complete blocks - now supports both JSON and Markdown
    # JSON blocks: { "dispatch_format": "json" ... }
    # Markdown blocks: [[TARGET:X]] ... [[DONE]]
    awk '
        BEGIN { in_block=0; block_count=0; json_block=0 }
        
        # Match JSON dispatch start
        /^\s*{\s*"dispatch_format":\s*"json"/ {
            json_block=1
            in_block=1
            block_count++
            blocks[block_count] = $0
            block_types[block_count] = "json"
            brace_count = 1
            next
        }
        
        # Track JSON braces for complete JSON object
        json_block {
            blocks[block_count] = blocks[block_count] "\n" $0
            # Count opening and closing braces
            for (i = 1; i <= length($0); i++) {
                ch = substr($0, i, 1)
                if (ch == "{") brace_count++
                if (ch == "}") {
                    brace_count--
                    if (brace_count == 0) {
                        json_block = 0
                        in_block = 0
                    }
                }
            }
            next
        }
        
        # Match TARGET marker for Markdown blocks (only A, B, or C individually, not A|B|C template)
        /.*\[\[TARGET:[ABC]\]\]/ {
            if (!json_block) {
                in_block=1
                block_count++
                blocks[block_count] = $0
                block_types[block_count] = "markdown"
            }
            next
        }
        
        # Match DONE marker for Markdown blocks
        /\[\[DONE\]\]/ {
            if (in_block && !json_block) {
                blocks[block_count] = blocks[block_count] "\n" $0
                in_block=0
            }
            next
        }
        
        # Collect lines when inside a block
        in_block && !json_block {
            blocks[block_count] = blocks[block_count] "\n" $0
        }
        
        END {
            # Output ALL complete blocks separated by delimiter
            found_any = 0
            for (i = 1; i <= block_count; i++) {
                # Check if block is complete
                is_complete = 0
                if (block_types[i] == "markdown" && index(blocks[i], "[[DONE]]") > 0) {
                    is_complete = 1
                } else if (block_types[i] == "json") {
                    # Basic check for complete JSON (has closing brace)
                    is_complete = 1
                }
                
                if (is_complete) {
                    if (found_any) print "===BLOCK_SEPARATOR==="
                    print blocks[i]
                    found_any = 1
                }
            }
        }
    ' "$temp_file"
    
    rm -f "$temp_file"
}

# Function to check if block is valid
validate_block() {
    local block="$1"
    
    # Check if it's JSON format
    if is_json "$block"; then
        # Validate JSON structure
        # Check for required fields using basic pattern matching
        if [[ "$block" == *'"dispatch_format"'* ]] && \
           [[ "$block" == *'"metadata"'* ]] && \
           [[ "$block" == *'"track"'* ]] && \
           [[ "$block" == *'"content"'* ]]; then
            
            # Reject test/example JSON blocks
            if [[ "$block" == *'"test"'* ]] || \
               [[ "$block" == *'"example"'* ]]; then
                log "Rejected test/example JSON block"
                return 1
            fi
            
            return 0
        else
            log "Invalid JSON structure - missing required fields"
            return 1
        fi
    else
        # Markdown validation (existing logic)
        if [[ "$block" == *"[[TARGET:"* ]] && [[ "$block" == *"[[DONE]]"* ]]; then
            # Additional validation: reject blocks that are literal examples/tests
            # NOTE: Do NOT filter on "/Desktop/Screenshot" — real dispatches can
            # reference screenshot paths as context for the target terminal.
            if [[ "$block" == *"example block"* ]] || \
               [[ "$block" == *"test block"* ]]; then
                log "Rejected example/test block"
                return 1
            fi
            
            # Reject blocks that are too short (less than 3 lines of actual content)
            local line_count=$(echo "$block" | grep -v "^\s*$" | wc -l)
            if [ "$line_count" -lt 4 ]; then
                log "Rejected block - too short (only $line_count lines)"
                return 1
            fi
            
            return 0
        else
            return 1
        fi
    fi
}

# Function to generate unique ID for dispatch
generate_dispatch_id() {
    echo "$(date +%Y%m%d-%H%M%S)-$(uuidgen | cut -c1-8 | tr '[:upper:]' '[:lower:]')"
}

# Function to save block to queue
save_to_queue() {
    local block="$1"
    local processed_block=""
    
    # Clean markdown for workers (they prefer markdown)
    if is_json "$block"; then
        log "Processing JSON dispatch, converting to Markdown for worker"
        local temp_file="/tmp/json_block_${RANDOM}.tmp"
        echo "$block" > "$temp_file"
        processed_block=$(json_to_markdown "$temp_file")
        rm -f "$temp_file"
    else
        # Markdown block - clean it for workers
        processed_block=$(echo "$block" | while IFS= read -r line; do
            # Step 1: Remove ALL ANSI escape codes (more comprehensive)
            # Handle both [XXm and [38;2;R;G;Bm color codes
            line=$(echo "$line" | sed 's/\[[0-9;]*m//g' | sed 's/\x1b\[[0-9;]*[A-Za-z]//g')
            line=$(echo "$line" | sed 's/\[38;[0-9;]*m//g' | sed 's/\[48;[0-9;]*m//g')
            line=$(echo "$line" | sed 's/\[39m//g' | sed 's/\[49m//g')

            # Step 2: Remove ⏺ symbol and any spaces before/after it from beginning of line
            line=$(echo "$line" | sed 's/^[[:space:]]*⏺[[:space:]]*//' | sed 's/^[[:space:]]*//')

            # Step 3: Ensure [[TARGET: starts at the beginning of line if present
            # This fixes cases where [[TARGET: might have stuff before it
            if [[ "$line" == *"[[TARGET:"* ]]; then
                line=$(echo "$line" | sed 's/.*\(\[\[TARGET:[^]]*\]\]\)/\1/')
            fi

            echo "$line"
        done)
    fi
    
    # Extract track from processed block (now always Markdown format)
    local track=""
    if is_json "$block"; then
        # For JSON, extract track from original before conversion
        track=$(echo "$block" | grep -o '"track"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/')
    else
        # For Markdown, extract from processed block
        track=$(echo "$processed_block" | sed -n 's/.*\[\[TARGET:\([^]]*\)\]].*/\1/p' | head -1)
    fi
    
    if [ -z "$track" ]; then
        log "WARNING: Could not extract track from block"
        return 1
    fi
    
    # Generate unique filename
    local dispatch_id=$(generate_dispatch_id)
    local filename="${dispatch_id}-${track}.md"  # Always save as .md (Markdown)
    local filepath="$QUEUE_DIR/$filename"
    
    # Save the processed block (always Markdown format) to file
    echo "$processed_block" > "$filepath"
    
    log "✓ Queued dispatch for Track $track: $filename (Markdown format)"
    
    # DO NOT copy to pending - must go through authorization UI first!
    # User must accept in popup before dispatch moves from queue → pending
    
    return 0
}

# Keep track of processed block hashes to avoid duplicates (store multiple)
PROCESSED_HASHES_FILE="$STATE_DIR/processed_block_hashes.txt"
touch "$PROCESSED_HASHES_FILE"

# Function to maintain hash file (keep last 1000 entries to prevent re-queueing old blocks)
cleanup_hash_file() {
    local line_count=$(wc -l < "$PROCESSED_HASHES_FILE" 2>/dev/null || echo 0)
    if [ "$line_count" -gt 1000 ]; then
        # Keep only the last 1000 hashes
        tail -1000 "$PROCESSED_HASHES_FILE" > "$PROCESSED_HASHES_FILE.tmp"
        mv "$PROCESSED_HASHES_FILE.tmp" "$PROCESSED_HASHES_FILE"
        log "Cleaned hash file, kept last 1000 entries"
    fi
}

# Clean up old hashes (keep last 1000 to prevent re-queueing)
if [ -f "$PROCESSED_HASHES_FILE" ] && [ $(wc -l < "$PROCESSED_HASHES_FILE") -gt 1000 ]; then
    tail -1000 "$PROCESSED_HASHES_FILE" > "${PROCESSED_HASHES_FILE}.tmp"
    mv "${PROCESSED_HASHES_FILE}.tmp" "$PROCESSED_HASHES_FILE"
    log "Cleaned hash file, kept last 1000 entries"
fi

# Function to check if block was already processed
is_block_processed() {
    local hash="$1"
    grep -q "^$hash$" "$PROCESSED_HASHES_FILE" 2>/dev/null
}

# Function to mark block as processed
mark_block_processed() {
    local hash="$1"
    echo "$hash" >> "$PROCESSED_HASHES_FILE"
    
    # Keep last 1000 hashes (increased from 100 for better coverage)
    tail -1000 "$PROCESSED_HASHES_FILE" > "$PROCESSED_HASHES_FILE.tmp"
    mv "$PROCESSED_HASHES_FILE.tmp" "$PROCESSED_HASHES_FILE"
}

# Function to clean block for canonical hashing
canonicalize_block() {
    local block="$1"
    
    # Apply EXACT same cleaning as save_to_queue
    # Step 1: Remove ALL ANSI escape codes (more comprehensive patterns)
    # Handle both [XXm and [38;2;R;G;Bm color codes
    block=$(echo "$block" | sed 's/\[[0-9;]*m//g' | sed 's/\x1b\[[0-9;]*[A-Za-z]//g')
    block=$(echo "$block" | sed 's/\[38;[0-9;]*m//g' | sed 's/\[48;[0-9;]*m//g')
    block=$(echo "$block" | sed 's/\[39m//g' | sed 's/\[49m//g')
    
    # Step 2: Remove bracketed paste markers
    block=$(echo "$block" | sed 's/\x1b\[200~//g' | sed 's/\x1b\[201~//g')
    
    # Step 3: Remove ⏺ symbol and leading/trailing spaces
    block=$(echo "$block" | sed 's/^[[:space:]]*⏺[[:space:]]*//' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//')
    
    # Step 4: Normalize whitespace (collapse multiple spaces/tabs to single space)
    block=$(echo "$block" | sed 's/[[:space:]]\+/ /g')
    
    echo "$block"
}

# Function to process a single block
process_single_block() {
    local block="$1"
    
    # FIXED: Hash AFTER cleaning for consistency
    local cleaned_block=$(canonicalize_block "$block")
    local block_hash=$(echo "$cleaned_block" | sha256sum | cut -d' ' -f1)
    
    # Check if block was already processed
    if ! is_block_processed "$block_hash"; then
        # Validate block structure
        if validate_block "$block"; then
            # RE-ENABLED: Similar dispatch checking (max 5 per track in queue)
            local track=$(echo "$block" | sed -n 's/.*\[\[TARGET:\([^]]*\)\]].*/\1/p' | head -1)
            local existing_count=$(find "$QUEUE_DIR" -name "*-${track}.md" -type f 2>/dev/null | wc -l)

            if [ "$existing_count" -ge 5 ]; then
                log "⚠ Already 5 dispatches in queue for Track $track, skipping to prevent overflow"
                # CRITICAL FIX: Mark as processed to prevent infinite popup repeats
                mark_block_processed "$block_hash"
                return 1
            fi
            
            # Save to queue
            if save_to_queue "$block"; then
                # Mark as processed to prevent repeated popups
                mark_block_processed "$block_hash"
                return 0
            fi
        else
            log "Invalid block structure detected"
            return 1
        fi
    else
        # Already processed this exact block - don't show popup again
        return 1
    fi
}

# Function to process captured content
process_capture() {
    # ── Hard queue gate ───────────────────────────────────────────────────
    # Only process new Manager Blocks if queue is empty.
    # This prevents T0 from flooding the queue with multiple dispatches.
    # NOTE: active/ is NOT checked here — dispatches sit in active/ while
    # terminals work on them (pending→active→completed lifecycle). Blocking
    # on active/ would prevent the smart tap from ever picking up new blocks.
    local queue_count=$(ls "$DISPATCH_DIR/queue/" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$queue_count" -gt 0 ]; then
        return
    fi

    # Capture larger buffer to ensure complete blocks with [[DONE]] markers
    # Hash-based deduplication prevents reprocessing old blocks safely
    # Using -S -300 to capture sufficient lines for fast development and large manager blocks
    # Using -J to preserve wrapped lines
    # REMOVED -e to prevent ANSI codes in the capture
    local content=$(tmux capture-pane -t "$T0_PANE" -p -J -S -300 2>/dev/null || echo "")
    
    if [ -z "$content" ]; then
        return
    fi
    
    # Extract ALL manager blocks
    local all_blocks=$(extract_all_manager_blocks "$content")
    
    if [ -n "$all_blocks" ]; then
        # Process each block found
        local blocks_processed=0
        local blocks_found=0
        local current_block=""
        
        # Split blocks and process each one
        while IFS= read -r line; do
            if [[ "$line" == "===BLOCK_SEPARATOR===" ]]; then
                # Process the accumulated block
                if [ -n "$current_block" ]; then
                    blocks_found=$((blocks_found + 1))
                    if process_single_block "$current_block"; then
                        blocks_processed=$((blocks_processed + 1))
                    fi
                fi
                current_block=""
            else
                # Accumulate lines for current block
                if [ -z "$current_block" ]; then
                    current_block="$line"
                else
                    current_block="$current_block"$'\n'"$line"
                fi
            fi
        done <<< "$all_blocks"
        
        # Process the last block
        if [ -n "$current_block" ]; then
            blocks_found=$((blocks_found + 1))
            if process_single_block "$current_block"; then
                blocks_processed=$((blocks_processed + 1))
            fi
        fi
        
        # Report summary
        if [ $blocks_processed -gt 0 ]; then
            log "📦 Processed $blocks_processed new blocks out of $blocks_found found"
            
            # Show visual feedback
            echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo -e "${GREEN}✓ $blocks_processed manager block(s) captured and queued${NC}"
            echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            
            # Don't launch popup here - let the popup watcher handle it
            # Popup watcher will detect new queue items and show the UI
        fi
    fi
}

# Main monitoring loop
log "Smart Tap (Multi-Block) initialized - Monitoring T0 pane $T0_PANE"
log "Using capture-pane with multi-block extraction"

# Pane validation counter for periodic checks
VALIDATION_COUNTER=0
VALIDATION_INTERVAL=20  # Re-validate pane every 60 seconds (20 cycles × 3s)

while true; do
    # Periodic pane validation to handle pane ID changes after VNX restart
    if [ $VALIDATION_COUNTER -ge $VALIDATION_INTERVAL ]; then
        # Re-resolve T0 from panes.json/cache every interval.
        # This also fixes cross-project pane-id drift where old pane ids
        # still exist in another tmux session.
        # Safety: grep filters to only the tmux pane ID (%N) in case sourced
        # functions leak debug output to stdout (the _pm_log rename prevents
        # this, but belt-and-suspenders).
        NEW_PANE=$(get_pane_id_smart "T0" | grep -E '^%[0-9]+$' | tail -1)
        if [ -n "$NEW_PANE" ] && [ "$NEW_PANE" != "$T0_PANE" ]; then
            log "T0 pane updated: $T0_PANE → $NEW_PANE"
            T0_PANE="$NEW_PANE"
        elif ! tmux list-panes -a -F "#{pane_id}" 2>/dev/null | grep -q "^${T0_PANE}$"; then
            log "T0 pane $T0_PANE no longer exists and no replacement was resolved"
        fi
        VALIDATION_COUNTER=0
    else
        VALIDATION_COUNTER=$((VALIDATION_COUNTER + 1))
    fi

    # Process capture
    process_capture

    # Wait before next check
    sleep 3
done
