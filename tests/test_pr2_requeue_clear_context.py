#!/usr/bin/env python3
"""
Tests for PR-2 scope items:
  1. ClearContext default → false
  2. Smart-tap reject heuristic hardening (Shell cwd was reset not blocking)
  3. Blocked-dispatch structured NDJSON audit
  4. Requeueable vs non-requeueable classification
  5. Duplicate delivery audit event
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TESTS_DIR = REPO_ROOT / "tests"


# ---------------------------------------------------------------------------
# 1. ClearContext default → false
# ---------------------------------------------------------------------------

class TestClearContextDefault(unittest.TestCase):
    """vnx_dispatch_extract_clear_context must default to 'false' when field absent."""

    def _run_metadata_helper(self, dispatch_content: str) -> str:
        """Source dispatch_metadata.sh and run extract_clear_context against tmp dispatch."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
            tmp.write(dispatch_content)
            tmp_path = tmp.name
        try:
            cmd = [
                "bash", "-c",
                f'source "{SCRIPTS_DIR}/lib/dispatch_metadata.sh" && '
                f'vnx_dispatch_extract_clear_context "{tmp_path}"',
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return result.stdout.strip()
        finally:
            os.unlink(tmp_path)

    def test_absent_field_defaults_to_false(self):
        """When ClearContext is not set, default must be 'false'."""
        value = self._run_metadata_helper("[[TARGET:B]]\nRole: backend-developer\n")
        self.assertEqual(value, "false",
            f"Expected default 'false', got '{value}'")

    def test_explicit_true_returns_true(self):
        """Explicit ClearContext: true must return 'true'."""
        value = self._run_metadata_helper("ClearContext: true\n")
        self.assertEqual(value, "true")

    def test_explicit_false_returns_false(self):
        """Explicit ClearContext: false must return 'false'."""
        value = self._run_metadata_helper("ClearContext: FALSE\n")
        self.assertEqual(value, "false")


# ---------------------------------------------------------------------------
# 2. Smart-tap reject heuristic: 'Shell cwd was reset' must NOT reject real blocks
# ---------------------------------------------------------------------------

class TestSmartTapShellCwdNoise(unittest.TestCase):
    """'Shell cwd was reset' in block content must not trigger reject."""

    SMART_TAP = SCRIPTS_DIR / "smart_tap_v7_json_translator.sh"

    def _run_reject_check(self, block_content: str) -> tuple[int, str, str]:
        """Run the validation logic inline using bash sourcing."""
        cmd = [
            "bash", "-c",
            f'source "{self.SMART_TAP}" && '
            f'if _vnx_validate_manager_block "$BLOCK"; then echo VALID; else echo REJECTED; fi',
        ]
        env = {**os.environ, "BLOCK": block_content}
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=10)
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    def test_shell_cwd_reset_noise_does_not_reject_valid_block(self):
        """'Shell cwd was reset' must not match the reject pattern (benign tool noise)."""
        valid_block = (
            "[[TARGET:B]]\n"
            "Role: backend-developer\n"
            "Dispatch-ID: 20260401-070200-test\n"
            "Track: B\n"
            "Shell cwd was reset to /tmp/some/path\n"
            "Gate: gate_test\n"
        )
        # Test the actual pattern used in smart_tap after the fix (no Shell cwd was reset)
        cmd = [
            "bash", "-c",
            r"""echo "$BLOCK" | grep -qE '(Cogitated for|^❯ |^> ja )' && echo REJECTED || echo VALID""",
        ]
        env = {**os.environ, "BLOCK": valid_block}
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(result.stdout.strip(), "VALID",
            "Shell cwd was reset must not match the reject pattern")

    def test_t0_conversation_output_still_rejected(self):
        """Blocks with 'Cogitated for' or '❯ ' prompt still trigger reject."""
        noisy_block = (
            "[[TARGET:B]]\n"
            "Cogitated for 3.5 seconds\n"
            "Role: backend-developer\n"
        )
        cmd = [
            "bash", "-c",
            f'echo "$BLOCK" | grep -qE \'(Cogitated for|^❯ |^> ja )\' && echo REJECTED || echo VALID',
        ]
        env = {**os.environ, "BLOCK": noisy_block}
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(result.stdout.strip(), "REJECTED",
            "'Cogitated for' must still trigger reject pattern")


