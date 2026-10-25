"""tests/test_skill_injection_seam.py — RS-2: a staged skill reaches the worker
through the ONE prompt seam, never a reviewer, and the door refuses a skill that
does not exist.

ADR-048 (RS-6) made a refactoring method a *skill* bound in the dispatch spec;
RS-1 made the field writable. This file pins the rest: (a) envelope and (b)
provider lanes prepend the skill body before the role block, (c) a reviewer role
never receives it, (d) a spec without one leaves the prompt unchanged, (e)
``compile_plan`` refuses an unknown slug naming both searched paths, and a skill
that stops resolving at delivery logs one WARNING and continues. Every case runs
against a throwaway root: the seam resolves it from ``subprocess_dispatch.__file__``.
"""
from __future__ import annotations

import hashlib
import logging
import sys
from dataclasses import fields
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import envelope_prepare  # noqa: E402
import provider_dispatch  # noqa: E402
import subprocess_dispatch  # noqa: E402
from dispatch_plan import ExecutionPlan, RuntimeSnapshot, compile_plan  # noqa: E402
from dispatch_spec import (  # noqa: E402
    DispatchPath, DispatchSpec, Provider, Reject, ValidatedSpec,
)
from envelope_types import EnvelopeSpec  # noqa: E402

MARKER = "RS2-SKILL-MARKER-4f1c"
SKILL = "tmpskill"
ROLE_HEADER = "# Role: Backend Developer"


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / "skills" / SKILL).mkdir(parents=True)
    (root / "skills" / SKILL / "SKILL.md").write_text(
        f"---\nname: {SKILL}\n---\n\n{MARKER}: apply the refactor-safe method.\n",
        encoding="utf-8",
    )
    fake_file = root / "scripts" / "lib" / "subprocess_dispatch.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.touch()
    monkeypatch.setattr(subprocess_dispatch, "__file__", str(fake_file))
    monkeypatch.setattr(subprocess_dispatch, "_build_intelligence_section", lambda *a, **k: "")
    return root


def _envelope_spec(tmp_path: Path, *, role: str = "backend-developer", skill=SKILL) -> EnvelopeSpec:
    """EnvelopeSpec carrying ``skill`` when the type has it (keeps the RED run readable)."""
    kwargs: dict = dict(
        dispatch_id="rs2-envelope", terminal_id="T1", provider="claude", model="sonnet",
        instruction="Refactor the module.", role=role, pr_id=None,
        state_dir=tmp_path / "state", data_dir=tmp_path / "data",
    )
    if "skill" in {f.name for f in fields(EnvelopeSpec)}:
        kwargs["skill"] = skill
    return EnvelopeSpec(**kwargs)


def _prepare(spec: EnvelopeSpec) -> str:
    with patch("dispatch_enricher.apply_repo_map_layer", side_effect=lambda instr, meta: instr):
        return envelope_prepare._prepare(spec)


def _enrich_via_provider(tmp_path: Path, *, role: str = "backend-developer", skill=SKILL) -> str:
    args = SimpleNamespace(
        provider="kimi", dispatch_id="rs2-provider", terminal_id="T1",
        instruction="Refactor the module.", model="kimi-k2", pr_id=None, dispatch_paths="",
        role=role, no_auto_commit=True, max_retries=1, gate="", deadline_seconds=900, skill=skill,
    )
    with (
        patch.object(provider_dispatch, "_resolve_state_dir", return_value=tmp_path / "state"),
        patch.object(provider_dispatch, "_resolve_data_dir", return_value=tmp_path / "data"),
        patch("dispatch_enricher.apply_repo_map_layer", side_effect=lambda instr, meta: instr),
    ):
        return provider_dispatch._enrich_instruction(args)


def _validated(tmp_path: Path, **overrides) -> ValidatedSpec:
    instruction = "# Refactor the module\n"
    ifile = tmp_path / "instruction.md"
    ifile.write_text(instruction, encoding="utf-8")
    spec = DispatchSpec(
        schema_version=1, project_id="vnx-dev", dispatch_id="rs2-door", staging_id="rs2-door",
        instruction_file=ifile, role="backend-developer", target_slot="T1", gate="codex_gate",
        dispatch_paths=(DispatchPath(PurePosixPath("scripts/lib/example.py")),),
        provider=Provider.CLAUDE, **overrides,
    )
    return ValidatedSpec(
        spec=spec, instruction_text=instruction, normalized_paths=(),
        instruction_sha256=hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
    )


