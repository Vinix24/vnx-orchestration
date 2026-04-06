#!/usr/bin/env python3
"""
Report Validation Script - Pre-Submit Hook
Validates unified reports before they can be processed as receipts

Usage:
    python3 validate_report.py path/to/report.md

Exit codes:
    0 - Report is valid
    1 - Report has errors (blocks submission)
    2 - Report has warnings (allows submission but logs issues)
"""

import sys
import re
from pathlib import Path
from typing import Tuple, List

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from validate_template_tokens import validate_all_templates


class ReportValidator:
    """Validates unified reports for completeness and correctness"""

    # Required metadata fields
    REQUIRED_FIELDS = [
        'Terminal',
        'Date',
        'Task ID',
        'Dispatch ID',
        'Status'
    ]

    # Valid status values
    VALID_STATUSES = ['success', 'blocked', 'fail', 'in_progress']

    # Required sections
    REQUIRED_SECTIONS = [
        '## Summary',
        '## Open Items'
    ]

    def __init__(self, report_path: Path):
        self.report_path = report_path
        self.content = report_path.read_text()
        self.errors = []
        self.warnings = []
        self.metadata = {}

    def validate(self) -> Tuple[bool, List[str], List[str]]:
        """
        Run all validations

        Returns:
            (is_valid, errors, warnings)
        """
        self._validate_metadata()
        self._validate_sections()
        self._validate_completion_report()
        self._validate_open_items()
        self._validate_templates()

        is_valid = len(self.errors) == 0
        return is_valid, self.errors, self.warnings

    def _validate_metadata(self):
        """Validate required metadata fields"""
        # Extract metadata from first 20 lines
        lines = self.content.split('\n')[:20]

        for field in self.REQUIRED_FIELDS:
            pattern = rf'\*\*{field}\*\*:\s*(.+)'
            match = None
            for line in lines:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    self.metadata[field] = match.group(1).strip()
                    break

            if not match:
                self.errors.append(f"Missing required field: **{field}**")
            elif not self.metadata[field]:
                self.errors.append(f"Empty value for required field: **{field}**")

        # Validate status value
        if 'Status' in self.metadata:
            status = self.metadata['Status'].lower()
            if status not in self.VALID_STATUSES:
                self.errors.append(
                    f"Invalid status '{self.metadata['Status']}'. "
                    f"Must be one of: {', '.join(self.VALID_STATUSES)}"
                )

    def _validate_sections(self):
        """Validate required sections exist"""
        for section in self.REQUIRED_SECTIONS:
            if section not in self.content:
                self.errors.append(f"Missing required section: {section}")

    def _validate_completion_report(self):
        """Validate completion reports (status=success) have all required info"""
        if 'Status' not in self.metadata:
            return  # Already reported as error

        status = self.metadata['Status'].lower()

        # If claiming success, check for PR-ID (for completion tracking)
        if status == 'success':
            # Check if PR-ID field exists in metadata
            pr_id_match = re.search(r'\*\*PR-?ID\*\*:\s*(.+)', self.content, re.IGNORECASE)

            if not pr_id_match:
                # Check filename for PR ID as fallback
                filename = self.report_path.name
                filename_pr_match = re.search(r'pr[0-9]+', filename, re.IGNORECASE)

                if not filename_pr_match:
                    self.warnings.append(
                        "⚠️  Completion report (status=success) missing **PR-ID** field\n"
                        "    Auto-completion will NOT work without PR-ID in metadata or filename\n"
                        "    Add: **PR-ID**: PR-X  (if this completes a specific PR)"
                    )
            else:
                pr_id_value = pr_id_match.group(1).strip()
                if not pr_id_value or 'if applicable' in pr_id_value.lower():
                    self.warnings.append(
                        "⚠️  PR-ID field exists but has no value\n"
                        "    Add: **PR-ID**: PR-X  (if this completes a specific PR)\n"
                        "    Or remove PR-ID line if this is not a completion report"
                    )

    def _validate_open_items(self):
        """Validate Open Items section format"""
        # Find Open Items section
        open_items_match = re.search(
            r'## Open Items\s*\n(.*?)(?=\n##|\Z)',
            self.content,
            re.DOTALL
        )

        if not open_items_match:
            return  # Already reported as missing section error

        open_items_content = open_items_match.group(1).strip()

        # Check if section is empty (besides comments)
        content_without_comments = re.sub(r'<!--.*?-->', '', open_items_content, flags=re.DOTALL)
        content_without_whitespace = content_without_comments.strip()

        if not content_without_whitespace:
            self.errors.append(
                "Open Items section is empty\n"
                "    Must contain either:\n"
                "    - 'None - all work completed and tested.'\n"
                "    - List of open items with format: - [ ] [severity] Title"
            )
            return

        # Check if says "None" (valid for completion)
        if 'none' in content_without_whitespace.lower():
            return  # Valid

        # Otherwise, validate item format
        items = re.findall(r'-\s*\[\s*\]\s*\[(blocker|warn|info)\]', open_items_content, re.IGNORECASE)

        if not items:
            # Has content but no properly formatted items
            self.warnings.append(
                "⚠️  Open Items section has content but no properly formatted items\n"
                "    Expected format: - [ ] [severity] Title\n"
                "    Valid severities: [blocker], [warn], [info]"
            )

        # Check for blocker/warn items in success reports
        if 'Status' in self.metadata and self.metadata['Status'].lower() == 'success':
            blockers = [s for s in items if s.lower() in ['blocker', 'warn']]
            if blockers:
                self.warnings.append(
                    f"⚠️  Report claims success but has {len(blockers)} blocker/warn items\n"
                    "    Auto-completion will be BLOCKED\n"
                    "    Either resolve items first, or change status to 'in_progress' or 'blocked'"
                )

    def _validate_templates(self):
        """Ensure canonical templates still define required tokens/sections."""
        template_keys = ["report_template", "unified_report_template"]
        try:
            results = validate_all_templates(keys=template_keys)
        except Exception as exc:
            self.errors.append(f"Template validation failed: {exc}")
            return

        for result in results:
            if result.is_valid:
                continue

            details = []
            if result.file_missing:
                details.append("template file missing")
            if result.missing_tokens:
                details.append(f"missing tokens: {', '.join(result.missing_tokens)}")
            if result.missing_sections:
                details.append(f"missing sections: {', '.join(result.missing_sections)}")

            detail_msg = "; ".join(details) or "unknown template issue"
            self.errors.append(
                f"Template validation failed for {result.check.label}: {detail_msg}"
            )


