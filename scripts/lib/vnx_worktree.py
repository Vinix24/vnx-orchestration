#!/usr/bin/env python3
"""VNX Worktree — Python-led worktree detection, snapshot, and lifecycle.

PR-3 deliverable: migrates worktree-sensitive path resolution and lifecycle
operations from bin/vnx shell functions into testable Python. Replaces:

  - _detect_worktree_context()  → detect_worktree_context()
  - _snapshot_intelligence()    → snapshot_intelligence()
  - cmd_worktree_start()        → worktree_start()
  - cmd_worktree_stop()         → worktree_stop()
  - cmd_worktree_refresh()      → worktree_refresh()
  - cmd_worktree_status()       → worktree_status()

Design:
  - Path resolution uses pathlib.resolve() to handle symlinks correctly
    (fixes the relative_to() fragility in vnx_paths.py).
  - Git operations use subprocess for determinism.
  - All functions return structured results for operator/T0 review.
  - Intelligence hydration prefers git-tracked .vnx-intelligence/ over
    .vnx-data/ snapshot (matching existing behavior).

Governance:
  G-R2: Worktree state is traceable via .snapshot_meta.
  A-R4: Path resolution is deterministic across main repo and worktrees.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Worktree detection
# ---------------------------------------------------------------------------

@dataclass
class WorktreeContext:
    """Result of worktree detection."""
    is_worktree: bool
    worktree_root: str = ""     # _WT_ROOT equivalent
    main_root: str = ""         # _MAIN_ROOT equivalent
    error: str = ""


def detect_worktree_context(project_root: str = "") -> WorktreeContext:
    """Detect if we're in a git worktree and resolve roots.

    Replaces _detect_worktree_context() from bin/vnx (lines 1341-1353).
    Uses pathlib.resolve() to handle symlinks correctly.
    """
    if not project_root:
        project_root = os.environ.get("PROJECT_ROOT", os.getcwd())

    try:
        git_common_dir = subprocess.check_output(
            ["git", "-C", project_root, "rev-parse", "--git-common-dir"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return WorktreeContext(False, error="git rev-parse --git-common-dir failed")

    try:
        wt_root = subprocess.check_output(
            ["git", "-C", project_root, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return WorktreeContext(False, error="git rev-parse --show-toplevel failed")

    # Resolve to absolute canonical paths (symlink-safe)
    git_common = Path(git_common_dir).resolve()
    main_root = str(git_common.parent)
    wt_root_resolved = str(Path(wt_root).resolve())

    if main_root != wt_root_resolved:
        return WorktreeContext(True, wt_root_resolved, main_root)

    return WorktreeContext(False, wt_root_resolved, main_root)


# ---------------------------------------------------------------------------
# Intelligence snapshot
# ---------------------------------------------------------------------------

DB_NAMES = (
    "intelligence.db",
    "quality_intelligence.db",
    "unified_state.db",
    "vnx_intelligence.db",
)


def snapshot_intelligence(
    main_data: str,
    wt_data: str,
    db_subdir: str = "database",
    state_subdir: str = "state",
) -> int:
    """Copy intelligence data from main to worktree .vnx-data/.

    Replaces _snapshot_intelligence() from bin/vnx (lines 1355-1393).
    Returns the number of items copied.
    """
    main_path = Path(main_data)
    wt_path = Path(wt_data)
    copied = 0

    # Snapshot intelligence databases
    for db_name in DB_NAMES:
        src = main_path / db_subdir / db_name
        if src.is_file():
            dst = wt_path / db_subdir / db_name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            copied += 1

    # Snapshot receipt history
    receipt_src = main_path / state_subdir / "t0_receipts.ndjson"
    if receipt_src.is_file():
        receipt_dst = wt_path / state_subdir / "t0_receipts.ndjson"
        receipt_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(receipt_src), str(receipt_dst))
        copied += 1

    # Snapshot startup presets
    presets_src = main_path / "startup_presets"
    if presets_src.is_dir():
        presets_dst = wt_path / "startup_presets"
        presets_dst.mkdir(parents=True, exist_ok=True)
        for item in presets_src.iterdir():
            if item.is_file():
                shutil.copy2(str(item), str(presets_dst / item.name))
        copied += 1

    # Write snapshot metadata
    meta = wt_path / ".snapshot_meta"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta.write_text(
        f"snapshot_date={ts}\n"
        f"source_dir={main_data}\n"
        f"source_project={main_path.parent}\n"
    )

    return copied


# ---------------------------------------------------------------------------
# Worktree lifecycle
# ---------------------------------------------------------------------------

@dataclass
class WorktreeResult:
    """Result of a worktree lifecycle operation."""
    success: bool
    message: str
    details: List[str] = field(default_factory=list)
    error: str = ""


def worktree_start(
    project_root: str = "",
    ensure_layout_fn: Optional[Any] = None,
    intelligence_import_fn: Optional[Any] = None,
) -> WorktreeResult:
    """Initialize isolated .vnx-data for a worktree.

    Replaces cmd_worktree_start() from bin/vnx (lines 1395-1465).
    """
    ctx = detect_worktree_context(project_root)
    if not ctx.is_worktree:
        return WorktreeResult(
            False, "Not in a git worktree",
            error=ctx.error or "Run from a worktree directory. Create one with: vnx new-worktree <name>",
        )

    wt_data = Path(ctx.worktree_root) / ".vnx-data"
    main_data = Path(ctx.main_root) / ".vnx-data"

    if not main_data.is_dir():
        return WorktreeResult(
            False, f"Main repo has no .vnx-data at: {main_data}",
            error="Run 'vnx init' in the main repo first.",
        )

    # Remove old symlink model
    if wt_data.is_symlink():
        target = os.readlink(str(wt_data))
        wt_data.unlink()
        details = [f"Removed old symlink: {wt_data} -> {target}"]
    else:
        details = []

    # Already initialized?
    snapshot_meta = wt_data / ".snapshot_meta"
    if wt_data.is_dir() and snapshot_meta.is_file():
        return WorktreeResult(
            True, f"Worktree already initialized: {wt_data}",
            details=["Use 'vnx worktree-refresh' to update intelligence snapshot."],
        )

    details.append(f"Initializing isolated .vnx-data for worktree: {ctx.worktree_root}")

    # Create directory structure
    wt_data_str = str(wt_data)
    _ensure_worktree_layout(wt_data_str)

    # Call ensure_runtime_layout if provided
    if ensure_layout_fn:
        ensure_layout_fn()

    # Hydrate intelligence
    intel_dir = Path(ctx.worktree_root) / ".vnx-intelligence" / "db_export"
    if intel_dir.is_dir() and intelligence_import_fn:
        details.append("Found .vnx-intelligence/ in worktree — importing into SQLite...")
        try:
            intelligence_import_fn()
        except Exception:
            details.append("WARN: intelligence import failed, falling back to snapshot")
            copied = snapshot_intelligence(str(main_data), wt_data_str)
            details.append(f"Snapshot: {copied} items copied from main")
    else:
        copied = snapshot_intelligence(str(main_data), wt_data_str)
        details.append(f"Snapshot: {copied} items copied from main")

    # Write .env_override
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    env_override = wt_data / ".env_override"
    env_override.write_text(
        f"# Auto-generated by vnx worktree-start ({ts})\n"
        f"# Worktree: {ctx.worktree_root}\n"
        f"# Main repo: {ctx.main_root}\n"
        f"export VNX_DATA_DIR='{wt_data_str}'\n"
    )
    details.append(".env_override written for VNX_DATA_DIR isolation")

    # Create inbox on main
    inbox = main_data / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    return WorktreeResult(True, f"Isolated .vnx-data created: {wt_data}", details=details)


def worktree_stop(
    project_root: str = "",
    merge_only: bool = False,
    skip_merge: bool = False,
    intelligence_export_fn: Optional[Any] = None,
    merge_script: str = "",
) -> WorktreeResult:
    """Stop and optionally clean up a worktree's .vnx-data.

    Replaces cmd_worktree_stop() from bin/vnx (lines 1467-1531).
    """
    ctx = detect_worktree_context(project_root)
    if not ctx.is_worktree:
        return WorktreeResult(False, "Not in a git worktree", error=ctx.error)

    wt_data = Path(ctx.worktree_root) / ".vnx-data"
    main_data = Path(ctx.main_root) / ".vnx-data"

    if not wt_data.is_dir() or wt_data.is_symlink():
        return WorktreeResult(
            False, "No isolated .vnx-data found in worktree",
        )

    details: List[str] = []

    # Intelligence export/merge
    if not skip_merge:
        intel_dir = Path(ctx.worktree_root) / ".vnx-intelligence"
        qi_db = wt_data / "state" / "quality_intelligence.db"

        if (intel_dir.is_dir() or qi_db.is_file()) and intelligence_export_fn:
            details.append("Exporting intelligence to .vnx-intelligence/...")
            try:
                intelligence_export_fn()
            except Exception:
                details.append("WARN: intelligence export failed, falling back to merge script")
                if merge_script and Path(merge_script).is_file():
                    subprocess.run(["bash", merge_script, str(wt_data)], check=False)
        elif merge_script and Path(merge_script).is_file():
            details.append("Merging intelligence data back to main...")
            subprocess.run(["bash", merge_script, str(wt_data)], check=False)

        # Clean up inbox relay
        wt_name = Path(ctx.worktree_root).name
        inbox_file = main_data / "inbox" / f"wt-{wt_name}.ndjson"
        inbox_file.unlink(missing_ok=True)

    if merge_only:
        return WorktreeResult(
            True, f"Merge complete (--merge-only). Data preserved in: {wt_data}",
            details=details,
        )

    # Clean up
    details.append("Cleaning up worktree .vnx-data...")
    shutil.rmtree(str(wt_data), ignore_errors=True)

    return WorktreeResult(
        True, "Done. Worktree intelligence merged to main.",
        details=details,
    )


def worktree_refresh(project_root: str = "") -> WorktreeResult:
    """Update intelligence snapshot from main.

    Replaces cmd_worktree_refresh() from bin/vnx (lines 1533-1555).
    """
    ctx = detect_worktree_context(project_root)
    if not ctx.is_worktree:
        return WorktreeResult(False, "Not in a git worktree", error=ctx.error)

    wt_data = Path(ctx.worktree_root) / ".vnx-data"
    main_data = Path(ctx.main_root) / ".vnx-data"

    if not wt_data.is_dir() or wt_data.is_symlink():
        return WorktreeResult(
            False, "No isolated .vnx-data found. Run 'vnx worktree-start' first.",
        )

    if not main_data.is_dir():
        return WorktreeResult(
            False, f"Main repo .vnx-data not found: {main_data}",
        )

    copied = snapshot_intelligence(str(main_data), str(wt_data))
    return WorktreeResult(
        True, f"Intelligence snapshot updated ({copied} items).",
    )


@dataclass
class WorktreeInfo:
    """Information about a single worktree."""
    path: str
    branch: str
    data_status: str  # "ISOLATED", "SYMLINK", "DIR", "NONE"
    snapshot_date: str = ""


def worktree_status(project_root: str = "") -> Tuple[WorktreeContext, List[WorktreeInfo]]:
    """Get worktree status for all worktrees.

    Replaces cmd_worktree_status() from bin/vnx (lines 1557-1615).
    """
    if not project_root:
        project_root = os.environ.get("PROJECT_ROOT", os.getcwd())

    ctx = detect_worktree_context(project_root)

    # List all worktrees
    worktrees: List[WorktreeInfo] = []
    try:
        output = subprocess.check_output(
            ["git", "-C", project_root, "worktree", "list"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ctx, worktrees

    if not output:
        return ctx, worktrees

    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        wt_path = parts[0]
        branch = ""
        # Extract branch from [branch_name]
        for p in parts:
            if p.startswith("[") and p.endswith("]"):
                branch = p[1:-1]
                break

        wt_vnx_data = Path(wt_path) / ".vnx-data"
        if wt_vnx_data.is_symlink():
            status = "SYMLINK"
        elif wt_vnx_data.is_dir():
            meta = wt_vnx_data / ".snapshot_meta"
            if meta.is_file():
                status = "ISOLATED"
            else:
                status = "DIR"
        else:
            status = "NONE"

        snap_date = ""
        if status == "ISOLATED":
            meta_file = wt_vnx_data / ".snapshot_meta"
            try:
                for mline in meta_file.read_text().splitlines():
                    if mline.startswith("snapshot_date="):
                        snap_date = mline.split("=", 1)[1]
                        break
            except OSError:
                pass

        worktrees.append(WorktreeInfo(wt_path, branch, status, snap_date))

    return ctx, worktrees


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_worktree_layout(data_dir: str) -> None:
    """Create the .vnx-data directory structure for a worktree."""
    base = Path(data_dir)
    for subdir in (
        "state", "logs", "pids", "locks", "database",
        "unified_reports", "receipts", "profiles",
        "dispatches/pending", "dispatches/active",
        "dispatches/completed", "dispatches/rejected",
        "dispatches/failed", "startup_presets",
    ):
        (base / subdir).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VNX Worktree Operations")
    sub = parser.add_subparsers(dest="command")

    detect = sub.add_parser("detect", help="Detect worktree context")
    detect.add_argument("--project-root", default="")

    start = sub.add_parser("start", help="Initialize worktree .vnx-data")
    start.add_argument("--project-root", default="")

    stop = sub.add_parser("stop", help="Stop worktree")
    stop.add_argument("--project-root", default="")
    stop.add_argument("--merge-only", action="store_true")
    stop.add_argument("--skip-merge", action="store_true")
    stop.add_argument("--merge-script", default="")

    refresh = sub.add_parser("refresh", help="Refresh intelligence snapshot")
    refresh.add_argument("--project-root", default="")

    status = sub.add_parser("status", help="Show worktree status")
    status.add_argument("--project-root", default="")

    args = parser.parse_args()

    if args.command == "detect":
        ctx = detect_worktree_context(args.project_root)
        print(json.dumps({
            "is_worktree": ctx.is_worktree,
            "worktree_root": ctx.worktree_root,
            "main_root": ctx.main_root,
            "error": ctx.error,
        }))

    elif args.command == "start":
        result = worktree_start(args.project_root)
        print(json.dumps({
            "success": result.success,
            "message": result.message,
            "details": result.details,
            "error": result.error,
        }))
        if not result.success:
            sys.exit(1)

    elif args.command == "stop":
        result = worktree_stop(
            args.project_root,
            merge_only=args.merge_only,
            skip_merge=args.skip_merge,
            merge_script=args.merge_script,
        )
        print(json.dumps({
            "success": result.success,
            "message": result.message,
            "details": result.details,
            "error": result.error,
        }))
        if not result.success:
            sys.exit(1)

    elif args.command == "refresh":
        result = worktree_refresh(args.project_root)
        print(json.dumps({
            "success": result.success,
            "message": result.message,
        }))
        if not result.success:
            sys.exit(1)

    elif args.command == "status":
        ctx, wts = worktree_status(args.project_root)
        print(json.dumps({
            "context": {
                "is_worktree": ctx.is_worktree,
                "worktree_root": ctx.worktree_root,
                "main_root": ctx.main_root,
            },
            "worktrees": [
                {
                    "path": w.path,
                    "branch": w.branch,
                    "data_status": w.data_status,
                    "snapshot_date": w.snapshot_date,
                }
                for w in wts
            ],
        }, indent=2))
