# oi1719-takeover-boeking-leesbaar: Takeover bookings count as review evidence

Merge and readiness checks can use an obligation recorded as fulfilled by takeover evidence, even when the successor's result lacks its own takeover path. The referenced review must still provide a complete PASS on the current PR head, cannot fill two seats, and cannot cancel a blocking review. Readiness output names the reviewer that supplied the takeover evidence.

## How

Run `vnx pr-ready <pr-number> --verbose`; see `vnx pr-ready --help` for flags. Follow [review gate verification](../operations/T0_RUNBOOKS.md#34-review-gate-verification) to inspect the supporting request, result and report.

## Modules

- [Merge door](../modules/merge-door.md)
- [Governance enforcement](../modules/governance-enforcement-stack.md)

## Pull requests

Track closed: 2026-10-17 (UTC).

[#2074](https://github.com/Vinix24/vnx-orchestration/pull/2074), [#1839](https://github.com/Vinix24/vnx-orchestration/pull/1839).
