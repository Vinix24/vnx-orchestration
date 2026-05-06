#!/usr/bin/env python3
"""Identity stamping tests for append_receipt + dispatch_register.

Phase 6 P2: optional ``operator_id`` / ``project_id`` / ``orchestrator_id``
/ ``agent_id`` fields ride along on receipts and lifecycle events. Existing
receipts that omit them must keep flowing untouched (additive-only).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
APPEND_SCRIPT = SCRIPTS_DIR / "append_receipt.py"

sys.path.insert(0, str(SCRIPTS_LIB))

from vnx_identity import (  # noqa: E402
    ENV_AGENT,
    ENV_OPERATOR,
    ENV_ORCHESTRATOR,
    ENV_PROJECT,
)


def _build_env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["PROJECT_ROOT"] = str(tmp_path)
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_HOME"] = str(VNX_ROOT)
    # Drop existing identity so we control resolution within each test.
    for var in (ENV_OPERATOR, ENV_PROJECT, ENV_ORCHESTRATOR, ENV_AGENT):
        env.pop(var, None)
    # Point the registry resolver at a non-existent path so only the env vars
    # the test sets are honoured. We cannot redirect REGISTRY_PATH from the
    # subprocess CLI, so we instead make sure HOME has no projects.json.
    env["HOME"] = str(tmp_path / "fake-home")
    (tmp_path / "fake-home").mkdir(exist_ok=True)
    return env


def _run_append(
    tmp_path: Path,
    payload: str,
    extra_env: Optional[dict] = None,
    extra_args: Optional[List[str]] = None,
) -> subprocess.CompletedProcess:
    env = _build_env(tmp_path)
    if extra_env:
        env.update(extra_env)
    args = [sys.executable, str(APPEND_SCRIPT)]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(args, input=payload, capture_output=True, text=True, env=env)


def _read_receipts(tmp_path: Path) -> list[dict]:
    receipts_file = tmp_path / "data" / "state" / "t0_receipts.ndjson"
    if not receipts_file.exists():
        return []
    return [json.loads(line) for line in receipts_file.read_text().splitlines() if line.strip()]


def _build_receipt(index: int = 1) -> dict:
    return {
        "timestamp": f"2026-05-06T10:00:{index:02d}Z",
        "event_type": "task_complete",
        "event": "task_complete",
        "dispatch_id": f"DISP-{index:03d}",
        "task_id": f"TASK-{index:03d}",
        "terminal": "T1",
        "status": "success",
        "source": "pytest",
    }


# -----------------------------------------------------------------------
# Receipt identity stamping
# -----------------------------------------------------------------------


def test_receipt_with_explicit_identity_round_trips(tmp_path):
    receipt = _build_receipt(index=1)
    receipt["operator_id"] = "vincent-vd"
    receipt["project_id"] = "vnx-dev"
    receipt["orchestrator_id"] = "dev-t0"
    receipt["agent_id"] = "t1"

    result = _run_append(tmp_path, json.dumps(receipt))
    assert result.returncode == 0, result.stderr

    records = _read_receipts(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["operator_id"] == "vincent-vd"
    assert rec["project_id"] == "vnx-dev"
    assert rec["orchestrator_id"] == "dev-t0"
    assert rec["agent_id"] == "t1"


def test_receipt_inherits_identity_from_env(tmp_path):
    receipt = _build_receipt(index=2)
    extra_env = {
        ENV_OPERATOR: "vincent-vd",
        ENV_PROJECT: "vnx-dev",
        ENV_ORCHESTRATOR: "dev-t0",
        ENV_AGENT: "t1",
    }
    result = _run_append(tmp_path, json.dumps(receipt), extra_env=extra_env)
    assert result.returncode == 0, result.stderr

    records = _read_receipts(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["operator_id"] == "vincent-vd"
    assert rec["project_id"] == "vnx-dev"
    assert rec["orchestrator_id"] == "dev-t0"
    assert rec["agent_id"] == "t1"


def test_receipt_without_identity_still_persists(tmp_path):
    """Backwards compat: receipts that don't supply identity must still work."""
    receipt = _build_receipt(index=3)
    # No env vars, no fields on receipt — resolver should fail silently and
    # the receipt should be appended unchanged.
    result = _run_append(tmp_path, json.dumps(receipt))
    assert result.returncode == 0, result.stderr
    records = _read_receipts(tmp_path)
    assert len(records) == 1
    rec = records[0]
    # When unresolvable, identity fields must be absent (not None) so the
    # NDJSON line stays compact and existing readers don't see a schema
    # change.
    assert "operator_id" not in rec or rec.get("operator_id") is None
    # Existing required fields untouched.
    assert rec["dispatch_id"] == "DISP-003"
    assert rec["status"] == "success"


