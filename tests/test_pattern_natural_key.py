#!/usr/bin/env python3
"""D3c: natural key (project_id, pattern_type, title) on success_patterns / antipatterns.

Covers, against tmp DBs only:
  (a/f) every writer folds a second write of the same pattern into one row
  (e)   a NULL/empty project_id or title is refused; blank rows are reported, never deleted
  (b)   the migration merges duplicates with the sum/min/max/union/weighted rules
  (c)   references to a merged id point at the survivor; zero dangling ids
  (d)   a second --apply changes nothing
  and   a fresh store gets the natural-key index; a store with duplicates is left alone

The migration is exercised through its CLI (scripts/migrate_pattern_natural_key.py)
the way the operator runs it. No test starts a provider or claude process and no
test touches a real ~/.vnx-data store.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

MIGRATE_CLI = SCRIPTS / "migrate_pattern_natural_key.py"
SCHEMA_FILE = REPO_ROOT / "schemas" / "quality_intelligence.sql"
PID = "vnx-dev"
OTHER = "other-proj"


@pytest.fixture(autouse=True)
def _tenant(monkeypatch):
    # Writers stamp project_id fail-closed (ADR-007); a tmp DB path carries no
    # tenant, so VNX_PROJECT_ID is the single source.
    monkeypatch.setenv("VNX_PROJECT_ID", PID)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _pattern_tables(conn: sqlite3.Connection, project_col: str = "project_id TEXT NOT NULL") -> None:
    """The live column set of both pattern tables (measured 2026-09-28)."""
    conn.executescript(f"""
        CREATE TABLE success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
            description TEXT NOT NULL, pattern_data TEXT NOT NULL,
            code_example TEXT, prerequisites TEXT, outcomes TEXT,
            usage_count INTEGER DEFAULT 0, avg_completion_time INTEGER,
            confidence_score REAL DEFAULT 0.0,
            source_dispatch_ids TEXT, source_receipts TEXT,
            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP, last_used DATETIME,
            valid_from DATETIME, valid_until DATETIME, invalidation_reason TEXT,
            tags TEXT, {project_col}
        );
        CREATE TABLE antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL,
            description TEXT NOT NULL, pattern_data TEXT NOT NULL,
            problem_example TEXT, why_problematic TEXT NOT NULL,
            better_alternative TEXT, occurrence_count INTEGER DEFAULT 1,
            avg_resolution_time INTEGER, severity TEXT DEFAULT 'medium',
            source_dispatch_ids TEXT,
            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP, last_seen DATETIME,
            valid_from DATETIME, valid_until DATETIME, invalidation_reason TEXT,
            tags TEXT, {project_col}
        );
    """)


def _mem_db(project_col: str = "project_id TEXT NOT NULL") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _pattern_tables(conn, project_col)
    return conn


def _count(conn: sqlite3.Connection, table: str, where: str = "1=1", args: tuple = ()) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def _seed_sp(conn, *, project_id=PID, title, category="", usage=1, pattern_type="approach",
             pattern_data='{"source": "seed"}', first="2026-01-01T00:00:00",
             last="2026-01-01T00:00:00", sources="[]", conf=0.5, description="seed"):
    cur = conn.execute(
        "INSERT INTO success_patterns (project_id, pattern_type, category, title, description, "
        "pattern_data, usage_count, confidence_score, source_dispatch_ids, first_seen, last_used) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, pattern_type, category, title, description, pattern_data, usage, conf,
         sources, first, last),
    )
    return cur.lastrowid


def _seed_ap(conn, *, project_id=PID, title, category="", count=1, pattern_type="approach",
             pattern_data='{"source": "seed"}', first="2026-01-01T00:00:00",
             last="2026-01-01T00:00:00", sources="[]", severity="medium", description="seed"):
    cur = conn.execute(
        "INSERT INTO antipatterns (project_id, pattern_type, category, title, description, "
        "pattern_data, why_problematic, severity, occurrence_count, source_dispatch_ids, "
        "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, pattern_type, category, title, description, pattern_data, "why", severity,
         count, sources, first, last),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# (a/f) Writers: a second write of the same pattern is one row, counter summed
# ---------------------------------------------------------------------------

class TestExtractorWriters:
    def test_success_pattern_extractor_twice_is_one_row(self):
        from success_pattern_extractor import insert_filtered_success_pattern

        conn = _mem_db()
        for dispatch in ("d-1", "d-2"):
            insert_filtered_success_pattern(
                conn, title="Use atomic writes", description="tmp + os.replace",
                usage_count=1, source_dispatch_ids=json.dumps([dispatch]), project_id=PID,
            )
        rows = conn.execute("SELECT usage_count, source_dispatch_ids FROM success_patterns").fetchall()
        assert len(rows) == 1
        assert rows[0]["usage_count"] == 2
        assert json.loads(rows[0]["source_dispatch_ids"]) == ["d-1", "d-2"]

    def test_antipattern_extractor_twice_is_one_row(self):
        from antipattern_extractor import insert_filtered_antipattern

        conn = _mem_db()
        for dispatch in ("d-1", "d-2"):
            insert_filtered_antipattern(
                conn, title="Skipping gates before merge", description="regressions",
                occurrence_count=1, source_dispatch_ids=json.dumps([dispatch]), project_id=PID,
            )
        rows = conn.execute("SELECT occurrence_count, source_dispatch_ids FROM antipatterns").fetchall()
        assert len(rows) == 1
        assert rows[0]["occurrence_count"] == 2
        assert json.loads(rows[0]["source_dispatch_ids"]) == ["d-1", "d-2"]

    def test_same_title_other_project_stays_separate(self):
        from success_pattern_extractor import insert_filtered_success_pattern

        conn = _mem_db()
        for pid in (PID, OTHER):
            insert_filtered_success_pattern(conn, title="Same text", description="x", project_id=pid)
        assert _count(conn, "success_patterns") == 2


class TestIntelligencePersistWriter:
    def test_success_signal_folds_into_row_with_same_natural_key(self):
        """A row written by another writer (other category) has the same natural
        key; the gate_success signal must fold into it, not add a second row."""
        import intelligence_persist as ip

        conn = _mem_db()
        rid = _seed_sp(conn, title="gate lint passed cleanly", category="code", usage=3,
                       sources='["d-0"]')
        ip._upsert_success_pattern(conn, "gate lint passed cleanly", "", "d-9",
                                   "2026-09-28T10:00:00")
        rows = conn.execute("SELECT id, usage_count, source_dispatch_ids FROM success_patterns").fetchall()
        assert [r["id"] for r in rows] == [rid]
        assert rows[0]["usage_count"] == 4
        assert json.loads(rows[0]["source_dispatch_ids"]) == ["d-0", "d-9"]

    def test_failure_signal_folds_into_row_with_same_natural_key(self):
        import intelligence_persist as ip

        conn = _mem_db()
        rid = _seed_ap(conn, title="codex gate blocked on lint", category="code", count=2)
        ip._upsert_antipattern(conn, "gate_failure", "codex gate blocked on lint", "high",
                               "d-9", "lint", "2026-09-28T10:00:00")
        rows = conn.execute("SELECT id, occurrence_count FROM antipatterns").fetchall()
        assert [r["id"] for r in rows] == [rid]
        assert rows[0]["occurrence_count"] == 3


class TestConversationBridgeWriter:
    def _metrics_flags(self):
        from conversation_analyzer.models import SessionFlags, SessionMetrics
        return SessionMetrics(), SessionFlags(has_test_cycle=True)

    def _suggestions_table(self, conn):
        conn.execute(
            "CREATE TABLE improvement_suggestions (id INTEGER PRIMARY KEY, category TEXT, "
            "component TEXT, suggested_improvement TEXT, priority TEXT, status TEXT, "
            "acted_on INTEGER DEFAULT 0, project_id TEXT)"
        )

    def test_repeat_is_one_row_per_project_and_other_tenant_untouched(self):
        from conversation_analyzer import intelligence_bridge

        conn = _mem_db()
        self._suggestions_table(conn)
        other_id = _seed_sp(conn, project_id=OTHER, title="Test-driven workflow detected",
                            usage=1, pattern_data='{"source": "session_analysis"}')
        metrics, flags = self._metrics_flags()
        intelligence_bridge.bridge_session_to_intelligence(conn, metrics, flags)
        intelligence_bridge.bridge_session_to_intelligence(conn, metrics, flags)

        other = conn.execute("SELECT usage_count FROM success_patterns WHERE id = ?",
                             (other_id,)).fetchone()
        assert other["usage_count"] == 1, "another tenant's row must not absorb this project's count"
        mine = conn.execute(
            "SELECT usage_count FROM success_patterns WHERE project_id = ? "
            "AND title = 'Test-driven workflow detected'", (PID,)
        ).fetchall()
        assert len(mine) == 1
        assert mine[0]["usage_count"] == 2


class TestMemoryConsolidatorWriter:
    def _consolidator(self, db_path: Path):
        import memory_consolidator
        mc = memory_consolidator.MemoryConsolidator.__new__(memory_consolidator.MemoryConsolidator)
        mc.db_path = db_path
        return mc

    def test_repeat_folds_per_project_and_other_tenant_untouched(self, tmp_path):
        from memory_consolidator import CATEGORY, ExtractedPattern

        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _pattern_tables(conn)
        other_id = _seed_sp(conn, project_id=OTHER, title="T1 dispatches: 85% success rate",
                            category=CATEGORY, usage=5)
        conn.commit()

        mc = self._consolidator(db_path)
        p = ExtractedPattern(title="T1 dispatches: 85% success rate", description="d",
                             pattern_type="success", pattern_subtype="terminal_rate",
                             evidence_count=3)
        first, _ = mc._upsert_success_pattern(conn, p, "2026-09-28T10:00:00")
        second, _ = mc._upsert_success_pattern(conn, p, "2026-09-28T11:00:00")

        assert conn.execute("SELECT usage_count FROM success_patterns WHERE id = ?",
                            (other_id,)).fetchone()[0] == 5
        mine = conn.execute("SELECT usage_count FROM success_patterns WHERE project_id = ?",
                            (PID,)).fetchall()
        assert len(mine) == 1
        assert mine[0]["usage_count"] == 6
        assert (first, second) == ("inserted", "updated")

    def test_antipattern_repeat_folds_per_project_and_other_tenant_untouched(self, tmp_path):
        from memory_consolidator import CATEGORY, ExtractedPattern

        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _pattern_tables(conn)
        other_id = _seed_ap(conn, project_id=OTHER, title="T2 dispatches: 40% failure rate",
                            category=CATEGORY, count=7)
        conn.commit()
        mc = self._consolidator(db_path)
        p = ExtractedPattern(title="T2 dispatches: 40% failure rate", description="d",
                             pattern_type="antipattern", pattern_subtype="terminal_rate",
                             evidence_count=2)
        mc._upsert_antipattern(conn, p, "2026-09-28T10:00:00")
        mc._upsert_antipattern(conn, p, "2026-09-28T11:00:00")
        assert conn.execute("SELECT occurrence_count FROM antipatterns WHERE id = ?",
                            (other_id,)).fetchone()[0] == 7
        rows = conn.execute("SELECT occurrence_count FROM antipatterns WHERE project_id = ?",
                            (PID,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["occurrence_count"] == 4


class TestLearningLoopWriter:
    def _loop(self, conn, db_path, failures):
        import learning_loop as ll
        loop = ll.LearningLoop.__new__(ll.LearningLoop)
        loop.conn = conn
        loop.db_path = db_path
        loop.pattern_metrics = {
            "intel_sp_7": ll.PatternUsageMetric(
                pattern_id="intel_sp_7", pattern_title="Test Driven Development",
                pattern_hash="h", used_count=5, confidence=0.8,
            )
        }
        loop.extract_failure_patterns = lambda: failures
        return loop

    def test_rerun_keeps_one_row_per_project_and_other_tenant_untouched(self, tmp_path):
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        _pattern_tables(conn)
        other_sp = _seed_sp(conn, project_id=OTHER, title="Test Driven Development", usage=99,
                            pattern_data='{"source": "learning_loop"}')
        other_ap = _seed_ap(conn, project_id=OTHER, title="Recurring failure: boom", count=42,
                            pattern_data='{"source": "learning_loop"}')
        conn.commit()
        failures = [{"error": "boom", "terminal": "T1"}, {"error": "boom", "terminal": "T2"}]

        loop = self._loop(conn, db_path, failures)
        loop.persist_to_intelligence_db()
        loop.persist_to_intelligence_db()

        assert conn.execute("SELECT usage_count FROM success_patterns WHERE id = ?",
                            (other_sp,)).fetchone()[0] == 99
        assert conn.execute("SELECT occurrence_count FROM antipatterns WHERE id = ?",
                            (other_ap,)).fetchone()[0] == 42
        sp = conn.execute("SELECT usage_count FROM success_patterns WHERE project_id = ?",
                          (PID,)).fetchall()
        ap = conn.execute("SELECT occurrence_count FROM antipatterns WHERE project_id = ?",
                          (PID,)).fetchall()
        # Cumulative counters: a rerun must not add them twice.
        assert [r[0] for r in sp] == [5]
        assert [r[0] for r in ap] == [2]


class TestPatternExtractorWriter:
    AFF = [{"files": ["a.py", "b.py"], "count": 4, "co_occurrence": 0.5}]

    def test_rerun_keeps_id_and_leaves_other_tenant(self):
        import pattern_extractor as pe

        conn = _mem_db(project_col="project_id TEXT")
        other_id = _seed_sp(conn, project_id=OTHER, title="Files co-occur: a.py + b.py",
                            category="behavior_analysis", pattern_type="behavioral")
        conn.commit()

        pe._upsert_affinity_patterns(conn, self.AFF, ["d-1"])
        first = conn.execute("SELECT id FROM success_patterns WHERE project_id = ?", (PID,)).fetchall()
        pe._upsert_affinity_patterns(conn, self.AFF, ["d-2"])
        second = conn.execute("SELECT id, usage_count, source_dispatch_ids FROM success_patterns "
                              "WHERE project_id = ?", (PID,)).fetchall()

        assert _count(conn, "success_patterns", "id = ?", (other_id,)) == 1, \
            "the snapshot delete must stay inside its own project"
        assert len(second) == 1
        assert [r[0] for r in first] == [second[0]["id"]], "a rerun must keep the id (intel_sp_<id>)"
        assert second[0]["usage_count"] == 4
        assert json.loads(second[0]["source_dispatch_ids"]) == ["d-2"]


# ---------------------------------------------------------------------------
# (e) NULL / empty key parts are refused
# ---------------------------------------------------------------------------

class TestNullPolicy:
    @pytest.mark.parametrize("project_id,title", [(None, "Real title"), ("", "Real title"),
                                                  (PID, ""), (PID, "   ")])
    def test_success_writer_refuses_blank_key(self, project_id, title):
        from success_pattern_extractor import insert_filtered_success_pattern

        conn = _mem_db(project_col="project_id TEXT")
        with pytest.raises(ValueError):
            insert_filtered_success_pattern(conn, title=title, description="d", project_id=project_id)
        assert _count(conn, "success_patterns") == 0

    @pytest.mark.parametrize("project_id,title", [(None, "Real title"), ("", "Real title"),
                                                  (PID, "")])
    def test_antipattern_writer_refuses_blank_key(self, project_id, title):
        from antipattern_extractor import insert_filtered_antipattern

        conn = _mem_db(project_col="project_id TEXT")
        with pytest.raises(ValueError):
            insert_filtered_antipattern(conn, title=title, description="d", project_id=project_id)
        assert _count(conn, "antipatterns") == 0


# ---------------------------------------------------------------------------
# Migration (b, c, d) through the CLI, on a store built the production way
# ---------------------------------------------------------------------------

def _store(tmp_path: Path) -> Path:
    """Fresh QI store: bootstrap_qi_db, then the 0010 project_id runner. The
    natural-key index is dropped so the store looks like one from before D3c
    and can hold duplicates."""
    import project_id_migration
    import quality_db_init

    db_path = tmp_path / "quality_intelligence.db"
    assert quality_db_init.bootstrap_qi_db(db_path, SCHEMA_FILE)
    project_id_migration.run_quality_intelligence_migration(db_path, default_project_id=PID)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP INDEX IF EXISTS ux_success_patterns_natural_key")
    conn.execute("DROP INDEX IF EXISTS ux_antipatterns_natural_key")
    conn.commit()
    conn.close()
    return db_path


def _migrate(db_path: Path, tmp_path: Path, *, apply: bool, name: str = "report") -> tuple:
    report = tmp_path / f"{name}.json"
    cmd = [sys.executable, str(MIGRATE_CLI), "--db", str(db_path), "--report", str(report)]
    if apply:
        cmd.append("--apply")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    data = json.loads(report.read_text()) if report.exists() else None
    return proc, data


def _natural_key_indexed(conn: sqlite3.Connection, table: str) -> bool:
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if row[2]:
            cols = tuple(r[2] for r in conn.execute(f"PRAGMA index_info({row[1]})").fetchall())
            if cols == ("project_id", "pattern_type", "title"):
                return True
    return False


def _seed_duplicates(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    ids = {
        "sp": [
            _seed_sp(conn, title="Dup", usage=2, first="2026-03-01T00:00:00",
                     last="2026-03-05T00:00:00", sources='["a", "b"]', conf=0.5,
                     description="first"),
            _seed_sp(conn, title="Dup", usage=6, first="2026-02-01 00:00:00",
                     last="2026-03-02T00:00:00", sources='["b", "c"]', conf=1.0,
                     description="highest count"),
            _seed_sp(conn, title="Dup", usage=2, first="2026-03-10T00:00:00",
                     last="2026-04-01T00:00:00", sources='["d"]', conf=0.25,
                     description="third"),
        ],
        "sp_other": _seed_sp(conn, project_id=OTHER, title="Dup", usage=1),
        "ap": [
            _seed_ap(conn, title="DupAP", count=1, first="2026-05-01T00:00:00",
                     last="2026-05-02T00:00:00", sources='["x"]', severity="low"),
            _seed_ap(conn, title="DupAP", count=3, first="2026-04-01T00:00:00",
                     last="2026-06-01T00:00:00", sources='["y"]', severity="high"),
        ],
    }
    conn.commit()
    conn.close()
    return ids


class TestMigrationMerge:
    def test_dry_run_writes_nothing(self, tmp_path):
        db_path = _store(tmp_path)
        _seed_duplicates(db_path)
        proc, report = _migrate(db_path, tmp_path, apply=False)
        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "success_patterns", "title = 'Dup' AND project_id = ?", (PID,)) == 3
        assert not _natural_key_indexed(conn, "success_patterns")
        assert proc.returncode == 0, proc.stderr
        assert report["before"]["tables"]["success_patterns"]["duplicate_groups"] == 1
        assert report["before"]["tables"]["antipatterns"]["duplicate_groups"] == 1

    def test_apply_merges_with_the_rules(self, tmp_path):
        db_path = _store(tmp_path)
        ids = _seed_duplicates(db_path)
        proc, report = _migrate(db_path, tmp_path, apply=True)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        sp = conn.execute("SELECT * FROM success_patterns WHERE title = 'Dup' AND project_id = ?",
                          (PID,)).fetchall()
        assert len(sp) == 1
        row = sp[0]
        assert row["id"] == ids["sp"][0], "the lowest id survives"
        assert row["usage_count"] == 10
        assert row["first_seen"] == "2026-02-01 00:00:00"
        assert row["last_used"] == "2026-04-01T00:00:00"
        assert json.loads(row["source_dispatch_ids"]) == ["a", "b", "c", "d"]
        assert row["confidence_score"] == pytest.approx((0.5 * 2 + 1.0 * 6 + 0.25 * 2) / 10)
        assert row["description"] == "highest count"
        # Other tenant's same-title row is not part of the group.
        assert _count(conn, "success_patterns", "id = ?", (ids["sp_other"],)) == 1

        ap = conn.execute("SELECT * FROM antipatterns WHERE title = 'DupAP'").fetchall()
        assert len(ap) == 1
        assert ap[0]["id"] == ids["ap"][0]
        assert ap[0]["occurrence_count"] == 4
        assert ap[0]["first_seen"] == "2026-04-01T00:00:00"
        assert ap[0]["last_seen"] == "2026-06-01T00:00:00"
        assert json.loads(ap[0]["source_dispatch_ids"]) == ["x", "y"]
        assert ap[0]["severity"] == "high"

        assert _natural_key_indexed(conn, "success_patterns")
        assert _natural_key_indexed(conn, "antipatterns")
        with pytest.raises(sqlite3.IntegrityError):
            _seed_sp(conn, title="Dup")
        assert proc.returncode == 0, proc.stderr
        assert report["after"]["tables"]["success_patterns"]["duplicate_groups"] == 0

    def test_blank_key_rows_are_reported_not_deleted(self, tmp_path):
        db_path = _store(tmp_path)
        conn = sqlite3.connect(str(db_path))
        blank = _seed_sp(conn, title="")
        conn.commit()
        conn.close()
        proc, report = _migrate(db_path, tmp_path, apply=True)
        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "success_patterns", "id = ?", (blank,)) == 1
        assert proc.returncode == 0, proc.stderr
        assert report["before"]["tables"]["success_patterns"]["blank_key_rows"] == [blank]
        assert report["after"]["tables"]["success_patterns"]["blank_key_rows"] == [blank]


class TestMigrationRemap:
    def test_references_move_to_the_survivor(self, tmp_path):
        db_path = _store(tmp_path)
        ids = _seed_duplicates(db_path)
        survivor, dup = ids["sp"][0], ids["sp"][1]
        s_ref, d_ref = f"intel_sp_{survivor}", f"intel_sp_{dup}"
        conn = sqlite3.connect(str(db_path))
        conn.executemany(
            "INSERT INTO dispatch_pattern_offered (dispatch_id, pattern_id, pattern_title, "
            "offered_at, project_id, ab_arm) VALUES (?, ?, 'Dup', '2026-03-01', ?, 'treatment')",
            [("disp-1", d_ref, PID), ("disp-2", d_ref, PID), ("disp-2", s_ref, PID)],
        )
        conn.executemany(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, "
            "ignored_count, project_id) VALUES (?, 'Dup', 'h', ?, ?, ?)",
            [(s_ref, 2, 1, PID), (d_ref, 3, 4, PID)],
        )
        conn.executemany(
            "INSERT INTO pattern_injection_outcome (dispatch_id, pattern_id, used, project_id) "
            "VALUES (?, ?, ?, ?)",
            [("disp-1", d_ref, 1, PID), ("disp-2", d_ref, 1, PID), ("disp-2", s_ref, 0, PID)],
        )
        conn.execute(
            "INSERT INTO dream_pattern_archives (cycle_id, project_id, original_pattern_id, "
            "original_table, archived_reason) VALUES ('c1', ?, ?, 'success_patterns', 'stale_30d')",
            (PID, dup),
        )
        conn.execute(
            "UPDATE success_patterns SET pattern_data = ? WHERE id = ?",
            (json.dumps({"source": "learning_loop", "pattern_id": d_ref}), ids["sp"][2]),
        )
        conn.commit()
        conn.close()

        proc, report = _migrate(db_path, tmp_path, apply=True)

        conn = sqlite3.connect(str(db_path))
        for table in ("dispatch_pattern_offered", "pattern_usage", "pattern_injection_outcome"):
            assert _count(conn, table, "pattern_id = ?", (d_ref,)) == 0, table
        assert sorted(r[0] for r in conn.execute(
            "SELECT dispatch_id FROM dispatch_pattern_offered WHERE pattern_id = ?", (s_ref,))) == \
            ["disp-1", "disp-2"]
        usage = conn.execute("SELECT used_count, ignored_count FROM pattern_usage "
                             "WHERE pattern_id = ?", (s_ref,)).fetchall()
        assert usage == [(5, 5)]
        outcomes = dict(conn.execute("SELECT dispatch_id, used FROM pattern_injection_outcome "
                                     "WHERE pattern_id = ?", (s_ref,)).fetchall())
        assert outcomes == {"disp-1": 1, "disp-2": 1}
        assert conn.execute("SELECT original_pattern_id FROM dream_pattern_archives").fetchone()[0] \
            == survivor
        data = json.loads(conn.execute("SELECT pattern_data FROM success_patterns WHERE id = ?",
                                       (survivor,)).fetchone()[0])
        assert data.get("pattern_id") in (None, s_ref)
        assert proc.returncode == 0, proc.stderr
        assert report["after"]["dangling_total"] == 0


class TestMigrationIdempotent:
    def test_second_apply_changes_nothing(self, tmp_path):
        db_path = _store(tmp_path)
        _seed_duplicates(db_path)
        _migrate(db_path, tmp_path, apply=True, name="first")
        conn = sqlite3.connect(str(db_path))
        before = conn.execute("SELECT * FROM success_patterns ORDER BY id").fetchall()
        conn.close()

        proc, report = _migrate(db_path, tmp_path, apply=True, name="second")
        conn = sqlite3.connect(str(db_path))
        after = conn.execute("SELECT * FROM success_patterns ORDER BY id").fetchall()
        assert _count(conn, "success_patterns", "title = 'Dup' AND project_id = ?", (PID,)) == 1
        assert _count(conn, "antipatterns", "title = 'DupAP'") == 1
        assert after == before
        assert proc.returncode == 0, proc.stderr
        assert report["changes"] == 0
        assert report["merges"] == {"success_patterns": [], "antipatterns": []}


class TestFreshStoreGetsKey:
    def test_fresh_store_has_natural_key_index(self, tmp_path):
        import project_id_migration
        import quality_db_init

        db_path = tmp_path / "quality_intelligence.db"
        assert quality_db_init.bootstrap_qi_db(db_path, SCHEMA_FILE)
        project_id_migration.run_quality_intelligence_migration(db_path, default_project_id=PID)
        conn = sqlite3.connect(str(db_path))
        assert _natural_key_indexed(conn, "success_patterns")
        assert _natural_key_indexed(conn, "antipatterns")

    def test_store_with_duplicates_is_left_alone(self, tmp_path):
        """The bootstrap path never merges: a store holding duplicates keeps
        every row and gets no index until the operator runs --apply."""
        import project_id_migration

        db_path = _store(tmp_path)
        _seed_duplicates(db_path)
        project_id_migration.run_quality_intelligence_migration(db_path, default_project_id=PID)
        conn = sqlite3.connect(str(db_path))
        assert _count(conn, "success_patterns", "title = 'Dup' AND project_id = ?", (PID,)) == 3
        assert not _natural_key_indexed(conn, "success_patterns")
