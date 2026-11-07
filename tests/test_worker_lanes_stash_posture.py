"""OI-2588: the codex and gemini worker lanes refuse `git stash` (OI-1447) or
provably run no builders.

Measured posture (docs/operations/WORKER_LANE_STASH_POSTURE.md):

- gemini can build through the door and has no per-run deny (no command
  denylist, no rules-file flag), so the door refuses a BUILD on it unless the
  operator sets ``VNX_OVERRIDE_STASH_BUILD_LANE``;
- codex can build and has no per-run deny either, but it IS the live
  availability fallback, so refusing it would break a routing default — it is
  deliberately not refused here and left to T0;
- litellm (one-shot HTTP runner) and local-gemma (ollama/mlx prompt runner) get
  no tools through the door, so they have no stash surface.

Nothing here starts codex, gemini or ollama: only their argv builders, the
routing tables and the door's pure predicate are exercised.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_LIB = str(_REPO / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import dispatch_cli  # noqa: E402
from dispatch_plan import ExecutionPlan, Isolation  # noqa: E402
from dispatch_spec import (  # noqa: E402
    DispatchPath,
    DispatchSpec,
    PathAccess,
    Provider,
)
from providers import provider_registry  # noqa: E402

_PROVIDERS_YAML = _REPO / "scripts" / "lib" / "providers"


def _spec(
    *,
    provider: Provider = Provider.GEMINI,
    role: str = "backend-developer",
    paths: tuple = (),
    pr_id=None,
) -> DispatchSpec:
    return DispatchSpec(
        schema_version=1,
        project_id="proj-stash-posture",
        dispatch_id="dlv-stash-posture",
        staging_id="stage-1",
        instruction_file=Path("/tmp/instruction.md"),
        role=role,
        target_slot="T1",
        gate="codex_gate",
        dispatch_paths=tuple(paths),
        provider=provider,
        pr_id=pr_id,
    )


def _plan(provider: Provider) -> ExecutionPlan:
    """A provider-lane ExecutionPlan: the real input type of the seam under test."""
    return ExecutionPlan(
        dispatch_id="dlv-stash-posture",
        project_id="proj-stash-posture",
        provider=provider,
        model="gemini-2.5-pro",
        lane="provider",
        adapter="provider",
        target_id="T1",
        billing="provider_metered",
        serialization_class=None,
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=True,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="n/a",
        deadline_seconds=3600,
        base_ref="origin/main",
        dispatch_paths=(),
        instruction_file=Path("/tmp/instruction.md"),
        route_reason="D11,D3,D1,D2,D4,D5,D6,D7,D8,D9,D10,D12",
    )


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    # Literal name (not dispatch_cli.STASH_BUILD_OVERRIDE_ENV) so the routing
    # pins still collect on old code and only the new-behaviour tests go red.
    monkeypatch.delenv("VNX_OVERRIDE_STASH_BUILD_LANE", raising=False)


# ---------------------------------------------------------------------------
# The door refusal — gemini can build and has no per-run deny
# ---------------------------------------------------------------------------

class TestGeminiBuildIsRefused:
    def test_a_gemini_build_is_refused_with_the_oi_1447_reason(self):
        reject = dispatch_cli._stash_build_reject(_spec(), "gemini")
        assert reject is not None
        assert reject.code == dispatch_cli.STASH_BUILD_REFUSAL_CODE
        assert "OI-1447" in reject.reason
        assert "git stash" in reject.reason
        assert "gemini" in reject.reason
        assert dispatch_cli.STASH_BUILD_OVERRIDE_ENV in reject.reason

    def test_the_override_env_lets_the_build_through(self, monkeypatch):
        monkeypatch.setenv(dispatch_cli.STASH_BUILD_OVERRIDE_ENV, "1")
        assert dispatch_cli._stash_build_reject(_spec(), "gemini") is None

    def test_an_explicit_override_lets_the_build_through(self):
        assert dispatch_cli._stash_build_reject(_spec(), "gemini", override=True) is None

    def test_a_writing_dispatch_path_is_a_build(self):
        paths = (DispatchPath(path=Path("scripts/lib/x.py"), access=PathAccess.READ_WRITE),)
        reject = dispatch_cli._stash_build_reject(
            _spec(role="code-reviewer", paths=paths), "gemini",
        )
        assert reject is not None and reject.code == dispatch_cli.STASH_BUILD_REFUSAL_CODE

    def test_a_pr_producing_dispatch_is_a_build(self):
        reject = dispatch_cli._stash_build_reject(
            _spec(role="code-reviewer", pr_id="2403"), "gemini",
        )
        assert reject is not None and reject.code == dispatch_cli.STASH_BUILD_REFUSAL_CODE

    def test_a_read_only_gemini_dispatch_is_allowed(self):
        # code-reviewer denies Edit, declares no paths and no PR: it never writes.
        assert dispatch_cli._stash_build_reject(
            _spec(role="code-reviewer"), "gemini",
        ) is None

    def test_other_lanes_are_not_refused_by_this_rule(self):
        assert dispatch_cli._stash_build_reject(_spec(provider=Provider.CODEX), "codex") is None
        assert dispatch_cli._stash_build_reject(_spec(provider=Provider.CLAUDE), "claude") is None

    def test_the_door_calls_the_rule_before_the_permit(self):
        # The rule rides on _lane_verdict, the seam run_dispatch already calls
        # after compile_plan and before the permit: run_dispatch itself may not
        # grow (its origin/main executable-line count is the ceiling), so the
        # check lives in that helper and the door keeps calling the helper.
        seam = inspect.getsource(dispatch_cli._lane_verdict)
        assert "_stash_build_reject(" in seam
        door = inspect.getsource(dispatch_cli.run_dispatch)
        assert "_lane_verdict(" in door
        assert door.index("_lane_verdict(") < door.index("issue_permit(plan)")
        assert "_stash_build_reject(" not in door


class TestTheSeamRefusesBeforeTheSwitchAndTheProbe:
    """The rule's place in the ORDER of checks, exercised through the seam itself."""

    def test_a_gemini_build_is_refused_by_the_seam_before_switch_or_probe(self, monkeypatch):
        def refuse_first(*args, **kwargs):
            raise AssertionError(
                "the stash refusal must come before the provider switch and reachability"
            )

        monkeypatch.setattr(dispatch_cli, "_provider_switch_verdict", refuse_first)
        monkeypatch.setattr(dispatch_cli, "_check_reachability", refuse_first)
        reject = dispatch_cli._lane_verdict(
            _plan(Provider.GEMINI),
            _spec(),
            project_id="proj-stash-posture",
            state_dir=Path("/nonexistent"),
            dry_run=True,
        )
        assert reject is not None
        assert reject.code == dispatch_cli.STASH_BUILD_REFUSAL_CODE

    def test_a_read_only_gemini_dispatch_reaches_the_switch(self, monkeypatch):
        # Not a build, so the stash rule passes it on: proving the seam does not
        # over-refuse what round 1 deliberately left allowed.
        asked: list[str] = []
        monkeypatch.setattr(
            dispatch_cli, "_provider_switch_verdict",
            lambda *a, **k: asked.append("switch") or None,
        )
        monkeypatch.setattr(
            dispatch_cli, "_check_reachability",
            lambda *a, **k: dispatch_cli.ReachabilityVerdict(block=False),
        )
        reject = dispatch_cli._lane_verdict(
            _plan(Provider.GEMINI),
            _spec(role="code-reviewer"),
            project_id="proj-stash-posture",
            state_dir=Path("/nonexistent"),
            dry_run=True,
        )
        assert reject is None
        assert asked == ["switch"]

    def test_a_codex_build_still_reaches_the_switch(self, monkeypatch):
        # Deliberately not refused: codex is the live availability fallback.
        asked: list[str] = []
        monkeypatch.setattr(
            dispatch_cli, "_provider_switch_verdict",
            lambda *a, **k: asked.append("switch") or None,
        )
        monkeypatch.setattr(
            dispatch_cli, "_check_reachability",
            lambda *a, **k: dispatch_cli.ReachabilityVerdict(block=False),
        )
        reject = dispatch_cli._lane_verdict(
            _plan(Provider.CODEX),
            _spec(provider=Provider.CODEX),
            project_id="proj-stash-posture",
            state_dir=Path("/nonexistent"),
            dry_run=True,
        )
        assert reject is None
        assert asked == ["switch"]


