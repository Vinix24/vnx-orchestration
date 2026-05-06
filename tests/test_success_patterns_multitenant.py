#!/usr/bin/env python3
"""Phase 1.5 PR-2 — multi-tenant correctness for success_patterns.

Closes OI-1315 (PR #352 findings) and OI-1321 (PR #311 findings).

Each test corresponds to a specific finding from the codex re-audit:

  Finding 1 — canonical lookup must filter by project_id
              (intelligence_selector._resolve_canonical_id)
  Finding 2 — after duplicate remap the IntelligenceItem MUST emit the
              CANONICAL row's confidence/usage_count/title/description, not
              the stale duplicate row's values
              (intelligence_selector._query_proven_patterns)
  Finding 3 — _record_pattern_usage and _stamp_source_dispatch_id must
              consume the canonical row consistently after remap (i.e. the
              writes target the canonical row's id, not the duplicate's)
  Finding 4 — pattern_dedup --apply must group by (project_id, content_hash)
              so per-project rows are preserved across tenants
  Finding 5 — source_dispatch_ids must be stamped at injection time so
              receipt-driven confidence updates can match a freshly injected
              pattern back to its source dispatch on receipt arrival

The DB fixture mirrors ``tests/test_dispatch_id_stamp.py`` and adds the
``project_id`` column from migration 0010 plus the ``content_hash`` column
from migration 0012.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import project_scope  # noqa: E402
from intelligence_selector import IntelligenceSelector  # noqa: E402
from intelligence_persist import update_confidence_from_outcome  # noqa: E402
from pattern_dedup import dedup_success_patterns  # noqa: E402
from runtime_coordination import init_schema  # noqa: E402


def _setup_quality_db(db_path: Path) -> sqlite3.Connection:
    """Create a quality_intelligence DB with project_id + content_hash columns."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, code_example TEXT, prerequisites TEXT, outcomes TEXT,
            success_rate REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
            avg_completion_time INTEGER, confidence_score REAL DEFAULT 0.0,
            source_dispatch_ids TEXT, source_receipts TEXT,
            first_seen DATETIME, last_used DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            content_hash TEXT,
            pattern_category TEXT NOT NULL DEFAULT 'code'
        );
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, problem_example TEXT, why_problematic TEXT,
            better_alternative TEXT, occurrence_count INTEGER DEFAULT 0,
            avg_resolution_time INTEGER, severity TEXT DEFAULT 'medium',
            source_dispatch_ids TEXT, first_seen DATETIME, last_seen DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT, rule_type TEXT, description TEXT,
            recommendation TEXT, confidence REAL DEFAULT 0.0,
            created_at TEXT, triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT UNIQUE, terminal TEXT, track TEXT,
            role TEXT, skill_name TEXT, gate TEXT, cognition TEXT DEFAULT 'normal',
            priority TEXT DEFAULT 'P1', pr_id TEXT, parent_dispatch TEXT,
            pattern_count INTEGER DEFAULT 0, prevention_rule_count INTEGER DEFAULT 0,
            intelligence_json TEXT, instruction_char_count INTEGER DEFAULT 0,
            context_file_count INTEGER DEFAULT 0,
            dispatched_at DATETIME, completed_at DATETIME,
            outcome_status TEXT, outcome_report_path TEXT, session_id TEXT,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE IF NOT EXISTS pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT NOT NULL,
            pattern_hash TEXT NOT NULL,
            used_count INTEGER DEFAULT 0,
            ignored_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            last_used TIMESTAMP,
            last_offered TIMESTAMP,
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE IF NOT EXISTS confidence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            terminal TEXT,
            outcome TEXT NOT NULL,
            patterns_boosted INTEGER DEFAULT 0,
            patterns_decayed INTEGER DEFAULT 0,
            confidence_change REAL NOT NULL,
            occurred_at TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        );
        CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
            dispatch_id   TEXT NOT NULL,
            pattern_id    TEXT NOT NULL,
            pattern_title TEXT NOT NULL,
            offered_at    TEXT NOT NULL,
            project_id    TEXT NOT NULL DEFAULT 'vnx-dev',
            PRIMARY KEY (dispatch_id, pattern_id)
        );
        """
    )
    conn.commit()
    return conn


def _seed_pattern(
    conn: sqlite3.Connection,
    *,
    title: str,
    description: str,
    project_id: str,
    confidence: float = 0.85,
    usage_count: int = 5,
    source_dispatch_ids: str | None = None,
    content_hash_value: str | None = None,
) -> int:
    """Insert a row and return its id. Stable content_hash supplied by caller."""
    if content_hash_value is None:
        # Mirror pattern_dedup._short_content_hash() so hashes line up.
        from pattern_dedup import _short_content_hash
        content_hash_value = _short_content_hash(title, description)
    cur = conn.execute(
        """
        INSERT INTO success_patterns
            (pattern_type, category, title, description, pattern_data,
             confidence_score, usage_count, source_dispatch_ids,
             first_seen, last_used, project_id, content_hash)
        VALUES ('approach', 'architect', ?, ?, '{}', ?, ?, ?,
                '2026-04-01', '2026-04-01', ?, ?)
        """,
        (
            title,
            description,
            confidence,
            usage_count,
            source_dispatch_ids,
            project_id,
            content_hash_value,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _read_row(db_path: Path, pattern_id: int) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM success_patterns WHERE id = ?",
            (pattern_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def _read_source_ids(db_path: Path, pattern_id: int) -> list[str]:
    row = _read_row(db_path, pattern_id)
    raw = row.get("source_dispatch_ids")
    if not raw:
        return []
    try:
        return list(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return []


def _all_pattern_ids(db_path: Path) -> list[int]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT id FROM success_patterns ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in rows]


class _MultiTenantFixture(unittest.TestCase):
    """Shared setup: quality DB on disk, isolated coord state dir."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self._quality_db_path = base / "quality_intelligence.db"
        self._state_dir = base / "state"
        self._state_dir.mkdir()
        init_schema(str(self._state_dir))
        conn = _setup_quality_db(self._quality_db_path)
        conn.close()
        self._original_env = {
            "VNX_PROJECT_ID": os.environ.get("VNX_PROJECT_ID"),
            "VNX_PROJECT_FILTER": os.environ.get("VNX_PROJECT_FILTER"),
        }

    def tearDown(self) -> None:
        for k, v in self._original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmpdir.cleanup()

    def _set_project(self, project_id: str) -> None:
        os.environ["VNX_PROJECT_ID"] = project_id
        os.environ["VNX_PROJECT_FILTER"] = "1"

    def _inject(self, dispatch_id: str) -> object:
        """Run select() + record_injection() + stamp_source_dispatch_ids().

        Mirrors the wiring in subprocess_dispatch_internals.skill_injection
        so the integration path is exercised end-to-end.
        """
        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        try:
            result = selector.select(
                dispatch_id=dispatch_id,
                injection_point="dispatch_create",
                skill_name="architect",
            )
            selector.record_injection(result)
            selector.stamp_source_dispatch_ids(result)
        finally:
            selector.close()
        return result


