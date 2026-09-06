"""test_envelope_provider_adapter_role_env.py — OI-1215: the provider-lane envelope
adapter must thread ``VNX_WORKER_ROLE`` into every provider spawn's ``extra_env``.

Measured root cause (OI-1215, punt 9): the door routes provider-lane dispatches
through ``dispatch_cli.run_dispatch -> run_envelope_plan -> ProviderAdapter.run``,
NOT through ``provider_dispatch._dispatch_*`` wrappers. ``ProviderAdapter.run``
called the ``spawn_*`` functions WITHOUT ``role``/``extra_env``, so the
``VNX_WORKER_ROLE`` overlay built by ``provider_dispatch._worker_role_env`` (the
#1522 fix, exercised at its seven ``_dispatch_*`` call sites) never reached the
provider-lane worker env. Every provider-lane worker therefore resolved to the
restrictive code-worker fallback in ``pretooluse_worker_scope_enforce.py``.

These tests pin the fix at the ONE loss point — the adapter — by mocking each
spawn function and asserting it receives ``extra_env={"VNX_WORKER_ROLE": <role>}``
for a genuinely-set role and ``extra_env=None`` (no fabricated role) when absent.

Pre-merge vs post-merge evidence (the dispatch's own footgun, OI-1201): these
tests exercise the adapter DIRECTLY (no real spawn), so they are PRE-MERGE proof
that the new code threads the overlay. The end-to-end proof — a real provider-lane
worker whose env actually contains ``VNX_WORKER_ROLE`` — is POST-MERGE only: the
dispatcher builds the worker argv with main-branch code, so a worker on this
feature branch is itself launched by the OLD adapter.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_spec import Provider  # noqa: E402
from envelope_adapters_provider import ProviderAdapter  # noqa: E402


class _OkSpawnResult:
    """Minimal spawn-result stub with the attributes the adapter's mapping code reads."""

    returncode = 0
    error = None
    timed_out = False
    stopped_early = False
    completion_text = "done"
    event_writer_failures = 0
    token_usage = {"input_tokens": 1, "output_tokens": 1}


def _plan(provider: Provider, *, role="research-analyst", model="gpt-5-codex") -> SimpleNamespace:
    return SimpleNamespace(
        provider=provider,
        model=model,
        dispatch_id="oi1215-role-env-test",
        target_id="T1",
        deadline_seconds=900,
        task_class=None,
        role=role,
    )


# (provider, spawn-module-dotted-path, model) — one entry per subprocess-spawning
# provider branch in ProviderAdapter.run. local-gemma runs in-process (no env) and
# is asserted separately.
SUBPROCESS_PROVIDERS = [
    ("codex", "provider_spawns.codex_spawn.spawn_codex", "gpt-5-codex"),
    ("gemini", "provider_spawns.gemini_spawn.spawn_gemini", "gemini-2.5-pro"),
    ("litellm-deepseek", "provider_spawns.litellm_spawn.spawn_litellm", "deepseek-v4-pro"),
    ("deepseek-harness", "provider_spawns.deepseek_harness_spawn.spawn_deepseek_harness", "deepseek-v4-pro"),
    ("glm-harness", "provider_spawns.glm_harness_spawn.spawn_glm_harness", "glm-5.2"),
]


@pytest.mark.parametrize("provider_key,spawn_path,model", SUBPROCESS_PROVIDERS)
def test_spawn_receives_worker_role_overlay(provider_key, spawn_path, model):
    """A genuinely-set plan.role must reach each provider spawn's extra_env.

    RED on origin/main: the adapter called the spawn WITHOUT extra_env, so the
    call carried no ``VNX_WORKER_ROLE`` key at all.
    """
    provider = Provider({
        "codex": "codex",
        "gemini": "gemini",
        "litellm-deepseek": "litellm:deepseek",
        "deepseek-harness": "deepseek-harness",
        "glm-harness": "glm-harness",
    }[provider_key])
    plan = _plan(provider, model=model)

    with patch(spawn_path, return_value=_OkSpawnResult()) as mock_spawn:
        ProviderAdapter().run(plan, "implement the change")

    mock_spawn.assert_called_once()
    kwargs = mock_spawn.call_args.kwargs
    assert "extra_env" in kwargs, (
        f"{spawn_path} was called without an extra_env kwarg — the overlay is not threaded"
    )
    # OI-1635: the overlay also always carries VNX_DATA_DIR (see
    # provider_dispatch._worker_role_env) — check role-key membership rather
    # than exact dict equality.
    assert kwargs["extra_env"].get("VNX_WORKER_ROLE") == "research-analyst", (
        f"{spawn_path} got extra_env={kwargs['extra_env']!r}, "
        f"expected VNX_WORKER_ROLE='research-analyst'"
    )


def test_kimi_spawn_receives_worker_role_overlay():
    """The kimi branch (separate _run_kimi method) threads the same overlay."""
    plan = _plan(Provider.KIMI, model="kimi-k3")

    with (
        patch("provider_dispatch._kimi_resolve_requested_key", return_value="kimi-k3"),
        patch("provider_dispatch._kimi_resolve_cli_model_arg", return_value="kimi-k3"),
        patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=_OkSpawnResult()) as mock_spawn,
    ):
        ProviderAdapter().run(plan, "implement the change")

    mock_spawn.assert_called_once()
    kwargs = mock_spawn.call_args.kwargs
    assert "extra_env" in kwargs
    assert kwargs["extra_env"].get("VNX_WORKER_ROLE") == "research-analyst"


