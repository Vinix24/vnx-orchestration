"""origin_oi / origin_pr at staging and the declared edge through record_edge (herwerk H2, ADR-071 besluit 3)."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dispatch_bridge as db  # noqa: E402
import quality_db_init as qdi  # noqa: E402

PID = "tproj"


@pytest.fixture
def store(tmp_path):
    assert qdi.bootstrap_qi_db(tmp_path / "state" / "quality_intelligence.db") is True
    return tmp_path


@pytest.fixture(autouse=True)
def so(monkeypatch, tmp_path):
    """The origin module with its two outside seams (open items, gh) patched; None before it exists."""
    monkeypatch.setattr(db, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(db, "policy_digest_for_role", lambda role: "digest")
    try:
        mod = importlib.import_module("stage_origin")
    except ImportError:
        return None
    items = {"items": [
        {"id": "OI-1", "origin_dispatch_id": "dlv-x"},
        {"id": "OI-2"},
    ]}
    monkeypatch.setattr(mod, "_read_oi_items", lambda state_dir: items)
    monkeypatch.setattr(mod, "_pr_head_branch", lambda n, root: {7: "dispatch/dlv-y", 8: "feature/foo"}[n])
    return mod


def _stage(store, dispatch_id="dlv-new", project_id=PID, **kw):
    return db.stage_spec_bundle(
        instruction_text="do it", dispatch_id=dispatch_id, role="backend-developer",
        target_slot="T1", project_id=project_id, data_dir=store, **kw,
    )


def _edges(store, project_id=PID):
    with closing(sqlite3.connect(store / "state" / "quality_intelligence.db")) as conn:
        return conn.execute(
            "SELECT rework_dispatch_id, origin_dispatch_id, kind, class, source, evidence_json "
            "FROM rework_edges WHERE project_id = ?", (project_id,)
        ).fetchall()


def _no_bundle(store, dispatch_id="dlv-new"):
    return not (store / "dispatches" / "pending" / dispatch_id).exists()


def test_origin_oi_fills_parent_and_writes_one_oi_fix_edge(store):
    spec = json.loads(_stage(store, origin_oi="OI-1").read_text())
    assert spec["parent_dispatch"] == "dlv-x"
    assert spec["origin_oi"] == "OI-1"
    rows = _edges(store)
    assert [r[:5] for r in rows] == [("dlv-new", "dlv-x", "oi_fix", "rework", "spec")]
    assert json.loads(rows[0][5]) == {"origin_oi": "OI-1"}


def test_origin_pr_fills_parent_and_writes_one_pr_fix_edge(store):
    spec = json.loads(_stage(store, origin_pr=7).read_text())
    assert spec["parent_dispatch"] == "dlv-y"
    rows = _edges(store)
    assert [r[:5] for r in rows] == [("dlv-new", "dlv-y", "pr_fix", "rework", "spec")]
    assert json.loads(rows[0][5]) == {"origin_pr": 7}


@pytest.mark.parametrize("oi", ["OI-2", "OI-404", ""])
def test_oi_without_dispatch_or_unknown_is_refused(store, oi):
    with pytest.raises(ValueError, match="origin_oi"):
        _stage(store, origin_oi=oi)
    assert _no_bundle(store) and _edges(store) == []


def test_pr_with_other_branch_shape_or_gh_failure_is_refused(store, so, monkeypatch):
    with pytest.raises(ValueError, match="dispatch/<id>"):
        _stage(store, origin_pr=8)

    def boom(n, root):
        raise ValueError("origin_pr 9: cannot read the head branch from gh (rc=1)")
    monkeypatch.setattr(so, "_pr_head_branch", boom)
    with pytest.raises(ValueError, match="gh"):
        _stage(store, origin_pr=9)
    assert _no_bundle(store) and _edges(store) == []


def test_origin_contradicting_work_ref_or_explicit_parent_is_refused(store):
    with pytest.raises(ValueError, match="conflicts with the predecessor"):
        _stage(store, origin_oi="OI-1", work_ref="dispatch/dlv-z")
    with pytest.raises(ValueError, match="conflicts with the origin"):
        _stage(store, origin_oi="OI-1", parent_dispatch="dlv-q")
    assert _no_bundle(store) and _edges(store) == []


def test_both_origins_is_refused(store):
    with pytest.raises(ValueError, match="mutually exclusive"):
        _stage(store, origin_oi="OI-1", origin_pr=7)
    assert _no_bundle(store) and _edges(store) == []


def test_round_from_work_ref_writes_one_round_edge(store):
    spec = json.loads(_stage(store, work_ref="dispatch/dlv-z").read_text())
    assert spec["parent_dispatch"] == "dlv-z"
    rows = _edges(store)
    assert [r[:5] for r in rows] == [("dlv-new", "dlv-z", "round", "rework", "spec")]
    assert json.loads(rows[0][5]) == {"work_ref": "dispatch/dlv-z"}


def test_round_from_explicit_parent_names_it_in_the_evidence(store):
    _stage(store, parent_dispatch="dlv-p")
    rows = _edges(store)
    assert [r[1:3] for r in rows] == [("dlv-p", "round")]
    assert json.loads(rows[0][5]) == {"parent_dispatch": "dlv-p"}


def test_no_predecessor_writes_no_edge(store):
    _stage(store)
    assert _edges(store) == []


def test_staging_twice_keeps_one_edge(store):
    _stage(store, origin_oi="OI-1")
    _stage(store, origin_oi="OI-1")
    assert len(_edges(store)) == 1


def test_other_project_does_not_see_the_edge(store):
    _stage(store, origin_oi="OI-1")
    assert len(_edges(store)) == 1
    assert _edges(store, project_id="other") == []


def test_missing_store_does_not_block_staging(tmp_path):
    spec_file = _stage(tmp_path, work_ref="dispatch/dlv-z")
    assert json.loads(spec_file.read_text())["parent_dispatch"] == "dlv-z"
    assert not (tmp_path / "state" / "quality_intelligence.db").exists()


def test_sqlite_error_does_not_block_staging(tmp_path):
    (tmp_path / "state").mkdir()
    with closing(sqlite3.connect(tmp_path / "state" / "quality_intelligence.db")) as conn:
        conn.execute("CREATE TABLE unrelated (x TEXT)")
    spec_file = _stage(tmp_path, work_ref="dispatch/dlv-z")
    assert spec_file.is_file()


def test_cli_stage_passes_origin_flags(store, tmp_path, capsys):
    instr = tmp_path / "i.md"
    instr.write_text("do it")
    rc = db.main([
        "stage", "--instruction", str(instr), "--dispatch-id", "dlv-cli", "--role", "backend-developer",
        "--slot", "T1", "--origin-pr", "7", "--data-dir", str(store),
    ])
    assert rc == 0
    spec = json.loads(Path(capsys.readouterr().out.strip()).read_text())
    assert spec["parent_dispatch"] == "dlv-y"
