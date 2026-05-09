"""Phase 6 P4 dual-writer shim.

Centralizes the "write a record to BOTH the per-project NDJSON AND the
central NDJSON path" pattern. Every existing dual-write site
(``append_receipt`` mirror, ``dispatch_register`` mirror) routes through
``mirror_record_to_central`` so that there is a single place to:

- enforce the P5 cutover guard (skip when central path == primary path)
- enforce atomic, fcntl-locked appends to the central file
- enforce idempotency via the directory sentinel lock so concurrent
  re-stampers cannot orphan an open fd to a replaced inode

This shim exists for the lifetime of Phases 4 and 5; Phase 6 P6 deletes
this module and the call sites revert to single-path writes against the
central DB.

Codex defense:
- Atomic open/append: ``open(path, "a")`` writes a single newline-
  terminated JSON line under ``LOCK_EX`` on a sentinel that lives in the
  same directory.
- All call sites use the helper: ``append_receipt`` and ``dispatch_register``
  delegate exclusively here for the central mirror; no inline mirror code
  remains.
- No double-write on cross-store mirror: ``primary_path.resolve() ==
  central_path.resolve()`` short-circuits with ``False`` and writes nothing.

The module is intentionally tiny (~30 LOC of logic + docstring) so the
behavior is auditable at a glance.
"""

from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def resolve_central_ndjson_path(project_id: str, filename: str) -> Optional[Path]:
    """Return ``~/.vnx-data/<project_id>/state/<filename>`` or None on bad pid."""
    pid = (project_id or "").strip()
    if not pid:
        return None
    try:
        from vnx_paths import resolve_central_data_dir  # type: ignore
        central_base = resolve_central_data_dir(pid)
    except Exception:
        return None
    return central_base / "state" / filename


def _is_cutover_skip(primary_path: Path, central_path: Path) -> bool:
    primary_resolved = primary_path.resolve() if primary_path.exists() else primary_path
    if central_path.exists() and central_path.resolve() == primary_resolved:
        return True
    if not central_path.exists() and str(central_path) == str(primary_resolved):
        return True
    return False


_DEFAULT_LOCK_FILENAME = ".state.lock"

# Per-file lock-name overrides preserve the historical lock conventions of
# pre-helper call sites; readers/writers in adjacent code paths grab the
# same lock file rather than racing against the helper.
_LOCK_FILENAME_BY_TARGET: Dict[str, str] = {
    "t0_receipts.ndjson": "append_receipt.lock",
}


def _resolve_lock_filename(target_filename: str, override: Optional[str]) -> str:
    if override:
        return override
    return _LOCK_FILENAME_BY_TARGET.get(target_filename, _DEFAULT_LOCK_FILENAME)


def _append_locked(central_path: Path, record: Dict[str, Any], lock_filename: str) -> None:
    """Append record to central_path under two fcntl locks.

    Lock contract (codex round-7 finding 4):
    1. Sentinel lock (LOCK_EX on ``lock_filename``): excludes concurrent
       writers from each other and from re-stampers that hold the sentinel
       while replacing the inode.
    2. Data-file lock (LOCK_EX on central_path): serialises against readers
       that hold LOCK_SH on the data file (e.g. dispatch_register
       ``_read_register_locked``).  Without this second lock a reader can
       interleave with an append mid-write and observe a truncated last line.

    Both ``_write_event_locked`` (primary path writer) and this helper must
    hold the data-file lock so readers and writers share the same lock surface.
    """
    central_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = central_path.parent / lock_filename
    with sentinel.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        with central_path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n")


def mirror_record_to_central(
    record: Dict[str, Any],
    primary_path: Path,
    project_id: str,
    filename: str,
    lock_filename: Optional[str] = None,
) -> bool:
    """Best-effort append of ``record`` to the central NDJSON.

    Returns True iff the central write succeeded; False on cutover skip,
    missing/invalid project_id, or any I/O error. Never raises.
    """
    central_path = resolve_central_ndjson_path(project_id, filename)
    if central_path is None:
        return False
    try:
        if _is_cutover_skip(primary_path, central_path):
            return False
        _append_locked(central_path, record, _resolve_lock_filename(filename, lock_filename))
        return True
    except Exception:
        return False


def mirror_record_to_central_strict(
    record: Dict[str, Any],
    primary_path: Path,
    project_id: str,
    filename: str,
    lock_filename: Optional[str] = None,
) -> bool:
    """Strict variant: raises on I/O errors so callers can queue the record.

    Returns True iff the central write succeeded; False ONLY on cutover skip
    or missing/invalid project_id. Raises ``OSError`` on I/O failure.
    """
    central_path = resolve_central_ndjson_path(project_id, filename)
    if central_path is None:
        return False
    if _is_cutover_skip(primary_path, central_path):
        return False
    _append_locked(central_path, record, _resolve_lock_filename(filename, lock_filename))
    return True


def append_record_locked(
    central_path: Path,
    record: Dict[str, Any],
    lock_filename: Optional[str] = None,
    target_filename: Optional[str] = None,
) -> None:
    """Lower-level entrypoint: append ``record`` to ``central_path`` under a
    sibling fcntl lock.

    Resolves the lock filename in the same order as
    ``mirror_record_to_central`` so call sites that compute the central
    path themselves (e.g. legacy resolvers under monkey-patched tests)
    still share the global lock convention.
    """
    name = central_path.name if target_filename is None else target_filename
    _append_locked(central_path, record, _resolve_lock_filename(name, lock_filename))


__all__ = [
    "append_record_locked",
    "mirror_record_to_central",
    "mirror_record_to_central_strict",
    "resolve_central_ndjson_path",
]
