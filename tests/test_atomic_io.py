"""test_atomic_io.py — Unit tests for scripts/lib/atomic_io.py.

Covers: atomic write safety, mode preservation, NDJSON append correctness,
concurrent-write safety, and directory auto-creation.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import sys
import threading
import unittest.mock as mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from atomic_io import atomic_write_text, audit_event_append


# ---------------------------------------------------------------------------
# atomic_write_text tests
# ---------------------------------------------------------------------------


def test_atomic_write_no_partial_on_crash(tmp_path: Path) -> None:
    """Original content must survive when os.replace raises mid-write."""
    target = tmp_path / "state.txt"
    original = "original content"
    target.write_text(original, encoding="utf-8")

    with mock.patch("atomic_io.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            atomic_write_text(target, "new content")

    # Target must be intact; temp file must not linger.
    assert target.read_text(encoding="utf-8") == original
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"temp file leaked: {tmp_files}"


def test_atomic_write_preserves_mode(tmp_path: Path) -> None:
    """Mode of existing file is preserved after atomic overwrite."""
    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    target.chmod(0o750)

    atomic_write_text(target, "#!/bin/sh\necho new\n")

    result_mode = stat.S_IMODE(target.stat().st_mode)
    assert result_mode == 0o750
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\necho new\n"


# ---------------------------------------------------------------------------
# audit_event_append tests
# ---------------------------------------------------------------------------


def test_audit_event_appends_ndjson_line_with_required_fields(tmp_path: Path) -> None:
    """Each appended event contains timestamp, pid, actor, and caller payload."""
    events_dir = tmp_path / "events"
    audit_event_append(events_dir, "decision", {"action": "accept", "dec_id": "DEC-1"})

    target = events_dir / "decision.ndjson"
    assert target.exists()

    lines = [l for l in target.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["event_type"] == "decision"
    assert "timestamp" in record
    assert "pid" in record
    assert "actor" in record
    assert record["action"] == "accept"
    assert record["dec_id"] == "DEC-1"


def _worker_append(events_dir_str: str, n: int) -> None:
    """Subprocess worker: append n events to the shared NDJSON file."""
    events_dir = Path(events_dir_str)
    for i in range(n):
        audit_event_append(events_dir, "conctest", {"seq": i})


def test_audit_event_concurrent_writes_no_interleave(tmp_path: Path) -> None:
    """Concurrent writers produce valid NDJSON with no interleaved partial lines."""
    events_dir = tmp_path / "events"
    n_processes = 4
    n_each = 20

    procs = [
        multiprocessing.Process(target=_worker_append, args=(str(events_dir), n_each))
        for _ in range(n_processes)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0, f"worker exited with code {p.exitcode}"

    target = events_dir / "conctest.ndjson"
    lines = [l for l in target.read_text().splitlines() if l.strip()]
    assert len(lines) == n_processes * n_each

    for line in lines:
        record = json.loads(line)
        assert record["event_type"] == "conctest"


def test_audit_event_creates_dir_if_missing(tmp_path: Path) -> None:
    """audit_event_append creates nested directories automatically."""
    events_dir = tmp_path / "deep" / "nested" / "events"
    assert not events_dir.exists()

    audit_event_append(events_dir, "boot", {"msg": "hello"})

    assert (events_dir / "boot.ndjson").exists()
