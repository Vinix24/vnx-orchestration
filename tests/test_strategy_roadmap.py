"""Unit tests for scripts.lib.strategy.roadmap."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.lib.strategy.roadmap import (  # noqa: E402
    OperatorDecision,
    Phase,
    Roadmap,
    RoadmapValidationError,
    Wave,
    dependents_of,
    load_roadmap,
    next_actionable_wave,
    phase_complete,
    validate_roadmap,
    write_roadmap,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def _minimal_payload() -> dict:
    return {
        "schema_version": 1,
        "roadmap_id": "test-roadmap",
        "title": "Test roadmap",
        "generated_at": "2026-05-06T00:00:00Z",
        "phases": [
            {
                "phase_id": 0,
                "title": "Test phase",
                "waves": ["wave-a", "wave-b"],
                "estimated_loc": 100,
                "estimated_weeks": 0.5,
                "blocked_on": [],
            },
        ],
        "waves": [
            {
                "wave_id": "wave-a",
                "title": "First wave",
                "phase_id": 0,
                "status": "planned",
                "risk_class": "low",
                "depends_on": [],
                "blocked_on": [],
            },
            {
                "wave_id": "wave-b",
                "title": "Second wave",
                "phase_id": 0,
                "status": "planned",
                "risk_class": "low",
                "depends_on": ["wave-a"],
                "blocked_on": [],
            },
        ],
        "operator_decisions": [],
        "completed_history": [],
        "notes": {},
    }


def _write_payload(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "roadmap.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_load_valid_committed_roadmap() -> None:
    """The committed .vnx-data/strategy/roadmap.yaml must load and validate."""
    roadmap = load_roadmap()
    errors = validate_roadmap(roadmap)
    assert errors == [], f"unexpected validation errors: {errors}"
    assert roadmap.schema_version >= 1
    assert len(roadmap.phases) > 0
    assert len(roadmap.waves) > 0


def test_reject_duplicate_wave_id(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["waves"].append(
        {
            "wave_id": "wave-a",
            "title": "Duplicate",
            "phase_id": 0,
            "status": "planned",
        }
    )
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path, strict=False)
    errors = validate_roadmap(roadmap)
    assert any("duplicate wave_id: wave-a" in e for e in errors)


def test_reject_dangling_depends_on(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["waves"][1]["depends_on"] = ["wave-zzz"]
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path, strict=False)
    errors = validate_roadmap(roadmap)
    assert any("dangling depends_on 'wave-zzz'" in e for e in errors)


def test_reject_dangling_blocked_on(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["waves"][0]["blocked_on"] = ["od_99"]
    payload["operator_decisions"] = [
        {"decision_id": "od_1", "title": "real", "status": "open"},
    ]
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path, strict=False)
    errors = validate_roadmap(roadmap)
    assert any("dangling blocked_on 'od_99'" in e for e in errors)


def test_status_enum_violation(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["waves"][0]["status"] = "unknown"
    path = _write_payload(tmp_path, payload)
    with pytest.raises(RoadmapValidationError, match="invalid status"):
        load_roadmap(path)


def test_decision_status_enum_violation(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["operator_decisions"] = [
        {"decision_id": "od_1", "title": "x", "status": "pending"},
    ]
    path = _write_payload(tmp_path, payload)
    with pytest.raises(RoadmapValidationError, match="invalid status"):
        load_roadmap(path)


def test_missing_required_field_raises(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["waves"][0].pop("status")
    path = _write_payload(tmp_path, payload)
    with pytest.raises(RoadmapValidationError, match="missing required key 'status'"):
        load_roadmap(path)


def test_next_actionable_wave_respects_depends_on(tmp_path: Path) -> None:
    payload = _minimal_payload()
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path)
    actionable = next_actionable_wave(roadmap)
    assert actionable is not None
    assert actionable.wave_id == "wave-a"

    payload["waves"][0]["status"] = "completed"
    payload["waves"][0]["completed_at"] = "2026-05-06"
    path2 = _write_payload(tmp_path, payload)
    roadmap2 = load_roadmap(path2)
    actionable2 = next_actionable_wave(roadmap2)
    assert actionable2 is not None
    assert actionable2.wave_id == "wave-b"


def test_next_actionable_wave_respects_blocked_on(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["operator_decisions"] = [
        {"decision_id": "od_1", "title": "open one", "status": "open"},
        {"decision_id": "od_2", "title": "closed one", "status": "closed"},
    ]
    payload["waves"][0]["blocked_on"] = ["od_1"]  # blocks wave-a
    payload["waves"][1]["blocked_on"] = ["od_2"]  # closed → wave-b unblocked
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path)
    actionable = next_actionable_wave(roadmap)
    # wave-a is blocked by open od_1; wave-b depends on wave-a (still planned).
    # So no wave is actionable.
    assert actionable is None

    # Detach wave-b's dependency on wave-a → wave-b becomes actionable.
    payload["waves"][1]["depends_on"] = []
    path2 = _write_payload(tmp_path, payload)
    roadmap2 = load_roadmap(path2)
    actionable2 = next_actionable_wave(roadmap2)
    assert actionable2 is not None
    assert actionable2.wave_id == "wave-b"


def test_next_actionable_wave_returns_none_when_all_done(tmp_path: Path) -> None:
    payload = _minimal_payload()
    for w in payload["waves"]:
        w["status"] = "completed"
        w["completed_at"] = "2026-05-06"
    path = _write_payload(tmp_path, payload)
    assert next_actionable_wave(load_roadmap(path)) is None


def test_round_trip_stable(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["waves"][0]["estimated_loc"] = 50
    payload["waves"][0]["branch_name"] = "feat/test"
    payload["waves"][0]["review_stack"] = ["gemini_review"]
    payload["operator_decisions"] = [
        {
            "decision_id": "od_1",
            "title": "Sample decision",
            "status": "closed",
            "decision": "go",
            "closed_at": "2026-05-06",
        },
    ]
    path = _write_payload(tmp_path, payload)
    roadmap_a = load_roadmap(path)

    out_path = tmp_path / "out.yaml"
    write_roadmap(roadmap_a, out_path)
    roadmap_b = load_roadmap(out_path)

    assert roadmap_a == roadmap_b


def test_schema_version_defaults_to_1(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload.pop("schema_version")
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path)
    assert roadmap.schema_version == 1


def test_dependents_of(tmp_path: Path) -> None:
    payload = _minimal_payload()
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path)
    dependents = dependents_of("wave-a", roadmap)
    assert [w.wave_id for w in dependents] == ["wave-b"]


def test_phase_complete(tmp_path: Path) -> None:
    payload = _minimal_payload()
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path)
    assert phase_complete(0, roadmap) is False

    payload["waves"][0]["status"] = "completed"
    payload["waves"][1]["status"] = "completed"
    path2 = _write_payload(tmp_path, payload)
    roadmap2 = load_roadmap(path2)
    assert phase_complete(0, roadmap2) is True

    # Unknown phase — no waves → not complete.
    assert phase_complete(99, roadmap2) is False


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RoadmapValidationError, match="not found"):
        load_roadmap(tmp_path / "nope.yaml")


def test_load_empty_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(RoadmapValidationError, match="empty"):
        load_roadmap(path)


def test_load_non_mapping_root_raises(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(RoadmapValidationError, match="must be a mapping"):
        load_roadmap(path)


def test_validate_phase_waves_undefined(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["phases"][0]["waves"].append("wave-ghost")
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path, strict=False)
    errors = validate_roadmap(roadmap)
    assert any("wave-ghost" in e for e in errors)


def test_validate_decision_blocking_waves_undefined(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["operator_decisions"] = [
        {
            "decision_id": "od_1",
            "title": "x",
            "status": "open",
            "blocking_waves": ["wave-ghost"],
        }
    ]
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path, strict=False)
    errors = validate_roadmap(roadmap)
    assert any("wave-ghost" in e for e in errors)


def test_validate_freeform_blocked_on_label_passes(tmp_path: Path) -> None:
    """Free-form labels (not matching od_/td_<n>) are accepted as external blockers."""
    payload = _minimal_payload()
    payload["waves"][0]["blocked_on"] = ["external_quota_recovery"]
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path)
    errors = validate_roadmap(roadmap)
    # freeform label should NOT be flagged as dangling
    assert not any("external_quota_recovery" in e for e in errors)


def test_dataclass_immutability() -> None:
    w = Wave(wave_id="x", title="t", phase_id=0, status="planned")
    with pytest.raises(Exception):
        w.status = "completed"  # type: ignore[misc]


def test_load_path_must_exist_message(tmp_path: Path) -> None:
    fake = tmp_path / "does-not-exist.yaml"
    with pytest.raises(RoadmapValidationError) as excinfo:
        load_roadmap(fake)
    assert str(fake) in str(excinfo.value)


def test_phase_with_rationale_and_notes(tmp_path: Path) -> None:
    payload = _minimal_payload()
    payload["phases"][0]["rationale"] = "because"
    payload["phases"][0]["notes"] = "see also"
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path)
    assert roadmap.phases[0].rationale == "because"
    assert roadmap.phases[0].notes == "see also"


def test_construct_dataclasses_directly() -> None:
    p = Phase(phase_id=0, title="P", waves=["w"])
    w = Wave(
        wave_id="w",
        title="W",
        phase_id=0,
        status="planned",
        depends_on=[],
        blocked_on=[],
    )
    d = OperatorDecision(decision_id="od_1", title="D", status="open")
    r = Roadmap(
        schema_version=1,
        roadmap_id="rid",
        title="t",
        generated_at="2026-05-06T00:00:00Z",
        phases=[p],
        waves=[w],
        operator_decisions=[d],
    )
    assert validate_roadmap(r) == []


# ---------------------------------------------------------------------------
# Fix-forward regression tests for codex_gate findings on PR #410
# (Phase 2 W-state-1 fix-forward, dispatch 20260506-phase02-wstate1-fixforward)
# ---------------------------------------------------------------------------
def test_write_roadmap_rejects_duplicate_wave_ids_and_leaves_file_unchanged(
    tmp_path: Path,
) -> None:
    """Finding 1 (BLOCKING): write_roadmap() must validate before persisting.

    Synthesize a Roadmap with duplicate wave_ids → write_roadmap raises
    RoadmapValidationError; the target file is not created/modified.
    """
    phase = Phase(phase_id=0, title="P", waves=["wave-a", "wave-a"])
    wave_a = Wave(
        wave_id="wave-a",
        title="First",
        phase_id=0,
        status="planned",
    )
    wave_a_dup = Wave(
        wave_id="wave-a",
        title="Duplicate",
        phase_id=0,
        status="planned",
    )
    bad = Roadmap(
        schema_version=1,
        roadmap_id="bad",
        title="Bad roadmap",
        generated_at="2026-05-06T00:00:00Z",
        phases=[phase],
        waves=[wave_a, wave_a_dup],
    )

    sentinel = "untouched-original-content\n"
    out_path = tmp_path / "preexisting.yaml"
    out_path.write_text(sentinel, encoding="utf-8")

    with pytest.raises(RoadmapValidationError) as excinfo:
        write_roadmap(bad, out_path)
    assert any("duplicate wave_id: wave-a" in e for e in excinfo.value.errors)
    # File must not have been overwritten by the failed write.
    assert out_path.read_text(encoding="utf-8") == sentinel

    # And a fresh path must not be created when validation fails.
    fresh_path = tmp_path / "should-not-exist.yaml"
    with pytest.raises(RoadmapValidationError):
        write_roadmap(bad, fresh_path)
    assert not fresh_path.exists()


def test_write_roadmap_rejects_dangling_depends_on(tmp_path: Path) -> None:
    """write_roadmap() must catch dangling depends_on too, not just dupes."""
    phase = Phase(phase_id=0, title="P", waves=["wave-a"])
    wave = Wave(
        wave_id="wave-a",
        title="W",
        phase_id=0,
        status="planned",
        depends_on=["wave-ghost"],
    )
    bad = Roadmap(
        schema_version=1,
        roadmap_id="bad",
        title="t",
        generated_at="2026-05-06T00:00:00Z",
        phases=[phase],
        waves=[wave],
    )
    out_path = tmp_path / "out.yaml"
    with pytest.raises(RoadmapValidationError) as excinfo:
        write_roadmap(bad, out_path)
    assert any("wave-ghost" in e for e in excinfo.value.errors)
    assert not out_path.exists()


def test_load_roadmap_strict_by_default_raises_on_dangling_depends_on(
    tmp_path: Path,
) -> None:
    """Finding 2 (advisory): load_roadmap() runs validate_roadmap() by default.

    A roadmap with dangling depends_on must raise on load (not silently return
    an invalid Roadmap that only fails when validate_roadmap is called).
    """
    payload = _minimal_payload()
    payload["waves"][1]["depends_on"] = ["wave-zzz"]
    path = _write_payload(tmp_path, payload)
    with pytest.raises(RoadmapValidationError) as excinfo:
        load_roadmap(path)
    assert any("wave-zzz" in e for e in excinfo.value.errors)


def test_load_roadmap_strict_by_default_raises_on_duplicate_wave_id(
    tmp_path: Path,
) -> None:
    payload = _minimal_payload()
    payload["waves"].append(
        {
            "wave_id": "wave-a",
            "title": "Duplicate",
            "phase_id": 0,
            "status": "planned",
        }
    )
    path = _write_payload(tmp_path, payload)
    with pytest.raises(RoadmapValidationError) as excinfo:
        load_roadmap(path)
    assert any("duplicate wave_id: wave-a" in e for e in excinfo.value.errors)


def test_load_roadmap_strict_false_returns_invalid_roadmap(tmp_path: Path) -> None:
    """strict=False is the parse-only mode: returns Roadmap, no cross-check raise."""
    payload = _minimal_payload()
    payload["waves"][1]["depends_on"] = ["wave-zzz"]
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path, strict=False)
    # No raise — and the validator still surfaces the issue when called manually.
    errors = validate_roadmap(roadmap)
    assert any("wave-zzz" in e for e in errors)


def test_load_roadmap_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    """Finding 3 (advisory): only schema_version=1 is supported."""
    payload = _minimal_payload()
    payload["schema_version"] = 2
    path = _write_payload(tmp_path, payload)
    with pytest.raises(RoadmapValidationError) as excinfo:
        load_roadmap(path)
    msg = str(excinfo.value)
    assert "schema_version" in msg
    assert "2" in msg


def test_load_roadmap_rejects_unsupported_schema_version_in_strict_false_mode(
    tmp_path: Path,
) -> None:
    """schema_version enforcement runs before strict cross-checks → strict=False
    cannot bypass it.
    """
    payload = _minimal_payload()
    payload["schema_version"] = 99
    path = _write_payload(tmp_path, payload)
    with pytest.raises(RoadmapValidationError):
        load_roadmap(path, strict=False)


def test_write_roadmap_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    """write_roadmap must also enforce schema_version."""
    phase = Phase(phase_id=0, title="P", waves=["wave-a"])
    wave = Wave(wave_id="wave-a", title="W", phase_id=0, status="planned")
    bad = Roadmap(
        schema_version=2,
        roadmap_id="rid",
        title="t",
        generated_at="2026-05-06T00:00:00Z",
        phases=[phase],
        waves=[wave],
    )
    out_path = tmp_path / "out.yaml"
    with pytest.raises(RoadmapValidationError) as excinfo:
        write_roadmap(bad, out_path)
    assert "schema_version" in str(excinfo.value)
    assert not out_path.exists()


def test_load_roadmap_accepts_schema_version_1(tmp_path: Path) -> None:
    """The supported schema_version=1 still loads cleanly."""
    payload = _minimal_payload()
    payload["schema_version"] = 1
    path = _write_payload(tmp_path, payload)
    roadmap = load_roadmap(path)  # strict default — must not raise
    assert roadmap.schema_version == 1


def test_roadmap_validation_error_exposes_errors_attribute() -> None:
    """RoadmapValidationError.errors must be a list for programmatic access."""
    err = RoadmapValidationError(["a", "b"])
    assert err.errors == ["a", "b"]
    assert "a" in str(err) and "b" in str(err)
    # Single-string form is wrapped in a list too.
    err_single = RoadmapValidationError("plain message")
    assert err_single.errors == ["plain message"]
    assert "plain message" in str(err_single)
