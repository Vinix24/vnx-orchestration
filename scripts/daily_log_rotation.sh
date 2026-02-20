#!/bin/bash
# Daily Log Rotation - Prevents logs from growing to 100GB+
# Runs automatically via cron

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
VNX_DIR="$VNX_HOME"
STATE_DIR="$VNX_STATE_DIR"
LOGS_DIR="$VNX_LOGS_DIR"

# Configuration
MAX_LOG_LINES=50000        # Keep max 50k lines per conversation log
MAX_DASHBOARD_LINES=10000  # Keep max 10k lines for dashboard
MAX_LOG_SIZE_MB=20         # Rotate logs bigger than 20MB
MAX_STATE_SIZE_MB=100      # Rotate state NDJSON files bigger than 100MB
MAX_STATE_LOG_LINES=20000  # Keep last 20k lines for state logs
MAX_LOG_ARCHIVE_LINES=50000 # Keep last 50k lines for large logs
MAX_UNIFIED_STATE_LINES=10000  # Keep last 10k unified_state lines
DISPATCH_ARCHIVE_DAYS=7        # Archive dispatch JSONs older than N days
ARCHIVE_DIR="$STATE_DIR/archive"
LOG_ARCHIVE_DIR="$LOGS_DIR/archive"

echo "[$(date)] Starting daily log rotation..."

archive_and_trim_file() {
    local file_path="$1"
    local max_size_mb="$2"
    local tail_lines="$3"
    local archive_dir="$4"

    [ -f "$file_path" ] || return 0

    local size_mb
    size_mb=$(du -m "$file_path" | cut -f1)
    if [ "${size_mb:-0}" -le "$max_size_mb" ]; then
        return 0
    fi

    local total_lines
    total_lines=$(wc -l < "$file_path" | tr -d ' ')
    if [ "${total_lines:-0}" -le "$tail_lines" ]; then
        return 0
    fi

    mkdir -p "$archive_dir"
    local base
    base=$(basename "$file_path")
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    local archive_file="$archive_dir/${base}.${timestamp}.archive"

    echo "  Rotating $(basename "$file_path") (${size_mb}MB > ${max_size_mb}MB)"
    head -n $((total_lines - tail_lines)) "$file_path" > "$archive_file"
    tail -n "$tail_lines" "$file_path" > "$file_path.tmp"
    mv "$file_path.tmp" "$file_path"
    echo "    ✓ Archived $((total_lines - tail_lines)) lines to $(basename "$archive_file")"
    echo "    ✓ Kept last $tail_lines lines"

    # Compress archive to save disk space
    if command -v gzip >/dev/null 2>&1 && [ -f "$archive_file" ]; then
        gzip "$archive_file"
        echo "    ✓ Compressed to $(basename "$archive_file").gz"
    fi
}

archive_old_dispatch_jsons() {
    local dispatch_dir="$STATE_DIR/dispatches"
    [ -d "$dispatch_dir" ] || return 0

    local archive_dir="$dispatch_dir/archive_$(date +%Y%m%d)"
    mkdir -p "$archive_dir"

    # Move only older dispatch JSONs to archive (keeps recent ones for active operations)
    find "$dispatch_dir" -maxdepth 1 -name "*.json" -mtime +"$DISPATCH_ARCHIVE_DAYS" -exec mv {} "$archive_dir/" \; 2>/dev/null || true
}

# 1. Rotate conversation logs if they exceed size/line limits
echo "Rotating conversation logs..."
for log in "$STATE_DIR"/t*_conversation.log; do
    [ -f "$log" ] || continue
    archive_and_trim_file "$log" "$MAX_LOG_SIZE_MB" "$MAX_LOG_LINES" "$ARCHIVE_DIR"
done

# 2. Rotate dashboard log
if [ -f "$LOGS_DIR/dashboard.log" ]; then
    SIZE_MB=$(du -m "$LOGS_DIR/dashboard.log" | cut -f1)
    if [ "$SIZE_MB" -gt 50 ]; then
        echo "  Rotating dashboard.log (${SIZE_MB}MB)"
        tail -n $MAX_DASHBOARD_LINES "$LOGS_DIR/dashboard.log" > "$LOGS_DIR/dashboard.log.tmp"
        mv "$LOGS_DIR/dashboard.log.tmp" "$LOGS_DIR/dashboard.log"
        echo "    ✓ Rotated to $MAX_DASHBOARD_LINES lines"
    fi
