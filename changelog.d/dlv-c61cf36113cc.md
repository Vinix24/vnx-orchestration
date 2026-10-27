- `vnx deliverable withdraw <id> --reason "..."` (track `deliverable-retarget-withdraw`, ADR-049): a proposed or ready
  deliverable that will not be built ends in its own terminal state `withdrawn`, with one `deliverable_withdrawn`
  event, instead of `expired` (which reads as failed) or a `close --evidence` for a PR that never delivered it. Same
  ledger-first order as `retarget`: a `state_mutation` receipt, then a compare-and-swap in one transaction with its
  event; a lost swap or a store error books `deliverable_withdraw_aborted`. A store whose `dispatches.state` still
  carries the CHECK of migration 0022 (a store built by `vnx init`) is refused with that reason.
  - `withdrawn` is in the one canonical terminal set (`coordination_db.TERMINAL_DISPATCH_STATES`), reachable from
    `proposed` and `ready` only; `track_reconciler` and `runtime_supervisor` read that set instead of a copy. A withdrawn
    row is never a deliverable stub, so a track declared done never completes it, and it is no delivery signal: a
    track whose rows are all withdrawn is not derived `done`.
  - Migration 0036 replaces the `deliverables` view (schema v36): `withdrawn` is its own `derived_status` and
    `withdrawn_count`, counted as neither planned, in flight, done nor failed. The down path
    (`apply_0036.revert_migration`) turns each withdrawn row `expired` with a `deliverable_withdrawn_reverted` event.
    An earlier manifest version accepts the view of a later one, as it accepts extra columns.
  - `deliverable list` and `horizon queue` hide withdrawn deliverables unless `--all`; the scope rule and the plan-gate
    paths never count them.
  - `retarget`: an error that is not a store error after the intent receipt now books the aborted receipt before it is
    raised (kimi on #2241).
