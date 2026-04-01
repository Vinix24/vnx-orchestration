#!/usr/bin/env python3
"""
Dual-stack HTTP server for the VNX dashboard.

Why:
- `python -m http.server` often binds to only IPv4 or only IPv6 depending on OS defaults.
- Many systems resolve `localhost` to `::1` first, which makes an IPv4-only server look "down".

This server binds to `::` and attempts to accept IPv4-mapped connections by disabling IPV6_V6ONLY.
It serves `.claude/vnx-system` so these paths work:
- `/` (redirects to `/dashboard/index.html` via `index.html`)
- `/dashboard/index.html`
- `/state/dashboard_status.json`
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone, timedelta
import json
import os
import socket
import sqlite3
import subprocess
import sys
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

# Make scripts/lib importable for conversation_read_model
_SCRIPTS_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, _SCRIPTS_LIB)


class DualStackHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        with contextlib.suppress(Exception):
            # Accept IPv4-mapped connections on the IPv6 socket (platform-dependent).
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


VNX_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = VNX_DIR.parents[1]
SCRIPTS_DIR = VNX_DIR / "scripts"
LOGS_DIR = VNX_DIR / "logs"
CANONICAL_STATE_DIR = Path(os.environ.get("VNX_STATE_DIR", str(PROJECT_ROOT / ".vnx-data" / "state")))
LEGACY_STATE_DIR = VNX_DIR / "state"

PROCESS_COMMANDS = {
    "smart_tap": ["bash", "smart_tap_v7_json_translator.sh"],
    "dispatcher": ["bash", "dispatcher_v8_minimal.sh"],
    "queue_watcher": ["bash", "queue_popup_watcher.sh"],
    "receipt_processor": ["bash", "receipt_processor_v4.sh"],
    "supervisor": ["bash", "vnx_supervisor_simple.sh"],
    "ack_dispatcher": ["bash", "dispatch_ack_watcher.sh"],
    "intelligence_daemon": ["python3", "intelligence_daemon.py"],
    "report_watcher": ["bash", "report_watcher.sh"],
    "receipt_notifier": ["bash", "receipt_notifier.sh"],
}

PROCESS_KILL_PATTERNS = {
    "smart_tap": "smart_tap_v7_json_translator",
    "dispatcher": "dispatcher_v8_minimal|dispatcher_v7_compilation",
    "queue_watcher": "queue_popup_watcher",
    "receipt_processor": "receipt_processor_v4",
    "report_watcher": "report_watcher",
    "receipt_notifier": "receipt_notifier",
    "supervisor": "vnx_supervisor_simple",
    "ack_dispatcher": "dispatch_ack_watcher|ack_dispatcher_v2",
    "intelligence_daemon": "intelligence_daemon.py",
}

TERMINAL_TRACK_MAP = {
    "T1": "A",
    "T2": "B",
    "T3": "C",
}

VALID_TERMINALS = frozenset({"T0", "T1", "T2", "T3"})

VNX_DATA_DIR = CANONICAL_STATE_DIR.parent  # .vnx-data/
DISPATCHES_DIR = VNX_DATA_DIR / "dispatches"
REPORTS_DIR = VNX_DATA_DIR / "unified_reports"

DISPATCH_DIR = Path(os.environ.get("VNX_DISPATCH_DIR", str(PROJECT_ROOT / ".vnx-data" / "dispatches")))
RECEIPTS_PATH = CANONICAL_STATE_DIR / "t0_receipts.ndjson"

DB_PATH = CANONICAL_STATE_DIR / "quality_intelligence.db"

# ---------- Events API ----------

DISPATCH_STAGES = ("staging", "pending", "active", "completed", "rejected")

_EVENT_TYPE_ICONS = {
    "dispatch_created": "dispatch",
    "dispatch_promoted": "dispatch",
    "receipt_complete": "receipt",
    "task_started": "task",
    "task_complete": "receipt",
    "gate_passed": "gate",
    "gate_failed": "gate",
    "context_pressure": "context",
    "open_item_created": "item",
    "open_item_resolved": "item",
}


def _parse_receipts_events(limit: int = 50) -> list[dict]:
    """Read last N events from t0_receipts.ndjson, skipping malformed lines."""
    if not RECEIPTS_PATH.exists():
        return []

    lines: list[str] = []
    try:
        with open(RECEIPTS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []

    events: list[dict] = []
    for line in lines[-(limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = record.get("event_type") or record.get("event") or "unknown"
        timestamp = record.get("timestamp") or ""
        terminal = record.get("terminal") or ""
        dispatch_id = record.get("dispatch_id") or ""
        status = record.get("status") or ""
        gate = record.get("gate") or ""

        pr_id = record.get("pr_id") or ""
        if not pr_id and record.get("type", "").startswith("PR-"):
            pr_id = record["type"].split()[0]

        mapped_type = event_type
        if event_type == "task_complete":
            if status == "success" and gate:
                mapped_type = "gate_passed"
            elif status in ("failure", "failed") and gate:
                mapped_type = "gate_failed"
            else:
                mapped_type = "receipt_complete"

        icon = _EVENT_TYPE_ICONS.get(mapped_type, "event")

        events.append({
            "type": mapped_type,
            "icon": icon,
            "timestamp": timestamp,
            "terminal": terminal,
            "dispatch_id": dispatch_id,
            "pr_id": pr_id,
            "gate": gate,
            "status": status,
            "summary": _event_summary(mapped_type, record),
        })

    return events


def _event_summary(event_type: str, record: dict) -> str:
    """Build a human-readable one-line summary for an event."""
    terminal = record.get("terminal") or "?"
    dispatch_id = record.get("dispatch_id") or ""
    short_dispatch = dispatch_id.split("-", 2)[-1][:30] if dispatch_id else ""
    gate = record.get("gate") or ""
    status = record.get("status") or ""

    if event_type == "gate_passed":
        return f"{gate} passed"
    if event_type == "gate_failed":
        return f"{gate} failed"
    if event_type == "receipt_complete":
        return f"Receipt: {short_dispatch} ({status})"
    if event_type == "task_started":
        return f"Task started: {short_dispatch}"
    if event_type == "context_pressure":
        pct = record.get("context_used_pct", "?")
        return f"Context pressure {pct}% on {terminal}"
    if event_type in ("dispatch_created", "dispatch_promoted"):
        return f"Dispatch {short_dispatch}"
    return short_dispatch or event_type


def _scan_dispatch_events() -> list[dict]:
    """Scan dispatch directories for lifecycle events based on file timestamps."""
    events: list[dict] = []
    if not DISPATCH_DIR.exists():
        return events

    for stage in DISPATCH_STAGES:
        stage_dir = DISPATCH_DIR / stage
        if not stage_dir.is_dir():
            continue
        for dispatch_file in stage_dir.iterdir():
            if not dispatch_file.name.endswith(".md"):
                continue
            try:
                mtime = dispatch_file.stat().st_mtime
                ts = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            except OSError:
                continue

            dispatch_id = dispatch_file.stem
            pr_id = ""
            try:
                content = dispatch_file.read_text(encoding="utf-8")
                for line in content.splitlines()[:20]:
                    if line.startswith("PR-ID:"):
                        pr_id = line.split(":", 1)[1].strip()
                        break
            except OSError:
                pass

            if stage == "staging":
                event_type = "dispatch_created"
            elif stage in ("pending", "active"):
                event_type = "dispatch_promoted"
            elif stage == "completed":
                event_type = "dispatch_promoted"
            elif stage == "rejected":
                event_type = "dispatch_promoted"
            else:
                event_type = "dispatch_created"

            icon = _EVENT_TYPE_ICONS.get(event_type, "dispatch")
            summary = f"Dispatch moved to {stage}"

            events.append({
                "type": event_type,
                "icon": icon,
                "timestamp": ts,
                "terminal": "",
                "dispatch_id": dispatch_id,
                "pr_id": pr_id,
                "gate": "",
                "status": stage,
                "summary": summary,
            })

    return events


def _query_events(params: dict[str, list[str]]) -> dict:
    """Combine receipt events + dispatch events, filter, sort, and cap at 30."""
    terminal_filter = (params.get("terminal") or [None])[0]
    pr_filter = (params.get("pr") or [None])[0]
    type_filter = (params.get("type") or [None])[0]
    limit = 30

    receipt_events = _parse_receipts_events(limit=80)
    dispatch_events = _scan_dispatch_events()

    all_events = receipt_events + dispatch_events

    # Deduplicate: receipt events take priority over dispatch scan for same dispatch_id
    seen_dispatches: dict[str, dict] = {}
    unique_events: list[dict] = []
    for evt in all_events:
        did = evt.get("dispatch_id")
        if did and did in seen_dispatches:
            existing = seen_dispatches[did]
            if evt.get("type") != existing.get("type"):
                unique_events.append(evt)
        else:
            if did:
                seen_dispatches[did] = evt
            unique_events.append(evt)

    # Apply filters
    filtered: list[dict] = []
    for evt in unique_events:
        if terminal_filter and evt.get("terminal") != terminal_filter:
            continue
        if pr_filter and evt.get("pr_id") != pr_filter:
            continue
        if type_filter and evt.get("type") != type_filter:
            continue
        filtered.append(evt)

    # Sort by timestamp descending (most recent first)
    filtered.sort(key=lambda e: e.get("timestamp") or "", reverse=True)

    return {
        "events": filtered[:limit],
        "total": len(filtered),
        "filters": {
            "terminal": terminal_filter,
            "pr": pr_filter,
            "type": type_filter,
        },
    }


# ---------- Token Stats API ----------

_GROUP_SQL = {
    "day": "session_date",
    "week": "strftime('%Y-W%W', session_date)",
    "month": "strftime('%Y-%m', session_date)",
}


def _get_db() -> sqlite3.Connection | None:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return None
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _query_token_stats(params: dict[str, list[str]]) -> list[dict]:
    conn = _get_db()
    if conn is None:
        return []

    date_from = (params.get("from") or [None])[0]
    date_to = (params.get("to") or [None])[0]
    group = (params.get("group") or ["day"])[0]
    terminal = (params.get("terminal") or [None])[0]
    model = (params.get("model") or [None])[0]

    if group not in _GROUP_SQL:
        group = "day"

    group_expr = _GROUP_SQL[group]
    today = datetime.now().strftime("%Y-%m-%d")
    if not date_from:
        date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    if not date_to:
        date_to = today

    conditions = ["session_date >= ?", "session_date <= ?"]
    bind = [date_from, date_to]

    if terminal:
        conditions.append("terminal = ?")
        bind.append(terminal)
    if model:
        conditions.append("session_model = ?")
        bind.append(model)

    where = " AND ".join(conditions)

    sql = f"""
        SELECT
            {group_expr} AS period,
            terminal,
            session_model AS model,
            COUNT(*) AS sessions,
            SUM(assistant_message_count) AS api_calls,
            ROUND(AVG(
                (total_input_tokens + cache_creation_tokens + cache_read_tokens) * 1.0
                / NULLIF(assistant_message_count, 0)
            ) / 1000.0, 0) AS context_per_call_K,
            ROUND(
                SUM(cache_read_tokens) * 100.0
                / NULLIF(SUM(total_input_tokens + cache_creation_tokens + cache_read_tokens), 0), 1
            ) AS cache_hit_pct,
            ROUND(AVG(
                (total_input_tokens + cache_creation_tokens) * 1.0
                / NULLIF(assistant_message_count, 0)
            ) / 1000.0, 1) AS new_per_call_K,
            ROUND(AVG(
                total_output_tokens * 1.0
                / NULLIF(assistant_message_count, 0)
            ) / 1000.0, 1) AS output_per_call_K,
            SUM(total_output_tokens) AS total_output_tokens,
            SUM(total_input_tokens) AS total_input_tokens,
            SUM(cache_creation_tokens) AS total_cache_creation_tokens,
            SUM(cache_read_tokens) AS total_cache_read_tokens,
            SUM(COALESCE(context_reset_count, CASE WHEN has_context_reset THEN 1 ELSE 0 END)) AS context_rotations,
            GROUP_CONCAT(DISTINCT primary_activity) AS activities
        FROM session_analytics
        WHERE {where}
        GROUP BY period, terminal, model
        ORDER BY period DESC, terminal
    """

    try:
        rows = conn.execute(sql, bind).fetchall()
        result = [dict(r) for r in rows]
    finally:
        conn.close()
    return result


def _query_token_sessions(params: dict[str, list[str]]) -> list[dict]:
    conn = _get_db()
    if conn is None:
        return []

    date = (params.get("date") or [None])[0]
    terminal = (params.get("terminal") or [None])[0]

    if not date:
        return []

    conditions = ["session_date = ?"]
    bind: list[str] = [date]

    if terminal:
        conditions.append("terminal = ?")
        bind.append(terminal)

    where = " AND ".join(conditions)

    sql = f"""
        SELECT
            session_id,
            terminal,
            session_model AS model,
            session_date AS date,
            assistant_message_count AS api_calls,
            ROUND(
                (total_input_tokens + cache_creation_tokens + cache_read_tokens) * 1.0
                / NULLIF(assistant_message_count, 0) / 1000.0, 0
            ) AS context_per_call_K,
            ROUND(
                cache_read_tokens * 100.0
                / NULLIF(total_input_tokens + cache_creation_tokens + cache_read_tokens, 0), 1
            ) AS cache_hit_pct,
            ROUND(
                total_output_tokens * 1.0
                / NULLIF(assistant_message_count, 0) / 1000.0, 1
            ) AS output_per_call_K,
            duration_minutes,
            primary_activity,
            tool_calls_total,
            has_error_recovery
        FROM session_analytics
        WHERE {where}
        ORDER BY assistant_message_count DESC
    """

    try:
        rows = conn.execute(sql, bind).fetchall()
        result = [dict(r) for r in rows]
    finally:
        conn.close()
    return result


# ---------- Dispatch Kanban API ----------

_DIR_TO_STAGE: dict[str, str] = {
    "staging": "staging",
    "pending": "pending",
    "queue": "pending",
    "active": "active",
    "completed": "done",
    "rejected": "done",
}


def _parse_dispatch_header(text: str) -> dict[str, str]:
    """Extract key-value metadata from a dispatch markdown header block."""
    header: dict[str, str] = {}
    past_target = False
    started = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[[TARGET:"):
            past_target = True
            continue
        if not past_target:
            continue
        if stripped in ("", "Manager Block"):
            if started:
                break
            continue
        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key_norm = key.strip().lower().replace("-", "_").replace(" ", "_")
            if key_norm in ("context", "instruction"):
                break
            header[key_norm] = val.strip()
            started = True
        elif started:
            break
    return header


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _scan_receipts() -> dict[str, dict]:
    """Return dispatch_id → receipt metadata from unified_reports/."""
    receipts: dict[str, dict] = {}
    if not REPORTS_DIR.exists():
        return receipts
    for path in REPORTS_DIR.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            rec: dict[str, str] = {}
            for line in text.splitlines()[:20]:
                if line.startswith("**Dispatch ID**:"):
                    rec["dispatch_id"] = line.split(":", 1)[1].strip()
                elif line.startswith("**PR**:"):
                    rec["pr"] = line.split(":", 1)[1].strip()
                elif line.startswith("**Status**:"):
                    rec["status"] = line.split(":", 1)[1].strip()
                elif line.startswith("**Gate**:"):
                    rec["gate"] = line.split(":", 1)[1].strip()
            if "dispatch_id" in rec:
                rec["report_file"] = path.name
                receipts[rec["dispatch_id"]] = rec
        except Exception:
            pass
    return receipts


def _scan_dispatches() -> dict:
    """Scan dispatch directories and return dispatches grouped by Kanban stage."""
    receipts = _scan_receipts()
    stages: dict[str, list] = {s: [] for s in ["staging", "pending", "active", "review", "done"]}

    if not DISPATCHES_DIR.exists():
        return {"stages": stages, "total": 0}

    now = datetime.now(timezone.utc).timestamp()

    for dir_name, base_stage in _DIR_TO_STAGE.items():
        dir_path = DISPATCHES_DIR / dir_name
        if not dir_path.exists():
            continue
        for path in sorted(dir_path.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                header = _parse_dispatch_header(text)
                duration_secs = now - path.stat().st_mtime
                dispatch_id = header.get("dispatch_id", path.stem)
                receipt = receipts.get(dispatch_id)

                # Promote active dispatches with a filed receipt to "review"
                stage = "review" if base_stage == "active" and receipt else base_stage

                stages[stage].append({
                    "id": dispatch_id,
                    "file": path.name,
                    "pr_id": header.get("pr_id", "—"),
                    "track": header.get("track", "—"),
                    "terminal": header.get("terminal", "—"),
                    "role": header.get("role", "—"),
                    "gate": header.get("gate", "—"),
                    "priority": header.get("priority", "—"),
                    "status": header.get("status", "—"),
                    "reason": header.get("reason", "—"),
                    "dir": dir_name,
                    "stage": stage,
                    "duration_secs": int(duration_secs),
                    "duration_label": _format_duration(duration_secs),
                    "has_receipt": receipt is not None,
                    "receipt_status": receipt.get("status") if receipt else None,
                })
            except Exception as exc:
                import sys
                print(f"[kanban] skipping {path.name}: {exc}", file=sys.stderr)

    total = sum(len(v) for v in stages.values())
    return {"stages": stages, "total": total}


# ---------- Conversations API ----------

CLAUDE_INDEX_DB = Path.home() / ".claude" / "conversation-index.db"


def _query_conversations(params: dict[str, list[str]]) -> dict:
    """Query conversation sessions via the read model (PR-2)."""
    from conversation_read_model import ConversationReadModel

    db_path = str(CLAUDE_INDEX_DB)
    if not CLAUDE_INDEX_DB.exists():
        return {"sessions": [], "sort_order": "DESC", "total": 0}

    sort_order = (params.get("sort") or ["DESC"])[0].upper()
    if sort_order not in ("DESC", "ASC"):
        sort_order = "DESC"

    project_filter = (params.get("project") or [None])[0]
    worktree_filter = (params.get("worktree") or [None])[0]
    terminal_filter = (params.get("terminal") or [None])[0]
    limit = int((params.get("limit") or ["50"])[0])
    group_by_wt = (params.get("group") or [None])[0] == "worktree"

    # Discover worktree roots from known project paths
    worktree_roots: list[str] = []
    project_root = str(PROJECT_ROOT)
    worktree_roots.append(project_root)

    # Add any sibling worktrees (same parent dir, same base name pattern)
    parent = PROJECT_ROOT.parent
    base = PROJECT_ROOT.name.split("-wt")[0] if "-wt" in PROJECT_ROOT.name else PROJECT_ROOT.name
    for sibling in parent.iterdir():
        if sibling.is_dir() and sibling.name.startswith(base):
            worktree_roots.append(str(sibling))

    model = ConversationReadModel(
        claude_index_db=db_path,
        worktree_roots=worktree_roots,
        receipt_path=str(RECEIPTS_PATH),
    )

    sessions = model.list_sessions(
        project_filter=project_filter,
        worktree_filter=worktree_filter,
        terminal_filter=terminal_filter,
        sort_order=sort_order,
        limit=limit,
    )

    session_dicts = [
        {
            "session_id": s.session_id,
            "project_path": s.project_path,
            "cwd": s.cwd,
            "last_message": s.last_message,
            "title": s.title,
            "message_count": s.message_count,
            "user_message_count": s.user_message_count,
            "total_tokens": s.total_tokens,
            "terminal": s.terminal,
            "worktree_root": s.worktree_root,
            "worktree_exists": s.worktree_exists,
        }
        for s in sessions
    ]

    result: dict = {
        "sessions": session_dicts,
        "sort_order": sort_order,
        "total": len(session_dicts),
    }

    if group_by_wt:
        groups = model.group_by_worktree(sessions)
        result["worktree_groups"] = [
            {
                "worktree_root": g.worktree_root,
                "worktree_exists": g.worktree_exists,
                "session_ids": [s.session_id for s in g.sessions],
            }
            for g in groups
        ]

    # Include rotation chains
    chains = model.discover_rotation_chains(sessions)
    if chains:
        result["rotation_chains"] = [
            {
                "dispatch_id": c.dispatch_id,
                "chain_depth": c.chain_depth,
                "latest_message": c.latest_message,
                "session_ids": [s.session_id for s in c.sessions],
            }
            for c in chains
        ]

    return result


def _resume_conversation(data: dict) -> dict:
    """Validate and build a resume command for a conversation session (PR-3)."""
    from conversation_read_model import ConversationReadModel
    from conversation_resume import resume_conversation

    session_id = data.get("session_id", "")
    if not session_id:
        return {"ok": False, "error": "missing_session_id", "message": "session_id is required", "session_id": ""}

    operator_cwd = data.get("cwd", str(PROJECT_ROOT))
    force = bool(data.get("force", False))

    db_path = str(CLAUDE_INDEX_DB)
    if not CLAUDE_INDEX_DB.exists():
        return {"ok": False, "error": "session_not_found", "message": "Conversation index not found", "session_id": session_id}

    # Discover worktree roots
    worktree_roots: list[str] = [str(PROJECT_ROOT)]
    parent = PROJECT_ROOT.parent
    base = PROJECT_ROOT.name.split("-wt")[0] if "-wt" in PROJECT_ROOT.name else PROJECT_ROOT.name
    for sibling in parent.iterdir():
        if sibling.is_dir() and sibling.name.startswith(base):
            worktree_roots.append(str(sibling))

    model = ConversationReadModel(
        claude_index_db=db_path,
        worktree_roots=worktree_roots,
        receipt_path=str(RECEIPTS_PATH),
    )

    result = resume_conversation(
        session_id=session_id,
        model=model,
        operator_cwd=operator_cwd,
        worktree_roots=worktree_roots,
        force=force,
    )
    return result.to_dict()


def _json_response(handler: "DashboardHandler", status: HTTPStatus, payload_obj: dict) -> None:
    payload = json.dumps(payload_obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(payload)


def _unlock_terminal(terminal_id: str) -> dict:
    if terminal_id not in TERMINAL_TRACK_MAP:
        raise ValueError(f"Unknown terminal: {terminal_id}")

    now = datetime.now(timezone.utc).isoformat()
    terminal_shadow_script = SCRIPTS_DIR / "terminal_state_shadow.py"
    progress_update_script = SCRIPTS_DIR / "update_progress_state.py"

    shadow_result = subprocess.run(
        [
            "python3",
            str(terminal_shadow_script),
            "--terminal-id",
            terminal_id,
            "--status",
            "idle",
            "--clear-claim",
            "--last-activity",
            now,
            "--state-dir",
            str(CANONICAL_STATE_DIR),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    track = TERMINAL_TRACK_MAP[terminal_id]
    subprocess.run(
        [
            "python3",
            str(progress_update_script),
            "--track",
            track,
            "--status",
            "idle",
            "--dispatch-id",
            "",
            "--updated-by",
            "dashboard_unlock",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    shadow_record = {}
    stdout = shadow_result.stdout.strip()
    if stdout:
        with contextlib.suppress(Exception):
            shadow_record = json.loads(stdout)

    # Force-refresh dashboard_status.json so the next UI refresh sees the update
    dashboard_update_script = SCRIPTS_DIR / "update_dashboard_status.sh"
    if dashboard_update_script.exists():
        with contextlib.suppress(Exception):
            subprocess.run(
                ["bash", str(dashboard_update_script)],
                cwd=str(SCRIPTS_DIR),
                capture_output=True,
                timeout=5,
            )

    return {
        "status": "ok",
        "terminal": terminal_id,
        "track": track,
        "unlocked_at": now,
        "terminal_state": shadow_record,
    }


def _jump_terminal(terminal_id: str) -> dict:
    """Switch tmux focus to the specified terminal's pane."""
    if terminal_id not in VALID_TERMINALS:
        raise ValueError(f"Unknown terminal: {terminal_id}")

    session_name = f"vnx-{PROJECT_ROOT.name}"

    # Check session exists
    check = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    if check.returncode != 0:
        raise RuntimeError(f"VNX session '{session_name}' not found — is VNX running?")

    # Resolve pane_id from panes.json
    pane_id = ""
    panes_file = CANONICAL_STATE_DIR / "panes.json"
    if panes_file.exists():
        with contextlib.suppress(Exception):
            panes_data = json.loads(panes_file.read_text(encoding="utf-8"))
            entry = panes_data.get(terminal_id) or {}
            pane_id = str(entry.get("pane_id") or "")

    # Fall back to positional index if pane_id not in panes.json
    pane_index_map = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
    pane_index = pane_index_map[terminal_id]

    # Select window first
    subprocess.run(
        ["tmux", "select-window", "-t", f"{session_name}:0"],
        check=True,
        capture_output=True,
    )

    # Select pane by ID (preferred) or by positional index (fallback)
    if pane_id:
        subprocess.run(
            ["tmux", "select-pane", "-t", pane_id],
            check=True,
            capture_output=True,
        )
        resolved_pane = pane_id
    else:
        subprocess.run(
            ["tmux", "select-pane", "-t", f"{session_name}:0.{pane_index}"],
            check=True,
            capture_output=True,
        )
        resolved_pane = f"index:{pane_index}"

    return {
        "status": "ok",
        "terminal": terminal_id,
        "pane": resolved_pane,
        "session": session_name,
    }


