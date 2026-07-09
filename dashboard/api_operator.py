"""Operator dashboard API handlers.

Extracted from serve_dashboard.py to keep module size manageable.
Covers: projects, sessions, terminals, open-items, kanban, gate-config,
governance-digest, health, terminal unlock/jump, conversations.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_logger = logging.getLogger(__name__)

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

# Compute paths locally to avoid circular imports with serve_dashboard.
VNX_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = VNX_DIR
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

_UTC = timezone.utc

# Pane current_command values that indicate an idle shell (not actively running a tool).
_IDLE_SHELL_COMMANDS = frozenset({"bash", "zsh", "sh", "-bash", "-zsh", "-sh"})

_gate_config_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Wave 1 shadow mode — lazy imports; no hard dep on scripts/lib availability
# ---------------------------------------------------------------------------

_OP_SCRIPTS_LIB = str(VNX_DIR / "scripts" / "lib")

try:
    if _OP_SCRIPTS_LIB not in sys.path:
        sys.path.insert(0, _OP_SCRIPTS_LIB)
    import shadow_verifier as _op_shadow_verifier  # type: ignore[import]
    import shadow_logger as _op_shadow_logger       # type: ignore[import]
    from vnx_paths import resolve_central_data_dir as _op_resolve_central  # type: ignore[import]
    from vnx_paths import project_id_from_state_dir as _op_project_id_from_state_dir  # type: ignore[import]
    from scout_prepass import scout_sidecar_path as _op_scout_sidecar_path  # type: ignore[import]
except Exception:
    _op_shadow_verifier = None   # type: ignore[assignment]
    _op_shadow_logger = None     # type: ignore[assignment]
    _op_resolve_central = None   # type: ignore[assignment]
    _op_project_id_from_state_dir = None  # type: ignore[assignment]
    _op_scout_sidecar_path = None  # type: ignore[assignment]


def _is_scout_enriched(dispatch_id: str) -> bool:
    """True when a scout pre-pass sidecar exists for this dispatch
    (``<state_dir>/scout/<dispatch_id>.json``). Fail-open: any error → False, so a
    missing scout layer never breaks the kanban. Mirrors the door's scout producer."""
    if _op_scout_sidecar_path is None:
        return False
    try:
        return _op_scout_sidecar_path(CANONICAL_STATE_DIR, dispatch_id).exists()
    except (ValueError, OSError):
        return False


# Coordination-DB states that mean "planned, not yet executing" — the future-ready lane.
# 'ready' = promoted (human-gated, dispatchable); 'proposed' = awaiting promotion.
_QUEUED_STATES = ("proposed", "ready", "queued")


def _scan_queued_dispatches(exclude_ids: "set[str]") -> list:
    """Future-ready deliverables: rows in runtime_coordination.db whose state is planned
    (proposed/ready/queued) and which are NOT yet a file in the kanban dirs. These are the
    dispatches 'staged to run in the future' — the planning layer's promoted-but-undispatched
    work. Fail-open: a missing DB / missing column / any error → [], never breaks the kanban."""
    import sqlite3
    db_path = CANONICAL_STATE_DIR / "runtime_coordination.db"
    if not db_path.exists():
        return []
    entries: list = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dispatches)").fetchall()}
        if "state" not in cols or "dispatch_id" not in cols:
            return []
        placeholders = ",".join("?" * len(_QUEUED_STATES))
        rows = conn.execute(
            f"SELECT dispatch_id, state, track, terminal_id, priority, gate, "
            f"output_kind, operator_approved_at "
            f"FROM dispatches WHERE state IN ({placeholders}) ORDER BY created_at DESC",
            _QUEUED_STATES,
        ).fetchall()
        for r in rows:
            d = dict(r)
            did = d.get("dispatch_id")
            if not did or did in exclude_ids:
                continue
            entries.append({
                "id": did,
                "file": "—",
                "pr_id": "—",
                "track": d.get("track") or "—",
                "terminal": d.get("terminal_id") or "—",
                "role": "—",
                "gate": d.get("gate") or "—",
                "priority": d.get("priority") or "—",
                "status": d.get("state"),
                "reason": "—",
                "domain": "coding",
                "dir": "queued",
                "stage": "queued",
                "output_kind": d.get("output_kind") or "—",
                "state": d.get("state"),
                "promoted": d.get("operator_approved_at") is not None,
                "duration_secs": 0,
                "duration_label": "—",
                "has_receipt": False,
                "receipt_status": None,
                "scout_enriched": _is_scout_enriched(did),
            })
    except sqlite3.Error as exc:
        print(f"[kanban] queued scan failed: {exc}", file=sys.stderr)
        return []
    finally:
        conn.close()
    return entries

