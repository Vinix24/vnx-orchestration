#!/usr/bin/env python3
"""
VNX LLM Benchmark v2 -- Real-World Coding Tasks

Focuses on actual SEOcrawler code generation and VNX bash-to-python migration.
Uses the same framework as llm_benchmark.py but with harder, more realistic tasks.

Usage:
  # All 6 models, all 8 tasks:
  python3 scripts/llm_benchmark_coding_v2.py

  # Specific models:
  python3 scripts/llm_benchmark_coding_v2.py --models qwen3.5:9b,codestral

  # Only SEOcrawler tasks (4):
  python3 scripts/llm_benchmark_coding_v2.py --tasks crawl4ai_extractor,sse_endpoint,storage_batch_upsert,dutch_validator

  # Only bash-to-python tasks (4):
  python3 scripts/llm_benchmark_coding_v2.py --tasks receipt_processor,dispatcher_routing,health_monitor,state_consolidator
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
VNX_BASE = SCRIPT_DIR.parent
REPORT_DIR = VNX_BASE / "reports" / "benchmarks"
PROGRESS_FILE = REPORT_DIR / "benchmark_progress.json"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

DEFAULT_MODELS = [
    "qwen2.5-coder:14b",
    "qwen3.5:9b",
    "qwen3.5:35b-a3b",
    "devstral",
    "codestral",
]

TIMEOUT_LOCAL = 900
NUM_CTX = 32768
NUM_PREDICT = 16384

# ---------------------------------------------------------------------------
# Dataclasses (same as v1)
# ---------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    category: str
    name: str
    prompt: str
    expected_format: str
    expected_fields: List[str]


@dataclass
class Result:
    task_id: str
    task_name: str
    category: str
    model: str
    success: bool
    duration_seconds: float
    tokens_generated: int
    tokens_per_second: float
    output: str
    output_length: int
    format_compliant: bool
    fields_present: List[str]
    fields_missing: List[str]
    completeness_score: float
    quality: Optional[int]
    error: Optional[str]
    timestamp: str


@dataclass
class BenchmarkConfig:
    models: List[str]
    task_ids: List[str]
    include_claude: bool
    run_id: str
    started_at: str
    no_think: bool = False


# ---------------------------------------------------------------------------
# SEOcrawler code tasks (4)
# ---------------------------------------------------------------------------

SEOCRAWLER_TASKS: List[Task] = [
    Task(
        id="crawl4ai_extractor",
        category="crawler",
        name="Crawl4AI Extraction Pipeline",
        prompt="""\
Write a production-ready Python module for SEOcrawler that extracts structured SEO data from crawled pages using Crawl4AI 0.7.4.

The module must:
1. Define an `SEOPageExtractor` class that takes a Crawl4AI `CrawlResult` object
2. Extract these fields from the crawled HTML:
   - meta_title, meta_description, canonical_url, h1 (first), h2_count
   - internal_links (list of hrefs), external_links (list of hrefs)
   - og_image, robots_meta (index/noindex/follow/nofollow)
   - word_count (visible text only), has_structured_data (bool)
3. Implement a `extract()` async method returning a dict with all fields
4. Use CSS selectors as primary extraction, with regex fallback for meta tags
5. Handle edge cases: missing tags return None, malformed HTML doesn't crash
6. Include proper type hints, use Python 3.11+ syntax
7. The class should work with this Crawl4AI result structure:
   - result.html: raw HTML string
   - result.cleaned_html: cleaned HTML
   - result.metadata: dict with title, description, etc.
   - result.links: dict with "internal" and "external" lists of link dicts
   - result.success: bool

```python
# Usage example:
from crawl4ai import AsyncWebCrawler, CrawlResult

async with AsyncWebCrawler() as crawler:
    result = await crawler.arun(url="https://example.nl")
    extractor = SEOPageExtractor(result)
    data = await extractor.extract()
    # data = {"meta_title": "...", "meta_description": "...", ...}
```

Return ONLY the complete Python module. Include imports at the top.
Wrap in ```python ... ``` fences.""",
        expected_format="code",
        expected_fields=[
            "class SEOPageExtractor",
            "async def extract",
            "meta_title",
            "meta_description",
            "internal_links",
            "external_links",
            "word_count",
            "has_structured_data",
        ],
    ),
    Task(
        id="sse_endpoint",
        category="api",
        name="FastAPI SSE Scan Endpoint",
        prompt="""\
Write a production-ready FastAPI endpoint that streams SEO scan progress via Server-Sent Events (SSE).

Requirements:
1. POST endpoint at `/api/v1/scan/stream` accepting JSON body: `{"url": str, "max_pages": int (default 5)}`
2. Returns `EventSourceResponse` from `sse_starlette`
3. Emits these SSE events in order:
   - `scan_started`: `{"scan_id": str, "url": str, "max_pages": int, "timestamp": str}`
   - `page_crawled` (per page): `{"page_num": int, "url": str, "status_code": int, "seo_score": float, "issues_count": int}`
   - `scan_progress`: `{"pages_done": int, "pages_total": int, "percent": float}`
   - `scan_finished`: `{"scan_id": str, "pages_crawled": int, "avg_score": float, "duration_seconds": float}`
   - On error: `scan_error`: `{"error": str, "page_url": str | null}`
4. Use `asyncio.Queue` for event buffering between crawler and SSE stream
5. Include proper error handling: catch exceptions per page, continue crawling, emit error events
6. Add request validation with Pydantic model
7. Include a `scan_id` generated with `uuid4().hex[:12]`
8. The generator function must handle client disconnect (check `request.is_disconnected()`)
9. Use `json.dumps` for event data serialization

The endpoint should work with this import structure:
```python
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel, HttpUrl
```

Return ONLY the complete Python module. Include all imports.
Wrap in ```python ... ``` fences.""",
        expected_format="code",
        expected_fields=[
            "EventSourceResponse",
            "scan_started",
            "page_crawled",
            "scan_finished",
            "scan_error",
            "async def",
            "asyncio.Queue",
            "is_disconnected",
        ],
    ),
    Task(
        id="storage_batch_upsert",
        category="storage",
        name="Async Batch Upsert with Retry",
        prompt="""\
Write a production-ready async storage service for SEOcrawler that batch-upserts scan results to Supabase (PostgreSQL).

