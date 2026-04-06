#!/bin/bash
# ============================================================================
# VNX Daily Cleanup — Kill stale development processes on macOS
# ============================================================================
# Install as cron job:
#   crontab -e, add:
#   0 2 * * * bash "$(cd "$(dirname "$0")" && pwd)/daily_cleanup.sh" >> "$HOME/.vnx-data/logs/cleanup.log" 2>&1
#
# Usage:
#   bash scripts/daily_cleanup.sh              # Normal run
#   bash scripts/daily_cleanup.sh --dry-run    # Preview what would be killed
#   bash scripts/daily_cleanup.sh --aggressive # Shorter thresholds (1h/1h/30m/6h/3h/2h)
# ============================================================================

set -eo pipefail

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
. "$SCRIPT_DIR/lib/vnx_paths.sh"

# ---------------------------------------------------------------------------
# CLI flags
# ---------------------------------------------------------------------------
DRY_RUN=false
AGGRESSIVE=false

for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=true ;;
    --aggressive) AGGRESSIVE=true ;;
    -h|--help)
      echo "Usage: $0 [--dry-run] [--aggressive]"
      echo "  --dry-run     Show what would be killed without killing"
      echo "  --aggressive  Use shorter age thresholds"
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg" >&2
      exit 1
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Thresholds (in seconds)
# ---------------------------------------------------------------------------
if $AGGRESSIVE; then
  THRESH_CLAUDE=3600        # 1 hour
  THRESH_MCP=3600           # 1 hour
  THRESH_PYTEST=1800        # 30 minutes
  THRESH_VNX_DAEMON=21600   # 6 hours
  THRESH_NEXTJS=10800       # 3 hours
  THRESH_NODE=7200          # 2 hours
else
  THRESH_CLAUDE=43200       # 12 hours
  THRESH_MCP=0              # any age (orphaned MCP processes)
  THRESH_PYTEST=7200        # 2 hours
  THRESH_VNX_DAEMON=86400   # 24 hours
  THRESH_NEXTJS=43200       # 12 hours
  THRESH_NODE=21600         # 6 hours
fi

# ---------------------------------------------------------------------------
# State — bash 3.2 compatible (no associative arrays, no nounset)
# ---------------------------------------------------------------------------
CURRENT_TTY=""
if tty -s 2>/dev/null; then
  CURRENT_TTY="$(tty 2>/dev/null | sed 's|/dev/||' || true)"
fi

KILLED_COUNT=0
SKIPPED_COUNT=0
FAILED_COUNT=0

# Category counters (bash 3.2 compatible — no associative arrays)
CAT_CLAUDE=0
CAT_MCP=0
CAT_PYTEST=0
CAT_VNX_DAEMON=0
CAT_NEXTJS=0
CAT_NODE=0

# Newline-delimited lists (bash 3.2 safe — no arrays for accumulation)
PIDS_TO_KILL=""
KILL_LOG_ENTRIES=""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
ts() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  echo "[$(ts)] $*"
}

# ---------------------------------------------------------------------------
# Category counter helpers
# ---------------------------------------------------------------------------
increment_category() {
  local cat="$1"
  case "$cat" in
    claude)     CAT_CLAUDE=$((CAT_CLAUDE + 1)) ;;
    mcp)        CAT_MCP=$((CAT_MCP + 1)) ;;
    pytest)     CAT_PYTEST=$((CAT_PYTEST + 1)) ;;
    vnx_daemon) CAT_VNX_DAEMON=$((CAT_VNX_DAEMON + 1)) ;;
    nextjs)     CAT_NEXTJS=$((CAT_NEXTJS + 1)) ;;
    node)       CAT_NODE=$((CAT_NODE + 1)) ;;
  esac
}

