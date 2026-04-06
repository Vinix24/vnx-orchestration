#!/usr/bin/env bash
# Coding Benchmark v2 -- Real-world SEOcrawler + bash-to-python tasks
# 6 models x 8 tasks, with thinking/no-think variants for qwen3.5
#
# Token limits: num_predict=16384, num_ctx=32768 (fits 24GB Mac)
#
# Usage: nohup bash scripts/benchmark_coding_v2_run.sh > /dev/null 2>&1 &

set -euo pipefail

export PATH="/opt/homebrew/opt/python@3.12/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VNX_BASE="$(dirname "$SCRIPT_DIR")"
REPORT_DIR="$VNX_BASE/reports/benchmarks"
LOG_FILE="$REPORT_DIR/coding_v2_$(date +%Y%m%d_%H%M%S).log"

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

run_model() {
    local phase="$1"
    local model="$2"
    local size="$3"
    local timeout="$4"
    local extra_flags="$5"

    log ""
    log "=== Phase $phase: $model ($size) ==="
    log "Settings: num_predict=16384, num_ctx=32768"
    log "Flags: $extra_flags"
    log "Memory: $(free_mem)"
    rm -f "$REPORT_DIR/benchmark_progress.json"

    if /opt/homebrew/opt/python@3.12/bin/python3.12 scripts/llm_benchmark_coding_v2.py \
        --models "$model" \
        --timeout "$timeout" \
        --num-predict 16384 \
        --num-ctx 32768 \
        $extra_flags 2>&1 | tee -a "$LOG_FILE"; then
        log "$model DONE"
    else
        log "$model FAILED ($?)"
    fi
    clean_between_runs "$model"
}

log "=== Coding Benchmark v2: Real-World Tasks ==="
log "Models: qwen2.5-coder:14b, qwen3.5:9b (x2), qwen3.5:35b-a3b (x2), devstral, codestral"
log "Tasks: 4 SEOcrawler + 4 bash-to-python migration"
log "Token limits: num_predict=16384, num_ctx=32768"
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

# Phase 1: qwen2.5-coder:14b (baseline champion)
run_model "1/8"  "qwen2.5-coder:14b"  "9.1 GB"     1200 ""

# Phase 2-3: qwen3.5:9b (no-think vs thinking)
run_model "2/8"  "qwen3.5:9b"         "6.6 GB"      900 "--no-think"
run_model "3/8"  "qwen3.5:9b"         "6.6 GB"     1200 ""

# Phase 4-5: qwen3.5:35b-a3b MoE (no-think vs thinking)
run_model "4/8"  "qwen3.5:35b-a3b"    "23 GB MoE"  1500 "--no-think"
run_model "5/8"  "qwen3.5:35b-a3b"    "23 GB MoE"  1800 ""

# Phase 6: devstral (Mistral coding MoE)
run_model "6/8"  "devstral"           "14 GB MoE"   1200 ""

# Phase 7: codestral (Mistral coding dense)
run_model "7/8"  "codestral"          "12 GB"       1200 ""

# Phase 8: BONUS -- qwen3.5:9b thinking with max tokens (stress test)
run_model "8/8"  "qwen3.5:9b"         "6.6 GB"     1800 "--num-predict 32768 --num-ctx 65536"

log ""
log "=== Coding Benchmark v2 Complete ==="
log "Reports: $REPORT_DIR/coding_v2_*.md"
log "Log: $LOG_FILE"

osascript -e 'display notification "Coding Benchmark v2 finished!" with title "VNX Benchmark"' 2>/dev/null || true
