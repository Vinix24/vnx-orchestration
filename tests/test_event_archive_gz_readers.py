#!/usr/bin/env python3
"""OI-1944 C1: every archive reader opens ``.ndjson.gz``; footprint probe thresholds.

G1-G5 drive readers that exist at the base commit; P1-P2 drive the subsystem
aggregator. The helper tests at the bottom cover the new archive helper surface.
"""

import gzip
import io
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "dashboard"))

DAY = 86400


def _events(tag: str) -> list:
    return [
        {"type": "init", "dispatch_id": "x", "terminal": "T1", "timestamp": "2026-09-01T10:00:00+00:00",
         "data": {"tag": tag}},
        {"type": "tool_use", "timestamp": "2026-09-01T10:00:01+00:00",
         "data": {"name": "Read", "id": "t1", "input": {"file_path": "a.py"}}},
        {"type": "tool_result", "timestamp": "2026-09-01T10:00:02+00:00",
         "data": {"tool_use_id": "t1", "content": "ok"}},
    ]


def _ndjson(events: list) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


def _write_plain(path: Path, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_ndjson(events))


def _write_gz(path: Path, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as fh:
        fh.write(_ndjson(events))


def _handler():
    handler = MagicMock()
    handler.wfile = io.BytesIO()
    return handler


def _body(handler):
    return json.loads(handler.wfile.getvalue())


def _status(handler) -> int:
    return int(handler.send_response.call_args[0][0])


@pytest.fixture
def stream(tmp_path, monkeypatch):
    import api_agent_stream as mod
    from event_store import EventStore

    store = EventStore(events_dir=tmp_path / "events")
    monkeypatch.setattr(mod, "_store", store)
    return mod, store


# --------------------------------------------------------------------------
# G1 / G2 / G5: dashboard agent-stream
# --------------------------------------------------------------------------

def test_g1_agent_stream_list_shows_gz_only_archive(stream):
    mod, store = stream
    _write_gz(store.archive_dir("T1") / "x.ndjson.gz", _events("gz"))

    handler = _handler()
    mod.handle_agent_stream_archive_list(handler, "T1")

    entries = _body(handler)
    assert [e["dispatch_id"] for e in entries] == ["x"]
    assert entries[0]["file_size"] > 0


def test_g2_agent_stream_archive_serves_gz_events(stream):
    mod, store = stream
    _write_gz(store.archive_dir("T1") / "x.ndjson.gz", _events("gz"))

    handler = _handler()
    mod.handle_agent_stream_archive(handler, "T1", "x")

    assert _status(handler) == 200
    assert [e["type"] for e in _body(handler)] == ["init", "tool_use", "tool_result"]


def test_g5_agent_stream_plain_wins_over_gz(stream):
    mod, store = stream
    _write_plain(store.archive_dir("T1") / "x.ndjson", _events("plain"))
    _write_gz(store.archive_dir("T1") / "x.ndjson.gz", _events("gz"))

    handler = _handler()
    mod.handle_agent_stream_archive(handler, "T1", "x")
    assert _body(handler)[0]["data"]["tag"] == "plain"

    listing = _handler()
    mod.handle_agent_stream_archive_list(listing, "T1")
    assert [e["dispatch_id"] for e in _body(listing)] == ["x"]


def test_agent_stream_archive_missing_is_404(stream):
    mod, store = stream
    handler = _handler()
    mod.handle_agent_stream_archive(handler, "T1", "nope")
    assert _status(handler) == 404


def test_agent_stream_corrupt_gz_is_500_not_crash(stream):
    mod, store = stream
    bad = store.archive_dir("T1") / "x.ndjson.gz"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a gzip stream")

    handler = _handler()
    mod.handle_agent_stream_archive(handler, "T1", "x")

    assert _status(handler) == 500


# --------------------------------------------------------------------------
# G3 / G5: dashboard dispatch events
# --------------------------------------------------------------------------

def _sd(tmp_path: Path):
    sd = MagicMock()
    sd.VNX_DATA_DIR = tmp_path
    return sd


def test_g3_dispatch_events_serves_gz_archive(tmp_path):
    import api_intelligence

    _write_gz(tmp_path / "events" / "archive" / "T1" / "x.ndjson.gz", _events("gz"))

    with patch.object(api_intelligence, "_sd", return_value=_sd(tmp_path)):
        payload, status = api_intelligence._dispatch_get_events("x")

    assert status == 200
    assert payload["dispatch_id"] == "x"
    assert any(e.get("tool_name") == "Read" for e in payload["events"])


def test_g5_dispatch_events_plain_wins_over_gz(tmp_path):
    import api_intelligence

    arch = tmp_path / "events" / "archive" / "T1"
    plain = _events("plain")
    plain[1]["data"]["name"] = "Edit"
    _write_plain(arch / "x.ndjson", plain)
    _write_gz(arch / "x.ndjson.gz", _events("gz"))

    with patch.object(api_intelligence, "_sd", return_value=_sd(tmp_path)):
        payload, status = api_intelligence._dispatch_get_events("x")

    assert status == 200
    assert [e["tool_name"] for e in payload["events"] if e["type"] == "tool_use"] == ["Edit"]


def test_dispatch_events_unknown_is_404(tmp_path):
    import api_intelligence

    (tmp_path / "events" / "archive").mkdir(parents=True)
    with patch.object(api_intelligence, "_sd", return_value=_sd(tmp_path)):
        _, status = api_intelligence._dispatch_get_events("nope")
    assert status == 404


# --------------------------------------------------------------------------
# G4 / G5: event_analyzer
# --------------------------------------------------------------------------

def test_g4_analyze_all_counts_gz_archive(tmp_path):
    import event_analyzer

    _write_gz(tmp_path / "T1" / "x.ndjson.gz", _events("gz"))

    behaviors = event_analyzer.analyze_all(tmp_path)

    assert len(behaviors) == 1
    assert behaviors[0].dispatch_id == "x"
    assert behaviors[0].reads == 1


def test_g5_analyze_all_counts_once_when_both_exist(tmp_path):
    import event_analyzer

    _write_plain(tmp_path / "T1" / "x.ndjson", _events("plain"))
    _write_gz(tmp_path / "T1" / "x.ndjson.gz", _events("gz"))

    assert len(event_analyzer.analyze_all(tmp_path)) == 1


def test_event_analyzer_finds_gz_dispatch_archive(tmp_path):
    import event_analyzer

    _write_gz(tmp_path / "T1" / "x.ndjson.gz", _events("gz"))
    found = event_analyzer._find_dispatch_archive("x", tmp_path)
    assert found is not None and found.name == "x.ndjson.gz"


# --------------------------------------------------------------------------
# retroactive_backfill
# --------------------------------------------------------------------------

def test_backfill_archive_index_and_parse_read_gz(tmp_path, monkeypatch):
    import retroactive_backfill as rb

    events = _events("gz")
    events.append({"type": "tool_use", "timestamp": "2026-09-01T10:00:03+00:00",
                   "data": {"name": "Bash", "input": {"command": "git commit -m x"}}})
    _write_gz(tmp_path / "T1" / "x.ndjson.gz", events)
    monkeypatch.setattr(rb, "ARCHIVE_DIR", tmp_path)

    index = rb._build_archive_index()

    assert set(index) == {"x"}
    assert rb._parse_archive(index["x"]).committed is True


def test_backfill_archive_index_prefers_plain(tmp_path, monkeypatch):
    import retroactive_backfill as rb

    _write_plain(tmp_path / "T1" / "x.ndjson", _events("plain"))
    _write_gz(tmp_path / "T1" / "x.ndjson.gz", _events("gz"))
    monkeypatch.setattr(rb, "ARCHIVE_DIR", tmp_path)

    assert rb._build_archive_index()["x"].name == "x.ndjson"


# --------------------------------------------------------------------------
# P1 / P2: footprint probe through subsystem_health.aggregate
# --------------------------------------------------------------------------

def _plain_archive(store: Path, name: str, age_days: int, size: int = 100) -> Path:
    path = store / "events" / "archive" / "T1" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    ts = time.time() - age_days * DAY
    os.utime(path, (ts, ts))
    return path


def _aggregate(store: Path):
    import subsystem_health

    return subsystem_health.aggregate(state_dir=store, subsystems=["vnx-data-footprint"])


def _disk(monkeypatch, free_fraction: float):
    """Inject free disk at the stdlib boundary, so the probe's own code runs."""
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    total = 1000 * 1024 ** 3
    free = int(total * free_fraction)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(total, total - free, free))


