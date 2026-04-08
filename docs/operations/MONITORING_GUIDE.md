# VNX System - Operational Monitoring Guide
**Last Updated**: 2026-02-09
**Owner**: T-MANAGER

**Version**: 8.1
**Date**: 2026-02-09
**Status**: Active
**Purpose**: Comprehensive monitoring, troubleshooting, and recovery guide for VNX orchestration system

---

## Table of Contents

1. [Overview](#overview)
2. [Dashboard Monitoring](#dashboard-monitoring)
3. [System Health Checks](#system-health-checks)
4. [Dispatcher Monitoring](#dispatcher-monitoring)
5. [Intelligence System Monitoring](#intelligence-system-monitoring)
6. [Receipt System Monitoring](#receipt-system-monitoring)
7. [Terminal Monitoring](#terminal-monitoring)
8. [Log Monitoring](#log-monitoring)
9. [Performance Monitoring](#performance-monitoring)
10. [Alert Conditions](#alert-conditions)
11. [Troubleshooting Procedures](#troubleshooting-procedures)
12. [Recovery Procedures](#recovery-procedures)
13. [Preventive Maintenance](#preventive-maintenance)
14. [Appendix](#appendix)

---

## Overview

### VNX Monitoring Strategy

The VNX system provides multi-layered observability through:
- **Real-time dashboard** - Web interface backed by 2-second dashboard snapshot updates
- **Process monitoring** - 8 critical processes with health checks
- **Receipt pipeline** - NDJSON event stream for task tracking
- **Intelligence system** - Quality metrics and pattern recognition
- **Terminal status** - Multi-signal detection (dispatches, ACKs, heartbeats)
- **Cost monitoring** - Receipt-based model usage and estimated spend (`vnx cost-report`)

For cost interpretation and caveats, see: `operations/COST_TRACKING_GUIDE.md`.

### Key Health Indicators

| Indicator | Target | Alert Threshold |
|-----------|--------|-----------------|
| Process uptime | 100% | Any process down |
| Receipt latency | <500ms | >2s |
| Dashboard refresh | 60s | >5min stale |
| Terminal response | <5min | >5min offline |
| Queue depth | <10 | >20 items |

---

## Dashboard Monitoring

### Dashboard Access

- **URL**: http://localhost:4173/dashboard/index.html
- **Launcher**: `.claude/vnx-system/dashboard/serve_dashboard.py` (port 4173)
- **Dashboard Update**: Intelligence daemon writes every 60 seconds
- **Client Refresh**: Auto-refresh every 7 seconds in browser
- **Status File**: `.claude/vnx-system/state/dashboard_status.json`
- **Cache Control**: Server sends `no-cache, no-store, must-revalidate` headers for JSON files
- **Client Cache Bust**: `?t=Date.now()` appended to fetch URLs

### Dashboard Sections

**1. Process Status**
```bash
# View all process states
cat .claude/vnx-system/state/dashboard_status.json | jq '.processes'

# Expected: All configured processes with "running": true
{
  "smart_tap": {"pid": "12345", "running": true},
  "dispatcher": {"pid": "12346", "running": true},
  "queue_watcher": {"pid": "12347", "running": true},
  "receipt_processor": {"pid": "12349", "running": true},
  "supervisor": {"pid": "12350", "running": true},
  "ack_dispatcher": {"pid": "12351", "running": true},
  "state_manager": {"pid": "12352", "running": true}
}
```

**2. Queue Metrics**
```bash
# View queue depths
cat .claude/vnx-system/state/dashboard_status.json | jq '.queues'

# Expected: Low queue counts
{
  "queue": 0,        # New dispatches
  "pending": 0,      # Awaiting processing
  "active": 0        # Currently executing
}
```

**3. Terminal Status**
```bash
# View terminal states
cat .claude/vnx-system/state/dashboard_status.json | jq '.terminals'

# Terminal states:
# - active: Dispatch in progress
# - ready: Available for work
# - busy: Processing
# - idle: No activity
# - offline: No signals >5min
```

**4. Intelligence System**
```bash
# Check intelligence status
cat .claude/vnx-system/state/dashboard_status.json | jq '.intelligence_daemon'

# Expected indicators:
{
  "daemon_running": true,
  "patterns_available": 966,
  "status": "healthy",
  "uptime_seconds": 20882
}
```

**5. Quality Intelligence**
```bash
# View code quality metrics
cat .claude/vnx-system/state/dashboard_status.json | jq '.quality_intelligence'

# Key metrics:
{
  "total_files": 523,
  "avg_complexity": 51.50,
  "critical_issues": 264,
  "warnings": 359
}
```

**6. PR Queue (Auto-Discovered)**
```bash
# View auto-discovered PR status
cat .claude/vnx-system/state/dashboard_status.json | jq '.pr_queue'

# Expected output:
{
  "active_feature": "Active Development",
  "total_prs": 4,
  "completed_prs": 3,
  "progress_percent": 75,
  "prs": [
    {"id": "PR1", "description": "Dead Code Removal", "status": "done", "deps": [], "blocked": false},
    {"id": "PR2", "description": "Config Alignment", "status": "done", "deps": ["PR1"], "blocked": false}
  ]
}

# View PR registry (persistent memory)
cat .claude/vnx-system/state/dashboard_status.json | jq '._pr_registry'

# NOTE: PRs are auto-discovered from track gates, receipt history, and
# progress_state.yaml - no pr_roadmap.json or config files needed.
```

### Dashboard Interpretation

**Healthy System Indicators**:
- ✅ All 8 processes running
- ✅ Queue depths <5
- ✅ Terminals showing ready/active
- ✅ Intelligence daemon healthy
- ✅ Timestamp <60s old

**Warning Indicators**:
- ⚠️ Queue depth 5-10
- ⚠️ Any terminal offline <5min
- ⚠️ Dashboard timestamp 1-5min old
- ⚠️ Intelligence patterns <900

**Critical Indicators**:
- 🚨 Any process stopped
- 🚨 Queue depth >10
- 🚨 Multiple terminals offline
- 🚨 Dashboard timestamp >5min old

---

## System Health Checks

### Quick Health Check

```bash
# One-command system health
.claude/vnx-system/scripts/vnx_supervisor_simple.sh status

# Shows all process status
# Expected: 8/8 processes running
```

### Process-by-Process Verification

```bash
# Check critical processes individually
pgrep -f smart_tap_v7_json_translator && echo "✓ Smart Tap V7" || echo "✗ Smart Tap"
pgrep -f dispatcher_v7_compilation && echo "✓ Dispatcher V7" || echo "✗ Dispatcher"
pgrep -f receipt_processor_v4 && echo "✓ Receipt Processor V4" || echo "✗ Receipt Processor"
pgrep -f ack_dispatcher_v2 && echo "✓ ACK Dispatcher V2" || echo "✗ ACK Dispatcher"
pgrep -f queue_popup_watcher && echo "✓ Queue Watcher" || echo "✗ Queue Watcher"
pgrep -f unified_state_manager_v2 && echo "✓ State Manager V2" || echo "✗ State Manager"
pgrep -f vnx_supervisor_simple && echo "✓ Supervisor" || echo "✗ Supervisor"
pgrep -f generate_valid_dashboard && echo "✓ Dashboard" || echo "✗ Dashboard"
```

### Configuration Validation

```bash
# CRITICAL: Validate Smart Tap capture window
grep "capture-pane.*-S" .claude/vnx-system/scripts/smart_tap_v7_json_translator.sh
# Must show: -S -100 (NOT -S -50)

# Validate queue popup mode
head -3 .claude/vnx-system/scripts/queue_popup_watcher.sh
# Should show: "Enhanced non-intrusive notifications"

# Quick validation script
echo "=== Configuration Validation ==="
if grep -q "\-S \-100" .claude/vnx-system/scripts/smart_tap_v7_json_translator.sh; then
    echo "✓ Smart Tap: Capture window correct (-S -100)"
else
    echo "❌ Smart Tap: Capture window too small"
fi

if grep -q "Enhanced non-intrusive" .claude/vnx-system/scripts/queue_popup_watcher.sh; then
    echo "✓ Queue Watcher: Non-intrusive mode"
else
    echo "❌ Queue Watcher: Auto-popup mode (interrupts typing)"
fi
```

### Singleton Enforcement Validation

```bash
# CRITICAL: Check for duplicate processes
echo "=== Singleton Validation ==="
for proc in smart_tap dispatcher receipt_processor; do
  count=$(pgrep -f $proc | wc -l)
  if [ "$count" -eq 1 ]; then
    echo "✓ $proc: Single instance"
  else
    echo "🚨 $proc: Multiple instances ($count) - CRITICAL ISSUE"
  fi
done

# Check lock files
ls -la /tmp/vnx-locks/ 2>/dev/null | grep -E "smart_tap|dispatcher|receipt"
# Should show single PID file per process
```

---

## Dispatcher Monitoring

### Dispatch Flow Monitoring

```bash
# View dispatch queue stages
echo "=== Dispatch Flow ==="
echo "Queue:     $(ls .claude/vnx-system/dispatches/queue/*.md 2>/dev/null | wc -l)"
echo "Pending:   $(ls .claude/vnx-system/dispatches/pending/*.md 2>/dev/null | wc -l)"
echo "Active:    $(ls .claude/vnx-system/dispatches/active/*.md 2>/dev/null | wc -l)"
echo "Completed: $(ls .claude/vnx-system/dispatches/completed/*.md 2>/dev/null | wc -l)"

# Monitor dispatch movement in real-time
watch -n 2 'find .claude/vnx-system/dispatches -type f -name "*.md" | \
  awk -F/ "{print \$NF, \$5}" | column -t'
```

### Dispatcher Performance

```bash
# Check dispatcher processing rate
tail -f .claude/vnx-system/logs/dispatcher.log | grep "Dispatch sent"

# Measure dispatch latency (queue → terminal)
grep "Dispatch sent" .claude/vnx-system/logs/dispatcher.log | tail -20 | \
  awk '{print $1, $2}'

# Check for dispatch errors
tail -100 .claude/vnx-system/logs/dispatcher.log | grep -i "error\|failed"
```

### Template Compilation (V7.3+)

```bash
# Check compiled prompt content
cat /tmp/compiled_prompt_*.txt 2>/dev/null | head -20

# Monitor template compilation
tail -f /tmp/dispatcher_v7_compilation.log 2>/dev/null

# Verify template components
ls -la .claude/terminals/library/templates/agents/     # Agent templates
ls -la .claude/terminals/library/context/              # Context files

# Check recent compilations
echo "Recent compilations:"
ls -lt /tmp/compiled_prompt_*.txt 2>/dev/null | head -3

# Verify constraints injection
grep -A5 "Constraints" /tmp/compiled_prompt_*.txt 2>/dev/null && \
  echo "✓ Constraints loaded" || echo "✗ Constraints missing"

# Verify @ symbol preservation (for fast file resolution)
grep -o '@[^]]*' /tmp/compiled_prompt_*.txt 2>/dev/null && \
  echo "✓ @ symbols preserved" || echo "✗ @ symbols removed"
```

### JSON Translation (V7.0+)

```bash
# Monitor JSON translation activity
ls -la .claude/vnx-system/dispatches/queue/.json/  # JSON archives
wc -l .claude/vnx-system/dispatches/queue/.json/*.json 2>/dev/null

# Check dual storage (Markdown + JSON)
echo "Markdown files: $(ls .claude/vnx-system/dispatches/queue/*.md 2>/dev/null | wc -l)"
echo "JSON archives:  $(ls .claude/vnx-system/dispatches/queue/.json/*.json 2>/dev/null | wc -l)"

# Validate JSON format
find .claude/vnx-system/dispatches/queue/.json/ -name "*.json" -exec jq '.' {} \; | head -10
```

---

## Intelligence System Monitoring

### Intelligence Daemon Status

```bash
# Check daemon health
cat .claude/vnx-system/state/dashboard_status.json | jq '.intelligence_daemon'

# Expected healthy state:
{
  "status": "healthy",
  "last_extraction": "2026-02-09T12:22:58Z",
  "patterns_available": 0,
  "extraction_errors": 0,
  "uptime_seconds": 2160,
  "last_update": "2026-02-09T12:58:59Z"
}

# Monitor daemon activity
tail -f .claude/vnx-system/logs/intelligence_daemon.log

# Verify auto-discovery is working (check PR queue in dashboard)
cat .claude/vnx-system/state/dashboard_status.json | jq '.pr_queue.prs | length'
# Should be > 0 if any PRs have been initialized via init-feature
```

### Quality Intelligence

```bash
# View quality digest
cat .claude/vnx-system/state/t0_quality_digest.json | jq '.'

# Check complexity hotspots
cat .claude/vnx-system/state/t0_quality_digest.json | jq '.top_hotspots[:5]'

# View track routing suggestions
cat .claude/vnx-system/state/t0_quality_digest.json | jq '.track_routing'

# Check critical issues count
cat .claude/vnx-system/state/dashboard_status.json | jq '.quality_intelligence.critical_issues'
# Alert if >300
```

### Report Intelligence

```bash
# View recent reports digest
cat .claude/vnx-system/state/t0_report_digest.json | jq '.recent_reports[:3]'

# Check terminal activity patterns
cat .claude/vnx-system/state/t0_report_digest.json | jq '.terminal_activity'

# View success patterns
cat .claude/vnx-system/state/t0_report_digest.json | jq '.success_patterns'
```

### Intelligence Freshness

```bash
# Check digest update times
ls -la .claude/vnx-system/state/t0_*digest.json

# Digests should update:
# - quality_digest: On code changes
# - report_digest: Every 5 minutes
# - brief: On significant changes

# Force digest update if stale
cd .claude/vnx-system/scripts
./build_t0_quality_digest.py
./build_t0_report_digest.py
```

---

## Receipt System Monitoring

### Receipt Pipeline Health

```bash
# Check receipt processor status
pgrep -f receipt_processor_v4 && echo "✓ Running" || echo "✗ Stopped"

# View recent receipt activity
tail -20 .claude/vnx-system/logs/receipt_processor_v4.log

# Count receipts today
grep "$(date +%Y-%m-%d)" .claude/vnx-system/state/t0_receipts.ndjson | wc -l
```

### Receipt Types Monitoring

```bash
# Monitor all receipts in real-time
tail -f .claude/vnx-system/state/t0_receipts.ndjson | jq '.'

# View ACK receipts only
tail -f .claude/vnx-system/state/t0_receipts.ndjson | jq 'select(.event_type=="task_ack")'

# View completion receipts only
tail -f .claude/vnx-system/state/t0_receipts.ndjson | jq 'select(.event_type=="task_complete")'

# Count receipts by type
jq -r '.event_type' .claude/vnx-system/state/t0_receipts.ndjson | sort | uniq -c
```

### Receipt Delivery Verification

```bash
# Check T0 pane detection
jq '.t0.pane_id' .claude/vnx-system/state/panes.json

# Verify recent deliveries
grep "Receipt delivered to T0" .claude/vnx-system/logs/receipt_processor_v4.log | tail -10

# Check receipts visible in T0 terminal
tmux capture-pane -t %0 -p -S -50 | grep "📨 RECEIPT"
```

### ACK Receipt Monitoring

```bash
# Check ACK dispatcher status
pgrep -f ack_dispatcher_v2 && echo "✓ ACK dispatcher running" || echo "✗ ACK dispatcher stopped"

# Monitor ACK generation
tail -f .claude/vnx-system/state/t0_receipts.ndjson | grep task_ack

# Check ACK socket for heartbeat monitor
ls -la /tmp/heartbeat_ack_monitor.sock 2>/dev/null || echo "ACK socket missing"

# Note: Zero ACK receipts is EXPECTED when no dispatches are active
```

### Receipt Processing Modes

```bash
# Check current processing mode
grep "MODE=" .claude/vnx-system/logs/receipt_processor_v4.log | tail -1

# Modes:
# - monitor: Real-time processing of new reports only (default)
# - catchup: Process reports from last N hours, then switch to monitor
# - manual: Process pending reports once, then exit

# Switch to catchup mode if receipts are missing
pkill -f receipt_processor_v4
VNX_MODE=catchup VNX_MAX_AGE_HOURS=24 .claude/vnx-system/scripts/receipt_processor_v4.sh &
```

---

## Terminal Monitoring

### Terminal Status Detection

VNX uses **multi-signal status detection** with priority order:

1. **Active Dispatches** (highest priority) - Currently executing tasks
2. **ACK States** - Recent acknowledgments (<5 min)
3. **Recent Receipts** - Completion signals
4. **Heartbeat** - Background pulse detection

```bash
# Check terminal status
cat .claude/vnx-system/state/dashboard_status.json | jq '.terminals'

# Terminal states:
# 🟢 active   - Dispatch in progress
# 🟡 ready    - Available for work
# 🔵 busy     - Processing (no dispatch)
# ⚫ offline  - No signals >5min
# 🔴 error    - Failure detected
```

### Terminal Activity Monitoring

```bash
# View terminal heartbeats
tail -f .claude/vnx-system/state/terminal_status.ndjson | jq 'select(.event=="heartbeat")'

# Check active dispatches by terminal
ls -la .claude/vnx-system/dispatches/active/

# View terminal locks
cat .claude/vnx-system/state/dashboard_status.json | jq '.locks'

# Monitor terminal commands
tail -f .claude/vnx-system/state/terminal_status.ndjson | jq '.current_command'
```

### Pane Mapping Verification

```bash
# Check terminal-to-pane mapping
cat .claude/vnx-system/state/panes.json | jq '.'

# Verify tmux panes exist
tmux list-panes -aF '#{pane_id} #{pane_current_path}'

# Compare mapping to actual panes
jq -r '.tracks | to_entries[] | "\(.key): \(.value.pane_id)"' \
  .claude/vnx-system/state/panes.json
```

---

## Log Monitoring

### Key Log Files

| Log File | Purpose | Location |
|----------|---------|----------|
| supervisor.log | Process management | .claude/vnx-system/logs/ |
| dispatcher.log | Dispatch routing | .claude/vnx-system/logs/ |
| receipt_processor_v4.log | Receipt processing | .claude/vnx-system/logs/ |
| smart_tap.log | Block capture | .claude/vnx-system/logs/ |
| dashboard_gen.log | Dashboard updates | .claude/vnx-system/logs/ |
| intelligence_daemon.log | Intelligence system | .claude/vnx-system/logs/ |

### Log Monitoring Commands

```bash
# Monitor all logs simultaneously
tail -f .claude/vnx-system/logs/*.log

# Watch for errors across all logs
tail -f .claude/vnx-system/logs/*.log | grep -i "error\|failed\|critical"

# Monitor specific component
tail -f .claude/vnx-system/logs/dispatcher.log | grep -E "Dispatch sent|error"

# Check for recent errors (last hour)
find .claude/vnx-system/logs -name "*.log" -mmin -60 -exec grep -i "error" {} \;
```

### Error Pattern Analysis

```bash
# Common error patterns to watch for

# 1. Singleton violations
grep "already running" .claude/vnx-system/logs/supervisor.log

# 2. Process crashes
grep "terminated\|died\|killed" .claude/vnx-system/logs/supervisor.log

# 3. Dispatch failures
grep "failed to send\|dispatch error" .claude/vnx-system/logs/dispatcher.log

# 4. Receipt delivery failures
grep "failed to deliver\|receipt error" .claude/vnx-system/logs/receipt_processor_v4.log

# 5. Date command errors (macOS)
grep "illegal option" .claude/vnx-system/logs/receipt_processor_v4.log

# 6. Template compilation errors
grep -i "template error\|compilation failed" /tmp/dispatcher_v7_compilation.log
```

---

## Performance Monitoring

### System Resource Usage

```bash
# Check VNX process memory
ps aux | grep vnx | awk '{sum+=$6} END {print "Total VNX Memory: " sum/1024 " MB"}'

# Monitor CPU usage by process
for proc in smart_tap dispatcher receipt_processor; do
  echo "$proc: $(ps aux | grep $proc | grep -v grep | awk '{print $3}')% CPU"
done

# Check disk usage
du -sh .claude/vnx-system/*
```

### Queue Processing Performance

```bash
# Monitor queue processing latency
grep "Dispatch sent" .claude/vnx-system/logs/dispatcher.log | \
  awk '{print $2, $3}' | tail -20

# Check receipt processing rate (receipts per minute)
grep "$(date +%Y-%m-%d)" .claude/vnx-system/state/t0_receipts.ndjson | \
  jq -r '.timestamp' | cut -d: -f1,2 | uniq -c

# Monitor queue depth trends
watch -n 5 'find .claude/vnx-system/dispatches -type f -name "*.md" | wc -l'
```

### Intelligence System Performance

```bash
# Check quality scan time
time sqlite3 .claude/vnx-system/state/quality_intelligence.db "SELECT COUNT(*) FROM vnx_code_quality"
# Target: <5s

# Monitor pattern extraction performance
grep "extraction completed" .claude/vnx-system/logs/intelligence_daemon.log | \
  tail -5 | awk '{print $NF}'

# Check database size
ls -lh .claude/vnx-system/state/quality_intelligence.db
# Alert if >100MB
```

### NDJSON File Growth

```bash
# Check NDJSON file sizes
ls -lh .claude/vnx-system/state/*.ndjson

# Count lines in receipts file
wc -l .claude/vnx-system/state/t0_receipts.ndjson

# Alert if any file >10,000 lines
for file in .claude/vnx-system/state/*.ndjson; do
  lines=$(wc -l < "$file")
  if [ "$lines" -gt 10000 ]; then
    echo "⚠️ $file: $lines lines (needs trimming)"
  fi
done
```

---

## Alert Conditions

### Critical Alerts (Immediate Action)

| Condition | Threshold | Action |
|-----------|-----------|--------|
| Process stopped | Any process down | Restart via supervisor |
| Singleton violation | >1 instance of process | Kill all, restart one |
| System deadlock | All active >30min | Emergency restart |
| Dashboard offline | >5min stale | Restart state manager |
| Queue overflow | >20 items | Investigate dispatcher |

### Warning Alerts (Review Required)

| Condition | Threshold | Action |
|-----------|-----------|--------|
| Terminal offline | >5min | Check terminal |
| Queue growing | >10 items | Monitor dispatcher |
| Receipt backlog | >10 pending | Check processor mode |
| Log errors | >10/hour | Review logs |
| Complexity spike | Critical issues >300 | Plan refactoring |

### Routine Checks (Daily/Weekly)

| Condition | Frequency | Action |
|-----------|-----------|--------|
| NDJSON growth | Daily | Trim if >10K lines |
| Log rotation | Daily | Delete logs >7 days |
| Completed dispatch archive | Weekly | Archive >7 days |
| Intelligence digest update | Daily | Force update if stale |
| Lock file cleanup | Daily | Clear stale locks |

---

## Troubleshooting Procedures

### Critical Issue: Multiple Smart Tap Processes

**Symptoms**:
- Old dispatch blocks processed repeatedly
- Multiple Smart Tap PIDs
- Hash deduplication bypassed
- System chaos

**Diagnosis**:
```bash
# Check for duplicates
pgrep -f smart_tap | wc -l  # Should be 1

# View all instances
ps aux | grep smart_tap | grep -v grep
```

**Solution**:
```bash
# STEP 1: Kill ALL Smart Tap processes
pgrep -f smart_tap | xargs kill -9

# STEP 2: Clear locks
rm -rf /tmp/vnx-locks/smart_tap*.lock*

# STEP 3: Verify singleton enforcer
grep -q "atomic lock" .claude/vnx-system/scripts/singleton_enforcer.sh && \
  echo "✓ Enforcer OK" || echo "✗ Enforcer needs update"

# STEP 4: Restart properly
cd .claude/vnx-system && ./scripts/smart_tap_v7_json_translator.sh &

# STEP 5: Verify single instance
sleep 3 && pgrep -f smart_tap | wc -l  # Must be 1
```

### Issue: Manager Block Not Captured

**Symptoms**:
- T0 creates manager block but Smart Tap misses it
- [[DONE]] markers cut off

**Diagnosis**:
```bash
# Check capture window size
grep "capture-pane.*-S" .claude/vnx-system/scripts/smart_tap_v7_json_translator.sh
# Must show: -S -100

# Check Smart Tap is running
pgrep -f smart_tap || echo "Smart Tap not running!"
```

**Solution**:
```bash
# Fix capture window if wrong
sed -i '' 's/-S -50/-S -100/g' .claude/vnx-system/scripts/smart_tap_v7_json_translator.sh

# Restart Smart Tap
pkill -f smart_tap
cd .claude/vnx-system && ./scripts/smart_tap_v7_json_translator.sh &

# Manual capture test
tmux capture-pane -t %0 -p -S -100 | grep -A20 "\[\[TARGET:"
```

### Issue: Receipts Not Appearing in T0

**Symptoms**:
- Receipts generated but not visible in T0
- Time filtering too aggressive

**Diagnosis**:
```bash
# Check receipt processor
pgrep -f receipt_processor_v4 || echo "Processor not running!"

# Check T0 pane detection
jq '.t0.pane_id' .claude/vnx-system/state/panes.json

# Check for time filtering issues
grep "Report too old" .claude/vnx-system/logs/receipt_processor_v4.log | tail -5
```

**Solution**:
```bash
# Solution 1: Switch to catchup mode
pkill -f receipt_processor_v4
VNX_MODE=catchup VNX_MAX_AGE_HOURS=24 \
  .claude/vnx-system/scripts/receipt_processor_v4.sh &

# Solution 2: Verify T0 pane mapping
jq '.t0.pane_id' .claude/vnx-system/state/panes.json
tmux list-panes -aF '#{pane_id}' | grep $(jq -r '.t0.pane_id' .claude/vnx-system/state/panes.json)

# Solution 3: Check receipt delivery
grep "Receipt delivered to T0" .claude/vnx-system/logs/receipt_processor_v4.log | tail -10
```

### Issue: Popup System Not Working

**Symptoms**:
- No automatic popups when T0 creates blocks
- Manual Ctrl+G works

**Diagnosis**:
```bash
# Check queue watcher status
cat .claude/vnx-system/state/dashboard_status.json | jq '.processes.queue_watcher'

# Check for auto-popup mode (should be non-intrusive)
grep -q "Enhanced non-intrusive" .claude/vnx-system/scripts/queue_popup_watcher.sh || \
  echo "⚠️ Auto-popup mode enabled (will interrupt)"
```

**Solution**:
```bash
# Restart queue watcher
cd .claude/vnx-system && ./scripts/queue_popup_watcher.sh &

# Verify dispatch workflow
ls -la .claude/vnx-system/dispatches/queue/
ls -la .claude/vnx-system/dispatches/pending/
```

### Issue: Terminal Permissions Prompting

**Symptoms**:
- T1/T2/T3 constantly asking for Read/Grep/Bash permissions

**Diagnosis**:
```bash
# Check project-level settings
cat .claude/settings.json | jq '.permissions'

# Test hook detection
PWD="$PROJECT_ROOT/.claude/terminals/T2" \
  bash .claude/hooks/t0-readonly-enforcer.sh "Read"
# Should exit 0 (allow)
```

**Solution**:
```bash
# Fix project settings - ensure Bash is in allow list
# In .claude/settings.json:
{
  "permissions": {
    "allow": ["Read(**)", "Grep(**)", "Glob(**)", "Bash"],
    "ask": ["Write(**)", "Edit(**)", "Bash(rm:*)", "Bash(sudo:*)"]
  }
}

# Verify terminal settings match
cat .claude/terminals/T1/settings.json | jq '.permissions.allow'
```

---

## Recovery Procedures

### Emergency Full System Restart

```bash
#!/bin/bash
# Emergency VNX restart procedure

echo "=== Emergency VNX Restart ==="

# Step 1: Stop all processes
echo "Stopping all processes..."
.claude/vnx-system/scripts/vnx_supervisor_simple.sh stop
tmux kill-server 2>/dev/null
pkill -f vnx_ 2>/dev/null
pkill -f smart_tap 2>/dev/null
pkill -f dispatcher 2>/dev/null
pkill -f receipt 2>/dev/null

# Step 2: Clear locks and state
echo "Clearing locks..."
rm -rf /tmp/vnx-locks/*
echo "{}" > .claude/vnx-system/state/locks.json

# Step 3: Archive stuck work
echo "Archiving stuck dispatches..."
mkdir -p .claude/vnx-system/dispatches/stuck-$(date +%s)
mv .claude/vnx-system/dispatches/active/* .claude/vnx-system/dispatches/stuck-*/ 2>/dev/null

# Step 4: Trim oversize files
echo "Trimming state files..."
for file in .claude/vnx-system/state/*.ndjson; do
  if [ -f "$file" ] && [ $(wc -l < "$file") -gt 10000 ]; then
    tail -5000 "$file" > "$file.tmp"
    mv "$file.tmp" "$file"
  fi
done

# Step 5: Restart system
echo "Restarting VNX..."
./VNX_HYBRID_FINAL.sh

# Step 6: Verify recovery
sleep 10
echo "=== Verification ==="
pgrep -f smart_tap | wc -l  # Should be 1
pgrep -f dispatcher | wc -l  # Should be 1
curl -s http://localhost:8080 >/dev/null && echo "✓ Dashboard online" || echo "✗ Dashboard offline"

echo "=== Restart Complete ==="
```

### Process-Specific Recovery

**Smart Tap Recovery**:
```bash
# Kill Smart Tap
pgrep -f smart_tap | xargs kill 2>/dev/null
sleep 2

# Clear hash deduplication
> .claude/vnx-system/state/processed_block_hashes.txt

# Restart
cd .claude/vnx-system && ./scripts/smart_tap_v7_json_translator.sh &
```

**Dispatcher Recovery**:
```bash
# Kill dispatcher
pkill -f dispatcher_v7

# Clear stuck dispatches
mkdir -p .claude/vnx-system/dispatches/stuck
mv .claude/vnx-system/dispatches/active/* .claude/vnx-system/dispatches/stuck/

# Restart
cd .claude/vnx-system && ./scripts/dispatcher_v7_compilation.sh &
```

**Receipt System Recovery**:
```bash
# Kill receipt processor
pkill -f receipt_processor

# Reset processing cursors
echo "0" > .claude/vnx-system/state/.track_a_cursor
echo "0" > .claude/vnx-system/state/.track_b_cursor
echo "0" > .claude/vnx-system/state/.track_c_cursor

# Restart in catchup mode
VNX_MODE=catchup VNX_MAX_AGE_HOURS=24 \
  .claude/vnx-system/scripts/receipt_processor_v4.sh &
```

### Intelligence System Recovery

```bash
# Clear intelligence state
> .claude/vnx-system/state/t0_brief.json

# Rebuild digests
cd .claude/vnx-system/scripts
./build_t0_quality_digest.py
./build_t0_report_digest.py

# Restart intelligence daemon
pkill -f intelligence_daemon
./intelligence_daemon.py &
```

### Restore from Backup

```bash
# Stop system
.claude/vnx-system/scripts/vnx_supervisor_simple.sh stop

# Restore state from backup
tar -xzf vnx-state-YYYYMMDD.tar.gz

# Clear current work
rm -rf .claude/vnx-system/dispatches/active/*
rm -rf .claude/vnx-system/dispatches/queue/*

# Restart
./VNX_HYBRID_FINAL.sh
```

---

## Preventive Maintenance

### Daily Tasks

```bash
# Morning startup checklist
./VNX_HYBRID_FINAL.sh
.claude/vnx-system/scripts/vnx_supervisor_simple.sh status
cat .claude/vnx-system/state/dashboard_status.json | jq '.processes | keys'

# Clean old logs
find .claude/vnx-system/logs -name "*.log" -mtime +7 -delete

# Archive completed dispatches older than 7 days
find .claude/vnx-system/dispatches/completed -mtime +7 -delete

# Verify intelligence digests are current
ls -la .claude/vnx-system/state/t0_*digest.json
```

### Weekly Tasks

```bash
# Full state backup
tar -czf vnx-state-$(date +%Y%m%d).tar.gz \
  .claude/vnx-system/state/ \
  .claude/terminals/*/CLAUDE.md \
  .claude/terminals/*/bootstrap.md

# Rotate report archives
mv .claude/vnx-system/unified_reports/*.md \
  .claude/vnx-system/unified_reports/archive/ 2>/dev/null

# Update intelligence digests
cd .claude/vnx-system/scripts
./build_t0_quality_digest.py
./build_t0_report_digest.py

# Vacuum intelligence database
sqlite3 .claude/vnx-system/state/quality_intelligence.db "VACUUM;"
```

### Monthly Tasks

```bash
# Archive old dispatches
tar -czf dispatches-$(date +%Y%m).tar.gz \
  .claude/vnx-system/dispatches/completed/
rm -rf .claude/vnx-system/dispatches/completed/*

# Clean processed hashes (prevent growth)
tail -1000 .claude/vnx-system/state/processed_block_hashes.txt > /tmp/hashes.tmp
mv /tmp/hashes.tmp .claude/vnx-system/state/processed_block_hashes.txt

# Trim large NDJSON files
for file in .claude/vnx-system/state/*.ndjson; do
  if [ $(wc -l < "$file") -gt 10000 ]; then
    tail -5000 "$file" > "$file.tmp"
    mv "$file.tmp" "$file"
  fi
done

# Review quality trends
sqlite3 .claude/vnx-system/state/quality_intelligence.db \
  "SELECT COUNT(*) as hotspots, AVG(complexity_score) as avg_complexity
   FROM vnx_code_quality WHERE complexity_score > 75"
```

---

## Appendix

### Monitoring Scripts

**health_monitor.sh** - System health check
```bash
#!/bin/bash
# Location: .claude/vnx-system/scripts/health_monitor.sh

# Check all processes
for proc in smart_tap dispatcher receipt_processor queue_popup ack_dispatcher; do
  pgrep -f $proc >/dev/null && echo "✓ $proc" || echo "✗ $proc CRITICAL"
done

# Check dashboard
curl -s http://localhost:8080 >/dev/null && echo "✓ Dashboard" || echo "✗ Dashboard"

# Check singleton enforcement
for proc in smart_tap dispatcher receipt_processor; do
  count=$(pgrep -f $proc | wc -l)
  [ "$count" -eq 1 ] && echo "✓ $proc singleton" || echo "🚨 $proc: $count instances"
done
```

### Log Locations

| Component | Log Path |
|-----------|----------|
| Supervisor | `.claude/vnx-system/logs/supervisor.log` |
| Dispatcher | `.claude/vnx-system/logs/dispatcher.log` |
| Receipt Processor | `.claude/vnx-system/logs/receipt_processor_v4.log` |
| Smart Tap | `.claude/vnx-system/logs/smart_tap.log` |
| Dashboard | `.claude/vnx-system/logs/dashboard_gen.log` |
| Intelligence Daemon | `.claude/vnx-system/logs/intelligence_daemon.log` |
| State Manager | `.claude/vnx-system/logs/state_manager.log` |

### Command Reference

**Quick Checks**:
```bash
# System status
.claude/vnx-system/scripts/vnx_supervisor_simple.sh status

# Dashboard check
curl -s http://localhost:8080/api/status | jq '.'

# Process health
pgrep -f "smart_tap|dispatcher|receipt" | wc -l  # Should be 3+

# Queue depth
find .claude/vnx-system/dispatches -name "*.md" -type f | wc -l
```

**Real-Time Monitoring**:
```bash
# Watch dashboard
watch -n 5 'jq .processes .claude/vnx-system/state/dashboard_status.json'

# Monitor receipts
tail -f .claude/vnx-system/state/t0_receipts.ndjson | jq '.event_type'

# Watch dispatch flow
watch -n 2 'ls -la .claude/vnx-system/dispatches/*/*.md | wc -l'

# Monitor logs
tail -f .claude/vnx-system/logs/*.log | grep -i "error\|warning"
```

---

## Operator Commands (VNX Upgrade)

The following commands provide unified visibility and scoped process controls. They replace the need to manually inspect PID files, lock directories, and state JSON.

### `vnx status` — Session Overview

Shows session state, terminal status, queue progress, and open items at a glance:

```bash
vnx status
```

Output includes:
- **Session**: tmux session name and active/stopped status
- **Terminals**: T1–T3 with status (idle/working/blocked), claimed dispatch, worktree
- **Dispatches**: Count of pending and active dispatches
- **Queue**: Progress bar with completed/total PRs
- **Open Items**: Count by severity (blocker/warn/info)

### `vnx ps` — Process Listing

Shows PID, parent PID, uptime, and health for all managed VNX processes:

```bash
vnx ps          # Human-readable table
vnx ps --json   # Structured JSON output
```

Columns: NAME, PID, PPID, UPTIME, HEALTH (ok/DEAD).

### `vnx cleanup` — Orphan Removal

Detects and removes dead PID files and stale lock directories without affecting healthy processes:

```bash
vnx cleanup             # Remove orphans
vnx cleanup --dry-run   # Preview without changes
```

Safety: only removes PID files for processes that are no longer running. Never kills live processes. Stale lock directories are removed only when the holding PID is dead.

### `vnx restart <process>` — Process Restart

Restarts a specific managed process with graceful stop and health check:

```bash
vnx restart dispatcher
vnx restart receipt_processor
vnx restart smart_tap
```

Known processes: dispatcher, smart_tap, receipt_processor, heartbeat_ack_monitor, queue_watcher, dashboard, state_manager, intelligence_daemon, recommendations_engine, vnx_supervisor.

### `vnx recover` — Session Recovery

Recovers a session after crash or unclean shutdown:

```bash
vnx recover              # Standard recovery (6 actions)
vnx recover --aggressive # Force-clean stale state
vnx recover --dry-run    # Preview recovery actions
```

Recovery actions: clear stale locks, remove orphan PIDs, reset terminal claims, restart core processes, re-export intelligence, validate with doctor.

### PID Metadata Model

Managed processes now write enhanced `.meta.json` files alongside PID files:

```json
{
  "name": "dispatcher",
  "pid": 12345,
  "ppid": 1001,
  "started_at": "2026-03-23T06:00:00Z",
  "owner": "vincent",
  "command": "bash /path/to/dispatcher_v8_minimal.sh"
}
```

Location: `.vnx-data/pids/<name>.meta.json`. Used by `vnx ps` for PPID and start timestamp. Written on process start and restart.

### Lock Lifecycle

- **Acquisition**: Atomic `mkdir` in `.vnx-data/locks/<name>.lock/`
- **Heartbeat**: Lock directory contains `pid` and `fingerprint` files
- **Stale detection**: `vnx cleanup` checks if holding PID is alive
- **Expiry**: Terminal state leases auto-expire (GC on every write)

### Troubleshooting: Common Recovery Scenarios

| Scenario | Command | What It Does |
|----------|---------|-------------|
| Duplicate dispatcher | `vnx cleanup && vnx restart dispatcher` | Removes stale PID, starts fresh |
| Terminal stuck "working" | `vnx recover` | Clears expired terminal claims |
| All processes dead | `vnx recover --aggressive` | Force-cleans state, restarts all |
| Lock directory stuck | `vnx cleanup` | Removes lock if holding PID dead |
| Need to check health | `vnx ps --json` | Machine-readable process status |

---

**Version History**:
- **9.0** (2026-03-23): Operator commands (status, ps, cleanup, restart, recover), PID metadata, lock lifecycle
- **8.1** (2026-02-09): Dashboard live sync, auto-discovery PR system, cache headers, 60s update cycle
- **8.0** (2026-01-26): Consolidated from 6 operational guides, added intelligence monitoring
- **7.3** (2026-01-12): Template compilation monitoring, V7.3 features
- **7.0** (2025-09-15): JSON translation monitoring, V7.0 architecture

**Related Documentation**:
- [System Architecture](../core/00_VNX_ARCHITECTURE.md)
- [Dispatch Guide](../DISPATCH_GUIDE.md)
- [Tag Taxonomy](../intelligence/TAG_TAXONOMY.md)
