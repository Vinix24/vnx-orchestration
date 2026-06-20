"""Regression test for the nightly_digests cross-tenant collision (Round-7).

`nightly_digests` shipped with a single-column ``digest_date DATE NOT NULL
UNIQUE`` that was never scoped to ``project_id``. Two tenants with a digest for
the same calendar day collide on the global UNIQUE, so ``INSERT OR IGNORE``
silently drops the second tenant's row — exactly the failure that broke the
vnx-dev import (its 20 digests shared dates with seocrawler-v2's 50).

Two latent gaps allowed it:
  1. ``nightly_digests`` was missing from the migrator's composite-unique
     rebuild list (``COMPOSITE_UNIQUE_TABLES_QI``).
  2. the Round-6 audit pattern did not treat ``*_date`` as a tenant-suspect
     column, so the regression guard never flagged it.

This proves both are fixed and that cross-tenant same-date rows coexist after
the rebuild while same-tenant duplicates are still rejected.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from scripts import migrate_to_central_vnx as M  # noqa: E402


# The exact pre-fix central shape: column-level UNIQUE on digest_date, with
# project_id added later via a DEFAULT'd ALTER (mirrors migration 0010).
_NIGHTLY_DIGESTS_SQL = """
CREATE TABLE nightly_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_date DATE NOT NULL UNIQUE,
    sessions_analyzed INTEGER DEFAULT 0,
    digest_markdown TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev'
)
"""


def _make_qi_with_nightly_digests(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(_NIGHTLY_DIGESTS_SQL)
    # one tenant already owns the 2026-06-15 date slot
    con.execute(
        "INSERT INTO nightly_digests (digest_date, digest_markdown, project_id) "
        "VALUES ('2026-06-15', '# seo digest', 'seocrawler-v2')"
    )
    con.commit()
    con.close()


def _empty_rc_db(path: Path) -> None:
    # apply_composite_unique_constraints also opens the RC db; an empty but
    # valid db exercises the missing-table skip path.
    sqlite3.connect(path).close()


def test_nightly_digests_is_in_composite_rebuild_list():
    assert M.COMPOSITE_UNIQUE_TABLES_QI.get("nightly_digests") == "digest_date"


def test_audit_pattern_now_flags_date_suffix():
    # Round-6 missed nightly_digests because *_date was not a suspect suffix.
    assert M._T3_SUSPECT_COLUMN_PATTERN.match("digest_date") is not None


def test_rebuild_makes_digest_date_composite_and_preserves_rows(tmp_path):
    qi = tmp_path / "quality_intelligence.db"
    rc = tmp_path / "runtime_coordination.db"
    _make_qi_with_nightly_digests(qi)
    _empty_rc_db(rc)

    M.apply_composite_unique_constraints(qi, rc)

    con = sqlite3.connect(qi)
    try:
        assert M._has_composite_project_unique(con, "nightly_digests", "digest_date")
        survived = con.execute(
            "SELECT COUNT(*) FROM nightly_digests WHERE project_id='seocrawler-v2'"
        ).fetchone()[0]
        assert survived == 1
    finally:
        con.close()


def test_cross_tenant_same_date_coexists_after_rebuild(tmp_path):
    qi = tmp_path / "quality_intelligence.db"
    rc = tmp_path / "runtime_coordination.db"
    _make_qi_with_nightly_digests(qi)
    _empty_rc_db(rc)

    M.apply_composite_unique_constraints(qi, rc)

    con = sqlite3.connect(qi)
    try:
        # The bug: a second tenant's digest for the SAME date must now insert.
        con.execute(
            "INSERT INTO nightly_digests (digest_date, digest_markdown, project_id) "
            "VALUES ('2026-06-15', '# vnx digest', 'vnx-dev')"
        )
        con.commit()
        total = con.execute(
            "SELECT COUNT(*) FROM nightly_digests WHERE digest_date='2026-06-15'"
        ).fetchone()[0]
        assert total == 2

        # But composite uniqueness still holds: a same-tenant duplicate is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO nightly_digests (digest_date, digest_markdown, project_id) "
                "VALUES ('2026-06-15', '# dup', 'vnx-dev')"
            )
    finally:
        con.close()
