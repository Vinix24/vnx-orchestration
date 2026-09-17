"""A write the overwrite guard refuses must leave a durable trace (OI-1750).

Measured on PR #1852 (2026-09-17): ``gate_execution_audit.ndjson`` carried
613 ``codex_gate`` lines, 62 ``ci_gate``, 58 ``gemini_review``, 54
``glm_gate``, 38 ``kimi_gate``, 28 ``deepseek_gate`` -- and ZERO lines for a
kimi_gate re-gate whose write bounced off an existing decided record for the
same head. The overwrite guard (:func:`gate_recorder._check_overwrite_guard`)
refused correctly -- that is not the defect and is not touched here. The
defect is that the refusal itself lived only in an in-memory
``write_refused`` flag (:func:`gate_recorder.annotate_refused_write`) or a
raised :class:`gate_recorder.ResultOverwriteRefused` a CLI caller
(kimi_gate.py/glm_gate.py) only prints to stderr -- neither reaches the
ledger a later reader actually looks at, which saw only the PRIOR run's
``kimi_gate execution completed successfully``.

Every scenario below forces a refusal the same way the dispatch's own
measurement did: offer the guard a write for the SAME head an existing
decided (terminal + complete-evidence) record already carries.
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
from gate_recorder import (
    ResultOverwriteRefused,
    record_failure,
    record_terminal_result,
    write_result_guarded,
)

# OI-1618: record_terminal_result requires execution_depth on every call.
# This file is about the write-refusal audit trail, not depth, so every call
# below passes a single non-degenerate depth.
_OK_DEPTH = gate_depth.single_shot_depth(100, False)

_HEAD = "48929cee00000000000000000000000000000a"


def _read_audit_lines(state_dir: Path) -> list:
    audit_path = state_dir / "gate_execution_audit.ndjson"
    if not audit_path.exists():
        return []
    return [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _make_pass(**overrides):
    payload = {
        "gate": "kimi_gate",
        "pr_id": "1852",
        "status": "pass",
        "contract_hash": "dd5ac45f7e84535e",
        "report_path": "/tmp/kimi-report.md",
        "dispatch_id": "kimi-gate-pr1852-1",
        "commit_sha": _HEAD,
    }
    payload.update(overrides)
    return payload


class TestRecordTerminalResultRefusalIsAudited:
    """The measured path: kimi_gate.py/glm_gate.py -> record_terminal_result."""

    def test_refused_write_lands_one_audit_line_with_gate_pr_head_and_reason(self, tmp_path):
        state_dir = tmp_path / "state"
        results_dir = state_dir / "review_gates" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        out = results_dir / "pr-1852-kimi_gate.json"

        pass_payload = _make_pass()
        record_terminal_result(
            gate="kimi_gate", pr_id="1852", result_path=out, payload=pass_payload,
            execution_depth=_OK_DEPTH,
        )
        # A clean, unrefused write must not itself add a ledger line.
        assert _read_audit_lines(state_dir) == []

        # Force the refusal exactly as the dispatch measured it: a re-run for
        # the SAME head, non-terminal (a transient provider outage on retry).
        outage_payload = {
            "gate": "kimi_gate", "pr_id": "1852", "status": "unavailable",
            "contract_hash": "", "report_path": "", "reason": "dispatch_error",
            "commit_sha": _HEAD,
        }
        with pytest.raises(ResultOverwriteRefused):
            record_terminal_result(
                gate="kimi_gate", pr_id="1852", result_path=out, payload=outage_payload,
                execution_depth=_OK_DEPTH,
            )

        # The guard's own behavior is unchanged: the decided pass still holds.
        assert json.loads(out.read_text(encoding="utf-8"))["status"] == "pass"

        lines = _read_audit_lines(state_dir)
        assert len(lines) == 1, f"expected exactly one durable audit line, got {lines!r}"
        record = lines[0]
        assert record["event_type"] == "gate_skip_rationale"
        assert record["gate"] == "kimi_gate"
        assert record["pr_id"] == "1852"
        assert record["commit_sha"] == _HEAD
        assert record["reason"] == "gate_result_write_refused"
        assert "unavailable" in record["reason_detail"]

    def test_two_refusals_append_two_lines_not_one_overwritten(self, tmp_path):
        state_dir = tmp_path / "state"
        results_dir = state_dir / "review_gates" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        out = results_dir / "pr-1852-kimi_gate.json"

        record_terminal_result(
            gate="kimi_gate", pr_id="1852", result_path=out, payload=_make_pass(),
            execution_depth=_OK_DEPTH,
        )
        outage = {
            "gate": "kimi_gate", "pr_id": "1852", "status": "unavailable",
            "contract_hash": "", "report_path": "", "reason": "dispatch_error",
            "commit_sha": _HEAD,
        }
        for _ in range(2):
            with pytest.raises(ResultOverwriteRefused):
                record_terminal_result(
                    gate="kimi_gate", pr_id="1852", result_path=out, payload=outage,
                    execution_depth=_OK_DEPTH,
                )

        lines = _read_audit_lines(state_dir)
        assert len(lines) == 2
        assert all(r["reason"] == "gate_result_write_refused" for r in lines)

    def test_flat_result_path_with_no_review_gates_ancestor_is_not_a_crash(self, tmp_path):
        """A test fixture (or a future caller) that writes straight into a
        bare tmp dir has no state tree to log into. Must degrade to "no
        audit line", never guess a path and never raise."""
        out = tmp_path / "pr-1852-kimi_gate.json"
        record_terminal_result(
            gate="kimi_gate", pr_id="1852", result_path=out, payload=_make_pass(),
            execution_depth=_OK_DEPTH,
        )
        outage = {
            "gate": "kimi_gate", "pr_id": "1852", "status": "unavailable",
            "contract_hash": "", "report_path": "", "reason": "dispatch_error",
            "commit_sha": _HEAD,
        }
        with pytest.raises(ResultOverwriteRefused):
            record_terminal_result(
                gate="kimi_gate", pr_id="1852", result_path=out, payload=outage,
                execution_depth=_OK_DEPTH,
            )
        # No ancestor of tmp_path may have gained a ledger file.
        for parent in [tmp_path, *tmp_path.parents]:
            assert not (parent / "gate_execution_audit.ndjson").exists()


