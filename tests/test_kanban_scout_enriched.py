#!/usr/bin/env python3
"""Tests for the kanban scout-enrichment indicator.

Dispatch-ID: 20260627-kanban-scout-enriched

The kanban (``_scan_dispatches``) now stamps each dispatch entry with ``scout_enriched``:
True when a scout pre-pass sidecar exists (``<state_dir>/scout/<dispatch_id>.json``), so the
operator can see at a glance which queued/running dispatches were grounded by the cheap scout
recon. Fail-open: a missing scout layer or an unsafe dispatch_id never breaks the kanban.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "dashboard"))
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import api_operator as op  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "CANONICAL_STATE_DIR", tmp_path)
    return tmp_path


def test_scout_enriched_true_when_sidecar_exists(state_dir):
    (state_dir / "scout").mkdir()
    (state_dir / "scout" / "20260627-feat-x.json").write_text('{"sketch": "..."}')
    assert op._is_scout_enriched("20260627-feat-x") is True


def test_scout_enriched_false_when_missing(state_dir):
    assert op._is_scout_enriched("20260627-no-scout") is False


def test_scout_enriched_false_on_unsafe_dispatch_id(state_dir):
    # Path-traversal id → the sidecar-path guard raises → fail-open False (never crashes).
    assert op._is_scout_enriched("../../etc/passwd") is False


def test_scout_enriched_false_when_scout_layer_absent(state_dir, monkeypatch):
    # If the scout helper failed to import, the kanban must still work (False, no crash).
    monkeypatch.setattr(op, "_op_scout_sidecar_path", None)
    assert op._is_scout_enriched("anything") is False


def test_scan_dispatches_entries_carry_scout_enriched(state_dir, monkeypatch, tmp_path):
    # A dispatch in pending/ with a matching scout sidecar → its kanban entry is scout_enriched.
    disp = tmp_path / "dispatches" / "pending"
    disp.mkdir(parents=True)
    (disp / "20260627-grounded.md").write_text(
        "---\ndispatch_id: 20260627-grounded\nterminal: T1\n---\n# task\n"
    )
    (state_dir / "scout").mkdir()
    (state_dir / "scout" / "20260627-grounded.json").write_text("{}")
    monkeypatch.setattr(op, "DISPATCHES_DIR", tmp_path / "dispatches")
    result = op._scan_dispatches()
    cards = [c for stage in result["stages"].values() for c in stage]
    grounded = next((c for c in cards if c["id"] == "20260627-grounded"), None)
    assert grounded is not None, "the pending dispatch should appear in the kanban"
    assert grounded["scout_enriched"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
