"""Regression tests for benchmark equal-context dispatch mode."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
RUNNERS = REPO_ROOT / "scripts" / "benchmark" / "field-tests" / "runners"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(RUNNERS))

import lane_adapter  # noqa: E402
import provider_dispatch  # noqa: E402
import subprocess_dispatch  # noqa: E402
from tmux_interactive_dispatch import TmuxInteractiveDispatch, TmuxResult  # noqa: E402


RAW_INSTRUCTION = "  # benchmark prompt\r\n\r\nPreserve trailing whitespace.  \n"


def _provider_args(instruction: str = RAW_INSTRUCTION) -> SimpleNamespace:
    return SimpleNamespace(
        instruction=instruction,
        dispatch_id="bench-equal-context-test",
        role="security-engineer",
        pr_id=None,
        dispatch_paths="scripts/lib/example.py",
    )


def test_provider_equal_context_returns_instruction_byte_for_byte(monkeypatch):
    intelligence = Mock(return_value="intelligence-added")
    repo_map = Mock(return_value="repo-map-added")
    monkeypatch.setitem(
        sys.modules,
        "intelligence_injection",
        SimpleNamespace(build_intelligence_section=intelligence),
    )
    monkeypatch.setitem(
        sys.modules,
        "dispatch_enricher",
        SimpleNamespace(apply_repo_map_layer=repo_map),
    )
    monkeypatch.setenv("VNX_BENCH_EQUAL_CONTEXT", "1")

    result = provider_dispatch._enrich_instruction(_provider_args())

    assert result == RAW_INSTRUCTION
    intelligence.assert_not_called()
    repo_map.assert_not_called()


def test_provider_without_equal_context_invokes_existing_enrichers(monkeypatch):
    intelligence = Mock(return_value="intelligence-added")
    repo_map = Mock(return_value="repo-map-added")
    monkeypatch.setitem(
        sys.modules,
        "intelligence_injection",
        SimpleNamespace(build_intelligence_section=intelligence),
    )
    monkeypatch.setitem(
        sys.modules,
        "dispatch_enricher",
        SimpleNamespace(apply_repo_map_layer=repo_map),
    )
    monkeypatch.delenv("VNX_BENCH_EQUAL_CONTEXT", raising=False)

    result = provider_dispatch._enrich_instruction(_provider_args())

    assert result == "repo-map-added"
    intelligence.assert_called_once()
    repo_map.assert_called_once_with(
        "intelligence-added",
        {"role": "security-engineer"},
    )


def test_tmux_equal_context_skips_all_context_assembly(monkeypatch, tmp_path):
    lane = TmuxInteractiveDispatch(tmp_path, project_root=tmp_path)
    monkeypatch.setenv("VNX_BENCH_EQUAL_CONTEXT", "1")
    monkeypatch.setenv("VNX_SHARED_PREPARE", "1")

    with (
        patch("dispatch_prepare.prepare") as prepare,
        patch(
            "subprocess_dispatch_internals.skill_injection._inject_skill_context",
        ) as inject_skill,
    ):
        result = lane._assemble_context(
            role="security-engineer",
            smart_context="must not be added",
            terminal_id="T1",
            dispatch_id="bench-equal-context-test",
            instruction=RAW_INSTRUCTION,
            dispatch_paths=["scripts/lib/example.py"],
        )

    assert result == RAW_INSTRUCTION
    prepare.assert_not_called()
    inject_skill.assert_not_called()


def test_tmux_without_equal_context_invokes_existing_enricher(monkeypatch, tmp_path):
    lane = TmuxInteractiveDispatch(tmp_path, project_root=tmp_path)
    monkeypatch.delenv("VNX_BENCH_EQUAL_CONTEXT", raising=False)
    monkeypatch.setenv("VNX_SHARED_PREPARE", "0")

    with patch(
        "subprocess_dispatch_internals.skill_injection._inject_skill_context",
        return_value="skill-context-added",
    ) as inject_skill:
        result = lane._assemble_context(
            role="security-engineer",
            terminal_id="T1",
            instruction=RAW_INSTRUCTION,
        )

    # Enricher output is used; the fallback path also appends the report-contract
    # directive (gap #3b) so the dispatch stays governed without VNX_SHARED_PREPARE.
    assert "skill-context-added" in result
    assert "<!-- VNX-REPORT-CONTRACT-DIRECTIVE -->" in result
    inject_skill.assert_called_once()


def test_equal_context_matches_tmux_and_provider_assembly(monkeypatch, tmp_path):
    monkeypatch.setenv("VNX_BENCH_EQUAL_CONTEXT", "1")
    lane = TmuxInteractiveDispatch(tmp_path, project_root=tmp_path)

    tmux_instruction = lane._assemble_context(
        role="security-engineer",
        terminal_id="T1",
        instruction=RAW_INSTRUCTION,
    )
    provider_instruction = provider_dispatch._enrich_instruction(
        _provider_args(),
    )

    assert tmux_instruction == provider_instruction == RAW_INSTRUCTION


def test_tmux_equal_context_delivers_only_the_benchmark_prompt(monkeypatch, tmp_path):
    runner = Mock()
    runner.available.return_value = True

    def run_tmux(args, **kwargs):
        if args[0] == "new-session":
            return TmuxResult(0, "%1\n", "")
        if args[0] == "display-message":
            return TmuxResult(0, "@1\n", "")
        if args[0] == "capture-pane":
            return TmuxResult(0, "Welcome to Claude\n? for shortcuts", "")
        return TmuxResult(0, "", "")

    runner.run.side_effect = run_tmux
    lane = TmuxInteractiveDispatch(
        tmp_path,
        runner=runner,
        receipts_file=tmp_path / "receipts.ndjson",
        project_root=tmp_path,
    )
    delivered: list[str] = []
    monkeypatch.setenv("VNX_BENCH_EQUAL_CONTEXT", "1")
    monkeypatch.setenv("VNX_SHARED_PREPARE", "1")
    monkeypatch.setattr(
        lane,
        "_deliver_instruction",
        lambda pane_id, body: delivered.append(body) or False,
    )

    lane.dispatch(
        RAW_INSTRUCTION,
        "bench-equal-context-test",
        role="security-engineer",
        deadline_seconds=1,
        warmup_timeout=0.05,
        warmup_poll_interval=0.001,
        isolated_worktree=False,
    )

    assert delivered == [RAW_INSTRUCTION]


def test_subprocess_equal_context_skips_headless_enrichers(monkeypatch):
    monkeypatch.setenv("VNX_BENCH_EQUAL_CONTEXT", "1")

    with (
        patch.object(
            subprocess_dispatch,
            "_standard_inject_skill_context",
            return_value="skill-context-added",
        ) as inject_skill,
        patch.object(
            subprocess_dispatch,
            "_standard_inject_permission_profile",
            return_value="permission-context-added",
        ) as inject_permission,
        patch.object(
            subprocess_dispatch,
            "_standard_build_continuation_prompt",
            return_value="handover-context-added",
        ) as build_handover,
    ):
        assert subprocess_dispatch._inject_skill_context(
            "T1", RAW_INSTRUCTION, role="security-engineer",
        ) == RAW_INSTRUCTION
        assert subprocess_dispatch._inject_permission_profile(
            "T1", "security-engineer", RAW_INSTRUCTION,
        ) == RAW_INSTRUCTION
        assert subprocess_dispatch._build_continuation_prompt(
            Path("/tmp/handover.md"), RAW_INSTRUCTION,
        ) == RAW_INSTRUCTION

    inject_skill.assert_not_called()
    inject_permission.assert_not_called()
    build_handover.assert_not_called()


def test_subprocess_equal_context_skips_cli_repo_map_and_footer(monkeypatch):
    repo_map = Mock(return_value="repo-map-added")
    footer = Mock(return_value="footer-added")
    monkeypatch.setitem(
        sys.modules,
        "dispatch_enricher",
        SimpleNamespace(apply_repo_map_layer=repo_map),
    )
    monkeypatch.setitem(
        sys.modules,
        "dispatch_footer",
        SimpleNamespace(append_dispatch_footer=footer),
    )
    monkeypatch.setenv("VNX_BENCH_EQUAL_CONTEXT", "1")

    result = subprocess_dispatch._enrich_cli_instruction(
        RAW_INSTRUCTION,
        "security-engineer",
    )

    assert result == RAW_INSTRUCTION
    repo_map.assert_not_called()
    footer.assert_not_called()


def test_subprocess_without_equal_context_invokes_existing_enrichers(monkeypatch):
    monkeypatch.delenv("VNX_BENCH_EQUAL_CONTEXT", raising=False)

    with (
        patch.object(
            subprocess_dispatch,
            "_standard_inject_skill_context",
            return_value="skill-context-added",
        ) as inject_skill,
        patch.object(
            subprocess_dispatch,
            "_standard_inject_permission_profile",
            return_value="permission-context-added",
        ) as inject_permission,
    ):
        assert subprocess_dispatch._inject_skill_context(
            "T1", RAW_INSTRUCTION, role="security-engineer",
        ) == "skill-context-added"
        assert subprocess_dispatch._inject_permission_profile(
            "T1", "security-engineer", RAW_INSTRUCTION,
        ) == "permission-context-added"

    inject_skill.assert_called_once()
    inject_permission.assert_called_once()


def test_subprocess_without_equal_context_invokes_cli_repo_map_and_footer(monkeypatch):
    repo_map = Mock(return_value="repo-map-added")
    footer = Mock(return_value="footer-added")
    monkeypatch.setitem(
        sys.modules,
        "dispatch_enricher",
        SimpleNamespace(apply_repo_map_layer=repo_map),
    )
    monkeypatch.setitem(
        sys.modules,
        "dispatch_footer",
        SimpleNamespace(append_dispatch_footer=footer),
    )
    monkeypatch.delenv("VNX_BENCH_EQUAL_CONTEXT", raising=False)

    result = subprocess_dispatch._enrich_cli_instruction(
        RAW_INSTRUCTION,
        "security-engineer",
    )

    assert result == "footer-added"
    repo_map.assert_called_once_with(
        RAW_INSTRUCTION,
        {"role": "security-engineer"},
    )
    footer.assert_called_once_with("repo-map-added")


def test_subprocess_equal_context_disables_shared_prepare_and_repo_map(monkeypatch):
    observed: dict = {}

    def fake_deliver(*args, **kwargs):
        observed["shared_prepare"] = subprocess_dispatch.os.environ.get(
            "VNX_SHARED_PREPARE",
        )
        observed["repo_map"] = kwargs["repo_map"]
        return True

    monkeypatch.setenv("VNX_BENCH_EQUAL_CONTEXT", "1")
    monkeypatch.setenv("VNX_SHARED_PREPARE", "1")
    monkeypatch.setattr(subprocess_dispatch, "_deliver_with_recovery", fake_deliver)

    assert subprocess_dispatch.deliver_with_recovery(
        "T1",
        RAW_INSTRUCTION,
        "sonnet",
        "bench-equal-context-test",
        repo_map="repo-map-added",
    )

    assert observed == {"shared_prepare": "0", "repo_map": None}
    assert subprocess_dispatch.os.environ["VNX_SHARED_PREPARE"] == "1"


@pytest.mark.parametrize(
    ("dispatch_helper", "lane"),
    [
        (
            lane_adapter._claude_subprocess_headless,
            {"id": "claude-test", "provider": "claude", "model_arg": "sonnet"},
        ),
        (
            lane_adapter._claude_tmux_spawn,
            {"id": "claude-test", "provider": "claude", "model_arg": "sonnet"},
        ),
        (
            lane_adapter._provider_dispatch,
            {"id": "gemma-test", "provider": "local-gemma", "model_arg": "gemma"},
        ),
    ],
)
def test_all_benchmark_invocations_set_equal_context_and_role(
    monkeypatch,
    dispatch_helper,
    lane,
):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(lane_adapter.subprocess, "run", run)

    dispatch_helper(
        lane,
        "bench-equal-context-test",
        RAW_INSTRUCTION,
        "scripts/lib/example.py",
        30,
        "security-engineer",
    )

    cmd = run.call_args.args[0]
    env = run.call_args.kwargs["env"]
    role_index = cmd.index("--role")
    assert cmd[role_index + 1] == "security-engineer"
    assert env["VNX_BENCH_EQUAL_CONTEXT"] == "1"


def test_dispatch_uses_tasks_primary_skill_as_role(monkeypatch):
    captured: dict = {}

    def fake_provider(*args):
        captured["instruction"] = args[2]
        captured["role"] = args[5]
        return 1, "", ""

    monkeypatch.setattr(lane_adapter, "_provider_dispatch", fake_provider)
    monkeypatch.setattr(lane_adapter, "REPORT_DIR_CANDIDATES", ())

    lane_adapter.dispatch(
        lane={
            "id": "gemma-test",
            "provider": "local-gemma",
            "model_arg": "gemma",
        },
        task_id="security-task",
        replication=1,
        instruction="secure the endpoint",
        dispatch_paths="scripts/lib/example.py",
        deadline_seconds=30,
        skill_names=["security-engineer", "backend-developer"],
    )

    assert captured["role"] == "security-engineer"
    assert "## Skill: security-engineer" in captured["instruction"]
