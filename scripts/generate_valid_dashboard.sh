#!/bin/bash
# Generate valid dashboard JSON from canonical state.

DASHBOARD_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$DASHBOARD_SCRIPT_DIR/lib/vnx_paths.sh"
# shellcheck source=singleton_enforcer.sh
source "$DASHBOARD_SCRIPT_DIR/singleton_enforcer.sh"
STATE_DIR="$VNX_STATE_DIR"
VNX_DIR="$VNX_HOME"
DISPATCH_DIR="$VNX_DISPATCH_DIR"

# Supervisor-compatible singleton (writes VNX_PIDS_DIR/dashboard.pid).
enforce_singleton "dashboard"

safe_count() {
    local pattern="$1"
    local file="$2"
    local count
    count=$(grep -c "$pattern" "$file" 2>/dev/null || true)
    if [ -z "$count" ]; then
        count=0
    fi
    printf '%s' "$count"
}

count_md() {
    local dir="$1"
    if [ -d "$dir" ]; then
        find "$dir" -name "*.md" 2>/dev/null | wc -l | tr -d ' '
    else
        echo 0
    fi
}

get_terminal_status_summary() {
    python3 "$DASHBOARD_SCRIPT_DIR/lib/canonical_state_views.py" \
        --state-dir "$STATE_DIR" \
        dashboard-terminals 2>/dev/null || \
        echo '{"T0":{"status":"unknown","model":"unknown","is_active":false,"current_command":"unknown","directory":"unknown","last_update":"never"},"T1":{"status":"offline","model":"sonnet","is_active":false,"current_command":"claude","directory":"vnx-terminal","last_update":"never"},"T2":{"status":"offline","model":"sonnet","is_active":false,"current_command":"claude","directory":"vnx-terminal","last_update":"never"},"T3":{"status":"offline","model":"opus","is_active":false,"current_command":"claude","directory":"vnx-terminal","last_update":"never"}}'
}

get_lock_status() {
    local locks_json='{'

    for track in A B C; do
        local track_lower
        track_lower=$(echo "$track" | tr A-Z a-z)
        local lock_file="$STATE_DIR/.track_${track_lower}_processing.lock"

        local cursor
        cursor=$(cat "$STATE_DIR/.track_${track_lower}_cursor" 2>/dev/null || echo "0")
        local lines=0
        if [ -f "$STATE_DIR/receipts_track_${track}.ndjson" ]; then
            lines=$(wc -l < "$STATE_DIR/receipts_track_${track}.ndjson" | tr -d ' ' || echo "0")
        fi
        cursor="${cursor:-0}"
        lines="${lines:-0}"

        if [ -d "$lock_file" ]; then
            local lock_age
            lock_age=$(($(date +%s) - $(stat -f %m "$lock_file" 2>/dev/null || date +%s)))
            locks_json+='"'track_$track'": {"locked": true, "age_seconds": '$lock_age', "cursor": '$cursor', "pending": '$lines'}, '
        else
            locks_json+='"'track_$track'": {"locked": false, "cursor": '$cursor', "pending": '$lines'}, '
        fi
    done

    locks_json="${locks_json%, }"
    locks_json+='}'
    echo "$locks_json"
}

get_intelligence_health() {
    local health_file="$STATE_DIR/intelligence_health.json"
    if [ ! -f "$health_file" ]; then
        echo "null"
        return
    fi

    python3 - <<'PY'
import json
import os
from pathlib import Path

state_dir = os.environ.get("VNX_STATE_DIR")
if not state_dir:
    raise RuntimeError("VNX_STATE_DIR not set")
health_file = Path(state_dir) / "intelligence_health.json"
try:
    data = json.loads(health_file.read_text())
    print(json.dumps(data))
except Exception:
    print("null")
PY
}

