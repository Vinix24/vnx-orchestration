#!/usr/bin/env python3
"""Tests for dispatch-agent-lane-coercion (20260713-LANECOERCE).

`vnx dispatch-agent --model kimi` used to discard the requested model's provider
entirely: deliver_via_door() was never passed a `provider=` kwarg, so it always
defaulted to "claude" — a kimi request silently spawned a claude-subscription
worker with no error, bypassing the kimi-via-cli-only constraint and misclassifying
billing.

Covers:
  1. _infer_provider_for_model: model -> provider inference (unit-level).
  2. vnx_dispatch_agent: --model kimi resolves provider="kimi" and is threaded
     through to deliver_via_door (plan-level assertion, no real worker spawned).
  3. vnx_dispatch_agent: --model opus/sonnet still resolves provider="claude"
     (no regression for the legitimate claude lane).
  4. vnx_dispatch_agent: an unresolvable --model hard-errors BEFORE dispatch
     instead of silently coercing to claude.
  5. vnx_dispatch_agent: agent_config["provider"] is honored when no explicit
     --model is given (the previously-discarded config field).
  6. vnx_dispatch_agent: when the single-entry door is disabled (legacy lane),
     a non-claude provider hard-errors instead of falling through to the
     claude-only legacy `deliver_with_recovery` path.
"""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

from vnx_cli.commands.dispatch_agent import _infer_provider_for_model, vnx_dispatch_agent


# ---------------------------------------------------------------------------
# 1. _infer_provider_for_model — unit tests
# ---------------------------------------------------------------------------

class TestInferProviderForModel:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("kimi", "kimi"),
            ("kimi-k2-6", "kimi"),
            ("kimi-k2-0905-default", "kimi"),
            ("KIMI", "kimi"),
            ("sonnet", "claude"),
            ("opus", "claude"),
            ("haiku", "claude"),
            ("claude-sonnet-5", "claude"),
            ("codex", "codex"),
            ("gpt-5.5", "codex"),
            ("gemini", "gemini"),
            ("gemini-2.5-pro", "gemini"),
            ("glm-5.1", "glm-harness"),
            ("glm-5.2", "glm-harness"),
            ("zai", "glm-harness"),
            ("deepseek-harness", "deepseek-harness"),
            ("deepseek-v4-pro", "deepseek-harness"),
            ("local-gemma", "local-gemma"),
            ("gemma-4b-local", "local-gemma"),
            ("", "claude"),
            (None, "claude"),
        ],
    )
    def test_known_models_map_to_expected_provider(self, model, expected):
        assert _infer_provider_for_model(model) == expected

    def test_unknown_model_raises_value_error(self):
        with pytest.raises(ValueError, match="does not map to any honorable provider"):
            _infer_provider_for_model("totally-unknown-xyz-999")


# ---------------------------------------------------------------------------
# Shared dispatch harness
# ---------------------------------------------------------------------------

def _make_agent(
    base: Path,
    name: str = "hello-world",
    *,
    default_instruction: str = "Say hi",
    provider: str | None = None,
    model: str | None = None,
) -> Path:
    agent_dir = base / "examples" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "CLAUDE.md").write_text(f"# {name} agent")
    lines = ["governance_profile: minimal", f'default_instruction: "{default_instruction}"']
    if provider:
        lines.append(f"provider: {provider}")
    if model:
        lines.append(f"model: {model}")
    (agent_dir / "config.yaml").write_text("\n".join(lines) + "\n")
    return agent_dir


def _run_dispatch_capturing_door_kwargs(
    tmp_path: Path,
    *,
    model: str | None,
    agent_provider: str | None = None,
    agent_model: str | None = None,
    legacy: bool = False,
    monkeypatch=None,
):
    """Invoke vnx_dispatch_agent with the REAL dispatch_bridge/dispatch_spec modules
    (needed for real provider inference), but with deliver_via_door replaced so no
    worker is ever staged/spawned — a plan-level assertion on the kwargs it receives."""
    _make_agent(tmp_path, provider=agent_provider, model=agent_model)
    if legacy and monkeypatch is not None:
        monkeypatch.setenv("VNX_DISPATCH_LEGACY", "1")

    captured = {}

    def fake_door(legacy_fn, **kwargs):
        captured.update(kwargs)
        return True

    import dispatch_bridge  # real module, scripts/lib already on sys.path

    from vnx_cli import _engine
    with patch.object(_engine, "engine_root", return_value=tmp_path), \
         patch.object(dispatch_bridge, "deliver_via_door", side_effect=fake_door):
        args = Namespace(agent="hello-world", instruction=None, model=model, project_dir=str(tmp_path))
        rc = vnx_dispatch_agent(args)

    return rc, captured


