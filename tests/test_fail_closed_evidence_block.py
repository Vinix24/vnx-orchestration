"""The shared fail-closed block reaches every build role on every lane, once (dlv-48b4e15d7ebc)."""

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

BUILD_ROLES = [
    "backend-developer", "frontend-developer", "quality-engineer", "security-engineer",
    "database-engineer", "test-engineer", "system-architect", "architect",
    "intelligence-engineer",
]
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


@pytest.mark.parametrize("lane", sorted(LANES))
@pytest.mark.parametrize("role", REVIEWER_ROLES)
def test_reviewer_role_gets_no_block(lane, role):
    assert fce.BLOCK_HEADING not in LANES[lane](role)


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