# ---------------------------------------------------------------------------
# Parse etime to seconds
# etime format from ps: [[dd-]hh:]mm:ss
# Examples: 00:05, 02:30:00, 1-02:30:00, 15:23
# ---------------------------------------------------------------------------
etime_to_seconds() {
  local etime="$1"
  local days=0 hours=0 minutes=0 seconds=0

  # Strip leading/trailing whitespace
  etime="$(echo "$etime" | xargs)"

  # Check for days component (dd-...)
  if [[ "$etime" == *-* ]]; then
    days="${etime%%-*}"
    etime="${etime#*-}"
  fi

  # Split remaining by colons
  local IFS=':'
  local parts
  read -ra parts <<< "$etime"

  local num_parts=${#parts[@]}
  if [[ $num_parts -eq 3 ]]; then
    hours=$((10#${parts[0]}))
    minutes=$((10#${parts[1]}))
    seconds=$((10#${parts[2]}))
  elif [[ $num_parts -eq 2 ]]; then
    minutes=$((10#${parts[0]}))
    seconds=$((10#${parts[1]}))
  elif [[ $num_parts -eq 1 ]]; then
    seconds=$((10#${parts[0]}))
  fi

  echo $(( days * 86400 + hours * 3600 + minutes * 60 + seconds ))
}

# ---------------------------------------------------------------------------
# Format seconds to human-readable
# ---------------------------------------------------------------------------
format_age() {
  local secs=$1
  if [[ $secs -ge 86400 ]]; then
    echo "$((secs / 86400))d$((secs % 86400 / 3600))h"
  elif [[ $secs -ge 3600 ]]; then
    echo "$((secs / 3600))h$((secs % 3600 / 60))m"
  else
    echo "$((secs / 60))m"
  fi
}

# ---------------------------------------------------------------------------
# Protected process check — never kill these
# ---------------------------------------------------------------------------
is_protected() {
  local cmd="$1"
  # System processes
  case "$cmd" in
    *Finder*|*Dock*|*WindowServer*|*SystemUIServer*|*loginwindow*) return 0 ;;
    *launchd*|*kernel_task*|*mds_stores*|*spotlight*) return 0 ;;
  esac
  # Ollama processes
  case "$cmd" in
    *"ollama serve"*|*"ollama runner"*|*/ollama*serve*|*/ollama*runner*) return 0 ;;
  esac
  return 1
}

# ---------------------------------------------------------------------------
# Evaluate a single process for potential cleanup
# Args: pid, etime, tty, command, category, threshold_seconds
# ---------------------------------------------------------------------------
evaluate_process() {
  local pid="$1"
  local etime="$2"
  local proc_tty="$3"
  local cmd="$4"
  local category="$5"
  local threshold="$6"

  # Skip self and parent
  if [[ "$pid" -eq $$ ]] || [[ "$pid" -eq $PPID ]]; then
    return
  fi

  # Skip protected processes
  if is_protected "$cmd"; then
    return
  fi

  # Parse age
  local age_secs
  age_secs="$(etime_to_seconds "$etime")"

  # Skip if below threshold
  if [[ $age_secs -lt $threshold ]]; then
    return
  fi

  # Skip if on current TTY
  local proc_tty_clean
  proc_tty_clean="$(echo "$proc_tty" | xargs)"
  if [[ -n "$CURRENT_TTY" && -n "$proc_tty_clean" && "$proc_tty_clean" != "??" && "$proc_tty_clean" != "-" ]]; then
    if [[ "$CURRENT_TTY" == *"$proc_tty_clean"* || "$proc_tty_clean" == *"$CURRENT_TTY"* ]]; then
      log "[SKIP] PID $pid $(echo "$cmd" | cut -c1-60) (current tty: $proc_tty_clean)"
      SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
      return
    fi
  fi

  local age_human
  age_human="$(format_age "$age_secs")"
  local tty_info=""
  if [[ -n "$proc_tty_clean" && "$proc_tty_clean" != "??" && "$proc_tty_clean" != "-" ]]; then
    tty_info=", tty: $proc_tty_clean"
  fi

  if $DRY_RUN; then
    log "[DRY-RUN] Would kill PID $pid $category: $(echo "$cmd" | cut -c1-80) (age: $age_human$tty_info)"
  else
    log "[KILL] PID $pid $category: $(echo "$cmd" | cut -c1-80) (age: $age_human$tty_info)"
    if [[ -n "$PIDS_TO_KILL" ]]; then
      PIDS_TO_KILL="$PIDS_TO_KILL
$pid"
    else
      PIDS_TO_KILL="$pid"
    fi
  fi

  KILLED_COUNT=$((KILLED_COUNT + 1))
  increment_category "$category"

  # Append to kill log (JSON-safe — escape double quotes in cmd)
  local safe_cmd
  safe_cmd="$(echo "$cmd" | cut -c1-100 | sed 's/"/\\"/g')"
  local entry="{\"pid\":$pid,\"category\":\"$category\",\"age_seconds\":$age_secs,\"age_human\":\"$age_human\",\"command\":\"$safe_cmd\"}"
  if [[ -n "$KILL_LOG_ENTRIES" ]]; then
    KILL_LOG_ENTRIES="$KILL_LOG_ENTRIES,$entry"
  else
    KILL_LOG_ENTRIES="$entry"
  fi
}

# ---------------------------------------------------------------------------
# Parse a ps output line and call evaluate_process
# ---------------------------------------------------------------------------
parse_and_evaluate() {
  local line="$1"
  local category="$2"
  local threshold="$3"

  [[ -z "$line" ]] && return

  local pid etime tty cmd
  pid="$(echo "$line" | awk '{print $1}')"
  etime="$(echo "$line" | awk '{print $2}')"
  tty="$(echo "$line" | awk '{print $3}')"
  cmd="$(echo "$line" | awk '{$1=$2=$3=""; print}' | sed 's/^ *//')"

  evaluate_process "$pid" "$etime" "$tty" "$cmd" "$category" "$threshold"
}

# ---------------------------------------------------------------------------
# Scan functions — each targets a specific process category
# ---------------------------------------------------------------------------

scan_claude_sessions() {
  while IFS= read -r line; do
    parse_and_evaluate "$line" "claude" "$THRESH_CLAUDE"
  done < <(ps -eo pid,etime,tty,command 2>/dev/null \
    | grep -E '(^[[:space:]]*[0-9]+[[:space:]].*/claude[[:space:]]|^[[:space:]]*[0-9]+[[:space:]].*[[:space:]]claude[[:space:]])' \
    | grep -v grep | grep -v "daily_cleanup" || true)
}

scan_mcp_processes() {
  while IFS= read -r line; do
    parse_and_evaluate "$line" "mcp" "$THRESH_MCP"
  done < <(ps -eo pid,etime,tty,command 2>/dev/null \
    | grep -E 'mcp-server-|context7-mcp|mcp-perplexity-search' \
    | grep -v grep || true)
}

scan_pytest_playwright() {
  while IFS= read -r line; do
    parse_and_evaluate "$line" "pytest" "$THRESH_PYTEST"
  done < <(ps -eo pid,etime,tty,command 2>/dev/null \
    | grep -E '(pytest|playwright)' \
    | grep -v grep || true)
}

scan_vnx_daemons() {
  local daemon_patterns='intelligence_daemon\.py|unified_state_manager_v2\.py|heartbeat_ack_monitor\.py|dispatcher_v8_minimal\.sh|smart_tap_v7_json_translator\.sh|queue_popup_watcher\.sh|receipt_processor_v4\.sh|recommendations_engine_daemon\.sh'
  while IFS= read -r line; do
    parse_and_evaluate "$line" "vnx_daemon" "$THRESH_VNX_DAEMON"
  done < <(ps -eo pid,etime,tty,command 2>/dev/null \
    | grep -E "$daemon_patterns" \
    | grep -v grep || true)
}

scan_nextjs_dev() {
  while IFS= read -r line; do
    parse_and_evaluate "$line" "nextjs" "$THRESH_NEXTJS"
  done < <(ps -eo pid,etime,tty,command 2>/dev/null \
    | grep -E 'next dev' \
    | grep -v grep || true)
}

scan_stale_node() {
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    # Skip if already matched by MCP or Next.js scans
    if echo "$line" | grep -qE 'mcp-server-|context7-mcp|mcp-perplexity-search|next dev'; then
      continue
    fi
    parse_and_evaluate "$line" "node" "$THRESH_NODE"
  done < <(ps -eo pid,etime,tty,command 2>/dev/null \
    | grep -E '\b(tsx|esbuild)\b' \
    | grep -v grep || true)
}

# ---------------------------------------------------------------------------
# Kill with grace period: SIGTERM, wait 3s, SIGKILL survivors
# ---------------------------------------------------------------------------
RAM_FREED_MB=0

execute_kills() {
  if [[ -z "$PIDS_TO_KILL" ]]; then
    return
  fi

  # Collect memory before kill for estimate
  local mem_before
  mem_before="$(vm_stat 2>/dev/null | awk '/Pages free/ {gsub(/\./,""); print $3}' || echo 0)"

  # SIGTERM all targets
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done <<< "$PIDS_TO_KILL"

  # Wait for graceful shutdown
  sleep 3

  # SIGKILL any survivors
  local force_killed=0
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    if kill -0 "$pid" 2>/dev/null; then
      log "[FORCE] PID $pid did not terminate gracefully, sending SIGKILL"
      kill -9 "$pid" 2>/dev/null || true
      force_killed=$((force_killed + 1))
    fi
  done <<< "$PIDS_TO_KILL"

  if [[ $force_killed -gt 0 ]]; then
    log "[INFO] Force-killed $force_killed stubborn process(es)"
  fi

  # Estimate RAM freed (rough: compare free pages before/after)
  sleep 1
  local mem_after
  mem_after="$(vm_stat 2>/dev/null | awk '/Pages free/ {gsub(/\./,""); print $3}' || echo 0)"
  local page_size=16384  # Apple Silicon page size
  local pages_freed=$(( mem_after - mem_before ))
  if [[ $pages_freed -lt 0 ]]; then
    pages_freed=0
  fi
  RAM_FREED_MB=$(( pages_freed * page_size / 1048576 ))
}

# ---------------------------------------------------------------------------
# Write JSON report
# ---------------------------------------------------------------------------
write_report() {
  local report_path="$VNX_STATE_DIR/cleanup_report.json"
  mkdir -p "$VNX_STATE_DIR"

  # Build category JSON — only include non-zero categories
  local cat_parts=""
  if [[ $CAT_CLAUDE -gt 0 ]]; then
    cat_parts="\"claude\":$CAT_CLAUDE"
  fi
  if [[ $CAT_MCP -gt 0 ]]; then
    [[ -n "$cat_parts" ]] && cat_parts="$cat_parts,"
    cat_parts="${cat_parts}\"mcp\":$CAT_MCP"
  fi
  if [[ $CAT_PYTEST -gt 0 ]]; then
    [[ -n "$cat_parts" ]] && cat_parts="$cat_parts,"
    cat_parts="${cat_parts}\"pytest\":$CAT_PYTEST"
  fi
  if [[ $CAT_VNX_DAEMON -gt 0 ]]; then
    [[ -n "$cat_parts" ]] && cat_parts="$cat_parts,"
    cat_parts="${cat_parts}\"vnx_daemon\":$CAT_VNX_DAEMON"
  fi
  if [[ $CAT_NEXTJS -gt 0 ]]; then
    [[ -n "$cat_parts" ]] && cat_parts="$cat_parts,"
    cat_parts="${cat_parts}\"nextjs\":$CAT_NEXTJS"
  fi
  if [[ $CAT_NODE -gt 0 ]]; then
    [[ -n "$cat_parts" ]] && cat_parts="$cat_parts,"
    cat_parts="${cat_parts}\"node\":$CAT_NODE"
  fi

  local mode="normal"
  if $DRY_RUN; then
    mode="dry-run"
  elif $AGGRESSIVE; then
    mode="aggressive"
  fi

  cat > "$report_path" << ENDJSON
{
  "timestamp": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "mode": "$mode",
  "killed_count": $KILLED_COUNT,
  "skipped_count": $SKIPPED_COUNT,
  "failed_count": $FAILED_COUNT,
  "ram_freed_mb": $RAM_FREED_MB,
  "categories": {$cat_parts},
  "kills": [${KILL_LOG_ENTRIES}]
}
ENDJSON

  log "[INFO] Report written to $report_path"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  local mode_label="normal"
  if $DRY_RUN; then
    mode_label="dry-run"
  elif $AGGRESSIVE; then
    mode_label="aggressive"
  fi

  log "=== VNX Daily Cleanup ==="
  log "Mode: $mode_label (--dry-run for preview)"
  if [[ -n "$CURRENT_TTY" ]]; then
    log "Current TTY: $CURRENT_TTY (processes here will be skipped)"
  fi

  # Run all scanners
  scan_claude_sessions
  scan_mcp_processes
  scan_pytest_playwright
  scan_vnx_daemons
  scan_nextjs_dev
  scan_stale_node

  # Execute kills (skipped in dry-run mode)
  if ! $DRY_RUN; then
    execute_kills
  fi

  # Summary
  log "=== Summary ==="
  if $DRY_RUN; then
    log "Would kill: $KILLED_COUNT processes"
  else
    log "Killed: $KILLED_COUNT processes"
  fi
  log "Skipped: $SKIPPED_COUNT processes (current session)"

  # Category breakdown
  local cat_summary=""
  if [[ $CAT_CLAUDE -gt 0 ]]; then
    cat_summary="claude=$CAT_CLAUDE"
  fi
  if [[ $CAT_MCP -gt 0 ]]; then
    [[ -n "$cat_summary" ]] && cat_summary="$cat_summary, "
    cat_summary="${cat_summary}mcp=$CAT_MCP"
  fi
  if [[ $CAT_PYTEST -gt 0 ]]; then
    [[ -n "$cat_summary" ]] && cat_summary="$cat_summary, "
    cat_summary="${cat_summary}pytest=$CAT_PYTEST"
  fi
  if [[ $CAT_VNX_DAEMON -gt 0 ]]; then
    [[ -n "$cat_summary" ]] && cat_summary="$cat_summary, "
    cat_summary="${cat_summary}vnx_daemon=$CAT_VNX_DAEMON"
  fi
  if [[ $CAT_NEXTJS -gt 0 ]]; then
    [[ -n "$cat_summary" ]] && cat_summary="$cat_summary, "
    cat_summary="${cat_summary}nextjs=$CAT_NEXTJS"
  fi
  if [[ $CAT_NODE -gt 0 ]]; then
    [[ -n "$cat_summary" ]] && cat_summary="$cat_summary, "
    cat_summary="${cat_summary}node=$CAT_NODE"
  fi
  if [[ -n "$cat_summary" ]]; then
    log "Categories: $cat_summary"
  fi

  if ! $DRY_RUN && [[ $RAM_FREED_MB -gt 0 ]]; then
    log "Estimated RAM freed: ~${RAM_FREED_MB}MB"
  fi

  # Write JSON report
  write_report

  log "=== Done ==="
}

main