_SYSTEM_HEALTH_COUNT_SQL_TEMPLATE = "SELECT COUNT(*) AS cnt FROM {table}"
_SYSTEM_HEALTH_COUNT_CENTRAL_SQL_TEMPLATE = "SELECT COUNT(*) AS cnt FROM {table} WHERE project_id = ?"


def _op_dashboard_project_id() -> str:
    """Derive project_id from CANONICAL_STATE_DIR, fallback to env var."""
    if _op_project_id_from_state_dir is not None:
        pid = _op_project_id_from_state_dir(CANONICAL_STATE_DIR)
        if pid:
            return pid
    return os.environ.get("VNX_PROJECT_ID", "").strip()


def _op_central_qi_db() -> "Path | None":
    """Return central quality_intelligence.db for the current project, or None."""
    if _op_resolve_central is None:
        return None
    project_id = _op_dashboard_project_id()
    if not project_id:
        return None
    try:
        central = _op_resolve_central(project_id) / "state" / "quality_intelligence.db"
        if not central.exists() or central.resolve() == DB_PATH.resolve():
            return None
        return central
    except Exception:
        return None


def _op_shadow_write(cmp: object, project_id: str, read_site: str) -> None:
    if _op_shadow_logger is not None and getattr(cmp, "divergences", None):
        try:
            _op_shadow_logger.write_comparison_result(cmp, project_id, read_site)  # type: ignore[union-attr]
        except Exception as e:
            _logger.debug("Failed to write shadow comparison result: %s", e)


# ---------- Dispatch Kanban helpers ----------

_DIR_TO_STAGE: dict[str, str] = {
    "staging": "staging",
    "pending": "pending",
    "queue": "pending",
    "active": "active",
    "completed": "done",
    "rejected": "rejected",
    "dead_letter": "rejected",
    "cancelled": "rejected",
}