Requirements:
1. Class `ScanStorageService` with async context manager support (`__aenter__`/`__aexit__`)
2. Constructor takes: `supabase_url: str, supabase_key: str, pool_size: int = 5, batch_size: int = 25`
3. Method `async def upsert_pages(self, scan_id: str, domain: str, pages: list[dict]) -> UpsertResult`
   - Deduplicates pages by URL (keep last occurrence)
   - Splits into batches of `batch_size`
   - Each batch retried up to 3 times with exponential backoff (0.5s, 1s, 2s)
   - Uses upsert with `on_conflict="domain,url,scan_id"`
   - Returns `UpsertResult` dataclass with: inserted, updated, skipped, errors, duration_ms
4. Method `async def get_scan_summary(self, scan_id: str) -> dict | None`
   - Returns: page_count, avg_seo_score, domain, created_at, status
   - Returns None if scan_id not found
5. Include connection health check: `async def health_check(self) -> bool`
6. All database calls must have a 10-second timeout
7. Log all operations using Python `logging` module (logger name: "seocrawler.storage")
8. Handle these specific errors gracefully:
   - Connection timeout -> retry with backoff
   - Duplicate key -> skip and count
   - Pool exhausted -> wait 1s and retry once

Use the `supabase` Python client:
```python
from supabase import create_client, Client
```

Return ONLY the complete Python module with all imports, the UpsertResult dataclass, and the ScanStorageService class.
Wrap in ```python ... ``` fences.""",
        expected_format="code",
        expected_fields=[
            "class ScanStorageService",
            "class UpsertResult",
            "async def upsert_pages",
            "async def get_scan_summary",
            "async def health_check",
            "batch_size",
            "retry",
            "logging",
        ],
    ),
    Task(
        id="dutch_validator",
        category="domain",
        name="Dutch Business Data Validator",
        prompt="""\
Write a production-ready Python module for validating Dutch business data extracted from websites.

The module must implement these validators:

1. `validate_kvk(number: str) -> KvKResult`
   - Must be exactly 8 digits
   - Cannot start with 0
   - Return dataclass: valid (bool), normalized (str), error (str|None)

2. `validate_btw(number: str) -> BTWResult`
   - Format: NL + 9 digits + B + 2 digits (e.g., NL123456789B01)
   - Case-insensitive input, normalize to uppercase
   - Strip spaces and dots from input
   - Return dataclass: valid (bool), normalized (str), error (str|None)

3. `validate_postcode(code: str) -> PostcodeResult`
   - Format: 4 digits + 2 uppercase letters (e.g., 1015 CJ)
   - First digit cannot be 0
   - Normalize: strip spaces, uppercase letters, insert space between digits and letters
   - Return dataclass: valid (bool), normalized (str), error (str|None)

4. `validate_phone(phone: str) -> PhoneResult`
   - Accept formats: +31 20 123 4567, 020-1234567, 0201234567, +31(0)20-123-4567
   - Normalize to international format: +31201234567
   - Validate: must have 10 digits (Dutch) or +31 + 9 digits
   - Return dataclass: valid (bool), normalized (str), original (str), error (str|None)

5. `validate_iban(iban: str) -> IBANResult`
   - Dutch IBANs: NL + 2 check digits + 4 letter bank code + 10 digits
   - Implement mod-97 check digit validation (ISO 7064)
   - Strip spaces, uppercase
   - Return dataclass: valid (bool), normalized (str), bank_code (str|None), error (str|None)

6. `class DutchBusinessValidator` that combines all validators:
   - Method `validate_all(data: dict) -> ValidationReport`
   - Input dict has optional keys: kvk, btw, postcode, phone, iban
   - ValidationReport: results (dict of field -> result), is_valid (bool), error_count (int)

Include comprehensive edge case handling. Use `re` module for pattern matching.
Type hints required. Python 3.11+ syntax.

Return ONLY the complete Python module.
Wrap in ```python ... ``` fences.""",
        expected_format="code",
        expected_fields=[
            "validate_kvk",
            "validate_btw",
            "validate_postcode",
            "validate_phone",
            "validate_iban",
            "class DutchBusinessValidator",
            "dataclass",
            "re",
        ],
    ),
]

# ---------------------------------------------------------------------------
# Bash-to-Python migration tasks (4)
# ---------------------------------------------------------------------------

BASH_RECEIPT_PROCESSOR = """\
#!/bin/bash
# receipt_processor.sh - Watches for new report files, generates NDJSON receipts, delivers to T0
# Simplified version focusing on core logic

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"

REPORTS_DIR="$VNX_REPORTS_DIR"
STATE_DIR="$VNX_STATE_DIR"
RECEIPT_FILE="$STATE_DIR/t0_receipts.ndjson"
PROCESSED_HASHES="$STATE_DIR/processed_receipts.txt"
LOG_FILE="$VNX_LOGS_DIR/receipt_processor.log"
PID_FILE="$VNX_PIDS_DIR/receipt_processor.pid"
POLL_INTERVAL=5
MAX_AGE_HOURS=24
RATE_LIMIT=10
FLOOD_THRESHOLD=50
FLOOD_LOCK="$STATE_DIR/receipt_flood.lock"

echo $$ > "$PID_FILE"
touch "$PROCESSED_HASHES" "$RECEIPT_FILE"

_sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }

log() {
    local level="${1:-INFO}"
    shift
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $*" >> "$LOG_FILE"
}

is_too_old() {
    local file="$1"
    local max_age=$((MAX_AGE_HOURS * 3600))
    local file_age=$(( $(date +%s) - $(stat -f %m "$file" 2>/dev/null || stat -c %Y "$file" 2>/dev/null || echo 0) ))
    [ "$file_age" -gt "$max_age" ]
}

is_already_processed() {
    local hash="$1"
    grep -qF "$hash" "$PROCESSED_HASHES" 2>/dev/null
}

check_flood() {
    if [ -f "$FLOOD_LOCK" ]; then
        local lock_age=$(( $(date +%s) - $(stat -f %m "$FLOOD_LOCK" 2>/dev/null || echo 0) ))
        if [ "$lock_age" -gt 300 ]; then
            rm -f "$FLOOD_LOCK"
            log "INFO" "Flood lock expired, resuming"
            return 1
        fi
        return 0
    fi
    return 1
}

