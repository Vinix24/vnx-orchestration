"""OI-2814: ``vnx main-window status`` reads the central store the merge door reads."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import config_store_db  # noqa: E402
import main_window as mw  # noqa: E402
import main_window_cli as cli  # noqa: E402
import pr_merge  # noqa: E402
import vnx_paths  # noqa: E402

PID = "vnx-dev"
WINDOW = mw.MainWindow(verdict=mw.VERDICT_RUNNING, base_sha="c" * 40, base_run_id=1,
                       entries=(), first_red=None, reason=None)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    for key in (mw.UNVERIFIED_MAX_KEY, mw.RUN_MAX_AGE_KEY):
        monkeypatch.delenv(key, raising=False)
    central = tmp_path / "central" / "state"
    central.mkdir(parents=True)
    (tmp_path / "central" / ".vnx-project-id").write_text(PID + "\n", encoding="utf-8")
    local = tmp_path / "repo" / ".vnx-data" / "state"
    local.mkdir(parents=True)
    conn = sqlite3.connect(central / "runtime_coordination.db")
    config_store_db.set_config(conn, PID, mw.UNVERIFIED_MAX_KEY, 1, actor="test", approval_id="test-approval")
    conn.close()
    sqlite3.connect(local / "runtime_coordination.db").close()
    monkeypatch.setattr(vnx_paths, "ensure_env", lambda: {"VNX_STATE_DIR": str(central)})
    monkeypatch.setattr(mw, "read_window", lambda *a, **k: WINDOW)
    monkeypatch.setattr(pr_merge, "read_window", lambda *a, **k: WINDOW)
    monkeypatch.setattr(pr_merge, "_target_root", lambda: tmp_path)
    monkeypatch.setattr(pr_merge, "ensure_env", lambda: {"VNX_STATE_DIR": str(central)})
    return tmp_path, central, local


def _status(root, **kwargs):
    lines = []
    code = cli.run_status(True, project_root=root, out=lines.append, **kwargs)
    return code, json.loads("\n".join(lines))


def test_status_without_state_dir_reads_the_central_store(stores):
    root, _central, _local = stores
    _code, record = _status(root)
    assert record["mode"] == "on"
    assert record["max_unverified"] == 1


def test_status_and_the_door_agree(stores):
    root, _central, _local = stores
    _code, record = _status(root)
    door = pr_merge._read_main_admission()
    assert (record["mode"], record["max_unverified"]) == (door["mode"], door["max_unverified"])
    assert door["mode"] == "on"


def test_an_explicit_state_dir_still_wins(stores):
    root, _central, local = stores
    _code, record = _status(root, state_dir=local, project_id=PID)
    assert record["mode"] == "off"


def test_active_store_names_no_project_for_an_unmarked_dir(tmp_path, monkeypatch):
    bare = tmp_path / "bare" / "state"
    bare.mkdir(parents=True)
    monkeypatch.setattr(vnx_paths, "ensure_env", lambda: {"VNX_STATE_DIR": str(bare)})
    assert mw.active_store() == (bare, None)
