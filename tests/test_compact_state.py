"""Tests for scripts/compact_state.py — state file rotation and compaction."""
from __future__ import annotations

import datetime
import gzip
import json
import sys
import time
from pathlib import Path

import pytest

# Make scripts/ importable
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
if str(_SCRIPTS_DIR / "lib") not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

import compact_state as cs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ndjson_line(timestamp: float, extra: str = "") -> str:
    return json.dumps({"timestamp": timestamp, "data": extra}) + "\n"


def _iso(days_ago: float) -> str:
    ts = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return ts.isoformat()


def _read_archive(archive_path: Path) -> list[str]:
    with gzip.open(archive_path, "rt", encoding="utf-8") as f:
        return [l for l in f.read().splitlines() if l.strip()]


def _setup_state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    return state


# ---------------------------------------------------------------------------
# intelligence_archive tests
# ---------------------------------------------------------------------------

class TestIntelligenceArchive:
    def test_rotation_archives_old_keeps_recent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Synthetic 100MB+ file: lines >7d old are archived; <7d lines stay live."""
        monkeypatch.setattr(cs, "INTELLIGENCE_ARCHIVE_MIN_MB", 0)

        state = _setup_state(tmp_path)
        live = state / "t0_intelligence_archive.ndjson"

        now = time.time()
        old_ts = now - 8 * 86400   # 8 days ago
        new_ts = now - 1 * 86400   # 1 day ago

        # Each line ~10KB; 5000 old + 5000 new ≈ 100MB synthetic file
        padding = "x" * 9900
        old_lines = [_make_ndjson_line(old_ts + i * 0.001, padding) for i in range(5000)]
        new_lines = [_make_ndjson_line(new_ts + i * 0.001, padding) for i in range(5000)]
        live.write_text("".join(old_lines + new_lines), encoding="utf-8")

        rc = cs.compact_intelligence_archive(state)

        assert rc == 0

        archives = list((state / "archive").glob("t0_intelligence_archive_*.ndjson.gz"))
        assert len(archives) == 1, "exactly one archive created"

        archived = _read_archive(archives[0])
        assert len(archived) == 5000, f"expected 5000 archived lines, got {len(archived)}"

        live_lines = [l for l in live.read_text().splitlines() if l.strip()]
        assert len(live_lines) == 5000, f"expected 5000 live lines, got {len(live_lines)}"

        # Confirm live lines are all recent
        for raw in live_lines:
            record = json.loads(raw)
            assert record["timestamp"] >= now - 7 * 86400

    def test_skip_below_threshold(self, tmp_path: Path) -> None:
        """Files below 50MB threshold are not rotated."""
        state = _setup_state(tmp_path)
        live = state / "t0_intelligence_archive.ndjson"
        live.write_text(_make_ndjson_line(time.time() - 10 * 86400), encoding="utf-8")

        rc = cs.compact_intelligence_archive(state)

        assert rc == 0
        assert not (state / "archive").exists() or not list((state / "archive").glob("*.gz"))

    def test_skip_when_file_absent(self, tmp_path: Path) -> None:
        state = _setup_state(tmp_path)
        rc = cs.compact_intelligence_archive(state)
        assert rc == 0

    def test_skip_when_all_records_fresh(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cs, "INTELLIGENCE_ARCHIVE_MIN_MB", 0)

        state = _setup_state(tmp_path)
        live = state / "t0_intelligence_archive.ndjson"
        now = time.time()
        live.write_text("".join(_make_ndjson_line(now - i) for i in range(100)), encoding="utf-8")

        rc = cs.compact_intelligence_archive(state)

        assert rc == 0
        archive_dir = state / "archive"
        assert not archive_dir.exists() or not list(archive_dir.glob("*.gz"))


# ---------------------------------------------------------------------------
# receipts tests
# ---------------------------------------------------------------------------

class TestReceipts:
    def test_15000_lines_leaves_10000_archives_5000(self, tmp_path: Path) -> None:
        """15000-line receipts file: 10000 newest retained; 5000 oldest archived."""
        state = _setup_state(tmp_path)
        live = state / "t0_receipts.ndjson"

        lines = [json.dumps({"seq": i, "timestamp": i}) + "\n" for i in range(15_000)]
        live.write_text("".join(lines), encoding="utf-8")

        rc = cs.compact_receipts(state)

        assert rc == 0

        archives = list((state / "archive").glob("t0_receipts_*.ndjson.gz"))
        assert len(archives) == 1

        archived = _read_archive(archives[0])
        assert len(archived) == 5_000, f"expected 5000 archived, got {len(archived)}"

        # Oldest 5000 are archived (seq 0..4999)
        assert json.loads(archived[0])["seq"] == 0
        assert json.loads(archived[-1])["seq"] == 4_999

        live_lines = [l for l in live.read_text().splitlines() if l.strip()]
        assert len(live_lines) == 10_000

        # Newest 10000 retained (seq 5000..14999)
        assert json.loads(live_lines[0])["seq"] == 5_000
        assert json.loads(live_lines[-1])["seq"] == 14_999

    def test_within_cap_no_rotation(self, tmp_path: Path) -> None:
        state = _setup_state(tmp_path)
        live = state / "t0_receipts.ndjson"
        lines = [json.dumps({"seq": i}) + "\n" for i in range(500)]
        live.write_text("".join(lines))

        rc = cs.compact_receipts(state)

        assert rc == 0
        assert not list((state / "archive").glob("*.gz")) if not (state / "archive").exists() else not list((state / "archive").glob("*.gz"))

    def test_skip_when_file_absent(self, tmp_path: Path) -> None:
        state = _setup_state(tmp_path)
        assert cs.compact_receipts(state) == 0

    def test_atomic_write_preserves_existing_on_verify(self, tmp_path: Path) -> None:
        """After rotation the live file is a complete, valid file (not corrupted)."""
        state = _setup_state(tmp_path)
        live = state / "t0_receipts.ndjson"
        lines = [json.dumps({"seq": i}) + "\n" for i in range(12_000)]
        live.write_text("".join(lines))

        rc = cs.compact_receipts(state)
        assert rc == 0

        retained = [json.loads(l) for l in live.read_text().splitlines() if l.strip()]
        assert len(retained) == 10_000
        assert retained[0]["seq"] == 2_000


# ---------------------------------------------------------------------------
# open_items_digest tests
# ---------------------------------------------------------------------------

class TestOpenItemsDigest:
    def _make_digest(self, state: Path, entries: list[dict]) -> Path:
        digest_file = state / "open_items_digest.json"
        digest = {
            "summary": {"open_count": len(entries)},
            "recent_closures": entries,
            "open_items": [],
            "last_updated": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
            "digest_generated": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        }
        digest_file.write_text(json.dumps(digest, indent=2))
        return digest_file

    def test_mixed_dates_only_fresh_retained(self, tmp_path: Path) -> None:
        """Entries with last_updated >30d are evicted; fresh entries remain."""
        state = _setup_state(tmp_path)

        entries = [
            {"id": "OI-001", "title": "Very old", "last_updated": _iso(45)},
            {"id": "OI-002", "title": "Exactly old", "last_updated": _iso(31)},
            {"id": "OI-003", "title": "Fresh", "last_updated": _iso(5)},
            {"id": "OI-004", "title": "No date"},  # no last_updated → keep
            {"id": "OI-005", "title": "Very fresh", "last_updated": _iso(0)},
        ]
        digest_file = self._make_digest(state, entries)

        rc = cs.compact_open_items_digest(state)

        assert rc == 0
        result = json.loads(digest_file.read_text())
        remaining_ids = [e["id"] for e in result["recent_closures"]]

        assert "OI-001" not in remaining_ids, "45d-old entry should be evicted"
        assert "OI-002" not in remaining_ids, "31d-old entry should be evicted"
        assert "OI-003" in remaining_ids, "5d-old entry should be retained"
        assert "OI-004" in remaining_ids, "no-date entry should be retained"
        assert "OI-005" in remaining_ids, "fresh entry should be retained"

    def test_all_fresh_no_mutation(self, tmp_path: Path) -> None:
        state = _setup_state(tmp_path)
        entries = [
            {"id": "OI-001", "title": "Fresh", "last_updated": _iso(1)},
        ]
        digest_file = self._make_digest(state, entries)
        original = digest_file.read_text()

        rc = cs.compact_open_items_digest(state)

        assert rc == 0
        # File should not have been mutated (no-op)
        assert digest_file.read_text() == original

    def test_skip_when_file_absent(self, tmp_path: Path) -> None:
        state = _setup_state(tmp_path)
        assert cs.compact_open_items_digest(state) == 0

    def test_schema_preserved(self, tmp_path: Path) -> None:
        """Top-level scalar fields and other list keys are preserved after eviction."""
        state = _setup_state(tmp_path)
        entries = [
            {"id": "OI-001", "title": "Stale", "last_updated": _iso(60)},
            {"id": "OI-002", "title": "Fresh", "last_updated": _iso(2)},
        ]
        digest_file = self._make_digest(state, entries)

        rc = cs.compact_open_items_digest(state)
        assert rc == 0

        result = json.loads(digest_file.read_text())
        assert "summary" in result
        assert "open_items" in result
        assert "last_updated" in result
        assert "digest_generated" in result


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_intelligence_archive_dry_run_no_mutation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cs, "INTELLIGENCE_ARCHIVE_MIN_MB", 0)

        state = _setup_state(tmp_path)
        live = state / "t0_intelligence_archive.ndjson"
        now = time.time()
        content = "".join(_make_ndjson_line(now - 10 * 86400) for _ in range(10))
        live.write_text(content)
        original = live.read_text()

        rc = cs.compact_intelligence_archive(state, dry_run=True)

        assert rc == 0
        assert live.read_text() == original, "dry-run must not mutate live file"
        assert not (state / "archive").exists() or not list((state / "archive").glob("*.gz"))

    def test_receipts_dry_run_no_mutation(self, tmp_path: Path) -> None:
        state = _setup_state(tmp_path)
        live = state / "t0_receipts.ndjson"
        lines = [json.dumps({"seq": i}) + "\n" for i in range(12_000)]
        content = "".join(lines)
        live.write_text(content)

        rc = cs.compact_receipts(state, dry_run=True)

        assert rc == 0
        assert live.read_text() == content, "dry-run must not mutate live file"
        assert not (state / "archive").exists() or not list((state / "archive").glob("*.gz"))

    def test_open_items_digest_dry_run_no_mutation(self, tmp_path: Path) -> None:
        state = _setup_state(tmp_path)
        entries = [{"id": "OI-001", "title": "Stale", "last_updated": _iso(60)}]
        digest = {"recent_closures": entries, "summary": {}}
        digest_file = state / "open_items_digest.json"
        digest_file.write_text(json.dumps(digest))
        original = digest_file.read_text()

        rc = cs.compact_open_items_digest(state, dry_run=True)

        assert rc == 0
        assert digest_file.read_text() == original, "dry-run must not mutate digest"


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_intelligence_archive_second_run_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second run is a no-op: today's archive already exists."""
        monkeypatch.setattr(cs, "INTELLIGENCE_ARCHIVE_MIN_MB", 0)

        state = _setup_state(tmp_path)
        live = state / "t0_intelligence_archive.ndjson"
        now = time.time()
        old_ts = now - 10 * 86400
        content = "".join(_make_ndjson_line(old_ts + i) for i in range(20))
        live.write_text(content)

        # First run
        rc1 = cs.compact_intelligence_archive(state)
        assert rc1 == 0

        live_after_1 = live.read_text()
        archives_after_1 = list((state / "archive").glob("*.gz"))
        assert len(archives_after_1) == 1

        # Second run
        rc2 = cs.compact_intelligence_archive(state)
        assert rc2 == 0

        assert live.read_text() == live_after_1, "live file unchanged on 2nd run"
        archives_after_2 = list((state / "archive").glob("*.gz"))
        assert len(archives_after_2) == 1, "no new archive created on 2nd run"

    def test_receipts_second_run_noop(self, tmp_path: Path) -> None:
        """Second run is a no-op: today's archive exists OR file is within cap."""
        state = _setup_state(tmp_path)
        live = state / "t0_receipts.ndjson"
        lines = [json.dumps({"seq": i}) + "\n" for i in range(15_000)]
        live.write_text("".join(lines))

        rc1 = cs.compact_receipts(state)
        assert rc1 == 0

        live_after_1 = live.read_text()
        archives_1 = list((state / "archive").glob("*.gz"))
        assert len(archives_1) == 1

        rc2 = cs.compact_receipts(state)
        assert rc2 == 0

        assert live.read_text() == live_after_1, "live file unchanged on 2nd run"
        archives_2 = list((state / "archive").glob("*.gz"))
        assert len(archives_2) == 1, "no new archive on 2nd run"

    def test_open_items_digest_second_run_noop(self, tmp_path: Path) -> None:
        """After eviction, second run finds no stale entries and is a no-op."""
        state = _setup_state(tmp_path)
        entries = [
            {"id": "OI-001", "title": "Old", "last_updated": _iso(40)},
            {"id": "OI-002", "title": "Fresh", "last_updated": _iso(3)},
        ]
        digest_file = state / "open_items_digest.json"
        digest_file.write_text(json.dumps({"recent_closures": entries}))

        rc1 = cs.compact_open_items_digest(state)
        assert rc1 == 0

        content_after_1 = digest_file.read_text()
        remaining = json.loads(content_after_1)["recent_closures"]
        assert len(remaining) == 1  # only fresh entry

        rc2 = cs.compact_open_items_digest(state)
        assert rc2 == 0

        assert digest_file.read_text() == content_after_1, "file unchanged on 2nd run"
