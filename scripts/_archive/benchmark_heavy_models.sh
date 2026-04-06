#!/usr/bin/env bash
# Run heavy model benchmarks sequentially: 27B then 35B-A3B
# Scheduled to run when all apps are closed for maximum RAM availability.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VNX_BASE="$(dirname "$SCRIPT_DIR")"
REPORT_DIR="$VNX_BASE/reports/benchmarks"
LOG_FILE="$REPORT_DIR/heavy_benchmark_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$REPORT_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "=== Heavy Model Benchmark Started ==="
log "Host: $(hostname), RAM: $(sysctl -n hw.memsize | awk '{printf "%.0f GB", $1/1024/1024/1024}')"
log "Free memory: $(vm_stat | awk '/Pages free/ {printf "%.1f GB", $3*4096/1024/1024/1024}')"

# Ensure Ollama is running
if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
    log "Starting Ollama..."
    open -a Ollama
    sleep 10
fi

# --- Run 27B ---
log ""
log "=== Phase 1: qwen3.5:27b (17 GB) ==="
log "Starting 27B benchmark with --no-think..."

cd "$VNX_BASE"
rm -f "$REPORT_DIR/benchmark_progress.json"

if python3 scripts/llm_benchmark.py --models qwen3.5:27b --no-think 2>&1 | tee -a "$LOG_FILE"; then
    log "27B benchmark COMPLETED successfully"
else
    log "27B benchmark FAILED (exit code: $?)"
fi

# Unload 27B to free RAM before loading 35B
log "Unloading 27B model..."
curl -s http://localhost:11434/api/generate -d '{"model":"qwen3.5:27b","keep_alive":0}' >/dev/null 2>&1 || true
sleep 5

log "Free memory after unload: $(vm_stat | awk '/Pages free/ {printf "%.1f GB", $3*4096/1024/1024/1024}')"

# --- Run 35B-A3B ---
log ""
log "=== Phase 2: qwen3.5:35b-a3b (23 GB, MoE) ==="
log "Starting 35B-A3B benchmark with --no-think..."

rm -f "$REPORT_DIR/benchmark_progress.json"

if python3 scripts/llm_benchmark.py --models "qwen3.5:35b-a3b" --no-think 2>&1 | tee -a "$LOG_FILE"; then
    log "35B-A3B benchmark COMPLETED successfully"
else
    log "35B-A3B benchmark FAILED (exit code: $?) — expected if RAM insufficient"
fi

# Cleanup
curl -s http://localhost:11434/api/generate -d '{"model":"qwen3.5:35b-a3b","keep_alive":0}' >/dev/null 2>&1 || true

log ""
log "=== Heavy Model Benchmark Finished ==="
log "Reports in: $REPORT_DIR"
log "Log: $LOG_FILE"

# Send notification
osascript -e 'display notification "Heavy model benchmark finished. Check reports." with title "VNX Benchmark"' 2>/dev/null || true
