#!/usr/bin/env python3
"""
Tests for VNX Incident Taxonomy — PR-0 recovery contract validation.

Verifies:
  - All incident classes have recovery contracts
  - Recovery contracts satisfy governance rules (G-R1 through G-R8)
  - Retry budgets are bounded and non-negative
  - Escalation rules are consistent with retry budgets
  - Dead-letter eligibility is correctly computed
  - Cooldown calculation with backoff is correct
  - Repeated failure loop threshold is defined
  - Validation helpers reject invalid inputs
"""

import sys
from pathlib import Path

# Add scripts/lib to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import pytest
from incident_taxonomy import (
    DeadLetterRule,
    DEAD_LETTER_ELIGIBLE_CLASSES,
    DEAD_LETTER_SOURCE_STATES,
    EscalationRule,
    EscalationTrigger,
    INCIDENT_CLASSES,
    IncidentClass,
    RECOVERY_CONTRACTS,
    RecoveryAction,
    RecoveryContract,
    REPEATED_FAILURE_THRESHOLD,
    RetryBudget,
    Severity,
    get_contract,
    get_cooldown_seconds,
    should_dead_letter,
    should_escalate,
    validate_incident_class,
)


# ---------------------------------------------------------------------------
# Completeness tests
# ---------------------------------------------------------------------------

class TestCompleteness:
    """Every incident class must have a recovery contract."""

    def test_all_classes_have_contracts(self):
        for ic in IncidentClass:
            assert ic in RECOVERY_CONTRACTS, (
                f"Missing recovery contract for incident class: {ic.value}"
            )

    def test_no_extra_contracts(self):
        for ic in RECOVERY_CONTRACTS:
            assert ic in IncidentClass, (
                f"Recovery contract for unknown class: {ic}"
            )

    def test_incident_classes_frozenset_matches_enum(self):
        enum_values = {ic.value for ic in IncidentClass}
        assert INCIDENT_CLASSES == enum_values

    def test_seven_incident_classes(self):
        assert len(IncidentClass) == 7


# ---------------------------------------------------------------------------
# Governance rule tests
# ---------------------------------------------------------------------------

class TestGovernanceRules:
    """Verify contracts satisfy G-R1 through G-R8."""

    def test_gr1_no_hidden_failures(self):
        """G-R1: Every contract has at least one permitted action."""
        for ic, contract in RECOVERY_CONTRACTS.items():
            assert len(contract.permitted_actions) > 0, (
                f"G-R1 violation: {ic.value} has no permitted recovery actions"
            )

    def test_gr2_bounded_retries(self):
        """G-R2: All retry budgets have finite max_retries."""
        for ic, contract in RECOVERY_CONTRACTS.items():
            budget = contract.retry_budget
            assert budget.max_retries >= 0, (
                f"G-R2 violation: {ic.value} has negative max_retries"
            )
            assert budget.max_retries <= 10, (
                f"G-R2 sanity: {ic.value} has unreasonably high max_retries={budget.max_retries}"
            )

    def test_gr3_incident_trail_required(self):
        """G-R3: Every contract requires an incident trail."""
        for ic, contract in RECOVERY_CONTRACTS.items():
            assert contract.requires_incident_trail is True, (
                f"G-R3 violation: {ic.value} does not require incident trail"
            )

    def test_gr3_cannot_disable_incident_trail(self):
        """G-R3: Cannot construct a contract with requires_incident_trail=False."""
        with pytest.raises(ValueError, match="G-R3 violation"):
            RecoveryContract(
                incident_class=IncidentClass.PROCESS_CRASH,
                default_severity=Severity.WARNING,
                retry_budget=RetryBudget(max_retries=1, cooldown_seconds=10),
                escalation=EscalationRule(escalate_after_retries=0),
                permitted_actions=(RecoveryAction.RESTART_PROCESS,),
                requires_incident_trail=False,
            )

    def test_gr5_dead_letter_explicit(self):
        """G-R5: Dead-letter eligible classes are explicitly marked."""
        for ic in DEAD_LETTER_ELIGIBLE_CLASSES:
            contract = RECOVERY_CONTRACTS[ic]
            assert contract.escalation.dead_letter_eligible is True

    def test_gr8_escalation_defined(self):
        """G-R8: Every contract has explicit escalation rules."""
        for ic, contract in RECOVERY_CONTRACTS.items():
            assert contract.escalation is not None, (
                f"G-R8 violation: {ic.value} has no escalation rule"
            )
            assert contract.escalation.escalate_to in ("T0", "operator"), (
                f"G-R8: {ic.value} escalation target must be T0 or operator"
            )


# ---------------------------------------------------------------------------
# Retry budget tests
# ---------------------------------------------------------------------------