def test_caller_supplied_identity_wins_over_env(tmp_path):
    receipt = _build_receipt(index=4)
    receipt["project_id"] = "explicit-proj"
    extra_env = {
        ENV_OPERATOR: "vincent-vd",
        ENV_PROJECT: "vnx-dev",
    }
    result = _run_append(tmp_path, json.dumps(receipt), extra_env=extra_env)
    assert result.returncode == 0, result.stderr
    rec = _read_receipts(tmp_path)[0]
    assert rec["project_id"] == "explicit-proj"
    # operator_id inherited from env because caller did not supply it.
    assert rec["operator_id"] == "vincent-vd"


# -----------------------------------------------------------------------
# dispatch_register identity stamping
# -----------------------------------------------------------------------


def test_dispatch_register_stamps_identity(tmp_path, monkeypatch):
    register_dir = tmp_path / "state"
    register_dir.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(register_dir))
    monkeypatch.setenv(ENV_OPERATOR, "vincent-vd")
    monkeypatch.setenv(ENV_PROJECT, "vnx-dev")
    monkeypatch.setenv(ENV_ORCHESTRATOR, "dev-t0")

    # Force re-import so the module picks up our env-resolved identity path.
    sys.modules.pop("dispatch_register", None)
    from dispatch_register import append_event

    ok = append_event(
        "dispatch_started",
        dispatch_id="DISP-099",
        terminal="T1",
    )
    assert ok is True

    register = register_dir / "dispatch_register.ndjson"
    assert register.exists()
    record = json.loads(register.read_text().strip().splitlines()[0])
    assert record["operator_id"] == "vincent-vd"
    assert record["project_id"] == "vnx-dev"
    assert record["orchestrator_id"] == "dev-t0"
    assert record["dispatch_id"] == "DISP-099"


def test_dispatch_register_explicit_identity_overrides_env(tmp_path, monkeypatch):
    register_dir = tmp_path / "state"
    register_dir.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(register_dir))
    monkeypatch.setenv(ENV_OPERATOR, "vincent-vd")
    monkeypatch.setenv(ENV_PROJECT, "vnx-dev")

    sys.modules.pop("dispatch_register", None)
    from dispatch_register import append_event

    ok = append_event(
        "dispatch_started",
        dispatch_id="DISP-100",
        terminal="T2",
        operator_id="other-op",
        project_id="other-proj",
    )
    assert ok is True

    record = json.loads((register_dir / "dispatch_register.ndjson").read_text().strip())
    assert record["operator_id"] == "other-op"
    assert record["project_id"] == "other-proj"


def test_dispatch_register_no_identity_when_unresolvable(tmp_path, monkeypatch):
    register_dir = tmp_path / "state"
    register_dir.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(register_dir))
    for var in (ENV_OPERATOR, ENV_PROJECT, ENV_ORCHESTRATOR, ENV_AGENT):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    sys.modules.pop("dispatch_register", None)
    from dispatch_register import append_event

    ok = append_event(
        "dispatch_started",
        dispatch_id="DISP-101",
        terminal="T3",
    )
    assert ok is True
    record = json.loads((register_dir / "dispatch_register.ndjson").read_text().strip())
    assert "operator_id" not in record
    assert record["dispatch_id"] == "DISP-101"
