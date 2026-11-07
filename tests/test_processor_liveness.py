"""tests/test_processor_liveness.py -- the five heartbeat states (ledger PR 1).

``processor_liveness`` is imported inside each test so that, on a tree where
the module does not exist yet, the failure is the assertion on the state and
not a collection-time ImportError.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
FILE = "receipt_processor_heartbeat.json"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _heartbeat(pid: int, age_seconds: float = 5.0, **overrides: Any) -> Dict[str, Any]:
    stamp = _iso(NOW - timedelta(seconds=age_seconds))
    body: Dict[str, Any] = {
        "schema": 1, "pid": pid, "cycle": 7, "started_at": stamp, "updated_at": stamp,
        "last_progress_at": stamp, "last_cycle_seconds": 0.4, "watermark_hash": "",
        "converter": {"rejected": 0, "malformed": 0, "errors": 0},
    }
    body.update(overrides)
    return body


def _write(state_dir: Path, body: Any) -> None:
    text = body if isinstance(body, str) else json.dumps(body)
    (state_dir / FILE).write_text(text, encoding="utf-8")


def _read(state_dir: Path) -> Dict[str, Any]:
    from processor_liveness import read_processor_liveness
    return read_processor_liveness(state_dir, now=NOW)


@pytest.fixture
def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_stalled_when_heartbeat_older_than_window_with_live_pid(tmp_path: Path) -> None:
    _write(tmp_path, _heartbeat(os.getpid(), age_seconds=120))
    result = _read(tmp_path)
    assert result["state"] == "stalled"
    assert result["overall"] == "fail"


def test_dead_pid_is_dead_even_with_a_fresh_heartbeat(tmp_path: Path, dead_pid: int) -> None:
    _write(tmp_path, _heartbeat(dead_pid, age_seconds=1))
    assert _read(tmp_path)["state"] == "dead"


def test_dead_pid_is_dead_regardless_of_age(tmp_path: Path, dead_pid: int) -> None:
    _write(tmp_path, _heartbeat(dead_pid, age_seconds=3600))
    result = _read(tmp_path)
    assert (result["state"], result["overall"]) == ("dead", "fail")


def test_fresh_with_live_pid(tmp_path: Path) -> None:
    _write(tmp_path, _heartbeat(os.getpid(), age_seconds=10))
    result = _read(tmp_path)
    assert (result["state"], result["overall"]) == ("fresh", "ok")


def test_window_boundary_is_inclusive(tmp_path: Path) -> None:
    from processor_liveness import WINDOW_SECONDS
    _write(tmp_path, _heartbeat(os.getpid(), age_seconds=WINDOW_SECONDS))
    assert _read(tmp_path)["state"] == "fresh"
    _write(tmp_path, _heartbeat(os.getpid(), age_seconds=WINDOW_SECONDS + 1))
    assert _read(tmp_path)["state"] == "stalled"


def test_absent_without_file(tmp_path: Path) -> None:
    result = _read(tmp_path)
    assert (result["state"], result["overall"]) == ("absent", "unknown")


def test_permission_error_on_kill_means_alive(tmp_path: Path) -> None:
    _write(tmp_path, _heartbeat(4242, age_seconds=5))
    with mock.patch("processor_liveness.os.kill", side_effect=PermissionError):
        assert _read(tmp_path)["state"] == "fresh"
    _write(tmp_path, _heartbeat(4242, age_seconds=500))
    with mock.patch("processor_liveness.os.kill", side_effect=PermissionError):
        assert _read(tmp_path)["state"] == "stalled"


def test_clock_skew_more_than_30s_ahead_is_unknown(tmp_path: Path) -> None:
    _write(tmp_path, _heartbeat(os.getpid(), age_seconds=-31))
    result = _read(tmp_path)
    assert (result["state"], result["overall"]) == ("unknown", "unknown")


def test_small_future_drift_within_30s_is_still_fresh(tmp_path: Path) -> None:
    _write(tmp_path, _heartbeat(os.getpid(), age_seconds=-20))
    assert _read(tmp_path)["state"] == "fresh"


def test_clock_skew_wins_over_a_dead_pid(tmp_path: Path, dead_pid: int) -> None:
    _write(tmp_path, _heartbeat(dead_pid, age_seconds=-600))
    assert _read(tmp_path)["state"] == "unknown"


@pytest.mark.parametrize("field", ["started_at", "updated_at", "last_progress_at"])
@pytest.mark.parametrize("bad", [None, "", "not a time", 12345])
def test_unparseable_or_missing_time_field_is_unknown(tmp_path: Path, field: str, bad: Any) -> None:
    body = _heartbeat(os.getpid())
    body[field] = bad
    _write(tmp_path, body)
    assert _read(tmp_path)["state"] == "unknown"


@pytest.mark.parametrize("field", ["started_at", "updated_at", "last_progress_at"])
def test_deleted_time_field_is_unknown(tmp_path: Path, field: str) -> None:
    body = _heartbeat(os.getpid())
    del body[field]
    _write(tmp_path, body)
    assert _read(tmp_path)["state"] == "unknown"


@pytest.mark.parametrize("schema", [None, 0, 2, "1", True])
def test_unknown_schema_is_unknown(tmp_path: Path, schema: Any) -> None:
    _write(tmp_path, _heartbeat(os.getpid(), schema=schema))
    assert _read(tmp_path)["state"] == "unknown"


@pytest.mark.parametrize("pid", [None, 0, -5, "123", 1.5, True])
def test_bad_pid_is_unknown(tmp_path: Path, pid: Any) -> None:
    body = _heartbeat(os.getpid())
    body["pid"] = pid
    _write(tmp_path, body)
    assert _read(tmp_path)["state"] == "unknown"


@pytest.mark.parametrize("raw", ["", "{not json", "[]", "null", '"text"', "42"])
def test_garbage_content_is_unknown(tmp_path: Path, raw: str) -> None:
    _write(tmp_path, raw)
    assert _read(tmp_path)["state"] == "unknown"


def test_unreadable_file_is_unknown(tmp_path: Path) -> None:
    (tmp_path / FILE).mkdir()  # a directory in place of the file: read raises OSError
    assert _read(tmp_path)["state"] == "unknown"


def test_naive_timestamp_is_read_as_utc(tmp_path: Path) -> None:
    naive = (NOW - timedelta(seconds=5)).replace(tzinfo=None).isoformat()
    _write(tmp_path, _heartbeat(os.getpid(), updated_at=naive))
    assert _read(tmp_path)["state"] == "fresh"


def test_freshness_comes_from_the_field_not_the_mtime(tmp_path: Path) -> None:
    _write(tmp_path, _heartbeat(os.getpid(), age_seconds=600))
    os.utime(tmp_path / FILE, (NOW.timestamp(), NOW.timestamp()))  # mtime says "just now"
    assert _read(tmp_path)["state"] == "stalled"


def test_default_now_is_the_wall_clock(tmp_path: Path) -> None:
    from processor_liveness import read_processor_liveness
    stamp = _iso(datetime.now(timezone.utc))
    _write(tmp_path, _heartbeat(os.getpid(), started_at=stamp, updated_at=stamp, last_progress_at=stamp))
    assert read_processor_liveness(tmp_path)["state"] == "fresh"


def test_state_dir_is_explicit_not_ambient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _write(other, _heartbeat(os.getpid()))
    monkeypatch.setenv("VNX_STATE_DIR", str(other))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _read(empty)["state"] == "absent"


def test_custom_window_is_honoured(tmp_path: Path) -> None:
    from processor_liveness import read_processor_liveness
    _write(tmp_path, _heartbeat(os.getpid(), age_seconds=20))
    assert read_processor_liveness(tmp_path, now=NOW, window_seconds=10)["state"] == "stalled"
    assert read_processor_liveness(tmp_path, now=NOW, window_seconds=30)["state"] == "fresh"
