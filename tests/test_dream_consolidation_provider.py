"""The dream consolidation runs on the provider VNX_DREAM_PROVIDER names (OI-2748).

Default deepseek, kimi behind the same selector, anything else fails closed. Every provider
is a fake that records its calls: no test starts a kimi, claude or DeepSeek process, and
every store lives under ``tmp_path``.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "scripts/lib", "scripts/dream"):
    sys.path.insert(0, str(REPO_ROOT / sub))

import consolidator  # noqa: E402
import classifier_providers  # noqa: E402
import kimi_wrapper  # noqa: E402
import provider_switch  # noqa: E402

PROJECT = "vnx-dev"
VALID = {
    "merged": [], "dropped": [{"id": 1, "table": "success_patterns", "reason": "stale"}],
    "archived": [], "flagged": [], "summary": "dropped one",
}
SCHEMA = """
CREATE TABLE dream_cycles (cycle_id TEXT NOT NULL, project_id TEXT NOT NULL, started_at TEXT,
    completed_at TEXT, status TEXT, provider TEXT NOT NULL DEFAULT 'kimi', insights_input INTEGER,
    merged_count INTEGER, dropped_count INTEGER, archived_count INTEGER, flagged_count INTEGER,
    operator_reviewed INTEGER DEFAULT 0, report_path TEXT, PRIMARY KEY (cycle_id, project_id));
