# ADR-027 — Signed-attestation enforcement at PR merge (key custody as the trust root)

**Status:** Accepted
**Date:** 2026-07-04
**Decided by:** Operator (Vincent van Deth), informed by a 5-family plan-gate panel (opus/kimi/glm/deepseek/codex across 4 revisions)
**Resolves:** The governed-method entry-gap problem (an agent or human could plan/land feature work outside the governed path via `vnx objective add`, pre-promote execution, or ad-hoc changes with no track). Companion to ADR-005 (NDJSON audit ledger), ADR-006 (staging→promote human gate), and ADR-023 (receipt hash chain), whose primitives this ADR reuses rather than replaces.

## Context

Rev 1 and rev 2 of the plan-gate panel (`claudedocs/2026-07-04-governance-attribution-enforce-PLAN.md`)
each returned 5× REVISE while converging on the same conclusion: for an actor with full local
write access, **no client-side layer is un-bypassable**. A CLI check, a SQLite trigger, or a
pre-commit hook can all be skipped by the same process that would otherwise honor them —
demonstrated live via direct `vnx objective add`, execution before promote, and ad-hoc changes
with no track attached.

The only point in the system that an actor cannot simply route around is a **server-side gate at
the permanence boundary** — the moment a PR merges. A server-side gate, in turn, can only trust
what it can cryptographically verify. That reframes the problem from "does the CLI enforce a
rule" to "is this code SSH-signed by a key the server trusts," which reduces enforcement strength
to exactly one thing: **key custody**.

Design constraint set by the operator: attribution must not become mandatory roadmap inclusion.
Ad-hoc, exploratory work has to stay unencumbered — no track, no closure requirement — with
enforcement firing only when feature code becomes permanent via a merged PR.

## Decision

**A five-part signed-attestation pipeline (D1–D5) gates feature-code PRs on a verifiable
signature, staged as an advisory (non-blocking) GitHub Actions check until a measured flip
criterion is met.**

1. **D1 — Signing authority.** A governed action signs an attestation manifest (Dispatch-ID,
   deliverable_id, track_id, plan-gate-pass ref) over SSH, hash-chained via the existing
   `ndjson_hash_chain` primitive. Ad-hoc actions get a distinct, unsigned `"ad-hoc"` manifest type
   — never confusable with a governed one.
2. **D2 — Content-keyed, diff-bound record.** Because the repo squash-merges, the attestation is
   keyed by a SHA-256 of the merge-base→HEAD diff (not the commit SHA), so it survives
   squashing/rebasing. The manifest embeds the diff hash it was signed for, so it cannot be
   silently repurposed for a different PR's code.
3. **D3 — Server-side gate, staged advisory.** A GitHub Actions required check verifies: the
   attestation record carried in the PR (`.vnx-attest/<content-key>.json`) has a valid detached SSH
   signature, the signer is in `allowed_signers`, and the manifest binds to this PR's diff. The
   **trust anchor** (`allowed_signers`) and the **verifier code** are read from the **base branch**,
   never the PR head, so a PR cannot weaken its own verifier or trust anchor in the same change. (The
   signed record itself necessarily travels in the PR head; base-branch resolution applies to what
   validates it, not to the record being validated.) Ships with `continue-on-error: true`; flips to
   blocking only after 5 consecutive PRs carry valid signatures, operator-confirmed.
4. **D4 — Signed, budgeted override.** An operator can bypass a failing gate only via a second
   signed attestation (type `"override"`) with a non-empty reason, capped at 5 per rolling 30-day
   window (`VNX_ATTEST_OVERRIDE_BUDGET`), counted from a tamper-evident append-only trail with
   anti-reset and trail-membership checks at the gate. An override is never a silent pass — it is
   a permanently recorded, signed deviation.
5. **D5 — Init wiring.** `vnx init` provisions the `.vnx-attest/` trust-root scaffold, CODEOWNERS
   entries, and the CI workflow template, without ever generating or handling a private key.

Explicitly deferred (see `docs/governance/ATTESTATION_ENFORCEMENT.md#not-yet-built`): D0 (formal
key-custody decision beyond "operator supplies `key_path`") and D6 (a standalone threat-model
document) were scoped in the plan but not built in this slice.

## Reasoning

1. **Preventive enforcement requires a boundary the actor cannot also control.** Every
   client-side mechanism considered in rev 1/2 failed this test. A server-side signature check
   does not, provided the trust anchor (verifier code + `allowed_signers`) is read from a
   reference the PR cannot mutate for its own evaluation.
