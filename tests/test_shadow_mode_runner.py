#!/usr/bin/env python3
"""Tests for shadow_mode_runner.py — F36 Wave C parity harness.

Covers:
- _load_ndjson: normal, empty file, missing file, malformed lines, limit
- _build_shadow_context: event metadata → context dict
- _infer_receipt_status: event_type → status string
- shadow_action_to_log_action: router vocabulary → log vocabulary
- compare_decisions: match/mismatch logic, None actual
- generate_parity_report: stats, by_event_type, empty input
- write_parity_jsonl: NDJSON format, file created
- write_parity_report_md: markdown format, all sections
- run_shadow_mode: integration with dry_run=True (no LLM, no files)
- main() CLI: --dry-run, --json, --limit, exit 0
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure scripts/lib is on the path so llm_decision_router can be imported
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from shadow_mode_runner import (
    _build_decision_index,
    _build_shadow_context,
    _infer_receipt_status,
    _load_ndjson,
    _pair_event_to_decision,
    compare_decisions,
    generate_parity_report,
    main,
    run_shadow_mode,
    shadow_action_to_log_action,
    write_parity_jsonl,
    write_parity_report_md,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

DISPATCH_EVENT = {
    "event_type": "t0_dispatch",
    "dispatch_id": "20260422-103500-f36-A",
    "dispatch_target": "T1",
    "trigger_reason": "new_report",
    "timestamp": "2026-04-22T10:35:00+00:00",
}

WAIT_EVENT = {
    "event_type": "t0_wait",
    "reason": "Waiting for Track B receipt",
    "timestamp": "2026-04-22T10:36:00+00:00",
}

ESCALATE_EVENT = {
    "event_type": "t0_escalate",
    "reason": "Blocker: unresolvable",
    "timestamp": "2026-04-22T10:39:00+00:00",
}

DISPATCH_DECISION = {
    "action": "dispatch",
    "dispatch_id": "20260422-103500-f36-A",
    "timestamp": "2026-04-22T10:35:01+00:00",
    "reasoning": "Dispatched to T1 — new_report",
}

WAIT_DECISION = {
    "action": "wait",
    "timestamp": "2026-04-22T10:36:01+00:00",
    "reasoning": "Waiting for Track B receipt",
}

ESCALATE_DECISION = {
    "action": "escalate",
    "timestamp": "2026-04-22T10:39:01+00:00",
    "reasoning": "Escalation triggered",
}

ALL_EVENTS = [DISPATCH_EVENT, WAIT_EVENT, ESCALATE_EVENT]
ALL_DECISIONS = [DISPATCH_DECISION, WAIT_DECISION, ESCALATE_DECISION]


def make_ndjson(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


# ---------------------------------------------------------------------------
# _load_ndjson
# ---------------------------------------------------------------------------

class TestLoadNdjson:
    def test_returns_empty_for_missing_file(self, tmp_path):
        result = _load_ndjson(tmp_path / "nonexistent.ndjson", limit=10)
        assert result == []

    def test_returns_empty_for_empty_file(self, tmp_path):
        f = tmp_path / "empty.ndjson"
        f.write_text("")
        assert _load_ndjson(f, limit=10) == []

    def test_loads_all_records_under_limit(self, tmp_path):
        f = make_ndjson(tmp_path / "events.ndjson", ALL_EVENTS)
        result = _load_ndjson(f, limit=10)
        assert len(result) == 3

    def test_limit_returns_last_n_records(self, tmp_path):
        f = make_ndjson(tmp_path / "events.ndjson", ALL_EVENTS)
        result = _load_ndjson(f, limit=2)
        assert len(result) == 2
        assert result[0]["event_type"] == "t0_wait"
        assert result[1]["event_type"] == "t0_escalate"

    def test_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "events.ndjson"
        f.write_text(
            json.dumps(DISPATCH_EVENT) + "\n"
            + "NOT_JSON\n"
            + json.dumps(WAIT_EVENT) + "\n"
        )
        result = _load_ndjson(f, limit=10)
        assert len(result) == 2
        assert result[0]["event_type"] == "t0_dispatch"

    def test_preserves_record_content(self, tmp_path):
        f = make_ndjson(tmp_path / "events.ndjson", [DISPATCH_EVENT])
        result = _load_ndjson(f, limit=10)
        assert result[0]["dispatch_id"] == "20260422-103500-f36-A"


# ---------------------------------------------------------------------------
# _infer_receipt_status
# ---------------------------------------------------------------------------

class TestInferReceiptStatus:
    def test_dispatch_is_success(self):
        assert _infer_receipt_status("t0_dispatch") == "success"

    def test_reject_is_failed(self):
        assert _infer_receipt_status("t0_reject") == "failed"

    def test_escalate_is_failed(self):
        assert _infer_receipt_status("t0_escalate") == "failed"

    def test_wait_is_unknown(self):
        assert _infer_receipt_status("t0_wait") == "unknown"

    def test_complete_is_unknown(self):
        assert _infer_receipt_status("t0_complete") == "unknown"

    def test_unrecognised_is_unknown(self):
        assert _infer_receipt_status("some_other_event") == "unknown"


# ---------------------------------------------------------------------------
# _build_shadow_context
# ---------------------------------------------------------------------------

class TestBuildShadowContext:
    def test_event_type_preserved(self, tmp_path):
        ctx = _build_shadow_context(DISPATCH_EVENT, tmp_path)
        assert ctx["event_type"] == "t0_dispatch"

    def test_reason_from_trigger_reason(self, tmp_path):
        ctx = _build_shadow_context(DISPATCH_EVENT, tmp_path)
        assert ctx["reason"] == "new_report"

    def test_reason_fallback_to_reason_field(self, tmp_path):
        ctx = _build_shadow_context(WAIT_EVENT, tmp_path)
        assert ctx["reason"] == "Waiting for Track B receipt"

    def test_dispatch_id_preserved(self, tmp_path):
        ctx = _build_shadow_context(DISPATCH_EVENT, tmp_path)
        assert ctx["dispatch_id"] == "20260422-103500-f36-A"

    def test_receipt_status_inferred(self, tmp_path):
        ctx = _build_shadow_context(ESCALATE_EVENT, tmp_path)
        assert ctx["receipt"]["status"] == "failed"

    def test_state_summary_zero_when_no_state_file(self, tmp_path):
        ctx = _build_shadow_context(DISPATCH_EVENT, tmp_path)
        assert ctx["state_summary"]["active_dispatches"] == 0
        assert ctx["state_summary"]["pending_dispatches"] == 0

    def test_state_summary_from_t0_state(self, tmp_path):
        state_file = tmp_path / "t0_state.json"
        state_file.write_text(json.dumps({
            "active_dispatches": [{"id": "abc"}, {"id": "def"}],
            "pending_dispatches": [{"id": "ghi"}],
        }))
        ctx = _build_shadow_context(DISPATCH_EVENT, tmp_path)
        assert ctx["state_summary"]["active_dispatches"] == 2
        assert ctx["state_summary"]["pending_dispatches"] == 1

    def test_corrupt_state_file_tolerated(self, tmp_path):
        state_file = tmp_path / "t0_state.json"
        state_file.write_text("CORRUPT_JSON")
        ctx = _build_shadow_context(DISPATCH_EVENT, tmp_path)
        assert ctx["state_summary"]["active_dispatches"] == 0


# ---------------------------------------------------------------------------
# shadow_action_to_log_action
# ---------------------------------------------------------------------------

class TestShadowActionToLogAction:
    def test_re_dispatch_maps_to_dispatch(self):
        assert shadow_action_to_log_action("re_dispatch") == "dispatch"

    def test_escalate_maps_to_escalate(self):
        assert shadow_action_to_log_action("escalate") == "escalate"

    def test_skip_maps_to_wait(self):
        assert shadow_action_to_log_action("skip") == "wait"

    def test_analyze_failure_maps_to_dispatch(self):
        assert shadow_action_to_log_action("analyze_failure") == "dispatch"

    def test_case_insensitive_input(self):
        assert shadow_action_to_log_action("RE_DISPATCH") == "dispatch"
        assert shadow_action_to_log_action("SKIP") == "wait"

    def test_unknown_action_returns_lowercase(self):
        result = shadow_action_to_log_action("FOOBAR")
        assert result == "foobar"


# ---------------------------------------------------------------------------
# compare_decisions
# ---------------------------------------------------------------------------

class TestCompareDecisions:
    def _make_shadow(self, action: str) -> dict:
        return {
            "shadow_decision": action,
            "shadow_reasoning": "test reasoning",
            "shadow_confidence": 0.9,
            "shadow_backend": "dry-run",
            "shadow_latency_ms": 0,
        }

    def test_match_when_shadow_equals_actual(self):
        shadow = self._make_shadow("RE_DISPATCH")
        result = compare_decisions(DISPATCH_EVENT, DISPATCH_DECISION, shadow)
        assert result["match"] is True

    def test_mismatch_when_shadow_differs(self):
        shadow = self._make_shadow("SKIP")  # maps to "wait", actual is "dispatch"
        result = compare_decisions(DISPATCH_EVENT, DISPATCH_DECISION, shadow)
        assert result["match"] is False

    def test_none_actual_produces_no_match(self):
        shadow = self._make_shadow("RE_DISPATCH")
        result = compare_decisions(DISPATCH_EVENT, None, shadow)
        assert result["match"] is False
        assert result["actual_action"] == "UNKNOWN"

    def test_escalate_shadow_matches_escalate_actual(self):
        shadow = self._make_shadow("ESCALATE")
        result = compare_decisions(ESCALATE_EVENT, ESCALATE_DECISION, shadow)
        assert result["match"] is True

    def test_required_fields_present(self):
        shadow = self._make_shadow("SKIP")
        result = compare_decisions(WAIT_EVENT, WAIT_DECISION, shadow)
        for key in (
            "timestamp", "event_type", "event_timestamp",
            "actual_action", "shadow_action", "shadow_log_action",
            "match", "shadow_reasoning", "shadow_confidence",
            "shadow_backend", "shadow_latency_ms", "dispatch_id",
        ):
            assert key in result, f"Missing field: {key}"

    def test_event_type_preserved(self):
        shadow = self._make_shadow("SKIP")
        result = compare_decisions(DISPATCH_EVENT, DISPATCH_DECISION, shadow)
        assert result["event_type"] == "t0_dispatch"

    def test_shadow_log_action_stored(self):
        shadow = self._make_shadow("RE_DISPATCH")
        result = compare_decisions(DISPATCH_EVENT, DISPATCH_DECISION, shadow)
        assert result["shadow_log_action"] == "dispatch"


# ---------------------------------------------------------------------------
# generate_parity_report
# ---------------------------------------------------------------------------

class TestGenerateparityReport:
    def _make_comparison(self, match: bool, event_type: str = "t0_dispatch") -> dict:
        return {
            "event_type": event_type,
            "match": match,
            "actual_action": "dispatch",
            "shadow_action": "RE_DISPATCH" if match else "SKIP",
            "shadow_log_action": "dispatch" if match else "wait",
        }

    def test_empty_input_returns_zero_stats(self):
        report = generate_parity_report([], {"run_id": "test"})
        assert report["total"] == 0
        assert report["matched"] == 0
        assert report["parity_rate"] is None

    def test_all_matched(self):
        comparisons = [self._make_comparison(True) for _ in range(5)]
        report = generate_parity_report(comparisons, {"run_id": "test"})
        assert report["total"] == 5
        assert report["matched"] == 5
        assert report["parity_rate"] == 100.0

    def test_none_matched(self):
        comparisons = [self._make_comparison(False) for _ in range(4)]
        report = generate_parity_report(comparisons, {"run_id": "test"})
        assert report["matched"] == 0
        assert report["mismatched"] == 4
        assert report["parity_rate"] == 0.0

    def test_partial_match(self):
        comparisons = [
            self._make_comparison(True),
            self._make_comparison(True),
            self._make_comparison(False),
            self._make_comparison(False),
        ]
        report = generate_parity_report(comparisons, {"run_id": "test"})
        assert report["matched"] == 2
        assert report["mismatched"] == 2
        assert report["parity_rate"] == 50.0

    def test_by_event_type_counts(self):
        comparisons = [
            self._make_comparison(True, "t0_dispatch"),
            self._make_comparison(False, "t0_dispatch"),
            self._make_comparison(True, "t0_wait"),
        ]
        report = generate_parity_report(comparisons, {"run_id": "test"})
        assert report["by_event_type"]["t0_dispatch"]["total"] == 2
        assert report["by_event_type"]["t0_dispatch"]["matched"] == 1
        assert report["by_event_type"]["t0_wait"]["total"] == 1
        assert report["by_event_type"]["t0_wait"]["matched"] == 1

    def test_run_metadata_merged_into_report(self):
        meta = {"run_id": "xyz", "backend": "dry-run", "limit": 10}
        report = generate_parity_report([], meta)
        assert report["run_id"] == "xyz"
        assert report["backend"] == "dry-run"
        assert report["limit"] == 10

    def test_comparisons_preserved_in_report(self):
        comparisons = [self._make_comparison(True)]
        report = generate_parity_report(comparisons, {"run_id": "test"})
        assert len(report["comparisons"]) == 1
        assert report["comparisons"][0]["match"] is True


# ---------------------------------------------------------------------------
# write_parity_jsonl
# ---------------------------------------------------------------------------

class TestWriteParityJsonl:
    def test_creates_file(self, tmp_path):
        f = tmp_path / "parity.jsonl"
        write_parity_jsonl([{"match": True, "event_type": "t0_dispatch"}], f)
        assert f.exists()

    def test_each_line_is_valid_json(self, tmp_path):
        f = tmp_path / "parity.jsonl"
        records = [{"match": True, "n": i} for i in range(5)]
        write_parity_jsonl(records, f)
        for line in f.read_text().strip().splitlines():
            json.loads(line)  # must not raise

    def test_line_count_matches_input(self, tmp_path):
        f = tmp_path / "parity.jsonl"
        records = [{"match": True} for _ in range(7)]
        write_parity_jsonl(records, f)
        assert len(f.read_text().strip().splitlines()) == 7

    def test_creates_parent_directory(self, tmp_path):
        f = tmp_path / "deep" / "nested" / "parity.jsonl"
        write_parity_jsonl([{"match": False}], f)
        assert f.exists()

    def test_empty_input_creates_empty_file(self, tmp_path):
        f = tmp_path / "parity.jsonl"
        write_parity_jsonl([], f)
        assert f.exists()
        assert f.read_text().strip() == ""


# ---------------------------------------------------------------------------
# write_parity_report_md
# ---------------------------------------------------------------------------

class TestWriteParityReportMd:
    def _base_report(self) -> dict:
        return {
            "run_id": "shadow-20260422T123456Z",
            "generated_at": "2026-04-22T12:34:56+00:00",
            "backend": "dry-run",
            "limit": 10,
            "total": 3,
            "matched": 2,
            "mismatched": 1,
            "parity_rate": 66.7,
            "by_event_type": {
                "t0_dispatch": {"total": 2, "matched": 2},
                "t0_wait": {"total": 1, "matched": 0},
            },
            "comparisons": [
                {
                    "event_type": "t0_dispatch",
                    "actual_action": "dispatch",
                    "shadow_log_action": "dispatch",
                    "match": True,
                },
                {
                    "event_type": "t0_wait",
                    "actual_action": "wait",
                    "shadow_log_action": "dispatch",
                    "match": False,
                },
            ],
        }

    def test_creates_file(self, tmp_path):
        f = tmp_path / "report.md"
        write_parity_report_md(self._base_report(), f)
        assert f.exists()

    def test_contains_run_id(self, tmp_path):
        f = tmp_path / "report.md"
        write_parity_report_md(self._base_report(), f)
        content = f.read_text()
        assert "shadow-20260422T123456Z" in content

    def test_contains_parity_rate(self, tmp_path):
        f = tmp_path / "report.md"
        write_parity_report_md(self._base_report(), f)
        assert "66.7" in f.read_text()

    def test_contains_backend(self, tmp_path):
        f = tmp_path / "report.md"
        write_parity_report_md(self._base_report(), f)
        assert "dry-run" in f.read_text()

    def test_contains_event_type_table(self, tmp_path):
        f = tmp_path / "report.md"
        write_parity_report_md(self._base_report(), f)
        content = f.read_text()
        assert "t0_dispatch" in content
        assert "t0_wait" in content

    def test_null_parity_rate_handled(self, tmp_path):
        report = self._base_report()
        report["parity_rate"] = None
        report["total"] = 0
        f = tmp_path / "report.md"
        write_parity_report_md(report, f)
        assert "N/A" in f.read_text()

    def test_creates_parent_directory(self, tmp_path):
        f = tmp_path / "deep" / "report.md"
        write_parity_report_md(self._base_report(), f)
        assert f.exists()


# ---------------------------------------------------------------------------
# run_shadow_mode — integration with dry_run=True
# ---------------------------------------------------------------------------

class TestRunShadowMode:
    def _setup_data(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create minimal data and state dirs with event and decision log files."""
        data_dir = tmp_path / "data"
        state_dir = tmp_path / "state"
        events_dir = data_dir / "events"
        events_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)

        make_ndjson(events_dir / "t0_decisions.ndjson", ALL_EVENTS)
        make_ndjson(state_dir / "t0_decision_log.jsonl", ALL_DECISIONS)

        return data_dir, state_dir

    def test_returns_report_dict(self, tmp_path):
        data_dir, state_dir = self._setup_data(tmp_path)
        report = run_shadow_mode(
            data_dir=data_dir, state_dir=state_dir,
            limit=10, backend="dry-run",
            output_dir=tmp_path / "out",
            dry_run=True,
        )
        assert isinstance(report, dict)
        assert "total" in report
        assert "parity_rate" in report

    def test_no_files_written_in_dry_run(self, tmp_path):
        data_dir, state_dir = self._setup_data(tmp_path)
        out_dir = tmp_path / "out"
        run_shadow_mode(
            data_dir=data_dir, state_dir=state_dir,
            limit=10, backend="dry-run",
            output_dir=out_dir,
            dry_run=True,
        )
        assert not out_dir.exists() or list(out_dir.iterdir()) == []

    def test_total_equals_event_count(self, tmp_path):
        data_dir, state_dir = self._setup_data(tmp_path)
        report = run_shadow_mode(
            data_dir=data_dir, state_dir=state_dir,
            limit=10, backend="dry-run",
            output_dir=tmp_path / "out",
            dry_run=True,
        )
        assert report["total"] == len(ALL_EVENTS)

    def test_missing_events_file_returns_empty_report(self, tmp_path):
        data_dir = tmp_path / "data"
        state_dir = tmp_path / "state"
        data_dir.mkdir(); state_dir.mkdir()
        # No events file created
        report = run_shadow_mode(
            data_dir=data_dir, state_dir=state_dir,
            limit=10, backend="dry-run",
            output_dir=tmp_path / "out",
            dry_run=True,
        )
        assert report["total"] == 0
        assert report["parity_rate"] is None

    def test_limit_respected(self, tmp_path):
        data_dir, state_dir = self._setup_data(tmp_path)
        report = run_shadow_mode(
            data_dir=data_dir, state_dir=state_dir,
            limit=2, backend="dry-run",
            output_dir=tmp_path / "out",
            dry_run=True,
        )
        assert report["total"] == 2

    def test_files_written_when_not_dry_run(self, tmp_path):
        data_dir, state_dir = self._setup_data(tmp_path)
        out_dir = tmp_path / "out"
        run_shadow_mode(
            data_dir=data_dir, state_dir=state_dir,
            limit=10, backend="dry-run",
            output_dir=out_dir,
            dry_run=False,
        )
        jsonl_files = list(out_dir.glob("shadow_parity_*.jsonl"))
        md_files    = list(out_dir.glob("shadow_parity_*.md"))
        assert len(jsonl_files) == 1, "Expected one JSONL file"
        assert len(md_files)    == 1, "Expected one markdown file"

    def test_run_id_present_in_report(self, tmp_path):
        data_dir, state_dir = self._setup_data(tmp_path)
        report = run_shadow_mode(
            data_dir=data_dir, state_dir=state_dir,
            limit=10, backend="dry-run",
            output_dir=tmp_path / "out",
            dry_run=True,
        )
        assert report["run_id"].startswith("shadow-")


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------

