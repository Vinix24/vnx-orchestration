"""Regression tests: silent-except hardening in append_receipt_internals/payload.py (OI-1437).

Covers the 5 findings narrowed in chore/cleanup-payload-silent-except:
1. _run_post_append_hooks: state rebuild hook — logs warning, does not raise
2. _run_post_append_hooks: classifier hook — logs warning, does not raise
3. _stamp_observability_tier: resolve failure — logs warning, does not raise
4. _mirror_receipt_to_central: mirror failure — logs warning, does not raise
5. _maybe_trigger_state_rebuild: trigger failure — logs warning, does not raise
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import append_receipt_internals.payload as payload_mod
from append_receipt_internals.common import register_facade, _facade_modules


def _make_receipt(dispatch_id: str = "d-test", project_id: str = "proj-x") -> dict:
    return {
        "dispatch_id": dispatch_id,
        "project_id": project_id,
        "event_type": "task_complete",
        "status": "success",
        "terminal": "T1",
    }


@pytest.fixture
def stub_facade():
    """Register a stub facade module with MagicMock hooks; clean up after test.

    _FacadeProxy prefers Mock-typed values over real ones, so these mocks
    take precedence over any previously-registered real facade module.
    """
    mod = types.ModuleType("_test_payload_stub")
    mod._register_quality_open_items = MagicMock()
    mod._update_confidence_from_receipt = MagicMock()
    mod._emit_dispatch_register = MagicMock()
    mod._maybe_trigger_state_rebuild = MagicMock()
    mod._trigger_receipt_classifier = MagicMock()
    register_facade(mod)
    yield mod
    if mod in _facade_modules:
        _facade_modules.remove(mod)


# ---------------------------------------------------------------------------
# Finding 1 + 2 — _run_post_append_hooks: state-rebuild and classifier
# ---------------------------------------------------------------------------

def test_runs_clean_with_valid_payload(stub_facade, caplog):
    """Baseline: _run_post_append_hooks succeeds when hooks behave normally."""
    receipt = _make_receipt()
    with caplog.at_level(logging.WARNING, logger="append_receipt_internals.payload"):
        payload_mod._run_post_append_hooks(receipt)
    assert not caplog.records, "No warnings expected on clean run"


def test_state_rebuild_hook_failure_logs_warning(stub_facade, caplog):
    """Finding 1: state rebuild hook failure logs warning, never silently swallows."""
    stub_facade._maybe_trigger_state_rebuild.side_effect = RuntimeError("state rebuild exploded")
    receipt = _make_receipt()
    with caplog.at_level(logging.WARNING, logger="append_receipt_internals.payload"):
        payload_mod._run_post_append_hooks(receipt)
    assert any("state rebuild hook failed" in r.message for r in caplog.records), (
        "Expected 'state rebuild hook failed' warning; got: " + str([r.message for r in caplog.records])
    )


def test_classifier_hook_failure_logs_warning(stub_facade, caplog):
    """Finding 2: classifier hook failure logs warning, never silently swallows."""
    stub_facade._trigger_receipt_classifier.side_effect = ValueError("classifier crashed")
    receipt = _make_receipt()
    with caplog.at_level(logging.WARNING, logger="append_receipt_internals.payload"):
        payload_mod._run_post_append_hooks(receipt)
    assert any("receipt classifier hook failed" in r.message for r in caplog.records), (
        "Expected 'receipt classifier hook failed' warning; got: " + str([r.message for r in caplog.records])
    )


# ---------------------------------------------------------------------------
# Finding 3 — _stamp_observability_tier: resolve failure
# ---------------------------------------------------------------------------

def test_stamp_observability_tier_import_error_is_silent(caplog):
    """Finding 3 (import path): ImportError → silent return, no warning expected."""
    receipt = {"provider": "some-provider"}

    with patch.dict("sys.modules", {"observability_tier": None}):
        with caplog.at_level(logging.WARNING, logger="append_receipt_internals.payload"):
            payload_mod._stamp_observability_tier(receipt)

    assert "observability_tier" not in receipt
    assert not caplog.records, "ImportError path must not log (expected miss)"


def test_stamp_observability_tier_resolve_failure_logs_warning(caplog):
    """Finding 3 (resolve path): resolve_effective_tier exception → logs warning."""
    receipt = {"provider": "bad-provider"}

    fake_module = MagicMock()
    fake_module.resolve_effective_tier.side_effect = ValueError("unknown provider")

    with patch.dict("sys.modules", {"observability_tier": fake_module}):
        with caplog.at_level(logging.WARNING, logger="append_receipt_internals.payload"):
            payload_mod._stamp_observability_tier(receipt)

    assert "observability_tier" not in receipt
    assert any("failed to resolve observability tier" in r.message for r in caplog.records), (
        "Expected observability tier warning; got: " + str([r.message for r in caplog.records])
    )


# ---------------------------------------------------------------------------
# Finding 4 — _mirror_receipt_to_central: corrupt prior-receipt state
# ---------------------------------------------------------------------------

def test_corrupt_prior_state_logs_warning(tmp_path, caplog):
    """Finding 4: _mirror_receipt_to_central failure logs warning, does not raise."""
    receipt = _make_receipt(project_id="proj-warn")
    primary_path = tmp_path / "receipts.ndjson"

    with patch.object(payload_mod, "_mirror_receipt_to_central_or_raise",
                      side_effect=OSError("central disk full")), \
         patch.object(payload_mod, "resolve_central_data_dir",
                      return_value=tmp_path / "central"):
        with caplog.at_level(logging.WARNING, logger="append_receipt_internals.payload"):
            payload_mod._mirror_receipt_to_central(receipt, primary_path)

    assert any("central mirror failed" in r.message for r in caplog.records), (
        "Expected 'central mirror failed' warning; got: " + str([r.message for r in caplog.records])
    )


def test_mirror_receipt_to_central_never_raises(tmp_path):
    """Finding 4: _mirror_receipt_to_central must never raise regardless of error."""
    receipt = _make_receipt(project_id="proj-safe")
    primary_path = tmp_path / "receipts.ndjson"

    with patch.object(payload_mod, "_mirror_receipt_to_central_or_raise",
                      side_effect=Exception("catastrophic failure")):
        payload_mod._mirror_receipt_to_central(receipt, primary_path)


# ---------------------------------------------------------------------------
# Finding 5 — _maybe_trigger_state_rebuild: trigger failure
# ---------------------------------------------------------------------------

def test_state_rebuild_trigger_failure_logs_warning(caplog):
    """Finding 5: maybe_trigger_state_rebuild exception → logs warning, does not raise."""
    receipt = _make_receipt()
    receipt["event_type"] = "task_complete"

    fake_module = MagicMock()
    fake_module.maybe_trigger_state_rebuild.side_effect = RuntimeError("rebuild service down")

    with patch.dict("sys.modules", {"state_rebuild_trigger": fake_module}):
        with caplog.at_level(logging.WARNING, logger="append_receipt_internals.payload"):
            payload_mod._maybe_trigger_state_rebuild(receipt)

    assert any("state rebuild trigger failed" in r.message for r in caplog.records), (
        "Expected 'state rebuild trigger failed' warning; got: " + str([r.message for r in caplog.records])
    )


def test_state_rebuild_trigger_import_error_is_silent(caplog):
    """Finding 5 (import path): ImportError → silent return, no warning expected."""
    receipt = _make_receipt()
    receipt["event_type"] = "task_complete"

    with patch.dict("sys.modules", {"state_rebuild_trigger": None}):
        with caplog.at_level(logging.WARNING, logger="append_receipt_internals.payload"):
            payload_mod._maybe_trigger_state_rebuild(receipt)

    assert not caplog.records, "ImportError path must not log (expected miss)"
