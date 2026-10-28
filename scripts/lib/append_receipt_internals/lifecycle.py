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

``decide_end_line`` is the single named hook for the append rule of ADR-045 §3
(K-3 fills it): a ``dispatch_finished`` is accepted only when the winner's
intent exists for its ``(project_id, dispatch_id, attempt)`` AND the line's
canonical form is byte-equal to the canonical line stored in that intent. Every
other end line for the key is refused with a named reason — published or not.
ADR-038 corrections do not apply to this type in the core (ADR-045 names the
exception). The intent itself is written by ``dispatch_lifecycle.finalize``
(step 1), never here; this module only reads it.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

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


# ---------------------------------------------------------------------------
# The end intent (ADR-045 §3): written by dispatch_lifecycle.finalize step 1 and
# read here for the append rule. One directory per project under the state dir
# that holds the ledger; one file per attempt.
# ---------------------------------------------------------------------------

END_INTENT_DIRNAME = "dispatch_end"
END_INTENT_VERSION = 1
END_INTENT_REQUIRED_KEYS = (
    "version", "project_id", "dispatch_id", "attempt", "end_state",
    "line", "sha256", "ledger_path", "ledger_offset",
)
# One canonical form for the writer (the finalizer) and the reader (the rule), so
# the two can never disagree on what "byte-equal" means.
_CANONICAL_SEPARATORS = (",", ":")


class EndIntentMalformed(ValueError):
    """An intent file is not a valid intent document."""


def canonical_end_line(line: Mapping[str, Any]) -> str:
    return json.dumps(dict(line), sort_keys=True, separators=_CANONICAL_SEPARATORS,
                      ensure_ascii=False, allow_nan=False)


