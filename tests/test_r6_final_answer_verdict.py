"""R6 (OI-2039): a harness seat keeps the verdict that is only in its final answer.

Measured 2026-10-15 on seven deepseek_gate runs (PRs 2065, 2066, 2069, 2070,
2071): the model wrote ``unified_reports/<dispatch_id>.md`` itself, without a
verdict block, and gave a complete verdict in its final answer, the ``result``
event of ``events/archive/plan-gate/<dispatch_id>.ndjson``. The runner read the
report and booked ``unavailable / validation_failed / no_verdict_block``. The
plan-gate tiebreaker lost a START the same way on 2026-10-12
(``plan-tiebreak-provider-schakelaar-20757ee5``, ``TiebreakerParseError``).

Every fixture is built in a tmp dir with the measured event shape. Nothing
starts a provider and nothing reads a real store. Module-level imports use
only names that exist before the fix, so the red runs (TestRedRuns) fail on
their assertions on the old code, not on an ImportError; tests that need the
new reader import it inside the test.
"""

from __future__ import annotations

import gzip
import json
import os
import stat
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import closure_verifier
import config_registry
import config_runtime
import event_store
import forge_gate_publisher
import gate_artifacts
import gate_runner
import plan_gate_panel as pgp
import plan_gate_tiebreaker as pgt
import vnx_paths
from gate_runner import GateRunner
from subprocess_adapter import SubprocessAdapter

HEAD = "1291e1533a18a6c9de4566ed9739a41c1b4d05f9"
OTHER_HEAD = "ef2e9d9b0000000000000000000000000000beef"
TERMINAL = "plan-gate"

# The lane report the model wrote itself: a full review, no verdict block.
# Shaped after unified_reports/deepseek-gate-pr2066-1792100303.md.
LANE_REPORT = """# Review gate — PR 2066 (deepseek_gate)

**Dispatch-ID**: {dispatch_id}
**Model**: deepseek
**Provider**: deepseek

## Summary

Strict review of PR 2066. The diff adds a `_checkout_root()` helper to
`scripts/lib/vnx_paths.py`. Verdict: **pass** — the change is narrow, the
fallback is monotonic, and no regression was found.

## Changes

- `scripts/lib/vnx_paths.py` — new `_checkout_root()`.
- `tests/test_producer_freshness_store_resolution.py` — 3 new tests.

## Verification

- `pytest tests/test_producer_freshness_store_resolution.py -q` → 3 passed.

## Open Items

- Non-blocking: the equivalent fallback was not applied to `scripts/vnx_doctor.py` (info).
"""

PASS_BLOCK = {
    "verdict": "pass",
    "findings": [
        {"severity": "info", "message": "The equivalent fallback was not applied to scripts/vnx_doctor.py.",
         "file_path": "", "line": 0},
        {"severity": "info", "message": "_checkout_root treats any .git entry as a checkout root.",
         "file_path": "scripts/lib/vnx_paths.py", "line": 122},
    ],
    "residual_risk": "A newly reachable code path in a stripped environment.",
}

FAIL_BLOCK = {
    "verdict": "fail",
    "findings": [
        {"severity": "error", "message": "The fallback reads a foreign store.",
         "file_path": "scripts/lib/vnx_paths.py", "line": 140},
    ],
    "residual_risk": "",
}


def _final_answer(block) -> str:
    """A final answer shaped like the measured one: prose, then a fenced json verdict."""
    return (
        "**Verdict: pass.** The fix is narrow and monotonic.\n\n"
        "Report written to `$VNX_DATA_DIR/unified_reports/x.md`.\n\n"
        "```json\n" + json.dumps(block, indent=2) + "\n```"
    )


PLAN_PASS_ANSWER = (
    "The plan is sound.\n\n"
    "```vnx-plan-verdict\n"
    '{"verdict": "pass", "blocking_findings": [], "rationale": "sound and testable"}\n'
    "```"
)

TIEBREAK_START_ANSWER = (
    "START\n\n"
    "```vnx-plan-tiebreak\n"
    '{"outcome": "START", "required_change": "", "rationale": "the plan has converged"}\n'
    "```"
)

PLAN_SEAT_REPORT = (
    "## Summary\n\nI reviewed the plan. It is sound, the layering is coherent and every "
    "acceptance case has a test. My verdict is pass.\n\n## Open Items\n\nNone\n"
)

TIEBREAK_REPORT = (
    "## Summary\n\nTiebreak decision for track provider-schakelaar after two full "
    "plan-gate rounds. I decided START: the plan is converged.\n\n## Open Items\n\nNone\n"
)


# ---------------------------------------------------------------------------
# Archive fixtures: the measured event shape
# ---------------------------------------------------------------------------


def _event(dispatch_id, etype, data, seq, terminal=TERMINAL):
    return {
        "type": etype, "timestamp": f"2026-10-15T21:40:{seq % 60:02d}.000+00:00",
        "dispatch_id": dispatch_id, "terminal": terminal, "sequence": seq,
        "data": data, "observability_tier": 2,
    }


def _result_event(dispatch_id, text, *, subtype="success", is_error=None, seq=10708,
                  terminal=TERMINAL):
    data = {
        "text": text, "subtype": subtype, "session_id": "3e4c1b78-139f-4081-b8d9-c67fac409211",
        "usage": {"input_tokens": 1200, "output_tokens": 800},
    }
    if is_error is not None:
        data["is_error"] = is_error
    return _event(dispatch_id, "result", data, seq, terminal)


def _run_events(dispatch_id, final_text, **result_kw):
    return [
        _event(dispatch_id, "system", {"type": "system", "subtype": "ui_invalidate"}, 1),
        _event(dispatch_id, "init", {"session_id": "s", "model": "deepseek-flash"}, 2),
        _event(dispatch_id, "tool_use", {"name": "Bash", "input": {"command": "git diff"}, "id": "t1"}, 3),
        _event(dispatch_id, "tool_result", {"tool_use_id": "t1", "content": "diff --git a/x b/x"}, 4),
        _event(dispatch_id, "text", {"text": "Writing the report."}, 5),
        _result_event(dispatch_id, final_text, **result_kw),
        _event(dispatch_id, "provider_teardown_worktree", {"worktree_state": "clean"}, 1),
    ]


def _write_archive(data_dir: Path, name: str, events, *, gz: bool = False) -> Path:
    archive_dir = Path(data_dir) / "events" / "archive" / TERMINAL
    archive_dir.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(e) + "\n" for e in events)
    if gz:
        path = archive_dir / f"{name}.ndjson.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path = archive_dir / f"{name}.ndjson"
        path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Runner fixtures (the OI-2066 harness: dispatcher factory double, no provider)
# ---------------------------------------------------------------------------

_CONFIG_KEYS = (
    "VNX_KIMI_GATE_TIMEOUT_SECONDS", "VNX_GLM_GATE_TIMEOUT_SECONDS",
    "VNX_DEEPSEEK_GATE_TIMEOUT_SECONDS", "VNX_KIMI_GATE_MAX_DIFF_CHARS",
    "VNX_GLM_GATE_MAX_DIFF_CHARS", "VNX_DEEPSEEK_GATE_MAX_DIFF_CHARS",
)


@pytest.fixture
def gate_dirs(tmp_path, monkeypatch):
    for key in _CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{config_registry._bare(key)}", raising=False)
    for key in ("VNX_TOTAL_DEADLINE", "VNX_PROJECT_ID", "VNX_DEEPSEEK_GATE_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {})
    config_runtime._wired_for.clear()
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)
    state = tmp_path / "state"
    (state / "review_gates" / "requests").mkdir(parents=True)
    (state / "review_gates" / "results").mkdir(parents=True)
    reports = tmp_path / "unified_reports"
    reports.mkdir()
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setattr(gate_runner, "fetch_branch", lambda *a, **kw: None)
    monkeypatch.setattr(GateRunner, "_fetch_gh_pr_diff", staticmethod(lambda _pr: _DIFF))
    yield {"data": tmp_path, "state": state, "reports": reports,
           "results": state / "review_gates" / "results"}
    config_runtime._wired_for.clear()
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)


_DIFF = (
    "diff --git a/scripts/lib/vnx_paths.py b/scripts/lib/vnx_paths.py\n"
    "--- a/scripts/lib/vnx_paths.py\n+++ b/scripts/lib/vnx_paths.py\n@@ -1,1 +1,2 @@\n"
    "+def _checkout_root(path):\n+    return path\n"
)


