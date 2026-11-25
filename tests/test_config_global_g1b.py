"""ADR-093: recover transactions, never infer success from a matching value."""
import fcntl
import hashlib
import json
import logging
from uuid import UUID

import pytest

from test_config_global import APPROVAL, PLAIN, _audit, cfg, cg


def write(value="1"):
    return cg.set_global(APPROVAL, value, actor="current", reason="g1b", approval_id="A-1")


def crash(monkeypatch, landed):
    append = cg._append_audit

    def stop(*args, **kwargs):
        raise SystemExit("simulated crash")

    def append_before_crash(path, record):
        if record["operation"] in ("commit", "abort"):
            stop()
        append(path, record)

    with monkeypatch.context() as patch:
        patch.setattr(cg, "_append_audit", append_before_crash)
        if not landed:
            patch.setattr(cg, "atomic_write_text", stop)
        with pytest.raises(SystemExit, match="simulated crash"):
            write("0")


@pytest.mark.parametrize("landed", [False, True])
@pytest.mark.parametrize("seed", [False, True])
def test_g1b_crash_recovery_and_second_round(cfg, monkeypatch, landed, seed):
    if seed:
        write()
    crash(monkeypatch, landed)
    pending = _audit(cfg)[-1]
    before_count = len(_audit(cfg))
    write()
    recovered = _audit(cfg)[before_count]
    assert recovered["operation"] == ("commit" if landed else "abort")
    assert recovered["txn"] == pending["txn"] and recovered["actor"] == "current"
    if not landed:
        assert recovered["error"] == "crash_recovery: replace never landed"
    before = cg.audit_log_path(cfg).read_bytes()
    with cg._locked(cfg):
        cg._recover(cfg, "current")
    assert cg.audit_log_path(cfg).read_bytes() == before
    audit, commit = _audit(cfg)[-2:]
    assert UUID(audit["txn"]).version == 4 and commit["txn"] == audit["txn"]
    assert json.loads(cfg.read_text())["last_txn"] == audit["txn"]
    assert hashlib.sha256(cfg.read_bytes()).hexdigest() == audit["new_sha256"]


def test_g1b_hand_edit_refuses_all_writes_including_cli(cfg, monkeypatch, tmp_path):
    import config_cli

    write()
    crash(monkeypatch, True)
    document = json.loads(cfg.read_text())
    document["values"][APPROVAL]["value"] = "1"
    cfg.write_text(json.dumps(document))
    before, log = cfg.read_bytes(), cg.audit_log_path(cfg).read_bytes()
    for key in (APPROVAL, PLAIN):
        for call in (cg.set_global, cg.unset_global):
            with pytest.raises(cg.GlobalConfigUnreadable, match="txn"):
                call(key, *(["1"] if call == cg.set_global else []), actor="op", reason="r", approval_id="A")
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "mode.json").write_text('{"mode":"operator","schema_version":1}')
    errors = []
    assert config_cli.main(["set", "VNX_OLLAMA_HOST", "h", "--global", "--reason", "r"],
                           project_id="p1", state_dir=state, err=errors.append) == 3
    assert all(s in errors[0] for s in (str(cfg), _audit(cfg)[-1]["txn"], "line", "config_audit.ndjson"))
    assert cfg.read_bytes() == before and cg.audit_log_path(cfg).read_bytes() == log
    assert cg.get(APPROVAL) == "1"


def test_g1b_read_warns_once_without_recovery(cfg, monkeypatch, caplog):
    write()
    crash(monkeypatch, False)
    before = cg.audit_log_path(cfg).read_bytes()
    with caplog.at_level(logging.WARNING, logger="config_global"):
        assert cg.get(APPROVAL) == "1"
    assert len(caplog.records) == 1 and "txn" in caplog.text
    assert cg.audit_log_path(cfg).read_bytes() == before


def test_g1b_legacy_and_aborted_predecessor(cfg, monkeypatch):
    cfg.write_text(json.dumps({"version": 1, "values": {APPROVAL: {"value": "1"}}}))
    cg.audit_log_path(cfg).write_text('{"operation":"set"}\n')
    crash(monkeypatch, False)
    with cg._locked(cfg):
        cg._recover(cfg, "current")
    crash(monkeypatch, False)
    write()
    assert [r["operation"] for r in _audit(cfg)] == ["set", "set", "abort", "set", "abort", "set", "commit"]


@pytest.mark.parametrize("field", ["txn", "new_sha256", "operation", "last_txn", "terminal"])
@pytest.mark.parametrize("bad", ["missing", None, [], "unknown", ""])
def test_g1b_invalid_evidence_never_recovers(cfg, monkeypatch, field, bad):
    write()
    crash(monkeypatch, True)
    path = cfg if field == "last_txn" else cg.audit_log_path(cfg)
    records = [json.loads(cfg.read_text())] if path == cfg else _audit(cfg)
    if field == "terminal":
        records.append({"txn": records[-1]["txn"], "operation": "commit"})
        field = "operation"
    if bad == "missing":
        records[-1].pop(field)
    else:
        records[-1][field] = bad
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    before = cg.audit_log_path(cfg).read_bytes()
    with pytest.raises(cg.GlobalConfigUnreadable):
        write()
    assert cg.audit_log_path(cfg).read_bytes() == before


@pytest.mark.parametrize("target", ["audit", "document"])
@pytest.mark.parametrize("damage", ["{", "[]", "denied"])
def test_g1b_unreadable_recovery_is_visible(cfg, monkeypatch, caplog, target, damage):
    write()
    crash(monkeypatch, True)
    path = cfg if target == "document" else cg.audit_log_path(cfg)
    if damage == "denied":
        def denied(*args, **kwargs):
            raise OSError("read denied")
        original = type(cfg).read_bytes if target == "document" else type(cfg).read_text
        monkeypatch.setattr(type(cfg), "read_bytes" if target == "document" else "read_text",
                            lambda p, *a, **kw: denied() if p == path else original(p, *a, **kw))
    else:
        path.write_text(damage)
    with pytest.raises(cg.GlobalConfigUnreadable, match="audit line"):
        write()
    if target == "audit":
        with caplog.at_level(logging.WARNING):
            assert cg.get(APPROVAL) == "0"
        assert len(caplog.records) == 1


def test_g1b_all_write_steps_hold_same_lock(cfg, monkeypatch):
    for name in ("_recover", "_read_document", "_append_audit", "atomic_write_text"):
        def checked(*args, original=getattr(cg, name), **kwargs):
            with cg.lock_file_path(cfg).open() as probe:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(*args, **kwargs)
        monkeypatch.setattr(cg, name, checked)
    write()
    crash(monkeypatch, False)
    write()


def test_g1b_nonfinite_document_refused_before_audit(cfg):
    cfg.write_text('{"version":1,"values":{},"future":NaN}')
    with pytest.raises(ValueError, match="JSON compliant"):
        write()
    assert not cg.audit_log_path(cfg).exists()
