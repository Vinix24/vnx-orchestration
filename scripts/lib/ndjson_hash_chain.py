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

Security note (origin pinning, PR #1085 fix):
- The first chained entry in a ledger is the chain origin. Its file line number
  and canonical hash are recorded in the project's trusted
  `runtime_coordination.db` when chaining is first adopted.
- `verify_chain` reads that record and rejects any ledger whose observed chain
  origin is later than (or different from) the recorded origin. This closes the
  prefix-strip / prefix-deletion + re-anchor bypass where an attacker removes
  entries from the start of the ledger and rewinds the chain to GENESIS_HASH.
- When no origin record exists the verifier falls back to strict mode: a chained
  ledger must start at line 1 with a GENESIS-anchored entry and every entry must
  carry `prev_hash`.
- The origin record is WRITE-ONCE (`ON CONFLICT DO NOTHING`): once pinned it can
  never be overwritten, so an attacker cannot re-record a forged origin after
  stripping the prefix (PR #1086 fix).

Threat model + residual limit (honest scope):
- This detects ACCIDENTAL corruption and NAIVE tampering (prefix strip/delete +
  re-anchor, forged-origin overwrite) against a ledger whose trusted origin row
  is intact.
- It does NOT fully defend a local attacker who both DELETES the origin row from
  `runtime_coordination.db` AND re-chains the entire remaining ledger from a new
  GENESIS: strict-mode then accepts the re-chained file. True tamper-EVIDENCE
  against a local attacker with write access to both the ledger and the origin
  store requires an EXTERNAL append-only / signed anchor (periodic head-hash
  seal), tracked as ADR-029 (`chain_epoch_seal`), out of scope for this fix.
  The flag stays default-off (`VNX_RECEIPT_HASH_CHAIN`) until that anchor lands.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


GENESIS_HASH = "0" * 64  # Sentinel for first entry in chain
_ORIGIN_TABLE = "vnx_chain_origin"


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


def _origin_store_path(ledger_path: Path) -> Path:
    """Trusted SQLite store for the chain origin of this ledger."""
    return ledger_path.parent / "runtime_coordination.db"


def _init_origin_store(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_ORIGIN_TABLE} (
            ledger_path TEXT PRIMARY KEY,
            line_number INTEGER NOT NULL,
            entry_hash TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _record_chain_origin(
    origin_db: Path,
    ledger_path: Path,
    line_number: int,
    entry_hash: str,
) -> None:
    """Persist the chain origin for a ledger in the trusted SQLite store."""
    origin_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(origin_db)) as conn:
        _init_origin_store(conn)
        # WRITE-ONCE: the chain origin is immutable history — the first chained
        # entry of a ledger. INSERT OR REPLACE would let a later append overwrite
        # the pinned origin, which defeats the pin (an attacker could strip the
        # prefix, append a new "first" chained entry, and re-record the origin).
        # ON CONFLICT DO NOTHING keeps the first-recorded origin forever; a
        # re-record attempt is silently ignored so verify_chain still checks the
        # ledger against the ORIGINAL origin. A genuine new epoch uses a new
        # ledger file (new path = new row), so this never blocks legitimate use.
        conn.execute(
            f"""
            INSERT INTO {_ORIGIN_TABLE}
                (ledger_path, line_number, entry_hash, recorded_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ledger_path) DO NOTHING
            """,
            (
                str(ledger_path.resolve()),
                line_number,
                entry_hash,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def _read_chain_origin(origin_db: Path, ledger_path: Path) -> dict | None:
    """Return the recorded origin for a ledger, or None if not recorded."""
    if not origin_db.exists():
        return None
    with sqlite3.connect(str(origin_db)) as conn:
        try:
            cur = conn.execute(
                f"""
                SELECT line_number, entry_hash FROM {_ORIGIN_TABLE}
                WHERE ledger_path = ?
                """,
                (str(ledger_path.resolve()),),
            )
            row = cur.fetchone()
        except sqlite3.OperationalError:
            # The origin table has never been created in this coordination DB
            # (e.g. a pre-existing runtime_coordination.db that predates chaining,
            # or no ledger has recorded an origin yet). Treat as "no origin
            # recorded" -> verify_chain uses strict mode; never crash the verify.
            return None
        if row is None:
            return None
        return {"line_number": row[0], "entry_hash": row[1]}


def _has_chained_entries(path: Path) -> bool:
    """True if the ledger already contains at least one entry with prev_hash."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                if "prev_hash" in json.loads(line):
                    return True
            except json.JSONDecodeError:
                continue
    return False


def append_chained_entry(
    path: Path,
    entry: dict,
    *,
    event_type: str | None = None,
) -> str:
    """Append a new entry with prev_hash linking to last entry in file.

    Returns the hash of the newly-appended entry.

    If the ledger has no chained entries yet (empty, missing, or only plain
    NDJSON lines), the new entry becomes the chain origin: its prev_hash is
    GENESIS_HASH and its line number + canonical hash are recorded in the
    project's trusted origin store (`runtime_coordination.db`).

    Event-type conventions:
    - None: regular entry (default)
    - 'backfill': historical entry — sets prev_hash to GENESIS_HASH
    - 'correction': must include 'corrects_hash' field
    - 'redaction': must include 'redacts_hash' field; body should be opaque
    - 'tombstone': must include 'tombstones_hash' field
    """
    already_chained = _has_chained_entries(path)

    if event_type == "backfill":
        prev_hash = GENESIS_HASH
    elif already_chained:
        prev_hash = _read_last_hash(path)
    else:
        # First chained entry in this ledger — starts a new epoch at GENESIS.
        prev_hash = GENESIS_HASH

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

    # Determine the line number this entry will occupy after append.
    next_line = 1
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            next_line = sum(1 for _ in f) + 1

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(chained) + "\n")

    new_hash = compute_entry_hash(chained)

    if not already_chained:
        _record_chain_origin(_origin_store_path(path), path, next_line, new_hash)

    return new_hash


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
    """Verify the hash-chain integrity for an NDJSON file.

    Returns (is_valid, violations_list, status) where status is one of:

    - "unchained": no entry in the file carries a prev_hash field; chaining
      was never enabled (VNX_CHAIN_RECEIPTS is off by default).  Integrity
      cannot be verified — this is NOT an error.  is_valid is True.
    - "verified": the chain from the recorded origin onward is intact.
      is_valid is True.  A ledger with an unchained prefix before the recorded
      origin is also reported "verified" (segmented ledger / mid-ledger
      adoption).
    - "broken": chain is present but has integrity violations (tampered,
      inserted, partially chained, or the observed origin differs from the
      recorded origin).  is_valid is False.

    When a chain origin has been recorded in the trusted origin store
    (`runtime_coordination.db`) the verifier tolerates an unchained prefix
    before that origin, but the entry at the recorded origin line MUST match
    the recorded hash and anchor to GENESIS_HASH.  Every entry after the
    origin MUST carry prev_hash == expected_prev_hash.

    When no origin record exists the verifier uses strict mode: a chained
    ledger must start at line 1 with a GENESIS-anchored entry and every entry
    must carry prev_hash.

    violations_list contains dicts with: line_number, expected_prev_hash,
    actual_prev_hash (and optionally 'error' or 'note').
    """
    entries: list[tuple[int, dict]] = []

    if not path.exists() or path.stat().st_size == 0:
        return (True, [], "unchained")

    parse_errors: list[dict] = []

    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                parse_errors.append({
                    "line_number": line_no,
                    "error": f"invalid JSON: {e}",
                })
                continue
            entries.append((line_no, entry))

    # Unparseable lines are always a violation regardless of chain status.
    if parse_errors and not entries:
        return (False, parse_errors, "broken")

    if not entries:
        return (True, [], "unchained")

    # Determine whether any entry carries prev_hash.
    chained_count = sum(1 for _, e in entries if "prev_hash" in e)
    total = len(entries)

    if chained_count == 0 and not parse_errors:
        # No entry has prev_hash — ledger is fully unchained.  Chaining was
        # not enabled (VNX_CHAIN_RECEIPTS default off).  Not an error.
        return (True, [], "unchained")

    # At least one entry has prev_hash (or there are parse errors mixed in).
    violations: list[dict] = list(parse_errors)
    origin = _read_chain_origin(_origin_store_path(path), path)

    if origin is not None:
        # Origin-aware mode: tolerate an unchained prefix before the recorded
        # origin, but pin the origin itself and verify the suffix.
        _verify_with_origin(path, entries, origin, violations)
    else:
        # Strict mode: no unchained prefix allowed for a chained ledger.
        _verify_strict(entries, chained_count, total, violations)

    if violations:
        return (False, violations, "broken")

    return (True, [], "verified")


def _verify_with_origin(
    path: Path,
    entries: list[tuple[int, dict]],
    origin: dict,
    violations: list[dict],
) -> None:
    """Verify entries from the recorded origin onward.

    Mutates ``violations`` in place. Entries before the recorded origin are
    tolerated as a legitimate unchained prefix.
    """
    origin_line = origin["line_number"]
    origin_hash = origin["entry_hash"]

    origin_entry = None
    for line_no, entry in entries:
        if line_no == origin_line:
            origin_entry = entry
            break

    if origin_entry is None:
        violations.append({
            "line_number": origin_line,
            "note": "recorded chain origin line not found in ledger (prefix stripped?)",
        })
        return

    if origin_entry.get("prev_hash") != GENESIS_HASH:
        violations.append({
            "line_number": origin_line,
            "expected_prev_hash": GENESIS_HASH,
            "actual_prev_hash": origin_entry.get("prev_hash"),
            "note": "recorded chain origin must anchor to GENESIS_HASH",
        })

    actual_hash = compute_entry_hash(origin_entry)
    if actual_hash != origin_hash:
        violations.append({
            "line_number": origin_line,
            "expected_entry_hash": origin_hash,
            "actual_entry_hash": actual_hash,
            "note": "recorded chain origin hash mismatch (prefix re-anchored?)",
        })

    expected_prev = GENESIS_HASH
    found_origin = False
    for line_no, entry in entries:
        if line_no < origin_line:
            continue
        if line_no == origin_line:
            found_origin = True
            expected_prev = compute_entry_hash(entry)
            continue
        if not found_origin:
            continue
        actual_prev = entry.get("prev_hash")
        if actual_prev != expected_prev:
            violations.append({
                "line_number": line_no,
                "expected_prev_hash": expected_prev,
                "actual_prev_hash": actual_prev,
            })
        expected_prev = compute_entry_hash(entry)


def _verify_strict(
    entries: list[tuple[int, dict]],
    chained_count: int,
    total: int,
    violations: list[dict],
) -> None:
    """Strict verification for ledgers with no recorded origin.

    A chained ledger must start at line 1 with a GENESIS-anchored entry and
    every entry must carry prev_hash.
    """
    if chained_count > 0:
        first_line_no, first_entry = entries[0]
        if first_entry.get("prev_hash") != GENESIS_HASH:
            violations.append({
                "line_number": first_line_no,
                "expected_prev_hash": GENESIS_HASH,
                "actual_prev_hash": first_entry.get("prev_hash"),
                "note": "chained ledger must start with GENESIS-anchored entry (origin not recorded)",
            })

    expected_prev = GENESIS_HASH
    for line_no, entry in entries:
        actual_prev = entry.get("prev_hash")

        if line_no == entries[0][0]:
            # First entry in the file.
            if actual_prev is not None and actual_prev != GENESIS_HASH:
                violations.append({
                    "line_number": line_no,
                    "expected_prev_hash": GENESIS_HASH,
                    "actual_prev_hash": actual_prev,
                    "note": "first entry: prev_hash must be GENESIS or absent",
                })
        else:
            if actual_prev != expected_prev:
                violations.append({
                    "line_number": line_no,
                    "expected_prev_hash": expected_prev,
                    "actual_prev_hash": actual_prev,
                })

        expected_prev = compute_entry_hash(entry)

    if chained_count < total:
        # Partial chain detected even with no hash mismatches.  This guard is
        # LOAD-BEARING for the edge case where the FIRST entry carries no
        # prev_hash (allowed by the first-entry branch) while later entries
        # hash-link correctly to their predecessor — the loop produces zero
        # violations there; only this count check catches the partial chain.
        violations.append({
            "note": (
                f"partial chain: {chained_count}/{total} entries carry prev_hash; "
                "ledger must be fully unchained or fully chained"
            )
        })


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