extract_terminal() {
    local report_file="$1"
    local terminal=""
    terminal=$(grep -oP 'terminal["\\'\\s:=]+\\K(T[0-3])' "$report_file" 2>/dev/null | head -1)
    if [ -z "$terminal" ]; then
        terminal=$(basename "$(dirname "$report_file")" | grep -oP 'T[0-3]' || echo "unknown")
    fi
    echo "$terminal"
}

generate_receipt() {
    local report_file="$1"
    local hash="$2"
    local terminal=$(extract_terminal "$report_file")
    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local filename=$(basename "$report_file")
    local task_id=$(echo "$filename" | sed 's/\\.[^.]*$//' | sed 's/_report$//')

    python3 -c "
import json, sys
receipt = {
    'event': 'task_complete',
    'terminal': sys.argv[1],
    'task_id': sys.argv[2],
    'report_file': sys.argv[3],
    'file_hash': sys.argv[4],
    'timestamp': sys.argv[5],
    'status': 'delivered'
}
print(json.dumps(receipt, separators=(',', ':')))
" "$terminal" "$task_id" "$report_file" "$hash" "$timestamp"
}

process_reports() {
    local count=0
    local minute_count=0

    for report_file in "$REPORTS_DIR"/*.md "$REPORTS_DIR"/*.json; do
        [ -f "$report_file" ] || continue

        if check_flood; then
            log "WARN" "Flood protection active, skipping"
            return
        fi

        if is_too_old "$report_file"; then
            continue
        fi

        local hash=$(_sha256 "$report_file")
        if is_already_processed "$hash"; then
            continue
        fi

        local receipt=$(generate_receipt "$report_file" "$hash")
        echo "$receipt" >> "$RECEIPT_FILE"
        echo "$hash" >> "$PROCESSED_HASHES"
        log "INFO" "Processed: $(basename "$report_file") -> $hash"

        count=$((count + 1))
        minute_count=$((minute_count + 1))

        if [ "$minute_count" -ge "$RATE_LIMIT" ]; then
            log "WARN" "Rate limit reached ($RATE_LIMIT/min), pausing 60s"
            sleep 60
            minute_count=0
        fi

        if [ "$count" -ge "$FLOOD_THRESHOLD" ]; then
            touch "$FLOOD_LOCK"
            log "ERROR" "Flood threshold reached ($FLOOD_THRESHOLD), locking"
            return
        fi
    done
}

cleanup() {
    log "INFO" "Receipt processor shutting down"
    rm -f "$PID_FILE"
    exit 0
}
trap cleanup SIGTERM SIGINT

log "INFO" "Receipt processor starting (PID: $$)"
while true; do
    process_reports
    sleep "$POLL_INTERVAL"
done"""

BASH_DISPATCHER_ROUTING = """\
#!/bin/bash
# dispatcher_routing.sh - Routes dispatch requests to available terminals
# Core routing logic extracted from dispatcher_v8

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"

STATE_DIR="$VNX_STATE_DIR"
DISPATCH_DIR="$VNX_DISPATCH_DIR"
QUEUE_DIR="$DISPATCH_DIR/queue"
COMPLETED_DIR="$DISPATCH_DIR/completed"
LOG_FILE="$VNX_LOGS_DIR/dispatcher.log"
TERMINAL_STATE="$STATE_DIR/terminal_state.json"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

get_terminal_status() {
    local terminal="$1"
    if [ ! -f "$TERMINAL_STATE" ]; then
        echo "unknown"
        return
    fi
    python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    state = json.load(f)
t = state.get('terminals', {}).get(sys.argv[2], {})
status = t.get('status', 'unknown')
claimed = t.get('claimed_by', '')
if status == 'idle' and not claimed:
    print('available')
elif status == 'busy' or claimed:
    print('busy')
else:
    print(status)
" "$TERMINAL_STATE" "$terminal"
}

find_available_terminal() {
    local skill="$1"
    local preferred_order="T1 T2 T3"

    for terminal in $preferred_order; do
        local status=$(get_terminal_status "$terminal")
        if [ "$status" = "available" ]; then
            echo "$terminal"
            return 0
        fi
    done
    echo ""
    return 1
}

parse_dispatch_file() {
    local file="$1"
    local dispatch_id=$(basename "$file" .md)
    local skill=$(grep -i "^skill:" "$file" 2>/dev/null | head -1 | sed 's/^[Ss]kill:\\s*//')
    local priority=$(grep -i "^priority:" "$file" 2>/dev/null | head -1 | sed 's/^[Pp]riority:\\s*//')
    local track=$(grep -i "^track:" "$file" 2>/dev/null | head -1 | sed 's/^[Tt]rack:\\s*//')

    echo "$dispatch_id|${skill:-general}|${priority:-P2}|${track:-unassigned}"
}

claim_terminal() {
    local terminal="$1"
    local dispatch_id="$2"
    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    python3 -c "
import json, sys
path = sys.argv[1]
terminal = sys.argv[2]
dispatch_id = sys.argv[3]
ts = sys.argv[4]

with open(path) as f:
    state = json.load(f)

if terminal not in state.get('terminals', {}):
    state.setdefault('terminals', {})[terminal] = {}

state['terminals'][terminal]['status'] = 'busy'
state['terminals'][terminal]['claimed_by'] = dispatch_id
state['terminals'][terminal]['claimed_at'] = ts

with open(path, 'w') as f:
    json.dump(state, f, indent=2)
" "$TERMINAL_STATE" "$terminal" "$dispatch_id" "$timestamp"
}

route_dispatch() {
    local file="$1"
    local parsed=$(parse_dispatch_file "$file")
    local dispatch_id=$(echo "$parsed" | cut -d'|' -f1)
    local skill=$(echo "$parsed" | cut -d'|' -f2)
    local priority=$(echo "$parsed" | cut -d'|' -f3)

    log "Routing dispatch: id=$dispatch_id skill=$skill priority=$priority"

    local terminal=$(find_available_terminal "$skill")
    if [ -z "$terminal" ]; then
        log "WARN: No available terminal for dispatch $dispatch_id"
        return 1
    fi

    claim_terminal "$terminal" "$dispatch_id"
    mv "$file" "$COMPLETED_DIR/${dispatch_id}.md"
    log "Dispatched $dispatch_id -> $terminal (skill=$skill)"
    return 0
}