class Finding1CanonicalLookupProjectScopedTests(_MultiTenantFixture):
    """Two projects with identical pattern text MUST NOT collide on canonical id."""

    SHARED_TITLE = "Use structured output"
    SHARED_DESC = "Structured output improves first-pass success."

    def setUp(self) -> None:
        super().setUp()
        conn = sqlite3.connect(str(self._quality_db_path))
        try:
            self._a_id = _seed_pattern(
                conn,
                title=self.SHARED_TITLE,
                description=self.SHARED_DESC,
                project_id="proj-a",
                confidence=0.7,
                usage_count=3,
            )
            self._b_id = _seed_pattern(
                conn,
                title=self.SHARED_TITLE,
                description=self.SHARED_DESC,
                project_id="proj-b",
                confidence=0.9,
                usage_count=10,
            )
        finally:
            conn.close()
        # Sanity: project A's row id is smaller (would be MIN(id) before fix).
        self.assertLess(self._a_id, self._b_id)

    def test_project_a_canonical_id_is_a_row_only(self) -> None:
        self._set_project("proj-a")
        result = self._inject("D-A-1")
        self.assertGreaterEqual(len(result.items), 1)
        sp_items = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(sp_items), 1)
        self.assertEqual(sp_items[0].item_id, f"intel_sp_{self._a_id}")
        # Cross-tenant pollution check: B's row source_dispatch_ids untouched.
        self.assertEqual(_read_source_ids(self._quality_db_path, self._b_id), [])
        self.assertEqual(_read_source_ids(self._quality_db_path, self._a_id), ["D-A-1"])

    def test_project_b_canonical_id_is_b_row_only(self) -> None:
        self._set_project("proj-b")
        result = self._inject("D-B-1")
        sp_items = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(sp_items), 1)
        # Bug pre-Fix-1: would emit intel_sp_<a_id> because MIN(id) ignored project.
        self.assertEqual(sp_items[0].item_id, f"intel_sp_{self._b_id}")
        # Project A's row stays clean.
        self.assertEqual(_read_source_ids(self._quality_db_path, self._a_id), [])
        self.assertEqual(_read_source_ids(self._quality_db_path, self._b_id), ["D-B-1"])


