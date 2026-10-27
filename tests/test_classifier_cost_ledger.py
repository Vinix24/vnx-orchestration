"""tests/test_classifier_cost_ledger.py — one ledger line per classifier call (K4b-1).

Track ``kosten-per-model-in-het-grootboek`` (ADR-043): ``DeepSeekProvider`` and
``HaikuProvider`` start ``claude --print`` with a fresh ``--session-id`` and book one
``cost_ledger`` line (role ``intelligence``) from that session's transcript. A refused
write is ``cost_usd`` None with ``extra["cost_error"]``, never a made-up 0.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import cost_ledger  # noqa: E402
import harness_run_cost  # noqa: E402
import receipt_classifier as rc  # noqa: E402
from classifier_providers.base import ClassifierProvider, ClassifierResult  # noqa: E402
from classifier_providers.deepseek_provider import DeepSeekProvider  # noqa: E402
from classifier_providers.haiku_provider import HaikuProvider  # noqa: E402

PROJECT = "vnx-test"
START = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
PAYLOAD = '{"impact_class":"trivial","suggested_edit":null}'
TOKENS = {"input_uncached": 4000, "cache_hit": 10000, "cache_write_5m": 0, "cache_write_1h": 0, "output": 500}

# A real child: writes the two-message transcript of the session id it was given.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
target = os.path.join(os.environ["VNX_CLAUDE_PROJECTS_DIR"], "-fake-worktree")
os.makedirs(target, exist_ok=True)
path = os.path.join(target, args[args.index("--session-id") + 1] + ".jsonl")
with open(path, "w", encoding="utf-8") as fh:
    for mid, ts, i, o, c in [("m1", "2026-10-21T09:01:00Z", 1000, 200, 4000),
                             ("m2", "2026-10-21T09:05:00Z", 3000, 300, 6000)]:
        usage = {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": c,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
        fh.write(json.dumps({"type": "assistant", "timestamp": ts,
                             "message": {"id": mid, "role": "assistant", "usage": usage}}) + "\n")
sys.stdout.write(%r)
''' % PAYLOAD


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
           "DEEPSEEK_API_KEY": "sk-test-k4b1-not-a-real-key",
           "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    return SimpleNamespace(ledger=data / "events" / "provider_costs.ndjson")


def _records(store) -> list:
    text = store.ledger.read_text(encoding="utf-8") if store.ledger.exists() else ""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _patch_child(monkeypatch, behave):
    """Replace only the ``claude`` child; every other subprocess call stays real."""
    real_run = subprocess.run

    def run(cmd, **kwargs):
        return behave(cmd) if cmd and cmd[0] == "claude" else real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


def _raise(exc):
    def behave(_cmd):
        raise exc
    return behave


class _NoCostProvider(ClassifierProvider):
    name = "stub"

    def classify(self, prompt: str, _max_tokens: int = 1500) -> ClassifierResult:
        return ClassifierResult(raw_response=PAYLOAD, parsed_json={"impact_class": "trivial"},
                                cost_usd=None, latency_ms=1, provider=self.name,
                                extra={"cost_error": "CostLedgerWriteError: no space"})


def test_deepseek_books_one_intelligence_line_from_the_transcript(store):
    result = DeepSeekProvider().classify("classify this")
    assert result.error is None and result.parsed_json == {"impact_class": "trivial", "suggested_edit": None}
    record, = _records(store)
    assert (record["role"], record["provider"], record["billing"]) == ("intelligence", "deepseek-harness", "metered")
    assert record["tokens"] == TOKENS and record["usage_completeness"] == "complete"
    assert record["cost_usd"] > 0 and result.cost_usd == record["cost_usd"]
    assert result.extra["cost_source"] == record["cost_source"]
    assert result.extra["cost_run_id"] == record["run_id"]


def test_haiku_books_a_subscription_line(store):
    result = HaikuProvider().classify("classify this")
    assert result.error is None
    record, = _records(store)
    assert (record["role"], record["provider"], record["billing"]) == ("intelligence", "claude", "subscription")
    assert record["tokens"] == TOKENS and record["cost_usd"] == 0.0 and record["api_equivalent_usd"] > 0
    assert result.cost_usd == 0.0 and result.extra["cost_run_id"] == record["run_id"]


def test_missing_cli_books_a_spawn_refused_line(store, monkeypatch):
    _patch_child(monkeypatch, _raise(FileNotFoundError("claude")))
    result = DeepSeekProvider().classify("classify this")
    assert result.error is not None and "not found" in result.error
    record, = _records(store)
    assert record["cost_source"] == "unavailable:spawn_refused" and record["usage_completeness"] == "complete"
    assert record["tokens"] == {name: 0 for name in cost_ledger.TOKEN_FIELDS}
    assert result.cost_usd == 0.0


def test_timeout_without_a_transcript_books_unknown_never_zero(store, monkeypatch):
    _patch_child(monkeypatch, _raise(subprocess.TimeoutExpired(cmd="claude", timeout=1)))
    result = HaikuProvider(timeout_seconds=1).classify("classify this")
    assert result.error is not None and "timeout" in result.error
    record, = _records(store)
    assert record["usage_completeness"] == "unknown:no_transcript"
    assert record["cost_source"] == "unavailable:no_transcript"
    assert record["tokens"] == {name: None for name in cost_ledger.TOKEN_FIELDS}
    assert result.cost_usd == 0.0  # subscription lane: 0 at the margin, its tokens unknown


def test_ledger_refusal_returns_no_cost_and_tracks_none(store, monkeypatch, caplog):
    seen = {}

    def behave(cmd):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=PAYLOAD, stderr="")

    def refuse(*_args, **_kwargs):
        raise cost_ledger.CostLedgerWriteError(errno.ENOSPC, "no space")

    _patch_child(monkeypatch, behave)
    monkeypatch.setattr(cost_ledger, "record_next_attempt", refuse)
    with caplog.at_level(logging.WARNING):
        result = DeepSeekProvider().classify("classify this")
    session_id = seen["cmd"][seen["cmd"].index("--session-id") + 1]
    assert result.parsed_json == {"impact_class": "trivial", "suggested_edit": None}
    assert result.cost_usd is None and "CostLedgerWriteError" in result.extra["cost_error"]
    assert any(session_id in record.getMessage() and "not booked" in record.getMessage()
               for record in caplog.records), caplog.text
    assert _records(store) == []
    tracked = []
    monkeypatch.setattr(rc, "track_cost", lambda name, cost: tracked.append((name, cost)))
    outcome = rc.classify_batch([{"dispatch_id": "DISP-1", "status": "success"}], provider=_NoCostProvider())
    assert outcome["status"] == "ok" and outcome["cost_usd"] is None and tracked == []


def test_two_calls_book_two_different_run_ids(store):
    provider = DeepSeekProvider()
    first, second = provider.classify("one"), provider.classify("two")
    assert first.extra["cost_run_id"] != second.extra["cost_run_id"]
    assert len({record["run_id"] for record in _records(store)}) == 2


def test_role_must_be_a_ledger_role_and_defaults_to_builder(store):
    run = harness_run_cost.BuilderRun(
        provider="deepseek-harness", model="deepseek-v4-pro", started=START, ended=START,
        session_id=None, spawn_refused="probe",
    )
    with pytest.raises(cost_ledger.InvalidRecord):
        harness_run_cost.record_builder_run(run, project_id=PROJECT, producer="probe", dispatch_id="d-1",
                                            role="panel_leg", ledger_path=store.ledger)
    assert not store.ledger.exists()
    record = harness_run_cost.record_builder_run(
        run, project_id=PROJECT, producer="probe", dispatch_id="d-1", ledger_path=store.ledger)
    assert record["role"] == "builder"
