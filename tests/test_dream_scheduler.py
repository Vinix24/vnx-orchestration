"""Tests for dream scheduler install/uninstall and consolidator preflight (GAP-7).

Coverage:
- test_preflight_missing_receipts: skip when receipts file absent
- test_preflight_empty_receipts: skip when receipts file is 0 bytes
- test_preflight_stale_receipts: skip when last receipt is too old
- test_preflight_fresh_receipts: pass when receipts are recent
- test_preflight_no_timestamp_field: pass (don't block on ambiguous data)
- test_consolidator_skips_on_preflight_failure: run_dream_cycle returns skipped=True
- test_consolidator_skips_on_empty_patterns: skips when all pattern tables empty
- test_scheduler_install_macos_writes_plist: plist is written atomically
- test_scheduler_install_linux_writes_cron: cron line inserted into crontab
- test_scheduler_uninstall_linux_removes_entry: cron line removed
- test_scheduler_install_linux_idempotent: second install replaces, not duplicates
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dream"))

import consolidator
import scheduler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DREAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_cycles (
    cycle_id          TEXT    NOT NULL,
    project_id        TEXT    NOT NULL DEFAULT 'vnx-dev',
    started_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at      TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','running','completed','failed','reviewed','rejected')),
    provider          TEXT    NOT NULL DEFAULT 'kimi',
    insights_input    INTEGER NOT NULL DEFAULT 0,
    merged_count      INTEGER NOT NULL DEFAULT 0,
    dropped_count     INTEGER NOT NULL DEFAULT 0,
    archived_count    INTEGER NOT NULL DEFAULT 0,
    flagged_count     INTEGER NOT NULL DEFAULT 0,
    operator_reviewed INTEGER NOT NULL DEFAULT 0,
    report_path       TEXT,
    PRIMARY KEY (cycle_id, project_id)
);
CREATE TABLE IF NOT EXISTS success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    title TEXT NOT NULL DEFAULT 'title',
    description TEXT NOT NULL DEFAULT 'desc',
    pattern_data TEXT NOT NULL DEFAULT '{}',
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS antipatterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    title TEXT NOT NULL DEFAULT 'title',
    description TEXT NOT NULL DEFAULT 'desc',
    pattern_data TEXT NOT NULL DEFAULT '{}',
    why_problematic TEXT NOT NULL DEFAULT 'bad',
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DREAM_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def _receipts_path(tmp_path: Path) -> Path:
    p = tmp_path / ".vnx-data" / "state" / "t0_receipts.ndjson"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write_receipt(path: Path, ts: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event_type": "receipt", "timestamp": ts.isoformat()}) + "\n")


# ---------------------------------------------------------------------------
# Preflight tests
# ---------------------------------------------------------------------------


class TestPreflightReceipts:
    def test_preflight_missing_receipts(self, tmp_path):
        """Missing receipts file → preflight fails."""
        path = tmp_path / "t0_receipts.ndjson"
        ok, reason = consolidator._preflight_receipts(path)
        assert not ok
        assert "missing" in reason

    def test_preflight_empty_receipts(self, tmp_path):
        """Zero-byte receipts file → preflight fails."""
        path = tmp_path / "t0_receipts.ndjson"
        path.write_text("", encoding="utf-8")
        ok, reason = consolidator._preflight_receipts(path)
        assert not ok
        assert "empty" in reason

    def test_preflight_stale_receipts(self, tmp_path):
        """Last receipt older than threshold → preflight fails."""
        path = tmp_path / "t0_receipts.ndjson"
        old_ts = datetime.now(timezone.utc) - timedelta(hours=73)
        _write_receipt(path, old_ts)
        ok, reason = consolidator._preflight_receipts(path, max_stale_hours=48)
        assert not ok
        assert "old" in reason

    def test_preflight_fresh_receipts(self, tmp_path):
        """Recent receipt → preflight passes."""
        path = tmp_path / "t0_receipts.ndjson"
        fresh_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        _write_receipt(path, fresh_ts)
        ok, reason = consolidator._preflight_receipts(path, max_stale_hours=48)
        assert ok
        assert reason == ""

    def test_preflight_no_timestamp_field(self, tmp_path):
        """Receipt without any known timestamp field → pass (don't block on ambiguous data)."""
        path = tmp_path / "t0_receipts.ndjson"
        path.write_text(
            json.dumps({"event_type": "receipt", "data": "no-ts"}) + "\n", encoding="utf-8"
        )
        ok, reason = consolidator._preflight_receipts(path)
        assert ok

    def test_preflight_multiple_receipts_uses_last(self, tmp_path):
        """Only the last receipt line's timestamp is checked for staleness."""
        path = tmp_path / "t0_receipts.ndjson"
        old_ts = datetime.now(timezone.utc) - timedelta(hours=100)
        fresh_ts = datetime.now(timezone.utc) - timedelta(hours=1)
        _write_receipt(path, old_ts)
        _write_receipt(path, fresh_ts)
        ok, _ = consolidator._preflight_receipts(path, max_stale_hours=48)
        assert ok


class TestConsolidatorPreflight:
    def test_skips_on_missing_receipts(self, tmp_path):
        """run_dream_cycle skips and returns skipped=True when receipts file absent."""
        db_path = _make_db(tmp_path)
        receipts = _receipts_path(tmp_path)
        # receipts file does NOT exist

        with (
            patch("consolidator.resolve_project_root", return_value=tmp_path),
            patch("consolidator._emit_dream_event") as mock_emit,
        ):
            result = consolidator.run_dream_cycle("vnx-dev", db_path)

        assert result["skipped"] is True
        assert result["incomplete_data"] is True
        assert "missing" in result["reason"]

        emitted_types = [c.args[0]["event_type"] for c in mock_emit.call_args_list]
        assert "dream_cycle_skipped" in emitted_types
        assert "dream_cycle_started" not in emitted_types

    def test_skips_on_empty_patterns(self, tmp_path):
        """run_dream_cycle skips when receipts OK but all pattern tables are empty."""
        db_path = _make_db(tmp_path)
        receipts = _receipts_path(tmp_path)
        _write_receipt(receipts, datetime.now(timezone.utc) - timedelta(hours=1))

        with (
            patch("consolidator.resolve_project_root", return_value=tmp_path),
            patch("consolidator._emit_dream_event") as mock_emit,
        ):
            result = consolidator.run_dream_cycle("vnx-dev", db_path)

        assert result["skipped"] is True
        assert result["incomplete_data"] is True
        assert "no patterns" in result["reason"]

    def test_proceeds_when_receipts_ok_and_patterns_exist(self, tmp_path):
        """run_dream_cycle proceeds past preflight when data is present."""
        db_path = _make_db(tmp_path)
        receipts = _receipts_path(tmp_path)
        _write_receipt(receipts, datetime.now(timezone.utc) - timedelta(hours=1))

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO success_patterns (project_id, title) VALUES (?,?)", ("vnx-dev", "p1")
        )
        conn.commit()
        conn.close()

        fake_consolidation = {
            "merged": [], "dropped": [], "archived": [], "flagged": [], "summary": "ok"
        }

        with (
            patch("consolidator.resolve_project_root", return_value=tmp_path),
            patch("consolidator._dispatch_kimi_consolidation", return_value=fake_consolidation),
        ):
            result = consolidator.run_dream_cycle("vnx-dev", db_path, dry_run=True)

        assert not result.get("skipped")
        assert result["input_count"] == 1


