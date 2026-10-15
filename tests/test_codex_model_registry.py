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
import json
import re
import subprocess
import sys
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
)
_GPT_LITERAL = re.compile(r"^gpt-[0-9]")


@pytest.mark.parametrize("module", _LAUNCH_MODULES)
def test_launch_module_carries_no_gpt_model_literal(module):
    tree = ast.parse((VNX_ROOT / module).read_text(encoding="utf-8"))
    found = sorted(
        {
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and _GPT_LITERAL.match(n.value.strip())
        }
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