2. **Honesty about scope beats overclaiming.** The gate verifies cryptographic facts (signature
   validity, signer trust, diff-binding) — it does **not** re-verify that the plan-gate actually
   passed; that lineage is signer-attested and corroborated by the receipt chain, not
   independently checked by CI. Two panel revisions (rev 1, rev 2) were REVISE'd specifically for
   overclaiming this boundary; rev 3/4 state it plainly instead.
3. **Squash-merge compatibility is load-bearing, not an edge case.** A commit-SHA-keyed
   attestation breaks the instant a PR is squash-merged, which is this repo's default merge
   strategy. Content-keying by diff hash was therefore a correctness requirement from day one, not
   a later hardening pass.
4. **A gate that its own PRs can disable is worthless.** CODEOWNERS-protecting `allowed_signers`,
   the gate workflow, and the local enforcement config — combined with the gate reading its trust
   anchor (`allowed_signers`) and verifier code only from the base branch during verification —
   closes the "PR edits its own trust anchor" hole that an earlier revision of this design left
   open. (The `.vnx/governance_enforcement.yaml` enforcement config is CODEOWNERS-guarded but is the
   separate local detective layer, not read by the D3 CI gate.)
5. **Staged rollout matches house style.** The same advisory-then-flip pattern already used for
   `VNX_AUTO_CLOSE` (`scripts/lib/objective_reconcile.py`, 7-consecutive-clean-run flip) is reused
   here (5-consecutive-signed-PR flip) rather than inventing a new rollout convention.
6. **Reuse existing primitives.** No new central-DB table was introduced (ADR-007 does not apply):
   the manifest lives in-repo under `.vnx-attest/`, and the hash-chaining reuses
   `ndjson_hash_chain.py` (ADR-023's mechanism) rather than a bespoke chain implementation.

## Consequences

### Accepted

- The signing key's custody model is, for now, "whatever the operator's local `git`/SSH setup
  provides" (Option A: Secure Enclave/Touch ID, non-exportable; Option B: a plain machine key,
  exportable). If the process invoking `emit_governed_attestation` can also read the key file, the
  guarantee is **detective, not preventive** — named explicitly in `KEY_PROVISIONING.md` as the
  single-machine residual, with door-level signing as the (unbuilt) hardening path.
- The exempt allowlist (`docs/`, `tests/`, `*.md`) is an intentionally accepted residual: a test
  helper or executable doc can still ship logic un-attributed. Mitigated by keeping the allowlist
  narrow and fail-closed (anything not explicitly exempt requires attestation) rather than
  fail-open.
- The gate does not block anything today — it is advisory until the flip criterion is met. Any
  correctness value it provides in this period is observational (surfacing unsigned PRs in CI
  output), not enforced.
- `vnx doctor --strict` does not yet detect a missing or misconfigured attestation setup; an
  operator who never runs `KEY_PROVISIONING.md`'s steps gets no proactive warning outside the CI
  gate's own advisory output.

### Rejected

- **Any purely client-side attribution mechanism** (CLI validation, SQLite triggers, pre-commit
  hooks) — proven bypassable by rev 1/2 of the panel for an actor with local write access.
  Rejected as insufficient by construction, not by preference.
- **Commit-SHA-keyed attestation** — breaks under this repo's squash-merge default. Rejected in
  favor of content-hash (diff) keying.
- **A local-only override sink** — unreachable by the CI Action, so it could never actually
  unblock a gate once the gate becomes required. Rejected in favor of an override that travels
  in the PR as its own signed, verifiable artifact.
- **A new central-DB table for the attestation or override record.** Rejected in favor of in-repo
  NDJSON ledgers (`.vnx-attest/*.ndjson`), keeping ADR-007's multi-tenant stamping rule
  inapplicable to this track entirely.

## See also

- `docs/governance/ATTESTATION_ENFORCEMENT.md` — the full pipeline narrative with file:line
  citations for every claim in this ADR.
- `docs/governance/KEY_PROVISIONING.md` — the operator key-setup runbook.
- ADR-005 — NDJSON audit ledger as primary (the ledger-first principle this pipeline extends).
- ADR-006 — Staging→promote human gate (the staged-rollout precedent D3 follows).
- ADR-007 — Multi-tenant project_id stamping (confirmed not applicable: no new central table).
- ADR-023 — Receipt hash chain (the `ndjson_hash_chain` primitive D1/D2/D4 reuse).
- `claudedocs/2026-07-04-governance-attribution-enforce-PLAN.md` — the 4-revision plan-gate
  record this ADR codifies.
