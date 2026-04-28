"""Tests for scripts/lib/dispatch_register.py — append-only lifecycle NDJSON."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
LIB_DIR = VNX_ROOT / "scripts" / "lib"
REGISTER_MODULE = LIB_DIR / "dispatch_register.py"

sys.path.insert(0, str(LIB_DIR))


def _env_with_data_dir(tmp_path: Path) -> dict:
    env = os.environ.copy()
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    env["VNX_DATA_DIR"] = str(data_dir)
    return env


def _import_register(tmp_path: Path):
    """Import dispatch_register with VNX_DATA_DIR pointed at tmp_path."""
    import importlib
    import importlib.util
    env_backup = os.environ.get("VNX_DATA_DIR")
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    os.environ["VNX_DATA_DIR"] = str(data_dir)
    spec = importlib.util.spec_from_file_location("dispatch_register", REGISTER_MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, env_backup


def test_append_valid_event_returns_true_and_persists(tmp_path: Path):
    mod, backup = _import_register(tmp_path)
    try:
        result = mod.append_event("dispatch_promoted", dispatch_id="abc-123", terminal="T1")
        assert result is True
        reg_path = tmp_path / "data" / "state" / "dispatch_register.ndjson"
        assert reg_path.exists()
        lines = [l for l in reg_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "dispatch_promoted"
        assert rec["dispatch_id"] == "abc-123"
        assert rec["terminal"] == "T1"
        assert "timestamp" in rec
    finally:
        if backup is None:
            os.environ.pop("VNX_DATA_DIR", None)
        else:
            os.environ["VNX_DATA_DIR"] = backup


def test_append_invalid_event_returns_false(tmp_path: Path):
    mod, backup = _import_register(tmp_path)
    try:
        result = mod.append_event("not_a_real_event", dispatch_id="x")
        assert result is False
        reg_path = tmp_path / "data" / "state" / "dispatch_register.ndjson"
        assert not reg_path.exists()
    finally:
        if backup is None:
            os.environ.pop("VNX_DATA_DIR", None)
        else:
            os.environ["VNX_DATA_DIR"] = backup


def test_read_events_returns_chronological_list(tmp_path: Path):
    mod, backup = _import_register(tmp_path)
    try:
        mod.append_event("dispatch_created", dispatch_id="d1")
        mod.append_event("dispatch_promoted", dispatch_id="d1", terminal="T1")
        mod.append_event("dispatch_completed", dispatch_id="d1")
        events = mod.read_events()
        assert len(events) == 3
        assert events[0]["event"] == "dispatch_created"
        assert events[1]["event"] == "dispatch_promoted"
        assert events[2]["event"] == "dispatch_completed"
    finally:
        if backup is None:
            os.environ.pop("VNX_DATA_DIR", None)
        else:
            os.environ["VNX_DATA_DIR"] = backup


def test_read_events_since_iso_filter(tmp_path: Path):
    mod, backup = _import_register(tmp_path)
    try:
        mod.append_event("dispatch_created", dispatch_id="d1")
        # Capture a timestamp mid-sequence
        import datetime
        mid_ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        mod.append_event("dispatch_promoted", dispatch_id="d1", terminal="T2")
        events = mod.read_events(since_iso=mid_ts)
        # Only events with timestamp >= mid_ts should appear
        assert all(e["event"] != "dispatch_created" or e["timestamp"] >= mid_ts for e in events)
        assert any(e["event"] == "dispatch_promoted" for e in events)
    finally:
        if backup is None:
            os.environ.pop("VNX_DATA_DIR", None)
        else:
            os.environ["VNX_DATA_DIR"] = backup


def test_read_events_missing_file_returns_empty(tmp_path: Path):
    mod, backup = _import_register(tmp_path)
    try:
        events = mod.read_events()
        assert events == []
    finally:
        if backup is None:
            os.environ.pop("VNX_DATA_DIR", None)
        else:
            os.environ["VNX_DATA_DIR"] = backup


def test_cli_append_writes_correct_record(tmp_path: Path):
    env = _env_with_data_dir(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(REGISTER_MODULE),
            "append",
            "dispatch_promoted",
            "dispatch_id=test-123",
            "terminal=T1",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    reg_path = tmp_path / "data" / "state" / "dispatch_register.ndjson"
    assert reg_path.exists()
    lines = [l for l in reg_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "dispatch_promoted"
    assert rec["dispatch_id"] == "test-123"
    assert rec["terminal"] == "T1"


def test_cli_unknown_event_exits_nonzero(tmp_path: Path):
    env = _env_with_data_dir(tmp_path)
    result = subprocess.run(
        [sys.executable, str(REGISTER_MODULE), "append", "bogus_event"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0


def test_concurrent_appends_no_corruption(tmp_path: Path):
    env = _env_with_data_dir(tmp_path)

    def _append_one(i: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(REGISTER_MODULE),
                "append",
                "dispatch_created",
                f"dispatch_id=d{i:03d}",
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    n = 20
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_append_one, range(n)))

    assert all(r.returncode == 0 for r in results)

    reg_path = tmp_path / "data" / "state" / "dispatch_register.ndjson"
    lines = [l for l in reg_path.read_text().splitlines() if l.strip()]
    assert len(lines) == n
    # All lines must be valid JSON
    for line in lines:
        rec = json.loads(line)
        assert rec["event"] == "dispatch_created"
