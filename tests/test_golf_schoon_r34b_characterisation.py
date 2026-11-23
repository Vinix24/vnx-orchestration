"""Characterisation of ``ProviderAdapter.run`` per provider branch (golf schoon R34b).

Pins the spawn kwargs, model resolution, error labels and result mapping of the gemini,
litellm, harness and local-gemma branches, plus the codex/kimi delegation. Complements
``tests/test_golf_schoon_r34_characterisation.py``. Only the provider spawns (external
processes) are patched, each at the module the branch imports it from at call time.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(_LIB))

from dispatch_spec import Provider  # noqa: E402
from provider_dispatch import _build_lane_key, _extract_token_usage  # noqa: E402

_spec = importlib.util.spec_from_file_location("_r34b_envelope_adapters_provider", _LIB / "envelope_adapters_provider.py")
eap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eap)
ProviderAdapter = eap.ProviderAdapter


class _Spawn:
    returncode = 0
    error = None
    timed_out = False
    stopped_early = False
    completion_text = "done"
    event_writer_failures = 0
    token_usage = {"input_tokens": 3, "output_tokens": 4}


def _res(**attrs) -> _Spawn:
    res = _Spawn()
    for k, v in attrs.items():
        setattr(res, k, v)
    return res


def _plan(provider: str, model: str = "default") -> SimpleNamespace:
    return SimpleNamespace(provider=Provider(provider), model=model, dispatch_id="r34b-d", target_id="T2",
                           deadline_seconds=600, task_class=None, role="backend-developer", project_id="p1")


def _writer(_event):
    return None


def test_litellm_explicit_model_skips_every_resolver():
    plan = _plan("litellm:zai", model="glm-x")
    with patch("provider_dispatch._resolve_deepseek_model") as r1, \
         patch("provider_dispatch._resolve_zai_model") as r2, \
         patch("provider_dispatch._resolve_moonshot_model") as r3, \
         patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=_Spawn()) as spawn:
        out = ProviderAdapter().run(plan, "go", event_writer=_writer, cwd=Path("/tmp"), role="reviewer")
    assert not (r1.called or r2.called or r3.called)
    assert spawn.call_args.args == ()
    assert spawn.call_args.kwargs == {
        "prompt": "go", "model": "glm-x", "dispatch_id": "r34b-d", "terminal_id": "T2",
        "event_writer": _writer, "sub_provider": "zai", "lane": _build_lane_key("zai", None),
        "extra_env": eap._plan_worker_env(plan, "reviewer"), "cwd": Path("/tmp"),
        "total_deadline": 600.0,
    }
    assert (out.status, out.returncode, out.model) == ("success", 0, "glm-x")
    assert out.token_usage == _extract_token_usage(spawn.return_value, "litellm:zai")


def test_litellm_empty_completion_fails_loud():
    with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=_res(completion_text="")):
        out = ProviderAdapter().run(_plan("litellm:moonshot", model="m"), "x")
    assert (out.status, out.returncode) == ("failure", 1)
    assert out.error.startswith("provider=litellm:moonshot returned an empty completion with returncode=0")


def test_gemini_full_kwargs_and_spawn_error():
    plan = _plan("gemini", model="gemini-explicit")
    with patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=_res(error="quota", returncode=7)) as spawn:
        out = ProviderAdapter().run(plan, "go", event_writer=_writer, cwd=Path("/w"))
    assert spawn.call_args.kwargs == {
        "prompt": "go", "model": "gemini-explicit", "dispatch_id": "r34b-d", "terminal_id": "T2",
        "event_writer": _writer, "extra_env": eap._plan_worker_env(plan, "backend-developer"), "cwd": Path("/w"),
        "total_deadline": 600.0,
    }
    assert (out.status, out.returncode, out.error, out.model) == ("failure", 7, "quota", "gemini-explicit")


@pytest.mark.parametrize("model", ["default", "sonnet", ""])
def test_gemini_placeholder_without_env_uses_builtin_default(model, monkeypatch):
    monkeypatch.delenv("VNX_GEMINI_MODEL", raising=False)
    with patch("provider_spawns.gemini_spawn.spawn_gemini", return_value=_res(timed_out=True)) as spawn:
        out = ProviderAdapter().run(_plan("gemini", model=model), "x")
    assert spawn.call_args.kwargs["model"] == "gemini-2.5-pro"
    assert (out.status, out.timed_out, out.model) == ("timeout", True, "gemini-2.5-pro")


@pytest.mark.parametrize("provider,spawn_path,label", [
    ("deepseek-harness", "provider_spawns.deepseek_harness_spawn.spawn_deepseek_harness", "deepseek-harness"),
    ("glm-harness", "provider_spawns.glm_harness_spawn.spawn_glm_harness", "glm-harness"),
])
def test_harness_branches_kwargs_and_broken_pipe(provider, spawn_path, label):
    from harness_run_cost import lane_model
    plan = _plan(provider, model="h-model")
    res = _res(session_id="sess-1", spawn_refused=None)
    with patch(spawn_path, return_value=res) as spawn:
        out = ProviderAdapter().run(plan, "go", event_writer=_writer, cwd=Path("/w"), role="tester")
    assert spawn.call_args.kwargs == {
        "prompt": "go", "model": lane_model(provider, "h-model"), "dispatch_id": "r34b-d", "terminal_id": "T2",
        "event_writer": _writer, "extra_env": eap._plan_worker_env(plan, "tester"), "cwd": Path("/w"),
        "role": "tester", "total_deadline": 600.0,
    }
    assert isinstance(spawn.call_args.kwargs["total_deadline"], float)
    assert (out.status, out.session_id, out.model) == ("success", "sess-1", lane_model(provider, "h-model"))
    with patch(spawn_path, side_effect=BrokenPipeError("eof")):
        out = ProviderAdapter().run(plan, "go")
    assert (out.status, out.returncode, out.error) == ("failure", 1, f"{label} spawn BrokenPipeError: eof")


def test_local_gemma_default_model_maps_to_mlx_and_role_override():
    with patch("provider_spawns.local_gemma_spawn.spawn_local_gemma", return_value=_Spawn()) as spawn:
        out = ProviderAdapter().run(_plan("local-gemma", model="sonnet"), "inst", role="reviewer")
    assert spawn.call_args.kwargs == {
        "instruction": "inst", "model": "mlx-community/gemma-3-4b-it-4bit", "role": "reviewer",
        "deadline_seconds": 300, "dispatch_id": "r34b-d", "project_id": "p1",
    }
    assert (out.status, out.returncode, out.model, out.error, out.completion_text) == (
        "success", 0, "mlx-community/gemma-3-4b-it-4bit", None, "done")
    assert out.token_usage == _extract_token_usage(_Spawn(), "local-gemma") and out.token_usage["output"] == 4
    assert out.event_writer_failures == 0


def test_local_gemma_unmapped_model_passes_raw_and_patched_map_is_read():
    with patch("provider_spawns.local_gemma_spawn.spawn_local_gemma", return_value=_Spawn()) as spawn:
        out = ProviderAdapter().run(_plan("local-gemma", model="my-gemma"), "i")
    assert spawn.call_args.kwargs["model"] == "my-gemma" and out.model == "my-gemma"
    with patch.dict("provider_dispatch._MLX_MODEL_MAP", {"my-gemma": "mlx/mine"}), \
         patch("provider_spawns.local_gemma_spawn.spawn_local_gemma", return_value=_Spawn()) as spawn:
        out = ProviderAdapter().run(_plan("local-gemma", model="my-gemma"), "i")
    assert spawn.call_args.kwargs["model"] == "mlx/mine" and out.model == "mlx/mine"


@pytest.mark.parametrize("attrs,expected", [
    ({"completion_text": ""}, ("failure", 1, "provider=local-gemma returned an empty completion with returncode=0")),
    ({"error": "warn-only", "returncode": 0}, ("success", 0, None)),
    ({"returncode": 3, "completion_text": ""}, ("failure", 3, "provider=local-gemma returned an empty completion with returncode=3 — no error captured by spawn")),
    ({"returncode": 4, "completion_text": "partial"}, ("failure", 4, None)),
])
def test_local_gemma_result_mapping_edges(attrs, expected):
    with patch("provider_spawns.local_gemma_spawn.spawn_local_gemma", return_value=_res(**attrs)):
        out = ProviderAdapter().run(_plan("local-gemma"), "i")
    status, rc, err = expected
    assert (out.status, out.returncode) == (status, rc)
    assert (out.error or "").startswith(err) if err else out.error is None


def test_local_gemma_broken_pipe_is_not_caught():
    with patch("provider_spawns.local_gemma_spawn.spawn_local_gemma", side_effect=BrokenPipeError("p")):
        with pytest.raises(BrokenPipeError):
            ProviderAdapter().run(_plan("local-gemma"), "i")


def test_codex_and_kimi_delegate_with_their_arguments():
    plan = _plan("codex")
    sentinel = object()
    with patch.object(ProviderAdapter, "_run_codex", return_value=sentinel) as codex:
        assert ProviderAdapter().run(plan, "c", event_writer=_writer, cwd=Path("/c"), role="r1") is sentinel
    assert codex.call_args.args == (plan, "c")
    assert codex.call_args.kwargs == {"event_writer": _writer, "cwd": Path("/c"),
                                      "role_env": eap._plan_worker_env(plan, "r1")}
    kplan = _plan("kimi")
    with patch.object(ProviderAdapter, "_run_kimi", return_value=sentinel) as kimi:
        assert ProviderAdapter().run(kplan, "k", cwd=Path("/k")) is sentinel
    assert kimi.call_args.args == (kplan, "k")
    assert kimi.call_args.kwargs == {"event_writer": None, "cwd": Path("/k"), "role": "backend-developer"}


def test_auto_provider_raises_value_error_with_exact_message():
    with pytest.raises(ValueError) as exc:
        ProviderAdapter().run(_plan("auto"), "x")
    assert str(exc.value) == (
        f"ProviderAdapter: unsupported provider {Provider.AUTO!r} — claude and auto do not route through "
        "the provider envelope (claude_headless runs through run_envelope_headless_plan)"
    )