get_open_items_summary() {
    local digest_file="$STATE_DIR/open_items_digest.json"
    if [ ! -f "$digest_file" ]; then
        echo "null"
        return
    fi

    jq -c '{
      summary: (.summary // {}),
      open_count: (.summary.open_count // 0),
      blocker_count: (.summary.blocker_count // 0),
      top_blockers: (.top_blockers // []),
      top_warnings: (.top_warnings // []),
      open_items: (.open_items // []),
      last_updated: (.last_updated // null)
    }' "$digest_file" 2>/dev/null || echo "null"
}

get_pr_queue_summary() {
    local pr_file="$STATE_DIR/pr_queue_state.yaml"
    if [ ! -f "$pr_file" ]; then
        echo "null"
        return
    fi

    python3 - <<'PY'
import json
import os
import re
from pathlib import Path

import yaml

path = Path(os.environ.get("VNX_STATE_DIR", "/tmp/vnx_state_missing")) / "pr_queue_state.yaml"
try:
    data = yaml.safe_load(path.read_text()) or {}
except Exception:
    print("null")
    raise SystemExit(0)

active_feature = (data.get("active_feature") or {}).get("name")
completed = set(data.get("completed_prs") or [])
in_progress = data.get("in_progress")
next_available = data.get("next_available") or []
execution_order = data.get("execution_order") or []

def load_plan_titles(plan_path):
    if not plan_path:
        return {}
    plan_file = Path(plan_path)
    if not plan_file.is_absolute():
        project_root = Path(os.environ.get("PROJECT_ROOT", "/tmp/vnx_project_missing"))
        plan_file = project_root / plan_file
    try:
        content = plan_file.read_text()
    except Exception:
        return {}
    titles = {}
    current_pr = None
    for line in content.splitlines():
        if line.startswith("## PR-"):
            current_pr = line.split(":")[0].replace("## ", "").strip()
            continue
        if current_pr and line.startswith("**Title**:"):
            titles[current_pr] = line.split("**Title**:", 1)[1].strip()
            current_pr = None
    return titles

def parse_feature_plan(plan_path):
    if not plan_path:
        return {}
    plan_file = Path(plan_path)
    if not plan_file.is_absolute():
        project_root = Path(os.environ.get("PROJECT_ROOT", "/tmp/vnx_project_missing"))
        plan_file = project_root / plan_file
    try:
        content = plan_file.read_text()
    except Exception:
        return {}

    blocks = {}
    current_pr = None
    current_name = None
    current_block = []

    def flush():
        nonlocal current_pr, current_name, current_block
        if not current_pr:
            return
        title = None
        deps = []
        for line in current_block:
            if title is None and line.startswith("**Title**:"):
                title = line.split("**Title**:", 1)[1].strip()
            if line.strip().startswith("Dependencies:"):
                bracket = line.split("Dependencies:", 1)[1].strip()
                if bracket.startswith("[") and bracket.endswith("]"):
                    inner = bracket[1:-1].strip()
                    if inner:
                        deps = [d.strip() for d in inner.split(",") if d.strip()]
                else:
                    deps = [d.strip() for d in bracket.split(",") if d.strip()]
        blocks[current_pr] = {
            "id": current_pr,
            "name": current_name,
            "title": title,
            "deps": deps,
        }
        current_pr = None
        current_name = None
        current_block = []

    for line in content.splitlines():
        if line.startswith("## PR-"):
            flush()
            header = line.replace("## ", "", 1).strip()
            pr_id = header.split(":", 1)[0].strip()
            pr_name = header.split(":", 1)[1].strip() if ":" in header else None
            current_pr = pr_id
            current_name = pr_name
            current_block = [line]
            continue
        if current_pr:
            current_block.append(line)
    flush()
    return blocks

plan_file = (data.get("active_feature") or {}).get("plan_file")
titles = load_plan_titles(plan_file)
plan_blocks = parse_feature_plan(plan_file)

def status_for(pr_id):
    if pr_id in completed:
        return "done"
    if in_progress and pr_id == in_progress:
        return "in_progress"
    return "pending"

def normalize_deps(deps):
    return [d for d in deps if isinstance(d, str) and d.startswith("PR-")]

prs = []
for pr_id in execution_order:
    block = plan_blocks.get(pr_id) or {}
    deps = normalize_deps(block.get("deps") or [])
    is_blocked = False
    if status_for(pr_id) == "pending" and deps:
        is_blocked = any(dep not in completed for dep in deps)
    prs.append(
        {
            "id": pr_id,
            "description": (block.get("name") or titles.get(pr_id) or pr_id),
            "status": status_for(pr_id),
            "deps": deps,
            "blocked": is_blocked,
        }
    )

total_prs = len(execution_order)
completed_count = len(completed.intersection(execution_order))
progress_percent = int(round((completed_count / total_prs) * 100)) if total_prs else 0

current_title = titles.get(in_progress) if in_progress else None
next_title = titles.get(next_available[0]) if next_available else None

registry = {}
for pr in prs:
    pr_id = str(pr.get("id") or "")
    match = re.search(r"PR-?(\d+)", pr_id, re.IGNORECASE)
    if not match:
        continue
    number = match.group(1)
    registry[number] = {
        "id": pr_id,
        "description": pr.get("description"),
        "gate_trigger": f"gate_pr{number}",
    }

summary = {
    "active_feature": active_feature,
    "plan_file": plan_file,
    "total_prs": total_prs,
    "completed_prs": completed_count,
    "in_progress": in_progress,
    "current_pr": in_progress,
    "current_pr_title": current_title,
    "next_available": next_available[:3],
    "next_pr_title": next_title,
    "blocked_count": len([p for p in prs if p.get("blocked")]),
    "updated_at": data.get("updated_at"),
    "progress_percent": progress_percent,
    "prs": prs,
    "_pr_registry": registry,
}
print(json.dumps(summary))
PY
}

get_attention_summary() {
    python3 "$DASHBOARD_SCRIPT_DIR/lib/canonical_state_views.py" \
        --state-dir "$STATE_DIR" \
        attention-summary 2>/dev/null || echo "null"
}

get_recommendations_summary() {
    local rec_file="$STATE_DIR/t0_recommendations.json"
    if [ ! -f "$rec_file" ]; then
        echo "null"
        return
    fi

    jq -c '{
      total_recommendations: (.total_recommendations // ((.recommendations // []) | length)),
      updated_at: (.generated_at // .timestamp // .updated_at // null),
      recommendations: ((.recommendations // [])[:5] | map({
        trigger: (.trigger // null),
        action: (.action // null),
        gate: (.gate // null),
        dispatch_id: (.dispatch_id // null),
        priority: (.priority // null)
      }))
    }' "$rec_file" 2>/dev/null || echo "null"
}

get_quality_intelligence() {
    local db_path="$STATE_DIR/quality_intelligence.db"
    if [ ! -f "$db_path" ]; then
        return
    fi

    local quality_json
    quality_json=$(sqlite3 "$db_path" << 'SQL'
.mode json
.once /dev/stdout
SELECT
    (SELECT COUNT(*) FROM vnx_code_quality) as total_files,
    (SELECT AVG(complexity_score) FROM vnx_code_quality) as avg_complexity,
    (SELECT SUM(critical_issues) FROM vnx_code_quality) as total_critical,
    (SELECT SUM(warning_issues) FROM vnx_code_quality) as total_warnings,
    (SELECT COUNT(*) FROM snippet_metadata) as total_snippets,
    (SELECT AVG(quality_score) FROM snippet_metadata) as avg_snippet_quality;
SQL
)

    if [ -n "$quality_json" ] && [ "$quality_json" != "[]" ]; then
        local total_files
        local avg_complexity
        local total_critical
        local total_warnings
        local total_snippets
        local avg_snippet
        total_files=$(echo "$quality_json" | jq -r '.[0].total_files // 0')
        avg_complexity=$(echo "$quality_json" | jq -r '.[0].avg_complexity // 0' | awk '{printf "%.2f", $1}')
        total_critical=$(echo "$quality_json" | jq -r '.[0].total_critical // 0')
        total_warnings=$(echo "$quality_json" | jq -r '.[0].total_warnings // 0')
        total_snippets=$(echo "$quality_json" | jq -r '.[0].total_snippets // 0')
        avg_snippet=$(echo "$quality_json" | jq -r '.[0].avg_snippet_quality // 0' | awk '{printf "%.2f", $1}')

        echo ", \"quality_intelligence\": {\"total_files\": $total_files, \"avg_complexity\": $avg_complexity, \"critical_issues\": $total_critical, \"warnings\": $total_warnings, \"total_snippets\": $total_snippets, \"avg_snippet_quality\": $avg_snippet}"
    fi
}

while true; do
    SMART_TAP=$(pgrep -f smart_tap_v7_json_translator | head -1 || echo "")
    DISPATCHER=$(pgrep -f "dispatcher_v8_minimal|dispatcher_v7_compilation" | head -1 || echo "")
    QUEUE_WATCHER=$(pgrep -f queue_popup_watcher | head -1 || echo "")
    REPORT_WATCHER=$(pgrep -f report_watcher.sh | head -1 || echo "")
    RECEIPT_PROCESSOR=$(pgrep -f receipt_processor_v4 | head -1 || echo "")
    SUPERVISOR=$(pgrep -f "vnx_supervisor_simple" | head -1 || echo "")
    if [ -z "$SUPERVISOR" ] && [ -f "$VNX_PIDS_DIR/vnx_supervisor.pid" ]; then
        PID_FROM_FILE=$(cat "$VNX_PIDS_DIR/vnx_supervisor.pid" 2>/dev/null || echo "")
        if [ -n "$PID_FROM_FILE" ] && ps -p "$PID_FROM_FILE" >/dev/null 2>&1; then
            SUPERVISOR="$PID_FROM_FILE"
        fi
    fi
    ACK_DISPATCHER=$(pgrep -f "ack_dispatcher_v2|dispatch_ack_watcher" | head -1 || echo "")
    STATE_MANAGER=$(pgrep -f "state_manager" | head -1 || echo "")

    QUEUE_COUNT=$(count_md "$DISPATCH_DIR/queue")
    PENDING_COUNT=$(count_md "$DISPATCH_DIR/pending")
    ACTIVE_COUNT=$(count_md "$DISPATCH_DIR/active")

    TERMINALS_JSON="$(get_terminal_status_summary)"
    ATTENTION_JSON="$(get_attention_summary)"
    OPEN_ITEMS_JSON="$(get_open_items_summary)"
    PR_QUEUE_WITH_REGISTRY_JSON="$(get_pr_queue_summary)"
    if [ -z "$PR_QUEUE_WITH_REGISTRY_JSON" ] || [ "$PR_QUEUE_WITH_REGISTRY_JSON" = "null" ]; then
        PR_QUEUE_JSON="null"
        PR_REGISTRY_JSON="{}"
    else
        PR_QUEUE_JSON="$(echo "$PR_QUEUE_WITH_REGISTRY_JSON" | jq -c 'del(._pr_registry)' 2>/dev/null || echo "null")"
        PR_REGISTRY_JSON="$(echo "$PR_QUEUE_WITH_REGISTRY_JSON" | jq -c '._pr_registry // {}' 2>/dev/null || echo "{}")"
    fi
    RECOMMENDATIONS_JSON="$(get_recommendations_summary)"
    LOCKS_JSON="$(get_lock_status)"
    INTELLIGENCE_HEALTH_JSON="$(get_intelligence_health)"
    QUALITY_SUFFIX="$(get_quality_intelligence)"

    tmp_file="$(mktemp "$STATE_DIR/dashboard_status.json.tmp.XXXXXX")"
    cat > "$tmp_file" << EOF
{
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "processes": {
        "smart_tap": {"pid": "${SMART_TAP:-0}", "running": $([ -n "$SMART_TAP" ] && echo "true" || echo "false")},
        "dispatcher": {"pid": "${DISPATCHER:-0}", "running": $([ -n "$DISPATCHER" ] && echo "true" || echo "false")},
        "queue_watcher": {"pid": "${QUEUE_WATCHER:-0}", "running": $([ -n "$QUEUE_WATCHER" ] && echo "true" || echo "false")},
        "report_watcher": {"pid": "${REPORT_WATCHER:-0}", "running": $([ -n "$REPORT_WATCHER" ] && echo "true" || echo "false")},
        "receipt_processor": {"pid": "${RECEIPT_PROCESSOR:-0}", "running": $([ -n "$RECEIPT_PROCESSOR" ] && echo "true" || echo "false")},
        "supervisor": {"pid": "${SUPERVISOR:-0}", "running": $([ -n "$SUPERVISOR" ] && echo "true" || echo "false")},
        "ack_dispatcher": {"pid": "${ACK_DISPATCHER:-0}", "running": $([ -n "$ACK_DISPATCHER" ] && echo "true" || echo "false")},
        "state_manager": {"pid": "${STATE_MANAGER:-0}", "running": $([ -n "$STATE_MANAGER" ] && echo "true" || echo "false")}
    },
    "intelligence_daemon": $INTELLIGENCE_HEALTH_JSON,
    "open_items": $OPEN_ITEMS_JSON,
    "pr_queue": $PR_QUEUE_JSON,
    "_pr_registry": $PR_REGISTRY_JSON,
    "recommendations": $RECOMMENDATIONS_JSON,
    "queues": {
        "queue": $QUEUE_COUNT,
        "pending": $PENDING_COUNT,
        "active": $ACTIVE_COUNT
    },
    "gates": {
        "system": {"phase": "operational", "current": "VNX V7.1 State Monitoring Active"},
        "infrastructure": {"phase": "enhanced", "current": "Canonical Terminal State + Reconciler Fallback"},
        "orchestration": {"phase": "ready", "current": "Multi-Terminal Coordination"}
    },
    "metrics": {
        "throughput": $(safe_count "Dispatch sent" "$VNX_LOGS_DIR/dispatcher.log"),
        "successRate": "100%",
        "avgProcessTime": "2s",
        "totalProcessed": $(safe_count "moved to active" "$VNX_LOGS_DIR/dispatcher.log")
    },
    "locks": $LOCKS_JSON,
    "attention": $ATTENTION_JSON,
    "terminals": $TERMINALS_JSON,
    "recentActivity": []$QUALITY_SUFFIX
}
EOF

    if jq -e . "$tmp_file" >/dev/null 2>&1; then
        mv "$tmp_file" "$STATE_DIR/dashboard_status.json"
    else
        rm -f "$tmp_file"
    fi

    sleep 2
done