def _payload(gate_dirs, *, pr_number=2066, commit_sha=HEAD):
    return {
        "gate": "deepseek_gate",
        "status": "requested",
        "branch": "dispatch/20261015-rg-r1-heartbeat-store",
        "pr_number": pr_number,
        "commit_sha": commit_sha,
        "requested_at": "2026-10-15T21:38:19Z",
        "report_path": str(gate_dirs["reports"] / "headless" / f"deepseek_gate-pr-{pr_number}.md"),
    }


def _run_gate(gate_dirs, monkeypatch, lane, *, commit_sha=HEAD, pr_number=2066):
    """Run deepseek_gate through GateRunner.run with *lane* standing in for the governed lane.

    *lane(data_dir, dispatch_id)* plays the lane: it writes whatever archive the
    case needs and returns the report text the dispatcher hands back.
    """
    calls = []

    def factory(data_dir, timeout_seconds, *, role="plan-reviewer", base_ref=None,
                total_deadline_seconds=None):
        def dispatch(provider, model, instruction, dispatch_id):
            calls.append(dispatch_id)
            return lane(Path(data_dir), dispatch_id)
        return dispatch

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
    runner = GateRunner(state_dir=gate_dirs["state"], reports_dir=gate_dirs["reports"])
    result = runner.run(
        gate="deepseek_gate",
        request_payload=_payload(gate_dirs, pr_number=pr_number, commit_sha=commit_sha),
        pr_number=pr_number,
    )
    record = json.loads(
        (gate_dirs["results"] / f"pr-{pr_number}-deepseek_gate.json").read_text(encoding="utf-8")
    )
    return result, record, calls


def _lane_with_answer(final_text, *, report=None, **result_kw):
    def lane(data_dir, dispatch_id):
        _write_archive(data_dir, dispatch_id, _run_events(dispatch_id, final_text, **result_kw))
        return (report if report is not None else LANE_REPORT).format(dispatch_id=dispatch_id)
    return lane


def _materialize(tmp_path, stdout, *, dispatch_id="deepseek-gate-pr2066-1792100303",
                 commit_sha=HEAD, final_answer_dir=None, pr_number=2066):
    """materialize_artifacts for a deepseek_gate run, as the harness lane calls it."""
    state = tmp_path / "state"
    requests_dir = state / "review_gates" / "requests"
    results_dir = state / "review_gates" / "results"
    reports_dir = tmp_path / "unified_reports"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    payload = {
        "gate": "deepseek_gate", "status": "executing", "provider": "deepseek-harness",
        "branch": "dispatch/20261015-rg-r1-heartbeat-store", "pr_number": pr_number,
        "commit_sha": commit_sha, "dispatch_id": dispatch_id, "model": "deepseek-flash",
        "prompt": "Review this code",
        "report_path": str(reports_dir / "headless" / f"deepseek_gate-pr-{pr_number}.md"),
    }
    kwargs = {"final_answer_dir": final_answer_dir} if final_answer_dir is not None else {}
    result = gate_artifacts.materialize_artifacts(
        gate="deepseek_gate", pr_number=pr_number, pr_id="", stdout=stdout,
        request_payload=payload, duration_seconds=149.8,
        requests_dir=requests_dir, results_dir=results_dir, reports_dir=reports_dir,
        **kwargs,
    )
    return result, payload


def _door(record):
    return closure_verifier._merge_door_record_verdict(record, "deepseek_gate", "2066")


# ---------------------------------------------------------------------------
# Red runs: these fail on 12ae30c2 on behaviour
# ---------------------------------------------------------------------------


class TestRedRuns:
    def test_harness_seat_books_the_verdict_of_its_final_answer(self, gate_dirs, monkeypatch):
        """(a) Report without a verdict block, archived result event with one: booked, not no_verdict_block."""
        result, record, calls = _run_gate(
            gate_dirs, monkeypatch, _lane_with_answer(_final_answer(PASS_BLOCK)),
        )
        assert len(calls) == 1
        assert record["status"] == "completed", record.get("reason_detail")
        assert "no_verdict_block" not in str(record.get("reason_detail", ""))
        assert record["verdict_source"] == "final_answer"
        assert record["evidence_source"] == "live"
        assert record["commit_sha"] == HEAD
        assert record["dispatch_id"] == calls[0]
        assert record["blocking_findings"] == []
        assert len(record["advisory_findings"]) == 2
        assert result["status"] == "completed"

    def test_tiebreaker_decides_from_its_final_answer(self, tmp_path):
        """(b) Tiebreak report without a block, result event with a START block: START, no parse error."""
        seen = []

        def dispatcher(provider, model_arg, instruction, dispatch_id):
            seen.append(dispatch_id)
            _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, TIEBREAK_START_ANSWER))
            return TIEBREAK_REPORT

        result = pgt.run_tiebreaker(
            doc_text="## Problem\n## Approach\n", track_id="provider-schakelaar",
            project_id="vnx-dev", round_number=3, model_arg="deepseek-v4-pro",
            dispatcher=dispatcher, data_dir=str(tmp_path),
        )
        assert result.outcome == "START"
        assert result.round == 3
        assert result.verdict_source == "final_answer"
        assert "final answer" in pgt.start_open_items_reason(result)
        assert len(seen) == 1

    def test_plan_seat_scores_from_its_final_answer_without_a_reextraction(self, tmp_path):
        calls = []

        def dispatcher(provider, model_arg, instruction, dispatch_id):
            calls.append(dispatch_id)
            if dispatch_id.endswith("-reextract"):
                return "```vnx-plan-verdict\n{\"verdict\": \"revise\", \"blocking_findings\": [\"x\"], \"rationale\": \"r\"}\n```"
            _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, PLAN_PASS_ANSWER))
            return PLAN_SEAT_REPORT

        out = pgp.run_panel(
            doc_text="## Problem\nx\n", track_id="trk", project_id="p1",
            panel=[{"label": "deepseek", "provider": "deepseek", "model_arg": "deepseek-flash"}],
            dispatcher=dispatcher, data_dir=str(tmp_path), seat_ledger_path=tmp_path / "seats.ndjson",
        )
        seat = out["panelists"][0]
        assert seat["seat_status"] == "scored_via_final_answer"
        assert seat["verdict"] == "pass"
        assert not any(c.endswith("-reextract") for c in calls)
        assert out["decision"] == "PASS"


# ---------------------------------------------------------------------------
# 2. Failed-run precedence
# ---------------------------------------------------------------------------


class TestFailedRunPrecedence:
    @pytest.mark.parametrize("result_kw", [
        {"subtype": "error_max_turns"},
        {"subtype": "error_during_execution"},
        {"subtype": "success", "is_error": True},
    ])
    def test_a_run_that_did_not_end_normally_keeps_no_verdict_block(self, gate_dirs, monkeypatch, result_kw):
        _result, record, _calls = _run_gate(
            gate_dirs, monkeypatch, _lane_with_answer(_final_answer(PASS_BLOCK), **result_kw),
        )
        assert record["status"] == "unavailable"
        assert record["reason"] == "validation_failed"
        assert record["reason_detail"].startswith("no_verdict_block:")
        assert "final answer refused" in record["reason_detail"]
        assert "verdict_source" not in record

    def test_a_deadline_cut_run_books_timeout_whatever_its_final_answer_says(self, gate_dirs, monkeypatch):
        cut = "---\ndeadline_exceeded: true\ndeadline_seconds: 900\nexit_code: 0\n---\n" + LANE_REPORT
        _result, record, _calls = _run_gate(
            gate_dirs, monkeypatch, _lane_with_answer(_final_answer(PASS_BLOCK), report=cut),
        )
        assert record["status"] == "unavailable"
        assert record["reason"] == "timeout"
        assert "verdict_source" not in record

    def test_a_run_with_a_nonzero_exit_code_keeps_its_booking(self, gate_dirs, monkeypatch):
        failed = "---\nexit_code: 1\n---\n" + LANE_REPORT
        _result, record, _calls = _run_gate(
            gate_dirs, monkeypatch, _lane_with_answer(_final_answer(PASS_BLOCK), report=failed),
        )
        assert record["reason"] == "harness_lane_exit_nonzero"
        assert "verdict_source" not in record

    def test_a_dispatch_that_raised_is_never_read(self, gate_dirs, monkeypatch):
        def lane(data_dir, dispatch_id):
            _write_archive(data_dir, dispatch_id, _run_events(dispatch_id, _final_answer(PASS_BLOCK)))
            raise subprocess.TimeoutExpired(["provider_dispatch"], 960)

        _result, record, _calls = _run_gate(gate_dirs, monkeypatch, lane)
        assert record["reason"] == "timeout"
        assert "verdict_source" not in record

    @pytest.mark.parametrize("report", [
        "---\ndeadline_exceeded: true\ndeadline_seconds: 900\n---\n" + LANE_REPORT,
        "---\nexit_code: 2\n---\n" + LANE_REPORT,
        "---\nfailure_reason: quota_or_auth\n---\n" + LANE_REPORT,
        "VNX_SYNTHESIZED_REPORT_BY_GOVERNANCE\n" + LANE_REPORT,
    ])
    def test_the_reader_refuses_every_failed_run_mark_in_the_report(self, tmp_path, report):
        """Checked by the reader itself, not only by the order of the runner's checks."""
        import final_answer_verdict as fav

        dispatch_id = "deepseek-gate-pr2066-1792100303"
        _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, _final_answer(PASS_BLOCK)))
        found = fav.recover_verdict(
            tmp_path, dispatch_id, report_text=report.format(dispatch_id=dispatch_id),
            parse=gate_artifacts._final_answer_review_verdict, head_sha=HEAD,
        )
        assert found.outcome == fav.REFUSED
        assert found.verdict is None

    def test_unreadable_frontmatter_never_crashes_the_reader(self, tmp_path):
        """A frontmatter yaml cannot read (a plain ValueError) carries no mark, as in lane_deadline_detail."""
        import final_answer_verdict as fav

        dispatch_id = "deepseek-gate-pr2066-1792100303"
        _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, _final_answer(PASS_BLOCK)))
        report = "---\nstarted_at: 2026-13-45\n---\n" + LANE_REPORT.format(dispatch_id=dispatch_id)
        found = fav.recover_verdict(
            tmp_path, dispatch_id, report_text=report,
            parse=gate_artifacts._final_answer_review_verdict, head_sha=HEAD,
        )
        assert found.recovered

    def test_the_normalized_result_event_keeps_is_error(self):
        failed = SubprocessAdapter._normalize_cli_event(
            {"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 402"},
        )
        clean = SubprocessAdapter._normalize_cli_event(
            {"type": "result", "subtype": "success", "is_error": False, "result": "ok"},
        )
        legacy = SubprocessAdapter._normalize_cli_event(
            {"type": "result", "subtype": "success", "result": "ok"},
        )
        assert failed[0]["data"]["is_error"] is True
        assert clean[0]["data"]["is_error"] is False
        assert "is_error" not in legacy[0]["data"]