# ---------------------------------------------------------------------------
# Scheduler tests (Linux cron path — platform-safe)
# ---------------------------------------------------------------------------


class TestSchedulerLinux:
    def test_install_linux_writes_cron(self):
        """install_linux injects a cron line with the project-id and marker."""
        with (
            patch("scheduler.subprocess.run") as mock_run,
        ):
            # Simulate empty crontab
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            scheduler.install_linux("vnx-dev", "/usr/local/bin/vnx")

        install_call = mock_run.call_args_list[-1]
        new_crontab = install_call.kwargs.get("input") or install_call.args[1] if len(install_call.args) > 1 else install_call.kwargs.get("input", "")
        # Find the actual crontab write call
        write_call = [c for c in mock_run.call_args_list if c.args and c.args[0] == ["crontab", "-"]]
        assert write_call, "Expected crontab - call"
        written = write_call[-1].kwargs.get("input", "")
        assert "vnx-dev" in written
        assert scheduler._CRON_MARKER in written
        assert "0 3 * * *" in written

    def test_install_linux_idempotent(self):
        """Second install replaces existing entry, does not duplicate."""
        existing = f"0 3 * * * /usr/local/bin/vnx dream run --project-id old  {scheduler._CRON_MARKER}\n"
        with patch("scheduler.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=existing, stderr="")
            scheduler.install_linux("vnx-dev", "/usr/local/bin/vnx")

        write_call = [c for c in mock_run.call_args_list if c.args and c.args[0] == ["crontab", "-"]]
        written = write_call[-1].kwargs.get("input", "")
        marker_count = written.count(scheduler._CRON_MARKER)
        assert marker_count == 1, f"Expected exactly 1 cron marker, found {marker_count}"
        assert "vnx-dev" in written

    def test_uninstall_linux_removes_entry(self):
        """uninstall_linux removes the vnx-auto-dream cron line."""
        existing = (
            "0 5 * * * /usr/bin/something-else\n"
            f"0 3 * * * /usr/local/bin/vnx dream run --project-id vnx-dev  {scheduler._CRON_MARKER}\n"
        )
        with patch("scheduler.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=existing, stderr="")
            scheduler.uninstall_linux()

        write_call = [c for c in mock_run.call_args_list if c.args and c.args[0] == ["crontab", "-"]]
        written = write_call[-1].kwargs.get("input", "")
        assert scheduler._CRON_MARKER not in written
        assert "/usr/bin/something-else" in written

    def test_uninstall_linux_no_entry(self, capsys):
        """uninstall_linux does nothing when no entry is present."""
        existing = "0 5 * * * /usr/bin/something-else\n"
        with patch("scheduler.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=existing, stderr="")
            scheduler.uninstall_linux()

        write_call = [c for c in mock_run.call_args_list if c.args and c.args[0] == ["crontab", "-"]]
        assert not write_call, "Should not write crontab when no marker present"


class TestSchedulerMacOS:
    def test_install_macos_writes_plist(self, tmp_path):
        """install_macos writes the plist file atomically (via temp+replace)."""
        with (
            patch("scheduler._launchagents_dir", return_value=tmp_path),
            patch("scheduler.subprocess.run") as mock_run,
            patch("scheduler.resolve_project_root", return_value=tmp_path),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            scheduler.install_macos("vnx-dev", "/usr/local/bin/vnx", str(tmp_path), load=False)

        plist = tmp_path / scheduler._PLIST_NAME
        assert plist.exists(), "Plist should be written"
        content = plist.read_text(encoding="utf-8")
        assert "com.vnx.auto-dream" in content
        assert "vnx-dev" in content

    def test_install_macos_calls_launchctl_load(self, tmp_path):
        """install_macos calls launchctl load -w when load=True."""
        with (
            patch("scheduler._launchagents_dir", return_value=tmp_path),
            patch("scheduler.subprocess.run") as mock_run,
            patch("scheduler.resolve_project_root", return_value=tmp_path),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            scheduler.install_macos("vnx-dev", "/usr/local/bin/vnx", str(tmp_path), load=True)

        commands = [tuple(c.args[0]) for c in mock_run.call_args_list if c.args]
        load_calls = [c for c in commands if "load" in c and "launchctl" in c]
        assert load_calls, f"Expected launchctl load call, got: {commands}"

    def test_uninstall_macos_removes_plist(self, tmp_path):
        """uninstall_macos unloads and deletes the plist."""
        plist = tmp_path / scheduler._PLIST_NAME
        plist.write_text("<plist/>", encoding="utf-8")

        with (
            patch("scheduler._launchagents_dir", return_value=tmp_path),
            patch("scheduler.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            scheduler.uninstall_macos()

        assert not plist.exists(), "Plist should be removed after uninstall"
        commands = [tuple(c.args[0]) for c in mock_run.call_args_list if c.args]
        unload_calls = [c for c in commands if "unload" in c]
        assert unload_calls, "Expected launchctl unload call"

    def test_uninstall_macos_no_plist(self, tmp_path, capsys):
        """uninstall_macos prints informative message when plist not present."""
        with patch("scheduler._launchagents_dir", return_value=tmp_path):
            scheduler.uninstall_macos()

        out = capsys.readouterr().out
        assert "not installed" in out.lower() or "not found" in out.lower()