# ---------------------------------------------------------------------------
# Why codex is not refused: it is the live availability fallback
# ---------------------------------------------------------------------------

class TestCodexIsALiveFallbackLane:
    def test_openai_section_dispatches_as_codex(self):
        assert provider_registry.load()["openai"].dispatch_enum == "codex"

    def test_a_tier_fallback_resolves_to_the_codex_provider_lane(self):
        steps = [
            step
            for route in provider_registry.load_tier_map().values()
            for step in route.fallback
        ]
        assert any(s.provider == "codex" and s.lane == "provider" for s in steps)

    def test_a_build_task_class_lists_codex_as_a_candidate(self):
        import yaml

        data = yaml.safe_load((_PROVIDERS_YAML / "routing_recommendations.yaml").read_text())
        candidates = data["routing_by_task"]["01_code_generation"]["candidates"]
        assert any(c["model_id"].startswith("codex-") for c in candidates)


# ---------------------------------------------------------------------------
# The argv of each lane: what a deny could ride on, and what it cannot
# ---------------------------------------------------------------------------

class TestLaneArgv:
    def test_codex_argv_has_no_per_run_rules_or_denylist_flag(self):
        from provider_spawns.codex_spawn import _build_cmd

        argv = _build_cmd("")
        assert argv[:3] == ["codex", "exec", "--json"]
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        # No per-run command denylist and no rules-file flag exist in 0.164.0.
        assert not [a for a in argv if "rule" in a.lower()]
        assert not [a for a in argv if "stash" in a.lower()]

    def test_gemini_argv_carries_only_model_and_output_format(self):
        from provider_spawns.gemini_spawn import _build_gemini_cmd

        assert _build_gemini_cmd("gemini-2.5-pro", "stream-json") == [
            "gemini", "--model", "gemini-2.5-pro", "--output-format", "stream-json",
        ]


# ---------------------------------------------------------------------------
# The no-builders lanes: the door routes them to a tool-less spawner
# ---------------------------------------------------------------------------

class TestNoBuilderLanes:
    def test_litellm_door_path_uses_the_one_shot_no_tool_runner(self):
        source = (_REPO / "scripts" / "lib" / "envelope_adapters_provider.py").read_text()
        assert "from provider_spawns.litellm_spawn import spawn_litellm" in source
        assert "spawn_litellm_agentic" not in source

    def test_local_gemma_door_path_is_the_prompt_only_ollama_runner(self):
        envelope = (_REPO / "scripts" / "lib" / "envelope_adapters_provider.py").read_text()
        assert "from provider_spawns.local_gemma_spawn import spawn_local_gemma" in envelope
        spawn = (_REPO / "scripts" / "lib" / "providers" / "local_gemma" / "spawn.py").read_text()
        assert '["ollama", "run", model, prompt]' in spawn

    def test_no_build_task_class_routes_to_gemini(self):
        import yaml

        data = yaml.safe_load((_PROVIDERS_YAML / "routing_recommendations.yaml").read_text())
        for task, body in data["routing_by_task"].items():
            for candidate in body.get("candidates", []):
                assert not candidate["model_id"].startswith("gemini-"), task
