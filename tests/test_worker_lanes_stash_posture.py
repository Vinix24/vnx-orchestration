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
        source = inspect.getsource(dispatch_cli.run_dispatch)
        assert "_stash_build_reject(" in source
        assert source.index("_stash_build_reject(") < source.index("issue_permit(plan)")


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
