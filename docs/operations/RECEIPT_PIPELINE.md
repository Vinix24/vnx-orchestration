# VNX Receipt Pipeline - Complete Documentation
**Last Updated**: 2026-02-18
**Owner**: T-MANAGER
**Purpose**: Documentation for VNX Receipt Pipeline - Complete Documentation.

**Version**: 8.1.0
**Date**: 2026-02-18
**Author**: T-MANAGER
**Status**: Active

## Executive Summary

The VNX Receipt Pipeline automates terminal task completion reporting through intelligent markdown-to-receipt conversion. Terminals write ONE markdown report per task, and the system handles all receipt generation, delivery, and confirmation automatically.

### Key Achievements
- **100% receipt delivery** via tmux Enter key submission (Phase 1B)
- **Zero false positives** via 4-signal terminal status detection
- **Monitor mode** prevents historical report reprocessing
- **Flood protection** with rate limiting and circuit breaker
- **95%+ terminal status accuracy** (was 60%)
- **Quality advisory sidecar** on every completion (V8.1)
- **Quality findings in T0 receipts** — top-10 findings with severity, file, message (V8.1)
- **Skill-triggered receipts** — receipt processor prefixes skill invocation (V8.1)
- **Parallel PR support** — multiple PRs in progress across tracks (V8.1)

## System Architecture

### Core Philosophy
```
Markdown Reports (Terminal) → Automated Parsing → Receipt Generation → T0 Delivery
```

### Component Overview

#### 1. Report Generation Layer (Terminal Responsibility)
- **What**: Terminals write ONE markdown report per investigation/task
- **Format**: Enhanced template with structured fields
- **Location**: `.claude/vnx-system/unified_reports/`
- **Naming**: `YYYYMMDD-HHMMSS-{terminal}-{brief-description}.md`

#### 2. Extraction Layer (Automated)
- **Report Parser**: `report_parser.py` - Extracts structured receipts from markdown
- **Location**: `.claude/vnx-system/scripts/`
- **Output**: JSON receipt with task metadata, tags, metrics

#### 3. Processing Layer (Automated)
- **Receipt Processor V4**: `receipt_processor_v4.sh` - Time-aware processing with flood protection
- **Monitor Mode**: Only processes reports created after startup (timestamp-based)
- **Rate Limiting**: Configurable receipts/minute with circuit breaker
- **Flood Protection**: Prevents processing storms (threshold: 50 reports)

#### 4. Delivery Layer (Automated)
- **T0 Delivery**: Receipts sent to T0 orchestrator via tmux inter-pane communication
- **Mechanism**: `load-buffer` → `paste-buffer` → double `send-keys Enter`
- **Format**: `📨 RECEIPT:{terminal}:{json_data}`

#### 5. Confirmation Layer (Automated)
- **4-Signal Detection**: Active dispatches → ACK states → recent receipts → conversation logs
- **Heartbeat Monitor**: `heartbeat_ack_monitor.py` - Multi-signal ACK confirmation
- **Smart Polling**: Active only between dispatch and receipt

## Receipt Flow Architecture

### Complete Flow (2026-01-07 Implementation)
```
1. Dispatch sent to terminal (T1/T2/T3)
   ↓
2. Heartbeat monitor starts polling (4 signals)
   ↓
3. Terminal activity detected → ACK generated
   ↓
4. Terminal completes investigation/task
   ↓
5. Terminal writes markdown report to unified_reports/
   ↓
6. Receipt processor V4 detects new report (monitor mode)
   ↓
7. report_parser.py extracts structured receipt
   ↓
7b. Contract verification (Phase 2a — if contract block exists)
   ↓
8. Receipt appended to t0_receipts.ndjson
   ↓
9. Receipt formatted for T0 delivery
   ↓
10. Tmux paste-buffer sends receipt to T0 pane
   ↓
11. Double Enter keypress submits receipt (bracketed paste mode)
   ↓
12. T0 receives receipt for orchestration decision
```

### File Flow
```
unified_reports/*.md (persistent)
        ↓
    report_parser.py
        ↓
t0_receipts.ndjson (persistent)
        ↓
receipt_processor_v4.sh
        ↓
    tmux paste-buffer
        ↓
T0 pane (Enter + Enter)
```

