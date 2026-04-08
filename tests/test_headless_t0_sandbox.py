#!/usr/bin/env python3
"""Unit tests for headless T0 sandbox infrastructure (no LLM calls).

Covers:
- fake_data generators produce valid, well-formed output
- setup_sandbox.py creates/destroys/resets correctly
- inject_receipt appends valid NDJSON
- inject_report writes files
- set_terminal_status mutates t0_brief.json
- assertions.py helpers raise/pass correctly
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Add tests/headless_t0/ to path for local imports
_HEADLESS_T0_DIR = Path(__file__).parent / "headless_t0"
sys.path.insert(0, str(_HEADLESS_T0_DIR))

import fake_data  # type: ignore[import]
import setup_sandbox  # type: ignore[import]
from assertions import (  # type: ignore[import]
    AssertionError,
    assert_decision_mentions,
    assert_dispatch_created,
    assert_dispatch_format,
    assert_file_read,
    assert_gate_refused,
    assert_no_dispatch_created,
)


# ---------------------------------------------------------------------------
# fake_data tests
# ---------------------------------------------------------------------------

class TestFakeReceipt:
    def test_returns_valid_json(self):
        line = fake_data.fake_receipt("d-001", "T1", "success", "/tmp/report.md")
        record = json.loads(line)
        assert record["event_type"] == "task_complete"
        assert record["dispatch_id"] == "d-001"
        assert record["terminal"] == "T1"
        assert record["status"] == "success"

    def test_failure_status(self):
        line = fake_data.fake_receipt("d-002", "T2", "failure", "/tmp/r.md")
        record = json.loads(line)
        assert record["status"] == "failure"
        assert record["confidence"] == 0.5

    def test_report_path_preserved(self):
        line = fake_data.fake_receipt("d-003", "T3", "success", "/absolute/path/report.md")
        record = json.loads(line)
        assert record["report_path"] == "/absolute/path/report.md"

    def test_track_and_gate_defaults(self):
        line = fake_data.fake_receipt("d-004", "T1", "success", "/r.md")
        record = json.loads(line)
        assert record["track"] == "A"
        assert record["gate"] == "implementation"


class TestFakeReports:
    def test_success_report_has_metadata_block(self):
        report = fake_data.fake_report_success("d-001", "A")
        assert "**Dispatch ID**: d-001" in report
        assert "**Track**: A" in report
        assert "**Status**: success" in report

    def test_partial_report_has_metadata(self):
        report = fake_data.fake_report_partial("d-002", "B")
        assert "**Dispatch ID**: d-002" in report
        assert "**Track**: B" in report

    def test_gate_fail_report_mentions_failures(self):
        report = fake_data.fake_report_gate_fail("d-003", "C")
        assert "failed" in report.lower() or "FAILED" in report
        assert "**Track**: C" in report

    def test_reports_are_nonempty_strings(self):
        for fn in (fake_data.fake_report_success, fake_data.fake_report_partial, fake_data.fake_report_gate_fail):
            assert len(fn("d", "A")) > 100


class TestFakeT0Brief:
    def test_structure(self):
        brief = fake_data.fake_t0_brief()
        assert "terminals" in brief
        assert "T1" in brief["terminals"]
        assert "T2" in brief["terminals"]
        assert "T3" in brief["terminals"]
        assert "queues" in brief
        assert "tracks" in brief

    def test_terminal_status_reflected(self):
        brief = fake_data.fake_t0_brief(t1_status="working", t2_status="idle", t3_status="idle")
        assert brief["terminals"]["T1"]["status"] == "working"
        assert brief["terminals"]["T1"]["ready"] is False
        assert brief["terminals"]["T2"]["status"] == "idle"
        assert brief["terminals"]["T2"]["ready"] is True

    def test_dispatch_id_in_working_terminal(self):
        brief = fake_data.fake_t0_brief(t1_status="working", t1_dispatch="my-dispatch")
        assert brief["terminals"]["T1"].get("current_task") == "my-dispatch"

    def test_queue_counts(self):
        brief = fake_data.fake_t0_brief(pending=3, active=2)
        assert brief["queues"]["pending"] == 3
        assert brief["queues"]["active"] == 2


class TestFakeOpenItems:
    def test_correct_counts(self):
        data = fake_data.fake_open_items(blockers=2, warnings=3)
        items = data["items"]
        assert len(items) == 5
        blockers = [i for i in items if i["severity"] == "blocker"]
        warnings = [i for i in items if i["severity"] == "warn"]
        assert len(blockers) == 2
        assert len(warnings) == 3

    def test_items_have_required_fields(self):
        data = fake_data.fake_open_items(blockers=1, warnings=0)
        item = data["items"][0]
        for field in ("id", "status", "severity", "title", "pr_id", "created_at"):
            assert field in item, f"Missing field: {field}"

    def test_ids_are_sequential(self):
        data = fake_data.fake_open_items(blockers=2, warnings=2)
        ids = [i["id"] for i in data["items"]]
        assert ids == ["OI-001", "OI-002", "OI-003", "OI-004"]

    def test_all_open(self):
        data = fake_data.fake_open_items(blockers=3)
        for item in data["items"]:
            assert item["status"] == "open"


class TestFakeDispatch:
    def test_starts_with_target(self):
        content = fake_data.fake_dispatch("d-001", "A", "T1", "backend-developer", "Do stuff")
        assert content.startswith("[[TARGET:A]]")

    def test_has_required_fields(self):
        content = fake_data.fake_dispatch("d-001", "B", "T2", "test-engineer", "Test stuff")
        assert "Manager Block" in content
        assert "Role: test-engineer" in content
        assert "Dispatch-ID: d-001" in content
        assert "Instruction:" in content

    def test_instruction_included(self):
        content = fake_data.fake_dispatch("d-001", "C", "T3", "reviewer", "Review the PR carefully")
        assert "Review the PR carefully" in content


# ---------------------------------------------------------------------------
# setup_sandbox tests
# ---------------------------------------------------------------------------

class TestCreateSandbox:
    def test_creates_required_directories(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        assert (sb / ".vnx-data" / "state").is_dir()
        assert (sb / ".vnx-data" / "dispatches" / "pending").is_dir()
        assert (sb / ".vnx-data" / "dispatches" / "active").is_dir()
        assert (sb / ".vnx-data" / "dispatches" / "completed").is_dir()
        assert (sb / ".vnx-data" / "unified_reports").is_dir()

    def test_creates_claude_md(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        claude_md = sb / "CLAUDE.md"
        assert claude_md.exists()
        assert "orchestrator" in claude_md.read_text().lower()

    def test_creates_initial_state_files(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        state = sb / ".vnx-data" / "state"
        for f in ("t0_brief.json", "t0_receipts.ndjson", "open_items.json",
                  "progress_state.yaml", "t0_recommendations.json"):
            assert (state / f).exists(), f"Missing: {f}"

    def test_t0_brief_is_valid_json(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        brief = json.loads((sb / ".vnx-data" / "state" / "t0_brief.json").read_text())
        assert "terminals" in brief
        assert "T1" in brief["terminals"]

    def test_open_items_is_valid_json(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        data = json.loads((sb / ".vnx-data" / "state" / "open_items.json").read_text())
        assert "items" in data
        assert len(data["items"]) > 0

    def test_idempotent_create(self, tmp_path):
        sb_path = tmp_path / "sandbox"
        setup_sandbox.create_sandbox(sb_path)
        # Second create should not crash
        setup_sandbox.create_sandbox(sb_path)
        assert (sb_path / "CLAUDE.md").exists()


class TestDestroySandbox:
    def test_removes_directory(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        assert sb.exists()
        setup_sandbox.destroy_sandbox(sb)
        assert not sb.exists()

    def test_noop_if_not_exists(self, tmp_path):
        sb = tmp_path / "nonexistent"
        # Should not raise
        setup_sandbox.destroy_sandbox(sb)


class TestResetSandbox:
    def test_rebuilds_fresh_state(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        # Mutate state
        receipts = sb / ".vnx-data" / "state" / "t0_receipts.ndjson"
        receipts.write_text('{"event_type":"task_complete"}\n')

        # Reset
        setup_sandbox.reset_sandbox(tmp_path / "sandbox")

        # Receipts file should be fresh (empty)
        assert receipts.read_text() == ""


class TestInjectReceipt:
    def test_appends_valid_ndjson(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        setup_sandbox.inject_receipt("d-001", "T1", "success", "/r.md", sandbox=sb)
        receipts = sb / ".vnx-data" / "state" / "t0_receipts.ndjson"
        lines = [l for l in receipts.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["dispatch_id"] == "d-001"

    def test_multiple_appends(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        for i in range(3):
            setup_sandbox.inject_receipt(f"d-{i:03d}", "T1", "success", "/r.md", sandbox=sb)
        receipts = sb / ".vnx-data" / "state" / "t0_receipts.ndjson"
        lines = [l for l in receipts.read_text().splitlines() if l.strip()]
        assert len(lines) == 3


class TestInjectReport:
    def test_writes_report_file(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        content = "# Report\nSome content."
        path = setup_sandbox.inject_report("test-report.md", content, sandbox=sb)
        assert path.exists()
        assert path.read_text() == content

    def test_returns_path(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        path = setup_sandbox.inject_report("r.md", "x", sandbox=sb)
        assert isinstance(path, Path)
        assert path.name == "r.md"


class TestSetTerminalStatus:
    def test_updates_status(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        setup_sandbox.set_terminal_status("T2", "working", "d-abc", sandbox=sb)
        brief = json.loads((sb / ".vnx-data" / "state" / "t0_brief.json").read_text())
        assert brief["terminals"]["T2"]["status"] == "working"
        assert brief["terminals"]["T2"]["ready"] is False
        assert brief["terminals"]["T2"]["current_task"] == "d-abc"

    def test_idle_clears_dispatch(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        setup_sandbox.set_terminal_status("T1", "working", "d-001", sandbox=sb)
        setup_sandbox.set_terminal_status("T1", "idle", sandbox=sb)
        brief = json.loads((sb / ".vnx-data" / "state" / "t0_brief.json").read_text())
        assert brief["terminals"]["T1"]["status"] == "idle"
        assert brief["terminals"]["T1"]["ready"] is True
        assert "current_task" not in brief["terminals"]["T1"]


# ---------------------------------------------------------------------------
# assertions.py tests
# ---------------------------------------------------------------------------

class TestAssertDispatchCreated:
    def test_passes_when_dispatch_exists(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        pending = sb / ".vnx-data" / "dispatches" / "pending"
        (pending / "20260407-test-B.md").write_text(
            "[[TARGET:B]]\nManager Block\nRole: test-engineer\nDispatch-ID: 20260407-test-B\nInstruction:\nDo stuff\n"
        )
        path = assert_dispatch_created(sb, "B")
        assert path.name == "20260407-test-B.md"

    def test_fails_when_empty(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        with pytest.raises(AssertionError, match="No dispatch files"):
            assert_dispatch_created(sb, "B")

    def test_fails_wrong_track(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        pending = sb / ".vnx-data" / "dispatches" / "pending"
        (pending / "test-A.md").write_text("[[TARGET:A]]\nManager Block\nRole: dev\nDispatch-ID: x\nInstruction:\nx\n")
        with pytest.raises(AssertionError, match="TARGET:B"):
            assert_dispatch_created(sb, "B")


class TestAssertDispatchFormat:
    def _write_dispatch(self, tmp_path, content):
        f = tmp_path / "d.md"
        f.write_text(content)
        return f

    def test_passes_on_valid_dispatch(self, tmp_path):
        f = self._write_dispatch(
            tmp_path,
            "[[TARGET:A]]\nManager Block\nRole: dev\nDispatch-ID: x\nInstruction:\nDo stuff\n",
        )
        assert_dispatch_format(f)  # should not raise

    def test_fails_missing_target(self, tmp_path):
        f = self._write_dispatch(tmp_path, "Manager Block\nRole: dev\nDispatch-ID: x\nInstruction:\n")
        with pytest.raises(AssertionError, match="TARGET"):
            assert_dispatch_format(f)

    def test_fails_missing_role(self, tmp_path):
        f = self._write_dispatch(tmp_path, "[[TARGET:B]]\nManager Block\nDispatch-ID: x\nInstruction:\n")
        with pytest.raises(AssertionError, match="Role:"):
            assert_dispatch_format(f)


class TestAssertDecisionMentions:
    def test_passes_all_present(self):
        assert_decision_mentions("Dispatch d-001 was approved for Track A", ["d-001", "approved", "Track A"])

    def test_case_insensitive(self):
        assert_decision_mentions("APPROVED the dispatch", ["approved"])

    def test_fails_missing_keyword(self):
        with pytest.raises(AssertionError, match="missing"):
            assert_decision_mentions("Something else happened", ["d-001"])


class TestAssertFileRead:
    def test_passes_on_filename_in_output(self):
        assert_file_read("T0 read the file example_module.py and counted 400 lines", "scripts/lib/example_module.py")

    def test_passes_on_full_path_in_output(self):
        assert_file_read("Read scripts/lib/example_module.py successfully", "scripts/lib/example_module.py")

    def test_fails_when_not_mentioned(self):
        with pytest.raises(AssertionError, match="does not indicate"):
            assert_file_read("I did some work", "scripts/lib/example_module.py")


class TestAssertNoDispatchCreated:
    def test_passes_on_empty_pending(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        assert_no_dispatch_created(sb)  # should not raise

    def test_fails_when_dispatch_exists(self, tmp_path):
        sb = setup_sandbox.create_sandbox(tmp_path / "sandbox")
        pending = sb / ".vnx-data" / "dispatches" / "pending"
        (pending / "test.md").write_text("[[TARGET:A]]\n...")
        with pytest.raises(AssertionError, match="test.md"):
            assert_no_dispatch_created(sb)


class TestAssertGateRefused:
    def test_passes_on_refusal_language(self):
        assert_gate_refused("Cannot merge without gate evidence. Gate review is required.")

    def test_passes_on_missing_keyword(self):
        assert_gate_refused("No gate evidence found — refusing to approve merge.")

    def test_fails_on_approval_language(self):
        with pytest.raises(AssertionError, match="did not refuse"):
            assert_gate_refused("Looks good to me, everything is in order!")
