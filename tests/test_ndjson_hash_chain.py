"""Tests for NDJSON hash-chain core (Task #17 PR-1).

Covers:
  - Deterministic hashing and canonical JSON serialization
  - Append with genesis / chain linking
  - Chain verification: valid, tampered, inserted
  - Event-type validation (correction, redaction, tombstone, backfill)
  - Walk chain generator
  - Empty file trivially valid
  - Backward-compat: plain NDJSON append without hash-chain
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from ndjson_hash_chain import (
    GENESIS_HASH,
    append_chained_entry,
    canonical_json,
    compute_entry_hash,
    verify_chain,
    walk_chain,
)


# ---------------------------------------------------------------------------
# Hashing primitives
# ---------------------------------------------------------------------------


def test_compute_entry_hash_deterministic():
    entry = {"ts": "2026-01-01", "event": "dispatch", "id": "d001"}
    h1 = compute_entry_hash(entry)
    h2 = compute_entry_hash(entry)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_canonical_json_excludes_prev_hash():
    """Hash must not change based on the chain position (prev_hash excluded)."""
    entry_plain = {"ts": "2026-01-01", "event": "dispatch"}
    entry_with_prev = {**entry_plain, "prev_hash": "abc123"}
    assert canonical_json(entry_plain) == canonical_json(entry_with_prev)
    assert compute_entry_hash(entry_plain) == compute_entry_hash(entry_with_prev)


# ---------------------------------------------------------------------------
# Append + chain linking
# ---------------------------------------------------------------------------


def test_append_chained_entry_first_uses_genesis(tmp_path):
    p = tmp_path / "chain.ndjson"
    append_chained_entry(p, {"event": "first", "id": "e1"})
    with p.open() as f:
        entry = json.loads(f.readline())
    assert entry["prev_hash"] == GENESIS_HASH


def test_append_chained_entry_links_to_previous(tmp_path):
    p = tmp_path / "chain.ndjson"
    h1 = append_chained_entry(p, {"event": "first", "id": "e1"})
    append_chained_entry(p, {"event": "second", "id": "e2"})

    lines = p.read_text().splitlines()
    entry2 = json.loads(lines[1])
    assert entry2["prev_hash"] == h1


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------


def test_verify_chain_valid_passes(tmp_path):
    p = tmp_path / "chain.ndjson"
    for i in range(5):
        append_chained_entry(p, {"seq": i, "ts": "2026-01-01"})
    ok, violations = verify_chain(p)
    assert ok is True
    assert violations == []


def test_verify_chain_tampered_entry_fails(tmp_path):
    p = tmp_path / "chain.ndjson"
    append_chained_entry(p, {"event": "e1", "id": "1"})
    append_chained_entry(p, {"event": "e2", "id": "2"})
    append_chained_entry(p, {"event": "e3", "id": "3"})

    lines = p.read_text().splitlines()
    # Tamper line 2 (index 1): change event field
    tampered = json.loads(lines[1])
    tampered["id"] = "TAMPERED"
    lines[1] = json.dumps(tampered)
    p.write_text("\n".join(lines) + "\n")

    ok, violations = verify_chain(p)
    assert ok is False
    # Line 3's prev_hash now points to old line 2 hash — mismatch
    assert any(v.get("line_number") == 3 for v in violations)


def test_verify_chain_inserted_entry_fails(tmp_path):
    p = tmp_path / "chain.ndjson"
    append_chained_entry(p, {"event": "e1", "id": "1"})
    append_chained_entry(p, {"event": "e2", "id": "2"})

    lines = p.read_text().splitlines()
    # Insert a fake entry between line 1 and line 2
    fake = {"event": "injected", "id": "fake", "prev_hash": GENESIS_HASH}
    lines.insert(1, json.dumps(fake))
    p.write_text("\n".join(lines) + "\n")

    ok, violations = verify_chain(p)
    assert ok is False
    assert len(violations) >= 1


# ---------------------------------------------------------------------------
# Event-type validation
# ---------------------------------------------------------------------------


def test_correction_event_requires_corrects_hash(tmp_path):
    p = tmp_path / "chain.ndjson"
    with pytest.raises(ValueError, match="corrects_hash"):
        append_chained_entry(p, {"event": "fix"}, event_type="correction")


def test_redaction_event_requires_redacts_hash(tmp_path):
    p = tmp_path / "chain.ndjson"
    with pytest.raises(ValueError, match="redacts_hash"):
        append_chained_entry(p, {"event": "redact"}, event_type="redaction")


def test_tombstone_event_requires_tombstones_hash(tmp_path):
    p = tmp_path / "chain.ndjson"
    with pytest.raises(ValueError, match="tombstones_hash"):
        append_chained_entry(p, {"event": "delete"}, event_type="tombstone")


def test_backfill_event_uses_genesis_prev_hash(tmp_path):
    p = tmp_path / "chain.ndjson"
    # Seed the file with a normal entry so last hash would differ from GENESIS
    append_chained_entry(p, {"event": "existing", "id": "e0"})

    # Backfill always links to GENESIS, regardless of existing last entry
    append_chained_entry(p, {"event": "historical", "id": "h1"}, event_type="backfill")

    lines = p.read_text().splitlines()
    backfilled = json.loads(lines[1])
    assert backfilled["prev_hash"] == GENESIS_HASH
    assert backfilled["event_type"] == "backfill"


# ---------------------------------------------------------------------------
# Walk chain
# ---------------------------------------------------------------------------


def test_walk_chain_yields_all_entries(tmp_path):
    p = tmp_path / "chain.ndjson"
    for i in range(4):
        append_chained_entry(p, {"seq": i})

    results = list(walk_chain(p))
    assert len(results) == 4
    for line_no, entry, hash_ in results:
        assert isinstance(line_no, int)
        assert isinstance(entry, dict)
        assert len(hash_) == 64


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_verify_empty_file_returns_valid(tmp_path):
    p = tmp_path / "empty.ndjson"
    p.touch()
    ok, violations = verify_chain(p)
    assert ok is True
    assert violations == []


def test_chain_survives_normal_append(tmp_path):
    """Existing emitters writing plain NDJSON continue to work (backward-compat)."""
    p = tmp_path / "legacy.ndjson"
    # Plain NDJSON appends without hash-chain (old-style)
    for i in range(3):
        with p.open("a") as f:
            f.write(json.dumps({"seq": i, "ts": "2026-01-01"}) + "\n")

    # All entries readable via walk_chain
    entries = list(walk_chain(p))
    assert len(entries) == 3
    # Plain entries have no prev_hash — backward-compat preserved
    _, entry0, _ = entries[0]
    assert "prev_hash" not in entry0
