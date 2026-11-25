# ADR-099: Proportionality in the review prompt

## Status

Proposed — 2026-11-25. Delivery R4c, dispatch `dlv-894036115804`.
ADR-098 already records the impact shadow workflow decision. ADR-099 was the
next free number after checking main and the changed files of all open PRs.

## Context

R3 proposal 6 in the `doorloop-per-feature` research asks code review to block
missing safeguards and send optional mechanisms to open items (OIs). R4a
provides the shared [proportionality rubric](../../../skills/proportionality/SKILL.md).
The plan-gate of 22 November, 09:52Z, required R4c either to change the verdict
code or to narrow its promise to advice and name an ADR. A prompt cannot
guarantee that advisory-only FAIL rounds disappear: the result writer creates
`verdict_without_blocking_finding` when a rejecting reviewer supplies findings
only at advisory severities.

[ADR-080 decision 1](ADR-080-afwijzing-zonder-bevinding-is-afwezigheid.md)
requires a `fail` with at least one finding to remain `fail`, including when all
findings are advisory. Reproduced defects with understated severity motivated
that protection (OI-1938).

## Decision

Choose the advisory route, preserving verdict classification. The shared
`scripts/lib/gate_prompt.py` prompt builder carries the following review policy:

- A touched T1-T8 class without its minimum mechanism is blocking, with severity
  `error` and a finding message beginning exactly `missing_risk_class:`. Name
  the class, failure path, violated guarantee and consequence.
- A mechanism without a risk class or current user, or an O1-O6 improvement
  without a concrete violated guarantee, receives severity `info` and the exact
  message prefix `unjustified_mechanism:`. On its own it never warrants FAIL:
  the reviewer returns PASS with the info finding, and T0 opens an OI from it.
- A reproducible privacy or data-loss defect stays blocking regardless of its
  label. Report the concrete defect at blocking severity.

Read the class and trigger names from `skills/proportionality/SKILL.md` at each
prompt build; do not duplicate the tables. An unreadable, undecodable or
incomplete skill raises `ProportionalityRubricError` with the named reason
`proportionality_rubric_unavailable`, and no prompt is returned. Resolve the
source relative to the builder module, independent of the caller's directory.

Keep both prefixes in the existing finding messages, countable from review
records for the 6 December measurement starting at R4c's merge. Add no result,
receipt or ledger field. `skills/reviewer/template.md` is a report template;
the active gate callers use `build_review_prompt` instead.

This decision amends nothing in ADR-080. Keep `failure_classification.py`,
`gate_artifacts.py` and `stop_conditions.py` unchanged. Existing ADR files also
remain unchanged, following [ADR-079](ADR-079-documentatie-volgt-de-code.md).

## Consequences

The prompt should reduce advisory-only rejection rounds; it cannot prevent a
reviewer from returning FAIL with only info findings. Such a result still
records fail and retains `verdict_without_blocking_finding` under ADR-080
decision 1 (OI-1938). Tests pin both that limit and PASS with the same info
finding, through the existing artifact, receipt and round writers. T0's OI
handling remains a governance action; this change adds no automatic OI writer.

Rollback: revert this PR. That removes the prompt policy and its skill-read
requirement without changing stored results or verdict classification.
