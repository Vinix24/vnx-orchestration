"""kimi writes its layer-0 policy record and keeps its flags (permissions P6).

No real kimi starts: tmp_path is the data dir and the config path is a stub.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from provider_spawns import kimi_spawn

DIGEST_ENV = "VNX_PERMISSION_POLICY_DIGEST"
ORIGIN_MAIN_CMD = ["kimi", "--print", "--output-format", "stream-json", "--yolo",
                   "-p", "do it", "-m", "kimi-k3", "-w", "/w", "--config-file", "/c.json"]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(DIGEST_ENV, raising=False)


def _build(extra_env=None, dispatch_id="d-1"):
    return kimi_spawn._build_recorded_kimi_cmd(
        "do it", "kimi-k3", "/w", Path("/c.json"), extra_env, dispatch_id, "T1",
    )


def _record(tmp_path, dispatch_id="d-1"):
    path = tmp_path / "state" / "permission_records" / f"T1_{dispatch_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_kimi_record_still_has_yolo(tmp_path):
    cmd = _build({DIGEST_ENV: "a" * 64})
    assert "--yolo" in cmd
    rec = _record(tmp_path)
    assert rec["lane"] == "kimi" and rec["policy_digest"] == "a" * 64
    assert rec["layer1"]["yolo"] is True
    assert rec["layer1"]["hook"] == "PreToolUse ^Shell$"
    assert rec["layer1"]["tools"]["deny"] == "unsupported"
    assert rec["layer1"]["filesystem"]["filesystem_write"] == "unsupported"
    assert rec["layer0"] == {"git_boundary": "worktree", "sandbox": "none", "cwd": "/w"}
    assert rec["capabilities"]["hooks"]["level"] == "degraded"


def test_record_holds_no_prompt_text(tmp_path):
    _build()
    assert "do it" not in json.dumps(_record(tmp_path))


def test_cmd_byte_identical_to_origin_main():
    assert _build() == ORIGIN_MAIN_CMD
    assert kimi_spawn._build_kimi_cmd("do it", "kimi-k3", "/w", Path("/c.json")) == ORIGIN_MAIN_CMD


def test_policy_absent_when_spec_has_no_digest(tmp_path):
    _build()
    rec = _record(tmp_path)
    assert rec["policy_state"] == "policy_absent" and rec["policy_digest"] is None


def test_digest_from_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv(DIGEST_ENV, "b" * 64)
    _build()
    assert _record(tmp_path)["policy_state"] == "resolved"


@pytest.mark.parametrize("bad", ["", "   "])
def test_blank_digest_is_absent(tmp_path, bad):
    _build({DIGEST_ENV: bad})
    assert _record(tmp_path)["policy_state"] == "policy_absent"


def test_unknown_lane_raises(monkeypatch):
    import permission_harness_map as phm

    monkeypatch.setattr(phm, "_TABLE", {})
    with pytest.raises(ValueError):
        kimi_spawn._kimi_policy_record(list(ORIGIN_MAIN_CMD), "/w", "policy_absent", "d-1", "T1")
    with pytest.raises(KeyError):
        phm.capability_for("kimi", "not_a_dimension")


def test_failed_write_is_a_warning_and_cmd_still_builds(monkeypatch, caplog):
    import atomic_io

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io, "atomic_write_json", boom)
    with caplog.at_level(logging.WARNING, logger=kimi_spawn.logger.name):
        cmd = _build(dispatch_id="d-2")
    assert cmd == ORIGIN_MAIN_CMD
    assert any("policy record not written for d-2" in r.getMessage() for r in caplog.records)


def test_enforcement_stays_off_and_untouched(monkeypatch):
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")
    assert _build() == ORIGIN_MAIN_CMD
