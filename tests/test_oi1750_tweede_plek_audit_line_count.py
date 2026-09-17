"""OI-1750, second finding (glm_gate review, info severity, on PR #1862 --
the "tweede plek" this dispatch fixes forward).

``gate_recorder.record_not_executable`` already opts out of
:func:`gate_recorder.write_result_guarded`'s own audit line
(``audit_refusal=False``) because it unconditionally writes its own
skip-rationale line right after -- without the opt-out a guard refusal
would land TWO lines in ``gate_execution_audit.ndjson`` for ONE event
(OI-1707). That reasoning is documented on ``gate_recorder.py:1631-1634``.

The SAME shape existed, unfixed, on three more production call sites in
``gate_request_handler.py``: ``_mark_gate_unavailable`` (shared by
``_request_codex``/``_request_gemini``/``_request_kimi``/
``_request_ci_gate``), ``_request_glm`` and ``_request_deepseek``. Each
calls ``self._write_not_executable_result`` (which itself routes through
``write_result_guarded``) and then unconditionally calls
``self._write_skip_rationale`` right after, exactly the record_not_executable
shape -- but without the opt-out, so a guard refusal on any of these three
paths landed two audit lines: the guard's own ``gate_result_write_refused``
line (WITH ``commit_sha``, since ``_write_not_executable_result``'s payload
always carries it) and the caller's own skip-rationale line under its own
reason (e.g. ``gate_runner_missing``) -- WITHOUT ``commit_sha``, since
``gate_report_generator.GateReportGeneratorMixin._write_skip_rationale``
never threaded it through to ``gate_recorder.write_skip_rationale``.

This file pins the COUNT for all four paths, not just "a line exists" --
"a line exists" is also satisfied by the pre-fix one-opted-out/three-not
state. Grep sweep performed for this fix-forward (see the PR body) found
exactly these four production callers of ``write_result_guarded`` that ALSO
write their own unconditional skip-rationale afterward. Every OTHER
``write_result_guarded`` caller in this tree -- ``record_failure``,
``record_terminal_result``'s own raise+audit, ``gate_artifacts``,
``gate_executor``'s CI-gate result writer, and the takeover-annotation
writer around ``gate_request_handler.py``'s ``_stamp_takeover_annotations``
(~line 738) -- does NOT pair a guarded write with its own unconditional
skip-rationale call, so each is correctly left at the ``audit_refusal=True``
default (exactly one line, already covered by
``tests/test_oi1750_write_refusal_audit_trail.py``).
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

from gate_recorder import record_not_executable

_HEAD = "48929cee00000000000000000000000000000a"
_BRANCH = "dispatch/20260917-oi1750-weigering-audit"


def _read_audit_lines(state_dir: Path) -> list:
    audit_path = state_dir / "gate_execution_audit.ndjson"
    if not audit_path.exists():
        return []
    return [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _decided_pass(gate: str, pr_id: str, commit_sha: str, *, pr_number=None) -> dict:
    """A terminal, evidenced verdict -- the ONLY shape the overwrite guard
    refuses a runner-refusal not_executable write against (OI-1707)."""
    return {
        "gate": gate,
        "pr_id": pr_id,
        "pr_number": pr_number,
        "status": "pass",
        "contract_hash": "dd5ac45f7e84535e",
        "report_path": "/tmp/report.md",
        "dispatch_id": f"{gate}-audit-count-1",
        "commit_sha": commit_sha,
    }


class TestRecordNotExecutableAuditCount:
    """Path 1/4: gate_recorder.record_not_executable. Already opted out
    (audit_refusal=False) before this dispatch -- this is the baseline the
    other three paths are brought into line with, not new behavior."""

    def test_refused_write_lands_exactly_one_line_with_head(self, tmp_path):
        state_dir = tmp_path / "state"
        requests_dir = state_dir / "review_gates" / "requests"
        results_dir = state_dir / "review_gates" / "results"
        requests_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)

        rf = results_dir / "pr-9001-kimi_gate.json"
        rf.write_text(
            json.dumps(_decided_pass("kimi_gate", "", _HEAD, pr_number=9001)),
            encoding="utf-8",
        )

        record_not_executable(
            gate="kimi_gate", pr_number=9001, pr_id="",
            reason="gate_runner_missing",
            reason_detail="kimi_gate is not available",
            request_payload={"commit_sha": _HEAD},
            requests_dir=requests_dir, results_dir=results_dir, state_dir=state_dir,
        )

        # The decided pass must survive untouched -- the guard's own job,
        # unchanged by this fix.
        assert json.loads(rf.read_text(encoding="utf-8"))["status"] == "pass"

        lines = _read_audit_lines(state_dir)
        assert len(lines) == 1, (
            f"expected exactly ONE durable audit line for one refused write, "
            f"got {len(lines)}: {lines!r}"
        )
        assert lines[0]["reason"] == "gate_runner_missing"
        assert lines[0]["commit_sha"] == _HEAD


@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    """Same isolated-store fixture shape as test_oi1624_gate_absence_vs_rejection.py."""
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    monkeypatch.setenv("VNX_GEMINI_REVIEW_ENABLED", "0")
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "0")
    monkeypatch.setenv("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0")
    return {
        "project_root": project_root,
        "state_dir": state_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


class TestMarkGateUnavailableAuditCount:
    """Path 2/4: gate_request_handler._mark_gate_unavailable -- shared by
    _request_codex/_request_gemini/_request_kimi/_request_ci_gate. Called
    directly with gate="kimi_gate" (a harness-lane gate,
    GATE_PROVIDER_HARNESS_LANE) so gate_result_parser._classify_unavailable's
    reason is deterministically "gate_runner_missing" regardless of what
    binaries actually exist on PATH in the test environment -- codex_gate
    (a PATH_BINARY gate) would make this test's outcome depend on whether
    `codex` happens to be installed on the runner, which is not what this
    test is about."""

    def test_refused_write_lands_exactly_one_line_with_head(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        import review_gate_manager as rgm
        manager = rgm.ReviewGateManager()

        rf = manager_env["results_dir"] / "pr-9002-kimi_gate.json"
        rf.write_text(
            json.dumps(_decided_pass("kimi_gate", "", _HEAD, pr_number=9002)),
            encoding="utf-8",
        )

        payload = {
            "branch": _BRANCH,
            "commit_sha": _HEAD,
            "requested_at": "2026-09-17T00:00:00Z",
        }
        manager._mark_gate_unavailable(
            payload, gate="kimi_gate", pr_number=9002, pr_id="",
        )

        assert json.loads(rf.read_text(encoding="utf-8"))["status"] == "pass"

        lines = _read_audit_lines(manager_env["state_dir"])
        assert len(lines) == 1, (
            f"expected exactly ONE durable audit line for one refused write, "
            f"got {len(lines)}: {lines!r}"
        )
        assert lines[0]["reason"] == "gate_runner_missing"
        assert lines[0]["commit_sha"] == _HEAD


class TestRequestGlmAuditCount:
    """Path 3/4: gate_request_handler._request_glm."""

    def test_refused_write_lands_exactly_one_line_with_head(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        import gate_request_handler
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: _HEAD)

        import review_gate_manager as rgm
        manager = rgm.ReviewGateManager()
        monkeypatch.setattr(manager, "_glm_gate_available", lambda: False)

        rf = manager_env["results_dir"] / "pr-9003-glm_gate.json"
        rf.write_text(
            json.dumps(_decided_pass("glm_gate", "", _HEAD, pr_number=9003)),
            encoding="utf-8",
        )

        manager._request_glm(
            pr_number=9003, branch=_BRANCH, risk_class="low",
            changed_files=["scripts/foo.py"], mode="per_pr",
        )

        assert json.loads(rf.read_text(encoding="utf-8"))["status"] == "pass"

        lines = _read_audit_lines(manager_env["state_dir"])
        assert len(lines) == 1, (
            f"expected exactly ONE durable audit line for one refused write, "
            f"got {len(lines)}: {lines!r}"
        )
        assert lines[0]["reason"] == "gate_runner_missing"
        assert lines[0]["commit_sha"] == _HEAD


class TestRequestDeepseekAuditCount:
    """Path 4/4: gate_request_handler._request_deepseek."""

    def test_refused_write_lands_exactly_one_line_with_head(self, manager_env, monkeypatch):
        monkeypatch.chdir(manager_env["project_root"])
        import gate_request_handler
        monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: _HEAD)

        import review_gate_manager as rgm
        manager = rgm.ReviewGateManager()
        monkeypatch.setattr(manager, "_deepseek_gate_available", lambda: False)

        rf = manager_env["results_dir"] / "pr-9004-deepseek_gate.json"
        rf.write_text(
            json.dumps(_decided_pass("deepseek_gate", "", _HEAD, pr_number=9004)),
            encoding="utf-8",
        )

        manager._request_deepseek(
            pr_number=9004, branch=_BRANCH, risk_class="low",
            changed_files=["scripts/foo.py"], mode="per_pr",
        )

        assert json.loads(rf.read_text(encoding="utf-8"))["status"] == "pass"

        lines = _read_audit_lines(manager_env["state_dir"])
        assert len(lines) == 1, (
            f"expected exactly ONE durable audit line for one refused write, "
            f"got {len(lines)}: {lines!r}"
        )
        assert lines[0]["reason"] == "gate_runner_missing"
        assert lines[0]["commit_sha"] == _HEAD


class TestFourPathsAgreeOnShape:
    """Cross-path invariant: whichever of the four opts out, the survivor is
    always the skip-rationale line (event_type="gate_skip_rationale") under
    the CALLER's own reason -- never the generic
    "gate_result_write_refused" from write_result_guarded's own audit path.
    A fix that flipped the opt-out (e.g. left write_result_guarded's line
    and suppressed the skip-rationale instead) would still pass a bare
    len(lines) == 1 check; this pins WHICH line survives."""

    def test_survivor_reason_is_always_the_callers_own_not_the_generic_one(self, tmp_path):
        state_dir = tmp_path / "state"
        requests_dir = state_dir / "review_gates" / "requests"
        results_dir = state_dir / "review_gates" / "results"
        requests_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)

        rf = results_dir / "pr-9005-kimi_gate.json"
        rf.write_text(
            json.dumps(_decided_pass("kimi_gate", "", _HEAD, pr_number=9005)),
            encoding="utf-8",
        )
        record_not_executable(
            gate="kimi_gate", pr_number=9005, pr_id="",
            reason="gate_runner_missing", reason_detail="kimi_gate is not available",
            request_payload={"commit_sha": _HEAD},
            requests_dir=requests_dir, results_dir=results_dir, state_dir=state_dir,
        )

        lines = _read_audit_lines(state_dir)
        assert len(lines) == 1
        assert lines[0]["event_type"] == "gate_skip_rationale"
        assert lines[0]["reason"] == "gate_runner_missing"
        assert lines[0]["reason"] != "gate_result_write_refused"
