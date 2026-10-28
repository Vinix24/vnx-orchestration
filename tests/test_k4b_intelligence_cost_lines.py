"""tests/test_k4b_intelligence_cost_lines.py — one ledger line per intelligence call (K4b).

Track ``kosten-per-model-in-het-grootboek`` (ADR-043): the conversation analyzer's
``claude`` and ``deepseek-harness`` lanes and the dream cycle's kimi call each book
one ``cost_ledger`` line (role ``intelligence``; producers ``conversation_analyzer``
and ``dream``) from the session transcript or the CLI's own report. A refused write
never changes the analysis or the consolidation result. Ollama is local and books
nothing: the ledger knows ``metered`` and ``subscription``, and a local model is
neither.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
for _path in (REPO / "scripts", REPO / "scripts" / "lib", REPO / "scripts" / "dream"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import consolidator  # noqa: E402
import cost_ledger  # noqa: E402
import harness_run_cost  # noqa: E402
from conversation_analyzer import deep_analyzer as da_module  # noqa: E402
from conversation_analyzer.deep_analyzer import DeepAnalyzer, LLMOutcome  # noqa: E402

PROJECT = "vnx-test"
REPORTED_MODEL = "claude-sonnet-5-5-20260901"
RESULT = json.dumps({"patterns": [], "bottlenecks": [], "suggestions": []})
TOKENS = {"input_uncached": 4000, "cache_hit": 10000, "cache_write_5m": 0, "cache_write_1h": 0, "output": 500}

# A real child: writes the two-message transcript of the session id it was given and
# prints the JSON the claude CLI prints, naming the model it ran in ``modelUsage``.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
session_id = args[args.index("--session-id") + 1]
model = args[args.index("--model") + 1] if "--model" in args else %r
config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
projects = (os.path.join(config_dir, "projects") if config_dir
            else os.environ["VNX_CLAUDE_PROJECTS_DIR"])
target = os.path.join(projects, "-fake-worktree")
os.makedirs(target, exist_ok=True)
with open(os.path.join(target, session_id + ".jsonl"), "w", encoding="utf-8") as fh:
    for mid, ts, i, o, c in [("m1", "2026-10-21T09:01:00Z", 1000, 200, 4000),
                             ("m2", "2026-10-21T09:05:00Z", 3000, 300, 6000)]:
        usage = {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": c,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
        fh.write(json.dumps({"type": "assistant", "timestamp": ts,
                             "message": {"id": mid, "role": "assistant", "usage": usage}}) + "\n")
print(json.dumps({"result": %r, "session_id": session_id,
                  "modelUsage": {model: {"inputTokens": 4000, "outputTokens": 500}}}))
''' % (REPORTED_MODEL, RESULT)

# kimi-cli 1.46.0's stream carries no usage accounting (ADR-043 §8): no line may invent a zero.
KIMI_NO_USAGE = (
    '{"event_type":"TurnBegin"}\n'
    '{"event_type":"ContentPart","content":"{\\"merged\\":[],\\"dropped\\":[],'
    '\\"archived\\":[],\\"flagged\\":[],\\"summary\\":\\"ok\\"}"}\n'
    '{"event_type":"complete"}\n'
)
DREAM_PATTERNS = {"success_patterns": [{"id": 1, "title": "t", "description": "d"}],
                  "antipatterns": [], "intelligence_injections": []}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    data = tmp_path / "store"
    (data / "state").mkdir(parents=True)
    projects, bin_dir = tmp_path / "claude-projects", tmp_path / "bin"
    projects.mkdir()
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(FAKE_CLAUDE, encoding="utf-8")
    (bin_dir / "claude").chmod(0o755)
    env = {"VNX_DATA_DIR": data, "VNX_DATA_DIR_EXPLICIT": "1", "VNX_STATE_DIR": data / "state",
           "VNX_PROJECT_ID": PROJECT, "VNX_CLAUDE_PROJECTS_DIR": projects,
           "DEEPSEEK_API_KEY": "sk-test-k4b-not-a-real-key",
           "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    # This worker itself runs on a harness lane, so an inherited CLAUDE_CONFIG_DIR would
    # point the claude-lane child (and its transcript) at the real store.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return SimpleNamespace(data=data, projects=projects,
                           ledger=data / "events" / "provider_costs.ndjson")


def _records(store) -> list:
    text = store.ledger.read_text(encoding="utf-8") if store.ledger.exists() else ""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _preflight(monkeypatch, status: str) -> None:
    monkeypatch.setattr(DeepAnalyzer, "_deepseek_preflight_cache", None, raising=False)
    monkeypatch.setattr(DeepAnalyzer, "_deepseek_preflight",
                        classmethod(lambda cls, key: LLMOutcome(status)))


def test_analyzer_claude_lane_books_a_subscription_line(store):
    outcome = DeepAnalyzer._try_claude_max("analyse this")
    assert outcome.status == "ok" and json.loads(outcome.text) == json.loads(RESULT)
    record, = _records(store)
    assert (record["role"], record["provider"], record["billing"]) == ("intelligence", "claude", "subscription")
    assert record["model"] == "sonnet-5-5"  # the model the child reported, resolved to the registry key
    assert record["tokens"] == TOKENS and record["usage_completeness"] == "complete"
    assert record["cost_usd"] == 0.0 and record["api_equivalent_usd"] > 0
    assert cost_ledger.split_run_id(record["run_id"])[1] == "conversation_analyzer"


def test_analyzer_deepseek_harness_lane_books_a_metered_line(store, monkeypatch):
    _preflight(monkeypatch, "ok")
    outcome = DeepAnalyzer._try_deepseek_harness("analyse this")
    assert outcome.status == "ok"
    record, = _records(store)
    assert (record["role"], record["provider"], record["billing"]) == (
        "intelligence", "deepseek-harness", "metered")
    assert record["model"] == harness_run_cost.lane_model("deepseek-harness", da_module.DEEPSEEK_HARNESS_MODEL)
    assert record["tokens"] == TOKENS and record["usage_completeness"] == "complete"
    assert record["cost_usd"] > 0 and "api_equivalent_usd" not in record
    assert cost_ledger.split_run_id(record["run_id"])[1] == "conversation_analyzer"


def test_analyzer_missing_cli_books_a_spawn_refused_line(store, monkeypatch):
    def missing(_cmd, **_kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(da_module.subprocess, "run", missing)
    outcome = DeepAnalyzer._try_claude_max("analyse this")
    assert outcome.status == "missing_cli"
    record, = _records(store)
    assert record["cost_source"] == "unavailable:spawn_refused"
    assert record["usage_completeness"] == "complete"
    assert record["tokens"] == {name: 0 for name in cost_ledger.TOKEN_FIELDS}


def test_analyzer_failed_preflight_books_a_spawn_refused_line(store, monkeypatch):
    _preflight(monkeypatch, "key_missing")
    outcome = DeepAnalyzer._try_deepseek_harness("analyse this")
    assert outcome.status == "key_missing"
    record, = _records(store)
    assert record["provider"] == "deepseek-harness" and record["billing"] == "metered"
    assert record["cost_source"] == "unavailable:spawn_refused"
    assert record["tokens"] == {name: 0 for name in cost_ledger.TOKEN_FIELDS}


def test_analyzer_timeout_without_a_transcript_books_unknown_never_zero(store, monkeypatch):
    def timed_out(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=90)

    monkeypatch.setattr(da_module.subprocess, "run", timed_out)
    outcome = DeepAnalyzer._try_claude_max("analyse this")
    assert outcome.status == "timeout"
    record, = _records(store)
    assert record["usage_completeness"] == "unknown:no_transcript"
    assert record["cost_source"] == "unavailable:no_transcript"
    assert record["tokens"] == {name: None for name in cost_ledger.TOKEN_FIELDS}


def test_dream_kimi_call_books_one_intelligence_line(store, monkeypatch):
    import kimi_wrapper

    monkeypatch.setattr(kimi_wrapper, "kimi_exec", lambda prompt, **kwargs: KIMI_NO_USAGE)
    result = consolidator._dispatch_kimi_consolidation(DREAM_PATTERNS, PROJECT, data_root=store.data)
    assert result["summary"] == "ok"
    record, = _records(store)
    assert (record["role"], record["provider"], record["billing"]) == ("intelligence", "kimi", "subscription")
    assert record["usage_completeness"] == "unknown:kimi_cli_no_usage"
    assert record["tokens"] == {name: None for name in cost_ledger.TOKEN_FIELDS}
    assert cost_ledger.split_run_id(record["run_id"])[1] == "dream"


def test_ollama_call_books_nothing(store, monkeypatch):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps({"response": RESULT}).encode("utf-8")

    monkeypatch.setattr(DeepAnalyzer, "_probe_ollama", classmethod(lambda cls: True))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: _Response())
    assert DeepAnalyzer._try_ollama("analyse this").status == "ok"
    assert _records(store) == []


def test_ledger_refusal_leaves_the_analysis_result_unchanged(store, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise cost_ledger.CostLedgerWriteError(28, "no space")

    monkeypatch.setattr(cost_ledger, "record_next_attempt", refuse)
    outcome = DeepAnalyzer._try_claude_max("analyse this")
    assert outcome.status == "ok" and json.loads(outcome.text) == json.loads(RESULT)
    assert _records(store) == []
