#!/usr/bin/env python3
"""OI-2062: the codex model comes from the registry in every launch path, and a
refused model is named in the gate result.

Five launch paths name a codex model: gate_runner ``_build_gate_cmd``,
``codex_wrapper``, ``codex_spawn``, the plan-gate panel and the deliberation
panel. Each must read ``openai.default_model`` from wave7_models.yaml instead of
carrying a name of its own, so a hard-coded name cannot cause the next refusal.
"""

from __future__ import annotations

import ast
import itertools
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import codex_wrapper  # noqa: E402
import deliberation_panel  # noqa: E402
import gate_runner  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402
from failure_classification import find_codex_model_refusal  # noqa: E402
from gate_recorder import EXECUTION_FAILURE_REASONS, QUOTA_REFUSAL_REASON  # noqa: E402
from gate_request_handler import GateRequestHandlerMixin  # noqa: E402
from gate_runner import GateRunner  # noqa: E402
from provider_spawns import codex_spawn  # noqa: E402
from providers import provider_registry  # noqa: E402

TMP_MODEL = "tmp-registry-model"

REFUSAL_CHATGPT = "The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account."
REFUSAL_OLD_CLI = (
    "The 'gpt-6-astra' model requires a newer version of Codex. "
    "Please upgrade to the latest app or CLI and try again."
)

_MODEL_BLOCK = """      {key}:
        litellm_name: "openai/{key}"
        cost_input_per_mtok: 1.0
        cost_output_per_mtok: 2.0
        max_tokens: 1000
        supports_streaming: true
        supports_tool_calls: true
"""


