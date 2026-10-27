"""tests/test_dispatch_skill_staging.py — RS-1: a staged spec can carry a skill.

Before this change ``DispatchSpec.skill`` was declared and parsed by
``dispatch_cli.load_spec``, but nothing could WRITE it: ``stage_spec_bundle`` had no
``skill`` parameter and its payload carried no ``skill`` key, so no bridge-staged
dispatch could ever name one (``skill_prefix`` had zero production callers as well).

This file pins both halves of the fix:

  1. staging carries the skill onto ``dispatch-spec.json`` and refuses a name that
     exists in neither skill tree (``<root>/skills/<name>/SKILL.md`` or
     ``<root>/.claude/skills/<name>/SKILL.md``) BEFORE writing a bundle;
  2. ``dispatch_spec.validate()`` Rule 17 refuses a malformed slug. Existence is
     explicitly NOT part of validate() — it is a staging/compile-time rule by design.

The project root is injected through ``dispatch_bridge._project_root`` so every case
runs against a throwaway tree and never the real checkout.
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_bridge  # noqa: E402
from dispatch_spec import (  # noqa: E402
    DispatchPath,
    DispatchSpec,
    Reject,
    ValidatedSpec,
    validate,
)

_GOOD_ID = "20261025-120000-rs1-skill"
_SKILL = "refactor-safe"


def _accepts_skill() -> bool:
    """Whether the bridge under test has the ``skill`` parameter at all.

    On origin/main it does not. Every staging case passes the kwarg only through this
    guard, so the RED run fails on the VALUE the spec carries (a missing key) instead
    of on a collection-time TypeError, and the reason stays readable.
    """
    return "skill" in inspect.signature(dispatch_bridge.stage_spec_bundle).parameters


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project root the bridge checks skill existence against."""
    root = tmp_path / "project"
    root.mkdir()
    # raising=False keeps the RED run readable: the old head has no such seam, and the
    # case must fail on the VALUE it asserts, not on fixture setup.
    monkeypatch.setattr(dispatch_bridge, "_project_root", lambda: root, raising=False)
    return root


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _skill_file(root: Path, name: str, tree: str = "skills") -> Path:
    folder = root / tree / name
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_text(f"---\nname: {name}\n---\n\nDo the thing.\n", encoding="utf-8")
    return path


def _stage_kwargs(data_dir: Path, **over) -> dict:
    kwargs = dict(
        instruction_text="do the thing",
        dispatch_id=_GOOD_ID,
        role="backend-developer",
        target_slot="T1",
        project_id="p1",
        provider="claude",
        data_dir=data_dir,
    )
    kwargs.update(over)
    return kwargs


def _pending_bundle(data_dir: Path) -> Path:
    return data_dir / "dispatches" / "pending" / _GOOD_ID


# ---------------------------------------------------------------------------
# 1. staging carries the skill
# ---------------------------------------------------------------------------


def test_skill_under_canonical_tree_lands_in_the_spec(project_root: Path, data_dir: Path) -> None:
    _skill_file(project_root, _SKILL)
    kwargs = _stage_kwargs(data_dir)
    if _accepts_skill():
        kwargs["skill"] = _SKILL
    payload = json.loads(dispatch_bridge.stage_spec_bundle(**kwargs).read_text(encoding="utf-8"))
    # On the old head the key is absent entirely — this raises KeyError, the measured gap.
    assert payload["skill"] == _SKILL


def test_skill_under_claude_tree_lands_in_the_spec(project_root: Path, data_dir: Path) -> None:
    _skill_file(project_root, _SKILL, tree=".claude/skills")
    kwargs = _stage_kwargs(data_dir)
    if _accepts_skill():
        kwargs["skill"] = _SKILL
    payload = json.loads(dispatch_bridge.stage_spec_bundle(**kwargs).read_text(encoding="utf-8"))
    assert payload["skill"] == _SKILL


# ---------------------------------------------------------------------------
# 2. an unknown skill is a typed refusal, before anything is written
# ---------------------------------------------------------------------------