def end_line_sha256(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def end_intent_path(state_dir: str | Path, project_id: str, dispatch_id: str,
                    attempt: int) -> Path:
    return (Path(state_dir) / END_INTENT_DIRNAME / str(project_id)
            / f"{dispatch_id}.{attempt}.json")


def end_intent_path_for_ledger(ledger_path: str | Path, key_source: Mapping[str, Any]) -> Path:
    """The intent path for a receipt, resolved from the ledger it is appended to."""
    return end_intent_path(Path(ledger_path).parent,
                           str(key_source.get("project_id") or ""),
                           str(key_source.get("dispatch_id") or ""),
                           int(key_source.get("attempt") or 0))


def build_end_intent(*, project_id: str, dispatch_id: str, attempt: int, end_state: str,
                     end_line: Mapping[str, Any], ledger_path: str | Path,
                     ledger_offset: int) -> Dict[str, Any]:
    """The intent document: the canonical end line and its sha256 (requirement 4)."""
    canonical = canonical_end_line(end_line)
    return {
        "version": END_INTENT_VERSION, "project_id": str(project_id),
        "dispatch_id": str(dispatch_id), "attempt": int(attempt),
        "end_state": str(end_state), "line": canonical,
        "sha256": end_line_sha256(canonical), "ledger_path": str(ledger_path),
        "ledger_offset": int(ledger_offset),
    }


def parse_end_intent(raw: str, *, source: str = "intent") -> Dict[str, Any]:
    """Validate and return an intent document; ``EndIntentMalformed`` on any gap."""
    def bad(detail: str) -> EndIntentMalformed:
        return EndIntentMalformed(f"{source}: {detail}")

    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise bad(f"not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise bad("not a JSON object")
    if missing := [key for key in END_INTENT_REQUIRED_KEYS if key not in doc]:
        raise bad(f"missing key(s) {missing}")
    if doc["version"] != END_INTENT_VERSION:
        raise bad(f"unsupported version {doc['version']!r}")
    for field, minimum in (("attempt", 1), ("ledger_offset", 0)):
        if not isinstance(doc[field], int) or isinstance(doc[field], bool) or doc[field] < minimum:
            raise bad(f"invalid {field} {doc[field]!r}")
    try:
        parsed = json.loads(doc["line"]) if isinstance(doc["line"], str) else None
    except json.JSONDecodeError as exc:
        raise bad(f"'line' is not JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise bad("'line' is not a non-empty JSON object")
    if doc["sha256"] != end_line_sha256(doc["line"]):
        raise bad("sha256 does not match 'line'")
    return doc


def load_end_intent(intent_path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = intent_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return parse_end_intent(raw, source=str(intent_path))


def end_line_matches_intent(receipt: Mapping[str, Any], intent: Mapping[str, Any]) -> bool:
    """True when the receipt's canonical line is byte-equal to the intent's.

    Only the fields the intent's line carries are compared; the append layer's
    derived stamps (``outcome_id``, ``schema_version``, ``writer_sha``) are not
    decisions, so they cannot cause a false mismatch.
    """
    expected = json.loads(intent["line"])
    canonical = canonical_end_line({key: receipt.get(key) for key in expected})
    return canonical == intent["line"] and end_line_sha256(canonical) == intent["sha256"]


def _refuse_intent_mismatch(receipt: Mapping[str, Any], detail: str, code: str) -> AppendReceiptError:
    return AppendReceiptError(
        code, EXIT_VALIDATION_ERROR,
        f"dispatch_finished refused: {detail} (project_id={receipt.get('project_id')!r}, "
        f"dispatch_id={receipt.get('dispatch_id')!r}, attempt={receipt.get('attempt')!r}). "
        "ADR-045 §3: an end line is accepted only against this attempt's intent.")


def decide_end_line(receipt: Dict[str, Any], prior_end: Optional[str], *,
                    ledger_path: Optional[Path] = None) -> str:
    """The append rule for a ``dispatch_finished`` (ADR-045 §3, K-3).

    Accepted only when the winner's intent for the key exists and the receipt's
    canonical line is byte-equal to it; refused by name as ``missing_end_intent``
    (requirement 4: no intent, published or not), ``invalid_end_intent``,
    ``duplicate_lifecycle_end`` or ``end_line_mismatch``. ADR-038 corrections do
    not apply to this type in the core.
    """
    intent_path = end_intent_path_for_ledger(ledger_path, receipt) if ledger_path else None
    try:
        intent = load_end_intent(intent_path) if intent_path is not None else None
    except EndIntentMalformed as exc:
        raise _refuse_intent_mismatch(
            receipt, f"the intent is malformed: {exc}", "invalid_end_intent") from exc
    if intent is None:
        raise _refuse_intent_mismatch(
            receipt, f"no intent exists at {intent_path}", "missing_end_intent")
    if ((intent["project_id"], intent["dispatch_id"], intent["attempt"])
            != (str(receipt.get("project_id")), str(receipt.get("dispatch_id")),
                receipt.get("attempt"))):
        raise _refuse_intent_mismatch(
            receipt, f"the intent at {intent_path} is for another attempt", "invalid_end_intent")
    if prior_end is not None:
        raise AppendReceiptError(
            "duplicate_lifecycle_end", EXIT_VALIDATION_ERROR,
            f"dispatch_finished refused: attempt (project_id={receipt.get('project_id')!r}, "
            f"dispatch_id={receipt.get('dispatch_id')!r}, attempt={receipt.get('attempt')!r}) "
            f"already has an end line (found via {prior_end}). One attempt gets exactly one "
            "end line; a new end is a new attempt, not a second line (ADR-045 §3).")
    if not end_line_matches_intent(receipt, intent):
        raise _refuse_intent_mismatch(
            receipt,
            "the line's canonical form differs from the intent's (the line decides "
            f"a different end, the intent says end_state={intent['end_state']!r})",
            "end_line_mismatch")
    return END_LINE_ACCEPTED


def check_end_line(
    receipt: Dict[str, Any],
    *,
    index_data: Optional[Dict[str, Any]],
    ledger_path: Path,
) -> str:
    """Enforce the end rule for ``dispatch_finished``; a no-op for every other type."""
    if str(receipt.get("event_type") or "").strip() != DISPATCH_FINISHED_EVENT_TYPE:
        return END_LINE_ACCEPTED
    prior = find_prior_end_line(receipt, index_data=index_data, ledger_path=ledger_path)
    return decide_end_line(receipt, prior, ledger_path=ledger_path)


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
    "END_INTENT_DIRNAME", "END_INTENT_VERSION", "EndIntentMalformed",
    "build_end_intent", "canonical_end_line", "check_end_line", "decide_end_line",
    "end_intent_path", "end_intent_path_for_ledger",
    "end_line_matches_intent", "end_line_sha256", "find_prior_end_line",
    "lifecycle_enabled", "load_end_intent", "parse_end_intent",
    "record_lifecycle_end", "stamp_lifecycle", "writer_sha",
]