def _write_registry(path: Path, *, default_model: str | None, models=(TMP_MODEL,)) -> Path:
    lines = ["providers:", "  openai:", "    enabled: true", "    api_key_env: OPENAI_API_KEY"]
    if default_model is not None:
        lines.append(f"    default_model: {default_model}")
    lines.append("    models:")
    body = "\n".join(lines) + "\n" + "".join(_MODEL_BLOCK.format(key=k) for k in models)
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    """A registry whose openai.default_model differs from the shipped one."""
    path = _write_registry(tmp_path / "registry.yaml", default_model=TMP_MODEL)
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", path)
    for name in ("VNX_CODEX_HEADLESS_MODEL", "VNX_CODEX_MODEL", "VNX_CODEX_DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def runner(tmp_path):
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    for sub in ("requests", "results"):
        (state_dir / "review_gates" / sub).mkdir(parents=True)
    reports_dir.mkdir()
    return GateRunner(state_dir=state_dir, reports_dir=reports_dir)


def _model_arg(cmd):
    return [a for a in cmd if a.startswith("model=")]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_shipped_registry_carries_a_default_that_is_a_registered_model():
    cfg = provider_registry.load()["openai"]
    assert cfg.default_model
    assert cfg.default_model in cfg.models


def test_resolver_order_explicit_then_env_then_registry(tmp_registry, monkeypatch):
    assert provider_registry.resolve_codex_model() == TMP_MODEL
    monkeypatch.setenv("FIRST", "from-first")
    monkeypatch.setenv("SECOND", "from-second")
    assert provider_registry.resolve_codex_model(env_names=("FIRST", "SECOND")) == "from-first"
    assert provider_registry.resolve_codex_model(env_names=("MISSING", "SECOND")) == "from-second"
    assert provider_registry.resolve_codex_model("explicit", env_names=("FIRST",)) == "explicit"


def test_resolver_fails_closed_without_default_model(tmp_path, monkeypatch):
    path = _write_registry(tmp_path / "registry.yaml", default_model=None)
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", path)
    with pytest.raises(provider_registry.CodexModelResolutionError, match="no openai.default_model"):
        provider_registry.resolve_codex_model()


def test_resolver_fails_closed_when_default_is_not_a_registered_model(tmp_path, monkeypatch):
    path = _write_registry(tmp_path / "registry.yaml", default_model="ghost")
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", path)
    with pytest.raises(provider_registry.CodexModelResolutionError, match="not one of the registered"):
        provider_registry.resolve_codex_model()


def test_resolver_fails_closed_when_registry_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", tmp_path / "absent.yaml")
    with pytest.raises(provider_registry.CodexModelResolutionError, match="unreadable"):
        provider_registry.resolve_codex_model()


# ---------------------------------------------------------------------------
# Path 1: gate_runner._build_gate_cmd
# ---------------------------------------------------------------------------

def test_gate_cmd_always_passes_the_registry_model(runner, tmp_registry):
    cmd = runner._build_gate_cmd("codex_gate", "codex", {})
    assert _model_arg(cmd) == [f'model="{TMP_MODEL}"']
    assert cmd[cmd.index("-c") + 1] == f'model="{TMP_MODEL}"'


def test_gate_cmd_env_and_payload_precedence(runner, tmp_registry, monkeypatch):
    payload = {"model": "from-payload"}
    assert _model_arg(runner._build_gate_cmd("codex_gate", "codex", payload)) == ['model="from-payload"']
    monkeypatch.setenv("VNX_CODEX_MODEL", "from-codex-model")
    assert _model_arg(runner._build_gate_cmd("codex_gate", "codex", payload)) == ['model="from-codex-model"']
    monkeypatch.setenv("VNX_CODEX_HEADLESS_MODEL", "from-headless")
    assert _model_arg(runner._build_gate_cmd("codex_gate", "codex", payload)) == ['model="from-headless"']


def test_gate_cmd_fails_closed_without_registry_default(runner, tmp_path, monkeypatch):
    path = _write_registry(tmp_path / "registry.yaml", default_model=None)
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", path)
    for name in ("VNX_CODEX_HEADLESS_MODEL", "VNX_CODEX_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(provider_registry.CodexModelResolutionError):
        runner._build_gate_cmd("codex_gate", "codex", {})


# ---------------------------------------------------------------------------
# Path 1, OI-2288: the review seat's reasoning effort follows the risk class.
# Without it codex took model_reasoning_effort from ~/.codex/config.toml (xhigh).
# ---------------------------------------------------------------------------

EFFORT_ENV = "VNX_CODEX_REVIEW_EFFORT"
CODEX_VERDICT_STREAM = (
    VNX_ROOT / "tests" / "fixtures" / "gate_verdict" / "pr-1869-codex_gate-bare-verdict.ndjson"
).read_bytes()


@pytest.fixture
def no_effort_env(monkeypatch):
    monkeypatch.delenv(EFFORT_ENV, raising=False)


def _effort_arg(cmd):
    return [a for a in cmd if a.startswith("model_reasoning_effort=")]


@pytest.mark.parametrize(
    "payload, effort",
    [
        ({"risk_class": "low"}, "medium"),
        ({"risk_class": "medium"}, "medium"),
        ({}, "medium"),
        ({"risk_class": None}, "medium"),
        ({"risk_class": "high"}, "high"),
        ({"risk_class": "critical"}, "high"),
    ],
)
def test_gate_cmd_effort_follows_the_risk_class(runner, tmp_registry, no_effort_env, payload, effort):
    cmd = runner._build_gate_cmd("codex_gate", "codex", dict(payload))
    assert _effort_arg(cmd) == [f'model_reasoning_effort="{effort}"']
    assert cmd[cmd.index(f'model_reasoning_effort="{effort}"') - 1] == "-c"
    assert cmd[cmd.index("-c") + 1] == f'model="{TMP_MODEL}"', "the model stays the first -c"


@pytest.mark.parametrize("value", ["low", "medium", "high", "xhigh", "max"])
def test_gate_cmd_effort_env_wins_over_the_risk_class(runner, tmp_registry, monkeypatch, value):
    monkeypatch.setenv(EFFORT_ENV, value)
    for risk in ("low", "critical"):
        cmd = runner._build_gate_cmd("codex_gate", "codex", {"risk_class": risk})
        assert _effort_arg(cmd) == [f'model_reasoning_effort="{value}"']


def test_gate_cmd_empty_effort_env_means_per_risk_class(runner, tmp_registry, monkeypatch):
    monkeypatch.setenv(EFFORT_ENV, "")
    cmd = runner._build_gate_cmd("codex_gate", "codex", {"risk_class": "high"})
    assert _effort_arg(cmd) == ['model_reasoning_effort="high"']


@pytest.mark.parametrize("bad", ["extreme", "HIGH", "minimal", "5", "high,low"])
def test_gate_cmd_refuses_an_invalid_effort(runner, tmp_registry, monkeypatch, bad):
    monkeypatch.setenv(EFFORT_ENV, bad)
    with pytest.raises(gate_runner.CodexReviewEffortError) as raised:
        runner._build_gate_cmd("codex_gate", "codex", {"risk_class": "medium"})
    assert EFFORT_ENV in str(raised.value)
    assert repr(bad) in str(raised.value)


def test_other_gates_carry_no_reasoning_effort(runner, monkeypatch):
    monkeypatch.setenv(EFFORT_ENV, "high")
    assert _effort_arg(runner._build_gate_cmd("gemini_review", "gemini", {"risk_class": "high"})) == []


def test_codex_review_effort_invalid_is_an_execution_failure_reason():
    assert "codex_review_effort_invalid" in EXECUTION_FAILURE_REASONS


def test_effort_variable_is_registered_env_only_with_an_empty_default():
    import config_registry

    entry = config_registry.CONFIG_REGISTRY[EFFORT_ENV]
    assert (entry.type, entry.default, entry.category) == ("string", "", "gate")
    assert entry.writable_from_ui is False, "the runner reads the environment, not project_config"
    assert "OI-2288" in entry.description
    assert gate_runner.CODEX_REVIEW_EFFORT_ENV == EFFORT_ENV


def test_gate_run_books_an_invalid_effort_and_launches_nothing(runner, tmp_registry, tmp_path, monkeypatch):
    monkeypatch.setenv(EFFORT_ENV, "extreme")
    worktree = tmp_path / "gate-wt"
    worktree.mkdir()
    payload = {"prompt": "review", "branch": "dispatch/x", "pr_number": 7, "risk_class": "high"}
    with patch("gate_runner.shutil.which", return_value="/usr/bin/codex"), \
         patch("gate_runner.create_gate_worktree", return_value=worktree), \
         patch("gate_runner.remove_gate_worktree"), \
         patch("gate_runner.subprocess.Popen") as popen:
        result = runner.run(gate="codex_gate", request_payload=payload, pr_number=7, pr_id="pr-7")
    popen.assert_not_called()
    results = _read_gate_files(runner, "results")
    assert len(results) == 1
    assert (results[0]["status"], results[0]["reason"]) == ("unavailable", "codex_review_effort_invalid")
    assert EFFORT_ENV in results[0]["reason_detail"]
    assert "'extreme'" in results[0]["reason_detail"]
    assert "reasoning_effort" not in results[0], "codex never launched, so no effort was used"
    assert result["reason"] == "codex_review_effort_invalid"
    requests = _read_gate_files(runner, "requests")
    assert requests and all(r["status"] != "executing" for r in requests)


def _run_codex_to_a_verdict(runner, tmp_path, payload, stream=CODEX_VERDICT_STREAM):
    proc = MagicMock()
    proc.stdout.fileno.return_value = 10
    proc.stderr.fileno.return_value = 11
    proc.poll.return_value = 0
    proc.returncode = 0
    proc.pid = 4242
    reads = iter([stream])

    def fake_read(fd, _size):
        return next(reads, b"") if fd == 10 else b""

    with patch("gate_runner.shutil.which", return_value="/usr/bin/codex"), \
         patch("gate_runner.create_gate_worktree", return_value=tmp_path), \
         patch("gate_runner.remove_gate_worktree"), \
         patch("gate_runner.subprocess.Popen", return_value=proc) as popen, \
         patch("gate_runner.select.select", return_value=([], [], [])), \
         patch("gate_runner.os.read", side_effect=fake_read), \
         patch("gate_runner.os.getpgid", return_value=4242):
        result = runner.run(gate="codex_gate", request_payload=payload, pr_number=7)
    return result, popen.call_args[0][0]


@pytest.mark.parametrize("risk, effort", [("high", "high"), ("low", "medium")])
def test_result_payload_carries_reasoning_effort_next_to_the_model(
    runner, tmp_registry, tmp_path, no_effort_env, risk, effort,
):
    payload = {
        "prompt": "review", "branch": "dispatch/x", "pr_number": 7, "risk_class": risk,
        "report_path": str(tmp_path / "reports" / f"codex-{risk}.md"),
    }
    result, cmd = _run_codex_to_a_verdict(runner, tmp_path, payload)

    assert result["status"] == "completed", result
    assert f'model_reasoning_effort="{effort}"' in cmd
    record = _read_gate_files(runner, "results")[0]
    assert record["reasoning_effort"] == effort
    assert record["model"] == TMP_MODEL
    assert result["reasoning_effort"] == effort


def test_a_previous_runs_launch_record_is_dropped_before_the_next_run(runner, tmp_registry, tmp_path, no_effort_env):
    """A re-executed request must not carry the last run's model and effort
    into a run that never launched codex."""
    payload = {
        "prompt": "review", "branch": "dispatch/x", "pr_number": 7, "risk_class": "high",
        "codex_launch": {"model": "stale-model", "reasoning_effort": "xhigh"},
    }
    with patch("gate_runner.shutil.which", return_value="/usr/bin/codex"), \
         patch("gate_runner.create_gate_worktree", side_effect=gate_runner.GateWorktreeError("no checkout")), \
         patch("gate_runner.subprocess.Popen") as popen:
        runner.run(gate="codex_gate", request_payload=payload, pr_number=7)
    popen.assert_not_called()
    requests = _read_gate_files(runner, "requests")
    assert requests and all("codex_launch" not in r for r in requests)
    results = _read_gate_files(runner, "results")
    assert results and all("model" not in r and "reasoning_effort" not in r for r in results)


# ---------------------------------------------------------------------------
# OI-2288: a failed codex run carries its model and effort too. A run that
# timed out, stalled or was refused as degenerate is booked by
# gate_recorder.record_failure; without them an effort comparison would count
# only the runs that got through.
# ---------------------------------------------------------------------------

# A verdict without a single investigative action (OI-1485): booked
# gate_execution_degenerate, never completed.
DEGENERATE_CODEX_STREAM = ("\n".join([
    json.dumps({"type": "thread.started", "thread_id": "01a0-dead-beef"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed", "item": {
        "id": "item_0", "type": "agent_message",
        "text": json.dumps({"verdict": "pass", "findings": [], "residual_risk": None}),
    }}),
    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 18219, "output_tokens": 602}}),
]) + "\n").encode("utf-8")


