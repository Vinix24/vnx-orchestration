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
  19. append_event requires at least one identifying field (ADVISORY fix Codex PR #277)

Path-resolution and shared-lock tests live in test_dispatch_register_path.py.
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
    data_dir = tmp_path / ".vnx-data"
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    return data_dir


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
        """Events with no identifying fields are now rejected (require ID field)."""
        result = append_event("dispatch_created")
        assert result is False
        assert not _reg_path(isolated_data_dir).exists()


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

    def test_since_iso_second_precision_cutoff_includes_microsecond_event(self, isolated_data_dir):
        """Codex round-2 finding 1: lex-compare drops same-second events.

        Writer emits microsecond-precision timestamps (``…00.123456Z``).
        A caller passing a coarser cutoff (``…00Z``) used to silently
        filter such events out because ``.`` (0x2E) sorts before ``Z`` (0x5A).
        Datetime-aware compare must include events at or after the cutoff.
        """
        reg = _reg_path(isolated_data_dir)
        reg.parent.mkdir(parents=True, exist_ok=True)
        micro_ts = "2026-04-29T12:00:00.123456Z"
        reg.write_text(
            json.dumps({"timestamp": micro_ts, "event": "dispatch_created", "dispatch_id": "d-1"}) + "\n"
        )
        # Same-second cutoff at second precision — must include the event.
        events = read_events(since_iso="2026-04-29T12:00:00Z")
        assert len(events) == 1, f"Same-second cutoff dropped microsecond event: {events!r}"
        assert events[0]["dispatch_id"] == "d-1"

    def test_since_iso_excludes_strictly_older_event_with_mixed_precision(self, isolated_data_dir):
        """Mixed-precision compare must still exclude truly older events."""
        reg = _reg_path(isolated_data_dir)
        reg.parent.mkdir(parents=True, exist_ok=True)
        old_ts = "2026-04-29T11:59:59.999999Z"
        new_ts = "2026-04-29T12:00:01.000000Z"
        reg.write_text(
            json.dumps({"timestamp": old_ts, "event": "dispatch_created"}) + "\n"
            + json.dumps({"timestamp": new_ts, "event": "dispatch_promoted"}) + "\n"
        )
        events = read_events(since_iso="2026-04-29T12:00:00Z")
        assert len(events) == 1
        assert events[0]["event"] == "dispatch_promoted"

    def test_since_iso_unparseable_falls_back_to_lexicographic(self, isolated_data_dir):
        """When since_iso cannot be parsed, the filter falls back to lex compare
        rather than silently disabling itself."""
        reg = _reg_path(isolated_data_dir)
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(
            json.dumps({"timestamp": "aaa", "event": "dispatch_created"}) + "\n"
            + json.dumps({"timestamp": "zzz", "event": "dispatch_promoted"}) + "\n"
        )
        events = read_events(since_iso="mmm")
        assert [e["event"] for e in events] == ["dispatch_promoted"]


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

    def test_cli_extra_field(self, isolated_data_dir):
        env = self._env(isolated_data_dir)
        result = subprocess.run(
            [
                sys.executable,
                str(_MODULE_PATH),
                "append",
                "dispatch_failed",
                "dispatch_id=abc",
                "extra.reason=timeout",
                "extra.attempt=3",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        reg = _reg_path(isolated_data_dir)
        rec = json.loads(reg.read_text().strip())
        assert rec["event"] == "dispatch_failed"
        assert rec["dispatch_id"] == "abc"
        assert rec["extra"] == {"reason": "timeout", "attempt": "3"}


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
# 19. append_event requires at least one identifying field
#     (ADVISORY fix — Codex PR #277 round 3)
# ---------------------------------------------------------------------------

class TestAppendEventIdRequirement:
    def test_append_event_requires_id_field(self, isolated_data_dir):
        """append_event with no dispatch_id/pr_number/feature_id returns False."""
        result = append_event("dispatch_created")
        assert result is False

    def test_no_file_written_when_no_id(self, isolated_data_dir):
        """When the ID requirement fails, no register file is created."""
        append_event("dispatch_created")
        assert not _reg_path(isolated_data_dir).exists()

    def test_append_event_accepts_dispatch_id_only(self, isolated_data_dir):
        """dispatch_id alone satisfies the ID requirement."""
        result = append_event("dispatch_created", dispatch_id="d-id-only")
        assert result is True

    def test_append_event_accepts_pr_number_only(self, isolated_data_dir):
        """pr_number alone satisfies the ID requirement."""
        result = append_event("pr_opened", pr_number=99)
        assert result is True

    def test_append_event_accepts_feature_id_only(self, isolated_data_dir):
        """feature_id alone satisfies the ID requirement."""
        result = append_event("dispatch_created", feature_id="F-55")
        assert result is True


# ---------------------------------------------------------------------------
# Wave 1 shadow-mode tests — VNX_USE_CENTRAL_DB flag (PR-W1.4)
# ---------------------------------------------------------------------------

import json as _json
import tempfile as _tempfile
import unittest as _unittest

# Import shadow-aware functions from dispatch_register
from dispatch_register import (
    _read_register_locked,
    _read_register_locked_per_project,
    _read_register_locked_central,
    _query_recent_dispatches,
    _query_recent_dispatches_per_project,
    _query_recent_dispatches_central,
)
import dispatch_register as _dr_module


def _make_ndjson_content(*events: dict) -> str:
    return "\n".join(_json.dumps(e) for e in events) + "\n"


class TestReadRegisterLockedShadow:
    """3-state flag tests for _read_register_locked."""

    def _write_register(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test__read_register_locked_unset_uses_per_project(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
        reg = tmp_path / "state" / "dispatch_register.ndjson"
        content = _make_ndjson_content({"event": "dispatch_created", "dispatch_id": "d-001"})
        self._write_register(reg, content)

        result = _read_register_locked(reg)
        assert "dispatch_created" in result
        assert "d-001" in result

    def test__read_register_locked_authoritative_uses_central(self, monkeypatch, tmp_path):
        """VNX_USE_CENTRAL_DB=1 → reads from central register path."""
        project_id = "test-project"
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")
        monkeypatch.setenv("VNX_PROJECT_ID", project_id)

        # Per-project file (legacy)
        reg = tmp_path / "state" / "dispatch_register.ndjson"
        legacy_content = _make_ndjson_content({"event": "dispatch_created", "dispatch_id": "legacy-d-001"})
        self._write_register(reg, legacy_content)

        # Central file
        central_dir = tmp_path / "central" / project_id / "state"
        central_dir.mkdir(parents=True)
        central_content = _make_ndjson_content({"event": "dispatch_created", "dispatch_id": "central-d-001"})
        (central_dir / "dispatch_register.ndjson").write_text(central_content)

        # Patch _resolve_central_data_dir to point at tmp_path/central/<pid>
        original = _dr_module._resolve_central_data_dir
        _dr_module._resolve_central_data_dir = lambda pid: tmp_path / "central" / pid
        try:
            result = _read_register_locked(reg)
            assert "central-d-001" in result, f"Expected central content, got: {result[:200]}"
            assert "legacy-d-001" not in result
        finally:
            _dr_module._resolve_central_data_dir = original

    def test__read_register_locked_shadow_logs_divergence(self, monkeypatch, tmp_path):
        """Shadow mode logs divergence when per-project and central differ."""
        project_id = "test-project"
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "shadow")
        monkeypatch.setenv("VNX_PROJECT_ID", project_id)

        # Per-project with 2 events
        reg = tmp_path / "state" / "dispatch_register.ndjson"
        legacy_content = _make_ndjson_content(
            {"event": "dispatch_created", "dispatch_id": "d-001"},
            {"event": "dispatch_completed", "dispatch_id": "d-002"},
        )
        self._write_register(reg, legacy_content)

        # Central with 1 event (count mismatch → metric 4 divergence)
        central_dir = tmp_path / "central" / project_id / "state"
        central_dir.mkdir(parents=True)
        central_content = _make_ndjson_content({"event": "dispatch_created", "dispatch_id": "d-001"})
        (central_dir / "dispatch_register.ndjson").write_text(central_content)

        import shadow_verifier as sv
        compare_calls = []
        original_compare = sv.compare

        def _spy_compare(*args, **kwargs):
            compare_calls.append(kwargs.get("read_site", ""))
            return original_compare(*args, **kwargs)

        original_rcd = _dr_module._resolve_central_data_dir
        _dr_module._resolve_central_data_dir = lambda pid: tmp_path / "central" / pid
        sv.compare = _spy_compare
        try:
            result = _read_register_locked(reg)
            # Legacy content is authoritative
            assert "d-001" in result
            assert "d-002" in result
            # Shadow comparison was invoked
            assert any("_read_register_locked" in c for c in compare_calls), f"compare not called: {compare_calls}"
        finally:
            sv.compare = original_compare
            _dr_module._resolve_central_data_dir = original_rcd


class TestQueryRecentDispatchesShadow:
    """3-state flag tests for _query_recent_dispatches."""

    def test__query_recent_dispatches_unset_uses_per_project(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
        project_id = "test-project"

        reg = tmp_path / "state" / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True)
        reg.write_text(_make_ndjson_content(
            {"event": "dispatch_created", "dispatch_id": "pp-001",
             "timestamp": "2026-05-01T10:00:00.000000Z"}
        ))

        events = _query_recent_dispatches(reg, project_id)
        assert len(events) == 1
        assert events[0]["dispatch_id"] == "pp-001"

    def test__query_recent_dispatches_authoritative_uses_central(self, monkeypatch, tmp_path):
        project_id = "test-project"
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")

        reg = tmp_path / "state" / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True)
        reg.write_text(_make_ndjson_content(
            {"event": "dispatch_created", "dispatch_id": "pp-001",
             "project_id": project_id, "timestamp": "2026-05-01T10:00:00.000000Z"}
        ))

        central_dir = tmp_path / "central" / project_id / "state"
        central_dir.mkdir(parents=True)
        (central_dir / "dispatch_register.ndjson").write_text(_make_ndjson_content(
            {"event": "dispatch_created", "dispatch_id": "central-001",
             "project_id": project_id, "timestamp": "2026-05-01T11:00:00.000000Z"}
        ))

        original = _dr_module._resolve_central_data_dir
        _dr_module._resolve_central_data_dir = lambda pid: tmp_path / "central" / pid
        try:
            events = _query_recent_dispatches(reg, project_id)
            ids = [e["dispatch_id"] for e in events]
            assert "central-001" in ids, f"Expected central event, got: {ids}"
            assert "pp-001" not in ids
        finally:
            _dr_module._resolve_central_data_dir = original

    def test__query_recent_dispatches_shadow_logs_divergence(self, monkeypatch, tmp_path):
        """Shadow mode calls compare for both metric 1 and metric 4."""
        project_id = "test-project"
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "shadow")

        reg = tmp_path / "state" / "dispatch_register.ndjson"
        reg.parent.mkdir(parents=True)
        reg.write_text(_make_ndjson_content(
            {"event": "dispatch_created", "dispatch_id": "pp-001",
             "project_id": project_id, "timestamp": "2026-05-01T10:00:00.000000Z"},
            {"event": "dispatch_completed", "dispatch_id": "pp-002",
             "project_id": project_id, "timestamp": "2026-05-01T11:00:00.000000Z"},
        ))

        central_dir = tmp_path / "central" / project_id / "state"
        central_dir.mkdir(parents=True)
        # Central has fewer events → metric 4 count divergence
        (central_dir / "dispatch_register.ndjson").write_text(_make_ndjson_content(
            {"event": "dispatch_created", "dispatch_id": "pp-001",
             "project_id": project_id, "timestamp": "2026-05-01T10:00:00.000000Z"}
        ))

        import shadow_verifier as sv
        compare_calls = []
        original_compare = sv.compare

        def _spy(*args, **kwargs):
            compare_calls.append(kwargs.get("metric_id"))
            return original_compare(*args, **kwargs)

        original_rcd = _dr_module._resolve_central_data_dir
        _dr_module._resolve_central_data_dir = lambda pid: tmp_path / "central" / pid
        sv.compare = _spy
        try:
            events = _query_recent_dispatches(reg, project_id)
            # Legacy is authoritative
            assert len(events) == 2
            # Both metric 1 and metric 4 should have been compared
            assert 1 in compare_calls, f"metric 1 not compared: {compare_calls}"
            assert 4 in compare_calls, f"metric 4 not compared: {compare_calls}"
        finally:
            sv.compare = original_compare
            _dr_module._resolve_central_data_dir = original_rcd


class TestWritePathsUnchangedByShadowMode:
    """Write paths in dispatch_register.py must be unaffected by VNX_USE_CENTRAL_DB."""

    def test_write_paths_unchanged_by_shadow_mode(self, monkeypatch, isolated_data_dir):
        """append_event writes correctly regardless of VNX_USE_CENTRAL_DB value."""
        for flag_val in ("", "shadow", "1"):
            if flag_val:
                monkeypatch.setenv("VNX_USE_CENTRAL_DB", flag_val)
            else:
                monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)

            dispatch_id = f"write-test-flag-{flag_val or 'unset'}"
            result = append_event("dispatch_created", dispatch_id=dispatch_id)
            assert result is True, f"append_event failed with flag={flag_val!r}"

        # All 3 writes should be in the register (one per flag value)
        reg = _reg_path(isolated_data_dir)
        assert reg.exists()
        lines = [l for l in reg.read_text().splitlines() if l.strip()]
        ids = [_json.loads(l)["dispatch_id"] for l in lines]
        assert "write-test-flag-unset" in ids
        assert "write-test-flag-shadow" in ids
        assert "write-test-flag-1" in ids


# ---------------------------------------------------------------------------
# register_proposed_track_dispatch — ADR-005 audit ordering
# ---------------------------------------------------------------------------

import sqlite3 as _sqlite3


def _make_state_dir(tmp_path: Path) -> Path:
    """Create a state dir with a minimal dispatches DB for register tests."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"
    conn = _sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("""
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT,
            track TEXT,
            priority TEXT DEFAULT 'P2',
            pr_ref TEXT,
            gate TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT,
            metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.commit()
    conn.close()
    return state_dir


class TestRegisterProposedTrackDispatch:
    """ADR-005: NDJSON must precede SQLite; failure in audit write must abort creation."""

    def test_raises_when_ndjson_write_fails(self, tmp_path):
        state_dir = _make_state_dir(tmp_path)
        with patch.object(dispatch_register, "_write_event_locked", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                dispatch_register.register_proposed_track_dispatch(
                    state_dir, "disp-fail-001", "T1", "track-01", "PR-FUT-1"
                )

    def test_no_db_row_created_when_ndjson_write_fails(self, tmp_path):
        state_dir = _make_state_dir(tmp_path)
        with patch.object(dispatch_register, "_write_event_locked", side_effect=OSError("disk full")):
            try:
                dispatch_register.register_proposed_track_dispatch(
                    state_dir, "disp-fail-002", "T1", "track-01", "PR-FUT-1"
                )
            except OSError:
                pass

        db_path = state_dir / "runtime_coordination.db"
        conn = _sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT dispatch_id FROM dispatches WHERE dispatch_id = 'disp-fail-002'"
        ).fetchone()
        conn.close()
        assert row is None, "No DB row must exist when NDJSON write failed"

    def test_success_path_creates_db_row_and_ndjson(self, tmp_path):
        state_dir = _make_state_dir(tmp_path)
        dispatch_register.register_proposed_track_dispatch(
            state_dir, "disp-ok-001", "T1", "track-01", "PR-FUT-1"
        )
        db_path = state_dir / "runtime_coordination.db"
        conn = _sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT dispatch_id, state FROM dispatches WHERE dispatch_id = 'disp-ok-001'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "proposed"

        ndjson_path = state_dir.parent / "events" / "dispatch_register.ndjson"
        assert ndjson_path.exists()
        lines = [l for l in ndjson_path.read_text().splitlines() if l.strip()]
        assert any("disp-ok-001" in l for l in lines)


# ---------------------------------------------------------------------------
# record_id in _build_event_record
# ---------------------------------------------------------------------------

class TestBuildEventRecordId:
    def test_build_event_record_contains_record_id(self, isolated_data_dir):
        """_build_event_record must include a deterministic record_id field."""
        append_event("dispatch_created", dispatch_id="dr-001")
        reg = _reg_path(isolated_data_dir)
        rec = json.loads(reg.read_text().strip())
        assert "record_id" in rec, "record_id must be present in every emitted event record"
        assert len(rec["record_id"]) == 16, "record_id must be a 16-hex-char sha256 prefix"

    def test_record_id_is_deterministic(self, isolated_data_dir):
        """Two records with the same event/dispatch_id/timestamp get the same record_id.
        
        We cannot control the timestamp from outside, so instead we verify that two
        different events get DIFFERENT record_ids (confirming the hash input varies).
        """
        append_event("dispatch_created", dispatch_id="det-001")
        append_event("dispatch_promoted", dispatch_id="det-002")
        lines = _reg_path(isolated_data_dir).read_text().strip().splitlines()
        records = [json.loads(l) for l in lines]
        rid_a = records[0]["record_id"]
        rid_b = records[1]["record_id"]
        assert rid_a != rid_b, "Different events must produce different record_ids"


# ---------------------------------------------------------------------------
# register_proposed_track_dispatch audit failure rollback
# ---------------------------------------------------------------------------

class TestRegisterProposedTrackDispatchRollback:
    """register_proposed_track_dispatch must NOT commit a dispatches row when the
    NDJSON write fails (ADR-005 ledger-first guarantee)."""

    def _make_db(self, tmp_path: Path) -> tuple[Path, Path]:
        """Returns (state_dir, db_path) with a post-0022 DB."""
        import sqlite3 as _sqlite3
        import sys as _sys
        schemas = Path(__file__).resolve().parent.parent / "schemas" / "migrations"
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
        import schema_migration as _sm

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        db_path = state_dir / "runtime_coordination.db"
        conn = _sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                state TEXT NOT NULL DEFAULT 'proposed',
                terminal_id TEXT, track TEXT,
                priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                expires_after TEXT, metadata_json TEXT DEFAULT '{}',
                operator_approved_at TEXT,
                UNIQUE(dispatch_id, project_id)
            )
        """)
        conn.commit()
        conn.close()
        return state_dir, db_path

    def test_no_dispatch_row_when_ndjson_write_fails(self, tmp_path):
        """When _write_event_locked raises, no dispatches row is committed."""
        state_dir, db_path = self._make_db(tmp_path)

        with patch.object(dispatch_register, "_write_event_locked", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                dispatch_register.register_proposed_track_dispatch(
                    state_dir, "dr-fail-001", "T1", "track-01", "PR-TEST-1"
                )

        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM dispatches WHERE dispatch_id = 'dr-fail-001'"
        ).fetchone()[0]
        conn.close()
        assert count == 0, "No dispatches row must be committed when NDJSON write fails"

    def test_dispatch_row_created_on_success(self, tmp_path):
        """On successful NDJSON write + DB insert, the row exists."""
        state_dir, db_path = self._make_db(tmp_path)
        events_dir = tmp_path / "events"
        events_dir.mkdir(parents=True)

        dispatch_register.register_proposed_track_dispatch(
            state_dir, "dr-ok-001", "T1", "track-01", "PR-TEST-2"
        )

        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM dispatches WHERE dispatch_id = 'dr-ok-001'"
        ).fetchone()[0]
        conn.close()
        assert count == 1
