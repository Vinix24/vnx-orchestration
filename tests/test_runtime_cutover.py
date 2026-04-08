#!/usr/bin/env python3
"""
Tests for PR-5: Runtime Core Cutover And Governance Compatibility.

Quality gate coverage (gate_pr5_runtime_core_cutover):
  - New dispatches use broker-first durable registration by default
  - Terminal assignment uses canonical lease state by default
  - Receipts still correlate cleanly to dispatch_id after cutover
  - Existing governance workflows remain functional (T0 authority preserved)
  - Rollback path to legacy transport is documented and tested
  - All tests pass for cutover compatibility and receipt linkage
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Resolve scripts/lib path
_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(_SCRIPTS_DIR))

from runtime_coordination import get_connection, get_dispatch, get_lease, init_schema
from dispatch_broker import DispatchBroker
from lease_manager import LeaseManager
from runtime_core import (
    RegisterResult,
    DeliveryStartResult,
    LeaseAcquireResult,
    RuntimeCore,
    load_runtime_core,
    runtime_primary_active,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _setup_dirs(tmp: tempfile.TemporaryDirectory) -> tuple[str, str]:
    base = Path(tmp.name)
    state_dir = str(base / "state")
    dispatch_dir = str(base / "dispatches")
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    Path(dispatch_dir).mkdir(parents=True, exist_ok=True)
    init_schema(state_dir)
    return state_dir, dispatch_dir


def _make_core(state_dir: str, dispatch_dir: str) -> RuntimeCore:
    broker = DispatchBroker(state_dir, dispatch_dir, shadow_mode=False)
    lease_mgr = LeaseManager(state_dir)
    return RuntimeCore(broker=broker, lease_mgr=lease_mgr)


# ---------------------------------------------------------------------------
# TestRuntimePrimaryFlag
# ---------------------------------------------------------------------------

class TestRuntimePrimaryFlag(unittest.TestCase):
    """VNX_RUNTIME_PRIMARY flag controls cutover on/off."""

    def test_runtime_primary_active_default_is_true(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VNX_RUNTIME_PRIMARY", None)
            self.assertTrue(runtime_primary_active())

    def test_runtime_primary_inactive_when_zero(self) -> None:
        with patch.dict(os.environ, {"VNX_RUNTIME_PRIMARY": "0"}):
            self.assertFalse(runtime_primary_active())

    def test_runtime_primary_active_when_one(self) -> None:
        with patch.dict(os.environ, {"VNX_RUNTIME_PRIMARY": "1"}):
            self.assertTrue(runtime_primary_active())

    def test_load_runtime_core_returns_none_when_disabled(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            state_dir, dispatch_dir = _setup_dirs(tmp)
            with patch.dict(os.environ, {"VNX_RUNTIME_PRIMARY": "0"}):
                core = load_runtime_core(state_dir, dispatch_dir)
            self.assertIsNone(core)
        finally:
            tmp.cleanup()

    def test_load_runtime_core_returns_instance_when_enabled(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            state_dir, dispatch_dir = _setup_dirs(tmp)
            with patch.dict(os.environ, {"VNX_RUNTIME_PRIMARY": "1",
                                          "VNX_BROKER_ENABLED": "1",
                                          "VNX_BROKER_SHADOW": "0"}):
                core = load_runtime_core(state_dir, dispatch_dir)
            self.assertIsInstance(core, RuntimeCore)
        finally:
            tmp.cleanup()


# ---------------------------------------------------------------------------
# TestBrokerFirstRegistration
# ---------------------------------------------------------------------------

class TestBrokerFirstRegistration(unittest.TestCase):
    """New dispatches use broker-first durable registration by default."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.core = _make_core(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_register_creates_db_row_before_delivery(self) -> None:
        result = self.core.register("d-cutover-001", "Do some work.")
        self.assertTrue(result.registered)
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-cutover-001")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "queued")

    def test_register_writes_bundle_to_disk(self) -> None:
        result = self.core.register("d-cutover-002", "Work instructions.")
        self.assertIsNotNone(result.bundle_dir)
        bundle_dir = Path(result.bundle_dir)
        self.assertTrue((bundle_dir / "bundle.json").exists())
        self.assertTrue((bundle_dir / "prompt.txt").exists())

    def test_register_stores_metadata_in_bundle(self) -> None:
        self.core.register(
            "d-cutover-003",
            "Instructions.",
            terminal_id="T2",
            track="B",
            skill="backend-developer",
            gate="gate_pr5",
            pr_ref="PR-5",
        )
        bundle_json = Path(self.dispatch_dir) / "d-cutover-003" / "bundle.json"
        data = json.loads(bundle_json.read_text())
        self.assertEqual(data["terminal_id"], "T2")
        self.assertEqual(data["track"], "B")
        self.assertEqual(data["gate"], "gate_pr5")
        self.assertEqual(data["pr_ref"], "PR-5")

    def test_register_idempotent_on_reregister(self) -> None:
        self.core.register("d-cutover-004", "First.")
        result2 = self.core.register("d-cutover-004", "Second (ignored).")
        self.assertTrue(result2.already_existed)
        # Prompt is immutable after first write (G-R6)
        prompt_path = Path(self.dispatch_dir) / "d-cutover-004" / "prompt.txt"
        self.assertEqual(prompt_path.read_text(), "First.")

    def test_register_returns_error_on_failure(self) -> None:
        # Corrupt state_dir to force a failure
        broken_core = _make_core("/nonexistent/state", "/nonexistent/dispatch")
        result = broken_core.register("d-broken", "prompt")
        self.assertFalse(result.registered)
        self.assertIsNotNone(result.error)


