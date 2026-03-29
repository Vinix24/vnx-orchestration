#!/usr/bin/env python3
"""Tests for CLI-agnostic trace token validation (PR-3).

Covers: preferred format, legacy formats, enforcement modes,
injection, CI batch validation, and edge cases.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
VALIDATOR = VNX_ROOT / "scripts" / "lib" / "trace_token_validator.py"

sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
from trace_token_validator import (
    EnforcementMode,
    Severity,
    TokenFormat,
    TraceTokens,
    extract_trace_tokens,
    inject_trace_token,
    validate_commits,
    validate_dispatch_id_format,
    validate_trace_token,
)


# ── extract_trace_tokens ─────────────────────────────────────────────

class TestExtractTraceTokens:
    def test_preferred_format(self):
        msg = "feat(scope): add feature\n\nSome body.\n\nDispatch-ID: 20260329-180606-slug-C\n"
        tokens = extract_trace_tokens(msg)
        assert tokens.preferred == "20260329-180606-slug-C"
        assert tokens.has_preferred
        assert tokens.primary_format == TokenFormat.PREFERRED

    def test_legacy_dispatch_inline(self):
        msg = "fix: something dispatch:20260329-180606-slug-B in body"
        tokens = extract_trace_tokens(msg)
        assert tokens.legacy_dispatch == "20260329-180606-slug-B"
        assert not tokens.has_preferred
        assert tokens.has_any_legacy

    def test_legacy_pr_reference(self):
        msg = "feat(governance): PR-3 add traceability"
        tokens = extract_trace_tokens(msg)
        assert tokens.legacy_pr == ["3"]
        assert tokens.primary_format == TokenFormat.LEGACY_PR
        assert tokens.primary_id == "PR-3"

    def test_legacy_fp_reference(self):
        msg = "fix(scope): close FP-D gaps"
        tokens = extract_trace_tokens(msg)
        assert tokens.legacy_fp == ["D"]
        assert tokens.primary_format == TokenFormat.LEGACY_FP
        assert tokens.primary_id == "FP-D"

    def test_multiple_legacy_pr_refs(self):
        msg = "feat: PR-1 and PR-2 combined work"
        tokens = extract_trace_tokens(msg)
        assert tokens.legacy_pr == ["1", "2"]

    def test_no_tokens(self):
        msg = "chore: update readme\n\nJust a simple change."
        tokens = extract_trace_tokens(msg)
        assert not tokens.has_preferred
        assert not tokens.has_any_legacy
        assert not tokens.has_any
        assert tokens.primary_format is None
        assert tokens.primary_id is None

    def test_preferred_plus_legacy(self):
        msg = "feat(governance): PR-3 traceability\n\nDispatch-ID: 20260329-180606-slug-C\n"
        tokens = extract_trace_tokens(msg)
        assert tokens.has_preferred
        assert tokens.has_any_legacy  # PR-3 in subject
        assert tokens.primary_format == TokenFormat.PREFERRED  # Preferred takes priority

    def test_co_authored_by_not_matched(self):
        """Co-Authored-By lines should not be matched as trace tokens."""
        msg = "feat: something\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
        tokens = extract_trace_tokens(msg)
        assert not tokens.has_any


# ── validate_dispatch_id_format ──────────────────────────────────────

class TestDispatchIdFormat:
    def test_valid_format(self):
        assert validate_dispatch_id_format("20260329-180606-slug-C")
        assert validate_dispatch_id_format("20260101-000000-my-long-slug-here-B")

    def test_invalid_format(self):
        assert not validate_dispatch_id_format("not-a-dispatch-id")
        assert not validate_dispatch_id_format("20260329-slug-C")  # missing time
        assert not validate_dispatch_id_format("")


# ── validate_trace_token ─────────────────────────────────────────────

class TestValidateTraceToken:
    def test_preferred_token_valid(self):
        msg = "feat: add feature\n\nDispatch-ID: 20260329-180606-slug-C\n"
        result = validate_trace_token(msg, EnforcementMode.SHADOW)
        assert result.valid
        assert result.format == TokenFormat.PREFERRED
        assert result.dispatch_id == "20260329-180606-slug-C"
        assert result.severity == Severity.INFO

    def test_legacy_accepted_in_shadow(self):
        msg = "feat(governance): PR-3 traceability"
        result = validate_trace_token(msg, EnforcementMode.SHADOW, legacy_accepted=True)
        assert result.valid
        assert result.format == TokenFormat.LEGACY_PR
        assert result.severity == Severity.WARNING

    def test_legacy_rejected_when_disabled(self):
        msg = "feat(governance): PR-3 traceability"
        result = validate_trace_token(msg, EnforcementMode.SHADOW, legacy_accepted=False)
        assert not result.valid

    def test_missing_token_shadow(self):
        msg = "chore: update readme"
        result = validate_trace_token(msg, EnforcementMode.SHADOW)
        assert not result.valid
        assert result.severity == Severity.WARNING

    def test_missing_token_enforced(self):
        msg = "chore: update readme"
        result = validate_trace_token(msg, EnforcementMode.ENFORCED)
        assert not result.valid
        assert result.severity == Severity.ERROR

    def test_malformed_dispatch_id_warns(self):
        msg = "feat: add\n\nDispatch-ID: not-valid-format\n"
        result = validate_trace_token(msg, EnforcementMode.SHADOW)
        assert result.valid  # Still valid — token is present
        assert len(result.warnings) > 0  # But warns about format

    def test_gap_event_generation(self):
        msg = "chore: no token"
        result = validate_trace_token(msg, EnforcementMode.SHADOW)
        event = result.to_gap_event("abc123", "hook")
        assert event["event_type"] == "provenance_gap"
        assert event["entity_type"] == "commit"
        assert event["metadata_json"]["gap_type"] == "missing_trace_token"
        assert event["metadata_json"]["enforcement_mode"] == "shadow"


# ── inject_trace_token ───────────────────────────────────────────────

class TestInjectTraceToken:
    def test_inject_into_empty_message(self):
        result = inject_trace_token("", "20260329-180606-slug-C")
        assert "Dispatch-ID: 20260329-180606-slug-C" in result

    def test_inject_into_message_with_body(self):
        msg = "feat: add feature\n\nSome description here."
        result = inject_trace_token(msg, "20260329-180606-slug-C")
        assert result.startswith("feat: add feature")
        assert "Dispatch-ID: 20260329-180606-slug-C" in result

    def test_no_duplicate_injection(self):
        msg = "feat: add feature\n\nDispatch-ID: 20260329-180606-slug-C\n"
        result = inject_trace_token(msg, "20260329-180606-other-B")
        assert result.count("Dispatch-ID:") == 1
        # Should keep the existing one
        assert "20260329-180606-slug-C" in result

    def test_preserves_existing_content(self):
        msg = "feat: add feature\n\nBody line 1.\nBody line 2."
        result = inject_trace_token(msg, "20260329-180606-slug-C")
        assert "Body line 1." in result
        assert "Body line 2." in result


# ── validate_commits (CI batch) ──────────────────────────────────────

class TestValidateCommits:
    def test_all_valid(self):
        messages = [
            "feat: one\n\nDispatch-ID: 20260329-180606-one-A\n",
            "fix: two\n\nDispatch-ID: 20260329-180606-two-B\n",
        ]
        summary = validate_commits(messages, EnforcementMode.SHADOW)
        assert summary["all_valid"]
        assert summary["valid"] == 2
        assert summary["invalid"] == 0

    def test_mixed_valid_and_invalid(self):
        messages = [
            "feat: one\n\nDispatch-ID: 20260329-180606-one-A\n",
            "chore: no token",
        ]
        summary = validate_commits(messages, EnforcementMode.SHADOW)
        assert not summary["all_valid"]
        assert summary["valid"] == 1
        assert summary["invalid"] == 1

    def test_legacy_counted(self):
        messages = [
            "feat(governance): PR-3 traceability",
        ]
        summary = validate_commits(messages, EnforcementMode.SHADOW, legacy_accepted=True)
        assert summary["all_valid"]
        assert summary["legacy"] == 1

    def test_empty_list(self):
        summary = validate_commits([], EnforcementMode.SHADOW)
        assert summary["all_valid"]
        assert summary["total"] == 0


# ── CLI entry point ──────────────────────────────────────────────────

class TestCLI:
    def test_validate_from_stdin(self):
        msg = "feat: add\n\nDispatch-ID: 20260329-180606-slug-C\n"
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), "validate", "-"],
            input=msg,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        result = json.loads(proc.stdout)
        assert result["valid"] is True

    def test_validate_invalid_from_stdin(self):
        msg = "chore: no token"
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), "validate", "-"],
            input=msg,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1
        result = json.loads(proc.stdout)
        assert result["valid"] is False

    def test_inject_creates_token(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("feat: add feature\n\nSome body.")
            f.flush()
            env = os.environ.copy()
            env["VNX_CURRENT_DISPATCH_ID"] = "20260329-180606-slug-C"
            proc = subprocess.run(
                [sys.executable, str(VALIDATOR), "inject", f.name],
                capture_output=True,
                text=True,
                env=env,
            )
            assert proc.returncode == 0
            content = Path(f.name).read_text()
            assert "Dispatch-ID: 20260329-180606-slug-C" in content
            os.unlink(f.name)

    def test_inject_noop_without_env(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("feat: add feature")
            f.flush()
            env = os.environ.copy()
            env.pop("VNX_CURRENT_DISPATCH_ID", None)
            proc = subprocess.run(
                [sys.executable, str(VALIDATOR), "inject", f.name],
                capture_output=True,
                text=True,
                env=env,
            )
            assert proc.returncode == 0
            content = Path(f.name).read_text()
            assert "Dispatch-ID:" not in content
            os.unlink(f.name)

    def test_check_commits_batch(self):
        messages = "feat: one\n\nDispatch-ID: 20260329-180606-one-A\n\x00chore: no token"
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), "check-commits"],
            input=messages,
            capture_output=True,
            text=True,
        )
        # Shadow mode — exit 0 even with invalid
        assert proc.returncode == 0
        result = json.loads(proc.stdout)
        assert result["total"] == 2
        assert result["invalid"] == 1


# ── Run tests ────────────────────────────────────────────────────────

def _run_all():
    """Simple test runner for CI without pytest dependency."""
    import traceback

    passed = 0
    failed = 0
    errors = []

    for cls_name, cls in [
        (n, o) for n, o in globals().items() if n.startswith("Test") and isinstance(o, type)
    ]:
        instance = cls()
        for method_name in sorted(dir(instance)):
            if not method_name.startswith("test_"):
                continue
            test_id = f"{cls_name}.{method_name}"
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS {test_id}")
            except Exception as e:
                failed += 1
                errors.append((test_id, traceback.format_exc()))
                print(f"  FAIL {test_id}: {e}")

    print(f"\n{passed} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for test_id, tb in errors:
            print(f"\n--- {test_id} ---\n{tb}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    # Support both pytest and standalone
    sys.exit(_run_all())