class TestWriteResultGuardedRefusalIsAudited:
    """The shared primitive record_not_executable/record_failure route through."""

    def test_refused_write_guarded_call_lands_an_audit_line(self, tmp_path):
        state_dir = tmp_path / "state"
        results_dir = state_dir / "review_gates" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        out = results_dir / "pr-1852-glm_gate.json"
        existing = {
            "gate": "glm_gate", "pr_id": "1852", "status": "pass",
            "contract_hash": "abc123", "report_path": "/tmp/glm.md",
            "dispatch_id": "glm-gate-pr1852-1", "commit_sha": _HEAD,
        }
        out.write_text(json.dumps(existing), encoding="utf-8")

        payload, written = write_result_guarded(
            out,
            {
                "gate": "glm_gate", "pr_id": "1852", "status": "unavailable",
                "contract_hash": "", "report_path": "", "commit_sha": _HEAD,
            },
            gate="glm_gate", pr_ref="1852",
        )

        assert written is False
        assert payload == existing

        lines = _read_audit_lines(state_dir)
        assert len(lines) == 1
        assert lines[0]["gate"] == "glm_gate"
        assert lines[0]["pr_id"] == "1852"
        assert lines[0]["commit_sha"] == _HEAD
        assert lines[0]["reason"] == "gate_result_write_refused"


class TestRecordFailureRefusalIsAudited:
    """gate_runner.py / gate_artifacts.materialize_artifacts failure branches."""

    @pytest.fixture
    def env(self, tmp_path):
        state_dir = tmp_path / "state"
        requests_dir = state_dir / "review_gates" / "requests"
        results_dir = state_dir / "review_gates" / "results"
        requests_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)
        return {"state_dir": state_dir, "requests_dir": requests_dir, "results_dir": results_dir}

    def test_refused_failure_write_lands_an_audit_line(self, env):
        """Live fixture shape (2026-08-26/2026-09-10): a completed verdict
        held the slot, a re-gate on the SAME head hit a provider outage,
        gate_runner._run_subprocess_path -> record_failure classified it as
        an execution failure (non-terminal 'unavailable') and the guard
        refused to erase the completed verdict."""
        rf = env["results_dir"] / "pr-1852-codex_gate.json"
        completed_payload = {
            "gate": "codex_gate", "pr_id": "", "pr_number": 1852,
            "status": "completed", "contract_hash": "466cd2ca75d7a7fb",
            "report_path": "/tmp/codex-report.md", "commit_sha": _HEAD,
            "blocking_findings": [], "dispatch_id": "codex-gate-pr1852-1",
        }
        rf.write_text(json.dumps(completed_payload), encoding="utf-8")

        result = record_failure(
            gate="codex_gate", pr_number=1852, pr_id="",
            result={
                "reason": "exit_nonzero",
                "reason_detail": "Subprocess exited with code 1: usage limit",
                "duration_seconds": 42.0, "partial_output_lines": 12, "runner_pid": 1,
            },
            request_payload={
                "gate": "codex_gate", "pr_number": 1852,
                "commit_sha": _HEAD,
                "dispatch_id": "codex-gate-pr1852-2",
            },
            requests_dir=env["requests_dir"], results_dir=env["results_dir"],
        )

        assert result.get("write_refused") is True
        assert json.loads(rf.read_text(encoding="utf-8"))["status"] == "completed"

        lines = _read_audit_lines(env["state_dir"])
        assert len(lines) == 1, f"expected exactly one durable audit line, got {lines!r}"
        record = lines[0]
        assert record["gate"] == "codex_gate"
        assert record["pr_id"] == "1852"
        assert record["commit_sha"] == _HEAD
        assert record["reason"] == "gate_result_write_refused"
