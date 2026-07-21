"""Tests for the kimi model resolver (Dispatch-ID: 20260721-kimi-lane-hardening).

Covers WS1 (registry: verified CLI strings, explicit K3 default, fail-loud resolver)
and WS2 (honoring args.model at the kimi spawn seam with an unambiguous precedence),
across both reachable seams:
  - scripts/lib/dispatch_envelope.py ProviderAdapter.run() kimi branch (governed/door path)
  - scripts/lib/provider_dispatch.py _dispatch_kimi (legacy CLI path)

All tests use registry fixtures / mocks — none invoke the live kimi-cli OAuth binary.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


# ---------------------------------------------------------------------------
# WS1 — registry: default_model flag + verified entries
# ---------------------------------------------------------------------------


class TestRegistryDefaultModelFlag:
    def test_provider_config_has_default_model_field(self):
        from providers.provider_registry import ProviderConfig

        cfg = ProviderConfig(enabled=True, api_key_env="", default_model="kimi-k3")
        assert cfg.default_model == "kimi-k3"

    def test_provider_config_default_model_defaults_to_none(self):
        from providers.provider_registry import ProviderConfig

        cfg = ProviderConfig(enabled=True, api_key_env="")
        assert cfg.default_model is None

    def test_wave7_yaml_kimi_cli_default_model_is_k3(self):
        from providers.provider_registry import load

        registry = load()
        assert registry["kimi_cli"].default_model == "kimi-k3"

    def test_resolve_kimi_model_label_honors_default_model_flag_independent_of_key_order(self):
        """A fabricated registry with kimi-k3 as the LAST dict key must still resolve to
        kimi-k3 via the default_model flag, not "first dict key" (loader/schema edit)."""
        import provider_dispatch as pd
        from providers.provider_registry import ProviderConfig, ProviderModel

        def _model(cli_arg):
            return ProviderModel(
                litellm_name="", cost_input_per_mtok=0.0, cost_output_per_mtok=0.0,
                max_tokens=8192, supports_streaming=True, supports_tool_calls=True,
                cli_model_arg=cli_arg, dispatch_allowed=True,
            )

        fake_cfg = ProviderConfig(
            enabled=True, api_key_env="", default_model="kimi-k3",
            models={
                "kimi-k2-7": _model("kimi-code/kimi-for-coding"),  # listed FIRST
                "kimi-k3": _model("kimi-code/k3"),                  # listed LAST
            },
        )
        with patch("providers.provider_registry.load", return_value={"kimi_cli": fake_cfg}):
            assert pd._resolve_kimi_model_label() == "kimi-k3"

    def test_resolve_kimi_model_label_falls_back_to_first_key_when_flag_stale(self):
        """default_model pointing at a nonexistent key falls back to 'first dict key',
        not a crash."""
        import provider_dispatch as pd
        from providers.provider_registry import ProviderConfig, ProviderModel

        model = ProviderModel(
            litellm_name="", cost_input_per_mtok=0.0, cost_output_per_mtok=0.0,
            max_tokens=8192, supports_streaming=True, supports_tool_calls=True,
        )
        fake_cfg = ProviderConfig(
            enabled=True, api_key_env="", default_model="kimi-does-not-exist",
            models={"kimi-k2-7": model},
        )
        with patch("providers.provider_registry.load", return_value={"kimi_cli": fake_cfg}):
            assert pd._resolve_kimi_model_label() == "kimi-k2-7"


class TestVerifiedRegistryEntries:
    """kimi-cli 1.46.0 model strings, verified 20260721 against the installed CLI's own
    ~/.kimi/config.toml (models."kimi-code/k3", "kimi-code/kimi-for-coding")."""

    def test_kimi_k3_cli_model_arg_verified(self):
        from providers.provider_registry import load

        entry = load()["kimi_cli"].models["kimi-k3"]
        assert entry.cli_model_arg == "kimi-code/k3"
        assert entry.dispatch_allowed is True

    def test_kimi_k2_7_cli_model_arg_verified(self):
        from providers.provider_registry import load

        entry = load()["kimi_cli"].models["kimi-k2-7"]
        assert entry.cli_model_arg == "kimi-code/kimi-for-coding"
        assert entry.dispatch_allowed is True

    def test_kimi_k2_6_disabled_and_cleared(self):
        """Retired upstream — disabled rather than left dispatchable with a stale arg."""
        from providers.provider_registry import load

        entry = load()["kimi_cli"].models["kimi-k2-6"]
        assert entry.dispatch_allowed is False


# ---------------------------------------------------------------------------
# WS2 — _kimi_resolve_requested_key: precedence + bare-alias normalization
# ---------------------------------------------------------------------------


class TestResolveRequestedKeyPrecedence:
    def test_explicit_model_wins_over_env(self, monkeypatch):
        import provider_dispatch as pd

        monkeypatch.setenv("VNX_KIMI_MODEL", "kimi-k2-7")
        assert pd._kimi_resolve_requested_key("kimi-k3") == "kimi-k3"

    def test_env_used_when_no_explicit_model(self, monkeypatch):
        import provider_dispatch as pd

        monkeypatch.setenv("VNX_KIMI_MODEL", "kimi-k2-7")
        assert pd._kimi_resolve_requested_key(None) == "kimi-k2-7"

    def test_default_placeholder_falls_through_to_env(self, monkeypatch):
        import provider_dispatch as pd

        monkeypatch.setenv("VNX_KIMI_MODEL", "kimi-k2-7")
        assert pd._kimi_resolve_requested_key("default") == "kimi-k2-7"

    def test_sonnet_placeholder_falls_through_to_env(self, monkeypatch):
        """'sonnet' is dispatch-agent's own CLI default, not a real kimi model request."""
        import provider_dispatch as pd

        monkeypatch.setenv("VNX_KIMI_MODEL", "kimi-k2-7")
        assert pd._kimi_resolve_requested_key("sonnet") == "kimi-k2-7"

    def test_none_case_resolves_to_k3_default(self, monkeypatch):
        """No explicit model, no env var -> the registry's K3-flagged default (WS2 #3)."""
        import provider_dispatch as pd

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        assert pd._kimi_resolve_requested_key(None) == "kimi-k3"

    def test_empty_string_env_treated_as_unset(self, monkeypatch):
        import provider_dispatch as pd

        monkeypatch.setenv("VNX_KIMI_MODEL", "")
        assert pd._kimi_resolve_requested_key(None) == "kimi-k3"

    @pytest.mark.parametrize("bare_token", ["kimi", "kimi-default", "KIMI", "Kimi-Default"])
    def test_bare_provider_alias_resolves_to_k3_default(self, monkeypatch, bare_token):
        """'kimi'/'kimi-default' are provider/alias tokens, not model ids — resolve to K3."""
        import provider_dispatch as pd

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        assert pd._kimi_resolve_requested_key(bare_token) == "kimi-k3"

    def test_bare_alias_via_env_also_resolves(self, monkeypatch):
        import provider_dispatch as pd

        monkeypatch.setenv("VNX_KIMI_MODEL", "kimi-default")
        assert pd._kimi_resolve_requested_key(None) == "kimi-k3"

    def test_explicit_real_model_id_passes_through_unresolved(self, monkeypatch):
        """A real model id (not a bare alias) is returned as-is — validity is checked later
        by _kimi_resolve_cli_model_arg, not by this precedence resolver."""
        import provider_dispatch as pd

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        assert pd._kimi_resolve_requested_key("kimi-k2-7") == "kimi-k2-7"
        assert pd._kimi_resolve_requested_key("kimi-bogus") == "kimi-bogus"


