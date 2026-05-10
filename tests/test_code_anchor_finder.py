#!/usr/bin/env python3
"""Tests for code_anchor_finder.py (Wave 5 P2)."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from code_anchor_finder import (
    MAX_ANCHORS_PER_FILE,
    MAX_CODE_ANCHOR_CHARS,
    MAX_FILES,
    ANCHOR_CONTEXT_LINES,
    CodeAnchor,
    extract_terms,
    fetch_code_anchors,
    format_code_anchors_section,
)


class TestExtractTerms(unittest.TestCase):
    def test_extract_terms_filters_stopwords(self):
        text = "should verify these before after check never would"
        terms = extract_terms(text)
        for stopword in ("should", "verify", "these", "before", "after", "check", "never", "would"):
            self.assertNotIn(stopword, terms)

    def test_extract_terms_keeps_camel_and_snake(self):
        text = "edit _import_table and IntelligenceSelector with fetch_code_anchors"
        terms = extract_terms(text)
        self.assertIn("_import_table", terms)
        self.assertIn("IntelligenceSelector", terms)
        self.assertIn("fetch_code_anchors", terms)

    def test_extract_terms_deduplicates(self):
        text = "fetch_code_anchors uses fetch_code_anchors to do fetch_code_anchors stuff"
        terms = extract_terms(text)
        count = sum(1 for t in terms if t == "fetch_code_anchors")
        self.assertEqual(count, 1)

    def test_extract_terms_minimum_length(self):
        text = "ab abc abcd abcde"
        terms = extract_terms(text)
        for t in terms:
            self.assertGreaterEqual(len(t), 4)

    def test_extract_terms_empty_input(self):
        self.assertEqual(extract_terms(""), [])

    def test_extract_terms_returns_list(self):
        result = extract_terms("fetch_code_anchors IntelligenceSelector")
        self.assertIsInstance(result, list)


class TestFetchCodeAnchors(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_file(self, name: str, content: str, mtime: float | None = None) -> Path:
        p = self._base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        if mtime is not None:
            os.utime(str(p), (mtime, mtime))
        return p

    def test_fetch_anchors_finds_function_def(self):
        src = "\n".join([
            "import os",
            "",
            "def _import_table(conn, table_name):",
            "    conn.execute(f'INSERT OR IGNORE INTO {table_name}')",
            "    return True",
        ])
        p = self._make_file("myscript.py", src)
        rel = str(p.relative_to(self._base))

        anchors = fetch_code_anchors(
            [rel],
            "edit _import_table and INSERT OR IGNORE logic",
            repo_root=self._base,
        )
        self.assertTrue(len(anchors) > 0)
        found = any("_import_table" in a.body or "INSERT" in a.body for a in anchors)
        self.assertTrue(found)

    def test_fetch_anchors_caps_at_max_anchors_per_file(self):
        # File with many matches for the same identifier
        lines = [f"def func_{i}():" for i in range(30)]
        lines += [f"    fetch_code_anchors call at line {i}" for i in range(30)]
        src = "\n".join(lines)
        p = self._make_file("many.py", src)
        rel = str(p.relative_to(self._base))

        instruction = " ".join(["fetch_code_anchors"] * 5)
        anchors = fetch_code_anchors([rel], instruction, repo_root=self._base)
        file_anchors = [a for a in anchors if a.file_path == rel]
        self.assertLessEqual(len(file_anchors), MAX_ANCHORS_PER_FILE)

    def test_fetch_anchors_caps_at_max_files(self):
        # Create 8 files, all matching
        files = []
        for i in range(8):
            src = f"def my_function_{i}():\n    migrate_table_data()\n"
            p = self._make_file(f"file_{i}.py", src)
            files.append(str(p.relative_to(self._base)))

        anchors = fetch_code_anchors(
            files,
            "migrate_table_data my_function",
            repo_root=self._base,
        )
        touched_files = {a.file_path for a in anchors}
        self.assertLessEqual(len(touched_files), MAX_FILES)

    def test_fetch_anchors_skips_nonexistent_path(self):
        anchors = fetch_code_anchors(
            ["nonexistent/path/foo.py", "also_missing.py"],
            "fetch_code_anchors migrate_table",
            repo_root=self._base,
        )
        self.assertEqual(anchors, [])

    def test_fetch_anchors_skips_binary_files(self):
        binary_content = b"GIF89a\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04"
        p = self._base / "image.gif"
        p.write_bytes(binary_content)
        rel = str(p.relative_to(self._base))

        anchors = fetch_code_anchors([rel], "migrate_table_data", repo_root=self._base)
        self.assertEqual(anchors, [])

    def test_fetch_anchors_budget_truncates_at_max_chars(self):
        # Create a file with many matches for a very long matched body
        line_filler = "x" * 80
        lines = []
        for i in range(50):
            lines.append(f"def _import_table_{i}():")
            for j in range(10):
                lines.append(f"    {line_filler}_{j}")
        src = "\n".join(lines)
        p = self._make_file("big.py", src)
        rel = str(p.relative_to(self._base))

        anchors = fetch_code_anchors(
            [rel],
            "_import_table operation",
            max_chars=MAX_CODE_ANCHOR_CHARS,
            repo_root=self._base,
        )
        formatted = format_code_anchors_section(anchors)
        self.assertLessEqual(len(formatted), MAX_CODE_ANCHOR_CHARS)

    def test_anchor_body_includes_context_lines(self):
        src_lines = [f"line_{i}" for i in range(30)]
        # Put _import_table at line 15 (0-based index 14)
        src_lines[14] = "def _import_table(conn):"
        src = "\n".join(src_lines)
        p = self._make_file("ctx.py", src)
        rel = str(p.relative_to(self._base))

        anchors = fetch_code_anchors(
            [rel],
            "_import_table usage",
            repo_root=self._base,
        )
        self.assertTrue(len(anchors) > 0)
        # Anchor should span from at least line 10 to line 20 (1-based)
        anchor = anchors[0]
        self.assertLessEqual(anchor.line_start, 15)
        self.assertGreaterEqual(anchor.line_end, 15)

    def test_recency_tiebreak_newer_mtime_first(self):
        old_time = time.time() - 3600
        new_time = time.time()

        old_file = self._make_file("old.py", "def migrate_schema():\n    pass\n", mtime=old_time)
        new_file = self._make_file("new.py", "def migrate_schema():\n    pass\n", mtime=new_time)

        rel_old = str(old_file.relative_to(self._base))
        rel_new = str(new_file.relative_to(self._base))

        # Pass old before new in dispatch_paths order
        anchors = fetch_code_anchors(
            [rel_old, rel_new],
            "migrate_schema operation",
            repo_root=self._base,
        )
        if len(anchors) >= 2:
            # Newer file should appear first
            first_file = anchors[0].file_path
            self.assertEqual(first_file, rel_new)

    def test_fetch_anchors_empty_dispatch_paths(self):
        self.assertEqual(fetch_code_anchors([], "some instruction", repo_root=self._base), [])

    def test_fetch_anchors_empty_instruction(self):
        p = self._make_file("x.py", "def foo():\n    pass\n")
        rel = str(p.relative_to(self._base))
        self.assertEqual(fetch_code_anchors([rel], "", repo_root=self._base), [])

    def test_fetch_anchors_no_term_matches(self):
        src = "def completely_different_name():\n    pass\n"
        p = self._make_file("nomatch.py", src)
        rel = str(p.relative_to(self._base))
        anchors = fetch_code_anchors([rel], "fetch_code_anchors _import_table", repo_root=self._base)
        self.assertEqual(anchors, [])


class TestFormatCodeAnchorsSection(unittest.TestCase):
    def test_format_section_includes_anti_anchoring_instruction(self):
        anchor = CodeAnchor(
            file_path="scripts/foo.py",
            line_start=10,
            line_end=20,
            matched_term="_import_table",
            body="def _import_table():\n    pass",
        )
        section = format_code_anchors_section([anchor])
        self.assertIn("Anti-anchoring notice", section)
        self.assertIn("re-read the file", section)

    def test_format_section_empty_returns_empty_string(self):
        self.assertEqual(format_code_anchors_section([]), "")

    def test_format_section_includes_file_path_and_lines(self):
        anchor = CodeAnchor(
            file_path="scripts/lib/migrate.py",
            line_start=100,
            line_end=110,
            matched_term="INSERT_OR_IGNORE",
            body="INSERT OR IGNORE INTO table_name",
        )
        section = format_code_anchors_section([anchor])
        self.assertIn("scripts/lib/migrate.py:100-110", section)
        self.assertIn("INSERT_OR_IGNORE", section)
        self.assertIn("INSERT OR IGNORE INTO table_name", section)

    def test_format_section_includes_code_block(self):
        anchor = CodeAnchor(
            file_path="scripts/foo.py",
            line_start=1,
            line_end=5,
            matched_term="fetch_rows",
            body="def fetch_rows():\n    return []",
        )
        section = format_code_anchors_section([anchor])
        self.assertIn("```", section)

    def test_format_section_multiple_anchors(self):
        anchors = [
            CodeAnchor("a.py", 1, 5, "func_a", "def func_a(): pass"),
            CodeAnchor("b.py", 10, 15, "func_b", "def func_b(): pass"),
        ]
        section = format_code_anchors_section(anchors)
        self.assertIn("a.py", section)
        self.assertIn("b.py", section)
        self.assertIn("func_a", section)
        self.assertIn("func_b", section)


if __name__ == "__main__":
    unittest.main()
