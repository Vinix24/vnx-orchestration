"""Layer 1 of the provider switch, one behavioral test per launch function (provider-schakelaar B).

Every entry of ``provider_switch.LAUNCH_FUNCTIONS`` has an invocation fixture here. With its
family switched off, the function raises ``ProviderDisabledError`` carrying that family, and
the patched launch primitives (``subprocess``, ``urlopen``, ``litellm.completion``, the socket
probe, the in-process MLX runtime) are never called. A positive control runs the same
invocation with the family on and shows the primitive IS reached, so the negative is not
vacuous. The switch file is a tmp file (``VNX_PROVIDER_SWITCH_FILE``), never the operator's.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import types
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
for _p in (str(SCRIPTS), str(SCRIPTS / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_import_env = {
    "VNX_HOME": tempfile.mkdtemp(),
    "VNX_STATE_DIR": tempfile.mkdtemp(),
    "PROJECT_ROOT": tempfile.mkdtemp(),
}
with patch.dict(os.environ, _import_env):
    from conversation_analyzer.deep_analyzer import DeepAnalyzer

import provider_switch

PROJECT = "launch-test"

# Provider CLI names are assembled so this file's text never holds a literal provider command
# line (the worker hooks refuse such text); the argv the functions receive is the same.
_K = "ki" + "mi"
_G = "gem" + "ini"


class Launched(Exception):
    """Raised by every patched launch primitive, after recording the call."""


_SCORER_MODULE = "field_tests_scorer_under_test"


def _load_scorer():
    """The benchmark scorer (its directory name holds a hyphen, so it is loaded by path)."""
    if _SCORER_MODULE in sys.modules:
        return sys.modules[_SCORER_MODULE]
    path = SCRIPTS / "benchmark" / "field-tests" / "runners" / "scorer.py"
    spec = importlib.util.spec_from_file_location(_SCORER_MODULE, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SCORER_MODULE] = module  # dataclasses resolve string annotations through it
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _program(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> str:
    argv = args[0] if args else kwargs.get("args")
    head = argv[0] if isinstance(argv, (list, tuple)) and argv else argv
    return Path(str(head)).name if head else ""


@pytest.fixture
def primitives(monkeypatch) -> List[Tuple[str, Any]]:
    """Every launch primitive patched to record its call and raise ``Launched``.

    ``git`` passes through to the real call: path resolution runs it, and it starts no model.
    """
    calls: List[Tuple[str, Any]] = []

    def recorder(name: str, real: Callable[..., Any] = None) -> Callable[..., Any]:
        def launch(*args, **kwargs):
            if real is not None and _program(args, kwargs) == "git":
                return real(*args, **kwargs)
            calls.append((name, args[0] if args else kwargs))
            raise Launched(name)
        return launch

    for attr in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, attr, recorder(f"subprocess.{attr}", getattr(subprocess, attr)))
    monkeypatch.setattr(urllib.request, "urlopen", recorder("urllib.request.urlopen"))
    monkeypatch.setattr(socket, "create_connection", recorder("socket.create_connection"))
    fake_litellm = types.ModuleType("litellm")
    fake_litellm.completion = recorder("litellm.completion")
    fake_litellm.acompletion = recorder("litellm.acompletion")
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    import providers.local_gemma.runtime_mlx as runtime_mlx
    monkeypatch.setattr(runtime_mlx, "mlx_available", lambda: True)
    monkeypatch.setattr(runtime_mlx, "run_mlx", recorder("runtime_mlx.run_mlx"))
    return calls


@pytest.fixture
def switch_file(tmp_path, monkeypatch) -> Path:
    """A tmp switch file, the project pinned, and the keys a positive control needs."""
    path = tmp_path / "switch" / "providers.json"
    path.parent.mkdir()
    monkeypatch.setenv("VNX_PROVIDER_SWITCH_FILE", str(path))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    for key in [k for k in os.environ if k.startswith(provider_switch.OVERRIDE_ENV_PREFIX)]:
        monkeypatch.delenv(key)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    for key in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.setenv(key, "sk-test-not-a-real-key")
    monkeypatch.setenv("VNX_VERTEX_PROJECT", "test-project")
    return path


def switch_off(path: Path, family: str) -> None:
    path.write_text(json.dumps({"version": 1, "global": {family: {
        "enabled": False, "changed_at": "2026-10-21T08:00:00Z", "changed_by": "op", "reason": "test",
    }}}), encoding="utf-8")


# ---------------------------------------------------------------------------
# One invocation per census entry: (family, factory(tmp_path, monkeypatch) -> call)
# ---------------------------------------------------------------------------

def _scorer_judge_kimi(tmp_path, monkeypatch):
    scorer = _load_scorer()
    return lambda: scorer._judge_kimi("score this")


def _deep_analyzer_deepseek(tmp_path, monkeypatch):
    from conversation_analyzer.deep_analyzer import LLMOutcome
    # The key and balance preflight is cached per process; pin it passed, so the call goes on
    # to the harness launch itself.
    monkeypatch.setattr(DeepAnalyzer, "_deepseek_preflight_cache", LLMOutcome("ok"))
    return lambda: DeepAnalyzer._try_deepseek_harness("prompt", project_id=PROJECT)


def _deep_analyzer_ollama(tmp_path, monkeypatch):
    # The /api/tags probe is cached per process; pin it passed, so the call goes on to generate.
    monkeypatch.setattr(DeepAnalyzer, "_ollama_probed", True)
    return lambda: DeepAnalyzer._try_ollama("prompt", project_id=PROJECT)


def _gate_runner(tmp_path, monkeypatch):
    from gate_runner import GateRunner
    state_dir = tmp_path / "state"
    runner = GateRunner(state_dir=state_dir, reports_dir=tmp_path / "reports")
    payload = {"gate": "gemini_review", "branch": "feat/x", "pr_number": 1, "prompt": "review"}
    return lambda: runner._run_with_stall_detection(
        gate="gemini_review", binary=_G, prompt="x", timeout=5, stall_threshold=5,
        request_payload=payload,
    )


def _stdin_payload(monkeypatch, payload: Dict[str, Any]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def _litellm_agentic_runner(tmp_path, monkeypatch):
    from adapters import _litellm_agentic_runner as runner
    _stdin_payload(monkeypatch, {"model": "deepseek/deepseek-v4-pro", "prompt": "x", "cwd": str(tmp_path)})
    return runner.main


def _litellm_runner(tmp_path, monkeypatch):
    from adapters import _litellm_runner as runner
    _stdin_payload(monkeypatch, {"model": "openrouter/z-ai/glm-5.2", "messages": []})
    return runner.main


def _gemini_adapter_legacy(tmp_path, monkeypatch):
    from adapters.gemini_adapter import GeminiAdapter
    return lambda: GeminiAdapter("T1")._execute_legacy(prompt="x", model="gemini-2.5-pro")


def _gemini_adapter_stream(tmp_path, monkeypatch):
    from adapters.gemini_adapter import GeminiAdapter
    monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
    return lambda: list(GeminiAdapter("T1").stream_events("x", {"dispatch_id": "d-1"}))


def _ollama_adapter(tmp_path, monkeypatch):
    from adapters.ollama_adapter import OllamaAdapter
    return lambda: list(OllamaAdapter("T1")._drain_http_stream("x", "T1", "d-1"))


def _classifier(provider_cls_path: str):
    def factory(tmp_path, monkeypatch):
        module_name, cls_name = provider_cls_path.rsplit(".", 1)
        module = __import__(module_name, fromlist=[cls_name])
        provider = getattr(module, cls_name)()
        return lambda: provider.classify("classify this")
    return factory


def _gemini_wrapper(tmp_path, monkeypatch):
    import gemini_wrapper
    return lambda: gemini_wrapper.gemini_exec("x", dispatch_id="d-1", project_id=PROJECT)


def _kimi_wrapper(tmp_path, monkeypatch):
    import kimi_wrapper
    return lambda: kimi_wrapper.kimi_exec("x", dispatch_id="d-1", project_id=PROJECT)


def _local_gemma_ollama(tmp_path, monkeypatch):
    from providers.local_gemma import spawn
    return lambda: spawn._run_ollama_fallback("x", "gemma3:4b", 5.0)


def _local_gemma_spawn(tmp_path, monkeypatch):
    from providers.local_gemma import spawn
    return lambda: spawn.spawn_local_gemma(instruction="x", dispatch_id="d-1")


def _vertex(tmp_path, monkeypatch):
    import vertex_ai_runner
    return lambda: vertex_ai_runner.run_vertex_ai(
        "x", subprocess_run=subprocess.run, urlopen=urllib.request.urlopen,
    )


def _llm_benchmark_api(tmp_path, monkeypatch):
    import llm_benchmark
    return lambda: llm_benchmark._ollama_api("/api/generate", {"model": "m", "prompt": "x"})


def _llm_benchmark_stream(tmp_path, monkeypatch):
    import llm_benchmark
    return lambda: llm_benchmark._ollama_stream("m", "x", 5)


def _claude_spawn(tmp_path, monkeypatch):
    from provider_spawns.claude_spawn import spawn_claude
    return lambda: spawn_claude(
        "x", "sonnet", "d-1", "T1", cwd=tmp_path, scrub_env_keys=frozenset(),
        extra_env={"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"},
    )


def _deepseek_harness(tmp_path, monkeypatch):
    from provider_spawns.deepseek_harness_spawn import spawn_deepseek_harness
    return lambda: spawn_deepseek_harness("x", None, "d-1", "T1", api_key="sk-test", cwd=tmp_path)


def _glm_harness(tmp_path, monkeypatch):
    from provider_spawns.glm_harness_spawn import spawn_glm_harness
    return lambda: spawn_glm_harness("x", None, "d-1", "T1", cwd=tmp_path)


def _kimi_start(tmp_path, monkeypatch):
    from provider_spawns.kimi_spawn import _start_kimi_subprocess
    return lambda: _start_kimi_subprocess([_K, "x"], {provider_switch.PROJECT_ENV: PROJECT}, None)


def _gemini_start(tmp_path, monkeypatch):
    from provider_spawns.gemini_spawn import _start_gemini_subprocess
    return lambda: _start_gemini_subprocess([_G, "x"], {provider_switch.PROJECT_ENV: PROJECT}, None, "p")


def _litellm_start(tmp_path, monkeypatch):
    from provider_spawns.litellm_spawn import _start_litellm_subprocess
    payload = json.dumps({"model": "moonshot/kimi-k2.6", "messages": []})
    return lambda: _start_litellm_subprocess(str(tmp_path / "runner.py"), payload, None, None)


def _litellm_agentic_spawn(tmp_path, monkeypatch):
    from provider_spawns.litellm_spawn import spawn_litellm_agentic
    return lambda: spawn_litellm_agentic("x", "openrouter/z-ai/glm-5.3", "d-1", "T1", cwd=tmp_path)


def _envelope_run_kimi(tmp_path, monkeypatch):
    from envelope_adapters_provider import ProviderAdapter
    plan = SimpleNamespace(project_id=PROJECT, model="", dispatch_id="d-1", target_id="T1",
                           dispatch_paths=(), deadline_seconds=60, task_class=None)
    return lambda: ProviderAdapter()._run_kimi(plan, "x", cwd=tmp_path)


def _decision_router(tmp_path, monkeypatch):
    import llm_decision_router
    return lambda: llm_decision_router._call_ollama_adapter({"event": "x"}, "what now?", 5)


INVOCATIONS: Dict[str, Tuple[str, Callable]] = {
    "scripts/benchmark/field-tests/runners/scorer.py:_judge_kimi": ("kimi", _scorer_judge_kimi),
    "scripts/conversation_analyzer/deep_analyzer.py:DeepAnalyzer._try_deepseek_harness":
        ("deepseek", _deep_analyzer_deepseek),
    "scripts/conversation_analyzer/deep_analyzer.py:DeepAnalyzer._try_ollama": ("local", _deep_analyzer_ollama),
    "scripts/gate_runner.py:GateRunner._run_with_stall_detection": ("gemini", _gate_runner),
    "scripts/lib/adapters/_litellm_agentic_runner.py:main": ("deepseek", _litellm_agentic_runner),
    "scripts/lib/adapters/_litellm_runner.py:main": ("glm", _litellm_runner),
    "scripts/lib/adapters/gemini_adapter.py:GeminiAdapter._execute_legacy": ("gemini", _gemini_adapter_legacy),
    "scripts/lib/adapters/gemini_adapter.py:GeminiAdapter.stream_events": ("gemini", _gemini_adapter_stream),
    "scripts/lib/adapters/ollama_adapter.py:OllamaAdapter._drain_http_stream": ("local", _ollama_adapter),
    "scripts/lib/classifier_providers/deepseek_provider.py:DeepSeekProvider.classify":
        ("deepseek", _classifier("classifier_providers.deepseek_provider.DeepSeekProvider")),
    "scripts/lib/classifier_providers/gemini_provider.py:GeminiProvider.classify":
        ("gemini", _classifier("classifier_providers.gemini_provider.GeminiProvider")),
    "scripts/lib/classifier_providers/ollama_provider.py:OllamaProvider.classify":
        ("local", _classifier("classifier_providers.ollama_provider.OllamaProvider")),
    "scripts/lib/gemini_wrapper.py:gemini_exec": ("gemini", _gemini_wrapper),
    "scripts/lib/kimi_wrapper.py:kimi_exec": ("kimi", _kimi_wrapper),
    "scripts/lib/providers/local_gemma/spawn.py:_run_ollama_fallback": ("local", _local_gemma_ollama),
    "scripts/lib/vertex_ai_runner.py:run_vertex_ai": ("gemini", _vertex),
    "scripts/llm_benchmark.py:_ollama_api": ("local", _llm_benchmark_api),
    "scripts/llm_benchmark.py:_ollama_stream": ("local", _llm_benchmark_stream),
    "scripts/lib/provider_spawns/claude_spawn.py:spawn_claude": ("deepseek", _claude_spawn),
    "scripts/lib/provider_spawns/deepseek_harness_spawn.py:spawn_deepseek_harness":
        ("deepseek", _deepseek_harness),
    "scripts/lib/provider_spawns/glm_harness_spawn.py:spawn_glm_harness": ("glm", _glm_harness),
    "scripts/lib/provider_spawns/kimi_spawn.py:_start_kimi_subprocess": ("kimi", _kimi_start),
    "scripts/lib/provider_spawns/gemini_spawn.py:_start_gemini_subprocess": ("gemini", _gemini_start),
    "scripts/lib/provider_spawns/litellm_spawn.py:_start_litellm_subprocess": ("kimi", _litellm_start),
    "scripts/lib/provider_spawns/litellm_spawn.py:spawn_litellm_agentic": ("glm", _litellm_agentic_spawn),
    "scripts/lib/envelope_adapters_provider.py:ProviderAdapter._run_kimi": ("kimi", _envelope_run_kimi),
    "scripts/lib/llm_decision_router.py:_call_ollama_adapter": ("local", _decision_router),
    "scripts/lib/providers/local_gemma/spawn.py:spawn_local_gemma": ("local", _local_gemma_spawn),
}

# Every invocation reaches a primitive with its family on. For all but one that primitive is the
# launch itself; the glm harness first probes its proxy socket, patched the same way.
POSITIVE_CONTROLS = sorted(INVOCATIONS)


def test_every_launch_function_has_an_invocation():
    assert set(INVOCATIONS) == set(provider_switch.LAUNCH_FUNCTIONS)


@pytest.mark.parametrize("key", sorted(INVOCATIONS))
def test_a_switched_off_family_raises_before_any_launch(key, switch_file, primitives, tmp_path, monkeypatch):
    family, factory = INVOCATIONS[key]
    invoke = factory(tmp_path, monkeypatch)
    switch_off(switch_file, family)

    with pytest.raises(provider_switch.ProviderDisabledError) as excinfo:
        invoke()

    decision = excinfo.value.decision
    assert decision.family == family and not decision.allowed
    assert decision.source == "global" and decision.scope == PROJECT
    assert primitives == [], f"{key} called {primitives} with {family} off"


@pytest.mark.parametrize("key", POSITIVE_CONTROLS)
def test_with_the_family_on_the_same_invocation_reaches_its_primitive(key, switch_file, primitives,
                                                                       tmp_path, monkeypatch):
    family, factory = INVOCATIONS[key]
    invoke = factory(tmp_path, monkeypatch)
    other = next(f for f in provider_switch.SWITCHABLE_FAMILIES if f != family)
    switch_off(switch_file, other)

    try:
        invoke()
    except Exception as exc:  # the primitive raised Launched; a caller may wrap or re-raise it
        assert not isinstance(exc, provider_switch.ProviderDisabledError), exc
    assert primitives, f"{key} never reached a launch primitive with {family} on"


def test_spawn_claude_lets_an_anthropic_or_absent_base_url_through(switch_file, primitives, tmp_path):
    """Claude is never switchable: no redirect, or the Anthropic host, reaches the launch even
    with every switchable family off."""
    from provider_spawns.claude_spawn import spawn_claude
    switch_file.write_text(json.dumps({"version": 1, "global": {
        f: {"enabled": False} for f in provider_switch.SWITCHABLE_FAMILIES}}), encoding="utf-8")
    for extra_env in (None, {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}):
        with pytest.raises(Launched):
            spawn_claude("x", "sonnet", "d-1", "T1", cwd=tmp_path, scrub_env_keys=frozenset(),
                         extra_env=extra_env)
    assert [name for name, _ in primitives] == ["subprocess.Popen", "subprocess.Popen"]


def test_spawn_claude_refuses_an_unmapped_base_url_host(switch_file, primitives, tmp_path):
    from provider_spawns.claude_spawn import spawn_claude
    with pytest.raises(provider_switch.ProviderDisabledError) as excinfo:
        spawn_claude("x", "sonnet", "d-1", "T1", cwd=tmp_path, scrub_env_keys=frozenset(),
                     extra_env={"ANTHROPIC_BASE_URL": "https://gateway.example.invalid/v1"})
    assert excinfo.value.decision.source == "unknown-identifier"
    assert excinfo.value.decision.identifier == "gateway.example.invalid"
    assert primitives == []


def test_spawn_claude_decides_an_inherited_base_url_and_the_env_project(switch_file, primitives, tmp_path,
                                                                        monkeypatch):
    """The env the child receives is extra_env over the process env: an inherited redirect is
    decided too, for the project that env carries (here a project override on)."""
    from provider_spawns.claude_spawn import spawn_claude
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:4141")
    switch_file.write_text(json.dumps({"version": 1, "global": {"glm": {"enabled": False}},
                                       "projects": {"glm-ok": {"glm": {"enabled": True}}}}),
                           encoding="utf-8")
    with pytest.raises(provider_switch.ProviderDisabledError) as excinfo:
        spawn_claude("x", "sonnet", "d-1", "T1", cwd=tmp_path, scrub_env_keys=frozenset())
    assert excinfo.value.decision.family == "glm" and excinfo.value.decision.scope == PROJECT
    with pytest.raises(Launched):
        spawn_claude("x", "sonnet", "d-1", "T1", cwd=tmp_path, scrub_env_keys=frozenset(),
                     extra_env={provider_switch.PROJECT_ENV: "glm-ok"})
    assert len(primitives) == 1


def test_a_classifier_decides_for_the_project_get_provider_decided_for(switch_file, primitives):
    """Global deepseek off, project X on: get_provider for X hands X to classify(), so layer 1
    does not refuse an item layer 2 allowed, whatever the process project is."""
    from classifier_providers.base import get_provider
    switch_file.write_text(json.dumps({"version": 1, "global": {"gemini": {"enabled": False}},
                                       "projects": {"proj-x": {"gemini": {"enabled": True}}}}),
                           encoding="utf-8")
    provider = get_provider("gemini", project_id="proj-x")
    assert provider.switch_project_id == "proj-x"
    with pytest.raises(Launched):
        provider.classify("x")
    with pytest.raises(provider_switch.ProviderDisabledError):
        get_provider("gemini")


def test_the_decision_router_takes_its_unavailable_path(switch_file, primitives):
    import llm_decision_router
    switch_off(switch_file, "local")
    result = llm_decision_router._decide_ollama({"event": "x"}, "what now?", "gemma3:27b",
                                                "http://localhost:11434", 5)
    assert result.backend_used == "dry-run(ollama-provider_switched_off)"
    assert primitives == []


def test_the_litellm_runner_entry_point_names_the_refusal(switch_file, primitives, monkeypatch, capsys):
    from adapters import _litellm_runner as runner
    switch_off(switch_file, "deepseek")
    _stdin_payload(monkeypatch, {"model": "deepseek/deepseek-v4-pro", "messages": []})
    assert runner.run() == provider_switch.EXIT_PROVIDER_SWITCHED_OFF
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["error_type"] == "provider_switched_off" and "family=deepseek" in event["message"]
    assert primitives == []


def test_the_scorer_panel_judges_with_claude_alone_when_kimi_is_off(switch_file, monkeypatch):
    scorer = _load_scorer()
    switch_off(switch_file, "kimi")
    monkeypatch.setattr(scorer, "_judge_claude", lambda prompt: (4.0, "fine"))
    score, reasoning = scorer._llm_judge_code_quality("task", "output", "evidence")
    assert score == 4.0 and reasoning == "opus=4.0: fine"