CREATE TABLE success_patterns (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    title TEXT, description TEXT, first_seen DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE antipatterns (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    title TEXT, description TEXT, first_seen DATETIME DEFAULT CURRENT_TIMESTAMP);
"""


class FakeDeepSeek:
    """Records the calls; ``result`` is what classify returns."""

    def __init__(self, result):
        self.result = result
        self.prompts: list[str] = []
        self.max_tokens: list[int] = []
        self.timeout_seconds = None

    def classify(self, prompt, _max_tokens=1500):
        self.prompts.append(prompt)
        self.max_tokens.append(_max_tokens)
        return self.result


def ok_result(payload=None):
    return SimpleNamespace(error=None, parsed_json=payload if payload is not None else VALID,
                           raw_response="", cost_usd=0.01)


@pytest.fixture
def world(tmp_path, monkeypatch):
    boundary = tmp_path / "boundary.json"
    boundary.write_text(json.dumps({"version": 1}), encoding="utf-8")
    monkeypatch.setenv("VNX_CONTENT_BOUNDARY_FILE", str(boundary))
    monkeypatch.setenv("VNX_PROVIDER_SWITCH_FILE", str(tmp_path / "switch" / "providers.json"))
    monkeypatch.setenv("VNX_DREAM_SCHEDULER_ENABLED", "1")
    monkeypatch.delenv("VNX_DREAM_PROVIDER", raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_DREAM_PROVIDER", raising=False)
    for name in list(__import__("os").environ):
        if name.startswith("VNX_OVERRIDE_PROVIDER_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(consolidator, "_injection_probe_health", lambda state_dir: "ok")

    data_root = tmp_path / ".vnx-data"
    (data_root / "state").mkdir(parents=True)
    ledger = data_root / "state" / "t0_receipts.ndjson"
    ledger.write_text(json.dumps({"dispatch_id": "d", "status": "success",
                                  "timestamp": consolidator._utc_timestamp()}) + "\n")
    db_path = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO success_patterns (project_id, title) VALUES (?, 'seeded')", (PROJECT,))
    conn.commit()
    conn.close()
    deepseek = FakeDeepSeek(ok_result())
    kimi_calls: list[str] = []

    def fake_kimi_exec(prompt, **kwargs):
        kimi_calls.append(prompt)
        return json.dumps({"type": "ContentPart", "content": json.dumps(VALID)})

    ds_calls: list[dict] = []

    def fake_get_provider(name=None, *, project_id=None):
        ds_calls.append({"name": name, "project_id": project_id})
        return deepseek

    monkeypatch.setattr(classifier_providers, "get_provider", fake_get_provider)
    monkeypatch.setattr(kimi_wrapper, "kimi_exec", fake_kimi_exec)
    # The kimi path books a ledger line; never let it touch a real one.
    monkeypatch.setattr(consolidator, "_book_kimi_call", lambda *a, **k: None)

    def events():
        out = []
        for path in sorted((data_root / "events" / "dream").glob("*.ndjson")):
            out += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
        return out

    def run(**kwargs):
        return consolidator.run_dream_cycle(PROJECT, db_path, data_root=data_root, **kwargs)

    return SimpleNamespace(deepseek=deepseek, kimi_calls=kimi_calls, ds_calls=ds_calls, events=events,
                           run=run, tmp=tmp_path, boundary=boundary, db_path=db_path)


def test_default_runs_on_deepseek_and_writes_the_pending_review(world):
    result = world.run()
    assert len(world.deepseek.prompts) == 1 and world.kimi_calls == []
    assert world.ds_calls == [{"name": "deepseek", "project_id": PROJECT}]
    review = json.loads(Path(result["review_path"]).read_text(encoding="utf-8"))
    assert review["requires_operator_review"] is True
    assert result["dropped_count"] == 1
    assert world.deepseek.max_tokens == [consolidator._DEEPSEEK_MAX_TOKENS]
    assert world.deepseek.timeout_seconds == consolidator._DEFAULT_TIMEOUT
    completed = [e for e in world.events() if e["event_type"] == "dream_cycle_completed"]
    assert completed[0]["provider"] == "deepseek"
    conn = sqlite3.connect(str(world.db_path))
    assert conn.execute("SELECT provider FROM dream_cycles").fetchone()[0] == "deepseek"
    conn.close()


def test_kimi_selector_calls_kimi_and_never_deepseek(world, monkeypatch):
    monkeypatch.setenv("VNX_DREAM_PROVIDER", "kimi")
    result = world.run()
    assert len(world.kimi_calls) == 1 and world.deepseek.prompts == [] and world.ds_calls == []
    assert result["dropped_count"] == 1
    assert {e["provider"] for e in world.events() if e["event_type"] != "dream_cycle_skipped"} == {"kimi"}


def test_provider_switched_off_skips_and_names_deepseek(world):
    entry = {"enabled": False, "changed_at": "2026-11-12T08:00:00Z", "changed_by": "op", "reason": "t"}
    path = Path(__import__("os").environ["VNX_PROVIDER_SWITCH_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "global": {"deepseek": entry}, "projects": {}}))
    result = world.run()
    assert result["status"] == "skipped" and result["reason"] == provider_switch.REASON_SWITCHED_OFF
    assert world.deepseek.prompts == [] and world.kimi_calls == [] and world.ds_calls == []
    skipped = [e for e in world.events() if e["event_type"] == "dream_cycle_skipped"]
    assert skipped[-1]["provider"] == "deepseek" and "deepseek" in skipped[-1]["detail"].lower()


@pytest.mark.parametrize("value", ["glm", "Kimi2", "", "  "])
def test_unknown_provider_skips_fail_closed(world, monkeypatch, value):
    monkeypatch.setenv("VNX_DREAM_PROVIDER", value)
    result = world.run()
    assert result["status"] == "skipped" and result["reason"] == "dream_provider_unknown"
    assert repr(value) in result["detail"]
    assert world.deepseek.prompts == [] and world.kimi_calls == [] and world.ds_calls == []
    assert not [e for e in world.events() if e["event_type"] == "dream_cycle_started"]


def test_client_project_without_a_deepseek_exception_is_skipped_at_the_boundary(world):
    document = {"version": 1, "client_project_ids": [PROJECT],
                "provider_exceptions": {PROJECT: ["kimi"]}}
    world.boundary.write_text(json.dumps(document), encoding="utf-8")
    result = world.run()
    assert result["reason"] == "content_restricted_project" and "deepseek" in result["detail"]
    assert world.deepseek.prompts == [] and world.kimi_calls == [] and world.ds_calls == []


def test_deepseek_error_writes_dream_cycle_failed_with_provider_and_error(world):
    world.deepseek.result = SimpleNamespace(error="exit 1: boom", parsed_json=None,
                                            raw_response="", cost_usd=None)
    with pytest.raises(RuntimeError, match="exit 1: boom"):
        world.run()
    failed = [e for e in world.events() if e["event_type"] == "dream_cycle_failed"]
    assert len(failed) == 1
    assert failed[0]["provider"] == "deepseek" and "exit 1: boom" in failed[0]["error"]


def test_deepseek_json_in_raw_response_is_parsed_with_the_shared_parser(world):
    world.deepseek.result = SimpleNamespace(
        error=None, parsed_json=None, cost_usd=None,
        raw_response="Here it is:\n" + json.dumps(VALID) + "\nDone.")
    assert world.run()["dropped_count"] == 1


def test_client_project_with_a_deepseek_exception_runs(world):
    document = {"version": 1, "client_project_ids": [PROJECT],
                "provider_exceptions": {PROJECT: ["deepseek"]}}
    world.boundary.write_text(json.dumps(document), encoding="utf-8")
    world.run()
    assert len(world.deepseek.prompts) == 1


def test_deepseek_without_a_json_object_fails_the_cycle(world):
    world.deepseek.result = SimpleNamespace(error=None, parsed_json=None,
                                            raw_response="no braces", cost_usd=None)
    with pytest.raises(ValueError, match="No JSON found"):
        world.run()
    failed = [e for e in world.events() if e["event_type"] == "dream_cycle_failed"]
    assert failed[0]["provider"] == "deepseek"


def test_deepseek_timeout_reports_deepseek_timeout(world, monkeypatch):
    monkeypatch.setenv("VNX_DREAM_TIMEOUT", "42")
    world.deepseek.result = SimpleNamespace(error="timeout after 42s: x", parsed_json=None,
                                            raw_response="", cost_usd=None)
    result = world.run()
    assert result["status"] == "timeout" and result["reason"] == "deepseek_timeout"
    assert result["timeout_seconds"] == 42 and world.deepseek.timeout_seconds == 42
    timeout = [e for e in world.events() if e["event_type"] == "dream_cycle_timeout"]
    assert timeout[0]["provider"] == "deepseek"


def test_kimi_timeout_keeps_its_reason(world, monkeypatch):
    monkeypatch.setenv("VNX_DREAM_PROVIDER", "kimi")

    def slow(prompt, **kwargs):
        raise subprocess.TimeoutExpired("kimi", 1)

    monkeypatch.setattr(kimi_wrapper, "kimi_exec", slow)
    assert world.run()["reason"] == "kimi_timeout"


def test_every_event_of_an_attempt_carries_the_provider(world):
    world.run()
    attempt = [e for e in world.events() if e["event_type"].startswith("dream_cycle_")]
    assert attempt and all(e.get("provider") == "deepseek" for e in attempt)


def test_registry_default_is_deepseek_in_the_intelligence_category():
    import config_registry
    entry = config_registry.CONFIG_REGISTRY["VNX_DREAM_PROVIDER"]
    assert (entry.default, entry.category, entry.type) == ("deepseek", "intelligence", "string")


def test_the_unknown_provider_skip_is_a_blocked_skip_for_the_liveness_watch():
    import dream_liveness
    assert dream_liveness.classify_skip("dream_provider_unknown", "x") == (dream_liveness.BLOCKED, True)
