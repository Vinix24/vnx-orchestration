#!/usr/bin/env python3
"""Tests for lesson_refs.py: reference grammar, read-only OI index, resolver."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import lesson_refs
from lesson_refs import (
    BIN_ROUTING,
    find_memory_refs,
    find_oi_refs,
    load_oi_index,
    resolve_oi_refs,
)


def make_state(root: Path, items=None, *, project_id: str = "proj-a", raw: str = None) -> Path:
    """A state dir with a .vnx-project-id above it and an optional open_items.json."""
    (root / ".vnx-project-id").write_text(project_id + "\n", encoding="utf-8")
    state = root / "state"
    state.mkdir()
    if raw is not None:
        (state / "open_items.json").write_text(raw, encoding="utf-8")
    elif items is not None:
        (state / "open_items.json").write_text(json.dumps({"items": items}), encoding="utf-8")
    return state


class TestGrammar(unittest.TestCase):
    def test_find_oi_refs_sorted_deduplicated(self):
        self.assertEqual(find_oi_refs("OI-20 en OI-3, OI-20 nog eens"), ["OI-20", "OI-3"])

    def test_find_oi_refs_rejects_leading_zero_and_embedded(self):
        self.assertEqual(find_oi_refs("OI-0 OI-007 XOI-5 OI-5x"), [])

    def test_find_oi_refs_empty_and_none(self):
        self.assertEqual(find_oi_refs(""), [])
        self.assertEqual(find_oi_refs(None), [])

    def test_find_memory_refs_drops_alias_and_duplicates(self):
        text = "zie [[naam-een]] en [[naam-een|alias]] en [[naam-twee#kop]]"
        self.assertEqual(find_memory_refs(text), ["naam-een", "naam-twee"])

    def test_find_memory_refs_none(self):
        self.assertEqual(find_memory_refs(None), [])
        self.assertEqual(find_memory_refs("[[]] [[ ]]"), [])

    def test_bin_routing_has_the_four_record_types(self):
        self.assertEqual(
            set(BIN_ROUTING), {"open_item", "memory_note", "build_log_nugget", "learning_loop"}
        )
        for row in BIN_ROUTING.values():
            self.assertTrue(row["home"] and row["why"])


class TestLoadIndex(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        lesson_refs._INDEX_CACHE.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def test_ok_returns_status_closed_at_and_pr(self):
        state = make_state(self.root, [
            {"id": "OI-1", "status": "done", "closed_at": "2026-11-08T10:00:00", "pr_id": "2590"},
        ])
        index, st = load_oi_index(state)
        self.assertEqual(st, "ok")
        self.assertEqual(index["OI-1"], {"status": "done", "closed_at": "2026-11-08T10:00:00", "pr_id": "2590"})

    def test_absent_file(self):
        self.assertEqual(load_oi_index(make_state(self.root)), ({}, "absent"))

    def test_corrupt_json_is_error(self):
        self.assertEqual(load_oi_index(make_state(self.root, raw="{not json"))[1], "error")

    def test_items_not_a_list_is_error(self):
        self.assertEqual(load_oi_index(make_state(self.root, raw='{"items": 5}'))[1], "error")

    def test_top_level_not_a_dict_is_error(self):
        self.assertEqual(load_oi_index(make_state(self.root, raw="[]"))[1], "error")

    def test_no_project_id_is_error(self):
        state = self.root / "state"
        state.mkdir()
        (state / "open_items.json").write_text('{"items": []}', encoding="utf-8")
        with mock.patch.object(lesson_refs, "project_id_from_state_dir", return_value=""):
            self.assertEqual(load_oi_index(state), ({}, "error"))

    def test_other_project_item_excluded_own_and_unstamped_kept(self):
        state = make_state(self.root, [
            {"id": "OI-1", "status": "open", "project_id": "proj-b"},
            {"id": "OI-2", "status": "open", "project_id": "proj-a"},
            {"id": "OI-3", "status": "open"},
        ])
        index, _ = load_oi_index(state)
        self.assertEqual(sorted(index), ["OI-2", "OI-3"])

    def test_malformed_items_skipped(self):
        state = make_state(self.root, ["x", {"status": "open"}, {"id": 5}, {"id": "OI-9", "status": "open"}])
        index, st = load_oi_index(state)
        self.assertEqual((sorted(index), st), (["OI-9"], "ok"))

    def test_cached_on_mtime_and_reread_after_change(self):
        state = make_state(self.root, [{"id": "OI-1", "status": "open"}])
        with mock.patch.object(lesson_refs, "_read_store", wraps=lesson_refs._read_store) as spy:
            load_oi_index(state)
            load_oi_index(state)
            self.assertEqual(spy.call_count, 1)
            (state / "open_items.json").write_text(
                json.dumps({"items": [{"id": "OI-1", "status": "done", "pad": "x" * 9}]}), encoding="utf-8")
            index, _ = load_oi_index(state)
            self.assertEqual(spy.call_count, 2)
        self.assertEqual(index["OI-1"]["status"], "done")


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.index = {"OI-1": {"status": "done", "closed_at": "2026-11-08T10:00:00", "pr_id": "2590"}}

    def test_resolved_known_status(self):
        r = resolve_oi_refs(["OI-1"], self.index, "ok")["OI-1"]
        self.assertTrue(r["resolved"])
        self.assertEqual((r["status"], r["pr_id"]), ("done", "2590"))

    def test_not_in_store(self):
        r = resolve_oi_refs(["OI-2"], self.index, "ok")["OI-2"]
        self.assertEqual((r["resolved"], r["status"], r["reason"]), (False, None, "not_in_store"))

    def test_store_states_other_than_ok_are_unavailable(self):
        for state in ("absent", "error", None, "", "weird"):
            r = resolve_oi_refs(["OI-1"], self.index, state)["OI-1"]
            self.assertEqual((r["resolved"], r["reason"]), (False, "store_unavailable"), state)

    def test_project_unknown(self):
        r = resolve_oi_refs(["OI-1"], self.index, "project_unknown")["OI-1"]
        self.assertEqual((r["resolved"], r["reason"]), (False, "project_unknown"))

    def test_missing_none_unknown_or_non_string_status_never_resolves(self):
        for entry in ({}, {"status": None}, {"status": "reopened"}, {"status": 5}, {"status": ["open"]}, None):
            r = resolve_oi_refs(["OI-1"], {"OI-1": entry}, "ok")["OI-1"]
            self.assertFalse(r["resolved"], entry)
            self.assertNotEqual(r["status"], "open")

    def test_index_not_a_dict_is_unavailable(self):
        r = resolve_oi_refs(["OI-1"], None, "ok")["OI-1"]
        self.assertEqual((r["resolved"], r["reason"]), (False, "store_unavailable"))


if __name__ == "__main__":
    unittest.main()
