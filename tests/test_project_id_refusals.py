"""AG-1o: one counter for an unresolved project id, shown per context in ``vnx doctor``."""
import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import project_id_refusals as pir

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in ("VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR", "VNX_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VNX_DATA_HOME", str(home / ".vnx-data"))
    store = home / ".vnx-data" / "proj"
    (store / "state").mkdir(parents=True)
    return store, home / ".vnx-data"


def _write(path: Path, *lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        (l if isinstance(l, str) else json.dumps(l)) + "\n" for l in lines
    ), encoding="utf-8")


def _ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def test_record_appends_event(tmp_path):
    pir.record_project_id_refusal("receipt", tmp_path, "no id", dispatch_id="d1")
    rec = json.loads((tmp_path / "skip_metrics.ndjson").read_text())
    assert rec["event_type"] == "project_id_unresolved"
    assert rec["context"] == "receipt" and rec["reason"] == "no id"
    assert rec["dispatch_id"] == "d1" and rec["ts"]


def test_record_logs_error(tmp_path, caplog):
    with caplog.at_level(logging.ERROR):
        pir.record_project_id_refusal("hook", tmp_path, "why")
    assert any("project_id_unresolved" in r.message for r in caplog.records)


def test_record_defaults_to_data_home(tmp_path, monkeypatch):
    anchor = tmp_path / "datahome"
    anchor.mkdir()
    monkeypatch.setattr(pir, "data_home", lambda: anchor)
    pir.record_project_id_refusal("hook", None, "why")
    assert (anchor / "skip_metrics.ndjson").exists()


def test_data_home_follows_env(env):
    _, home = env
    assert pir.data_home() == home


def test_record_never_raises_on_unwritable_anchor(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with caplog.at_level(logging.ERROR):
        pir.record_project_id_refusal("hook", blocker / "sub", "why")
    assert any("append failed" in r.message for r in caplog.records)


def test_record_never_raises_on_missing_dir_and_full_disk(tmp_path, monkeypatch, caplog):
    pir.record_project_id_refusal("hook", tmp_path / "missing" / "dir", "why")
    import state_writer

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(state_writer, "append_locked", boom)
    with caplog.at_level(logging.ERROR):
        pir.record_project_id_refusal("hook", tmp_path, "why")
    assert any("No space" in r.message for r in caplog.records)


def test_logging_failure_still_appends_and_names_it_on_stderr(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("log handler broke")

    monkeypatch.setattr(pir.logger, "error", boom)
    pir.record_project_id_refusal("hook", tmp_path, "why")
    rec = json.loads((tmp_path / "skip_metrics.ndjson").read_text())
    assert rec["context"] == "hook" and rec["event_type"] == "project_id_unresolved"
    err = capsys.readouterr().err
    assert "context=hook" in err and "log handler broke" in err


def test_append_failure_still_logs_and_does_not_raise(tmp_path, monkeypatch, caplog):
    import state_writer

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(state_writer, "append_locked", boom)
    with caplog.at_level(logging.ERROR):
        pir.record_project_id_refusal("hook", tmp_path, "why")
    messages = [r.getMessage() for r in caplog.records]
    assert any("project_id_unresolved: context=hook" in m for m in messages)
    assert any("append failed" in m and "skip_metrics.ndjson" in m for m in messages)


def test_both_log_and_append_failing_never_raises_and_reaches_stderr(tmp_path, monkeypatch, capsys):
    import state_writer

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pir.logger, "error", boom)
    monkeypatch.setattr(state_writer, "append_locked", boom)
    pir.record_project_id_refusal("hook", tmp_path, "why")
    err = capsys.readouterr().err
    assert "logging failed" in err and "append failed" in err and "No space" in err


def test_section_counts_per_context_window_and_sources(env):
    store, home = env
    _write(store / "state" / "skip_metrics.ndjson",
           {"event_type": "tenant_stamp_skip", "table": "dispatch_metadata", "timestamp": _ago(1)},
           {"event_type": "project_id_unresolved", "context": "receipt", "ts": _ago(2)},
           {"event_type": "project_id_unresolved", "context": "receipt", "ts": _ago(25)},
           {"event_type": "other", "context": "receipt", "ts": _ago(1)})
    _write(home / "skip_metrics.ndjson",
           {"event_type": "project_id_unresolved", "context": "receipt", "ts": _ago(3)})
    sec = pir.project_id_refusal_section(store, home=home, now=NOW)
    assert sec["contexts"] == {"dispatch_metadata": 1, "receipt": 2}
    assert sec["unreadable"] == 0
    assert sec["unattributed"] == {"ledger": False, "count": 0, "amount_usd": 0.0, "unreadable": 0}
    status, detail = pir.format_section(sec)
    assert status == "warn" and "receipt=2" in detail and "no ledger" in detail


def test_section_clean_is_pass(env):
    store, home = env
    status, detail = pir.format_section(pir.project_id_refusal_section(store, home=home, now=NOW))
    assert status == "pass" and "no ledger" in detail


def test_unattributed_ledger_present_and_unreadable(env):
    store, home = env
    _write(home / "provider_costs.unattributed.ndjson",
           {"timestamp": _ago(1), "cost_usd_estimate": 1.5},
           {"timestamp": _ago(2), "cost_usd_estimate": 0.25},
           {"timestamp": _ago(30), "cost_usd_estimate": 9.0},
           "{not json",
           {"timestamp": "garbage", "cost_usd_estimate": 1})
    un = pir.project_id_refusal_section(store, home=home, now=NOW)["unattributed"]
    assert un == {"ledger": True, "count": 2, "amount_usd": 1.75, "unreadable": 2}
    status, detail = pir.format_section(pir.project_id_refusal_section(store, home=home, now=NOW))
    assert status == "warn" and "2 unreadable" in detail


def test_unreadable_skip_metrics_line_is_reported(env):
    store, home = env
    _write(home / "skip_metrics.ndjson", "{broken",
           {"event_type": "project_id_unresolved", "context": "x"})
    sec = pir.project_id_refusal_section(store, home=home, now=NOW)
    assert sec["unreadable"] == 2 and sec["total"] == 0
    assert pir.format_section(sec)[0] == "warn"


def _args(project):
    return argparse.Namespace(project_dir=str(project), json=True, strict=False)


def test_vnx_cli_doctor_shows_tenant_stamp_skip(env, capsys):
    """Red run on origin/main: a tenant_stamp_skip line never showed in ``vnx doctor``."""
    store, _ = env
    project = store.parent.parent / "project"
    (project / ".vnx").mkdir(parents=True)
    (project / ".vnx-project-id").write_text("proj\n", encoding="utf-8")
    now = datetime.now(timezone.utc).isoformat()
    _write(store / "state" / "skip_metrics.ndjson",
           {"event_type": "tenant_stamp_skip", "table": "dispatch_metadata", "timestamp": now})
    from vnx_cli.commands.doctor import vnx_doctor

    vnx_doctor(_args(project))
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["project-id-refusals"]["status"] == "WARN"
    assert "dispatch_metadata=1" in checks["project-id-refusals"]["detail"]


def test_fabric_doctor_check_shows_refusal(env):
    store, _ = env
    import vnx_doctor as fabric

    _write(store / "state" / "skip_metrics.ndjson",
           {"event_type": "project_id_unresolved", "context": "door",
            "ts": datetime.now(timezone.utc).isoformat()})
    res = fabric.check_project_id_refusals({"VNX_DATA_DIR": str(store)})[0]
    assert res.status == fabric.WARN and "door=1" in res.message
