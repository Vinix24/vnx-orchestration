"""CI guard: FEATURE_PLAN.md and PR_QUEUE.md must stay in sync with ROADMAP.yaml.

FEATURE_PLAN.md check: verifies that every feature in ROADMAP.yaml appears in
the committed FEATURE_PLAN.md with the correct status line.

PR_QUEUE.md check: regenerates from ROADMAP.yaml and asserts byte-exact match
against the committed file (since PR_QUEUE.md is purely derived from ROADMAP.yaml
with no live sources).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROADMAP = _REPO_ROOT / "ROADMAP.yaml"
_FEATURE_PLAN = _REPO_ROOT / "FEATURE_PLAN.md"
_PR_QUEUE = _REPO_ROOT / "PR_QUEUE.md"


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
    data = _load_roadmap()
    content = _FEATURE_PLAN.read_text(encoding="utf-8")

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


def test_pr_queue_matches_roadmap() -> None:
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "build_pr_queue.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"PR_QUEUE.md is stale. Re-run: python3 scripts/build_pr_queue.py\n"
        f"Details: {result.stderr.strip()}"
    )


def test_no_conflict_markers_in_roadmap_files() -> None:
    for path in (_ROADMAP, _FEATURE_PLAN, _PR_QUEUE):
        content = path.read_text(encoding="utf-8")
        for i, line in enumerate(content.splitlines(), 1):
            if line.startswith(("<<<<<<", ">>>>>>>", "=======")):
                pytest.fail(f"Conflict marker in {path.name} at line {i}: {line!r}")
