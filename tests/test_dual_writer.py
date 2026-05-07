"""Tests for scripts/lib/dual_writer.py (Phase 6 P4 helper)."""

from __future__ import annotations

import json
import sys
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