process_queue() {
    local dispatched=0
    for file in $(ls -t "$QUEUE_DIR"/*.md 2>/dev/null); do
        [ -f "$file" ] || continue
        if route_dispatch "$file"; then
            dispatched=$((dispatched + 1))
        fi
    done
    return $dispatched
}

cleanup() {
    log "Dispatcher routing shutting down"
    exit 0
}
trap cleanup SIGTERM SIGINT

log "=== Dispatcher Routing Starting ==="
while true; do
    process_queue
    sleep 3
done"""

BASH_HEALTH_MONITOR = """\
#!/bin/bash
# health_monitor.sh - Monitors VNX process health and auto-restarts failed processes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"

PID_DIR="$VNX_PIDS_DIR"
LOG_DIR="$VNX_LOGS_DIR"
STATE_DIR="$VNX_STATE_DIR"
HEALTH_LOG="$LOG_DIR/health_monitor.log"
HEALTH_STATE="$STATE_DIR/process_health.json"
CHECK_INTERVAL=10
MAX_RESTARTS=3
COOLDOWN_SECONDS=60

declare -A RESTART_COUNTS
declare -A LAST_RESTART

MONITORED_PROCESSES=(
    "dispatcher:dispatcher_v8_minimal.sh"
    "smart_tap:smart_tap_v7_json_translator.sh"
    "receipt_processor:receipt_processor_v4.sh"
    "heartbeat_ack_monitor:heartbeat_ack_monitor.py"
    "state_manager:unified_state_manager_v2.py"
)

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$HEALTH_LOG"
}

check_process() {
    local name="$1"
    local pid_file="$PID_DIR/${name}.pid"

    if [ ! -f "$pid_file" ]; then
        echo "no_pidfile"
        return
    fi

    local pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
        echo "running:$pid"
    else
        rm -f "$pid_file"
        echo "dead:$pid"
    fi
}

should_restart() {
    local name="$1"
    local count=${RESTART_COUNTS[$name]:-0}
    local last=${LAST_RESTART[$name]:-0}
    local now=$(date +%s)

    if [ "$count" -ge "$MAX_RESTARTS" ]; then
        local elapsed=$((now - last))
        if [ "$elapsed" -lt "$COOLDOWN_SECONDS" ]; then
            return 1
        fi
        RESTART_COUNTS[$name]=0
    fi
    return 0
}

restart_process() {
    local name="$1"
    local script="$2"
    local script_path="$SCRIPT_DIR/$script"

    if [ ! -f "$script_path" ]; then
        log "ERROR: Script not found: $script_path"
        return 1
    fi

    log "Restarting $name ($script)..."

    if [[ "$script" == *.py ]]; then
        nohup python3 "$script_path" >> "$LOG_DIR/${name}.log" 2>&1 &
    else
        nohup bash "$script_path" >> "$LOG_DIR/${name}.log" 2>&1 &
    fi

    local new_pid=$!
    echo "$new_pid" > "$PID_DIR/${name}.pid"

    sleep 2
    if kill -0 "$new_pid" 2>/dev/null; then
        log "Restarted $name successfully (PID: $new_pid)"
        RESTART_COUNTS[$name]=$(( ${RESTART_COUNTS[$name]:-0} + 1 ))
        LAST_RESTART[$name]=$(date +%s)
        return 0
    else
        log "ERROR: $name failed to restart"
        rm -f "$PID_DIR/${name}.pid"
        return 1
    fi
}

update_health_state() {
    python3 -c "
import json, sys, os
from datetime import datetime

health = {'timestamp': datetime.utcnow().isoformat() + 'Z', 'processes': {}}
for line in sys.stdin:
    name, status = line.strip().split('=', 1)
    health['processes'][name] = {
        'status': status.split(':')[0],
        'pid': status.split(':')[1] if ':' in status else None
    }

path = sys.argv[1]
with open(path, 'w') as f:
    json.dump(health, f, indent=2)
" "$HEALTH_STATE" <<< "$(for entry in "${health_data[@]}"; do echo "$entry"; done)"
}

monitor_loop() {
    while true; do
        local health_data=()
        for entry in "${MONITORED_PROCESSES[@]}"; do
            local name="${entry%%:*}"
            local script="${entry#*:}"
            local status=$(check_process "$name")

            health_data+=("$name=$status")

            case "$status" in
                dead:*|no_pidfile)
                    log "ALERT: $name is $status"
                    if should_restart "$name"; then
                        restart_process "$name" "$script"
                    else
                        log "WARN: $name exceeded restart limit, waiting cooldown"
                    fi
                    ;;
                running:*)
                    ;;
            esac
        done

        update_health_state
        sleep "$CHECK_INTERVAL"
    done
}

cleanup() {
    log "Health monitor shutting down"
    exit 0
}
trap cleanup SIGTERM SIGINT

log "=== Health Monitor Starting ==="
log "Monitoring: ${MONITORED_PROCESSES[*]}"
monitor_loop"""

BASH_STATE_CONSOLIDATOR = """\
#!/bin/bash
# state_consolidator.sh - Merges terminal states, dispatch queue, and receipts into unified state
# Runs every 5 seconds to keep dashboard state fresh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"

STATE_DIR="$VNX_STATE_DIR"
DISPATCH_DIR="$VNX_DISPATCH_DIR"
LOG_FILE="$VNX_LOGS_DIR/state_consolidator.log"
UNIFIED_STATE="$STATE_DIR/unified_state.json"
TERMINAL_STATE="$STATE_DIR/terminal_state.json"
RECEIPT_FILE="$STATE_DIR/t0_receipts.ndjson"
CYCLE_INTERVAL=5

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

count_files() {
    local dir="$1"
    local ext="${2:-md}"
    local count=0
    if [ -d "$dir" ]; then
        count=$(find "$dir" -maxdepth 1 -name "*.$ext" -type f 2>/dev/null | wc -l | tr -d ' ')
    fi
    echo "$count"
}

get_queue_stats() {
    local queued=$(count_files "$DISPATCH_DIR/queue")
    local completed=$(count_files "$DISPATCH_DIR/completed")
    local failed=$(count_files "$DISPATCH_DIR/failed")
    echo "$queued|$completed|$failed"
}

get_receipt_stats() {
    if [ ! -f "$RECEIPT_FILE" ]; then
        echo "0|0"
        return
    fi
    local total=$(wc -l < "$RECEIPT_FILE" | tr -d ' ')
    local today=$(grep "$(date +%Y-%m-%d)" "$RECEIPT_FILE" 2>/dev/null | wc -l | tr -d ' ')
    echo "$total|$today"
}

get_terminal_summary() {
    if [ ! -f "$TERMINAL_STATE" ]; then
        echo "{}"
        return
    fi
    python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    state = json.load(f)
terminals = state.get('terminals', {})
summary = {}
for tid, tdata in terminals.items():
    summary[tid] = {
        'status': tdata.get('status', 'unknown'),
        'claimed_by': tdata.get('claimed_by', ''),
        'last_activity': tdata.get('last_activity', '')
    }
print(json.dumps(summary))
" "$TERMINAL_STATE"
}

consolidate() {
    local queue_stats=$(get_queue_stats)
    local receipt_stats=$(get_receipt_stats)
    local terminal_summary=$(get_terminal_summary)
    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    python3 -c "
import json, sys

queue_parts = sys.argv[1].split('|')
receipt_parts = sys.argv[2].split('|')
terminal_json = sys.argv[3]
timestamp = sys.argv[4]

state = {
    'timestamp': timestamp,
    'queue': {
        'pending': int(queue_parts[0]),
        'completed': int(queue_parts[1]),
        'failed': int(queue_parts[2])
    },
    'receipts': {
        'total': int(receipt_parts[0]),
        'today': int(receipt_parts[1])
    },
    'terminals': json.loads(terminal_json),
    'health': 'ok' if int(queue_parts[0]) < 20 else 'degraded'
}

with open(sys.argv[5], 'w') as f:
    json.dump(state, f, indent=2)
" "$queue_stats" "$receipt_stats" "$terminal_summary" "$timestamp" "$UNIFIED_STATE"
}

cleanup() {
    log "State consolidator shutting down"
    exit 0
}
trap cleanup SIGTERM SIGINT

log "=== State Consolidator Starting (${CYCLE_INTERVAL}s cycle) ==="
while true; do
    consolidate
    sleep "$CYCLE_INTERVAL"
done"""

BASH_TO_PYTHON_TASKS: List[Task] = [
    Task(
        id="receipt_processor",
        category="bash_migration",
        name="Receipt Processor (Bash to Python)",
        prompt=f"""\
Convert this VNX receipt processor bash script to production-ready Python 3.11+.

The Python version must:
1. Be functionally identical (same file paths via vnx_paths, same processing logic)
2. Use `pathlib` for all paths
3. Use `logging` module with structured log format: `[timestamp] [LEVEL] message`
4. Use `hashlib.sha256` instead of shasum
5. Use `json` module for receipt generation (no subprocess python3 -c calls)
6. Implement rate limiting with a sliding window counter
7. Implement flood protection with auto-expiring lock file
8. Use `signal` handlers for graceful shutdown
9. Include type hints and dataclasses where appropriate
10. Use `os.stat().st_mtime` for file age checks
11. Include a proper `__main__` guard
12. Make configuration values into class attributes with env var overrides

Here is the bash script:

```bash
{BASH_RECEIPT_PROCESSOR}
```

Return ONLY the complete Python file. No explanations.
Wrap in ```python ... ``` fences.""",
        expected_format="code",
        expected_fields=[
            "pathlib",
            "logging",
            "hashlib",
            "signal",
            "dataclass",
            "__main__",
            "sha256",
            "process_reports",
        ],
    ),
    Task(
        id="dispatcher_routing",
        category="bash_migration",
        name="Dispatcher Routing (Bash to Python)",
        prompt=f"""\
Convert this VNX dispatcher routing bash script to production-ready Python 3.11+.

The Python version must:
1. Be functionally identical (same routing logic, same terminal preference order)
2. Use `pathlib` for all paths
3. Use `logging` module instead of echo
4. Use `json` module directly (no subprocess python3 -c calls)
5. Use proper dataclasses for Dispatch and TerminalState
6. Implement file-based terminal state management with atomic writes (write to .tmp, then rename)
7. Use `signal` handlers for cleanup
8. Include type hints everywhere
9. Parse dispatch markdown files using regex
10. Include a proper `__main__` guard
11. The routing priority: T1 > T2 > T3 (first available)

Here is the bash script:

```bash
{BASH_DISPATCHER_ROUTING}
```

Return ONLY the complete Python file. No explanations.
Wrap in ```python ... ``` fences.""",
        expected_format="code",
        expected_fields=[
            "pathlib",
            "logging",
            "json",
            "signal",
            "dataclass",
            "__main__",
            "route_dispatch",
            "find_available_terminal",
        ],
    ),
    Task(
        id="health_monitor",
        category="bash_migration",
        name="Health Monitor (Bash to Python)",
        prompt=f"""\
Convert this VNX health monitor bash script to production-ready Python 3.11+.

The Python version must:
1. Be functionally identical (same process checks, restart logic, cooldown behavior)
2. Use `pathlib` for all paths
3. Use `logging` module with structured format
4. Use `psutil` for process status checks (instead of kill -0)
5. Use `subprocess.Popen` for process restarts
6. Use `json` module for health state persistence
7. Use `signal` handlers for cleanup
8. Use dataclasses for ProcessConfig and HealthStatus
9. Implement restart tracking with per-process counters and cooldown timers
10. Include type hints, enums for ProcessStatus
11. Include a proper `__main__` guard
12. Support both .py and .sh script types

Here is the bash script:

```bash
{BASH_HEALTH_MONITOR}
```

Return ONLY the complete Python file. No explanations.
Wrap in ```python ... ``` fences.""",
        expected_format="code",
        expected_fields=[
            "pathlib",
            "logging",
            "psutil",
            "subprocess",
            "signal",
            "dataclass",
            "__main__",
            "check_process",
            "restart_process",
        ],
    ),
    Task(
        id="state_consolidator",
        category="bash_migration",
        name="State Consolidator (Bash to Python)",
        prompt=f"""\
Convert this VNX state consolidator bash script to production-ready Python 3.11+.

The Python version must:
1. Be functionally identical (same state merging logic, same output format)
2. Use `pathlib` for all paths (including glob for counting files)
3. Use `logging` module instead of echo
4. Use `json` module directly (no subprocess python3 -c calls)
5. Use dataclasses for QueueStats, ReceiptStats, UnifiedState
6. Implement atomic state file writes (write .tmp, rename)
7. Use `signal` handlers for cleanup
8. Include type hints everywhere
9. Add error handling: if any data source is unavailable, use defaults
10. Include a proper `__main__` guard
11. Add a `health` field: "ok" if queue < 20, "degraded" if < 50, "critical" if >= 50

Here is the bash script:

```bash
{BASH_STATE_CONSOLIDATOR}
```

Return ONLY the complete Python file. No explanations.
Wrap in ```python ... ``` fences.""",
        expected_format="code",
        expected_fields=[
            "pathlib",
            "logging",
            "json",
            "signal",
            "dataclass",
            "__main__",
            "consolidate",
            "atomic",
        ],
    ),
]

ALL_TASKS = SEOCRAWLER_TASKS + BASH_TO_PYTHON_TASKS

# ---------------------------------------------------------------------------
# Ollama runner (from v1)
# ---------------------------------------------------------------------------


def _ollama_api(endpoint: str, payload: dict | None = None,
                method: str = "GET", timeout: int = 10) -> dict:
    url = f"{OLLAMA_URL}{endpoint}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    else:
        req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_ollama_available() -> bool:
    try:
        _ollama_api("/api/tags", timeout=5)
        return True
    except Exception:
        return False


def list_ollama_models() -> List[str]:
    try:
        data = _ollama_api("/api/tags", timeout=5)
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _ollama_stream(model: str, prompt: str, timeout: int) -> tuple:
    url = f"{OLLAMA_URL}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {
            "temperature": 0.3,
            "num_predict": NUM_PREDICT,
            "num_ctx": NUM_CTX,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    chunks = []
    eval_count = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in resp:
            if time.monotonic() - start > timeout:
                raise TimeoutError(f"Streaming exceeded {timeout}s")
            line = line.decode("utf-8").strip()
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("response"):
                chunks.append(chunk["response"])
            if chunk.get("done"):
                eval_count = chunk.get("eval_count", 0)
                break
    elapsed = time.monotonic() - start
    full_text = "".join(chunks)
    if eval_count == 0:
        eval_count = max(1, int(len(full_text.split()) * 1.3))
    return full_text, eval_count, elapsed


def run_ollama(model: str, prompt: str, no_think: bool = False,
               timeout: int = 0) -> Result:
    effective_timeout = timeout if timeout > 0 else TIMEOUT_LOCAL
    start = time.monotonic()
    try:
        actual_prompt = f"/no_think\n{prompt}" if no_think else prompt
        output, eval_count, elapsed = _ollama_stream(
            model, actual_prompt, effective_timeout)
        tps = eval_count / elapsed if elapsed > 0 else 0.0
        return Result(
            task_id="", task_name="", category="", model=model,
            success=True,
            duration_seconds=round(elapsed, 2),
            tokens_generated=eval_count,
            tokens_per_second=round(tps, 1),
            output=output, output_length=len(output),
            format_compliant=False,
            fields_present=[], fields_missing=[],
            completeness_score=0.0, quality=None, error=None,
            timestamp=datetime.now().isoformat(),
        )
    except (TimeoutError, Exception) as exc:
        return _error_result(model, time.monotonic() - start, str(exc))


def _error_result(model: str, elapsed: float, error: str) -> Result:
    return Result(
        task_id="", task_name="", category="", model=model,
        success=False,
        duration_seconds=round(elapsed, 2),
        tokens_generated=0, tokens_per_second=0.0,
        output="", output_length=0,
        format_compliant=False,
        fields_present=[], fields_missing=[],
        completeness_score=0.0, quality=None,
        error=error[:500],
        timestamp=datetime.now().isoformat(),
    )


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------


def _extract_code(text: str) -> Optional[str]:
    match = re.search(r"```python\s*([\s\S]*?)```", text)
    if match:
        return match.group(1).strip()
    if re.match(r"^\s*(#!.*python|import |from |def |class |@|\"\"\")",
                text.strip()):
        return text.strip()
    return None


def _check_python_syntax(code: str) -> bool:
    try:
        compile(code, "<benchmark>", "exec")
        return True
    except SyntaxError:
        return False


def score_result(result: Result, task: Task) -> Result:
    if not result.success:
        result.fields_missing = task.expected_fields[:]
        return result

    if task.expected_format == "code":
        code = _extract_code(result.output)
        if code is not None:
            result.format_compliant = _check_python_syntax(code)
            present = []
            missing = []
            for marker in task.expected_fields:
                if marker in code:
                    present.append(marker)
                else:
                    missing.append(marker)
            result.fields_present = present
            result.fields_missing = missing
            if task.expected_fields:
                result.completeness_score = round(
                    len(present) / len(task.expected_fields), 2)
            else:
                result.completeness_score = 1.0

            # Bonus: count lines of actual code (not comments/blanks)
            code_lines = [l for l in code.split("\n")
                          if l.strip() and not l.strip().startswith("#")]
            if len(code_lines) < 20:
                # Too short for a real implementation
                result.completeness_score = max(0.0,
                    result.completeness_score - 0.2)
                if "too_short" not in (result.error or ""):
                    result.error = f"code_lines={len(code_lines)} (low)"
        else:
            result.fields_missing = task.expected_fields[:]
            result.error = "no_python_code_extracted"

    return result


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------


def _ascii_bar(value: float, max_value: float, width: int = 30) -> str:
    if max_value <= 0:
        return ""
    filled = int((value / max_value) * width)
    return "#" * min(filled, width) + "." * (width - min(filled, width))


def generate_markdown_report(
    results: Dict[str, Dict], all_models: List[str], config: BenchmarkConfig,
) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    task_count = len(set(r["task_id"] for r in results.values()))

    lines = [
        f"# LLM Coding Benchmark v2 -- {ts}",
        "",
        f"**Run ID**: {config.run_id}",
        f"**Models**: {', '.join(all_models)}",
        f"**Mode**: {'no-think' if config.no_think else 'thinking (default)'}",
        f"**Token limits**: num_predict={NUM_PREDICT}, num_ctx={NUM_CTX}",
        f"**Tasks**: {task_count} (4 SEOcrawler + 4 bash-to-python)",
        f"**Total runs**: {len(results)}",
        "", "---", "",
    ]

    # Summary table
    lines.extend([
        "## Summary", "",
        "| Model | Success | Avg Speed | Format OK | Completeness | Avg Time |",
        "|-------|---------|-----------|-----------|--------------|----------|",
    ])

    for model in all_models:
        mr = [r for r in results.values() if r["model"] == model]
        if not mr:
            continue
        n = len(mr)
        successes = sum(1 for r in mr if r["success"])
        avg_tps = sum(r["tokens_per_second"] for r in mr) / n
        fmt_ok = sum(1 for r in mr if r["format_compliant"])
        avg_comp = sum(r["completeness_score"] for r in mr) / n
        avg_dur = sum(r["duration_seconds"] for r in mr) / n
        lines.append(
            f"| {model} | {successes}/{n} | {avg_tps:.1f} tok/s | "
            f"{fmt_ok}/{n} | {avg_comp:.0%} | {avg_dur:.1f}s |")

    lines.extend(["", "---", ""])

    # Speed chart
    lines.extend(["## Speed Comparison", ""])
    model_avg_tps = {}
    for model in all_models:
        mr = [r for r in results.values()
              if r["model"] == model and r["success"]]
        model_avg_tps[model] = (
            sum(r["tokens_per_second"] for r in mr) / len(mr) if mr else 0.0)

    max_tps = max(model_avg_tps.values(), default=0.1) or 0.1
    lines.append("```")
    nw = max((len(m) for m in all_models), default=15)
    for model in all_models:
        tps = model_avg_tps.get(model, 0)
        bar = _ascii_bar(tps, max_tps, 40)
        lines.append(f"  {model:<{nw}} |{bar}| {tps:.1f} tok/s")
    lines.extend(["```", "", "---", ""])

    # Category breakdown
    lines.extend([
        "## Category Breakdown", "",
        "| Category | Model | Format | Completeness | Speed |",
        "|----------|-------|--------|-------------|-------|",
    ])
    categories = ["crawler", "api", "storage", "domain", "bash_migration"]
    for cat in categories:
        for model in all_models:
            cr = [r for r in results.values()
                  if r["model"] == model and r["category"] == cat]
            if not cr:
                continue
            n = len(cr)
            fmt = sum(1 for r in cr if r["format_compliant"])
            comp = sum(r["completeness_score"] for r in cr) / n
            spd = sum(r["tokens_per_second"] for r in cr) / n
            lines.append(
                f"| {cat} | {model} | {fmt}/{n} | {comp:.0%} | {spd:.1f} |")
    lines.extend(["", "---", ""])

    # Per-task details
    lines.extend(["## Per-Task Results", ""])
    task_ids_seen = []
    for r in results.values():
        if r["task_id"] not in task_ids_seen:
            task_ids_seen.append(r["task_id"])

    for task_id in task_ids_seen:
        task_results = {
            r["model"]: r for r in results.values() if r["task_id"] == task_id}
        if not task_results:
            continue
        first = next(iter(task_results.values()))
        lines.extend([
            f"### {first['task_name']} (`{first['category']}`)", "",
            "| Metric | " + " | ".join(all_models) + " |",
            "|--------|" + "|".join("------" for _ in all_models) + "|",
        ])

        # Rows: Duration, Speed, Format, Completeness, Code lines, Missing
        for label, key in [("Duration", "duration_seconds"),
                           ("Tokens/sec", "tokens_per_second")]:
            vals = []
            for m in all_models:
                r = task_results.get(m)
                if r and r["success"]:
                    v = r[key]
                    vals.append(f"{v:.1f}s" if "dur" in key else f"{v:.1f}")
                else:
                    vals.append("FAIL" if r else "-")
            lines.append(f"| {label} | " + " | ".join(vals) + " |")

        vals = []
        for m in all_models:
            r = task_results.get(m)
            vals.append("Yes" if r and r["format_compliant"] else "No")
        lines.append("| Syntax OK | " + " | ".join(vals) + " |")

        vals = []
        for m in all_models:
            r = task_results.get(m)
            c = r["completeness_score"] if r else 0
            vals.append(f"{c:.0%}")
        lines.append("| Completeness | " + " | ".join(vals) + " |")

        vals = []
        for m in all_models:
            r = task_results.get(m)
            missing = r.get("fields_missing", []) if r else []
            vals.append(", ".join(missing[:3]) if missing else "none")
        lines.append("| Missing | " + " | ".join(vals) + " |")
        lines.append("")

        # Output previews
        for m in all_models:
            r = task_results.get(m)
            if r and r["success"]:
                preview = r["output"][:800].replace("\n", "\n> ")
                lines.extend([
                    f"<details><summary>{m} output (preview)</summary>",
                    "", f"> {preview}", "", "</details>", "",
                ])
        lines.extend(["---", ""])

    # Winner per category
    lines.extend([
        "## Recommended Models", "",
        "| Category | Best Model | Score | Reasoning |",
        "|----------|-----------|-------|-----------|",
    ])
    for cat in categories:
        cat_results = [r for r in results.values() if r["category"] == cat]
        if not cat_results:
            continue
        model_agg: Dict[str, List[float]] = {}
        for r in cat_results:
            m = r["model"]
            if m not in model_agg:
                model_agg[m] = []
            score = r["completeness_score"]
            if r["format_compliant"]:
                score += 0.3
            if r["success"]:
                score += 0.2
            model_agg[m].append(score)

        best = max(model_agg,
                   key=lambda m: sum(model_agg[m]) / len(model_agg[m]))
        avg = sum(model_agg[best]) / len(model_agg[best])
        br = [r for r in cat_results if r["model"] == best]
        tps = sum(r["tokens_per_second"] for r in br) / len(br) if br else 0
        fmt = sum(1 for r in br if r["format_compliant"]) / len(br) if br else 0
        lines.append(
            f"| {cat} | **{best}** | {avg:.2f} | "
            f"{tps:.0f} tok/s, {fmt:.0%} syntax valid |")

    lines.extend(["", "---", ""])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Progress / resume
# ---------------------------------------------------------------------------


def load_progress() -> Dict[str, Any]:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_progress(progress: Dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(
        json.dumps(progress, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main benchmark runner
# ---------------------------------------------------------------------------


def run_benchmark(config: BenchmarkConfig, tasks: List[Task],
                  resume: bool = False, no_think: bool = False,
                  timeout: int = 0) -> Dict[str, Dict]:
    progress = load_progress() if resume else {}
    all_models = config.models[:]
    total_runs = len(tasks) * len(all_models)
    run_idx = 0

    for task in tasks:
        for model in all_models:
            run_idx += 1
            key = f"{task.id}__{model}"

            if key in progress:
                print(f"  [{run_idx}/{total_runs}] SKIP "
                      f"{task.name} x {model} (cached)")
                continue

            print(f"  [{run_idx}/{total_runs}] {task.name} x {model}...",
                  end=" ", flush=True)

            result = run_ollama(model, task.prompt, no_think=no_think,
                                timeout=timeout)
            result.task_id = task.id
            result.task_name = task.name
            result.category = task.category
            result = score_result(result, task)

            if result.success:
                print(f"OK ({result.duration_seconds:.1f}s, "
                      f"{result.tokens_per_second:.1f} tok/s, "
                      f"fmt={'Y' if result.format_compliant else 'N'}, "
                      f"comp={result.completeness_score:.0%})")
            else:
                print(f"FAIL: {result.error}")

            entry = {
                "task_id": result.task_id,
                "task_name": result.task_name,
                "category": result.category,
                "model": result.model,
                "success": result.success,
                "duration_seconds": result.duration_seconds,
                "tokens_generated": result.tokens_generated,
                "tokens_per_second": result.tokens_per_second,
                "output": result.output,
                "output_length": result.output_length,
                "format_compliant": result.format_compliant,
                "fields_present": result.fields_present,
                "fields_missing": result.fields_missing,
                "completeness_score": result.completeness_score,
                "quality": result.quality,
                "error": result.error,
                "timestamp": result.timestamp,
            }
            progress[key] = entry
            save_progress(progress)

    return progress


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _apply_token_overrides(num_predict: int, num_ctx: int) -> None:
    global NUM_PREDICT, NUM_CTX
    if num_predict > 0:
        NUM_PREDICT = num_predict
    if num_ctx > 0:
        NUM_CTX = num_ctx


def main() -> None:
    parser = argparse.ArgumentParser(
        description="VNX LLM Coding Benchmark v2 -- real-world SEOcrawler & bash migration tasks"
    )
    parser.add_argument(
        "--models", type=str, default=None,
        help=f"Comma-separated model names (default: {', '.join(DEFAULT_MODELS)})",
    )
    parser.add_argument(
        "--tasks", type=str, default=None,
        help="Comma-separated task IDs to run",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from benchmark_progress.json checkpoint",
    )
    parser.add_argument(
        "--no-think", action="store_true",
        help="Prefix prompts with /no_think for thinking models",
    )
    parser.add_argument(
        "--timeout", type=int, default=0,
        help=f"Override timeout per task in seconds (default: {TIMEOUT_LOCAL}s)",
    )
    parser.add_argument(
        "--num-predict", type=int, default=0,
        help=f"Override max output tokens (default: {NUM_PREDICT})",
    )
    parser.add_argument(
        "--num-ctx", type=int, default=0,
        help=f"Override context window (default: {NUM_CTX})",
    )
    parser.add_argument(
        "--seocrawler-only", action="store_true",
        help="Run only SEOcrawler tasks (4 tasks)",
    )
    parser.add_argument(
        "--migration-only", action="store_true",
        help="Run only bash-to-python migration tasks (4 tasks)",
    )
    args = parser.parse_args()

    _apply_token_overrides(args.num_predict, args.num_ctx)

    print("=" * 64)
    print("  VNX LLM Coding Benchmark v2 (Real-World Tasks)")
    print("=" * 64)

    if not check_ollama_available():
        print(f"\nERROR: Ollama not available at {OLLAMA_URL}")
        print("Start with: ollama serve")
        sys.exit(1)

    if args.models:
        models = [m.strip() for m in args.models.split(",")]
    else:
        available = list_ollama_models()
        models = [m for m in DEFAULT_MODELS if m in available]
        if not models:
            print(f"\nNo default models found. Available: {available}")
            print(f"Expected: {DEFAULT_MODELS}")
            sys.exit(1)

    # Select tasks
    if args.tasks:
        task_ids = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in ALL_TASKS if t.id in task_ids]
    elif args.seocrawler_only:
        tasks = SEOCRAWLER_TASKS[:]
    elif args.migration_only:
        tasks = BASH_TO_PYTHON_TASKS[:]
    else:
        tasks = ALL_TASKS[:]

    if not tasks:
        print("ERROR: No valid tasks selected")
        sys.exit(1)

    print(f"\nModels:  {', '.join(models)}")
    print(f"Tasks:   {', '.join(t.id for t in tasks)} ({len(tasks)} total)")
    print(f"Tokens:  num_predict={NUM_PREDICT}, num_ctx={NUM_CTX}")
    est_minutes = len(tasks) * len(models) * 5
    print(f"Est:     ~{est_minutes:.0f} minutes")
    if args.resume:
        print(f"Resume:  from {PROGRESS_FILE}")
    print()

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    config = BenchmarkConfig(
        models=models,
        task_ids=[t.id for t in tasks],
        include_claude=False,
        run_id=run_id,
        started_at=datetime.now().isoformat(),
        no_think=args.no_think,
    )

    results = run_benchmark(config, tasks, resume=args.resume,
                            no_think=args.no_think, timeout=args.timeout)

    if not results:
        print("No results collected.")
        sys.exit(1)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    report_md = generate_markdown_report(results, models, config)
    md_path = REPORT_DIR / f"coding_v2_{run_id}.md"
    md_path.write_text(report_md, encoding="utf-8")

    json_path = REPORT_DIR / f"coding_v2_{run_id}.json"
    raw_data = {"config": asdict(config), "results": results}
    json_path.write_text(
        json.dumps(raw_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print("=" * 64)
    print("  RESULTS")
    print("=" * 64)

    for model in models:
        mr = [r for r in results.values() if r["model"] == model]
        if not mr:
            continue
        successes = sum(1 for r in mr if r["success"])
        avg_dur = sum(r["duration_seconds"] for r in mr) / len(mr)
        avg_tps = sum(r["tokens_per_second"] for r in mr) / len(mr)
        fmt_ok = sum(1 for r in mr if r["format_compliant"])
        avg_comp = sum(r["completeness_score"] for r in mr) / len(mr)
        print(f"  {model:<25} {successes}/{len(mr)} ok  "
              f"{avg_dur:6.1f}s avg  {avg_tps:6.1f} tok/s  "
              f"fmt {fmt_ok}/{len(mr)}  comp {avg_comp:.0%}")

    print()
    print(f"  Report:   {md_path}")
    print(f"  Raw data: {json_path}")
    print(f"  Progress: {PROGRESS_FILE}")
    print()


if __name__ == "__main__":
    main()