def print_validation_results(is_valid: bool, errors: List[str], warnings: List[str], report_path: Path):
    """Print formatted validation results"""
    filename = report_path.name

    if is_valid and not warnings:
        print(f"✅ Report validation PASSED: {filename}")
        return

    print(f"\n{'='*70}")
    print(f"Report Validation Results: {filename}")
    print(f"{'='*70}\n")

    if errors:
        print(f"❌ ERRORS ({len(errors)}) - Report BLOCKED from submission:")
        print(f"{'-'*70}")
        for i, error in enumerate(errors, 1):
            print(f"{i}. {error}\n")

    if warnings:
        print(f"⚠️  WARNINGS ({len(warnings)}) - Report allowed but has issues:")
        print(f"{'-'*70}")
        for i, warning in enumerate(warnings, 1):
            print(f"{i}. {warning}\n")

    print(f"{'='*70}\n")

    if not is_valid:
        print("❌ Report CANNOT be submitted. Fix errors above.")
        print("\nQuick Fixes:")
        print("- Add missing fields to metadata section")
        print("- Ensure Open Items section exists (even if 'None')")
        print("- Check status is valid: success, blocked, fail, in_progress")
    else:
        print("✅ Report CAN be submitted (warnings are advisory only)")


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 validate_report.py path/to/report.md")
        sys.exit(1)

    report_path = Path(sys.argv[1])

    if not report_path.exists():
        print(f"❌ Report file not found: {report_path}")
        sys.exit(1)

    validator = ReportValidator(report_path)
    is_valid, errors, warnings = validator.validate()

    print_validation_results(is_valid, errors, warnings, report_path)

    # Exit codes:
    # 0 = valid
    # 1 = errors (blocks submission)
    # 2 = warnings only (allows submission)
    if not is_valid:
        sys.exit(1)
    elif warnings:
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