class Finding2CanonicalScoreEmittedAfterRemapTests(_MultiTenantFixture):
    """After dedup remap, the IntelligenceItem MUST emit the canonical row's score."""

    TITLE = "Cache compiled regex modules"
    DESC = "Compiling regex inside hot loops dominates CPU; cache at module scope."

    def setUp(self) -> None:
        super().setUp()
        conn = sqlite3.connect(str(self._quality_db_path))
        try:
            # Canonical row: low confidence, low usage_count, but it's the
            # smallest id so _resolve_canonical_id() will pick it.
            self._canonical_id = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-c",
                confidence=0.62,
                usage_count=2,
                source_dispatch_ids=json.dumps(["D-old-canonical"]),
            )
            # Duplicate row: high confidence, high usage_count. Pre-Fix-2 the
            # selector would have emitted THIS row's confidence/usage_count
            # because the per-row dict was used after remap.
            self._duplicate_id = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-c",
                confidence=0.95,
                usage_count=99,
                source_dispatch_ids=json.dumps(["D-old-duplicate"]),
            )
        finally:
            conn.close()
        self.assertLess(self._canonical_id, self._duplicate_id)

    def test_emitted_confidence_matches_canonical_not_duplicate(self) -> None:
        self._set_project("proj-c")
        result = self._inject("D-C-1")
        sp_items = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(sp_items), 1)
        emitted = sp_items[0]
        self.assertEqual(emitted.item_id, f"intel_sp_{self._canonical_id}")
        self.assertAlmostEqual(emitted.confidence, 0.62, places=4)
        self.assertEqual(emitted.evidence_count, 2)
        # source_refs reflect the canonical row's source_dispatch_ids list.
        self.assertIn("D-old-canonical", emitted.source_refs)
        self.assertNotIn("D-old-duplicate", emitted.source_refs)


class Finding3HelpersUseCanonicalRowTests(_MultiTenantFixture):
    """_record_pattern_usage + _stamp_source_dispatch_id must target the canonical row."""

    TITLE = "Bound retries on transient failure"
    DESC = "Exponential backoff with jitter prevents retry-storm cascades."

    def setUp(self) -> None:
        super().setUp()
        conn = sqlite3.connect(str(self._quality_db_path))
        try:
            self._canonical_id = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-d",
                confidence=0.68,
                usage_count=4,
            )
            self._duplicate_id = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-d",
                confidence=0.91,
                usage_count=20,
            )
        finally:
            conn.close()

    def test_pattern_usage_keyed_to_canonical_id(self) -> None:
        self._set_project("proj-d")
        self._inject("D-D-1")

        conn = sqlite3.connect(str(self._quality_db_path))
        conn.row_factory = sqlite3.Row
        try:
            keys = [
                row["pattern_id"]
                for row in conn.execute(
                    "SELECT pattern_id FROM pattern_usage "
                    "WHERE pattern_id LIKE 'intel_sp_%'"
                ).fetchall()
            ]
        finally:
            conn.close()
        self.assertIn(f"intel_sp_{self._canonical_id}", keys)
        self.assertNotIn(f"intel_sp_{self._duplicate_id}", keys)

    def test_source_dispatch_id_stamped_on_canonical_row(self) -> None:
        self._set_project("proj-d")
        self._inject("D-D-2")
        # Canonical row stamped, duplicate row left untouched.
        self.assertEqual(
            _read_source_ids(self._quality_db_path, self._canonical_id),
            ["D-D-2"],
        )
        self.assertEqual(
            _read_source_ids(self._quality_db_path, self._duplicate_id),
            [],
        )