_STAGE_PRIORITY: dict[str, int] = {
    "staging": 0,
    "pending": 1,
    "active": 2,
    "review": 3,
    "done": 4,
    "rejected": 5,
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
        except OSError as e:
            _logger.debug("Failed to read receipt %s: %s", path.name, e)
    return receipts


def _scan_dispatches() -> dict:
    """Scan dispatch directories and return dispatches grouped by Kanban stage.

    Deduplicates by dispatch_id: when the same id appears in multiple directories
    (e.g. both completed/ and rejected/), keeps the entry from the highest-priority
    stage. Priority order: staging > pending > active > review > done > rejected.
    Ties broken by most-recent mtime.
    """
    receipts = _scan_receipts()
    stages: dict[str, list] = {s: [] for s in ["queued", "staging", "pending", "active", "review", "done", "rejected"]}

    if not DISPATCHES_DIR.exists():
        # No file-based dispatches, but the future-ready lane is DB-backed and independent.
        stages["queued"] = _scan_queued_dispatches(set())
        return {"stages": stages, "total": len(stages["queued"])}

    now = datetime.now(timezone.utc).timestamp()

    # Collect all candidate entries keyed by dispatch_id
    candidates: dict[str, list] = defaultdict(list)

    for dir_name, base_stage in _DIR_TO_STAGE.items():
        dir_path = DISPATCHES_DIR / dir_name
        if not dir_path.exists():
            continue
        for path in dir_path.glob("*.md"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                header = _parse_dispatch_header(text)
                mtime = path.stat().st_mtime
                duration_secs = now - mtime
                dispatch_id = header.get("dispatch_id", path.stem)
                receipt = receipts.get(dispatch_id)

                # Promote active dispatches with a filed receipt to "review"
                stage = "review" if base_stage == "active" and receipt else base_stage

                entry = {
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
                    "domain": header.get("domain", "coding"),
                    "dir": dir_name,
                    "stage": stage,
                    "duration_secs": int(duration_secs),
                    "duration_label": _format_duration(duration_secs),
                    "has_receipt": receipt is not None,
                    "receipt_status": receipt.get("status") if receipt else None,
                    "scout_enriched": _is_scout_enriched(dispatch_id),
                }
                candidates[dispatch_id].append((stage, mtime, entry))
            except Exception as exc:
                print(f"[kanban] skipping {path.name}: {exc}", file=sys.stderr)

    # Dedup: pick one canonical entry per dispatch_id (highest-priority stage wins)
    dupe_count = sum(len(v) - 1 for v in candidates.values() if len(v) > 1)
    if dupe_count:
        print(f"[kanban] deduped {dupe_count} duplicate dispatch entries", file=sys.stderr)

    for dispatch_id, entries in candidates.items():
        winner_stage, _, winner_entry = min(
            entries,
            key=lambda e: (_STAGE_PRIORITY.get(e[0], 99), -e[1]),
        )
        stages[winner_stage].append(winner_entry)

    # Future-ready lane: planned (proposed/ready/queued) coordination-DB rows that are NOT
    # already a file in the stages above. Deduped against the file-based dispatch ids.
    stages["queued"] = _scan_queued_dispatches(set(candidates.keys()))

    # Restore mtime-descending order within each stage (most recent first)
    for stage_name in stages:
        stages[stage_name].sort(key=lambda e: e["duration_secs"])

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


def _session_name_to_project(session_name: str) -> "str | None":
    """Best-effort project name from a tmux session name."""
    if session_name.startswith("vnx-"):
        return session_name[4:] or None
    if session_name.startswith("t0-"):
        return "system"
    return session_name


def _parse_iso_or_none(value: "str | None") -> "datetime | None":
    """Parse an ISO-8601 timestamp; return None on any failure."""
    if not value:
        return None
    try:
        ts = value.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _list_tmux_sessions(now: datetime) -> "tuple[list[dict], list[str]]":
    """Return (sessions, degraded_reasons) from tmux list-sessions/list-panes.

    If tmux is absent or its commands fail, returns ([], []) — never raises.
    """
    if shutil.which("tmux") is None:
        return [], []

    sessions_result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}\t#{session_activity}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if sessions_result.returncode != 0:
        return [], []

    # Collect per-session pane commands in one tmux call so busy/idle is cheap.
    pane_commands: dict[str, list[str]] = defaultdict(list)
    panes_result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_current_command}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if panes_result.returncode == 0:
        for line in panes_result.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                pane_commands[parts[0]].append(parts[1])

    sessions: list[dict] = []
    for line in sessions_result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        name, activity_str = parts
        try:
            activity_epoch = int(activity_str)
        except ValueError:
            continue
        last_activity_dt = datetime.fromtimestamp(activity_epoch, tz=_UTC)
        last_activity = last_activity_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        age_seconds = max(0, int((now - last_activity_dt).total_seconds()))
        commands = pane_commands.get(name, [])
        status = "busy" if any(cmd not in _IDLE_SHELL_COMMANDS for cmd in commands) else "idle"
        sessions.append({
            "name": name,
            "project": _session_name_to_project(name),
            "status": status,
            "last_activity": last_activity,
            "age_seconds": age_seconds,
        })

    return sessions, []


