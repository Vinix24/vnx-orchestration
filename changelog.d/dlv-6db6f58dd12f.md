- Receipts (ADR-083): the five writers that do not chain (`backfill_pr_merged_receipts`, `backfill_wt_receipts`,
  `vnx_worktree_merge_data.sh`, the receipts step of `compact_state`, `backfill_headless_receipts`) refuse a chained,
  sealed or ambiguous ledger and write nothing. The check and the mutation share one critical section under
  `append_receipt.lock` (`ndjson_hash_chain.unchained_write`); `compact_state` skips the receipts rotation with a
  `receipts_skip_chained_ledger` event and exit 1, its other steps run as before.
