"""Provider switch, deliverable E: the intelligence consumers honor the operator's switch.

The classifier factory refuses a disabled family. Tagger, scout and receipt classifier skip and
count. The nightly analyzer makes no attempt on a disabled lane, counts the skip, and the beacon
says ``degraded`` with reason ``provider_switched_off`` when nothing was analysed for that reason.

Every launch primitive is a spy (the provider's ``classify``, ``subprocess.run``, ``urlopen``):
no deepseek, ollama or claude process starts. The switch file, the boundary file, HOME, the QI
database and the beacon all live in tmp_path; the operator's real files are never read.
"""

import argparse
import importlib.util
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

_import_env = {
    "VNX_HOME": tempfile.mkdtemp(),
    "VNX_STATE_DIR": tempfile.mkdtemp(),
    "PROJECT_ROOT": tempfile.mkdtemp(),
}
with patch.dict(os.environ, _import_env):
    import conversation_analyzer as ca_pkg
    import conversation_analyzer.deep_analyzer as da_module
    import conversation_analyzer.runner as runner_module
    from conversation_analyzer import ConversationAnalyzer, DeepAnalyzer, RunStats
    from conversation_analyzer.deep_analyzer import LLMOutcome

import classifier_providers  # noqa: E402
import content_class  # noqa: E402
import provider_switch as ps  # noqa: E402
import quality_db_init  # noqa: E402
import receipt_classifier  # noqa: E402
import scout_prepass  # noqa: E402
import vnx_tagger  # noqa: E402
from classifier_providers.base import ClassifierResult, get_provider  # noqa: E402
from classifier_providers.deepseek_provider import DeepSeekProvider  # noqa: E402

PROJECT_X = "proj-x"
PROJECT_Y = "proj-y"
TENANT = "tenant-t"


def _entry(enabled):
    return {"enabled": enabled, "changed_at": "2026-10-15T08:00:00Z",
            "changed_by": "op", "reason": "test"}


@pytest.fixture
def switch(tmp_path, monkeypatch):
    """A tmp switch file; the returned callable writes a version 1 document into it."""
    path = tmp_path / "switch" / "providers.json"
    monkeypatch.setenv("VNX_PROVIDER_SWITCH_FILE", str(path))
    for name in list(os.environ):
        if name.startswith(ps.OVERRIDE_ENV_PREFIX):
            monkeypatch.delenv(name)
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT_X)

    def write(global_off=(), project_on=None, project_off=None):
        document = {"version": 1,
                    "global": {f: _entry(False) for f in global_off},
                    "projects": {}}
        for pid, families in (project_on or {}).items():
            document["projects"].setdefault(pid, {}).update({f: _entry(True) for f in families})
        for pid, families in (project_off or {}).items():
            document["projects"].setdefault(pid, {}).update({f: _entry(False) for f in families})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    return write


class _Spy:
    """Replaces DeepSeekProvider.classify / is_available: records every call."""

    def __init__(self, monkeypatch, parsed=None):
        self.calls = []
        parsed = parsed if parsed is not None else {"tags": ["dispatch"]}

        def classify(provider_self, prompt, *args, **kwargs):
            self.calls.append(prompt)
            return ClassifierResult(raw_response="", parsed_json=parsed, cost_usd=0.0,
                                    latency_ms=1, provider="deepseek", error=None)

        monkeypatch.setattr(DeepSeekProvider, "classify", classify)
        monkeypatch.setattr(DeepSeekProvider, "is_available", lambda provider_self: True)


# ---------------------------------------------------------------------------
# Classifier factory
# ---------------------------------------------------------------------------

def test_get_provider_raises_for_a_disabled_family(switch):
    switch(global_off=["deepseek"])
    with pytest.raises(ps.ProviderDisabledError) as exc:
        get_provider("deepseek", project_id=PROJECT_X)
    assert exc.value.decision.family == "deepseek" and not exc.value.decision.allowed
    assert not isinstance(exc.value, ValueError)


@pytest.mark.parametrize("name,family", [("ollama", "local"), ("gemini", "gemini")])
def test_get_provider_refuses_every_switchable_provider_key(switch, name, family):
    switch(global_off=[family])
    with pytest.raises(ps.ProviderDisabledError):
        get_provider(name)


