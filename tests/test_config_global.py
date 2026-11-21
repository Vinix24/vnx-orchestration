"""scripts/lib/config_global.py (ADR-087 decision 1). Every test pins VNX_GLOBAL_CONFIG_FILE to tmp_path."""

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
import config_global as cg
from config_registry import CONFIG_REGISTRY

PLAIN, PLAIN2 = [k for k, e in CONFIG_REGISTRY.items() if e.writable_from_ui and not e.requires_approval][:2]
APPROVAL = next(k for k, e in CONFIG_REGISTRY.items() if e.writable_from_ui and e.requires_approval)
LOCKED = next(k for k, e in CONFIG_REGISTRY.items() if not e.writable_from_ui)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_GLOBAL_CONFIG_FILE", str(tmp_path / "g" / "config.json"))
    (tmp_path / "g").mkdir()
    return tmp_path / "g" / "config.json"


def _audit(path):
    return [json.loads(line) for line in cg.audit_log_path(path).read_text().splitlines()]


def _plant(path, content):
    path.write_text(content if isinstance(content, str) else json.dumps(content))


def test_missing_file_then_round_trip_with_audit(cfg):
    assert (cg.config_file_path(), cg.lock_file_path()) == (cfg, cfg.parent / "config.json.lock")
    assert cg.load() == {} and cg.get(PLAIN) is None
    cg.set_global(PLAIN, "1", actor="op", reason="try it")
    assert cg.get(PLAIN) == "1"
    assert cg.get_with_meta(PLAIN)["reason"] == "try it"
    cg.unset_global(PLAIN, actor="op", reason="back")
    assert cg.get(PLAIN) is None
    first, second = _audit(cfg)
    assert {"ts", "actor", "cwd", "pid", "argv", "approval_id"} <= set(first)
    assert (first["operation"], first["key"], first["old"], first["new"]["value"]) == ("set", PLAIN, None, "1")
    assert (second["operation"], second["old"]["value"], second["new"]) == ("unset", "1", None)


def test_failed_replace_leaves_audit_then_abort_and_file_unchanged(cfg, monkeypatch):
    cg.set_global(PLAIN, "1", actor="op", reason="first")
    before = cfg.read_text()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(cg, "atomic_write_text", boom)
    with pytest.raises(OSError, match="disk full"):
        cg.set_global(PLAIN, "0", actor="op", reason="second")
    assert cfg.read_text() == before
    records = _audit(cfg)
    assert [r["operation"] for r in records] == ["set", "set", "abort"]
    assert records[1]["new"]["value"] == "0" and "disk full" in records[2]["error"]


@pytest.mark.parametrize("content", [
    "{not json", [1], {"values": {}}, {"version": "1", "values": {}}, {"version": 2, "values": {}},
    {"version": 1, "values": []}, {"version": 1, "values": {PLAIN: "1"}}, {"version": 1, "values": {PLAIN: {}}},
])
def test_unreadable_file_raises_and_is_never_overwritten(cfg, content):
    _plant(cfg, content)
    before = cfg.read_text()
    with pytest.raises(cg.GlobalConfigUnreadable, match=str(cfg)):
        cg.get(PLAIN)
    with pytest.raises(cg.GlobalConfigUnreadable, match=str(cg.audit_log_path(cfg))):
        cg.set_global(PLAIN2, "1", actor="op", reason="r")
    assert cfg.read_text() == before and not cg.audit_log_path(cfg).exists()


def test_approval_key_needs_approval_id_for_set_and_unset(cfg):
    for call in (lambda: cg.set_global(APPROVAL, "1", actor="op", reason="r"),
                 lambda: cg.unset_global(APPROVAL, actor="op", reason="r")):
        with pytest.raises(PermissionError, match="approval_id"):
            call()
    cg.set_global(APPROVAL, "1", actor="op", reason="r", approval_id="A-1")
    cg.unset_global(APPROVAL, actor="op", reason="r", approval_id="A-2")
    assert [r["approval_id"] for r in _audit(cfg)] == ["A-1", "A-2"]


@pytest.mark.parametrize("key, value, reason, error", [
    ("VNX_NOT_A_KEY", "1", "r", ValueError), (LOCKED, "1", "r", PermissionError),
    (PLAIN, "1", "", ValueError), (PLAIN, "1", "  ", ValueError), (PLAIN, "1", None, ValueError),
    (PLAIN, 1, "r", TypeError),
])
def test_refusals_write_nothing(cfg, key, value, reason, error):
    with pytest.raises(error):
        cg.set_global(key, value, actor="op", reason=reason)
    assert not cfg.exists() and not cg.audit_log_path(cfg).exists()


def test_unknown_field_and_key_survive_a_write(cfg):
    future = {"value": "x", "extra": [1]}
    _plant(cfg, {"version": 1, "future": {"a": 1}, "values": {"VNX_FROM_LATER": future}})
    assert cg.load() == {}
    cg.set_global(PLAIN, "1", actor="op", reason="r")
    document = json.loads(cfg.read_text())
    assert (document["future"], document["values"]["VNX_FROM_LATER"]) == ({"a": 1}, future)


def test_concurrent_writers_lose_no_update(cfg, monkeypatch):
    real_read, barrier = cg._read_document, threading.Barrier(2, timeout=1)

    def slow_read(path):
        try:
            barrier.wait()  # without the lock both writers would read the same old document here
        except threading.BrokenBarrierError:
            pass  # expected: the lock serialises the readers, so the barrier never fills
        return real_read(path)

    monkeypatch.setattr(cg, "_read_document", slow_read)
    threads = [threading.Thread(target=cg.set_global, args=(k, "1"), kwargs={"actor": "op", "reason": "r"})
               for k in (PLAIN, PLAIN2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(json.loads(cfg.read_text())["values"]) == {PLAIN, PLAIN2} and len(_audit(cfg)) == 2