## Contract Verification Stage (Step D2)

After report parsing and before receipt storage, the receipt processor runs lightweight contract verification when a dispatch includes a `## Contract` block.

**Trigger**: Receipt with `task_complete` status and a dispatch ID that maps to a dispatch file containing a contract block.

**Script**: `scripts/verify_claims.py`

**Behavior**:
- **Phase 2a (current)**: Verification runs only when a contract block exists. Dispatches without contracts continue processing normally.
- **Non-fatal**: Verification errors are logged but never block receipt processing. The receipt pipeline continues regardless of verification outcome.
- **Fast checks only**: file_exists, file_changed, pattern_match, no_pattern, bash_check. No test suite execution (that happens at pre-merge gate time).
- **Results stored**: `.vnx-data/state/verification_results/{dispatch_id}_{timestamp}.json`

**Integration point** (receipt_processor_v4.sh):
```bash
# Step D2: Contract verification (Phase 2a)
verify_result=$(python3 "$SCRIPTS_DIR/verify_claims.py" \
    --dispatch-id "$dispatch_id" --store 2>/dev/null)
```

**Result format**:
```json
{
  "dispatch_id": "20260322-205106-gate-enforcement-C",
  "verified_at": "2026-03-22T20:30:45Z",
  "verdict": "pass",
  "total_claims": 3,
  "passed": 3,
  "failed": 0,
  "results": [ ... ]
}
```

The verdict is included in the receipt for T0 review. At pre-merge time, `vnx gate-check` runs heavier checks (pytest, AST, artifacts) on top of the contract verification.

---

## Cost Metrics Stage (Phase M2)

After receipts are generated, VNX can derive deterministic usage/cost metrics from `t0_receipts.ndjson`.

**Script**:
- `.claude/vnx-system/scripts/cost_tracker.py`

**CLI**:
- `vnx cost-report` (JSON)
- `vnx cost-report --human` (readable summary)

**Input / Output**:
- Input: `$VNX_STATE_DIR/t0_receipts.ndjson`
- Output: `$VNX_STATE_DIR/cost_metrics.json`

**Behavior**:
- Aggregates `task_complete` receipts by model, terminal, and worker.
- Uses static model pricing table (no external billing API).
- Missing model/token fields are explicitly counted as `unknown`.

See `operations/COST_TRACKING_GUIDE.md` for interpretation, caveats, and examples.

## Quality Advisory Pipeline (V8.1)

### Overview

On **every** task completion, `append_receipt.py` now generates a quality advisory sidecar alongside the receipt. This ensures T0 always receives quality signals, even for clean completions.

### Flow
```
Worker writes report → receipt_processor_v4.sh → report_parser.py
                                                       ↓
                                                append_receipt.py
                                                  ↓           ↓
                                          t0_receipts.ndjson   quality_sidecar.json
                                                  ↓
                                          Receipt to T0 (includes quality findings)
```

### Quality Sidecar Format

**File**: Written alongside receipt, always present (even if clean):
```json
{
  "decision": "approve_with_followup",
  "risk_score": 0.35,
  "findings": [
    {
      "severity": "warn",
      "file": "src/services/lead_scoring_engine.py",
      "symbol": "file_size_warning",
      "message": "File exceeds 500 lines (555)"
    }
  ]
}
```

### T0 Receipt Enhancement

Receipt delivery to T0 now includes up to 5 quality finding detail lines:
```
📨 RECEIPT:T1:{...receipt_json...}
🔍 Quality: approve_with_followup (risk: 0.35)
  ⚠ warn: lead_scoring_engine.py — File exceeds 500 lines (555)
```

### Thresholds (Python files)
- **Warning**: 500 lines per file
- **Blocker**: 800 lines per file

### Skill-Triggered Receipts

Receipt processor now prefixes the T0 skill invocation (`/t0-orchestrator`) when delivering receipts. This triggers T0's orchestrator skill for proper receipt handling.

## Implementation Components

### Enhanced Report Template

**File**: `.claude/terminals/library/templates/report_template.md`