def test_unknown_skill_is_refused_before_anything_is_written(
    project_root: Path, data_dir: Path
) -> None:
    if not _accepts_skill():
        # The old head cannot even express the name: the bundle stages silently with
        # no skill field at all, which is precisely the gap this dispatch closes.
        staged = json.loads(
            dispatch_bridge.stage_spec_bundle(**_stage_kwargs(data_dir)).read_text(encoding="utf-8")
        )
        assert "skill" not in staged
        pytest.fail(
            "stage_spec_bundle has no skill parameter: an unknown skill stages silently "
            "instead of being refused with both searched paths"
        )
    with pytest.raises(dispatch_bridge.UnknownSkillError) as excinfo:
        dispatch_bridge.stage_spec_bundle(**_stage_kwargs(data_dir, skill="no-such-skill"))
    message = str(excinfo.value)
    assert str(project_root / "skills" / "no-such-skill" / "SKILL.md") in message
    assert str(project_root / ".claude" / "skills" / "no-such-skill" / "SKILL.md") in message
    assert not _pending_bundle(data_dir).exists()


@pytest.mark.parametrize("bad", ["../etc", "a/b", "a..b", "-lead", ".lead"])
def test_malformed_skill_slug_is_refused(project_root: Path, data_dir: Path, bad: str) -> None:
    kwargs = _stage_kwargs(data_dir)
    if _accepts_skill():
        kwargs["skill"] = bad
    with pytest.raises(ValueError):
        dispatch_bridge.stage_spec_bundle(**kwargs)
    assert not _pending_bundle(data_dir).exists()


# ---------------------------------------------------------------------------
# 3. no skill: the bundle is unchanged apart from the explicit null
# ---------------------------------------------------------------------------


def test_no_skill_bundle_differs_from_a_skill_bundle_only_in_skill(
    project_root: Path, data_dir: Path
) -> None:
    _skill_file(project_root, _SKILL)
    kwargs = _stage_kwargs(data_dir)
    if _accepts_skill():
        kwargs["skill"] = _SKILL
    with_skill = json.loads(dispatch_bridge.stage_spec_bundle(**kwargs).read_text(encoding="utf-8"))

    # Same arguments into the same pending/<id> dir, so every other field — including
    # the absolute instruction_file path and its sha — is identical.
    plain = json.loads(
        dispatch_bridge.stage_spec_bundle(**_stage_kwargs(data_dir)).read_text(encoding="utf-8")
    )
    assert plain["skill"] is None
    assert with_skill["skill"] == _SKILL
    plain.pop("skill")
    with_skill.pop("skill")
    assert plain == with_skill


# ---------------------------------------------------------------------------
# 4. validate() Rule 17 — slug format only, existence stays a staging rule
# ---------------------------------------------------------------------------


def _valid_spec(instruction_file: Path, **overrides) -> DispatchSpec:
    defaults: dict = dict(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="20261025-test-rs1",
        staging_id="20261025-test-rs1",
        instruction_file=instruction_file,
        role="backend-developer",
        target_slot="T1",
        gate="codex_gate",
        dispatch_paths=(DispatchPath(PurePosixPath("scripts/lib/foo.py")),),
    )
    defaults.update(overrides)
    return DispatchSpec(**defaults)


def _validate(spec: DispatchSpec):
    return validate(spec, project_id="vnx-dev", repo_root=Path("/fake/repo"))


def test_validate_accepts_a_well_formed_skill_slug(tmp_path: Path) -> None:
    # Existence is NOT checked here: no such skill exists under /fake/repo.
    ifile = tmp_path / "instruction.md"
    ifile.write_text("Do the work.", encoding="utf-8")
    result = _validate(_valid_spec(ifile, skill=_SKILL))
    assert isinstance(result, ValidatedSpec)
    assert result.spec.skill == _SKILL


@pytest.mark.parametrize("bad", ["../etc", "a/b", "a..b", "-lead", ".lead", ""])
def test_validate_rejects_a_malformed_skill_slug(tmp_path: Path, bad: str) -> None:
    ifile = tmp_path / "instruction.md"
    ifile.write_text("Do the work.", encoding="utf-8")
    result = _validate(_valid_spec(ifile, skill=bad))
    assert isinstance(result, Reject)
    assert result.code == "bad-skill"
