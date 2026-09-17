"""test_dispatch_oi1756_task_class_validation.py — OI-1756: an unknown task_class
is refused loud, at the door AND at the routing lookup, instead of silently
producing zero routing candidates.

Two independent loci, both closed here:
  1. smart_router.recommend() used to do ``recs.get(task_class, [])`` — an
     unknown/typo'd class (e.g. the legacy "implementation" string) and a real
     class with zero surviving candidates both returned an empty list,
     indistinguishable from each other. recommend() now raises
     UnknownTaskClassError for the first case; the second still returns [].
  2. compile_plan (mirroring OI-921's valid_roles pattern exactly) now rejects
     an explicit DispatchSpec.task_class that is not a key in
     routing_recommendations.yaml's routing_by_task — BEFORE the dispatch is
     ever accepted, not discovered later as an empty candidate list. None (no
     task_class declared) is always valid.

Tests in TestRecommendUnknownTaskClass and TestCompilePlanTaskClassRegistry are
red on the pre-fix source (real AssertionError — "DID NOT RAISE" / "plan is not
a Reject" — never a TypeError/ImportError/AttributeError, since every symbol
referenced already existed before this dispatch). TestMinQualityTierBounds is
the same for the min_quality_tier=0 defect (point 4 of the dispatch).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
import yaml

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_cli  # noqa: E402
from dispatch_plan import RuntimeSnapshot, compile_plan  # noqa: E402
from dispatch_spec import (  # noqa: E402
    DispatchSpec,
    Provider,
    Reject,
    ValidatedSpec,
)
from smart_router import recommend  # noqa: E402

_REPO_ROOT = _LIB.parents[1]

# The registry-discovery helper is new in OI-1756; on pre-fix source the symbol
# does not exist, so its own tests are skipped there (the red-on-pre-fix proof
# for this item is the compile_plan-unknown-task-class test, not discovery).
_DISCOVER_VALID_TASK_CLASSES = getattr(dispatch_cli, "_discover_valid_task_classes", None)


# ---------------------------------------------------------------------------
# 1. smart_router.recommend() — unknown class raises, empty class doesn't
# ---------------------------------------------------------------------------

def _write_yaml(tmp_path: Path, routing_by_task: dict) -> Path:
    p = tmp_path / "routing_recommendations.yaml"
    p.write_text(yaml.dump({"routing_by_task": routing_by_task}, default_flow_style=False), encoding="utf-8")
    return p


class TestRecommendUnknownTaskClass:
    def test_unknown_task_class_raises_not_silently_empty(self, tmp_path):
        """Red on pre-fix code: recommend() returns [] for an unknown class
        instead of raising — a typo (e.g. --task-class implementation) then
        looks identical to a real class with zero configured candidates."""
        rec_path = _write_yaml(tmp_path, {
            "01_code_generation": [
                {"model_id": "claude-sonnet-5", "composite_score": 9.0,
                 "avg_duration_seconds": 100.0},
            ],
        })
        with pytest.raises(ValueError, match="99_nonexistent"):
            recommend("99_nonexistent", recommendations_path=rec_path)

    def test_unknown_task_class_error_names_valid_classes(self, tmp_path):
        rec_path = _write_yaml(tmp_path, {
            "01_code_generation": [
                {"model_id": "claude-sonnet-5", "composite_score": 9.0,
                 "avg_duration_seconds": 100.0},
            ],
            "02_code_review": [
                {"model_id": "claude-opus-4-8", "composite_score": 9.0,
                 "avg_duration_seconds": 100.0},
            ],
        })
        with pytest.raises(ValueError) as excinfo:
            recommend("implementation", recommendations_path=rec_path)
        msg = str(excinfo.value)
        assert "01_code_generation" in msg
        assert "02_code_review" in msg

    def test_declared_but_empty_class_still_returns_empty_list(self, tmp_path):
        """A class that IS a routing_by_task key, with zero candidates, must
        stay a legitimate empty list — never conflated with 'class doesn't
        exist' (that case is covered by test_unknown_task_class_raises above)."""
        rec_path = _write_yaml(tmp_path, {"05_debugging": []})
        assert recommend("05_debugging", recommendations_path=rec_path) == []

    def test_error_type_is_subclass_of_value_error(self, tmp_path):
        """Only meaningful post-fix — UnknownTaskClassError is the concrete type."""
        from smart_router import UnknownTaskClassError
        rec_path = _write_yaml(tmp_path, {"01_code_generation": []})
        with pytest.raises(UnknownTaskClassError):
            recommend("not-a-real-class", recommendations_path=rec_path)