def test_get_provider_leaves_claude_and_codex_alone(switch):
    switch(global_off=["deepseek", "glm", "kimi", "gemini", "local", "openrouter"])
    assert get_provider("haiku").name == "haiku"
    assert get_provider("codex").name == "codex"


def test_get_provider_allows_an_enabled_family_and_honors_the_project(switch):
    switch(global_off=["deepseek"], project_on={PROJECT_X: ["deepseek"]})
    assert get_provider("deepseek", project_id=PROJECT_X).name == "deepseek"
    with pytest.raises(ps.ProviderDisabledError):
        get_provider("deepseek", project_id=PROJECT_Y)


def test_get_provider_unknown_name_stays_a_value_error(switch):
    switch(global_off=["deepseek"])
    with pytest.raises(ValueError, match="unknown classifier provider"):
        get_provider("nope")


def test_get_provider_refuses_on_an_unreadable_switch_file(switch):
    path = switch()
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ps.ProviderDisabledError) as exc:
        get_provider("deepseek")
    assert exc.value.decision.source == ps.SOURCE_UNREADABLE


# ---------------------------------------------------------------------------
# Tagger
# ---------------------------------------------------------------------------

def test_tagger_skips_a_disabled_provider_and_counts_it(switch, monkeypatch, caplog):
    switch(global_off=["deepseek"])
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    monkeypatch.delenv("VNX_TAGGER_PROVIDER", raising=False)
    spy = _Spy(monkeypatch)
    before = vnx_tagger.switch_skip_count()
    with caplog.at_level(logging.WARNING, logger="vnx_tagger"):
        assert vnx_tagger.llm_tags("fix the dispatch lane", ["scripts/lib/dispatch_cli.py"]) == []
    assert spy.calls == []
    assert vnx_tagger.switch_skip_count() == before + 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "vnx_tagger"]
    assert len(warnings) == 1 and "provider_switched_off" in warnings[0].getMessage()


def test_tagger_calls_the_provider_when_the_project_is_on(switch, monkeypatch):
    switch(global_off=["deepseek"], project_on={PROJECT_X: ["deepseek"]})
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    monkeypatch.delenv("VNX_TAGGER_PROVIDER", raising=False)
    spy = _Spy(monkeypatch)
    assert "dispatch" in vnx_tagger.llm_tags("fix the dispatch lane", project_id=PROJECT_X)
    assert len(spy.calls) == 1
    assert vnx_tagger.llm_tags("fix the dispatch lane", project_id=PROJECT_Y) == []
    assert len(spy.calls) == 1


def test_tagger_batch_run_reports_the_skips(switch, monkeypatch, tmp_path):
    switch(global_off=["deepseek"])
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    monkeypatch.delenv("VNX_TAGGER_PROVIDER", raising=False)
    spy = _Spy(monkeypatch)
    db = tmp_path / "qi.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE success_patterns (id INTEGER PRIMARY KEY, title TEXT, "
                 "description TEXT, tags TEXT)")
    conn.executemany("INSERT INTO success_patterns (title, description) VALUES (?, ?)",
                     [("fix dispatch lane", ""), ("harden receipt gate", "")])
    conn.commit()
    conn.close()
    out = vnx_tagger.enrich_pattern_tags(db, tables=["success_patterns"])
    assert out["success_patterns"] == 2 and out["_provider_switched_off"] == 2
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Scout
# ---------------------------------------------------------------------------

def test_scout_skips_a_disabled_provider_and_counts_it(switch, monkeypatch, caplog):
    switch(global_off=["deepseek"])
    spy = _Spy(monkeypatch, parsed={"include": []})
    before = scout_prepass.SCOUT_SWITCH_SKIPS["count"]
    with caplog.at_level(logging.WARNING, logger="scout_prepass"):
        outcome = scout_prepass._invoke_scout_model("prompt", "deepseek")
    assert outcome == (None, "", 0.0, 0)
    assert spy.calls == []
    assert scout_prepass.SCOUT_SWITCH_SKIPS["count"] == before + 1
    assert sum("provider switched off" in r.getMessage() for r in caplog.records) == 1