def _snapshot(root: Path) -> RuntimeSnapshot:
    # repo_root only once the snapshot type carries it (keeps the RED run readable)
    kwargs = {"repo_root": root} if "repo_root" in {f.name for f in fields(RuntimeSnapshot)} else {}
    return RuntimeSnapshot(
        staging_promoted=True, target_health={"T1": "healthy"},
        target_capable={"T1": True}, **kwargs,
    )


def test_envelope_prepare_injects_the_skill_before_the_role_block(
    project_root: Path, tmp_path: Path
) -> None:
    prompt = _prepare(_envelope_spec(tmp_path))
    assert MARKER in prompt, (
        f"envelope_prepare._prepare dropped the skill from {project_root}/skills/{SKILL}/SKILL.md"
    )
    assert prompt.index(MARKER) < prompt.index(ROLE_HEADER), "skill must precede the role block"


def test_provider_lane_injects_the_skill(project_root: Path, tmp_path: Path) -> None:
    prompt = _enrich_via_provider(tmp_path)
    assert MARKER in prompt, (
        f"provider_dispatch._enrich_instruction dropped the skill ({project_root})"
    )
    assert prompt.index(MARKER) < prompt.index(ROLE_HEADER)


@pytest.mark.parametrize("role", ["review-gate", "plan-reviewer"])
def test_reviewer_role_never_receives_the_skill(project_root: Path, tmp_path: Path, role: str) -> None:
    prompt = _prepare(_envelope_spec(tmp_path, role=role))
    assert MARKER not in prompt, (
        f"role={role} is a reviewer role — enrichment_allowed() must suppress the skill (D1)"
    )
    assert "Refactor the module." in prompt


def test_no_skill_leaves_the_prompt_unchanged(project_root: Path, tmp_path: Path) -> None:
    with patch("skill_prefix.build_skill_prefix",
               side_effect=AssertionError("build_skill_prefix called with no staged skill")):
        prompt = _prepare(_envelope_spec(tmp_path, skill=None))
    assert prompt.startswith("# Base Worker Context"), f"prompt opens on {prompt[:60]!r}"
    assert "# SKILL:" not in prompt


def test_door_refuses_a_skill_that_exists_in_neither_tree(
    project_root: Path, tmp_path: Path
) -> None:
    result = compile_plan(_validated(tmp_path, skill="does-not-exist"), _snapshot(project_root))
    assert isinstance(result, Reject), (
        "compile_plan accepted a hand-written spec naming an unknown skill — the door "
        "must refuse it, not discover it at delivery"
    )
    assert result.code == "unknown-skill"
    assert str(project_root / "skills" / "does-not-exist" / "SKILL.md") in result.reason
    assert str(project_root / ".claude" / "skills" / "does-not-exist" / "SKILL.md") in result.reason


def test_door_accepts_a_known_skill_and_ignores_an_absent_one(
    project_root: Path, tmp_path: Path
) -> None:
    result = compile_plan(_validated(tmp_path, skill=SKILL), _snapshot(project_root))
    assert isinstance(result, ExecutionPlan), f"expected a plan, got {result!r}"
    assert result.skill == SKILL, "the plan must carry the skill to the lane"
    plain = compile_plan(_validated(tmp_path), _snapshot(project_root))
    assert isinstance(plain, ExecutionPlan), f"expected a plan, got {plain!r}"
    assert plain.skill is None


def test_skill_that_no_longer_resolves_logs_one_warning_and_continues(
    project_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="skill_context"):
        prompt = _prepare(_envelope_spec(tmp_path, skill="gone-skill"))
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "gone-skill" in r.getMessage()]
    assert len(warnings) == 1, f"expected one WARNING for the slug, got {caplog.records}"
    assert MARKER not in prompt
    assert "Refactor the module." in prompt