def test_p1_old_plain_archive_is_degraded_and_writes_beacon(tmp_path, monkeypatch):
    store = tmp_path / "alpha"
    _plain_archive(store, "old.ndjson", age_days=17)
    _disk(monkeypatch, 0.5)

    result = _aggregate(store)["vnx-data-footprint"]

    assert result["status"] == "degraded"
    assert result["detail"]["plain_archive_files_older_than_window"] == 1
    beacon = json.loads((store / "health" / "vnx-data-footprint.json").read_text())
    assert beacon["status"] == "stale"


def test_p1_plain_inside_grace_window_is_ok(tmp_path, monkeypatch):
    store = tmp_path / "alpha"
    _plain_archive(store, "recent.ndjson", age_days=15)
    _disk(monkeypatch, 0.5)

    assert _aggregate(store)["vnx-data-footprint"]["status"] == "ok"


def test_p1_second_project_does_not_leak(tmp_path, monkeypatch):
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    _plain_archive(alpha, "same-id.ndjson", age_days=40)
    _plain_archive(beta, "same-id.ndjson", age_days=1)
    _disk(monkeypatch, 0.5)

    assert _aggregate(alpha)["vnx-data-footprint"]["status"] == "degraded"
    assert _aggregate(beta)["vnx-data-footprint"]["status"] == "ok"
    assert json.loads((beta / "health" / "vnx-data-footprint.json").read_text())["status"] == "ok"