# ---------------------------------------------------------------------------
# WS1 — _kimi_resolve_cli_model_arg: fail-loud, no substitution
# ---------------------------------------------------------------------------


class TestResolveCliModelArgFailLoud:
    def test_verified_key_resolves_to_verified_cli_arg(self):
        import provider_dispatch as pd

        assert pd._kimi_resolve_cli_model_arg("kimi-k3") == "kimi-code/k3"
        assert pd._kimi_resolve_cli_model_arg("kimi-k2-7") == "kimi-code/kimi-for-coding"

    def test_unmapped_model_raises_not_returns_raw(self):
        """The core bug being fixed: an unmapped model must never come back unchanged."""
        import provider_dispatch as pd

        with pytest.raises(pd.KimiModelResolutionError):
            pd._kimi_resolve_cli_model_arg("kimi-bogus")

    def test_unmapped_model_never_silently_substitutes_k3(self):
        import provider_dispatch as pd

        with pytest.raises(pd.KimiModelResolutionError) as exc_info:
            pd._kimi_resolve_cli_model_arg("kimi-bogus")
        assert "kimi-code/k3" not in str(exc_info.value)

    def test_disabled_model_raises(self):
        import provider_dispatch as pd

        with pytest.raises(pd.KimiModelResolutionError, match="disabled"):
            pd._kimi_resolve_cli_model_arg("kimi-k2-6")

    def test_registry_load_failure_raises_not_returns_raw(self):
        import provider_dispatch as pd

        with patch("providers.provider_registry.load", side_effect=FileNotFoundError("no yaml")):
            with pytest.raises(pd.KimiModelResolutionError):
                pd._kimi_resolve_cli_model_arg("kimi-k3")

    def test_kimi_cli_section_missing_raises(self):
        import provider_dispatch as pd

        with patch("providers.provider_registry.load", return_value={}):
            with pytest.raises(pd.KimiModelResolutionError):
                pd._kimi_resolve_cli_model_arg("kimi-k3")

    def test_reverse_lookup_still_works_for_cli_arg_form_input(self):
        """A caller already passing the CLI-arg form ('kimi-code/k3') gets it back unchanged."""
        import provider_dispatch as pd

        assert pd._kimi_resolve_cli_model_arg("kimi-code/k3") == "kimi-code/k3"