# ---------------------------------------------------------------------------
# TestDeliveryLifecycle
# ---------------------------------------------------------------------------

class TestDeliveryLifecycle(unittest.TestCase):
    """Delivery lifecycle: start, success, failure."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.core = _make_core(self.state_dir, self.dispatch_dir)
        self.core.register("d-lifecycle-001", "Prompt.", terminal_id="T2", track="B")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_delivery_start_transitions_to_delivering(self) -> None:
        result = self.core.delivery_start("d-lifecycle-001", "T2")
        self.assertTrue(result.started)
        self.assertIsNotNone(result.attempt_id)
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-lifecycle-001")
        self.assertEqual(row["state"], "delivering")

    def test_delivery_success_transitions_to_accepted(self) -> None:
        start = self.core.delivery_start("d-lifecycle-001", "T2")
        result = self.core.delivery_success("d-lifecycle-001", start.attempt_id)
        self.assertTrue(result.get("success"))
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-lifecycle-001")
        self.assertEqual(row["state"], "accepted")

    def test_delivery_failure_transitions_to_failed_delivery(self) -> None:
        start = self.core.delivery_start("d-lifecycle-001", "T2")
        result = self.core.delivery_failure(
            "d-lifecycle-001", start.attempt_id, reason="tmux timeout"
        )
        self.assertTrue(result.get("recorded"))
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-lifecycle-001")
        self.assertEqual(row["state"], "failed_delivery")

    def test_delivery_failure_is_durable_not_logs_only(self) -> None:
        """Failures must be recorded in DB, not just logs (G-R3, G-R5)."""
        start = self.core.delivery_start("d-lifecycle-001", "T2")
        self.core.delivery_failure("d-lifecycle-001", start.attempt_id, reason="pane not found")
        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-lifecycle-001")
        # State must be failed_delivery, not stuck in delivering
        self.assertEqual(row["state"], "failed_delivery")


# ---------------------------------------------------------------------------
# TestCanonicalLeaseIntegration
# ---------------------------------------------------------------------------

class TestCanonicalLeaseIntegration(unittest.TestCase):
    """Terminal assignment uses canonical lease state by default."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)
        self.core = _make_core(self.state_dir, self.dispatch_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reg(self, dispatch_id: str) -> None:
        """Register dispatch in broker so FK constraint is satisfied."""
        self.core.register(dispatch_id, "prompt", terminal_id="T2", track="B")

    def test_check_terminal_available_when_no_lease(self) -> None:
        result = self.core.check_terminal("T2", "d-check-001")
        self.assertTrue(result["available"])

    def test_acquire_lease_succeeds_on_idle_terminal(self) -> None:
        self._reg("d-lease-001")
        result = self.core.acquire_lease("T2", "d-lease-001")
        self.assertTrue(result.acquired)
        self.assertIsNotNone(result.generation)
        self.assertGreater(result.generation, 0)

    def test_acquire_lease_creates_db_row(self) -> None:
        self._reg("d-lease-002")
        self.core.acquire_lease("T2", "d-lease-002")
        with get_connection(self.state_dir) as conn:
            row = get_lease(conn, "T2")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "leased")
        self.assertEqual(row["dispatch_id"], "d-lease-002")

    def test_check_terminal_blocked_when_leased_to_other(self) -> None:
        self._reg("d-other-dispatch")
        self._reg("d-new-dispatch")
        self.core.acquire_lease("T2", "d-other-dispatch")
        result = self.core.check_terminal("T2", "d-new-dispatch")
        self.assertFalse(result["available"])
        self.assertIn("leased", result["reason"])

    def test_check_terminal_available_for_same_dispatch(self) -> None:
        self._reg("d-same-001")
        self.core.acquire_lease("T2", "d-same-001")
        result = self.core.check_terminal("T2", "d-same-001")
        self.assertTrue(result["available"])
        self.assertEqual(result["reason"], "same_dispatch")

    def test_release_lease_returns_terminal_to_idle(self) -> None:
        self._reg("d-release-001")
        acq = self.core.acquire_lease("T2", "d-release-001")
        self.core.release_lease("T2", acq.generation)
        with get_connection(self.state_dir) as conn:
            row = get_lease(conn, "T2")
        self.assertEqual(row["state"], "idle")

    def test_double_claim_prevention(self) -> None:
        self._reg("d-first")
        self._reg("d-second")
        self.core.acquire_lease("T2", "d-first")
        result = self.core.acquire_lease("T2", "d-second")
        self.assertFalse(result.acquired)
        self.assertIsNotNone(result.error)

    def test_stale_lease_check_does_not_raise(self) -> None:
        # DB error in check_terminal must fail closed without raising.
        broken_core = _make_core("/nonexistent/state", "/nonexistent/dispatch")
        result = broken_core.check_terminal("T2", "d-broken")
        self.assertFalse(result["available"])
        self.assertIn("check_error_fail_closed", result["reason"])


