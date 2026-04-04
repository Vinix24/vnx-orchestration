"""Operator dashboard API handlers.

Extracted from serve_dashboard.py to keep module size manageable.
Covers: projects, sessions, terminals, open-items, kanban, gate-config,
governance-digest, health, terminal unlock/jump, conversations.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

# Compute paths locally to avoid circular imports with serve_dashboard.
VNX_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = VNX_DIR.parents[1]
SCRIPTS_DIR = VNX_DIR / "scripts"
CANONICAL_STATE_DIR = Path(os.environ.get("VNX_STATE_DIR", str(PROJECT_ROOT / ".vnx-data" / "state")))
_VNX_DATA_DIR = CANONICAL_STATE_DIR.parent
DISPATCHES_DIR = _VNX_DATA_DIR / "dispatches"
REPORTS_DIR = _VNX_DATA_DIR / "unified_reports"
DISPATCH_DIR = Path(os.environ.get("VNX_DISPATCH_DIR", str(PROJECT_ROOT / ".vnx-data" / "dispatches")))
RECEIPTS_PATH = CANONICAL_STATE_DIR / "t0_receipts.ndjson"
DB_PATH = CANONICAL_STATE_DIR / "quality_intelligence.db"
GATE_CONFIG_PATH = VNX_DIR / "configs" / "governance_gates.yaml"
TERMINAL_TRACK_MAP = {"T1": "A", "T2": "B", "T3": "C"}
VALID_TERMINALS = frozenset({"T0", "T1", "T2", "T3"})

_gate_config_lock = threading.Lock()


# ---------- Dispatch Kanban helpers ----------

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
    """Return dispatch_id -> receipt metadata from unified_reports/."""
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
                    "pr_id": header.get("pr_id", "\u2014"),
                    "track": header.get("track", "\u2014"),
                    "terminal": header.get("terminal", "\u2014"),
                    "role": header.get("role", "\u2014"),
                    "gate": header.get("gate", "\u2014"),
                    "priority": header.get("priority", "\u2014"),
                    "status": header.get("status", "\u2014"),
                    "reason": header.get("reason", "\u2014"),
                    "dir": dir_name,
                    "stage": stage,
                    "duration_secs": int(duration_secs),
                    "duration_label": _format_duration(duration_secs),
                    "has_receipt": receipt is not None,
                    "receipt_status": receipt.get("status") if receipt else None,
                })
            except Exception as exc:
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


# ---------- Terminal control ----------

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
        raise RuntimeError(f"VNX session '{session_name}' not found \u2014 is VNX running?")

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
    """GET /api/operator/projects -- cross-project overview via ProjectsView."""
    try:
        from dashboard_read_model import ProjectsView
        view = ProjectsView()
        envelope = view.list_projects()
        return envelope.to_dict()
    except Exception as exc:
        return {"view": "ProjectsView", "degraded": True, "degraded_reasons": [str(exc)], "data": []}


def _operator_get_session(params: dict) -> dict:
    """GET /api/operator/session -- per-project session state via SessionView."""
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
    """GET /api/operator/terminals -- all terminal health via TerminalView."""
    try:
        from dashboard_read_model import TerminalView
        view = TerminalView(CANONICAL_STATE_DIR)
        envelope = view.get_all_terminals()
        return envelope.to_dict()
    except Exception as exc:
        return {"view": "TerminalView", "degraded": True, "degraded_reasons": [str(exc)], "data": []}


def _operator_get_terminal(terminal_id: str) -> dict:
    """GET /api/operator/terminal/<id> -- single terminal health."""
    try:
        from dashboard_read_model import TerminalView
        view = TerminalView(CANONICAL_STATE_DIR)
        envelope = view.get_terminal(terminal_id)
        return envelope.to_dict()
    except Exception as exc:
        return {"view": "TerminalView", "degraded": True, "degraded_reasons": [str(exc)], "data": {"terminal_id": terminal_id}}


def _operator_get_open_items(params: dict) -> dict:
    """GET /api/operator/open-items -- per-project open items."""
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
    """GET /api/operator/open-items/aggregate -- cross-project open items."""
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


# ---------- Health endpoint ----------

def _api_health() -> dict:
    """GET /api/health -- server status, uptime, and data source availability."""
    import serve_dashboard as _sd
    now = datetime.now(timezone.utc)
    uptime_seconds = round((now - _sd._SERVER_START_TIME).total_seconds(), 1)

    sources = {
        "receipts":    _sd.RECEIPTS_PATH.exists(),
        "dispatches":  _sd.DISPATCHES_DIR.exists(),
        "reports":     _sd.REPORTS_DIR.exists(),
        "state_dir":   _sd.CANONICAL_STATE_DIR.exists(),
        "quality_db":  _sd.DB_PATH.exists(),
    }

    return {
        "status": "ok",
        "uptime_seconds": uptime_seconds,
        "server_start": _sd._SERVER_START_TIME.isoformat(),
        "queried_at": now.isoformat(),
        "data_sources": {name: "available" if ok else "unavailable" for name, ok in sources.items()},
        "all_sources_available": all(sources.values()),
    }


def _operator_get_kanban() -> dict:
    """GET /api/operator/kanban -- dispatch cards grouped by Kanban stage."""
    try:
        return _scan_dispatches()
    except Exception as exc:
        return {"stages": {}, "total": 0, "degraded": True, "degraded_reasons": [str(exc)]}


# ---------- Gate Config API ----------

def _read_gate_config(path: Path | None = None) -> dict:
    """Read governance_gates.yaml; return empty gates dict on missing/invalid file."""
    cfg_path = path or GATE_CONFIG_PATH
    if not _YAML_AVAILABLE:
        return {"gates": {}}
    if not cfg_path.exists():
        return {"gates": {}}
    try:
        raw = cfg_path.read_text(encoding="utf-8")
        data = _yaml.safe_load(raw) or {}
        if not isinstance(data.get("gates"), dict):
            data["gates"] = {}
        return data
    except Exception:
        return {"gates": {}}


def _write_gate_config(config: dict, path: Path | None = None) -> None:
    """Atomically write gate config YAML under a threading lock."""
    cfg_path = path or GATE_CONFIG_PATH
    if not _YAML_AVAILABLE:
        raise RuntimeError("PyYAML not available \u2014 cannot persist gate config")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(".yaml.tmp")
    tmp.write_text(
        "# Governance gate configuration\n"
        "# Managed by POST /api/operator/gate/toggle\n"
        + _yaml.dump(config, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(cfg_path)


def _operator_get_gate_config(params: dict, config_path: Path | None = None) -> dict:
    """GET /api/operator/gate/config -- per-project gate enabled/disabled state."""
    project = (params.get("project") or [None])[0]
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _gate_config_lock:
            data = _read_gate_config(config_path)
        gates_root: dict = data.get("gates", {})
        if project is not None:
            gates = {
                gate: entry
                for gate, entry in gates_root.get(project, {}).items()
            }
        else:
            gates = gates_root
        return {
            "project": project,
            "gates": gates,
            "queried_at": now,
            "config_path": str(config_path or GATE_CONFIG_PATH),
        }
    except Exception as exc:
        return {
            "project": project,
            "gates": {},
            "queried_at": now,
            "config_path": str(config_path or GATE_CONFIG_PATH),
            "error": str(exc),
        }


def _operator_post_gate_toggle(body: dict, config_path: Path | None = None) -> tuple[dict, int]:
    """POST /api/operator/gate/toggle -- enable or disable a gate for a project."""
    project = body.get("project", "")
    gate = body.get("gate", "")
    enabled = body.get("enabled")
    now = datetime.now(timezone.utc).isoformat()

    if not project or not isinstance(project, str):
        return {"status": "failed", "message": "project is required", "timestamp": now}, 400
    if not gate or not isinstance(gate, str):
        return {"status": "failed", "message": "gate is required", "timestamp": now}, 400
    if not isinstance(enabled, bool):
        return {"status": "failed", "message": "enabled must be a boolean", "timestamp": now}, 400

    if not _YAML_AVAILABLE:
        return {"status": "failed", "message": "PyYAML not available", "timestamp": now}, 503

    try:
        with _gate_config_lock:
            data = _read_gate_config(config_path)
            if "gates" not in data or not isinstance(data["gates"], dict):
                data["gates"] = {}
            if project not in data["gates"]:
                data["gates"][project] = {}
            data["gates"][project][gate] = {"enabled": enabled}
            _write_gate_config(data, config_path)
        return {
            "action": "gate/toggle",
            "project": project,
            "gate": gate,
            "enabled": enabled,
            "status": "success",
            "message": f"Gate {gate!r} for project {project!r} set to {'enabled' if enabled else 'disabled'}",
            "timestamp": now,
        }, 200
    except Exception as exc:
        return {
            "action": "gate/toggle",
            "project": project,
            "gate": gate,
            "enabled": enabled,
            "status": "failed",
            "message": str(exc),
            "timestamp": now,
        }, 500


# ---------- Governance Digest API ----------

_DIGEST_STALE_THRESHOLD = 600  # seconds -- digest older than 10 min is degraded


def _operator_get_governance_digest(digest_path: Path | None = None) -> dict:
    """GET /api/operator/governance-digest -- governance digest with freshness envelope."""
    now = datetime.now(timezone.utc)
    path = digest_path or (CANONICAL_STATE_DIR / "governance_digest.json")

    # Compute source freshness
    mtime_iso: str | None = None
    staleness: float | None = None
    try:
        mt = path.stat().st_mtime
        mtime_iso = datetime.fromtimestamp(mt, tz=timezone.utc).isoformat()
        ts = datetime.fromisoformat(mtime_iso)
        staleness = (now - ts).total_seconds()
    except (OSError, ValueError):
        pass

    # Load digest data
    data = None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        pass

    # Compute degraded state
    degraded_reasons: list[str] = []
    if data is None:
        degraded_reasons.append("governance_digest.json not found or unreadable")
    elif staleness is not None and staleness > _DIGEST_STALE_THRESHOLD:
        degraded_reasons.append(
            f"governance_digest.json stale ({staleness:.0f}s old, threshold {_DIGEST_STALE_THRESHOLD}s)"
        )

    return {
        "view": "GovernanceDigestView",
        "queried_at": now.isoformat(),
        "source_freshness": {"governance_digest": mtime_iso},
        "staleness_seconds": round(staleness, 1) if staleness is not None else None,
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
        "data": data or {},
    }
