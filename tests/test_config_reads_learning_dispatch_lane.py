"""PR-7a (config-een-oppervlak): the learning, central-db and auto-report keys read through
``config_runtime``. A value stored in the project's config store wins while the env var is empty.

One test per key: store a value, leave the env empty, the code under test uses the stored value.
The env layer is covered too (``config_runtime`` is the one reader; there is no second env read).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib"), str(VNX_ROOT / "scripts" / "dream"), str(VNX_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config_registry  # noqa: E402
import config_runtime  # noqa: E402
import config_store_db  # noqa: E402
import review_stack_support  # noqa: E402

PROJECT = "learning-dispatch-lane-project"
APPROVAL = "appr-pr7a"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    yield from review_stack_support.isolate_config_registry(monkeypatch)


def _store(tmp_path, monkeypatch, values: dict):
    """A project store with ``values`` written as project_config rows, wired into config_runtime."""
    store = review_stack_support.build_project_store(tmp_path, monkeypatch)
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    conn = sqlite3.connect(store["state_dir"] / "runtime_coordination.db")
    try:
        config_store_db.ensure_config_tables(conn)
        for key, value in values.items():
            config_store_db.set_config(conn, PROJECT, key, value, actor="test", approval_id=APPROVAL)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    assert config_runtime.autowire(state_dir=store["state_dir"], project_id=PROJECT) is True
    return store


# -- learning loop gate -----------------------------------------------------------------------


def test_learning_loop_gate_reads_the_stored_values(tmp_path, monkeypatch):
    import learning_loop

    store = _store(tmp_path, monkeypatch, {"VNX_LEARNING_LOOP_ENABLED": "1", "VNX_INJECTION_FEEDBACK_ENABLED": "1"})
    monkeypatch.setattr(
        "injection_effectiveness_probe.InjectionEffectivenessProbe.run",
        lambda self: SimpleNamespace(status="unknown", signal="test"),
    )
    result = learning_loop.evaluate_activation_gate(store["state_dir"])
    assert result["action"] == "degraded"


def test_learning_loop_stored_enable_without_feedback_stays_misconfigured(tmp_path, monkeypatch):
    import learning_loop

    store = _store(tmp_path, monkeypatch, {"VNX_LEARNING_LOOP_ENABLED": "1"})
    with pytest.raises(learning_loop.LearningLoopMisconfigured):
        learning_loop.evaluate_activation_gate(store["state_dir"])


def test_learning_loop_nothing_stored_is_dormant(tmp_path, monkeypatch):
    import learning_loop

    store = _store(tmp_path, monkeypatch, {})
    assert learning_loop.evaluate_activation_gate(store["state_dir"])["action"] == "dormant"


# -- dream scheduler --------------------------------------------------------------------------


def test_dream_scheduler_stored_value_opens_the_gate(tmp_path, monkeypatch):
    import consolidator

    store = _store(tmp_path, monkeypatch, {"VNX_DREAM_SCHEDULER_ENABLED": "1"})
    monkeypatch.setattr(consolidator, "_injection_probe_health", lambda state_dir: "produces_crap")
    data_root = store["state_dir"].parent
    result = consolidator.run_dream_cycle(PROJECT, data_root / "state" / "q.db", dry_run=True, data_root=data_root)
    assert result.get("reason") != "scheduler_disabled"


def test_dream_scheduler_nothing_stored_skips_as_disabled(tmp_path, monkeypatch):
    import consolidator

    store = _store(tmp_path, monkeypatch, {})
    data_root = store["state_dir"].parent
    result = consolidator.run_dream_cycle(PROJECT, data_root / "state" / "q.db", dry_run=True, data_root=data_root)
    assert result["status"] == "skipped"
    assert result["detail"] == "VNX_DREAM_SCHEDULER_ENABLED=0"


# -- placebo arm ------------------------------------------------------------------------------


def test_placebo_arm_reads_the_stored_value(tmp_path, monkeypatch):
    import intelligence_selector

    _store(tmp_path, monkeypatch, {"VNX_INTEL_PLACEBO_ARM": "1"})
    assert intelligence_selector._placebo_arm_enabled() is True


def test_placebo_arm_has_no_env_fallback_when_config_runtime_fails(monkeypatch):
    import intelligence_selector

    def boom(key):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(config_runtime, "get_bool", boom)
    monkeypatch.setenv("VNX_INTEL_PLACEBO_ARM", "1")
    with pytest.raises(RuntimeError):
        intelligence_selector._placebo_arm_enabled()


# -- central db flag --------------------------------------------------------------------------


def test_central_db_flag_reads_a_stored_row(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch, {})
    conn = sqlite3.connect(store["state_dir"] / "runtime_coordination.db")
    try:
        conn.execute(
            "INSERT INTO project_config (project_id, config_key, config_value, updated_at, updated_by) "
            "VALUES (?, 'VNX_USE_CENTRAL_DB', 'shadow', '2026-11-19T00:00:00Z', 'test')", (PROJECT,),
        )
        conn.commit()
    finally:
        conn.close()
    assert config_runtime.central_db_flag() == "shadow"


def test_central_db_flag_env_layer_and_default(monkeypatch):
    assert config_runtime.central_db_flag() == ""
    monkeypatch.setenv("VNX_USE_CENTRAL_DB", " 1 ")
    assert config_runtime.central_db_flag() == "1"


def test_central_db_readers_follow_the_helper(monkeypatch):
    import dispatch_register

    monkeypatch.setattr(dispatch_register, "_central_db_flag", lambda: "1")
    monkeypatch.setattr(dispatch_register, "_read_register_locked_central", lambda pid: "central:" + pid)
    monkeypatch.setattr(dispatch_register, "_read_register_locked_per_project", lambda path: "per-project")
    monkeypatch.setattr(dispatch_register, "_project_id_from_state_dir", lambda p: "p1")
    assert dispatch_register._read_register_locked(Path("/x/state/dispatch_register.ndjson")) == "central:p1"


# -- auto report ------------------------------------------------------------------------------


def test_auto_report_registry_default_is_the_value_the_fabric_runs():
    entry = config_registry.CONFIG_REGISTRY["VNX_AUTO_REPORT"]
    assert entry.default == "1"
    assert entry.read_site_wired is True


def test_trigger_report_pipeline_stored_off_wins_without_env(tmp_path, monkeypatch):
    from subprocess_adapter import SubprocessAdapter

    _store(tmp_path, monkeypatch, {"VNX_AUTO_REPORT": "0"})
    assert SubprocessAdapter().trigger_report_pipeline("T1", "d-1") is False


def test_trigger_report_pipeline_default_is_enabled(tmp_path, monkeypatch):
    from subprocess_adapter import SubprocessAdapter

    store = _store(tmp_path, monkeypatch, {})
    adapter = SubprocessAdapter()
    assert adapter.trigger_report_pipeline("T1", "d-2", project_root=str(store["state_dir"].parents[1])) is True


def test_vnx_paths_no_longer_exports_auto_report():
    text = (VNX_ROOT / "scripts" / "lib" / "vnx_paths.sh").read_text(encoding="utf-8")
    assert "VNX_AUTO_REPORT" not in text


def _run_hook(project: Path, env_extra: dict) -> subprocess.CompletedProcess:
    import json
    import os

    payload = json.dumps({"session_id": "s1", "cwd": str(project / ".claude" / "terminals" / "T1"), "transcript_path": ""})
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_") and k != "PYTHONPATH"}
    env.update({"VNX_DATA_DIR": str(project / ".vnx-data")})
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(VNX_ROOT / "scripts" / "hooks" / "stop_report_hook.sh")],
        input=payload, text=True, capture_output=True, env=env, cwd=project, timeout=60,
    )


def _hook_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".claude" / "terminals" / "T1").mkdir(parents=True)
    (project / ".vnx-data" / "state").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "scripts").symlink_to(VNX_ROOT / "scripts")
    return project


def test_stop_hook_stored_off_skips_with_the_env_empty(tmp_path):
    project = _hook_project(tmp_path)
    conn = sqlite3.connect(project / ".vnx-data" / "state" / "runtime_coordination.db")
    try:
        config_store_db.ensure_config_tables(conn)
        config_store_db.set_config(conn, PROJECT, "VNX_AUTO_REPORT", "0", actor="test", approval_id=APPROVAL)
        conn.commit()
    finally:
        conn.close()
    out = _run_hook(project, {"VNX_PROJECT_ID": PROJECT})
    assert out.returncode == 0
    assert "VNX_AUTO_REPORT disabled" in out.stdout


def test_stop_hook_unreadable_gate_names_the_key_and_skips(tmp_path):
    project = _hook_project(tmp_path)
    (project / "scripts").unlink()
    (project / "scripts").mkdir()
    out = _run_hook(project, {})
    assert out.returncode == 0
    assert "VNX_AUTO_REPORT could not be read" in out.stderr
