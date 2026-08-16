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
      The canonical success set (payload imports it from event_outcome_semantics)
      is {success, completed, complete, ok, done}. Empty status ("") was removed
      from canonical success — absence is not a success claim. drain still
      carries "" as a documented read-side classifier tolerance this
      consolidation leaves untouched; digest omits it.
  C) Semantic checks: the `failures_direct` branch in receipt_classifier is
     tested to fire immediately for a `contract_invalid` receipt and to
     queue for batch for a success receipt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import FrozenSet, Optional, Set
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


def _resolve_canonical_vocab(name: str) -> FrozenSet[str]:
    """Resolve NAME (e.g. 'FAILURE_STATUSES') off the canonical event_outcome_semantics module."""
    import event_outcome_semantics

    return frozenset(getattr(event_outcome_semantics, name))


def _extract_payload_vocab(
    var_name: str,
    func_name: str = "_update_confidence_from_receipt",
    source: Optional[str] = None,
) -> FrozenSet[str]:
    """Extract var_name (FAILURE_STATUSES/SUCCESS_STATUSES) from func_name in payload.py.

    OI-1148 made payload.py an IMPORTER of the canonical vocab rather than a
    hand-copied literal, so the extractor must recognize both forms a
    consumer's function body may contain (parsed from source via AST rather
    than executed, to avoid needing an active DB):

      1. A local literal assignment (`NAME = frozenset({...})` / `NAME = {...}`).
         Extracted structurally and returned as-is -- this is what lets the
         sync guard keep catching a hand-copied vocabulary that has drifted
         from the canonical module (see
         TestPayloadCanonicalImportRecognition.test_diverging_local_copy_still_fails_common_core_check
         for a regression proof this still fires).
      2. `from event_outcome_semantics import NAME` -- resolved by reading the
         LIVE attribute off the canonical module, so the sync assertion keeps
         comparing against the real source of truth instead of raising
         "not found" on an import shape it doesn't recognize.

    If both forms are somehow present, the one that lexically appears LAST
    wins -- mirroring Python's own name-binding semantics (whichever runs
    last is what the function actually uses at runtime).
    """
    import ast

    if source is None:
        payload_file = LIB_DIR / "append_receipt_internals" / "payload.py"
        source = payload_file.read_text(encoding="utf-8")
    tree = ast.parse(source)

    candidates: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id == var_name:
                            candidates.append((stmt.lineno, _extract_frozenset_or_set_literal(stmt.value)))
                elif isinstance(stmt, ast.ImportFrom) and stmt.module == "event_outcome_semantics":
                    for alias in stmt.names:
                        local_name = alias.asname or alias.name
                        if local_name == var_name:
                            candidates.append((stmt.lineno, _resolve_canonical_vocab(alias.name)))
    if not candidates:
        raise AssertionError(
            f"{var_name} not found in payload.{func_name} "
            "(neither a local literal assignment nor a "
            "`from event_outcome_semantics import ...`)"
        )
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def _get_payload_failure_statuses() -> FrozenSet[str]:
    """Extract FAILURE_STATUSES used by payload._update_confidence_from_receipt.

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
    return _extract_payload_vocab("FAILURE_STATUSES")


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
    """Extract SUCCESS_STATUSES used by payload._update_confidence_from_receipt.

    See _extract_payload_vocab for the two recognized forms (local literal or
    `from event_outcome_semantics import ...`).
    """
    return _extract_payload_vocab("SUCCESS_STATUSES")


# Documented structural differences in SUCCESS_STATUSES across modules:
#
#   empty status ("") is NOT canonical success:
#     event_outcome_semantics dropped "" from SUCCESS_STATUSES — absence is not
#     a success claim (resolve_status_category returns "no_signal"). drain still
#     carries "" as a pre-existing read-side classifier tolerance this
#     consolidation leaves untouched (check_active_drain is a read-side
#     consumer, not the write path the consolidation governs). digest omits ""
#     because an empty status is not meaningful after event-type gating.
#
# The canonical core that ALL three must carry:
_SUCCESS_CORE = frozenset({"success", "completed", "complete", "ok", "done"})

# drain-known extra vs canonical (documented acceptable difference):
_DRAIN_SUCCESS_KNOWN_EXTRA: FrozenSet[str] = frozenset({""})
# digest-known omissions vs drain (documented acceptable gap):
_DIGEST_SUCCESS_KNOWN_OMISSIONS: FrozenSet[str] = frozenset({""})


class TestSuccessVocabCrossModuleSync:
    """Structural drift tests between SUCCESS_STATUSES sets in drain / digest / payload.

    The canonical reference is payload.SUCCESS_STATUSES (imported from
    event_outcome_semantics, the single generated source). It is exactly the
    core {success, completed, complete, ok, done}.
    - drain may carry "" as a documented extra (read-side classifier tolerance).
    - digest omits "" (documented acceptable difference).
    - All three must contain the common core.
    """

    def test_payload_success_set_is_the_canonical_core(self):
        """payload.SUCCESS_STATUSES is exactly the canonical core (no "")."""
        payload = _get_payload_success_statuses()
        assert payload == _SUCCESS_CORE, (
            f"payload.SUCCESS_STATUSES must be exactly the canonical success core.\n"
            f"  expected: {sorted(_SUCCESS_CORE)}\n"
            f"  actual  : {sorted(payload)}"
        )

    def test_drain_success_carries_only_empty_as_extra(self):
        """drain may differ from canonical only by the documented "" extra."""
        drain = _get_drain_success_statuses()
        assert drain - _SUCCESS_CORE == _DRAIN_SUCCESS_KNOWN_EXTRA, (
            f"check_active_drain.SUCCESS_STATUSES has an undocumented extra vs canonical core:\n"
            f"  drain only: {sorted(drain - _SUCCESS_CORE)}"
        )
        assert _SUCCESS_CORE - drain == frozenset(), (
            f"check_active_drain.SUCCESS_STATUSES is missing canonical core entries: "
            f"{sorted(_SUCCESS_CORE - drain)}"
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
# B3) Regression coverage for the payload extractor's canonical-import form.
#
# OI-1148 turned payload.py from a hand-copied-literal consumer into an
# IMPORTER of event_outcome_semantics. _extract_payload_vocab (above) learned
# to resolve that import shape against the live canonical module. These tests
# pin both halves of that behaviour so a future edit to the extractor can't
# silently regress either one:
#   - it must actually resolve the import (not just stop raising "not found"),
#   - it must still catch a hand-copied literal that has drifted, because
#     that drift-detection is the entire reason this guard exists.
# ---------------------------------------------------------------------------

class TestPayloadCanonicalImportRecognition:
    def test_extractor_resolves_canonical_import_to_live_value(self):
        """payload.py's `from event_outcome_semantics import FAILURE_STATUSES`
        must resolve to the actual canonical value, not merely stop raising.
        """
        failure = _get_payload_failure_statuses()
        success = _get_payload_success_statuses()
        assert failure == _resolve_canonical_vocab("FAILURE_STATUSES")
        assert success == _resolve_canonical_vocab("SUCCESS_STATUSES")

    def test_diverging_local_copy_still_fails_common_core_check(self):
        """Regression proof for the guard's actual purpose: if a future edit
        reintroduces a hand-copied local literal in payload.py that drops a
        required core status, the sync guard must still fail.

        We synthesize a payload.py body carrying a diverging LOCAL literal
        (rather than the canonical import) and feed it through the same
        extractor the real tests use, then reproduce the common-core
        assertion from TestVocabCrossModuleSync.test_common_core_present_in_all_four
        inline. If the extractor ever regressed to silently preferring/ignoring
        a local override, this would go green when it must stay red.
        """
        diverging_source = '''
def _update_confidence_from_receipt(receipt):
    FAILURE_STATUSES = frozenset({"failed", "error"})  # missing "contract_invalid", "blocked", "failure"
    status = str(receipt.get("status", "")).lower()
    if status in FAILURE_STATUSES:
        pass
'''
        extracted = _extract_payload_vocab("FAILURE_STATUSES", source=diverging_source)
        assert extracted == frozenset({"failed", "error"})
        assert extracted != _resolve_canonical_vocab("FAILURE_STATUSES")

        core = frozenset({"failed", "failure", "error", "blocked", "contract_invalid"})
        with pytest.raises(AssertionError):
            missing = core - extracted
            assert not missing, (
                f"payload: missing core failure statuses (gate-F2): {sorted(missing)}"
            )

    def test_extractor_raises_when_neither_form_present(self):
        """If a function carries neither a local literal nor the canonical
        import, the extractor must fail loudly (not silently return an empty
        set, which would make every drift check vacuously pass).
        """
        empty_source = '''
def _update_confidence_from_receipt(receipt):
    return None
'''
        with pytest.raises(AssertionError):
            _extract_payload_vocab("FAILURE_STATUSES", source=empty_source)


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