**Required Fields**:
```markdown
**Terminal**: T1|T2|T3
**Timestamp**: YYYY-MM-DD HH:MM:SS
**Status**: success|blocked|error
**Investigation Focus**: Brief description
**Task ID**: {original_task_id_from_dispatch} (optional)
**Dispatch ID**: {dispatch_filename} (optional)

## Summary
[Brief overview]

## Investigation Details
[Detailed findings]

## Outcome
[Results and next steps]
```

### Report Parser

**File**: `.claude/vnx-system/scripts/report_parser.py`

**Features**:
- Extracts terminal, timestamp, status from markdown
- Handles both enhanced and legacy report formats
- Generates optimized JSON receipts (<2KB)
- Preserves full report path for reference

**Receipt Structure**:
```python
{
    'event': 'receipt',
    'terminal': 'T1|T2|T3',
    'timestamp': '2026-01-07T20:15:30Z',
    'status': 'success|blocked|error',
    'summary': str,
    'details': str,
    'task_id': str (optional),
    'dispatch_id': str (optional),
    'report_path': str
}
```

### Receipt Processor V4

**File**: `.claude/vnx-system/scripts/receipt_processor_v4.sh`

**Key Features** (2026-01-07):
1. **Monitor Mode** - Only processes NEW reports (timestamp-based cutoff)
2. **Time-Aware Processing** - Prevents reprocessing of historical reports
3. **Flood Protection** - Circuit breaker at 50 reports, rate limiting
4. **Smart Pane Discovery** - Finds T0 pane using pane_manager_v2.sh
5. **Bracketed Paste Support** - Double Enter keypress for reliable submission

**Configuration** (Environment Variables):
```bash
VNX_MODE="monitor"              # monitor|catchup|manual
VNX_MAX_AGE_HOURS="24"          # Only process reports from last N hours
VNX_RATE_LIMIT="10"             # Max receipts per minute
VNX_FLOOD_THRESHOLD="50"        # Circuit breaker threshold
```

**Operational Modes**:
- **monitor**: Real-time processing of new reports only (default)
- **catchup**: Process reports from last N hours, then switch to monitor
- **manual**: Process pending reports once, then exit

**Delivery Mechanism** (Lines 177-196):
```bash
# Smart pane discovery
local t0_pane=$(get_pane_id_smart "T0" 2>/dev/null)

# Format receipt message
local receipt_msg="📨 RECEIPT:${terminal}:$(echo "$receipt_json" | jq -c .)"

# Send via tmux
echo "$receipt_msg" | tmux load-buffer -
tmux paste-buffer -t "$t0_pane"
sleep 1

# Submit with double Enter (bracketed paste mode)
tmux send-keys -t "$t0_pane" Enter
sleep 0.3
tmux send-keys -t "$t0_pane" Enter
```

### Heartbeat ACK Monitor

**File**: `.claude/vnx-system/scripts/heartbeat_ack_monitor.py`

**4-Signal Terminal Status Detection** (2026-01-07):
1. **Active Dispatches**: Check for dispatch files in `/ack_states/{terminal}/`
2. **ACK State Files**: Read `.ack_state` metadata files
3. **Recent Receipts**: Parse t0_receipts.ndjson for terminal activity
4. **Conversation Logs**: Check terminal conversation log timestamps

**Smart Features**:
- Dedicated thread per dispatch
- Multi-signal confidence scoring
- Automatic timeout handling (60s default)
- Stop monitoring on final receipt
- Error handling for missing signals

**Configuration**:
```python
heartbeat_poll_interval = 5     # seconds (updated 2026-01-07)
confirmation_threshold = 3      # seconds after dispatch
timeout_seconds = 60            # max wait
required_signals = 2            # minimum for ACK
```

## Performance Metrics

### Current System (Phase 1B)
- **Receipt Delivery**: 100% success rate (was 0%)
- **Terminal Status Accuracy**: 95%+ (was 60%)
- **Report Parsing**: <100ms per report
- **Receipt Size**: <2KB optimized JSON
- **ACK Confirmation**: 2-5 seconds after dispatch
- **Health Check Interval**: 5 seconds (was 2s)