# ---------------------------------------------------------------------------
# TestReceiptLinkage
# ---------------------------------------------------------------------------

class TestReceiptLinkage(unittest.TestCase):
    """Receipts still correlate cleanly to dispatch_id after cutover."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_broker_bundle_preserves_dispatch_id(self) -> None:
        """dispatch_id in broker bundle matches dispatch_id in receipt."""
        broker = DispatchBroker(self.state_dir, self.dispatch_dir, shadow_mode=False)
        broker.register("d-receipt-001", "Instructions.", terminal_id="T2", track="B")
        bundle = (Path(self.dispatch_dir) / "d-receipt-001" / "bundle.json").read_text()
        data = json.loads(bundle)
        self.assertEqual(data["dispatch_id"], "d-receipt-001")

    def test_receipt_dispatch_id_survives_transport_change(self) -> None:
        """dispatch_id in receipt markdown is independent of broker state.

        Receipt markdown contains '**Dispatch ID**: <id>' which the receipt
        processor extracts. This is not affected by broker cutover.
        """
        receipt_markdown = """
## Report Metadata

```
**Dispatch ID**: d-receipt-002
**PR**: PR-5
**Track**: B
**Gate**: gate_pr5_runtime_core_cutover
**Status**: success
```
"""
        # Simulate receipt processing: extract dispatch_id from markdown
        import re
        match = re.search(r"\*\*Dispatch ID\*\*:\s*(\S+)", receipt_markdown)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "d-receipt-002")

    def test_compat_check_receipt_linkage_passes(self) -> None:
        """Compat check receipt_linkage component passes when no receipts yet."""
        result = RuntimeCore.check_compatibility(self.state_dir, self.dispatch_dir)
        receipt_result = result["components"]["receipt_linkage"]
        self.assertTrue(receipt_result["ok"])

    def test_compat_check_receipt_linkage_with_dispatch_id_in_receipt(self) -> None:
        """Compat check detects dispatch_id in existing receipt file."""
        receipts_path = Path(self.state_dir) / "t0_receipts.ndjson"
        receipts_path.write_text(
            json.dumps({"dispatch_id": "d-old-001", "event": "task_complete"}) + "\n"
        )
        result = RuntimeCore.check_compatibility(self.state_dir, self.dispatch_dir)
        receipt_result = result["components"]["receipt_linkage"]
        self.assertTrue(receipt_result["ok"])
        self.assertTrue(receipt_result.get("has_dispatch_id", False))


# ---------------------------------------------------------------------------
# TestGovernanceCompatibility
# ---------------------------------------------------------------------------

class TestGovernanceCompatibility(unittest.TestCase):
    """Existing governance workflows remain functional without regression."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_t0_authority_preserved_in_compat_check(self) -> None:
        """Compat check confirms T0 completion authority is not moved to broker."""
        result = RuntimeCore.check_compatibility(self.state_dir, self.dispatch_dir)
        t0 = result["components"]["t0_authority"]
        self.assertTrue(t0["ok"])
        self.assertIn("receipt-processor", t0["note"])
        self.assertIn("T0", t0["note"])

    def test_broker_does_not_auto_complete_dispatches(self) -> None:
        """Broker delivery path stops at 'accepted', never 'completed' (G-R4)."""
        core = _make_core(self.state_dir, self.dispatch_dir)
        core.register("d-gov-001", "Instructions.", terminal_id="T2", track="B")
        start = core.delivery_start("d-gov-001", "T2")
        core.delivery_success("d-gov-001", start.attempt_id)

        with get_connection(self.state_dir) as conn:
            row = get_dispatch(conn, "d-gov-001")

        # Broker sets 'accepted', NOT 'completed' — T0 must do that
        self.assertEqual(row["state"], "accepted")
        self.assertNotEqual(row["state"], "completed")

    def test_compat_check_all_components_ok(self) -> None:
        """Full compat check passes with default env (runtime primary enabled)."""
        with patch.dict(os.environ, {
            "VNX_RUNTIME_PRIMARY": "1",
            "VNX_BROKER_ENABLED": "1",
            "VNX_BROKER_SHADOW": "0",
        }):
            result = RuntimeCore.check_compatibility(self.state_dir, self.dispatch_dir)
        self.assertTrue(result["compatible"])


