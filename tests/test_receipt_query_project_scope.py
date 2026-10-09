#!/usr/bin/env python3
"""ADR-007 regression — receipt_query lookups and reconcile-oi-pending honor the project.

Dispatch ids and PR numbers are not unique across projects, and a project's
receipt ledger can carry a line stamped with another project's ``project_id``.
Every reader must drop those lines; ``receipt_outcome.is_foreign_project`` is
the shared test (a line without a ``project_id`` is the ledger's own project).

Before this fix ``by-pr``, ``since``, ``by-dispatch`` and ``by-track`` mixed in
another project's receipts (``by-track`` scoped only the SQLite dispatch-id
lookup), and ``reconcile-oi-pending`` even filed open items in this project's
store for another project's warnings. Each test below is written against that
collision: the same dispatch id ``d-1`` and PR ``42`` exist under two projects.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

import receipt_provenance as rp  # noqa: E402
import receipt_query as rq  # noqa: E402

OWN = "vnx-dev"
FOREIGN = "other-proj"
SINCE = "2026-09-30T00:00:00Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_ledger(state_dir: Path, *receipts: Dict[str, Any]) -> None:
    lines = [json.dumps(r) for r in receipts]
    (state_dir / rq.LEDGER_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _colliding(project_id: Any, **overrides: Any) -> Dict[str, Any]:
    """A receipt for dispatch ``d-1``/PR ``42`` — ids another project also uses."""
    receipt = {
        "schema_version": 2,
        "dispatch_id": "d-1",
        "pr_id": "42",
        "timestamp": "2026-09-30T12:00:00Z",
        "event_type": "task_complete",
        "status": "done",
        "project_id": project_id,
    }
    receipt.update(overrides)
    return receipt


def _oi_warning(code: str) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": "blocker",
        "message": f"message for {code}",
        "destination": "oi_pending",
        "oi_id": None,
        "reason": "store lock held",
        "requires_tracking": True,
    }


def _create_runtime_db(state_dir: Path) -> Path:
    db_path = state_dir / rq.RUNTIME_COORDINATION_DB_NAME
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE dispatches ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL, track TEXT, "
            "UNIQUE(dispatch_id, project_id))"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_dispatch(db_path: Path, dispatch_id: str, project_id: str, track: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, track) VALUES (?, ?, ?)",
            (dispatch_id, project_id, track),
        )
        conn.commit()
    finally:
        conn.close()


def _load_oim(tmp_path: Path):
    """Isolated real open_items_manager bound to a per-test state dir."""
    env_patch = {
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(tmp_path / "data" / "state"),
        "VNX_HOME": str(VNX_ROOT),
    }
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
    mod_name = f"open_items_manager_project_scope_{tmp_path.name}"
    with patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(
            mod_name, SCRIPTS_DIR / "open_items_manager.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            del sys.modules[mod_name]
            raise
    return mod


class _RecordingOIM:
    """Records every ``add_item_programmatic`` call; never touches a real store."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def add_item_programmatic(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "oi-1"


class _ExplodingOIM:
    """Fails loudly if reconcile ever tries to file a foreign warning."""

    def add_item_programmatic(self, **kwargs: Any) -> str:
        raise AssertionError(f"must not file another project's warning: {kwargs!r}")


# ---------------------------------------------------------------------------
# The reproduced collision — every lookup returns only the selected project
# ---------------------------------------------------------------------------

def test_reproduction_lookups_return_only_the_selected_project(tmp_path):
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    ledger = tmp_path / rq.LEDGER_NAME
    for receipts in (
        rq.find_receipts_by_pr(ledger, "42", project_id=OWN),
        rq.find_receipts_by_dispatch(ledger, "d-1", OWN),
        rq.find_receipts_since(ledger, SINCE, project_id=OWN),
    ):
        assert [r["project_id"] for r in receipts] == [OWN]


# ---------------------------------------------------------------------------
# by-pr (Python + CLI)
# ---------------------------------------------------------------------------

