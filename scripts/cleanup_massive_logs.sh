#!/bin/bash
# Emergency Cleanup - 145GB → Reasonable Size
# Cleans up massive conversation logs and old backups

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
VNX_DIR="$VNX_HOME"
STATE_DIR="$VNX_STATE_DIR"
LOGS_DIR="$VNX_LOGS_DIR"

echo "═══════════════════════════════════════════════════════════"
echo "   VNX System Cleanup - Reclaim 100GB+ Disk Space"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Show current size
echo "Current size of .claude directory:"
du -sh "$PROJECT_ROOT/.claude" 2>/dev/null || true
echo ""

# Backup important files first
echo "Creating backups of critical state files..."
mkdir -p "$STATE_DIR/backup_before_cleanup"
cp "$STATE_DIR/t0_receipts.ndjson" "$STATE_DIR/backup_before_cleanup/" 2>/dev/null || true
cp "$STATE_DIR/unified_state.ndjson" "$STATE_DIR/backup_before_cleanup/" 2>/dev/null || true
echo "✓ Critical files backed up"
echo ""

# 1. MASSIVE conversation logs (104GB total)
echo "Step 1: Cleaning massive conversation logs (104GB)..."
echo "Files to clean:"
ls -lh "$STATE_DIR"/*_conversation.log 2>/dev/null | awk '{print "  ", $9, "-", $5}'

read -p "Delete these huge conversation logs? (y/n) " -n 1 -r || true
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Keep only last 1000 lines of each as .tail backup
    for log in "$STATE_DIR"/t*_conversation.log; do
        if [ -f "$log" ]; then
            echo "  Processing $(basename $log)..."
            tail -1000 "$log" > "$log.tail" 2>/dev/null || true
            rm "$log"
            echo "    ✓ Deleted, kept last 1000 lines in .tail"
        fi
    done
    echo "✓ Conversation logs cleaned (saved ~104GB)"
else
    echo "⊘ Skipped conversation logs"
fi
echo ""

# 2. Old backups (1.7GB)
echo "Step 2: Cleaning old backup files (1.7GB)..."
echo "Files to clean:"
ls -lh "$STATE_DIR"/*.backup* 2>/dev/null | awk '{print "  ", $9, "-", $5}'

read -p "Delete old backup files? (y/n) " -n 1 -r || true
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -f "$STATE_DIR"/*.backup* 2>/dev/null || true
    echo "✓ Old backups deleted (saved ~1.7GB)"
else
    echo "⊘ Skipped backups"
fi
echo ""

# 3. Large dashboard log (348MB)
echo "Step 3: Cleaning dashboard log (348MB)..."
if [ -f "$LOGS_DIR/dashboard.log" ]; then
    SIZE=$(du -h "$LOGS_DIR/dashboard.log" | cut -f1)
    echo "  dashboard.log - $SIZE"

    read -p "Truncate dashboard.log? (y/n) " -n 1 -r || true
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        tail -10000 "$LOGS_DIR/dashboard.log" > "$LOGS_DIR/dashboard.log.tmp"
        mv "$LOGS_DIR/dashboard.log.tmp" "$LOGS_DIR/dashboard.log"
        echo "✓ Dashboard log truncated to last 10k lines"
    else
        echo "⊘ Skipped dashboard log"
    fi
fi
echo ""

# 4. Archive old state
echo "Step 4: Archiving old intelligence files..."
if [ -f "$STATE_DIR/t0_intelligence_archive.ndjson" ]; then
    SIZE=$(du -h "$STATE_DIR/t0_intelligence_archive.ndjson" | cut -f1)
    echo "  t0_intelligence_archive.ndjson - $SIZE"

    read -p "Delete intelligence archive? (y/n) " -n 1 -r || true
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -f "$STATE_DIR/t0_intelligence_archive.ndjson"
        echo "✓ Intelligence archive deleted"
    else
        echo "⊘ Skipped intelligence archive"
    fi
fi
echo ""

# 5. Compressed old logs
echo "Step 5: Cleaning compressed logs..."
find "$STATE_DIR" -name "*.gz" -type f -exec ls -lh {} \; 2>/dev/null | awk '{print "  ", $9, "-", $5}'

read -p "Delete compressed logs? (y/n) " -n 1 -r || true
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    find "$STATE_DIR" -name "*.gz" -type f -delete
    echo "✓ Compressed logs deleted"
else
    echo "⊘ Skipped compressed logs"
fi
echo ""

# 6. Corrupted/old files
echo "Step 6: Cleaning corrupted and old receipt files..."
ls -lh "$STATE_DIR"/*.corrupted 2>/dev/null | awk '{print "  ", $9, "-", $5}'
ls -lh "$STATE_DIR"/*_migrated.ndjson 2>/dev/null | awk '{print "  ", $9, "-", $5}'

read -p "Delete corrupted/migrated files? (y/n) " -n 1 -r || true
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -f "$STATE_DIR"/*.corrupted 2>/dev/null || true
    rm -f "$STATE_DIR"/*_migrated.ndjson 2>/dev/null || true
    echo "✓ Corrupted/migrated files deleted"
else
    echo "⊘ Skipped corrupted files"
fi
echo ""

# Summary
echo "═══════════════════════════════════════════════════════════"
echo "   Cleanup Complete"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "New size of .claude directory:"
du -sh "$PROJECT_ROOT/.claude" 2>/dev/null || true
echo ""
echo "Disk space reclaimed!"
echo ""
echo "Backups saved in: $STATE_DIR/backup_before_cleanup/"
echo "Conversation log tails saved as: *.tail files"
echo ""