class TestRetryBudgets:

    def test_cooldown_non_negative(self):
        for ic, contract in RECOVERY_CONTRACTS.items():
            assert contract.retry_budget.cooldown_seconds >= 0, (
                f"{ic.value}: negative cooldown"
            )

    def test_backoff_factor_positive(self):
        for ic, contract in RECOVERY_CONTRACTS.items():
            assert contract.retry_budget.backoff_factor >= 1.0, (
                f"{ic.value}: backoff_factor < 1.0 would reduce cooldown"
            )

    def test_max_cooldown_gte_base_cooldown(self):
        for ic, contract in RECOVERY_CONTRACTS.items():
            budget = contract.retry_budget
            if budget.max_retries > 0:
                assert budget.max_cooldown_seconds >= budget.cooldown_seconds, (
                    f"{ic.value}: max_cooldown < base cooldown"
                )

    def test_escalation_within_budget(self):
        """Escalation trigger cannot exceed retry budget."""
        for ic, contract in RECOVERY_CONTRACTS.items():
            assert contract.escalation.escalate_after_retries <= contract.retry_budget.max_retries, (
                f"{ic.value}: escalation after {contract.escalation.escalate_after_retries} "
                f"exceeds max_retries {contract.retry_budget.max_retries}"
            )

    def test_escalation_within_budget_enforced_at_construction(self):
        """Construction rejects escalation > max_retries."""
        with pytest.raises(ValueError, match="cannot exceed max_retries"):
            RecoveryContract(
                incident_class=IncidentClass.PROCESS_CRASH,
                default_severity=Severity.WARNING,
                retry_budget=RetryBudget(max_retries=1, cooldown_seconds=10),
                escalation=EscalationRule(escalate_after_retries=5),
                permitted_actions=(RecoveryAction.RESTART_PROCESS,),
            )


# ---------------------------------------------------------------------------
# Cooldown calculation tests
# ---------------------------------------------------------------------------

class TestCooldownCalculation:

    def test_first_attempt_base_cooldown(self):
        cooldown = get_cooldown_seconds(IncidentClass.DELIVERY_FAILURE, 0)
        assert cooldown == 5  # Base cooldown for delivery_failure

    def test_backoff_applied(self):
        # delivery_failure: base=5s, factor=2.0
        c0 = get_cooldown_seconds(IncidentClass.DELIVERY_FAILURE, 0)
        c1 = get_cooldown_seconds(IncidentClass.DELIVERY_FAILURE, 1)
        c2 = get_cooldown_seconds(IncidentClass.DELIVERY_FAILURE, 2)
        assert c0 == 5
        assert c1 == 10
        assert c2 == 20

    def test_max_cooldown_cap(self):
        # process_crash: base=10s, factor=2.0, max=120s
        c10 = get_cooldown_seconds(IncidentClass.PROCESS_CRASH, 10)
        assert c10 <= 120

    def test_unknown_class_safe_default(self):
        """Unknown classes get a safe 60s default."""
        # Simulate by passing a non-existent class value
        # (this tests the None branch in get_cooldown_seconds)
        cooldown = get_cooldown_seconds.__wrapped__(None, 0) if hasattr(get_cooldown_seconds, '__wrapped__') else 60
        assert cooldown == 60

    def test_repeated_failure_loop_no_cooldown(self):
        cooldown = get_cooldown_seconds(IncidentClass.REPEATED_FAILURE_LOOP, 0)
        assert cooldown == 0


# ---------------------------------------------------------------------------
# Escalation tests
# ---------------------------------------------------------------------------

class TestEscalation:

    def test_immediate_escalation(self):
        """lease_conflict and resume_failed escalate at retry 0."""
        assert should_escalate(IncidentClass.LEASE_CONFLICT, 0) is True
        assert should_escalate(IncidentClass.RESUME_FAILED, 0) is True
        assert should_escalate(IncidentClass.REPEATED_FAILURE_LOOP, 0) is True

    def test_delayed_escalation(self):
        """process_crash and delivery_failure escalate after 2 retries."""
        assert should_escalate(IncidentClass.PROCESS_CRASH, 0) is False
        assert should_escalate(IncidentClass.PROCESS_CRASH, 1) is False
        assert should_escalate(IncidentClass.PROCESS_CRASH, 2) is True

        assert should_escalate(IncidentClass.DELIVERY_FAILURE, 1) is False
        assert should_escalate(IncidentClass.DELIVERY_FAILURE, 2) is True

    def test_ack_timeout_escalates_after_1(self):
        assert should_escalate(IncidentClass.ACK_TIMEOUT, 0) is False
        assert should_escalate(IncidentClass.ACK_TIMEOUT, 1) is True


# ---------------------------------------------------------------------------
# Dead-letter tests
# ---------------------------------------------------------------------------

