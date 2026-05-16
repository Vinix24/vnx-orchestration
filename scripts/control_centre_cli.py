#!/usr/bin/env python3
"""control_centre_cli.py — Wave 5 PR-5.5: Control Centre operator CLI.

Operator-facing tool that routes /cc-* commands to the appropriate managers.
Each command instantiates T0LifecycleManager, StateAggregator, and/or
IntelligenceAggregator as needed.

ADR-005: every command emits an audit event to
    <vnx_data_dir>/events/control_centre.ndjson
via StateAggregator.submit() using project_id=cc-system for global ops.

Usage:
    python3 scripts/control_centre_cli.py status
    python3 scripts/control_centre_cli.py dispatch --project <id> --task "..."
    python3 scripts/control_centre_cli.py heartbeat --project <id>
    python3 scripts/control_centre_cli.py kill --project <id>
    python3 scripts/control_centre_cli.py reap
    python3 scripts/control_centre_cli.py intel --project <id>
    python3 scripts/control_centre_cli.py aggregate
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ---------------------------------------------------------------------------
# Path setup — must come before local imports.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.aggregator.state_aggregator import ProjectStateUpdate, StateAggregator
from scripts.aggregator.t0_lifecycle import T0LifecycleManager
from scripts.lib.intelligence_aggregator import IntelligenceAggregator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY = _REPO_ROOT / "scripts" / "control_centre_projects.yaml"
_CC_AUDIT_PROJECT = "cc-system"
_CC_EVENTS_SUBPATH = "events/control_centre.ndjson"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _load_registry(registry_path: Path) -> List[Dict[str, Any]]:
    """Load project registry from YAML. Returns list of project dicts."""
    if not registry_path.exists():
        return []
    with open(registry_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return (data or {}).get("projects", [])


def _project_vnx_data(project: Dict[str, Any]) -> Path:
    """Resolve .vnx-data dir for a project."""
    root = Path(project["root"])
    coord_db_rel = project.get("coord_db", ".vnx-data/state/runtime_coordination.db")
    # .vnx-data is two levels up from coord_db
    return root / Path(coord_db_rel).parts[0]


def _coord_db_path(project: Dict[str, Any]) -> Path:
    root = Path(project["root"])
    coord_db_rel = project.get("coord_db", ".vnx-data/state/runtime_coordination.db")
    return root / coord_db_rel


def _make_aggregator(vnx_data_dir: Path) -> StateAggregator:
    return StateAggregator(vnx_data_dir=vnx_data_dir)


def _emit_audit(
    aggregator: StateAggregator,
    project_id: str,
    event_type: str,
    payload: Dict[str, Any],
) -> None:
    update = ProjectStateUpdate(
        project_id=project_id,
        timestamp=_now_iso(),
        event_type=event_type,
        payload=payload,
        source_t0="control-centre",
    )
    aggregator.submit(update)


def _get_active_lease(coord_db: Path) -> Optional[Dict[str, Any]]:
    """Read the active (state=leased, terminal_id=T0) row from runtime_coordination.db."""
    import sqlite3

    if not coord_db.exists():
        return None
    try:
        conn = sqlite3.connect(str(coord_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM terminal_leases "
            "WHERE terminal_id = 'T0' AND state = 'leased' "
            "LIMIT 1"
        ).fetchone()
        conn.close()
        if row is None:
            return None
        meta: Dict[str, Any] = {}
        raw = row["metadata_json"]
        if raw:
            try:
                meta = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
        return {
            "project_id": row["project_id"],
            "lease_token": row["lease_token"] or "",
            "generation": row["generation"],
            "last_heartbeat_at": row["last_heartbeat_at"] or "",
            "metadata": meta,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("_get_active_lease: error reading %s: %s", coord_db, exc)
        return None


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """List all projects + T0 lifecycle state."""
    registry = _load_registry(Path(args.registry))
    if not registry:
        print("No projects registered. See scripts/control_centre_projects.yaml.example")
        return 0

    vnx_data_dir = _repo_vnx_data()
    agg = _make_aggregator(vnx_data_dir)

    central: Dict[str, Any] = {}
    central_path = vnx_data_dir / "aggregator" / "central_state.json"
    if central_path.exists():
        try:
            central = json.loads(central_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    print(f"{'PROJECT':<20} {'STATE':<14} {'PID':<8} {'LAST_HEARTBEAT':<32}")
    print("-" * 76)

    for project in registry:
        pid_str = args.project if hasattr(args, "project") else ""
        proj_id = project["id"]
        coord_db = _coord_db_path(project)
        lease = _get_active_lease(coord_db)

        if lease is not None:
            lifecycle_state = lease["metadata"].get("lifecycle_state", "RUNNING")
            pid_val = str(lease["metadata"].get("pid", "?"))
            hb = lease["last_heartbeat_at"][:19] if lease["last_heartbeat_at"] else "?"
        else:
            lifecycle_state = "not_spawned"
            pid_val = "-"
            hb = "-"

        proj_counts = central.get("projects", {}).get(proj_id, {}).get("event_counts", {})
        events_summary = " ".join(f"{k}={v}" for k, v in list(proj_counts.items())[:3])
        print(f"{proj_id:<20} {lifecycle_state:<14} {pid_val:<8} {hb:<32} {events_summary}")

    _emit_audit(agg, _CC_AUDIT_PROJECT, "cc.status.requested", {
        "project_count": len(registry),
    })
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    """Forward a dispatch instruction to a project T0 via its pending/ queue."""
    registry = _load_registry(Path(args.registry))
    project = _find_project(registry, args.project)
    if project is None:
        print(f"Project not found in registry: {args.project}", file=sys.stderr)
        return 1

    root = Path(project["root"])
    vnx_data_project = root / ".vnx-data"
    pending_dir = vnx_data_project / "dispatches" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dispatch_id = f"cc-{ts}-{args.project}"
    dispatch_dir = pending_dir / dispatch_id
    dispatch_dir.mkdir(parents=True, exist_ok=True)

    instruction_path = dispatch_dir / "instruction.md"
    instruction_path.write_text(args.task, encoding="utf-8")

    dispatch_payload = {
        "dispatch_id": dispatch_id,
        "terminal_id": "T0",
        "model": "sonnet",
        "role": "t0-orchestrator",
        "track": "cc-forwarded",
        "pr_id": "CC",
        "priority": "P2",
        "cognition": "normal",
        "parent_dispatch": "",
        "reason": f"Control Centre forwarded dispatch to {args.project}",
        "branch": "",
        "worktree": str(root),
        "instruction_path": "instruction.md",
        "project_id": args.project,
        "source": "control-centre",
    }

    dispatch_json = dispatch_dir / "dispatch.json"
    tmp = dispatch_json.with_suffix(".tmp")
    tmp.write_text(json.dumps(dispatch_payload, indent=2), encoding="utf-8")
    os.replace(tmp, dispatch_json)

    print(f"Dispatch created: {dispatch_id}")
    print(f"  -> {dispatch_json}")

    vnx_data_dir = _repo_vnx_data()
    agg = _make_aggregator(vnx_data_dir)
    _emit_audit(agg, args.project, "cc.dispatch.forwarded", {
        "dispatch_id": dispatch_id,
        "project": args.project,
        "task_preview": args.task[:120],
    })
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    """Update heartbeat for a running T0."""
    registry = _load_registry(Path(args.registry))
    project = _find_project(registry, args.project)
    if project is None:
        print(f"Project not found in registry: {args.project}", file=sys.stderr)
        return 1

    coord_db = _coord_db_path(project)
    lease = _get_active_lease(coord_db)
    if lease is None:
        print(f"No active T0 lease for project: {args.project}", file=sys.stderr)
        return 1

    vnx_data_dir = _repo_vnx_data()
    agg = _make_aggregator(vnx_data_dir)
    mgr = T0LifecycleManager(coord_db_path=coord_db, aggregator=agg)

    pid = lease["metadata"].get("pid", -1)
    token = lease["lease_token"]
    ok = mgr.heartbeat(args.project, pid=pid, lease_token=token)

    if ok:
        print(f"Heartbeat recorded for {args.project} (pid={pid})")
    else:
        print(f"Heartbeat failed for {args.project} — lease mismatch or expired", file=sys.stderr)
        return 1

    _emit_audit(agg, args.project, "cc.heartbeat.sent", {
        "project": args.project,
        "pid": pid,
        "token": token,
        "result": ok,
    })
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    """Graceful shutdown of project T0 (SIGTERM -> wait -> SIGKILL)."""
    registry = _load_registry(Path(args.registry))
    project = _find_project(registry, args.project)
    if project is None:
        print(f"Project not found in registry: {args.project}", file=sys.stderr)
        return 1

    coord_db = _coord_db_path(project)
    lease = _get_active_lease(coord_db)
    if lease is None:
        print(f"No active T0 lease for project: {args.project}", file=sys.stderr)
        return 1

    vnx_data_dir = _repo_vnx_data()
    agg = _make_aggregator(vnx_data_dir)
    mgr = T0LifecycleManager(coord_db_path=coord_db, aggregator=agg)

    token = lease["lease_token"]
    result = mgr.kill(args.project, lease_token=token, source="control-centre")

    print(f"Kill result for {args.project}:")
    print(f"  signaled:           {result.signaled}")
    print(f"  verified_dead:      {result.verified_dead}")
    print(f"  lease_released:     {result.lease_released}")
    print(f"  escalated_sigkill:  {result.escalated_to_sigkill}")
    if result.duration_ms is not None:
        print(f"  duration_ms:        {result.duration_ms}")
    if result.error:
        print(f"  error:              {result.error}", file=sys.stderr)
        return 1

    _emit_audit(agg, args.project, "cc.kill.requested", {
        "project": args.project,
        "token": token,
        "verified_dead": result.verified_dead,
        "lease_released": result.lease_released,
    })
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    """Sweep stale T0 leases across all registered projects."""
    registry = _load_registry(Path(args.registry))
    if not registry:
        print("No projects registered.")
        return 0

    vnx_data_dir = _repo_vnx_data()
    agg = _make_aggregator(vnx_data_dir)

    total_reaped = 0
    for project in registry:
        coord_db = _coord_db_path(project)
        if not coord_db.exists():
            continue
        mgr = T0LifecycleManager(coord_db_path=coord_db, aggregator=agg)
        results = mgr.reap_dead_t0s()
        for r in results:
            print(f"  {r.project_id:<20} {r.classification:<20} lease_released={r.lease_released}")
            if r.error:
                print(f"    error: {r.error}", file=sys.stderr)
            total_reaped += 1

    print(f"Reap complete. {total_reaped} leases processed.")

    _emit_audit(agg, _CC_AUDIT_PROJECT, "cc.reap.completed", {
        "total_processed": total_reaped,
        "project_count": len(registry),
    })
    return 0


def cmd_intel(args: argparse.Namespace) -> int:
    """Show cross-project intelligence recommendations for target project."""
    registry = _load_registry(Path(args.registry))
    project = _find_project(registry, args.project)
    if project is None:
        print(f"Project not found in registry: {args.project}", file=sys.stderr)
        return 1

    db_paths = _build_intel_db_paths(registry)
    ia = IntelligenceAggregator(project_db_paths=db_paths)

    recs = ia.recommend_cross_project(args.project)
    if not recs:
        print(f"No cross-project recommendations for {args.project}.")
    else:
        print(f"Cross-project recommendations for {args.project}:")
        for rec in recs:
            print(f"  [{rec.confidence:.2f}] from={rec.source_project}")
            print(f"    {rec.rationale}")

    vnx_data_dir = _repo_vnx_data()
    agg = _make_aggregator(vnx_data_dir)
    _emit_audit(agg, args.project, "cc.intel.requested", {
        "target_project": args.project,
        "recommendation_count": len(recs),
    })
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    """Refresh global intelligence facet from all project DBs."""
    registry = _load_registry(Path(args.registry))
    db_paths = _build_intel_db_paths(registry)
    ia = IntelligenceAggregator(project_db_paths=db_paths)

    vnx_data_dir = _repo_vnx_data()
    output_path = vnx_data_dir / "aggregator" / "global_intelligence.json"
    ia.export_global_facet(output_path)

    print(f"Global facet refreshed: {output_path}")

    agg = _make_aggregator(vnx_data_dir)
    _emit_audit(agg, _CC_AUDIT_PROJECT, "cc.aggregate.completed", {
        "output_path": str(output_path),
        "project_count": len(registry),
    })
    return 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _repo_vnx_data() -> Path:
    """Return .vnx-data dir for the repo this CLI lives in."""
    return _REPO_ROOT / ".vnx-data"


def _find_project(registry: List[Dict[str, Any]], project_id: str) -> Optional[Dict[str, Any]]:
    for p in registry:
        if p["id"] == project_id:
            return p
    return None


def _build_intel_db_paths(registry: List[Dict[str, Any]]) -> Dict[str, Path]:
    """Build {project_id: quality_intelligence.db path} for all registry projects."""
    result: Dict[str, Path] = {}
    for project in registry:
        root = Path(project["root"])
        intel_db = project.get(
            "intel_db",
            ".vnx-data/state/quality_intelligence.db",
        )
        result[project["id"]] = root / intel_db
    return result


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="control_centre_cli",
        description="VNX Control Centre — multi-project T0 supervisor",
    )
    parser.add_argument(
        "--registry",
        default=str(_DEFAULT_REGISTRY),
        help="Path to control_centre_projects.yaml (default: scripts/control_centre_projects.yaml)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="List all projects + T0 state")

    dp = sub.add_parser("dispatch", help="Forward dispatch to project T0")
    dp.add_argument("--project", required=True, help="Target project ID")
    dp.add_argument("--task", required=True, help="Dispatch instruction text")

    hb = sub.add_parser("heartbeat", help="Update T0 heartbeat")
    hb.add_argument("--project", required=True, help="Project ID")

    kp = sub.add_parser("kill", help="Graceful T0 shutdown")
    kp.add_argument("--project", required=True, help="Project ID")

    sub.add_parser("reap", help="Sweep stale T0 leases")

    ip = sub.add_parser("intel", help="Cross-project intelligence recommendations")
    ip.add_argument("--project", required=True, help="Target project ID")

    sub.add_parser("aggregate", help="Refresh global intelligence facet")

    return parser


_COMMANDS = {
    "status": cmd_status,
    "dispatch": cmd_dispatch,
    "heartbeat": cmd_heartbeat,
    "kill": cmd_kill,
    "reap": cmd_reap,
    "intel": cmd_intel,
    "aggregate": cmd_aggregate,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s")

    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
