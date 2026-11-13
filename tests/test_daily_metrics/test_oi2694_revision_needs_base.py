"""OI-2694: the day store refuses a revision of a day that has no base file."""

from __future__ import annotations

import pytest

from daily_metrics import store as store_mod

from .test_store import _day_doc

DAY = "2026-11-01"


def test_empty_store_refuses_revision_and_writes_nothing(tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    with pytest.raises(store_mod.NoBaseFile, match=DAY):
        store.write_revision(DAY, _day_doc(DAY), "reason")
    assert list(tmp_path.glob("*.json")) == []


def test_final_base_accepts_revision(tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    store.write_final(DAY, _day_doc(DAY))
    path = store.write_revision(DAY, _day_doc(DAY), "reason")
    assert path.name == f"{DAY}.r2.json"
    assert store.load(DAY)["revision"] == 2
    assert store.list_days() == [DAY]


def test_incomplete_base_accepts_revision(tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    store.write_incomplete(DAY, _day_doc(DAY, "incomplete"))
    path = store.write_revision(DAY, _day_doc(DAY, "incomplete"), "reason")
    assert path.name == f"{DAY}.r2.json"