def test_scout_calls_the_provider_for_a_project_that_is_on(switch, monkeypatch):
    switch(global_off=["deepseek"], project_on={PROJECT_X: ["deepseek"]})
    spy = _Spy(monkeypatch, parsed={"include": []})
    scout_prepass._invoke_scout_model("prompt", "deepseek", PROJECT_X)
    assert len(spy.calls) == 1
    scout_prepass._invoke_scout_model("prompt", "deepseek", PROJECT_Y)
    assert len(spy.calls) == 1


def test_scout_sweep_reports_the_skips(switch, monkeypatch, tmp_path):
    switch(global_off=["deepseek"])
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "1")
    spy = _Spy(monkeypatch, parsed={"include": []})
    monkeypatch.setattr(scout_prepass, "_candidate_refs", lambda paths, text: ["a.py:1-5"])
    data_dir, state_dir = tmp_path / "data", tmp_path / "state"
    bundle = data_dir / "dispatches" / "pending" / "d-1"
    bundle.mkdir(parents=True)
    state_dir.mkdir()
    (bundle / "instruction.md").write_text("implement the thing " * 40, encoding="utf-8")
    summary = scout_prepass.scout_pending_sweep(data_dir, state_dir)
    assert summary["provider_switched_off"] == 1 and summary["produced"] == 0
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Receipt classifier
# ---------------------------------------------------------------------------

def test_receipt_classifier_skips_a_disabled_provider(switch, monkeypatch, tmp_path):
    switch(global_off=["deepseek"])
    monkeypatch.setenv(receipt_classifier.ENV_PROVIDER, "deepseek")
    monkeypatch.setenv(receipt_classifier.ENV_DAILY_BUDGET, "5")
    monkeypatch.setenv(receipt_classifier.ENV_STATE_DIR, str(tmp_path))
    spy = _Spy(monkeypatch)
    result = receipt_classifier.classify_batch([{"dispatch_id": "d-1", "project_id": PROJECT_X}])
    assert result["status"] == "skipped" and result["reason"] == "provider_switched_off"
    assert result["provider"] == "deepseek" and "family=deepseek" in result["detail"]
    assert spy.calls == []


def test_receipt_classifier_runs_when_the_receipt_project_is_on(switch, monkeypatch, tmp_path):
    switch(global_off=["deepseek"], project_on={PROJECT_X: ["deepseek"]})
    monkeypatch.setenv(receipt_classifier.ENV_PROVIDER, "deepseek")
    monkeypatch.setenv(receipt_classifier.ENV_DAILY_BUDGET, "5")
    monkeypatch.setenv(receipt_classifier.ENV_STATE_DIR, str(tmp_path))
    spy = _Spy(monkeypatch, parsed={"impact_class": "none"})
    result = receipt_classifier.classify_batch([{"dispatch_id": "d-1", "project_id": PROJECT_X}])
    assert result["status"] == "ok" and len(spy.calls) == 1


# ---------------------------------------------------------------------------
# Nightly deep analysis: layer 1 and layer 2
# ---------------------------------------------------------------------------