class Finding4PatternDedupGroupsByProjectTests(_MultiTenantFixture):
    """pattern_dedup --apply on multi-project DB MUST preserve per-project rows."""

    TITLE = "Verify schema migration before deploy"
    DESC = "Run the migration in a shadow DB and diff before flipping prod."

    def setUp(self) -> None:
        super().setUp()
        conn = sqlite3.connect(str(self._quality_db_path))
        try:
            self._a_id = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-a",
                confidence=0.8,
                usage_count=4,
            )
            self._b_id = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-b",
                confidence=0.7,
                usage_count=3,
            )
            # Same project as A but second row → these MUST be collapsed.
            self._a_dup_id = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-a",
                confidence=0.6,
                usage_count=1,
            )
        finally:
            conn.close()

    def test_dedup_apply_preserves_per_project_rows(self) -> None:
        report = dedup_success_patterns(self._quality_db_path, apply=True)
        # Exactly one duplicate group: project-a, 1 row collapsed.
        self.assertEqual(sum(report.values()), 1)

        ids_after = _all_pattern_ids(self._quality_db_path)
        # Project A's canonical (smaller id) and Project B's row both survive.
        self.assertIn(self._a_id, ids_after)
        self.assertIn(self._b_id, ids_after)
        # Project A's duplicate is gone.
        self.assertNotIn(self._a_dup_id, ids_after)

    def test_dedup_dry_run_reports_per_project_grouping(self) -> None:
        report = dedup_success_patterns(self._quality_db_path, apply=False)
        # Dry-run reports duplicates; project-b's row is NOT a duplicate.
        keys = list(report.keys())
        self.assertEqual(len(keys), 1)
        # Grouping key carries the project_id when present.
        self.assertTrue(keys[0].startswith("proj-a:"))


class Finding5InjectionTimeStampingForFreshlyInjectedTests(_MultiTenantFixture):
    """Receipt-driven decay updates the right canonical row after fresh injection."""

    TITLE = "Persist test fixture between runs"
    DESC = "Re-creating fixtures per-test inflates wall-clock by ~10x."

    def test_fresh_injection_then_failure_decays_canonical_only(self) -> None:
        self._set_project("proj-e")
        conn = sqlite3.connect(str(self._quality_db_path))
        try:
            # Pre-existing canonical row with known confidence; nothing in
            # source_dispatch_ids yet → "freshly injected" relative to D-E-1.
            canonical_id = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-e",
                confidence=0.85,
                usage_count=5,
            )
            # An unrelated tenant's row that MUST NOT be touched by D-E-1.
            other_tenant_id = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-f",
                confidence=0.85,
                usage_count=5,
            )
        finally:
            conn.close()

        # Inject and read back source_dispatch_ids on each row.
        self._inject("D-E-1")
        self.assertEqual(
            _read_source_ids(self._quality_db_path, canonical_id),
            ["D-E-1"],
        )
        self.assertEqual(
            _read_source_ids(self._quality_db_path, other_tenant_id),
            [],
        )

        # Failure receipt → confidence on the canonical row decays.
        before = _read_row(self._quality_db_path, canonical_id)["confidence_score"]
        result = update_confidence_from_outcome(
            self._quality_db_path,
            dispatch_id="D-E-1",
            terminal="T1",
            status="failure",
        )
        after = _read_row(self._quality_db_path, canonical_id)["confidence_score"]
        self.assertEqual(result["decayed"], 1)
        self.assertLess(after, before)
        # Cross-tenant row's confidence is unchanged.
        other_after = _read_row(self._quality_db_path, other_tenant_id)["confidence_score"]
        self.assertAlmostEqual(other_after, 0.85, places=4)


