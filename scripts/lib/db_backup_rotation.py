"""Shared database-backup rotation — keep-N retention across backup mechanisms.

Extracted from ``scripts/quality_db_init.py``'s ``_rotate_quality_db_backups``
so the same pattern applies to every backup path without duplicated logic.

Rotation sorts by the filename (the backup path's ``.name``), NOT by mtime,
because ``shutil.copy2`` and ``VACUUM INTO`` preserve the source file's mtime —
every backup inherits the live DB's mtime rather than its own creation time. The
filename timestamp is the true, monotonic backup time; a zero-padded
``YYYYMMDD_HHMMSS`` or ``YYYYMMDDTHHMMSSZ`` suffix sorts lexicographically in
chronological order.

For backup naming schemes that do NOT embed a sortable timestamp (e.g.
``<db>.presnap.<label>.<pid>`` where ``<pid>`` is non-monotonic), callers pass
``sort_key=os.path.getmtime`` to fall back to mtime-based rotation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable


def parse_backup_keep(raw: str | None, default: int = 3) -> int:
    """Return a sane keep count from an env-var string.

    Falls back to ``default`` when the value is unset, invalid, or < 1.
    Never returns 0 so the just-made backup is never deleted.
    """
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    return value if value >= 1 else default


def rotate_backups(
    state_dir: Path,
    prefix: str,
    keep: int,
    *,
    sort_key: Callable[[Path], object] | None = None,
) -> None:
    """Keep only the newest ``keep`` files matching ``prefix`` in *state_dir*.

    Prunes the backup database files plus any matching ``-wal`` / ``-shm``
    sidecars. Errors are logged but not raised: rotation is best-effort.

    Args:
        state_dir: Directory containing the backup files.
        prefix: Filename prefix to match (e.g. ``"quality_intelligence.db.backup_"``).
            Files whose ``.name`` does NOT start with ``prefix`` are ignored, as
            are files whose name ends with ``-wal`` or ``-shm`` (sidecars are
            handled per-backup).
        keep: Number of most-recent backups to retain. Must be >= 1.
        sort_key: Key function for sorting ``Path`` objects. Defaults to
            ``lambda p: p.name`` (sort by filename, which works for
            timestamp-embedded names). Pass ``os.path.getmtime`` for backup
            schemes that don't embed a sortable timestamp.
    """
    keep = parse_backup_keep(str(keep) if keep is not None else None)
    if sort_key is None:
        sort_key = lambda p: p.name  # noqa: E731

    # Only consider the actual DB backups; sidecars are handled per-backup.
    backups = [
        p for p in state_dir.glob(f"{prefix}*")
        if not (p.name.endswith("-wal") or p.name.endswith("-shm"))
    ]
    # Sort newest-first: the sort_key should produce a monotonic ordering
    # where higher values = newer (for name-based timestamps with zero-padded
    # YYYYMMDD, lexicographic sort is chronological; reverse=True gives newest
    # first).
    backups.sort(key=sort_key, reverse=True)

    kept = backups[:keep]
    pruned = backups[keep:]

    if not pruned:
        return

    for old in pruned:
        try:
            old.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                sidecar = state_dir / f"{old.name}{suffix}"
                sidecar.unlink(missing_ok=True)
        except OSError:
            pass


def rotate_backups_safe(
    state_dir: Path,
    prefix: str,
    keep: int,
    *,
    sort_key: Callable[[Path], object] | None = None,
    log_fn: Callable[[str, str], None] | None = None,
) -> None:
    """Best-effort wrapper: rotation failure never raises.

    Wraps :func:`rotate_backups` so callers can invoke it after a backup
    without worrying about rotation failures aborting the operation.
    """
    try:
        rotate_backups(state_dir, prefix, keep, sort_key=sort_key)
    except Exception as exc:
        if log_fn is not None:
            log_fn("WARNING", f"Backup rotation failed (backup itself succeeded): {exc}")
