"""ADR-045 (audit-spoor-kern K-1): the append-layer lifecycle seam.

Two jobs, both in the ONE place the append path can do them deterministically:

1. **Stamp.** The three lifecycle line types (``dispatch_started`` /
   ``dispatch_finished`` / ``report_ingested``) are the only receipts stamped
   ``schema_version: 3``, and the only ones carrying ``writer_sha`` — the
   fabric commit that wrote the line, resolved once per process (ADR-045 §8).
   A ``dispatch_finished`` line also carries the ``status`` its ``end_state``
   projects to, so every existing status reader sees the projection without a
   second mapping (ADR-045 §4). Every other type keeps
   ``CURRENT_SCHEMA_VERSION`` (2), byte-for-byte as before.

2. **Decide.** A second ``dispatch_finished`` for the same
   ``(project_id, dispatch_id, attempt)`` is refused. The lookup is the
   ADR-038 durable outcome index (``receipt_outcome_index.json``), with a
   bounded tail scan of the ledger as the fallback for the case that matters:
   the index write is fail-open by design, so a recently appended end line can
   be in the ledger and not yet in the index.

``decide_end_line`` is the single named hook K-3 fills with the byte-equality
against ``state/dispatch_end/<project_id>/<dispatch_id>.<attempt>.json``.
Today it accepts a first end line and refuses a second one; no intent is
written or read yet.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import AppendReceiptError, EXIT_IO_ERROR, EXIT_VALIDATION_ERROR, SCRIPTS_DIR

from config_registry import get_bool
from event_outcome_semantics import end_state_status
from ledger_schema_version import CURRENT_SCHEMA_VERSION, LIFECYCLE_SCHEMA_VERSION
from receipt_schema import (
    DISPATCH_FINISHED_EVENT_TYPE,
    is_lifecycle_event_type,
    lifecycle_end_key,
)

# ADR-045 §9: the setting and its single default. Off is the shipped state and
# the emergency brake — with it off no lifecycle line is written at all.
LIFECYCLE_SETTING_KEY = "VNX_LEDGER_LIFECYCLE"

# The fallback scan reads only the last 256 KiB of the ledger: an end line for
# a live attempt is by construction recent, and the index (the primary lookup)
# spans the whole ledger. A match older than this window is not seen; that is
# the bound the ADR names, not an accident.
LIFECYCLE_TAIL_BYTES = 262_144

END_LINE_ACCEPTED = "accept"


def lifecycle_enabled() -> bool:
    """True when ``ledger_lifecycle`` is on (ADR-045 §9, default off)."""
    return get_bool(LIFECYCLE_SETTING_KEY)


@functools.lru_cache(maxsize=1)
def writer_sha() -> str:
    """The commit of the checkout THIS module lives in, resolved once per process.

    Not the project root: ADR-045 §8 defines ``writer_sha`` as the commit of the
    code that wrote the line, and the cohort test compares
    ``git merge-base --is-ancestor <activation> <writer_sha>`` against it. The
    project root is ``project_root.resolve_project_root``, which the central-mode
    path gate reserves for the project — asking it here would name the project
    in a central install, not the fabric. So the probe runs ``git -C <scripts
    dir>``: git walks up from any directory inside a checkout to that checkout's
    head, which is the dev repo, a dispatch worktree, or
    ``~/.vnx-system/versions/<v>``. ``scripts/check_no_file_derived_data_paths.py``
    (OI-1145 shape 3a) blocks the resolver route here.

    Fail-closed: an unresolvable sha is an ``AppendReceiptError``, never an empty
    required field — an unknown provenance is worse than no line.
    """
    try:
        probe = subprocess.run(["git", "-C", str(SCRIPTS_DIR), "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppendReceiptError("writer_sha_unresolved", EXIT_IO_ERROR,
                                 f"cannot resolve writer_sha for {SCRIPTS_DIR}: {exc}") from exc
    sha = probe.stdout.strip()
    if probe.returncode != 0 or not sha:
        raise AppendReceiptError(
            "writer_sha_unresolved", EXIT_IO_ERROR,
            f"git rev-parse HEAD failed in {SCRIPTS_DIR} (exit {probe.returncode}): "
            f"{probe.stderr.strip() or 'no output'}")
    return sha


def stamp_lifecycle(receipt: Dict[str, Any]) -> None:
    """The one stamp site for a receipt's schema generation (ADR-045 §8).

    Called for every append, so the non-lifecycle branch is the existing
    behaviour unchanged (``setdefault`` — a caller may pin its own version). The
    lifecycle branch is authoritative: the ledger stamps the version and the
    provenance, the caller does not supply them.
    """
    event_type = str(receipt.get("event_type") or receipt.get("event") or "").strip()
    if not is_lifecycle_event_type(event_type):
        receipt.setdefault("schema_version", CURRENT_SCHEMA_VERSION)
        return
    if not lifecycle_enabled():
        raise AppendReceiptError(
            "ledger_lifecycle_disabled", EXIT_VALIDATION_ERROR,
            f"{event_type} refused: ledger_lifecycle is off (ADR-045 §9 — the setting "
            "is the emergency brake; no lifecycle line is written while it is off). "
            "Set VNX_LEDGER_LIFECYCLE=1 to open the path.")
    receipt["schema_version"] = LIFECYCLE_SCHEMA_VERSION
    receipt["writer_sha"] = writer_sha()
    if event_type != DISPATCH_FINISHED_EVENT_TYPE:
        return
    projected = end_state_status(receipt.get("end_state"))
    if projected is None:
        return  # an unknown/absent end_state is refused by the field contract
    supplied = receipt.get("status")
    if supplied is not None and str(supplied).strip().lower() != projected:
        raise AppendReceiptError(
            "lifecycle_status_conflict", EXIT_VALIDATION_ERROR,
            f"{event_type} end_state={receipt.get('end_state')!r} projects to status "
            f"{projected!r}, but the line carries status {supplied!r}; refusing to "
            "write a line whose own projection contradicts it.")
    receipt["status"] = projected


def _tail_lines(ledger_path: Path) -> List[bytes]:
    """The last ``LIFECYCLE_TAIL_BYTES`` of the ledger, as whole lines."""
    try:
        with ledger_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            start = max(0, handle.tell() - LIFECYCLE_TAIL_BYTES)
            handle.seek(start)
            blob = handle.read()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise AppendReceiptError(
            "lifecycle_end_lookup_failed", EXIT_IO_ERROR,
            f"cannot read the ledger tail at {ledger_path} to check for an "
            f"existing dispatch_finished: {exc}") from exc
    lines = blob.split(b"\n")
    lines = lines[1:] if start > 0 else lines  # the first record may be cut mid-line
    return [line for line in lines if line.strip()]


def find_prior_end_line(
    receipt: Dict[str, Any],
    *,
    index_data: Optional[Dict[str, Any]],
    ledger_path: Path,
) -> Optional[str]:
    """Where this receipt's ``(project_id, dispatch_id, attempt)`` already ended.

    The index's ``outcome_id``, the literal ``"ledger_tail"``, or None. The
    index is the whole-ledger lookup; the bounded tail scan backstops the
    fail-open index write. A torn line (a crash mid-append) is not evidence of a
    complete end line, so it is skipped rather than guessed at.
    """
    key = lifecycle_end_key(receipt)
    lifecycle_index = (index_data or {}).get("lifecycle_index")
    if isinstance(lifecycle_index, dict) and lifecycle_index.get(key):
        return str(lifecycle_index[key])
    for raw in reversed(_tail_lines(ledger_path)):
        try:
            line = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if (isinstance(line, dict) and line.get("event_type") == DISPATCH_FINISHED_EVENT_TYPE
                and lifecycle_end_key(line) == key):
            return "ledger_tail"
    return None


def decide_end_line(receipt: Dict[str, Any], prior_end: Optional[str]) -> str:
    """The single named hook for writing a ``dispatch_finished`` (ADR-045 §3).

    Today (K-1): a first end line for the key is ``END_LINE_ACCEPTED``; a second
    one is refused with ``duplicate_lifecycle_end``. The intent file does not
    exist yet, so there is nothing to compare bytes with.

    **K-3 fills this body.** It reads
    ``state/dispatch_end/<project_id>/<dispatch_id>.<attempt>.json`` and accepts
    only a line that exists in an intent and is byte-equal to the canonical line
    in it (sha256 included). That replaces the ``prior_end`` test below, not
    merely the acceptance — a matching ``outcome_id`` is not enough, because it
    admits a different ``end_state``.
    """
    if prior_end is not None:
        raise AppendReceiptError(
            "duplicate_lifecycle_end", EXIT_VALIDATION_ERROR,
            "dispatch_finished refused: attempt (project_id="
            f"{receipt.get('project_id')!r}, dispatch_id={receipt.get('dispatch_id')!r}, "
            f"attempt={receipt.get('attempt')!r}) already has an end line (found via "
            f"{prior_end}). One attempt gets exactly one end line; a new end is a new "
            "attempt, not a second line (ADR-045 §3).")
    return END_LINE_ACCEPTED


def check_end_line(
    receipt: Dict[str, Any],
    *,
    index_data: Optional[Dict[str, Any]],
    ledger_path: Path,
) -> str:
    """Refuse a second end line for the attempt; a no-op for every other type."""
    if str(receipt.get("event_type") or "").strip() != DISPATCH_FINISHED_EVENT_TYPE:
        return END_LINE_ACCEPTED
    prior = find_prior_end_line(receipt, index_data=index_data, ledger_path=ledger_path)
    return decide_end_line(receipt, prior)


def record_lifecycle_end(
    receipt: Dict[str, Any], index_data: Dict[str, Any], outcome_id: str,
) -> None:
    """Record a written end line in the ADR-038 index's lifecycle projection.

    ``record_outcome`` (the index's own writer) calls this for every appended
    line; a non-end line is ignored. The value is the outcome_id, which is what
    a caller of :func:`find_prior_end_line` needs to name the prior line.
    """
    if str(receipt.get("event_type") or "").strip() != DISPATCH_FINISHED_EVENT_TYPE:
        return
    index_data.setdefault("lifecycle_index", {})[lifecycle_end_key(receipt)] = outcome_id


__all__ = [
    "LIFECYCLE_SETTING_KEY", "LIFECYCLE_TAIL_BYTES", "END_LINE_ACCEPTED",
    "check_end_line", "decide_end_line", "find_prior_end_line", "lifecycle_enabled",
    "record_lifecycle_end", "stamp_lifecycle", "writer_sha",
]
