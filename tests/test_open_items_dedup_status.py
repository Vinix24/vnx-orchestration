#!/usr/bin/env python3
"""Round-2 codex regate (PR #307): _find_by_dedup_key matches any status.

Replayed/duplicate receipts must not be able to recreate findings that were
previously closed. Dedup checks therefore consider items in any status
(open/done/deferred/wontfix), not just open ones.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"


def _load_oim(tmp_path: Path):
    """Reload open_items_manager with a clean per-test STATE_DIR."""
    env_patch = {
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(tmp_path / "data" / "state"),
        "VNX_HOME": str(TESTS_DIR.parent),
    }
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)

    mod_name = f"open_items_manager_test_{tmp_path.name}"
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


def test_find_by_dedup_key_matches_open_item(tmp_path: Path):
    oim = _load_oim(tmp_path)
    data = {"items": [
        {"id": "OI-001", "status": "open", "dedup_key": "qa:c1:foo.py:fn"},
    ]}
    found = oim._find_by_dedup_key(data, "qa:c1:foo.py:fn")
    assert found is not None
    assert found["id"] == "OI-001"


def test_find_by_dedup_key_matches_closed_item(tmp_path: Path):
    """Round-2 fix: closed items now match dedup key to prevent replay recreation."""
    oim = _load_oim(tmp_path)
    data = {"items": [
        {"id": "OI-002", "status": "done", "dedup_key": "qa:c2:bar.py:"},
        {"id": "OI-003", "status": "deferred", "dedup_key": "qa:c3:baz.py:"},
        {"id": "OI-004", "status": "wontfix", "dedup_key": "qa:c4:qux.py:"},
    ]}
    assert oim._find_by_dedup_key(data, "qa:c2:bar.py:")["id"] == "OI-002"
    assert oim._find_by_dedup_key(data, "qa:c3:baz.py:")["id"] == "OI-003"
    assert oim._find_by_dedup_key(data, "qa:c4:qux.py:")["id"] == "OI-004"


def test_find_by_dedup_key_returns_none_when_absent(tmp_path: Path):
    oim = _load_oim(tmp_path)
    data = {"items": [{"id": "OI-001", "status": "open", "dedup_key": "qa:other:x.py:"}]}
    assert oim._find_by_dedup_key(data, "qa:c1:missing.py:") is None


def test_legacy_alias_still_resolves(tmp_path: Path):
    """_find_open_by_dedup_key is kept as a backwards-compatible alias."""
    oim = _load_oim(tmp_path)
    assert oim._find_open_by_dedup_key is oim._find_by_dedup_key


def test_add_item_programmatic_dedups_against_closed_item(tmp_path: Path):
    """Replay-safety regression: adding an item whose dedup_key matches a CLOSED
    item must return (existing_id, False) and not create a new OI."""
    oim = _load_oim(tmp_path)
    # First call creates the item, second call closes it manually via JSON edit,
    # then a third call with the same dedup_key must dedup (created=False).
    item_id_1, created_1 = oim.add_item_programmatic(
        title="initial finding",
        severity="blocker",
        dispatch_id="DISP-ALPHA",
        dedup_key="qa:c1:foo.py:bar",
    )
    assert created_1 is True
    assert item_id_1.startswith("OI-")

    # Mark the item closed in the on-disk store.
    data = oim.load_items()
    for it in data["items"]:
        if it["id"] == item_id_1:
            it["status"] = "done"
            it["closed_reason"] = "fixed"
    oim.save_items(data)

    # Replayed receipt: same dedup_key, must dedup against the closed item.
    item_id_2, created_2 = oim.add_item_programmatic(
        title="initial finding (replay)",
        severity="blocker",
        dispatch_id="DISP-ALPHA-REPLAY",
        dedup_key="qa:c1:foo.py:bar",
    )
    assert created_2 is False, (
        "Round-2 fix: closed items must dedup. Otherwise replayed receipts "
        "recreate findings that were already closed."
    )
    assert item_id_2 == item_id_1