# ---------- Operator Dashboard API ----------

def _operator_get_projects() -> dict:
    """GET /api/operator/projects — cross-project overview via ProjectsView."""
    try:
        from dashboard_read_model import ProjectsView
        view = ProjectsView()
        envelope = view.list_projects()
        return envelope.to_dict()
    except Exception as exc:
        return {"view": "ProjectsView", "degraded": True, "degraded_reasons": [str(exc)], "data": []}


def _operator_get_session(params: dict) -> dict:
    """GET /api/operator/session — per-project session state via SessionView."""
    project_path = (params.get("project_path") or [None])[0]
    if not project_path:
        state_dir = CANONICAL_STATE_DIR
    else:
        state_dir = Path(project_path) / ".vnx-data" / "state"
    try:
        from dashboard_read_model import SessionView
        view = SessionView(state_dir)
        envelope = view.get_session()
        return envelope.to_dict()
    except Exception as exc:
        return {"view": "SessionView", "degraded": True, "degraded_reasons": [str(exc)], "data": {}}


def _operator_get_terminals() -> dict:
    """GET /api/operator/terminals — all terminal health via TerminalView."""
    try:
        from dashboard_read_model import TerminalView
        view = TerminalView(CANONICAL_STATE_DIR)
        envelope = view.get_all_terminals()
        return envelope.to_dict()
    except Exception as exc:
        return {"view": "TerminalView", "degraded": True, "degraded_reasons": [str(exc)], "data": []}


