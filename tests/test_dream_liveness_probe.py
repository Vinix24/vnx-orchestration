"""Tests for scripts/dream_liveness_probe.py (ADR-066, dream PR-7).

Everything runs under tmp_path with the sources read from fixtures: the probe
neither reads the real launchd domain nor writes the real central store.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dream_liveness_probe as probe

PROJECT = "vnx-dev"
PLIST = {"path": "p", "label": "com.vnx.auto-dream", "project_id": PROJECT,
         "hour": 3, "minute": 0, "arm_switch": "1"}


def _blocked_skip_events():
    stamp = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    return [{"event_type": "dream_cycle_skipped", "cycle_id": "c-1", "timestamp": stamp,
             "reason": "probe_not_ok"}]


@pytest.fixture
def root(tmp_path, monkeypatch):
    data_root = tmp_path / ".vnx-data" / PROJECT
    data_root.mkdir(parents=True)
    monkeypatch.setattr(probe, "resolve_central_data_dir", lambda pid: data_root)
    monkeypatch.setattr(probe.dream_liveness, "load_sources",
                        lambda pid, data_root=None, plist_path=None:
                        {"project_id": pid, "events": _blocked_skip_events(), "plist": PLIST})
    return data_root


def _tree(path: Path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def test_json_prints_the_document(root, capsys):
    assert probe.main(["--project-id", PROJECT, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "dream_liveness/1"


def test_without_flags_nothing_is_written(root):
    assert probe.main(["--project-id", PROJECT]) == 0
    assert _tree(root) == []


def test_strict_exits_1_on_a_blocked_skip(root, capsys):
    assert probe.main(["--project-id", PROJECT, "--json", "--strict"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["state"] != "ok"
    assert "blocked_skip" in {f["id"] for f in document["findings"]}


def test_append_adds_exactly_one_line_and_the_beacon(root):
    assert probe.main(["--project-id", PROJECT, "--append"]) == 0
    assert probe.main(["--project-id", PROJECT, "--append"]) == 0
    lines = (root / "state" / "dream_liveness.ndjson").read_text().splitlines()
    assert len(lines) == 2 and all(json.loads(x)["schema"] == "dream_liveness/1" for x in lines)
    beacon = json.loads((root / "health" / "dream_liveness.json").read_text())
    assert beacon["component"] == "dream_liveness"
    assert beacon["expected_interval_seconds"] == 90000
    assert beacon["status"] == "fail"  # the blocked-skip fixture is not ok: never a favourable default


def test_append_writes_only_the_two_paths_of_d15(root):
    probe.main(["--project-id", PROJECT, "--append"])
    written = {p for p in _tree(root) if (root / p).is_file()}
    assert written == {"state/dream_liveness.ndjson", "health/dream_liveness.json",
                       "health/dream_liveness.json.lock"}
    assert not (root / "state" / "health").exists()


def test_a_beacon_reader_finds_the_beacon_after_one_append(root):
    from health_beacon import all_beacons
    probe.main(["--project-id", PROJECT, "--append"])
    beacons = all_beacons(root)
    assert "dream_liveness" in beacons
    assert beacons["dream_liveness"]["status"] == "fail"


def test_unwritable_state_dir_is_exit_1_and_no_beacon(root, capsys):
    state = root / "state"
    state.mkdir()
    state.chmod(0o500)
    try:
        if os.access(state, os.W_OK):
            pytest.skip("running as a user that ignores directory modes")
        assert probe.main(["--project-id", PROJECT, "--append"]) == 1
    finally:
        state.chmod(0o700)
    assert "write failed" in capsys.readouterr().err
    assert not (root / "health" / "dream_liveness.json").exists()


def test_beacon_failure_after_the_append_is_exit_1(root, monkeypatch, capsys):
    def boom(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(probe.HealthBeacon, "heartbeat_strict", boom)
    assert probe.main(["--project-id", PROJECT, "--append"]) == 1
    assert "disk full" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["../x", "a/b", ""])
def test_rejected_project_id_is_exit_2(bad, monkeypatch, capsys):
    def reject(pid, **k):
        raise ValueError("bad id")
    monkeypatch.setattr(probe.dream_liveness, "load_sources", reject)
    assert probe.main(["--project-id", bad, "--append"]) == 2