# ---------------------------------------------------------------------------
# 2. --model kimi routes to provider="kimi"
# ---------------------------------------------------------------------------

class TestKimiModelRoutesToKimiProvider:
    def test_explicit_kimi_model_resolves_kimi_provider(self, tmp_path):
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, model="kimi")

        assert rc == 0
        assert captured.get("provider") == "kimi", (
            "requested --model kimi must resolve provider='kimi', not silently default to claude"
        )
        assert captured.get("model") == "kimi"

    def test_kimi_variant_model_resolves_kimi_provider(self, tmp_path):
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, model="kimi-k2-6")

        assert rc == 0
        assert captured.get("provider") == "kimi"


# ---------------------------------------------------------------------------
# 3. --model opus/sonnet still routes to provider="claude" (no regression)
# ---------------------------------------------------------------------------

class TestClaudeModelsStillRouteToClaudeProvider:
    @pytest.mark.parametrize("model", ["opus", "sonnet", "haiku"])
    def test_claude_tier_model_resolves_claude_provider(self, tmp_path, model):
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, model=model)

        assert rc == 0
        assert captured.get("provider") == "claude"
        assert captured.get("model") == model

    def test_no_explicit_model_defaults_to_claude(self, tmp_path):
        """No --model, no agent_config model/provider -> legacy default sonnet on claude."""
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, model=None)

        assert rc == 0
        assert captured.get("provider") == "claude"
        assert captured.get("model") == "sonnet"


# ---------------------------------------------------------------------------
# 4. Unresolvable model hard-errors instead of silently coercing to claude
# ---------------------------------------------------------------------------

class TestUnresolvableModelHardErrors:
    def test_unknown_model_hard_errors_before_dispatch(self, tmp_path, capsys):
        rc, captured = _run_dispatch_capturing_door_kwargs(tmp_path, model="totally-unknown-xyz-999")

        assert rc == 1
        assert captured == {}, "deliver_via_door must never be called for an unresolvable model"
        err = capsys.readouterr().err
        assert "does not map to any honorable provider" in err


# ---------------------------------------------------------------------------
# 5. agent_config["provider"] is honored when no explicit --model is given
# ---------------------------------------------------------------------------

class TestAgentConfigProviderHonored:
    def test_agent_configured_provider_used_without_explicit_model(self, tmp_path):
        """The agent declares provider: codex with no matching model — previously this
        field was resolved (agent_resolver) but then discarded outright."""
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path, model=None, agent_provider="codex",
        )

        assert rc == 0
        assert captured.get("provider") == "codex"

    def test_explicit_model_override_wins_over_agent_config_provider(self, tmp_path):
        """An explicit --model kimi must win even if the agent's own config.yaml
        declares a different provider — the user's request is authoritative."""
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path, model="kimi", agent_provider="claude",
        )

        assert rc == 0
        assert captured.get("provider") == "kimi"


# ---------------------------------------------------------------------------
# 6. Legacy (door-disabled) lane hard-errors for a non-claude provider
# ---------------------------------------------------------------------------

class TestLegacyLaneGuardsNonClaudeProvider:
    def test_kimi_model_hard_errors_when_door_disabled(self, tmp_path, monkeypatch, capsys):
        """VNX_DISPATCH_LEGACY=1 routes deliver_via_door() straight to the claude-only
        legacy deliver_with_recovery() callable, which cannot honor a kimi request —
        must hard-error rather than silently spawning a claude worker."""
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path, model="kimi", legacy=True, monkeypatch=monkeypatch,
        )

        assert rc == 1
        assert captured == {}, "deliver_via_door must never be reached on the legacy-lane guard"
        err = capsys.readouterr().err
        assert "legacy dispatch lane only drives the claude CLI" in err

    def test_claude_model_still_works_when_door_disabled(self, tmp_path, monkeypatch):
        """No regression: a genuine claude model must still work on the legacy lane."""
        rc, captured = _run_dispatch_capturing_door_kwargs(
            tmp_path, model="sonnet", legacy=True, monkeypatch=monkeypatch,
        )

        assert rc == 0
        assert captured.get("provider") == "claude"
