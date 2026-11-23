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
    result, status, returncode, _ = _run_via_door_path(tmp_path, monkeypatch, [CREDIT + "\n", REAL_ANSWER])
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


FIXTURE_20_09 = (
    "API Error: 500 Error calling litellm.acompletion for non-Anthropic model: "
    "litellm.APIError: APIError: OpenrouterException - "
    '{"error":{"message":"This request requires more credits, or fewer max_tokens. '
    'You requested up to 32000 tokens, but can only afford 5085.","code":402}}'
)


def _outcome(text):
    return eap._map_generic_spawn_result(_spawn_result(text), "litellm:zai", model="m")


def test_prose_about_a_402_is_a_real_answer():
    out = _outcome("Fixed the error handling for HTTP 402 insufficient credits; tests pass.")
    assert out.status == "success" and out.returncode == 0


def test_refusal_then_short_summary_is_success():
    out = _outcome(CREDIT + "\nImplemented the retry in scripts/foo.py; tests pass.")
    assert out.status == "success" and out.returncode == 0


def test_many_repeated_refusals_fail_whatever_the_length():
    for text in ("\n".join([CREDIT] * 25), CREDIT * 25):
        assert len(text) >= 1819
        out = _outcome(text)
        assert out.status == "failure" and out.returncode != 0
        assert eap._blocking_refusal_class(text) == "credit_exhausted"


def test_single_refusal_lines():
    assert eap._blocking_refusal_class(CREDIT) == "credit_exhausted"
    assert eap._blocking_refusal_class(AUTH) == "auth_rejected"
    assert _outcome(AUTH).status == "failure"


def test_ff_intel_3b_fixture_fails():
    out = _outcome(FIXTURE_20_09)
    assert out.status == "failure" and out.returncode != 0


@pytest.mark.parametrize("text", ["", " \n ", None, "API Error: 418 teapot", "API Error: 500 boom"])
def test_non_refusal_or_unknown_shapes_are_not_refusal_only(text):
    assert eap._blocking_refusal_class(text) is None


PREFIXED = 'Failed to authenticate. API Error: 401 {"error":{"message":"invalid x-api-key"}}'


def test_prefixed_auth_refusal_alone_fails():
    assert eap._blocking_refusal_class(PREFIXED) == "auth_rejected"
    out = _outcome(PREFIXED)
    assert out.status == "failure" and out.returncode != 0


@pytest.mark.parametrize("sep", ["\n", "", " "])
def test_prefixed_auth_refusal_repeated_fails(sep):
    text = sep.join([PREFIXED] * 3)
    assert eap._blocking_refusal_class(text) == "auth_rejected"
    out = _outcome(text)
    assert out.status == "failure" and out.returncode != 0


def test_auth_prefix_followed_by_prose_is_success():
    out = _outcome("Failed to authenticate. I fixed the retry loop.")
    assert out.status == "success" and out.returncode == 0
