"""ADR-056 zelfst PR-6b (Dispatch-ID: dlv-533b6e2e55f4): per-project coverage per counted source.

``covered`` = rows carrying ``(project_id, delivery_id)``, ``uncovered`` = rows without it or with
``project_id=unknown``, ``no_data`` = no rows of that project. Stores are real files under ``tmp_path``.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_db  # noqa: E402
import lifecycle_count  # noqa: E402
import project_id_migration  # noqa: E402

PROJECT, OTHER = "vnx-dev", "other-proj"
SOURCES = ("promote", "close", "decide", "merge", "override", "prompt")
IDENTITY = {"project_id": PROJECT, "delivery_id": "dlv-aaaa00000001"}


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "vnx-data" / "state"
    sd.mkdir(parents=True)
    coordination_db.init_schema(sd)
    project_id_migration.run_runtime_coordination_migration(
        coordination_db.db_path_from_state_dir(sd), default_project_id=PROJECT)
    return sd


def _coverage(state_dir: Path):
    """The measurement, or ``None`` while the function does not exist (the red run asserts the outcome)."""
    measure = getattr(lifecycle_count, "measure_coverage", None)
    return measure(state_dir, project_id=PROJECT) if measure else None


def _state(coverage, source: str):
    return (getattr(coverage, "sources", None) or {}).get(source, {}).get("state")


def _event(state_dir: Path, event_type: str, meta: dict, project: str = PROJECT) -> None:
    with coordination_db.get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id, "
            "actor, metadata_json, project_id) VALUES (?, ?, 'dispatch', 'dlv-aaaa00000001', "
            "'operator', ?, ?)", (uuid.uuid4().hex[:16], event_type, json.dumps(meta), project))
        conn.commit()


def _override(state_dir: Path, meta: dict) -> None:
    with coordination_db.get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO governance_overrides (override_id, entity_type, entity_id, actor, "
            "override_type, justification, outcome, metadata_json) "
            "VALUES (?, 'dispatch', 'dlv-aaaa00000001', 'operator', 'manual', 'j', 'applied', ?)",
            (uuid.uuid4().hex, json.dumps(meta)))
        conn.commit()


def _line(state_dir: Path, name: str, **line) -> None:
    with (state_dir / name).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def test_empty_project_is_no_data(state_dir):
    c = _coverage(state_dir)

    assert getattr(c, "state", None) == "no_data"
    assert {s: _state(c, s) for s in SOURCES} == dict.fromkeys(SOURCES, "no_data")


def test_promote_with_identity_is_covered(state_dir):
    _event(state_dir, "deliverable_promoted", IDENTITY)

    c = _coverage(state_dir)

    assert _state(c, "promote") == "covered"
    assert c.sources["promote"]["with_identity"] == 1
    assert _state(c, "close") == "no_data"
    assert c.state == "uncovered"  # D13: one source covered is not a covered project


def test_promote_without_identity_is_uncovered(state_dir):
    _event(state_dir, "deliverable_promoted", {"actor_class": "operator"})
    _event(state_dir, "deliverable_completed", {"project_id": PROJECT})  # delivery_id missing

    assert [_state(_coverage(state_dir), s) for s in ("promote", "close")] == ["uncovered"] * 2


@pytest.mark.parametrize("column,meta", [
    ("unknown", {"project_id": "unknown", "delivery_id": "dlv-aaaa00000001"}),
    (PROJECT, {"project_id": "unknown", "delivery_id": "dlv-aaaa00000001"}),
    ("unknown", dict(IDENTITY)),
])
def test_unknown_project_is_uncovered(state_dir, column, meta):
    _event(state_dir, "deliverable_promoted", meta, project=column)

    assert _state(_coverage(state_dir), "promote") == "uncovered"


def test_another_projects_rows_never_count(state_dir):
    other = {"project_id": OTHER, "delivery_id": "dlv-bbbb00000001"}
    _event(state_dir, "deliverable_promoted", other, project=OTHER)
    _line(state_dir, "t0_receipts.ndjson", event_type="pr_merged", **other)

    c = _coverage(state_dir)

    assert c.state == "no_data"


def test_merge_without_identity_is_uncovered_and_other_events_are_ignored(state_dir):
    _line(state_dir, "t0_receipts.ndjson", event_type="pr_merged", conclusion="merged")
    _line(state_dir, "t0_receipts.ndjson", event_type="task_complete", **IDENTITY)

    assert _state(_coverage(state_dir), "merge") == "uncovered"


def test_decide_override_and_prompt_read_their_own_stores(state_dir):
    _line(state_dir, "t0_decision_log.jsonl", action="close_oi", dispatch_id="x")
    _override(state_dir, {})
    _line(state_dir, "prompt_attribution.ndjson", **IDENTITY)

    c = _coverage(state_dir)

    assert (_state(c, "decide"), _state(c, "override"), _state(c, "prompt")) == (
        "uncovered", "uncovered", "covered")


def test_every_source_with_identity_is_covered(state_dir):
    _event(state_dir, "deliverable_promoted", IDENTITY)
    _event(state_dir, "deliverable_completed", IDENTITY)
    _line(state_dir, "t0_decision_log.jsonl", action="close_oi", **IDENTITY)
    _line(state_dir, "t0_receipts.ndjson", event_type="pr_merged", **IDENTITY)
    _override(state_dir, IDENTITY)
    _line(state_dir, "prompt_attribution.ndjson", **IDENTITY)

    c = _coverage(state_dir)

    assert c.state == "covered"
    assert {s: _state(c, s) for s in SOURCES} == dict.fromkeys(SOURCES, "covered")


def test_malformed_identity_fields_stay_uncovered(state_dir):
    _line(state_dir, "t0_decision_log.jsonl", project_id=PROJECT, delivery_id=None)
    _line(state_dir, "t0_receipts.ndjson", event_type="pr_merged", project_id=PROJECT, delivery_id=7)
    _line(state_dir, "prompt_attribution.ndjson", project_id=["x"], delivery_id="dlv-aaaa00000001")

    c = _coverage(state_dir)

    assert (_state(c, "decide"), _state(c, "merge"), _state(c, "prompt")) == (
        "uncovered", "uncovered", "uncovered")


def _corrupt_log(sd):
    (sd / "t0_decision_log.jsonl").write_text('{"a": 1}\nnot json\n{"b": 2}\n', encoding="utf-8")


def _db_is_a_directory(sd):
    db = coordination_db.db_path_from_state_dir(sd)
    db.unlink()
    db.mkdir()


@pytest.mark.parametrize("break_store", [_corrupt_log, _db_is_a_directory])
def test_unreadable_store_is_unmeasured_not_no_data(state_dir, break_store):
    break_store(state_dir)

    with pytest.raises(lifecycle_count.Unmeasured):
        lifecycle_count.measure_coverage(state_dir, project_id=PROJECT)


def test_missing_database_is_no_data_and_missing_project_is_unmeasured(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    sd = tmp_path / "bare" / "state"
    sd.mkdir(parents=True)

    assert lifecycle_count.measure_coverage(sd, project_id=PROJECT).state == "no_data"
    with pytest.raises(lifecycle_count.Unmeasured):
        lifecycle_count.measure_coverage(sd, project_id="  ")
