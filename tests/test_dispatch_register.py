"""Tests for dispatch_register.py — append-only NDJSON lifecycle log.

Covers:
  1.  append_event valid event → True, persists JSON line
  2.  append_event invalid event → False, nothing written
  3.  append_event all optional kwargs → all fields present
  4.  append_event writes microsecond-precision timestamp
  5.  read_events chronological order (insertion order)
  6.  read_events since_iso filter
  7.  read_events skips malformed JSON lines silently
  8.  read_events returns empty list when file absent
  9.  CLI append writes correct record
  10. CLI invalid event → exit 1
  11. CLI missing args → exit 2
  12. Concurrent writes via threads → both records present, no corruption
  13. Best-effort: OSError on open → append_event returns False (never raises)
  14. Path resolution: VNX_DATA_DIR env var is respected
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts/lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_register
from dispatch_register import append_event, read_events

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "dispatch_register.py"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch, tmp_path):
    """Route all register I/O into a fresh tmp dir for every test."""
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
    return tmp_path / ".vnx-data"


def _reg_path(data_dir: Path) -> Path:
    return data_dir / "state" / "dispatch_register.ndjson"


# ---------------------------------------------------------------------------
# 1. append_event valid event → True, persists JSON line
# ---------------------------------------------------------------------------

class TestAppendEventValid:
    def test_returns_true(self, isolated_data_dir):
        assert append_event("dispatch_created", dispatch_id="d-001") is True

    def test_persists_json_line(self, isolated_data_dir):
        append_event("dispatch_created", dispatch_id="d-001")
        reg = _reg_path(isolated_data_dir)
        assert reg.exists()
        lines = reg.read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "dispatch_created"
        assert rec["dispatch_id"] == "d-001"
        assert "timestamp" in rec


# ---------------------------------------------------------------------------
# 2. append_event invalid event → False, nothing written
# ---------------------------------------------------------------------------

class TestAppendEventInvalid:
    def test_returns_false_for_unknown_event(self, isolated_data_dir):
        result = append_event("no_such_event", dispatch_id="d-999")
        assert result is False

    def test_no_file_written_for_invalid_event(self, isolated_data_dir):
        append_event("no_such_event")
        assert not _reg_path(isolated_data_dir).exists()


# ---------------------------------------------------------------------------
# 3. append_event all optional kwargs → all fields present
# ---------------------------------------------------------------------------

class TestAppendEventAllKwargs:
    def test_all_fields_persisted(self, isolated_data_dir):
        append_event(
            "gate_passed",
            dispatch_id="abc",
            pr_number=42,
            feature_id="F99",
            terminal="T1",
            gate="codex",
            extra={"foo": "bar"},
        )
        rec = json.loads(_reg_path(isolated_data_dir).read_text().strip())
        assert rec["event"] == "gate_passed"
        assert rec["dispatch_id"] == "abc"
        assert rec["pr_number"] == 42
        assert rec["feature_id"] == "F99"
        assert rec["terminal"] == "T1"
        assert rec["gate"] == "codex"
        assert rec["extra"] == {"foo": "bar"}

    def test_omitted_optional_fields_absent(self, isolated_data_dir):
        append_event("dispatch_created")
        rec = json.loads(_reg_path(isolated_data_dir).read_text().strip())
        assert "dispatch_id" not in rec
        assert "pr_number" not in rec
        assert "feature_id" not in rec
        assert "terminal" not in rec
        assert "gate" not in rec
        assert "extra" not in rec


# ---------------------------------------------------------------------------
# 4. Microsecond-precision timestamp
# ---------------------------------------------------------------------------

class TestTimestampPrecision:
    def test_timestamp_includes_fractional_seconds(self, isolated_data_dir):
        append_event("dispatch_created", dispatch_id="ts-001")
        rec = json.loads(_reg_path(isolated_data_dir).read_text().strip())
        ts = rec["timestamp"]
        # Format: 2026-04-28T12:34:56.123456Z — fractional part has 6 digits before Z
        assert ts.endswith("Z"), f"Timestamp must end with Z, got: {ts}"
        assert "." in ts, f"Timestamp must include fractional seconds, got: {ts}"
        frac_part = ts.split(".")[1].rstrip("Z")
        assert len(frac_part) == 6, f"Expected 6 fractional digits, got {len(frac_part)} in: {ts}"


# ---------------------------------------------------------------------------
# 5. read_events chronological order
# ---------------------------------------------------------------------------

class TestReadEventsOrder:
    def test_returns_insertion_order(self, isolated_data_dir):
        for evt in ("dispatch_created", "dispatch_promoted", "dispatch_started"):
            append_event(evt, dispatch_id="seq-001")
        events = read_events()
        assert [e["event"] for e in events] == [
            "dispatch_created",
            "dispatch_promoted",
            "dispatch_started",
        ]


# ---------------------------------------------------------------------------
# 6. read_events since_iso filter
# ---------------------------------------------------------------------------

class TestReadEventsSinceIso:
    def test_since_iso_excludes_older_events(self, isolated_data_dir):
        reg = _reg_path(isolated_data_dir)
        reg.parent.mkdir(parents=True, exist_ok=True)
        old_ts = "2026-01-01T00:00:00.000000Z"
        new_ts = "2026-06-01T00:00:00.000000Z"
        reg.write_text(
            json.dumps({"timestamp": old_ts, "event": "dispatch_created"}) + "\n"
            + json.dumps({"timestamp": new_ts, "event": "dispatch_promoted"}) + "\n"
        )
        cutoff = "2026-03-01T00:00:00.000000Z"
        events = read_events(since_iso=cutoff)
        assert len(events) == 1
        assert events[0]["event"] == "dispatch_promoted"

    def test_since_iso_includes_equal_timestamp(self, isolated_data_dir):
        reg = _reg_path(isolated_data_dir)
        reg.parent.mkdir(parents=True, exist_ok=True)
        ts = "2026-04-01T12:00:00.000000Z"
        reg.write_text(json.dumps({"timestamp": ts, "event": "dispatch_created"}) + "\n")
        events = read_events(since_iso=ts)
        assert len(events) == 1


# ---------------------------------------------------------------------------
# 7. read_events skips invalid JSON silently
# ---------------------------------------------------------------------------

class TestReadEventsInvalidJson:
    def test_skips_malformed_lines(self, isolated_data_dir):
        reg = _reg_path(isolated_data_dir)
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(
            '{"timestamp":"2026-01-01T00:00:00.000000Z","event":"dispatch_created"}\n'
            "not-valid-json\n"
            '{"timestamp":"2026-01-02T00:00:00.000000Z","event":"dispatch_promoted"}\n'
        )
        events = read_events()
        assert len(events) == 2
        assert events[0]["event"] == "dispatch_created"
        assert events[1]["event"] == "dispatch_promoted"


# ---------------------------------------------------------------------------
# 8. read_events returns empty list when file absent
# ---------------------------------------------------------------------------

class TestReadEventsNoFile:
    def test_returns_empty_list(self, isolated_data_dir):
        assert not _reg_path(isolated_data_dir).exists()
        assert read_events() == []


# ---------------------------------------------------------------------------
# 9–11. CLI tests
# ---------------------------------------------------------------------------

class TestCli:
    def _env(self, isolated_data_dir):
        env = os.environ.copy()
        env["VNX_DATA_DIR"] = str(isolated_data_dir)
        return env

    def test_cli_append_writes_record(self, isolated_data_dir):
        env = self._env(isolated_data_dir)
        result = subprocess.run(
            [
                sys.executable,
                str(_MODULE_PATH),
                "append",
                "dispatch_promoted",
                "dispatch_id=abc",
                "terminal=T1",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        reg = _reg_path(isolated_data_dir)
        rec = json.loads(reg.read_text().strip())
        assert rec["event"] == "dispatch_promoted"
        assert rec["dispatch_id"] == "abc"
        assert rec["terminal"] == "T1"

    def test_cli_invalid_event_exits_1(self, isolated_data_dir):
        env = self._env(isolated_data_dir)
        result = subprocess.run(
            [sys.executable, str(_MODULE_PATH), "append", "bad_event"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1

    def test_cli_missing_args_exits_2(self, isolated_data_dir):
        env = self._env(isolated_data_dir)
        result = subprocess.run(
            [sys.executable, str(_MODULE_PATH)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# 12. Concurrent writes via threads — no corruption
# ---------------------------------------------------------------------------

class TestConcurrentWrites:
    def test_both_records_present(self, isolated_data_dir):
        results = []

        def write(evt):
            r = append_event(evt, dispatch_id="concurrent-test")
            results.append(r)

        t1 = threading.Thread(target=write, args=("dispatch_created",))
        t2 = threading.Thread(target=write, args=("dispatch_promoted",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert all(results), f"One or both writes failed: {results}"
        events = read_events()
        assert len(events) == 2
        event_names = {e["event"] for e in events}
        assert event_names == {"dispatch_created", "dispatch_promoted"}


# ---------------------------------------------------------------------------
# 13. Best-effort: OSError → False, never raises
# ---------------------------------------------------------------------------

class TestBestEffortOsError:
    def test_oserror_returns_false_not_raises(self, isolated_data_dir):
        with patch.object(dispatch_register.Path, "open", side_effect=OSError("disk full")):
            result = append_event("dispatch_created", dispatch_id="oserror-test")
        assert result is False


# ---------------------------------------------------------------------------
# 14. VNX_DATA_DIR path resolution
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_register_lands_in_vnx_data_dir(self, tmp_path, monkeypatch):
        custom_data = tmp_path / "custom-data"
        monkeypatch.setenv("VNX_DATA_DIR", str(custom_data))
        result = append_event("dispatch_created", dispatch_id="path-test")
        assert result is True
        expected = custom_data / "state" / "dispatch_register.ndjson"
        assert expected.exists(), f"Register not found at {expected}"
        rec = json.loads(expected.read_text().strip())
        assert rec["dispatch_id"] == "path-test"
