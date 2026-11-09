"""ADR-056 zelfst PR-7b: the autonomy verdict and the write-once outcome marker.

Dispatch-ID: dlv-93807e274bb5

Stores live under ``tmp_path``; the real central store is never read.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import cohort_outcome as co  # noqa: E402
from test_operator_cohort import PROJECT, add_delivery, add_event, make_store  # noqa: E402

SCRIPT = ROOT / "scripts" / "receipt_query.py"
ADR = ROOT / "docs" / "governance" / "decisions" / "ADR-056-zelfstandigheid-meten.md"
FIVE = {"vnx-dev", "seocrawler", "mission-control", "sales-copilot", "pacompany"}


def run(*args: str, home: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_") or k == "VNX_HOME"}
    env["VNX_DATA_HOME"] = str(home)
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


@pytest.fixture()
def store(tmp_path: Path):
    home = tmp_path / "home"
    sd = make_store(tmp_path / "data" / "state")
    sd_mc = make_store(home / "mission-control" / "state", "mission-control")
    add_delivery(sd_mc, "dlv-0000000000b1", project="mission-control")
    add_event(sd_mc, "deliverable_promoted", "dlv-0000000000b1", "t0", project="mission-control")
    make_store(home / "pacompany" / "state", "pacompany")
    add_delivery(sd, "dlv-000000000001")
    add_event(sd, "deliverable_promoted", "dlv-000000000001", "t0")
    return home, sd


def outcome(store, *extra: str) -> subprocess.CompletedProcess:
    home, sd = store
    return run("operator-outcome", "--project", PROJECT, "--state-dir", str(sd), *extra, home=home)


@pytest.mark.parametrize("n,zero,unknown,want", [
    (49, 1.0, 0.0, "onvoldoende_meting"),
    (50, 1.0, 0.21, "onvoldoende_meting"),
    (50, 1.0, 0.20, "selfstandig"),
    (50, 0.80, 0.0, "selfstandig"),
    (50, 0.79, 0.0, "niet_selfstandig"),
    (50, None, 0.0, "onvoldoende_meting"),
    (50, 1.0, None, "onvoldoende_meting"),
    (None, 1.0, 0.0, "onvoldoende_meting"),
    (50, "0.9", 0.0, "onvoldoende_meting"),
    (50, True, 0.0, "onvoldoende_meting"),
    (50, 1.5, 0.0, "onvoldoende_meting"),
    (50, float("nan"), 0.0, "onvoldoende_meting"),
    (True, 1.0, 0.0, "onvoldoende_meting"),
])
def test_verdict(n, zero, unknown, want):
    out = co.verdict(n, zero, unknown)
    assert out["verdict"] == want and out["reason"]


def test_thresholds_match_adr_table():
    text = ADR.read_text(encoding="utf-8")
    table = text.split("## Drempel", 1)[1].split("\n## ", 1)[0]
    assert f"`N < {co.MIN_COHORT}`" in table
    assert f"`unknown_rate` > {co.UNKNOWN_RATE_CEILING:.2f}" in table
    assert f"`zero_rate` >= {co.ZERO_RATE_FLOOR:.2f}" in table
    assert re.search(rf"`N >= {co.MIN_COHORT}`", table)


def test_marker_is_write_once(store):
    first = outcome(store, "--write-marker", "t1")
    assert first.returncode == 0, first.stderr
    path = store[1] / "autonomy_markers" / "t1.json"
    before = path.read_bytes()

    second = outcome(store, "--write-marker", "t1")

    assert second.returncode != 0
    assert path.read_bytes() == before


def test_marker_holds_every_field(store):
    assert outcome(store, "--write-marker", "t2").returncode == 0
    marker = json.loads((store[1] / "autonomy_markers" / "t2.json").read_text())

    assert marker["written_once"] is True and marker["project_id"] == PROJECT
    assert marker["period"] == {"since": "2026-10-30T00:00:00Z", "until": "2026-11-09T00:00:00Z"}
    assert marker["N"] == 1 and marker["zero_rate"] == 1.0 and marker["unknown_rate"] == 0.0
    assert marker["thresholds"] == {"min_cohort": 50, "zero_rate_floor": 0.8, "unknown_rate_ceiling": 0.2}
    assert marker["verdict"] == "onvoldoende_meting" and marker["reason"]
    assert marker["end_source"] == "dispatches.state"
    assert set(marker["coverage"]) == FIVE
    assert marker["coverage"]["mission-control"]["state"] == "uncovered"
    assert marker["coverage"]["pacompany"]["state"] == "no_data"
    assert re.fullmatch(r"[0-9a-f]{40}", marker["code_sha"]) and marker["written_at"].endswith("Z")


def test_default_name_and_second_name(store):
    assert outcome(store, "--write-marker").returncode == 0
    assert (store[1] / "autonomy_markers" / "zelfstandigheid-2026-10-30--2026-11-09.json").is_file()
    assert outcome(store, "--write-marker", "other").returncode == 0
    assert len(list((store[1] / "autonomy_markers").glob("*.json"))) == 2


def test_no_write_without_flag_and_json_shape(store):
    proc = outcome(store, "--json")
    out = json.loads(proc.stdout)
    assert proc.returncode == 0 and out["verdict"] == "onvoldoende_meting" and "marker" not in out
    assert not (store[1] / "autonomy_markers").exists()


def test_marker_name_must_be_plain(store):
    proc = outcome(store, "--write-marker", "../evil")
    assert proc.returncode != 0
    assert not (store[1].parent / "evil.json").exists()


def test_unmeasured_store_writes_no_marker(tmp_path):
    sd = tmp_path / "state"
    (sd).mkdir()
    (sd / "runtime_coordination.db").mkdir()
    proc = run("operator-outcome", "--project", PROJECT, "--state-dir", str(sd), "--write-marker", "x",
               home=tmp_path / "home")
    assert proc.returncode == 1 and not (sd / "autonomy_markers").exists()


def test_interrupted_write_leaves_no_partial_marker(tmp_path, monkeypatch):
    def boom(fd):
        raise OSError("disk gone")
    monkeypatch.setattr(co.os, "fsync", boom)

    with pytest.raises(OSError):
        co.write_marker(tmp_path, "m", {"a": 1})

    assert list((tmp_path / co.MARKER_DIR).iterdir()) == []


def test_existing_marker_survives_a_second_writer(tmp_path):
    co.write_marker(tmp_path, "m", {"a": 1})
    final = tmp_path / co.MARKER_DIR / "m.json"
    before = final.read_bytes()

    with pytest.raises(co.MarkerExists):
        co.write_marker(tmp_path, "m", {"a": 2})

    assert final.read_bytes() == before
    assert [p.name for p in final.parent.iterdir()] == ["m.json"]
