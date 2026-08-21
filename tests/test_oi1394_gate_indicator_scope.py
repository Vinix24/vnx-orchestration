"""Regression tests for OI-1394: the merge-poort telt leesmateriaal als bevinding.

closure_verifier._count_report_blocking_indicators used to regex-scan the
FULL headless gate report — including the raw ``## Gate Output`` tool
transcript, which can contain the literal substrings it looks for (a
`blocking:` parameter name, a `severity: blocking` string literal) in code
the reviewer merely READ, never in its verdict. Measured live on PR #1633:
codex_gate read scripts/kimi_gate.py during its review, which embeds exactly
that code, and the merge gate refused the PR despite a clean verdict
(status=completed, has_required_failure=false, 2 warning findings, 0
blockers).

The fix: gate_artifacts.format_report now writes a normalized ``## Findings``
section (from the gate's own parsed blocking/advisory lists) ahead of the
raw ``## Gate Output`` dump for gates that parse structured findings
(codex_gate). closure_verifier._count_report_blocking_indicators scans only
that normalized section when present, never the raw transcript that follows
it. Gates without a normalized section (ci_gate's compact non-toolstream
output) fall back to a full-content scan, unchanged.

Covers the three pieces of evidence required by the dispatch:
  1. Raw toolstream containing `blocking:` in READ source code, clean
     verdict -> zero indicators, contradiction check passes (merge allowed).
  2. The REVIEWER's own verdict reporting a genuine blocking finding ->
     indicator counted, contradiction check fails (merge refused). Without
     this test, tightening the scan scope could be indistinguishable from
     disabling the check outright.
  3. A faithful shrink of the real PR #1633 report material (the three
     literal kimi_gate.py fragments the reviewer read) run through the full
     gate_artifacts.materialize_artifacts -> closure_verifier pipeline.
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

import closure_verifier as cv
from gate_artifacts import materialize_artifacts
from review_contract import Deliverable, QualityGate, ReviewContract, TestEvidence

BRANCH = "feature/oi-1394-test"

# The three fragments codex_gate read from scripts/kimi_gate.py while
# reviewing PR #1633 — verbatim, per the dispatch's live measurement. Each
# contains the literal substring `blocking:` that the old unscoped regex
# scan matched as a "blocking indicator", despite being read material, not
# a verdict.
KIMI_GATE_FRAGMENTS = (
    '    if v == "pass" and not blocking:\n'
    '    if v in {"fail", "blocked"} or blocking:  return "fail", blocking, residual or "kimi gate reported blocking findings"\n'
    '    def _status_summary(status: str, blocking: list) -> str:\n'
)


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    """Isolated requests/results/reports dirs, and VNX_STATE_DIR redirected so
    the codex_gate register-emit side effect never touches real state."""
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    reports_dir = tmp_path / "reports"
    state_dir = tmp_path / "state"
    for d in (requests_dir, results_dir, reports_dir, state_dir):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    return {
        "requests_dir": requests_dir,
        "results_dir": results_dir,
        "reports_dir": reports_dir,
    }


def _make_contract(pr_id: str = "PR-0", review_stack=None) -> ReviewContract:
    return ReviewContract(
        pr_id=pr_id,
        pr_title="Test PR",
        feature_title="Test Feature",
        branch=BRANCH,
        track="C",
        risk_class="high",
        merge_policy="human",
        review_stack=list(review_stack or ["codex_gate"]),
        closure_stage="in_review",
        deliverables=[Deliverable(description="test", category="implementation")],
        non_goals=[],
        scope_files=[],
        changed_files=[],
        quality_gate=QualityGate(gate_id="gate_test", checks=["check 1"]),
        test_evidence=TestEvidence(test_files=["tests/test_demo.py"], test_command="pytest"),
        deterministic_findings=[],
        content_hash="abcdef1234567890",
    )


def _codex_stdout(*, verdict_findings: list, raw_reads: str = "") -> str:
    """Build a codex NDJSON stdout: a tool-read event (ignored by the
    findings parser, but present in the raw transcript) followed by the
    agent's final verdict message (the only part the parser reads).

    Includes enough preamble lines to clear materialize_artifacts'
    substantive-content floor (gate_artifacts._validate_content requires
    3+ non-blank lines) — real codex transcripts run to hundreds of lines.
    """
    lines = [
        json.dumps({"type": "reasoning", "item": {"type": "reasoning", "text": "Starting review of changed files."}}),
        json.dumps({"type": "reasoning", "item": {"type": "reasoning", "text": "Scanning diff for governance-relevant changes."}}),
    ]
    if raw_reads:
        lines.append(json.dumps({
            "type": "tool_call",
            "item": {
                "type": "file_read",
                "path": "scripts/kimi_gate.py",
                "content": raw_reads,
            },
        }))
    verdict = {
        "verdict": "fail" if any(f["severity"] == "blocking" for f in verdict_findings) else "pass",
        "findings": verdict_findings,
        "residual_risk": "",
    }
    lines.append(json.dumps({
        "item": {
            "type": "agent_message",
            "text": "Review complete.\n\n```json\n" + json.dumps(verdict) + "\n```\n",
        },
    }))
    return "\n".join(lines)


def _run_codex_gate(gate_env, *, pr_id, stdout) -> Path:
    """Drive the real gate_artifacts pipeline, mirroring how gate_runner invokes it.

    request_payload carries branch + contract_hash so the written result
    record is directly joinable by closure_verifier's branch-scoped lookup
    (gate_recorder.stamp_request_identity), no post-hoc patching needed.
    """
    reports_dir = gate_env["reports_dir"]
    report_path = reports_dir / f"{pr_id.lower()}-codex_gate.md"
    request_payload = {
        "gate": "codex_gate",
        "status": "requested",
        "provider": "codex",
        "branch": BRANCH,
        "pr_id": pr_id,
        "review_mode": "per_pr",
        "risk_class": "high",
        "changed_files": ["scripts/closure_verifier.py"],
        "requested_at": "20260821T090000Z",
        "prompt": "Review this code",
        "dispatch_id": f"codex-gate-{pr_id.lower()}",
        "contract_hash": "abcdef1234567890",
        "report_path": str(report_path),
    }
    result = materialize_artifacts(
        gate="codex_gate",
        pr_number=None,
        pr_id=pr_id,
        stdout=stdout,
        request_payload=request_payload,
        duration_seconds=12.5,
        requests_dir=gate_env["requests_dir"],
        results_dir=gate_env["results_dir"],
        reports_dir=reports_dir,
    )
    # materialize_artifacts always stamps status="completed" — pass/fail
    # derivation from findings is a downstream concern, not this writer's.
    # This is exactly the shape OI-1394 hit: a "completed" result can still
    # carry blocking findings.
    assert result["status"] == "completed"
    assert result["branch"] == BRANCH
    return report_path


class TestBlockingIndicatorScope:
    def test_raw_toolstream_read_source_not_counted_merge_allowed(self, gate_env):
        """Evidence 1: `blocking:` in READ source code (raw transcript) with a
        clean verdict (0 blocking, 2 advisory) must count zero indicators —
        the merge-gate contradiction check must pass."""
        stdout = _codex_stdout(
            verdict_findings=[
                {"severity": "warning", "message": "Consider adding a docstring"},
                {"severity": "warning", "message": "Minor naming inconsistency"},
            ],
            raw_reads=KIMI_GATE_FRAGMENTS,
        )
        report_path = _run_codex_gate(gate_env, pr_id="PR-0", stdout=stdout)

        # Sanity: the raw transcript really does carry the literal `blocking:`
        # substrings, so a pass here proves scoping — not an empty fixture.
        report_text = report_path.read_text(encoding="utf-8")
        assert report_text.count("blocking:") >= 3

        contract = _make_contract(pr_id="PR-0", review_stack=["codex_gate"])
        checks = cv._detect_gate_report_contradictions(contract, gate_env["results_dir"])

        assert len(checks) == 1
        assert checks[0].status == "PASS", checks[0].detail
        assert cv._count_report_blocking_indicators(report_text) == 0

    def test_reviewer_reported_blocking_finding_counted_merge_refused(self, gate_env):
        """Evidence 2 (required): closure_verifier must still catch a genuine
        blocking finding in the report's normalized section even when the
        JSON result record claims a clean pass — the exact mismatch
        invariant 7 exists to catch (gate result says pass, report disagrees).
        Without this test, narrowing the scan to the normalized section
        could be indistinguishable from disabling the check outright: a
        blanket-disabled scan would also make evidence 1/3 pass "clean".
        """
        stdout = _codex_stdout(
            verdict_findings=[
                {"severity": "blocking", "message": "SQL injection in query_builder.py"},
            ],
        )
        report_path = _run_codex_gate(gate_env, pr_id="PR-1", stdout=stdout)
        report_text = report_path.read_text(encoding="utf-8")
        assert "[BLOCKING]" in report_text

        # gate_is_pass already derives "passed" from blocking_findings, so a
        # result that HONESTLY carries the blocking finding never reaches
        # the "gate says pass" branch of the contradiction check at all —
        # it correctly reads as consistent (both sides agree: this fails).
        # Simulate the actual bug shape instead: a JSON result that
        # (incorrectly) claims a clean pass while the report — immutable
        # evidence of what the reviewer actually said — still shows the
        # blocking language.
        result_path = gate_env["results_dir"] / "pr1-codex_gate-contract.json"
        gate_result = json.loads(result_path.read_text())
        gate_result["blocking_findings"] = []
        gate_result["blocking_count"] = 0
        result_path.write_text(json.dumps(gate_result))

        contract = _make_contract(pr_id="PR-1", review_stack=["codex_gate"])
        checks = cv._detect_gate_report_contradictions(contract, gate_env["results_dir"])

        assert len(checks) == 1
        assert checks[0].status == "FAIL"
        assert "evidence mismatch" in checks[0].detail
        assert cv._count_report_blocking_indicators(report_text) > 0

    def test_pr_1633_fixture_shrink_zero_indicators(self, gate_env):
        """Evidence 3: faithful shrink of the real PR #1633 report material —
        a clean verdict (0 blocking, 2 advisory warnings) with the raw
        transcript embedding the three literal kimi_gate.py fragments the
        reviewer actually read — run through the full materialize_artifacts
        -> closure_verifier pipeline."""
        raw_reads = (
            "Reading scripts/kimi_gate.py to check verdict-mapping conventions "
            "used elsewhere in the gate fleet:\n\n" + KIMI_GATE_FRAGMENTS
        )
        stdout = _codex_stdout(
            verdict_findings=[
                {"severity": "warning", "message": "Docstring could clarify the unavailable state"},
                {"severity": "warning", "message": "Consider a named constant for the demotion reason"},
            ],
            raw_reads=raw_reads,
        )
        report_path = _run_codex_gate(gate_env, pr_id="PR-1633", stdout=stdout)
        report_text = report_path.read_text(encoding="utf-8")

        contract = _make_contract(pr_id="PR-1633", review_stack=["codex_gate"])
        checks = cv._detect_gate_report_contradictions(contract, gate_env["results_dir"])

        assert len(checks) == 1
        assert checks[0].status == "PASS", (
            f"expected merge to be allowed on a clean verdict; got: {checks[0].detail}\n"
            f"--- report ---\n{report_text}"
        )