# ---------------------------------------------------------------------------
# 3. Each surface's own parser
# ---------------------------------------------------------------------------


class TestEachSurfaceItsOwnParser:
    def test_a_pr_review_seat_never_reads_a_plan_verdict(self, gate_dirs, monkeypatch):
        _result, record, _calls = _run_gate(gate_dirs, monkeypatch, _lane_with_answer(PLAN_PASS_ANSWER))
        assert record["status"] == "unavailable"
        assert record["reason_detail"].startswith("no_verdict_block:")
        assert "final answer no_verdict" in record["reason_detail"]

    def test_a_pr_review_seat_never_reads_a_tiebreak_decision(self, gate_dirs, monkeypatch):
        answer = TIEBREAK_START_ANSWER.replace('"outcome": "START"', '"verdict": "pass", "outcome": "START"')
        _result, record, _calls = _run_gate(gate_dirs, monkeypatch, _lane_with_answer(answer))
        assert record["status"] == "unavailable"
        assert "final answer no_verdict" in record["reason_detail"]

    def test_the_fence_rule_keeps_json_and_unlabelled_fences(self):
        import final_answer_verdict as fav

        text = (
            "a\n```vnx-plan-verdict\n{\"verdict\": \"pass\"}\n```\n"
            "```python\nx = {\"verdict\": \"pass\"}\n```\n"
            "```json\n{\"verdict\": \"fail\"}\n```\n"
            "```\n{\"verdict\": \"blocked\"}\n```\n"
            "```vnx-plan-tiebreak\n{\"verdict\": \"pass\"\n"
        )
        kept = fav.without_foreign_fences(text)
        assert "vnx-plan-verdict" not in kept and "python" not in kept
        assert "vnx-plan-tiebreak" not in kept
        assert '{"verdict": "fail"}' in kept and '{"verdict": "blocked"}' in kept
        assert gate_artifacts._final_answer_review_verdict(_final_answer(PASS_BLOCK)) == PASS_BLOCK

    def test_a_pr_review_seat_never_reads_free_text(self, gate_dirs, monkeypatch):
        answer = "Verdict: pass. No findings, no residual risk.\n"
        _result, record, _calls = _run_gate(gate_dirs, monkeypatch, _lane_with_answer(answer))
        assert record["status"] == "unavailable"
        assert "final answer no_verdict" in record["reason_detail"]

    def test_a_plan_seat_never_reads_a_pr_review_verdict(self, tmp_path):
        calls = []

        def dispatcher(provider, model_arg, instruction, dispatch_id):
            calls.append(dispatch_id)
            if dispatch_id.endswith("-reextract"):
                return "no block here either"
            _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, _final_answer(PASS_BLOCK)))
            return PLAN_SEAT_REPORT

        result = pgp._dispatch_one(
            dispatcher, {"label": "deepseek", "provider": "deepseek", "model_arg": "m"},
            "instruction", "plan-gate-trk-deepseek-aaaa1111", tmp_path,
        )
        assert result.seat_status == pgp.SEAT_PROSE_NO_FENCE
        assert result.parse_error is True
        assert result.final_answer_refused == ""

    def test_the_tiebreaker_never_reads_a_plan_verdict(self, tmp_path):
        def dispatcher(provider, model_arg, instruction, dispatch_id):
            _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, PLAN_PASS_ANSWER))
            return TIEBREAK_REPORT

        with pytest.raises(pgt.TiebreakerParseError):
            pgt.run_tiebreaker(
                doc_text="## Problem\n", track_id="trk", project_id="p1", round_number=3,
                model_arg="deepseek-v4-pro", dispatcher=dispatcher, data_dir=str(tmp_path),
            )

    def test_a_tiebreak_block_the_contract_rejects_is_never_overridden(self, tmp_path):
        """A report WITH a fence keeps its own reading; the final answer does not replace it."""
        rejected = (
            TIEBREAK_REPORT + "\n```vnx-plan-tiebreak\n"
            '{"outcome": "START", "findings": ["a", "b"], "rationale": "r"}\n```\n'
        )

        def dispatcher(provider, model_arg, instruction, dispatch_id):
            _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, TIEBREAK_START_ANSWER))
            return rejected

        with pytest.raises(pgt.TiebreakerParseError, match="findings"):
            pgt.run_tiebreaker(
                doc_text="## Problem\n", track_id="trk", project_id="p1", round_number=3,
                model_arg="deepseek-v4-pro", dispatcher=dispatcher, data_dir=str(tmp_path),
            )

    def test_an_unparseable_plan_fence_in_the_report_keeps_its_booking(self, tmp_path):
        report = PLAN_SEAT_REPORT + "\n```vnx-plan-verdict\nnot json at all\n```\n"

        def dispatcher(provider, model_arg, instruction, dispatch_id):
            _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, PLAN_PASS_ANSWER))
            return report

        result = pgp._dispatch_one(
            dispatcher, {"label": "glm", "provider": "glm", "model_arg": "m"},
            "instruction", "plan-gate-trk-glm-bbbb2222", tmp_path,
        )
        assert result.seat_status == pgp.SEAT_UNPARSEABLE_FENCE


# ---------------------------------------------------------------------------
# 4. Binding: dispatch id, head, file
# ---------------------------------------------------------------------------


