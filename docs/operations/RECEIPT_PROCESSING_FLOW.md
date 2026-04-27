# VNX Receipt Processing Flow - Automatic Report to T0 Delivery

**Status**: ⚠️ HISTORICAL — `report_watcher.sh` is deprecated; see `RECEIPT_PIPELINE.md` for the active `receipt_processor_v4.sh` flow
**Date**: 2026-02-08 (document last updated; `report_watcher.sh` superseded 2026-04-27)

> **⚠️ Deprecation notice**: `report_watcher.sh` (described below) is deprecated and exits
> immediately with a "superseded" message. The active receipt pipeline uses
> `scripts/receipt_processor_v4.sh` — see `RECEIPT_PIPELINE.md` for current documentation.
> This document is retained for historical reference only.

## How It Should Work

### 1. Terminal Creates Report
When any terminal (T1, T2, T3) completes work, they write a markdown report to:
```
.claude/vnx-system/unified_reports/[timestamp]-[terminal]-[type]-[title].md
```

### 2. Report Watcher Detects (AUTOMATIC)
The `report_watcher.sh` script:
- Monitors `/unified_reports/` directory for new `.md` files
- Uses `fswatch` for real-time file system monitoring
- Detects new reports immediately upon creation

### 3. Receipt Generation
When a new report is detected:
1. `report_watcher.sh` calls `report_parser.py`
2. Parser extracts metadata from markdown report:
   - Terminal ID (T1/T2/T3)
   - Track (A/B/C)
   - Gate position
   - Status (success/failure)
   - Dispatch ID
   - Confidence score
3. Generates JSON receipt (NDJSON format)

### 4. Receipt Delivery to T0
The receipt is automatically:
1. Appended to `.vnx-data/state/t0_receipts.ndjson`
2. Intelligence system picks up via `intelligence_refresh.sh`
3. Updates `t0_brief.json` with latest status
4. T0 receives notification (if using tmux integration)

## Current Status After P1 Migration

### ✅ Fixed Issues
1. **Report Watcher Missing from Dashboard**: Added to `serve_dashboard.py`
2. **State Files Migrated**: All intelligence files moved to `.vnx-data/state/`
3. **UserPromptSubmit Hook**: Fixed to use new paths

### Configuration Required

#### Dashboard Process Management
The report_watcher is now managed by the dashboard. Access at: http://localhost:4173/dashboard/

**Process List** (should show):
- smart_tap ✅
- dispatcher ✅
- queue_watcher ✅
- receipt_processor ✅
- supervisor ✅
- ack_dispatcher ✅
- intelligence_daemon ✅
- **report_watcher** ✅ (NEW - just added)

#### Starting Report Watcher
1. **Via Dashboard UI**: Click "Start" button for report_watcher
2. **Manual Start**: `bash .claude/vnx-system/scripts/report_watcher.sh &`
3. **Verify Running**: `ps aux | grep report_watcher`

## Testing the Flow

### Test Automatic Receipt Processing
```bash
# 1. Create a test report (simulating T3 completion)
cat > .claude/vnx-system/unified_reports/test-T3-IMPL-test.md << 'EOF'
# Test Report

## Metadata
- **Terminal**: T3
- **Track**: C
- **Gate**: planning
- **Status**: success
- **Dispatch-ID**: TEST-123

## Summary
Test report for receipt processing
EOF

# 2. Check if receipt was generated (wait 2-3 seconds)
tail -1 .vnx-data/state/t0_receipts.ndjson

# 3. Verify intelligence update
cat .vnx-data/state/t0_brief.json | jq '.recent_receipts'
```

## Troubleshooting

### Report Watcher Not Running
```bash
# Check if running
ps aux | grep report_watcher

# Check state file
cat .vnx-data/state/report_watcher.state

# Check logs
tail -f .vnx-data/logs/report_watcher.log
```

### Receipts Not Generated
```bash
# Test parser manually
cd .claude/vnx-system/scripts
python3 report_parser.py ../unified_reports/[report-file].md

# Check for errors
tail .vnx-data/logs/report_watcher.log | grep ERROR
```

### Path Issues After P1
- Old path: `.claude/vnx-system/state/`
- New path: `.vnx-data/state/`
- Ensure all scripts use `source lib/vnx_paths.sh`

## Architecture Diagram

```
Terminal (T1/T2/T3)
    |
    v
Write Report → /unified_reports/*.md
    |
    v
report_watcher.sh (fswatch monitoring)
    |
    v
report_parser.py (extract metadata)
    |
    v
Generate Receipt (JSON)
    |
    v
Append → t0_receipts.ndjson
    |
    v
intelligence_refresh.sh
    |
    v
Update → t0_brief.json
    |
    v
T0 Notification (optional tmux)
```

## Key Files

- **Watcher**: `.claude/vnx-system/scripts/report_watcher.sh`
- **Parser**: `.claude/vnx-system/scripts/report_parser.py`
- **Receipts**: `.vnx-data/state/t0_receipts.ndjson`
- **Intelligence**: `.vnx-data/state/t0_brief.json`
- **Dashboard**: `.claude/vnx-system/dashboard/serve_dashboard.py`

## Summary

The receipt processing system is designed to be **fully automatic**. When terminals write reports to `/unified_reports/`, the report_watcher detects them, generates receipts, and delivers them to T0 without manual intervention. After the P1 migration and dashboard update, this system is now fully operational.

---
*Documentation by T-MANAGER - VNX System Orchestration*