@pytest.fixture
def harness(monkeypatch):
    """Everything the deepseek-harness launch touches, replaced; ``run`` is the launch spy."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-0123456789abcdef")
    monkeypatch.setattr(DeepAnalyzer, "_deepseek_preflight_cache", None, raising=False)
    monkeypatch.setattr(DeepAnalyzer, "_deepseek_preflight",
                        classmethod(lambda cls, key: LLMOutcome("ok")))
    monkeypatch.setattr(da_module, "build_harness_child_env", lambda key: {})
    monkeypatch.setattr(da_module, "build_harness_argv", lambda *a, **k: ["claude"])
    monkeypatch.setattr(da_module, "resolve_claude_cli", lambda: "claude")
    answer = json.dumps({"result": json.dumps({"patterns": [], "bottlenecks": [], "suggestions": []})})
    run = MagicMock(return_value=subprocess.CompletedProcess(["claude"], 0, answer, ""))
    monkeypatch.setattr(da_module.subprocess, "run", run)
    return run


def test_try_deepseek_harness_requires_before_any_launch(switch, harness):
    switch(global_off=["deepseek"])
    with pytest.raises(ps.ProviderDisabledError):
        DeepAnalyzer._try_deepseek_harness("prompt", project_id=PROJECT_X)
    harness.assert_not_called()


def test_try_ollama_requires_before_any_request(switch, monkeypatch):
    switch(global_off=["local"])
    monkeypatch.setattr(DeepAnalyzer, "_probe_ollama", classmethod(lambda cls: True))
    with patch("urllib.request.urlopen") as urlopen:
        with pytest.raises(ps.ProviderDisabledError):
            DeepAnalyzer._try_ollama("prompt", project_id=PROJECT_X)
    urlopen.assert_not_called()


def _analyze(project_origin):
    analyzer = DeepAnalyzer()
    metrics = SimpleNamespace(session_id="s", total_output_tokens=5000, tool_calls_total=20)
    with patch.object(DeepAnalyzer, "_build_session_summary", return_value="summary"), \
         patch.object(DeepAnalyzer, "_routes_restricted", return_value=False), \
         patch.object(da_module, "LLM_STRATEGY", "deepseek-harness"):
        result = analyzer.analyze_session(Path("x.jsonl"), metrics, None, origin=project_origin)
    return analyzer, result


def _origin(project_id):
    return content_class.Origin(content_class.FABRIC, project_id, content_class.SRC_MARKER)


def test_cross_project_global_off_project_x_on(switch, harness):
    """Global deepseek off, project X on: X is analysed on deepseek-harness, Y is skipped."""
    switch(global_off=["deepseek"], project_on={PROJECT_X: ["deepseek"]})
    analyzer_x, result_x = _analyze(_origin(PROJECT_X))
    assert result_x is not None and result_x["_model"].startswith("deepseek-harness:")
    assert harness.call_count == 1 and analyzer_x.deep_attempts == 1 and analyzer_x.deep_disabled_skips == 0

    analyzer_y, result_y = _analyze(_origin(PROJECT_Y))
    assert result_y is None
    assert harness.call_count == 1
    assert analyzer_y.deep_attempts == 0 and analyzer_y.deep_failures == 0
    assert analyzer_y.deep_config_skips == 0 and analyzer_y.deep_disabled_skips == 1


def test_a_session_without_a_project_is_decided_globally(switch, harness):
    """The process project (VNX_PROJECT_ID=proj-x, on) does not stand in for the session's."""
    switch(global_off=["deepseek"], project_on={PROJECT_X: ["deepseek"]})
    analyzer, result = _analyze(content_class.Origin(content_class.FABRIC, None, content_class.SRC_NONE))
    assert result is None and harness.call_count == 0 and analyzer.deep_disabled_skips == 1
    analyzer, result = _analyze(None)
    assert result is None and harness.call_count == 0 and analyzer.deep_disabled_skips == 1


def test_a_disabled_lane_is_not_a_failure_or_a_config_gap(switch, harness):
    switch(global_off=["deepseek"])
    analyzer, _ = _analyze(_origin(PROJECT_X))
    assert (analyzer.deep_attempts, analyzer.deep_failures, analyzer.deep_config_skips) == (0, 0, 0)
    assert analyzer.deep_failure_reasons == {} and analyzer.deep_disabled_skips == 1
    assert analyzer.last_status == "failed"


def test_ollama_lane_is_skipped_when_local_is_off(switch, monkeypatch):
    switch(global_off=["local"])
    monkeypatch.setattr(DeepAnalyzer, "_probe_ollama", classmethod(lambda cls: True))
    analyzer = DeepAnalyzer()
    metrics = SimpleNamespace(session_id="s", total_output_tokens=5000, tool_calls_total=20)
    with patch("urllib.request.urlopen") as urlopen, \
         patch.object(DeepAnalyzer, "_build_session_summary", return_value="summary"), \
         patch.object(DeepAnalyzer, "_routes_restricted", return_value=False), \
         patch.object(da_module, "LLM_STRATEGY", "ollama-only"):
        assert analyzer.analyze_session(Path("x.jsonl"), metrics, None, origin=_origin(PROJECT_X)) is None
    urlopen.assert_not_called()
    assert analyzer.deep_disabled_skips == 1 and analyzer.deep_attempts == 0


# ---------------------------------------------------------------------------
# Beacon, end to end through the runner
# ---------------------------------------------------------------------------

