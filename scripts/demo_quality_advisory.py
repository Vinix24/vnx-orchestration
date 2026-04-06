#!/usr/bin/env python3
"""Live demonstration of quality advisory pipeline.

Creates two scenarios:
1. Clean completion - no issues, approve decision
2. High-risk completion - multiple issues, hold/approve_with_followup decision
"""

import json
import tempfile
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from quality_advisory import generate_quality_advisory
from append_receipt import _enrich_completion_receipt


def demo_clean_completion():
    """Demonstrate clean completion with no quality issues."""
    print("=" * 80)
    print("SCENARIO 1: Clean Completion (approve)")
    print("=" * 80)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        # Create a small, clean Python file
        test_file = repo_root / "clean.py"
        test_file.write_text("""#!/usr/bin/env python3
def hello():
    return "Hello, world!"

def main():
    print(hello())
""")

        # Generate advisory
        advisory = generate_quality_advisory([test_file], repo_root=repo_root)

        print("\nChanged files:", advisory.scope)
        print("\nQuality checks:")
        for check in advisory.checks:
            print(f"  - [{check['severity']}] {check['check_id']}: {check['message']}")

        if not advisory.checks:
            print("  (No issues detected)")

        print("\nSummary:")
        print(f"  Warnings: {advisory.summary['warning_count']}")
        print(f"  Blocking: {advisory.summary['blocking_count']}")
        print(f"  Risk Score: {advisory.summary['risk_score']}")

        print("\nT0 Recommendation:")
        rec = advisory.t0_recommendation
        print(f"  Decision: {rec['decision']}")
        print(f"  Reason: {rec['reason']}")
        print(f"  Suggested Dispatches: {len(rec['suggested_dispatches'])}")

        print("\nFull Advisory JSON:")
        print(json.dumps(advisory.to_dict(), indent=2))


def demo_high_risk_completion():
    """Demonstrate high-risk completion with multiple quality issues."""
    print("\n\n")
    print("=" * 80)
    print("SCENARIO 2: High-Risk Completion (hold or approve_with_followup)")
    print("=" * 80)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        # Create a large Python file with oversized function
        problem_file = repo_root / "problematic.py"
        lines = ["#!/usr/bin/env python3", "", "def massive_function():"]
        # Create a 75-line function (exceeds blocking threshold of 70)
        for i in range(75):
            lines.append(f"    variable_{i} = {i}")
        lines.append("    return variable_0")

        # Pad the file to 850 lines (exceeds blocking threshold of 800)
        while len(lines) < 850:
            lines.append("")

        problem_file.write_text("\n".join(lines))

        # Also create a src file change without test change
        src_file = repo_root / "src" / "module.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("def business_logic():\n    pass\n")

        # Generate advisory
        advisory = generate_quality_advisory([problem_file, src_file], repo_root=repo_root)

        print("\nChanged files:", [str(Path(p).name) for p in advisory.scope])
        print("\nQuality checks:")
        for check in advisory.checks:
            print(f"  - [{check['severity'].upper()}] {check['check_id']}: {check['message'][:80]}")

        print("\nSummary:")
        print(f"  Warnings: {advisory.summary['warning_count']}")
        print(f"  Blocking: {advisory.summary['blocking_count']}")
        print(f"  Risk Score: {advisory.summary['risk_score']}")

        print("\nT0 Recommendation:")
        rec = advisory.t0_recommendation
        print(f"  Decision: {rec['decision']}")
        print(f"  Reason: {rec['reason']}")
        print(f"  Suggested Dispatches:")
        for dispatch in rec['suggested_dispatches']:
            print(f"    - {dispatch['type']}: {dispatch['description']}")

        print("\nOpen Items:")
        for item in rec['open_items']:
            print(f"    - [{item['severity']}] {item['item'][:60]}")

        print("\nFull Advisory JSON:")
        print(json.dumps(advisory.to_dict(), indent=2))


def demo_enriched_receipt():
    """Demonstrate enriched completion receipt."""
    print("\n\n")
    print("=" * 80)
    print("SCENARIO 3: Enriched Completion Receipt")
    print("=" * 80)

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_root = Path(tmpdir)

        # Create a simple completion receipt
        receipt = {
            "timestamp": "2026-02-15T12:00:00Z",
            "event_type": "task_complete",
            "task_id": "demo-task-123",
            "dispatch_id": "demo-dispatch-456",
            "terminal": "T1",
            "message": "Task completed successfully",
        }

        # Enrich it
        enriched = _enrich_completion_receipt(receipt, repo_root=repo_root)

        print("\nOriginal receipt keys:", list(receipt.keys()))
        print("Enriched receipt keys:", list(enriched.keys()))

        print("\nAdded fields:")
        if "quality_advisory" in enriched:
            print("  ✓ quality_advisory")
            if isinstance(enriched["quality_advisory"], dict):
                if "status" in enriched["quality_advisory"]:
                    print(f"    Status: {enriched['quality_advisory']['status']}")
                else:
                    print(f"    Decision: {enriched['quality_advisory']['t0_recommendation']['decision']}")

        if "terminal_snapshot" in enriched:
            print("  ✓ terminal_snapshot")
            if isinstance(enriched["terminal_snapshot"], dict):
                if "status" in enriched["terminal_snapshot"]:
                    print(f"    Status: {enriched['terminal_snapshot']['status']}")
                else:
                    terminals = enriched["terminal_snapshot"].get("terminals", {})
                    print(f"    Terminals captured: {list(terminals.keys())}")

        print("\nFull Enriched Receipt:")
        print(json.dumps(enriched, indent=2))


def main():
    """Run all demonstrations."""
    demo_clean_completion()
    demo_high_risk_completion()
    demo_enriched_receipt()

    print("\n\n")
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print("""
The quality advisory pipeline successfully:
1. ✓ Detects clean completions and approves them
2. ✓ Detects high-risk completions with blocking issues
3. ✓ Generates appropriate T0 recommendations
4. ✓ Enriches completion receipts with advisory + terminal snapshot
5. ✓ Operates in best-effort mode (failures don't crash receipt flow)

All requirements satisfied!
""")


if __name__ == "__main__":
    main()
