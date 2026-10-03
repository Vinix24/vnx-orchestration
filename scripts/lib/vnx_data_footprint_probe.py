"""vnx_data_footprint_probe — how much disk a project's VNX store takes (OI-1944).

Reads one store (``<data_dir>``, the parent of the aggregator's state dir):
``events/archive`` bytes, plain (uncompressed) archive bytes older than the
retention window, ``salvage`` bytes, the data-home total and the free disk.

Classification:

- ``produces_crap``: free disk under 5% or the archive over 8 GiB.
- ``degraded``: plain archive bytes older than N+2 days above 0 (retention is not
  running), the archive over 3 GiB, salvage over 1 GiB, or free disk under 15%.
- ``ok`` otherwise.

N is ``VNX_EVENTS_ARCHIVE_COMPRESS_DAYS`` (default 14). Read-only: this probe
never compresses, moves or deletes anything.

ADR-007: one store per call, no central DB.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from effectiveness_probe import EffectivenessProbe, register_probe, resolve_probe_state_dir

COMPRESS_DAYS_ENV = "VNX_EVENTS_ARCHIVE_COMPRESS_DAYS"
DEFAULT_COMPRESS_DAYS = 14
GRACE_DAYS = 2

GIB = 1024 ** 3
ARCHIVE_DEGRADED_BYTES = 3 * GIB
ARCHIVE_CRAP_BYTES = 8 * GIB
SALVAGE_DEGRADED_BYTES = 1 * GIB
FREE_DEGRADED_FRACTION = 0.15
FREE_CRAP_FRACTION = 0.05


def compress_days() -> int:
    raw = os.environ.get(COMPRESS_DAYS_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_COMPRESS_DAYS
    return value if value >= 0 else DEFAULT_COMPRESS_DAYS


def _disk_usage(path: Path) -> Tuple[int, int]:
    """(total_bytes, free_bytes) of the volume holding ``path``."""
    usage = shutil.disk_usage(path)
    return usage.total, usage.free


def _walk_files(root: Path):
    """Regular files under ``root``; symlinks are neither followed nor counted."""
    if not root.is_dir():
        return
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield entry
        except OSError:
            continue


def _tree_bytes(root: Path) -> int:
    total = 0
    for entry in _walk_files(root):
        try:
            total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


@register_probe("vnx-data-footprint")
class VnxDataFootprintProbe(EffectivenessProbe):
    subsystem = "vnx-data-footprint"

    def __init__(self, state_dir: Optional[Path] = None) -> None:
        self._data_dir = resolve_probe_state_dir(state_dir).parent

    def probe(self) -> Dict[str, Any]:
        window_days = compress_days()
        cutoff = time.time() - (window_days + GRACE_DAYS) * 86400
        archive_bytes = 0
        plain_old_bytes = 0
        plain_old_files = 0
        for entry in _walk_files(self._data_dir / "events" / "archive"):
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            archive_bytes += st.st_size
            if entry.name.endswith(".ndjson") and st.st_mtime < cutoff:
                plain_old_bytes += st.st_size
                plain_old_files += 1
        total, free = _disk_usage(self._data_dir if self._data_dir.exists() else self._data_dir.parent)
        return {
            "data_dir": str(self._data_dir),
            "compress_days": window_days,
            "archive_bytes": archive_bytes,
            "plain_archive_bytes_older_than_window": plain_old_bytes,
            "plain_archive_files_older_than_window": plain_old_files,
            "salvage_bytes": _tree_bytes(self._data_dir / "salvage"),
            "data_home_bytes": _tree_bytes(self._data_dir.parent),
            "disk_total_bytes": total,
            "disk_free_bytes": free,
            "disk_free_fraction": (free / total) if total else 1.0,
        }

    def signal(self, raw: Dict[str, Any]) -> str:
        return (
            f"archive {raw['archive_bytes'] / GIB:.2f} GiB "
            f"({raw['plain_archive_files_older_than_window']} plain files past "
            f"{raw['compress_days'] + GRACE_DAYS} d), "
            f"salvage {raw['salvage_bytes'] / GIB:.2f} GiB, "
            f"free disk {raw['disk_free_fraction'] * 100:.1f}%"
        )

    def health(self, raw: Dict[str, Any]) -> str:
        if raw["disk_free_fraction"] < FREE_CRAP_FRACTION or raw["archive_bytes"] > ARCHIVE_CRAP_BYTES:
            return "produces_crap"
        if (
            raw["plain_archive_bytes_older_than_window"] > 0
            or raw["archive_bytes"] > ARCHIVE_DEGRADED_BYTES
            or raw["salvage_bytes"] > SALVAGE_DEGRADED_BYTES
            or raw["disk_free_fraction"] < FREE_DEGRADED_FRACTION
        ):
            return "degraded"
        return "ok"


__all__ = ["VnxDataFootprintProbe", "compress_days"]
