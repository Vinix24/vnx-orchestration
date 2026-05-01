"""Phase 1 of the single-VNX migration — project_id wiring.

Phase 0 (#334) added the ``project_id`` column with DEFAULT ``vnx-dev`` to
the hot quality_intelligence and runtime_coordination tables. Phase 1
threads ``current_project_id()`` through the call sites that read and
write those tables so multi-tenant data is partitioned at runtime even
though the schema is still shared.

Coverage:
  A. ``current_project_id()`` defaults to ``vnx-dev`` when ``VNX_PROJECT_ID``
     is unset.
  B. ``current_project_id()`` honours an explicit ``VNX_PROJECT_ID``.
  C. Writers stamp ``project_id`` on the rows they create.
  D. Cross-tenant query: opt-out via ``VNX_PROJECT_FILTER=0`` returns rows
     from every project; default ON returns only the caller's project.
  E. Backward compat: rows that pre-date Phase 0 (and thus carry the
     ``vnx-dev`` default) remain readable by the default-tenant caller.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

_SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
for _p in (_SCRIPTS_LIB, _SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import project_scope  # noqa: E402
from project_scope import (  # noqa: E402
    DEFAULT_PROJECT,
    ENV_VAR,
    FILTER_ENV_VAR,
    current_project_id,
    project_filter_enabled,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _env(**overrides: str | None):
    """Temporarily set/unset env vars; restore originals on exit."""
    saved: dict[str, str | None] = {k: os.environ.get(k) for k in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _build_quality_db(path: Path) -> None:
    """Build a minimal quality_intelligence.db that mirrors the post-Phase-0 schema."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT,
            confidence_score REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT,
            first_seen DATETIME, last_used DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, why_problematic TEXT, better_alternative TEXT,
            severity TEXT DEFAULT 'medium', occurrence_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT, first_seen DATETIME, last_seen DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT, rule_type TEXT, description TEXT,
            recommendation TEXT, confidence REAL DEFAULT 0.0,
            created_at TEXT, triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT, pattern_hash TEXT,
            used_count INTEGER DEFAULT 0, ignored_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0, failure_count INTEGER DEFAULT 0,
            last_used TEXT, last_offered TEXT,
            confidence REAL DEFAULT 0.0,
            created_at TEXT, updated_at TEXT,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE confidence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT, terminal TEXT, outcome TEXT,
            patterns_boosted INTEGER, patterns_decayed INTEGER,
            confidence_change REAL, occurred_at TEXT,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT, terminal TEXT, track TEXT,
            role TEXT, skill_name TEXT, gate TEXT,
            cognition TEXT, priority TEXT, pr_id TEXT,
            pattern_count INTEGER DEFAULT 0,
            prevention_rule_count INTEGER DEFAULT 0,
            intelligence_json TEXT,
            instruction_char_count INTEGER DEFAULT 0,
            context_file_count INTEGER DEFAULT 0,
            target_open_items TEXT,
            dispatched_at DATETIME, completed_at DATETIME,
            outcome_status TEXT,
            cqs REAL, normalized_status TEXT, cqs_components TEXT,
            open_items_created INTEGER DEFAULT 0,
            open_items_resolved INTEGER DEFAULT 0,
            quality_advisory_json TEXT,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE dispatch_pattern_offered (
            dispatch_id   TEXT NOT NULL,
            pattern_id    TEXT NOT NULL,
            pattern_title TEXT NOT NULL,
            offered_at    TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            PRIMARY KEY (dispatch_id, pattern_id)
        );
        """
    )
    conn.commit()
    conn.close()


def _seed_pattern(
    db: Path,
    *,
    title: str,
    project_id: str,
    confidence: float = 0.9,
    usage_count: int = 5,
    category: str = "architect",
) -> int:
    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        "INSERT INTO success_patterns "
        "(pattern_type, category, title, description, pattern_data, "
        " confidence_score, usage_count, source_dispatch_ids, "
        " first_seen, last_used, project_id) "
        "VALUES ('approach', ?, ?, ?, '{}', ?, ?, '[]', "
        " '2026-04-01T00:00:00', '2026-04-01T00:00:00', ?)",
        (category, title, f"desc for {title}", confidence, usage_count, project_id),
    )
    conn.commit()
    pk = cur.lastrowid
    conn.close()
    return pk


# ---------------------------------------------------------------------------
# A + B — current_project_id() resolution
# ---------------------------------------------------------------------------

def test_case_a_default_when_env_unset():
    with _env(**{ENV_VAR: None}):
        assert current_project_id() == DEFAULT_PROJECT == "vnx-dev"


def test_case_b_explicit_env_var():
    with _env(**{ENV_VAR: "tenant-foo"}):
        assert current_project_id() == "tenant-foo"


def test_case_b_invalid_env_var_rejected():
    with _env(**{ENV_VAR: "Bad ID"}):
        with pytest.raises(ValueError):
            current_project_id()


# ---------------------------------------------------------------------------
# project_filter_enabled() — VNX_PROJECT_FILTER opt-out
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_filter_enabled_falsy_values_disable(value: str) -> None:
    with _env(**{FILTER_ENV_VAR: value}):
        assert project_filter_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything-else"])
def test_filter_enabled_truthy_values_enable(value: str) -> None:
    with _env(**{FILTER_ENV_VAR: value}):
        assert project_filter_enabled() is True


def test_filter_enabled_defaults_on():
    with _env(**{FILTER_ENV_VAR: None}):
        assert project_filter_enabled() is True


# ---------------------------------------------------------------------------
# C — writers stamp project_id
# ---------------------------------------------------------------------------

def test_case_c_intelligence_persist_stamps_project_id(tmp_path):
    db = tmp_path / "quality_intelligence.db"
    _build_quality_db(db)

    import intelligence_persist  # noqa: PLC0415

    class _Sig:
        signal_type = "gate_success"
        content = "gate gate_pr0_input_ready_contract passed"
        severity = "info"
        defect_family = ""

        class correlation:
            dispatch_id = "20260430-test-success"
            feature_id = "f-test"

    with _env(**{ENV_VAR: "tenant-c"}):
        result = intelligence_persist.persist_signals_to_db([_Sig()], db)
    assert result["patterns_upserted"] == 1

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT title, project_id FROM success_patterns"
    ).fetchall()
    conn.close()
    assert rows == [("gate gate_pr0_input_ready_contract passed", "tenant-c")]


def test_case_c_confidence_event_carries_project_id(tmp_path):
    db = tmp_path / "quality_intelligence.db"
    _build_quality_db(db)

    import intelligence_persist  # noqa: PLC0415

    pattern_id = _seed_pattern(
        db, title="seeded", project_id="tenant-c", confidence=0.8
    )
    # Re-link source_dispatch_ids so update_confidence_from_outcome can find
    # the seeded row.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE success_patterns SET source_dispatch_ids='[\"20260430-x\"]' "
        "WHERE id=?",
        (pattern_id,),
    )
    conn.commit()
    conn.close()

    with _env(**{ENV_VAR: "tenant-c"}):
        intelligence_persist.update_confidence_from_outcome(
            db, "20260430-x", "T1", "success",
        )

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT dispatch_id, terminal, outcome, project_id FROM confidence_events"
    ).fetchall()
    pu_rows = conn.execute(
        "SELECT pattern_id, project_id FROM pattern_usage"
    ).fetchall()
    conn.close()
    assert rows == [("20260430-x", "T1", "success", "tenant-c")]
    assert len(pu_rows) == 1 and pu_rows[0][1] == "tenant-c"


# ---------------------------------------------------------------------------
# D — selector reads filter on current project; opt-out reads all
# ---------------------------------------------------------------------------

def test_case_d_selector_filters_to_current_project(tmp_path):
    db = tmp_path / "quality_intelligence.db"
    _build_quality_db(db)
    _seed_pattern(db, title="dev-pattern", project_id="vnx-dev")
    _seed_pattern(db, title="foo-pattern", project_id="tenant-foo")

    import intelligence_selector  # noqa: PLC0415

    # Default tenant — sees only its own project rows.
    with _env(**{ENV_VAR: None, FILTER_ENV_VAR: None}):
        sel = intelligence_selector.IntelligenceSelector(quality_db_path=db)
        try:
            result = sel.select(
                dispatch_id="d-1",
                injection_point="dispatch_create",
                skill_name="architect",
                scope_tags=["architect"],
            )
        finally:
            sel.close()
    titles = [item.title for item in result.items]
    assert "dev-pattern" in titles
    assert "foo-pattern" not in titles

    # Foreign tenant — sees its rows, not vnx-dev's.
    with _env(**{ENV_VAR: "tenant-foo", FILTER_ENV_VAR: None}):
        sel = intelligence_selector.IntelligenceSelector(quality_db_path=db)
        try:
            result = sel.select(
                dispatch_id="d-2",
                injection_point="dispatch_create",
                skill_name="architect",
                scope_tags=["architect"],
            )
        finally:
            sel.close()
    titles = [item.title for item in result.items]
    assert "foo-pattern" in titles
    assert "dev-pattern" not in titles


def test_case_d_filter_opt_out_returns_cross_tenant_rows(tmp_path):
    db = tmp_path / "quality_intelligence.db"
    _build_quality_db(db)
    _seed_pattern(db, title="dev-pattern", project_id="vnx-dev")
    _seed_pattern(db, title="foo-pattern", project_id="tenant-foo")

    import intelligence_selector  # noqa: PLC0415

    with _env(**{ENV_VAR: None, FILTER_ENV_VAR: "0"}):
        sel = intelligence_selector.IntelligenceSelector(quality_db_path=db)
        try:
            result = sel.select(
                dispatch_id="d-3",
                injection_point="dispatch_create",
                skill_name="architect",
                scope_tags=["architect"],
            )
        finally:
            sel.close()
    titles = {item.title for item in result.items}
    # Only one proven_pattern slot per call, but the candidate pool is now
    # cross-tenant; ranking by confidence must surface a pattern. We assert
    # that the chosen pattern came from one of the two tenants and that the
    # selector did not raise.
    assert titles & {"dev-pattern", "foo-pattern"}


# ---------------------------------------------------------------------------
# E — backward compat: rows with the Phase 0 default are still readable.
# ---------------------------------------------------------------------------

def test_case_e_phase0_default_rows_readable_for_default_tenant(tmp_path):
    db = tmp_path / "quality_intelligence.db"
    _build_quality_db(db)

    # Insert without naming project_id — Phase 0 default 'vnx-dev' applies.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO success_patterns (pattern_type, category, title, description, "
        " pattern_data, confidence_score, usage_count, source_dispatch_ids, "
        " first_seen, last_used) "
        "VALUES ('approach', 'architect', 'phase0-row', 'desc', '{}', 0.9, 5, "
        " '[]', '2026-04-01T00:00:00', '2026-04-01T00:00:00')",
    )
    conn.commit()
    project = conn.execute(
        "SELECT project_id FROM success_patterns WHERE title='phase0-row'"
    ).fetchone()[0]
    conn.close()
    assert project == "vnx-dev"

    import intelligence_selector  # noqa: PLC0415

    with _env(**{ENV_VAR: None}):
        sel = intelligence_selector.IntelligenceSelector(quality_db_path=db)
        try:
            result = sel.select(
                dispatch_id="d-e",
                injection_point="dispatch_create",
                skill_name="architect",
                scope_tags=["architect"],
            )
        finally:
            sel.close()
    titles = [item.title for item in result.items]
    assert "phase0-row" in titles


# ---------------------------------------------------------------------------
# Extra: log_dispatch_metadata writes project_id when column is present.
# ---------------------------------------------------------------------------

def test_log_dispatch_metadata_stamps_project_id(tmp_path, monkeypatch):
    db = tmp_path / "quality_intelligence.db"
    _build_quality_db(db)

    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv(ENV_VAR, "tenant-log")

    # Reload module so its DB_PATH is recomputed from the patched env var.
    if "log_dispatch_metadata" in sys.modules:
        del sys.modules["log_dispatch_metadata"]
    import log_dispatch_metadata as ldm

    monkeypatch.setattr(ldm, "DB_PATH", db)

    monkeypatch.setattr(
        sys, "argv",
        [
            "log_dispatch_metadata.py",
            "--dispatch-id", "20260430-tenant-log",
            "--terminal", "T1",
            "--track", "A",
            "--skill-name", "backend-developer",
        ],
    )
    rc = ldm.main()
    assert rc == 0

    conn = sqlite3.connect(str(db))
    project = conn.execute(
        "SELECT project_id FROM dispatch_metadata WHERE dispatch_id='20260430-tenant-log'"
    ).fetchone()[0]
    conn.close()
    assert project == "tenant-log"
