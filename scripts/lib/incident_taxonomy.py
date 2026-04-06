#!/usr/bin/env python3
"""
VNX Incident Taxonomy — Canonical incident classes, severity, and recovery contracts.

PR-0 deliverable: locks down recovery language before implementation PRs diverge.

This module is the programmatic source of truth for FP-B. Later PRs (PR-1 through
PR-5) import these definitions rather than inventing local copies.

Design rules:
  - Incident classes are non-overlapping: every runtime failure maps to exactly one class.
  - Recovery contracts are bounded: retry limits, cooldown windows, and escalation
    thresholds are explicit.
  - Dead-letter entry is explicit: dispatches that cannot safely resume must stop in
    a reviewable terminal state (G-R5).
  - Every recovery action must emit an incident trail (G-R3).

Governance references:
  G-R1: No automatic recovery may hide a failure class
  G-R2: Retry budgets are mandatory — no infinite restart or resend loops
  G-R3: Every recovery action must emit an incident trail
  G-R5: Dead-letter is explicit
  G-R8: Final recovery authority remains governance-aware
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Optional


# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    """Incident severity determines urgency and escalation speed.

    INFO:     Observable event, no recovery needed. Logged for audit.
    WARNING:  Recoverable automatically within budget. Operator notified.
    ERROR:    Recovery attempted but operator attention likely required.
    CRITICAL: Immediate operator intervention required. Auto-retry halted.
    """
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Incident classes
# ---------------------------------------------------------------------------

class IncidentClass(str, Enum):
    """Canonical incident classes for FP-B runtime.

    Each class maps to exactly one failure domain. The supervisor, reconciler,
    and operator commands use these classes to select recovery actions.

    Classes are ordered from process-level (infrastructure) through
    workflow-level (dispatch coordination) to compound (meta-failures).
    """

    # -- Process-level incidents --
    PROCESS_CRASH = "process_crash"
    """A supervised process (worker, dispatcher, adapter) terminated unexpectedly.
    Detected by: PID check, exit-code observation, or process monitor.
    Scope: single process on a single terminal."""

    TERMINAL_UNRESPONSIVE = "terminal_unresponsive"
    """A terminal (tmux pane or worker session) is not responding to health checks
    or heartbeat renewal, but the process may still exist.
    Detected by: heartbeat timeout, tmux pane query failure.
    Scope: single terminal."""

    # -- Delivery-level incidents --
    DELIVERY_FAILURE = "delivery_failure"
    """Dispatch bundle could not be delivered to the target terminal.
    Detected by: tmux send-keys error, adapter pane-not-found, transport timeout.
    Scope: single dispatch attempt."""

    ACK_TIMEOUT = "ack_timeout"
    """Dispatch was delivered but the worker did not acknowledge receipt within
    the expected window.
    Detected by: dispatch stuck in 'delivering' or 'accepted' past threshold.
    Scope: single dispatch attempt."""

    # -- Ownership-level incidents --
    LEASE_CONFLICT = "lease_conflict"
    """A lease operation failed due to generation mismatch, concurrent claim,
    or state that does not permit the requested transition.
    Detected by: generation mismatch error, InvalidTransitionError on lease ops.
    Scope: single terminal lease."""

    # -- Workflow-level incidents --
    RESUME_FAILED = "resume_failed"
    """A dispatch that was recovered and re-queued failed again on its next
    attempt. The dispatch state allows retry but the failure persisted.
    Detected by: second+ failure on a previously-recovered dispatch.
    Scope: single dispatch across multiple attempts."""

    REPEATED_FAILURE_LOOP = "repeated_failure_loop"
    """A dispatch or terminal has hit the same failure class multiple times
    within its retry budget, indicating a systemic issue rather than a transient
    fault. This is a compound incident that gates further automatic recovery.
    Detected by: attempt_count >= threshold with same failure class repeating.
    Scope: single dispatch or terminal across its retry history."""


# Canonical set for validation
INCIDENT_CLASSES: FrozenSet[str] = frozenset(ic.value for ic in IncidentClass)


# ---------------------------------------------------------------------------
# Recovery contracts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryBudget:
    """Bounded retry policy for a single incident class.

    Attributes:
        max_retries: Maximum automatic retry attempts before escalation.
                     0 means no automatic retry (immediate escalation).
        cooldown_seconds: Minimum wait between retry attempts.
        backoff_factor: Multiplier applied to cooldown after each attempt.
                        1.0 = fixed cooldown, 2.0 = exponential doubling.
        max_cooldown_seconds: Upper bound on cooldown after backoff.
    """
    max_retries: int
    cooldown_seconds: int
    backoff_factor: float = 1.0
    max_cooldown_seconds: int = 600


@dataclass(frozen=True)
class EscalationRule:
    """When and how to escalate an incident beyond automatic recovery.

    Attributes:
        escalate_after_retries: Escalate to operator/T0 after this many retries.
                                Must be <= RetryBudget.max_retries.
        escalate_to: Target for escalation notification.
        halt_auto_recovery: If True, stop all automatic recovery on escalation.
        dead_letter_eligible: If True, dispatch enters dead_letter on budget exhaustion.
    """
    escalate_after_retries: int
    escalate_to: str = "T0"
    halt_auto_recovery: bool = False
    dead_letter_eligible: bool = False


@dataclass(frozen=True)
class RecoveryContract:
    """Complete recovery specification for one incident class.

    Each incident class has exactly one RecoveryContract. The contract specifies:
    - Default severity (can be elevated by context)
    - Retry budget (bounded, with cooldown and backoff)
    - Escalation rules
    - Which recovery actions are permitted
    - Whether the incident class can trigger dead-letter

    Governance:
      G-R1: severity + permitted_actions ensure no failure class is hidden
      G-R2: retry_budget enforces bounded retries
      G-R3: requires_incident_trail is always True (enforced at construction)
      G-R5: dead_letter_eligible marks explicit dead-letter entry
      G-R8: escalation keeps governance authority explicit
    """
    incident_class: IncidentClass
    default_severity: Severity
    retry_budget: RetryBudget
    escalation: EscalationRule
    permitted_actions: tuple  # Tuple of action name strings
    requires_incident_trail: bool = True  # G-R3: always True, not configurable
    description: str = ""

    def __post_init__(self):
        if not self.requires_incident_trail:
            raise ValueError(
                f"G-R3 violation: requires_incident_trail must be True "
                f"for incident class {self.incident_class.value}"
            )
        if self.escalation.escalate_after_retries > self.retry_budget.max_retries:
            raise ValueError(
                f"escalate_after_retries ({self.escalation.escalate_after_retries}) "
                f"cannot exceed max_retries ({self.retry_budget.max_retries}) "
                f"for incident class {self.incident_class.value}"
            )


# ---------------------------------------------------------------------------
# Permitted recovery actions (vocabulary for contracts)
# ---------------------------------------------------------------------------

class RecoveryAction(str, Enum):
    """Canonical recovery actions that contracts may permit."""
    RESTART_PROCESS = "restart_process"
    REDELIVER_DISPATCH = "redeliver_dispatch"
    EXPIRE_LEASE = "expire_lease"
    RECOVER_LEASE = "recover_lease"
    REMAP_PANE = "remap_pane"
    TIMEOUT_DISPATCH = "timeout_dispatch"
    RECOVER_DISPATCH = "recover_dispatch"
    DEAD_LETTER_DISPATCH = "dead_letter_dispatch"
    ESCALATE_TO_OPERATOR = "escalate_to_operator"
    HALT_TERMINAL = "halt_terminal"


# ---------------------------------------------------------------------------
# Canonical recovery contracts (one per incident class)
# ---------------------------------------------------------------------------

RECOVERY_CONTRACTS: Dict[IncidentClass, RecoveryContract] = {

    IncidentClass.PROCESS_CRASH: RecoveryContract(
        incident_class=IncidentClass.PROCESS_CRASH,
        default_severity=Severity.WARNING,
        retry_budget=RetryBudget(
            max_retries=3,
            cooldown_seconds=10,
            backoff_factor=2.0,
            max_cooldown_seconds=120,
        ),
        escalation=EscalationRule(
            escalate_after_retries=2,
            escalate_to="T0",
            halt_auto_recovery=False,
            dead_letter_eligible=False,
        ),
        permitted_actions=(
            RecoveryAction.RESTART_PROCESS,
            RecoveryAction.EXPIRE_LEASE,
            RecoveryAction.ESCALATE_TO_OPERATOR,
        ),
        description=(
            "Supervised process terminated unexpectedly. "
            "Restart up to 3 times with exponential backoff. "
            "Escalate to T0 after 2nd failure. "
            "Process crash alone does not dead-letter the dispatch — "
            "dispatch state is evaluated separately."
        ),
    ),

    IncidentClass.TERMINAL_UNRESPONSIVE: RecoveryContract(
        incident_class=IncidentClass.TERMINAL_UNRESPONSIVE,
        default_severity=Severity.ERROR,
        retry_budget=RetryBudget(
            max_retries=2,
            cooldown_seconds=30,
            backoff_factor=2.0,
            max_cooldown_seconds=120,
        ),
        escalation=EscalationRule(
            escalate_after_retries=1,
            escalate_to="T0",
            halt_auto_recovery=False,
            dead_letter_eligible=True,
        ),
        permitted_actions=(
            RecoveryAction.EXPIRE_LEASE,
            RecoveryAction.RECOVER_LEASE,
            RecoveryAction.REMAP_PANE,
            RecoveryAction.HALT_TERMINAL,
            RecoveryAction.ESCALATE_TO_OPERATOR,
        ),
        description=(
            "Terminal not responding to health probes. "
            "Expire lease after first probe failure (30s cooldown). "
            "Attempt pane remap once. Escalate after 1st retry. "
            "If terminal stays unresponsive after 2 attempts, "
            "associated dispatches become dead-letter eligible."
        ),
    ),

    IncidentClass.DELIVERY_FAILURE: RecoveryContract(
        incident_class=IncidentClass.DELIVERY_FAILURE,
        default_severity=Severity.WARNING,
        retry_budget=RetryBudget(
            max_retries=3,
            cooldown_seconds=5,
            backoff_factor=2.0,
            max_cooldown_seconds=60,
        ),
        escalation=EscalationRule(
            escalate_after_retries=2,
            escalate_to="T0",
            halt_auto_recovery=False,
            dead_letter_eligible=True,
        ),
        permitted_actions=(
            RecoveryAction.REDELIVER_DISPATCH,
            RecoveryAction.REMAP_PANE,
            RecoveryAction.TIMEOUT_DISPATCH,
            RecoveryAction.ESCALATE_TO_OPERATOR,
        ),
        description=(
            "Dispatch delivery transport failed (tmux error, pane gone, etc.). "
            "Retry delivery up to 3 times with backoff. "
            "Attempt pane remap if pane-not-found. "
            "Escalate after 2nd failure. "
            "Dead-letter after budget exhaustion."
        ),
    ),

    IncidentClass.ACK_TIMEOUT: RecoveryContract(
        incident_class=IncidentClass.ACK_TIMEOUT,
        default_severity=Severity.WARNING,
        retry_budget=RetryBudget(
            max_retries=2,
            cooldown_seconds=30,
            backoff_factor=1.5,
            max_cooldown_seconds=120,
        ),
        escalation=EscalationRule(
            escalate_after_retries=1,
            escalate_to="T0",
            halt_auto_recovery=False,
            dead_letter_eligible=True,
        ),
        permitted_actions=(
            RecoveryAction.REDELIVER_DISPATCH,
            RecoveryAction.TIMEOUT_DISPATCH,
            RecoveryAction.RECOVER_DISPATCH,
            RecoveryAction.ESCALATE_TO_OPERATOR,
        ),
        description=(
            "Dispatch delivered but worker did not ACK within deadline. "
            "Retry delivery once after 30s cooldown. "
            "Escalate immediately after 1st timeout. "
            "Dead-letter after budget exhaustion. "
            "Must verify terminal is responsive before re-delivery."
        ),
    ),

    IncidentClass.LEASE_CONFLICT: RecoveryContract(
        incident_class=IncidentClass.LEASE_CONFLICT,
        default_severity=Severity.ERROR,
        retry_budget=RetryBudget(
            max_retries=1,
            cooldown_seconds=15,
            backoff_factor=1.0,
            max_cooldown_seconds=15,
        ),
        escalation=EscalationRule(
            escalate_after_retries=0,
            escalate_to="T0",
            halt_auto_recovery=True,
            dead_letter_eligible=False,
        ),
        permitted_actions=(
            RecoveryAction.EXPIRE_LEASE,
            RecoveryAction.RECOVER_LEASE,
            RecoveryAction.ESCALATE_TO_OPERATOR,
        ),
        description=(
            "Lease operation failed due to generation mismatch or invalid transition. "
            "Escalate to T0 immediately (before any retry). "
            "One reconciliation attempt permitted to resolve stale lease. "
            "Automatic recovery halted until operator confirms resolution. "
            "Dispatch is NOT dead-lettered — ownership is resolved first."
        ),
    ),

    IncidentClass.RESUME_FAILED: RecoveryContract(
        incident_class=IncidentClass.RESUME_FAILED,
        default_severity=Severity.ERROR,
        retry_budget=RetryBudget(
            max_retries=1,
            cooldown_seconds=60,
            backoff_factor=1.0,
            max_cooldown_seconds=60,
        ),
        escalation=EscalationRule(
            escalate_after_retries=0,
            escalate_to="T0",
            halt_auto_recovery=False,
            dead_letter_eligible=True,
        ),
        permitted_actions=(
            RecoveryAction.REDELIVER_DISPATCH,
            RecoveryAction.DEAD_LETTER_DISPATCH,
            RecoveryAction.ESCALATE_TO_OPERATOR,
        ),
        description=(
            "Previously-recovered dispatch failed again on re-delivery. "
            "Escalate to T0 immediately. "
            "One more retry permitted (60s cooldown). "
            "If retry fails, dispatch enters dead-letter. "
            "Indicates potential systemic issue — operator must assess root cause."
        ),
    ),

    IncidentClass.REPEATED_FAILURE_LOOP: RecoveryContract(
        incident_class=IncidentClass.REPEATED_FAILURE_LOOP,
        default_severity=Severity.CRITICAL,
        retry_budget=RetryBudget(
            max_retries=0,
            cooldown_seconds=0,
            backoff_factor=1.0,
            max_cooldown_seconds=0,
        ),
        escalation=EscalationRule(
            escalate_after_retries=0,
            escalate_to="T0",
            halt_auto_recovery=True,
            dead_letter_eligible=True,
        ),
        permitted_actions=(
            RecoveryAction.DEAD_LETTER_DISPATCH,
            RecoveryAction.HALT_TERMINAL,
            RecoveryAction.ESCALATE_TO_OPERATOR,
        ),
        description=(
            "Compound incident: dispatch or terminal hit the same failure class "
            "multiple times, exhausting its retry budget. "
            "All automatic recovery halted immediately. "
            "Dispatch enters dead-letter. Terminal may be halted. "
            "Operator/T0 must investigate before any further action. "
            "This is the circuit-breaker for the runtime."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Dead-letter rules
# ---------------------------------------------------------------------------

# Incident classes that can trigger dead-letter entry
DEAD_LETTER_ELIGIBLE_CLASSES: FrozenSet[IncidentClass] = frozenset(
    ic for ic, contract in RECOVERY_CONTRACTS.items()
    if contract.escalation.dead_letter_eligible
)

# Dispatch states from which dead-letter transition is valid
DEAD_LETTER_SOURCE_STATES: FrozenSet[str] = frozenset({
    "timed_out",
    "failed_delivery",
    "recovered",  # recovered but then failed again -> dead_letter
})


@dataclass(frozen=True)
class DeadLetterRule:
    """Conditions under which a dispatch enters dead-letter state.

    A dispatch enters dead-letter when ALL of:
    1. Its current state is in DEAD_LETTER_SOURCE_STATES
    2. The triggering incident class is in DEAD_LETTER_ELIGIBLE_CLASSES
    3. The retry budget for that incident class is exhausted
    4. The escalation rule permits dead-letter

    Dead-letter is a terminal state for automatic recovery but NOT for
    operator intervention. T0 can review and manually re-queue.
    """
    dispatch_id: str
    incident_class: IncidentClass
    reason: str
    attempt_count: int
    max_retries: int
    terminal_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Escalation triggers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EscalationTrigger:
    """Conditions that trigger escalation to T0/operator.

    Escalation does NOT necessarily halt recovery — see halt_auto_recovery
    in the contract's EscalationRule.
    """
    incident_class: IncidentClass
    severity: Severity
    retry_count: int
    budget_exhausted: bool
    dispatch_id: Optional[str] = None
    terminal_id: Optional[str] = None
    reason: str = ""


def should_escalate(
    incident_class: IncidentClass,
    retry_count: int,
) -> bool:
    """Check whether an incident at the given retry count requires escalation."""
    contract = RECOVERY_CONTRACTS.get(incident_class)
    if contract is None:
        return True  # Unknown class -> always escalate
    return retry_count >= contract.escalation.escalate_after_retries


def should_dead_letter(
    incident_class: IncidentClass,
    retry_count: int,
    dispatch_state: str,
) -> bool:
    """Check whether a dispatch should enter dead-letter state."""
    if incident_class not in DEAD_LETTER_ELIGIBLE_CLASSES:
        return False
    if dispatch_state not in DEAD_LETTER_SOURCE_STATES:
        return False
    contract = RECOVERY_CONTRACTS.get(incident_class)
    if contract is None:
        return False
    return retry_count >= contract.retry_budget.max_retries


def get_cooldown_seconds(
    incident_class: IncidentClass,
    retry_count: int,
) -> int:
    """Calculate cooldown for the next retry attempt, applying backoff."""
    contract = RECOVERY_CONTRACTS.get(incident_class)
    if contract is None:
        return 60  # Safe default for unknown classes
    budget = contract.retry_budget
    cooldown = budget.cooldown_seconds * (budget.backoff_factor ** retry_count)
    return min(int(cooldown), budget.max_cooldown_seconds)


# ---------------------------------------------------------------------------
# Repeated failure loop detection threshold
# ---------------------------------------------------------------------------

REPEATED_FAILURE_THRESHOLD = 3
"""Number of same-class failures within a dispatch's lifetime that triggers
REPEATED_FAILURE_LOOP classification. This is the circuit-breaker threshold."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_incident_class(value: str) -> IncidentClass:
    """Validate and return an IncidentClass from a string value.

    Raises ValueError if the value is not a recognized incident class.
    """
    try:
        return IncidentClass(value)
    except ValueError:
        raise ValueError(
            f"Unknown incident class: {value!r}. "
            f"Valid classes: {sorted(INCIDENT_CLASSES)}"
        )


def get_contract(incident_class: IncidentClass) -> RecoveryContract:
    """Return the recovery contract for an incident class.

    Raises KeyError if no contract is defined (should never happen if
    RECOVERY_CONTRACTS is complete).
    """
    contract = RECOVERY_CONTRACTS.get(incident_class)
    if contract is None:
        raise KeyError(
            f"No recovery contract defined for incident class: {incident_class.value}"
        )
    return contract