def test_find_receipts_by_pr_scopes_to_the_project(tmp_path):
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    ledger = tmp_path / rq.LEDGER_NAME
    assert [r["project_id"] for r in rq.find_receipts_by_pr(ledger, "42", project_id=OWN)] == [OWN]
    assert [
        r["project_id"] for r in rq.find_receipts_by_pr(ledger, "42", project_id=FOREIGN)
    ] == [FOREIGN]


def test_find_receipts_by_pr_requires_a_project_id(tmp_path):
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    with pytest.raises(TypeError):
        rq.find_receipts_by_pr(tmp_path / rq.LEDGER_NAME, "42")


def test_lookup_without_project_id_counts_as_this_projects(tmp_path):
    own_line = _colliding(OWN)
    del own_line["project_id"]
    _write_ledger(tmp_path, own_line)
    ledger = tmp_path / rq.LEDGER_NAME
    assert len(rq.find_receipts_by_pr(ledger, "42", project_id=OWN)) == 1
    assert len(rq.find_receipts_by_pr(ledger, "42", project_id=FOREIGN)) == 1


def test_by_pr_cli_accepts_project_id_and_filters(tmp_path, capsys):
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    rc = rq.main([
        "by-pr", "42", "--state-dir", str(tmp_path), "--project-id", OWN, "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project_id"] == OWN
    assert out["count"] == 1
    assert out["receipts"][0]["project_id"] == OWN


def test_by_pr_cli_without_a_project_refuses(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    rc = rq.main(["by-pr", "42", "--state-dir", str(tmp_path), "--json"])
    assert rc == 2
    assert "no project id" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# since (Python + CLI)
# ---------------------------------------------------------------------------

def test_find_receipts_since_scopes_to_the_project(tmp_path):
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    ledger = tmp_path / rq.LEDGER_NAME
    assert [r["project_id"] for r in rq.find_receipts_since(ledger, SINCE, project_id=OWN)] == [OWN]
    assert [
        r["project_id"] for r in rq.find_receipts_since(ledger, SINCE, project_id=FOREIGN)
    ] == [FOREIGN]


def test_since_cli_accepts_project_id_and_filters(tmp_path, capsys):
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    rc = rq.main([
        "since", SINCE, "--state-dir", str(tmp_path), "--project-id", OWN, "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project_id"] == OWN
    assert out["count"] == 1
    assert out["receipts"][0]["project_id"] == OWN


# ---------------------------------------------------------------------------
# by-dispatch (Python wrapper + provenance function + CLI)
# ---------------------------------------------------------------------------

def test_find_receipts_by_dispatch_scopes_to_the_project(tmp_path):
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    ledger = tmp_path / rq.LEDGER_NAME
    assert [r["project_id"] for r in rq.find_receipts_by_dispatch(ledger, "d-1", OWN)] == [OWN]


def test_provenance_find_receipts_by_dispatch_scopes_when_given_project(tmp_path):
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    ledger = tmp_path / rq.LEDGER_NAME
    assert [
        r["project_id"] for r in rp.find_receipts_by_dispatch(ledger, "d-1", project_id=OWN)
    ] == [OWN]
    # an unscoped call keeps the historical behaviour for callers with no project
    assert len(rp.find_receipts_by_dispatch(ledger, "d-1")) == 2


def test_by_dispatch_cli_accepts_project_id_and_filters(tmp_path, capsys):
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    rc = rq.main([
        "by-dispatch", "d-1", "--state-dir", str(tmp_path), "--project-id", OWN, "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project_id"] == OWN
    assert out["count"] == 1
    assert out["receipts"][0]["project_id"] == OWN


# ---------------------------------------------------------------------------
# by-track — filters the receipts, not only the dispatch-id lookup
# ---------------------------------------------------------------------------

def test_find_receipts_by_track_filters_foreign_receipts_for_same_dispatch(tmp_path):
    db_path = _create_runtime_db(tmp_path)
    _insert_dispatch(db_path, "d-1", OWN, "track-a")
    # the same dispatch id is also stamped for another project in the ledger
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    receipts = rq.find_receipts_by_track(
        tmp_path, tmp_path / rq.LEDGER_NAME, "track-a", OWN,
    )
    assert [r["project_id"] for r in receipts] == [OWN]


def test_by_track_cli_filters_foreign_receipts(tmp_path, capsys):
    db_path = _create_runtime_db(tmp_path)
    _insert_dispatch(db_path, "d-1", OWN, "track-a")
    _write_ledger(tmp_path, _colliding(OWN), _colliding(FOREIGN))
    rc = rq.main([
        "by-track", "track-a", "--state-dir", str(tmp_path), "--project-id", OWN, "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project_id"] == OWN
    assert out["count"] == 1
    assert out["receipts"][0]["project_id"] == OWN


# ---------------------------------------------------------------------------
# reconcile-oi-pending — another project's warning is never filed here
# ---------------------------------------------------------------------------

def test_reconcile_oi_pending_skips_foreign_receipts(tmp_path):
    own = _colliding(OWN, warnings=[_oi_warning("own_code")])
    foreign = _colliding(FOREIGN, warnings=[_oi_warning("foreign_code")])
    _write_ledger(tmp_path, own, foreign)
    oim = _RecordingOIM()
    result = rq.reconcile_oi_pending(
        tmp_path / rq.LEDGER_NAME, project_id=OWN, open_items_manager_module=oim,
    )
    assert result["project_id"] == OWN
    assert result["scanned"] == 1
    assert result["reconciled"] == 1
    assert [call["dedup_key"] for call in oim.calls] == ["own_code"]


def test_reconcile_oi_pending_foreign_only_counts_as_nothing_scanned(tmp_path):
    foreign = _colliding(FOREIGN, warnings=[_oi_warning("foreign_code")])
    _write_ledger(tmp_path, foreign)
    result = rq.reconcile_oi_pending(
        tmp_path / rq.LEDGER_NAME, project_id=OWN, open_items_manager_module=_ExplodingOIM(),
    )
    assert result["scanned"] == 0
    assert result["reconciled"] == 0
    assert result["still_pending"] == 0
    assert result["failed"] == 0
    assert result["escalated"] == []


def test_reconcile_oi_pending_creates_no_open_item_for_foreign_warning(tmp_path):
    oim = _load_oim(tmp_path)
    own = _colliding(OWN, warnings=[_oi_warning("own_code")], report_path="reports/own.md")
    foreign = _colliding(
        FOREIGN, warnings=[_oi_warning("foreign_code")], report_path="reports/foreign.md",
    )
    _write_ledger(tmp_path, own, foreign)
    result = rq.reconcile_oi_pending(
        tmp_path / rq.LEDGER_NAME, project_id=OWN, open_items_manager_module=oim,
    )
    assert result["scanned"] == 1
    assert result["reconciled"] == 1
    data = oim.load_items()
    assert oim._find_by_dedup_key(data, "own_code") is not None
    assert oim._find_by_dedup_key(data, "foreign_code") is None


def test_reconcile_oi_pending_cli_accepts_project_id(tmp_path, capsys):
    own = _colliding(OWN, warnings=[_oi_warning("own_code")])
    foreign = _colliding(FOREIGN, warnings=[_oi_warning("foreign_code")])
    _write_ledger(tmp_path, own, foreign)
    oim = _RecordingOIM()
    with patch.object(rq, "_load_open_items_manager", return_value=oim):
        rc = rq.main([
            "reconcile-oi-pending", "--state-dir", str(tmp_path), "--project-id", OWN, "--json",
        ])
    assert rc == 0
    raw = capsys.readouterr().out
    out = json.loads(raw[raw.index("{"):])
    assert out["project_id"] == OWN
    assert out["scanned"] == 1
    assert [call["dedup_key"] for call in oim.calls] == ["own_code"]
