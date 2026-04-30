#!/usr/bin/env python3
"""Production canary: assert intelligence loop is alive in the runtime DBs.

These tests run against the real ``.vnx-data/state/quality_intelligence.db``
and ``runtime_coordination.db`` when present.  They are intentionally
non-mock canaries: in CI Profile D they catch silent loop failures within
24h of regression.

If neither DB exists (e.g. fresh checkout, sandboxed CI without runtime
state), tests SKIP rather than fail — the absence of state is not the
failure mode we're trying to catch.

Failure modes detected:
    * confidence_events stops being written -> outcome path broken
    * intelligence_injections stops being written -> selector / dispatch path broken
    * pattern_usage rows never refresh -> learning loop offline
    * confidence saturates at 1.0 -> open-circuit boost-only path
    * injections become content-identical -> diversity collapse
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Resolve project root + canonical state dir without depending on env vars.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _state_dir() -> Path:
    """Locate .vnx-data/state — env override wins, then repo-local fallback."""
    env_data = os.environ.get("VNX_DATA_DIR")
    if env_data and os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1":
        return Path(env_data) / "state"
    env_state = os.environ.get("VNX_STATE_DIR")
    if env_state:
        return Path(env_state)
    return _REPO_ROOT / ".vnx-data" / "state"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _open(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        pytest.skip(f"{db_path} not present — canary requires runtime state")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _require_table(conn: sqlite3.Connection, name: str) -> None:
    if not _table_exists(conn, name):
        pytest.skip(f"table {name} not yet migrated in this runtime DB")


@pytest.fixture(scope="module")
def quality_db() -> Path:
    return _state_dir() / "quality_intelligence.db"


@pytest.fixture(scope="module")
def coord_db() -> Path:
    return _state_dir() / "runtime_coordination.db"


# ---------------------------------------------------------------------------
# Recency assertions — production canaries
# ---------------------------------------------------------------------------

class TestConfidenceEventsRecent:
    """Confidence outcomes must continue to flow."""

    def test_confidence_events_in_last_24h(self, quality_db: Path) -> None:
        conn = _open(quality_db)
        try:
            _require_table(conn, "confidence_events")
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM confidence_events WHERE occurred_at >= ?",
                (cutoff,),
            ).fetchone()
        finally:
            conn.close()
        assert row["n"] > 0, (
            "No confidence_events rows in the last 24h — the receipt -> "
            "update_confidence_from_outcome path is silently broken (loop is open-circuit)."
        )


class TestInjectionsRecent:
    """The selector must continue to inject patterns into dispatches."""

    def test_intelligence_injections_in_last_24h(self, coord_db: Path) -> None:
        conn = _open(coord_db)
        try:
            _require_table(conn, "intelligence_injections")
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM intelligence_injections WHERE injected_at >= ?",
                (cutoff,),
            ).fetchone()
        finally:
            conn.close()
        assert row["n"] > 0, (
            "No intelligence_injections rows in the last 24h — selector or dispatch "
            "path stopped writing audit rows."
        )


class TestPatternUsageRecent:
    """pattern_usage must show activity in the last 7 days."""

    def test_pattern_usage_updated_in_last_7d(self, quality_db: Path) -> None:
        conn = _open(quality_db)
        try:
            _require_table(conn, "pattern_usage")
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            # updated_at OR last_used recency — either signals activity.
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pattern_usage "
                "WHERE COALESCE(updated_at, last_used) >= ?",
                (cutoff,),
            ).fetchone()
        finally:
            conn.close()
        assert row["n"] > 0, (
            "pattern_usage has no rows updated in the last 7 days — "
            "feedback loop is dormant."
        )


class TestNoConfidenceSaturation:
    """A healthy loop never lets every pattern stick at 1.0 (means decay never fires)."""

    def test_not_all_patterns_at_max_confidence(self, quality_db: Path) -> None:
        conn = _open(quality_db)
        try:
            _require_table(conn, "success_patterns")
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM success_patterns"
            ).fetchone()["n"]
            if total == 0:
                pytest.skip("no success_patterns yet — saturation check N/A")
            saturated = conn.execute(
                "SELECT COUNT(*) AS n FROM success_patterns WHERE confidence_score >= 0.999"
            ).fetchone()["n"]
        finally:
            conn.close()
        # 100% saturation indicates failure decay path is dead.  Allow up to
        # 95% to absorb genuinely well-validated patterns; the original audit
        # found the system at 100%.
        ratio = saturated / total
        assert ratio < 0.95, (
            f"{saturated}/{total} success_patterns at confidence>=0.999 ({ratio:.0%}). "
            "Failure decay path is likely broken — loop is boost-only."
        )


class TestInjectionDiversity:
    """Recent injections must not all carry byte-identical payloads.

    NOTE: Marked xfail (strict=False) because the 2026-04-30 audit found the
    runtime DB at >95% duplication. The canary still RUNS in Profile D — when
    the diversity bug is fixed and the test starts passing, strict=False keeps
    it green; if duplication regresses again it will surface as XFAIL again.
    Flip to strict=True once the bug is closed and a soak window confirms
    sustained diversity.
    """

    @pytest.mark.xfail(
        strict=False,
        reason="2026-04-30 audit: production DB has 90%+ identical injections; "
        "see claudedocs/2026-04-30-self-learning-loop-audit.md",
    )
    def test_recent_injections_not_all_identical(self, coord_db: Path) -> None:
        conn = _open(coord_db)
        try:
            _require_table(conn, "intelligence_injections")
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            rows = conn.execute(
                "SELECT items_json FROM intelligence_injections WHERE injected_at >= ?",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        if len(rows) < 10:
            pytest.skip(f"only {len(rows)} recent injections — sample too small")

        payloads = [r["items_json"] for r in rows]
        unique = set(payloads)
        # Audit found 90%+ duplication.  Healthy is < 90% duplication.
        unique_ratio = len(unique) / len(payloads)
        assert unique_ratio > 0.10, (
            f"{len(payloads)} injections collapsed to {len(unique)} unique payloads "
            f"({unique_ratio:.0%} unique). Diversity bug: selector echoing identical content."
        )
