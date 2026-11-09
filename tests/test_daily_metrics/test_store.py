"""Day-store tests for the daily-metrics track (PR-1b).

The test package ``test_daily_metrics`` never shadows the library ``daily_metrics``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_store_module_is_importable():
    """The store submodule exists: on a tree without it this fails on an assert."""
    assert importlib.util.find_spec("daily_metrics") is not None
    assert importlib.util.find_spec("daily_metrics.store") is not None


@pytest.fixture()
def store_mod():
    from daily_metrics import store as module

    return module


def _day_doc(day: str = "2026-11-06", status: str = "final") -> dict:
    record = {"kind": "scalar", "value": None, "source": "daily_metrics.ledger(deferred)",
              "baseline_7d": None, "unknown_reason": "ledger-is-falsifiable not merged",
              "sample_size": 0, "details": {}}
    return {"project_id": "proj", "day": day, "status": status, "revision": 1,
            "collected_at": "2026-11-08T03:10:00Z", "as_of": "2026-11-07T23:59:59Z",
            "reason": None, "metrics": {"merges_without_receipt_count": record}}


def test_write_final_is_immutable(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    doc = _day_doc()
    path = store.write_final("2026-11-06", doc)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(store_mod.AlreadyFinal):
        store.write_final("2026-11-06", {**doc, "metrics": {}})
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_revision_requires_reason_and_wins(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    doc = _day_doc()
    store.write_final("2026-11-06", doc)
    with pytest.raises(ValueError):
        store.write_revision("2026-11-06", doc, reason="   ")
    assert store.write_revision("2026-11-06", doc, reason="recollect").name == "2026-11-06.r2.json"
    assert store.load("2026-11-06")["revision"] == 2
    assert (tmp_path / "2026-11-06.json").exists()


def test_none_value_reads_back(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    store.write_incomplete("2026-11-06", _day_doc(status="incomplete"))
    record = store.load("2026-11-06")["metrics"]["merges_without_receipt_count"]
    assert record["value"] is None
    assert record["unknown_reason"] == "ledger-is-falsifiable not merged"


def test_write_incomplete_replaces_until_final(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    store.write_incomplete("2026-11-06", _day_doc(status="incomplete"))
    store.write_incomplete("2026-11-06", _day_doc(status="incomplete"))
    assert store.is_final("2026-11-06") is False
    store.write_final("2026-11-06", _day_doc())
    assert store.is_final("2026-11-06") is True
    with pytest.raises(store_mod.AlreadyFinal):
        store.write_incomplete("2026-11-06", _day_doc(status="incomplete"))


def test_list_days_and_missing_load(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    assert store.load("2026-11-06") is None
    assert store.list_days() == []
    store.write_final("2026-11-07", _day_doc(day="2026-11-07"))
    store.write_final("2026-11-06", _day_doc())
    assert store.list_days() == ["2026-11-06", "2026-11-07"]


def test_malformed_day_is_refused(store_mod, tmp_path):
    with pytest.raises(ValueError):
        store_mod.DayStore(tmp_path, "proj").write_final("../escape", _day_doc())


# --- the store keeps its immutability promise under real concurrency ---------
#
# The workers are real OS processes (not threads) that all call the real store
# method, released together by a start file so the calls really overlap. A
# check-then-replace store lets two of them see "free" and lose a revision.

_WORKER_SCRIPT = r'''
import json, sys, time
from pathlib import Path

root, day, idx, lib, start, kind = sys.argv[1:7]
sys.path.insert(0, lib)

from daily_metrics.store import AlreadyFinal, DayStore

root = Path(root)
worker = int(idx)
(root / f"ready-{worker}").write_text("", encoding="utf-8")
start = Path(start)
while not start.exists():
    time.sleep(0.001)

store = DayStore(root, "proj")
doc = {"worker": worker, "status": "incomplete" if kind == "incomplete" else "final",
       "marker": f"w{worker}" * 512}
try:
    if kind == "revision":
        path = store.write_revision(day, doc, reason="race")
    elif kind == "final":
        path = store.write_final(day, doc)
    else:
        path = store.write_incomplete(day, doc)
    print(json.dumps({"ok": True, "worker": worker, "name": path.name}))
except AlreadyFinal:
    print(json.dumps({"ok": False, "worker": worker, "error": "AlreadyFinal"}))
'''


def _race(store_mod, root: Path, day: str, kinds: list[str]) -> list[dict]:
    """One process per kind, all released by a start file; return their JSON results."""
    lib_dir = str(Path(store_mod.__file__).resolve().parent.parent)
    start = root / "start-race"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER_SCRIPT, str(root), day, str(index), lib_dir,
             str(start), kind],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for index, kind in enumerate(kinds)
    ]
    deadline = time.monotonic() + 10
    for index in range(len(kinds)):
        ready = root / f"ready-{index}"
        while not ready.exists():
            assert time.monotonic() < deadline, f"worker {index} never reached the barrier"
            time.sleep(0.005)
    start.write_text("", encoding="utf-8")
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=10)
        assert proc.returncode == 0, f"worker failed: {err}"
        results.append(json.loads(out.strip().splitlines()[-1]))
    return results


def test_eight_processes_write_distinct_revisions(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    store.write_final("2026-11-06", _day_doc())
    results = _race(store_mod, tmp_path, "2026-11-06", ["revision"] * 8)
    assert all(result["ok"] for result in results), results
    names = sorted(result["name"] for result in results)
    assert names == [f"2026-11-06.r{n}.json" for n in range(2, 10)]
    payloads = [json.loads((tmp_path / name).read_text()) for name in names]
    assert sorted(payload["worker"] for payload in payloads) == list(range(8))
    assert [payload["revision"] for payload in payloads] == list(range(2, 10))
    assert store.load("2026-11-06")["revision"] == 9


def test_eight_processes_write_final_only_one_wins(store_mod, tmp_path):
    results = _race(store_mod, tmp_path, "2026-11-06", ["final"] * 8)
    winners = [result for result in results if result["ok"]]
    losers = [result for result in results if not result["ok"]]
    assert len(winners) == 1, results
    assert len(losers) == 7, results
    assert all(result["error"] == "AlreadyFinal" for result in losers)
    winner = winners[0]["worker"]
    text = (tmp_path / "2026-11-06.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["worker"] == winner
    assert payload["status"] == "final"
    assert payload["marker"] == f"w{winner}" * 512
    for loser in losers:
        assert f"w{loser['worker']}" * 512 not in text


@pytest.mark.parametrize("round_index", range(4))
def test_final_and_incomplete_never_end_incomplete(store_mod, tmp_path, round_index):
    day = f"2026-11-{10 + round_index:02d}"
    results = _race(store_mod, tmp_path, day, ["final", "incomplete"])
    final_result = results[0]
    assert final_result["ok"] is True, results
    if not results[1]["ok"]:
        assert results[1]["error"] == "AlreadyFinal"
    assert store_mod.DayStore(tmp_path, "proj").load(day)["status"] == "final"


def test_failed_publish_leaves_no_temp_file(store_mod, tmp_path, monkeypatch):
    store = store_mod.DayStore(tmp_path, "proj")

    def refuse(*args, **kwargs):
        raise OSError("link refused")

    monkeypatch.setattr(store_mod.os, "link", refuse)
    with pytest.raises(OSError):
        store.write_final("2026-11-06", _day_doc())
    with pytest.raises(OSError):
        store.write_revision("2026-11-06", _day_doc(), reason="boom")
    assert not (tmp_path / "2026-11-06.json").exists()
    assert [entry.name for entry in tmp_path.iterdir() if entry.name.endswith(".tmp")] == []


def test_failed_incomplete_publish_leaves_no_temp_file(store_mod, tmp_path, monkeypatch):
    store = store_mod.DayStore(tmp_path, "proj")
    store.write_incomplete("2026-11-06", _day_doc(status="incomplete"))

    def refuse(*args, **kwargs):
        raise OSError("replace refused")

    monkeypatch.setattr(store_mod.os, "replace", refuse)
    with pytest.raises(OSError):
        store.write_incomplete("2026-11-06", _day_doc(status="incomplete"))
    assert store.load("2026-11-06")["status"] == "incomplete"
    assert [entry.name for entry in tmp_path.iterdir() if entry.name.endswith(".tmp")] == []


def test_readers_ignore_lock_files(store_mod, tmp_path):
    store = store_mod.DayStore(tmp_path, "proj")
    store.write_final("2026-11-06", _day_doc())
    assert (tmp_path / "2026-11-06.lock").exists()
    assert store.list_days() == ["2026-11-06"]
    assert store.load("2026-11-06")["revision"] == 1
    assert store.is_final("2026-11-06") is True
