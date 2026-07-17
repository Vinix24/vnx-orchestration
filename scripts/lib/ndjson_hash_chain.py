"""NDJSON hash-chain for VNX audit ledger integrity (Task #17 PR-1).

Each NDJSON entry MAY include `prev_hash` metadata that references the SHA-256
of the prior entry's canonical-JSON body. This creates a tamper-evident chain.

Event-type conventions per session-handoff:
- backfill: historical entry retroactively added (no prev_hash for first backfilled)
- correction: supersedes prior entry (cites prior_entry_hash + reason)
- redaction: removes content from prior entry (preserves chain by tombstoning body)
- tombstone: marks entry deleted/withdrawn (preserves entry-id for chain continuity)

Per ADR-005: NDJSON-first principle preserved. Hash-chain is ADDITIVE metadata,
not a replacement.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


GENESIS_HASH = "0" * 64  # Sentinel for first entry in chain

# ADR-029: a chain epoch begins with a dedicated marker entry whose prev_hash is
# GENESIS. The immutable pre-adoption entries form epoch 0 (legitimately
# unchained); every entry from a marker forward chains within its epoch. This
# lets chaining be adopted on an existing ledger without rewriting history
# (ADR-005 append-only preserved) and without flipping the fleet-wide integrity
# check RED on the first post-flip receipt.
EPOCH_MARKER_TYPE = "chain_epoch_start"


def canonical_json(obj: dict) -> str:
    """Canonical JSON serialization for stable hashing.

    Excludes 'prev_hash' field (it's the chain pointer itself).
    Stable key ordering, no whitespace.
    """
    filtered = {k: v for k, v in obj.items() if k != "prev_hash"}
    return json.dumps(filtered, sort_keys=True, separators=(",", ":"))


def compute_entry_hash(entry: dict) -> str:
    """SHA-256 of canonical-JSON body. Excludes prev_hash."""
    return hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()


def ledger_lock_path(ledger_path: Path) -> Path:
    """The ONE public lock primitive for a given ledger (ADR-034 §4).

    Byte-identical to ``append_receipt_internals.idempotency._lock_file_for``'s
    private path — the hot receipt-append path already locks here today via
    that private helper, so the live lock file for ``state/t0_receipts.ndjson``
    is unchanged and no migration is needed. This is now the single shared
    source of truth: ``append_receipt_payload`` (via ``idempotency``, itself
    unmodified by this ADR), ``append_chained_entry``, ``append_epoch_marker``,
    and ``chain_origin_anchor.seal_and_commit_origin`` all take this SAME lock
    around their critical sections — not independent locks on different paths
    (the original ADR-034 draft's bug, corrected before implementation).
    """
    return ledger_path.parent / "append_receipt.lock"


@contextmanager
def _ledger_locked(ledger_path: Path) -> Iterator[None]:
    """Hold ``ledger_lock_path(ledger_path)``'s flock for the wrapped block.

    Not re-entrant: ``fcntl.flock`` excludes by open-file-description, not by
    process, so a second ``open()`` + ``flock()`` of the same path from the
    same process would block on itself. A caller that already holds this lock
    (``seal_and_commit_origin``) must call the ``_locked`` suffixed internals
    below directly instead of re-entering this context manager (ADR-034 §4).
    """
    lock_path = ledger_lock_path(ledger_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def append_chained_entry(
    path: Path,
    entry: dict,
    *,
    event_type: str | None = None,
) -> str:
    """Append a new entry with prev_hash linking to last entry in file.

    Returns the hash of the newly-appended entry.

    If file is empty/missing: uses GENESIS_HASH as prev_hash.

    Event-type conventions:
    - None: regular entry (default)
    - 'backfill': historical entry — sets prev_hash to GENESIS_HASH
    - 'correction': must include 'corrects_hash' field
    - 'redaction': must include 'redacts_hash' field; body should be opaque
    - 'tombstone': must include 'tombstones_hash' field

    Holds ``ledger_lock_path(path)``'s flock around the read-tail + write
    critical section (ADR-034 §4) — previously unlocked, which let a
    concurrent writer race the tail-hash read.
    """
    with _ledger_locked(path):
        return _append_chained_entry_locked(path, entry, event_type=event_type)


def _append_chained_entry_locked(
    path: Path,
    entry: dict,
    *,
    event_type: str | None = None,
) -> str:
    """Body of ``append_chained_entry``, minus lock acquisition.

    Caller MUST already hold ``ledger_lock_path(path)``'s flock. Exists so
    ``seal_and_commit_origin`` (which holds the lock itself across a larger
    critical section) can append without a second, self-deadlocking
    ``flock()`` call on the same file from the same process.
    """
    if event_type == "backfill":
        prev_hash = GENESIS_HASH
    else:
        prev_hash = _read_last_hash(path) or GENESIS_HASH

    chained = {**entry, "prev_hash": prev_hash}
    if event_type:
        chained["event_type"] = event_type

    if event_type == "correction" and "corrects_hash" not in chained:
        raise ValueError("correction event requires 'corrects_hash' field")
    if event_type == "redaction" and "redacts_hash" not in chained:
        raise ValueError("redaction event requires 'redacts_hash' field")
    if event_type == "tombstone" and "tombstones_hash" not in chained:
        raise ValueError("tombstone event requires 'tombstones_hash' field")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(chained) + "\n")

    return compute_entry_hash(chained)


def _iter_parsed(path: Path) -> Iterator[tuple[int, dict]]:
    """Yield (line_number, entry) for each parseable non-blank NDJSON line."""
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError:
                continue


def epoch_state(path: Path) -> tuple[int, bool]:
    """Inspect a ledger's epoch state for the ADR-029 seal migration.

    Returns ``(max_epoch, chaining_active)``:
    - ``max_epoch``: highest ``epoch`` on any ``chain_epoch_start`` marker (0 if none).
    - ``chaining_active``: True when the LAST non-blank entry carries ``prev_hash``
      (a marker or a chained entry), so a newly-appended receipt would chain into
      the current epoch rather than land in an unchained prefix. When True the
      seal is a no-op — this is what makes ``chain_epoch_seal`` idempotent.
    """
    max_epoch = 0
    last_has_prev = False
    for _, entry in _iter_parsed(path):
        if entry.get("type") == EPOCH_MARKER_TYPE:
            try:
                max_epoch = max(max_epoch, int(entry.get("epoch", 0)))
            except (TypeError, ValueError):
                pass
            last_has_prev = True
        else:
            last_has_prev = "prev_hash" in entry
    return max_epoch, last_has_prev


def append_epoch_marker(path: Path, epoch: int) -> str:
    """Append a ``chain_epoch_start`` marker (``prev_hash == GENESIS``) opening ``epoch``.

    Append-only: never rewrites a historical line (ADR-005 preserved). Returns
    the marker entry's hash so the next appended receipt links to it.

    Holds ``ledger_lock_path(path)``'s flock around the append (ADR-034 §4) —
    previously unlocked.
    """
    with _ledger_locked(path):
        return _append_epoch_marker_locked(path, epoch)


def _append_epoch_marker_locked(path: Path, epoch: int) -> str:
    """Body of ``append_epoch_marker``, minus lock acquisition.

    Caller MUST already hold ``ledger_lock_path(path)``'s flock. Exists so
    ``seal_and_commit_origin`` can open a new epoch inside its own
    already-held critical section without a second, self-deadlocking
    ``flock()`` call (ADR-034 §4).
    """
    marker = {
        "type": EPOCH_MARKER_TYPE,
        "epoch": epoch,
        "epoch_ts": datetime.now(timezone.utc).isoformat(),
        "prev_hash": GENESIS_HASH,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(marker) + "\n")
    return compute_entry_hash(marker)


def _read_last_hash(path: Path) -> str | None:
    """Return hash of last entry in file, or None if file empty/missing."""
    if not path.exists() or path.stat().st_size == 0:
        return None

    last_line = None
    with path.open("rb") as f:
        try:
            f.seek(-2, 2)
            while f.read(1) != b"\n":
                f.seek(-2, 1)
        except OSError:
            # File smaller than 2 bytes — read whole thing
            f.seek(0)
        last_line = f.readline().decode("utf-8").strip()

    if not last_line:
        return None

    try:
        entry = json.loads(last_line)
        return compute_entry_hash(entry)
    except json.JSONDecodeError:
        return None


def verify_chain(path: Path) -> tuple[bool, list[dict], str]:
    """Verify the hash-chain integrity of an NDJSON ledger (ADR-029 epoch-aware).

    Returns (is_valid, violations_list, status) where status is one of:

    - "unchained": no entry carries prev_hash and there is no chain_epoch_start
      marker — chaining was never enabled (VNX_CHAIN_RECEIPTS default off).
      Integrity cannot be verified; NOT an error. is_valid=True.
    - "verified": the ledger chains from a single GENESIS with no unchained
      prefix and no explicit epoch marker. is_valid=True.
    - "verified-segmented": a legitimate ADR-029 layout — an immutable unchained
      epoch-0 prefix and/or one or more explicit chain_epoch_start epochs, each
      chained intact. is_valid=True.
    - "broken": a within-epoch prev_hash mismatch, an unchained entry INSIDE a
      chained epoch, a chained entry with no preceding epoch marker after an
      unchained prefix (a naive VNX_CHAIN_RECEIPTS flip that skipped the seal),
      an epoch marker whose prev_hash != GENESIS, or unparseable lines.
      is_valid=False.

    Threat-model note (ADR-029, accepted residual): epoch-0 history and the
    relocation of an epoch boundary are NOT defended here. A local actor with
    write access to the ledger can strip a prefix, insert a fresh
    chain_epoch_start marker, and re-chain the remainder — this verifies. A
    ledger is only as trustworthy as its first sealed epoch onward; full
    tamper-evidence against a local attacker needs an EXTERNAL append-only
    anchor (out of scope for ADR-029 — a future epoch-seal-anchor ADR). This
    check detects accidental corruption and within-epoch tampering, the
    governance value ADR-023/ADR-029 target. Flag stays default-off until the
    fleet-wide seal migration lands.

    violations_list contains dicts with: line_number and one of
    expected_prev_hash/actual_prev_hash, 'error', or 'note'.
    """
    entries: list[tuple[int, dict]] = []
    parse_errors: list[dict] = []

    if not path.exists() or path.stat().st_size == 0:
        return (True, [], "unchained")

    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append((line_no, json.loads(line)))
            except json.JSONDecodeError as e:
                parse_errors.append({"line_number": line_no, "error": f"invalid JSON: {e}"})

    if parse_errors and not entries:
        return (False, parse_errors, "broken")
    if not entries:
        return (True, [], "unchained")

    violations: list[dict] = list(parse_errors)
    in_chain = False    # currently inside a chained epoch
    saw_chain = False   # any chained content at all (marker or chained entry)
    saw_prefix = False  # an unchained epoch-0 prefix entry
    saw_marker = False  # an explicit chain_epoch_start marker
    expected_prev = GENESIS_HASH

    for line_no, entry in entries:
        is_marker = entry.get("type") == EPOCH_MARKER_TYPE
        has_prev = "prev_hash" in entry
        actual_prev = entry.get("prev_hash")

        if is_marker:
            saw_chain = True
            saw_marker = True
            if actual_prev != GENESIS_HASH:
                violations.append({
                    "line_number": line_no,
                    "expected_prev_hash": GENESIS_HASH,
                    "actual_prev_hash": actual_prev,
                    "note": "chain_epoch_start marker must anchor to GENESIS",
                })
            in_chain = True
            expected_prev = compute_entry_hash(entry)
            continue

        if not in_chain:
            # Leading region: the epoch-0 unchained prefix, or the first chained
            # entry of a marker-less GENESIS chain.
            if has_prev:
                saw_chain = True
                if actual_prev == GENESIS_HASH and not saw_prefix:
                    # Chained from the first line — a marker-less "verified" chain.
                    in_chain = True
                    expected_prev = compute_entry_hash(entry)
                else:
                    # Chained entry after an unchained prefix, or anchored to a
                    # non-GENESIS hash, with no chain_epoch_start marker: the
                    # naive-flip / unsealed case.
                    violations.append({
                        "line_number": line_no,
                        "note": "chained entry without a preceding chain_epoch_start marker (unsealed / naive flip)",
                    })
                    in_chain = True
                    expected_prev = compute_entry_hash(entry)
            else:
                saw_prefix = True
        else:
            # Inside a chained epoch.
            if not has_prev:
                violations.append({
                    "line_number": line_no,
                    "note": "unchained entry inside a chained epoch",
                })
            elif actual_prev != expected_prev:
                violations.append({
                    "line_number": line_no,
                    "expected_prev_hash": expected_prev,
                    "actual_prev_hash": actual_prev,
                })
            expected_prev = compute_entry_hash(entry)

    if violations:
        return (False, violations, "broken")
    if not saw_chain:
        return (True, [], "unchained")
    if saw_prefix or saw_marker:
        return (True, [], "verified-segmented")
    return (True, [], "verified")


def walk_chain(path: Path) -> Iterator[tuple[int, dict, str]]:
    """Yield (line_number, entry, computed_hash) for each chain entry."""
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                yield (line_no, entry, compute_entry_hash(entry))
            except json.JSONDecodeError:
                continue
