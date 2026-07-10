#!/usr/bin/env python3
"""Tests for scripts/lib/store_integrity.py — the advisory pre-migration FK/integrity
check (Tier F, task #30). Reproduces the mission-control dangling-FK class in a fixture."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = str(REPO_ROOT / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from store_integrity import (  # noqa: E402
    StoreIntegrityError,
    check_store_integrity,
    preflight_or_report,
    strict_fk_enabled,
)


def _make_clean_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tracks (track_id TEXT PRIMARY KEY);
        CREATE TABLE deps (
            id INTEGER PRIMARY KEY,
            from_track TEXT REFERENCES tracks(track_id)
        );
        INSERT INTO tracks VALUES ('t1');
        INSERT INTO deps (from_track) VALUES ('t1');
        """
    )
    conn.commit()
    conn.close()


def _make_dangling_fk_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    # FK enforcement OFF so we can plant a dangling edge (the mission-control class).
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE tracks (track_id TEXT PRIMARY KEY);
        CREATE TABLE deps (
            id INTEGER PRIMARY KEY,
            from_track TEXT REFERENCES tracks(track_id)
        );
        INSERT INTO tracks VALUES ('t1');
        INSERT INTO deps (from_track) VALUES ('ghost-track');
        """
    )
    conn.commit()
    conn.close()


class TestCheckStoreIntegrity:
    def test_clean_db_is_ok(self, tmp_path):
        db = tmp_path / "runtime_coordination.db"
        _make_clean_db(db)
        report = check_store_integrity(db)
        assert report.ok
        assert report.fk_violations == []
        assert report.integrity_errors == []

    def test_dangling_fk_detected(self, tmp_path):
        db = tmp_path / "runtime_coordination.db"
        _make_dangling_fk_db(db)
        report = check_store_integrity(db)
        assert not report.ok
        assert len(report.fk_violations) == 1
        assert any("deps" in str(v[0]) for v in report.fk_violations)

    def test_missing_db_reports_ok(self, tmp_path):
        report = check_store_integrity(tmp_path / "nope.db")
        assert report.ok


class TestPreflightAdvisoryVsStrict:
    def test_advisory_does_not_raise_on_violation(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("VNX_MIGRATE_STRICT_FK", raising=False)
        db = tmp_path / "runtime_coordination.db"
        _make_dangling_fk_db(db)
        report = preflight_or_report(db, label="mission-control")
        assert not report.ok
        err = capsys.readouterr().err
        assert "dangling FK" in err
        assert "advisory only" in err

    def test_strict_raises_on_violation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_MIGRATE_STRICT_FK", "1")
        db = tmp_path / "runtime_coordination.db"
        _make_dangling_fk_db(db)
        with pytest.raises(StoreIntegrityError):
            preflight_or_report(db, label="mission-control")

    def test_clean_db_silent_and_no_raise_even_in_strict(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("VNX_MIGRATE_STRICT_FK", "1")
        db = tmp_path / "runtime_coordination.db"
        _make_clean_db(db)
        report = preflight_or_report(db)
        assert report.ok
        assert capsys.readouterr().err == ""


def test_strict_fk_enabled_flag():
    assert strict_fk_enabled({"VNX_MIGRATE_STRICT_FK": "1"}) is True
    assert strict_fk_enabled({"VNX_MIGRATE_STRICT_FK": "0"}) is False
    assert strict_fk_enabled({}) is False
