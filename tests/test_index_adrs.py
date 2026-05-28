"""tests/test_index_adrs.py — ADR indexer parsing + idempotency + NDJSON events (PR-INT-1).

Verifies:
- Correct parsing of adr_id, status, title, decision_summary, binding_rules
- Idempotency: rerun with same source_hash skips row (0 changes)
- NDJSON event emitted per ADR indexed
- Raises on event-write failure (ADR-005)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import schema_migration
from quality_db_init import _migrate_v19
from index_adrs import _parse_adr, index_adrs


_FIXTURE_ADR = """\
# ADR-007 — Multi-tenant `project_id` Stamping Pattern

**Status:** Accepted
**Date:** 2026-05-09

## Context

VNX began as a single-tenant system.

## Decision

**All multi-tenant tables carry a project_id column with composite UNIQUE/PK.**

Concrete rules:

- Every table declares project_id TEXT NOT NULL DEFAULT 'vnx-dev'
- UNIQUE constraints must be composite over project_id
- Importers must stamp project_id explicitly

## Consequences

### Accepted

- New tables must be project_id-stamped at design time
"""


def _setup_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "qi.db"
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None
    schema_migration.apply_if_below(conn, 19, _migrate_v19)
    conn.close()
    return db_path


def _write_adr(tmp_path: Path, filename: str, content: str) -> Path:
    adr_dir = tmp_path / "decisions"
    adr_dir.mkdir(exist_ok=True)
    p = adr_dir / filename
    p.write_text(content, encoding="utf-8")
    return adr_dir


class TestAdrParser:
    def test_adr_id_extracted(self, tmp_path):
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        adr = _parse_adr(adr_dir / "ADR-007-multitenant.md")
        assert adr["adr_id"] == "ADR-007"

    def test_status_extracted(self, tmp_path):
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        adr = _parse_adr(adr_dir / "ADR-007-multitenant.md")
        assert adr["status"] == "Accepted"

    def test_title_extracted(self, tmp_path):
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        adr = _parse_adr(adr_dir / "ADR-007-multitenant.md")
        assert "Multi-tenant" in adr["title"]

    def test_decision_summary_populated(self, tmp_path):
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        adr = _parse_adr(adr_dir / "ADR-007-multitenant.md")
        assert "project_id" in adr["decision_summary"]

    def test_binding_rules_json_list(self, tmp_path):
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        adr = _parse_adr(adr_dir / "ADR-007-multitenant.md")
        rules = json.loads(adr["binding_rules"])
        assert isinstance(rules, list)
        assert len(rules) >= 1
        assert any("project_id" in r for r in rules)

    def test_source_hash_16_chars(self, tmp_path):
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        adr = _parse_adr(adr_dir / "ADR-007-multitenant.md")
        assert len(adr["source_hash"]) == 16

    def test_project_id_stamped(self, tmp_path):
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        adr = _parse_adr(adr_dir / "ADR-007-multitenant.md", project_id="test-proj")
        assert adr["project_id"] == "test-proj"


class TestIndexAdrs:
    def test_indexes_adr_row(self, tmp_path):
        db_path = _setup_db(tmp_path)
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        events_file = tmp_path / "events" / "adr_index.ndjson"

        result = index_adrs(db_path, adr_dir, events_file)
        assert result["indexed"] == 1
        assert result["total"] == 1

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT adr_id FROM adrs WHERE adr_id='ADR-007'").fetchone()
        conn.close()
        assert row is not None

    def test_idempotent_rerun_skips(self, tmp_path):
        db_path = _setup_db(tmp_path)
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        events_file = tmp_path / "events" / "adr_index.ndjson"

        index_adrs(db_path, adr_dir, events_file)
        result2 = index_adrs(db_path, adr_dir, events_file)
        assert result2["indexed"] == 0
        assert result2["skipped"] == 1

    def test_ndjson_event_emitted(self, tmp_path):
        db_path = _setup_db(tmp_path)
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        events_file = tmp_path / "events" / "adr_index.ndjson"

        index_adrs(db_path, adr_dir, events_file)
        assert events_file.exists()
        lines = [l for l in events_file.read_text().splitlines() if l.strip()]
        assert len(lines) >= 1
        event = json.loads(lines[0])
        assert event["event_type"] == "adr_indexed"
        assert event["adr_id"] == "ADR-007"
        assert "record_id" in event

    def test_ndjson_event_has_record_id(self, tmp_path):
        db_path = _setup_db(tmp_path)
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        events_file = tmp_path / "events" / "adr_index.ndjson"

        index_adrs(db_path, adr_dir, events_file)
        lines = [l for l in events_file.read_text().splitlines() if l.strip()]
        event = json.loads(lines[0])
        # record_id must be a 64-char hex sha256
        assert len(event["record_id"]) == 64

    def test_raises_on_event_write_failure(self, tmp_path):
        db_path = _setup_db(tmp_path)
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        events_file = tmp_path / "events" / "adr_index.ndjson"

        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                index_adrs(db_path, adr_dir, events_file)

    def test_changed_content_reindexed(self, tmp_path):
        db_path = _setup_db(tmp_path)
        adr_dir = _write_adr(tmp_path, "ADR-007-multitenant.md", _FIXTURE_ADR)
        events_file = tmp_path / "events" / "adr_index.ndjson"

        index_adrs(db_path, adr_dir, events_file)

        # Modify the ADR
        (adr_dir / "ADR-007-multitenant.md").write_text(
            _FIXTURE_ADR + "\n\n## Additional notes\n\nUpdated content.\n",
            encoding="utf-8",
        )
        result2 = index_adrs(db_path, adr_dir, events_file)
        assert result2["indexed"] == 1
