# phantom_guard

Flags a delivery worker's completion claim as phantom when the supplied worktree or pushed-branch diff is empty. Read-only work, review roles and review task classes are exempt; spending tokens does not establish that a deliverable exists.

Background: [dispatch governance and phantom guard](../core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md#6-govern--the-phantom-guard).

## Entry points

[`scripts/lib/phantom_guard.py::phantom_guard`](../../scripts/lib/phantom_guard.py), [`scripts/lib/phantom_guard.py::guard_at_govern`](../../scripts/lib/phantom_guard.py).

## Tests

[tests/test_phantom_guard.py](../../tests/test_phantom_guard.py), [tests/test_phantom_guard_fix_forward.py](../../tests/test_phantom_guard_fix_forward.py).

## Decisions

none

## Flag

—
