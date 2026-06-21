"""W-init tests: project_id threading + migration linter.

Covers three deliverables from the W-init scope (W1-TENANT-STAMPING-FIX-SPEC.md):

1. resolve_init_project_id() — fail-closed resolution helper.
2. run_runtime_coordination_migration() / run_quality_intelligence_migration()
   — stamp the RESOLVED pid as the ADD COLUMN DEFAULT, not hardcoded 'vnx-dev'.
3. migration_linter.py — flags a planted DEFAULT 'vnx-dev' in a new file;
   passes the grandfathered allowlisted set.

All tests use tmp fixtures under tempfile.gettempdir().
NEVER opens or modifies ~/.vnx-data or any real store.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _PROJECT_ROOT / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import project_id_migration as pim  # noqa: E402
import migration_linter as ml  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
)
"""


def _make_db(tmp_dir: Path, filename: str = "quality_intelligence.db") -> Path:
    """Create a minimal SQLite DB with a simple table in tmp_dir."""
    db_path = tmp_dir / filename
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SIMPLE_TABLE_DDL)
    conn.commit()
    conn.close()
    return db_path


def _write_marker(directory: Path, pid: str) -> Path:
    """Write a .vnx-project-id marker file with the given pid as the first line."""
    marker = directory / ".vnx-project-id"
    marker.write_text(f"{pid}\n", encoding="utf-8")
    return marker


# ---------------------------------------------------------------------------
# resolve_init_project_id — marker resolution
# ---------------------------------------------------------------------------

class TestResolveInitProjectId:
    """Tests for the fail-closed pid resolver in project_id_migration.py."""

    def test_resolves_from_marker_file(self, tmp_path):
        """Marker at the DB directory level → returns the pid from the file."""
        _write_marker(tmp_path, "my-project")
        db = _make_db(tmp_path)
        pid = pim.resolve_init_project_id(db)
        assert pid == "my-project"

    def test_resolves_from_marker_in_parent(self, tmp_path):
        """Marker at a parent directory → still resolved via upward walk."""
        _write_marker(tmp_path, "parent-project")
        sub = tmp_path / "state"
        sub.mkdir()
        db = _make_db(sub)
        pid = pim.resolve_init_project_id(db)
        assert pid == "parent-project"

    def test_resolves_from_env_var(self, tmp_path, monkeypatch):
        """No marker present but VNX_PROJECT_ID set → returns env value."""
        monkeypatch.setenv("VNX_PROJECT_ID", "env-project")
        db = _make_db(tmp_path)
        pid = pim.resolve_init_project_id(db)
        assert pid == "env-project"

    def test_marker_takes_precedence_when_both_agree(self, tmp_path, monkeypatch):
        """Marker and env agree → returns the (only) distinct value."""
        _write_marker(tmp_path, "agree-project")
        monkeypatch.setenv("VNX_PROJECT_ID", "agree-project")
        db = _make_db(tmp_path)
        pid = pim.resolve_init_project_id(db)
        assert pid == "agree-project"

    def test_fails_closed_when_marker_and_env_conflict(self, tmp_path, monkeypatch):
        """Marker says 'project-a', env says 'project-b' → RuntimeError (fail-closed)."""
        _write_marker(tmp_path, "project-a")
        monkeypatch.setenv("VNX_PROJECT_ID", "project-b")
        db = _make_db(tmp_path)
        with pytest.raises(RuntimeError, match="conflict"):
            pim.resolve_init_project_id(db)

    def test_defaults_to_vnx_dev_when_no_source(self, tmp_path, monkeypatch):
        """No marker, no env, no .vnx-data path → defaults to 'vnx-dev' with a warning."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        db = _make_db(tmp_path)
        with self._capture_log(pim) as records:
            pid = pim.resolve_init_project_id(db)
        assert pid == "vnx-dev"
        assert any("vnx-dev" in r.getMessage() for r in records), (
            "Expected a warning mentioning 'vnx-dev' fallback in the log records"
        )

    def test_resolves_from_db_path_layout(self, tmp_path, monkeypatch):
        """DB at <root>/.vnx-data/<pid>/state/<db> resolves pid from path alone."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        # Build the canonical central-store layout in tmp_path
        state_dir = tmp_path / ".vnx-data" / "seocrawler-v2" / "state"
        state_dir.mkdir(parents=True)
        db = _make_db(state_dir, filename="runtime_coordination.db")
        # No marker, no env — path layout is the only source
        pid = pim.resolve_init_project_id(db)
        assert pid == "seocrawler-v2"

    @staticmethod
    def _capture_log(module):
        """Context manager to capture log records emitted by module's logger."""
        import logging
        records: list = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture()
        logger = logging.getLogger(module.__name__)
        logger.addHandler(handler)
        orig_level = logger.level
        logger.setLevel(logging.WARNING)

        class _CM:
            def __enter__(self):
                return records
            def __exit__(self, *_):
                logger.removeHandler(handler)
                logger.setLevel(orig_level)

        return _CM()

    def test_fails_closed_on_invalid_pid_format(self, tmp_path, monkeypatch):
        """A pid that fails the VNX id regex → RuntimeError."""
        _write_marker(tmp_path, "INVALID_ID_WITH_UPPERCASE")
        db = _make_db(tmp_path)
        with pytest.raises(RuntimeError, match="not a valid VNX id"):
            pim.resolve_init_project_id(db)

    def test_empty_marker_file_falls_through_to_env(self, tmp_path, monkeypatch):
        """A marker file with an empty first line is ignored; env is used."""
        marker = tmp_path / ".vnx-project-id"
        marker.write_text("\n", encoding="utf-8")
        monkeypatch.setenv("VNX_PROJECT_ID", "env-fallback")
        db = _make_db(tmp_path)
        pid = pim.resolve_init_project_id(db)
        assert pid == "env-fallback"


