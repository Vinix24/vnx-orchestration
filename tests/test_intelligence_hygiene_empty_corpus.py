"""Q7: daily quality checks accept an empty corpus with the production DDL."""

import logging
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from gather_intelligence import T0IntelligenceGatherer
from intelligence_hygiene import HygieneRunner


@pytest.fixture
def runner(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "quality_intelligence.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript((ROOT / "schemas/quality_intelligence.sql").read_text())
        assert conn.execute("SELECT COUNT(*) FROM code_snippets").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM snippet_metadata").fetchone()[0] == 0
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_HOME", str(ROOT))
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(ROOT))
    gatherer = T0IntelligenceGatherer()
    try:
        assert gatherer.quality_db_path == db_path
        assert gatherer.quality_db is not None
        yield HygieneRunner(gatherer, ROOT, ROOT, False, state_dir.__truediv__)
    finally:
        if gatherer.quality_db is not None:
            gatherer.quality_db.close()
        if gatherer.tag_engine is not None and gatherer.tag_engine.db is not None:
            gatherer.tag_engine.db.close()


def test_empty_corpus_logs_zero_patterns_without_error(runner, caplog):
    with caplog.at_level(logging.INFO, logger="intelligence_hygiene"):
        runner._verify_pattern_quality()

    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert caplog.record_tuples == [
        ("intelligence_hygiene", logging.INFO, "Pattern quality: 0 patterns")
    ]


def test_three_snippets_keep_existing_quality_summary(runner, caplog):
    runner.gatherer.quality_db.executemany(
        "INSERT INTO code_snippets (quality_score) VALUES (?)",
        [(80,), (85,), (90,)],
    )
    with caplog.at_level(logging.INFO, logger="intelligence_hygiene"):
        runner._verify_pattern_quality()

    assert caplog.record_tuples == [
        (
            "intelligence_hygiene",
            logging.INFO,
            "Pattern quality: 3 total, 85.0 avg quality, 2 high quality (≥85)",
        )
    ]


def test_missing_corpus_table_still_logs_error(runner, caplog):
    runner.gatherer.quality_db.execute("DROP TABLE code_snippets")
    with caplog.at_level(logging.INFO, logger="intelligence_hygiene"):
        runner._verify_pattern_quality()

    assert caplog.record_tuples == [
        (
            "intelligence_hygiene",
            logging.ERROR,
            "Pattern quality check failed: no such table: code_snippets",
        )
    ]