def _load_session_store_entries() -> "dict[str, dict]":
    """Read persisted per-terminal session entries without mutating state."""
    try:
        from session_store import SessionStore
        return SessionStore().all_entries()
    except Exception:
        return {}


def _operator_get_sessions() -> dict:
    """GET /api/operator/sessions -- read-only live session observability tile.

    Composes tmux session liveness (name, project, idle/busy, activity age) with
    the persistent session-store state (provider session_id, last dispatch_id).
    No control actions are exposed here; Part B restart panel is out of scope.
    """
    now = datetime.now(_UTC)
    degraded = False
    degraded_reasons: list[str] = []
    sessions_by_name: dict[str, dict] = {}

    try:
        tmux_sessions, tmux_reasons = _list_tmux_sessions(now)
        degraded_reasons.extend(tmux_reasons)
    except Exception as exc:
        tmux_sessions = []
        degraded = True
        degraded_reasons.append(f"tmux enumeration failed: {exc}")

    for s in tmux_sessions:
        sessions_by_name[s["name"]] = {
            "name": s["name"],
            "project": s["project"],
            "status": s["status"],
            "last_activity": s["last_activity"],
            "age_seconds": s["age_seconds"],
            "session_id": None,
            "dispatch_id": None,
            "remote_control_url": None,
        }

    try:
        store_entries = _load_session_store_entries()
    except Exception as exc:
        store_entries = {}
        degraded = True
        degraded_reasons.append(f"session store read failed: {exc}")

    for terminal_id, entry in store_entries.items():
        name = terminal_id
        session_id = entry.get("session_id") or None
        dispatch_id = entry.get("dispatch_id") or None
        updated_at = entry.get("updated_at")
        updated_dt = _parse_iso_or_none(updated_at)

        if name in sessions_by_name:
            existing = sessions_by_name[name]
            existing["session_id"] = existing["session_id"] or session_id
            existing["dispatch_id"] = existing["dispatch_id"] or dispatch_id
            # The session-store update time can be more recent than tmux activity;
            # surface the freshest timestamp we have.
            if updated_dt is not None:
                existing_dt = _parse_iso_or_none(existing.get("last_activity"))
                if existing_dt is None or updated_dt > existing_dt:
                    existing["last_activity"] = updated_at
                    existing["age_seconds"] = max(0, int((now - updated_dt).total_seconds()))
        else:
            last_activity = updated_at if updated_dt is not None else None
            age_seconds = max(0, int((now - updated_dt).total_seconds())) if updated_dt is not None else None
            sessions_by_name[name] = {
                "name": name,
                "project": _session_name_to_project(name),
                "status": "idle",
                "last_activity": last_activity,
                "age_seconds": age_seconds,
                "session_id": session_id,
                "dispatch_id": dispatch_id,
                "remote_control_url": None,
            }

    data = sorted(
        sessions_by_name.values(),
        key=lambda s: ((s["project"] or ""), s["name"]),
    )

    return {
        "view": "live-sessions",
        "queried_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "degraded": degraded,
        "degraded_reasons": degraded_reasons,
        "data": data,
    }


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


# ---------- System Health API ----------

import sqlite3
from typing import Any


def _effective_db_mtime(db_path: "Path | str") -> float:
    """Return the effective mtime of a SQLite db, accounting for WAL mode.

    In WAL mode, .db-wal accumulates writes until checkpoint; the base .db
    file mtime alone can make a live db appear stale. Returns
    max(mtime(.db), mtime(.db-wal), mtime(.db-shm)) so freshness checks
    remain correct regardless of checkpoint timing.
    """
    p = Path(db_path)
    mtimes = []
    for candidate in (p, p.parent / (p.name + "-wal"), p.parent / (p.name + "-shm")):
        try:
            mtimes.append(candidate.stat().st_mtime)
        except OSError:
            pass
    return max(mtimes) if mtimes else 0.0