def _run_codex_that_never_answers(runner, tmp_path, payload):
    """A codex process that writes nothing and never exits, on a clock that
    moves one second per reading, so the runner's own timeout or stall check
    ends it."""
    proc = MagicMock()
    proc.stdout.fileno.return_value = 10
    proc.stderr.fileno.return_value = 11
    proc.poll.return_value = None
    proc.pid = 4242
    ticks = itertools.count()
    start = time.monotonic()
    with patch("gate_runner.shutil.which", return_value="/usr/bin/codex"), \
         patch("gate_runner.create_gate_worktree", return_value=tmp_path), \
         patch("gate_runner.remove_gate_worktree"), \
         patch("gate_runner.subprocess.Popen", return_value=proc), \
         patch("gate_runner.select.select", return_value=([], [], [])), \
         patch("gate_runner.time.monotonic", side_effect=lambda: start + next(ticks)), \
         patch("gate_runner.os.getpgid", return_value=4242), \
         patch("gate_runner.os.killpg"):
        return runner.run(gate="codex_gate", request_payload=payload, pr_number=7)


@pytest.mark.parametrize(
    "reason, timeout, stall",
    [("timeout", "1", "300"), ("stall", "300", "2")],
)
@pytest.mark.parametrize("risk, effort", [("critical", "high"), ("low", "medium")])
def test_a_codex_run_that_timed_out_or_stalled_records_its_model_and_effort(
    runner, tmp_registry, tmp_path, no_effort_env, monkeypatch, reason, timeout, stall, risk, effort,
):
    monkeypatch.setenv("VNX_CODEX_GATE_TIMEOUT", timeout)
    monkeypatch.setenv("VNX_CODEX_STALL_THRESHOLD", stall)
    payload = {"prompt": "review", "branch": "dispatch/x", "pr_number": 7, "risk_class": risk}

    result = _run_codex_that_never_answers(runner, tmp_path, payload)

    assert (result["status"], result["reason"]) == ("unavailable", reason)
    record = _read_gate_files(runner, "results")[0]
    assert record["reason"] == reason
    assert record["model"] == TMP_MODEL
    assert record["reasoning_effort"] == effort


@pytest.mark.parametrize("risk, effort", [("high", "high"), ("medium", "medium")])
def test_a_degenerate_codex_run_records_its_model_and_effort(
    runner, tmp_registry, tmp_path, no_effort_env, risk, effort,
):
    payload = {
        "prompt": "review", "branch": "dispatch/x", "pr_number": 7, "risk_class": risk,
        "report_path": str(tmp_path / "reports" / f"codex-degenerate-{risk}.md"),
    }
    result, cmd = _run_codex_to_a_verdict(runner, tmp_path, payload, stream=DEGENERATE_CODEX_STREAM)

    assert (result["status"], result["reason"]) == ("unavailable", "gate_execution_degenerate"), result
    assert f'model_reasoning_effort="{effort}"' in cmd
    record = _read_gate_files(runner, "results")[0]
    assert record["reason"] == "gate_execution_degenerate"
    assert record["model"] == TMP_MODEL
    assert record["reasoning_effort"] == effort


