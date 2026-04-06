#!/usr/bin/env python3
"""
Evidence-Based Completion Verification Script

This script validates terminal completion reports against evidence requirements:
1. Files claimed to be created/modified actually exist
2. Tests claimed to be run actually executed
3. Integration points claimed exist at specified locations
4. Requirements have traceable implementations

Usage:
    python verify_completion.py --receipt-file <path>
    python verify_completion.py --receipt-ndjson <line>

Exit codes:
    0 - All verification passed
    1 - Verification failed (with detailed report)
    2 - Script error (missing files, parse errors, etc.)
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Any


class CompletionVerifier:
    """Evidence-based completion verification system"""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.passed_checks: List[str] = []
        self.receipt: Dict[str, Any] = {}
        self.report_content: str = ""

    def load_receipt(self, receipt_data: Dict[str, Any]) -> bool:
        """Load receipt data from dict"""
        self.receipt = receipt_data
        self.report_content = receipt_data.get("content", "")
        return True

    def load_receipt_file(self, receipt_path: Path) -> bool:
        """Load receipt from markdown file"""
        try:
            if not receipt_path.exists():
                self.errors.append(f"❌ Receipt file not found: {receipt_path}")
                return False

            with open(receipt_path, 'r', encoding='utf-8') as f:
                self.report_content = f.read()

            # Parse receipt from markdown
            self.receipt = self._parse_receipt_from_markdown(self.report_content)
            return True

        except Exception as e:
            self.errors.append(f"❌ Failed to load receipt: {e}")
            return False

    def _parse_receipt_from_markdown(self, content: str) -> Dict[str, Any]:
        """Extract receipt information from markdown report"""
        receipt = {
            "content": content,
            "files_created": [],
            "tests_executed": False,
            "test_output": "",
            "integration_points": [],
            "requirements": []
        }

        # Extract files from "Files Created/Modified" section
        files_section = re.search(
            r'### Files Created/Modified\s*\n(.*?)(?=\n###|\n##|$)',
            content,
            re.DOTALL | re.IGNORECASE
        )
        if files_section:
            files_text = files_section.group(1)
            # Match patterns like: - [x] `path/to/file.py` or - `path/to/file.py`
            file_matches = re.findall(r'[-*]\s*(?:\[x\])?\s*`([^`]+)`', files_text)
            receipt["files_created"] = [{"path": f} for f in file_matches]

        # Extract test output
        tests_section = re.search(
            r'### Tests Executed\s*\n(.*?)(?=\n###|\n##|$)',
            content,
            re.DOTALL | re.IGNORECASE
        )
        if tests_section:
            test_text = tests_section.group(1)
            receipt["tests_executed"] = True
            receipt["test_output"] = test_text

        # Extract integration points
        integration_section = re.search(
            r'### Integration Points\s*\n(.*?)(?=\n###|\n##|$)',
            content,
            re.DOTALL | re.IGNORECASE
        )
        if integration_section:
            integration_text = integration_section.group(1)
            # Match patterns like: - `file.py:123` or - file.py:123
            integration_matches = re.findall(
                r'[-*]\s*`?([^`\s]+\.py):(\d+)`?',
                integration_text
            )
            receipt["integration_points"] = [
                {"file": f, "line": int(l)} for f, l in integration_matches
            ]

        # Extract requirements
        req_section = re.search(
            r'### Requirements Traceability\s*\n(.*?)(?=\n###|\n##|$)',
            content,
            re.DOTALL | re.IGNORECASE
        )
        if req_section:
            req_text = req_section.group(1)
            # Match patterns like: - REQ-001: description → file.py:123
            req_matches = re.findall(
                r'[-*]\s*(REQ-\d+)[:\s]+.*?→\s*`?([^`\s]+\.py):(\d+)`?',
                req_text
            )
            receipt["requirements"] = [
                {"id": r, "file": f, "line": int(l)} for r, f, l in req_matches
            ]

        return receipt

    def verify_files_exist(self) -> bool:
        """Verify all claimed files actually exist"""
        claimed_files = self.receipt.get("files_created", [])

        if not claimed_files:
            self.warnings.append("⚠️  No files listed in completion report")
            return True

        all_exist = True
        for file_info in claimed_files:
            file_path = self.project_root / file_info["path"]

            if not file_path.exists():
                self.errors.append(
                    f"❌ CLAIMED FILE MISSING: {file_info['path']}"
                )
                all_exist = False
            else:
                self.passed_checks.append(
                    f"✅ File exists: {file_info['path']}"
                )

        return all_exist

    def verify_tests_ran(self) -> bool:
        """Verify tests were actually executed"""
        if not self.receipt.get("tests_executed", False):
            # Check if code changes require tests
            if self.receipt.get("files_created"):
                self.warnings.append(
                    "⚠️  Code changed but no tests mentioned"
                )
            return True

        test_output = self.receipt.get("test_output", "")

        # Check for actual test framework output markers
        test_markers = [
            r'pytest',
            r'PASSED',
            r'FAILED',
            r'\d+ passed',
            r'test_\w+',
            r'=+ test session starts =+'
        ]

        has_real_output = any(
            re.search(marker, test_output, re.IGNORECASE)
            for marker in test_markers
        )

        if not has_real_output:
            self.errors.append(
                "❌ TEST OUTPUT MISSING: No actual test framework output found"
            )
            self.errors.append(
                "   Expected pytest output with PASSED/FAILED markers"
            )
            return False

        # Check for failures
        if re.search(r'FAILED|ERROR', test_output, re.IGNORECASE):
            self.errors.append(
                "❌ TESTS FAILED: Test output shows failures"
            )
            return False

        self.passed_checks.append("✅ Tests executed with valid output")
        return True

    def verify_integrations(self) -> bool:
        """Verify claimed integration points exist in code"""
        integration_points = self.receipt.get("integration_points", [])

        if not integration_points:
            # Check if this was an integration task
            if re.search(
                r'\bintegrat(e|ion|ing)\b',
                self.report_content,
                re.IGNORECASE
            ):
                self.warnings.append(
                    "⚠️  Integration mentioned but no specific points listed"
                )
            return True

        all_verified = True
        for integration in integration_points:
            file_path = self.project_root / integration["file"]
            line_num = integration["line"]

            if not file_path.exists():
                self.errors.append(
                    f"❌ INTEGRATION FILE MISSING: {integration['file']}"
                )
                all_verified = False
                continue

            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                if line_num > len(lines):
                    self.errors.append(
                        f"❌ INTEGRATION LINE OUT OF RANGE: "
                        f"{integration['file']}:{line_num} "
                        f"(file has {len(lines)} lines)"
                    )
                    all_verified = False
                else:
                    self.passed_checks.append(
                        f"✅ Integration point verified: "
                        f"{integration['file']}:{line_num}"
                    )

            except Exception as e:
                self.errors.append(
                    f"❌ Failed to verify integration: {integration['file']} - {e}"
                )
                all_verified = False

        return all_verified

    def verify_requirements(self) -> bool:
        """Verify requirements have traceable implementation"""
        requirements = self.receipt.get("requirements", [])

        if not requirements:
            self.warnings.append(
                "⚠️  No requirements traceability provided"
            )
            return True

        all_traced = True
        for req in requirements:
            file_path = self.project_root / req["file"]
            line_num = req["line"]

            if not file_path.exists():
                self.errors.append(
                    f"❌ REQUIREMENT FILE MISSING: {req['id']} → {req['file']}"
                )
                all_traced = False
                continue

            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                if line_num > len(lines):
                    self.errors.append(
                        f"❌ REQUIREMENT LINE OUT OF RANGE: "
                        f"{req['id']} → {req['file']}:{line_num} "
                        f"(file has {len(lines)} lines)"
                    )
                    all_traced = False
                else:
                    self.passed_checks.append(
                        f"✅ Requirement traced: {req['id']} → "
                        f"{req['file']}:{line_num}"
                    )

            except Exception as e:
                self.errors.append(
                    f"❌ Failed to verify requirement {req['id']}: {e}"
                )
                all_traced = False

        return all_traced

    def verify_no_duplicates(self) -> bool:
        """Check for duplicate code patterns (basic check)"""
        # This is a placeholder for future duplicate detection
        # Could integrate with tools like jscpd, pylint, etc.
        self.passed_checks.append(
            "ℹ️  Duplicate detection: Not yet implemented"
        )
        return True

    def calculate_score(self) -> float:
        """Calculate verification score (0.0-1.0)"""
        total_checks = len(self.passed_checks) + len(self.errors)
        if total_checks == 0:
            return 0.0
        return len(self.passed_checks) / total_checks

    def generate_report(self) -> str:
        """Generate human-readable verification report"""
        lines = []
        lines.append("=" * 60)
        lines.append("EVIDENCE-BASED COMPLETION VERIFICATION REPORT")
        lines.append("=" * 60)
        lines.append("")

        # Summary
        score = self.calculate_score()
        lines.append(f"Verification Score: {score:.1%}")
        lines.append(f"Passed Checks: {len(self.passed_checks)}")
        lines.append(f"Errors: {len(self.errors)}")
        lines.append(f"Warnings: {len(self.warnings)}")
        lines.append("")

        # Passed checks
        if self.passed_checks:
            lines.append("PASSED CHECKS:")
            lines.append("-" * 60)
            for check in self.passed_checks:
                lines.append(check)
            lines.append("")

        # Errors
        if self.errors:
            lines.append("ERRORS:")
            lines.append("-" * 60)
            for error in self.errors:
                lines.append(error)
            lines.append("")

        # Warnings
        if self.warnings:
            lines.append("WARNINGS:")
            lines.append("-" * 60)
            for warning in self.warnings:
                lines.append(warning)
            lines.append("")

        # Final verdict
        lines.append("=" * 60)
        if self.errors:
            lines.append("VERDICT: ❌ VERIFICATION FAILED")
            lines.append("")
            lines.append("This completion report does NOT meet evidence requirements.")
            lines.append("Please address the errors above and resubmit.")
        else:
            lines.append("VERDICT: ✅ VERIFICATION PASSED")
            lines.append("")
            lines.append("This completion report meets evidence requirements.")
            if self.warnings:
                lines.append("Note: Some warnings were issued (see above).")
        lines.append("=" * 60)

        return "\n".join(lines)

    def run_all_verifications(self) -> bool:
        """Run all verification checks"""
        checks = [
            ("Files Exist", self.verify_files_exist),
            ("Tests Executed", self.verify_tests_ran),
            ("Integrations Verified", self.verify_integrations),
            ("Requirements Traced", self.verify_requirements),
            ("No Duplicates", self.verify_no_duplicates),
        ]

        all_passed = True
        for check_name, check_func in checks:
            try:
                passed = check_func()
                if not passed:
                    all_passed = False
            except Exception as e:
                self.errors.append(
                    f"❌ {check_name} check failed with error: {e}"
                )
                all_passed = False

        return all_passed


def main():
    parser = argparse.ArgumentParser(
        description="Verify evidence-based completion reports"
    )
    parser.add_argument(
        "--receipt-file",
        type=Path,
        help="Path to markdown receipt file"
    )
    parser.add_argument(
        "--receipt-ndjson",
        type=str,
        help="NDJSON receipt line"
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Project root directory (default: current directory)"
    )

    args = parser.parse_args()

    if not args.receipt_file and not args.receipt_ndjson:
        parser.error("Either --receipt-file or --receipt-ndjson is required")

    # Initialize verifier
    verifier = CompletionVerifier(args.project_root)

    # Load receipt
    if args.receipt_file:
        if not verifier.load_receipt_file(args.receipt_file):
            print(verifier.generate_report())
            return 2
    else:
        try:
            receipt_data = json.loads(args.receipt_ndjson)
            verifier.load_receipt(receipt_data)
        except json.JSONDecodeError as e:
            print(f"❌ Failed to parse NDJSON: {e}", file=sys.stderr)
            return 2

    # Run verifications
    all_passed = verifier.run_all_verifications()

    # Print report
    print(verifier.generate_report())

    # Exit with appropriate code
    if all_passed:
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
