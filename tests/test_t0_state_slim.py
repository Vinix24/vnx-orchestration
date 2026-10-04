"""D8 fabric-state-herstel: t0_state has no dead or bloated sections.

Measured 2026-09-29 on the real vnx-dev store: t0_state.json was 603 KB. Eight
sections (feature_state, canonical_tracks, human_gate_queue, recent_dispatches,
dispatch_register_events, intelligence_brief, dispatch_insights, pr_progress)
carried 507 KB and had no reader outside the builder; pr_queue.queued_features
carried another 50 KB. A T0 that needs them runs a command instead:
planning_cli.py for tracks and the human gate, receipt_query.py for dispatches.

ADR-007: the tracks fixture seeds a second project whose track ids collide with
the first. Nothing of it may reach the state document.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import build_t0_state as bts
import pr_queue_state
from build_t0_state import _DETAIL_SECTION_MAP, _state_to_brief, build_t0_state

# Proposal from the plan: 80 KB. The measured state without the removed
# sections is ~40 KB on the real store; this fixture is far smaller.
T0_STATE_MAX_BYTES = 80 * 1024

REMOVED_SECTIONS = (
    "feature_state",
    "pr_progress",
    "canonical_tracks",
    "human_gate_queue",
    "dispatch_register_events",
    "recent_dispatches",
    "intelligence_brief",
    "dispatch_insights",
)

PROJECT = "fsh-d8"
OTHER_PROJECT = "fsh-d8-other"
OTHER_TITLE = "OTHER-TENANT-TRACK-TITLE"


def _seed_register(state_dir: Path, count: int) -> None:
    reg = state_dir / "dispatch_register.ndjson"
    lines = []
    for i in range(count):
        lines.append(json.dumps({
            "timestamp": f"2026-09-29T10:{i // 60:02d}:{i % 60:02d}.000000Z",
            "event": "dispatch_created" if i % 3 else "gate_passed",
            "dispatch_id": f"20260929-d8-{i:04d}",
            "feature_id": f"F{i % 40}",
            "pr_number": 1000 + i,
        }))
    reg.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_tracks(state_dir: Path) -> None:
    db = sqlite3.connect(state_dir / "runtime_coordination.db")
    try:
        db.execute(
            "CREATE TABLE tracks (track_id TEXT, project_id TEXT, title TEXT, phase TEXT, "
            "next_up INTEGER, sort_order INTEGER, priority TEXT, pr_ref TEXT, "
            "phase_changed_at TEXT, completed_at TEXT, PRIMARY KEY (track_id, project_id))"
        )
        db.execute(
            "CREATE TABLE track_open_items (track_id TEXT, project_id TEXT, oi_id TEXT, "
            "link_type TEXT, link_source TEXT, linked_at TEXT)"
        )
        for i in range(400):
            for pid, title in ((PROJECT, f"own track {i} " + "x" * 200), (OTHER_PROJECT, OTHER_TITLE)):
                db.execute(
                    "INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"track-{i}", pid, title, "now", 0, i, "P2", None, None, None),
                )
        db.commit()
    finally:
        db.close()


@pytest.fixture
def state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    dispatch_dir = tmp_path / "dispatches"
    state_dir.mkdir(parents=True)
    for sub in ("pending", "active", "conflicts"):
        (dispatch_dir / sub).mkdir(parents=True)
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setattr(pr_queue_state, "_get_open_prs", lambda project_root: ([], None))
    monkeypatch.setattr(pr_queue_state, "_get_merged_today", lambda project_root: ([], None))
    _seed_register(state_dir, 700)
    _seed_tracks(state_dir)
    return build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)


def test_t0_state_stays_under_size_bound(state):
    size = len(json.dumps(state, separators=(",", ":"), default=str).encode("utf-8"))
    assert size < T0_STATE_MAX_BYTES, f"t0_state is {size} bytes, bound {T0_STATE_MAX_BYTES}"


@pytest.mark.parametrize("section", REMOVED_SECTIONS)
def test_removed_section_does_not_come_back(state, section):
    assert section not in state


def test_pr_queue_carries_no_queued_features(state):
    assert "queued_features" not in state["pr_queue"]


def test_no_other_tenant_row_reaches_the_state(state):
    assert OTHER_TITLE not in json.dumps(state, default=str)


def test_detail_map_names_no_removed_section():
    assert not set(REMOVED_SECTIONS) & set(_DETAIL_SECTION_MAP)
    assert "dispatch_register" not in _DETAIL_SECTION_MAP.values()
    assert "feature_state" not in _DETAIL_SECTION_MAP.values()


def test_brief_adapter_reads_no_pr_progress(state):
    brief = _state_to_brief(state)
    assert "pr_progress" not in brief


def test_builder_defines_no_reader_for_removed_sections():
    for name in (
        "_build_feature_state", "_build_pr_progress", "_build_tracks_from_db",
        "_build_human_gate_queue", "_collect_recent_dispatches",
        "_collect_intelligence_brief", "_collect_dispatch_insights",
        "_build_register_events",
    ):
        assert not hasattr(bts, name), f"{name} is back in build_t0_state"


def test_pr_queue_state_defines_no_queued_features_builder():
    assert not hasattr(pr_queue_state, "_build_queued_features")