def test_an_effort_env_set_for_the_run_lands_on_its_failure_record(runner, tmp_registry, tmp_path, monkeypatch):
    monkeypatch.setenv(EFFORT_ENV, "xhigh")
    monkeypatch.setenv("VNX_CODEX_GATE_TIMEOUT", "1")
    monkeypatch.setenv("VNX_CODEX_STALL_THRESHOLD", "300")
    payload = {"prompt": "review", "branch": "dispatch/x", "pr_number": 7, "risk_class": "low"}

    _run_codex_that_never_answers(runner, tmp_path, payload)

    record = _read_gate_files(runner, "results")[0]
    assert (record["reason"], record["reasoning_effort"]) == ("timeout", "xhigh")


@pytest.mark.parametrize("launch", [None, "tmp-registry-model/high", ["tmp-registry-model", "high"]])
def test_a_failure_record_without_a_launch_record_carries_no_settings(runner, launch):
    """No ``codex_launch``, or one that is not a dict: the record names no
    model and no effort rather than one it cannot know."""
    from gate_recorder import record_failure

    payload = {"branch": "dispatch/x", "pr_number": 7}
    if launch is not None:
        payload["codex_launch"] = launch
    record = record_failure(
        gate="codex_gate", pr_number=7, pr_id="",
        result={"reason": "timeout", "reason_detail": "timed out", "duration_seconds": 1.0,
                "partial_output_lines": 0, "runner_pid": 1},
        request_payload=payload,
        requests_dir=runner._state_dir / "review_gates" / "requests",
        results_dir=runner._state_dir / "review_gates" / "results",
    )
    assert record["status"] == "unavailable"
    assert "model" not in record
    assert "reasoning_effort" not in record


# ---------------------------------------------------------------------------
# Path 2: codex_wrapper
# ---------------------------------------------------------------------------

def _wrapper_cmd(**kwargs):
    done = MagicMock(returncode=0, stdout="", stderr="")
    with patch("codex_wrapper.subprocess.run", return_value=done) as run, \
         patch("provider_costs.emit_provider_cost"):
        codex_wrapper.codex_exec("hello", **kwargs)
    return run.call_args[0][0]


def test_codex_wrapper_reads_the_registry(tmp_registry):
    cmd = _wrapper_cmd()
    assert cmd == ["codex", "exec", "--json", "--model", TMP_MODEL]


def test_codex_wrapper_explicit_model_wins(tmp_registry):
    assert _wrapper_cmd(model="explicit")[-1] == "explicit"


def test_codex_wrapper_has_no_default_constant():
    assert not hasattr(codex_wrapper, "DEFAULT_CODEX_MODEL")


# ---------------------------------------------------------------------------
# Path 3: codex_spawn
# ---------------------------------------------------------------------------

def test_codex_spawn_reads_the_registry(tmp_registry):
    assert codex_spawn._build_cmd("")[-2:] == ["--model", TMP_MODEL]
    assert codex_spawn._build_cmd(None)[-1] == TMP_MODEL


def test_codex_spawn_env_default_beats_registry_and_explicit_beats_env(tmp_registry, monkeypatch):
    monkeypatch.setenv("VNX_CODEX_DEFAULT_MODEL", "from-env")
    assert codex_spawn._build_cmd("")[-1] == "from-env"
    assert codex_spawn._build_cmd("explicit")[-1] == "explicit"


def test_codex_spawn_fails_closed_without_registry_default(tmp_path, monkeypatch):
    path = _write_registry(tmp_path / "registry.yaml", default_model=None)
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", path)
    monkeypatch.delenv("VNX_CODEX_DEFAULT_MODEL", raising=False)
    with pytest.raises(provider_registry.CodexModelResolutionError):
        codex_spawn._build_cmd("")


def test_codex_spawn_and_lane_reexport_carry_no_default_constant():
    from providers.provider_lanes import codex as lane

    assert not hasattr(codex_spawn, "DEFAULT_CODEX_MODEL")
    assert not hasattr(lane, "DEFAULT_CODEX_MODEL")
    assert "DEFAULT_CODEX_MODEL" not in lane.__all__


# ---------------------------------------------------------------------------
# Paths 4 and 5: the panels resolve when they are built
# ---------------------------------------------------------------------------

def test_default_panel_codex_seat_reads_the_registry(tmp_registry):
    seats = {s["label"]: s for s in pgp.default_panel()}
    assert seats["codex"] == {"label": "codex", "provider": "codex", "model_arg": TMP_MODEL}
    assert seats["opus"]["model_arg"] == "opus"


def test_shipped_panel_config_codex_seat_reads_the_registry(tmp_registry):
    seats = {s["label"]: s for s in pgp.load_panel_seats()}
    assert seats["codex"]["model_arg"] == TMP_MODEL


def test_panel_config_seat_naming_its_own_codex_model_keeps_it(tmp_registry, tmp_path):
    config = tmp_path / "panel.yaml"
    config.write_text(
        "seats:\n  - label: codex\n    provider: codex\n    model_arg: pinned-by-operator\n",
        encoding="utf-8",
    )
    assert pgp.load_panel_seats(config)[0]["model_arg"] == "pinned-by-operator"


def test_default_roster_codex_entry_reads_the_registry(tmp_registry):
    roster = dict(deliberation_panel.default_roster())
    assert roster["codex"] == TMP_MODEL
    assert roster["claude"] == "sonnet"


def test_panels_resolve_at_build_time_not_import_time(tmp_registry):
    first = dict(deliberation_panel.default_roster())["codex"]
    other = _write_registry(tmp_registry.parent / "other.yaml", default_model="second", models=("second",))
    with patch.object(provider_registry, "_REGISTRY_PATH", other):
        assert dict(deliberation_panel.default_roster())["codex"] == "second"
        assert {s["label"]: s for s in pgp.default_panel()}["codex"]["model_arg"] == "second"
    assert first == TMP_MODEL


# ---------------------------------------------------------------------------
# Guard: no launch module carries a model name of its own
# ---------------------------------------------------------------------------

