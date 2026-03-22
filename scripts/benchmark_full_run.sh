#!/usr/bin/env bash
# Full Benchmark Run -- Marketing (with Claude) + Coding v2
# Part 1: Marketing (10 tasks) -- Claude Haiku/Sonnet + 5 local models (7 phases)
# Part 2: Coding v2 (8 tasks) -- 6 local model runs (8 phases)
# Total: 15 phases, estimated 10-14 hours
#
# Usage: nohup bash .claude/vnx-system/scripts/benchmark_full_run.sh > /dev/null 2>&1 &
# Monitor: tail -f .claude/vnx-system/reports/benchmarks/full_run_*.log

set -euo pipefail

export PATH="/opt/homebrew/opt/python@3.12/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VNX_BASE="$(dirname "$SCRIPT_DIR")"
REPORT_DIR="$VNX_BASE/reports/benchmarks"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="$REPORT_DIR/full_run_${TIMESTAMP}.log"

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

run_marketing() {
    local phase="$1"
    local model="$2"
    local timeout="$3"
    local extra_flags="$4"

    log ""
    log "=== MKT Phase $phase: $model ==="
    log "Memory: $(free_mem)"
    rm -f "$REPORT_DIR/benchmark_progress.json"

    if /opt/homebrew/opt/python@3.12/bin/python3.12 "$VNX_BASE/scripts/llm_benchmark.py" \
        --models "$model" \
        --timeout "$timeout" \
        --num-predict 8192 \
        --num-ctx 16384 \
        --marketing \
        $extra_flags 2>&1 | tee -a "$LOG_FILE"; then
        log "$model marketing DONE"
    else
        log "$model marketing FAILED ($?)"
    fi
    clean_between_runs "$model"
}

run_claude_marketing() {
    local phase="$1"

    log ""
    log "=== MKT Phase $phase: Claude Haiku + Sonnet 4.6 ==="
    rm -f "$REPORT_DIR/benchmark_progress.json"

    if /opt/homebrew/opt/python@3.12/bin/python3.12 "$VNX_BASE/scripts/llm_benchmark.py" \
        --models none \
        --include-claude \
        --marketing 2>&1 | tee -a "$LOG_FILE"; then
        log "Claude marketing DONE"
    else
        log "Claude marketing FAILED ($?)"
    fi
}

run_coding() {
    local phase="$1"
    local model="$2"
    local timeout="$3"
    local extra_flags="$4"

    log ""
    log "=== CODE Phase $phase: $model ==="
    log "Memory: $(free_mem)"
    rm -f "$REPORT_DIR/benchmark_progress.json"

    if /opt/homebrew/opt/python@3.12/bin/python3.12 "$VNX_BASE/scripts/llm_benchmark_coding_v2.py" \
        --models "$model" \
        --timeout "$timeout" \
        --num-predict 16384 \
        --num-ctx 32768 \
        $extra_flags 2>&1 | tee -a "$LOG_FILE"; then
        log "$model coding DONE"
    else
        log "$model coding FAILED ($?)"
    fi
    clean_between_runs "$model"
}

# =====================================================================
log "============================================================"
log "  FULL BENCHMARK RUN"
log "  Part 1: Marketing (7 phases) + Part 2: Coding v2 (8 phases)"
log "  Started: $(date)"
log "============================================================"
log "Host: $(hostname), RAM: $(sysctl -n hw.memsize | awk '{printf "%.0f GB", $1/1024/1024/1024}')"
log "Start: $(free_mem)"

if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
    log "Starting Ollama..."
    open -a Ollama
    sleep 10
fi

purge 2>/dev/null || true
sleep 5

cd "$VNX_BASE"

# =====================================================================
# PART 1: MARKETING (10 Dutch MKB tasks)
# =====================================================================
log ""
log "###########################################################"
log "  PART 1/2: MARKETING BENCHMARK (10 tasks per model)"
log "###########################################################"

# Cloud first (fast, no memory pressure)
run_claude_marketing "1/7"

# Local models
run_marketing "2/7"  "qwen3.5:35b-a3b"  1200 "--no-think"
run_marketing "3/7"  "qwen3.5:35b-a3b"  1200 ""
run_marketing "4/7"  "qwen3.5:9b"        900 "--no-think"
run_marketing "5/7"  "devstral"           900 ""
run_marketing "6/7"  "codestral"          900 ""
run_marketing "7/7"  "phi4-mini"          300 ""

log ""
log ">>> PART 1 COMPLETE: Marketing benchmark finished <<<"
log ""

# =====================================================================
# PART 2: CODING V2 (4 SEOcrawler + 4 bash-to-python)
# =====================================================================
log ""
log "###########################################################"
log "  PART 2/2: CODING BENCHMARK V2 (8 tasks per model)"
log "###########################################################"

run_coding "1/8"  "qwen2.5-coder:14b"  1200 ""
run_coding "2/8"  "qwen3.5:9b"          900 "--no-think"
run_coding "3/8"  "qwen3.5:9b"         1200 ""
run_coding "4/8"  "qwen3.5:35b-a3b"    1500 "--no-think"
run_coding "5/8"  "qwen3.5:35b-a3b"    1800 ""
run_coding "6/8"  "devstral"           1200 ""
run_coding "7/8"  "codestral"          1200 ""
run_coding "8/8"  "qwen3.5:9b"         1800 "--num-predict 32768 --num-ctx 65536"

log ""
log "============================================================"
log "  FULL BENCHMARK RUN COMPLETE"
log "  Finished: $(date)"
log "  Reports: $REPORT_DIR"
log "  Log: $LOG_FILE"
log "============================================================"

osascript -e 'display notification "Full benchmark run finished!" with title "VNX Benchmark"' 2>/dev/null || true