def _load_cli():
    spec = importlib.util.spec_from_file_location("analyzer_cli_ps_e", SCRIPTS / "conversation_analyzer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_session(projects_dir, sid, cwd):
    folder = projects_dir / f"-{sid}"
    folder.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "user", "timestamp": "2026-10-01T10:00:00Z", "cwd": str(cwd),
         "message": {"role": "user", "content": "hello there"}},
        {"type": "assistant", "timestamp": "2026-10-01T10:30:00Z",
         "message": {"model": "claude-sonnet-5-5", "content": [],
                     "usage": {"input_tokens": 10, "output_tokens": 150_000}}},
    ]
    (folder / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")


@pytest.fixture
def world(tmp_path, monkeypatch, switch, harness):
    home = tmp_path / "home"
    for name in ("x", "y"):
        project_dir = home / "dev" / f"proj-{name}"
        project_dir.mkdir(parents=True)
        (project_dir / ".vnx-project-id").write_text(f"proj-{name}\n")
    boundary_file = tmp_path / "content_boundary.json"
    boundary_file.write_text(json.dumps({"version": 1, "client_roots": [str(home / "clients")],
                                         "personal_roots": [str(home / "personal")]}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VNX_CONTENT_BOUNDARY_FILE", str(boundary_file))
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(ca_pkg, "CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr(da_module, "LLM_STRATEGY", "deepseek-harness")
    monkeypatch.setattr(runner_module, "resolve_stamp_project_id", lambda *a, **k: TENANT)
    db_path = tmp_path / "state" / "quality_intelligence.db"
    db_path.parent.mkdir()
    assert quality_db_init.bootstrap_qi_db(db_path)
    conn = sqlite3.connect(db_path)
    for table in ("success_patterns", "antipatterns"):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT")
    conn.commit()
    conn.close()
    _write_session(projects, "sx", home / "dev" / "proj-x")
    _write_session(projects, "sy", home / "dev" / "proj-y")
    return {"db": db_path, "tmp": tmp_path}


def _run_and_beacon(world):
    analyzer = ConversationAnalyzer(world["db"])
    analyzer.connect()
    try:
        stats = analyzer.run(dry_run=False, deep_budget=20)
    finally:
        analyzer.close()
    cli = _load_cli()
    cli.PATHS = {"VNX_DATA_DIR": str(world["tmp"] / "beacon-data")}
    cli._write_heartbeat(argparse.Namespace(max_sessions=50, dry_run=False), stats, "ok", None)
    beacon = json.loads((world["tmp"] / "beacon-data" / "health" / "conversation_analyzer.json").read_text())
    return stats, beacon


def test_beacon_is_degraded_when_every_deep_session_was_skipped_by_the_switch(world, switch, harness):
    switch(global_off=["deepseek"])
    stats, beacon = _run_and_beacon(world)
    assert harness.call_count == 0
    assert stats.deep_disabled_skips == 2 and stats.deep_attempts == 0 and stats.sessions_deep == 0
    assert beacon["status"] == "degraded"
    assert beacon["details"]["reason"] == "provider_switched_off"
    assert beacon["details"]["deep_disabled_skips"] == 2


def test_beacon_stays_ok_when_one_project_was_analysed(world, switch, harness):
    switch(global_off=["deepseek"], project_on={PROJECT_X: ["deepseek"]})
    stats, beacon = _run_and_beacon(world)
    assert harness.call_count == 1
    assert stats.deep_attempts == 1 and stats.deep_disabled_skips == 1
    assert beacon["status"] == "ok" and beacon["details"]["deep_disabled_skips"] == 1
    assert "reason" not in beacon["details"]


def test_beacon_ok_when_the_switch_is_on(world, switch, harness):
    switch()
    stats, beacon = _run_and_beacon(world)
    assert harness.call_count == 2 and stats.deep_disabled_skips == 0
    assert beacon["status"] == "ok" and beacon["details"]["deep_disabled_skips"] == 0


def test_a_failed_run_keeps_its_fail_status(world, switch, harness):
    cli = _load_cli()
    cli.PATHS = {"VNX_DATA_DIR": str(world["tmp"] / "beacon-data")}
    stats = RunStats(deep_disabled_skips=1, deep_open_sessions=1)
    cli._write_heartbeat(argparse.Namespace(max_sessions=1, dry_run=False), stats, "fail", "boom")
    beacon = json.loads((world["tmp"] / "beacon-data" / "health" / "conversation_analyzer.json").read_text())
    assert beacon["status"] == "fail" and beacon["details"]["error"] == "boom"


def test_a_config_skip_next_to_switch_skips_is_not_every_session():
    stats = RunStats(deep_disabled_skips=1, deep_open_sessions=2, deep_config_skips=1)
    assert ca_pkg.deep_all_skipped_by_switch(stats) is False
    assert ca_pkg.deep_all_skipped_by_switch(RunStats(deep_disabled_skips=2, deep_open_sessions=2)) is True
    assert ca_pkg.deep_all_skipped_by_switch(RunStats()) is False
    assert ca_pkg.deep_all_skipped_by_switch(None) is False


# ---------------------------------------------------------------------------
# Per-item project decisions (PR #2057 fix round 2)
# ---------------------------------------------------------------------------

def test_receipt_batch_decides_the_switch_per_receipt(switch, monkeypatch, tmp_path):
    """DeepSeek off globally, on only for X: a receipt of Y in the same batch never reaches the prompt."""
    switch(global_off=["deepseek"], project_on={PROJECT_X: ["deepseek"]})
    monkeypatch.setenv(receipt_classifier.ENV_PROVIDER, "deepseek")
    monkeypatch.setenv(receipt_classifier.ENV_DAILY_BUDGET, "5")
    monkeypatch.setenv(receipt_classifier.ENV_STATE_DIR, str(tmp_path))
    spy = _Spy(monkeypatch, parsed={"impact_class": "none"})
    result = receipt_classifier.classify_batch([
        {"dispatch_id": "d-x", "project_id": PROJECT_X, "summary": "secret-of-x"},
        {"dispatch_id": "d-y", "project_id": PROJECT_Y, "summary": "secret-of-y"},
    ])
    assert all("secret-of-y" not in call for call in spy.calls)
    assert len(spy.calls) == 1 and "secret-of-x" in spy.calls[0]
    assert result["status"] == "ok" and result["switched_off_receipts"] == 1


def test_receipt_batch_all_projects_off_is_skipped(switch, monkeypatch, tmp_path):
    switch(global_off=["deepseek"])
    monkeypatch.setenv(receipt_classifier.ENV_PROVIDER, "deepseek")
    monkeypatch.setenv(receipt_classifier.ENV_DAILY_BUDGET, "5")
    monkeypatch.setenv(receipt_classifier.ENV_STATE_DIR, str(tmp_path))
    spy = _Spy(monkeypatch)
    result = receipt_classifier.classify_batch([
        {"dispatch_id": "d-x", "project_id": PROJECT_X, "summary": "secret-of-x"},
        {"dispatch_id": "d-y", "project_id": PROJECT_Y, "summary": "secret-of-y"},
    ])
    assert result["status"] == "skipped" and result["reason"] == "provider_switched_off"
    assert spy.calls == []


def test_tagger_rows_decide_the_switch_by_their_own_project(switch, monkeypatch, tmp_path):
    """Process project X is on, the row of disabled project Y must not reach the provider."""
    switch(global_off=["deepseek"], project_on={PROJECT_X: ["deepseek"]})
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    monkeypatch.delenv("VNX_TAGGER_PROVIDER", raising=False)
    spy = _Spy(monkeypatch)
    db = tmp_path / "qi.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE success_patterns (id INTEGER PRIMARY KEY, title TEXT, "
                 "description TEXT, tags TEXT, project_id TEXT)")
    conn.executemany("INSERT INTO success_patterns (title, description, project_id) VALUES (?, ?, ?)",
                     [("fix-x-lane", "", PROJECT_X), ("secret-y-lane", "", PROJECT_Y),
                      ("no-project-lane", "", None)])
    conn.commit()
    conn.close()
    out = vnx_tagger.enrich_pattern_tags(db, tables=["success_patterns"])
    assert all("secret-y-lane" not in c and "no-project-lane" not in c for c in spy.calls)
    assert len(spy.calls) == 1 and "fix-x-lane" in spy.calls[0]
    assert out["success_patterns"] == 3 and out["_provider_switched_off"] == 2