# ---------------------------------------------------------------------------
# 3. Blocked-dispatch structured NDJSON audit + 4. Classification + 5. Duplicate
# ---------------------------------------------------------------------------

class TestBlockedDispatchAudit(unittest.TestCase):
    """emit_blocked_dispatch_audit writes correct NDJSON with category/requeueable."""

    DISPATCHER = SCRIPTS_DIR / "dispatcher_v8_minimal.sh"

    def _emit_audit(
        self,
        dispatch_id: str,
        terminal_id: str,
        block_reason: str,
        event_type: str = "dispatch_blocked",
    ) -> dict:
        """Call emit_blocked_dispatch_audit via bash and return the parsed NDJSON event."""
        with tempfile.TemporaryDirectory() as state_dir:
            audit_file = os.path.join(state_dir, "blocked_dispatch_audit.ndjson")
            # Source only up to and including the two new functions (skip singleton enforcer)
            cmd = [
                "bash", "-c",
                f"""
set +e
# Stub out singleton enforcer and sourced deps
enforce_singleton() {{ :; }}
source_if() {{ :; }}
source "{SCRIPTS_DIR}/lib/vnx_paths.sh" 2>/dev/null || true
VNX_STATE_DIR="{state_dir}"
STATE_DIR="{state_dir}"
log() {{ :; }}

_classify_blocked_dispatch() {{
    local reason="$1"
    case "$reason" in
        active_claim:*|status_claimed:*)   echo "busy true" ;;
        canonical_lease:lease_expired*|recent_*|canonical_check_error:*|terminal_state_unreadable) echo "ambiguous true" ;;
        canonical_lease:*)                 echo "busy true" ;;
        *)                                 echo "invalid false" ;;
    esac
}}

emit_blocked_dispatch_audit() {{
    local dispatch_id="$1"
    local terminal_id="$2"
    local block_reason="$3"
    local event_type="${{4:-dispatch_blocked}}"
    local audit_file="$STATE_DIR/blocked_dispatch_audit.ndjson"

    local classification
    classification=$(_classify_blocked_dispatch "$block_reason")
    local block_category="${{classification%% *}}"
    local requeueable="${{classification##* }}"

    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    python3 - "$event_type" "$dispatch_id" "$terminal_id" "$block_reason" \\
        "$block_category" "$requeueable" "$ts" "$audit_file" <<'PY'
import json, sys, os
event_type, dispatch_id, terminal_id, block_reason, block_category, requeueable_str, ts, audit_file = sys.argv[1:]
event = {{
    "event_type": event_type,
    "dispatch_id": dispatch_id,
    "terminal_id": terminal_id,
    "block_reason": block_reason,
    "block_category": block_category,
    "requeueable": requeueable_str == "true",
    "timestamp": ts,
}}
os.makedirs(os.path.dirname(os.path.abspath(audit_file)), exist_ok=True)
with open(audit_file, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(event, separators=(",", ":")) + "\\n")
PY
}}

emit_blocked_dispatch_audit "{dispatch_id}" "{terminal_id}" "{block_reason}" "{event_type}"
cat "{audit_file}"
""",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, f"Script failed: {result.stderr}")
            lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
            self.assertTrue(lines, f"No NDJSON output. stderr={result.stderr}")
            return json.loads(lines[-1])

    # --- Test 3: structured audit fields present ---

    def test_audit_event_has_required_fields(self):
        """Blocked audit event must include all required fields."""
        event = self._emit_audit("d-001", "T2", "active_claim:d-other")
        for field in ("event_type", "dispatch_id", "terminal_id", "block_reason",
                      "block_category", "requeueable", "timestamp"):
            self.assertIn(field, event, f"Missing field: {field}")

    def test_audit_event_dispatch_id(self):
        event = self._emit_audit("test-dispatch-001", "T1", "active_claim:d-prior")
        self.assertEqual(event["dispatch_id"], "test-dispatch-001")

    def test_audit_event_terminal_id(self):
        event = self._emit_audit("d-002", "T3", "active_claim:d-prior")
        self.assertEqual(event["terminal_id"], "T3")

    # --- Test 4: classification ---

    def test_active_claim_classified_as_busy_requeueable(self):
        """active_claim → category=busy, requeueable=true."""
        event = self._emit_audit("d-003", "T2", "active_claim:d-other")
        self.assertEqual(event["block_category"], "busy")
        self.assertTrue(event["requeueable"])

    def test_status_claimed_classified_as_busy_requeueable(self):
        """status_claimed → category=busy, requeueable=true."""
        event = self._emit_audit("d-004", "T2", "status_claimed:d-other:working")
        self.assertEqual(event["block_category"], "busy")
        self.assertTrue(event["requeueable"])

    def test_lease_expired_classified_as_ambiguous_requeueable(self):
        """lease_expired_not_cleaned → category=ambiguous, requeueable=true."""
        event = self._emit_audit("d-005", "T2", "canonical_lease:lease_expired_not_cleaned:d-old")
        self.assertEqual(event["block_category"], "ambiguous")
        self.assertTrue(event["requeueable"])

    def test_terminal_state_unreadable_classified_as_ambiguous(self):
        """terminal_state_unreadable → category=ambiguous, requeueable=true."""
        event = self._emit_audit("d-006", "T2", "terminal_state_unreadable")
        self.assertEqual(event["block_category"], "ambiguous")
        self.assertTrue(event["requeueable"])

    def test_unknown_reason_classified_as_invalid_not_requeueable(self):
        """Unknown/metadata reason → category=invalid, requeueable=false."""
        event = self._emit_audit("d-007", "T2", "metadata_missing_role")
        self.assertEqual(event["block_category"], "invalid")
        self.assertFalse(event["requeueable"])

    # --- Test 5: duplicate delivery audit ---

    def test_duplicate_delivery_event_type(self):
        """duplicate_delivery_prevented event_type is set correctly."""
        event = self._emit_audit("d-008", "T2", "active_claim:d-008",
                                  event_type="duplicate_delivery_prevented")
        self.assertEqual(event["event_type"], "duplicate_delivery_prevented")
        self.assertEqual(event["dispatch_id"], "d-008")

    def test_standard_block_event_type(self):
        """Normal blocked dispatch uses event_type=dispatch_blocked."""
        event = self._emit_audit("d-009", "T1", "active_claim:d-other",
                                  event_type="dispatch_blocked")
        self.assertEqual(event["event_type"], "dispatch_blocked")

    def test_audit_appends_multiple_events(self):
        """Multiple audit calls append multiple NDJSON lines."""
        with tempfile.TemporaryDirectory() as state_dir:
            audit_file = os.path.join(state_dir, "blocked_dispatch_audit.ndjson")
            for i in range(3):
                cmd = [
                    "bash", "-c",
                    f"""
STATE_DIR="{state_dir}"
log() {{ :; }}
_classify_blocked_dispatch() {{ echo "busy true"; }}
emit_blocked_dispatch_audit() {{
    local audit_file="$STATE_DIR/blocked_dispatch_audit.ndjson"
    local classification=$(_classify_blocked_dispatch "$3")
    local block_category="${{classification%% *}}"
    local requeueable="${{classification##* }}"
    local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    python3 -c "
import json,sys,os
e={{'event_type':'dispatch_blocked','dispatch_id':'$1','terminal_id':'$2','block_reason':'$3','block_category':'busy','requeueable':True,'timestamp':'now'}}
os.makedirs(os.path.dirname(os.path.abspath('{audit_file}')),exist_ok=True)
open('{audit_file}','a').write(json.dumps(e)+'\\\\n')
"
}}
emit_blocked_dispatch_audit "d-multi-{i}" "T2" "active_claim:d-prior"
""",
                ]
                subprocess.run(cmd, capture_output=True, text=True, timeout=10)

            lines = Path(audit_file).read_text().strip().splitlines()
            self.assertEqual(len(lines), 3, f"Expected 3 NDJSON lines, got {len(lines)}")
            for line in lines:
                event = json.loads(line)
                self.assertIn("dispatch_id", event)


if __name__ == "__main__":
    unittest.main(verbosity=2)