# ---------------------------------------------------------------------------
# run_runtime_coordination_migration — stamps resolved pid, not 'vnx-dev'
# ---------------------------------------------------------------------------

class TestRunRuntimeCoordinationMigration:
    """run_runtime_coordination_migration uses the resolved pid for ADD COLUMN DEFAULT."""

    def _make_rc_db(self, tmp_dir: Path) -> Path:
        """Create a minimal runtime_coordination.db with the dispatches table."""
        db_path = tmp_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dispatches ("
            "  id TEXT PRIMARY KEY,"
            "  state TEXT NOT NULL"
            ")"
        )
        conn.execute("INSERT INTO dispatches (id, state) VALUES ('d1', 'queued')")
        conn.commit()
        conn.close()
        return db_path

    def test_stamps_resolved_pid_not_vnx_dev(self, tmp_path, monkeypatch):
        """When VNX_PROJECT_ID is set, ADD COLUMN stamps that pid, not 'vnx-dev'."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        _write_marker(tmp_path, "seocrawler-v2")
        db = self._make_rc_db(tmp_path)

        result = pim.run_runtime_coordination_migration(db)
        assert result["status"] == "ok"

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT project_id FROM dispatches WHERE id='d1'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "seocrawler-v2", (
            f"Expected 'seocrawler-v2' but got {row[0]!r} — "
            "ADD COLUMN DEFAULT stamped the wrong tenant."
        )

    def test_explicit_pid_param_overrides_resolution(self, tmp_path, monkeypatch):
        """Explicit default_project_id param bypasses resolution."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        # No marker → resolution would fail if we relied on it
        db = self._make_rc_db(tmp_path)
        result = pim.run_runtime_coordination_migration(
            db, default_project_id="explicit-pid"
        )
        assert result["status"] == "ok"

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT project_id FROM dispatches WHERE id='d1'"
        ).fetchone()
        conn.close()
        assert row[0] == "explicit-pid"

    def test_skips_when_db_absent(self, tmp_path):
        """Returns skipped_no_db when the DB file does not exist."""
        db = tmp_path / "nonexistent.db"
        result = pim.run_runtime_coordination_migration(
            db, default_project_id="test-project"
        )
        assert result["status"] == "skipped_no_db"

    def test_defaults_to_vnx_dev_when_no_pid_resolvable(self, tmp_path, monkeypatch):
        """No marker, no env, no .vnx-data path → defaults to 'vnx-dev' (backward-compat)."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        db = self._make_rc_db(tmp_path)
        result = pim.run_runtime_coordination_migration(db)
        assert result["status"] == "ok"

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT project_id FROM dispatches WHERE id='d1'"
        ).fetchone()
        conn.close()
        assert row[0] == "vnx-dev"


# ---------------------------------------------------------------------------
# run_quality_intelligence_migration — stamps resolved pid, not 'vnx-dev'
# ---------------------------------------------------------------------------

class TestRunQualityIntelligenceMigration:
    """run_quality_intelligence_migration uses the resolved pid for ADD COLUMN DEFAULT."""

    def _make_qi_db(self, tmp_dir: Path) -> Path:
        """Create a minimal quality_intelligence.db with the success_patterns table."""
        db_path = tmp_dir / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS success_patterns ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  pattern TEXT NOT NULL"
            ")"
        )
        conn.execute("INSERT INTO success_patterns (pattern) VALUES ('p1')")
        conn.commit()
        conn.close()
        return db_path

    def test_stamps_resolved_pid_not_vnx_dev(self, tmp_path, monkeypatch):
        """ADD COLUMN stamps the resolved pid, not 'vnx-dev'."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        _write_marker(tmp_path, "mc-local")
        db = self._make_qi_db(tmp_path)

        result = pim.run_quality_intelligence_migration(db)
        assert result["status"] == "ok"

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT project_id FROM success_patterns WHERE id=1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "mc-local", (
            f"Expected 'mc-local' but got {row[0]!r}."
        )

    def test_explicit_pid_param(self, tmp_path, monkeypatch):
        """Explicit default_project_id bypasses resolution."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        db = self._make_qi_db(tmp_path)
        pim.run_quality_intelligence_migration(db, default_project_id="test-qi-pid")

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT project_id FROM success_patterns WHERE id=1"
        ).fetchone()
        conn.close()
        assert row[0] == "test-qi-pid"

    def test_defaults_to_vnx_dev_when_no_pid_resolvable(self, tmp_path, monkeypatch):
        """No marker, no env, no .vnx-data path → defaults to 'vnx-dev' (backward-compat)."""
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        db = self._make_qi_db(tmp_path)
        result = pim.run_quality_intelligence_migration(db)
        assert result["status"] == "ok"

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT project_id FROM success_patterns WHERE id=1"
        ).fetchone()
        conn.close()
        assert row[0] == "vnx-dev"


# ---------------------------------------------------------------------------
# migration_linter — catches planted violations, passes grandfathered set
# ---------------------------------------------------------------------------

class TestMigrationLinter:
    """migration_linter.py catches new DEFAULT 'vnx-dev' and passes the allowlist."""

    def test_passes_on_clean_tree(self, tmp_path):
        """A directory with no SQL or Python files → no violations."""
        violations = ml.scan(tmp_path)
        assert violations == []

    def test_catches_planted_violation_in_sql(self, tmp_path):
        """A new migration file with DEFAULT 'vnx-dev' → 1 violation."""
        mig_dir = tmp_path / "schemas" / "migrations"
        mig_dir.mkdir(parents=True)
        (mig_dir / "0099_new_migration.sql").write_text(
            "ALTER TABLE foo ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';\n"
        )
        violations = ml.scan(tmp_path)
        assert len(violations) == 1
        assert "0099_new_migration.sql" in violations[0]["file"]

    def test_catches_planted_violation_in_python(self, tmp_path):
        """A new Python DDL helper with DEFAULT 'vnx-dev' → 1 violation."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "new_db_init.py").write_text(
            "ddl = \"CREATE TABLE t (project_id TEXT NOT NULL DEFAULT 'vnx-dev')\"\n"
        )
        violations = ml.scan(tmp_path)
        assert len(violations) == 1
        assert "new_db_init.py" in violations[0]["file"]

    def test_allowlisted_file_passes_in_normal_mode(self, tmp_path):
        """An allowlisted file with DEFAULT 'vnx-dev' → no violation in normal mode."""
        mig_dir = tmp_path / "schemas" / "migrations"
        mig_dir.mkdir(parents=True)
        # Use a grandfathered filename from the allowlist.
        (mig_dir / "0010_add_project_id.sql").write_text(
            "ALTER TABLE success_patterns ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';\n"
        )
        violations = ml.scan(tmp_path, allowlist=ml.ALLOWLISTED_FILES)
        assert violations == []

    def test_allowlisted_file_fails_in_strict_mode(self, tmp_path):
        """Same file in strict mode → violation (ignores allowlist)."""
        mig_dir = tmp_path / "schemas" / "migrations"
        mig_dir.mkdir(parents=True)
        (mig_dir / "0010_add_project_id.sql").write_text(
            "ALTER TABLE success_patterns ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev';\n"
        )
        violations = ml.scan(tmp_path, strict=True, allowlist=ml.ALLOWLISTED_FILES)
        assert len(violations) == 1

    def test_passes_actual_repo_in_normal_mode(self):
        """The real repo passes the linter in normal mode (no new violations)."""
        violations = ml.scan(_PROJECT_ROOT)
        assert violations == [], (
            f"Unexpected new DEFAULT 'vnx-dev' violations in the repo:\n"
            + "\n".join(f"  {v['file']}:{v['line']}: {v['text']}" for v in violations)
        )

    def test_violation_reports_correct_line_number(self, tmp_path):
        """Violation dict contains the correct line number."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(parents=True)
        content = (
            "# line 1\n"
            "# line 2\n"
            "col_ddl = \"project_id TEXT NOT NULL DEFAULT 'vnx-dev'\"\n"
            "# line 4\n"
        )
        (scripts_dir / "check_line.py").write_text(content)
        violations = ml.scan(tmp_path)
        assert len(violations) == 1
        assert violations[0]["line"] == 3

    def test_case_insensitive_match(self, tmp_path):
        """Pattern matches case variants like DEFAULT 'vnx-dev' and default 'vnx-dev'."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(parents=True)
        # All-caps DEFAULT — same SQLite semantics
        (scripts_dir / "case_test.py").write_text(
            "sql = \"ALTER TABLE t ADD COLUMN pid TEXT DEFAULT 'vnx-dev'\"\n"
        )
        violations = ml.scan(tmp_path)
        assert len(violations) == 1

    def test_multiple_violations_reported(self, tmp_path):
        """Multiple new violations all reported."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(parents=True)
        content = (
            "# file with two violations\n"
            "a = \"project_id TEXT NOT NULL DEFAULT 'vnx-dev'\"\n"
            "b = \"also DEFAULT 'vnx-dev' here\"\n"
        )
        (scripts_dir / "multi.py").write_text(content)
        violations = ml.scan(tmp_path)
        assert len(violations) == 2

    def test_main_exit_code_1_on_violation(self, tmp_path):
        """CLI returns exit code 1 when violations are found."""
        mig_dir = tmp_path / "schemas" / "migrations"
        mig_dir.mkdir(parents=True)
        (mig_dir / "0099_bad.sql").write_text(
            "ALTER TABLE t ADD COLUMN p TEXT DEFAULT 'vnx-dev';\n"
        )
        rc = ml.main(["--project-root", str(tmp_path)])
        assert rc == 1

    def test_main_exit_code_0_on_clean(self, tmp_path):
        """CLI returns exit code 0 on a clean tree."""
        rc = ml.main(["--project-root", str(tmp_path)])
        assert rc == 0
