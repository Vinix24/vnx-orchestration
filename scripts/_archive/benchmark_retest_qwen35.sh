#!/usr/bin/env bash
# Retest qwen3.5:35b-a3b and qwen3.5:9b with higher token limits.
# Previous runs hit num_predict=4096 ceiling, causing 50-75% empty outputs.
# This run doubles both num_predict (8192) and num_ctx (16384).
#
# Usage: nohup bash scripts/benchmark_retest_qwen35.sh > /dev/null 2>&1 &

set -euo pipefail

export PATH="/opt/homebrew/opt/python@3.12/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VNX_BASE="$(dirname "$SCRIPT_DIR")"
REPORT_DIR="$VNX_BASE/reports/benchmarks"
LOG_FILE="$REPORT_DIR/retest_qwen35_$(date +%Y%m%d_%H%M%S).log"

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
    for pid in $(pgrep -f "python3.*llm_benchmark" 2>/dev/null || true); do
        [ "$pid" != "$$" ] && kill "$pid" 2>/dev/null || true
    done
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
    log "=== Phase $phase: $model ($size) — HIGH TOKEN RETEST ==="
    log "Settings: num_predict=8192, num_ctx=16384"
    log "Memory: $(free_mem)"
    rm -f "$REPORT_DIR/benchmark_progress.json"

    if /opt/homebrew/opt/python@3.12/bin/python3.12 scripts/llm_benchmark.py \
        --models "$model" \
        --timeout "$timeout" \
        --num-predict 8192 \
        --num-ctx 16384 \
        $extra_flags 2>&1 | tee -a "$LOG_FILE"; then
        log "$model DONE"
    else
        log "$model FAILED ($?)"
    fi
    clean_between_runs "$model"
}

log "=== Run 2: Retest + Marketing Suite ==="
log "Models: qwen3.5:35b-a3b, qwen3.5:9b, devstral, codestral"
log "Reason: qwen3.5 hit num_predict=4096 ceiling; Mistral never ran marketing tasks"
log "Change: num_predict 4096→8192, num_ctx 8192→16384 (applies to all)"
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

# ---- CODING TASKS (8 tasks) ----
log ""
log "===== PART 1: CODING/SEOCRAWLER TASKS ====="

# 35b is the promising one — test with and without thinking
run_model "1/8"  "qwen3.5:35b-a3b"  "23 GB MoE"  1200 "--no-think"
run_model "2/8"  "qwen3.5:35b-a3b"  "23 GB MoE"  1200 ""
# 9b for comparison
run_model "3/8"  "qwen3.5:9b"       "6.6 GB"      900 "--no-think"
run_model "4/8"  "qwen3.5:9b"       "6.6 GB"      900 ""

# ---- MARKETING TASKS (10 tasks) ----
log ""
log "===== PART 2: MARKETING/MKB TASKS ====="

# Qwen 3.5 with higher token limits
run_model "5/12"  "qwen3.5:35b-a3b"  "23 GB MoE"  1200 "--no-think --marketing"
run_model "6/12"  "qwen3.5:35b-a3b"  "23 GB MoE"  1200 "--marketing"
run_model "7/12"  "qwen3.5:9b"       "6.6 GB"      900 "--no-think --marketing"
run_model "8/12"  "qwen3.5:9b"       "6.6 GB"      900 "--marketing"

# Mistral models — best Dutch language from Run 1, first time on marketing tasks
# Timeout 900s (not 600s) because 5-6 tok/s + num_predict=8192 needs ~1200s worst case
run_model "9/12"   "devstral"   "14 GB MoE"  900 "--marketing"
run_model "10/12"  "codestral"  "12 GB"       900 "--marketing"

# Also retest Mistral on coding with higher token limits (fix docstring JSON escaping?)
run_model "11/12"  "devstral"   "14 GB MoE"  900 ""
run_model "12/12"  "codestral"  "12 GB"       900 ""

log ""
log "=== Full Retest Complete (Coding + Marketing) ==="
log "Reports: $REPORT_DIR"
log "Log: $LOG_FILE"

osascript -e 'display notification "Qwen 3.5 full retest finished!" with title "VNX Benchmark"' 2>/dev/null || true
