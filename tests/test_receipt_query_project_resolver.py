"""OI-1928 — all six lookup subcommands of receipt_query resolve their project.

``by-dispatch``, ``by-pr``, ``since``, ``by-track``, ``digest`` and
``reconcile-oi-pending`` used to default ``--project-id`` to a hardcoded
``vnx-dev`` when ``VNX_PROJECT_ID`` was unset, so a store of another project
showed the ``vnx-dev`` view. They now resolve like ``open-outcomes``: explicit
``--project-id``, else derived from ``--state-dir``, else ``VNX_PROJECT_ID``,
else a refusal (exit 2). Everything runs on a tmp HOME and tmp store.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import receipt_query as rq  # noqa: E402

OTHER = "other-project"
TWIN = "vnx-dev"
SINCE = "2026-07-01T00:00:00Z"
SIX = ["by-dispatch", "by-pr", "since", "by-track", "digest", "reconcile-oi-pending"]


def _receipt(project_id: str) -> Dict[str, Any]:
    return {
        "dispatch_id": "d-1",
        "terminal_id": "T2",
        "status": "success",
        "pr_id": "42",
        "timestamp": "2026-07-22T01:00:00Z",
        "project_id": project_id,
        "warnings": [{
            "code": "worker_permission_violation",
            "severity": "blocker",
            "message": "worker wrote outside declared scope",
            "destination": "oi_pending",
            "oi_id": None,
            "reason": "store lock held",
            "requires_tracking": True,
        }] if project_id == OTHER else [],
    }


def _make_store(home: Path, project_id: str, receipts: List[Dict[str, Any]]) -> Path:
    state_dir = home / ".vnx-data" / project_id / "state"
    state_dir.mkdir(parents=True)
    (state_dir / rq.LEDGER_NAME).write_text(
        "".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
    conn = sqlite3.connect(str(state_dir / rq.RUNTIME_COORDINATION_DB_NAME))
    try:
        conn.execute(
            "CREATE TABLE dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL, track TEXT, "
            "UNIQUE(dispatch_id, project_id))")
        for pid in (OTHER, TWIN):
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, track) VALUES (?, ?, ?)",
                ("d-1", pid, "track-a"))
        conn.commit()
    finally:
        conn.close()
    return state_dir


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store of ``other-project`` holding its own receipt and a ``vnx-dev`` twin."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    return _make_store(home, OTHER, [_receipt(OTHER), _receipt(TWIN)])


@pytest.fixture
def unresolvable(tmp_path, monkeypatch):
    """A store whose path names no project, with ``VNX_PROJECT_ID`` unset."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    state_dir = tmp_path / "plain" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / rq.LEDGER_NAME).write_text(
        json.dumps(_receipt(OTHER)) + "\n", encoding="utf-8")
    return state_dir


def _argv(cmd: str, state_dir: Path, *extra: str) -> List[str]:
    positional = {"by-dispatch": ["d-1"], "by-pr": ["42"], "since": [SINCE],
                  "by-track": ["track-a"]}.get(cmd, [])
    window = ["--window", "36500d"] if cmd == "digest" else []
    return [cmd, *positional, "--state-dir", str(state_dir), *window, *extra, "--json"]


def _run(capsys, argv: List[str]):
    rc = rq.main(argv)
    captured = capsys.readouterr()
    return rc, captured


def _payload(out: str) -> Dict[str, Any]:
    return json.loads(out[out.index("{"):])


@pytest.mark.parametrize("cmd", SIX)
def test_state_dir_names_the_project_with_the_env_unset(store, capsys, cmd):
    with _patched_oim(cmd):
        rc, captured = _run(capsys, _argv(cmd, store))
    assert rc == 0
    out = _payload(captured.out)
    assert out["project_id"] == OTHER
    if cmd in ("by-dispatch", "by-pr", "since", "by-track"):
        assert out["count"] == 1
        assert [r["project_id"] for r in out["receipts"]] == [OTHER]
    elif cmd == "digest":
        assert out["noise_counts"].get("foreign_project", 0) == 1
    else:
        assert out["scanned"] == 1


@pytest.mark.parametrize("cmd", SIX)
def test_no_resolvable_project_refuses(unresolvable, capsys, cmd):
    rc, captured = _run(capsys, _argv(cmd, unresolvable))
    assert rc == 2
    assert "no project id" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("cmd", SIX)
def test_explicit_project_id_wins_over_state_dir_and_env(store, monkeypatch, capsys, cmd):
    monkeypatch.setenv("VNX_PROJECT_ID", "env-project")
    with _patched_oim(cmd):
        rc, captured = _run(capsys, _argv(cmd, store, "--project-id", TWIN))
    assert rc == 0
    out = _payload(captured.out)
    assert out["project_id"] == TWIN
    if cmd in ("by-dispatch", "by-pr", "since", "by-track"):
        assert [r["project_id"] for r in out["receipts"]] == [TWIN]
    elif cmd == "reconcile-oi-pending":
        assert out["scanned"] == 0


@pytest.mark.parametrize("cmd", SIX)
def test_env_is_the_fallback_when_the_state_dir_names_no_project(unresolvable, monkeypatch, capsys, cmd):
    monkeypatch.setenv("VNX_PROJECT_ID", OTHER)
    with _patched_oim(cmd):
        rc, captured = _run(capsys, _argv(cmd, unresolvable))
    assert rc == 0
    assert _payload(captured.out)["project_id"] == OTHER


def test_the_hardcoded_default_project_is_gone():
    assert not hasattr(rq, "DEFAULT_PROJECT_ID")


@pytest.mark.parametrize("call", [
    lambda p: rq.find_receipts_by_dispatch(p, "d-1"),
    lambda p: rq.find_receipts_by_pr(p, "42"),
    lambda p: rq.find_receipts_since(p, SINCE),
    lambda p: rq.compute_digest(p),
    lambda p: rq.reconcile_oi_pending(p),
])
def test_functions_require_a_project_id(tmp_path, call):
    with pytest.raises(TypeError):
        call(tmp_path / rq.LEDGER_NAME)


class _RecordingOIM:
    """open_items_manager stand-in for reconcile/digest on the tmp store: the
    real store is never touched."""

    def load_items(self):
        return {"items": []}

    def _find_by_dedup_key(self, data, key):
        return None

    def add_item_programmatic(self, **kwargs):
        raise RuntimeError("tmp store has no open-items backend")


def _patched_oim(cmd: str):
    from unittest.mock import patch
    if cmd in ("digest", "reconcile-oi-pending"):
        return patch.object(rq, "_load_open_items_manager", return_value=_RecordingOIM())
    return patch.object(rq, "_load_open_items_manager", return_value=None)