class TestBinding:
    def test_a_foreign_dispatchs_archive_is_refused(self, gate_dirs, monkeypatch):
        """The archive under this run's name holds another dispatch's events."""
        foreign = "deepseek-gate-pr2069-1792098731"

        def lane(data_dir, dispatch_id):
            _write_archive(data_dir, dispatch_id, _run_events(foreign, _final_answer(PASS_BLOCK)))
            return LANE_REPORT.format(dispatch_id=dispatch_id)

        _result, record, _calls = _run_gate(gate_dirs, monkeypatch, lane)
        assert record["status"] == "unavailable"
        assert "final answer refused" in record["reason_detail"]
        assert foreign in record["reason_detail"]

    def test_another_dispatchs_archive_beside_this_one_is_never_read(self, gate_dirs, monkeypatch):
        """Only the exact name counts: a longer id that contains this one is not this run's archive."""
        def lane(data_dir, dispatch_id):
            neighbour = f"{dispatch_id}-retry"
            _write_archive(data_dir, neighbour, _run_events(neighbour, _final_answer(PASS_BLOCK)))
            return LANE_REPORT.format(dispatch_id=dispatch_id)

        _result, record, _calls = _run_gate(gate_dirs, monkeypatch, lane)
        assert record["status"] == "unavailable"
        assert "final answer unavailable" in record["reason_detail"]

    def test_a_run_without_a_head_is_refused(self, gate_dirs, monkeypatch):
        _result, record, _calls = _run_gate(
            gate_dirs, monkeypatch, _lane_with_answer(_final_answer(PASS_BLOCK)), commit_sha="",
        )
        assert record["status"] == "unavailable"
        assert "final answer refused" in record["reason_detail"]
        assert "head sha" in record["reason_detail"]

    def test_a_report_carrying_a_recovered_marker_of_another_head_is_refused(self, tmp_path):
        """A lane report never carries the runner's marker; one that does is not this run's report.

        The text holds the marker of another dispatch and head and no verdict
        block, so the report is read as verdict-less and the fallback is asked.
        """
        dispatch_id = "deepseek-gate-pr2066-1792100303"
        _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, _final_answer(PASS_BLOCK)))
        stdout = (
            LANE_REPORT.format(dispatch_id=dispatch_id)
            + "\n## Verdict recovered from the final answer\n\n"
            + f"<!-- vnx-final-answer-verdict dispatch_id=deepseek-gate-pr2069-1792098731 head={OTHER_HEAD} -->\n"
        )
        result, _payload_ = _materialize(tmp_path, stdout, final_answer_dir=tmp_path)
        assert result["status"] == "unavailable"
        assert "final answer refused" in result["reason_detail"]
        assert "recovered-verdict marker" in result["reason_detail"]

    @pytest.mark.parametrize("events_for", [
        lambda did: _run_events(did, _final_answer(PASS_BLOCK), terminal="T1"),
        lambda did: _run_events(did, _final_answer(PASS_BLOCK))
        + [_result_event(did, _final_answer(FAIL_BLOCK), seq=10709)],
    ])
    def test_another_terminal_or_two_final_answers_are_refused(self, tmp_path, events_for):
        import final_answer_verdict as fav

        dispatch_id = "deepseek-gate-pr2066-1792100303"
        _write_archive(tmp_path, dispatch_id, events_for(dispatch_id))
        found = fav.recover_verdict(
            tmp_path, dispatch_id, report_text=LANE_REPORT,
            parse=gate_artifacts._final_answer_review_verdict, head_sha=HEAD,
        )
        assert found.outcome == fav.REFUSED

    @pytest.mark.parametrize("bad_id", ["", "../deepseek-gate-pr2066-1", "a/b", " deepseek-gate-pr2066-1"])
    def test_a_dispatch_id_that_names_no_single_file_reads_nothing(self, tmp_path, bad_id):
        import final_answer_verdict as fav

        found = fav.recover_verdict(
            tmp_path, bad_id, report_text=LANE_REPORT,
            parse=gate_artifacts._final_answer_review_verdict,
        )
        assert found.outcome == fav.UNAVAILABLE

    def test_the_seat_terminal_is_the_one_the_lanes_dispatch_under(self, tmp_path, monkeypatch):
        """The archive dir the reader looks in is the terminal the dispatcher hands provider_dispatch."""
        import final_answer_verdict as fav

        assert fav.HARNESS_SEAT_TERMINAL == pgp._PANEL_TERMINAL_ID
        dispatch_id = "deepseek-gate-pr2066-1792100303"
        (tmp_path / "unified_reports").mkdir()
        (tmp_path / "unified_reports" / f"{dispatch_id}.md").write_text(LANE_REPORT, encoding="utf-8")
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(pgp.subprocess, "run", fake_run)
        dispatch = pgp._make_default_dispatcher(str(tmp_path), 60, role="review-gate")
        dispatch("deepseek", "deepseek-flash", "instruction", dispatch_id)
        cmd = seen["cmd"]
        assert cmd[cmd.index("--terminal-id") + 1] == fav.HARNESS_SEAT_TERMINAL


# ---------------------------------------------------------------------------
# 5. One truth on disk
# ---------------------------------------------------------------------------


class TestOneTruthOnDisk:
    def test_the_evidence_report_carries_the_recovered_verdict(self, gate_dirs, monkeypatch):
        _result, record, calls = _run_gate(
            gate_dirs, monkeypatch, _lane_with_answer(_final_answer(PASS_BLOCK)),
        )
        report = Path(record["report_path"]).read_text(encoding="utf-8")
        lane_report = LANE_REPORT.format(dispatch_id=calls[0])
        # The model's own text is in the report, unchanged.
        assert lane_report.strip() in report
        assert report.count("## Verdict recovered from the final answer") == 1
        assert f"dispatch_id={calls[0]} head={HEAD}" in report
        fence = closure_verifier._extract_report_verdict_fence(report)
        assert fence == PASS_BLOCK
        assert closure_verifier._count_report_blocking_indicators(report) == 0
        door = _door(record)
        assert door["verdict"] == "GO", door
        ok, reason = forge_gate_publisher._proven_pass_on_head(record, HEAD)
        assert ok, reason
        ok_other, _ = forge_gate_publisher._proven_pass_on_head(record, OTHER_HEAD)
        assert not ok_other

    def test_a_recovered_fail_is_booked_as_a_fail_and_the_door_agrees(self, gate_dirs, monkeypatch):
        _result, record, _calls = _run_gate(
            gate_dirs, monkeypatch, _lane_with_answer(_final_answer(FAIL_BLOCK)),
        )
        assert record["verdict_source"] == "final_answer"
        assert len(record["blocking_findings"]) == 1
        report = Path(record["report_path"]).read_text(encoding="utf-8")
        assert closure_verifier._extract_report_verdict_fence(report)["verdict"] == "fail"
        assert _door(record)["verdict"] == "NO-GO"

    def test_a_backtick_in_a_finding_never_closes_the_fence(self, tmp_path):
        import final_answer_verdict as fav

        block = {"verdict": "pass", "findings": [
            {"severity": "info", "message": "use ```json {} ``` here", "file_path": "", "line": 0}],
            "residual_risk": ""}
        recovery = fav.FinalAnswerVerdict(outcome=fav.RECOVERED, detail="", verdict=block,
                                          dispatch_id="d-1", head_sha=HEAD)
        section = fav.recovered_section(recovery, block)
        assert closure_verifier._extract_report_verdict_fence(section) == block

    def test_a_verdict_from_the_report_is_marked_report(self, tmp_path):
        stdout = LANE_REPORT.format(dispatch_id="x") + "\n```json\n" + json.dumps(PASS_BLOCK) + "\n```\n"
        result, _payload_ = _materialize(tmp_path, stdout, final_answer_dir=tmp_path)
        assert result["status"] == "completed"
        assert result["verdict_source"] == "report"
        assert "evidence_source" not in result
        report = Path(result["report_path"]).read_text(encoding="utf-8")
        assert "## Verdict recovered from the final answer" not in report

    def test_a_second_booking_of_the_same_dispatch_appends_no_second_block(self, tmp_path):
        dispatch_id = "deepseek-gate-pr2066-1792100303"
        _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, _final_answer(PASS_BLOCK)))
        stdout = LANE_REPORT.format(dispatch_id=dispatch_id)
        first, _p1 = _materialize(tmp_path, stdout, final_answer_dir=tmp_path)
        second, _p2 = _materialize(tmp_path, stdout, final_answer_dir=tmp_path)
        assert first["report_path"] == second["report_path"]
        report = Path(second["report_path"]).read_text(encoding="utf-8")
        assert report.count("## Verdict recovered from the final answer") == 1
        assert report.count("<!-- vnx-final-answer-verdict") == 1
        assert second["verdict_source"] == "final_answer"

    def test_appending_the_section_twice_is_idempotent(self):
        import final_answer_verdict as fav

        recovery = fav.FinalAnswerVerdict(outcome=fav.RECOVERED, detail="", verdict=PASS_BLOCK,
                                          dispatch_id="d-1", head_sha=HEAD)
        section = fav.recovered_section(recovery, PASS_BLOCK)
        once = fav.with_recovered_section(LANE_REPORT, section)
        twice = fav.with_recovered_section(once, section)
        assert twice == once
        assert once.startswith(LANE_REPORT.rstrip("\n"))

    def test_the_vertex_and_subprocess_strategies_never_read_a_final_answer(self, tmp_path):
        """No final_answer_dir, no fallback: the old no_verdict_block booking stands."""
        dispatch_id = "deepseek-gate-pr2066-1792100303"
        _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, _final_answer(PASS_BLOCK)))
        result, _payload_ = _materialize(tmp_path, LANE_REPORT.format(dispatch_id=dispatch_id))
        assert result["status"] == "unavailable"
        assert result["reason_detail"].startswith("no_verdict_block:")
        assert "final answer" not in result["reason_detail"]


