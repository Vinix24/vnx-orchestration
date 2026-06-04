# Task 07 — Review state-machine with transition validation + audit trail

Source-inspiratie: Mission Control PR #239 (HITL state-machine + SSE — scoped-down). Tier: T3 complex. Deadline: 2 hours wallclock.

## Context

You build a reviewable-document state-machine. A document moves through 5 states with strict allowed transitions, and every transition is auditable. This is a stripped-down version of MC's PR-239 HITL flow — the full original spanned SSE streaming + 5 FastAPI endpoints, this task focuses purely on the state-machine + persistence + invariants. That core is what discriminates architecturally-capable models from pattern-bounded ones.

## States and transitions

```
DRAFT ──submit──► PENDING_REVIEW
PENDING_REVIEW ──approve──► APPROVED
PENDING_REVIEW ──request_changes──► CHANGES_REQUESTED
PENDING_REVIEW ──reject──► REJECTED
CHANGES_REQUESTED ──submit──► PENDING_REVIEW    (resubmit after edits)
APPROVED ──(terminal)──► (no outgoing transitions)
REJECTED ──(terminal)──► (no outgoing transitions)
```

Invariants:
- A document is always in exactly one state
- Terminal states (APPROVED, REJECTED) reject all transitions with `InvalidTransition`
- Every transition writes an audit row with (from_state, to_state, actor, timestamp, reason)
- `from_state` in audit always matches the state immediately before the transition (no skipping)

## Required deliverables

### 1. `state_machine.py`

```python
class ReviewableDocument:
    def __init__(self, doc_id: int, project_id: str = "default"): ...

    state: str  # one of the 5 states (DRAFT, PENDING_REVIEW, APPROVED, CHANGES_REQUESTED, REJECTED)

    def transition(self, action: str, actor: str, reason: str = "") -> None:
        """Apply transition or raise InvalidTransition."""

class InvalidTransition(Exception):
    """Raised when action is not allowed from current state."""
```

### 2. `persistence.py`

```python
def save_document(conn, doc: ReviewableDocument) -> None: ...
def load_document(conn, doc_id: int, project_id: str = "default") -> ReviewableDocument: ...
def get_audit_trail(conn, doc_id: int, project_id: str = "default") -> list[dict]: ...
```

SQLite tables (via migration `migrations/001_state_machine.sql`):
- `documents`: (id, project_id, state, created_at, updated_at) — composite PK (id, project_id) per ADR-007
- `state_transitions`: (id, document_id, project_id, from_state, to_state, actor, reason, occurred_at) — append-only audit

### 3. `tests/test_state_machine.py`

12 tests:
- `test_initial_state_is_draft`
- `test_submit_from_draft_goes_to_pending_review`
- `test_approve_from_pending_review`
- `test_request_changes_from_pending_review`
- `test_reject_from_pending_review`
- `test_resubmit_from_changes_requested`
- `test_invalid_transition_from_draft_approve_raises`
- `test_terminal_approved_rejects_all_actions`
- `test_terminal_rejected_rejects_all_actions`
- `test_audit_row_written_per_transition`
- `test_audit_from_state_matches_pre_transition`
- `test_project_id_isolation_two_docs_same_id`

### 4. `migrations/001_state_machine.sql`

Idempotent. Tables + composite PK per ADR-007.

## Definition of done

- All 12 tests pass: `pytest tests/test_state_machine.py -v`
- Migration is idempotent
- No `transition()` shortcut that bypasses audit
- `InvalidTransition` is the only exception type for state-machine-rejected actions
- No bare except clauses
