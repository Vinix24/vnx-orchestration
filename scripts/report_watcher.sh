#!/bin/bash
# DEPRECATED: report_watcher replaced by receipt_processor.sh
# Exit immediately to prevent duplicate receipts
exit 0

#############################################################################
# VNX Report Watcher - Markdown to Enhanced Receipt Converter
#############################################################################
# Watches for new markdown reports and generates enhanced NDJSON receipts
# Part of the Unified Reporting Strategy to eliminate duplicate effort
#
# Author: T-MANAGER
# Date: 2025-09-25
# Version: 1.0
#############################################################################

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
VNX_DIR="$VNX_HOME"
REPORTS_DIR="$VNX_REPORTS_DIR"
RECEIPTS_FILE="$VNX_STATE_DIR/t0_receipts.ndjson"
PARSER_SCRIPT="$VNX_DIR/scripts/report_parser.py"
APPEND_RECEIPT_SCRIPT="$VNX_DIR/scripts/append_receipt.py"
STATE_FILE="$VNX_STATE_DIR/report_watcher.state"
PID_FILE="$VNX_PIDS_DIR/report_watcher.pid"
LOG_FILE="$VNX_LOGS_DIR/report_watcher.log"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Ensure directories exist
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null
mkdir -p "$(dirname "$PID_FILE")" 2>/dev/null
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null
mkdir -p "$REPORTS_DIR" 2>/dev/null || true

# Singleton enforcement
if [ -f "$SCRIPT_DIR/singleton_enforcer.sh" ]; then
    # shellcheck source=singleton_enforcer.sh
    source "$SCRIPT_DIR/singleton_enforcer.sh"
    enforce_singleton "report_watcher.sh"
fi

# Store our PID for introspection
echo $$ > "$PID_FILE"
echo -e "${GREEN}[START]${NC} Report watcher starting (PID: $$)"

# Log function
log() {
    local level=$1
    shift
    local message="$@"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo -e "${level}[${timestamp}]${NC} $message" | tee -a "$LOG_FILE"
}

# Process a markdown report
process_report() {
    local report_path="$1"
    local report_name=$(basename "$report_path")

    log "$BLUE" "Processing report: $report_name"

    # Check if we've already processed this report
    if [ -f "$STATE_FILE" ]; then
        if grep -q "$report_name" "$STATE_FILE" 2>/dev/null; then
            log "$YELLOW" "Already processed: $report_name"
            return 0
        fi
    fi

    # Mark as processed FIRST to prevent race conditions from fswatch triggering multiple times
    echo "$report_name" >> "$STATE_FILE"

    # Extract enhanced receipt using Python parser
    if [ -f "$PARSER_SCRIPT" ]; then
        # Run parser and capture output (keep stderr visible for debugging)
        PARSER_OUTPUT=$(python "$PARSER_SCRIPT" "$report_path" 2>&1)
        RECEIPT=$(echo "$PARSER_OUTPUT" | grep '^{' | head -1)

        if [ -n "$RECEIPT" ]; then
            # Canonical append path for receipts.
            if printf '%s\n' "$RECEIPT" | python3 "$APPEND_RECEIPT_SCRIPT" 2>>"$LOG_FILE"; then
                log "$GREEN" "✅ Receipt generated: $report_name"
            else
                log "$RED" "❌ append_receipt.py failed: $report_name"
                return 1
            fi

            # Extract key fields for logging
            TERMINAL=$(echo "$RECEIPT" | grep -o '"terminal":"[^"]*"' | cut -d'"' -f4)
            TYPE=$(echo "$RECEIPT" | grep -o '"type":"[^"]*"' | cut -d'"' -f4)
            STATUS=$(echo "$RECEIPT" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)

            log "$GREEN" "  └─ Terminal: $TERMINAL | Type: $TYPE | Status: $STATUS"
        else
            log "$RED" "❌ Failed to parse: $report_name"
            log "$RED" "  └─ Parser output: $PARSER_OUTPUT"
        fi
    else
        log "$RED" "Parser script not found: $PARSER_SCRIPT"
        exit 1
    fi
}

# Process existing reports on startup
process_existing_reports() {
    log "$BLUE" "Checking for unprocessed reports..."

    local count=0
    local dirs=("$REPORTS_DIR")

    for dir in "${dirs[@]}"; do
        for report in "$dir"/*.md; do
            if [ -f "$report" ]; then
                local report_name=$(basename "$report")

                # Check if already processed
                if [ -f "$STATE_FILE" ]; then
                    if grep -q "$report_name" "$STATE_FILE" 2>/dev/null; then
                        continue
                    fi
                fi

                process_report "$report"
                count=$((count + 1))
            fi
        done
    done

    if [ $count -eq 0 ]; then
        log "$GREEN" "All reports already processed"
    else
        log "$GREEN" "Processed $count existing reports"
    fi
}

# Main watch loop
watch_reports() {
    local dirs=("$REPORTS_DIR")

    log "$BLUE" "Starting watch on: ${dirs[*]}"

    # Use fswatch if available (macOS), otherwise fall back to polling
    if command -v fswatch > /dev/null 2>&1; then
        log "$GREEN" "Using fswatch for file monitoring"

        fswatch -0 "${dirs[@]}" --event Created --event Updated | while read -d '' event; do
            # Only process .md files
            if [[ "$event" == *.md ]]; then
                # Small delay to ensure file is fully written
                sleep 0.5
                process_report "$event"
            fi
        done
    else
        log "$YELLOW" "fswatch not found, using polling method"

        # Polling fallback
        while true; do
            for dir in "${dirs[@]}"; do
                for report in "$dir"/*.md; do
                    if [ -f "$report" ]; then
                        process_report "$report"
                    fi
                done
            done
            sleep 5
        done
    fi
}

# Cleanup on exit
cleanup() {
    log "$YELLOW" "Report watcher stopping..."
    rm -f "$PID_FILE"
    exit 0
}

# Set up signal handlers
# Only trap INT and TERM for daemon mode, not EXIT (which triggers on nohup)
trap cleanup INT TERM

# Main execution
main() {
    log "$GREEN" "VNX Report Watcher v1.0 started"
    log "$BLUE" "Reports directory: $REPORTS_DIR"
    log "$BLUE" "Receipts file: $RECEIPTS_FILE"
    log "$BLUE" "State file: $STATE_FILE"

    # Process any existing reports first
    process_existing_reports

    # Start watching for new reports
    watch_reports
}

# Run if executed directly
if [ "${BASH_SOURCE[0]}" == "${0}" ]; then
    main "$@"
fi
