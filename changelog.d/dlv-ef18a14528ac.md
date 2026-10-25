- `vnx deliverable retarget <id> --objective <track> --reason "..."` (track `deliverable-retarget-withdraw`): a proposed or
  ready deliverable moves to another track with one `deliverable_retargeted` event, instead of a `close --evidence` that
  attests a PR which never delivered the work. A ready one goes back to proposed; started or ended ones are refused.
  The decision lands first as a `state_mutation` receipt (`trigger: deliverable_retarget`) in the store's
  `t0_receipts.ndjson` (ADR-005); a ledger that does not take it is exit 1 and nothing moves. A row that changed in
  between, or a store error after that receipt, adds a `deliverable_retarget_aborted` receipt with the re-read state
  and track. Each decision carries its own `retarget_id` in `status`, so the ledger's outcome dedup (ADR-038) never
  drops the aborted line or a second move of the same deliverable as a duplicate.