def test_p2_free_disk_thresholds(tmp_path, monkeypatch):
    store = tmp_path / "alpha"
    gz = store / "events" / "archive" / "T1" / "old.ndjson.gz"
    _write_gz(gz, _events("gz"))
    ts = time.time() - 60 * DAY
    os.utime(gz, (ts, ts))

    _disk(monkeypatch, 0.50)
    assert _aggregate(store)["vnx-data-footprint"]["status"] == "ok"
    _disk(monkeypatch, 0.10)
    assert _aggregate(store)["vnx-data-footprint"]["status"] == "degraded"
    _disk(monkeypatch, 0.04)
    assert _aggregate(store)["vnx-data-footprint"]["status"] == "produces_crap"


def test_p2_archive_and_salvage_size_thresholds(tmp_path, monkeypatch):
    import vnx_data_footprint_probe as probe

    store = tmp_path / "alpha"
    _plain_archive(store, "new.ndjson", age_days=0, size=1000)
    _disk(monkeypatch, 0.5)
    monkeypatch.setattr(probe, "ARCHIVE_DEGRADED_BYTES", 500)
    monkeypatch.setattr(probe, "ARCHIVE_CRAP_BYTES", 5000)
    assert _aggregate(store)["vnx-data-footprint"]["status"] == "degraded"
    monkeypatch.setattr(probe, "ARCHIVE_CRAP_BYTES", 900)
    assert _aggregate(store)["vnx-data-footprint"]["status"] == "produces_crap"

    monkeypatch.setattr(probe, "ARCHIVE_DEGRADED_BYTES", 10 ** 9)
    monkeypatch.setattr(probe, "ARCHIVE_CRAP_BYTES", 10 ** 10)
    (store / "salvage").mkdir()
    (store / "salvage" / "blob").write_bytes(b"y" * 2000)
    monkeypatch.setattr(probe, "SALVAGE_DEGRADED_BYTES", 1500)
    assert _aggregate(store)["vnx-data-footprint"]["status"] == "degraded"


def test_p2_window_follows_env_knob(tmp_path, monkeypatch):
    store = tmp_path / "alpha"
    _plain_archive(store, "x.ndjson", age_days=10)
    _disk(monkeypatch, 0.5)
    assert _aggregate(store)["vnx-data-footprint"]["status"] == "ok"
    monkeypatch.setenv("VNX_EVENTS_ARCHIVE_COMPRESS_DAYS", "3")
    assert _aggregate(store)["vnx-data-footprint"]["status"] == "degraded"


def test_probe_ignores_symlinks_in_archive(tmp_path, monkeypatch):
    store = tmp_path / "alpha"
    outside = tmp_path / "outside.ndjson"
    outside.write_bytes(b"z" * 50)
    ts = time.time() - 90 * DAY
    os.utime(outside, (ts, ts))
    link = store / "events" / "archive" / "T1" / "link.ndjson"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    _disk(monkeypatch, 0.5)

    result = _aggregate(store)["vnx-data-footprint"]
    assert result["status"] == "ok"
    assert result["detail"]["archive_bytes"] == 0


# --------------------------------------------------------------------------
# New surface: archive helper
# --------------------------------------------------------------------------

def test_helper_resolve_prefers_plain_then_gz(tmp_path):
    from event_store import resolve_archive_file

    assert resolve_archive_file(tmp_path, "x") is None
    _write_gz(tmp_path / "x.ndjson.gz", _events("gz"))
    assert resolve_archive_file(tmp_path, "x").name == "x.ndjson.gz"
    _write_plain(tmp_path / "x.ndjson", _events("plain"))
    assert resolve_archive_file(tmp_path, "x").name == "x.ndjson"


def test_helper_archive_id_strips_both_suffixes():
    from event_store import archive_id, is_archive_file

    assert archive_id("T1/a.b.ndjson.gz") == "a.b"
    assert archive_id("a.ndjson") == "a"
    assert is_archive_file("a.ndjson.gz") and not is_archive_file("a.ndjson.tmp")


def test_helper_list_ignores_tmp_and_dedups(tmp_path):
    from event_store import list_archive_files

    _write_plain(tmp_path / "a.ndjson", _events("p"))
    _write_gz(tmp_path / "a.ndjson.gz", _events("g"))
    _write_gz(tmp_path / "b.ndjson.gz", _events("g"))
    (tmp_path / "c.ndjson.gz.tmp").write_bytes(b"partial")
    assert [p.name for p in list_archive_files(tmp_path)] == ["a.ndjson", "b.ndjson.gz"]


def test_helper_find_exact_beats_substring(tmp_path):
    from event_store import find_archive_file

    _write_plain(tmp_path / "T1" / "x-long.ndjson", _events("p"))
    _write_gz(tmp_path / "T2" / "x.ndjson.gz", _events("g"))
    assert find_archive_file(tmp_path, "x").name == "x.ndjson.gz"
    assert find_archive_file(tmp_path, "long").name == "x-long.ndjson"
    assert find_archive_file(tmp_path / "missing", "x") is None
