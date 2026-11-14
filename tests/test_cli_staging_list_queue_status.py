"""tests/test_cli_staging_list_queue_status.py — OI-2828.

``vnx staging-list`` lists the staged spec bundles (pending/<id>/dispatch-spec.json),
``vnx queue-status`` reads the horizon queue. Both run as real ``bin/vnx``
subprocesses against a temporary data dir.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BIN = _ROOT / "bin" / "vnx"
for _p in (_ROOT / "scripts", _ROOT / "scripts" / "lib", _ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from test_horizon_queue import _insert_dispatch, _seed_tracks  # noqa: E402
from test_planning_deliverables import _build_db  # noqa: E402

PROJECT_ID = "test-proj"  # the project test_horizon_queue seeds its rows under


def _env(data_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.update({
        "VNX_DATA_DIR": str(data_dir),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(data_dir / "state"),
        "VNX_DISPATCH_DIR": str(data_dir / "dispatches"),
        "VNX_DATA_HOME": str(data_dir.parent / "_vnx_account_home"),
        "VNX_PROJECT_ID": PROJECT_ID,
    })
    return env


def _vnx(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_BIN), *args], capture_output=True, text=True, timeout=60,
        env=_env(data_dir), cwd=str(_ROOT),
    )


def _bundle(data_dir: Path, dispatch_id: str, mtime: float, **fields) -> None:
    d = data_dir / "dispatches" / "pending" / dispatch_id
    d.mkdir(parents=True)
    spec = {"dispatch_id": dispatch_id, "role": "backend-developer", "provider": "claude",
            "model": "sonnet", "pr_id": None, "parent_dispatch": None, "track_id": None}
    spec.update(fields)
    path = d / "dispatch-spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    os.utime(path, (mtime, mtime))


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def test_lists_two_bundles_and_one_invalid_oldest_first(data_dir):
    _bundle(data_dir, "dlv-newer", 2_000_000_000, track_id="trk-b", pr_id="PR-2")
    _bundle(data_dir, "dlv-older", 1_900_000_000, track_id="trk-a", parent_dispatch="dlv-root")
    bad = data_dir / "dispatches" / "pending" / "dlv-broken"
    bad.mkdir()
    (bad / "dispatch-spec.json").write_text("{not json", encoding="utf-8")
    os.utime(bad / "dispatch-spec.json", (1_950_000_000, 1_950_000_000))

    result = _vnx(data_dir, "staging-list")
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(lines) == 3
    assert [ln.split()[0] for ln in lines] == ["dlv-older", "dlv-broken", "dlv-newer"]
    assert "track=trk-a" in lines[0] and "parent=dlv-root" in lines[0]
    assert "engine=claude/sonnet" in lines[0]
    assert "INVALID" in lines[1]
    assert "pr=PR-2" in lines[2]


def test_json_round_trips(data_dir):
    _bundle(data_dir, "dlv-one", 1_900_000_000, track_id="trk-a")
    _bundle(data_dir, "dlv-two", 1_950_000_000, pr_id="PR-9")
    result = _vnx(data_dir, "staging-list", "--json")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [r["dispatch_id"] for r in rows] == ["dlv-one", "dlv-two"]
    assert rows[0]["track_id"] == "trk-a" and rows[1]["pr_id"] == "PR-9"
    assert {"role", "provider", "model", "parent_dispatch", "age_seconds"} <= set(rows[0])


def test_json_reports_invalid_file_with_error(data_dir):
    d = data_dir / "dispatches" / "pending" / "dlv-list"
    d.mkdir(parents=True)
    (d / "dispatch-spec.json").write_text("[1, 2]", encoding="utf-8")
    rows = json.loads(_vnx(data_dir, "staging-list", "--json").stdout)
    assert len(rows) == 1 and "error" in rows[0]


def test_help_exits_zero_without_traceback(data_dir):
    result = _vnx(data_dir, "staging-list", "--help")
    assert result.returncode == 0
    assert "Usage" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


def test_unknown_argument_is_refused(data_dir):
    result = _vnx(data_dir, "staging-list", "--bogus")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_no_pending_dir_gives_no_bundles_line(data_dir):
    result = _vnx(data_dir, "staging-list")
    assert result.returncode == 0, result.stderr
    assert "No staged spec bundles" in result.stdout
    assert len([ln for ln in result.stdout.splitlines() if ln.strip()]) == 1


def test_queue_status_prints_horizon_queue(tmp_path, data_dir):
    state_dir = _build_db(tmp_path)
    _seed_tracks(state_dir)
    _insert_dispatch(state_dir, "dlv-000000000001", state="ready",
                     output_ref="pr:dlv-000000000001", title="Queued thing")
    result = _vnx(
        data_dir, "queue-status", "--project-id", PROJECT_ID, "--state-dir", str(state_dir),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Unknown command" not in result.stdout + result.stderr
    assert f"VNX queue — project '{PROJECT_ID}'" in result.stdout
    assert "dlv-000000000001" in result.stdout
