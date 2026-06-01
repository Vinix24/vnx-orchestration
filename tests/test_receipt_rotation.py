#!/usr/bin/env python3
"""Tests for vnx_receipt_rotate — chain-safe receipt-ledger rotation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from ndjson_hash_chain import (
    GENESIS_HASH,
    append_chained_entry,
    compute_entry_hash,
    verify_chain,
    verify_history,
)
from vnx_receipt_rotate import (
    _count_lines,
    _read_last_entry,
    _ROTATION_EVENT_TYPE,
    check,
    rotate,
)


def _make_receipt(index: int) -> dict:
    return {
        "event_type": "task_complete",
        "dispatch_id": f"DISP-{index:04d}",
        "terminal": "T1",
        "timestamp": f"2026-01-{(index % 28) + 1:02d}T10:00:00Z",
        "status": "success",
    }


def _write_receipts(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for i in range(count):
            f.write(json.dumps(_make_receipt(i)) + "\n")


def _write_large_receipts(path: Path, target_mb: float) -> None:
    """Write enough entries to exceed target_mb."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = _make_receipt(0)
    line = json.dumps(entry) + "\n"
    line_bytes = len(line.encode("utf-8"))
    target_bytes = int(target_mb * 1024 * 1024) + 1
    count = max(1, target_bytes // line_bytes)
    with path.open("w", encoding="utf-8") as f:
        for i in range(count):
            f.write(json.dumps(_make_receipt(i)) + "\n")


class TestCheckCommand:
    def test_check_nonexistent_file(self, tmp_path):
        result = check(receipts_file=str(tmp_path / "missing.ndjson"))
        assert result["size_mb"] == 0.0
        assert result["would_rotate"] is False

    def test_check_small_file(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        _write_receipts(receipts, 5)
        result = check(receipts_file=str(receipts), max_mb=50.0)
        assert result["size_mb"] < 50.0
        assert result["would_rotate"] is False
        assert result["line_count"] == 5

    def test_check_would_rotate(self, tmp_path):
        receipts = tmp_path / "t0_receipts.ndjson"
        _write_large_receipts(receipts, 0.01)
        result = check(receipts_file=str(receipts), max_mb=0.001)
        assert result["would_rotate"] is True


class TestRotationBelowThreshold:
    def test_no_rotation_when_below_threshold(self, tmp_path):
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipts(receipts, 10)
        result = rotate(receipts_file=str(receipts), max_mb=500.0)
        assert result["rotated"] is False
        assert result["reason"] == "below_threshold"
        assert receipts.exists()

    def test_no_rotation_missing_file(self, tmp_path):
        result = rotate(receipts_file=str(tmp_path / "missing.ndjson"))
        assert result["rotated"] is False
        assert result["reason"] == "receipts_file_not_found"


class TestRotationExecuted:
    def test_rotation_creates_archive(self, tmp_path):
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        archive_dir = tmp_path / "state" / "archive"
        _write_receipts(receipts, 20)

        result = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)

        assert result["rotated"] is True
        assert result["archive_path"] is not None
        archive_path = Path(result["archive_path"])
        assert archive_path.exists()
        assert archive_path.parent == archive_dir

    def test_rotation_creates_fresh_live_file(self, tmp_path):
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipts(receipts, 20)

        rotate(receipts_file=str(receipts), max_mb=0.0001, force=True)

        assert receipts.exists()
        lines = receipts.read_text().strip().split("\n")
        assert len(lines) == 1
        sentinel = json.loads(lines[0])
        assert sentinel["event_type"] == _ROTATION_EVENT_TYPE

    def test_sentinel_has_archive_path(self, tmp_path):
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipts(receipts, 5)

        result = rotate(receipts_file=str(receipts), max_mb=0.0001, force=True)

        sentinel = json.loads(receipts.read_text().strip().split("\n")[0])
        assert sentinel["archive_path"] == result["archive_path"]
        assert sentinel["archived_lines"] == result["archived_lines"]

    def test_archived_lines_matches_original_count(self, tmp_path):
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipts(receipts, 42)

        result = rotate(receipts_file=str(receipts), max_mb=0.0001, force=True)

        assert result["archived_lines"] == 42

    def test_archive_filename_contains_timestamp(self, tmp_path):
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipts(receipts, 3)

        result = rotate(receipts_file=str(receipts), max_mb=0.0001, force=True)

        archive_name = Path(result["archive_path"]).name
        assert archive_name.startswith("t0_receipts-")
        assert archive_name.endswith(".ndjson")


class TestChainContinuity:
    def test_sentinel_prev_hash_equals_last_archived_entry_hash(self, tmp_path):
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipts(receipts, 10)

        last_entry = _read_last_entry(receipts)
        expected_prev_hash = compute_entry_hash(last_entry)

        rotate(receipts_file=str(receipts), max_mb=0.0001, force=True)

        sentinel = json.loads(receipts.read_text().strip().split("\n")[0])
        assert sentinel["prev_hash"] == expected_prev_hash

    def test_genesis_hash_when_ledger_empty(self, tmp_path):
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        receipts.parent.mkdir(parents=True, exist_ok=True)
        receipts.write_text("")

        rotate(receipts_file=str(receipts), max_mb=0.0, force=True)

        sentinel = json.loads(receipts.read_text().strip().split("\n")[0])
        assert sentinel["prev_hash"] == GENESIS_HASH

    def test_chain_valid_across_rotation_boundary(self, tmp_path):
        """Verify full-history chain: archive entries + rotation sentinel + new entries."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        archive_dir = tmp_path / "state" / "archive"

        # Write entries WITH prev_hash so verify_chain works.
        from ndjson_hash_chain import append_chained_entry
        for i in range(5):
            entry = _make_receipt(i)
            append_chained_entry(receipts, entry)

        result = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)

        archive_path = Path(result["archive_path"])

        # Verify archive chain is internally valid.
        ok, violations = verify_chain(archive_path)
        assert ok, f"Archive chain violations: {violations}"

        # The new live file starts with the rotation sentinel.
        sentinel = json.loads(receipts.read_text().strip().split("\n")[0])
        assert sentinel["event_type"] == _ROTATION_EVENT_TYPE

        # The sentinel's prev_hash must equal the hash of the last archived entry.
        archive_entries = [json.loads(l) for l in archive_path.read_text().strip().split("\n") if l.strip()]
        last_archive_hash = compute_entry_hash(archive_entries[-1])
        assert sentinel["prev_hash"] == last_archive_hash

    def test_multi_rotation_second_prev_hash_links_to_first_archive_last(self, tmp_path):
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        archive_dir = tmp_path / "state" / "archive"

        _write_receipts(receipts, 5)
        result1 = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)

        # Append more entries to the new live file.
        with receipts.open("a", encoding="utf-8") as f:
            for i in range(5, 10):
                f.write(json.dumps(_make_receipt(i)) + "\n")

        # Read the last entry of the live file before second rotation.
        last_entry = _read_last_entry(receipts)
        expected_prev_hash = compute_entry_hash(last_entry)

        result2 = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)

        sentinel2 = json.loads(receipts.read_text().strip().split("\n")[0])
        assert sentinel2["prev_hash"] == expected_prev_hash

        archive1 = Path(result1["archive_path"])
        archive2 = Path(result2["archive_path"])
        assert archive1.exists()
        assert archive2.exists()
        assert archive1 != archive2


class TestProcessorContinuity:
    def test_idempotency_cache_survives_rotation(self, tmp_path):
        """Rotation must not clear the idempotency cache — no dups/skips after."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipts(receipts, 10)

        # Write a fake idempotency cache alongside receipts.
        cache = receipts.parent / "receipt_idempotency_recent.ndjson"
        cache.write_text('{"ts":1700000000,"key":"abc123"}\n')

        rotate(receipts_file=str(receipts), max_mb=0.0001, force=True)

        # Idempotency cache must still exist with original content.
        assert cache.exists()
        assert "abc123" in cache.read_text()

    def test_rotation_is_atomic_no_data_loss(self, tmp_path):
        """After rotation, all original entries are in the archive."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        original_entries = [_make_receipt(i) for i in range(15)]
        receipts.parent.mkdir(parents=True, exist_ok=True)
        with receipts.open("w") as f:
            for e in original_entries:
                f.write(json.dumps(e) + "\n")

        result = rotate(receipts_file=str(receipts), max_mb=0.0001, force=True)

        archive_path = Path(result["archive_path"])
        archived_entries = [json.loads(l) for l in archive_path.read_text().strip().split("\n") if l.strip()]
        assert len(archived_entries) == 15
        for orig, arch in zip(original_entries, archived_entries):
            assert orig["dispatch_id"] == arch["dispatch_id"]

    def test_new_live_file_writable_after_rotation(self, tmp_path):
        """After rotation, the new live file accepts appends (simulates append_receipt.py)."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipts(receipts, 5)

        rotate(receipts_file=str(receipts), max_mb=0.0001, force=True)

        # Simulate an appender writing a new receipt.
        new_receipt = _make_receipt(99)
        with receipts.open("a", encoding="utf-8") as f:
            f.write(json.dumps(new_receipt) + "\n")

        lines = [l for l in receipts.read_text().strip().split("\n") if l.strip()]
        assert len(lines) == 2
        assert json.loads(lines[1])["dispatch_id"] == "DISP-0099"


class TestFullHistoryVerification:
    """Tests for verify_history: cross-rotation chain continuity."""

    def test_full_history_valid_after_rotation_with_chained_appends(self, tmp_path):
        """Archive + live together form a valid chain after rotation + new chained entries."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        archive_dir = tmp_path / "state" / "archive"

        for i in range(5):
            append_chained_entry(receipts, _make_receipt(i))

        result = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)
        archive_path = Path(result["archive_path"])

        # Append M entries to the live file via append_chained_entry so they chain from sentinel.
        for i in range(5, 10):
            append_chained_entry(receipts, _make_receipt(i))

        ok, violations = verify_history([archive_path, receipts])
        assert ok, f"Full-history violations: {violations}"

    def test_full_history_tampered_archive_entry_fails(self, tmp_path):
        """Tampering any archive entry breaks the full-history chain."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        archive_dir = tmp_path / "state" / "archive"

        for i in range(5):
            append_chained_entry(receipts, _make_receipt(i))

        result = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)
        archive_path = Path(result["archive_path"])

        for i in range(5, 8):
            append_chained_entry(receipts, _make_receipt(i))

        # Tamper an entry in the middle of the archive.
        lines = archive_path.read_text().strip().split("\n")
        tampered = json.loads(lines[2])
        tampered["status"] = "TAMPERED"
        lines[2] = json.dumps(tampered)
        archive_path.write_text("\n".join(lines) + "\n")

        ok, violations = verify_history([archive_path, receipts])
        assert not ok
        assert len(violations) >= 1

    def test_full_history_tampered_live_entry_fails(self, tmp_path):
        """Tampering a post-rotation live entry breaks the full-history chain."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        archive_dir = tmp_path / "state" / "archive"

        for i in range(3):
            append_chained_entry(receipts, _make_receipt(i))

        result = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)
        archive_path = Path(result["archive_path"])

        for i in range(3, 6):
            append_chained_entry(receipts, _make_receipt(i))

        # Tamper an entry in the live file (not the sentinel).
        lines = receipts.read_text().strip().split("\n")
        assert len(lines) >= 3
        tampered = json.loads(lines[2])
        tampered["terminal"] = "TAMPERED"
        lines[2] = json.dumps(tampered)
        receipts.write_text("\n".join(lines) + "\n")

        ok, violations = verify_history([archive_path, receipts])
        assert not ok
        assert len(violations) >= 1

    def test_verify_chain_with_nongenesis_expected_prev(self, tmp_path):
        """verify_chain(path, expected_prev=<non-genesis>) validates a post-rotation segment."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        archive_dir = tmp_path / "state" / "archive"

        for i in range(4):
            append_chained_entry(receipts, _make_receipt(i))

        archive_last_entry = _read_last_entry(receipts)
        archive_tail_hash = compute_entry_hash(archive_last_entry)

        result = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)

        # The sentinel is the first entry of the new live file.
        # verify_chain with expected_prev=archive_tail_hash must accept it.
        ok, violations = verify_chain(receipts, expected_prev=archive_tail_hash)
        assert ok, f"Live-segment violations: {violations}"

    def test_multi_archive_full_history_valid(self, tmp_path):
        """Two rotations: [archive1, archive2, live] form a valid history chain."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        archive_dir = tmp_path / "state" / "archive"

        for i in range(3):
            append_chained_entry(receipts, _make_receipt(i))
        result1 = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)
        archive1 = Path(result1["archive_path"])

        for i in range(3, 6):
            append_chained_entry(receipts, _make_receipt(i))
        result2 = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)
        archive2 = Path(result2["archive_path"])

        for i in range(6, 9):
            append_chained_entry(receipts, _make_receipt(i))

        ok, violations = verify_history([archive1, archive2, receipts])
        assert ok, f"Multi-archive history violations: {violations}"


class TestProcessorRotationAwareness:
    """Tests verifying that rotation is detectable via inode change and chain remains intact."""

    def test_inode_changes_on_atomic_rotation(self, tmp_path):
        """The new live file has a different inode from the archived file (atomic rename)."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        _write_receipts(receipts, 10)
        pre_inode = receipts.stat().st_ino

        rotate(receipts_file=str(receipts), max_mb=0.0001, force=True)

        post_inode = receipts.stat().st_ino
        assert post_inode != pre_inode, "Inode must differ after rotation (new file created)"

    def test_processor_continuation_no_dup_no_skip(self, tmp_path):
        """After rotation, exactly-once: N archived + M new live — no duplicates, no skips."""
        receipts = tmp_path / "state" / "t0_receipts.ndjson"
        archive_dir = tmp_path / "state" / "archive"

        N = 5
        M = 4

        # Simulate processor having written N receipts.
        for i in range(N):
            append_chained_entry(receipts, _make_receipt(i))

        pre_inode = receipts.stat().st_ino

        # Rotation happens.
        result = rotate(receipts_file=str(receipts), archive_dir=str(archive_dir), max_mb=0.0001, force=True)
        archive_path = Path(result["archive_path"])

        # Processor detects rotation via inode change.
        post_inode = receipts.stat().st_ino
        assert post_inode != pre_inode, "Processor must detect inode change as rotation signal"

        # Processor continues: writes M new receipts to the new live file.
        for i in range(N, N + M):
            append_chained_entry(receipts, _make_receipt(i))

        # Verify: N entries in archive, sentinel + M in live.
        archive_lines = [l for l in archive_path.read_text().strip().split("\n") if l.strip()]
        live_lines = [l for l in receipts.read_text().strip().split("\n") if l.strip()]
        assert len(archive_lines) == N
        assert len(live_lines) == M + 1  # sentinel + M

        # No dispatch IDs duplicated across archive and live.
        all_entries = [json.loads(l) for l in archive_lines + live_lines]
        receipt_entries = [e for e in all_entries if e.get("event_type") not in (_ROTATION_EVENT_TYPE,)]
        dispatch_ids = [e["dispatch_id"] for e in receipt_entries]
        assert len(dispatch_ids) == len(set(dispatch_ids)), "No duplicate dispatch IDs across archive + live"

        # Full-history chain is valid end-to-end.
        ok, violations = verify_history([archive_path, receipts])
        assert ok, f"Full-history violations after processor continuation: {violations}"
