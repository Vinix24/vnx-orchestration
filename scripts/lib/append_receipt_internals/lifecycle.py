"""ADR-045 (audit-spoor-kern K-1/K-3b): the append-layer lifecycle seam.

Three jobs, all in the ONE place the append path can do them deterministically:

1. **Stamp.** The three lifecycle line types (``dispatch_started`` /
   ``dispatch_finished`` / ``report_ingested``) are the only receipts stamped
   ``schema_version: 3``, and the only ones carrying ``writer_sha`` — the
   fabric commit that wrote the line, resolved once per process (ADR-045 §8).
   A ``dispatch_finished`` line also carries the ``status`` its ``end_state``
   projects to, so every existing status reader sees the projection without a
   second mapping (ADR-045 §4). Every other type keeps
   ``CURRENT_SCHEMA_VERSION`` (2), byte-for-byte as before.

2. **Freeze.** :func:`prepare_end_line` runs the caller's line through the
   append layer's own stamps and validation (the same ``payload._prepare_receipt``
   the write uses), then drops the derived stamps. A line the append would
   refuse raises here with the same code, so no intent is left behind (K-3b).

3. **Decide.** A ``dispatch_finished`` is accepted only when the winner's
   intent exists for its ``(project_id, dispatch_id, attempt)``, is well-formed,
   and is byte-equal (sha256 included) to the receipt on every field apart from
   :data:`DERIVED_STAMP_FIELDS` and, per warning, :data:`DERIVED_WARNING_FIELDS`;
   it is re-checked after the pre-write hook. K-3c's finalizer writes it, never here.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .common import AppendReceiptError, EXIT_IO_ERROR, EXIT_VALIDATION_ERROR, SCRIPTS_DIR
from .warning_destination import reapply_frozen_classification

from config_registry import get_bool
from event_outcome_semantics import end_state_status
from ledger_schema_version import CURRENT_SCHEMA_VERSION, LIFECYCLE_SCHEMA_VERSION
from receipt_schema import (
    DISPATCH_FINISHED_EVENT_TYPE,
    LIFECYCLE_END_STATES,
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


# The end intent (ADR-045 §3): one file per attempt under the state dir that
# holds the ledger, written by K-3c's finalizer and read here for the rule.
END_INTENT_DIRNAME = "dispatch_end"
END_INTENT_VERSION = 1
END_INTENT_REQUIRED_KEYS = (
    "version", "project_id", "dispatch_id", "attempt", "end_state",
    "line", "sha256", "ledger_path", "ledger_offset")
# One canonical form for writer and reader, so they cannot disagree on "byte-equal".
_CANONICAL_SEPARATORS = (",", ":")
_UNSAFE_COMPONENT_CHARS = ("/", "\\", "\x00")
_TRAVERSAL_COMPONENTS = (".", "..")


class EndIntentMalformed(ValueError):
    """An intent file is not a valid intent document."""


def canonical_end_line(line: Mapping[str, Any]) -> str:
    """The one canonical form the freeze and the comparison share.

    A value JSON cannot represent (a NaN or Infinity float, a non-serialisable
    object) is a named ``invalid_end_line`` refusal, not a bare ``ValueError``
    from deep inside ``json``: nothing is frozen from a line that has no
    canonical form (K-3b follow-up, 30-10).
    """
    try:
        return json.dumps(dict(line), sort_keys=True, separators=_CANONICAL_SEPARATORS,
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _refuse_invalid_end_line(
            f"the end line has no canonical JSON form: {exc}") from exc


def end_line_sha256(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _invalid_identifier(detail: str) -> AppendReceiptError:
    return AppendReceiptError("invalid_identifier", EXIT_VALIDATION_ERROR, detail)


def _safe_component(value: object, field: str) -> str:
    """One identifier as a single safe path component; raises ``invalid_identifier``."""
    if not isinstance(value, str) or not value.strip():
        raise _invalid_identifier(f"{field} must be a non-empty string, got {value!r}")
    if value in _TRAVERSAL_COMPONENTS:
        raise _invalid_identifier(f"{field} may not be {value!r}")
    if any(char in value for char in _UNSAFE_COMPONENT_CHARS):
        raise _invalid_identifier(f"{field} may not carry a separator or NUL: {value!r}")
    if Path(value).is_absolute() or os.path.isabs(value):
        raise _invalid_identifier(f"{field} may not be an absolute path: {value!r}")
    return value


def end_intent_path(state_dir: str | Path, project_id: str, dispatch_id: str,
                    attempt: int) -> Path:
    """``<state>/dispatch_end/<project_id>/<dispatch_id>.<attempt>.json``.

    All three are one safe path component and the resolved path must stay under
    the root; else ``invalid_identifier`` and nothing created (codex r1).
    """
    project = _safe_component(project_id, "project_id")
    dispatch = _safe_component(dispatch_id, "dispatch_id")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise _invalid_identifier(f"attempt must be a positive integer, got {attempt!r}")
    root = Path(state_dir) / END_INTENT_DIRNAME
    candidate = root / project / f"{dispatch}.{attempt}.json"
    try:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
    except OSError as exc:
        raise _invalid_identifier(f"cannot resolve the intent path {candidate}: {exc}") from exc
    if not resolved_candidate.is_relative_to(resolved_root):
        raise _invalid_identifier(f"{candidate} resolves outside {resolved_root}")
    return candidate


def end_intent_path_for_ledger(ledger_path: str | Path, key_source: Mapping[str, Any]) -> Path:
    """The intent path for a receipt, resolved from the ledger it is appended to."""
    return end_intent_path(Path(ledger_path).parent, key_source.get("project_id"),
                           key_source.get("dispatch_id"), key_source.get("attempt"))


def _refuse_invalid_end_line(detail: str) -> AppendReceiptError:
    return AppendReceiptError("invalid_end_line", EXIT_VALIDATION_ERROR, detail)


def build_end_intent(*, project_id: str, dispatch_id: str, attempt: int, end_state: str,
                     end_line: Mapping[str, Any], ledger_path: str | Path,
                     ledger_offset: int) -> Dict[str, Any]:
    """The intent document: the line validated against its key, canonically, with sha256."""
    if not isinstance(end_line, Mapping):
        raise _refuse_invalid_end_line(f"the end line is not a mapping: {type(end_line).__name__}")
    if str(end_line.get("event_type") or "").strip() != DISPATCH_FINISHED_EVENT_TYPE:
        raise _refuse_invalid_end_line(
            f"the end line's event_type is {end_line.get('event_type')!r}, "
            f"not {DISPATCH_FINISHED_EVENT_TYPE!r}")
    if (not isinstance(project_id, str) or end_line.get("project_id") != project_id
            or not isinstance(dispatch_id, str) or end_line.get("dispatch_id") != dispatch_id):
        raise _refuse_invalid_end_line("the end line does not carry the intent key's project/dispatch")
    supplied_attempt = end_line.get("attempt")
    if (isinstance(supplied_attempt, bool) or supplied_attempt != attempt):
        raise _refuse_invalid_end_line(
            f"the end line's attempt is {supplied_attempt!r}, not {attempt!r}")
    supplied_end_state = str(end_line.get("end_state") or "").strip()
    if supplied_end_state not in LIFECYCLE_END_STATES or supplied_end_state != str(end_state):
        raise _refuse_invalid_end_line(
            f"the end line's end_state is {supplied_end_state!r}, not the intent's {end_state!r}")
    canonical = canonical_end_line(end_line)
    return {
        "version": END_INTENT_VERSION, "project_id": str(project_id),
        "dispatch_id": str(dispatch_id), "attempt": int(attempt),
        "end_state": str(end_state), "line": canonical, "sha256": end_line_sha256(canonical),
        "ledger_path": str(ledger_path), "ledger_offset": int(ledger_offset),
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
    if not isinstance(doc["line"], str):
        raise bad("'line' is not a string")
    try:
        line = json.loads(doc["line"])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise bad(f"'line' is not JSON: {exc}") from exc
    if not isinstance(line, dict) or not line:
        raise bad("'line' is not a non-empty JSON object")
    if doc["sha256"] != end_line_sha256(doc["line"]):
        raise bad("sha256 does not match 'line'")
    if str(line.get("event_type") or "").strip() != DISPATCH_FINISHED_EVENT_TYPE:
        raise bad("'line' is not a dispatch_finished event")
    if (line.get("project_id"), line.get("dispatch_id"), line.get("attempt")) != (
            doc["project_id"], doc["dispatch_id"], doc["attempt"]):
        raise bad("'line' does not carry the intent's key")
    if str(line.get("end_state") or "").strip() != str(doc["end_state"]):
        raise bad("'line' does not carry the intent's end_state")
    return doc


def load_end_intent(intent_path: Path) -> Optional[Dict[str, Any]]:
    """The intent at ``intent_path``, ``None`` when there is none.

    A file that exists but cannot be read or decoded is a named
    ``invalid_end_intent`` refusal, never a bare ``UnicodeDecodeError`` or
    ``OSError`` escaping into a caller that only knows the append vocabulary
    (K-3b follow-up, 30-10).
    """
    try:
        raw = intent_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise AppendReceiptError(
            "invalid_end_intent", EXIT_VALIDATION_ERROR,
            f"the end intent at {intent_path} is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise AppendReceiptError(
            "invalid_end_intent", EXIT_IO_ERROR,
            f"cannot read the end intent at {intent_path}: {exc}") from exc
    return parse_end_intent(raw, source=str(intent_path))


def write_end_intent(intent_path: Path, document: Mapping[str, Any]) -> bool:
    """Freeze an intent exclusively (temp file plus ``os.link``); ``False`` when another won."""
    intent_path = Path(intent_path)
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".intent-", suffix=".tmp", dir=str(intent_path.parent))
    tmp = Path(name)
    try:
        text = json.dumps(dict(document), sort_keys=True,
                          separators=_CANONICAL_SEPARATORS) + "\n"
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, intent_path)
        except FileExistsError:
            return False
        return True
    finally:
        tmp.unlink(missing_ok=True)


# The append layer's own stamps, never a caller decision about this end. They
# are the ONLY fields allowed on the receipt and absent from the intent; any
# other one-sided field is a mismatch (ADR-045 §3), so an added ``no_push``
# after the freeze cannot slip through.
DERIVED_STAMP_FIELDS = frozenset({
    # this module, the lifecycle seam:
    "schema_version",   # stamp_lifecycle: LIFECYCLE_SCHEMA_VERSION
    "writer_sha",       # stamp_lifecycle: writer_sha()
    "status",           # stamp_lifecycle: the end_state projection
    # payload.py, the one append entry point:
    "operator_id", "orchestrator_id", "agent_id",   # _stamp_identity
    "observability_tier",                           # _stamp_observability_tier
    "parent_dispatch", "task_class", "tier_from", "tier_to",  # _CHAIN_LINK_ENV_FIELDS
    "open_items_created",                           # _prepare_receipt setdefault
    "ingested_at",                                  # _stamp_ingested_at
    # idempotency.py, the ADR-038 outcome identity and the chained append:
    "outcome_id",                                   # _write_receipt_under_lock
    "prev_hash",                                    # _write_receipt_under_lock, chain on
    # receipt_finalize.py, the pre-write hook of the same append lock:
    "verdict",                                      # commit_receipt_v2_fields
})

# The warning keys ``warning_destination.commit_destination`` (the pre-write
# hook) rewrites on each ``warnings[]`` entry from lock-time state the freeze
# cannot see; derived like the stamps above, on both the freeze and the compare.
DERIVED_WARNING_FIELDS = frozenset({"destination", "oi_id", "reason"})


def project_end_line(line: Mapping[str, Any]) -> Dict[str, Any]:
    """The canonical form the freeze and the comparison share: every field, each
    warning's derived keys removed. Every other warning key stays compared; a
    non-mapping entry is left as it stands.
    """
    projected = dict(line)
    entries = projected.get("warnings")
    if isinstance(entries, list):
        projected["warnings"] = [
            {k: v for k, v in entry.items() if k not in DERIVED_WARNING_FIELDS}
            if isinstance(entry, Mapping) else entry for entry in entries
        ]
    return projected


def prepare_end_line(line: Mapping[str, Any], *, ledger_path: str | Path) -> Dict[str, Any]:
    """The canonical end line an intent may freeze (ADR-045 §3, K-3b).

    A deep copy runs through the append layer's own ``_prepare_receipt`` — every
    stamp and validation the write applies — so a line the append would refuse
    raises here with the same code, before anything exists on disk (codex
    round 3). Only the derived stamps are removed; ``model`` stays normalised on
    both sides of the later comparison. Writes nothing.
    """
    if not isinstance(line, Mapping):
        raise _refuse_invalid_end_line(
            f"the end line must be a mapping, got {type(line).__name__}")
    if str(line.get("event_type") or "").strip() != DISPATCH_FINISHED_EVENT_TYPE:
        raise _refuse_invalid_end_line(
            "prepare_end_line only freezes a "
            f"{DISPATCH_FINISHED_EVENT_TYPE!r} line, got {line.get('event_type')!r}")
    prepared = copy.deepcopy(dict(line))
    receipt_path = Path(ledger_path).expanduser().resolve()
    # Imported here: payload imports this module at module scope for
    # stamp_lifecycle, so a top-level import would close the cycle.
    from .payload import _prepare_receipt

    prepared, _event_name, _receipt_path = _prepare_receipt(
        prepared, receipt_path, receipts_file=str(receipt_path))
    projected = project_end_line(prepared)
    return {key: value for key, value in projected.items() if key not in DERIVED_STAMP_FIELDS}


def end_line_matches_intent(receipt: Mapping[str, Any], intent: Mapping[str, Any]) -> bool:
    """True when the receipt's canonical line is byte-equal to the intent's.

    Every field is compared: the receipt may carry only the intent's fields plus
    :data:`DERIVED_STAMP_FIELDS`, and a warning only the intent's keys plus
    :data:`DERIVED_WARNING_FIELDS`. Discarding unknown fields would let an added
    ``no_push`` amend the frozen decision (codex round 1 finding 4).
    """
    expected = json.loads(intent["line"])
    if not isinstance(expected, dict) or not expected:
        return False
    if set(expected) - set(receipt):
        return False
    if set(receipt) - set(expected) - DERIVED_STAMP_FIELDS:
        return False
    canonical = canonical_end_line(
        project_end_line({key: receipt.get(key) for key in expected}))
    return canonical == intent["line"] and end_line_sha256(canonical) == intent["sha256"]


def restore_frozen_classification(receipt: Dict[str, Any], intent: Mapping[str, Any]) -> None:
    """Put the intent's frozen warning classification back onto the receipt.

    ``requires_tracking`` is decided once, when the intent is frozen; the append
    classifies warnings again against the recurrence counter, which can cross
    its threshold in between (OI-2452). The frozen decision is what the line is
    written against, so it is restored before the deferred commit and before the
    comparison. A side that is not a ``warnings`` list, or two lists of a
    different length, is left as it stands: the comparison then refuses the line
    instead of guessing which entry the other belongs to.
    """
    frozen = json.loads(intent["line"]).get("warnings")
    entries = receipt.get("warnings")
    if not isinstance(entries, list) or not isinstance(frozen, list) or len(entries) != len(frozen):
        return
    receipt["warnings"] = [
        reapply_frozen_classification(entry, frozen_entry)
        if isinstance(entry, Mapping) and isinstance(frozen_entry, Mapping) else entry
        for entry, frozen_entry in zip(entries, frozen)
    ]


def _event_type(receipt: Mapping[str, Any]) -> str:
    """The type the append layer stamps by: ``event_type``, else ``event``."""
    return str(receipt.get("event_type") or receipt.get("event") or "").strip()


def _refuse_end_line(receipt: Mapping[str, Any], detail: str, code: str) -> AppendReceiptError:
    return AppendReceiptError(
        code, EXIT_VALIDATION_ERROR,
        f"dispatch_finished refused: {detail} (project_id={receipt.get('project_id')!r}, "
        f"dispatch_id={receipt.get('dispatch_id')!r}, attempt={receipt.get('attempt')!r}); "
        "ADR-045 §3 an end line is accepted only against this attempt's intent.")


def decide_end_line(receipt: Dict[str, Any], prior_end: Optional[str], *,
                    ledger_path: Optional[Path] = None) -> str:
    """The append rule for a ``dispatch_finished`` (ADR-045 §3, K-3b).

    Refused by name as ``missing_end_intent``, ``invalid_end_intent``,
    ``duplicate_lifecycle_end`` or ``end_line_mismatch``. ADR-038 corrections do
    not apply here: a matching ``outcome_id`` admits a different ``end_state``.
    """
    intent_path = end_intent_path_for_ledger(ledger_path, receipt) if ledger_path else None
    try:
        intent = load_end_intent(intent_path) if intent_path is not None else None
    except EndIntentMalformed as exc:
        raise _refuse_end_line(
            receipt, f"the intent is malformed: {exc}", "invalid_end_intent") from exc
    if intent is None:
        raise _refuse_end_line(
            receipt, f"no intent exists at {intent_path}", "missing_end_intent")
    receipt_key = (receipt.get("project_id"), receipt.get("dispatch_id"), receipt.get("attempt"))
    intent_key = (intent["project_id"], intent["dispatch_id"], intent["attempt"])
    if intent_key != receipt_key:
        raise _refuse_end_line(
            receipt, f"the intent at {intent_path} is for another attempt", "invalid_end_intent")
    restore_frozen_classification(receipt, intent)
    if prior_end is not None:
        raise AppendReceiptError(
            "duplicate_lifecycle_end", EXIT_VALIDATION_ERROR,
            f"dispatch_finished refused: attempt (project_id={receipt.get('project_id')!r}, "
            f"dispatch_id={receipt.get('dispatch_id')!r}, attempt={receipt.get('attempt')!r}) "
            f"already has an end line (found via {prior_end}). One attempt gets exactly one "
            "end line; a new end is a new attempt, not a second line (ADR-045 §3).")
    if not end_line_matches_intent(receipt, intent):
        raise _refuse_end_line(
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
    if _event_type(receipt) != DISPATCH_FINISHED_EVENT_TYPE:
        return END_LINE_ACCEPTED
    prior = find_prior_end_line(receipt, index_data=index_data, ledger_path=ledger_path)
    return decide_end_line(receipt, prior, ledger_path=ledger_path)


def verify_final_end_line(receipt: Dict[str, Any], *, ledger_path: Path,
                          frozen_event_type: Optional[str] = None) -> str:
    """Re-check the line against its intent after the pre-write hook rewrote the
    derived fields, so what is persisted is what was frozen; a no-op otherwise.

    ``frozen_event_type`` is the type the receipt carried when the append took
    the lock. The hook runs in between, so without it a hook that turned an end
    line into ``task_complete`` (or a ``task_complete`` into an end line) would
    decide whether this guard runs at all (OI-2470). A line whose type moved is
    refused; one that was never an end line stays a no-op.
    """
    current = _event_type(receipt)
    frozen = current if frozen_event_type is None else frozen_event_type
    if current != frozen:
        raise _refuse_end_line(
            receipt, f"the pre-write hook changed event_type from {frozen!r} to "
            f"{current!r}; the line frozen in the intent is not the line being written",
            "end_line_mismatch")
    if frozen != DISPATCH_FINISHED_EVENT_TYPE:
        return END_LINE_ACCEPTED
    intent_path = end_intent_path_for_ledger(ledger_path, receipt)
    try:
        intent = load_end_intent(intent_path)
    except EndIntentMalformed as exc:
        raise _refuse_end_line(receipt, f"the intent is malformed after the hook: {exc}",
                               "invalid_end_intent") from exc
    if intent is None:
        raise _refuse_end_line(receipt, f"no intent exists at {intent_path} after the hook",
                               "missing_end_intent")
    restore_frozen_classification(receipt, intent)
    if not end_line_matches_intent(receipt, intent):
        raise _refuse_end_line(receipt, "the pre-write hook changed a frozen field",
                               "end_line_mismatch")
    return END_LINE_ACCEPTED


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
    "DERIVED_STAMP_FIELDS", "DERIVED_WARNING_FIELDS", "END_INTENT_DIRNAME", "END_INTENT_VERSION",
    "EndIntentMalformed", "build_end_intent", "canonical_end_line", "check_end_line",
    "decide_end_line", "end_intent_path", "end_intent_path_for_ledger",
    "end_line_matches_intent", "end_line_sha256", "find_prior_end_line",
    "lifecycle_enabled", "load_end_intent", "parse_end_intent", "prepare_end_line", "project_end_line",
    "record_lifecycle_end", "restore_frozen_classification", "stamp_lifecycle",
    "verify_final_end_line", "write_end_intent", "writer_sha",
]
