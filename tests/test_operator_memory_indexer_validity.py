#!/usr/bin/env python3
"""ADR-078 M3: the operator memory indexer honours invalid_at, valid_from, superseded_by."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import operator_memory_indexer as omi

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)


def _note(directory: Path, filename: str, extra: str = "", *, nested: bool = False,
          raw_frontmatter: str = "") -> None:
    if nested:
        extra = "metadata:\n" + "".join(f"  {l}\n" for l in extra.splitlines())
    fm = raw_frontmatter or f"name: {filename[:-3]}\ndescription: indexer validity widget\n{extra}"
    (directory / filename).write_text(f"---\n{fm.rstrip()}\n---\nbody widget\n", encoding="utf-8")


class ValidityBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def fetch(self):
        warnings: list = []
        got = omi.fetch_relevant_memories(
            None, [], "indexer validity widget", memory_dir=self.dir, now=NOW, warnings=warnings
        )
        return {m.file_path.name for m in got}, warnings

    def kinds(self, warnings):
        return sorted(w["kind"] for w in warnings)


class TestRules(ValidityBase):
    def test_a_invalid_at_in_the_past_or_equal_to_now_is_left_out(self):
        _note(self.dir, "a.md", "invalid_at: 2026-11-14T12:00:00Z")
        self.assertEqual(self.fetch(), (set(), []))

    def test_a_invalid_at_in_the_future_stays(self):
        _note(self.dir, "a.md", "invalid_at: 2026-11-14T12:00:01Z")
        self.assertEqual(self.fetch()[0], {"a.md"})

    def test_b_valid_from_in_the_future_is_left_out(self):
        _note(self.dir, "b.md", "valid_from: 2026-11-14T12:00:01Z")
        self.assertEqual(self.fetch()[0], set())

    def test_c_valid_successor_leaves_the_old_note_out_and_is_not_added(self):
        _note(self.dir, "old.md", "superseded_by: new.md")
        _note(self.dir, "new.md")
        self.assertEqual(self.fetch(), ({"new.md"}, []))  # only the successor, scored itself

    def test_c_chain_of_ten_links_reaches_valid_successor(self):
        _note(self.dir, "n0.md", "superseded_by: n1.md")
        for i in range(1, 10):
            _note(self.dir, f"n{i}.md", f"invalid_at: 2026-01-01\nsuperseded_by: n{i + 1}.md")
        _note(self.dir, "n10.md")
        self.assertEqual(self.fetch(), ({"n10.md"}, []))


class TestDates(ValidityBase):
    def test_date_without_time_is_midnight_utc(self):
        _note(self.dir, "a.md", "invalid_at: 2026-11-14")  # 00:00Z, before NOW
        _note(self.dir, "b.md", "invalid_at: 2026-11-15")  # tomorrow
        _note(self.dir, "c.md", "valid_from: 2026-11-15")
        self.assertEqual(self.fetch()[0], {"b.md"})

    def test_every_unreadable_form_counts_as_unset_with_a_warning(self):
        bad = ["2026-11-14T10:00:00", "2026-11-14T10:00:00+02:00", "14-11-2026",
               "2026-13-45", "yesterday", "12345", "[2026, 11]"]
        for value in bad:
            with self.subTest(value=value):
                for old in self.dir.glob("*.md"):
                    old.unlink()
                _note(self.dir, "u.md", f"invalid_at: {value}")
                self.assertEqual(self.fetch(), ({"u.md"}, [
                    {"kind": "unreadable_date", "note": "u.md", "field": "invalid_at"}]))


class TestWarningsKeepNoteValid(ValidityBase):
    def test_cycle(self):
        # a valid successor ends a chain, so a cycle needs out-of-force links
        _note(self.dir, "a.md", "superseded_by: b.md")
        _note(self.dir, "b.md", "invalid_at: 2026-01-01\nsuperseded_by: c.md")
        _note(self.dir, "c.md", "invalid_at: 2026-01-01\nsuperseded_by: a.md")
        names, w = self.fetch()
        self.assertEqual((names, self.kinds(w)), ({"a.md"}, ["cycle"]))

    def test_missing_or_path_successor_counts_as_missing(self):
        for i, target in enumerate(["gone.md", "../other/new.md", "sub/new.md"]):
            _note(self.dir, f"a{i}.md", f"superseded_by: {target}")
        names, w = self.fetch()
        self.assertEqual(names, {"a0.md", "a1.md", "a2.md"})
        self.assertEqual(self.kinds(w), ["successor_missing"] * 3)

    def test_chain_of_eleven(self):
        _note(self.dir, "n0.md", "superseded_by: n1.md")
        for i in range(1, 11):
            _note(self.dir, f"n{i}.md", f"invalid_at: 2026-01-01\nsuperseded_by: n{i + 1}.md")
        _note(self.dir, "n11.md")
        self.assertEqual(self.fetch(), ({"n0.md", "n11.md"}, [{"kind": "chain_too_long", "note": "n0.md"}]))

    def test_successor_itself_out_of_force(self):
        _note(self.dir, "old.md", "superseded_by: new.md")
        _note(self.dir, "new.md", "invalid_at: 2026-01-01")
        names, w = self.fetch()
        self.assertEqual((names, self.kinds(w)), ({"old.md"}, ["successor_not_valid"]))


class TestFrontmatterLocation(ValidityBase):
    def test_metadata_field_is_honoured(self):
        _note(self.dir, "a.md", "invalid_at: 2026-01-01", nested=True)
        self.assertEqual(self.fetch()[0], set())

    def test_metadata_wins_over_top_level(self):
        _note(self.dir, "a.md", raw_frontmatter=(
            "name: a\ndescription: indexer validity widget\n"
            "invalid_at: 2026-01-01\nmetadata:\n  invalid_at: 2027-01-01"))
        self.assertEqual(self.fetch()[0], {"a.md"})

    def test_metadata_not_a_mapping_falls_back_to_top_level(self):
        _note(self.dir, "a.md", raw_frontmatter=(
            "name: a\ndescription: indexer validity widget\n"
            "metadata: plain\ninvalid_at: 2026-01-01"))
        self.assertEqual(self.fetch()[0], set())

    def test_unparseable_and_absent_frontmatter_stay_valid(self):
        _note(self.dir, "bad.md", raw_frontmatter=(
            "name: bad\ndescription: indexer validity widget: broken\ninvalid_at: 2026-01-01"))
        (self.dir / "none.md").write_text("indexer validity widget body\n", encoding="utf-8")
        names, w = self.fetch()
        self.assertIn("bad.md", names)
        self.assertEqual(w, [])


if __name__ == "__main__":
    unittest.main()
