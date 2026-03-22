#!/usr/bin/env bash
# Run ALL 7 models sequentially, biggest first.
# Cleans memory between each run: unload model + purge + kill stale.
# Usage: nohup bash scripts/benchmark_all_models.sh > /dev/null 2>&1 &

set -euo pipefail

# Ensure full PATH for nohup (aliases not available)
export PATH="/opt/homebrew/opt/python@3.12/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VNX_BASE="$(dirname "$SCRIPT_DIR")"
REPORT_DIR="$VNX_BASE/reports/benchmarks"
LOG_FILE="$REPORT_DIR/all_models_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$REPORT_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

free_mem() {
    /opt/homebrew/opt/python@3.12/bin/python3.12 -c "import psutil; m=psutil.virtual_memory(); print(f'{m.available/1024**3:.1f} GB free / {m.percent}% used / swap {psutil.swap_memory().used/1024**3:.1f} GB')"
}

clean_between_runs() {
    local model="$1"
    log "Cleaning up after $model..."

    # 1. Unload model from Ollama
    curl -s http://localhost:11434/api/generate -d "{\"model\":\"$model\",\"keep_alive\":0}" >/dev/null 2>&1 || true
    sleep 3

    # 2. Kill stale benchmark processes (not ourselves)
    local my_pid=$$
    for pid in $(pgrep -f "python3.*llm_benchmark" 2>/dev/null || true); do
        [ "$pid" != "$my_pid" ] && kill "$pid" 2>/dev/null || true
    done

    # 3. Purge macOS disk cache -> free RAM
    purge 2>/dev/null || true
    sleep 5

    log "Post-cleanup: $(free_mem)"
}

run_model() {
    local phase="$1"
    local model="$2"
    local size="$3"
    local timeout="$4"
    local extra_flags="$5"

    log ""
    log "=== Phase $phase: $model ($size) ==="
    log "Memory: $(free_mem)"
    rm -f "$REPORT_DIR/benchmark_progress.json"

    if /opt/homebrew/opt/python@3.12/bin/python3.12 scripts/llm_benchmark.py --models "$model" --timeout "$timeout" $extra_flags 2>&1 | tee -a "$LOG_FILE"; then
        log "$model DONE"
    else
        log "$model FAILED ($?)"
    fi
    clean_between_runs "$model"
}

log "=== Full 7-Model Benchmark Suite (biggest first) ==="
log "Host: $(hostname), RAM: $(sysctl -n hw.memsize | awk '{printf "%.0f GB", $1/1024/1024/1024}')"
log "Start: $(free_mem)"

# Ensure Ollama is running
if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
    log "Starting Ollama..."
    open -a Ollama
    sleep 10
fi

# Initial purge
log "Initial memory purge..."
purge 2>/dev/null || true
sleep 5
log "After purge: $(free_mem)"

cd "$VNX_BASE"

# Biggest first — heavy models while user is away
run_model "1/9"  "qwen3.5:35b-a3b"   "23 GB MoE"  900 "--no-think"
run_model "2/9"  "qwen3.5:35b-a3b"   "23 GB MoE"  900 ""
run_model "3/9"  "qwen3.5:27b"       "17 GB"       900 "--no-think"
run_model "4/9"  "devstral"           "14 GB MoE"   600 ""
run_model "5/9"  "codestral"          "12 GB"        600 ""
run_model "6/9"  "qwen2.5-coder:14b" "9 GB"         300 ""
run_model "7/9"  "qwen3.5:9b"        "6.6 GB"       900 "--no-think"
run_model "8/9"  "qwen3.5:9b"        "6.6 GB"       900 ""
run_model "9/9"  "phi4-mini"          "2.5 GB"       300 ""

# ---- Done ----
log ""
log "=== All 9 Runs Complete (7 models, 2x think/no-think) ==="
log "Reports: $REPORT_DIR"
log "Log: $LOG_FILE"

osascript -e 'display notification "All 7 model benchmarks finished!" with title "VNX Benchmark"' 2>/dev/null || true
