"""Observability dashboard API — the governance/audit-trail panel.

GET /api/operator/observability returns five read-only, fail-open sections:
  - self_learning : recent confidence_events (what the self-learning loop adjusted) + proposal count
  - tagging       : recent tagging_events (what the tagging agent tagged + with which model)
  - runtime       : cron jobs (schedule + last-run) + VNX daemon liveness
  - provenance    : provenance_registry chain_status counts + recent rows with their gaps
  - rework        : per-role first-pass success + rework-by-origin-role + recent rework edges

Single-tenant: reads this dashboard's project (CANONICAL_STATE_DIR). Every section is best-effort —
a missing table / db / command yields an empty section + a `degraded` flag, never an error.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from api_operator import CANONICAL_STATE_DIR, _op_dashboard_project_id

_logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(CANONICAL_STATE_DIR).resolve().parent.parent  # …/<repo or project>


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _qi_db() -> Path:
    return CANONICAL_STATE_DIR / "quality_intelligence.db"


def _runtime_db() -> Path:
    return CANONICAL_STATE_DIR / "runtime_coordination.db"


def _ro_conn(path: Path):
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _self_learning(limit: int, project_id: str) -> dict:
    out: dict = {"events": [], "proposals": 0}
    conn = _ro_conn(_qi_db())
    if conn is None:
        out["degraded"] = True
        return out
    try:
        rows = conn.execute(
            "SELECT dispatch_id, outcome, confidence_change, patterns_boosted, patterns_decayed, occurred_at "
            "FROM confidence_events WHERE project_id = ? ORDER BY occurred_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        out["events"] = [dict(r) for r in rows]
    except sqlite3.Error:
        out["degraded"] = True
    finally:
        conn.close()
    # Operator-gated proposals (pending_rules.json) — best-effort.
    try:
        pr = CANONICAL_STATE_DIR / "pending_rules.json"
        if pr.exists():
            data = json.loads(pr.read_text())
            out["proposals"] = len(data) if isinstance(data, list) else len(data.get("rules", data))
    except Exception:  # vnx-silent-except: the proposals count is an optional best-effort enrichment
        pass
    return out


def _tagging(limit: int, project_id: str) -> dict:
    out: dict = {"events": []}
    conn = _ro_conn(_qi_db())
    if conn is None:
        out["degraded"] = True
        return out
    try:
        rows = conn.execute(
            "SELECT table_name, pattern_id, pattern_title, tags_json, provider, tagged_at "
            "FROM tagging_events WHERE project_id = ? ORDER BY tagged_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        out["events"] = [
            {**dict(r), "tags": _safe_tags(r["tags_json"])} for r in rows
        ]
    except sqlite3.Error:
        # table absent (tagger never ran / older schema) → empty, not an error
        out["degraded"] = True
    finally:
        conn.close()
    return out


def _safe_tags(tags_json) -> list:
    try:
        v = json.loads(tags_json) if tags_json else []
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def _provenance(limit: int) -> dict:
    out: dict = {"by_status": {}, "recent": []}
    conn = _ro_conn(_runtime_db())
    if conn is None:
        out["degraded"] = True
        return out
    try:
        for r in conn.execute(
            "SELECT chain_status, COUNT(*) AS n FROM provenance_registry GROUP BY chain_status"
        ).fetchall():
            out["by_status"][r["chain_status"]] = r["n"]
        rows = conn.execute(
            "SELECT dispatch_id, receipt_id, commit_sha, pr_number, chain_status, gaps_json, registered_at "
            "FROM provenance_registry ORDER BY registered_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out["recent"] = [
            {**dict(r), "gaps": _safe_tags(r["gaps_json"])} for r in rows
        ]
    except sqlite3.Error:
        out["degraded"] = True
    finally:
        conn.close()
    return out


def _runtime() -> dict:
    """Cron jobs + VNX daemon liveness for this host. Filtered to VNX-relevant entries; honest that
    cron/daemons are host + cross-project."""
    out: dict = {"cron": [], "daemons": []}
    # Cron jobs (host crontab), VNX-relevant lines only.
    try:
        cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        for line in (cron.stdout or "").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if re.search(r"vnx|dispatch|receipt|intelligence|shadow|compact_state", s, re.I):
                m = re.match(r"^(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.*)$", s)
                schedule, cmd = (m.group(1), m.group(2)) if m else ("?", s)
                # last-run from the log mtime if the command redirects to one
                log_m = re.search(r">>?\s*(\S+\.log)", cmd)
                last_run = None
                if log_m:
                    try:
                        lp = Path(log_m.group(1))
                        if lp.exists():
                            last_run = datetime.fromtimestamp(lp.stat().st_mtime, timezone.utc).isoformat()
                    except OSError:
                        pass
                out["cron"].append({"schedule": schedule, "command": cmd[:160], "last_run": last_run})
    except Exception:
        out["cron_degraded"] = True
    # VNX daemon liveness (this project's paths only).
    try:
        ps = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True, timeout=5)
        proj = str(_PROJECT_ROOT)
        wanted = ("dispatcher", "receipt_processor", "intelligence_daemon", "headless_trigger", "recommendations_engine")
        for line in (ps.stdout or "").splitlines():
            if proj in line and any(w in line for w in wanted):
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    name = next((w for w in wanted if w in parts[1]), "daemon")
                    out["daemons"].append({"pid": parts[0], "name": name})
    except Exception:
        out["daemons_degraded"] = True
    out["daemons_running"] = len(out["daemons"])
    return out


def _rework(limit: int, project_id: str) -> dict:
    """Rework→skill attribution (slice 1): per-role first-pass success, rework-by-origin-role
    (self-join over parent_dispatch), and recent rework edges. Read-only, fail-open."""
    out: dict = {"by_role": [], "by_origin_role": [], "recent": []}
    conn = _ro_conn(_qi_db())
    if conn is None:
        out["degraded"] = True
        return out
    try:
        roles = conn.execute(
            "SELECT role, total_dispatches, successes, success_rate "
            "FROM dispatch_success_by_role WHERE role IS NOT NULL ORDER BY total_dispatches DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out["by_role"] = [dict(r) for r in roles]
    except sqlite3.Error:
        out["degraded"] = True
    try:
        origin = conn.execute(
            "SELECT p.role AS origin_role, COUNT(*) AS reworked "
            "FROM dispatch_metadata d JOIN dispatch_metadata p "
            "  ON d.parent_dispatch = p.dispatch_id AND d.project_id = p.project_id "
            "WHERE d.project_id = ? AND d.parent_dispatch IS NOT NULL AND d.parent_dispatch != '' "
            "GROUP BY p.role ORDER BY reworked DESC",
            (project_id,),
        ).fetchall()
        out["by_origin_role"] = [dict(r) for r in origin]
        edges = conn.execute(
            "SELECT d.dispatch_id AS rework_dispatch, d.role AS rework_role, "
            "       p.dispatch_id AS origin_dispatch, p.role AS origin_role, d.dispatched_at "
            "FROM dispatch_metadata d JOIN dispatch_metadata p "
            "  ON d.parent_dispatch = p.dispatch_id AND d.project_id = p.project_id "
            "WHERE d.project_id = ? AND d.parent_dispatch IS NOT NULL AND d.parent_dispatch != '' "
            "ORDER BY d.dispatched_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
        out["recent"] = [dict(r) for r in edges]
    except sqlite3.Error:
        # parent_dispatch self-join unavailable on older schema → empty, not an error
        out["degraded"] = True
    finally:
        conn.close()
    return out


def operator_get_observability(params: dict, *, project_id: "str | None" = None) -> "tuple[dict, int]":
    """GET /api/operator/observability — the five governance/audit sections. Always 200 (fail-open;
    each section self-reports `degraded`)."""
    pid = project_id or _op_dashboard_project_id() or "vnx-dev"
    try:
        limit = max(1, min(int((params.get("limit") or ["25"])[0]), 200))
    except (TypeError, ValueError):
        limit = 25
    body = {
        "project_id": pid,
        "queried_at": _now(),
        "self_learning": _self_learning(limit, pid),
        "tagging": _tagging(limit, pid),
        "provenance": _provenance(limit),
        "rework": _rework(limit, pid),
        "runtime": _runtime(),
    }
    return body, 200