# ---------------------------------------------------------------------------
# WS2 — _dispatch_kimi (legacy/secondary seam): args.model honored end-to-end
# ---------------------------------------------------------------------------


def _build_args(**overrides):
    defaults = {
        "provider": "kimi",
        "terminal_id": "T1",
        "dispatch_id": "test-dispatch-kimi-resolver",
        "instruction": "Say hi",
        "model": "sonnet",
        "max_retries": 3,
        "no_auto_commit": False,
        "gate": "",
        "dispatch_paths": "",
        "pr_id": None,
        "role": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestDispatchKimiModelResolution:
    def _make_success_result(self):
        from provider_spawns.kimi_spawn import KimiSpawnResult

        return KimiSpawnResult(
            returncode=0, completion_text="hi", events_written=1, session_id=None,
            timed_out=False, token_usage={"input_tokens": 1, "output_tokens": 1},
        )

    def test_none_case_spawns_with_k3_cli_arg(self, monkeypatch):
        """A bare dispatch (args.model='sonnet' placeholder, no env) resolves to K3 — the
        None-case bug (line ~1670 pre-fix: model_cli_arg stayed None -> CLI's own default)."""
        import provider_dispatch as pd

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        args = _build_args(model="sonnet")
        spawn_mock = MagicMock(return_value=self._make_success_result())

        with patch("provider_spawns.kimi_spawn.spawn_kimi", spawn_mock), \
             patch("event_store.EventStore", return_value=MagicMock()), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/r.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/r.md")):
            exit_code = pd._dispatch_kimi(args)

        assert exit_code == 0
        assert spawn_mock.call_args.kwargs["model"] == "kimi-code/k3"

    def test_explicit_model_honored_over_default(self, monkeypatch):
        import provider_dispatch as pd

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        args = _build_args(model="kimi-k2-7")
        spawn_mock = MagicMock(return_value=self._make_success_result())

        with patch("provider_spawns.kimi_spawn.spawn_kimi", spawn_mock), \
             patch("event_store.EventStore", return_value=MagicMock()), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/r.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/r.md")):
            exit_code = pd._dispatch_kimi(args)

        assert exit_code == 0
        assert spawn_mock.call_args.kwargs["model"] == "kimi-code/kimi-for-coding"

    def test_env_var_honored_when_no_explicit_model(self, monkeypatch):
        import provider_dispatch as pd

        monkeypatch.setenv("VNX_KIMI_MODEL", "kimi-k2-7")
        args = _build_args(model="sonnet")
        spawn_mock = MagicMock(return_value=self._make_success_result())

        with patch("provider_spawns.kimi_spawn.spawn_kimi", spawn_mock), \
             patch("event_store.EventStore", return_value=MagicMock()), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/r.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/r.md")):
            exit_code = pd._dispatch_kimi(args)

        assert exit_code == 0
        assert spawn_mock.call_args.kwargs["model"] == "kimi-code/kimi-for-coding"

    def test_bare_provider_alias_resolves_to_k3(self, monkeypatch):
        import provider_dispatch as pd

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        args = _build_args(model="kimi")
        spawn_mock = MagicMock(return_value=self._make_success_result())

        with patch("provider_spawns.kimi_spawn.spawn_kimi", spawn_mock), \
             patch("event_store.EventStore", return_value=MagicMock()), \
             patch("governance_emit.emit_dispatch_receipt", return_value=Path("/tmp/r.ndjson")), \
             patch("governance_emit.emit_unified_report", return_value=Path("/tmp/r.md")):
            exit_code = pd._dispatch_kimi(args)

        assert exit_code == 0
        assert spawn_mock.call_args.kwargs["model"] == "kimi-code/k3"

    def test_bogus_model_fails_loud_never_spawns(self, monkeypatch):
        """--model kimi-bogus must fail before spawn_kimi is ever invoked — no `-m` emitted,
        no silent K3 substitution."""
        import provider_dispatch as pd

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        args = _build_args(model="kimi-bogus")
        spawn_mock = MagicMock(return_value=self._make_success_result())

        with patch("provider_spawns.kimi_spawn.spawn_kimi", spawn_mock), \
             patch("event_store.EventStore", return_value=MagicMock()):
            exit_code = pd._dispatch_kimi(args)

        assert exit_code == 1
        spawn_mock.assert_not_called()

    def test_disabled_model_fails_loud_never_spawns(self, monkeypatch):
        """--model kimi-k2-6 (retired/disabled) must fail before spawn — no stale arg passed."""
        import provider_dispatch as pd

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        args = _build_args(model="kimi-k2-6")
        spawn_mock = MagicMock(return_value=self._make_success_result())

        with patch("provider_spawns.kimi_spawn.spawn_kimi", spawn_mock), \
             patch("event_store.EventStore", return_value=MagicMock()):
            exit_code = pd._dispatch_kimi(args)

        assert exit_code == 1
        spawn_mock.assert_not_called()


# ---------------------------------------------------------------------------
# WS2 — ProviderAdapter.run() kimi branch (governed/door seam, dispatch_envelope.py)
# ---------------------------------------------------------------------------


def _make_kimi_plan(tmp_path, model):
    """Minimal ExecutionPlan for the kimi provider (dispatch_envelope.ProviderAdapter)."""
    from dispatch_plan import ExecutionPlan
    from dispatch_spec import DispatchPath, Isolation, Provider

    instruction_file = tmp_path / "inst.md"
    instruction_file.write_text("test instruction", encoding="utf-8")
    import hashlib

    sha = hashlib.sha256(instruction_file.read_bytes()).hexdigest()
    return ExecutionPlan(
        dispatch_id="test-kimi-seam",
        project_id="test-project",
        provider=Provider.KIMI,
        model=model,
        lane="provider",
        adapter="provider",
        target_id="ephemeral",
        billing="subscription",
        serialization_class=None,
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="n/a",
        deadline_seconds=3600,
        base_ref="origin/main",
        dispatch_paths=(),
        instruction_file=instruction_file,
        route_reason="test",
        instruction_sha256=sha,
    )


class TestProviderAdapterKimiSeam:
    def _make_success_result(self):
        from provider_spawns.kimi_spawn import KimiSpawnResult

        return KimiSpawnResult(
            returncode=0, completion_text="hi", events_written=1, session_id=None,
            timed_out=False, token_usage={"input_tokens": 1, "output_tokens": 1},
        )

    def test_explicit_model_reaches_spawn_as_verified_cli_arg(self, tmp_path, monkeypatch):
        from dispatch_envelope import ProviderAdapter

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        plan = _make_kimi_plan(tmp_path, model="kimi-k3")
        spawn_mock = MagicMock(return_value=self._make_success_result())

        with patch("provider_spawns.kimi_spawn.spawn_kimi", spawn_mock), \
             patch("provider_dispatch._extract_token_usage", return_value={}):
            result = ProviderAdapter().run(plan, "prompt")

        assert result.status == "success"
        assert spawn_mock.call_args.kwargs["model"] == "kimi-code/k3"

    def test_default_placeholder_resolves_to_k3(self, tmp_path, monkeypatch):
        from dispatch_envelope import ProviderAdapter

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        plan = _make_kimi_plan(tmp_path, model="default")
        spawn_mock = MagicMock(return_value=self._make_success_result())

        with patch("provider_spawns.kimi_spawn.spawn_kimi", spawn_mock), \
             patch("provider_dispatch._extract_token_usage", return_value={}):
            result = ProviderAdapter().run(plan, "prompt")

        assert result.status == "success"
        assert spawn_mock.call_args.kwargs["model"] == "kimi-code/k3"

    def test_bogus_model_fails_loud_never_spawns(self, tmp_path, monkeypatch):
        from dispatch_envelope import ProviderAdapter

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        plan = _make_kimi_plan(tmp_path, model="kimi-bogus")
        spawn_mock = MagicMock(return_value=self._make_success_result())

        with patch("provider_spawns.kimi_spawn.spawn_kimi", spawn_mock):
            result = ProviderAdapter().run(plan, "prompt")

        assert result.status == "failure"
        assert "kimi-code/k3" not in (result.error or "")
        spawn_mock.assert_not_called()

    def test_bare_provider_alias_resolves_to_k3(self, tmp_path, monkeypatch):
        from dispatch_envelope import ProviderAdapter

        monkeypatch.delenv("VNX_KIMI_MODEL", raising=False)
        plan = _make_kimi_plan(tmp_path, model="kimi")
        spawn_mock = MagicMock(return_value=self._make_success_result())

        with patch("provider_spawns.kimi_spawn.spawn_kimi", spawn_mock), \
             patch("provider_dispatch._extract_token_usage", return_value={}):
            result = ProviderAdapter().run(plan, "prompt")

        assert result.status == "success"
        assert spawn_mock.call_args.kwargs["model"] == "kimi-code/k3"
