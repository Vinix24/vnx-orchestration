"""The shared fail-closed block reaches every build role on every lane, once.

dlv-48b4e15d7ebc introduced the block; dlv-4457d0e83469 (OI-2318) closed the
fallback hole: a build-role prompt no longer drops the block when skill
injection fails, and a missing block file now fails the dispatch before any
worker is spawned.
"""

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

import fail_closed_evidence as fce
import provider_dispatch
import subprocess_dispatch as sd
from envelope_prepare import _prepare

REPO = Path(__file__).resolve().parent.parent

# Parametrize from the positive list itself: a role added to BUILD_ROLES is
# covered automatically instead of silently untested (the five skill-only roles
# were: api-developer, debugger, python-optimizer, supabase-expert,
# performance-profiler).
BUILD_ROLES = sorted(fce.BUILD_ROLES)
REVIEWER_ROLES = ["reviewer", "review-gate", "plan-reviewer", "code-reviewer", "deliberation-panelist"]


def _headless(role: str) -> str:
    spec = SimpleNamespace(
        instruction="Do X", role=role, dispatch_id="d1", model="sonnet",
        provider="claude", terminal_id="T1", pr_id=None,
    )
    return _prepare(spec)


def _provider(role: str) -> str:
    args = argparse.Namespace(
        instruction="Do X", role=role, dispatch_id="d1", terminal_id="T1",
        pr_id=None, dispatch_paths="",
    )
    return provider_dispatch._enrich_instruction(args)


LANES = {"headless": _headless, "provider": _provider}


@pytest.fixture(autouse=True)
def _no_enrichment():
    with patch.object(sd, "_build_intelligence_section", return_value=""), \
            patch("dispatch_enricher.apply_repo_map_layer", side_effect=lambda i, m: i):
        yield


@pytest.mark.parametrize("lane", sorted(LANES))
@pytest.mark.parametrize("role", BUILD_ROLES)
def test_build_role_gets_block_exactly_once(lane, role):
    prompt = LANES[lane](role)
    assert prompt.count(fce.BLOCK_HEADING) == 1
    assert fce.load_block() in prompt


def test_build_roles_has_14_members():
    """A removal from BUILD_ROLES must be a visible, deliberate edit."""
    assert len(fce.BUILD_ROLES) == 14


@pytest.mark.parametrize("lane", sorted(LANES))
@pytest.mark.parametrize("role", REVIEWER_ROLES)
def test_reviewer_role_gets_no_block(lane, role):
    assert fce.BLOCK_HEADING not in LANES[lane](role)


@pytest.mark.parametrize("lane", sorted(LANES))
@pytest.mark.parametrize("role", BUILD_ROLES)
def test_injection_failure_still_carries_block_exactly_once(lane, role):
    """OI-2318: the fallback header must not drop the rule for a build role."""
    with patch("skill_context._inject_skill_context", side_effect=RuntimeError("inject boom")):
        prompt = LANES[lane](role)
    assert prompt.count(fce.BLOCK_HEADING) == 1
    assert fce.load_block() in prompt
    assert f"**{role}**" in prompt


@pytest.mark.parametrize("lane", sorted(LANES))
@pytest.mark.parametrize("role", REVIEWER_ROLES)
def test_injection_failure_keeps_reviewer_bare(lane, role):
    """D1: a reviewer role's fallback header stays unenriched."""
    with patch("skill_context._inject_skill_context", side_effect=RuntimeError("inject boom")):
        prompt = LANES[lane](role)
    assert fce.BLOCK_HEADING not in prompt
    assert f"**{role}**" in prompt


@pytest.mark.parametrize("lane", sorted(LANES))
@pytest.mark.parametrize("role", ["backend-developer", "api-developer"])
def test_missing_block_file_fails_preparation(lane, role):
    """A broken install raises before a worker can be spawned — never a bare header."""
    absent = Path("/nonexistent-vnx-install/fail_closed_evidence.md")
    with patch.object(fce, "_BLOCK_PATH", absent):
        with pytest.raises(fce.FailClosedBlockError) as raised:
            LANES[lane](role)
    message = str(raised.value)
    assert role in message
    assert "d1" in message
    assert "fail_closed_evidence.md" in message


def test_legacy_route_without_role_source_still_carries_block():
    """OI-2318: the no-CLAUDE.md legacy return path used to drop the block.

    ``api-developer`` has no PromptAssembler role file and no agents/skills
    CLAUDE.md, and terminal T99 has none either — so the legacy resolver used
    to return the raw instruction. A build role must still get the rule.
    """
    with patch("skill_context._try_prompt_assembler", return_value=None):
        prompt = sd._inject_skill_context(
            "T99", "Do X", "api-developer", {"dispatch_id": "d1"},
        )
    assert prompt.count(fce.BLOCK_HEADING) == 1
    assert fce.load_block() in prompt


def test_block_stays_under_2kb():
    assert len(fce.load_block().encode()) < 2048


def test_with_block_is_idempotent():
    once = fce.with_block("text", "backend-developer")
    assert fce.with_block(once, "backend-developer") == once


@pytest.mark.parametrize("role", [None, "", "reviewer", "unknown-role", "blog-writer"])
def test_with_block_leaves_non_build_roles_alone(role):
    assert fce.with_block("text", role) == "text"


def test_missing_block_file_raises(tmp_path):
    with patch.object(fce, "_BLOCK_PATH", tmp_path / "absent.md"):
        with pytest.raises(FileNotFoundError):
            fce.with_block("text", "backend-developer")


@pytest.mark.parametrize("path", [
    "scripts/lib/prompts/roles/backend-developer.md",
    "skills/backend-developer/SKILL.md",
    ".claude/skills/backend-developer/SKILL.md",
])
def test_null_guard_item_points_to_block(path):
    text = (REPO / path).read_text()
    assert "display and formatting code" in text
    assert "Fail-closed on evidence" in text
    assert "Especially for fields from external/legacy sources" not in text


def test_quality_engineer_claude_md_points_to_block():
    text = (REPO / "agents/quality-engineer/CLAUDE.md").read_text()
    assert "Fail-closed on evidence" in text
