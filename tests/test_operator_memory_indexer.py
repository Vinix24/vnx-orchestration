#!/usr/bin/env python3
"""Tests for operator_memory_indexer.py (Wave 5 P3)."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import operator_memory_indexer
from operator_memory_indexer import (
    MAX_MEMORIES_PER_DISPATCH,
    MAX_MEMORY_CHARS,
    OperatorMemory,
    _MemoryCache,
    _extract_instruction_terms,
    _infer_tags,
    _parse_frontmatter,
    _parse_memory_file,
    _project_memory_dir,
    _score_memory,
    fetch_relevant_memories,
    format_memories_section,
)


def _make_memory_file(directory: Path, filename: str, content: str) -> Path:
    p = directory / filename
    p.write_text(content, encoding="utf-8")
    return p


def _feedback_file(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\ntype: feedback\n---\n{body}"


def _project_file(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\ntype: project\n---\n{body}"


class TestParseFrontmatter(unittest.TestCase):
    def test_parse_frontmatter_extracts_name_description_type(self):
        text = "---\nname: My Memory\ndescription: A test memory\ntype: feedback\n---\nBody content here."
        kv, body = _parse_frontmatter(text)
        self.assertEqual(kv["name"], "My Memory")
        self.assertEqual(kv["description"], "A test memory")
        self.assertEqual(kv["type"], "feedback")
        self.assertIn("Body content here", body)

    def test_parse_handles_no_frontmatter_gracefully(self):
        text = "No frontmatter here, just content."
        kv, body = _parse_frontmatter(text)
        self.assertEqual(kv, {})
        self.assertEqual(body, text)

    def test_parse_frontmatter_with_empty_body(self):
        text = "---\nname: Empty\ndescription: empty\ntype: reference\n---\n"
        kv, body = _parse_frontmatter(text)
        self.assertEqual(kv["name"], "Empty")
        # body may be empty string
        self.assertIsInstance(body, str)

    def test_parse_frontmatter_multiline_body(self):
        text = "---\nname: Multi\ndescription: multi-line body test\ntype: user\n---\nLine1\nLine2\nLine3"
        kv, body = _parse_frontmatter(text)
        self.assertIn("Line1", body)
        self.assertIn("Line3", body)


class TestTagInference(unittest.TestCase):
    def test_tag_inference_from_name_and_description(self):
        tags = _infer_tags("database migration cleanup", "Check expired leases before dispatch")
        self.assertTrue(len(tags) > 0)
        # Should infer database-related tags
        tag_str = " ".join(tags)
        self.assertTrue(
            "database" in tag_str or "migration" in tag_str or "lease" in tag_str,
            f"Expected database/migration/lease tags, got: {tags}"
        )

    def test_tag_inference_backend_role(self):
        tags = _infer_tags("backend subprocess adapter", "Backend developer subprocess dispatch")
        tag_str = " ".join(tags)
        self.assertTrue(
            "backend" in tag_str or "subprocess" in tag_str,
            f"Expected backend/subprocess tags, got: {tags}"
        )

    def test_tag_inference_filters_stopwords(self):
        tags = _infer_tags("always never should would", "using via through")
        # None of the stopwords should appear as tags
        for t in tags:
            self.assertNotIn(t, {"always", "never", "should", "would", "using", "via", "through"})


class TestScoringAlgorithm(unittest.TestCase):
    def _make_memory(self, name: str, description: str, mem_type: str, body: str = "") -> OperatorMemory:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.md"
            content = f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n{body}"
            p.write_text(content, encoding="utf-8")
            return _parse_memory_file(p, content)

    def test_select_by_role_match_scores_highest(self):
        mem_db = self._make_memory(
            "Database stale lease", "Check runtime_coordination.db leases", "feedback"
        )
        mem_unrelated = self._make_memory(
            "Frontend styling tip", "CSS tricks for dashboard", "feedback"
        )
        score_db = _score_memory(mem_db, "database-engineer", [], [])
        score_unrelated = _score_memory(mem_unrelated, "database-engineer", [], [])
        self.assertGreater(score_db, score_unrelated)

    def test_select_by_dispatch_paths_overlap(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "lease.md"
            body = "Check runtime_coordination.db before dispatch.\n\nSee scripts/lib/runtime_coordination.py for details."
            content = f"---\nname: Lease cleanup\ndescription: lease cleanup\ntype: feedback\n---\n{body}"
            p.write_text(content, encoding="utf-8")
            mem = _parse_memory_file(p, content)

        score_with_path = _score_memory(
            mem, None, ["scripts/lib/runtime_coordination.py"], []
        )
        score_no_path = _score_memory(mem, None, [], [])
        self.assertGreater(score_with_path, score_no_path)

    def test_select_by_instruction_term_overlap(self):
        mem = self._make_memory(
            "Codex gate findings", "Parse codex findings from blocking_findings array", "feedback"
        )
        score_match = _score_memory(mem, None, [], ["codex", "blocking", "findings"])
        score_no_match = _score_memory(mem, None, [], ["frontend", "styling"])
        self.assertGreater(score_match, score_no_match)

    def test_feedback_type_weighted_higher_than_project(self):
        mem_feedback = self._make_memory(
            "Test memory", "Some database migration tip", "feedback"
        )
        mem_project = self._make_memory(
            "Test memory", "Some database migration tip", "project"
        )
        score_feedback = _score_memory(mem_feedback, "database-engineer", [], [])
        score_project = _score_memory(mem_project, "database-engineer", [], [])
        self.assertGreater(score_feedback, score_project)


class TestFetchAndBudget(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.memory_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()
        # Clear module-level caches
        operator_memory_indexer._CACHES.clear()

    def _write_memory(self, filename: str, content: str) -> None:
        (self.memory_dir / filename).write_text(content, encoding="utf-8")

    def test_top_k_capped_at_3(self):
        # Write 5 highly relevant memories
        for i in range(5):
            content = _feedback_file(
                f"Database migration tip {i}",
                f"database migration runtime_coordination tip {i}",
                f"Check leases before dispatch. Tip number {i}.",
            )
            self._write_memory(f"feedback_db_{i}.md", content)

        results = fetch_relevant_memories(
            "database-engineer",
            [],
            "database migration dispatch",
            memory_dir=self.memory_dir,
        )
        self.assertLessEqual(len(results), MAX_MEMORIES_PER_DISPATCH)

    def test_budget_truncates_at_max_chars(self):
        # Write 3 very large memory files
        for i in range(3):
            body = "x" * 1000  # large body
            content = _feedback_file(
                f"Large memory {i}",
                "database migration large memory",
                body,
            )
            self._write_memory(f"feedback_large_{i}.md", content)

        results = fetch_relevant_memories(
            "database-engineer",
            [],
            "database migration",
            max_chars=MAX_MEMORY_CHARS,
            memory_dir=self.memory_dir,
        )
        formatted = format_memories_section(results)
        self.assertLessEqual(len(formatted), MAX_MEMORY_CHARS)

    def test_returns_empty_when_memory_dir_missing(self):
        missing_dir = self.memory_dir / "nonexistent"
        results = fetch_relevant_memories(
            "backend-developer",
            [],
            "some instruction",
            memory_dir=missing_dir,
        )
        self.assertEqual(results, [])

    def test_path_traversal_guard_rejects_escape(self):
        # Create a legitimate memory file
        content = _feedback_file("Legit memory", "database dispatch", "body")
        self._write_memory("feedback_legit.md", content)

        # Create a symlink that escapes the memory dir
        import os
        escape_target = self.memory_dir / ".." / "escape.md"
        escape_link = self.memory_dir / "escape_link.md"
        # Write the target file
        (self.memory_dir.parent / "escape.md").write_text(
            _feedback_file("Escape", "escape attempt", "body"),
            encoding="utf-8",
        )
        try:
            os.symlink(str(escape_target.resolve()), str(escape_link))
        except (OSError, NotImplementedError):
            # Symlinks might not be supported in all test environments
            return

        # Clear cache so new scan picks up the symlink
        operator_memory_indexer._CACHES.clear()

        # fetch_relevant_memories should skip the symlink-based escape
        results = fetch_relevant_memories(
            "database-engineer",
            [],
            "database dispatch",
            memory_dir=self.memory_dir,
        )
        # All returned memories should be inside memory_dir
        for mem in results:
            self.assertTrue(
                str(mem.file_path.resolve()).startswith(str(self.memory_dir.resolve())),
                f"Memory file {mem.file_path} escapes memory_dir {self.memory_dir}",
            )

    def test_cache_refreshes_after_ttl(self):
        # Write initial memory
        content = _feedback_file("Initial memory", "database dispatch tip", "Initial body")
        self._write_memory("feedback_initial.md", content)

        results_1 = fetch_relevant_memories(
            "database-engineer", [], "database dispatch", memory_dir=self.memory_dir
        )
        self.assertEqual(len(results_1), 1)

        # Manually expire the cache
        cache_key = str(self.memory_dir.resolve())
        if cache_key in operator_memory_indexer._CACHES:
            operator_memory_indexer._CACHES[cache_key].loaded_at = 0.0

        # Write a second memory
        content2 = _feedback_file("Second memory", "database migration tip 2", "Second body")
        self._write_memory("feedback_second.md", content2)

        results_2 = fetch_relevant_memories(
            "database-engineer", [], "database dispatch", memory_dir=self.memory_dir
        )
        # After TTL expiry, should see both memories
        self.assertGreater(len(results_2), len(results_1))


class TestFormatSection(unittest.TestCase):
    def test_format_section_includes_anti_anchoring_instruction(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "feedback_test.md"
            content = _feedback_file("Test memory", "A test memory", "Test body content.")
            p.write_text(content, encoding="utf-8")
            mem = _parse_memory_file(p, content)

        formatted = format_memories_section([mem])
        self.assertIn("Anti-anchoring", formatted)
        self.assertIn("verify", formatted.lower())

    def test_format_section_returns_empty_for_no_memories(self):
        self.assertEqual(format_memories_section([]), "")

    def test_format_section_includes_memory_names(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "feedback_lease.md"
            content = _feedback_file(
                "Stale lease cleanup", "Check leases before dispatch", "Release old leases."
            )
            p.write_text(content, encoding="utf-8")
            mem = _parse_memory_file(p, content)

        formatted = format_memories_section([mem])
        self.assertIn("Stale lease cleanup", formatted)
        self.assertIn("[feedback]", formatted)

    def test_format_section_includes_description(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "feedback_x.md"
            content = _feedback_file("Name", "Specific description here", "Body text.")
            p.write_text(content, encoding="utf-8")
            mem = _parse_memory_file(p, content)

        formatted = format_memories_section([mem])
        self.assertIn("Specific description here", formatted)


class TestNoBodyInLogs(unittest.TestCase):
    def test_no_body_content_in_logs(self):
        """Memory body content must not appear in log output (privacy guard)."""
        PRIVATE_BODY = "PRIVATE_OPERATOR_CONTENT_XYZ_12345_SECRET"

        with tempfile.TemporaryDirectory() as td:
            memory_dir = Path(td)
            content = _feedback_file(
                "Private memory",
                "test privacy guard",
                PRIVATE_BODY,
            )
            (memory_dir / "feedback_private.md").write_text(content, encoding="utf-8")

            # Capture log output
            import io
            log_capture = io.StringIO()
            handler = logging.StreamHandler(log_capture)
            handler.setLevel(logging.DEBUG)

            root_logger = logging.getLogger()
            root_logger.addHandler(handler)
            old_level = root_logger.level
            root_logger.setLevel(logging.DEBUG)

            try:
                operator_memory_indexer._CACHES.clear()
                fetch_relevant_memories(
                    "backend-developer",
                    [],
                    "test privacy guard instruction",
                    memory_dir=memory_dir,
                )
            finally:
                root_logger.removeHandler(handler)
                root_logger.setLevel(old_level)

            log_output = log_capture.getvalue()
            self.assertNotIn(
                PRIVATE_BODY,
                log_output,
                "Memory body content appeared in log output — privacy violation",
            )


class TestOpenItemStatus(unittest.TestCase):
    """The live status of the OI a note names, read-only, unknown never open."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.memory_dir = root / "memory"
        self.memory_dir.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        (root / ".vnx-project-id").write_text("proj-a\n", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"VNX_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        operator_memory_indexer._CACHES.clear()
        lesson_refs = sys.modules.get("lesson_refs")
        if lesson_refs is not None:
            lesson_refs._INDEX_CACHE.clear()

    @property
    def lesson_refs(self):
        import lesson_refs

        return lesson_refs

    def _store(self, items=None, raw=None):
        text = raw if raw is not None else json.dumps({"items": items})
        (self.state / "open_items.json").write_text(text, encoding="utf-8")

    def _note(self, body="Note names OI-1 as the cause.", filename="a.md"):
        _make_memory_file(self.memory_dir, filename, _feedback_file(
            "Backend lesson", "backend dispatch lesson", body))

    def _section(self):
        mems = fetch_relevant_memories(
            "backend-developer", [], "backend dispatch lesson", memory_dir=self.memory_dir)
        self.assertEqual(len(mems), 1)
        return format_memories_section(mems)

    def test_open_item_status_closed_item_shows_date_and_pr(self):
        self._store([{"id": "OI-1", "status": "done", "closed_at": "2026-11-08T09:00:00", "pr_id": "2590"}])
        self._note()
        section = self._section()
        self.assertIn("OI-1: done", section)
        self.assertIn("- OI-1: done (gesloten 2026-11-08, #2590)", section)

    def test_open_item_status_open_item_shows_status_only(self):
        self._store([{"id": "OI-1", "status": "open"}])
        self._note()
        self.assertIn("- OI-1: open\n", self._section())

    def test_open_item_status_refs_parsed_from_body_only(self):
        mem = _parse_memory_file(Path("x.md"), _feedback_file("n", "d", "See OI-7 and OI-7, [[other]]."))
        self.assertEqual(mem.open_item_refs, frozenset({"OI-7"}))
        self.assertEqual(mem.open_item_status, ())
        fm = _parse_memory_file(Path("y.md"), "---\nname: n\ndescription: OI-9\nopen_item: OI-8\n---\nNo refs")
        self.assertEqual(fm.open_item_refs, frozenset())

    def _assert_unknown(self, reason):
        section = self._section()
        self.assertIn(f"- OI-1: onbekend ({reason})", section)
        self.assertNotIn("OI-1: open", section)

    def test_open_item_status_field_missing_stays_unknown(self):
        self._store([{"id": "OI-1"}])
        self._note()
        self._assert_unknown("store_unavailable")

    def test_open_item_status_none_stays_unknown(self):
        self._store([{"id": "OI-1", "status": None}])
        self._note()
        self._assert_unknown("store_unavailable")

    def test_open_item_status_corrupt_json_stays_unknown(self):
        self._store(raw="{corrupt")
        self._note()
        self._assert_unknown("store_unavailable")

    def test_open_item_status_unknown_value_stays_unknown(self):
        self._store([{"id": "OI-1", "status": "reopened"}])
        self._note()
        self._assert_unknown("store_unavailable")

    def test_open_item_status_no_project_id_stays_unknown(self):
        self._store([{"id": "OI-1", "status": "open"}])
        self._note()
        with mock.patch.object(operator_memory_indexer, "project_id_from_state_dir", return_value=""):
            with mock.patch.object(self.lesson_refs, "_read_store") as read:
                self._assert_unknown("project_unknown")
                read.assert_not_called()

    def test_open_item_status_store_absent_stays_unknown(self):
        self._note()
        self._assert_unknown("store_unavailable")

    def test_open_item_status_ref_not_in_store_stays_unknown(self):
        self._store([{"id": "OI-2", "status": "open"}])
        self._note()
        self._assert_unknown("not_in_store")

    def test_open_item_status_other_project_item_stays_unknown(self):
        self._store([{"id": "OI-1", "status": "open", "project_id": "proj-b"}])
        self._note()
        self._assert_unknown("not_in_store")

    def test_open_item_status_store_read_once_per_call(self):
        self._store([{"id": "OI-1", "status": "open"}, {"id": "OI-2", "status": "open"}])
        self._note("Names OI-1 and OI-2.", "a.md")
        self._note("Also names OI-2.", "b.md")
        with mock.patch.object(self.lesson_refs, "_read_store", wraps=self.lesson_refs._read_store) as spy:
            mems = fetch_relevant_memories(
                "backend-developer", [], "backend dispatch lesson", memory_dir=self.memory_dir)
        self.assertGreaterEqual(len(mems), 2)
        self.assertEqual(spy.call_count, 1)

    def test_open_item_status_note_without_refs_reads_nothing(self):
        self._store([{"id": "OI-1", "status": "open"}])
        self._note("No references in here.")
        with mock.patch.object(self.lesson_refs, "load_oi_index") as load, \
                mock.patch.object(self.lesson_refs, "_read_store") as read:
            mems = fetch_relevant_memories(
                "backend-developer", [], "backend dispatch lesson", memory_dir=self.memory_dir)
        self.assertEqual(len(mems), 1)
        load.assert_not_called()
        read.assert_not_called()
        self.assertNotIn("Open items named", format_memories_section(mems))

    def test_open_item_status_lines_count_toward_budget(self):
        items = [{"id": f"OI-{n}", "status": "open"} for n in range(1, 41)]
        self._store(items)
        body = "Short note " + " ".join(f"OI-{n}" for n in range(1, 41))
        self._note(body)
        without = fetch_relevant_memories(
            "backend-developer", [], "backend dispatch lesson", memory_dir=self.memory_dir, max_chars=10_000)
        self.assertEqual(len(without), 1)
        full = len(format_memories_section(without))
        tight = fetch_relevant_memories(
            "backend-developer", [], "backend dispatch lesson", memory_dir=self.memory_dir, max_chars=full - 1)
        self.assertEqual(tight, [])
