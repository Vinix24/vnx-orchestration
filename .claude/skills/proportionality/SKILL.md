---
name: proportionality
description: >
  Apply one rubric against work that is too thin or overbuilt. Load at planning
  (horizon, planner or T0), at the plan-gate to check risk coverage and the removal
  list, and at code review to separate missing safeguards from optional mechanisms.
  Use manually today; automated planning and review integration is separate work.
allowed-tools: [Read, Grep, Glob]
---

# Proportionality

Keep the smallest version that delivers the whole user flow with its required
guarantees. Apply this rubric to each mechanism in a plan or diff, not to line
counts or the number of green tests. It describes how existing readers judge
work; it introduces no gate, ledger or receipt field. R4b wires planning and the
plan-gate; R4c wires code review. Loading this skill alone changes neither gate.

## Three questions

1. **Which risk class does it cover?** Name the applicable T1–T8 class below.
2. **Who uses it now?** Name a live caller, paying tier or running job. A future
   user is not a current user.
3. **How do we see it works?** Name a test that fails without it, or an operational
   measurement of the promised result.

A mechanism answering all three stays. A touched class without its minimum
mechanism is **too thin**. A mechanism without a class or current user goes on
the **removal list**. Missing proof remains unproven; it is not evidence that a
safeguard works or that removing it is safe. Cover each touched class, with T7
applying to every feature. One mechanism may cover several classes.

## Too thin: risk classes

Examples below are historical observations. A reproduced defect or shadow
experiment is not automatically a production incident or a measured failure rate.

| Class | Touched when | Minimum mechanism | Concrete example and evidence |
|---|---|---|---|
| T1 — Double action | Two runs, retries or requests can overlap. | Atomic claim or idempotency key on the external action. | Overlapping payment webhooks produced two renders and two emails in the one-day version, versus one each on main; this did not demonstrate double charging. `dlv-f68c07e2ec95-e2.md` § Scenario's, S01. |
| T2 — Money and access state | Payment, refund, licence or revocation changes. | Each transition independent and verified; a half-failed change never leaves access open. | With a failed refund-status write, the small version left downloads at HTTP 200 until retry; main independently revoked access and returned HTTP 410. This was a shadow scenario, not a production incident. `dlv-f68c07e2ec95-e2.md` § Scenario's, S05-status_write. |
| T3 — Durable delivery | A message or job must arrive despite an outage. | Persisted retry (outbox), with separate states for stored, definitively failed and unknown external outcome. | Both outboxes retained a message through HTTP 503/timeout and restart, but both duplicated after acceptance with a lost response. A stored retry is not proof of exactly-once delivery. `dlv-f68c07e2ec95-e3.md` § Scenario's, S05/S12; § Oordeel. |
| T4 — Privacy boundary | Personal data crosses a process, provider or store. | Redaction or a ceiling enforced at the destination, tested per destination. | OnCue redacted for the global Ollama setting while OpenRouter actually received insights context, allowing unredacted PII in that configuration. Review proved the code defect, not a customer leak. `dlv-f68c07e2ec95-b.md` § 4. |
| T5 — Write order and data loss | A crash or race between two writes loses data. | Local-first or atomic write, with a tested recovery path. | OnCue had periodic checkpoints, atomic files and local reports before provider work, but no production checkpoint recovery was established and no dashboard resume consumer was found. Storage alone did not prove recovery. `dlv-f68c07e2ec95-b.md` § 3. |
| T6 — Silent failure | A person or gate reads a status, health check or job result. | Never report “ok” while errors exist; distinguish empty from failed. | Main's conversion health reported `ok` for one success and two errors, while the small ledger reported failure. `dlv-f68c07e2ec95-e1.md` § Scenario's, S09. |
| T7 — Delivered user outcome | Always. | An end-to-end check that the user actually received the result, beyond the system saying it did. | The operator inbox closed an unanswered decision question after observing `dispatches.state=completed`; OI-2995 records the false closure. `dlv-f68c07e2ec95-a.md` § 1. |
| T8 — Secrets | Import, export, logs or reports copy content out. | A secret check on every path that copies content out. | Re-importing an edited decision entry exposed a secret in its title on stderr before secret validation; review reproduced the leak. `dlv-f68c07e2ec95-a.md` § 2. |

T7 was the most frequently thin class in R2. Prove the user flow from its actual
entry point through the delivered result, including a relevant failure case.
Privacy, money and irreversible actions need their minimum protection from the
first real execution (`dlv-9820ba59ecf5.md` § Synthese; § Voorstel, proposal 4).

## Overbuilt: removal-list triggers

A trigger identifies a candidate to justify or remove, not permission to drop a
required guarantee. Check today's caller and product promise before acting on a
historical example.

