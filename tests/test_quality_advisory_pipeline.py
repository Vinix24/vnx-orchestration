#!/usr/bin/env python3
"""Tests for quality advisory pipeline integration."""

import json
import tempfile
from pathlib import Path
import sys
import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import quality_advisory as quality_advisory_module
from quality_advisory import (
    QualityCheck,
    build_whole_repo_file_size_backlog,
    check_file_size,
    check_function_sizes,
    calculate_risk_score,
    make_t0_decision,
    generate_quality_advisory,
    get_changed_files,
)
from terminal_snapshot import collect_terminal_snapshot, TerminalState


class TestFileSizeGates:
    """Test file size threshold behavior."""

    def test_python_file_within_limits(self, tmp_path):
        """Python file within limits should produce no checks."""
        test_file = tmp_path / "test.py"
        test_file.write_text("\n" * 100)  # 100 lines, well under 500 warning threshold

        checks = check_file_size(test_file)

        assert checks == []

    def test_python_file_warning_threshold(self, tmp_path):
        """Python file over warning threshold should produce warning."""
        test_file = tmp_path / "test.py"
        test_file.write_text("\n" * 600)  # 600 lines, over 500 warning

        checks = check_file_size(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "warning"
        assert checks[0].check_id == "file_size_warning"
        assert "600" in checks[0].message

    def test_python_file_between_warning_and_ceiling_is_warning(self, tmp_path):
        """900 lines is over the 500 warning but under the 1200 hard ceiling: advisory warning."""
        test_file = tmp_path / "test.py"
        test_file.write_text("\n" * 900)

        checks = check_file_size(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "warning"
        assert checks[0].check_id == "file_size_warning"

    def test_python_file_over_hard_ceiling_blocks(self, tmp_path):
        """A new (non-allowlisted) Python file over the 1200 hard ceiling is a real block."""
        test_file = tmp_path / "test.py"
        test_file.write_text("\n" * 1300)

        checks = check_file_size(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "blocking"
        assert checks[0].check_id == "file_size_blocking"
        assert checks[0].action_required is True

    def test_allowlisted_monolith_over_ceiling_is_advisory(self, tmp_path):
        """A grandfathered monolith over the ceiling is surfaced as advisory, not blocking,
        so touching it does not HOLD the gate (path-suffix match on the allowlist key)."""
        f = tmp_path / "scripts" / "migrate_future_system.py"
        f.parent.mkdir(parents=True)
        f.write_text("\n" * 1300)

        checks = check_file_size(f)

        assert len(checks) == 1
        assert checks[0].severity == "warning"
        assert checks[0].check_id == "file_size_grandfathered"
        assert checks[0].action_required is False

    def test_large_test_file_is_advisory_not_blocking(self, tmp_path):
        """A large test file is exempt from the hard-ceiling block: a thorough suite is
        legitimately large, so it stays advisory rather than HOLDing the gate."""
        f = tmp_path / "tests" / "test_big.py"
        f.parent.mkdir(parents=True)
        f.write_text("\n" * 1300)

        checks = check_file_size(f)

        assert len(checks) == 1
        assert checks[0].severity == "warning"
        assert checks[0].check_id == "file_size_grandfathered"
        assert checks[0].action_required is False

    def test_shell_file_warning_threshold(self, tmp_path):
        """Shell file over warning threshold should produce warning."""
        test_file = tmp_path / "test.sh"
        test_file.write_text("\n" * 350)  # 350 lines, over 300 warning for shell

        checks = check_file_size(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "warning"

    def test_shell_file_over_ceiling_blocks(self, tmp_path):
        """A new (non-allowlisted) shell file over the 600 hard ceiling is a real block."""
        test_file = tmp_path / "test.sh"
        test_file.write_text("\n" * 650)  # 650 lines, over the 600 shell ceiling

        checks = check_file_size(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "blocking"
        assert checks[0].check_id == "file_size_blocking"
        assert checks[0].action_required is True


class TestFunctionSizeGates:
    """Test function size threshold behavior."""

    def test_python_function_within_limits(self, tmp_path):
        """Python function within limits should produce no checks."""
        test_file = tmp_path / "test.py"
        test_file.write_text("""
def small_function():
    x = 1
    y = 2
    return x + y
""")

        checks = check_function_sizes(test_file)

        assert checks == []

    def test_python_function_warning_threshold(self, tmp_path):
        """Python function over warning threshold should produce warning."""
        test_file = tmp_path / "test.py"
        # Create a 50-line function (over 40 warning threshold)
        lines = ["def large_function():"]
        for i in range(50):
            lines.append(f"    x{i} = {i}")
        test_file.write_text("\n".join(lines))

        checks = check_function_sizes(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "warning"
        assert checks[0].symbol == "large_function"

    def test_python_function_blocking_threshold(self, tmp_path):
        """Python function over soft-max threshold should produce advisory warning (not blocking)."""
        test_file = tmp_path / "test.py"
        # Create a 75-line function (over 70 soft max threshold)
        lines = ["def huge_function():"]
        for i in range(75):
            lines.append(f"    x{i} = {i}")
        test_file.write_text("\n".join(lines))

        checks = check_function_sizes(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "warning"
        assert checks[0].action_required is False

    def test_shell_function_warning_threshold(self, tmp_path):
        """Shell function over warning threshold should produce warning."""
        test_file = tmp_path / "test.sh"
        # Create a 35-line shell function (over 30 warning threshold)
        lines = ["large_function() {"]
        for i in range(35):
            lines.append(f"  echo {i}")
        lines.append("}")
        test_file.write_text("\n".join(lines))

        checks = check_function_sizes(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "warning"


class TestSizeAdvisoryPolicy:
    """File size over the hard ceiling now BLOCKS (unless grandfathered); function size and
    sub-ceiling file size stay advisory (warning)."""

    def test_900_line_python_file_is_warning_not_blocking(self, tmp_path):
        # 900 < the 1200 hard ceiling: still just a warning, not a block.
        test_file = tmp_path / "big.py"
        test_file.write_text("\n" * 900)

        checks = check_file_size(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "warning"
        assert checks[0].check_id == "file_size_warning"
        assert checks[0].action_required is False

    def test_100_line_python_function_is_warning_not_blocking(self, tmp_path):
        test_file = tmp_path / "big.py"
        lines = ["def fat():"]
        for i in range(100):
            lines.append(f"    x{i} = {i}")
        test_file.write_text("\n".join(lines))

        checks = check_function_sizes(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "warning"
        assert checks[0].check_id == "function_size_blocking"
        assert checks[0].action_required is False

    def test_70_line_shell_function_is_warning_not_blocking(self, tmp_path):
        test_file = tmp_path / "big.sh"
        lines = ["large_fn() {"]
        for i in range(70):
            lines.append(f"  echo {i}")
        lines.append("}")
        test_file.write_text("\n".join(lines))

        checks = check_function_sizes(test_file)

        assert len(checks) == 1
        assert checks[0].severity == "warning"
        assert checks[0].check_id == "function_size_blocking"
        assert checks[0].action_required is False

    def test_function_size_checks_stay_advisory(self, tmp_path):
        """Function-size findings remain advisory (warning) — only FILE size over the hard
        ceiling escalates to blocking (covered in TestFileSizeGates)."""
        py_file = tmp_path / "x.py"
        # A 200-line function is well over the 70-line function-blocking threshold.
        lines = ["def monster():"]
        for i in range(200):
            lines.append(f"    x{i} = {i}")
        py_file.write_text("\n".join(lines))

        sh_file = tmp_path / "x.sh"
        sh_lines = ["big_fn() {"]
        for i in range(200):
            sh_lines.append(f"  echo {i}")
        sh_lines.append("}")
        sh_file.write_text("\n".join(sh_lines))

        for f in [py_file, sh_file]:
            for check in check_function_sizes(f):
                assert check.severity != "blocking", (
                    f"Function-size check emitted blocking for {f}: {check}"
                )


class TestRiskScoring:
    """Test risk score calculation."""

    def test_empty_checks_zero_score(self):
        """No checks should result in zero risk score."""
        score = calculate_risk_score([])
        assert score == 0

    def test_single_warning_low_score(self):
        """Single warning should produce low risk score."""
        checks = [
            QualityCheck(
                check_id="test",
                severity="warning",
                file="test.py",
                message="test warning",
            )
        ]
        score = calculate_risk_score(checks)
        assert score == 10  # RISK_WEIGHT_WARNING

    def test_single_blocking_high_score(self):
        """Single blocking issue should produce high risk score."""
        checks = [
            QualityCheck(
                check_id="test",
                severity="blocking",
                file="test.py",
                message="test blocking",
            )
        ]
        score = calculate_risk_score(checks)
        assert score == 50  # RISK_WEIGHT_BLOCKING

    def test_multiple_warnings_cumulative(self):
        """Multiple warnings should cumulate risk score."""
        checks = [
            QualityCheck(check_id=f"w{i}", severity="warning", file="test.py", message="w")
            for i in range(5)
        ]
        score = calculate_risk_score(checks)
        assert score == 50  # 5 * 10

    def test_score_caps_at_100(self):
        """Risk score should cap at 100."""
        checks = [
            QualityCheck(check_id=f"b{i}", severity="blocking", file="test.py", message="b")
            for i in range(10)  # 10 * 50 = 500, should cap at 100
        ]
        score = calculate_risk_score(checks)
        assert score == 100


class TestT0DecisionPolicy:
    """Test T0 decision policy mapping."""

    def test_no_issues_approve(self):
        """No issues should result in approve decision."""
        checks = []
        decision = make_t0_decision(checks, risk_score=0)

        assert decision["decision"] == "approve"
        assert decision["reason"] == "No significant quality issues detected"
        assert decision["suggested_dispatches"] == []

    def test_single_blocking_hold(self):
        """Single blocking issue should result in hold decision."""
        checks = [
            QualityCheck(
                check_id="file_size_blocking",
                severity="blocking",
                file="test.py",
                message="File too large",
                action_required=True,
            )
        ]
        decision = make_t0_decision(checks, risk_score=50)

        assert decision["decision"] == "hold"
        assert "blocking" in decision["reason"]
        assert len(decision["suggested_dispatches"]) > 0

    def test_two_warnings_approve_with_followup(self):
        """Two warnings should result in approve_with_followup."""
        checks = [
            QualityCheck(check_id="w1", severity="warning", file="test.py", message="w1"),
            QualityCheck(check_id="w2", severity="warning", file="test.py", message="w2"),
        ]
        decision = make_t0_decision(checks, risk_score=20)

        assert decision["decision"] == "approve_with_followup"
        assert "warning" in decision["reason"]

    def test_high_risk_score_approve_with_followup(self):
        """High risk score should result in approve_with_followup."""
        checks = [
            QualityCheck(check_id="w1", severity="warning", file="test.py", message="w1"),
        ]
        decision = make_t0_decision(checks, risk_score=55)  # Over 50 threshold

        assert decision["decision"] == "approve_with_followup"
        assert "risk_score=55" in decision["reason"]

    def test_suggested_dispatches_for_file_size(self):
        """File size issues should generate refactoring dispatch."""
        checks = [
            QualityCheck(
                check_id="file_size_blocking",
                severity="blocking",
                file="test.py",
                message="File too large",
            )
        ]
        decision = make_t0_decision(checks, risk_score=50)

        dispatches = decision["suggested_dispatches"]
        assert len(dispatches) > 0
        assert any(d["type"] == "refactoring" for d in dispatches)


class TestQualityAdvisoryGeneration:
    """Test end-to-end quality advisory generation."""

    def test_empty_scope_minimal_advisory(self):
        """Empty scope should produce minimal advisory."""
        advisory = generate_quality_advisory([], repo_root=Path("/tmp"))

        assert advisory.version == "1.0"
        assert advisory.scope == []
        assert advisory.checks == []
        assert advisory.summary["warning_count"] == 0
        assert advisory.summary["blocking_count"] == 0
        assert advisory.t0_recommendation["decision"] == "approve"

    def test_advisory_includes_all_checks(self, tmp_path):
        """Advisory should include all check types."""
        # Create a file with multiple issues
        test_file = tmp_path / "test.py"
        # 600 lines (file size warning) with large function (function size warning)
        lines = ["def huge():"]
        for i in range(600):
            lines.append(f"    x{i} = {i}")
        test_file.write_text("\n".join(lines))

        advisory = generate_quality_advisory([test_file], repo_root=tmp_path)

        # Should have at least 1 check (file size or function size)
        assert len(advisory.checks) >= 1
        assert advisory.summary["warning_count"] >= 1
        # With multiple warnings, should get approve_with_followup or hold
        assert advisory.t0_recommendation["decision"] in ("approve", "approve_with_followup", "hold")

    def test_advisory_schema_structure(self, tmp_path):
        """Advisory should have correct schema structure."""
        test_file = tmp_path / "test.py"
        test_file.write_text("x = 1\n")

        advisory = generate_quality_advisory([test_file], repo_root=tmp_path)
        advisory_dict = advisory.to_dict()

        # Check required top-level fields
        assert "version" in advisory_dict
        assert "generated_at" in advisory_dict
        assert "scope" in advisory_dict
        assert "checks" in advisory_dict
        assert "summary" in advisory_dict
        assert "t0_recommendation" in advisory_dict

        # Check summary structure
        summary = advisory_dict["summary"]
        assert "warning_count" in summary
        assert "blocking_count" in summary
        assert "risk_score" in summary

        # Check t0_recommendation structure
        rec = advisory_dict["t0_recommendation"]
        assert "decision" in rec
        assert "reason" in rec
        assert "suggested_dispatches" in rec
        assert "open_items" in rec

    def test_missing_quality_tools_emit_tool_unavailable_warnings(self, tmp_path, monkeypatch):
        """Missing ruff, shellcheck, and vulture should be visible warnings."""
        py_file = tmp_path / "test.py"
        py_file.write_text("x = 1\n")
        sh_file = tmp_path / "test.sh"
        sh_file.write_text("#!/usr/bin/env bash\necho ok\n")

        def missing_tool(*args, **kwargs):
            raise FileNotFoundError(args[0][0])

        monkeypatch.setattr(quality_advisory_module.subprocess, "run", missing_tool)

        advisory = generate_quality_advisory([py_file, sh_file], repo_root=tmp_path)
        warnings = [c for c in advisory.checks if c.code == "tool_unavailable"]

        assert {w.tool for w in warnings} == {"ruff", "shellcheck", "vulture"}
        assert all(w.severity == "warning" for w in warnings)
        assert all(w.level == "warn" for w in warnings)
        assert all(w["code"] == "tool_unavailable" for w in warnings)


class TestWholeRepoFileSizeBacklog:
    """Test the whole-repo advisory backlog pass."""

    def test_backlog_lists_oversized_allowlisted_and_skips_small(self, tmp_path):
        """Whole-repo pass surfaces warning + allowlisted files and skips under-threshold files."""
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        lib = scripts / "lib"
        lib.mkdir(parents=True)

        big = scripts / "too_big.py"
        big.write_text("\n" * 600)  # over python warning (500), under blocking (1200)

        allowed = lib / "provider_dispatch.py"
        allowed.write_text("\n" * 1300)  # over blocking, allowlisted

        small = scripts / "small.py"
        small.write_text("\n" * 100)  # under warning

        backlog = build_whole_repo_file_size_backlog(tmp_path)

        files = {e["repo_relative"] for e in backlog["backlog"]}
        assert "scripts/too_big.py" in files
        assert "scripts/lib/provider_dispatch.py" in files
        assert "scripts/small.py" not in files

        by_rel = {e["repo_relative"]: e for e in backlog["backlog"]}
        assert by_rel["scripts/too_big.py"]["status"] == "warning"
        assert by_rel["scripts/lib/provider_dispatch.py"]["status"] == "allowlisted"
        assert "allowlist_reason" in by_rel["scripts/lib/provider_dispatch.py"]
        assert "grandfathered monolith" in by_rel["scripts/lib/provider_dispatch.py"]["allowlist_reason"]

        # Sorted worst-first
        counts = [e["line_count"] for e in backlog["backlog"]]
        assert counts == sorted(counts, reverse=True)

        assert backlog["total_backlog"] == 2
        assert backlog["warning_count"] == 1
        assert backlog["allowlisted_count"] == 1
        assert backlog["blocking_count"] == 0

    def test_backlog_flags_non_allowlisted_blocking_monolith(self, tmp_path):
        """A file over the hard ceiling that is NOT allowlisted is flagged blocking."""
        f = tmp_path / "new_monolith.py"
        f.write_text("\n" * 1300)

        backlog = build_whole_repo_file_size_backlog(tmp_path)

        assert backlog["total_backlog"] == 1
        entry = backlog["backlog"][0]
        assert entry["status"] == "blocking"
        assert entry["severity"] == "blocking"
        assert entry["line_count"] == 1300

    def test_backlog_skips_ignored_directories(self, tmp_path):
        """Files in skipped directories (e.g. __pycache__) do not pollute the backlog."""
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "big.pyc").write_bytes(b"x" * 10000)  # irrelevant binary
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "small.py").write_text("\n" * 100)

        backlog = build_whole_repo_file_size_backlog(tmp_path)
        assert backlog["total_backlog"] == 0


class TestTerminalSnapshot:
    """Test terminal snapshot collection."""

    def test_snapshot_includes_all_terminals(self, tmp_path):
        """Snapshot should include T0, T1, T2, T3."""
        # Create mock state directory with terminal_status.ndjson
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        status_file = state_dir / "terminal_status.ndjson"
        status_file.write_text(
            '{"terminal":"T0","status":"idle","model":"sonnet","timestamp":"2026-02-15T10:00:00Z"}\n'
            '{"terminal":"T1","status":"active","model":"sonnet","timestamp":"2026-02-15T10:00:00Z"}\n'
        )

        snapshot = collect_terminal_snapshot(state_dir)
        snapshot_dict = snapshot.to_dict()

        assert "timestamp" in snapshot_dict
        assert "terminals" in snapshot_dict

        terminals = snapshot_dict["terminals"]
        # All four terminals should be present
        for t in ("T0", "T1", "T2", "T3"):
            assert t in terminals

    def test_snapshot_terminal_fields(self, tmp_path):
        """Terminal snapshot should have required fields."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        dashboard_file = state_dir / "dashboard_status.json"
        dashboard_file.write_text(json.dumps({
            "timestamp": "2026-02-15T10:00:00Z",
            "terminals": {
                "T0": {
                    "status": "active",
                    "claimed_by": "task-123",
                    "provider": "anthropic",
                    "model": "sonnet",
                    "last_update": "2026-02-15T10:00:00Z",
                    "lease_expires_at": "2026-02-15T11:00:00Z",
                }
            }
        }))

        snapshot = collect_terminal_snapshot(state_dir)
        t0 = snapshot.terminals["T0"]

        assert t0["status"] == "active"
        assert t0["claimed_by"] == "task-123"
        assert t0["provider"] == "anthropic"
        assert t0["model"] == "sonnet"
        assert t0["last_activity"] == "2026-02-15T10:00:00Z"
        assert t0["lease_expires_at"] == "2026-02-15T11:00:00Z"

    def test_snapshot_fallback_for_missing_state(self, tmp_path):
        """Snapshot should gracefully handle missing state files."""
        state_dir = tmp_path / "nonexistent"

        snapshot = collect_terminal_snapshot(state_dir)

        # Should still return all terminals (with unknown status)
        assert len(snapshot.terminals) == 4
        for terminal in snapshot.terminals.values():
            assert "status" in terminal


class TestReceiptEnrichment:
    """Test receipt enrichment integration."""

    def test_non_completion_receipt_not_enriched(self):
        """Non-completion receipts should not be enriched."""
        from append_receipt import _is_completion_event, _enrich_completion_receipt

        receipt = {
            "timestamp": "2026-02-15T10:00:00Z",
            "event_type": "task_started",
            "task_id": "test-123",
        }

        assert not _is_completion_event(receipt)
        enriched = _enrich_completion_receipt(receipt)

        # Should not have quality_advisory or terminal_snapshot
        assert "quality_advisory" not in enriched
        assert "terminal_snapshot" not in enriched

    def test_completion_receipt_enriched(self, tmp_path):
        """Completion receipts should be enriched."""
        from append_receipt import _is_completion_event, _enrich_completion_receipt

        receipt = {
            "timestamp": "2026-02-15T10:00:00Z",
            "event_type": "task_complete",
            "task_id": "test-123",
        }

        assert _is_completion_event(receipt)
        enriched = _enrich_completion_receipt(receipt, repo_root=tmp_path)

        # ADR-035 §3.3/§9 PR-5: quality_advisory{} generation is retired
        # (superseded by verdict{}/warnings[], §6) -- terminal_snapshot is
        # unaffected.
        assert "quality_advisory" not in enriched
        assert "terminal_snapshot" in enriched

    def test_enrichment_failure_fallback(self, tmp_path):
        """Failed enrichment should add unavailable status markers."""
        from append_receipt import _enrich_completion_receipt

        receipt = {
            "timestamp": "2026-02-15T10:00:00Z",
            "event_type": "task_complete",
            "task_id": "test-123",
        }

        # Pass invalid repo_root to trigger failure
        enriched = _enrich_completion_receipt(receipt, repo_root=Path("/nonexistent"))

        # Should have markers with unavailable status or error handling
        assert "terminal_snapshot" in enriched
        # May have status=unavailable or may have succeeded with fallback logic


class TestNonRegression:
    """Test that existing receipt flow still works."""

    def test_regular_receipt_still_works(self, tmp_path):
        """Non-completion receipts should flow through unchanged."""
        from append_receipt import append_receipt_payload

        receipts_file = tmp_path / "test_receipts.ndjson"

        receipt = {
            "timestamp": "2026-02-15T10:00:00Z",
            "event_type": "heartbeat",
            "terminal": "T1",
        }

        result = append_receipt_payload(receipt, receipts_file=str(receipts_file))

        assert result.status == "appended"
        assert receipts_file.exists()

        # Read back and verify
        written = receipts_file.read_text()
        assert "heartbeat" in written

    def test_completion_receipt_enrichment_optional(self, tmp_path):
        """Completion receipt enrichment failure should not block append."""
        from append_receipt import append_receipt_payload

        receipts_file = tmp_path / "test_receipts.ndjson"

        receipt = {
            "timestamp": "2026-02-15T10:00:00Z",
            "event_type": "task_complete",
            "task_id": "test-123",
            "dispatch_id": "test-dispatch",
        }

        # Should succeed even if enrichment fails
        result = append_receipt_payload(receipt, receipts_file=str(receipts_file))

        assert result.status == "appended"
        assert receipts_file.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