_SYSTEM_HEALTH_DB_TABLES = ("success_patterns", "antipatterns", "prevention_rules", "dispatch_metadata")


def _operator_get_system_health() -> dict:
    """GET /api/operator/system-health -- deep system health check.

    3-state VNX_USE_CENTRAL_DB dispatcher (Wave 1) applied to intelligence DB
    table counts. Per-project count remains authoritative; central compared via
    compare_aggregate_count (metric 4 aggregate, <0.1% tolerance).
    """
    now = datetime.now(timezone.utc)
    components: dict[str, dict] = {}
    flag = os.environ.get("VNX_USE_CENTRAL_DB", "").strip()
    if flag not in ("", "1", "shadow"):
        _logger.warning("unknown VNX_USE_CENTRAL_DB value %r; falling back to legacy", flag)
        flag = ""

    # 1. Intelligence DB table population (3-state VNX_USE_CENTRAL_DB dispatcher)
    intel_details: dict[str, Any] = {}
    try:
        if flag == "1":
            # Cutover: count from central DB with project_id filter; no fallback
            central_qi = _op_central_qi_db()
            project_id = _op_dashboard_project_id()
            if central_qi is not None and central_qi.exists() and project_id:
                conn = sqlite3.connect(str(central_qi))
                conn.row_factory = sqlite3.Row
                total_rows = 0
                for table in _SYSTEM_HEALTH_DB_TABLES:
                    try:
                        row = conn.execute(
                            _SYSTEM_HEALTH_COUNT_CENTRAL_SQL_TEMPLATE.format(table=table),  # noqa: S608
                            (project_id,),
                        ).fetchone()
                        count = row["cnt"] if row else 0
                    except Exception:
                        count = 0
                    intel_details[table] = count
                    total_rows += count
                conn.close()
                status = "healthy" if total_rows > 0 else "dead"
                if total_rows > 0 and any(intel_details[t] == 0 for t in _SYSTEM_HEALTH_DB_TABLES):
                    status = "degraded"
            else:
                status = "dead"
                intel_details["error"] = "central quality_intelligence.db not available"
        elif DB_PATH.exists():
            # WAL-aware mtime: .db-wal may be more recent than .db
            intel_details["db_mtime"] = _effective_db_mtime(DB_PATH)
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            total_rows = 0
            for table in _SYSTEM_HEALTH_DB_TABLES:
                try:
                    row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()  # noqa: S608
                    count = row["cnt"] if row else 0
                except Exception:
                    count = 0
                intel_details[table] = count
                total_rows += count
            conn.close()
            status = "healthy" if total_rows > 0 else "dead"
            if total_rows > 0 and any(intel_details[t] == 0 for t in _SYSTEM_HEALTH_DB_TABLES):
                status = "degraded"

            # Shadow: compare per-table counts against central DB (aggregate metric)
            if flag == "shadow" and _op_shadow_verifier is not None:
                central_qi = _op_central_qi_db()
                project_id = _op_dashboard_project_id()
                if central_qi is not None and project_id:
                    try:
                        c_conn = sqlite3.connect(str(central_qi))
                        c_conn.row_factory = sqlite3.Row
                        for table in _SYSTEM_HEALTH_DB_TABLES:
                            try:
                                c_row = c_conn.execute(
                                    f"SELECT COUNT(*) as cnt FROM {table} WHERE project_id = ?",  # noqa: S608
                                    (project_id,),
                                ).fetchone()
                                c_count = c_row["cnt"] if c_row else 0
                            except Exception:
                                continue
                            cmp = _op_shadow_verifier.compare_aggregate_count(
                                intel_details.get(table, 0), c_count,
                                project_id=project_id,
                                read_site=f"dashboard.api.system_health.{table}",
                                sql_template=_SYSTEM_HEALTH_COUNT_SQL_TEMPLATE.format(table=table),
                            )
                            _op_shadow_write(cmp, project_id, f"dashboard.api.system_health.{table}")
                        c_conn.close()
                    except sqlite3.Error as e:
                        _logger.debug("Failed shadow comparison for system health: %s", e)
        else:
            status = "dead"
            intel_details["error"] = "quality_intelligence.db not found"
    except Exception as exc:
        status = "dead"
        intel_details["error"] = str(exc)
    components["intelligence_db"] = {"status": status, "details": intel_details}

    # 2. Governance digest freshness
    digest_path = CANONICAL_STATE_DIR / "governance_digest.json"
    try:
        if digest_path.exists():
            mt = digest_path.stat().st_mtime
            mtime_dt = datetime.fromtimestamp(mt, tz=timezone.utc)
            age_seconds = (now - mtime_dt).total_seconds()
            pattern_count_24h = 0
            try:
                data = json.loads(digest_path.read_text(encoding="utf-8"))
                pattern_count_24h = data.get("recurring_pattern_count", 0)
            except (OSError, json.JSONDecodeError) as e:
                _logger.debug("Failed to read governance digest for pattern count: %s", e)
            digest_status = "healthy" if age_seconds < 600 else "degraded"
            components["governance_digest"] = {
                "status": digest_status,
                "details": {
                    "last_updated": mtime_dt.isoformat(),
                    "age_seconds": round(age_seconds, 1),
                    "patterns_detected": pattern_count_24h,
                },
            }
        else:
            components["governance_digest"] = {
                "status": "dead",
                "details": {"error": "governance_digest.json not found"},
            }
    except Exception as exc:
        components["governance_digest"] = {"status": "dead", "details": {"error": str(exc)}}

    # 3. Dispatcher health (last successful delivery)
    try:
        dispatch_log = CANONICAL_STATE_DIR / "dispatch_events.ndjson"
        if dispatch_log.exists():
            last_line = ""
            with open(dispatch_log, "rb") as f:
                f.seek(0, 2)
                pos = f.tell()
                while pos > 0:
                    pos -= 1
                    f.seek(pos)
                    ch = f.read(1)
                    if ch == b"\n" and last_line:
                        break
                    last_line = ch.decode(errors="replace") + last_line
            if last_line.strip():
                try:
                    evt = json.loads(last_line.strip())
                    ts = evt.get("timestamp", "")
                    components["dispatcher"] = {
                        "status": "healthy",
                        "details": {"last_event": ts},
                    }
                except Exception:
                    components["dispatcher"] = {
                        "status": "degraded",
                        "details": {"error": "unparseable last event"},
                    }
            else:
                components["dispatcher"] = {
                    "status": "degraded",
                    "details": {"error": "empty log"},
                }
        else:
            components["dispatcher"] = {
                "status": "dead",
                "details": {"error": "dispatch_events.ndjson not found"},
            }
    except Exception as exc:
        components["dispatcher"] = {"status": "dead", "details": {"error": str(exc)}}

    # 4. Receipt processor (pending receipt count)
    try:
        if RECEIPTS_PATH.exists():
            line_count = sum(1 for _ in open(RECEIPTS_PATH, encoding="utf-8"))
            components["receipt_processor"] = {
                "status": "healthy",
                "details": {"total_receipts": line_count},
            }
        else:
            components["receipt_processor"] = {
                "status": "dead",
                "details": {"error": "t0_receipts.ndjson not found"},
            }
    except Exception as exc:
        components["receipt_processor"] = {"status": "dead", "details": {"error": str(exc)}}

    # 5. Lease health (stale lease count)
    try:
        coord_db = CANONICAL_STATE_DIR / "runtime_coordination.db"
        if coord_db.exists():
            conn = sqlite3.connect(str(coord_db))
            try:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM terminal_leases "
                    "WHERE status = 'active' AND expires_at < ?",
                    (now.isoformat(),),
                ).fetchone()
                stale = row[0] if row else 0
            except Exception:
                stale = 0
            conn.close()
            lease_status = "healthy" if stale == 0 else "degraded"
            components["lease_health"] = {
                "status": lease_status,
                "details": {"stale_leases": stale},
            }
        else:
            components["lease_health"] = {
                "status": "degraded",
                "details": {"error": "runtime_coordination.db not found"},
            }
    except Exception as exc:
        components["lease_health"] = {"status": "degraded", "details": {"error": str(exc)}}

    # 6. Report index
    try:
        if REPORTS_DIR.exists():
            reports = list(REPORTS_DIR.glob("*.md"))
            total_reports = len(reports)
            latest_age = None
            if reports:
                newest = max(reports, key=lambda p: p.stat().st_mtime)
                age = (now - datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)).total_seconds()
                latest_age = round(age, 1)
            report_status = "healthy" if total_reports > 0 else "degraded"
            components["report_index"] = {
                "status": report_status,
                "details": {
                    "total_reports": total_reports,
                    "latest_report_age_seconds": latest_age,
                },
            }
        else:
            components["report_index"] = {
                "status": "dead",
                "details": {"error": "unified_reports dir not found"},
            }
    except Exception as exc:
        components["report_index"] = {"status": "dead", "details": {"error": str(exc)}}

    # Summary score
    status_scores = {"healthy": 2, "degraded": 1, "dead": 0}
    scores = [status_scores.get(c["status"], 0) for c in components.values()]
    avg_score = sum(scores) / len(scores) if scores else 0
    if avg_score >= 1.5:
        overall = "healthy"
    elif avg_score >= 0.5:
        overall = "degraded"
    else:
        overall = "dead"

    return {
        "status": overall,
        "queried_at": now.isoformat(),
        "components": components,
        "health_score": round(avg_score / 2.0, 2),
    }


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


