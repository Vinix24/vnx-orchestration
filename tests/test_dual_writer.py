"""Tests for scripts/lib/dual_writer.py (Phase 6 P4 helper)."""

from __future__ import annotations

import fcntl
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import dual_writer as DW  # noqa: E402


def test_resolve_central_ndjson_path_rejects_empty():
    assert DW.resolve_central_ndjson_path("", "x.ndjson") is None
    assert DW.resolve_central_ndjson_path(None, "x.ndjson") is None  # type: ignore[arg-type]


def test_resolve_central_ndjson_path_returns_under_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = DW.resolve_central_ndjson_path("vnx-dev", "t0_receipts.ndjson")
    assert p is not None
    assert str(p).startswith(str(tmp_path))
    assert p.name == "t0_receipts.ndjson"


def test_mirror_record_writes_central_when_paths_differ(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    primary = tmp_path / "project" / "state" / "x.ndjson"
    primary.parent.mkdir(parents=True)
    primary.write_text("")

    record = {"event": "dispatch_started", "dispatch_id": "d1"}
    ok = DW.mirror_record_to_central(record, primary, "vnx-dev", "x.ndjson")
    assert ok is True

    central = tmp_path / ".vnx-data" / "vnx-dev" / "state" / "x.ndjson"
    assert central.exists()
    line = central.read_text().strip().splitlines()[-1]
    assert json.loads(line) == record


def test_mirror_record_skips_when_central_equals_primary(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    central = tmp_path / ".vnx-data" / "vnx-dev" / "state" / "x.ndjson"
    central.parent.mkdir(parents=True)
    central.write_text("")
    # Primary path == central path → cutover skip path
    ok = DW.mirror_record_to_central({"a": 1}, central, "vnx-dev", "x.ndjson")
    assert ok is False
    # File untouched (still empty)
    assert central.read_text() == ""


def test_mirror_record_returns_false_on_invalid_project_id(tmp_path: Path):
    primary = tmp_path / "x.ndjson"
    primary.write_text("")
    # Uppercase fails the project_id regex → resolve returns None.
    ok = DW.mirror_record_to_central({"a": 1}, primary, "BAD-ID", "x.ndjson")
    assert ok is False


def test_mirror_record_strict_raises_on_io_failure(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    primary = tmp_path / "project" / "state" / "x.ndjson"
    primary.parent.mkdir(parents=True)
    primary.write_text("")
    # Make the central parent a FILE so mkdir cannot create the dir.
    blocker = tmp_path / ".vnx-data" / "vnx-dev"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("blocker")
    with pytest.raises(OSError):
        DW.mirror_record_to_central_strict(
            {"a": 1}, primary, "vnx-dev", "x.ndjson"
        )


def test_mirror_record_best_effort_swallows_io_error(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    primary = tmp_path / "project" / "state" / "x.ndjson"
    primary.parent.mkdir(parents=True)
    primary.write_text("")
    blocker = tmp_path / ".vnx-data" / "vnx-dev"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("blocker")
    # Best-effort variant must NOT raise.
    ok = DW.mirror_record_to_central({"a": 1}, primary, "vnx-dev", "x.ndjson")
    assert ok is False


# ---------------------------------------------------------------------------
# Codex round-7 finding 4: concurrent reader/writer must share lock surface
# ---------------------------------------------------------------------------

def test_append_record_locked_data_file_lock_acquired(tmp_path: Path):
    """_append_locked must acquire LOCK_EX on the data file itself.

    This verifies the Fix 4 contract: the writer acquires an exclusive lock
    on the data file so that readers using LOCK_SH (e.g.
    dispatch_register._read_register_locked) are properly serialised.

    Strategy: hold LOCK_SH on the data file in the main thread, then have
    a background thread attempt _append_locked with a short timeout. With
    LOCK_EX on the data file, the background thread must block while the
    read lock is held. After releasing the read lock, the append must succeed.
    """
    ndjson = tmp_path / "data.ndjson"
    ndjson.parent.mkdir(parents=True, exist_ok=True)
    ndjson.write_text("")

    append_started = threading.Event()
    append_done = threading.Event()
    was_blocked = {"value": False}

    def _append_worker():
        append_started.set()
        DW.append_record_locked(ndjson, {"x": 1}, lock_filename=".test.lock")
        append_done.set()

    # Acquire LOCK_SH on the data file in the main thread.
    with ndjson.open("r", encoding="utf-8") as read_fh:
        fcntl.flock(read_fh.fileno(), fcntl.LOCK_SH)

        t = threading.Thread(target=_append_worker, daemon=True)
        t.start()
        append_started.wait(timeout=2)

        # Give the writer thread time to attempt the lock.
        time.sleep(0.1)

        # If the writer already finished, it was NOT blocked — that's a failure.
        if append_done.is_set():
            was_blocked["value"] = False
        else:
            was_blocked["value"] = True

        # Release the read lock — writer should unblock now.
        fcntl.flock(read_fh.fileno(), fcntl.LOCK_UN)

    t.join(timeout=3)
    assert not t.is_alive(), "Appender thread did not complete after read lock released"
    assert was_blocked["value"], (
        "Appender was NOT blocked by LOCK_SH on the data file. "
        "Fix 4 requires _append_locked to hold LOCK_EX on the data file to "
        "serialise against readers."
    )
    lines = [line for line in ndjson.read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"x": 1}


def test_concurrent_append_and_read_no_truncated_lines(tmp_path: Path):
    """N writer threads + 1 reader thread must never observe a truncated line.

    Codex finding 4: without data-file LOCK_EX in the writer, a reader
    holding LOCK_SH can interleave mid-append and see a partial JSON line.
    """
    ndjson = tmp_path / "register.ndjson"
    ndjson.parent.mkdir(parents=True, exist_ok=True)
    ndjson.write_text("")

    N = 20
    truncated_lines: list[str] = []
    stop_reader = threading.Event()

    def _reader():
        while not stop_reader.is_set():
            try:
                content = ndjson.read_text(encoding="utf-8", errors="replace")
                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        truncated_lines.append(line)
            except OSError:
                pass
            time.sleep(0.001)

    def _writer(i: int):
        DW.append_record_locked(
            ndjson,
            {"event": "dispatch_created", "dispatch_id": f"d-concurrent-{i:03d}", "seq": i},
            lock_filename=".register.lock",
        )

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    writer_threads = [threading.Thread(target=_writer, args=(i,), daemon=True) for i in range(N)]
    for t in writer_threads:
        t.start()
    for t in writer_threads:
        t.join(timeout=5)

    stop_reader.set()
    reader_thread.join(timeout=2)

    assert not truncated_lines, (
        f"Reader observed {len(truncated_lines)} truncated line(s) during concurrent appends. "
        f"First: {truncated_lines[0]!r}. "
        "Fix 4: _append_locked must hold LOCK_EX on the data file to exclude concurrent readers."
    )

    lines = [line for line in ndjson.read_text().splitlines() if line.strip()]
    assert len(lines) == N, f"Expected {N} records, got {len(lines)}"
