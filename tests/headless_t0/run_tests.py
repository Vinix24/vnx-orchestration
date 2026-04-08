#!/usr/bin/env python3
"""Headless T0 test runner — executes 7 progressive orchestration tests.

Each test makes a real claude -p --model opus call against an isolated sandbox.
Tests are designed to be run one at a time during development, or all at once
for regression validation.

Usage:
    python3 tests/headless_t0/run_tests.py --test 1
    python3 tests/headless_t0/run_tests.py --all
    python3 tests/headless_t0/run_tests.py --test 1 --dry-run
    python3 tests/headless_t0/run_tests.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

# Ensure tests/headless_t0/ is importable
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from assertions import (  # type: ignore[import]
    AssertionError,
    assert_decision_mentions,
    assert_dispatch_created,
    assert_dispatch_format,
    assert_file_read,
    assert_gate_refused,
    assert_no_dispatch_created,
)
from fake_data import (  # type: ignore[import]
    fake_dispatch,
    fake_open_items,
    fake_report_gate_fail,
    fake_report_partial,
    fake_report_success,
    fake_t0_brief,
)
from setup_sandbox import (  # type: ignore[import]
    SANDBOX_PATH,
    inject_receipt,
    inject_report,
    reset_sandbox,
    set_terminal_status,
    write_fake_file,
)

CLAUDE_MODEL = "claude-opus-4-6"
MAX_TURNS = 20


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    test_num: int
    name: str
    passed: bool
    output: str
    error: str = ""

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"[{status}] Test {self.test_num}: {self.name}"]
        if not self.passed:
            lines.append(f"  Error: {self.error}")
            lines.append(f"  Output snippet: {self.output[:300]!r}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------

def run_claude(prompt: str, dry_run: bool = False) -> str:
    """Invoke claude -p with the given prompt in the sandbox cwd.

    Returns the result text on success.
    Raises RuntimeError on non-zero exit.
    """
    if dry_run:
        print("=== DRY RUN — prompt that would be sent to claude -p ===")
        print(prompt)
        print("=== END DRY RUN ===")
        return "[dry-run]"

    cmd = [
        "claude",
        "-p",
        "--model", CLAUDE_MODEL,
        "--output-format", "json",
        "--dangerously-skip-permissions",
        "--max-turns", str(MAX_TURNS),
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(SANDBOX_PATH),
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude -p timed out after 300s")
    except FileNotFoundError:
        raise RuntimeError("claude CLI not found — ensure Claude Code CLI is installed")

    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {result.returncode}: {result.stderr[:300]}"
        )

    try:
        outer = json.loads(result.stdout)
        return outer.get("result", result.stdout)
    except json.JSONDecodeError:
        return result.stdout


# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------

class HeadlessT0TestRunner:

    def test_1_receipt_reading(self, dry_run: bool = False) -> TestResult:
        """Can T0 read a receipt and follow report_path to the report?"""
        dispatch_id = "20260407-sandbox-t1-receipt-A"
        report_filename = "20260407-sandbox-t1-A-success.md"

        sb = reset_sandbox()
        report_path = str(sb / ".vnx-data" / "unified_reports" / report_filename)

        inject_report(report_filename, fake_report_success(dispatch_id, "A"))
        inject_receipt(dispatch_id, "T1", "success", report_path)

        prompt = textwrap.dedent(f"""\
            A new receipt has arrived in .vnx-data/state/t0_receipts.ndjson.

            1. Read .vnx-data/state/t0_receipts.ndjson
            2. Find the receipt with dispatch_id="{dispatch_id}"
            3. Follow its report_path and read that file
            4. Summarize what the worker did and whether you accept the receipt
        """)

        try:
            output = run_claude(prompt, dry_run)
            if not dry_run:
                assert_decision_mentions(output, [dispatch_id, "success"])
                assert_file_read(output, report_filename)
            return TestResult(1, "receipt_reading", True, output)
        except (AssertionError, RuntimeError) as exc:
            return TestResult(1, "receipt_reading", False, output if "output" in dir() else "", str(exc))

    def test_2_claim_verification(self, dry_run: bool = False) -> TestResult:
        """Can T0 verify worker claims against actual filesystem evidence?"""
        dispatch_id = "20260407-sandbox-t2-verify-A"
        report_filename = "20260407-sandbox-t2-A-verify.md"
        target_file = "scripts/lib/example_module.py"

        sb = reset_sandbox()

        # Create a fake file at exactly 400 lines
        fake_file_content = "\n".join(
            [f"# line {i + 1}" for i in range(400)]
        )
        write_fake_file(target_file, fake_file_content)

        # Report claims file was reduced to 400 lines
        report_content = fake_report_success(dispatch_id, "A").replace(
            "150-line implementation",
            "reduced example_module.py to 400 lines",
        )
        report_path = str(sb / ".vnx-data" / "unified_reports" / report_filename)
        inject_report(report_filename, report_content)
        inject_receipt(dispatch_id, "T1", "success", report_path)

        prompt = textwrap.dedent(f"""\
            Review this receipt from T1:
            - dispatch_id: {dispatch_id}
            - Report claims: "example_module.py was reduced to 400 lines"

            1. Read the receipt from .vnx-data/state/t0_receipts.ndjson
            2. Read the report at its report_path
            3. Verify the claim: check the actual line count of {target_file}
            4. State whether the claim is accurate and whether you accept the receipt
        """)

        try:
            output = run_claude(prompt, dry_run)
            if not dry_run:
                assert_decision_mentions(output, ["400", "accept"])
                assert_file_read(output, "example_module.py")
            return TestResult(2, "claim_verification", True, output)
        except (AssertionError, RuntimeError) as exc:
            return TestResult(2, "claim_verification", False, output if "output" in dir() else "", str(exc))

    def test_3_dispatch_creation(self, dry_run: bool = False) -> TestResult:
        """Can T0 write a valid dispatch file to .vnx-data/dispatches/pending/?"""
        sb = reset_sandbox()

        # T2 is idle, T3 is idle — T0 should dispatch Track B
        set_terminal_status("T1", "idle")
        set_terminal_status("T2", "idle")
        set_terminal_status("T3", "idle")

        prompt = textwrap.dedent("""\
            Review the current system state:
            1. Read .vnx-data/state/t0_brief.json
            2. Read .vnx-data/state/t0_recommendations.json

            T2 is idle and Track B needs a test-engineer dispatch.
            Create a dispatch file in .vnx-data/dispatches/pending/ for Track B.

            The dispatch must:
            - Start with [[TARGET:B]]
            - Include Manager Block header
            - Include Role: test-engineer
            - Include a Dispatch-ID (format: YYYYMMDD-HHMMSS-f36-sandbox-B)
            - Include an Instruction section describing: "Write integration tests for the sandbox feature"
        """)

        try:
            output = run_claude(prompt, dry_run)
            if not dry_run:
                dispatch_path = assert_dispatch_created(sb, "B")
                assert_dispatch_format(dispatch_path)
                assert_decision_mentions(output, ["dispatch", "Track B"])
            return TestResult(3, "dispatch_creation", True, output)
        except (AssertionError, RuntimeError) as exc:
            return TestResult(3, "dispatch_creation", False, output if "output" in dir() else "", str(exc))

    def test_4_open_items(self, dry_run: bool = False) -> TestResult:
        """Can T0 reason about open items and make close/defer decisions?"""
        dispatch_id = "20260407-sandbox-t4-oi-A"
        report_filename = "20260407-sandbox-t4-A-oi.md"

        sb = reset_sandbox()

        # Report provides evidence for OI-001
        report_content = (
            fake_report_success(dispatch_id, "A")
            + "\n\nEvidence for OI-001: The blocker was resolved in commit abc1234. "
              "File scripts/lib/example.py now handles the edge case correctly.\n"
        )
        report_path = str(sb / ".vnx-data" / "unified_reports" / report_filename)
        inject_report(report_filename, report_content)
        inject_receipt(dispatch_id, "T1", "success", report_path)

        prompt = textwrap.dedent(f"""\
            Review the open items and incoming receipt:
            1. Read .vnx-data/state/open_items.json
            2. Read the receipt from .vnx-data/state/t0_receipts.ndjson
            3. Read the report at its report_path
            4. Determine: does the report provide sufficient evidence to close OI-001?
            5. State your decision: close OI-001 (with reason) or defer (with reason)
        """)

        try:
            output = run_claude(prompt, dry_run)
            if not dry_run:
                assert_decision_mentions(output, ["OI-001"])
                # T0 should make a close or defer decision
                has_decision = any(
                    kw in output.lower() for kw in ["close", "defer", "resolve", "evidence"]
                )
                if not has_decision:
                    raise AssertionError("T0 did not make a close/defer decision for OI-001")
            return TestResult(4, "open_items", True, output)
        except (AssertionError, RuntimeError) as exc:
            return TestResult(4, "open_items", False, output if "output" in dir() else "", str(exc))

    def test_5_multi_worker(self, dry_run: bool = False) -> TestResult:
        """Can T0 handle two simultaneous receipts and dispatch to idle terminal?"""
        dispatch_id_a = "20260407-sandbox-t5-multi-A"
        dispatch_id_b = "20260407-sandbox-t5-multi-B"
        report_a = "20260407-sandbox-t5-A-multi.md"
        report_b = "20260407-sandbox-t5-B-multi.md"

        sb = reset_sandbox()

        report_path_a = str(sb / ".vnx-data" / "unified_reports" / report_a)
        report_path_b = str(sb / ".vnx-data" / "unified_reports" / report_b)

        inject_report(report_a, fake_report_success(dispatch_id_a, "A"))
        inject_report(report_b, fake_report_success(dispatch_id_b, "B"))
        inject_receipt(dispatch_id_a, "T1", "success", report_path_a, track="A")
        inject_receipt(dispatch_id_b, "T2", "success", report_path_b, track="B")

        set_terminal_status("T1", "idle")
        set_terminal_status("T2", "idle")
        set_terminal_status("T3", "idle")

        prompt = textwrap.dedent(f"""\
            Two receipts have arrived. Review both and determine next actions:
            1. Read .vnx-data/state/t0_receipts.ndjson — there are 2 receipts
            2. Read each receipt's report_path
            3. Assess both: are they acceptable?
            4. T3 is idle. If both receipts pass review, create a dispatch for T3 (Track C)
               with Role: reviewer to review the combined work from both tracks.

            Dispatch to .vnx-data/dispatches/pending/ if appropriate.
        """)

        try:
            output = run_claude(prompt, dry_run)
            if not dry_run:
                assert_decision_mentions(output, [dispatch_id_a, dispatch_id_b])
                assert_dispatch_created(sb, "C")
            return TestResult(5, "multi_worker", True, output)
        except (AssertionError, RuntimeError) as exc:
            return TestResult(5, "multi_worker", False, output if "output" in dir() else "", str(exc))

    def test_6_gate_discipline(self, dry_run: bool = False) -> TestResult:
        """Does T0 refuse to approve a merge when gate evidence is missing?"""
        sb = reset_sandbox()

        # No gate evidence files — no review_gates results
        gate_results_dir = sb / ".vnx-data" / "state" / "gate_results"
        gate_results_dir.mkdir(parents=True, exist_ok=True)
        # Directory exists but is empty

        prompt = textwrap.dedent("""\
            The operator is asking: "Should we merge F36 to main?"

            1. Check .vnx-data/state/t0_brief.json for current state
            2. Check .vnx-data/state/ for any gate evidence (review_gates, gate_results/)
            3. Make a governance decision: approve merge or refuse and state why

            Be strict about gate requirements. Do not approve without evidence.
        """)

        try:
            output = run_claude(prompt, dry_run)
            if not dry_run:
                assert_gate_refused(output)
            return TestResult(6, "gate_discipline", True, output)
        except (AssertionError, RuntimeError) as exc:
            return TestResult(6, "gate_discipline", False, output if "output" in dir() else "", str(exc))

    def test_7_full_cycle(self, dry_run: bool = False) -> TestResult:
        """Complete orchestration cycle: receipt → read report → verify → dispatch → decision log."""
        dispatch_id = "20260407-sandbox-t7-full-A"
        report_filename = "20260407-sandbox-t7-A-full.md"
        target_file = "scripts/lib/full_cycle_module.py"

        sb = reset_sandbox()

        # Create a verifiable file
        file_content = "\n".join([f"def func_{i}(): pass" for i in range(50)])
        write_fake_file(target_file, file_content)

        # Report references the file
        report_content = (
            fake_report_success(dispatch_id, "A")
            + f"\n\nKey change: created {target_file} with 50 functions.\n"
        )
        report_path = str(sb / ".vnx-data" / "unified_reports" / report_filename)
        inject_report(report_filename, report_content)
        inject_receipt(dispatch_id, "T1", "success", report_path)

        set_terminal_status("T1", "idle")
        set_terminal_status("T2", "idle")

        decision_log = sb / ".vnx-data" / "state" / "t0_decision_log.jsonl"

        prompt = textwrap.dedent(f"""\
            Complete orchestration cycle for dispatch {dispatch_id}:

            1. Read .vnx-data/state/t0_receipts.ndjson
            2. Find the receipt for {dispatch_id} and read its report_path
            3. Verify the key claim: {target_file} exists and has ~50 functions
               (use Read or Bash wc -l to check)
            4. If verified: create a follow-up dispatch for T2 (Track B) in
               .vnx-data/dispatches/pending/ with Role: test-engineer
            5. Write your decision to .vnx-data/state/t0_decision_log.jsonl as a JSON line:
               {{"timestamp":"...", "action":"approve", "dispatch_id":"{dispatch_id}",
                "reasoning":"...", "next_expected":"Track B test dispatch"}}
        """)

        try:
            output = run_claude(prompt, dry_run)
            if not dry_run:
                assert_dispatch_created(sb, "B")
                assert_file_read(output, target_file)
                # Decision log should exist and have content
                if not decision_log.exists():
                    raise AssertionError(
                        f"Decision log not written to {decision_log}"
                    )
                log_lines = [l for l in decision_log.read_text().splitlines() if l.strip()]
                if not log_lines:
                    raise AssertionError("Decision log is empty")
                # Validate it's valid JSON
                json.loads(log_lines[-1])
            return TestResult(7, "full_cycle", True, output)
        except (AssertionError, RuntimeError, json.JSONDecodeError) as exc:
            return TestResult(7, "full_cycle", False, output if "output" in dir() else "", str(exc))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

ALL_TEST_NUMS = list(range(1, 8))

TEST_DESCRIPTIONS = {
    1: "receipt_reading — Can T0 read a receipt and follow report_path?",
    2: "claim_verification — Can T0 verify worker claims against code?",
    3: "dispatch_creation — Can T0 write a valid dispatch to pending/?",
    4: "open_items — Can T0 manage open items (close/defer decisions)?",
    5: "multi_worker — Can T0 handle two simultaneous receipts?",
    6: "gate_discipline — Does T0 refuse merge without gate evidence?",
    7: "full_cycle — Complete cycle: receipt → verify → dispatch → decision log",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run headless T0 orchestration tests (real Opus calls)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", type=int, metavar="N", help="Run single test N (1-7)")
    group.add_argument("--all", action="store_true", help="Run all 7 tests")
    group.add_argument("--list", action="store_true", help="List all tests")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt, don't call claude")
    args = parser.parse_args(argv)

    if args.list:
        print("Available tests:")
        for num, desc in TEST_DESCRIPTIONS.items():
            print(f"  {num}: {desc}")
        return 0

    runner = HeadlessT0TestRunner()
    test_methods = {
        1: runner.test_1_receipt_reading,
        2: runner.test_2_claim_verification,
        3: runner.test_3_dispatch_creation,
        4: runner.test_4_open_items,
        5: runner.test_5_multi_worker,
        6: runner.test_6_gate_discipline,
        7: runner.test_7_full_cycle,
    }

    to_run = ALL_TEST_NUMS if args.all else [args.test]
    results: list[TestResult] = []

    for num in to_run:
        if num not in test_methods:
            print(f"Unknown test number: {num}. Valid range: 1-7")
            return 1
        print(f"\nRunning Test {num}: {TEST_DESCRIPTIONS[num]}")
        print("-" * 60)
        result = test_methods[num](dry_run=args.dry_run)
        results.append(result)
        print(result.summary())

    if not args.dry_run:
        print("\n" + "=" * 60)
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        print(f"Results: {passed}/{total} passed")
        return 0 if passed == total else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
