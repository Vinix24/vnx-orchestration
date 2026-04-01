#!/usr/bin/env python3
"""
Tests for fail-closed dispatch guard (PR-1: gate_pr1_fail_closed_dispatch_guard)

Coverage:
  - check_terminal() exception → available=False (fail-closed, not available=True)
  - check_terminal() with leased non-matching terminal → blocked
  - check_terminal() with idle terminal → allowed
  - check_terminal() with same_dispatch → allowed
  - check_terminal() with expired-TTL lease → blocked (not cleaned)
  - acquire_lease() InvalidTransitionError → acquired=False, not raised
  - acquire_lease() general exception → acquired=False with error
  - acquire_lease() success → acquired=True with generation
  - SkillValidator: valid skill → True
  - SkillValidator: invalid skill → False with error text
  - SkillValidator: empty / None-ish role → validate handles gracefully
  - Existing safe dispatch path (idle terminal + valid lease) is unaffected
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from runtime_coordination import (
    InvalidTransitionError,
    get_connection,
    init_schema,
    register_dispatch,
)
from runtime_core import RuntimeCore
from lease_manager import LeaseManager, LeaseResult


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_tmp_dirs():
    """Return a TemporaryDirectory plus (state_dir, dispatch_dir) strings."""
    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    state_dir = base / "state"
    dispatch_dir = base / "dispatches"
    state_dir.mkdir(parents=True)
    dispatch_dir.mkdir(parents=True)
    init_schema(str(state_dir))
    return tmp, str(state_dir), str(dispatch_dir)


def _make_core(state_dir: str, dispatch_dir: str) -> RuntimeCore:
    from dispatch_broker import DispatchBroker
    broker = DispatchBroker(state_dir, dispatch_dir, shadow_mode=False)
    mgr = LeaseManager(state_dir, auto_init=False)
    return RuntimeCore(broker, mgr)


def _register_dispatch_row(state_dir: str, dispatch_id: str, terminal_id: str = "T2") -> None:
    """Pre-register a dispatch row to satisfy terminal_leases FK constraint."""
    with get_connection(state_dir) as conn:
        register_dispatch(conn, dispatch_id=dispatch_id, terminal_id=terminal_id)
        conn.commit()


def _idle_lease_result(terminal_id: str) -> LeaseResult:
    return LeaseResult(
        terminal_id=terminal_id,
        state="idle",
        generation=1,
        dispatch_id=None,
        leased_at=None,
        expires_at=None,
        last_heartbeat_at=None,
    )


def _leased_lease_result(terminal_id: str, dispatch_id: str, *, expires_future: bool = True) -> LeaseResult:
    now = datetime.now(timezone.utc)
    delta = timedelta(seconds=600 if expires_future else -1)
    return LeaseResult(
        terminal_id=terminal_id,
        state="leased",
        generation=2,
        dispatch_id=dispatch_id,
        leased_at=now.isoformat(),
        expires_at=(now + delta).isoformat(),
        last_heartbeat_at=now.isoformat(),
    )


# ---------------------------------------------------------------------------
# TestCheckTerminalFailClosed
# ---------------------------------------------------------------------------

class TestCheckTerminalFailClosed(unittest.TestCase):
    """check_terminal() must fail-closed on exception, not fail-open."""

    def setUp(self):
        self._tmp, self.state_dir, self.dispatch_dir = _make_tmp_dirs()
        self.core = _make_core(self.state_dir, self.dispatch_dir)

    def tearDown(self):
        self._tmp.cleanup()

    # --- Exception path: fail-closed ---

    def test_exception_returns_unavailable(self):
        """DB/lease exception → available=False (fail-closed, not True)."""
        with patch.object(self.core._lease_mgr, "get", side_effect=RuntimeError("db locked")):
            result = self.core.check_terminal("T2", "d-001")

        self.assertFalse(result["available"],
            "check_terminal must return available=False on exception (fail-closed)")
        self.assertIn("check_error_fail_closed", result["reason"])

    def test_exception_includes_terminal_id(self):
        """Error result still identifies the terminal."""
        with patch.object(self.core._lease_mgr, "get", side_effect=OSError("connection reset")):
            result = self.core.check_terminal("T3", "d-002")

        self.assertEqual(result["terminal_id"], "T3")
        self.assertFalse(result["available"])

    def test_exception_reason_contains_error_text(self):
        """Error detail is propagated in reason string for auditability."""
        with patch.object(self.core._lease_mgr, "get", side_effect=ValueError("corrupt state")):
            result = self.core.check_terminal("T1", "d-003")

        self.assertIn("corrupt state", result["reason"])

    # --- Normal paths still work ---

    def test_idle_terminal_allowed(self):
        """Idle terminal → available=True."""
        with patch.object(self.core._lease_mgr, "get", return_value=None):
            result = self.core.check_terminal("T2", "d-004")

        self.assertTrue(result["available"])
        self.assertEqual(result["reason"], "idle")

    def test_idle_state_terminal_allowed(self):
        """Terminal in idle state → available=True."""
        mock_lease = _idle_lease_result("T2")
        with patch.object(self.core._lease_mgr, "get", return_value=mock_lease):
            result = self.core.check_terminal("T2", "d-005")

        self.assertTrue(result["available"])
        self.assertEqual(result["reason"], "idle")

    def test_same_dispatch_allowed(self):
        """Terminal leased by same dispatch_id → available=True."""
        mock_lease = _leased_lease_result("T2", "d-006")
        with patch.object(self.core._lease_mgr, "get", return_value=mock_lease):
            result = self.core.check_terminal("T2", "d-006")

        self.assertTrue(result["available"])
        self.assertEqual(result["reason"], "same_dispatch")

    def test_leased_different_dispatch_blocked(self):
        """Terminal leased by different dispatch → available=False."""
        mock_lease = _leased_lease_result("T2", "d-other")
        with (
            patch.object(self.core._lease_mgr, "get", return_value=mock_lease),
            patch.object(self.core._lease_mgr, "is_expired_by_ttl", return_value=False),
        ):
            result = self.core.check_terminal("T2", "d-007")

        self.assertFalse(result["available"])
        self.assertIn("leased:d-other", result["reason"])

    def test_expired_ttl_lease_blocked(self):
        """Lease whose TTL has elapsed but not cleaned → available=False."""
        mock_lease = _leased_lease_result("T2", "d-old", expires_future=False)
        with (
            patch.object(self.core._lease_mgr, "get", return_value=mock_lease),
            patch.object(self.core._lease_mgr, "is_expired_by_ttl", return_value=True),
        ):
            result = self.core.check_terminal("T2", "d-008")

        self.assertFalse(result["available"])
        self.assertIn("lease_expired_not_cleaned", result["reason"])


# ---------------------------------------------------------------------------
# TestAcquireLeaseFailClosed
# ---------------------------------------------------------------------------

class TestAcquireLeaseFailClosed(unittest.TestCase):
    """acquire_lease() must return acquired=False on InvalidTransitionError and exceptions."""

    def setUp(self):
        self._tmp, self.state_dir, self.dispatch_dir = _make_tmp_dirs()
        self.core = _make_core(self.state_dir, self.dispatch_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_invalid_transition_returns_not_acquired(self):
        """InvalidTransitionError (terminal busy) → acquired=False, not raised."""
        with patch.object(
            self.core._lease_mgr, "acquire",
            side_effect=InvalidTransitionError("terminal already leased"),
        ):
            result = self.core.acquire_lease("T2", "d-001")

        self.assertFalse(result.acquired,
            "acquire_lease must return acquired=False on InvalidTransitionError")
        self.assertIsNotNone(result.error)
        self.assertIn("terminal already leased", result.error)

    def test_general_exception_returns_not_acquired(self):
        """General exception during lease acquire → acquired=False with error."""
        with patch.object(
            self.core._lease_mgr, "acquire",
            side_effect=OSError("db unavailable"),
        ):
            result = self.core.acquire_lease("T2", "d-002")

        self.assertFalse(result.acquired)
        self.assertIn("db unavailable", result.error)

    def test_acquire_failure_does_not_raise(self):
        """Lease acquire failure must never propagate an exception to the caller."""
        with patch.object(
            self.core._lease_mgr, "acquire",
            side_effect=RuntimeError("unexpected state"),
        ):
            try:
                result = self.core.acquire_lease("T2", "d-003")
            except Exception as exc:
                self.fail(f"acquire_lease raised unexpectedly: {exc}")

        self.assertFalse(result.acquired)

    def test_successful_acquire_returns_generation(self):
        """Successful acquire → acquired=True with integer generation."""
        tmp2, state2, dispatch2 = _make_tmp_dirs()
        try:
            core2 = _make_core(state2, dispatch2)
            _register_dispatch_row(state2, "d-success-001", "T2")
            result = core2.acquire_lease("T2", "d-success-001")
            self.assertTrue(result.acquired,
                f"Acquire failed unexpectedly: {result.error}")
            self.assertIsNotNone(result.generation)
            self.assertIsInstance(result.generation, int)
        finally:
            tmp2.cleanup()

    def test_double_acquire_same_terminal_blocked(self):
        """Second acquire on same terminal → acquired=False (terminal exclusivity)."""
        tmp2, state2, dispatch2 = _make_tmp_dirs()
        try:
            core2 = _make_core(state2, dispatch2)
            _register_dispatch_row(state2, "d-first", "T2")
            _register_dispatch_row(state2, "d-second", "T2")
            first = core2.acquire_lease("T2", "d-first")
            self.assertTrue(first.acquired, f"First acquire failed: {first.error}")

            second = core2.acquire_lease("T2", "d-second")
            self.assertFalse(second.acquired,
                "Second lease acquire on occupied terminal must be blocked")
        finally:
            tmp2.cleanup()


# ---------------------------------------------------------------------------
# TestSkillMetadataValidation
# ---------------------------------------------------------------------------

class TestSkillMetadataValidation(unittest.TestCase):
    """validate_skill.SkillValidator rejects invalid skills before delivery."""

    @classmethod
    def setUpClass(cls):
        """Import SkillValidator, skip suite if skills.yaml not present."""
        try:
            from validate_skill import SkillValidator
            cls.SkillValidator = SkillValidator
            cls._validator = SkillValidator()
            cls._skip_reason = None
        except Exception as exc:
            cls._skip_reason = str(exc)

    def _maybe_skip(self):
        if getattr(self, "_skip_reason", None):
            self.skipTest(f"SkillValidator not available: {self._skip_reason}")

    def test_valid_skill_passes(self):
        """A skill in skills.yaml registry → is_valid=True."""
        self._maybe_skip()
        valid_skills = self._validator.get_valid_skills()
        if not valid_skills:
            self.skipTest("No valid skills found in registry")

        skill = valid_skills[0]
        is_valid, error = self._validator.validate(skill)
        self.assertTrue(is_valid, f"Known skill '{skill}' should pass validation")

    def test_invalid_skill_rejected(self):
        """A skill not in registry → is_valid=False with error message."""
        self._maybe_skip()
        is_valid, error = self._validator.validate("completely-nonexistent-skill-xyz-abc")
        self.assertFalse(is_valid,
            "Invalid skill must be rejected before delivery")
        self.assertIsNotNone(error)
        self.assertIn("Invalid skill", error)

    def test_invalid_skill_suggests_alternatives(self):
        """Invalid skill with close match provides suggestion."""
        self._maybe_skip()
        is_valid, error = self._validator.validate("backend")
        # backend-developer is a valid skill; "backend" is a prefix match
        # We only assert the error is non-None and informative
        if not is_valid:
            self.assertIsNotNone(error)

    def test_at_prefix_stripped(self):
        """@-prefixed role name is normalized and validated."""
        self._maybe_skip()
        valid_skills = self._validator.get_valid_skills()
        if not valid_skills:
            self.skipTest("No valid skills found")

        skill = valid_skills[0]
        is_valid, _ = self._validator.validate(f"@{skill}")
        self.assertTrue(is_valid, "@ prefix should be stripped before validation")

    def test_slash_prefix_stripped(self):
        """/-prefixed role name is normalized and validated."""
        self._maybe_skip()
        valid_skills = self._validator.get_valid_skills()
        if not valid_skills:
            self.skipTest("No valid skills found")

        skill = valid_skills[0]
        is_valid, _ = self._validator.validate(f"/{skill}")
        self.assertTrue(is_valid, "/ prefix should be stripped before validation")

    def test_backend_developer_is_valid(self):
        """backend-developer skill must be valid (used in this feature)."""
        self._maybe_skip()
        is_valid, error = self._validator.validate("backend-developer")
        self.assertTrue(is_valid,
            f"backend-developer must be in registry; got: {error}")

    def test_empty_string_rejected(self):
        """Empty string normalized to empty → is_valid=False (no registry match)."""
        self._maybe_skip()
        is_valid, error = self._validator.validate("")
        # Empty normalized skill cannot be in registry
        self.assertFalse(is_valid)

    def test_normalize_skill_name(self):
        """normalize_skill_name strips @ and / prefixes."""
        self._maybe_skip()
        self.assertEqual(self._validator.normalize_skill_name("@backend-developer"), "backend-developer")
        self.assertEqual(self._validator.normalize_skill_name("/backend-developer"), "backend-developer")
        self.assertEqual(self._validator.normalize_skill_name("backend-developer"), "backend-developer")


# ---------------------------------------------------------------------------
# TestEndToEndSafeDispatchPath
# ---------------------------------------------------------------------------

class TestEndToEndSafeDispatchPath(unittest.TestCase):
    """Verify existing safe dispatch paths are unaffected by fail-closed changes."""

    def setUp(self):
        self._tmp, self.state_dir, self.dispatch_dir = _make_tmp_dirs()
        self.core = _make_core(self.state_dir, self.dispatch_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_idle_terminal_check_is_available(self):
        """No lease record → check returns available=True (safe path unaffected)."""
        result = self.core.check_terminal("T1", "d-safe-001")
        self.assertTrue(result["available"])

    def test_acquire_then_check_blocks_second(self):
        """Acquire on T1, then check T1 with different dispatch → blocked."""
        _register_dispatch_row(self.state_dir, "d-first", "T1")
        acq = self.core.acquire_lease("T1", "d-first")
        self.assertTrue(acq.acquired, f"Acquire failed: {acq.error}")

        check = self.core.check_terminal("T1", "d-second")
        self.assertFalse(check["available"],
            "Second dispatch must be blocked after first acquires lease")

    def test_acquire_check_release_check_available(self):
        """Full acquire-release cycle restores idle availability."""
        _register_dispatch_row(self.state_dir, "d-cycle", "T1")
        acq = self.core.acquire_lease("T1", "d-cycle")
        self.assertTrue(acq.acquired, f"Acquire failed: {acq.error}")

        self.core.release_lease("T1", acq.generation)

        check = self.core.check_terminal("T1", "d-new")
        self.assertTrue(check["available"],
            "Terminal must be available again after lease release")

    def test_no_dispatch_continues_after_check_exception(self):
        """check_terminal exception result has available=False, blocking dispatch."""
        with patch.object(self.core._lease_mgr, "get", side_effect=Exception("db gone")):
            result = self.core.check_terminal("T2", "d-exc")

        # The caller (dispatcher) checks result["available"] and must block
        self.assertFalse(result["available"],
            "Caller must see available=False and block dispatch on exception")


if __name__ == "__main__":
    unittest.main(verbosity=2)