### Reliability Improvements
- **Process Stability**: Frequent crashes → Stable with auto-recovery
- **Duplicate Prevention**: Atomic locking with singleton enforcement
- **Timezone Handling**: UTC-aware datetimes for consistency
- **Flood Protection**: Prevents processing storms

## Troubleshooting

### Quick Diagnosis

#### Health Check Command
```bash
cd $PROJECT_ROOT

echo "=== RECEIPT PIPELINE HEALTH CHECK ==="

# Check process status
echo "Receipt Processor: PID $(cat $VNX_HOME/pids/receipt_processor.pid 2>/dev/null || echo 'NOT RUNNING')"
echo "Supervisor: PID $(cat $VNX_HOME/pids/vnx_supervisor.pid 2>/dev/null || echo 'NOT RUNNING')"

# Check recent activity
echo
echo "Recent Reports (last 5):"
ls -lt $VNX_HOME/unified_reports/*.md 2>/dev/null | head -5 | awk '{print $9, "(" $6, $7, $8 ")"}'

echo
echo "Recent Receipts (last 5):"
tail -5 $VNX_HOME/state/t0_receipts.ndjson 2>/dev/null | jq -r '.terminal + " - " + .timestamp + " - " + .status'

# Check for flood protection
if [ -f $VNX_HOME/state/receipt_flood.lock ]; then
    echo
    echo "🚨 FLOOD PROTECTION ACTIVE - Remove lock to resume processing"
fi
```

### Common Issues

#### 1. No Receipts Appearing in T0

**Symptoms**:
- New reports in unified_reports/ but no receipts in T0
- receipt_processor running but no activity

**Diagnostic Steps**:
```bash
# Check processor is running
ps aux | grep receipt_processor_v4

# Check processing log
tail -50 $VNX_HOME/state/receipt_processing.log

# Check for flood lock
ls -la $VNX_HOME/state/receipt_flood.lock

# Test parser manually
cd $VNX_HOME/scripts
python3 report_parser.py ../unified_reports/latest.md
```

**Common Causes**:
1. **Flood Protection Active** - Remove `.claude/vnx-system/state/receipt_flood.lock`
2. **Old Reports** - Processor in monitor mode skips reports older than startup time
3. **T0 Pane Not Found** - Smart pane discovery failing
4. **Parser Failure** - Report format issues

**Fix**:
```bash
# Remove flood lock if present
rm -f $VNX_HOME/state/receipt_flood.lock

# For old reports, use catchup mode
VNX_MODE=catchup VNX_MAX_AGE_HOURS=24 \
    $VNX_HOME/scripts/receipt_processor_v4.sh

# Check T0 pane exists
tmux list-panes -a -F "#{pane_id} #{pane_title}" | grep T0

# Test parser
python3 $VNX_HOME/scripts/report_parser.py \
    $VNX_HOME/unified_reports/latest.md
```

#### 2. Receipt Processor Keeps Restarting

**Symptoms**:
- Rapidly changing PID in dashboard
- Multiple receipt_processor processes

**Diagnostic Steps**:
```bash
# Check for multiple processes
pgrep -af receipt_processor

# Check singleton lock
ls -la /tmp/vnx-locks/receipt_processor.lock

# Review supervisor log
tail .claude/vnx-system/logs/vnx_supervisor.log
```

**Fix**:
```bash
# Kill all instances
pkill -9 -f receipt_processor_v4

# Clean lock
rm -f /tmp/vnx-locks/receipt_processor.lock

# Let supervisor restart (waits 5 seconds)
sleep 6
ps aux | grep receipt_processor
```

#### 3. Receipts Delivered But Not Visible in T0

**Symptoms**:
- Processing log shows "Receipt delivered to T0"
- T0 doesn't show receipt messages

**Cause**: Bracketed paste mode requires double Enter press

**Fix**:
- Already implemented in receipt_processor_v4.sh (lines 188-192)
- No manual intervention needed
- If still failing, check tmux version supports bracketed paste

#### 4. Terminal Status Showing as "offline" Despite Activity

**Symptoms**:
- Dashboard shows terminal offline
- Terminal is actually active and working

**Cause**: 4-signal detection not finding signals

