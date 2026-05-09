"""Regression tests for partial-identity fallback in dispatch_register (codex round-7 finding 3).

Pre-fix: the identity resolution block fired only when ALL four fields
(operator_id, project_id, orchestrator_id, agent_id) were absent.  A caller
that supplied even one field suppressed resolution of the others, so partial-
identity callers silently skipped the central mirror path (``if project_id:``
downstream).

Post-fix: the block fires whenever ANY field is absent (``not all([...])``),
and empty-string values are treated as unset.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_register
from dispatch_register import append_event


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    state_dir = tmp_path / ".vnx-data" / "state"
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / ".vnx-data"))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.delenv("VNX_OPERATOR_ID", raising=False)
    return tmp_path


def _reg_path(data_dir: Path) -> Path:
    return data_dir / ".vnx-data" / "state" / "dispatch_register.ndjson"


class TestPartialIdentityFallback:
    """When a caller supplies only some identity fields, the rest must be resolved."""

    def test_operator_id_only_still_resolves_project_id(self, isolated_state, tmp_path):
        """Supplying operator_id must not suppress project_id resolution.

        Pre-fix: the ``or`` guard treated operator_id alone as 'identity present'
        and skipped ``_resolve_identity_for_register`` entirely, so project_id
        stayed None and the central mirror was never attempted.
        """
        central_base = tmp_path / "central"
        resolved_project_id = "vnx-resolved"

        def _fake_identity():
            return {
                "operator_id": "op-from-env",
                "project_id": resolved_project_id,
                "orchestrator_id": None,
                "agent_id": None,
            }

        def _patched_resolve_central(pid):
            return central_base / pid

        with patch.object(dispatch_register, "_resolve_identity_for_register", side_effect=_fake_identity), \
             patch.object(dispatch_register, "_resolve_central_data_dir", _patched_resolve_central):
            result = append_event(
                "dispatch_created",
                dispatch_id="d-partial-001",
                operator_id="op-explicit",  # only operator_id supplied
            )

        assert result is True

        # The central mirror must have been written because project_id was resolved.
        central_path = central_base / resolved_project_id / "state" / "dispatch_register.ndjson"
        assert central_path.exists(), (
            "Central mirror must be written when project_id is resolved from identity. "
            "Pre-fix: partial-identity callers skipped central mirror entirely."
        )
        rec = json.loads(central_path.read_text().strip())
        assert rec["dispatch_id"] == "d-partial-001"
        assert rec["project_id"] == resolved_project_id
        assert rec["operator_id"] == "op-explicit", (
            "Caller-supplied operator_id must not be overwritten by resolved identity."
        )

    def test_empty_string_operator_id_treated_as_unset(self, isolated_state, tmp_path):
        """An empty-string operator_id must be treated as unset and resolved."""
        central_base = tmp_path / "central"

        def _fake_identity():
            return {
                "operator_id": "op-from-env",
                "project_id": "vnx-proj",
                "orchestrator_id": None,
                "agent_id": None,
            }

        def _patched_resolve_central(pid):
            return central_base / pid

        with patch.object(dispatch_register, "_resolve_identity_for_register", side_effect=_fake_identity), \
             patch.object(dispatch_register, "_resolve_central_data_dir", _patched_resolve_central):
            result = append_event(
                "dispatch_created",
                dispatch_id="d-empty-op",
                operator_id="",  # empty string → treated as unset
            )

        assert result is True
        reg = _reg_path(isolated_state)
        rec = json.loads(reg.read_text().strip())
        # Empty string should be resolved to the identity value.
        assert rec.get("operator_id") == "op-from-env", (
            "Empty-string operator_id must be resolved from identity, not left empty."
        )

    def test_all_fields_present_skips_resolution(self, isolated_state, tmp_path):
        """When all four fields are supplied, identity resolution must NOT be called."""
        resolution_called = {"called": False}

        def _tracking_identity():
            resolution_called["called"] = True
            return {}

        with patch.object(dispatch_register, "_resolve_identity_for_register", side_effect=_tracking_identity):
            append_event(
                "dispatch_created",
                dispatch_id="d-all-fields",
                operator_id="op-x",
                project_id="proj-x",
                orchestrator_id="orch-x",
                agent_id="agent-x",
            )

        assert not resolution_called["called"], (
            "Identity resolution must be skipped when all four fields are supplied."
        )

    def test_project_id_only_still_resolves_operator_id(self, isolated_state, tmp_path):
        """Supplying project_id must not suppress operator_id resolution."""
        central_base = tmp_path / "central"
        supplied_project = "my-project"

        def _fake_identity():
            return {
                "operator_id": "op-resolved",
                "project_id": "should-not-overwrite",
                "orchestrator_id": None,
                "agent_id": None,
            }

        def _patched_resolve_central(pid):
            return central_base / pid

        with patch.object(dispatch_register, "_resolve_identity_for_register", side_effect=_fake_identity), \
             patch.object(dispatch_register, "_resolve_central_data_dir", _patched_resolve_central):
            append_event(
                "gate_passed",
                dispatch_id="d-pid-only",
                project_id=supplied_project,  # only project_id supplied
            )

        reg = _reg_path(isolated_state)
        rec = json.loads(reg.read_text().strip())
        # Caller-supplied project_id must be kept; operator_id should be resolved.
        assert rec["project_id"] == supplied_project, (
            "Caller-supplied project_id must not be overwritten."
        )
        assert rec.get("operator_id") == "op-resolved", (
            "operator_id must be resolved from identity when project_id is the only field supplied."
        )
