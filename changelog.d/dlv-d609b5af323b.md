- Deliverable retarget/withdraw (track `deliverable-retarget-withdraw`): the track's nameting,
  measured read-only on `vnx-dev` (`user_version` 36), is in
  `docs/investigations/2026-10-27-deliverable-retarget-withdraw-nameting.md`. All three goal
  points are `met`: both verbs are on main with a red run on the old head (#2241, #2255); the two
  withdrawn deliverables count as neither delivered nor failed and keep no track open (P09 and
  P21 do); and the OI-2325 reclassification moved 17 deliverables and withdrew 2, with 17 + 2
  `coordination_events` and 17 + 2 `state_mutation` ledger lines, each with its reason.
  The ADR-049 down-migration section and the `-- Down:` comment of
  `schemas/migrations/0036_deliverable_withdrawn.sql` now describe the ledger-first revert of
  #2255 round 2: one `migration_0036_revert` receipt before the transaction, a refusal when that
  append is not `appended`, and the aborted line when the transaction fails after the intent line.
