"""Vulture whitelist — symbols intentionally kept despite appearing unused.

Run vulture with:
    vulture scripts/ scripts/vulture_whitelist.py --min-confidence 80

Items here are 90%-confidence findings kept because they may be used via
dynamic dispatch, __all__ exports, or are part of a planned interface.
"""

# ---------------------------------------------------------------------------
# 90%-confidence unused imports — kept, reason noted
# ---------------------------------------------------------------------------

# dispatch_broker.py: transition_dispatch_idempotent, select_intelligence —
#   imported at module level, may be re-exported or used by dynamic callers.
# mixed_execution_router.py: load_headless_adapter — conditional usage path.
# safe_autonomy_cutover.py: verify_batch — cutover gate, not yet wired.
# supervisor_shadow.py: escalate_incident — reserved escalation path.
# event_store.py: Union — used inside a string annotation (vulture false-positive).
