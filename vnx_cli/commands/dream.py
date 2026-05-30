"""vnx dream — auto-dream consolidation CLI (ADR-019)."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from vnx_cli import _engine
_engine.ensure_engine_on_path()

_NOT_IN_PROJECT_MSG = (
    "not in a VNX project — run `vnx init` inside a git repo, or set VNX_CANONICAL_ROOT"
)


def _get_project_id(args) -> str | None:
    pid = getattr(args, "project_id", None)
    project_dir = Path(getattr(args, "project_dir", "."))
    try:
        return _engine.derive_project_id(project_dir, explicit=pid)
    except (ValueError, RuntimeError):
        return None


def _resolve_paths(project_dir: Path) -> tuple[Path, Path]:
    """Returns (project_root, db_path).

    db_path anchors on project_dir via _engine.resolve_data_root so pool/dream
    resolve the SAME DB as migrate/init/track/doctor (ADR-007, PR-RESOLVER-UNIFY).
    """
    from project_root import resolve_project_root  # type: ignore[import]
    try:
        root = resolve_project_root()
    except RuntimeError:
        print(f"error: {_NOT_IN_PROJECT_MSG}", file=sys.stderr)
        sys.exit(1)
    state_dir = _engine.resolve_data_root(project_dir) / "state"
    return root, state_dir / "quality_intelligence.db"


def _resolve_data_root(project_dir: Path) -> Path:
    """Return the canonical VNX data root anchored on project_dir."""
    return _engine.resolve_data_root(project_dir)


def _cmd_run(args) -> int:
    project_dir = Path(getattr(args, "project_dir", "."))
    project_id = _get_project_id(args)
    if not project_id:
        print("error: cannot resolve project_id; set --project-id or VNX_PROJECT_ID")
        return 1
    dry_run = getattr(args, "dry_run", False)
    root, db_path = _resolve_paths(project_dir)
    import consolidator  # type: ignore[import]
    result = consolidator.run_dream_cycle(project_id, db_path, dry_run=dry_run)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_status(args) -> int:
    project_dir = Path(getattr(args, "project_dir", "."))
    project_id = _get_project_id(args)
    if not project_id:
        print("error: cannot resolve project_id; set --project-id or VNX_PROJECT_ID")
        return 1
    _, db_path = _resolve_paths(project_dir)
    if not db_path.exists():
        print(f"No dream cycles found for project '{project_id}'.")
        return 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT cycle_id, status, completed_at, insights_input,"
            " merged_count, dropped_count, flagged_count, operator_reviewed"
            " FROM dream_cycles WHERE project_id=? ORDER BY completed_at DESC LIMIT 1",
            (project_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    if not rows:
        print(f"No dream cycles found for project '{project_id}'.")
        return 0
    r = rows[0]
    print(f"Latest dream cycle — '{project_id}'")
    print(f"  cycle_id    : {r['cycle_id']}")
    print(f"  status      : {r['status']}")
    print(f"  completed   : {r['completed_at']}")
    print(f"  input/merged/dropped/flagged: "
          f"{r['insights_input']}/{r['merged_count']}/{r['dropped_count']}/{r['flagged_count']}")
    print(f"  reviewed    : {bool(r['operator_reviewed'])}")
    import review_gate  # type: ignore[import]
    pending = review_gate.list_pending_reviews(project_id, _resolve_data_root(project_dir))
    if pending:
        print(f"\n  Pending reviews ({len(pending)}):")
        for p in pending:
            print(f"    - {p['cycle_id']}")
    return 0


def _cmd_review(args) -> int:
    project_dir = Path(getattr(args, "project_dir", "."))
    cycle_id: str = args.cycle_id
    project_id = _get_project_id(args)
    if not project_id:
        print("error: cannot resolve project_id; set --project-id or VNX_PROJECT_ID")
        return 1
    _, db_path = _resolve_paths(project_dir)
    data_root = _resolve_data_root(project_dir)
    import review_gate  # type: ignore[import]
    approve = getattr(args, "approve", False)
    reject = getattr(args, "reject", False)
    reason = getattr(args, "reason", "operator rejected")
    if not approve and not reject:
        answer = input(f"Cycle {cycle_id}: [a]pprove / [r]eject? ").strip().lower()
        approve = answer.startswith("a")
        reject = answer.startswith("r")
        if reject and not reason:
            reason = input("Rejection reason: ").strip() or "operator rejected"
    try:
        if reject:
            review_gate.reject_cycle(cycle_id, project_id, reason, db_path, data_root)
            print(f"Cycle {cycle_id} rejected.")
        else:
            review_gate.approve_cycle(cycle_id, project_id, db_path, data_root)
            print(f"Cycle {cycle_id} approved; consolidation applied.")
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


def _cmd_history(args) -> int:
    project_dir = Path(getattr(args, "project_dir", "."))
    project_id = _get_project_id(args)
    if not project_id:
        print("error: cannot resolve project_id; set --project-id or VNX_PROJECT_ID")
        return 1
    limit = getattr(args, "limit", 10)
    _, db_path = _resolve_paths(project_dir)
    if not db_path.exists():
        print(f"No dream cycles found for project '{project_id}'.")
        return 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT cycle_id, status, completed_at, insights_input,"
            " merged_count, dropped_count, archived_count, flagged_count, operator_reviewed"
            " FROM dream_cycles WHERE project_id=? ORDER BY completed_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    if not rows:
        print(f"No dream cycles found for project '{project_id}'.")
        return 0
    hdr = f"{'CYCLE_ID':<45} {'STATUS':<10} {'COMPLETED':<20} {'IN':>4} {'M':>3} {'D':>3} {'A':>3} {'F':>3} REV"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        rev = "Y" if r["operator_reviewed"] else "N"
        ts = str(r["completed_at"] or "")[:20]
        print(
            f"{r['cycle_id']:<45} {r['status']:<10} {ts:<20}"
            f" {r['insights_input']:>4} {r['merged_count']:>3} {r['dropped_count']:>3}"
            f" {r['archived_count']:>3} {r['flagged_count']:>3} {rev:>3}"
        )
    return 0


def _cmd_install_scheduler(args) -> int:
    project_dir = Path(getattr(args, "project_dir", "."))
    project_id = _get_project_id(args)
    if not project_id:
        print("error: cannot resolve project_id; set --project-id or VNX_PROJECT_ID")
        return 1
    root, _ = _resolve_paths(project_dir)
    import scheduler  # type: ignore[import]
    try:
        msg = scheduler.install_scheduler(project_id=project_id, project_root=root)
        print(msg)
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def _cmd_uninstall_scheduler(args) -> int:
    import scheduler  # type: ignore[import]
    try:
        msg = scheduler.uninstall_scheduler()
        print(msg)
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def vnx_dream(args) -> int:
    sub = getattr(args, "dream_subcommand", None)
    dispatch = {
        "run": _cmd_run,
        "status": _cmd_status,
        "review": _cmd_review,
        "history": _cmd_history,
        "install-scheduler": _cmd_install_scheduler,
        "uninstall-scheduler": _cmd_uninstall_scheduler,
    }
    if sub in dispatch:
        return dispatch[sub](args)
    print("Usage: vnx dream {run|status|review|history|install-scheduler|uninstall-scheduler}")
    return 1
