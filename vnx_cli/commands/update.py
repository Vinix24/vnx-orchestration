#!/usr/bin/env python3
"""vnx update — version-flip for central VNX install (pre-central-install scaffolding).

Schema-bootstrap (--schema) is a no-op stub until CENTRAL-4 (idempotent schema bootstrap)
merges. Atomic symlink flip via os.replace() ensures no partial-swap window.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


VNX_GIT_REMOTE = "https://github.com/Vinix24/vnx-orchestration"
DEFAULT_KEEP_LAST = 3


def _resolve_root() -> Path:
    env_root = os.environ.get("VNX_HOME_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    candidate = Path.home() / ".vnx-system"
    if candidate.is_dir():
        return candidate

    # Sandbox: central install doesn't exist yet
    return (Path.home() / ".vnx-system-test").expanduser().resolve()


def _list_version_dirs(root: Path) -> list[Path]:
    versions_dir = root / "versions"
    if not versions_dir.is_dir():
        return []
    return sorted(
        [d for d in versions_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
    )


def _current_target(root: Path) -> Path | None:
    current = root / "current"
    if current.is_symlink():
        try:
            return current.resolve()
        except OSError:
            return None
    return None


def _fetch_version(root: Path, target: str, dry_run: bool) -> Path:
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

    return target_dir


def _atomic_symlink_flip(root: Path, target_dir: Path, dry_run: bool) -> None:
    current = root / "current"

    if dry_run:
        print(f"[dry-run] Would flip symlink: {current} -> {target_dir}")
        return

    root.mkdir(parents=True, exist_ok=True)
    tmp_link = root / "current.tmp"

    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink(missing_ok=True)

    tmp_link.symlink_to(target_dir)
    os.replace(tmp_link, current)
    print(f"Activated: {current} -> {target_dir}")


def _prune_old_versions(root: Path, keep_last: int, dry_run: bool) -> None:
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
    target: str | None = getattr(args, "to_version", None)
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

    # Schema bootstrap: no-op until CENTRAL-4
    print("Warning: schema-bootstrap skipped (no-op until CENTRAL-4 merges).")

    try:
        target_dir = _fetch_version(root, target, dry_run=dry_run)
        _atomic_symlink_flip(root, target_dir, dry_run=dry_run)
        _prune_old_versions(root, keep_last=keep_last, dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        print(f"Error: git operation failed: {exc}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[dry-run] Update to '{target}' would succeed.")
    else:
        print(f"VNX updated to '{target}'.")

    return 0
