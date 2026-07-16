#!/usr/bin/env python3
"""staging_validator.py — Enforce staging→pending→promote dispatch gate (ADR-006).

Every dispatch fired via subprocess_dispatch or tmux_interactive_dispatch must
originate from .vnx-data/dispatches/pending/ (or /staging/) — the mandatory
human approval gate. Callers bypassing the gate must pass
--allow-unstaged --reason '<text>' for an explicit audit-logged override.

Closes the T0 direct-call bypass tracked in issue #17.
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Allowlist: alphanumeric start, then alphanumeric/underscore/dot/hyphen, max 128 chars total.
# Rejects slashes, backslashes, null bytes, shell metacharacters, and relative traversal.
_DISPATCH_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')


def _exists_in_dir(base: Path, dispatch_id: str) -> bool:
    """True if dispatch_id is present under *base* as a directory, bare file, or <id>.md.

    Includes defense-in-depth: the resolved candidate path must stay within base.
    """
    candidate = base / dispatch_id
    # Defense-in-depth: resolved path must not escape base (belt + regex suspenders).
    try:
        if not candidate.resolve().is_relative_to(base.resolve()):
            return False
    except (OSError, ValueError):
        return False
    return (
        (candidate / "dispatch.json").exists()
        or candidate.is_dir()
        or candidate.is_file()
        or (base / f"{dispatch_id}.md").is_file()
    )


def validate_staging_path(
    from_staging_id: Optional[str],
    allow_unstaged: bool,
    reason: Optional[str],
    *,
    data_dir: Optional[Path] = None,
    dispatch_id: Optional[str] = None,
) -> None:
    """Enforce the staging→pending→promote dispatch gate.

    Passes silently on success. Writes to stderr and calls sys.exit(1) on
    violation so callers can rely on the process exit code.

    Args:
        from_staging_id: Dispatch ID to validate against pending/ or staging/.
        allow_unstaged:  Explicit bypass (requires non-empty reason for audit trail).
        reason:          Audit reason string — required with allow_unstaged.
        data_dir:        VNX data root (auto-resolved via project_root when None).
        dispatch_id:     The --dispatch-id the caller is about to actually execute
            with (OI-627). Only the staging *id* was previously checked for
            existence — the lane then spawns the worker, worktree, and
            provenance trailer (VNX_CURRENT_DISPATCH_ID) under a separately
            supplied --dispatch-id with no cross-check, so a caller passing
            mismatched values silently commits under the wrong id and breaks
            the dispatch->commit provenance chain. When both are given they
            must match exactly. Optional (defaults to None = no check) so
            existing callers/tests that don't pass it are unaffected.
    """
    if allow_unstaged:
        if not reason or not reason.strip():
            _reject("--allow-unstaged requires a non-empty --reason")
        logger.warning(
            "staging_validator: unstaged dispatch override — reason=%r", reason
        )
        _data = _resolve_data_dir(data_dir)
        _write_audit_event(_data, from_staging_id, reason)
        return

    if from_staging_id and from_staging_id.strip():
        sid = from_staging_id.strip()
        # PATH TRAVERSAL FIX — validate format before any path join
        if not _DISPATCH_ID_RE.match(sid):
            _reject("staging-pending-flow violated: invalid dispatch_id format")
        # OI-627 follow-up: compare the RAW dispatch_id (no .strip()) against sid.
        # Both entry-points (subprocess_dispatch.py, tmux_interactive_dispatch.py)
        # thread the raw, unstripped args.dispatch_id into every downstream use
        # (worktree/branch name, VNX_CURRENT_DISPATCH_ID env var, commit trailer).
        # Stripping only for this comparison let a caller pass e.g. "real-id "
        # (trailing whitespace) and slip past the guard while downstream code
        # still executed under the differing raw value — reopening the exact
        # provenance break this guard exists to close. Comparing raw-to-raw
        # means guard-pass implies byte-for-byte identity with what runs next.
        if dispatch_id is not None and dispatch_id != sid:
            _reject(
                f"staging-pending-flow violated: --dispatch-id {dispatch_id!r} "
                f"does not match --from-staging-id {sid!r} — the executed dispatch "
                "(worktree, worker env, commit provenance trailer) must run under the "
                "same id that was staged"
            )
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


def _write_audit_event(
    data_dir: Path,
    dispatch_id: Optional[str],
    reason: str,
) -> None:
    """Append NDJSON audit event for unstaged override (ADR-005)."""
    events_dir = data_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dispatch_id": dispatch_id,
        "reason": reason,
        "event_type": "unstaged_override",
        "actor": getpass.getuser(),
        "pid": os.getpid(),
    }
    audit_file = events_dir / "staging_validator.ndjson"
    with audit_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")
    logger.debug("staging_validator: ADR-005 audit event written to %s", audit_file)