def test_absent_role_threads_no_overlay():
    """role=None must thread no VNX_WORKER_ROLE key — never a fabricated default.

    OI-1635: extra_env is no longer None in this case (it always carries the
    VNX_DATA_DIR stamp — see provider_dispatch._worker_role_env), so the
    assertion checks role-key absence rather than extra_env is None.
    """
    plan = _plan(Provider.DEEPSEEK_HARNESS, role=None)

    with patch(
        "provider_spawns.deepseek_harness_spawn.spawn_deepseek_harness",
        return_value=_OkSpawnResult(),
    ) as mock_spawn:
        ProviderAdapter().run(plan, "implement the change")

    mock_spawn.assert_called_once()
    kwargs = mock_spawn.call_args.kwargs
    assert "extra_env" in kwargs
    assert kwargs["extra_env"] is not None
    assert "VNX_WORKER_ROLE" not in kwargs["extra_env"], (
        f"absent role must thread no VNX_WORKER_ROLE key, got {kwargs['extra_env']!r}"
    )


def test_identity_unresolved_sentinel_threads_no_overlay():
    """The canonical identity_unresolved sentinel must NOT leak into the env."""
    plan = _plan(Provider.DEEPSEEK_HARNESS, role="identity_unresolved")

    with patch(
        "provider_spawns.deepseek_harness_spawn.spawn_deepseek_harness",
        return_value=_OkSpawnResult(),
    ) as mock_spawn:
        ProviderAdapter().run(plan, "implement the change")

    mock_spawn.assert_called_once()
    assert "VNX_WORKER_ROLE" not in mock_spawn.call_args.kwargs["extra_env"]


def test_local_gemma_receives_plan_role():
    """local-gemma spawns in-process (no env), but must still receive plan.role —
    the hardcoded role=None was the same 'role never leaves the adapter' defect."""
    plan = _plan(Provider.LOCAL_GEMMA, model="gemma-4b-local")

    with patch(
        "provider_spawns.local_gemma_spawn.spawn_local_gemma",
        return_value=_OkSpawnResult(),
    ) as mock_spawn:
        ProviderAdapter().run(plan, "implement the change")

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["role"] == "research-analyst"


def test_local_gemma_absent_role_is_none():
    """role=None for local-gemma must stay None (no fabricated default)."""
    plan = _plan(Provider.LOCAL_GEMMA, role=None, model="gemma-4b-local")

    with patch(
        "provider_spawns.local_gemma_spawn.spawn_local_gemma",
        return_value=_OkSpawnResult(),
    ) as mock_spawn:
        ProviderAdapter().run(plan, "implement the change")

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs["role"] is None


# ---------------------------------------------------------------------------
# OI-1231/OI-1244: the spawn-side role channel (the part this dispatch adds).
#
# The OI-1215 fix above threads VNX_WORKER_ROLE (the worker-side env channel the
# pretooluse hook reads). But the SPAWN-SIDE scope-args builder
# (subprocess_adapter._build_worker_scope_args -> resolve_worker_profile) is what
# actually logs ``resolve_worker_profile: role is None`` in the door output — and
# it reads the ``role`` param of ``spawn_claude``, not VNX_WORKER_ROLE. The harness
# spawns (deepseek/glm) forward their **kwargs to spawn_claude, so the adapter must
# pass ``role=`` through them for the role to reach resolve_worker_profile at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_key,spawn_path,model", [
    ("deepseek-harness", "provider_spawns.deepseek_harness_spawn.spawn_deepseek_harness", "deepseek-v4-pro"),
    ("glm-harness", "provider_spawns.glm_harness_spawn.spawn_glm_harness", "glm-5.2"),
])
def test_harness_spawn_receives_role_kwarg(provider_key, spawn_path, model):
    """The harness spawns receive role= so spawn_claude can hand it to
    resolve_worker_profile (spawn-side), not just the VNX_WORKER_ROLE env overlay."""
    provider = Provider({
        "deepseek-harness": "deepseek-harness",
        "glm-harness": "glm-harness",
    }[provider_key])
    plan = _plan(provider, model=model)

    with patch(spawn_path, return_value=_OkSpawnResult()) as mock_spawn:
        ProviderAdapter().run(plan, "implement the change")

    mock_spawn.assert_called_once()
    assert mock_spawn.call_args.kwargs.get("role") == "research-analyst", (
        f"{spawn_path} was not handed role=research-analyst: "
        f"got {mock_spawn.call_args.kwargs.get('role')!r}"
    )


def test_explicit_role_wins_over_plan_role_for_harness():
    """ProviderAdapter.run(role=...) must override plan.role on BOTH channels:
    the spawn-side role= kwarg and the worker-side VNX_WORKER_ROLE env overlay."""
    plan = _plan(Provider.DEEPSEEK_HARNESS, role="research-analyst")

    with patch(
        "provider_spawns.deepseek_harness_spawn.spawn_deepseek_harness",
        return_value=_OkSpawnResult(),
    ) as mock_spawn:
        ProviderAdapter().run(plan, "implement the change", role="backend-developer")

    mock_spawn.assert_called_once()
    kwargs = mock_spawn.call_args.kwargs
    assert kwargs.get("role") == "backend-developer"
    assert kwargs.get("extra_env", {}).get("VNX_WORKER_ROLE") == "backend-developer"
