"""codex writes its layer-0 policy record and keeps its flags (permissions P6).

No real codex starts: _launch_codex_proc is patched, tmp_path is the data dir.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from provider_spawns import codex_spawn

DIGEST_ENV = "VNX_PERMISSION_POLICY_DIGEST"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(DIGEST_ENV, raising=False)
    monkeypatch.delenv("VNX_CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(codex_spawn, "_resolve_codex_model", lambda m: m or "gpt-test")


@pytest.fixture
def launch(monkeypatch):
    def run(extra_env=None, dispatch_id="d-1", started=True):
        proc = MagicMock() if started else None
        monkeypatch.setattr(codex_spawn, "_launch_codex_proc", lambda *a, **k: (proc, None))
        return codex_spawn._launch_recorded_codex_proc(
            "p", "gpt-test", extra_env, "/w", dispatch_id, "T2")
    return run


def _record(tmp_path, dispatch_id="d-1"):
    path = tmp_path / "state" / "permission_records" / f"T2_{dispatch_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_codex_record_reads_sandbox_from_env(tmp_path, monkeypatch, launch):
    monkeypatch.setenv("VNX_CODEX_SANDBOX", "workspace-write")
    launch({DIGEST_ENV: "a" * 64})
    rec = _record(tmp_path)
    assert rec["lane"] == "codex" and rec["policy_digest"] == "a" * 64
    assert rec["layer1"]["fs"] == "workspace-write"
    assert rec["layer0"] == {"git_boundary": "worktree", "sandbox": "workspace-write", "cwd": "/w"}
    assert rec["layer1"]["tools"] == {"tools_allow": "unsupported", "tools_deny": "unsupported"}
    assert rec["layer1"]["mcp"] == "unsupported"
    assert "--sandbox" in rec["flags"] and "workspace-write" in rec["flags"]


@pytest.mark.parametrize("value", [None, "", "yolo-mode", "WORKSPACE-WRITE"])
def test_codex_fs_unsupported_when_unset_or_unknown(tmp_path, monkeypatch, value, launch):
    if value is not None:
        monkeypatch.setenv("VNX_CODEX_SANDBOX", value)
    launch()
    rec = _record(tmp_path)
    assert rec["layer1"]["fs"] == "unsupported"
    assert rec["layer0"]["sandbox"] == "bypass"
    assert "--dangerously-bypass-approvals-and-sandbox" in rec["flags"]


@pytest.mark.parametrize("value,expected", [
    (None, ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--model", "gpt-test"]),
    ("bogus", ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--model", "gpt-test"]),
    ("read-only", ["codex", "exec", "--json", "--sandbox", "read-only", "--model", "gpt-test"]),
    ("workspace-write", ["codex", "exec", "--json", "--sandbox", "workspace-write", "--model", "gpt-test"]),
    ("danger-full-access", ["codex", "exec", "--json", "--sandbox", "danger-full-access", "--model", "gpt-test"]),
])
def test_cmd_byte_identical_to_origin_main(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("VNX_CODEX_SANDBOX", value)
    assert codex_spawn._build_cmd("gpt-test") == expected


def test_policy_absent_when_spec_has_no_digest(tmp_path, launch):
    launch()
    rec = _record(tmp_path)
    assert rec["policy_state"] == "policy_absent" and rec["policy_digest"] is None


def test_no_record_when_codex_did_not_start(tmp_path, launch):
    launch(started=False)
    assert _record(tmp_path) is None


def test_unknown_lane_raises(monkeypatch):
    import permission_harness_map as phm

    monkeypatch.setattr(phm, "_TABLE", {})
    with pytest.raises(ValueError):
        codex_spawn._codex_policy_record(["codex", "exec", "--json"], "/w", "policy_absent", "d-1", "T2")
    with pytest.raises(KeyError):
        phm.capability_for("codex", "not_a_dimension")


def test_failed_write_is_a_warning_and_spawn_still_launches(monkeypatch, launch, caplog):
    import atomic_io

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io, "atomic_write_json", boom)
    with caplog.at_level(logging.WARNING, logger=codex_spawn.logger.name):
        proc, early = launch(dispatch_id="d-2")
    assert proc is not None and early is None
    assert any("policy record not written for d-2" in r.getMessage() for r in caplog.records)