**Diagnostic Steps**:
```bash
# Check terminal status file
cat .claude/vnx-system/state/terminal_status.ndjson

# Check conversation log exists
ls -la .claude/vnx-system/state/t{1,2,3}_conversation.log

# Check ACK states directory
ls -la .claude/vnx-system/state/ack_states/T{1,2,3}/

# Check recent receipts
tail .claude/vnx-system/state/t0_receipts.ndjson
```

**Fix**:
- Ensure heartbeat_ack_monitor.py is running
- Check terminal conversation logs are being written
- Verify /ack_states directories exist for all terminals

### Emergency Recovery Procedures

#### Immediate Recovery (Level 1)
```bash
echo "=== EMERGENCY RECOVERY - LEVEL 1 ==="

# 1. Remove flood lock
rm -f .claude/vnx-system/state/receipt_flood.lock

# 2. Restart receipt processor
pkill -f receipt_processor_v4
sleep 6  # Wait for supervisor restart

# 3. Verify restart
ps aux | grep receipt_processor_v4
```

#### Process Restart (Level 2)
```bash
echo "=== EMERGENCY RECOVERY - LEVEL 2 ==="

# 1. Stop all receipt processing
pkill -9 -f receipt_processor
pkill -9 -f report_parser

# 2. Clean locks
rm -f /tmp/vnx-locks/receipt_processor.lock
rm -f .claude/vnx-system/state/receipt_flood.lock

# 3. Clean processed hashes (if reprocessing needed)
rm -f .claude/vnx-system/state/processed_receipts.txt

# 4. Restart supervisor
pkill -f vnx_supervisor
cd .claude/vnx-system/scripts
nohup ./vnx_supervisor_simple.sh > ../logs/vnx_supervisor.log 2>&1 &

# 5. Monitor restart
tail -f .claude/vnx-system/state/receipt_processing.log
```

#### Complete System Restart (Level 3)
```bash
echo "=== EMERGENCY RECOVERY - LEVEL 3 ==="

# Full VNX system restart
cd $PROJECT_ROOT
./VNX_HYBRID_FINAL.sh

# Monitor startup
tail -f $VNX_HOME/logs/vnx_supervisor.log
```

## Monitoring Commands

### Real-time Receipt Monitoring
```bash
# Watch receipt processing log
tail -f .claude/vnx-system/state/receipt_processing.log

# Monitor t0_receipts file
watch 'tail -5 .claude/vnx-system/state/t0_receipts.ndjson | jq -r ".terminal + \" - \" + .status"'

# Check process health
watch 'cat .claude/vnx-system/state/dashboard_status.json | jq ".processes.receipt_processor"'
```

### Dashboard Verification
```bash
# Full dashboard status
cat .claude/vnx-system/state/dashboard_status.json | jq .

# Receipt processor status
cat .claude/vnx-system/state/dashboard_status.json | jq '.processes.receipt_processor'

# Terminal status
cat .claude/vnx-system/state/dashboard_status.json | jq '.terminals'
```

### Performance Metrics
```bash
# Count reports by terminal
ls .claude/vnx-system/unified_reports/*.md | grep -oE 'T[0-9]' | sort | uniq -c

# Count receipts by terminal
cat .claude/vnx-system/state/t0_receipts.ndjson | jq -r .terminal | sort | uniq -c

# Check processing latency
tail -100 .claude/vnx-system/state/receipt_processing.log | grep "Processing:" | tail -10
```

## Configuration Reference

### Environment Variables

**Receipt Processor V4**:
```bash
# Operating mode
VNX_MODE="monitor"              # monitor|catchup|manual

# Time filtering
VNX_MAX_AGE_HOURS="24"          # Only process reports from last N hours

# Rate limiting
VNX_RATE_LIMIT="10"             # Max receipts per minute

# Flood protection
VNX_FLOOD_THRESHOLD="50"        # Circuit breaker at N reports
```

**Heartbeat Monitor**:
```python
# In heartbeat_ack_monitor.py
heartbeat_poll_interval = 5     # Health check interval (seconds)
confirmation_threshold = 3      # Seconds to wait before ACK
timeout_seconds = 60            # Max wait for confirmation
required_signals = 2            # Minimum signals for ACK
```