# ---------- Session History / Reports API ----------

# Filename pattern: YYYYMMDD-HHMMSS-{TRACK}-{slug}.md
# Track-to-terminal mapping (inverse of TERMINAL_TRACK_MAP)
_TRACK_TO_TERMINAL = {v: k for k, v in TERMINAL_TRACK_MAP.items()}
# HEADLESS track maps to T0
_TRACK_TO_TERMINAL["HEADLESS"] = "T0"


def _parse_report_filename(filename: str) -> dict:
    """Parse metadata from a unified report filename."""
    stem = filename
    if stem.endswith(".md"):
        stem = stem[:-3]
    parts = stem.split("-", 3)
    result: dict = {
        "filename": filename,
        "timestamp": None,
        "track": None,
        "terminal": None,
        "slug": None,
    }
    if len(parts) >= 3:
        date_part = parts[0]   # YYYYMMDD
        time_part = parts[1]   # HHMMSS
        track_part = parts[2]  # track or "HEADLESS"
        slug_part = parts[3] if len(parts) > 3 else ""

        result["track"] = track_part
        result["terminal"] = _TRACK_TO_TERMINAL.get(track_part)
        result["slug"] = slug_part

        try:
            dt = datetime.strptime(f"{date_part}{time_part}", "%Y%m%d%H%M%S")
            result["timestamp"] = dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    return result