_LAUNCH_MODULES = (
    "scripts/gate_runner.py",
    "scripts/lib/codex_wrapper.py",
    "scripts/lib/provider_spawns/codex_spawn.py",
    "scripts/lib/plan_gate_panel.py",
    "scripts/lib/deliberation_panel.py",
    "scripts/lib/provider_dispatch.py",
)
_GPT_LITERAL = re.compile(r"^gpt-[0-9]")
# Bookkeeping, not a launch model: the openrouter lane-key alias for the
# arbitrary-OpenRouter litellm lane, which dispatches gpt-4o-mini and never codex.
_NOT_A_CODEX_MODEL = {
    "scripts/lib/provider_dispatch.py": {"gpt-4o-mini-default"},
}


@pytest.mark.parametrize("module", _LAUNCH_MODULES)
def test_launch_module_carries_no_gpt_model_literal(module):
    tree = ast.parse((VNX_ROOT / module).read_text(encoding="utf-8"))
    found = sorted(
        {
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and _GPT_LITERAL.match(n.value.strip())
        } - _NOT_A_CODEX_MODEL.get(module, set())
    )
    assert "gpt-5.5" not in found
    assert found == [], f"{module} hard-codes a codex model: {found}"


# ---------------------------------------------------------------------------
# A refused model is named in the gate result
# ---------------------------------------------------------------------------

def test_find_refusal_matches_both_measured_sentences_and_ignores_the_cache_noise():
    assert find_codex_model_refusal(f"x {REFUSAL_CHATGPT} y") == REFUSAL_CHATGPT
    assert find_codex_model_refusal("", REFUSAL_OLD_CLI) == REFUSAL_OLD_CLI
    noise = "failed to load models cache: missing field base_instructions"
    assert find_codex_model_refusal(noise) is None
    assert find_codex_model_refusal(None, "") is None


def test_model_not_supported_is_an_execution_failure_reason():
    assert "model_not_supported" in EXECUTION_FAILURE_REASONS


def _run_refused_codex_gate(runner, stdout=b"", stderr=b"", binary_path="/opt/old/bin/codex"):
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout.fileno.return_value = 10
    mock_proc.stderr.fileno.return_value = 11
    mock_proc.poll.return_value = 1
    mock_proc.returncode = 1
    mock_proc.pid = 4242
    reads = {10: stdout, 11: stderr}

    def fake_read(fd, size):
        data, reads[fd] = reads.get(fd, b""), b""
        return data

    with patch("gate_runner.subprocess.Popen", return_value=mock_proc), \
         patch("gate_runner.select.select", return_value=([10, 11], [], [])), \
         patch("gate_runner.os.read", side_effect=fake_read), \
         patch("gate_runner.os.getpgid", return_value=4242), \
         patch("gate_runner.shutil.which", return_value=binary_path):
        return runner._run_with_stall_detection(
            gate="codex_gate", binary="codex", prompt="review", timeout=5,
            stall_threshold=5, request_payload={"model": "gpt-5.5"},
        )


@pytest.mark.parametrize("sentence,requested", [(REFUSAL_CHATGPT, "gpt-5.5"), (REFUSAL_OLD_CLI, "gpt-5.5")])
def test_refusal_on_stderr_books_model_not_supported(runner, tmp_registry, sentence, requested):
    noise = b"failed to load models cache: missing field base_instructions\n"
    result = _run_refused_codex_gate(runner, stderr=noise + f"ERROR: {sentence}\n".encode())
    assert result["status"] == "failed"
    assert result["reason"] == "model_not_supported"
    detail = result["reason_detail"]
    assert sentence in detail
    assert f"requested model: {requested}" in detail
    assert "codex binary: /opt/old/bin/codex" in detail
    assert "base_instructions" not in detail


def test_refusal_in_the_json_event_stream_is_named(runner, tmp_registry):
    event = json.dumps({"type": "error", "message": REFUSAL_OLD_CLI}) + "\n"
    result = _run_refused_codex_gate(runner, stdout=event.encode())
    assert result["reason"] == "model_not_supported"
    assert REFUSAL_OLD_CLI in result["reason_detail"]


def test_refusal_sentence_is_capped_at_500_characters():
    long_stderr = "The 'x' model requires a newer version of Codex. " + "Please upgrade " * 100
    sentence = find_codex_model_refusal(long_stderr)
    assert sentence is not None and len(sentence) <= 500


def test_a_plain_nonzero_exit_stays_exit_nonzero(runner, tmp_registry):
    result = _run_refused_codex_gate(runner, stderr=b"something else broke\n")
    assert result["reason"] == "exit_nonzero"


@pytest.mark.parametrize("sentence", [REFUSAL_CHATGPT, REFUSAL_OLD_CLI])
def test_refusal_record_is_not_read_as_lane_exhausted(runner, tmp_registry, sentence):
    result = _run_refused_codex_gate(runner, stderr=sentence.encode())
    record = {
        "status": "unavailable",
        "reason": result["reason"],
        "reason_detail": result["reason_detail"],
    }
    state = GateRequestHandlerMixin._classify_review_seat_failure(None, record)
    assert state != "lane_exhausted"
    assert state != QUOTA_REFUSAL_REASON


# ---------------------------------------------------------------------------
# The build lane names the refused model in the report's failure_reason
# ---------------------------------------------------------------------------

class _InlineThread:
    """Runs the stderr drain synchronously so the tail is complete when it is read."""

    def __init__(self, target, args=(), **_):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _spawn_refused(sentence: str):
    from failure_classification import classify_failure

    with patch("threading.Thread", _InlineThread), patch("provider_spawns.codex_spawn.subprocess.Popen") as popen:
        proc = MagicMock()
        proc.pid = 99
        proc.returncode = 1
        proc.wait = MagicMock(return_value=1)
        proc.poll = MagicMock(return_value=1)
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.readline.side_effect = [
            b"failed to load models cache: missing field base_instructions\n",
            f"ERROR: {sentence}\n".encode(),
            b"",
        ]
        popen.return_value = proc
        with patch("provider_spawns.codex_spawn._NormalizerHost.drain_stream", return_value=iter([])):
            result = codex_spawn.spawn_codex(
                prompt="x", model="gpt-5.5", dispatch_id="d-refused", terminal_id="T1",
            )
    return result, classify_failure(status="failed", error=result.error, provider="codex", returncode=1)


