"""chain_origin_anchor.py — sibling append-only store pinning a ledger's chain origin.

Closes the ``verify_chain`` prefix-strip bypass documented in ADR-029's
threat-model note (see ADR-033): a local actor with ledger write access can
strip an early prefix, insert a fresh ``chain_epoch_start`` marker
(``prev_hash=GENESIS``), and re-chain the remainder — this verifies as
``verified-segmented`` because ``verify_chain`` has no memory of where the
chain was supposed to begin. This module gives it that memory.

Design note (chosen after PR #1086's DB-backed origin store — recorded in
``runtime_coordination.db`` — was HELD through four codex_gate rounds: mutable
origin via ``INSERT OR REPLACE``, then a non-atomic ledger-write/origin-write
pair, then fail-open on store errors): pin the origin in a SIBLING APPEND-ONLY
NDJSON file next to the ledger, written only by the one-time
``chain_epoch_seal`` migration — never on the hot receipt-append path.
- **Write-once** is a plain "does a record already exist" check made under an
  exclusive flock; there is no UPDATE/REPLACE surface to make mutable.
- There is no per-append atomicity problem to solve, because the anchor write
  happens out-of-band from receipt appends rather than paired with every one.
  A seal that crashes between the marker append and the anchor write is
  self-healing: ``chain_epoch_seal.seal_ledger`` is idempotent, so re-running
  it completes the anchor write without re-marking the ledger.
- The anchor file is itself an ADR-005-shaped append-only NDJSON record, so it
  carries its own audit trail (``sealed_at``) without a separate event.
- Reads fail CLOSED: a corrupt/unparseable anchor file is reported as such,
  never silently treated as "no anchor" (which would fail open).

Explicitly out of scope (see ADR-033): a root attacker who edits BOTH the
ledger and its sibling anchor file defeats this scheme. Full defense against
that needs an external/remote append-only anchor — deferred, same accepted
residual as ADR-029.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ANCHOR_SUFFIX = ".origin_anchor.ndjson"

# Sentinel returned by read_origin_anchor when the anchor file exists but no
# line for this ledger parses cleanly — fail CLOSED, never "no anchor".
CORRUPT = {"_corrupt": True}


def anchor_path(ledger_path: Path) -> Path:
    """Sibling append-only anchor file for ``ledger_path``."""
    return ledger_path.with_name(ledger_path.name + ANCHOR_SUFFIX)


def _ledger_identity(ledger_path: Path) -> str:
    return str(ledger_path.resolve())


def read_origin_anchor(ledger_path: Path) -> dict | None:
    """Return the pinned origin record for ``ledger_path``, or ``None`` if
    the ledger has never been sealed (no anchor file, or an anchor file with
    no record for this ledger and nothing unparseable in it either).

    Fails CLOSED on corruption: if the anchor file exists and contains at
    least one unparseable line but no valid record for this ledger, returns
    ``{"_corrupt": True}`` rather than silently treating it as "no anchor".
    """
    anchor_file = anchor_path(ledger_path)
    if not anchor_file.exists() or anchor_file.stat().st_size == 0:
        return None

    identity = _ledger_identity(ledger_path)
    record: dict | None = None
    saw_unparseable = False
    with anchor_file.open("r", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    saw_unparseable = True
                    continue
                if obj.get("ledger_identity") == identity:
                    record = obj
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    if record is None and saw_unparseable:
        return dict(CORRUPT)
    return record


def write_origin_anchor_if_absent(ledger_path: Path, origin: dict) -> dict:
    """Idempotently pin ``origin`` for ``ledger_path``.

    Write-once: the read-check-then-append critical section runs under an
    exclusive flock, so a second (or concurrent) call for the same ledger
    returns the EXISTING record untouched — the anchor, once written, is
    immutable, never overwritten with ``origin``.

    ``origin`` must contain ``origin_type``, ``origin_hash``,
    ``origin_line_number``, and ``origin_epoch``.
    """
    anchor_file = anchor_path(ledger_path)
    anchor_file.parent.mkdir(parents=True, exist_ok=True)
    identity = _ledger_identity(ledger_path)

    with open(anchor_file, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("ledger_identity") == identity:
                    return obj

            record = {
                "ledger_identity": identity,
                "origin_type": origin["origin_type"],
                "origin_hash": origin["origin_hash"],
                "origin_line_number": origin["origin_line_number"],
                "origin_epoch": origin["origin_epoch"],
                "entries_before_origin": origin["origin_line_number"] - 1,
                "sealed_at": datetime.now(timezone.utc).isoformat(),
            }
            f.write(json.dumps(record, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
            return record
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
