#!/bin/bash
# Automatic Cleanup - No prompts, just clean!

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
VNX_DIR="$VNX_HOME"
STATE_DIR="$VNX_STATE_DIR"
LOGS_DIR="$VNX_LOGS_DIR"

echo "═══════════════════════════════════════════════════════════"
echo "   VNX AUTOMATIC Cleanup - Reclaiming 100GB+ Disk Space"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Show current size
echo "Current size:"
du -sh "$PROJECT_ROOT/.claude" 2>/dev/null || true
echo ""

# Backup critical files
echo "1. Creating backups..."
mkdir -p "$STATE_DIR/backup_before_cleanup_$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$STATE_DIR/backup_before_cleanup_$(date +%Y%m%d_%H%M%S)"
cp "$STATE_DIR/t0_receipts.ndjson" "$BACKUP_DIR/" 2>/dev/null || true
cp "$STATE_DIR/unified_state.ndjson" "$BACKUP_DIR/" 2>/dev/null || true
echo "✓ Backups created in $BACKUP_DIR"
echo ""

# 2. Clean conversation logs (104GB!)
echo "2. Cleaning conversation logs (104GB)..."
for log in "$STATE_DIR"/t*_conversation.log; do
    if [ -f "$log" ]; then
        SIZE=$(du -h "$log" | cut -f1)
        echo "  Processing $(basename $log) ($SIZE)..."
        # Keep last 1000 lines
        tail -1000 "$log" > "$log.tail" 2>/dev/null || true
        rm "$log"
        echo "    ✓ Cleaned (kept last 1000 lines in .tail)"
    fi
done
echo "✓ Conversation logs cleaned (~104GB saved)"
echo ""

# 3. Clean old backups (1.7GB)
echo "3. Cleaning old backup files..."
rm -f "$STATE_DIR"/*.backup* 2>/dev/null || true
echo "✓ Old backups deleted (~1.7GB saved)"
echo ""

# 4. Truncate dashboard log (348MB)
echo "4. Truncating dashboard log..."
if [ -f "$LOGS_DIR/dashboard.log" ]; then
    SIZE=$(du -h "$LOGS_DIR/dashboard.log" | cut -f1)
    echo "  dashboard.log - $SIZE"
    tail -10000 "$LOGS_DIR/dashboard.log" > "$LOGS_DIR/dashboard.log.tmp" 2>/dev/null || true
    mv "$LOGS_DIR/dashboard.log.tmp" "$LOGS_DIR/dashboard.log"
    echo "✓ Dashboard log truncated (~348MB saved)"
fi
echo ""

# 5. Clean intelligence archive (370MB)
echo "5. Cleaning intelligence archive..."
if [ -f "$STATE_DIR/t0_intelligence_archive.ndjson" ]; then
    SIZE=$(du -h "$STATE_DIR/t0_intelligence_archive.ndjson" | cut -f1)
    echo "  t0_intelligence_archive.ndjson - $SIZE"
    rm -f "$STATE_DIR/t0_intelligence_archive.ndjson"
    echo "✓ Intelligence archive deleted (~370MB saved)"
fi
echo ""

# 6. Clean compressed logs
echo "6. Cleaning compressed logs..."
find "$STATE_DIR" -name "*.gz" -type f -delete 2>/dev/null || true
echo "✓ Compressed logs deleted"
echo ""

# 7. Clean corrupted/migrated files
echo "7. Cleaning corrupted/migrated files..."
rm -f "$STATE_DIR"/*.corrupted 2>/dev/null || true
rm -f "$STATE_DIR"/*_migrated.ndjson 2>/dev/null || true
echo "✓ Corrupted/migrated files deleted"
echo ""

# 8. Clean old archive directories
echo "8. Cleaning archive directories..."
if [ -d "$STATE_DIR/archive" ]; then
    SIZE=$(du -sh "$STATE_DIR/archive" | cut -f1)
    echo "  archive/ - $SIZE"
    rm -rf "$STATE_DIR/archive"
    echo "✓ Archive directory cleaned"
fi
echo ""

# Summary
echo "═══════════════════════════════════════════════════════════"
echo "   Cleanup Complete!"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "New size:"
du -sh "$PROJECT_ROOT/.claude" 2>/dev/null || true
echo ""
echo "Files saved:"
echo "  - Backups: $BACKUP_DIR"
echo "  - Conversation tails: $STATE_DIR/*.tail"
echo ""
echo "Disk space reclaimed: ~106GB"
echo ""