# ---------------------------------------------------------------------------
# 2. min_quality_tier / max_quality_tier bounds (point 4 of the dispatch)
# ---------------------------------------------------------------------------

class TestMinQualityTierBounds:
    def test_min_quality_tier_zero_rejected(self, tmp_path):
        """Red on pre-fix code: min_quality_tier=0 silently filters nothing
        (every quality_tier is already >= 1) while reading as a valid floor —
        recommend() must reject it the same way an out-of-range quality_tier
        (e.g. 5) is already rejected."""
        rec_path = _write_yaml(tmp_path, {
            "02_code_review": {
                "candidates": [
                    {"model_id": "claude-opus-4-8", "composite_score": 9.0,
                     "avg_duration_seconds": 100.0},
                ],
                "min_quality_tier": 0,
            },
        })
        with pytest.raises(ValueError, match="min_quality_tier"):
            recommend("02_code_review", recommendations_path=rec_path)

    def test_max_quality_tier_zero_rejected(self, tmp_path):
        rec_path = _write_yaml(tmp_path, {
            "02_code_review": {
                "candidates": [
                    {"model_id": "claude-opus-4-8", "composite_score": 9.0,
                     "avg_duration_seconds": 100.0},
                ],
                "max_quality_tier": 0,
            },
        })
        with pytest.raises(ValueError, match="max_quality_tier"):
            recommend("02_code_review", recommendations_path=rec_path)

    def test_min_quality_tier_valid_bound_still_works(self, tmp_path):
        """No-regression guard: the existing 1-3 bounds still filter normally."""
        rec_path = _write_yaml(tmp_path, {
            "02_code_review": {
                "candidates": [
                    {"model_id": "premium", "composite_score": 9.0, "avg_duration_seconds": 90.0},
                    {"model_id": "low", "composite_score": 1.0, "avg_duration_seconds": 10.0},
                ],
                "min_quality_tier": 3,
            },
        })
        ids = [c.model_id for c in recommend("02_code_review", recommendations_path=rec_path)]
        assert ids == ["premium"]

    def test_min_quality_tier_string_is_coerced_like_per_candidate_quality_tier(self, tmp_path):
        """glm_gate finding (severity info, this dispatch): the bounds check must
        coerce a quoted YAML scalar (e.g. `min_quality_tier: "3"`) the same way
        the per-candidate `quality_tier` check already does (`int(entry["quality_tier"])`)
        — before the fix, the bounds check rejected a string while the
        per-candidate check accepted one, an asymmetry within the same PR."""
        rec_path = _write_yaml(tmp_path, {
            "02_code_review": {
                "candidates": [
                    {"model_id": "premium", "composite_score": 9.0, "avg_duration_seconds": 90.0},
                    {"model_id": "low", "composite_score": 1.0, "avg_duration_seconds": 10.0},
                ],
                "min_quality_tier": "3",
            },
        })
        ids = [c.model_id for c in recommend("02_code_review", recommendations_path=rec_path)]
        assert ids == ["premium"]

    def test_min_quality_tier_non_numeric_string_still_rejected(self, tmp_path):
        """A bound that cannot be coerced to int at all is still a hard error,
        not silently ignored."""
        rec_path = _write_yaml(tmp_path, {
            "02_code_review": {
                "candidates": [
                    {"model_id": "claude-opus-4-8", "composite_score": 9.0,
                     "avg_duration_seconds": 100.0},
                ],
                "min_quality_tier": "high",
            },
        })
        with pytest.raises(ValueError, match="min_quality_tier"):
            recommend("02_code_review", recommendations_path=rec_path)


# ---------------------------------------------------------------------------
# 3. compile_plan — the door's task_class closed-set check
# ---------------------------------------------------------------------------

def _snapshot(valid_task_classes) -> RuntimeSnapshot:
    """Build a promoted RuntimeSnapshot carrying the task_class registry.

    Compatible with pre-fix source, where RuntimeSnapshot has no
    ``valid_task_classes`` field: a TypeError there falls back to a plain
    snapshot, so the red-on-pre-fix proof is the assertion failure ("pre-fix
    code does not reject"), never a collection/kwarg error.
    """
    try:
        return RuntimeSnapshot(staging_promoted=True, valid_task_classes=valid_task_classes)
    except TypeError:  # pre-fix source: no valid_task_classes field
        return RuntimeSnapshot(staging_promoted=True)


