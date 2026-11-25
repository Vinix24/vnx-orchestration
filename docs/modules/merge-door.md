# merge-door

Runs merge preflight checks, obtains a door permit when required and pins the merge to the checked PR head.
It confirms GitHub's merged state before recording the merge receipt and dispatch-register event.
Background: [enforcement chain](../core/ENFORCEMENT_CHAIN.md).

## Entry points

- [`scripts/pr_merge.py::main`](../../scripts/pr_merge.py)
- [`scripts/pr_merge.py::merge_pr`](../../scripts/pr_merge.py)

## Tests

- [tests/test_pr_merge_door_permit.py](../../tests/test_pr_merge_door_permit.py)
- [tests/test_pr_merge_match_head.py](../../tests/test_pr_merge_match_head.py)

## Decisions

[ADR-061](../governance/decisions/ADR-061-deur-afgedwongen.md)

## Flag

—
