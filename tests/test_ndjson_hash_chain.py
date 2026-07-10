"""Tests for NDJSON hash-chain core (Task #17 PR-1).

Covers:
  - Deterministic hashing and canonical JSON serialization
  - Append with genesis / chain linking
  - Chain verification: valid, tampered, inserted
  - Event-type validation (correction, redaction, tombstone, backfill)
  - Walk chain generator
  - Empty file trivially valid
  - Backward-compat: plain NDJSON append without hash-chain
  - LB-5: unchained vs broken distinction (verify_chain three-status contract)
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
    ok, violations, status = verify_chain(p)
    assert ok is True
    assert violations == []
    assert status == "verified"


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

    ok, violations, status = verify_chain(p)
    assert ok is False
    assert status == "broken"
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

    ok, violations, status = verify_chain(p)
    assert ok is False
    assert status == "broken"
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
    ok, violations, status = verify_chain(p)
    assert ok is True
    assert violations == []
    assert status == "unchained"


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


# ---------------------------------------------------------------------------
# LB-5: unchained vs broken distinction
# ---------------------------------------------------------------------------


def test_verify_chain_unchained_no_prev_hash_is_ok(tmp_path):
    """Plain NDJSON without any prev_hash → status 'unchained', exit 0 equivalent.

    This is the default state when VNX_CHAIN_RECEIPTS is off.  Must NOT be
    reported as broken or verified — it is simply not chained.
    """
    p = tmp_path / "plain.ndjson"
    for i in range(4):
        with p.open("a") as f:
            f.write(json.dumps({"seq": i, "event": "dispatch", "id": f"d{i}"}) + "\n")

    ok, violations, status = verify_chain(p)
    assert ok is True
    assert violations == []
    assert status == "unchained"


def test_verify_chain_chained_intact_is_verified(tmp_path):
    """Fully chained ledger with correct hashes → status 'verified'."""
    p = tmp_path / "chained.ndjson"
    for i in range(5):
        append_chained_entry(p, {"seq": i, "event": "dispatch"})

    ok, violations, status = verify_chain(p)
    assert ok is True
    assert violations == []
    assert status == "verified"


def test_verify_chain_tampered_chained_is_broken(tmp_path):
    """Tampered entry in a chained ledger → status 'broken', exit 1 equivalent."""
    p = tmp_path / "tampered.ndjson"
    append_chained_entry(p, {"event": "e1", "id": "1"})
    append_chained_entry(p, {"event": "e2", "id": "2"})
    append_chained_entry(p, {"event": "e3", "id": "3"})

    lines = p.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["id"] = "TAMPERED"
    lines[1] = json.dumps(tampered)
    p.write_text("\n".join(lines) + "\n")

    ok, violations, status = verify_chain(p)
    assert ok is False
    assert status == "broken"
    assert len(violations) >= 1


def test_verify_chain_partial_chain_is_broken(tmp_path):
    """Partial chain — some entries have prev_hash, some do not — is 'broken'.

    A partially-chained ledger is NOT 'unchained'.  The presence of any
    prev_hash field means chaining was attempted; missing ones are violations.
    """
    p = tmp_path / "partial.ndjson"
    # First two entries: plain NDJSON, no prev_hash
    with p.open("a") as f:
        f.write(json.dumps({"seq": 0, "id": "plain-0"}) + "\n")
        f.write(json.dumps({"seq": 1, "id": "plain-1"}) + "\n")
    # Third entry: manually inject a prev_hash to simulate partial chaining
    with p.open("a") as f:
        f.write(json.dumps({"seq": 2, "id": "chained-2", "prev_hash": GENESIS_HASH}) + "\n")

    ok, violations, status = verify_chain(p)
    assert ok is False
    assert status == "broken"
    assert len(violations) >= 1


def test_verify_chain_first_unchained_rest_linked_is_broken(tmp_path):
    """LOAD-BEARING guard case (kimi-gate PR #840 F1): first entry has no
    prev_hash (allowed by the first-entry branch), later entries hash-link
    correctly to their predecessor. The verify loop produces zero violations
    here — only the chained_count < total guard catches the partial chain."""
    p = tmp_path / "first-unchained.ndjson"
    first = {"seq": 0, "id": "plain-0"}
    with p.open("a") as f:
        f.write(json.dumps(first, sort_keys=True, separators=(",", ":")) + "\n")
    # Second entry links CORRECTLY to the first entry's computed hash.
    second = {"seq": 1, "id": "chained-1", "prev_hash": compute_entry_hash(first)}
    with p.open("a") as f:
        f.write(json.dumps(second, sort_keys=True, separators=(",", ":")) + "\n")

    ok, violations, status = verify_chain(p)
    assert ok is False
    assert status == "broken"
    assert any("partial chain" in str(v) for v in violations)


def test_verify_chain_missing_file_is_unchained(tmp_path):
    """Non-existent ledger file is treated as unchained (nothing written yet)."""
    p = tmp_path / "nonexistent.ndjson"
    assert not p.exists()
    ok, violations, status = verify_chain(p)
    assert ok is True
    assert violations == []
    assert status == "unchained"


# ---------------------------------------------------------------------------
# Origin pinning: prefix-strip / prefix-deletion attack regression
# ---------------------------------------------------------------------------


def test_verify_chain_prefix_deletion_reanchor_attack_is_broken(tmp_path):
    """PR #1085 bypass regression: attacker deletes a prefix and re-anchors.

    A fully-chained ledger pins its origin at line 1. An attacker holding a
    valid ledger can delete the first k+1 entries and rewrite the new first
    entry to use GENESIS_HASH; the suffix still links correctly because
    canonical_json excludes prev_hash. Without origin pinning this verifies
    as "verified"; with pinning the recorded origin hash no longer matches
    the observed first chained entry and the ledger is "broken".
    """
    p = tmp_path / "attack.ndjson"
    for i in range(5):
        append_chained_entry(p, {"seq": i, "event": "dispatch", "id": f"e{i}"})

    # Sanity: intact chain verifies.
    ok, violations, status = verify_chain(p)
    assert ok is True
    assert status == "verified"

    lines = p.read_text().splitlines()
    # Delete the prefix entries 0 and 1.
    del lines[0:2]
    # Re-anchor the new first entry (originally entry 2) to GENESIS_HASH.
    reanchored = json.loads(lines[0])
    reanchored["prev_hash"] = GENESIS_HASH
    lines[0] = json.dumps(reanchored)
    p.write_text("\n".join(lines) + "\n")

    ok, violations, status = verify_chain(p)
    assert ok is False
    assert status == "broken"
    assert any("origin" in str(v).lower() for v in violations)


def test_verify_chain_stripped_prefix_with_reanchor_is_broken(tmp_path):
    """Variant: prev_hash is stripped from a prefix but the lines remain.

    The recorded origin is at line 1. Stripping prev_hash from entries 0..k
    moves the observed switch point to line k+2. verify_chain detects the
    origin line mismatch.
    """
    p = tmp_path / "strip-attack.ndjson"
    for i in range(5):
        append_chained_entry(p, {"seq": i, "event": "dispatch", "id": f"e{i}"})

    lines = p.read_text().splitlines()
    # Strip prev_hash from entries 0 and 1.
    for i in range(2):
        stripped = json.loads(lines[i])
        stripped.pop("prev_hash", None)
        lines[i] = json.dumps(stripped)
    # Re-anchor entry 2 (now the switch point) to GENESIS.
    reanchored = json.loads(lines[2])
    reanchored["prev_hash"] = GENESIS_HASH
    lines[2] = json.dumps(reanchored)
    p.write_text("\n".join(lines) + "\n")

    ok, violations, status = verify_chain(p)
    assert ok is False
    assert status == "broken"
    assert any("origin" in str(v).lower() for v in violations)


def test_verify_chain_mid_ledger_adoption_is_verified(tmp_path):
    """Approach B preserves genuine mid-ledger adoption.

    A legacy unchained prefix (chaining never adopted before line N) is valid
    when the chain origin was recorded at the adoption point: entries before
    the origin are tolerated, entries from the origin onward must chain.
    """
    p = tmp_path / "mid-adopt.ndjson"
    # Legacy unchained prefix.
    for i in range(3):
        with p.open("a") as f:
            f.write(json.dumps({"seq": i, "event": "legacy"}) + "\n")

    # First chained entry starts a new epoch.
    append_chained_entry(p, {"seq": 3, "event": "dispatch"})
    append_chained_entry(p, {"seq": 4, "event": "dispatch"})

    ok, violations, status = verify_chain(p)
    assert ok is True
    assert status == "verified"
    assert violations == []
