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

Full-history verification across rotation boundaries
-----------------------------------------------------
After rotation, the live file starts with a ``ledger_rotation`` sentinel whose
``prev_hash`` equals the hash of the last archived entry.  To verify the
complete chain (all archives + live), use ``verify_history``::

    from pathlib import Path
    archives = sorted((archive_dir).glob("*.ndjson"))  # ascending = chronological
    verify_history(archives + [live_file])

Internally, ``verify_history`` calls ``verify_chain(segment, expected_prev=...)``
for each segment, passing the tail hash of the previous segment as the starting
point.  ``verify_chain`` accepts ``expected_prev=GENESIS_HASH`` (default) for
the first segment and any non-genesis hash for continuation segments.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterator


GENESIS_HASH = "0" * 64  # Sentinel for first entry in chain


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


def verify_chain(path: Path, *, expected_prev: str = GENESIS_HASH) -> tuple[bool, list[dict]]:
    """Verify the hash-chain integrity for an NDJSON file segment.

    Returns (is_valid, violations_list).
    violations_list contains dicts with: line_number, expected_prev_hash, actual_prev_hash

    expected_prev: starting prev_hash value for this segment.
    - GENESIS_HASH (default): use for the first/only segment (or standalone file).
      When expected_prev is GENESIS_HASH, the first entry may omit prev_hash
      entirely (legacy backward-compat) or carry GENESIS_HASH explicitly.
    - hash of last archived entry: use for the live segment after rotation.
      The live segment's first entry (the ledger_rotation sentinel) must carry
      prev_hash == expected_prev explicitly; None is not accepted.

    For full cross-rotation verification use verify_history() instead.
    """
    violations: list[dict] = []
    _expected = expected_prev

    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                violations.append({
                    "line_number": line_no,
                    "error": f"invalid JSON: {e}",
                })
                continue

            actual_prev = entry.get("prev_hash")

            if line_no == 1 and _expected == GENESIS_HASH:
                # Genesis segment: first entry may omit prev_hash (legacy) or use GENESIS_HASH.
                if actual_prev is not None and actual_prev != GENESIS_HASH:
                    violations.append({
                        "line_number": line_no,
                        "expected_prev_hash": _expected,
                        "actual_prev_hash": actual_prev,
                        "note": "first genesis entry: prev_hash must be GENESIS or absent",
                    })
            elif actual_prev != _expected:
                violations.append({
                    "line_number": line_no,
                    "expected_prev_hash": _expected,
                    "actual_prev_hash": actual_prev,
                })

            _expected = compute_entry_hash(entry)

    return (len(violations) == 0, violations)


def verify_history(segments: list[Path]) -> tuple[bool, list[dict]]:
    """Verify hash-chain continuity across multiple NDJSON file segments.

    Walk each segment in order (first segment starts from GENESIS_HASH),
    carrying the tail hash of each segment as the expected_prev for the next.

    Typical usage after rotation::

        archives = sorted(archive_dir.glob("*.ndjson"))  # ascending = chronological
        ok, violations = verify_history(archives + [live_file])

    Each violation dict includes a 'segment' key with the file path.
    Returns (is_valid, all_violations).
    """
    all_violations: list[dict] = []
    expected_prev = GENESIS_HASH

    for seg_path in segments:
        _, violations = verify_chain(seg_path, expected_prev=expected_prev)
        for v in violations:
            v["segment"] = str(seg_path)
        all_violations.extend(violations)
        tail = _read_last_hash(seg_path)
        if tail is not None:
            expected_prev = tail

    return (len(all_violations) == 0, all_violations)


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
