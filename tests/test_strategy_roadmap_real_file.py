"""Integration test: load and validate the committed roadmap.yaml."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.lib.strategy.roadmap import load_roadmap, validate_roadmap  # noqa: E402


def test_committed_roadmap_loads_and_validates() -> None:
    roadmap = load_roadmap()
    errors = validate_roadmap(roadmap)
    assert errors == [], f"committed roadmap.yaml has validation errors: {errors}"