class TestMain:
    def _setup(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        data_dir  = tmp_path / "data"
        state_dir = tmp_path / "state"
        out_dir   = tmp_path / "out"
        events_dir = data_dir / "events"
        events_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)
        make_ndjson(events_dir / "t0_decisions.ndjson", ALL_EVENTS)
        make_ndjson(state_dir / "t0_decision_log.jsonl", ALL_DECISIONS)
        return data_dir, state_dir, out_dir

    def test_returns_0_on_success(self, tmp_path):
        data_dir, state_dir, out_dir = self._setup(tmp_path)
        rc = main([
            "--data-dir",   str(data_dir),
            "--state-dir",  str(state_dir),
            "--output-dir", str(out_dir),
            "--dry-run",
        ])
        assert rc == 0

    def test_returns_0_when_no_events(self, tmp_path):
        data_dir  = tmp_path / "data"
        state_dir = tmp_path / "state"
        data_dir.mkdir(); state_dir.mkdir()
        rc = main([
            "--data-dir",   str(data_dir),
            "--state-dir",  str(state_dir),
            "--output-dir", str(tmp_path / "out"),
            "--dry-run",
        ])
        assert rc == 0

    def test_json_flag_prints_report(self, tmp_path, capsys):
        data_dir, state_dir, out_dir = self._setup(tmp_path)
        rc = main([
            "--data-dir",   str(data_dir),
            "--state-dir",  str(state_dir),
            "--output-dir", str(out_dir),
            "--dry-run",
            "--json",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert "total" in parsed
        assert "parity_rate" in parsed

    def test_limit_flag_respected(self, tmp_path, capsys):
        data_dir, state_dir, out_dir = self._setup(tmp_path)
        main([
            "--data-dir",   str(data_dir),
            "--state-dir",  str(state_dir),
            "--output-dir", str(out_dir),
            "--dry-run",
            "--json",
            "--limit", "1",
        ])
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["total"] == 1

    def test_files_not_written_with_dry_run_flag(self, tmp_path):
        data_dir, state_dir, out_dir = self._setup(tmp_path)
        main([
            "--data-dir",   str(data_dir),
            "--state-dir",  str(state_dir),
            "--output-dir", str(out_dir),
            "--dry-run",
        ])
        assert not out_dir.exists() or list(out_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# _build_decision_index
# ---------------------------------------------------------------------------

class TestBuildDecisionIndex:
    def test_empty_input_returns_empty_collections(self):
        by_did, unkeyed = _build_decision_index([])
        assert by_did == {}
        assert unkeyed == []

    def test_keyed_decisions_indexed_by_dispatch_id(self):
        decisions = [DISPATCH_DECISION]
        by_did, unkeyed = _build_decision_index(decisions)
        assert "20260422-103500-f36-A" in by_did
        assert by_did["20260422-103500-f36-A"]["action"] == "dispatch"
        assert unkeyed == []

    def test_decisions_without_dispatch_id_go_to_unkeyed(self):
        by_did, unkeyed = _build_decision_index([WAIT_DECISION, ESCALATE_DECISION])
        assert by_did == {}
        assert len(unkeyed) == 2
        assert unkeyed[0]["action"] == "wait"
        assert unkeyed[1]["action"] == "escalate"

    def test_mixed_keyed_and_unkeyed(self):
        decisions = [DISPATCH_DECISION, WAIT_DECISION, ESCALATE_DECISION]
        by_did, unkeyed = _build_decision_index(decisions)
        assert len(by_did) == 1
        assert "20260422-103500-f36-A" in by_did
        assert len(unkeyed) == 2

    def test_last_wins_on_duplicate_dispatch_id(self):
        first  = {"dispatch_id": "dup-id", "action": "dispatch", "version": 1}
        second = {"dispatch_id": "dup-id", "action": "wait",     "version": 2}
        by_did, _ = _build_decision_index([first, second])
        assert by_did["dup-id"]["version"] == 2

    def test_none_dispatch_id_treated_as_unkeyed(self):
        d = {"dispatch_id": None, "action": "wait"}
        by_did, unkeyed = _build_decision_index([d])
        assert by_did == {}
        assert len(unkeyed) == 1


# ---------------------------------------------------------------------------
# _pair_event_to_decision
# ---------------------------------------------------------------------------

class TestPairEventToDecision:
    def _index(self, decisions=None):
        return _build_decision_index(decisions or ALL_DECISIONS)

    def test_dispatch_event_pairs_by_dispatch_id(self):
        by_did, unkeyed = self._index([DISPATCH_DECISION])
        result = _pair_event_to_decision(DISPATCH_EVENT, by_did, unkeyed)
        assert result is not None
        assert result["action"] == "dispatch"

    def test_dispatch_event_missing_from_index_returns_none(self):
        by_did, unkeyed = _build_decision_index([])
        result = _pair_event_to_decision(DISPATCH_EVENT, by_did, unkeyed)
        assert result is None

    def test_non_dispatch_event_consumes_unkeyed_fifo(self):
        by_did, unkeyed = _build_decision_index([WAIT_DECISION, ESCALATE_DECISION])
        first  = _pair_event_to_decision(WAIT_EVENT,     by_did, unkeyed)
        second = _pair_event_to_decision(ESCALATE_EVENT, by_did, unkeyed)
        assert first  is not None and first["action"]  == "wait"
        assert second is not None and second["action"] == "escalate"

    def test_unkeyed_exhausted_returns_none(self):
        by_did, unkeyed = _build_decision_index([WAIT_DECISION])
        _pair_event_to_decision(WAIT_EVENT, by_did, unkeyed)  # consume the only one
        result = _pair_event_to_decision(ESCALATE_EVENT, by_did, unkeyed)
        assert result is None

    def test_dispatch_event_does_not_consume_unkeyed(self):
        by_did, unkeyed = _build_decision_index([DISPATCH_DECISION, WAIT_DECISION])
        _pair_event_to_decision(DISPATCH_EVENT, by_did, unkeyed)
        assert len(unkeyed) == 1  # WAIT_DECISION still there

    def test_positional_drift_does_not_corrupt_pairing(self):
        # Simulate: events file has 3 records, decision log only has 2 (cursor lag).
        # With old positional approach, ESCALATE_EVENT would get WAIT_DECISION.
        # With keyed approach, ESCALATE_EVENT gets None (correct: no logged decision yet).
        by_did, unkeyed = _build_decision_index([DISPATCH_DECISION, WAIT_DECISION])
        d_result = _pair_event_to_decision(DISPATCH_EVENT, by_did, unkeyed)
        w_result = _pair_event_to_decision(WAIT_EVENT, by_did, unkeyed)
        e_result = _pair_event_to_decision(ESCALATE_EVENT, by_did, unkeyed)
        assert d_result is not None and d_result["action"] == "dispatch"
        assert w_result is not None and w_result["action"] == "wait"
        assert e_result is None  # no decision logged yet — correct