# ---------------------------------------------------------------------------
# 7. Order against _reextract_verdict
# ---------------------------------------------------------------------------


def _panel_dispatcher(tmp_path, final_text, calls, *, report=PLAN_SEAT_REPORT, archive=True, **result_kw):
    def dispatcher(provider, model_arg, instruction, dispatch_id):
        calls.append(dispatch_id)
        if dispatch_id.endswith("-reextract"):
            return '```vnx-plan-verdict\n{"verdict": "pass", "blocking_findings": [], "rationale": "re"}\n```'
        if archive:
            _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, final_text, **result_kw))
        return report
    return dispatcher


def _one_seat_panel(tmp_path, dispatcher, monkeypatch):
    monkeypatch.setenv("VNX_PANEL_RETRY", "0")
    return pgp.run_panel(
        doc_text="## Problem\nx\n", track_id="trk", project_id="p1",
        panel=[{"label": "deepseek", "provider": "deepseek", "model_arg": "deepseek-flash"}],
        dispatcher=dispatcher, data_dir=str(tmp_path), seat_ledger_path=tmp_path / "seats.ndjson",
    )


class TestOrderAgainstReextraction:
    def test_a_recovered_seat_is_never_rebooked_by_the_reextraction(self, tmp_path, monkeypatch):
        calls = []
        revise = PLAN_PASS_ANSWER.replace('"pass"', '"revise"').replace("[]", '["one gap"]')
        out = _one_seat_panel(tmp_path, _panel_dispatcher(tmp_path, revise, calls), monkeypatch)
        seat = out["panelists"][0]
        assert seat["seat_status"] == pgp.SEAT_SCORED_VIA_FINAL_ANSWER
        assert seat["verdict"] == "revise"
        assert seat["reextraction_dispatch_id"] == ""
        assert not any(c.endswith("-reextract") for c in calls)

    def test_the_reextraction_hands_a_recovered_seat_back_untouched(self):
        def dispatcher(*_args):
            raise AssertionError("a recovered seat must never be re-extracted")

        seat = pgp.PanelistResult(
            label="deepseek", provider="deepseek", verdict="revise", dispatched=True,
            seat_status=pgp.SEAT_SCORED_VIA_FINAL_ANSWER,
        )
        member = {"label": "deepseek", "provider": "deepseek", "model_arg": "m"}
        out = pgp._reextract_verdict(dispatcher, member, seat, "did-reextract")
        assert out is seat
        assert out.verdict == "revise"
        assert out.reextraction_dispatch_id == ""

    def test_a_run_r6_refused_gets_no_verdict_from_the_reextraction(self, tmp_path, monkeypatch):
        calls = []
        out = _one_seat_panel(
            tmp_path, _panel_dispatcher(tmp_path, PLAN_PASS_ANSWER, calls, subtype="error_max_turns"),
            monkeypatch,
        )
        seat = out["panelists"][0]
        assert seat["seat_status"] == pgp.SEAT_PROSE_NO_FENCE
        assert "did not end normally" in seat["final_answer_refused"]
        assert not any(c.endswith("-reextract") for c in calls)
        assert out["decision"] == "INFRA_FAIL"
        ledger = [json.loads(line) for line in (tmp_path / "seats.ndjson").read_text().splitlines()]
        assert "did not end normally" in ledger[-1]["final_answer_refused"]

    def test_the_reextraction_hands_a_refused_seat_back_untouched(self):
        def dispatcher(*_args):
            raise AssertionError("a seat R6 refused must never be re-extracted")

        seat = pgp.PanelistResult(
            label="deepseek", provider="deepseek", dispatched=True, parse_error=True,
            seat_status=pgp.SEAT_PROSE_NO_FENCE, raw_text=PLAN_SEAT_REPORT,
            final_answer_refused="the run did not end normally: result subtype 'error_max_turns'",
        )
        member = {"label": "deepseek", "provider": "deepseek", "model_arg": "m"}
        out = pgp._reextract_verdict(dispatcher, member, seat, "did-reextract")
        assert out is seat
        assert out.seat_status == pgp.SEAT_PROSE_NO_FENCE

    @pytest.mark.parametrize("final_text,archive", [
        ("I wrote my review to the report file.", True),     # ended normally, no block
        ("```vnx-plan-verdict\n{\"verdict\": \"maybe\"}\n```", True),  # malformed block
        (PLAN_PASS_ANSWER, False),                           # no archive at all (kimi, older runs)
    ])
    def test_without_a_refusal_the_reextraction_still_runs(self, tmp_path, monkeypatch, final_text, archive):
        """OI-1434 stays: a normally ended run with no verdict in its final answer is re-extracted."""
        calls = []
        out = _one_seat_panel(
            tmp_path, _panel_dispatcher(tmp_path, final_text, calls, archive=archive), monkeypatch,
        )
        seat = out["panelists"][0]
        assert seat["seat_status"] == pgp.SEAT_SCORED_VIA_REEXTRACTION
        assert seat["final_answer_refused"] == ""
        assert sum(c.endswith("-reextract") for c in calls) == 1


# ---------------------------------------------------------------------------
# 8. Archive forms and a malformed final answer
# ---------------------------------------------------------------------------


class TestArchiveForms:
    def test_a_gzipped_archive_is_read(self, gate_dirs, monkeypatch):
        def lane(data_dir, dispatch_id):
            _write_archive(data_dir, dispatch_id, _run_events(dispatch_id, _final_answer(PASS_BLOCK)), gz=True)
            return LANE_REPORT.format(dispatch_id=dispatch_id)

        _result, record, _calls = _run_gate(gate_dirs, monkeypatch, lane)
        assert record["status"] == "completed"
        assert record["verdict_source"] == "final_answer"

    def test_a_malformed_block_in_the_final_answer_keeps_no_verdict_block(self, gate_dirs, monkeypatch):
        malformed = "Verdict below.\n\n```json\n{\"verdict\": \"pass\", \"findings\": [\n```"
        _result, record, _calls = _run_gate(gate_dirs, monkeypatch, _lane_with_answer(malformed))
        assert record["status"] == "unavailable"
        assert record["reason_detail"].startswith("no_verdict_block:")
        assert "final answer no_verdict" in record["reason_detail"]

    def test_a_verdict_outside_pass_fail_blocked_is_not_a_verdict(self, gate_dirs, monkeypatch):
        answer = _final_answer({"verdict": "approve", "findings": []})
        _result, record, _calls = _run_gate(gate_dirs, monkeypatch, _lane_with_answer(answer))
        assert record["status"] == "unavailable"

    def test_a_torn_line_does_not_hide_the_result_event(self, tmp_path):
        import final_answer_verdict as fav

        dispatch_id = "deepseek-gate-pr2066-1792100303"
        path = _write_archive(tmp_path, dispatch_id, _run_events(dispatch_id, _final_answer(PASS_BLOCK)))
        path.write_text('{"type": "text", "dispa\n' + path.read_text(encoding="utf-8"), encoding="utf-8")
        found = fav.recover_verdict(
            tmp_path, dispatch_id, report_text=LANE_REPORT,
            parse=gate_artifacts._final_answer_review_verdict, head_sha=HEAD,
        )
        assert found.recovered
        assert found.verdict["verdict"] == "pass"

    def test_an_archive_without_a_result_event_reads_nothing(self, tmp_path):
        import final_answer_verdict as fav

        dispatch_id = "kimi-gate-pr2066-1792098586"
        events = [e for e in _run_events(dispatch_id, "x") if e["type"] != "result"]
        _write_archive(tmp_path, dispatch_id, events)
        found = fav.recover_verdict(
            tmp_path, dispatch_id, report_text=LANE_REPORT,
            parse=gate_artifacts._final_answer_review_verdict, head_sha=HEAD,
        )
        assert found.outcome == fav.UNAVAILABLE


