#!/usr/bin/env python3
"""
VNX LLM Benchmark -- Side-by-Side Model Comparison

Runs identical SEOcrawler-domain prompts through multiple models (Ollama local
+ Claude CLI) and produces a structured comparison report with automatic
scoring on speed, format compliance, and completeness.

Usage:
  # Quick test (3 tasks, ~15 min):
  python3 scripts/llm_benchmark.py --quick --include-claude

  # Full benchmark (7 tasks x N models, ~90 min):
  python3 scripts/llm_benchmark.py --include-claude

  # Specific models only:
  python3 scripts/llm_benchmark.py --models qwen3.5:9b,qwen2.5-coder:14b

  # Resume after crash:
  python3 scripts/llm_benchmark.py --resume
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Path setup -- no hardcoded user paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
VNX_BASE = SCRIPT_DIR.parent  # VNX dist root
REPORT_DIR = VNX_BASE / "reports" / "benchmarks"
PROGRESS_FILE = REPORT_DIR / "benchmark_progress.json"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

DEFAULT_LOCAL_MODELS = [
    "qwen3.5:9b",
    "qwen2.5-coder:14b",
    "qwen3.5:27b",
    "qwen3.5:35b-a3b",
]

TIMEOUT_LOCAL = 300   # seconds per task for local models (default)
TIMEOUT_CLAUDE = 120  # seconds per task for Claude (increased for marketing tasks)

CLAUDE_MODELS = {
    "claude-haiku": "haiku",
    "claude-sonnet": "sonnet",
}
NUM_CTX = 8192        # context window (input + output tokens combined) — overridable via --num-ctx
NUM_PREDICT = 4096    # max output tokens — overridable via --num-predict

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """A single benchmark task definition."""

    id: str
    category: str
    name: str
    prompt: str
    expected_format: str  # "json", "code", "text"
    expected_fields: List[str]  # top-level JSON keys or markers to check


@dataclass
class Result:
    """Measurement for one task x model combination."""

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
    completeness_score: float  # 0.0 - 1.0
    quality: Optional[int]  # null -- reserved for manual 1-5 scoring
    error: Optional[str]
    timestamp: str


@dataclass
class BenchmarkConfig:
    """Runtime configuration for a benchmark session."""

    models: List[str]
    task_ids: List[str]
    include_claude: bool
    run_id: str
    started_at: str
    no_think: bool = False


# ---------------------------------------------------------------------------
# Task definitions -- all prompts inline with realistic SEOcrawler data
# ---------------------------------------------------------------------------

SAMPLE_SESSION_SUMMARY = """\
Session f8a3c1d2e4b6 | Terminal: T-BACKEND | Date: 2026-03-04
  Duration: 47 min | Tokens: 128,432 in / 34,891 out
  Tool calls: 89 (Read: 31, Edit: 24, Bash: 19, Grep: 12, Write: 3)
  Primary activity: bug-fix (PDF generation crash on int seo_score)
  Error recovery: yes (2 attempts) | Context reset: no
  Files touched: 7 | Tests run: 14 passed, 0 failed

Session a2e7b9c0f153 | Terminal: T-MANAGER | Date: 2026-03-04
  Duration: 23 min | Tokens: 64,210 in / 18,540 out
  Tool calls: 42 (Read: 18, Bash: 14, Grep: 8, Edit: 2)
  Primary activity: intelligence-analysis (nightly digest generation)
  Error recovery: no | Context reset: no
  Files touched: 3 | Tests run: 0

Session c5d9e1f3a7b2 | Terminal: T-BACKEND | Date: 2026-03-03
  Duration: 112 min | Tokens: 312,004 in / 87,650 out
  Tool calls: 214 (Read: 72, Edit: 58, Bash: 41, Grep: 28, Write: 15)
  Primary activity: feature-impl (email delivery + nurture scheduler)
  Error recovery: yes (5 attempts) | Context reset: yes (1)
  Files touched: 18 | Tests run: 31 passed, 3 failed

Session d7f2a8b4c6e0 | Terminal: T-ARCHITECT | Date: 2026-03-03
  Duration: 35 min | Tokens: 89,100 in / 22,300 out
  Tool calls: 56 (Read: 28, Bash: 15, Grep: 10, Edit: 3)
  Primary activity: code-review (Mollie webhook idempotency)
  Error recovery: no | Context reset: no
  Files touched: 5 | Tests run: 8 passed, 0 failed

