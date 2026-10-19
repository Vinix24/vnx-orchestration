- Plan-gate (P2 of plan-gate-alleen-voor-architectuur): one seat reads the plan, `default_seat`
  (codex) in `configs/plan_gate_panel.yaml`, and its one readable PASS clears the gate. When it
  returns BLOCK, `escalation_seat` (opus) reads the same plan in the same run and the rule decides
  over both. The governance variant no longer sizes the panel and a new feature no longer gets all
  five seats; the variant stays in the trace and on the round record. `--panel-seats` still
  overrides, runs exactly the seats it names and keeps the quorum of two for several seats.
  - Rounds count per plan version: the `plan_gate_round` record carries the plan's sha256 and the
    seats' verdicts. A revised plan always gets its seat round before the tiebreaker sees it, and
    the tiebreaker reads the latest version with the seat's verdict on it. `max_rounds` is 1: one
    seat, one round, then the tiebreaker, in the same run. A read version below the threshold
    must change first. Records written before this count as one version each.
  - The plan reviewer is asked for scope, PR split, decisions and ADRs, and feasibility, and not
    for line numbers or test details. A tiebreaker STOP now files the seat's real findings as
    open items.
