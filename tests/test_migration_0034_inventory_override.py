"""The inventory override for 0034's comment-only .sql must equal what apply_0034
touches, so the two cannot drift."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import migration_inventory as mi
from coordination_db import _RC_COMPOSITE_KEYS
from migrations import apply_0034


def test_override_is_the_union_of_apply_0034_tables():
    derived = (
        set(apply_0034.COLUMN_TABLES)
        | {t for t, _ in _RC_COMPOSITE_KEYS}
        | {t for t, _, _ in apply_0034.UPSERT_KEYS}
    )
    override = mi._MANUAL_TABLE_OVERRIDES["0034_runtime_project_id_completion.sql"]
    assert set(override) == derived
    assert len(override) == len(set(override))