# ---------------------------------------------------------------------------
# Round 2 (codex_gate warnings on 6beaf1a8)
#
# 1. plan_gate_panel.py:1607 — a claude plan seat's stream was archived only
#    when the NEXT dispatch on the terminal rotated the boundary, after
#    _seat_final_answer had looked for it. Measured on 2026-10-15:
#    plan-gate-reparatiegolf-opus-66671476 wrote its result event at
#    17:05:26.5Z, its report at 17:05:27Z, and the next seat's first event
#    (deepseek-8911dd83) came at 17:05:29.3Z. The tests below run the real
#    lane: _make_default_dispatcher's claude branch, ClaudeSubprocessAdapter,
#    spawn_claude, SubprocessAdapter and EventStore. Only the claude binary is
#    a script on PATH.
#
#    The seat tests replace dispatch_govern.govern with a stand-in that leaves
#    the worker's report as written: the shape codex reproduced (a fenceless
#    report reaching the panel). The real govern replaces a fenceless
#    plan-reviewer report with a synthesized body, which the panel books
#    lane_no_answer before R6 is asked; that measured state is pinned by
#    test_with_the_real_govern_a_fenceless_claude_seat_is_lane_no_answer.
# 2. final_answer_verdict.py:242 — a damaged gzip body raised zlib.error out
#    of recover_verdict.
# ---------------------------------------------------------------------------


_FAKE_CLAUDE = """\
import json, os, sys, time

spec = json.load(open(os.environ["R6_FAKE_CLAUDE_SPEC"], encoding="utf-8"))
report = os.environ["VNX_REPORT_PATH"]
dispatch_id = os.environ["VNX_CURRENT_DISPATCH_ID"]
reextract = dispatch_id.endswith("-reextract")
with open(spec["calls"], "a", encoding="utf-8") as log:
    log.write(dispatch_id + "\\n")
os.makedirs(os.path.dirname(report), exist_ok=True)
with open(report, "w", encoding="utf-8") as fh:
    fh.write(spec["extract_report"] if reextract else spec["seat_report"])


def emit(event):
    sys.stdout.write(json.dumps(event) + "\\n")
    sys.stdout.flush()


emit({"type": "system", "subtype": "init", "session_id": "s-r6", "model": "claude-opus-5-5"})
if spec.get("foreign_event") and not reextract:
    # A parallel dispatch writing into the same ring buffer (OI-1869, OI-1930):
    # wait until the lane stored this run's init, then append before this
    # run's final answer.
    live = os.path.join(os.environ["VNX_DATA_DIR"], "events", "plan-gate.ndjson")
    deadline = time.time() + 10
    while time.time() < deadline:
        if os.path.exists(live) and dispatch_id in open(live, encoding="utf-8").read():
            break
        time.sleep(0.02)
    with open(live, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(spec["foreign_event"]) + "\\n")
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Review written."}]}})
emit({"type": "result", "subtype": "success", "is_error": False,
      "result": spec["final_answer"], "session_id": "s-r6",
      "usage": {"input_tokens": 10, "output_tokens": 5}})
"""

# A full-contract seat report without a verdict fence: the measured prose_no_fence shape.
CLAUDE_SEAT_REPORT = (
    "# Plan review\n\n## Summary\n\nI reviewed the plan. It is sound, the layering is coherent "
    "and every acceptance case has a test. My verdict is pass.\n\n## Changes\n\nNone: a plan "
    "review changes no file.\n\n## Verification\n\nRead the plan and checked each acceptance "
    "case against the approach.\n\n## Open Items\n\nNone\n"
)
EXTRACTED_REVISE = (
    '```vnx-plan-verdict\n{"verdict": "revise", "blocking_findings": ["one gap"], '
    '"rationale": "re-extracted"}\n```\n'
)
PLAN_BLOCK_ANSWER = (
    'Blocked.\n\n```vnx-plan-verdict\n{"verdict": "block", "blocking_findings": ["x"], '
    '"rationale": "another seat"}\n```'
)
CLAUDE_PANEL = [{"label": "opus", "provider": "claude", "model_arg": "opus"}]


class _FakeClaude:
    def __init__(self, tmp_path: Path):
        self.data_dir = tmp_path / "data"
        self.data_dir.mkdir()
        self._spec = tmp_path / "fake-claude.json"
        self._calls = tmp_path / "claude-calls.log"

    def configure(self, final_answer, *, foreign_event=None) -> Path:
        self._spec.write_text(json.dumps({
            "calls": str(self._calls), "seat_report": CLAUDE_SEAT_REPORT,
            "extract_report": EXTRACTED_REVISE, "final_answer": final_answer,
            "foreign_event": foreign_event,
        }), encoding="utf-8")
        return self.data_dir

    def calls(self):
        return self._calls.read_text(encoding="utf-8").split() if self._calls.exists() else []


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """The claude CLI replaced by a script; the real lane, adapter and EventStore run around it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n" + _FAKE_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    fake = _FakeClaude(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(checkout))
    monkeypatch.setenv("R6_FAKE_CLAUDE_SPEC", str(fake._spec))
    monkeypatch.setenv("VNX_PANEL_RETRY", "0")
    for key in ("VNX_TOTAL_DEADLINE", "VNX_CHUNK_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    return fake


def _govern_keeps_the_worker_report(spec, raw, *, lane):
    """dispatch_govern.govern stand-in: the seat's report stays as the worker wrote it."""
    return None


@pytest.fixture
def worker_report_kept(monkeypatch):
    import dispatch_govern

    monkeypatch.setattr(dispatch_govern, "govern", _govern_keeps_the_worker_report)


def _claude_panel(fake, final_answer, **kw):
    data_dir = fake.configure(final_answer, **kw)
    return pgp.run_panel(
        doc_text="## Problem\nx\n", track_id="trk", project_id="p1", panel=CLAUDE_PANEL,
        data_dir=str(data_dir), seat_ledger_path=data_dir / "seats.ndjson",
    )


def _damaged_gzip(events) -> bytes:
    """A valid gzip header over a deflate body whose first block has the reserved type 3."""
    good = gzip.compress("".join(json.dumps(e) + "\n" for e in events).encode("utf-8"))
    return good[:10] + bytes([good[10] | 0x06]) + good[11:]


def _write_damaged_archive(data_dir: Path, dispatch_id: str, final_text) -> Path:
    archive_dir = Path(data_dir) / "events" / "archive" / TERMINAL
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{dispatch_id}.ndjson.gz"
    path.write_bytes(_damaged_gzip(_run_events(dispatch_id, final_text)))
    return path


class TestRoundTwoRedRuns:
    def test_a_claude_plan_seat_scores_from_its_final_answer(self, fake_claude, worker_report_kept):
        """Finding 1: report without a fence, final answer with one -> scored_via_final_answer."""
        out = _claude_panel(fake_claude, PLAN_PASS_ANSWER)
        seat = out["panelists"][0]
        assert seat["seat_status"] == pgp.SEAT_SCORED_VIA_FINAL_ANSWER, seat
        assert seat["verdict"] == "pass"
        assert seat["final_answer_refused"] == ""
        assert not any(c.endswith("-reextract") for c in fake_claude.calls())
        assert out["decision"] == "PASS"

    def test_the_seat_stream_is_archived_before_the_lane_hands_back_its_report(self, fake_claude):
        """Finding 1, the timing: the archive exists when the lane returns, not at the next dispatch.

        With the real govern: the archive does not depend on what govern makes of the report.
        """
        data_dir = fake_claude.configure(PLAN_PASS_ANSWER)
        did = "plan-gate-trk-opus-0a1b2c3d"
        report = pgp._make_default_dispatcher(str(data_dir), 60)("claude", "opus", "## Problem\n", did)
        assert f"dispatch_id: {did}" in report
        archive = data_dir / "events" / "archive" / TERMINAL / f"{did}.ndjson"
        assert archive.is_file(), "no archive when the lane handed back the seat's report"
        events = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
        assert {e["dispatch_id"] for e in events} == {did}
        assert [e["type"] for e in events].count("result") == 1
        assert (data_dir / "events" / f"{TERMINAL}.ndjson").read_text(encoding="utf-8") == ""

    def test_a_damaged_gzip_body_is_unavailable_not_a_raise(self, tmp_path):
        """Finding 2: a valid gzip header over a damaged deflate body raised zlib.error."""
        import final_answer_verdict as fav

        dispatch_id = "deepseek-gate-pr2066-1792100303"
        path = _write_damaged_archive(tmp_path, dispatch_id, _final_answer(PASS_BLOCK))
        with pytest.raises(zlib.error):  # the damage is in the archive itself
            with event_store.open_archive_text(path) as handle:
                handle.read()
        found = fav.recover_verdict(
            tmp_path, dispatch_id, report_text=LANE_REPORT,
            parse=gate_artifacts._final_answer_review_verdict, head_sha=HEAD,
        )
        assert found.outcome == fav.UNAVAILABLE
        assert "zlib.error" in found.detail
        assert found.archive_path == path

    def test_the_runner_books_a_damaged_archive_and_never_leaves_the_request_executing(
        self, gate_dirs, monkeypatch,
    ):
        def lane(data_dir, dispatch_id):
            _write_damaged_archive(data_dir, dispatch_id, _final_answer(PASS_BLOCK))
            return LANE_REPORT.format(dispatch_id=dispatch_id)

        result, record, _calls = _run_gate(gate_dirs, monkeypatch, lane)
        assert record["status"] == "unavailable"
        assert record["reason"] == "validation_failed"
        assert record["reason_detail"].startswith("no_verdict_block:")
        assert "final answer unavailable" in record["reason_detail"]
        assert "zlib.error" in record["reason_detail"]
        assert "verdict_source" not in record
        request = json.loads(
            (gate_dirs["state"] / "review_gates" / "requests" / "pr-2066-deepseek_gate.json")
            .read_text(encoding="utf-8")
        )
        assert request["status"] != "executing"
        assert result["status"] == "unavailable"


