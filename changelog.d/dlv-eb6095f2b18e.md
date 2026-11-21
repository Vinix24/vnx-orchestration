- The door refuses a fire whose hard predecessor deliverable has no `pr_merged` receipt (ADR-096 B2), with
  `VNX_DELIVERABLE_PREDECESSOR_MODE` (`refuse`/`warn`/`off`) and `--override-predecessor "<reason>"`. Open question 1:
  refuse, warn and override lines go to a new ledger, `state/deliverable_predecessor.ndjson`, key
  `(project_id, ts, dispatch_id, seq)`; open question 3: a soft dependency is reported in the door message only.