Session e1a3c5d7f9b2 | Terminal: T-BACKEND | Date: 2026-03-02
  Duration: 68 min | Tokens: 198,700 in / 51,200 out
  Tool calls: 147 (Read: 48, Edit: 42, Bash: 32, Grep: 18, Write: 7)
  Primary activity: refactoring (storage optimizer + cache invalidation)
  Error recovery: yes (3 attempts) | Context reset: no
  Files touched: 12 | Tests run: 22 passed, 1 failed"""

SAMPLE_FUNCTION = """\
class StorageClient:
    def __init__(self, supabase_url: str, supabase_key: str, pool_size: int = 5):
        self._url = supabase_url
        self._key = supabase_key
        self._pool_size = pool_size
        self._client = None
        self._retry_count = 3
        self._retry_delay = 0.5

    async def upsert_scan_results(
        self, domain: str, scan_id: str, pages: list[dict],
        metadata: dict | None = None, ttl_hours: int = 168
    ) -> dict:
        if not domain or not scan_id:
            raise ValueError("domain and scan_id are required")
        if len(pages) > 50:
            raise ValueError(f"Maximum 50 pages per upsert, got {len(pages)}")

        deduped = {p["url"]: p for p in pages if p.get("url")}
        now = datetime.utcnow().isoformat()
        records = []
        for url, page in deduped.items():
            records.append({
                "domain": domain,
                "scan_id": scan_id,
                "url": url,
                "title": page.get("title", ""),
                "status_code": page.get("status_code", 0),
                "seo_score": page.get("seo_score"),
                "issues": page.get("issues", []),
                "metadata": metadata or {},
                "crawled_at": now,
                "expires_at": (datetime.utcnow() + timedelta(hours=ttl_hours)).isoformat(),
            })

        for attempt in range(1, self._retry_count + 1):
            try:
                result = await self._client.table("scan_pages").upsert(
                    records, on_conflict="domain,url,scan_id"
                ).execute()
                return {
                    "inserted": len(result.data),
                    "duplicates_merged": len(pages) - len(deduped),
                    "scan_id": scan_id,
                }
            except Exception as exc:
                if attempt == self._retry_count:
                    raise
                await asyncio.sleep(self._retry_delay * attempt)"""

SAMPLE_WEBHOOK_FUNCTION = """\
class MollieWebhookHandler:
    \"\"\"Handles Mollie payment webhook callbacks.\"\"\"

    def __init__(self, mollie_client, supabase_client, email_service):
        self.mollie = mollie_client
        self.db = supabase_client
        self.email = email_service

    async def handle_webhook(self, payment_id: str) -> dict:
        payment = await self.mollie.payments.get(payment_id)
        order = await self.db.table("orders").select("*").eq(
            "mollie_payment_id", payment_id
        ).single().execute()

        if not order.data:
            raise ValueError(f"No order found for payment {payment_id}")

        current_status = order.data["status"]
        new_status = payment.status  # "paid", "failed", "expired", "canceled"

        if current_status == new_status:
            return {"action": "no_change", "status": current_status}

        await self.db.table("orders").update({
            "status": new_status,
            "paid_at": payment.paid_at if new_status == "paid" else None,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", order.data["id"]).execute()

        if new_status == "paid" and current_status != "paid":
            token = secrets.token_urlsafe(32)
            await self.db.table("download_tokens").insert({
                "order_id": order.data["id"],
                "token": token,
                "expires_at": (datetime.utcnow() + timedelta(hours=72)).isoformat(),
            }).execute()
            await self.email.send_download_link(
                to=order.data["email"],
                download_url=f"/api/download/{token}",
                order_ref=order.data["reference"],
            )

        return {"action": "updated", "old_status": current_status, "new_status": new_status}"""

SAMPLE_PRINT_CODE = """\
# From storage_client.py
print(f"Connecting to Supabase: {url}")
print(f"Query took {elapsed:.2f}s for {table}")
print(f"ERROR: Failed to upsert record: {e}")
print(f"Warning: duplicate key for {domain}, skipping")
print(f"  -> Inserted {count} rows into {table}")
print(f"Cache hit for {key}")
print(f"CRITICAL: Database connection pool exhausted")
print(f"Retrying query ({attempt}/3)...")
print(f"  Debug: raw response = {resp[:200]}")
print(f"Storage optimization complete: {saved_mb:.1f}MB freed")"""

SAMPLE_HTML = """\
<footer class="site-footer">
  <div class="contact-info">
    <p>VNX Digital B.V.</p>
    <p>KvK: 87654321</p>
    <p>BTW: NL123456789B01</p>
    <p>Adres: Keizersgracht 123, 1015 CJ Amsterdam</p>
    <p>Tel: +31 (0)20 123 4567</p>
    <p>Email: info@vnxdigital.nl</p>
  </div>
  <div class="legal">
    <a href="/algemene-voorwaarden">Algemene Voorwaarden</a>
    <a href="/privacy">Privacybeleid</a>
    <a href="https://www.kvk.nl/zoeken/?source=all&q=87654321">KvK Registratie</a>
  </div>
  <div class="social">
    <a href="https://linkedin.com/company/vnx-digital">LinkedIn</a>
  </div>
</footer>"""

SAMPLE_STACK_TRACE = """\
Traceback (most recent call last):
  File "/app/src/services/crawl_orchestrator.py", line 287, in _crawl_page
    result = await browser_pool.execute(url, timeout=30000)
  File "/app/src/services/browser_pool.py", line 142, in execute
    page = await context.new_page()
  File "/app/.venv/lib/python3.11/site-packages/playwright/async_api/_impl.py", line 891, in new_page
    return await self._inner_new_page()
playwright._impl._errors.TimeoutError: Timeout 30000ms exceeded.
=========================== logs ===========================
navigating to "https://www.example-sme.nl/diensten", waiting until "load"
===============================================================

Context: Crawling page 3 of 5 for domain example-sme.nl
Browser pool: 3/3 slots occupied, memory usage 1.8GB
Previous 2 pages completed successfully (avg 12s each)
This page has heavy JavaScript (React SPA with client-side rendering)"""

SAMPLE_DIGEST_STATS = """\
Periode: 2026-03-04 (nachtelijke analyse)
Sessies geanalyseerd: 5
Totaal tokens verbruikt: 792,446 (in) / 214,581 (uit)
Gemiddelde sessieduur: 57 minuten
Tool-aanroepen: 548 totaal (Read: 197, Edit: 129, Bash: 121, Grep: 76, Write: 25)
Activiteiten: bug-fix (2), feature-impl (1), intelligence-analysis (1), code-review (1)
Error recovery events: 10 (3 sessies)
Context resets: 1
Tests uitgevoerd: 75 passed, 4 failed (94.9% success rate)
Bestanden gewijzigd: 45 unieke bestanden
Meest actieve terminal: T-BACKEND (3 sessies, 227 min)"""

BENCHMARK_TASKS: List[Task] = [
    Task(
        id="session_analysis",
        category="intelligence",
        name="Deep Session Analysis",
        prompt=f"""You are a development session analyst. Analyze these Claude Code session summaries and return structured JSON.

For each session, evaluate efficiency. Then provide an overall analysis.

## Session Data

{SAMPLE_SESSION_SUMMARY}

## Required Output

Return valid JSON with exactly this structure:
{{
  "efficiency_score": <1-10 integer>,
  "primary_activity": "<most common activity across sessions>",
  "bottlenecks": ["<list of identified bottlenecks>"],
  "recommendations": ["<list of actionable recommendations>"],
  "token_efficiency_rating": "<low|medium|high>"
}}

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=[
            "efficiency_score",
            "primary_activity",
            "bottlenecks",
            "recommendations",
            "token_efficiency_rating",
        ],
    ),
    Task(
        id="docstring_generation",
        category="code_quality",
        name="Docstring Generation",
        prompt=f"""Generate Google-style Python docstrings for the class and its methods below.

```python
{SAMPLE_FUNCTION}
```

Return valid JSON with this structure:
{{
  "docstring": "<full docstring for upsert_scan_results method with Args, Returns, Raises sections>"
}}

The docstring must include:
- One-line summary
- Extended description (1-2 sentences)
- Args: each parameter with type and description
- Returns: description of the return dict
- Raises: ValueError conditions

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["docstring"],
    ),
    Task(
        id="test_generation",
        category="testing",
        name="Test Case Generation",
        prompt=f"""Generate pytest test cases for this Mollie webhook handler. Include:
- Happy path: payment transitions to "paid", download token created, email sent
- Error case: payment_id not found in database
- Error case: Mollie API call fails
- Edge case: duplicate webhook (status unchanged, no side effects)
- Edge case: transition from "paid" back to "failed" (no duplicate download token)

Use pytest fixtures, unittest.mock.AsyncMock for dependencies, and pytest.mark.asyncio.

```python
{SAMPLE_WEBHOOK_FUNCTION}
```

Return ONLY a complete Python test file. Start with imports, then fixtures, then test functions.
Wrap the entire code in ```python ... ``` fences.""",
        expected_format="code",
        expected_fields=[
            "test_happy_path",
            "test_not_found",
            "test_api_failure",
            "test_duplicate",
            "test_",
        ],
    ),
    Task(
        id="error_classification",
        category="intelligence",
        name="Error Classification",
        prompt=f"""Classify this error from a Python web crawler. Analyze the stack trace and context.

## Stack Trace
```
{SAMPLE_STACK_TRACE}
```

Return valid JSON with exactly this structure:
{{
  "error_type": "<timeout|memory|connection|parsing|schema|permission>",
  "severity": "<critical|high|medium|low>",
  "root_cause": "<concise root cause analysis>",
  "suggested_fix": "<specific actionable fix>",
  "prevention_rule": "<rule to prevent recurrence>",
  "automated_fix_possible": true or false
}}

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=[
            "error_type",
            "severity",
            "root_cause",
            "suggested_fix",
            "prevention_rule",
            "automated_fix_possible",
        ],
    ),
    Task(
        id="print_to_logger",
        category="code_quality",
        name="Print-to-Logger Conversion",
        prompt=f"""Convert these print() calls to Python structured logging. Determine the correct log level from context.

```python
{SAMPLE_PRINT_CODE}
```

For each line, return the replacement with the correct log level (debug/info/warning/error/critical).

Return valid JSON with this structure:
{{
  "conversions": [
    {{
      "original": "print(...)",
      "replacement": "logger.info(...)",
      "log_level": "info"
    }}
  ]
}}

There should be exactly 10 conversions (one per print statement).
Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["conversions"],
    ),
    Task(
        id="dutch_extraction",
        category="domain",
        name="Dutch Data Extraction",
        prompt=f"""Extract Dutch business information from this HTML snippet. Validate KvK (8 digits) and BTW (NL + 9 digits + B + 2 digits) formats.

```html
{SAMPLE_HTML}
```

Return valid JSON with this structure:
{{
  "company": {{ "name": "...", "legal_form": "..." }},
  "kvk": {{ "number": "...", "valid": true/false, "format_check": "..." }},
  "btw": {{ "number": "...", "valid": true/false, "format_check": "..." }},
  "address": {{ "street": "...", "postal_code": "...", "city": "..." }},
  "contact": {{ "phone": "...", "email": "...", "phone_formatted": "+31..." }},
  "legal_pages": ["..."],
  "social_links": ["..."]
}}

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=[
            "company",
            "kvk",
            "btw",
            "address",
            "contact",
            "legal_pages",
            "social_links",
        ],
    ),
    Task(
        id="digest_narrative",
        category="intelligence",
        name="Digest Narrative (NL)",
        prompt=f"""Je bent een VNX orchestration analyst. Schrijf een professionele Nederlandse samenvatting (precies 150 woorden) van deze nachtelijke sessie-statistieken voor een digest-email.

## Statistieken

{SAMPLE_DIGEST_STATS}

Eisen:
- Precies 150 woorden (mag 10% afwijken)
- Professioneel Nederlands, geen opsommingstekens
- Vloeiende zinnen, zakelijke toon
- Noem de belangrijkste patronen, risico's en aanbevelingen
- Sluit af met een concrete actie voor morgen

Geef ALLEEN de Nederlandse tekst. Geen titel, geen markdown.""",
        expected_format="text",
        expected_fields=[],  # text format -- no field checks
    ),

    # Task 8: Bash-to-Python Migration (real VNX script)
    Task(
        id="bash_to_python",
        name="Bash-to-Python Migration",
        category="code_migration",
        prompt="""\
Convert this VNX orchestration bash script to production-ready Python 3.11+.
The Python version must:
1. Be functionally identical (same file paths, same logic, same monitoring behavior)
2. Use pathlib for all paths
3. Use proper logging instead of echo
4. Use json module instead of jq
5. Use subprocess for external commands
6. Use signal handlers for cleanup
7. Include type hints
8. Use the existing vnx_paths.py module (import from lib/vnx_paths)
9. Include a proper __main__ guard
10. Handle all edge cases the bash version handles

Here is the bash script (dispatch_ack_watcher.sh):

```bash
#!/bin/bash

# Dispatch ACK Watcher - SHADOW MODE
# Monitors dispatches moving from queue to completed
# Automatically starts heartbeat ACK monitoring for each dispatch

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/vnx_paths.sh"
source "$SCRIPT_DIR/lib/process_lifecycle.sh"
VNX_DIR="$VNX_HOME"
STATE_DIR="$VNX_STATE_DIR"
QUEUE_DIR="$VNX_DISPATCH_DIR/queue"
COMPLETED_DIR="$VNX_DISPATCH_DIR/completed"
LOG_FILE="$VNX_LOGS_DIR/dispatch_ack_watcher.log"
PID_FILE="$VNX_PIDS_DIR/dispatch_ack_watcher.pid"
MONITOR_SCRIPT="$VNX_HOME/scripts/heartbeat_ack_monitor.py"
PROC_NAME="dispatch_ack_watcher"

TRACKING_FILE="$STATE_DIR/ack_tracking.json"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE")" "$STATE_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

init_tracking() {
    if [ ! -f "$TRACKING_FILE" ]; then
        echo "{}" > "$TRACKING_FILE"
    fi
}

is_tracked() {
    local dispatch_id="$1"
    jq -e ".\\"$dispatch_id\\"" "$TRACKING_FILE" > /dev/null 2>&1
    return $?
}

add_tracking() {
    local dispatch_id="$1"
    local terminal="$2"
    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    jq ". + {\\"$dispatch_id\\": {\\"terminal\\": \\"$terminal\\", \\"started\\": \\"$timestamp\\", \\"status\\": \\"monitoring\\"}}" "$TRACKING_FILE" > "$TRACKING_FILE.tmp"
    mv "$TRACKING_FILE.tmp" "$TRACKING_FILE"
}

get_dispatch_terminal() {
    local dispatch_id="$1"
    local log_entry=$(grep "$dispatch_id" "$VNX_LOGS_DIR/dispatcher.log" 2>/dev/null | tail -1)
    if [[ "$log_entry" =~ terminal[[:space:]]([T][0-9]) ]]; then
        echo "${BASH_REMATCH[1]}"
    elif [[ "$log_entry" =~ vnx-terminal:([T][0-9]) ]]; then
        echo "${BASH_REMATCH[1]}"
    else
        local dispatch_file="$COMPLETED_DIR/${dispatch_id}.md"
        if [ -f "$dispatch_file" ]; then
            local target=$(grep -o 'TARGET:[A-C]' "$dispatch_file" | head -1 | cut -d: -f2)
            case "$target" in
                A) echo "T1" ;; B) echo "T2" ;; C) echo "T3" ;; *) echo "unknown" ;;
            esac
        else
            echo "unknown"
        fi
    fi
}

start_ack_monitoring() {
    local dispatch_id="$1"
    local terminal="$2"
    local dispatch_file="$COMPLETED_DIR/${dispatch_id}.md"
    local task_id="$dispatch_id"
    if [ -f "$dispatch_file" ]; then
        local extracted_task=$(grep -i "task.*id\\|task:" "$dispatch_file" 2>/dev/null | head -1 | sed 's/.*:\\s*//')
        if [ ! -z "$extracted_task" ]; then
            task_id="$extracted_task"
        fi
    fi
    log "Starting ACK monitor: dispatch=$dispatch_id, terminal=$terminal, task=$task_id"
    RECEIPT_FILE="$STATE_DIR/t0_receipts.ndjson" python3 "$MONITOR_SCRIPT" --stdin <<EOF &
{"dispatch_id": "$dispatch_id", "terminal": "$terminal", "task_id": "$task_id", "sent_time": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"}
EOF
    local monitor_pid=$!
    log "ACK monitor started with PID $monitor_pid for $dispatch_id"
    add_tracking "$dispatch_id" "$terminal"
}

monitor_completed() {
    log "Monitoring completed directory for new dispatches..."
    while true; do
        for dispatch_file in "$COMPLETED_DIR"/*.md; do
            [ -f "$dispatch_file" ] || continue
            local dispatch_id=$(basename "$dispatch_file" .md)
            if ! is_tracked "$dispatch_id"; then
                log "New completed dispatch detected: $dispatch_id"
                local terminal=$(get_dispatch_terminal "$dispatch_id")
                if [ "$terminal" != "unknown" ]; then
                    start_ack_monitoring "$dispatch_id" "$terminal"
                else
                    log "WARNING: Could not determine terminal for $dispatch_id"
                fi
            fi
        done
        sleep 2
    done
}

cleanup() {
    log "Dispatch ACK watcher shutting down..."
    rm -f "$PID_FILE" "${PID_FILE}.fingerprint"
    exit 0
}

main() {
    echo $$ > "$PID_FILE"
    trap cleanup SIGTERM SIGINT
    log "=== Dispatch ACK Watcher Starting ==="
    log "PID: $$"
    log "Monitoring: $COMPLETED_DIR"
    init_tracking
    monitor_completed
}

if [ "${BASH_SOURCE[0]}" == "${0}" ]; then
    main "$@"
fi
```

Return ONLY the complete Python file. No explanations, no markdown fences, just the Python code.""",
        expected_format="code",
        expected_fields=["pathlib", "logging", "json", "subprocess", "signal", "__main__"],
    ),
]