def _operator_get_terminal(terminal_id: str) -> dict:
    """GET /api/operator/terminal/<id> — single terminal health."""
    try:
        from dashboard_read_model import TerminalView
        view = TerminalView(CANONICAL_STATE_DIR)
        envelope = view.get_terminal(terminal_id)
        return envelope.to_dict()
    except Exception as exc:
        return {"view": "TerminalView", "degraded": True, "degraded_reasons": [str(exc)], "data": {"terminal_id": terminal_id}}


def _operator_get_open_items(params: dict) -> dict:
    """GET /api/operator/open-items — per-project open items."""
    project_path = (params.get("project_path") or [None])[0]
    severity = (params.get("severity") or [None])[0]
    include_resolved = (params.get("include_resolved") or ["false"])[0].lower() == "true"

    if not project_path:
        state_dir = CANONICAL_STATE_DIR
    else:
        state_dir = Path(project_path) / ".vnx-data" / "state"
    try:
        from dashboard_read_model import OpenItemsView
        view = OpenItemsView(state_dir)
        envelope = view.get_items(severity=severity, include_resolved=include_resolved)
        return envelope.to_dict()
    except Exception as exc:
        return {"view": "OpenItemsView", "degraded": True, "degraded_reasons": [str(exc)], "data": {"items": [], "summary": {}}}


