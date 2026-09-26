"""OI-1851: a gate may not pass a diff it only partly saw, and the record must show it.

PR #1915: a 176.860-char diff against MAX_DIFF_CHARS = 50.000; glm_gate booked
`completed` with `diff_chars: 0, diff_truncated: false` and the merge door read
it as passing. Real code throughout; only the provider call, the diff fetch and
the gh identity lookups are stubbed.
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

import closure_verifier
import gate_runner
from gate_lane_contract import MAX_DIFF_CHARS
from gate_runner import GateRunner
from gate_status import has_complete_evidence, is_pass

BRANCH = "feature/oi1851"
HEAD_SHA = "cafedeadbeef"

PASS_REPORT = (
    "Reviewed the diff.\nChecked the changed functions.\nNo issues found.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)
FAIL_REPORT = (
    "Reviewed the diff.\nFound a real problem.\nSee finding.\n\n"
    "```json\n"
    '{"verdict": "fail", "findings": [{"severity": "error", "message": "sql injection"}],'
    ' "residual_risk": "unsanitized input"}\n'
    "```\n"
)

SMALL_DIFF = "diff --git a/small.py b/small.py\n+ok = True\n"


def _big_diff() -> str:
    head = "diff --git a/scripts/head.py b/scripts/head.py\n" + "+x = 1\n" * 100
    tail = "diff --git a/scripts/tail.py b/scripts/tail.py\n" + "+y = 2\n" * 12000
    diff = head + tail
    assert len(diff) > MAX_DIFF_CHARS
    assert len(head) < MAX_DIFF_CHARS
    return diff


BIG_DIFF = _big_diff()


def _merge_door(results_dir: Path, pr: str, gate: str, *, branch=BRANCH, head_sha=HEAD_SHA):
    return closure_verifier.check_review_gate_for_merge(
        pr, gate, results_dir, branch=branch, head_sha=head_sha,
    )


# --- Standalone single-shot gates (glm_gate.py / kimi_gate.py)


def _run_standalone(module, gate: str, tmp_path, monkeypatch, *, diff: str, report: str, pr: str):
    data_dir = tmp_path / "data"

    def _make(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            reports = data_dir / "unified_reports"
            reports.mkdir(parents=True, exist_ok=True)
            (reports / f"{dispatch_id}.md").write_text(report, encoding="utf-8")
            return report
        return _dispatch

    monkeypatch.setattr(module, "_get_diff", lambda pr_arg, diff_file: diff)
    monkeypatch.setattr(module, "_make_default_dispatcher", _make)
    monkeypatch.setattr(module, "get_pr_head_branch", lambda pr_number: BRANCH)
    monkeypatch.setattr(module, "get_pr_head_sha", lambda pr_number: HEAD_SHA)
    rc = module.main(["--pr", pr, "--data-dir", str(data_dir)])
    results_dir = data_dir / "state" / "review_gates" / "results"
    record = json.loads((results_dir / f"pr-{pr}-{gate}.json").read_text(encoding="utf-8"))
    return rc, record, results_dir


@pytest.fixture(params=["glm_gate", "kimi_gate"])
def standalone_gate(request, monkeypatch):
    monkeypatch.delenv("VNX_GLM_GATE_MODEL", raising=False)
    monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)
    module = __import__(request.param)
    return module, request.param


def test_single_shot_pass_on_a_truncated_diff_is_partial_review_and_no_go(
    standalone_gate, tmp_path, monkeypatch,
):
    module, gate = standalone_gate
    rc, record, results_dir = _run_standalone(
        module, gate, tmp_path, monkeypatch, diff=BIG_DIFF, report=PASS_REPORT, pr="1915",
    )
    depth = record["execution_depth"]
    assert (depth["mode"], depth["diff_chars"], depth["diff_truncated"], depth["diff_limit"]) == (
        "single_shot", len(BIG_DIFF.strip()), True, MAX_DIFF_CHARS)
    assert depth["truncated_files"] == ["scripts/tail.py"]
    assert (record["status"], record["reason"]) == ("partial_review", "diff_truncated")
    assert rc != 0
    assert is_pass(record)[1].startswith("partial_review:")
    assert has_complete_evidence(record) is False

    verdict = _merge_door(results_dir, "1915", gate)
    assert verdict["verdict"] == "NO-GO"
    assert str(len(BIG_DIFF.strip())) in verdict["message"]
    assert str(MAX_DIFF_CHARS) in verdict["message"]
    assert "scripts/tail.py" in verdict["message"]
    assert "codex_gate" in verdict["message"]  # the way out is named


def test_single_shot_fail_on_a_truncated_diff_stays_a_fail(standalone_gate, tmp_path, monkeypatch):
    module, gate = standalone_gate
    rc, record, _ = _run_standalone(
        module, gate, tmp_path, monkeypatch, diff=BIG_DIFF, report=FAIL_REPORT, pr="1916",
    )
    assert record["status"] == "fail"
    assert rc == 2


def test_single_shot_pass_under_the_cap_is_unchanged(standalone_gate, tmp_path, monkeypatch):
    module, gate = standalone_gate
    rc, record, results_dir = _run_standalone(
        module, gate, tmp_path, monkeypatch, diff=SMALL_DIFF, report=PASS_REPORT, pr="1920",
    )
    assert rc == 0
    assert record["status"] == "pass"
    assert "reason_detail" not in record
    depth = record["execution_depth"]
    assert (depth["diff_chars"], depth["diff_truncated"]) == (len(SMALL_DIFF.strip()), False)
    assert _merge_door(results_dir, "1920", gate)["verdict"] == "GO"


def test_full_peer_pass_is_the_way_out_of_a_partial_review(tmp_path, monkeypatch):
    """A full review by a peer on the same head is the way out."""
    monkeypatch.delenv("VNX_GLM_GATE_MODEL", raising=False)
    monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)
    import glm_gate
    import kimi_gate

    _rc, glm_record, results_dir = _run_standalone(
        glm_gate, "glm_gate", tmp_path, monkeypatch, diff=BIG_DIFF, report=PASS_REPORT, pr="1917",
    )
    assert glm_record["status"] == "partial_review"
    assert _merge_door(results_dir, "1917", "glm_gate")["verdict"] == "NO-GO"

    _run_standalone(
        kimi_gate, "kimi_gate", tmp_path, monkeypatch, diff=SMALL_DIFF, report=PASS_REPORT, pr="1917",
    )
    verdict = _merge_door(results_dir, "1917", "glm_gate")
    assert verdict["verdict"] == "GO", verdict["message"]
    assert verdict["evidence_gate"] == "kimi_gate"


# --- Harness lane through gate_runner (the path PR #1915 actually took)


@pytest.fixture
def runner_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "project" / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    (state_dir / "review_gates" / "requests").mkdir(parents=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "project"))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.delenv("VNX_GLM_GATE_MODEL", raising=False)
    monkeypatch.setattr(
        gate_runner.subprocess, "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("harness lane must not spawn")),
    )
    return {"state_dir": state_dir, "reports_dir": reports_dir,
            "results_dir": state_dir / "review_gates" / "results"}


def _run_harness_lane(runner_env, monkeypatch, *, diff: str, report: str, pr: int):
    def factory(data_dir, timeout_seconds, *, role="plan-reviewer"):
        def dispatch(provider, model, instruction, dispatch_id):
            return report
        return dispatch

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
    monkeypatch.setattr(GateRunner, "_fetch_gh_pr_diff", staticmethod(lambda pr_number: diff))
    payload = {"gate": "glm_gate", "status": "requested", "branch": BRANCH, "pr_number": pr,
               "report_path": str(runner_env["reports_dir"] / f"glm-gate-pr{pr}.md")}
    runner = GateRunner(state_dir=runner_env["state_dir"], reports_dir=runner_env["reports_dir"])
    result = runner.run(gate="glm_gate", request_payload=payload, pr_number=pr)
    saved = json.loads((runner_env["results_dir"] / f"pr-{pr}-glm_gate.json").read_text())
    return result, saved


def test_harness_lane_pass_on_a_truncated_diff_is_partial_review_and_no_go(runner_env, monkeypatch):
    result, saved = _run_harness_lane(runner_env, monkeypatch, diff=BIG_DIFF, report=PASS_REPORT, pr=1915)
    depth = saved["execution_depth"]
    assert (depth["parsed"], depth["diff_chars"], depth["diff_truncated"]) == (
        False, len(BIG_DIFF.strip()), True)
    assert result["status"] == saved["status"] == "partial_review"
    assert "completed successfully" not in saved["summary"]
    assert is_pass(saved)[0] is False

    verdict = _merge_door(runner_env["results_dir"], "1915", "glm_gate", head_sha=None)
    assert verdict["verdict"] == "NO-GO"
    assert str(len(BIG_DIFF.strip())) in verdict["message"]
    assert "scripts/tail.py" in verdict["message"]


def test_harness_lane_pass_under_the_cap_is_unchanged(runner_env, monkeypatch):
    result, saved = _run_harness_lane(runner_env, monkeypatch, diff=SMALL_DIFF, report=PASS_REPORT, pr=1921)
    assert result["status"] == "completed"
    assert "completed successfully" in saved["summary"]
    assert saved["execution_depth"]["diff_chars"] == len(SMALL_DIFF.strip())
    assert saved["execution_depth"]["diff_truncated"] is False
    verdict = _merge_door(runner_env["results_dir"], "1921", "glm_gate", head_sha=None)
    assert verdict["verdict"] == "GO", verdict["message"]


# --- A record whose writer said "completed" over a cut diff it never read


def test_completed_record_with_unread_truncation_is_not_complete_evidence(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(PASS_REPORT, encoding="utf-8")
    record = {
        "gate": "glm_gate", "pr_id": "1930", "pr_number": 1930, "status": "completed",
        "contract_hash": "0123456789abcdef", "report_path": str(report),
        "blocking_findings": [], "dispatch_id": "glm-gate-pr1930-1790000000",
        "branch": BRANCH, "commit_sha": HEAD_SHA,
        "execution_depth": {
            "parsed": False, "mode": "agentic", "files_read": 0,
            "diff_chars": 176860, "diff_truncated": True, "diff_limit": MAX_DIFF_CHARS,
            "truncated_files": ["scripts/pr_merge.py"],
        },
    }
    assert has_complete_evidence(record) is False
    assert is_pass(record)[0] is False
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    (results_dir / "pr-1930-glm_gate.json").write_text(json.dumps(record), encoding="utf-8")
    verdict = _merge_door(results_dir, "1930", "glm_gate")
    assert verdict["verdict"] == "NO-GO"
    assert "176860" in verdict["message"]
    assert "scripts/pr_merge.py" in verdict["message"]


# --- The coverage criterion itself, and the readers that must know the status


def test_coverage_gap_closes_only_on_a_measured_read_of_every_cut_file():
    import gate_depth

    coverage = gate_depth.diff_coverage(BIG_DIFF, MAX_DIFF_CHARS)
    unmeasured = gate_depth.with_diff_coverage(gate_depth.ExecutionDepth(), coverage)
    assert gate_depth.coverage_gap(unmeasured)  # parsed: false never closes it
    assert gate_depth.from_dict(json.loads(json.dumps(unmeasured.to_dict()))) == unmeasured

    def agentic(files_read):
        depth = gate_depth.ExecutionDepth(parsed=True, investigative_actions=1, files_read=files_read)
        return gate_depth.with_diff_coverage(depth, coverage)

    assert gate_depth.coverage_gap(agentic(1)) == ""
    assert gate_depth.coverage_gap(agentic(0))
    assert gate_depth.coverage_gap(gate_depth.single_shot_depth(1, True))


def test_no_diff_in_hand_leaves_the_depth_unmeasured():
    import gate_depth

    depth = gate_depth.ExecutionDepth(parsed=True, mode="agentic", investigative_actions=2)
    assert gate_depth.with_diff_coverage(depth, None) is depth
    assert gate_depth.coverage_gap(depth) == ""


def test_forge_publisher_maps_partial_review_to_action_required():
    import forge_gate_publisher

    verdict = forge_gate_publisher.classify_record(
        {"gate": "glm_gate", "status": "partial_review", "commit_sha": HEAD_SHA,
         "dispatch_id": "glm-gate-pr1915-1790000000"},
        HEAD_SHA,
    )
    assert verdict.conclusion == forge_gate_publisher.CONCLUSION_ACTION_REQUIRED