class TestClaudeSeatStream:
    def test_a_stream_shared_with_another_dispatch_is_never_read(self, fake_claude, worker_report_kept):
        """OI-1869/OI-1930: another dispatch's result in the live stream is never this seat's answer.

        The shared stream is not archived at the end of the run, so the seat
        reads as before this round: no archive, no refusal, re-extraction.
        Archived as a whole, the other dispatch's events under this seat's id
        would have been refused, and a refused seat is never re-extracted.
        """
        foreign = _result_event("plan-gate-other-deepseek-9f8e7d6c", PLAN_BLOCK_ANSWER)
        out = _claude_panel(fake_claude, PLAN_PASS_ANSWER, foreign_event=foreign)
        seat = out["panelists"][0]
        assert seat["seat_status"] == pgp.SEAT_SCORED_VIA_REEXTRACTION, seat
        assert seat["verdict"] == "revise"
        assert seat["final_answer_refused"] == ""
        assert sum(c.endswith("-reextract") for c in fake_claude.calls()) == 1

    def test_a_claude_seat_without_a_verdict_in_its_final_answer_is_re_extracted(
        self, fake_claude, worker_report_kept,
    ):
        out = _claude_panel(fake_claude, "I wrote my review to the report file.")
        seat = out["panelists"][0]
        assert seat["seat_status"] == pgp.SEAT_SCORED_VIA_REEXTRACTION
        assert seat["final_answer_refused"] == ""

    def test_with_the_real_govern_a_fenceless_claude_seat_is_lane_no_answer(self, fake_claude):
        """The measured state on the real lane, an open item of this round.

        dispatch_govern replaces a plan-reviewer report without a fence by a
        synthesized body (pinned since 2026-06-22 by
        test_govern_spec_role_plan_reviewer_fenceless_report_becomes_synthesized).
        Since OI-1066 the panel books that body lane_no_answer before it parses
        anything, so neither R6 nor the OI-1434 re-extraction reaches a claude
        seat, even with its stream archived.
        """
        out = _claude_panel(fake_claude, PLAN_PASS_ANSWER)
        seat = out["panelists"][0]
        assert seat["seat_status"] == pgp.SEAT_LANE_NO_ANSWER
        assert not any(c.endswith("-reextract") for c in fake_claude.calls())
        archive = fake_claude.data_dir / "events" / "archive" / TERMINAL
        assert len(list(archive.glob("plan-gate-trk-opus-*.ndjson"))) == 1


def _live_stream(events_dir: Path, lines) -> Path:
    events_dir.mkdir(parents=True, exist_ok=True)
    live = events_dir / f"{TERMINAL}.ndjson"
    live.write_text("".join(line if isinstance(line, str) else json.dumps(line) + "\n" for line in lines),
                    encoding="utf-8")
    return live


