#!/usr/bin/env python3
"""vnx update — version-flip for central VNX install.

Atomic symlink flip via os.replace() ensures no partial-swap window.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


VNX_GIT_REMOTE = "https://github.com/Vinix24/vnx-orchestration.git"
DEFAULT_KEEP_LAST = 3

_VERSION_RE = re.compile(r"^(edge|latest|v?\d+\.\d+\.\d+(?:-[\w.]+)?)$")


def _resolve_root() -> Path:
    env_root = os.environ.get("VNX_HOME_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    candidate = Path.home() / ".vnx-system"
    if candidate.is_dir():
        return candidate

    # Sandbox: central install doesn't exist yet
    return (Path.home() / ".vnx-system-test").expanduser().resolve()


def _resolve_audit_log() -> Path:
    """Resolve path for central install audit event log."""
    vnx_data = os.environ.get("VNX_DATA_DIR")
    if vnx_data:
        base = Path(vnx_data).expanduser().resolve()
    else:
        base = Path.home() / ".vnx-data"
    return base / "events" / "central_install.ndjson"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _emit_audit_event(
    event_type: str, fields: dict, audit_log: "Path | None" = None
) -> None:
    """Append an audit event to the central install NDJSON log with exclusive locking."""
    path = audit_log or _resolve_audit_log()
    record = {"event_type": event_type, "timestamp": _now_iso(), **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(line)


def _validate_version_name(target: str) -> str:
    """Validate and return a safe version name.

    Allowed: edge, latest, vX.Y.Z, X.Y.Z, vX.Y.Z-suffix (alphanumeric/./-)
    Raises ValueError on path traversal, shell metacharacters, or invalid format.
    """
    if not _VERSION_RE.match(target):
        raise ValueError(f"invalid version name: {target!r}")
    return target


def _list_version_dirs(root: Path) -> list:
    versions_dir = root / "versions"
    if not versions_dir.is_dir():
        return []
    return sorted(
        [d for d in versions_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
    )


def _current_target(root: Path):
    current = root / "current"
    if current.is_symlink():
        try:
            return current.resolve()
        except OSError:
            return None
    return None


def _fetch_version(root: Path, target: str, dry_run: bool) -> Path:
    # Belt-and-suspenders: validate before every path join regardless of call site
    _validate_version_name(target)

    versions_dir = root / "versions"
    target_dir = versions_dir / target

    if dry_run:
        print(f"[dry-run] Would clone/pull {VNX_GIT_REMOTE} -> {target_dir}")
        return target_dir

    versions_dir.mkdir(parents=True, exist_ok=True)

    if target_dir.is_dir():
        print(f"Pulling {target} in {target_dir}...")
        subprocess.run(
            ["git", "-C", str(target_dir), "pull", "--ff-only"],
            check=True,
        )
    else:
        ref = "main" if target == "edge" else target
        print(f"Cloning {VNX_GIT_REMOTE} (ref={ref}) -> {target_dir}...")
        subprocess.run(
            ["git", "clone", "--branch", ref, "--depth", "1",
             VNX_GIT_REMOTE, str(target_dir)],
            check=True,
        )

    # The installed engine tree must be TENANT-NEUTRAL. The repo tracks its own
    # `.vnx-project-id = vnx-dev`, so a clone/pull drags that marker into the shared
    # version dir. In central-install mode the door's CWD is this tree; a stray marker
    # there makes CWD-based project_id resolution return `vnx-dev` for EVERY consumer
    # (the fleet-wide misroute/hard-reject class). Strip it after every fetch.
    _strip_tenant_marker(target_dir)
    return target_dir


def _strip_tenant_marker(version_dir: Path) -> None:
    """Remove `.vnx-project-id` from an installed engine version dir (tenant-neutral)."""
    marker = version_dir / ".vnx-project-id"
    try:
        if marker.is_file():
            marker.unlink()
            print(f"Stripped stray tenant marker: {marker}")
    except OSError as exc:
        print(f"[warn] could not strip tenant marker {marker}: {exc}")


def _atomic_symlink_flip(
    root: Path, target_dir: Path, dry_run: bool, audit_log: "Path | None" = None
) -> None:
    current = root / "current"

    if dry_run:
        print(f"[dry-run] Would flip symlink: {current} -> {target_dir}")
        return

    from_version = _current_target(root)
    from_name = from_version.name if from_version else None
    to_name = target_dir.name

    root.mkdir(parents=True, exist_ok=True)
    tmp_link = root / "current.tmp"

    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink(missing_ok=True)

    tmp_link.symlink_to(target_dir)

    _emit_audit_event(
        "central_install_update",
        {"from_version": from_name, "to_version": to_name, "success": False, "phase": "before_flip"},
        audit_log=audit_log,
    )

    os.replace(tmp_link, current)

    _emit_audit_event(
        "central_install_update",
        {"from_version": from_name, "to_version": to_name, "success": True, "phase": "after_flip"},
        audit_log=audit_log,
    )

    print(f"Activated: {current} -> {target_dir}")


def _prune_old_versions(
    root: Path, keep_last: int, dry_run: bool, audit_log: "Path | None" = None
) -> None:
    versions = _list_version_dirs(root)
    current = _current_target(root)

    # Keep the newest keep_last dirs; prune anything older, skip current
    if len(versions) <= keep_last:
        return

    to_prune = versions[: len(versions) - keep_last]
    for version_dir in to_prune:
        if current and version_dir.resolve() == current.resolve():
            continue
        if dry_run:
            print(f"[dry-run] Would prune: {version_dir}")
        else:
            _emit_audit_event(
                "central_install_prune",
                {"pruned_version": version_dir.name, "keep_last_N": keep_last},
                audit_log=audit_log,
            )
            print(f"Pruning: {version_dir}")
            shutil.rmtree(version_dir)


def _do_rollback(root: Path, dry_run: bool) -> int:
    versions = _list_version_dirs(root)
    current = _current_target(root)

    if current is None:
        print("Error: no current symlink — nothing to roll back.", file=sys.stderr)
        return 1

    previous = [v for v in reversed(versions) if v.resolve() != current.resolve()]
    if not previous:
        print("Error: no previous version available for rollback.", file=sys.stderr)
        return 1

    prev_dir = previous[0]
    if dry_run:
        print(f"[dry-run] Would rollback current -> {prev_dir}")
        return 0

    _atomic_symlink_flip(root, prev_dir, dry_run=False)
    print(f"Rolled back to: {prev_dir.name}")
    return 0


def vnx_update(args) -> int:
    target: "str | None" = getattr(args, "to_version", None)
    keep_last: int = getattr(args, "keep_last", DEFAULT_KEEP_LAST)
    dry_run: bool = getattr(args, "dry_run", False)
    rollback: bool = getattr(args, "rollback", False)

    root = _resolve_root()

    if dry_run:
        print(f"[dry-run] VNX_HOME_ROOT: {root}")

    if rollback:
        return _do_rollback(root, dry_run=dry_run)

    if not target:
        print("Error: --to <version> is required (or use --rollback).", file=sys.stderr)
        return 1

    try:
        target = _validate_version_name(target)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        target_dir = _fetch_version(root, target, dry_run=dry_run)
        _atomic_symlink_flip(root, target_dir, dry_run=dry_run)
        _prune_old_versions(root, keep_last=keep_last, dry_run=dry_run)
    except FileNotFoundError:
        print("Error: git executable not found in PATH", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: git operation failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: OS error during update: {exc}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[dry-run] Update to '{target}' would succeed.")
        print("[dry-run] Would migrate all central per-project stores to the new schema.")
        return 0

    print(f"VNX updated to '{target}'.")

    # D4 fleet-sync: after flipping the engine version, migrate every central
    # per-project store so none is left half-migrated behind the newer engine.
    # Best-effort — a per-store failure is logged, never aborts the update.
    try:
        print("\nMigrating central per-project stores to the new schema ...")
        from vnx_cli.commands.migrate import migrate_all_central_stores
        migrate_all_central_stores()
    except Exception as exc:
        print(f"  warning: fleet store migration sweep failed: {exc}", file=sys.stderr)

    return 0
