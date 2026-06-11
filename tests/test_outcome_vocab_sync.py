"""Gate-F2: cross-module vocabulary consistency tests.

Four modules carry a FAILURE_STATUSES set that must be kept in sync:
  1. scripts/check_active_drain.py         — FAILURE_STATUSES (frozenset)
  2. scripts/weekly_digest.py              — _DispatchOutcomeClassifier._FAILURE_STATUSES
  3. scripts/lib/receipt_classifier.py     — _FAILURE_STATUSES (set)
  4. scripts/lib/append_receipt_internals/payload.py — FAILURE_STATUSES (local set)

Three modules carry a SUCCESS_STATUSES set that is also sync-guarded:
  1. scripts/check_active_drain.py         — SUCCESS_STATUSES (frozenset)
  2. scripts/weekly_digest.py              — _DispatchOutcomeClassifier._SUCCESS_STATUSES
  3. scripts/lib/append_receipt_internals/payload.py — SUCCESS_STATUSES (local set)

This file contains:
  A) Per-module assertions that "contract_invalid" is present (gate-F2 requirement).
  B) A cross-module consistency test that imports all four FAILURE sets and verifies
     they are identical, so any future divergence fails CI structurally.
  B2) Cross-module SUCCESS_STATUSES structural tests (drain/digest/payload).
      Documented payload-specific additions: "" (empty string as success for
      the confidence-scoring path — drain carries it too; digest omits it, which
      is an existing documented difference between the two consumers).
  C) Semantic checks: the `failures_direct` branch in receipt_classifier is
     tested to fire immediately for a `contract_invalid` receipt and to
     queue for batch for a success receipt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import FrozenSet, Set
from unittest import mock

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Helpers to extract the four sets without side effects from module-level env.
# ---------------------------------------------------------------------------

def _get_drain_failure_statuses() -> FrozenSet[str]:
    import check_active_drain
    return check_active_drain.FAILURE_STATUSES


def _extract_frozenset_or_set_literal(node: "ast.expr") -> FrozenSet[str]:  # type: ignore[name-defined]
    """Extract a frozenset from either a set literal or a frozenset({...}) call."""
    import ast

    # Plain set literal: {a, b, ...}
    if isinstance(node, ast.Set):
        return frozenset(ast.literal_eval(e) for e in node.elts)
    # frozenset({...}) call
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and node.args
        and isinstance(node.args[0], ast.Set)
    ):
        return frozenset(ast.literal_eval(e) for e in node.args[0].elts)
    raise ValueError(f"Unrecognised set AST node: {ast.dump(node)}")


def _get_digest_failure_statuses() -> FrozenSet[str]:
    """Extract _FAILURE_STATUSES from weekly_digest.collect_metrics via AST.

    The set is an annotated assignment (`_FAILURE_STATUSES: frozenset[str] = frozenset({...})`)
    inside collect_metrics, so we parse it from source rather than executing the
    function (which requires a live filesystem + DB).
    """
    import ast

    wd_file = SCRIPTS_DIR / "weekly_digest.py"
    src = wd_file.read_text(encoding="utf-8")
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "collect_metrics":
            for stmt in ast.walk(node):
                # Annotated assignment: _FAILURE_STATUSES: frozenset[str] = frozenset({...})
                if isinstance(stmt, ast.AnnAssign):
                    if isinstance(stmt.target, ast.Name) and stmt.target.id == "_FAILURE_STATUSES":
                        if stmt.value is not None:
                            return _extract_frozenset_or_set_literal(stmt.value)
                # Plain assignment: _FAILURE_STATUSES = {... } or frozenset({...})
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == "_FAILURE_STATUSES":
                            return _extract_frozenset_or_set_literal(stmt.value)
    raise AssertionError("_FAILURE_STATUSES not found in weekly_digest.collect_metrics")


def _get_classifier_failure_statuses() -> FrozenSet[str]:
    import receipt_classifier as rc
    return frozenset(rc._FAILURE_STATUSES)


def _get_payload_failure_statuses() -> FrozenSet[str]:
    """Extract FAILURE_STATUSES from _update_confidence_from_receipt in payload.

    The set is defined locally inside the function body, so we parse it from
    source via AST to avoid executing the function (which requires an active DB).

    Intentional semantic gap vs. the other three sets:
      - payload excludes 'timeout' because task_timeout is handled by the
        event_type == "task_failed" branch (line ~392), not the FAILURE_STATUSES
        status-match.  The confidence scorer uses a different routing path for
        timeouts than the dispatch router (check_active_drain) or the FPY
        classifier (weekly_digest / receipt_classifier).

    The cross-module sync tests therefore compare the common-core subset
    (i.e. drain ∩ classifier ∩ digest ∩ payload) rather than demanding
    strict equality across all four sets.
    """
    import ast

    payload_file = LIB_DIR / "append_receipt_internals" / "payload.py"
    src = payload_file.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_update_confidence_from_receipt":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == "FAILURE_STATUSES":
                            return _extract_frozenset_or_set_literal(stmt.value)
    raise AssertionError("FAILURE_STATUSES not found in payload._update_confidence_from_receipt")


# ---------------------------------------------------------------------------
# A) Per-module: contract_invalid presence
# ---------------------------------------------------------------------------

class TestContractInvalidPresence:
    def test_drain_has_contract_invalid(self):
        assert "contract_invalid" in _get_drain_failure_statuses(), (
            "check_active_drain.FAILURE_STATUSES is missing 'contract_invalid'"
        )

    def test_digest_has_contract_invalid(self):
        assert "contract_invalid" in _get_digest_failure_statuses(), (
            "weekly_digest._FAILURE_STATUSES is missing 'contract_invalid'"
        )

    def test_classifier_has_contract_invalid(self):
        assert "contract_invalid" in _get_classifier_failure_statuses(), (
            "receipt_classifier._FAILURE_STATUSES is missing 'contract_invalid' (gate-F2)"
        )

    def test_payload_has_contract_invalid(self):
        assert "contract_invalid" in _get_payload_failure_statuses(), (
            "payload.FAILURE_STATUSES is missing 'contract_invalid' (gate-F2)"
        )


# ---------------------------------------------------------------------------
# B) Cross-module structural consistency — FAILURE_STATUSES
# ---------------------------------------------------------------------------

# Intentional documented gaps between the four FAILURE_STATUSES sets:
#
#   payload excludes 'timeout':
#     task_timeout events reach the else-return branch in
#     _update_confidence_from_receipt and never arrive at the FAILURE_STATUSES
#     status-match at all.  Routing task_timeout through the event_type=="task_failed"
#     branch is INCORRECT (that branch only fires on event_type=="task_failed"); the
#     correct description is that task_timeout is simply excluded from confidence
#     scoring (pre-existing behaviour, kept deliberately).  All other entries MUST be
#     present in payload's FAILURE_STATUSES.
#
# The canonical reference for the gate-F2 requirement is the shared core:
#   {"failed","failure","error","blocked","contract_invalid"}
# plus "timeout" for drain/classifier/digest (but NOT payload — documented above).

_PAYLOAD_KNOWN_EXCLUSIONS: FrozenSet[str] = frozenset({"timeout"})


class TestVocabCrossModuleSync:
    """Structural drift tests between the four FAILURE_STATUSES sets.

    The canonical reference is check_active_drain.FAILURE_STATUSES.
    - classifier and digest must exactly match drain.
    - payload must contain all drain entries EXCEPT the documented exclusions.
    Adding a new status to drain without updating the others fails this test.
    """

    def test_classifier_failure_set_matches_drain(self):
        drain = _get_drain_failure_statuses()
        classifier = _get_classifier_failure_statuses()
        assert classifier == drain, (
            f"receipt_classifier._FAILURE_STATUSES diverged from check_active_drain.FAILURE_STATUSES.\n"
            f"  drain only  : {drain - classifier}\n"
            f"  classif only: {classifier - drain}"
        )

    def test_digest_failure_set_matches_drain(self):
        drain = _get_drain_failure_statuses()
        digest = _get_digest_failure_statuses()
        assert digest == drain, (
            f"weekly_digest._FAILURE_STATUSES diverged from check_active_drain.FAILURE_STATUSES.\n"
            f"  drain only  : {drain - digest}\n"
            f"  digest only : {digest - drain}"
        )

    def test_payload_contains_drain_minus_known_exclusions(self):
        """payload.FAILURE_STATUSES must be a superset of (drain - _PAYLOAD_KNOWN_EXCLUSIONS).

        'timeout' is the only intentional exclusion; any other missing member is a bug.
        """
        drain = _get_drain_failure_statuses()
        payload = _get_payload_failure_statuses()
        required = drain - _PAYLOAD_KNOWN_EXCLUSIONS
        missing = required - payload
        assert not missing, (
            f"payload.FAILURE_STATUSES is missing required members (gate-F2).\n"
            f"  missing       : {sorted(missing)}\n"
            f"  known-excluded: {sorted(_PAYLOAD_KNOWN_EXCLUSIONS)}\n"
            f"  payload has   : {sorted(payload)}"
        )

    def test_payload_has_no_unexpected_exclusions(self):
        """No new intentional exclusions beyond _PAYLOAD_KNOWN_EXCLUSIONS.

        If a new exclusion is intentional, add it to _PAYLOAD_KNOWN_EXCLUSIONS
        with an explicit comment explaining the semantic reason.
        """
        drain = _get_drain_failure_statuses()
        payload = _get_payload_failure_statuses()
        unexpected_missing = (drain - payload) - _PAYLOAD_KNOWN_EXCLUSIONS
        assert not unexpected_missing, (
            f"payload.FAILURE_STATUSES has unexpected missing entries: {sorted(unexpected_missing)}.\n"
            "If intentional, add to _PAYLOAD_KNOWN_EXCLUSIONS with a comment."
        )

    def test_common_core_present_in_all_four(self):
        """The common core (gate-F2 requirements) must be in all four sets."""
        core = frozenset({
            "failed", "failure", "error", "blocked", "contract_invalid",
        })
        drain = _get_drain_failure_statuses()
        classifier = _get_classifier_failure_statuses()
        digest = _get_digest_failure_statuses()
        payload = _get_payload_failure_statuses()

        sets = {
            "drain": drain,
            "classifier": classifier,
            "digest": digest,
            "payload": payload,
        }
        for name, s in sets.items():
            missing = core - s
            assert not missing, (
                f"{name}: missing core failure statuses (gate-F2).\n"
                f"  missing: {sorted(missing)}"
            )


# ---------------------------------------------------------------------------
# B2) Cross-module SUCCESS_STATUSES structural consistency
# ---------------------------------------------------------------------------

# Helpers to extract SUCCESS sets from the three modules that carry them.

def _get_drain_success_statuses() -> FrozenSet[str]:
    import check_active_drain
    return check_active_drain.SUCCESS_STATUSES


def _get_digest_success_statuses() -> FrozenSet[str]:
    """Extract _SUCCESS_STATUSES from weekly_digest.collect_metrics via AST."""
    import ast

    wd_file = SCRIPTS_DIR / "weekly_digest.py"
    src = wd_file.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "collect_metrics":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.AnnAssign):
                    if isinstance(stmt.target, ast.Name) and stmt.target.id == "_SUCCESS_STATUSES":
                        if stmt.value is not None:
                            return _extract_frozenset_or_set_literal(stmt.value)
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == "_SUCCESS_STATUSES":
                            return _extract_frozenset_or_set_literal(stmt.value)
    raise AssertionError("_SUCCESS_STATUSES not found in weekly_digest.collect_metrics")


def _get_payload_success_statuses() -> FrozenSet[str]:
    """Extract SUCCESS_STATUSES from payload._update_confidence_from_receipt via AST."""
    import ast

    payload_file = LIB_DIR / "append_receipt_internals" / "payload.py"
    src = payload_file.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_update_confidence_from_receipt":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == "SUCCESS_STATUSES":
                            return _extract_frozenset_or_set_literal(stmt.value)
    raise AssertionError("SUCCESS_STATUSES not found in payload._update_confidence_from_receipt")


# Documented structural differences in SUCCESS_STATUSES across modules:
#
#   digest omits "":
#     weekly_digest classifies receipts that passed event-type gating; an empty
#     status string is not meaningful in that context so the digest excludes it.
#     drain and payload include "" (drain: upstream classifier; payload: empty
#     status on a task_complete receipt treated conservatively as success).
#
# The canonical core that ALL three must carry:
_SUCCESS_CORE = frozenset({"success", "completed", "complete", "ok", "done"})

# payload-specific addition documented above (drain also has it):
_PAYLOAD_SUCCESS_OWN_ADDITIONS: FrozenSet[str] = frozenset({""})
# digest-known omissions vs drain (documented acceptable gap):
_DIGEST_SUCCESS_KNOWN_OMISSIONS: FrozenSet[str] = frozenset({""})


class TestSuccessVocabCrossModuleSync:
    """Structural drift tests between SUCCESS_STATUSES sets in drain / digest / payload.

    The canonical reference is check_active_drain.SUCCESS_STATUSES.
    - payload must contain all drain entries (drain also has "" so payload matches drain).
    - digest may omit "" (documented acceptable difference).
    - All three must contain the common core.
    """

    def test_payload_success_set_matches_drain(self):
        """After adding 'done', payload.SUCCESS_STATUSES must equal drain.SUCCESS_STATUSES."""
        drain = _get_drain_success_statuses()
        payload = _get_payload_success_statuses()
        assert payload == drain, (
            f"payload.SUCCESS_STATUSES diverged from check_active_drain.SUCCESS_STATUSES.\n"
            f"  drain only  : {drain - payload}\n"
            f"  payload only: {payload - drain}"
        )

    def test_payload_success_contains_done(self):
        """Regression guard: 'done' must be in payload.SUCCESS_STATUSES (gate finding)."""
        payload = _get_payload_success_statuses()
        assert "done" in payload, (
            "payload.SUCCESS_STATUSES is missing 'done' — tmux-lane writes status='done' "
            "and those receipts must update success-confidence (gate finding)."
        )

    def test_digest_success_core_present(self):
        """digest._SUCCESS_STATUSES must contain the common core (minus documented omissions)."""
        digest = _get_digest_success_statuses()
        missing = _SUCCESS_CORE - digest
        assert not missing, (
            f"weekly_digest._SUCCESS_STATUSES missing core success entries: {sorted(missing)}"
        )

    def test_drain_success_core_present(self):
        """drain.SUCCESS_STATUSES must contain the full common core."""
        drain = _get_drain_success_statuses()
        missing = _SUCCESS_CORE - drain
        assert not missing, (
            f"check_active_drain.SUCCESS_STATUSES missing core entries: {sorted(missing)}"
        )

    def test_digest_has_no_unexpected_omissions_vs_drain(self):
        """digest may only omit entries in _DIGEST_SUCCESS_KNOWN_OMISSIONS."""
        drain = _get_drain_success_statuses()
        digest = _get_digest_success_statuses()
        unexpected = (drain - digest) - _DIGEST_SUCCESS_KNOWN_OMISSIONS
        assert not unexpected, (
            f"weekly_digest._SUCCESS_STATUSES has unexpected missing entries vs drain: {sorted(unexpected)}.\n"
            "If intentional, add to _DIGEST_SUCCESS_KNOWN_OMISSIONS with a comment."
        )


# ---------------------------------------------------------------------------
# C) Semantic checks for receipt_classifier failures_direct mode
# ---------------------------------------------------------------------------

class TestClassifierContractInvalidSemantics:
    """Verify that failures_direct mode treats contract_invalid as an immediate fire."""

    @pytest.fixture
    def env_state(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("VNX_STATE_DIR", str(state))
        monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_ENABLED", "1")
        monkeypatch.setenv("VNX_RECEIPT_CLASSIFIER_MODE", "failures_direct")
        return state

    def _make_receipt(self, status: str, event_type: str = "task_complete") -> dict:
        return {
            "dispatch_id": "TEST-123",
            "event_type": event_type,
            "status": status,
            "timestamp": "2026-06-01T12:00:00Z",
            "terminal": "T1",
        }

    def test_contract_invalid_fires_directly(self, env_state):
        import receipt_classifier as rc
        receipt = self._make_receipt("contract_invalid")
        with mock.patch.object(rc, "_spawn_async_classify") as spawn, \
             mock.patch.object(rc, "_append_to_queue") as queue:
            action = rc.trigger_receipt_classifier_async(receipt)
        assert action == "fired_failure_direct"
        spawn.assert_called_once()
        queue.assert_not_called()

    def test_success_queued_for_batch(self, env_state):
        import receipt_classifier as rc
        receipt = self._make_receipt("success")
        with mock.patch.object(rc, "_spawn_async_classify") as spawn, \
             mock.patch.object(rc, "_append_to_queue") as queue:
            action = rc.trigger_receipt_classifier_async(receipt)
        assert action == "queued_success_for_batch"
        queue.assert_called_once()
        spawn.assert_not_called()

    def test_timeout_fires_directly(self, env_state):
        """task_timeout event always fires directly regardless of status field."""
        import receipt_classifier as rc
        receipt = self._make_receipt("timeout", event_type="task_timeout")
        with mock.patch.object(rc, "_spawn_async_classify") as spawn, \
             mock.patch.object(rc, "_append_to_queue"):
            action = rc.trigger_receipt_classifier_async(receipt)
        assert action == "fired_failure_direct"
        spawn.assert_called_once()

    def test_failed_fires_directly(self, env_state):
        import receipt_classifier as rc
        receipt = self._make_receipt("failed", event_type="task_failed")
        with mock.patch.object(rc, "_spawn_async_classify") as spawn, \
             mock.patch.object(rc, "_append_to_queue"):
            action = rc.trigger_receipt_classifier_async(receipt)
        assert action == "fired_failure_direct"
        spawn.assert_called_once()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
