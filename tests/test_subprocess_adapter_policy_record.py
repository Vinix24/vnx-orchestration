"""The seam writes a policy material record and exports the digest (permissions P3).

No real worker starts: Popen is patched, the data dir is tmp_path.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import subprocess_adapter
from subprocess_adapter import SubprocessAdapter

DIGEST_ENV = "VNX_PERMISSION_POLICY_DIGEST"


class _Profile:
    policy_digest = "d" * 64


@pytest.fixture
def seam(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VNX_ENFORCE_WORKER_PERMISSIONS", raising=False)
    calls = {}

    def fake_popen(cmd, **kwargs):
        calls["cmd"], calls["env"] = list(cmd), kwargs.get("env")
        proc = MagicMock()
        proc.pid = 4242
        proc.stdout = io.BytesIO(b"")
        return proc

    def deliver(role="backend-developer", **kwargs):
        with patch("subprocess_adapter.subprocess.Popen", side_effect=fake_popen), \
                patch("subprocess_adapter.os.setsid", create=True):
            adapter = SubprocessAdapter()
            adapter._get_event_store = lambda: None
            return adapter.deliver("T1", "disp-1", instruction="do", model="sonnet", role=role, **kwargs)

    def record():
        path = tmp_path / "state" / "permission_records" / "T1_disp-1.json"
        return json.loads(path.read_text()) if path.exists() else None

    return calls, deliver, record


def _patch_scope(monkeypatch, profile):
    """Resolve to *profile* through the seam's own collaborators (names that exist on main)."""
    monkeypatch.setattr(subprocess_adapter, "worker_scoped_enabled", lambda: True)
    monkeypatch.setattr(subprocess_adapter, "worker_permission_enforcement_enabled", lambda: False)
    monkeypatch.setattr(subprocess_adapter, "resolve_worker_profile", lambda role: profile)
    monkeypatch.setattr(subprocess_adapter, "build_claude_scope_args",
                        lambda p, requires_mcp=False: ["--permission-mode", "acceptEdits"])


def test_deliver_command_carries_policy_digest(seam, monkeypatch):
    calls, deliver, record = seam
    _patch_scope(monkeypatch, _Profile())
    assert deliver(lane="glm_harness").success
    assert calls["env"][DIGEST_ENV] == "d" * 64
    rec = record()
    assert rec["policy_digest"] == "d" * 64 and rec["lane"] == "glm_harness"
    assert rec["flags"] == ["--permission-mode", "acceptEdits"]
    assert rec["capabilities"]["hooks"]["level"] == "unsupported"


def test_unknown_harness_is_unsupported(seam, monkeypatch):
    calls, deliver, record = seam
    _patch_scope(monkeypatch, _Profile())
    deliver(lane="gemini")
    rec = record()
    assert rec["lane_known"] is False
    assert {c["level"] for c in rec["capabilities"].values()} == {"unsupported"}
    deliver()
    assert record()["lane"] == "unspecified"


def test_kimi_tools_deny_is_unsupported(seam, monkeypatch):
    _, deliver, record = seam
    _patch_scope(monkeypatch, _Profile())
    deliver(lane="kimi")
    assert record()["capabilities"]["tools_deny"]["level"] == "unsupported"
    assert "tools_deny" in record()["unsupported"]


def test_no_resolved_policy_exports_no_digest(seam, monkeypatch):
    calls, deliver, record = seam
    _patch_scope(monkeypatch, _Profile())
    monkeypatch.setattr(subprocess_adapter, "worker_scoped_enabled", lambda: False)
    deliver(lane="claude_headless")
    assert DIGEST_ENV not in (calls["env"] or {})
    assert record()["policy_state"] == "unresolved"
    deliver(lane="claude_headless", scope_args=["--x"])
    assert DIGEST_ENV not in (calls["env"] or {}) and record()["flags"] == ["--x"]


def test_enforcement_off_argv_is_byte_identical(seam, monkeypatch):
    """OFF: the argv after the prompt is exactly what _build_worker_scope_args returns."""
    calls, deliver, record = seam
    monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
    monkeypatch.delenv("VNX_WORKER_BLANKET_SKIP", raising=False)
    expected = subprocess_adapter._build_worker_scope_args("backend-developer")
    deliver(lane="claude_headless")
    assert calls["cmd"][calls["cmd"].index("do") + 1:] == expected
    assert record()["flags"] == expected


def test_record_write_failure_does_not_fail_the_delivery(seam, monkeypatch):
    _, deliver, _ = seam
    _patch_scope(monkeypatch, _Profile())
    with patch("atomic_io.atomic_write_json", side_effect=OSError("disk full")):
        assert deliver(lane="codex").success


def test_refused_policy_writes_no_record_and_spawns_nothing(seam, monkeypatch):
    calls, deliver, record = seam

    def boom(role):
        raise subprocess_adapter.PermissionPolicyRefused("nope", "source-malformed")

    _patch_scope(monkeypatch, None)
    monkeypatch.setattr(subprocess_adapter, "resolve_worker_profile", boom)
    result = deliver(lane="claude_headless")
    assert not result.success and "source-malformed" in result.failure_reason
    assert "cmd" not in calls and record() is None


def test_mcp_config_error_has_its_own_slug(monkeypatch):
    """OI-2651 pt.1: resolving the policy and building the MCP scope args are distinct refusals."""
    monkeypatch.setattr(subprocess_adapter, "resolve_worker_profile", lambda role: _Profile())
    monkeypatch.setattr(subprocess_adapter, "worker_scoped_enabled", lambda: True)
    monkeypatch.setattr(subprocess_adapter, "worker_permission_enforcement_enabled", lambda: False)

    def bad_build(profile, requires_mcp=False):
        raise ValueError("bad mcp json")

    monkeypatch.setattr(subprocess_adapter, "build_claude_scope_args", bad_build)
    with pytest.raises(subprocess_adapter.PermissionPolicyRefused) as info:
        subprocess_adapter._build_worker_scope_args("r")
    assert info.value.reason == "scope-args-build-error"

    def bad_resolve(role):
        raise RuntimeError("x")

    monkeypatch.setattr(subprocess_adapter, "resolve_worker_profile", bad_resolve)
    with pytest.raises(subprocess_adapter.PermissionPolicyRefused) as info:
        subprocess_adapter._build_worker_scope_args("r")
    assert info.value.reason == "policy-resolution-error"
