#!/usr/bin/env python3
"""staging_validator.py — Enforce staging→pending→promote dispatch gate (ADR-006).

Every dispatch fired via subprocess_dispatch or tmux_interactive_dispatch must
originate from .vnx-data/dispatches/pending/ (or /staging/) — the mandatory
human approval gate. Callers bypassing the gate must pass
--allow-unstaged --reason '<text>' for an explicit audit-logged override.

Closes the T0 direct-call bypass tracked in issue #17.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _exists_in_dir(base: Path, dispatch_id: str) -> bool:
    """True if dispatch_id is present under *base* as a directory, bare file, or <id>.md."""
    return (
        (base / dispatch_id / "dispatch.json").exists()
        or (base / dispatch_id).is_dir()
        or (base / dispatch_id).is_file()
        or (base / f"{dispatch_id}.md").is_file()
    )


def validate_staging_path(
    from_staging_id: Optional[str],
    allow_unstaged: bool,
    reason: Optional[str],
    *,
    data_dir: Optional[Path] = None,
) -> None:
    """Enforce the staging→pending→promote dispatch gate.

    Passes silently on success. Writes to stderr and calls sys.exit(1) on
    violation so callers can rely on the process exit code.

    Args:
        from_staging_id: Dispatch ID to validate against pending/ or staging/.
        allow_unstaged:  Explicit bypass (requires non-empty reason for audit trail).
        reason:          Audit reason string — required with allow_unstaged.
        data_dir:        VNX data root (auto-resolved via project_root when None).
    """
    if allow_unstaged:
        if not reason or not reason.strip():
            _reject("--allow-unstaged requires a non-empty --reason")
        logger.warning(
            "staging_validator: unstaged dispatch override — reason=%r", reason
        )
        return

    if from_staging_id and from_staging_id.strip():
        sid = from_staging_id.strip()
        _data = _resolve_data_dir(data_dir)
        dispatches = _data / "dispatches"
        if _exists_in_dir(dispatches / "pending", sid) or _exists_in_dir(
            dispatches / "staging", sid
        ):
            logger.debug("staging_validator: dispatch %r found — OK", sid)
            return
        _reject(
            f"staging-pending-flow violated: dispatch {sid!r} not found in "
            f"pending/ or staging/ under {dispatches}"
        )

    _reject(
        "staging-pending-flow violated: pass --from-staging-id <id> (must exist in "
        ".vnx-data/dispatches/pending/ or /staging/) or --allow-unstaged --reason '<text>'"
    )


def _reject(message: str) -> None:
    print(f"[staging_validator] ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _resolve_data_dir(data_dir: Optional[Path]) -> Path:
    if data_dir is not None:
        return data_dir
    try:
        from project_root import resolve_data_dir  # noqa: PLC0415
        return resolve_data_dir(caller_file=__file__)
    except Exception as exc:
        _reject(f"Cannot resolve VNX data dir: {exc}")
        raise  # unreachable; _reject exits