@pytest.mark.parametrize("sentence", [REFUSAL_CHATGPT, REFUSAL_OLD_CLI])
def test_build_lane_failure_reason_names_the_refused_model(tmp_registry, sentence):
    result, classified = _spawn_refused(sentence)
    assert result.error.startswith("model_not_supported: ")
    assert sentence in result.error
    assert "requested model: gpt-5.5" in result.error
    assert sentence in classified["failure_reason"]
    assert "base_instructions" not in classified["failure_reason"]


# ---------------------------------------------------------------------------
# Fix round 2 (OI-2062): an unresolvable model ends as a recorded result
# ---------------------------------------------------------------------------

def _unresolvable_registry(kind: str, tmp_path, monkeypatch) -> Path:
    if kind == "no_default":
        path = _write_registry(tmp_path / "registry.yaml", default_model=None)
    else:
        path = tmp_path / "registry-dir"
        path.mkdir()
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", path)
    for name in ("VNX_CODEX_HEADLESS_MODEL", "VNX_CODEX_MODEL", "VNX_CODEX_DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return path


def _read_gate_files(runner, sub):
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((runner._state_dir / "review_gates" / sub).glob("*.json"))]


def test_codex_model_unresolved_is_an_execution_failure_reason():
    assert "codex_model_unresolved" in EXECUTION_FAILURE_REASONS


@pytest.mark.parametrize("kind", ["no_default", "unreadable"])
def test_gate_run_records_unresolved_model_and_leaves_no_executing_request(runner, tmp_path, monkeypatch, kind):
    registry_path = _unresolvable_registry(kind, tmp_path, monkeypatch)
    worktree = tmp_path / "gate-wt"
    worktree.mkdir()
    payload = {"prompt": "review", "branch": "dispatch/x", "pr_number": 7}
    with patch("gate_runner.shutil.which", return_value="/usr/bin/codex"), \
         patch("gate_runner.create_gate_worktree", return_value=worktree), \
         patch("gate_runner.remove_gate_worktree"), \
         patch("gate_runner.subprocess.Popen") as popen:
        result = runner.run(gate="codex_gate", request_payload=payload, pr_number=7, pr_id="pr-7")
    popen.assert_not_called()
    results = _read_gate_files(runner, "results")
    assert len(results) == 1
    assert results[0]["reason"] == "codex_model_unresolved"
    assert results[0]["status"] == "unavailable"
    assert str(registry_path) in results[0]["reason_detail"]
    assert result["reason"] == "codex_model_unresolved"
    requests = _read_gate_files(runner, "requests")
    assert requests and all(r["status"] != "executing" for r in requests)


def test_codex_spawn_unresolved_model_returns_a_named_failure(tmp_path, monkeypatch):
    path = _unresolvable_registry("no_default", tmp_path, monkeypatch)
    with patch("provider_spawns.codex_spawn.subprocess.Popen") as popen:
        proc, early = codex_spawn._launch_codex_proc("prompt", None, None, None)
    popen.assert_not_called()
    assert proc is None
    assert early.returncode == 1
    assert early.error.startswith("codex_model_unresolved: ")
    assert str(path) in early.error


def test_plan_gate_seat_abstains_with_a_named_status_when_model_unresolved(tmp_path, monkeypatch):
    _unresolvable_registry("no_default", tmp_path, monkeypatch)
    seats = {s["label"]: s for s in pgp.default_panel()}
    assert seats["opus"]["model_arg"] == "opus"
    calls = []

    def dispatcher(provider, model, instruction, dispatch_id):
        calls.append(provider)
        return "x"

    result = pgp._dispatch_one(dispatcher, seats["codex"], "plan", "seat-codex")
    assert result.seat_status == pgp.SEAT_MODEL_UNRESOLVED
    assert pgp.SEAT_MODEL_UNRESOLVED in pgp.SEAT_STATUSES
    assert pgp.SEAT_MODEL_UNRESOLVED not in pgp.SCORING_SEAT_STATUSES
    assert result.dispatched is False
    assert "openai.default_model" in result.error
    assert calls == []


def test_deliberation_codex_seat_fails_alone_when_model_unresolved(tmp_path, monkeypatch):
    _unresolvable_registry("no_default", tmp_path, monkeypatch)
    roster = deliberation_panel.default_roster()
    assert dict(roster)["claude"] == "sonnet"
    seen = []

    def dispatcher(provider, model, prompt, dispatch_id):
        seen.append((provider, model))
        return f"answer from {provider}"

    result = deliberation_panel.run_deliberation(
        "sweep", "a question", roster=roster, dispatcher=dispatcher,
        receipts_path=tmp_path / "none.ndjson",
    )
    assert "codex" not in [p for p, _ in seen]
    assert any(p == "claude" for p, _ in seen)
    assert any(s["provider"] == "codex" for s in result.failed_seats)


# ---------------------------------------------------------------------------
# Sixth path: provider_dispatch and its envelope adapter resolve through the resolver
# ---------------------------------------------------------------------------

import argparse  # noqa: E402
import types  # noqa: E402

import provider_dispatch  # noqa: E402
from dispatch_spec import Provider  # noqa: E402
from envelope_adapters_provider import ProviderAdapter  # noqa: E402

FIRST_MODEL = "gpt-5.5"


@pytest.fixture
def first_model_registry(tmp_path, monkeypatch):
    """openai lists gpt-5.5 FIRST while default_model names another model."""
    path = _write_registry(
        tmp_path / "first.yaml", default_model=TMP_MODEL, models=(FIRST_MODEL, TMP_MODEL),
    )
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", path)
    for name in ("VNX_CODEX_MODEL", "VNX_CODEX_DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def unreadable_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", tmp_path / "missing.yaml")
    monkeypatch.delenv("VNX_CODEX_MODEL", raising=False)


