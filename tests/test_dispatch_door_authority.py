#!/usr/bin/env python3
"""Regression tests for the central-store door-authority fix (2026-07-10).

The fleet-wide hard-reject/misroute class had ONE root: the door re-resolved
`project_id`/`data_dir` from ambient CWD/env (hardcoded `vnx-dev`) instead of from
the PHYSICAL location the spec bundle was staged into. In a central install the door's
CWD is the shared engine tree, whose stray `.vnx-project-id=vnx-dev` mis-resolved every
non-vnx-dev consumer.

These tests pin the coherent authority model:
- `stage_spec_bundle` stages into the TARGET project's store (not ambient vnx-dev).
- `_authority_from_spec_path` derives (project_id, data_dir) from that physical location.
- Ambient `VNX_PROJECT_ID` must NEVER override the staged-bundle authority.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for sub in ("scripts/lib", "scripts"):
    p = str(REPO_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_bridge  # noqa: E402
import dispatch_cli  # noqa: E402


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() + the data-home at tmp, and clear data-dir overrides."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    return tmp_path


class TestAuthorityFromSpecPath:
    def test_derives_tenant_from_central_staged_bundle(self, fake_home, monkeypatch):
        pid = "sales-copilot"
        bundle = fake_home / ".vnx-data" / pid / "dispatches" / "pending" / "20260710-x-y"
        bundle.mkdir(parents=True)
        spec_file = bundle / "dispatch-spec.json"
        spec_file.write_text("{}", encoding="utf-8")
        # ambient env points at a DIFFERENT tenant — must be ignored.
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

        got_pid, got_data_dir = dispatch_cli._authority_from_spec_path(spec_file)
        assert got_pid == pid
        assert got_data_dir == fake_home / ".vnx-data" / pid

    def test_returns_none_for_adhoc_spec_outside_layout(self, tmp_path):
        spec_file = tmp_path / "dispatch-spec.json"
        spec_file.write_text("{}", encoding="utf-8")
        assert dispatch_cli._authority_from_spec_path(spec_file) == (None, None)

    def test_returns_none_for_wrong_filename(self, fake_home):
        bundle = fake_home / ".vnx-data" / "p" / "dispatches" / "pending" / "20260710-x-y"
        bundle.mkdir(parents=True)
        other = bundle / "instruction.md"
        other.write_text("x", encoding="utf-8")
        assert dispatch_cli._authority_from_spec_path(other) == (None, None)


class TestStageBundleUsesTargetStore:
    def test_bundle_stages_into_project_store_not_ambient(self, fake_home, monkeypatch):
        # ambient env resolves vnx-dev; the passed project_id must win.
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        spec_file = dispatch_bridge.stage_spec_bundle(
            instruction_text="do a thing",
            dispatch_id="20260710-test-x",
            role="backend-developer",
            target_slot="T1",
            project_id="sales-copilot",
        )
        parents = set(spec_file.parents)
        assert fake_home / ".vnx-data" / "sales-copilot" in parents
        assert fake_home / ".vnx-data" / "vnx-dev" not in parents

    def test_staged_bundle_and_derived_authority_agree(self, fake_home, monkeypatch):
        # End-to-end coherence: what stage_spec_bundle writes, the door re-derives.
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")  # stray/ambient
        spec_file = dispatch_bridge.stage_spec_bundle(
            instruction_text="do a thing",
            dispatch_id="20260710-test-z",
            role="backend-developer",
            target_slot="T1",
            project_id="mission-control",
        )
        got_pid, _ = dispatch_cli._authority_from_spec_path(spec_file)
        assert got_pid == "mission-control"


class TestEndToEndNoHardReject:
    def test_consumer_dispatch_validates_without_env(self, fake_home, monkeypatch):
        """The exact fleet-blocker: a sales-copilot dispatch with NO VNX_PROJECT_ID set,
        while the ambient env would resolve vnx-dev, must pass validate (not project-mismatch).

        Reproduces the door's resolve->load->validate chain end-to-end against a bundle
        physically staged in the consumer's store."""
        from dispatch_spec import Reject, ValidatedSpec  # noqa: PLC0415
        from dispatch_cli import _authority_from_spec_path, _resolve_project_id, load_spec  # noqa: PLC0415
        from dispatch_spec import validate  # noqa: PLC0415

        # ambient env simulates the stray engine-tree resolution.
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        spec_file = dispatch_bridge.stage_spec_bundle(
            instruction_text="ship a real feature end to end",
            dispatch_id="20260710-sc-e2e",
            role="backend-developer",
            target_slot="T1",
            project_id="sales-copilot",
        )

        # The door's authority chain (as in run_dispatch):
        derived_pid, _ = _authority_from_spec_path(spec_file)
        project_id = derived_pid or _resolve_project_id()
        spec = load_spec(spec_file)
        result = validate(spec, project_id=project_id, repo_root=REPO_ROOT)

        assert not isinstance(result, Reject), getattr(result, "reason", result)
        assert isinstance(result, ValidatedSpec)
        assert project_id == "sales-copilot"  # authority came from the store, not env


class TestOperatorPinCrossCheck:
    """codex-gate PR #1093: an operator-pinned tenant/data-root must not be contradicted
    by the staged-bundle authority (restores ADR-007 anti-redirect when a pin exists)."""

    def test_run_dispatch_rejects_project_id_pin_mismatch(self, fake_home, monkeypatch, capsys):
        from dispatch_cli import run_dispatch  # noqa: PLC0415
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        spec_file = dispatch_bridge.stage_spec_bundle(
            instruction_text="x", dispatch_id="20260710-pin-a", role="backend-developer",
            target_slot="T1", project_id="sales-copilot",
        )
        # operator pins a DIFFERENT project than the bundle physically lives in.
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        rc = run_dispatch(spec_file)
        assert rc == 1
        assert "pinned VNX_PROJECT_ID" in capsys.readouterr().err

    def test_run_dispatch_rejects_explicit_data_dir_mismatch(self, fake_home, monkeypatch, capsys):
        from dispatch_cli import run_dispatch  # noqa: PLC0415
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        spec_file = dispatch_bridge.stage_spec_bundle(
            instruction_text="x", dispatch_id="20260710-pin-b", role="backend-developer",
            target_slot="T1", project_id="sales-copilot",
        )
        # operator pins an explicit data root elsewhere than where the bundle lives.
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(fake_home / "elsewhere"))
        rc = run_dispatch(spec_file)
        assert rc == 1
        assert "pinned VNX_DATA_DIR" in capsys.readouterr().err


class TestResolveDataDirOverride:
    def test_project_id_override_beats_env_default(self, fake_home, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        got = dispatch_cli._resolve_data_dir("seocrawler-v2")
        assert got == fake_home / ".vnx-data" / "seocrawler-v2"

    def test_explicit_data_dir_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "explicit"))
        got = dispatch_cli._resolve_data_dir("seocrawler-v2")
        assert got == (tmp_path / "explicit").resolve()