QUICK_TASK_IDS = {"session_analysis", "test_generation", "error_classification"}

# ---------------------------------------------------------------------------
# Marketing benchmark tasks -- Dutch MKB scenarios
# ---------------------------------------------------------------------------

MARKETING_TASKS: List[Task] = [
    Task(
        id="linkedin_post",
        category="social_media",
        name="LinkedIn Post (NL)",
        prompt="""\
Je bent een social media marketeer voor een Nederlands MKB-bedrijf.

Bedrijf: Loodgietersbedrijf Van der Berg, Utrecht
Specialisatie: Particulier en zakelijk loodgieterswerk, 15 jaar ervaring
Seizoen: November (winter nadert)
Doel: Volgers informeren over leidingen winterklaar maken

Schrijf een LinkedIn post met praktische wintertips voor huiseigenaren.
De post moet:
- 150-200 woorden in het Nederlands
- Professioneel maar toegankelijk
- Praktische tips bevatten
- Eindigen met een call-to-action

Return valid JSON with exactly this structure:
{
  "post_text": "<150-200 woorden Nederlandse LinkedIn post>",
  "hashtags": ["<5-8 relevante Nederlandse hashtags>"],
  "call_to_action": "<korte CTA tekst>"
}

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["post_text", "hashtags", "call_to_action"],
    ),
    Task(
        id="google_ads",
        category="advertising",
        name="Google Ads Copy",
        prompt="""\
Je bent een Google Ads specialist voor Nederlandse MKB-bedrijven.

Klant: Bakker & De Vries Accountants, Amsterdam
Zoekwoord: "boekhouder MKB Amsterdam"
Diensten: Jaarrekening, BTW-aangifte, salarisadministratie, fiscaal advies
USP: Persoonlijke aanpak, 20+ jaar ervaring, gratis kennismakingsgesprek

Maak Google Ads koppen en beschrijvingen. Let op de strikte tekenlimiet!
Nederlandse woorden zijn vaak langer dan Engelse -- houd hier rekening mee.

Return valid JSON with exactly this structure:
{
  "headlines": ["<headline 1 max 30 tekens>", "<headline 2 max 30 tekens>", "<headline 3 max 30 tekens>", "<headline 4 max 30 tekens>", "<headline 5 max 30 tekens>"],
  "descriptions": ["<description 1 max 90 tekens>", "<description 2 max 90 tekens>", "<description 3 max 90 tekens>"],
  "target_keyword": "boekhouder MKB Amsterdam"
}

BELANGRIJK: Headlines MAXIMAAL 30 tekens. Descriptions MAXIMAAL 90 tekens. Tel zorgvuldig!

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["headlines", "descriptions", "target_keyword"],
    ),
    Task(
        id="seo_meta_tags",
        category="seo",
        name="SEO Meta Tags",
        prompt="""\
Je bent een SEO specialist. Schrijf meta tags voor een tandartspraktijk website.

Bedrijf: Tandartspraktijk Smile Rotterdam
Locatie: Rotterdam-Zuid
Diensten: Algemene tandheelkunde, implantaten, orthodontie, tandsteen verwijderen
USP: Angstvrije behandeling, avondspreken, directe vergoeding bij alle zorgverzekeraars

Schrijf meta tags voor deze 3 pagina's:
1. Homepage
2. Implantaten dienstpagina
3. Orthodontie (beugels) dienstpagina

Return valid JSON with exactly this structure:
{
  "pages": [
    {
      "page": "homepage",
      "meta_title": "<max 60 tekens>",
      "meta_description": "<max 155 tekens>",
      "primary_keyword": "<hoofd zoekwoord>",
      "secondary_keywords": ["<2-3 extra zoekwoorden>"]
    },
    {
      "page": "implantaten",
      "meta_title": "<max 60 tekens>",
      "meta_description": "<max 155 tekens>",
      "primary_keyword": "<hoofd zoekwoord>",
      "secondary_keywords": ["<2-3 extra zoekwoorden>"]
    },
    {
      "page": "orthodontie",
      "meta_title": "<max 60 tekens>",
      "meta_description": "<max 155 tekens>",
      "primary_keyword": "<hoofd zoekwoord>",
      "secondary_keywords": ["<2-3 extra zoekwoorden>"]
    }
  ]
}

BELANGRIJK: meta_title MAXIMAAL 60 tekens. meta_description MAXIMAAL 155 tekens.

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["pages"],
    ),
    Task(
        id="review_response",
        category="customer_service",
        name="Review Response (NL)",
        prompt="""\
Je bent de eigenaar van een restaurant in Den Haag. Reageer op deze Google review.

Review (3 sterren):
"We zijn vorige week zaterdag geweest met z'n vieren. Het eten was uitstekend -- de ossobuco was een van de beste die ik ooit heb gehad en de tiramisu was perfect. Helaas moesten we 25 minuten wachten op onze drankjes en de bediening leek wat gestrest. De sfeer is fijn en het interieur mooi. Zouden zeker terugkomen als de service wat vlotter zou zijn."

Restaurant: Trattoria Bella Vita, Den Haag
Stijl: Italiaans restaurant, mid-range (hoofdgerechten EUR 18-28)

Return valid JSON with exactly this structure:
{
  "response_text": "<80-120 woorden professioneel Nederlands antwoord>",
  "tone_analysis": "<beschrijving van de gekozen toon en waarom>",
  "sentiment": "<positive|mixed|negative>",
  "follow_up_action": "<concrete interne actie naar aanleiding van de review>"
}

Het antwoord moet empathisch zijn, de complimenten erkennen, het probleem serieus nemen, en uitnodigen om terug te komen.

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["response_text", "tone_analysis", "sentiment", "follow_up_action"],
    ),
    Task(
        id="product_description",
        category="ecommerce",
        name="Product Description (NL)",
        prompt="""\
Je bent een copywriter voor een Nederlandse webshop in artisanale producten.

Product: Oud Amsterdammer Belegen Kaas (hele kaas, 4.5 kg)
Producent: Kaasboerderij De Weide, Gouda
Kenmerken: 8 maanden gerijpt, rauwe melk, ambachtelijk bereid, geen kunstmatige toevoegingen
Prijs: EUR 49,95
Doelgroep: Foodies, relatiegeschenken, kaasliefhebbers

Return valid JSON with exactly this structure:
{
  "short_description": "<max 50 woorden, pakkende webshop korte beschrijving>",
  "long_description": "<ca 150 woorden, uitgebreide productbeschrijving>",
  "bullet_points": ["<USP 1>", "<USP 2>", "<USP 3>", "<USP 4>", "<USP 5>"],
  "seo_title": "<max 60 tekens, SEO-geoptimaliseerde titel>"
}

Schrijf in aantrekkelijk, smaaklijk Nederlands dat de ambachtelijke kwaliteit benadrukt.

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["short_description", "long_description", "bullet_points", "seo_title"],
    ),
    Task(
        id="email_reactivation",
        category="email",
        name="Re-engagement Email (NL)",
        prompt="""\
Je bent een email marketeer voor een fitness studio.

Bedrijf: FitZone Eindhoven
Probleem: Leden die 30+ dagen niet zijn geweest reactiveren
Aanbod: Gratis personal training sessie bij terugkomst deze maand
Toon: Motiverend maar niet pushy, persoonlijk
Lid: {{voornaam}} (personalisatie-token)

Return valid JSON with exactly this structure:
{
  "subject_lines": ["<variant 1>", "<variant 2>", "<variant 3>", "<variant 4>", "<variant 5>"],
  "preview_text": "<max 90 tekens preview tekst voor inbox>",
  "email_body": "<200-250 woorden Nederlandse email body, gebruik {{voornaam}} voor personalisatie>",
  "cta_button_text": "<korte CTA button tekst>"
}

De subject lines moeten varieren in stijl: vraag, statement, urgentie, persoonlijk, benefit.

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["subject_lines", "preview_text", "email_body", "cta_button_text"],
    ),
    Task(
        id="faq_generation",
        category="content",
        name="FAQ Generation (NL)",
        prompt="""\
Je bent een content specialist voor een schildersbedrijf.

Bedrijf: Schildersbedrijf Groen & Zn., Groningen
Diensten: Binnenschilderwerk, buitenschilderwerk, behangen, houtrot reparatie, kleuradvies
Doelgroep: Particuliere huiseigenaren in Groningen en omgeving
Website doel: SEO traffic en vertrouwen opbouwen

Genereer FAQ's die zowel informatief zijn als SEO-waarde hebben.
De antwoorden moeten de expertise van het bedrijf laten zien.

Return valid JSON with exactly this structure:
{
  "faqs": [
    {"question": "<veelgestelde vraag 1>", "answer": "<50-80 woorden antwoord>"},
    {"question": "<veelgestelde vraag 2>", "answer": "<50-80 woorden antwoord>"},
    {"question": "<veelgestelde vraag 3>", "answer": "<50-80 woorden antwoord>"},
    {"question": "<veelgestelde vraag 4>", "answer": "<50-80 woorden antwoord>"},
    {"question": "<veelgestelde vraag 5>", "answer": "<50-80 woorden antwoord>"}
  ],
  "schema_type": "FAQPage"
}

Schrijf in helder, toegankelijk Nederlands. Vermijd jargon.

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["faqs", "schema_type"],
    ),
    Task(
        id="lead_qualification",
        category="sales",
        name="Lead Qualification",
        prompt="""\
Je bent een sales analyst voor een B2B software bedrijf.

Er is een nieuw contactformulier binnengekomen. Beoordeel deze lead.

Formulierdata:
- Naam: Pieter Jansen
- Email: p.jansen@vandermeijden-installatie.nl
- Bedrijf: Van der Meijden Installatietechniek
- Functie: Directeur-eigenaar
- Aantal medewerkers: 35
- Vraag: "We zoeken een betere manier om onze offertes en projecten bij te houden. Nu werken we nog met Excel maar dat wordt onoverzichtelijk bij 50+ lopende projecten. Budget is niet het probleem, we willen vooral iets dat onze monteurs ook in het veld kunnen gebruiken."
- Bron: Google zoekresultaat "project management software installatiebedrijf"
- Pagina bezocht: Pricing pagina (Enterprise plan bekeken)

Return valid JSON with exactly this structure:
{
  "score": "<1-100 integer>",
  "qualification": "<MQL|SQL|unqualified>",
  "priority": "<high|medium|low>",
  "reasoning": "<3-4 zinnen uitleg van de score>",
  "budget_signals": ["<lijst van budget-indicatoren uit het formulier>"],
  "suggested_next_action": "<concrete volgende stap voor sales team>"
}

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["score", "qualification", "priority", "reasoning", "budget_signals", "suggested_next_action"],
    ),
    Task(
        id="competitor_comparison",
        category="analysis",
        name="Competitor Comparison (NL)",
        prompt="""\
Je bent een marketing strateeg voor een webdesign bureau.

Bedrijf: Studio Digitaal, maatwerk webdesign bureau (team van 8)
Concurrenten: Wix (doe-het-zelf website builder) en Squarespace (design-first builder)
Doelgroep: MKB-ondernemers die twijfelen tussen een bureau en zelf bouwen
Prijsniveau Studio Digitaal: EUR 3.000 - EUR 15.000 voor een complete website

Maak een vergelijking die eerlijk is maar de waarde van maatwerk benadrukt.

Return valid JSON with exactly this structure:
{
  "comparison_table": [
    {"criterium": "<vergelijkingspunt>", "studio_digitaal": "<score/omschrijving>", "wix": "<score/omschrijving>", "squarespace": "<score/omschrijving>"}
  ],
  "summary_paragraph": "<100 woorden Nederlands samenvattend advies>",
  "key_differentiators": ["<3-5 unieke voordelen van maatwerk>"]
}

De comparison_table moet minimaal 6 criteria bevatten (bijv. design, SEO, snelheid, support, schaalbaarheid, eigendom).

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["comparison_table", "summary_paragraph", "key_differentiators"],
    ),
    Task(
        id="instagram_carousel",
        category="social_media",
        name="Instagram Carousel (NL)",
        prompt="""\
Je bent een social media specialist voor een bloemist.

Bedrijf: Bloemenatelier Rosalie, Haarlem
Aanleiding: Bruiloftsseizoen promoten (mei-september)
Stijl: Romantisch, elegant, natuurlijk
Doel: Aanvragen voor bruidsboeketten genereren

Ontwerp een Instagram carousel post (5 slides) over bruiloftsbloemen.

Return valid JSON with exactly this structure:
{
  "caption": "<100-150 woorden Nederlandse Instagram caption>",
  "hashtags": ["<10-15 relevante hashtags, mix van NL en EN>"],
  "carousel_slides": [
    {"slide_number": 1, "visual_description": "<wat moet op de foto/graphic staan>", "text_overlay": "<korte tekst op de slide>"},
    {"slide_number": 2, "visual_description": "<...>", "text_overlay": "<...>"},
    {"slide_number": 3, "visual_description": "<...>", "text_overlay": "<...>"},
    {"slide_number": 4, "visual_description": "<...>", "text_overlay": "<...>"},
    {"slide_number": 5, "visual_description": "<...>", "text_overlay": "<...>"}
  ]
}

De carousel moet een verhaal vertellen: van inspiratie naar concrete actie.

Return ONLY the JSON object. No explanation, no markdown fences.""",
        expected_format="json",
        expected_fields=["caption", "hashtags", "carousel_slides"],
    ),
]

QUICK_MARKETING_IDS = {"linkedin_post", "google_ads", "lead_qualification"}


# ---------------------------------------------------------------------------
# Ollama model runner
# ---------------------------------------------------------------------------


def _ollama_api(endpoint: str, payload: dict | None = None,
                method: str = "GET", timeout: int = 10) -> dict:
    """Make a request to the Ollama HTTP API."""
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
    """Return True if Ollama is reachable."""
    try:
        _ollama_api("/api/tags", timeout=5)
        return True
    except Exception:
        return False


def list_ollama_models() -> List[str]:
    """List locally available Ollama model names."""
    try:
        data = _ollama_api("/api/tags", timeout=5)
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def pull_ollama_model(model: str) -> bool:
    """Pull a model via ollama CLI. Returns True on success."""
    print(f"    Pulling {model} (this may take a while)...", flush=True)
    try:
        result = subprocess.run(
            ["ollama", "pull", model],
            capture_output=True, text=True, timeout=600,
        )
        return result.returncode == 0
    except Exception:
        return False


def _ollama_stream(model: str, prompt: str, timeout: int) -> tuple:
    """Stream a response from Ollama, returning (full_text, eval_count, elapsed)."""
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


def run_ollama(model: str, prompt: str, no_think: bool = False, timeout: int = 0) -> Result:
    """Run a prompt through a local Ollama model with streaming."""
    effective_timeout = timeout if timeout > 0 else TIMEOUT_LOCAL
    start = time.monotonic()
    try:
        actual_prompt = f"/no_think\n{prompt}" if no_think else prompt
        output, eval_count, elapsed = _ollama_stream(model, actual_prompt, effective_timeout)
        tps = eval_count / elapsed if elapsed > 0 else 0.0

        return Result(
            task_id="", task_name="", category="", model=model,
            success=True,
            duration_seconds=round(elapsed, 2),
            tokens_generated=eval_count,
            tokens_per_second=round(tps, 1),
            output=output,
            output_length=len(output),
            format_compliant=False,
            fields_present=[], fields_missing=[],
            completeness_score=0.0, quality=None, error=None,
            timestamp=datetime.now().isoformat(),
        )
    except urllib.error.URLError as exc:
        elapsed_so_far = time.monotonic() - start
        if "error" in str(exc).lower() or elapsed_so_far < 5:
            if pull_ollama_model(model):
                return _run_ollama_retry(model, prompt)
        return _error_result(model, time.monotonic() - start, str(exc))
    except (TimeoutError, Exception) as exc:
        return _error_result(model, time.monotonic() - start, str(exc))


def _run_ollama_retry(model: str, prompt: str) -> Result:
    """Single retry after pulling a model."""
    start = time.monotonic()
    try:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": NUM_PREDICT, "num_ctx": NUM_CTX},
        }
        body = _ollama_api("/api/generate", payload=payload, timeout=TIMEOUT_LOCAL)
        elapsed = time.monotonic() - start
        output = body.get("response", "")
        eval_count = body.get("eval_count", max(1, int(len(output.split()) * 1.3)))
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
    except Exception as exc:
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
# Claude CLI runner
# ---------------------------------------------------------------------------


def run_claude(prompt: str, model_key: str = "claude-sonnet") -> Result:
    """Run a prompt through the Claude CLI."""
    cli_model = CLAUDE_MODELS.get(model_key, "sonnet")
    start = time.monotonic()
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "json", "--model", cli_model,
             "--max-turns", "1"],
            input=prompt,
            capture_output=True, text=True,
            timeout=TIMEOUT_CLAUDE,
        )
        elapsed = time.monotonic() - start

        if result.returncode != 0:
            return _error_result(
                model_key, elapsed,
                f"Exit code {result.returncode}: {result.stderr[:300]}",
            )

        # Parse Claude JSON output envelope
        try:
            envelope = json.loads(result.stdout)
            output_text = envelope.get("result", result.stdout)
            cost_usd = envelope.get("cost_usd", 0)
        except json.JSONDecodeError:
            output_text = result.stdout
            cost_usd = 0

        tokens_est = max(1, int(len(output_text.split()) * 1.3))
        tps = tokens_est / elapsed if elapsed > 0 else 0.0

        r = Result(
            task_id="", task_name="", category="", model=model_key,
            success=True,
            duration_seconds=round(elapsed, 2),
            tokens_generated=tokens_est,
            tokens_per_second=round(tps, 1),
            output=output_text, output_length=len(output_text),
            format_compliant=False,
            fields_present=[], fields_missing=[],
            completeness_score=0.0, quality=None,
            error=f"cost=${cost_usd:.4f}" if cost_usd else None,
            timestamp=datetime.now().isoformat(),
        )
        return r

    except FileNotFoundError:
        return _error_result(model_key, time.monotonic() - start,
                             "Claude CLI not found in PATH")
    except subprocess.TimeoutExpired:
        return _error_result(model_key, time.monotonic() - start,
                             f"Timeout after {TIMEOUT_CLAUDE}s")


# ---------------------------------------------------------------------------
# Scoring engine
# ---------------------------------------------------------------------------


NL_STOPWORDS = {"de", "het", "een", "van", "voor", "niet", "met", "zijn", "dat",
                 "die", "als", "aan", "maar", "bij", "ook", "nog", "naar", "wel",
                 "dan", "meer", "uit", "werd", "worden", "heeft", "kan", "zou",
                 "dit", "over", "door", "deze", "geen", "onder", "andere"}


def _score_dutch_quality(text: str) -> dict:
    """Score Dutch language quality of marketing output.

    Returns dict with:
      - is_dutch: bool (heuristic based on stopword density)
      - dutch_score: float 0-1 (stopword density ratio)
      - word_count: int
      - char_limit_violations: list of {field, limit, actual} dicts
    """
    words = text.lower().split()
    if not words:
        return {"is_dutch": False, "dutch_score": 0.0, "word_count": 0,
                "char_limit_violations": []}

    nl_words = sum(1 for w in words if w.strip(".,!?:;\"'()") in NL_STOPWORDS)
    density = nl_words / len(words)

    return {
        "is_dutch": density > 0.06,
        "dutch_score": round(min(1.0, density / 0.15), 2),
        "word_count": len(words),
        "char_limit_violations": [],
    }


def _check_char_limits(parsed: dict, task_id: str) -> List[dict]:
    """Check character limits for marketing tasks that have them."""
    violations = []

    if task_id == "google_ads":
        for i, h in enumerate(parsed.get("headlines", [])):
            if len(h) > 30:
                violations.append({"field": f"headline_{i+1}", "limit": 30, "actual": len(h)})
        for i, d in enumerate(parsed.get("descriptions", [])):
            if len(d) > 90:
                violations.append({"field": f"description_{i+1}", "limit": 90, "actual": len(d)})

    elif task_id == "seo_meta_tags":
        for page in parsed.get("pages", []):
            name = page.get("page", "unknown")
            title = page.get("meta_title", "")
            desc = page.get("meta_description", "")
            if len(title) > 60:
                violations.append({"field": f"{name}_meta_title", "limit": 60, "actual": len(title)})
            if len(desc) > 155:
                violations.append({"field": f"{name}_meta_description", "limit": 155, "actual": len(desc)})

    elif task_id == "product_description":
        seo_title = parsed.get("seo_title", "")
        if len(seo_title) > 60:
            violations.append({"field": "seo_title", "limit": 60, "actual": len(seo_title)})

    return violations


def _extract_json(text: str) -> Optional[dict]:
    """Try to extract a JSON object from text, handling markdown fences."""
    # Strip markdown code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE)

    # Try the full cleaned text first
    try:
        return json.loads(cleaned.strip())
    except json.JSONDecodeError:
        pass

    # Try to find the outermost { ... }
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def _extract_code(text: str) -> Optional[str]:
    """Extract Python code from markdown fences or raw text."""
    match = re.search(r"```python\s*([\s\S]*?)```", text)
    if match:
        return match.group(1).strip()
    # If it looks like raw Python (starts with import or def)
    if re.match(r"^\s*(#!.*python|import |from |def |class |@|\"\"\")", text.strip()):
        return text.strip()
    return None


def _check_python_syntax(code: str) -> bool:
    """Check if code is syntactically valid Python."""
    try:
        compile(code, "<benchmark>", "exec")
        return True
    except SyntaxError:
        return False


def score_result(result: Result, task: Task) -> Result:
    """Score the result and update format/completeness fields in-place."""
    if not result.success:
        result.fields_missing = task.expected_fields[:]
        return result

    if task.expected_format == "json":
        parsed = _extract_json(result.output)
        if parsed is not None and isinstance(parsed, dict):
            result.format_compliant = True
            present = []
            missing = []
            for f in task.expected_fields:
                if f in parsed:
                    present.append(f)
                else:
                    missing.append(f)
            result.fields_present = present
            result.fields_missing = missing
            if task.expected_fields:
                result.completeness_score = round(
                    len(present) / len(task.expected_fields), 2
                )
            else:
                result.completeness_score = 1.0
        else:
            result.fields_missing = task.expected_fields[:]

    elif task.expected_format == "code":
        code = _extract_code(result.output)
        if code is not None:
            result.format_compliant = _check_python_syntax(code)
            # Check for expected test function patterns
            present = []
            missing = []
            for marker in task.expected_fields:
                if marker in code:
                    present.append(marker)
                else:
                    missing.append(marker)
            result.fields_present = present
            result.fields_missing = missing
            test_count = len(re.findall(r"def test_\w+", code))
            if task.expected_fields:
                result.completeness_score = round(
                    len(present) / len(task.expected_fields), 2
                )
            else:
                result.completeness_score = min(1.0, test_count / 5)
        else:
            result.fields_missing = task.expected_fields[:]

    elif task.expected_format == "text":
        text = result.output.strip()
        word_count = len(text.split())
        # Valid if >50 words
        result.format_compliant = word_count > 50
        # Completeness based on target ~150 words
        result.completeness_score = round(min(1.0, word_count / 150), 2)

    # Marketing-specific scoring: Dutch quality + char limits
    if task.id in {t.id for t in MARKETING_TASKS}:
        # Collect all text from output for Dutch scoring
        all_text = result.output
        if task.expected_format == "json":
            parsed = _extract_json(result.output)
            if parsed and isinstance(parsed, dict):
                # Extract text fields for Dutch analysis
                text_parts = []
                for v in parsed.values():
                    if isinstance(v, str):
                        text_parts.append(v)
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, str):
                                text_parts.append(item)
                            elif isinstance(item, dict):
                                for sv in item.values():
                                    if isinstance(sv, str):
                                        text_parts.append(sv)
                all_text = " ".join(text_parts)

                # Check char limits
                violations = _check_char_limits(parsed, task.id)
                if violations:
                    # Penalize completeness for char limit violations
                    penalty = len(violations) * 0.1
                    result.completeness_score = max(0.0,
                        round(result.completeness_score - penalty, 2))

        dutch = _score_dutch_quality(all_text)
        # Store dutch quality info in error field if not already an error
        if result.success and not result.error:
            dutch_info = f"dutch={dutch['dutch_score']:.0%}"
            if not dutch["is_dutch"]:
                dutch_info += " [NOT_DUTCH]"
            violations = _check_char_limits(
                _extract_json(result.output) or {}, task.id)
            if violations:
                dutch_info += f" charlim_violations={len(violations)}"
            result.error = dutch_info

    return result


# ---------------------------------------------------------------------------
# Progress / resume
# ---------------------------------------------------------------------------


def load_progress() -> Dict[str, Any]:
    """Load progress from the checkpoint file."""
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_progress(progress: Dict[str, Any]) -> None:
    """Persist progress incrementally."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(
        json.dumps(progress, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------


def _ascii_bar(value: float, max_value: float, width: int = 30) -> str:
    """Render an ASCII bar for the speed chart."""
    if max_value <= 0:
        return ""
    filled = int((value / max_value) * width)
    filled = min(filled, width)
    return "#" * filled + "." * (width - filled)


def generate_markdown_report(
    results: Dict[str, Dict], all_models: List[str], config: BenchmarkConfig,
) -> str:
    """Generate the full markdown comparison report."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    task_count = len(set(r["task_id"] for r in results.values()))

    lines = [
        f"# LLM Benchmark Report -- {ts}",
        "",
        f"**Run ID**: {config.run_id}",
        f"**Models**: {', '.join(all_models)}",
        f"**Mode**: {'no-think' if config.no_think else 'thinking (default)'}",
        f"**Tasks**: {task_count}",
        f"**Total runs**: {len(results)}",
        "",
        "---",
        "",
    ]

    # -- Summary table --
    lines.extend([
        "## Summary",
        "",
        "| Model | Success | Avg Speed (tok/s) | Format OK | Completeness | Avg Time |",
        "|-------|---------|-------------------|-----------|--------------|----------|",
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
            f"| {model} | {successes}/{n} | {avg_tps:.1f} | "
            f"{fmt_ok}/{n} | {avg_comp:.0%} | {avg_dur:.1f}s |"
        )

    lines.extend(["", "---", ""])

    # -- Speed comparison chart (ASCII) --
    lines.extend(["## Speed Comparison (tokens/second)", ""])

    # Collect average tps per model
    model_avg_tps: Dict[str, float] = {}
    for model in all_models:
        mr = [r for r in results.values() if r["model"] == model and r["success"]]
        if mr:
            model_avg_tps[model] = sum(r["tokens_per_second"] for r in mr) / len(mr)
        else:
            model_avg_tps[model] = 0.0

    max_tps = max(model_avg_tps.values()) if model_avg_tps else 1.0
    max_tps = max(max_tps, 0.1)  # avoid div by zero

    lines.append("```")
    name_width = max((len(m) for m in all_models), default=15)
    for model in all_models:
        tps = model_avg_tps.get(model, 0)
        bar = _ascii_bar(tps, max_tps, 40)
        lines.append(f"  {model:<{name_width}} |{bar}| {tps:.1f} tok/s")
    lines.append("```")
    lines.extend(["", "---", ""])

    # -- Per-task detailed results --
    lines.extend(["## Per-Task Results", ""])

    task_ids_seen: List[str] = []
    for r in results.values():
        if r["task_id"] not in task_ids_seen:
            task_ids_seen.append(r["task_id"])

    for task_id in task_ids_seen:
        task_results = {
            r["model"]: r for r in results.values() if r["task_id"] == task_id
        }
        if not task_results:
            continue
        first = next(iter(task_results.values()))
        lines.extend([
            f"### {first['task_name']} (`{first['category']}`)",
            "",
            "| Metric | " + " | ".join(all_models) + " |",
            "|--------|" + "|".join("------" for _ in all_models) + "|",
        ])

        # Duration row
        vals = []
        for m in all_models:
            r = task_results.get(m)
            if r and r["success"]:
                vals.append(f"{r['duration_seconds']:.1f}s")
            elif r:
                vals.append("FAIL")
            else:
                vals.append("-")
        lines.append("| Duration | " + " | ".join(vals) + " |")

        # Tokens/sec row
        vals = []
        for m in all_models:
            r = task_results.get(m)
            tps = r["tokens_per_second"] if r and r["success"] else 0
            vals.append(f"{tps:.1f}" if tps > 0 else "-")
        lines.append("| Tokens/sec | " + " | ".join(vals) + " |")

        # Format compliant row
        vals = []
        for m in all_models:
            r = task_results.get(m)
            vals.append("Yes" if r and r["format_compliant"] else "No")
        lines.append("| Format OK | " + " | ".join(vals) + " |")

        # Completeness row
        vals = []
        for m in all_models:
            r = task_results.get(m)
            c = r["completeness_score"] if r else 0
            vals.append(f"{c:.0%}")
        lines.append("| Completeness | " + " | ".join(vals) + " |")

        # Fields missing row
        vals = []
        for m in all_models:
            r = task_results.get(m)
            missing = r.get("fields_missing", []) if r else []
            vals.append(", ".join(missing) if missing else "none")
        lines.append("| Missing fields | " + " | ".join(vals) + " |")

        lines.append("")

        # Output previews in collapsible sections
        for m in all_models:
            r = task_results.get(m)
            if r and r["success"]:
                preview = r["output"][:600].replace("\n", "\n> ")
                lines.extend([
                    f"<details><summary>{m} output (preview)</summary>",
                    "",
                    f"> {preview}",
                    "",
                    "</details>",
                    "",
                ])

        lines.extend(["---", ""])

    # -- Recommendations per category --
    lines.extend([
        "## Recommendations per Category",
        "",
        "| Category | Recommended Model | Score | Reasoning |",
        "|----------|------------------|-------|-----------|",
    ])

    categories = list(dict.fromkeys(r["category"] for r in results.values()))
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

        best_model = max(
            model_agg,
            key=lambda m: sum(model_agg[m]) / len(model_agg[m]),
        )
        best_avg = sum(model_agg[best_model]) / len(model_agg[best_model])

        # Build reasoning
        best_results = [r for r in cat_results if r["model"] == best_model]
        avg_tps = (
            sum(r["tokens_per_second"] for r in best_results) / len(best_results)
            if best_results else 0
        )
        fmt_rate = (
            sum(1 for r in best_results if r["format_compliant"]) / len(best_results)
            if best_results else 0
        )
        reason = (
            f"Avg {avg_tps:.0f} tok/s, {fmt_rate:.0%} format compliance, "
            f"composite {best_avg:.2f}/1.5"
        )
        lines.append(f"| {cat} | **{best_model}** | {best_avg:.2f} | {reason} |")

    lines.extend(["", "---", ""])
    lines.append("*Quality scores are null (reserved for manual 1-5 review).*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main benchmark runner
# ---------------------------------------------------------------------------


def run_benchmark(config: BenchmarkConfig, tasks: List[Task],
                  resume: bool = False, no_think: bool = False,
                  timeout: int = 0) -> Dict[str, Dict]:
    """Execute the full benchmark suite sequentially."""
    progress = load_progress() if resume else {}

    all_models = config.models[:]
    if config.include_claude:
        all_models.extend(k for k in CLAUDE_MODELS if k not in all_models)

    total_runs = len(tasks) * len(all_models)
    run_idx = 0

    for task in tasks:
        for model in all_models:
            run_idx += 1
            key = f"{task.id}__{model}"

            if key in progress:
                print(
                    f"  [{run_idx}/{total_runs}] SKIP "
                    f"{task.name} x {model} (cached)"
                )
                continue

            print(
                f"  [{run_idx}/{total_runs}] {task.name} x {model}...",
                end=" ", flush=True,
            )

            if model in CLAUDE_MODELS:
                result = run_claude(task.prompt, model_key=model)
            else:
                result = run_ollama(model, task.prompt, no_think=no_think, timeout=timeout)

            # Fill task metadata
            result.task_id = task.id
            result.task_name = task.name
            result.category = task.category

            # Score
            result = score_result(result, task)

            # Status line
            if result.success:
                print(
                    f"OK ({result.duration_seconds:.1f}s, "
                    f"{result.tokens_per_second:.1f} tok/s, "
                    f"fmt={'Y' if result.format_compliant else 'N'}, "
                    f"comp={result.completeness_score:.0%})"
                )
            else:
                print(f"FAIL: {result.error}")

            # Store as serializable dict
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="VNX LLM Benchmark -- compare Ollama models vs Claude on SEOcrawler tasks"
    )
    parser.add_argument(
        "--models", type=str, default=None,
        help=f"Comma-separated model names (default: {', '.join(DEFAULT_LOCAL_MODELS)})",
    )
    parser.add_argument(
        "--include-claude", action="store_true",
        help="Include Claude CLI (sonnet) in comparison",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="Run only 3 quick tasks (~15 min)",
    )
    parser.add_argument(
        "--marketing", action="store_true",
        help="Run marketing/MKB benchmark tasks instead of coding tasks",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from benchmark_progress.json checkpoint",
    )
    parser.add_argument(
        "--tasks", type=str, default=None,
        help="Comma-separated task IDs to run",
    )
    parser.add_argument(
        "--no-think", action="store_true",
        help="Prefix prompts with /no_think for Qwen 3.5 thinking models",
    )
    parser.add_argument(
        "--timeout", type=int, default=0,
        help="Override timeout per task in seconds (0 = use default 300s)",
    )
    parser.add_argument(
        "--num-predict", type=int, default=0,
        help="Override max output tokens (default: 4096). Use 8192+ for thinking models.",
    )
    parser.add_argument(
        "--num-ctx", type=int, default=0,
        help="Override context window size (default: 8192). Use 16384+ for longer outputs.",
    )
    args = parser.parse_args()

    # Apply token limit overrides
    global NUM_PREDICT, NUM_CTX
    if args.num_predict > 0:
        NUM_PREDICT = args.num_predict
    if args.num_ctx > 0:
        NUM_CTX = args.num_ctx

    print("=" * 64)
    mode_label = "Marketing/MKB" if args.marketing else "Coding/SEOcrawler"
    print(f"  VNX LLM Benchmark ({mode_label})")
    print("=" * 64)

    # Validate Ollama connectivity (skip if only running Claude)
    only_claude = args.include_claude and args.models and args.models.strip().lower() == "none"
    if not only_claude and not check_ollama_available():
        print("\nERROR: Ollama not available at " + OLLAMA_URL)
        print("Start with: ollama serve")
        sys.exit(1)

    # Determine models
    if args.models:
        if args.models.strip().lower() == "none":
            models: List[str] = []
        else:
            models = [m.strip() for m in args.models.split(",")]
    else:
        available = list_ollama_models()
        # Filter to defaults that are available, preserve order
        models = [m for m in DEFAULT_LOCAL_MODELS if m in available]
        if not models and available:
            # Fall back to any available qwen models
            models = [m for m in available if "qwen" in m.lower()]
        if not models and available:
            models = available[:4]
        if not models and not args.include_claude:
            print(f"\nNo local models found. Available: {available}")
            print(f"Expected: {DEFAULT_LOCAL_MODELS}")
            print("Pull a model with: ollama pull qwen3.5:9b")
            sys.exit(1)

    # Determine task pool
    task_pool = MARKETING_TASKS if args.marketing else BENCHMARK_TASKS
    quick_ids = QUICK_MARKETING_IDS if args.marketing else QUICK_TASK_IDS

    if args.tasks:
        task_ids_requested = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in task_pool if t.id in task_ids_requested]
    elif args.quick:
        tasks = [t for t in task_pool if t.id in quick_ids]
    else:
        tasks = task_pool[:]

    if not tasks:
        print("ERROR: No valid tasks selected")
        sys.exit(1)

    all_models_display = models + (list(CLAUDE_MODELS.keys()) if args.include_claude else [])
    print(f"\nModels:  {', '.join(all_models_display) or '(none)'}")
    print(f"Tasks:   {', '.join(t.id for t in tasks)} ({len(tasks)} total)")
    est_minutes = len(tasks) * len(all_models_display) * 1.5
    print(f"Est:     ~{est_minutes:.0f} minutes")
    if args.resume:
        print(f"Resume:  from {PROGRESS_FILE}")
    print()

    # Build config
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    config = BenchmarkConfig(
        models=models,
        task_ids=[t.id for t in tasks],
        include_claude=args.include_claude,
        run_id=run_id,
        started_at=datetime.now().isoformat(),
        no_think=args.no_think,
    )

    # Execute
    results = run_benchmark(config, tasks, resume=args.resume, no_think=args.no_think, timeout=args.timeout)

    if not results:
        print("No results collected.")
        sys.exit(1)

    # Generate reports
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    report_md = generate_markdown_report(results, all_models_display, config)
    md_path = REPORT_DIR / f"benchmark_{run_id}.md"
    md_path.write_text(report_md, encoding="utf-8")

    json_path = REPORT_DIR / f"benchmark_{run_id}.json"
    raw_data = {
        "config": asdict(config),
        "results": results,
    }
    json_path.write_text(
        json.dumps(raw_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Print summary
    print()
    print("=" * 64)
    print("  RESULTS")
    print("=" * 64)

    for model in all_models_display:
        mr = [r for r in results.values() if r["model"] == model]
        if not mr:
            continue
        successes = sum(1 for r in mr if r["success"])
        avg_dur = sum(r["duration_seconds"] for r in mr) / len(mr)
        avg_tps = sum(r["tokens_per_second"] for r in mr) / len(mr)
        fmt_ok = sum(1 for r in mr if r["format_compliant"])
        avg_comp = sum(r["completeness_score"] for r in mr) / len(mr)
        print(
            f"  {model:<25} {successes}/{len(mr)} ok  "
            f"{avg_dur:6.1f}s avg  {avg_tps:6.1f} tok/s  "
            f"fmt {fmt_ok}/{len(mr)}  comp {avg_comp:.0%}"
        )

    print()
    print(f"  Report:   {md_path}")
    print(f"  Raw data: {json_path}")
    print(f"  Progress: {PROGRESS_FILE}")
    print()


if __name__ == "__main__":
    main()
