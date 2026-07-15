"""Tests for the ADR-033 chain-origin anchor (scripts/lib/chain_origin_anchor.py)
and its wiring into ndjson_hash_chain.verify_chain / chain_epoch_seal.seal_ledger.

Covers the DoD from the hashchain-anchor dispatch:
  - the documented ADR-029 prefix-strip-and-reseal bypass is now `broken`
  - a legitimately sealed ledger with a matching anchor still verifies
  - no anchor present -> identical result to the pre-ADR-033 verifier
  - first-seal bootstrap writes the anchor exactly once; a second seal is a
    no-op (idempotent, append-only)
  - fail-closed behavior on a corrupt anchor file
  - a moved epoch boundary (line number shifts, content unchanged) is caught
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import chain_origin_anchor  # noqa: E402
from ndjson_hash_chain import (  # noqa: E402
    append_chained_entry,
    append_epoch_marker,
    compute_entry_hash,
    seal_chain_origin,
    verify_chain,
    _verify_chain_unanchored,
)

# Load scripts/chain_epoch_seal.py (not importable as a package name).
_spec = importlib.util.spec_from_file_location(
    "chain_epoch_seal", REPO / "scripts" / "chain_epoch_seal.py"
)
seal_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seal_mod)  # type: ignore[union-attr]
seal_ledger = seal_mod.seal_ledger


def _write_unchained(path: Path, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        with path.open("a") as f:
            f.write(json.dumps({"seq": i, "old": True}) + "\n")


# ---------------------------------------------------------------------------
# The bypass is closed
# ---------------------------------------------------------------------------


def test_bypass_closed_truncate_and_reseal_is_broken(tmp_path):
    """Build a sealed ledger, record its anchor, then truncate-and-reseal it:
    strip the original unchained prefix + original epoch marker, keep only the
    tail chained entries' bodies, and re-chain them under a FRESH GENESIS-
    rooted marker. This is exactly the ADR-029 threat-model bypass: before
    ADR-033, `verify_chain` returns `verified-segmented` (valid) for this.
    After the anchor, it must return `broken`.
    """
    p = tmp_path / "ledger.ndjson"
    _write_unchained(p, 3)                    # epoch-0 unchained prefix (lines 1-3)
    res = seal_ledger(p)                      # seals epoch 1 (line 4) + pins the anchor
    assert res["action"] == "sealed"
    append_chained_entry(p, {"seq": 3, "payload": "legit-a"})   # line 5
    append_chained_entry(p, {"seq": 4, "payload": "legit-b"})   # line 6

    ok, _v, status = verify_chain(p)
    assert ok is True and status == "verified-segmented"       # sanity: legit ledger passes

    anchor = chain_origin_anchor.read_origin_anchor(p)
    assert anchor is not None
    assert anchor["origin_line_number"] == 4
    assert anchor["origin_type"] == "epoch_marker"

    # --- Attack: strip lines 1-4 (prefix + original marker), keep the tail
    # entries' bodies, re-chain them from a brand-new GENESIS-rooted marker.
    tail_bodies = []
    for line in p.read_text().splitlines()[4:]:
        body = json.loads(line)
        body.pop("prev_hash", None)
        tail_bodies.append(body)

    p.write_text("")  # truncate the ledger
    append_epoch_marker(p, 1)                 # fresh marker, prev_hash=GENESIS, now at line 1
    for body in tail_bodies:
        append_chained_entry(p, body)

    ok2, violations2, status2 = verify_chain(p)
    assert ok2 is False
    assert status2 == "broken"
    assert any("anchor" in str(v.get("note", "")) for v in violations2)


def test_moved_epoch_boundary_same_content_is_broken(tmp_path):
    """A subtler variant: the attacker removes exactly ONE prefix line before
    the original marker. The marker's own content (and hence hash) is
    byte-identical, but it now lives at a different line number. The pinned
    anchor must catch the shifted boundary even though the hash matches.
    """
    p = tmp_path / "ledger.ndjson"
    _write_unchained(p, 3)
    seal_ledger(p)  # marker at line 4, anchor pinned there
    append_chained_entry(p, {"seq": 3})

    anchor = chain_origin_anchor.read_origin_anchor(p)
    assert anchor["origin_line_number"] == 4

    lines = p.read_text().splitlines()
    # Drop the first unchained prefix line (index 0) only; marker + tail intact.
    new_lines = lines[1:]
    p.write_text("\n".join(new_lines) + "\n")

    # The marker line itself is untouched content-wise, but it's now line 3.
    ok, violations, status = verify_chain(p)
    assert ok is False
    assert status == "broken"
    assert any("anchor" in str(v.get("note", "")) for v in violations)


# ---------------------------------------------------------------------------
# Legitimate seal + matching anchor stays healthy
# ---------------------------------------------------------------------------


def test_legitimate_seal_with_matching_anchor_is_verified_segmented(tmp_path):
    p = tmp_path / "ledger.ndjson"
    _write_unchained(p, 2)
    seal_ledger(p)
    append_chained_entry(p, {"seq": 2})
    append_chained_entry(p, {"seq": 3})

    ok, violations, status = verify_chain(p)
    assert ok is True
    assert violations == []
    assert status == "verified-segmented"


def test_legitimate_second_epoch_keeps_original_anchor_and_still_verifies(tmp_path):
    """A second, immediately-chained epoch (no unchained gap in between) must
    NOT move the pinned anchor — it stays pinned to the very first sealed
    origin — and the ledger stays healthy."""
    p = tmp_path / "ledger.ndjson"
    _write_unchained(p, 1)
    seal_ledger(p)                              # epoch 1, anchor pinned at its marker (line 2)
    append_chained_entry(p, {"seq": 1})
    anchor_after_first_seal = chain_origin_anchor.read_origin_anchor(p)
    assert anchor_after_first_seal["origin_line_number"] == 2

    append_epoch_marker(p, 2)                    # epoch 2 opens immediately, no gap
    append_chained_entry(p, {"seq": 2})

    seal_chain_origin(p)                          # idempotent — must stay a no-op
    anchor_after_second_epoch = chain_origin_anchor.read_origin_anchor(p)
    assert anchor_after_second_epoch == anchor_after_first_seal   # untouched

    ok, violations, status = verify_chain(p)
    assert ok is True
    assert violations == []
    assert status == "verified-segmented"


def test_marker_less_genesis_chain_anchors_first_entry(tmp_path):
    """A ledger chained directly from GENESIS with no epoch marker at all
    ('verified') anchors to its first entry, not a marker."""
    p = tmp_path / "ledger.ndjson"
    append_chained_entry(p, {"seq": 0})
    append_chained_entry(p, {"seq": 1})

    anchor = seal_chain_origin(p)
    assert anchor is not None
    assert anchor["origin_type"] == "genesis_entry"
    assert anchor["origin_line_number"] == 1

    ok, _v, status = verify_chain(p)
    assert ok is True and status == "verified"

    # Tamper: strip the first entry's prev_hash-bearing status by removing it
    # and re-anchoring the remainder from a fresh GENESIS link.
    lines = p.read_text().splitlines()
    second = json.loads(lines[1])
    second_body = {k: v for k, v in second.items() if k != "prev_hash"}
    p.write_text("")
    append_chained_entry(p, second_body)  # now chains from GENESIS at line 1 again

    ok2, violations2, status2 = verify_chain(p)
    assert ok2 is False
    assert status2 == "broken"
    assert any("anchor" in str(v.get("note", "")) for v in violations2)


# ---------------------------------------------------------------------------
# No anchor present -> byte-for-byte unchanged (regression guard)
# ---------------------------------------------------------------------------


def test_no_anchor_present_matches_unanchored_verifier(tmp_path):
    p = tmp_path / "ledger.ndjson"
    _write_unchained(p, 2)
    append_epoch_marker(p, 1)
    append_chained_entry(p, {"seq": 2})
    append_chained_entry(p, {"seq": 3})

    assert chain_origin_anchor.read_origin_anchor(p) is None  # never sealed via seal_ledger

    anchored = verify_chain(p)
    unanchored = _verify_chain_unanchored(p)
    assert anchored == unanchored


def test_no_anchor_present_tampered_ledger_still_broken_same_as_before(tmp_path):
    p = tmp_path / "ledger.ndjson"
    append_chained_entry(p, {"seq": 0})
    append_chained_entry(p, {"seq": 1})
    append_chained_entry(p, {"seq": 2})
    lines = p.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["seq"] = 99
    lines[1] = json.dumps(tampered)
    p.write_text("\n".join(lines) + "\n")

    assert chain_origin_anchor.read_origin_anchor(p) is None
    assert verify_chain(p) == _verify_chain_unanchored(p)
    ok, _v, status = verify_chain(p)
    assert ok is False and status == "broken"


# ---------------------------------------------------------------------------
# First-seal bootstrap writes the anchor exactly once; idempotent, append-only
# ---------------------------------------------------------------------------


def test_first_seal_writes_anchor_once_second_seal_is_noop(tmp_path):
    p = tmp_path / "ledger.ndjson"
    _write_unchained(p, 2)

    first = seal_ledger(p)
    assert first["action"] == "sealed"
    anchor_file = chain_origin_anchor.anchor_path(p)
    assert anchor_file.exists()
    lines_after_first = anchor_file.read_text().splitlines()
    assert len(lines_after_first) == 1

    second = seal_ledger(p)
    assert second["action"] == "noop"
    lines_after_second = anchor_file.read_text().splitlines()
    assert lines_after_second == lines_after_first  # nothing appended, byte-identical


def test_write_origin_anchor_if_absent_is_truly_write_once(tmp_path):
    """Direct unit test on the anchor primitive: a second call with a
    DIFFERENT origin for the same ledger must NOT overwrite the first."""
    p = tmp_path / "ledger.ndjson"
    origin_a = {"origin_type": "epoch_marker", "origin_hash": "a" * 64, "origin_line_number": 4, "origin_epoch": 1}
    origin_b = {"origin_type": "epoch_marker", "origin_hash": "b" * 64, "origin_line_number": 9, "origin_epoch": 2}

    first = chain_origin_anchor.write_origin_anchor_if_absent(p, origin_a)
    assert first["origin_hash"] == "a" * 64

    second = chain_origin_anchor.write_origin_anchor_if_absent(p, origin_b)
    assert second["origin_hash"] == "a" * 64  # unchanged — origin_b never wins

    anchor_file = chain_origin_anchor.anchor_path(p)
    lines = anchor_file.read_text().splitlines()
    assert len(lines) == 1  # exactly one record ever, append-only


def test_anchor_path_is_sibling_file(tmp_path):
    p = tmp_path / "state" / "t0_receipts.ndjson"
    anchor_file = chain_origin_anchor.anchor_path(p)
    assert anchor_file.parent == p.parent
    assert anchor_file.name == "t0_receipts.ndjson.origin_anchor.ndjson"


# ---------------------------------------------------------------------------
# Fail-closed behavior
# ---------------------------------------------------------------------------


def test_corrupt_anchor_file_fails_closed(tmp_path):
    p = tmp_path / "ledger.ndjson"
    _write_unchained(p, 2)
    seal_ledger(p)
    append_chained_entry(p, {"seq": 2})

    ok, _v, status = verify_chain(p)
    assert ok is True and status == "verified-segmented"  # sane before corruption

    anchor_file = chain_origin_anchor.anchor_path(p)
    anchor_file.write_text("{not valid json at all\n")

    assert chain_origin_anchor.read_origin_anchor(p) == {"_corrupt": True}

    ok2, violations2, status2 = verify_chain(p)
    assert ok2 is False
    assert status2 == "broken"
    assert any("corrupt" in str(v.get("note", "")) for v in violations2)


def test_anchor_pinned_but_ledger_erased_to_unchained_is_broken(tmp_path):
    """If the entire ledger is wiped back to a plain unchained state while an
    anchor from a prior seal still exists, that is history erasure and must
    be reported broken, not silently accepted as 'chaining not yet enabled'."""
    p = tmp_path / "ledger.ndjson"
    _write_unchained(p, 2)
    seal_ledger(p)
    append_chained_entry(p, {"seq": 2})

    assert chain_origin_anchor.read_origin_anchor(p) is not None

    p.write_text(json.dumps({"seq": 0, "wiped": True}) + "\n")  # plain, no prev_hash anywhere

    ok, violations, status = verify_chain(p)
    assert ok is False
    assert status == "broken"
    assert any("anchor" in str(v.get("note", "")) for v in violations)


def test_seal_chain_origin_noop_on_unchained_ledger(tmp_path):
    p = tmp_path / "ledger.ndjson"
    _write_unchained(p, 3)  # no marker, no chaining at all yet
    assert seal_chain_origin(p) is None
    assert chain_origin_anchor.read_origin_anchor(p) is None


def test_seal_chain_origin_noop_on_empty_or_absent_ledger(tmp_path):
    p = tmp_path / "absent.ndjson"
    assert seal_chain_origin(p) is None
    p.touch()
    assert seal_chain_origin(p) is None