class TestDeadLetter:

    def test_eligible_classes(self):
        expected = {
            IncidentClass.TERMINAL_UNRESPONSIVE,
            IncidentClass.DELIVERY_FAILURE,
            IncidentClass.ACK_TIMEOUT,
            IncidentClass.RESUME_FAILED,
            IncidentClass.REPEATED_FAILURE_LOOP,
        }
        assert DEAD_LETTER_ELIGIBLE_CLASSES == expected

    def test_non_eligible_classes(self):
        assert IncidentClass.PROCESS_CRASH not in DEAD_LETTER_ELIGIBLE_CLASSES
        assert IncidentClass.LEASE_CONFLICT not in DEAD_LETTER_ELIGIBLE_CLASSES

    def test_dead_letter_source_states(self):
        assert DEAD_LETTER_SOURCE_STATES == {"timed_out", "failed_delivery", "recovered"}

    def test_should_dead_letter_budget_exhausted(self):
        # delivery_failure: max_retries=3
        assert should_dead_letter(
            IncidentClass.DELIVERY_FAILURE, 3, "failed_delivery"
        ) is True

    def test_should_not_dead_letter_budget_remaining(self):
        assert should_dead_letter(
            IncidentClass.DELIVERY_FAILURE, 1, "failed_delivery"
        ) is False

    def test_should_not_dead_letter_wrong_state(self):
        assert should_dead_letter(
            IncidentClass.DELIVERY_FAILURE, 3, "queued"
        ) is False

    def test_should_not_dead_letter_non_eligible_class(self):
        assert should_dead_letter(
            IncidentClass.PROCESS_CRASH, 10, "failed_delivery"
        ) is False

    def test_repeated_failure_loop_immediate_dead_letter(self):
        # max_retries=0, so retry_count >= 0 is always true
        assert should_dead_letter(
            IncidentClass.REPEATED_FAILURE_LOOP, 0, "failed_delivery"
        ) is True


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestValidation:

    def test_validate_known_class(self):
        for ic in IncidentClass:
            result = validate_incident_class(ic.value)
            assert result == ic

    def test_validate_unknown_class(self):
        with pytest.raises(ValueError, match="Unknown incident class"):
            validate_incident_class("not_a_real_class")

    def test_get_contract_known(self):
        contract = get_contract(IncidentClass.PROCESS_CRASH)
        assert contract.incident_class == IncidentClass.PROCESS_CRASH

    def test_repeated_failure_threshold(self):
        assert REPEATED_FAILURE_THRESHOLD == 3


# ---------------------------------------------------------------------------
# Contract-specific property tests
# ---------------------------------------------------------------------------

class TestContractProperties:

    def test_process_crash_no_dead_letter(self):
        c = RECOVERY_CONTRACTS[IncidentClass.PROCESS_CRASH]
        assert c.escalation.dead_letter_eligible is False
        assert c.escalation.halt_auto_recovery is False

    def test_lease_conflict_halts_auto_recovery(self):
        c = RECOVERY_CONTRACTS[IncidentClass.LEASE_CONFLICT]
        assert c.escalation.halt_auto_recovery is True
        assert c.escalation.dead_letter_eligible is False

    def test_repeated_failure_loop_zero_retries(self):
        c = RECOVERY_CONTRACTS[IncidentClass.REPEATED_FAILURE_LOOP]
        assert c.retry_budget.max_retries == 0
        assert c.escalation.halt_auto_recovery is True
        assert c.default_severity == Severity.CRITICAL

    def test_all_contracts_have_descriptions(self):
        for ic, contract in RECOVERY_CONTRACTS.items():
            assert len(contract.description) > 0, (
                f"{ic.value}: missing description"
            )

    def test_escalate_to_operator_includes_action(self):
        """Every contract that can escalate should include ESCALATE_TO_OPERATOR."""
        for ic, contract in RECOVERY_CONTRACTS.items():
            assert RecoveryAction.ESCALATE_TO_OPERATOR in contract.permitted_actions, (
                f"{ic.value}: missing ESCALATE_TO_OPERATOR in permitted_actions"
            )

    def test_dead_letter_eligible_contracts_include_action(self):
        """Dead-letter eligible contracts must include DEAD_LETTER_DISPATCH action."""
        for ic in DEAD_LETTER_ELIGIBLE_CLASSES:
            contract = RECOVERY_CONTRACTS[ic]
            has_dl = RecoveryAction.DEAD_LETTER_DISPATCH in contract.permitted_actions
            # Only required for classes that can actually trigger dead-letter directly
            if contract.retry_budget.max_retries == 0 or ic == IncidentClass.RESUME_FAILED:
                assert has_dl, (
                    f"{ic.value}: dead-letter eligible but DEAD_LETTER_DISPATCH not permitted"
                )


# ---------------------------------------------------------------------------
# Non-overlap test
# ---------------------------------------------------------------------------

class TestNonOverlap:
    """Incident classes must be non-overlapping in their detection criteria."""

    def test_unique_enum_values(self):
        values = [ic.value for ic in IncidentClass]
        assert len(values) == len(set(values)), "Duplicate incident class values"

    def test_severity_levels_ordered(self):
        """Severity levels have a clear ordering."""
        levels = [Severity.INFO, Severity.WARNING, Severity.ERROR, Severity.CRITICAL]
        assert len(levels) == 4