**Supervisor**:
```bash
# In vnx_supervisor_simple.sh
HEALTH_CHECK_INTERVAL=5         # Process health check interval (seconds)
```

### File Locations

**Scripts**:
- `.claude/vnx-system/scripts/receipt_processor_v4.sh`
- `.claude/vnx-system/scripts/report_parser.py`
- `.claude/vnx-system/scripts/heartbeat_ack_monitor.py`
- `.claude/vnx-system/scripts/pane_manager_v2.sh`

**State Files**:
- `.claude/vnx-system/state/t0_receipts.ndjson` - Production receipts
- `.claude/vnx-system/state/terminal_status.ndjson` - Terminal status
- `.claude/vnx-system/state/receipt_processing.log` - Processor log
- `.claude/vnx-system/state/processed_receipts.txt` - Hash tracking
- `.claude/vnx-system/state/receipt_last_processed` - Timestamp cursor
- `.claude/vnx-system/state/receipt_flood.lock` - Flood protection lock

**PID Files**:
- `.claude/vnx-system/pids/receipt_processor.pid`
- `.claude/vnx-system/pids/vnx_supervisor.pid`

**Reports**:
- `.claude/vnx-system/unified_reports/*.md` - Terminal reports

## Deployment Status

### ✅ Production (Phase 1B - 2026-01-07)
1. Receipt processor V4 with monitor mode and flood protection
2. Report parser with enhanced extraction
3. T0 delivery with bracketed paste support (double Enter)
4. 4-signal terminal status detection
5. Supervisor with 5-second health checks
6. Timezone-aware datetime handling
7. Atomic process singleton enforcement
8. /ack_states directory structure
9. Enhanced error handling and recovery

### 📊 Metrics (2026-01-07 Post-Deployment)
- Receipt delivery: 0% → 100%
- Terminal status accuracy: 60% → 95%+
- Process stability: Frequent crashes → Stable with auto-recovery
- Health check reliability: Intermittent → Consistent

## Future Enhancements

### Short Term (Next Sprint)
1. Receipt delivery confirmation (T0 ACK back to processor)
2. Metrics dashboard for processing statistics
3. Auto-cleanup of old reports (>7 days)

### Medium Term (1-2 Months)
1. ML-based report quality scoring
2. Automatic report categorization
3. Terminal performance analytics

### Long Term (3+ Months)
1. Distributed receipt processing for scale
2. Real-time receipt streaming to T0
3. Predictive terminal workload distribution

## Related Documentation

- **DOCS_INDEX.md** - Current documentation index
- **00_VNX_ARCHITECTURE.md** - Complete architecture overview
- **operations/README.md** - Operational procedures index
- **T0_ORCHESTRATION_INTELLIGENCE.md** - T0 intelligence system
- **VNX_SYSTEM_BOUNDARIES.md** - Terminal and boundary rules

## Changelog

### Version 8.1.0 (2026-02-18) - Quality Advisory + Multi-Provider
- Quality advisory sidecar written on every completion (even clean)
- Quality findings (top-10) included in T0 receipt delivery
- Skill-triggered receipts (prefixes /t0-orchestrator skill)
- Terminal detection for receipts (Track A/B/C mapping)
- Receipt processor stderr logging (prevents stdout pollution)
- PR queue state scoped to VNX_STATE_DIR
- Multi-provider skill sync (claude/codex/gemini)
- Quality advisory fallback when no open items found
- Parallel PR queue support (in_progress as list)

### Version 8.0.1 (2026-01-07) - Phase 1B Complete
- Replaced receipt_notifier.sh with receipt_processor_v4.sh
- Added monitor mode with timestamp-based cutoff
- Implemented flood protection and rate limiting
- Fixed T0 delivery with bracketed paste support
- Enhanced 4-signal terminal status detection
- Extended health checks from 2s to 5s
- Added timezone-aware datetime handling
- Implemented atomic process singleton enforcement

### Version 8.0 (2025-10-02)
- Initial unified reporting system
- Automated receipt generation from markdown
- Multi-signal ACK detection

---

**Last Updated**: 2026-02-18 by T-MANAGER
**Next Review**: After quality advisory validation in demo project
