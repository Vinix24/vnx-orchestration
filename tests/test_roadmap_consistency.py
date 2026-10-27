"""CI guard: FEATURE_PLAN.md must stay in sync with ROADMAP.yaml.

FEATURE_PLAN.md is a generated, untracked artifact (scripts/build_feature_plan.py
— OI-1376) with no committed copy to read in a fresh checkout, so this
regenerates it in memory from ROADMAP.yaml alone and verifies every roadmap
feature appears with the correct status line.

PR_QUEUE.md and its generator are gone (ADR-047, B4): there is no second view
left to compare.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROADMAP = _REPO_ROOT / "ROADMAP.yaml"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))


def _load_roadmap() -> dict:
    with _ROADMAP.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def test_roadmap_yaml_is_valid() -> None:
    data = _load_roadmap()
    assert "features" in data, "ROADMAP.yaml must have a 'features' key"
    assert "launch_state" in data, "ROADMAP.yaml must have a 'launch_state' key"
    for feat in data["features"]:
        assert "feature_id" in feat, f"Feature missing feature_id: {feat}"
        assert "status" in feat, f"Feature {feat.get('feature_id')} missing status"


def test_feature_plan_contains_all_roadmap_features() -> None:
    from build_feature_plan import generate_feature_plan

    data = _load_roadmap()
    content = generate_feature_plan(
        register_events=[], merged_prs=[], roadmap_features=data.get("features") or [],
    )

    missing = []
    wrong_status = []
    for feat in data.get("features") or []:
        fid = feat["feature_id"]
        status = feat.get("status", "planned")
        if fid not in content:
            missing.append(fid)
        elif f"Status: {status}" not in content:
            wrong_status.append((fid, status))

    assert not missing, (
        f"FEATURE_PLAN.md is missing these feature_ids: {missing}. "
        f"Re-run: python3 scripts/build_feature_plan.py"
    )
    assert not wrong_status, (
        f"FEATURE_PLAN.md has wrong status for features: {wrong_status}. "
        f"Re-run: python3 scripts/build_feature_plan.py"
    )


def test_no_conflict_markers_in_roadmap_files() -> None:
    # FEATURE_PLAN.md is excluded here: it is a generated, untracked artifact
    # (OI-1376) with no committed copy to inspect in a fresh checkout, and its
    # content is built programmatically each time (not merged), so it cannot
    # carry leftover conflict markers.
    content = _ROADMAP.read_text(encoding="utf-8")
    for i, line in enumerate(content.splitlines(), 1):
        if line.startswith(("<<<<<<", ">>>>>>>", "=======")):
            pytest.fail(f"Conflict marker in {_ROADMAP.name} at line {i}: {line!r}")