def _make_vspec(*, task_class, tmp_path: Path) -> ValidatedSpec:
    ifile = tmp_path / "instruction.md"
    ifile.write_text("# Do the work\n", encoding="utf-8")
    spec = DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="oi1756-test-dispatch",
        staging_id="oi1756-test-staging",
        instruction_file=ifile,
        role="backend-developer",
        target_slot="T1",
        gate="codex_gate",
        dispatch_paths=(),
        provider=Provider.CLAUDE,
        model=None,
        task_class=task_class,
    )
    text = ifile.read_text(encoding="utf-8")
    return ValidatedSpec(
        spec=spec,
        instruction_text=text,
        normalized_paths=(),
        instruction_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


class TestCompilePlanTaskClassRegistry:
    _CLASSES = frozenset({"01_code_generation", "02_code_review", "05_debugging"})

    def test_unknown_task_class_is_rejected(self, tmp_path):
        """Red on pre-fix code: compile_plan accepts ANY task_class string."""
        vspec = _make_vspec(task_class="implementation", tmp_path=tmp_path)
        result = compile_plan(vspec, _snapshot(self._CLASSES))
        assert isinstance(result, Reject)
        assert result.code == "unknown-task-class"
        assert "implementation" in result.reason
        assert "01_code_generation" in result.reason

    def test_none_task_class_is_always_valid(self, tmp_path):
        """A dispatch that declares no task_class at all is never rejected —
        the check fires on an unknown STRING, never on absence."""
        vspec = _make_vspec(task_class=None, tmp_path=tmp_path)
        result = compile_plan(vspec, _snapshot(frozenset()))
        assert not isinstance(result, Reject)

    def test_known_task_class_is_accepted(self, tmp_path):
        vspec = _make_vspec(task_class="02_code_review", tmp_path=tmp_path)
        result = compile_plan(vspec, _snapshot(self._CLASSES))
        assert not isinstance(result, Reject)

    def test_empty_registry_fails_closed_for_explicit_class(self, tmp_path):
        """An empty valid_task_classes set (undiscoverable routing table)
        rejects every EXPLICIT task_class — same fail-closed convention as
        OI-921's valid_roles."""
        vspec = _make_vspec(task_class="01_code_generation", tmp_path=tmp_path)
        result = compile_plan(vspec, _snapshot(frozenset()))
        assert isinstance(result, Reject)
        assert result.code == "unknown-task-class"

    def test_without_valid_task_classes_keeps_legacy_behavior(self, tmp_path):
        """valid_task_classes=None (registry not provided) skips the check —
        direct callers and pre-OI-1756 tests keep working unchanged."""
        vspec = _make_vspec(task_class="not-a-real-class", tmp_path=tmp_path)
        result = compile_plan(vspec, RuntimeSnapshot(staging_promoted=True))
        assert not isinstance(result, Reject)


# ---------------------------------------------------------------------------
# 4. dispatch_cli: the door's snapshot carries the task_class registry
# ---------------------------------------------------------------------------

class TestDiscoverValidTaskClasses:
    pytestmark = pytest.mark.skipif(
        _DISCOVER_VALID_TASK_CLASSES is None,
        reason="OI-1756 registry discovery not present on this source revision",
    )

    def test_discovers_real_routing_recommendations_registry(self):
        """The shipped routing_recommendations.yaml yields exactly the 7
        documented classes — the same closed set smart_router.TASK_CLASSES
        already documents (no second hardcoded list)."""
        from smart_router import TASK_CLASSES, valid_task_classes as sr_valid_task_classes

        classes = _DISCOVER_VALID_TASK_CLASSES()
        assert classes == sr_valid_task_classes()
        assert set(TASK_CLASSES) == set(classes)
        assert "01_code_generation" in classes
        assert "implementation" not in classes

    def test_missing_registry_file_is_empty(self, tmp_path):
        """A missing/unreadable routing_recommendations.yaml → empty set
        (compile_plan fails closed on any EXPLICIT task_class).

        Fix-forward (dispatch-20260917-oi1756-fixforward): passes the absent
        path directly rather than monkeypatching smart_router's module-global
        `_RECOMMENDATIONS_PATH`. `smart_router` and `lib.smart_router` are two
        distinct module objects with independent globals when both are on
        sys.path (as happens under a full test sweep) — patching one identity's
        global left `_discover_valid_task_classes()` free to resolve the OTHER
        identity, which still pointed at the real YAML, so this test was green
        solo and red under a sweep. A path parameter has no such ambiguity: mirrors
        _discover_valid_roles(agents_dir), which is immune for the same reason.
        """
        assert _DISCOVER_VALID_TASK_CLASSES(tmp_path / "absent.yaml") == frozenset()
