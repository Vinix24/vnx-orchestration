"""OI-1725: a harness-lane gate must never book a verdict under a builder's
dispatch-id.

Since #1837 (commit 0188b016), gate_runner routes glm_gate/kimi_gate through
the harness-lane strategy. That strategy reused the BUILDER's dispatch-id from
the request payload, so ``plan_gate_panel._read_report`` read the builder's
own unified report as the gate's verdict, and ``materialize_artifacts`` never
stamped ``provider``/``model``. Measured on ``pr-1840-kimi_gate.json`` and
``pr-1841-kimi_gate.json``: builder dispatch ids + ``provider: None`` /
``model: None``; the correct ``pr-1839-glm_gate.json`` carries
``glm-gate-pr1839-1789147884`` + ``provider``/``model`` filled.

The guard is a POSITIVE identity check, not a list of known builder ids: a
harness-lane gate result whose dispatch-id is not gate-eigen
(``<shortname>-gate-pr<N>-<ts>``) is refused loudly, so a NEW form of the same
collision fails too.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_depth
from gate_artifacts import materialize_artifacts
from gate_recorder import (
    gate_dispatch_identity_error,
    harness_lane_dispatch_prefix,
    is_gate_eigen_dispatch_id,
    record_terminal_result,
)

_BUILDER_DISPATCH_ID = "20260911-oi1711-publicatiepad-werkt-twee-keer"
_OK_DEPTH = gate_depth.single_shot_depth(50000, True)


# ---------------------------------------------------------------------------
# 1. The positive identity predicate
# ---------------------------------------------------------------------------


def test_gate_eigen_prefix_strips_the_gate_suffix():
    assert harness_lane_dispatch_prefix("glm_gate") == "glm-gate-pr"
    assert harness_lane_dispatch_prefix("kimi_gate") == "kimi-gate-pr"


def test_is_gate_eigen_accepts_exactly_the_own_shape():
    assert is_gate_eigen_dispatch_id("glm_gate", "glm-gate-pr1839-1789147884")
    assert is_gate_eigen_dispatch_id("kimi_gate", "kimi-gate-pr1840-1789147884")
    # A short timestamp is still gate-eigen (int(time.time()) is the only
    # contract, and test fixtures use "1").
    assert is_gate_eigen_dispatch_id("glm_gate", "glm-gate-pr99-1")


def test_is_gate_eigen_rejects_a_builders_or_foreign_id():
    assert not is_gate_eigen_dispatch_id("kimi_gate", _BUILDER_DISPATCH_ID)
    assert not is_gate_eigen_dispatch_id("glm_gate", _BUILDER_DISPATCH_ID)
    # The wrong gate's own id is also foreign.
    assert not is_gate_eigen_dispatch_id("kimi_gate", "glm-gate-pr1839-1789147884")
    # A non-digit timestamp tail is not the gate-eigen shape.
    assert not is_gate_eigen_dispatch_id("kimi_gate", "kimi-gate-pr1830-runner-refusal")
    assert not is_gate_eigen_dispatch_id("kimi_gate", "")
    assert not is_gate_eigen_dispatch_id("kimi_gate", None)


def test_is_gate_eigen_is_scoped_to_harness_lane_gates():
    # path_binary gates carry the builder's dispatch-id BY DESIGN — never
    # refused here, because their id is the builder's own.
    assert not is_gate_eigen_dispatch_id("codex_gate", "codex-gate-pr42-1")
    assert gate_dispatch_identity_error("codex_gate", _BUILDER_DISPATCH_ID) is None


# ---------------------------------------------------------------------------
# 2. The guard refuses a colliding dispatch-id on both terminal write paths
# ---------------------------------------------------------------------------


def test_materialize_artifacts_refuses_a_builder_dispatch_id(tmp_path):
    """The gate_runner harness-lane path (the actual bug) books ``unavailable``
    with reason ``gate_dispatch_identity_invalid`` instead of a completed
    verdict when handed a builder dispatch-id."""
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    payload = {
        "gate": "kimi_gate",
        "status": "requested",
        "branch": "feature/x",
        "pr_number": 1840,
        "prompt": "Review this diff",
        "report_path": str(reports_dir / "kimi-gate-pr1840.md"),
        "dispatch_id": _BUILDER_DISPATCH_ID,
    }
    result = materialize_artifacts(
        gate="kimi_gate", pr_number=1840, pr_id="",
        stdout="Review complete.\nNo blocking findings.\n",
        request_payload=payload, duration_seconds=1.0,
        requests_dir=requests_dir, results_dir=results_dir,
        reports_dir=reports_dir,
    )

    assert result["status"] == "unavailable"
    assert result["reason"] == "gate_dispatch_identity_invalid"

    saved = json.loads((results_dir / "pr-1840-kimi_gate.json").read_text(encoding="utf-8"))
    assert saved["status"] == "unavailable"
    assert saved["reason"] == "gate_dispatch_identity_invalid"
    assert saved["reason_detail"] != ""
    assert _BUILDER_DISPATCH_ID in saved["reason_detail"]


def test_record_terminal_result_refuses_a_builder_dispatch_id(tmp_path):
    """The standalone glm_gate/kimi_gate write path also refuses loudly."""
    payload = {
        "gate": "kimi_gate",
        "pr_id": "1840",
        "status": "pass",
        "contract_hash": "abc",
        "report_path": "/tmp/r.md",
        "dispatch_id": _BUILDER_DISPATCH_ID,
    }
    with pytest.raises(ValueError, match="gate-eigen"):
        record_terminal_result(
            gate="kimi_gate", pr_id="1840",
            result_path=tmp_path / "pr-1840-kimi_gate.json",
            payload=payload, execution_depth=_OK_DEPTH,
        )
    assert not (tmp_path / "pr-1840-kimi_gate.json").exists(), (
        "refused write must not leave a record behind"
    )


def test_record_terminal_result_still_accepts_a_gate_eigen_id(tmp_path):
    """The guard is positive: a gate-eigen id passes through unchanged."""
    payload = {
        "gate": "kimi_gate",
        "pr_id": "1840",
        "status": "pass",
        "contract_hash": "abc",
        "report_path": "/tmp/r.md",
        "provider": "kimi",
        "model": "kimi-k3",
        "dispatch_id": "kimi-gate-pr1840-1789147884",
    }
    out = tmp_path / "pr-1840-kimi_gate.json"
    record_terminal_result(
        gate="kimi_gate", pr_id="1840", result_path=out,
        payload=payload, execution_depth=_OK_DEPTH,
    )
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "pass"