class FixForwardCodexFinding1RedirectCarriesProjectIdTests(_MultiTenantFixture):
    """pattern_dedup --apply must carry project_id through the dispatch_pattern_offered redirect.

    Pre-fix bug: ``_redirect_dispatch_pattern_offered`` re-inserted the
    redirected row WITHOUT ``project_id``, so the row picked up the table
    default ('vnx-dev' from migration 0010). A same-content row from a
    different tenant could be silently rebound onto the wrong project.
    """

    TITLE = "Cross-tenant redirect protection"
    DESC = "Same-text rows in different projects must keep their tenant on redirect."

    def test_redirect_preserves_source_row_project_id(self) -> None:
        # Two duplicates within proj-x (canonical + dup) plus one offered
        # row each, AND a same-text proj-y baseline that must remain
        # untouched. After dedup, the proj-x dup row's offering should be
        # rebound to the proj-x canonical with project_id='proj-x', NOT the
        # 'vnx-dev' table default.
        conn = sqlite3.connect(str(self._quality_db_path))
        try:
            x_canonical = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-x",
                confidence=0.8,
                usage_count=2,
            )
            x_duplicate = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-x",
                confidence=0.7,
                usage_count=1,
            )
            y_baseline = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-y",
                confidence=0.75,
                usage_count=4,
            )
            now_ts = "2026-05-06T10:00:00Z"
            conn.execute(
                """
                INSERT INTO dispatch_pattern_offered
                    (dispatch_id, pattern_id, pattern_title, offered_at, project_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("D-X-CANON", f"intel_sp_{x_canonical}", self.TITLE, now_ts, "proj-x"),
            )
            conn.execute(
                """
                INSERT INTO dispatch_pattern_offered
                    (dispatch_id, pattern_id, pattern_title, offered_at, project_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("D-X-DUP", f"intel_sp_{x_duplicate}", self.TITLE, now_ts, "proj-x"),
            )
            conn.execute(
                """
                INSERT INTO dispatch_pattern_offered
                    (dispatch_id, pattern_id, pattern_title, offered_at, project_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("D-Y-1", f"intel_sp_{y_baseline}", self.TITLE, now_ts, "proj-y"),
            )
            conn.commit()
        finally:
            conn.close()

        report = dedup_success_patterns(self._quality_db_path, apply=True)
        # Exactly one duplicate group: proj-x had 2 rows → 1 collapsed.
        self.assertEqual(sum(report.values()), 1)

        # The redirected row from D-X-DUP must now point at the canonical
        # pattern_id AND retain project_id='proj-x'.
        conn = sqlite3.connect(str(self._quality_db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT dispatch_id, pattern_id, project_id
                FROM   dispatch_pattern_offered
                WHERE  dispatch_id = 'D-X-DUP'
                """
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "Redirected D-X-DUP row missing after dedup --apply")
        self.assertEqual(row["pattern_id"], f"intel_sp_{x_canonical}")
        # Pre-fix this would have been 'vnx-dev' (the table default).
        self.assertEqual(row["project_id"], "proj-x")

        # Cross-tenant proj-y row stays exactly as it was.
        conn = sqlite3.connect(str(self._quality_db_path))
        conn.row_factory = sqlite3.Row
        try:
            y_row = conn.execute(
                """
                SELECT pattern_id, project_id
                FROM   dispatch_pattern_offered
                WHERE  dispatch_id = 'D-Y-1'
                """
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(y_row)
        self.assertEqual(y_row["pattern_id"], f"intel_sp_{y_baseline}")
        self.assertEqual(y_row["project_id"], "proj-y")


class FixForwardCodexFinding2CanonicalCategoryAfterRemapTests(_MultiTenantFixture):
    """After dedup remap, the IntelligenceItem MUST emit the canonical row's category.

    Pre-fix bug: ``_query_proven_patterns`` derived ``pattern_scope`` from the
    pre-remap (duplicate) row's ``category`` value, even though title/content/
    confidence/pattern_category were correctly remapped. A higher-ranked
    duplicate with a different ``category`` could therefore tag the emitted
    item with a stale scope.
    """

    TITLE = "Bound subprocess output capture"
    DESC = "Stream stdout/stderr with bounded buffers to avoid OOM on long runs."

    def setUp(self) -> None:
        super().setUp()
        # Canonical row uses category='governance' (smaller id, lower confidence
        # and usage_count); duplicate uses category='code' with higher
        # confidence so the duplicate's row would be processed first.
        conn = sqlite3.connect(str(self._quality_db_path))
        try:
            cur = conn.execute(
                """
                INSERT INTO success_patterns
                    (pattern_type, category, title, description, pattern_data,
                     confidence_score, usage_count, source_dispatch_ids,
                     first_seen, last_used, project_id, content_hash,
                     pattern_category)
                VALUES ('approach', 'governance', ?, ?, '{}', ?, ?, ?,
                        '2026-04-01', '2026-04-01', ?, ?, 'governance')
                """,
                (
                    self.TITLE,
                    self.DESC,
                    0.65,
                    2,
                    None,
                    "proj-h",
                    self._content_hash(),
                ),
            )
            self._canonical_id = int(cur.lastrowid)
            cur = conn.execute(
                """
                INSERT INTO success_patterns
                    (pattern_type, category, title, description, pattern_data,
                     confidence_score, usage_count, source_dispatch_ids,
                     first_seen, last_used, project_id, content_hash,
                     pattern_category)
                VALUES ('approach', 'code', ?, ?, '{}', ?, ?, ?,
                        '2026-04-01', '2026-04-01', ?, ?, 'code')
                """,
                (
                    self.TITLE,
                    self.DESC,
                    0.95,
                    50,
                    None,
                    "proj-h",
                    self._content_hash(),
                ),
            )
            self._duplicate_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        self.assertLess(self._canonical_id, self._duplicate_id)

    def _content_hash(self) -> str:
        from pattern_dedup import _short_content_hash
        return _short_content_hash(self.TITLE, self.DESC)

    def test_emitted_scope_tags_match_canonical_category(self) -> None:
        self._set_project("proj-h")
        # Pass scope_tags explicitly so BOTH rows pass the per-row scope
        # filter regardless of category; we want the test to exercise the
        # post-remap pattern_scope assignment, not the pre-filter.
        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        try:
            result = selector.select(
                dispatch_id="D-H-1",
                injection_point="dispatch_create",
                # explicit task class avoids governance-penalty short-circuit
                task_class="research_structured",
                scope_tags=["governance", "code"],
            )
        finally:
            selector.close()

        sp_items = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(sp_items), 1)
        emitted = sp_items[0]
        self.assertEqual(emitted.item_id, f"intel_sp_{self._canonical_id}")
        # Pre-fix: emitted.scope_tags would be ['code'] (the duplicate's
        # category). Post-fix: it must reflect the canonical's 'governance'.
        self.assertEqual(emitted.scope_tags, ["governance"])
        # pattern_category was already canonical-sourced; confirm regression
        # protection so a future refactor cannot silently break it either.
        self.assertEqual(emitted.pattern_category, "governance")


class Finding5StampingIdempotencyTests(_MultiTenantFixture):
    """stamp_source_dispatch_ids is idempotent when called twice for the same dispatch."""

    TITLE = "Cache compiled regex modules"
    DESC = "Compiling regex inside hot loops dominates CPU; cache at module scope."

    def test_repeated_call_is_idempotent(self) -> None:
        self._set_project("proj-g")
        conn = sqlite3.connect(str(self._quality_db_path))
        try:
            pattern_id = _seed_pattern(
                conn,
                title=self.TITLE,
                description=self.DESC,
                project_id="proj-g",
            )
        finally:
            conn.close()
        self._inject("D-G-1")
        self._inject("D-G-1")  # second call must NOT duplicate the entry
        self.assertEqual(_read_source_ids(self._quality_db_path, pattern_id), ["D-G-1"])


if __name__ == "__main__":
    unittest.main()