fi

# 2b. Rotate large state NDJSON files that can grow without bound
echo "Rotating large state files..."
archive_and_trim_file "$STATE_DIR/unified_state.ndjson" "$MAX_STATE_SIZE_MB" "$MAX_UNIFIED_STATE_LINES" "$ARCHIVE_DIR"
archive_and_trim_file "$STATE_DIR/t0_intelligence_full.ndjson" "$MAX_STATE_SIZE_MB" "$MAX_UNIFIED_STATE_LINES" "$ARCHIVE_DIR"
archive_and_trim_file "$STATE_DIR/terminal_status.ndjson" "$MAX_STATE_SIZE_MB" "$MAX_UNIFIED_STATE_LINES" "$ARCHIVE_DIR"

# 2c. Rotate large state logs (non-receipt, non-audit)
echo "Rotating large state logs..."
archive_and_trim_file "$STATE_DIR/receipt_processing.log" "$MAX_LOG_SIZE_MB" "$MAX_STATE_LOG_LINES" "$ARCHIVE_DIR"
archive_and_trim_file "$STATE_DIR/t0_brief_errors.log" "$MAX_LOG_SIZE_MB" "$MAX_STATE_LOG_LINES" "$ARCHIVE_DIR"

# 2d. Rotate large runtime logs (non-dashboard)
echo "Rotating large runtime logs..."
if [ -d "$LOGS_DIR" ]; then
    mkdir -p "$LOG_ARCHIVE_DIR"
    for log in "$LOGS_DIR"/*.log; do
        [ -f "$log" ] || continue
        if [ "$(basename "$log")" = "dashboard.log" ]; then
            continue
        fi
        archive_and_trim_file "$log" "$MAX_LOG_SIZE_MB" "$MAX_LOG_ARCHIVE_LINES" "$LOG_ARCHIVE_DIR"
    done
fi

# 2e. Archive old dispatch JSONs
echo "Archiving old dispatch JSONs (>${DISPATCH_ARCHIVE_DAYS} days)..."
archive_old_dispatch_jsons
echo "  ✓ Dispatch JSONs archived"

# 3. Clean old backup files (older than 7 days)
echo "Cleaning old backups (>7 days)..."
find "$STATE_DIR" -name "backup_before_cleanup_*" -type d -mtime +7 -exec rm -rf {} \; 2>/dev/null || true
find "$STATE_DIR" -name "*.backup*" -type f -mtime +7 -delete 2>/dev/null || true
echo "  ✓ Old backups cleaned"

# 4. Clean old .tail files (older than 30 days)
echo "Cleaning old tail files (>30 days)..."
find "$STATE_DIR" -name "*.tail" -type f -mtime +30 -delete 2>/dev/null || true
echo "  ✓ Old tail files cleaned"

# 5. Compress old logs (older than 7 days)
echo "Compressing old logs..."
find "$LOGS_DIR" -name "*.log" -type f -mtime +7 ! -name "dashboard.log" -exec gzip {} \; 2>/dev/null || true
echo "  ✓ Old logs compressed"

# 6. Delete compressed logs older than 30 days
echo "Deleting old compressed logs (>30 days)..."
find "$LOGS_DIR" -name "*.gz" -type f -mtime +30 -delete 2>/dev/null || true
echo "  ✓ Old compressed logs deleted"

# 7. Report final sizes
echo ""
echo "Log sizes after rotation:"
echo "  Conversation logs:"
for log in "$STATE_DIR"/t*_conversation.log; do
    if [ -f "$log" ]; then
        SIZE=$(du -h "$log" | cut -f1)
        echo "    $(basename $log): $SIZE"
    fi
done

if [ -f "$LOGS_DIR/dashboard.log" ]; then
    SIZE=$(du -h "$LOGS_DIR/dashboard.log" | cut -f1)
    echo "  Dashboard log: $SIZE"
fi

echo ""
echo "Total .claude size: $(du -sh $PROJECT_ROOT/.claude | cut -f1)"
echo "[$(date)] Log rotation complete"
