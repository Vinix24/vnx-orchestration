"""Wave 1 shadow-mode divergence event logger (NDJSON append-only).

Consumes DivergenceEvent from shadow_verifier; writes to
``${VNX_STATE_DIR}/shadow_divergence.ndjson`` per ADR-005 (NDJSON ledger as
primary substrate). Same append+lock pattern as dual_writer._append_locked
(sentinel + LOCK_EX on data file — codex round-7 finding 4 fix from PR #432
round-8). State-dir resolution goes through vnx_paths.resolve_paths() so the
literal path lives only in the helper, not here.
"""

from __future__ import annotations

import fcntl
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from shadow_verifier import ComparisonResult, DivergenceEvent  # noqa: E402

LEDGER_FILENAME = "shadow_divergence.ndjson"
LOCK_FILENAME = "shadow_divergence.lock"

_DEFAULT_STATE_DIR = Path(".vnx-data/state")


def _resolve_ledger_path(ledger_path: Optional[Path]) -> Path:
    if ledger_path is not None:
        return ledger_path
    try:
        # Canonical resolver (VNX_HOME + project-marker aware). project_root's
        # resolve_state_dir(__file__) resolves the keystone git-root
        # (~/.vnx-system/current/.vnx-data) in a central install. See #1023.
        from vnx_paths import resolve_state_dir

        return resolve_state_dir() / LEDGER_FILENAME
    except Exception:
        return _DEFAULT_STATE_DIR / LEDGER_FILENAME


def _event_to_record(event: DivergenceEvent, sql_template_hash: str = "") -> dict:
    return {
        "event": "shadow_divergence",
        "metric_id": event.metric_id,
        "severity": event.severity,
        "project_id": event.project_id,
        "read_site": event.read_site,
        "sql_template_hash": sql_template_hash,
        "detail": event.detail,
        "legacy_count": event.legacy_count,
        "central_count": event.central_count,
        "timestamp_iso": event.timestamp_iso,
    }


def _append_locked(ledger_path: Path, record: dict) -> None:
    """Append record to ledger under sentinel + LOCK_EX on data file.

    Lock contract (mirrors dual_writer._append_locked, codex round-7 finding 4):
    1. Sentinel lock (LOCK_EX on sibling .lock file): excludes concurrent writers.
    2. Data-file lock (LOCK_EX on ledger): serialises against readers holding LOCK_SH.
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = ledger_path.parent / LOCK_FILENAME
    with sentinel.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        with ledger_path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n")


def write_event(event: DivergenceEvent, *, ledger_path: Optional[Path] = None) -> None:
    """Append a single divergence event to the NDJSON ledger.

    Uses sentinel lock + LOCK_EX on data file (per ADR-005 + dual_writer locking
    convention). Idempotent at the line level; same event written twice produces 2
    lines — caller is responsible for deduplication if needed.
    """
    resolved = _resolve_ledger_path(ledger_path)
    record = _event_to_record(event)
    _append_locked(resolved, record)


def write_comparison_result(
    result: ComparisonResult,
    project_id: str,
    read_site: str,
    *,
    ledger_path: Optional[Path] = None,
) -> int:
    """Convenience: write all divergences from a ComparisonResult.

    Returns count of events written.
    """
    if not result.divergences:
        return 0
    resolved = _resolve_ledger_path(ledger_path)
    for event in result.divergences:
        record = _event_to_record(event, sql_template_hash=result.sql_template_hash)
        _append_locked(resolved, record)
    return len(result.divergences)
