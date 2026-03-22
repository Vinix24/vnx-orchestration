#!/usr/bin/env bash
# Marketing Benchmark -- Full comparison: local models + Claude Haiku/Sonnet 4.6
# Runs 10 Dutch MKB marketing tasks across all models
#
# Local models: qwen3.5:35b-a3b, qwen3.5:9b, devstral, codestral
# Cloud models: claude-haiku (4.5), claude-sonnet (4.6)
#
# Usage: nohup bash scripts/benchmark_marketing_full.sh > /dev/null 2>&1 &

set -euo pipefail

export PATH="/opt/homebrew/opt/python@3.12/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VNX_BASE="$(dirname "$SCRIPT_DIR")"
REPORT_DIR="$VNX_BASE/reports/benchmarks"
LOG_FILE="$REPORT_DIR/marketing_full_$(date +%Y%m%d_%H%M%S).log"

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
    curl -s http://localhost:11434/api/generate -d "{\"model\":\"$model\",\"keep_alive\":0}" >/dev/null 2>&1 || true
    sleep 3
    purge 2>/dev/null || true
    sleep 5
    log "Post-cleanup: $(free_mem)"
}

run_local_model() {
    local phase="$1"
    local model="$2"
    local size="$3"
    local timeout="$4"
    local extra_flags="$5"

    log ""
    log "=== Phase $phase: $model ($size) ==="
    log "Settings: num_predict=8192, num_ctx=16384, marketing mode"
    log "Flags: $extra_flags"
    log "Memory: $(free_mem)"
    rm -f "$REPORT_DIR/benchmark_progress.json"

    if /opt/homebrew/opt/python@3.12/bin/python3.12 scripts/llm_benchmark.py \
        --models "$model" \
        --timeout "$timeout" \
        --num-predict 8192 \
        --num-ctx 16384 \
        --marketing \
        $extra_flags 2>&1 | tee -a "$LOG_FILE"; then
        log "$model DONE"
    else
        log "$model FAILED ($?)"
    fi
    clean_between_runs "$model"
}

run_claude_models() {
    local phase="$1"

    log ""
    log "=== Phase $phase: Claude Haiku + Sonnet 4.6 (cloud) ==="
    log "Mode: claude -p subprocess, marketing tasks"
    rm -f "$REPORT_DIR/benchmark_progress.json"

    if /opt/homebrew/opt/python@3.12/bin/python3.12 scripts/llm_benchmark.py \
        --models none \
        --include-claude \
        --marketing 2>&1 | tee -a "$LOG_FILE"; then
        log "Claude models DONE"
    else
        log "Claude models FAILED ($?)"
    fi
}

log "=== Marketing Benchmark: Full Model Comparison ==="
log "Local models: qwen3.5:35b-a3b (no-think + think), qwen3.5:9b (no-think), devstral, codestral"
log "Cloud models: claude-haiku, claude-sonnet (4.6)"
log "Tasks: 10 Dutch MKB marketing tasks"
log "Host: $(hostname), RAM: $(sysctl -n hw.memsize | awk '{printf "%.0f GB", $1/1024/1024/1024}')"
log "Start: $(free_mem)"

if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
    log "Starting Ollama..."
    open -a Ollama
    sleep 10
fi

log "Initial memory purge..."
purge 2>/dev/null || true
sleep 5
log "After purge: $(free_mem)"

cd "$VNX_BASE"

# ---- CLOUD MODELS FIRST (no memory pressure, fast) ----
log ""
log "===== PART 1: CLAUDE CLOUD MODELS ====="
run_claude_models "1/7"

# ---- LOCAL MODELS ----
log ""
log "===== PART 2: LOCAL MODELS ====="

# qwen3.5:35b-a3b -- best Dutch quality from previous runs
run_local_model "2/7"  "qwen3.5:35b-a3b"  "23 GB MoE"  1200 "--no-think"
run_local_model "3/7"  "qwen3.5:35b-a3b"  "23 GB MoE"  1200 ""

# qwen3.5:9b -- fastest local, no-think only (thinking wastes tokens on marketing)
run_local_model "4/7"  "qwen3.5:9b"       "6.6 GB"      900 "--no-think"

# Mistral models -- first full marketing comparison
run_local_model "5/7"  "devstral"          "14 GB MoE"   900 ""
run_local_model "6/7"  "codestral"         "12 GB"       900 ""

# phi4-mini -- speed demon, see if it handles Dutch marketing
run_local_model "7/7"  "phi4-mini"         "2.5 GB"      300 ""

log ""
log "=== Marketing Benchmark Complete ==="
log "Reports: $REPORT_DIR"
log "Log: $LOG_FILE"
log ""
log "Compare local vs cloud with the generated reports!"

osascript -e 'display notification "Marketing Benchmark finished!" with title "VNX Benchmark"' 2>/dev/null || true
