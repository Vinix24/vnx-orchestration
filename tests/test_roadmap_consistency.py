"""CI guard: ROADMAP.yaml stays a valid roadmap document.

FEATURE_PLAN.md and its generator are gone (ADR-047, B3): the tracks DB is the
only plan source, so there is no second view left to compare and nothing here
regenerates one. What stays is the shape check on the shipped roadmap file.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROADMAP = _REPO_ROOT / "ROADMAP.yaml"


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


def test_no_conflict_markers_in_roadmap_files() -> None:
    content = _ROADMAP.read_text(encoding="utf-8")
    for i, line in enumerate(content.splitlines(), 1):
        if line.startswith(("<<<<<<", ">>>>>>>", "=======")):
            pytest.fail(f"Conflict marker in {_ROADMAP.name} at line {i}: {line!r}")