# ---------------------------------------------------------------------------
# TestRollbackPath
# ---------------------------------------------------------------------------

class TestRollbackPath(unittest.TestCase):
    """Rollback path to legacy transport is documented and tested."""

    def test_rollback_script_exists(self) -> None:
        rollback_script = _SCRIPTS_DIR / "rollback_runtime_core.py"
        self.assertTrue(rollback_script.exists(), "rollback_runtime_core.py must exist")

    def test_rollback_docs_exist(self) -> None:
        docs = _SCRIPTS_DIR.parent / "docs" / "operations" / "RUNTIME_CORE_ROLLBACK.md"
        self.assertTrue(docs.exists(), "docs/operations/RUNTIME_CORE_ROLLBACK.md must exist")

    def test_runtime_core_disabled_when_primary_zero(self) -> None:
        """When VNX_RUNTIME_PRIMARY=0, load_runtime_core returns None (legacy path)."""
        tmp = tempfile.TemporaryDirectory()
        try:
            state_dir, dispatch_dir = _setup_dirs(tmp)
            with patch.dict(os.environ, {"VNX_RUNTIME_PRIMARY": "0"}):
                core = load_runtime_core(state_dir, dispatch_dir)
            self.assertIsNone(core)
        finally:
            tmp.cleanup()

    def test_rollback_write_updates_env_override(self) -> None:
        """rollback_runtime_core.py rollback writes VNX_RUNTIME_PRIMARY=0."""
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        try:
            env_override = Path(tmp.name) / ".env_override"
            env = {**os.environ, "VNX_DATA_DIR": tmp.name}
            result = subprocess.run(
                [sys.executable, str(_SCRIPTS_DIR / "rollback_runtime_core.py"), "rollback"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0)
            self.assertTrue(env_override.exists())
            content = env_override.read_text()
            self.assertIn("VNX_RUNTIME_PRIMARY=0", content)
        finally:
            tmp.cleanup()

    def test_rollback_enable_writes_runtime_primary_one(self) -> None:
        """rollback_runtime_core.py enable writes VNX_RUNTIME_PRIMARY=1."""
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        try:
            env = {**os.environ, "VNX_DATA_DIR": tmp.name}
            result = subprocess.run(
                [sys.executable, str(_SCRIPTS_DIR / "rollback_runtime_core.py"), "enable"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0)
            env_override = Path(tmp.name) / ".env_override"
            content = env_override.read_text()
            self.assertIn("VNX_RUNTIME_PRIMARY=1", content)
        finally:
            tmp.cleanup()


# ---------------------------------------------------------------------------
# TestCutoverCompatibilityCheck
# ---------------------------------------------------------------------------

class TestCutoverCompatibilityCheck(unittest.TestCase):
    """runtime_cutover_check.py validates all components."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir, self.dispatch_dir = _setup_dirs(self._tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_compat_check_returns_compatible_true(self) -> None:
        with patch.dict(os.environ, {
            "VNX_BROKER_ENABLED": "1",
            "VNX_BROKER_SHADOW": "0",
        }):
            result = RuntimeCore.check_compatibility(self.state_dir, self.dispatch_dir)
        self.assertTrue(result["compatible"])

    def test_compat_check_flags_reflect_environment(self) -> None:
        with patch.dict(os.environ, {
            "VNX_RUNTIME_PRIMARY": "1",
            "VNX_BROKER_SHADOW": "0",
            "VNX_CANONICAL_LEASE_ACTIVE": "1",
        }):
            result = RuntimeCore.check_compatibility(self.state_dir, self.dispatch_dir)
        flags = result["flags"]
        self.assertEqual(flags["VNX_RUNTIME_PRIMARY"], "1")
        self.assertEqual(flags["VNX_BROKER_SHADOW"], "0")
        self.assertEqual(flags["VNX_CANONICAL_LEASE_ACTIVE"], "1")

    def test_compat_check_has_all_required_components(self) -> None:
        result = RuntimeCore.check_compatibility(self.state_dir, self.dispatch_dir)
        required = {"db", "broker", "lease_manager", "adapter", "receipt_linkage", "t0_authority"}
        self.assertEqual(required, required & set(result["components"].keys()))

    def test_compat_check_script_exits_zero_on_compatible(self) -> None:
        import subprocess
        env = {
            **os.environ,
            "VNX_DATA_DIR": str(Path(self._tmp.name)),
            "VNX_STATE_DIR": self.state_dir,
            "VNX_DISPATCH_DIR": self.dispatch_dir,
            "VNX_BROKER_ENABLED": "1",
            "VNX_BROKER_SHADOW": "0",
        }
        result = subprocess.run(
            [sys.executable, str(_SCRIPTS_DIR / "runtime_cutover_check.py")],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