class TestEndOfRunArchive:
    """EventStore.archive_completed_stream: the teardown the claude seat's lane lacked."""

    DID = "plan-gate-trk-opus-11112222"

    def test_a_stream_of_one_dispatch_is_archived_and_the_live_file_cleared(self, tmp_path):
        events_dir = tmp_path / "events"
        events = _run_events(self.DID, PLAN_PASS_ANSWER)
        live = _live_stream(events_dir, events)
        live_mode = stat.S_IMODE(live.stat().st_mode)
        store = event_store.EventStore(events_dir=events_dir)
        dest = store.archive_completed_stream(TERMINAL, self.DID)
        assert dest == events_dir / "archive" / TERMINAL / f"{self.DID}.ndjson"
        assert [json.loads(line) for line in dest.read_text(encoding="utf-8").splitlines()] == events
        assert live.read_text(encoding="utf-8") == ""
        # No scratch file of any name beside the archive, and the archive keeps
        # the live stream's mode, as archive()'s copy2 gives it (not mkstemp's 0600).
        assert [p.name for p in dest.parent.iterdir()] == [dest.name]
        assert stat.S_IMODE(dest.stat().st_mode) == live_mode
        # A second call finds nothing of this dispatch left: idempotent.
        assert store.archive_completed_stream(TERMINAL, self.DID) is None
        assert len(dest.read_text(encoding="utf-8").splitlines()) == len(events)

    @pytest.mark.parametrize("other_id", ["plan-gate-trk-deepseek-33334444", ""])
    def test_a_shared_stream_is_left_for_the_boundary_guard(self, tmp_path, other_id):
        events_dir = tmp_path / "events"
        mixed = _run_events(self.DID, PLAN_PASS_ANSWER)
        mixed.insert(3, _event(other_id, "text", {"text": "a parallel dispatch"}, 9))
        live = _live_stream(events_dir, mixed)
        before = live.read_bytes()
        store = event_store.EventStore(events_dir=events_dir)
        assert store.archive_completed_stream(TERMINAL, self.DID) is None
        assert live.read_bytes() == before
        assert not (events_dir / "archive" / TERMINAL / f"{self.DID}.ndjson").exists()

    def test_an_archive_the_dispatch_already_has_is_extended_not_replaced(self, tmp_path):
        """A parallel dispatch's boundary guard archived the head; the tail is appended to it."""
        import final_answer_verdict as fav

        events = _run_events(self.DID, PLAN_PASS_ANSWER)
        head_path = _write_archive(tmp_path, self.DID, events[:3])
        _live_stream(tmp_path / "events", events[3:])
        dest = event_store.EventStore(events_dir=tmp_path / "events").archive_completed_stream(
            TERMINAL, self.DID,
        )
        assert dest == head_path
        assert [json.loads(line) for line in dest.read_text(encoding="utf-8").splitlines()] == events
        found = fav.recover_verdict(
            tmp_path, self.DID, report_text=PLAN_SEAT_REPORT, parse=pgp._readable_plan_verdict,
        )
        assert found.recovered
        assert found.verdict["verdict"] == "pass"

    def test_a_head_without_a_final_newline_is_not_glued_to_the_tail(self, tmp_path):
        events = _run_events(self.DID, PLAN_PASS_ANSWER)
        head_path = _write_archive(tmp_path, self.DID, events[:2])
        head_path.write_text(head_path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
        _live_stream(tmp_path / "events", events[2:])
        dest = event_store.EventStore(events_dir=tmp_path / "events").archive_completed_stream(
            TERMINAL, self.DID,
        )
        assert [json.loads(line) for line in dest.read_text(encoding="utf-8").splitlines()] == events

    @pytest.mark.parametrize("lines,dispatch_id", [
        ([], DID),                                        # an empty live file
        (['{"type": "text", "dispa\n'], DID),             # only a torn line: nothing of this dispatch
        ([_event(DID, "text", {"text": "x"}, 1)], ""),    # no dispatch id to archive under
    ])
    def test_nothing_of_this_dispatch_archives_nothing(self, tmp_path, lines, dispatch_id):
        live = _live_stream(tmp_path / "events", lines)
        before = live.read_bytes()
        store = event_store.EventStore(events_dir=tmp_path / "events")
        assert store.archive_completed_stream(TERMINAL, dispatch_id) is None
        assert live.read_bytes() == before

    def test_a_missing_live_file_archives_nothing(self, tmp_path):
        store = event_store.EventStore(events_dir=tmp_path / "events")
        assert store.archive_completed_stream(TERMINAL, self.DID) is None

    def test_a_torn_line_travels_with_the_dispatch_it_sits_in(self, tmp_path):
        events = _run_events(self.DID, PLAN_PASS_ANSWER)
        lines = [events[0], '{"type": "text", "dispa\n', *events[1:]]
        _live_stream(tmp_path / "events", lines)
        dest = event_store.EventStore(events_dir=tmp_path / "events").archive_completed_stream(
            TERMINAL, self.DID,
        )
        assert dest is not None
        assert '{"type": "text", "dispa' in dest.read_text(encoding="utf-8")

    def test_a_failed_archive_never_raises_and_keeps_the_live_stream(self, tmp_path):
        events_dir = tmp_path / "events"
        live = _live_stream(events_dir, _run_events(self.DID, PLAN_PASS_ANSWER))
        before = live.read_bytes()
        (events_dir / "archive").mkdir()
        (events_dir / "archive" / TERMINAL).write_text("a file where the archive dir belongs\n")
        store = event_store.EventStore(events_dir=events_dir)
        assert store.archive_completed_stream(TERMINAL, self.DID) is None
        assert live.read_bytes() == before

    @pytest.mark.parametrize("fails_at", ["write", "move"])
    def test_a_failed_write_leaves_no_scratch_file_beside_the_archive(self, tmp_path, monkeypatch, fails_at):
        """The scratch file is the writer's own (OI-1486), so the writer removes it on failure."""
        events_dir = tmp_path / "events"
        live = _live_stream(events_dir, _run_events(self.DID, PLAN_PASS_ANSWER))
        before = live.read_bytes()
        archive_dir = events_dir / "archive" / TERMINAL
        if fails_at == "write":
            def no_sync(_fd):
                raise OSError(28, "No space left on device")
            monkeypatch.setattr(event_store.os, "fsync", no_sync)
            expected = []
        else:
            # A directory where the archive file belongs: the move onto it fails.
            (archive_dir / f"{self.DID}.ndjson").mkdir(parents=True)
            (archive_dir / f"{self.DID}.ndjson" / "occupant").write_text("x\n")
            expected = [f"{self.DID}.ndjson"]
        store = event_store.EventStore(events_dir=events_dir)
        assert store.archive_completed_stream(TERMINAL, self.DID) is None
        assert sorted(p.name for p in archive_dir.iterdir()) == expected
        assert live.read_bytes() == before

    def test_another_writers_scratch_file_is_never_taken_over(self, tmp_path):
        """A scratch named after the destination alone is every writer's scratch (OI-1486)."""
        events = _run_events(self.DID, PLAN_PASS_ANSWER)
        _live_stream(tmp_path / "events", events)
        archive_dir = tmp_path / "events" / "archive" / TERMINAL
        archive_dir.mkdir(parents=True)
        theirs = archive_dir / f"{self.DID}.ndjson.tmp"
        theirs.write_bytes(b"another writer's half-written bytes\n")
        dest = event_store.EventStore(events_dir=tmp_path / "events").archive_completed_stream(
            TERMINAL, self.DID,
        )
        assert [json.loads(line) for line in dest.read_text(encoding="utf-8").splitlines()] == events
        assert theirs.read_bytes() == b"another writer's half-written bytes\n"

    def test_the_next_dispatch_starts_a_fresh_stream(self, tmp_path):
        events_dir = tmp_path / "events"
        _live_stream(events_dir, _run_events(self.DID, PLAN_PASS_ANSWER))
        store = event_store.EventStore(events_dir=events_dir)
        store.append(TERMINAL, {"type": "text", "data": {"text": "x"}}, dispatch_id=self.DID)
        assert store.archive_completed_stream(TERMINAL, self.DID) is not None
        store.append(TERMINAL, {"type": "init", "data": {}}, dispatch_id="plan-gate-trk-glm-55556666")
        live = [json.loads(line) for line in (events_dir / f"{TERMINAL}.ndjson").read_text().splitlines()]
        assert [(e["dispatch_id"], e["sequence"]) for e in live] == [("plan-gate-trk-glm-55556666", 1)]


class TestArchiveReadErrors:
    """Finding 2: nothing reading an archive raises leaves recover_verdict."""

    DID = "deepseek-gate-pr2066-1792100303"

    def _recover(self, tmp_path):
        import final_answer_verdict as fav

        return fav.recover_verdict(
            tmp_path, self.DID, report_text=LANE_REPORT,
            parse=gate_artifacts._final_answer_review_verdict, head_sha=HEAD,
        )

    def _gz(self, tmp_path, payload: bytes) -> Path:
        archive_dir = tmp_path / "events" / "archive" / TERMINAL
        archive_dir.mkdir(parents=True, exist_ok=True)
        path = archive_dir / f"{self.DID}.ndjson.gz"
        path.write_bytes(payload)
        return path

    def test_a_cut_off_gzip_stream_is_unavailable(self, tmp_path):
        good = gzip.compress(
            "".join(json.dumps(e) + "\n" for e in _run_events(self.DID, _final_answer(PASS_BLOCK)))
            .encode("utf-8")
        )
        self._gz(tmp_path, good[: len(good) // 2])
        found = self._recover(tmp_path)
        assert found.outcome == "unavailable"
        assert "EOFError" in found.detail

    def test_a_file_that_is_no_gzip_is_unavailable(self, tmp_path):
        self._gz(tmp_path, b"this is no gzip stream\n" * 4)
        found = self._recover(tmp_path)
        assert found.outcome == "unavailable"
        assert "gzip.BadGzipFile" in found.detail

    def test_an_undecodable_text_is_unavailable(self, tmp_path, monkeypatch):
        import io

        _write_archive(tmp_path, self.DID, _run_events(self.DID, _final_answer(PASS_BLOCK)))
        monkeypatch.setattr(
            event_store, "open_archive_text",
            lambda _path: io.TextIOWrapper(io.BytesIO(b'{"type": "\xff\xfe"}\n'), encoding="utf-8"),
        )
        found = self._recover(tmp_path)
        assert found.outcome == "unavailable"
        assert "UnicodeDecodeError" in found.detail

    def test_an_unreadable_archive_is_unavailable(self, tmp_path, monkeypatch):
        _write_archive(tmp_path, self.DID, _run_events(self.DID, _final_answer(PASS_BLOCK)))

        def _denied(path):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(event_store, "open_archive_text", _denied)
        found = self._recover(tmp_path)
        assert found.outcome == "unavailable"
        assert "PermissionError" in found.detail

    def test_an_archive_that_cannot_be_located_is_unavailable(self, tmp_path, monkeypatch):
        def _denied(directory, dispatch_id):
            raise PermissionError(13, "Permission denied", str(directory))

        monkeypatch.setattr(event_store, "resolve_archive_file", _denied)
        found = self._recover(tmp_path)
        assert found.outcome == "unavailable"
        assert "cannot be located" in found.detail
        assert "PermissionError" in found.detail

    def test_lines_json_cannot_decode_are_named_and_never_raise(self, tmp_path):
        """A torn line and a line nested past the recursion limit (RecursionError, not a ValueError)."""
        archive_dir = tmp_path / "events" / "archive" / TERMINAL
        archive_dir.mkdir(parents=True)
        (archive_dir / f"{self.DID}.ndjson").write_text(
            '{"type": "result", "dispa\n' + "[" * 100000 + "\n", encoding="utf-8",
        )
        found = self._recover(tmp_path)
        assert found.outcome == "unavailable"
        assert "holds no result event (2 line(s) not JSON)" in found.detail

    def test_a_deeply_nested_line_does_not_hide_the_result_event(self, tmp_path):
        path = _write_archive(tmp_path, self.DID, _run_events(self.DID, _final_answer(PASS_BLOCK)))
        path.write_text("[" * 100000 + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        assert self._recover(tmp_path).recovered

    def test_a_plan_seat_with_a_damaged_archive_is_re_extracted(self, tmp_path, monkeypatch):
        calls = []

        def dispatcher(provider, model_arg, instruction, dispatch_id):
            calls.append(dispatch_id)
            if dispatch_id.endswith("-reextract"):
                return EXTRACTED_REVISE
            _write_damaged_archive(tmp_path, dispatch_id, PLAN_PASS_ANSWER)
            return PLAN_SEAT_REPORT

        out = _one_seat_panel(tmp_path, dispatcher, monkeypatch)
        seat = out["panelists"][0]
        assert seat["seat_status"] == pgp.SEAT_SCORED_VIA_REEXTRACTION
        assert seat["final_answer_refused"] == ""
        assert sum(c.endswith("-reextract") for c in calls) == 1

    def test_the_tiebreaker_with_a_damaged_archive_keeps_its_parse_error(self, tmp_path):
        def dispatcher(provider, model_arg, instruction, dispatch_id):
            _write_damaged_archive(tmp_path, dispatch_id, TIEBREAK_START_ANSWER)
            return TIEBREAK_REPORT

        with pytest.raises(pgt.TiebreakerParseError):
            pgt.run_tiebreaker(
                doc_text="## Problem\n", track_id="trk", project_id="p1", round_number=3,
                model_arg="deepseek-v4-pro", dispatcher=dispatcher, data_dir=str(tmp_path),
            )