| Trigger | Example | Evidence |
|---|---|---|
| O1 — Generalisation for a second user that does not exist. | OnCue's additional Supabase audit adapter was motivated by multi-user deployments without demonstrated operational use; the report calls it a candidate, not proven dead code. | `dlv-f68c07e2ec95-b.md` § 5. |
| O2 — Built but not switched on. | VNX's inspected receipt store had 55,322 records with no `prev_hash` fields and health reported `chain_receipts_configured: false`. The implementation's existence did not establish deployed protection. | `dlv-f68c07e2ec95-a.md` § 4. |
| O3 — Two paths for the same outcome. | Mission Control was advised to choose one publication implementation and one working blog output path to reduce duplicate maintenance. | `dlv-f68c07e2ec95-c.md` § Rode draad. |
| O4 — Governance or audit extension without a current user. | OnCue's extra central audit scope had no demonstrated operational return in the report; this is a reason to demand a current user, not to remove a sold guarantee. | `dlv-f68c07e2ec95-b.md` § 5. |
| O5 — Tests that suggest a guarantee they do not prove. | Mission Control's synthesis warns against treating trivial determinism tests as a reliability guarantee. | `dlv-f68c07e2ec95-c.md` § Rode draad, priority 3. |
| O6 — Hardening that only compensates for a self-chosen architecture. | OnCue needed transcript replay because late subscribers missed earlier text. An implementation without that internal pub/sub layer could avoid the failure, provided the same required functions remain. | `dlv-f68c07e2ec95-b.md` § 3; § Rode draad. |

The binding design qualifies O4: **retain OnCue's central hash route because the
Pro tier sells it; a sold feature has a user**. This later product decision is
distinct from R2-b's historical finding. Likewise, “built but off” prompts a
decision about activation or removal; it does not make external tamper detection
useless (`dlv-f68c07e2ec95-e1.md` § Oordeel).

## Three moments

### 1. Planning: horizon, planner or T0

State the smallest version that delivers the whole flow. Deliverable 1 proves
that flow end to end (T7). For every deliverable, name its touched classes, their
minimum mechanisms, current users and acceptance evidence. For every additional
mechanism, answer the same three questions or put it on the plan's removal list.
Include that list even when the conclusion is “None”, with the reason.

### 2. Plan-gate: the plan reviewer

Compare the plan and its smaller alternative against this rubric. Use the
existing `blocking_findings` entries with these exact prefixes so findings can
be counted; no additional field is needed:

- `missing_risk_class:` a touched class lacks its minimum mechanism. Identify
  the deliverable, class, failure path and required verification.
- `unjustified_mechanism:` a mechanism has no class or no current user. Name it
  and the missing justification, or ask for its removal-list disposition.
- `no_removal_list:` the plan has no explicit removal list. An explained “None”
  is a list; an omitted section is not.

### 3. Code review gate: the diff reviewer

A touched risk class without its minimum mechanism is blocking: name the class,
failure path, violated guarantee and concrete consequence. Inspect all touched
paths, including risks absent from the plan. A mechanism without a risk class,
or an O1–O6 improvement without a concrete violated guarantee, becomes an open
item (OI), **never a FAIL round on its own**. A reproducible privacy or data-loss
defect remains blocking even when labelled “warning”. This is the rubric's review
policy, not a claim that R4a implements verdict handling
(`dlv-9820ba59ecf5.md` § Voorstel, proposal 6).

## How to write a removal list

Name the exact mechanism, O-trigger, current-user evidence, proposed disposition,
guarantee that stays and how to verify it. Separate proposed removals from work
already removed. If no candidate is safe, write “None” and explain which current
users and touched classes justify the mechanisms examined.

Worked example, proposed from the SEOcrawler retention research:

| Candidate and trigger | User evidence and disposition | Preserved guarantee and verification |
|---|---|---|
| `ReportArtifactStore.list_artifacts` and `get_scan_dir`, O1. | R2 found no production callers in `src/`, `scripts/` or `deployment/` and identified 20 lines as an unused extension. Recheck current callers; remove these interfaces only if that remains true. | Keep retention's T5 protection: the shared write/cleanup lock, safe scan paths and classification before deletion. Keep T7 download delivery through an already opened file; run the actual removal-race test and require both disappearance of the path and complete delivered content. |

Evidence for this worked example: `dlv-f68c07e2ec95-d.md` § Bewaren en opruimen
van rapportartefacten. The report also records OI-394: a stale mock target had
left that race test green without exercising the race. This proposal preserves
the repaired check; it does not claim the interfaces have already been removed.

## Measurement after two weeks

Count from the R4c merge using existing plans, receipts and OIs:

- Share of plans with a removal list; count the three plan-gate prefixes above.
- Review rounds per PR against the preceding two weeks (R1 baseline).
- Escaped T1–T8 defects: fix-forward PRs and warn/blocker OIs tied to a feature.
- O-findings filed as OIs versus FAIL rounds.

Keep the product promise and exposure in view: no observed incident with little
use does not establish equal quality (`dlv-9820ba59ecf5.md` § Voorstel).

## Source locations

The binding text is `claudedocs/specs/proportionality-skill/design.md`. R2-a–d
above are `dlv-f68c07e2ec95-{a,b,c,d}.md` under
`claudedocs/specs/doorloop-per-feature/r2-resultaten/`. R2-e1–e3 and R3 are
`dlv-f68c07e2ec95-e{1,2,3}.md` and `dlv-9820ba59ecf5.md` in the research store
`~/.vnx-data/vnx-dev/unified_reports/`. These historical sources may be outside
an installed consumer or worktree; locate them in the research checkout/store.
Numbered citations refer to the report's numbered feature sections; original
section titles are retained in citations for exact lookup.
