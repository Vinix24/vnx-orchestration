"""OI-1803: a provider run that got only credit/auth refusals books failure, not success."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import envelope_adapters_provider as eap
from dispatch_envelope import _envelope_outcome
from dispatch_spec import Provider
from provider_spawns import litellm_spawn

REAL_ANSWER = "Implemented the change in scripts/foo.py and added tests. " * 40

CREDIT = 'API Error: 402 {"error":{"message":"Insufficient credits. Add more credits to continue."}}'
AUTH = 'API Error: 401 {"error":{"message":"Unauthorized: invalid api key"}}'

_FAKE_RUNNER = textwrap.dedent(
    '''
    import json, sys
    sys.stdin.read()
    for text in json.loads({texts!r}):
        print(json.dumps({{"choices": [{{"delta": {{"content": text}}}}]}}), flush=True)
    sys.exit(0)
    '''
)


def _plan():
    return SimpleNamespace(
        provider=Provider.LITELLM_ZAI, model="openrouter/z-ai/glm-5", dispatch_id="oi1803-x",
        target_id="T1", role=None, deadline_seconds=60, dispatch_paths=(), project_id=None,
    )


def _run_via_door_path(tmp_path, monkeypatch, texts):
    """Real spawn_litellm against a fake runner process, then the envelope's own outcome."""
    runner = tmp_path / "fake_runner.py"
    runner.write_text(_FAKE_RUNNER.format(texts=json.dumps(texts)))
    monkeypatch.setattr(litellm_spawn, "_RUNNER_PATH", runner)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    result = eap.ProviderAdapter().run(_plan(), "do the work", cwd=tmp_path)
    status, returncode, error = _envelope_outcome(result.status, Path("receipt"), result.error)
    return result, status, returncode, error


def _spawn_result(text, rc=0):
    return SimpleNamespace(
        returncode=rc, completion_text=text, error=None, timed_out=False,
        stopped_early=False, event_writer_failures=0, token_usage=None,
        session_id=None, spawn_refused=None,
    )


@pytest.mark.parametrize("refusal,klass", [(CREDIT, "credit_exhausted"), (AUTH, "auth_rejected")])
def test_refusal_only_run_fails_and_door_exits_nonzero(tmp_path, monkeypatch, refusal, klass):
    from failure_classification import classify_failure

    result, status, returncode, error = _run_via_door_path(tmp_path, monkeypatch, [refusal, refusal])
    assert result.status == "failure"
    assert status == "failure"
    assert returncode != 0
    assert result.returncode != 0
    assert error
    got = classify_failure(
        status=result.status, error=result.error, completion_text=result.completion_text,
        provider="litellm:zai",
    )
    assert got["failure_class"] == klass


@pytest.mark.parametrize("mapper", ["generic", "harness"])
@pytest.mark.parametrize("refusal", [CREDIT, AUTH])
def test_both_mappers_refuse_refusal_only_completion(mapper, refusal):
    res = _spawn_result(refusal)
    if mapper == "generic":
        out = eap._map_generic_spawn_result(res, "litellm:zai", model="m")
    else:
        out = eap._map_harness_spawn_result(res, "glm-harness", "m")
    assert out.status == "failure"
    assert out.returncode != 0
    assert out.error


def test_one_refusal_then_real_answer_stays_success(tmp_path, monkeypatch):
    result, status, returncode, _ = _run_via_door_path(tmp_path, monkeypatch, [CREDIT, REAL_ANSWER])
    assert (result.status, status, returncode) == ("success", "success", 0)


def test_normal_completion_unchanged(tmp_path, monkeypatch):
    result, status, returncode, _ = _run_via_door_path(tmp_path, monkeypatch, [REAL_ANSWER])
    assert (result.status, status, returncode) == ("success", "success", 0)


def test_short_non_refusal_answer_stays_success():
    out = eap._map_generic_spawn_result(_spawn_result("Done: renamed the helper."), "x", model="m")
    assert out.status == "success"


def test_empty_completion_still_fails():
    out = eap._map_generic_spawn_result(_spawn_result(""), "x", model="m")
    assert out.status == "failure" and out.returncode != 0
