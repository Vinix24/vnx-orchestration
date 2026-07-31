#!/usr/bin/env python3
"""gate_obligations.py — review-gate obligations: the durable link between a
``gate=<name>`` declaration in a dispatch spec and actual review-gate evidence.

OI-876 / OI-881 (2026-07-31): the dispatch spec carried ``gate=codex_gate`` all
the way into the staged bundle, but after ``dispatch_cli.load_spec`` read the
field nothing consumed it — nine dispatches and ten merged PRs produced zero
request records and zero result records while every one of them declared a
gate. A dispatch whose declared gate never ran was indistinguishable from one
whose gate did run.

This module is the registry half of the fix:

  1. The dispatch door (``dispatch_cli.run_dispatch``) calls
     :func:`register_obligation` for every accepted dispatch whose spec
     declares a gate. One obligation record per dispatch, written to
     ``<state_dir>/review_gates/obligations/<dispatch_id>.json``.
  2. ``scripts/gate_obligation_runner.py`` fulfils pending obligations: it
     resolves the dispatch's PR and invokes ``review_gate_manager`` so the
     request record AND the result record actually land — or, when the gate
     cannot run, records a loud ``not_executable``/``failed`` outcome via
     ``gate_recorder`` (which also writes both records). Silence is no longer
     a possible end state.
  3. ``producer_freshness.scan_gate_obligations`` groups obligations per gate
     key and flags a key whose oldest pending declaration exceeds cadence —
     declaration without evidence becomes a finding, per sleutel, never per
     directory.

Design notes:
  - Best-effort at the door: :func:`register_obligation` never raises; the
    door must never be blocked by bookkeeping (same contract as
    ``_persist_dispatch_row``).
  - Idempotent: re-registering an existing obligation (retry / fix-forward
    with the same dispatch_id) leaves the existing record untouched — a
    fulfilled obligation is never reset to pending.
  - Atomic writes: tmp-file + ``os.replace`` (lint pattern B).
  - No central-DB write path: plain JSON files under the state dir, keeping
    ADR-007 out of scope exactly like the review_gates requests/results
    directories themselves.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

OBLIGATIONS_SUBDIR = Path("review_gates") / "obligations"

STATUS_PENDING = "pending"
STATUS_FULFILLED = "fulfilled"
STATUS_NOT_EXECUTABLE = "not_executable"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset({STATUS_FULFILLED, STATUS_NOT_EXECUTABLE, STATUS_FAILED})

# Whole-string match: bare digits, or the PR-<digits> family. Contract slugs
# like "pr-4d" must NOT resolve to PR 4 — they are not GitHub PRs.
_PR_NUMBER_RE = re.compile(r"^(?:#|PR[- ]?#?)?(\d+)$", re.IGNORECASE)


def obligations_dir(state_dir: Path) -> Path:
    return Path(state_dir) / OBLIGATIONS_SUBDIR


def obligation_path(state_dir: Path, dispatch_id: str) -> Path:
    return obligations_dir(state_dir) / f"{dispatch_id}.json"


def pr_number_from_pr_id(pr_id: Optional[str]) -> Optional[int]:
    """Extract an integer PR number from a pr_id string.

    Accepts the shapes the fabric actually uses: ``"879"``, ``"PR-879"``,
    ``"pr879"``, ``"#879"``. Returns None when no digits are present (e.g.
    contract slugs like ``pr-4d`` — those are not GitHub PRs).
    """
    if not pr_id:
        return None
    match = _PR_NUMBER_RE.match(str(pr_id).strip())
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _utc_now_iso() -> str:
    # Stdlib-only on purpose: this module is imported by the dispatch door,
    # which must never fail on a transitive scripts/-side import chain.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            logger.debug("gate_obligations: tmp cleanup failed for %s", tmp_name)
        raise


def register_obligation(
    state_dir: Path,
    *,
    dispatch_id: str,
    gate: str,
    project_id: str = "",
    pr_number: Optional[int] = None,
    branch: Optional[str] = None,
) -> Optional[Path]:
    """Register the review-gate obligation for a door-accepted dispatch.

    Returns the obligation path, or None when the registration was skipped
    (empty gate/dispatch_id, or an OS error — this function NEVER raises;
    obligation bookkeeping must never block the door).

    Idempotent: an existing obligation for the same dispatch_id is left
    untouched, so a retry never resets a fulfilled obligation to pending.
    """
    gate = (gate or "").strip()
    dispatch_id = (dispatch_id or "").strip()
    if not gate or not dispatch_id:
        return None
    try:
        path = obligation_path(state_dir, dispatch_id)
        if path.exists():
            return path
        record: Dict[str, Any] = {
            "schema_version": 1,
            "kind": "review_gate_obligation",
            "dispatch_id": dispatch_id,
            "gate": gate,
            "project_id": project_id or "",
            "declared_at": _utc_now_iso(),
            "pr_number": pr_number,
            "branch": branch,
            "status": STATUS_PENDING,
            "attempts": 0,
            "last_attempt_at": None,
            "resolved_at": None,
            "request_path": None,
            "result_path": None,
            "reason": None,
            "reason_detail": None,
        }
        _atomic_write_json(path, record)
        return path
    except OSError as exc:
        logger.warning(
            "gate_obligations: registration failed for dispatch=%s gate=%s (non-fatal): %s",
            dispatch_id, gate, exc,
        )
        return None


def iter_obligations(state_dir: Path) -> Iterator[Tuple[Path, Dict[str, Any]]]:
    """Yield (path, record) for every readable obligation, sorted by name.

    Unreadable files raise ValueError — an obligation that cannot be read is
    exactly the class of silent-evidence failure this mechanism exists to
    expose, so callers (the freshness scanner) surface it as a finding
    instead of skipping it.
    """
    root = obligations_dir(state_dir)
    if not root.is_dir():
        return
    for entry in sorted(root.glob("*.json")):
        if not entry.is_file():
            continue
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"unreadable gate obligation {entry.name}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"gate obligation {entry.name} is not a JSON object")
        yield entry, record


def update_obligation(path: Path, **fields: Any) -> Dict[str, Any]:
    """Atomically merge ``fields`` into an existing obligation record.

    Returns the updated record. Raises OSError/ValueError on unreadable or
    malformed existing content — the runner treats that as a loud failure,
    never a skip.
    """
    path = Path(path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot update unreadable gate obligation {path.name}: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"gate obligation {path.name} is not a JSON object")
    record.update(fields)
    _atomic_write_json(path, record)
    return record


__all__ = [
    "OBLIGATIONS_SUBDIR",
    "STATUS_PENDING",
    "STATUS_FULFILLED",
    "STATUS_NOT_EXECUTABLE",
    "STATUS_FAILED",
    "TERMINAL_STATUSES",
    "obligations_dir",
    "obligation_path",
    "pr_number_from_pr_id",
    "register_obligation",
    "iter_obligations",
    "update_obligation",
]