def _operator_get_open_items_aggregate(params: dict) -> dict:
    """GET /api/operator/open-items/aggregate — cross-project open items."""
    project_filter = (params.get("project") or [None])[0]
    try:
        from dashboard_read_model import AggregateOpenItemsView
        view = AggregateOpenItemsView()
        envelope = view.get_aggregate(project_filter=project_filter)
        return envelope.to_dict()
    except Exception as exc:
        return {"view": "AggregateOpenItemsView", "degraded": True, "degraded_reasons": [str(exc)], "data": {"items": [], "per_project_subtotals": {}, "total_summary": {}}}


def _operator_post_action(action: str, body: dict) -> tuple[dict, int]:
    """Dispatch operator control actions. Returns (response_dict, http_status_int)."""
    try:
        from dashboard_actions import (
            start_session, stop_session, attach_terminal,
            refresh_projections, run_reconciliation, inspect_open_item,
        )
    except ImportError as exc:
        return {"action": action, "status": "failed", "message": f"dashboard_actions unavailable: {exc}"}, 503

    project_path = body.get("project_path", "")
    dry_run = bool(body.get("dry_run", False))

    if action == "session/start":
        outcome = start_session(project_path, dry_run=dry_run)
    elif action == "session/stop":
        outcome = stop_session(project_path, dry_run=dry_run)
    elif action == "terminal/attach":
        terminal_id = body.get("terminal_id", "")
        outcome = attach_terminal(project_path, terminal_id, dry_run=dry_run)
    elif action == "projections/refresh":
        outcome = refresh_projections(project_path, dry_run=dry_run)
    elif action == "reconcile":
        outcome = run_reconciliation(project_path, dry_run=dry_run)
    elif action == "open-item/inspect":
        item_id = body.get("item_id", "")
        outcome = inspect_open_item(project_path, item_id)
    else:
        return {"action": action, "status": "failed", "message": f"Unknown action: {action}"}, 400

    result = outcome.to_dict()
    status_code = 200 if outcome.status in ("success", "already_active", "degraded") else 422
    return result, status_code


class DashboardHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        """
        Serve `/state/*` from canonical state first, with legacy fallback.
        Keeps dashboard UI stable while state ownership moved to `.vnx-data/state`.
        """
        parsed_path = unquote(urlsplit(path).path)
        if parsed_path.startswith("/state/"):
            rel = parsed_path[len("/state/") :]
            rel_parts = [part for part in Path(rel).parts if part not in ("", ".", "..")]
            canonical_path = CANONICAL_STATE_DIR.joinpath(*rel_parts)
            if canonical_path.exists():
                return str(canonical_path)
            return str(LEGACY_STATE_DIR.joinpath(*rel_parts))
        return super().translate_path(path)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)

        if path == "/api/events":
            try:
                result = _query_events(params)
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "events": []})
                return
            _json_response(self, HTTPStatus.OK, result)
            return

        if path == "/api/token-stats":
            result = _query_token_stats(params)
            _json_response(self, HTTPStatus.OK, {"data": result, "count": len(result)})
            return

        if path == "/api/token-stats/sessions":
            result = _query_token_sessions(params)
            _json_response(self, HTTPStatus.OK, {"data": result, "count": len(result)})
            return

        if path == "/api/conversations":
            try:
                result = _query_conversations(params)
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "sessions": []})
                return
            _json_response(self, HTTPStatus.OK, result)
            return

        if path == "/api/dispatches":
            try:
                result = _scan_dispatches()
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            _json_response(self, HTTPStatus.OK, result)
            return

        # Operator Dashboard API
        if path == "/api/operator/projects":
            _json_response(self, HTTPStatus.OK, _operator_get_projects())
            return

        if path == "/api/operator/session":
            _json_response(self, HTTPStatus.OK, _operator_get_session(params))
            return

        if path == "/api/operator/terminals":
            _json_response(self, HTTPStatus.OK, _operator_get_terminals())
            return

        if path.startswith("/api/operator/terminal/"):
            tid = path[len("/api/operator/terminal/"):]
            _json_response(self, HTTPStatus.OK, _operator_get_terminal(tid))
            return

        if path == "/api/operator/open-items/aggregate":
            _json_response(self, HTTPStatus.OK, _operator_get_open_items_aggregate(params))
            return

        if path == "/api/operator/open-items":
            _json_response(self, HTTPStatus.OK, _operator_get_open_items(params))
            return

        # Fall through to static file serving
        super().do_GET()

    def end_headers(self) -> None:
        """Add no-cache headers for JSON state files to ensure live updates."""
        if self.path and (".json" in self.path):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def do_POST(self) -> None:
        parsed_path = unquote(urlsplit(self.path).path)

        # /api/jump/{terminal} — switch tmux focus to terminal
        if parsed_path.startswith("/api/jump/"):
            terminal_id = parsed_path[len("/api/jump/"):]
            if terminal_id not in VALID_TERMINALS:
                self.send_error(HTTPStatus.BAD_REQUEST, f"Unknown terminal: {terminal_id}")
                return
            try:
                response = _jump_terminal(terminal_id)
            except RuntimeError as exc:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
                return
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b"").decode(errors="replace").strip()
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"tmux error: {stderr or exc}")
                return
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Jump failed: {exc}")
                return
            _json_response(self, HTTPStatus.OK, response)
            return

        # /api/conversations/resume — validate and return resume command
        if parsed_path == "/api/conversations/resume":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
                return
            try:
                result = _resume_conversation(data)
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc), "session_id": data.get("session_id", "")})
                return
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
            _json_response(self, status, result)
            return

        # Operator control actions
        _OPERATOR_ACTIONS = {
            "/api/operator/session/start": "session/start",
            "/api/operator/session/stop": "session/stop",
            "/api/operator/terminal/attach": "terminal/attach",
            "/api/operator/projections/refresh": "projections/refresh",
            "/api/operator/reconcile": "reconcile",
            "/api/operator/open-item/inspect": "open-item/inspect",
        }
        if parsed_path in _OPERATOR_ACTIONS:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body_bytes = self.rfile.read(length) if length else b"{}"
            try:
                body_data = json.loads(body_bytes.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
                return
            result, status_int = _operator_post_action(_OPERATOR_ACTIONS[parsed_path], body_data)
            _json_response(self, HTTPStatus(status_int), result)
            return

        if parsed_path not in ("/api/restart-process", "/api/unlock-terminal"):
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
            return

        if parsed_path == "/api/unlock-terminal":
            terminal_id = data.get("terminal")
            if terminal_id not in TERMINAL_TRACK_MAP:
                self.send_error(HTTPStatus.BAD_REQUEST, f"Unknown terminal: {terminal_id}")
                return
            try:
                response = _unlock_terminal(terminal_id)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unlock failed: {stderr or exc}")
                return
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unlock failed: {exc}")
                return
            _json_response(self, HTTPStatus.OK, response)
            return

        process_name = data.get("process")
        if process_name not in PROCESS_COMMANDS:
            self.send_error(HTTPStatus.BAD_REQUEST, f"Unknown process: {process_name}")
            return

        kill_pattern = PROCESS_KILL_PATTERNS.get(process_name, process_name)
        subprocess.run(["pkill", "-f", kill_pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / f"{process_name}.log"
        log_handle = open(log_path, "ab", buffering=0)

        try:
            subprocess.Popen(
                PROCESS_COMMANDS[process_name],
                cwd=str(SCRIPTS_DIR),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
        except Exception as exc:
            log_handle.close()
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to start: {exc}")
            return

        response = {"status": "ok", "process": process_name}
        _json_response(self, HTTPStatus.OK, response)


def main() -> None:
    port = int(os.environ.get("PORT", "4173"))

    # Serve from `.claude/vnx-system` regardless of where the script is launched from.
    service_dir = Path(__file__).resolve().parents[1]
    handler = partial(DashboardHandler, directory=str(service_dir))

    server = DualStackHTTPServer(("::", port), handler)
    print(
        f"Serving dashboard from {service_dir} on http://localhost:{port} (dashboard at /dashboard/index.html)",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