def _parse_report_metadata(path: Path) -> dict:
    """Read first 20 lines of a report file to extract embedded metadata."""
    meta: dict = {
        "dispatch_id": None,
        "pr_id": None,
        "status": None,
        "gate": None,
        "title": None,
        "auto_generated": False,
    }
    try:
        lines = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= 20:
                    break
                lines.append(line.rstrip())

        for line in lines:
            if line.startswith("# "):
                meta["title"] = line[2:].strip()
            elif line.startswith("**Dispatch ID**:"):
                meta["dispatch_id"] = line.split(":", 1)[1].strip()
            elif line.startswith("**PR**:"):
                meta["pr_id"] = line.split(":", 1)[1].strip()
            elif line.startswith("**Status**:"):
                meta["status"] = line.split(":", 1)[1].strip()
            elif line.startswith("**Gate**:"):
                meta["gate"] = line.split(":", 1)[1].strip()

        # Auto-generated reports typically have dispatch_id embedded
        meta["auto_generated"] = meta["dispatch_id"] is not None

    except OSError:
        pass

    return meta


def _operator_get_reports(params: dict[str, list[str]]) -> dict:
    """GET /api/operator/reports?limit=50&offset=0&terminal=T1&track=A"""
    reports_dir = _VNX_DATA_DIR / "unified_reports"

    limit = int((params.get("limit") or ["50"])[0])
    offset = int((params.get("offset") or ["0"])[0])
    terminal_filter = (params.get("terminal") or [None])[0]
    track_filter = (params.get("track") or [None])[0]

    if not reports_dir.exists():
        return {"reports": [], "total": 0, "limit": limit, "offset": offset}

    # Collect all .md files sorted by mtime descending
    all_files = sorted(
        reports_dir.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    # Build report objects with parsed metadata
    reports = []
    for path in all_files:
        parsed = _parse_report_filename(path.name)
        meta = _parse_report_metadata(path)

        # Apply filters before slicing
        if terminal_filter and parsed.get("terminal") != terminal_filter:
            continue
        if track_filter and parsed.get("track") != track_filter:
            continue

        report = {**parsed, **meta}
        reports.append(report)

    total = len(reports)
    page = reports[offset: offset + limit]

    return {"reports": page, "total": total, "limit": limit, "offset": offset}


def _operator_get_report_content(filename: str) -> dict | None:
    """Return full markdown content for a specific report file, or None if not found."""
    # Sanitise: only allow plain filenames (no path traversal)
    safe_name = Path(filename).name
    if safe_name != filename:
        return None

    reports_dir = _VNX_DATA_DIR / "unified_reports"
    path = reports_dir / safe_name

    if not path.exists() or not path.is_file():
        return None

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return {
            "filename": safe_name,
            "content": content,
            "size_bytes": path.stat().st_size,
        }
    except OSError:
        return None


# ---------- Agent Name Mapping API ----------

_TERMINAL_AGENT_DEFAULTS = {
    "T0": {"name": "Orchestrator", "role": "t0-orchestrator", "track": None},
    "T1": {"name": "Backend Developer", "role": "backend-developer", "track": "A"},
    "T2": {"name": "Test Engineer", "role": "test-engineer", "track": "B"},
    "T3": {"name": "Architect", "role": "architect", "track": "C"},
}


def _operator_get_agents() -> dict:
    """GET /api/operator/agents — terminal ID to agent name/role mapping."""
    agents = []

    for terminal_id in ("T0", "T1", "T2", "T3"):
        defaults = _TERMINAL_AGENT_DEFAULTS[terminal_id]

        # Resolve adapter from environment (VNX_ADAPTER_T1, etc.)
        adapter_env_key = f"VNX_ADAPTER_{terminal_id}"
        adapter = os.environ.get(adapter_env_key, "tmux")

        # Attempt to infer actual role from the most recent active dispatch for this terminal
        role = defaults["role"]
        name = defaults["name"]

        active_dispatch_dir = DISPATCHES_DIR / "active"
        if active_dispatch_dir.exists():
            for dispatch_file in sorted(
                active_dispatch_dir.glob("*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ):
                try:
                    text = dispatch_file.read_text(encoding="utf-8", errors="replace")
                    header = _parse_dispatch_header(text)
                    if header.get("terminal") == terminal_id:
                        if header.get("role"):
                            role = header["role"]
                            # Convert role slug to display name
                            name = role.replace("-", " ").title()
                        break
                except OSError as e:
                    _logger.debug("Failed to read dispatch file %s: %s", dispatch_file.name, e)

        entry: dict = {
            "terminal": terminal_id,
            "name": name,
            "role": role,
            "track": defaults["track"],
            "adapter": adapter,
        }
        agents.append(entry)

    return {"agents": agents}
