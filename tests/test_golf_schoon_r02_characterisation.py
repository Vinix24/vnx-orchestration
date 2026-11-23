"""Characterisation tests for golf-schoon R02 (extract-method refactor).

Pins the observable behaviour of
``build_strategy_projection.build_projection`` so the refactor stays
behaviour-neutral. Real temp dirs and real subprocesses; nothing is patched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "scripts" / "lib"))

import build_strategy_projection as bsp

# -------------------------------------------------------- build_projection
def _roadmap(tmp_path: Path, features) -> Path:
    import yaml
    p = tmp_path / "roadmap.yaml"
    p.write_text(yaml.safe_dump({"roadmap_id": "rid", "title": "T", "features": features}))
    return p


_NOW = "2026-06-01T12:00:00+00:00"


def test_projection_full_shape(tmp_path, capsys):
    feats = [
        {"feature_id": "late", "status": "planned", "milestone": "1.0",
         "depends_on": ["done1", "ghost"], "risk_class": "high",
         "review_stack": ["codex", ""], "branch_name": "b", "plan_path": "p.md",
         "notes": "  hi  "},
        {"feature_id": "", "title": "skipped"},
        {"feature_id": "done1", "title": "Done One", "status": "done",
         "pr_queue": [{"pr_id": "x"}, {"pr_id": "#42"}, {"pr_id": "#99"}]},
        {"feature_id": "free", "status": "bogus-status", "risk_class": "weird"},
        {"feature_id": "wip", "status": "in_progress", "pr_queue": [{"pr_id": "#abc"}]},
    ]
    path = _roadmap(tmp_path, feats)
    rm = bsp.build_projection(path, now_iso=_NOW)
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert lines[0] == "[build_strategy_projection] skipping feature with empty feature_id"
    assert any("wave late: depends_on 'ghost' not found" in ln for ln in lines)
    assert not any("depends_on 'done1'" in ln for ln in lines)
    ids = [w.wave_id for w in rm.waves]
    assert ids[0] == "done1"
    by = {w.wave_id: w for w in rm.waves}
    assert ids.index("free") < ids.index("late")
    assert by["done1"].status == "completed" and by["done1"].pr_number == 42
    assert by["done1"].title == "Done One" and by["free"].title == "free"
    assert by["free"].status == "planned" and by["free"].risk_class == "low"
    assert by["late"].risk_class == "high"
    assert by["late"].depends_on == ["done1", "ghost"]
    assert by["late"].review_stack == ["codex"]
    assert (by["late"].branch_name, by["late"].plan_path, by["late"].notes) == ("b", "p.md", "hi")
    assert by["free"].branch_name is None and by["free"].notes is None
    assert by["wip"].pr_number is None
    assert rm.roadmap_id == "rid" and rm.title == "T" and rm.generated_at == _NOW
    assert [p.phase_id for p in rm.phases] == sorted({w.phase_id for w in rm.waves})
    for p in rm.phases:
        assert p.waves == [w.wave_id for w in rm.waves if w.phase_id == p.phase_id]
        assert p.title == bsp._PHASE_TITLES.get(p.phase_id, f"Phase {p.phase_id}")
    assert rm.completed_history == [
        {"wave_id": "done1", "title": "Done One", "completed_at": "2026-06-01", "pr": "#42"}]
    assert rm.operator_decisions == [] and rm.schema_version == 1
    assert rm.notes["source"] == str(path)
    assert rm.notes["generator"] == "scripts/build_strategy_projection.py"


def test_projection_defaults_and_empty(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("features: []\n")
    rm = bsp.build_projection(p, now_iso=_NOW)
    assert rm.roadmap_id == "vnx-roadmap-autopilot"
    assert rm.title == "VNX Multi-Feature Roadmap"
    assert rm.waves == [] and rm.phases == [] and rm.completed_history == []


def test_projection_status_priority_order(tmp_path):
    feats = [
        {"feature_id": "p2", "status": "planned", "depends_on": ["p1"]},
        {"feature_id": "p1", "status": "planned"},
        {"feature_id": "ip", "status": "in_progress"},
        {"feature_id": "c", "status": "done"},
    ]
    rm = bsp.build_projection(_roadmap(tmp_path, feats), now_iso=_NOW)
    ids = [w.wave_id for w in rm.waves]
    assert ids == ["c", "p1", "p2", "ip"]


def test_projection_missing_file_raises(tmp_path):
    with pytest.raises(Exception):
        bsp.build_projection(tmp_path / "nope.yaml", now_iso=_NOW)