def _args():
    return provider_dispatch._build_parser().parse_args(
        ["--provider", "codex", "--terminal-id", "T1", "--dispatch-id", "r3-codex",
         "--instruction", "noop", "--model", "sonnet"]
    )


def _spawn_result():
    return MagicMock(
        returncode=0, error=None, timed_out=False, event_writer_failures=0,
    )


def _run_dispatch_codex(args):
    """Run the legacy codex lane with everything but model resolution stubbed."""
    spawn = MagicMock(return_value=_spawn_result())
    with patch("provider_dispatch._emit_governance"), \
         patch("provider_dispatch._enrich_instruction", return_value="noop"), \
         patch("provider_dispatch._prepare_provider_workdir", return_value=(None, None)), \
         patch("provider_dispatch._finish_provider_worktree"), \
         patch("provider_dispatch._event_store_safety_net"), \
         patch("event_store.EventStore", return_value=MagicMock()), \
         patch("provider_spawns.codex_spawn.spawn_codex", spawn):
        code = provider_dispatch._dispatch_codex(args)
    return code, spawn


def _run_dispatch_via_envelope(args):
    run = MagicMock(return_value=MagicMock(returncode=0))
    with patch("dispatch_envelope.run_envelope", run):
        code = provider_dispatch._dispatch_codex_via_envelope(args)
    return code, run


def _adapter_plan(model="default"):
    return types.SimpleNamespace(
        provider=Provider.CODEX, model=model, role=None, dispatch_id="r3-codex",
        target_id="T1", dispatch_paths=(),
    )


def _run_adapter(plan):
    spawn = MagicMock(return_value=_spawn_result())
    with patch("provider_spawns.codex_spawn.spawn_codex", spawn):
        result = ProviderAdapter().run(plan, "noop")
    return result, spawn


def test_all_callers_resolve_to_registry_default_not_the_first_model(first_model_registry):
    assert provider_dispatch._constraint_model_for_provider(_args(), "codex") == TMP_MODEL

    code, spawn = _run_dispatch_codex(_args())
    assert code == 0
    assert spawn.call_args.kwargs["model"] == TMP_MODEL

    code, run = _run_dispatch_via_envelope(_args())
    assert code == 0
    assert run.call_args.args[0].model == TMP_MODEL

    _, spawn = _run_adapter(_adapter_plan())
    assert spawn.call_args.kwargs["model"] == TMP_MODEL


def test_vnx_codex_model_env_still_wins_in_every_caller(first_model_registry, monkeypatch):
    monkeypatch.setenv("VNX_CODEX_MODEL", "from-env")
    assert provider_dispatch._constraint_model_for_provider(_args(), "codex") == "from-env"
    _, spawn = _run_dispatch_codex(_args())
    assert spawn.call_args.kwargs["model"] == "from-env"
    _, run = _run_dispatch_via_envelope(_args())
    assert run.call_args.args[0].model == "from-env"
    _, spawn = _run_adapter(_adapter_plan())
    assert spawn.call_args.kwargs["model"] == "from-env"


def test_adapter_explicit_plan_model_beats_env_and_registry(first_model_registry, monkeypatch):
    monkeypatch.setenv("VNX_CODEX_MODEL", "from-env")
    _, spawn = _run_adapter(_adapter_plan(model="from-plan"))
    assert spawn.call_args.kwargs["model"] == "from-plan"


def test_unresolved_model_ends_the_codex_lane_as_a_named_failure(unreadable_registry):
    recorded = []
    with patch("provider_dispatch._emit_refusal_receipt",
               side_effect=lambda *a, **k: recorded.append((a, k))):
        code, spawn = _run_dispatch_codex(_args())
    assert code == 1
    spawn.assert_not_called()
    assert len(recorded) == 1
    (_, provider, model, reason), kwargs = recorded[0]
    assert provider == "codex"
    assert reason.startswith("codex_model_unresolved: ")
    assert "gpt-5.2-codex" not in reason
    assert kwargs["status"] == "failure"


def test_unresolved_model_ends_the_envelope_lane_as_a_named_failure(unreadable_registry):
    recorded = []
    with patch("provider_dispatch._emit_refusal_receipt",
               side_effect=lambda *a, **k: recorded.append(a)):
        code, run = _run_dispatch_via_envelope(_args())
    assert code == 1
    run.assert_not_called()
    assert recorded[0][3].startswith("codex_model_unresolved: ")


def test_unresolved_model_is_a_failed_adapter_result_not_an_exception(unreadable_registry):
    result, spawn = _run_adapter(_adapter_plan())
    spawn.assert_not_called()
    assert result.returncode == 1
    assert result.status == "failure"
    assert result.error.startswith("codex_model_unresolved: ")


def test_constraint_preflight_defers_an_unresolved_model_to_the_lane(unreadable_registry):
    assert provider_dispatch._constraint_model_for_provider(_args(), "codex") == ""


def test_failure_receipt_write_error_does_not_leave_the_lane(unreadable_registry):
    with patch("provider_dispatch._emit_refusal_receipt", side_effect=OSError("disk full")):
        code, spawn = _run_dispatch_codex(_args())
    assert code == 1
    spawn.assert_not_called()


def test_provider_dispatch_has_no_hardcoded_codex_fallback():
    source = (VNX_ROOT / "scripts" / "lib" / "provider_dispatch.py").read_text(encoding="utf-8")
    assert "gpt-5.2-codex" not in source


# ---------------------------------------------------------------------------
# R7 (OI-2068): a malformed model row is an unresolved model, never a raise
# ---------------------------------------------------------------------------

