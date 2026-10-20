- Review stop rule (R2a, ADR-041): a PR at the B3 cap gets a next step. `vnx review cap-verdict --pr N --for-dispatch <id>`
  asks one registry model for one outcome of the cap rule and records it in `review_cap_verdicts` (migration 0035,
  ADR-007 key). Never a findings list. `verkleinen` grants one extra codex-seat round to the named dispatch,
  used by the first codex verdict on a head with a commit of that dispatch; the merge door counts only that round's PASS.
  `terug-naar-plan` and `mergen-met-OI` are recorded as `outcome_unavailable_in_r2a` and leave the cap closed.
  - `vnx review rounds open` on an opened PR releases a head whose verdict reached the round ledger while its
    result record did not (an interrupted publication).
