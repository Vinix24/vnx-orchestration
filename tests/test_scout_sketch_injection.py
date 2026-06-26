#!/usr/bin/env python3
"""Tests for the scout pre-pass sidecar contract + injection path (build-step 5a).

Dispatch-ID: 20260626-scout-sidecar-contract

Covers the consumption side:
- scout_prepass: sidecar path, fail-open read, defensive normalization, bounded render
- scout_sketch source: build_scout_sketch_item returns an item or None
- format_intelligence_items: scout_sketch renders, and leads the direct-injection block
- IntelligenceSelector.select: a sidecar surfaces as a scout_sketch item in the result
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import scout_prepass  # noqa: E402
from intelligence_sources.scout_sketch import build_scout_sketch_item  # noqa: E402
from intelligence_injection import format_intelligence_items  # noqa: E402


def _write_sidecar(state_dir: Path, dispatch_id: str, payload: dict) -> Path:
    path = scout_prepass.scout_sidecar_path(state_dir, dispatch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Self-identify so the read-side dispatch_id check passes for the id we wrote under.
    body = {**payload, "dispatch_id": dispatch_id}
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


_GOOD_SIDECAR = {
    "schema_version": 1,
    "dispatch_id": "D-1",
    "generated_at": "2026-06-26T12:00:00+00:00",
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "include": [{"ref": "scripts/lib/foo.py:10-20", "why": "core change site"}],
    "maybe": [{"ref": "scripts/lib/bar.py:50-60", "why": "caller"}],
    "exclude": [{"ref": "scripts/lib/legacy.py:1-5", "why": "unrelated"}],
    "tests": ["tests/test_foo.py"],
    "docs": ["docs/foo.md"],
    "plan_sketch": "Edit foo, update caller, add a test.",
}


# ---------------------------------------------------------------------------
# scout_prepass: path + read
# ---------------------------------------------------------------------------

def test_sidecar_path_shape(tmp_path):
    p = scout_prepass.scout_sidecar_path(tmp_path, "D-abc")
    assert p == tmp_path / "scout" / "D-abc.json"


def test_read_missing_sidecar_is_none(tmp_path):
    assert scout_prepass.read_scout_sidecar(tmp_path, "nope") is None


@pytest.mark.parametrize("evil", ["../../etc/passwd", "..", ".", "a/b", "x\\y", "/abs", ""])
def test_sidecar_path_rejects_unsafe_dispatch_id(tmp_path, evil):
    """A dispatch_id that is not a safe path segment must not build a path."""
    with pytest.raises(ValueError):
        scout_prepass.scout_sidecar_path(tmp_path, evil)


def test_read_unsafe_dispatch_id_fails_open(tmp_path):
    """Traversal id on the read path degrades to None, never reads outside scope."""
    assert scout_prepass.read_scout_sidecar(tmp_path, "../../etc/passwd") is None


def test_read_none_state_dir_is_none():
    assert scout_prepass.read_scout_sidecar(None, "D-1") is None


def test_read_malformed_json_is_none(tmp_path):
    path = scout_prepass.scout_sidecar_path(tmp_path, "D-bad")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert scout_prepass.read_scout_sidecar(tmp_path, "D-bad") is None


def test_read_non_object_is_none(tmp_path):
    path = scout_prepass.scout_sidecar_path(tmp_path, "D-arr")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert scout_prepass.read_scout_sidecar(tmp_path, "D-arr") is None


def test_read_good_sidecar_roundtrips(tmp_path):
    _write_sidecar(tmp_path, "D-1", _GOOD_SIDECAR)
    data = scout_prepass.read_scout_sidecar(tmp_path, "D-1")
    assert data["provider"] == "deepseek"


def test_read_rejects_future_schema_version(tmp_path):
    path = scout_prepass.scout_sidecar_path(tmp_path, "D-fut")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 999, "dispatch_id": "D-fut"}), encoding="utf-8")
    assert scout_prepass.read_scout_sidecar(tmp_path, "D-fut") is None


def test_read_rejects_dispatch_id_mismatch(tmp_path):
    path = scout_prepass.scout_sidecar_path(tmp_path, "D-here")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sidecar self-identifies as a DIFFERENT dispatch → must not be misread.
    path.write_text(json.dumps({"schema_version": 1, "dispatch_id": "D-other",
                                "include": [{"ref": "a.py:1-2"}]}), encoding="utf-8")
    assert scout_prepass.read_scout_sidecar(tmp_path, "D-here") is None


# ---------------------------------------------------------------------------
# scout_prepass: normalize + render
# ---------------------------------------------------------------------------

def test_normalize_coerces_and_caps():
    messy = {
        "include": ["scripts/a.py:1-2", {"ref": "scripts/b.py:3-4", "why": "x" * 500}],
        "maybe": "not a list",
        "tests": ["t1", "t2", "t3", "t4", "t5", "t6", "t7"],  # > cap
        "plan_sketch": "y" * 1000,
    }
    norm = scout_prepass.normalize_sidecar(messy)
    assert norm["include"][0] == {"ref": "scripts/a.py:1-2", "why": ""}
    assert len(norm["include"][1]["why"]) <= 120
    assert norm["maybe"] == []  # non-list coerced to empty
    assert len(norm["tests"]) <= 5
    assert len(norm["plan_sketch"]) <= 300


def test_format_renders_include_maybe_tests_docs_plan():
    out = scout_prepass.format_scout_sketch(_GOOD_SIDECAR)
    assert "SCOUT PRE-PASS" in out
    assert "scripts/lib/foo.py:10-20" in out
    assert "core change site" in out
    assert "scripts/lib/bar.py:50-60" in out
    assert "tests/test_foo.py" in out
    assert "docs/foo.md" in out
    assert "Edit foo" in out


def test_format_omits_exclude():
    out = scout_prepass.format_scout_sketch(_GOOD_SIDECAR)
    # EXCLUDE refs are deliberately not injected (noise the worker shouldn't anchor on).
    assert "legacy.py" not in out


def test_format_empty_sidecar_is_blank():
    assert scout_prepass.format_scout_sketch({}) == ""
    assert scout_prepass.format_scout_sketch({"exclude": [{"ref": "x:1-2"}]}) == ""


def test_format_is_bounded():
    big = {
        "provider": "deepseek",
        "include": [{"ref": f"scripts/f{i}.py:1-9", "why": "w" * 119} for i in range(8)],
        "maybe": [{"ref": f"scripts/g{i}.py:1-9", "why": "w" * 119} for i in range(8)],
        "plan_sketch": "p" * 300,
    }
    out = scout_prepass.format_scout_sketch(big)
    assert len(out) <= scout_prepass.SCOUT_SKETCH_MAX_CHARS
    # truncation lands on a line boundary (no dangling half-pointer backtick line)
    assert not out.endswith("`")


def test_evidence_count_counts_include_and_maybe():
    assert scout_prepass.sidecar_evidence_count(_GOOD_SIDECAR) == 2


# ---------------------------------------------------------------------------
# scout_sketch source builder
# ---------------------------------------------------------------------------

def test_build_item_none_without_sidecar(tmp_path):
    assert build_scout_sketch_item(tmp_path, "absent", "2026-06-26T00:00:00Z") is None


def test_build_item_none_for_none_state_dir():
    assert build_scout_sketch_item(None, "D-1", "2026-06-26T00:00:00Z") is None


def test_build_item_from_sidecar(tmp_path):
    _write_sidecar(tmp_path, "D-1", _GOOD_SIDECAR)
    item = build_scout_sketch_item(tmp_path, "D-1", "2026-06-26T00:00:00Z")
    assert item is not None
    assert item.item_class == "scout_sketch"
    assert item.item_id == "intel_scout_D-1"
    assert item.evidence_count == 2
    assert item.last_seen == "2026-06-26T12:00:00+00:00"  # from generated_at
    assert "scripts/lib/foo.py:10-20" in item.content
    assert item.source_refs == ["scripts/lib/foo.py:10-20"]


# ---------------------------------------------------------------------------
# render integration
# ---------------------------------------------------------------------------

def test_format_intelligence_items_renders_scout_sketch_first(tmp_path):
    _write_sidecar(tmp_path, "D-1", _GOOD_SIDECAR)
    scout = build_scout_sketch_item(tmp_path, "D-1", "2026-06-26T00:00:00Z")

    class _Anchor:
        item_class = "code_anchor"
        title = "anchors"
        content = "## CODE ANCHORS\n- `scripts/lib/zzz.py:1-2`"

    rendered = format_intelligence_items([_Anchor(), scout])
    assert "SCOUT PRE-PASS" in rendered
    assert "CODE ANCHORS" in rendered
    # scout_sketch leads the direct-injection block
    assert rendered.index("SCOUT PRE-PASS") < rendered.index("CODE ANCHORS")


# ---------------------------------------------------------------------------
# selector integration — a sidecar surfaces as a scout_sketch item
# ---------------------------------------------------------------------------

def test_selector_surfaces_scout_sketch(tmp_path):
    from intelligence_selector import IntelligenceSelector

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_sidecar(state_dir, "D-sel", _GOOD_SIDECAR)

    selector = IntelligenceSelector(
        quality_db_path=state_dir / "quality_intelligence.db",  # absent → no standard items
        coord_db_state_dir=state_dir,
    )
    try:
        result = selector.select(
            dispatch_id="D-sel",
            injection_point="dispatch_create",
            skill_name="python-expert",
            dispatch_paths=[],
            instruction_text="",
        )
    finally:
        selector.close()

    classes = [i.item_class for i in result.items]
    assert "scout_sketch" in classes
    scout = next(i for i in result.items if i.item_class == "scout_sketch")
    assert "scripts/lib/foo.py:10-20" in scout.content


def test_selector_no_sidecar_no_scout_item(tmp_path):
    from intelligence_selector import IntelligenceSelector

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    selector = IntelligenceSelector(
        quality_db_path=state_dir / "quality_intelligence.db",
        coord_db_state_dir=state_dir,
    )
    try:
        result = selector.select(
            dispatch_id="D-none",
            injection_point="dispatch_create",
            skill_name="python-expert",
            dispatch_paths=[],
            instruction_text="",
        )
    finally:
        selector.close()
    assert "scout_sketch" not in [i.item_class for i in result.items]