# load() parses every model row of every provider section, so one malformed row
# anywhere in the registry stops the codex resolution. Each kind is one parse
# error _parse_model raises for a row: a missing field (KeyError) and a null
# where a number belongs (TypeError), in the openai section and in another one.
_OTHER_SECTION_NULL_MAX_TOKENS = """  kimi_cli:
    enabled: true
    api_key_env: ""
    models:
      kimi-k3:
        litellm_name: "kimi/k3"
        cost_input_per_mtok: 1.0
        cost_output_per_mtok: 2.0
        max_tokens: null
        supports_streaming: true
        supports_tool_calls: true
"""

_MALFORMED_KINDS = {
    "openai-row-without-litellm_name": "KeyError",
    "openai-row-with-null-price": "TypeError",
    "other-section-row-with-null-max_tokens": "TypeError",
}


def _malformed_registry(kind: str, tmp_path, monkeypatch) -> Path:
    path = _write_registry(tmp_path / "malformed.yaml", default_model=TMP_MODEL)
    text = path.read_text(encoding="utf-8")
    if kind == "openai-row-without-litellm_name":
        text = text.replace(f'        litellm_name: "openai/{TMP_MODEL}"\n', "")
    elif kind == "openai-row-with-null-price":
        text = text.replace("cost_input_per_mtok: 1.0", "cost_input_per_mtok: null")
    else:
        text += _OTHER_SECTION_NULL_MAX_TOKENS
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(provider_registry, "_REGISTRY_PATH", path)
    for name in ("VNX_CODEX_HEADLESS_MODEL", "VNX_CODEX_MODEL", "VNX_CODEX_DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return path


_MALFORMED = pytest.mark.parametrize("kind", sorted(_MALFORMED_KINDS))


@_MALFORMED
def test_resolver_wraps_a_malformed_model_row(tmp_path, monkeypatch, kind):
    path = _malformed_registry(kind, tmp_path, monkeypatch)
    with pytest.raises(provider_registry.CodexModelResolutionError) as caught:
        provider_registry.resolve_codex_model()
    message = str(caught.value)
    assert str(path) in message
    assert _MALFORMED_KINDS[kind] in message, "the parse error is named, not swallowed"
    assert type(caught.value.__cause__).__name__ == _MALFORMED_KINDS[kind]


@_MALFORMED
def test_gate_run_books_a_malformed_row_and_leaves_no_executing_request(runner, tmp_path, monkeypatch, kind):
    """The red run of OI-2068: GateRunner.run raised and the request stayed `executing`."""
    path = _malformed_registry(kind, tmp_path, monkeypatch)
    worktree = tmp_path / "gate-wt"
    worktree.mkdir()
    payload = {"prompt": "review", "branch": "dispatch/x", "pr_number": 7}
    raised = None
    result = None
    with patch("gate_runner.shutil.which", return_value="/usr/bin/codex"), \
         patch("gate_runner.create_gate_worktree", return_value=worktree), \
         patch("gate_runner.remove_gate_worktree"), \
         patch("gate_runner.subprocess.Popen") as popen:
        try:
            result = runner.run(gate="codex_gate", request_payload=payload, pr_number=7, pr_id="pr-7")
        except Exception as exc:  # the old shape: the run raised out of the runner
            raised = exc
    popen.assert_not_called()
    requests = _read_gate_files(runner, "requests")
    statuses = [r["status"] for r in requests]
    assert statuses and "executing" not in statuses, f"request statuses {statuses}"
    assert raised is None, f"GateRunner.run raised {raised!r}"
    results = _read_gate_files(runner, "results")
    assert len(results) == 1
    assert (results[0]["status"], results[0]["reason"]) == ("unavailable", "codex_model_unresolved")
    assert str(path) in results[0]["reason_detail"]
    assert result["reason"] == "codex_model_unresolved"


@_MALFORMED
def test_codex_spawn_books_a_malformed_row_as_unresolved(tmp_path, monkeypatch, kind):
    _malformed_registry(kind, tmp_path, monkeypatch)
    with patch("provider_spawns.codex_spawn.subprocess.Popen") as popen:
        result = codex_spawn.spawn_codex(prompt="x", model=None, dispatch_id="d-malformed", terminal_id="T1")
    popen.assert_not_called()
    assert result.returncode == 1
    assert result.error.startswith("codex_model_unresolved: ")


@_MALFORMED
def test_plan_gate_codex_seat_abstains_on_a_malformed_row(tmp_path, monkeypatch, kind):
    _malformed_registry(kind, tmp_path, monkeypatch)
    seats = {s["label"]: s for s in pgp.default_panel()}
    result = pgp._dispatch_one(lambda *_a: "x", seats["codex"], "plan", "seat-codex")
    assert result.seat_status == pgp.SEAT_MODEL_UNRESOLVED
    assert result.dispatched is False


def test_a_refusal_is_formatted_from_the_launch_model_not_a_second_resolution(tmp_registry):
    """codex_spawn resolved the model again while formatting a refusal. With
    model=None and a registry gone after the start, that raised inside the
    refusal itself; the model that was launched is already known."""
    from failure_classification import classify_failure

    def start_then_lose_the_registry(*_args, **_kwargs):
        tmp_registry.unlink()
        return proc

    with patch("threading.Thread", _InlineThread), \
         patch("provider_spawns.codex_spawn.subprocess.Popen", side_effect=start_then_lose_the_registry):
        proc = MagicMock()
        proc.pid = 99
        proc.returncode = 1
        proc.wait = MagicMock(return_value=1)
        proc.poll = MagicMock(return_value=1)
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.readline.side_effect = [f"ERROR: {REFUSAL_CHATGPT}\n".encode(), b""]
        with patch("provider_spawns.codex_spawn._NormalizerHost.drain_stream", return_value=iter([])):
            result = codex_spawn.spawn_codex(prompt="x", model=None, dispatch_id="d-refused", terminal_id="T1")

    assert not tmp_registry.exists()
    assert result.error.startswith("model_not_supported: ")
    assert REFUSAL_CHATGPT in result.error
    assert f"requested model: {TMP_MODEL}" in result.error
    classified = classify_failure(status="failed", error=result.error, provider="codex", returncode=1)
    assert REFUSAL_CHATGPT in classified["failure_reason"]
