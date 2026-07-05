# Attestation Enforcement — the D1–D5 pipeline

**Status:** Current | **Shipped:** 2026-07-04/05 (PRs #1004, #1007, #1009, #1011, #1012) | **Stage:** advisory (non-blocking)

This document narrates the signed-attestation system that gates whether feature code can merge
without a cryptographically verifiable link back to a governed dispatch. It **complements**
file-based gate locks (see `docs/manifesto/GOVERNANCE_ARCHITECTURE.md` §3) — the two mechanisms
coexist — adding a server-side, key-custody-backed enforcement layer at the one moment gate locks
do not cover: PR merge.

See also: [ADR-027](decisions/ADR-027-signed-attestation-enforcement.md) (the binding decision),
[KEY_PROVISIONING.md](KEY_PROVISIONING.md) (the operator key-setup runbook).

---

## Why this exists

Rev 1/2 of the plan-gate panel (`claudedocs/2026-07-04-governance-attribution-enforce-PLAN.md`)
established that **no client-side layer is un-bypassable** for an actor with full local write
access — a CLI check, a SQLite trigger, a pre-commit hook can all be skipped by the same process
that would need to honor them. The only point that cannot be talked around is a **server-side
gate at the permanence boundary** (PR merge), and a server-side gate can only trust what it can
cryptographically verify. That reframes governance enforcement from "does the CLI check a rule"
to "is this code SSH-signed by a key the server trusts" — enforcement strength reduces exactly to
**key custody**.

Design constraint: attribution ≠ mandatory roadmap inclusion. Ad-hoc/exploratory work stays free
(stamped `ad-hoc`, no track, no closure requirement). The gate fires only when feature code is
made **permanent** — i.e. lands via a merged PR.

---

## The trust model

1. A governed action (a dispatch that passed the plan-gate) signs an **attestation manifest** —
   lineage (Dispatch-ID, deliverable_id, track_id, plan-gate-pass ref) — with a VNX signing key.
2. The signature travels **in the repo**: a committed, hash-chained record under `.vnx-attest/`.
3. A GitHub Actions required check verifies the signature server-side, against a trusted
   `allowed_signers` public key list, before the PR can merge.
4. Bypass now requires possession of the signing key — not knowledge of a CLI flag.

**Honest residual:** if the worker process that authors a commit can also *read* the signing key,
the guarantee degrades from preventive to detective (the worker signs its own unreviewed work).
The hardening path — a remote signing service where the door signs and the worker never holds the
key — is not yet built; it is named as a future slice in
[KEY_PROVISIONING.md](KEY_PROVISIONING.md#single-machine-residual).

**What the gate can and cannot verify.** The GitHub Action has no access to the local/central
track DB. It verifies, independently of anything local: (a) a detached SSH signature over the
manifest's canonical bytes is valid, (b) the signer is on `allowed_signers`, (c) the manifest
binds to *this* PR's diff. It does **not** independently confirm the plan-gate-pass / deliverable
state claimed inside the manifest — that lineage is signer-attested, corroborated by the receipt
chain, not re-verified by CI. This is a deliberate scope boundary, not an oversight.

---

## D1 — Signing authority + attestation manifest

`scripts/lib/attestation.py`

- SSH signing namespace: `"vnx-attestation"` (`scripts/lib/attestation.py:34`) — must match on
  both `sign_manifest` and `verify_attestation`.
- Canonical bytes for signing **exclude** `signature` and `prev_hash`
  (`scripts/lib/attestation.py:46-53`), so the signature is stable across chain positions.
- Two manifest types, always distinguishable by `attestation_type`:
  - **Governed** (`scripts/lib/attestation.py:66-94`) — carries `dispatch_id`, `deliverable_id`,
    `track_id`, `plan_gate_ref`, `signer_identity`, `timestamp`.
  - **Ad-hoc** (`scripts/lib/attestation.py:97-125`) — no lineage fields, never signed with the
    governed key path; distinguishable purely by `attestation_type == "ad-hoc"`.
- Signing/verification are `ssh-keygen -Y sign` / `ssh-keygen -Y verify` subprocess calls
  (`scripts/lib/attestation.py:132-218`). The signing key is **always caller-supplied** — this
  module never provisions, stores, or looks up a key (`scripts/lib/attestation.py:7-8`).
- Two ledgers, both hash-chained via `scripts/lib/ndjson_hash_chain.py`:
  `.vnx-attest/governed.ndjson` and `.vnx-attest/adhoc.ndjson`
  (`scripts/lib/attestation.py:11-12`, `:246-321`).

## D2 — Content-keyed, diff-bound in-repo record (squash-safe)

`scripts/lib/content_key.py`, `scripts/lib/attest_record.py`

The repo squash-merges PRs, so a commit-SHA-keyed attestation would break at merge. Instead:

- **Content-key** = SHA-256 of the `git diff` from merge-base to HEAD, **excluding**
  `.vnx-attest/` itself (so writing the attest record doesn't change its own key)
  (`scripts/lib/content_key.py:3-9, 20, 63-82`).
- Squash-safe by construction: squashing or rebasing without changing the resulting code delta
  produces the same content-key, because the key is derived from the diff, not commit history.
- The content-key **is** the diff hash — not a separately-minted UUID
  (`scripts/lib/attest_record.py:117`).
- **Diff-binding**: `build_attest_manifest` embeds `diff_hash` inside the signed manifest
  (`scripts/lib/attest_record.py:58-73`), so a manifest signed for one track's diff cannot be
  silently reused to vouch for a different track's code — the signature covers the diff hash.
- Each governed build writes `.vnx-attest/<content-key>.json`
  (`scripts/lib/attest_record.py:132-134`). Verification (`verify_attest_record`,
  `scripts/lib/attest_record.py:149-203`) checks, in order: record exists for the current
  content-key → `manifest.diff_hash` matches the current diff → the detached signature is valid.
- **Base-branch trust anchor**: `read_allowed_signers_from_base`
  (`scripts/lib/attest_record.py:206-234`) reads `allowed_signers` via
  `git show <base_ref>:.vnx-attest/allowed_signers` (falling back to `.vnx/allowed_signers`) —
  **never** from the PR working tree. A PR-writable trust anchor would let a rogue key
  self-verify; reading only from the base branch closes that.

## D3 — Server-side verification gate (staged advisory)

`scripts/lib/verify_pr.py`, `.github/workflows/attestation-gate.yml`

- **Fail-closed exempt allowlist** (`scripts/lib/verify_pr.py:50-53, 94-105`): only `docs/`,
  `tests/`, and `*.md` are exempt. Classification is **inverted** from a feature-list check —
  every changed path that is *not* exempt requires attestation
  (`scripts/lib/verify_pr.py:121-147`). A brand-new top-level file or directory is never silently
  ungated; the conservative default is a false-positive (unexpected attestation requirement) over
  a false-negative (silently ungated change).
- **Verifier safety**: `verify_pr.py` and its dependency chain (`attest_record.py`,
  `attestation.py`, `content_key.py`, `ndjson_hash_chain.py`) are extracted from `origin/main` via
  `git show` before the gate runs (`.github/workflows/attestation-gate.yml:55-67`,
  `scripts/lib/verify_pr.py:14-17`). The PR-tree copy of `verify_pr.py` is never executed for the
  gate decision — a PR cannot weaken its own verifier.
- **allowed_signers** is likewise read from `origin/main`
  (`.github/workflows/attestation-gate.yml:99-117`), never the PR head.
- Exit codes (`scripts/lib/verify_pr.py:29-32`): `0` = PASS or EXEMPT, `1` = FAIL, `2` = CONFIG
  ERROR (`allowed_signers` missing at the base branch).
- **Staged rollout** (`.github/workflows/attestation-gate.yml:1-7, 33-37`): the workflow runs
  today with `continue-on-error: true` — it reports but never blocks a merge. Flip criterion: 5
  consecutive PRs carrying valid signatures, operator-confirmed. Operator step to flip: promote
  the check to required + `enforce_admins` in branch-protection settings, then remove
  `continue-on-error: true`. The same staged-then-flip pattern is used elsewhere in this codebase
  for `VNX_AUTO_CLOSE` (`scripts/lib/objective_reconcile.py:50`, `FLIP_STREAK_REQUIRED = 7`).

## D4 — Signed, budgeted, audited override

`scripts/lib/attest_override.py`

An override is **not** a blanket skip — it is itself a signed attestation of type `"override"`,
recording a deviation rather than hiding one. ISO/ISAE framing: zero *unrecorded* deviations, a
recorded-and-signed one is acceptable.

- Default budget: **5 overrides per rolling 30-day window**
  (`scripts/lib/attest_override.py:55-56`), configurable via `VNX_ATTEST_OVERRIDE_BUDGET`
  (`scripts/lib/attest_override.py:69-74`).
- `count_overrides_in_window` (`scripts/lib/attest_override.py:111-188`) derives the count from
  the append-only NDJSON trail — never a mutable counter:
  1. **Chain integrity first** — a tampered or spliced trail raises `ValueError` rather than being
     silently trusted (`:145-158`).
  2. Each entry only counts if its detached signature verifies against the (base-branch-pinned)
     `allowed_signers` (`:172-177`) — an unsigned or forged entry cannot inflate *or* deflate the
     count.
- `write_override_record` (`scripts/lib/attest_override.py:191-272`) re-derives and re-checks the
  budget itself (defense in depth) and **refuses to write** when exhausted, even if the caller
  skipped its own check (`:232-244`).
- **Gate-side triple enforcement** (`scripts/lib/verify_pr.py:255-327`), run when a regular
  attestation fails but a signed override is present:
  1. **Anti-reset** — the PR's override trail must append-only *extend* the base-branch trail;
     truncating rows to free budget is rejected (`:271-284`).
  2. **Trail-membership** — a matching, validly-signed entry must exist in the append-only trail,
     not just the standalone override-record file, closing an audit-ledger-bypass path where the
     record survives but its trail row is deleted (`:289-312`).
  3. **Budget exhaustion** — count vs. `VNX_ATTEST_OVERRIDE_BUDGET`, checked again at the gate
     (`:313-327`).
  A verdict is never a silent PASS: it is either `"PASS: attestation valid"`, `"override: RECORDED
  deviation — signed by ..."`, `"override REJECTED: ..."`, or `"FAIL: ..."`
  (`scripts/lib/verify_pr.py:335-353`).
- CLI surface: `vnx attest override --key <path> --reason "<text>"`
  (`vnx_cli/commands/attest.py:220-370`) — resolves `allowed_signers` from the base branch by
  default, computes the content-key, checks budget, writes the signed record + trail entry, and
  git-commits both.

## D5 — Init wiring (trust-root scaffold + CI template)

`vnx_cli/commands/init_cmd.py:474-538`

- `_provision_attest_trust_root` (`:474-508`) — idempotent: creates `.vnx-attest/allowed_signers`
  (empty scaffold, no key generated) and `.vnx-attest/README.md`, and appends the CODEOWNERS
  trust-root block if the sentinel line is absent. Never generates a signing key — key generation
  is an operator Touch ID / keychain step (see `KEY_PROVISIONING.md`).
- `_provision_github_workflow` (`:511-536`) — copies `templates/init/attestation-gate.yml` into
  `.github/workflows/attestation-gate.yml`, skipping if the destination already exists (never
  clobbers a project's own workflow).
- Both run unconditionally during `vnx init`.
- **Not shipped in this slice:** `vnx doctor --strict` does not yet check for attestation
  infrastructure (key presence, `allowed_signers` well-formedness, workflow presence, git signing
  config). The plan's D5a described this as part of the doctor contract; only the `init`
  provisioning half landed. See [Not yet built](#not-yet-built) below.

---

## CODEOWNERS — the trust-root

`CODEOWNERS` designates three paths as requiring `@Vinix24` review:

```
.vnx-attest/allowed_signers                    @Vinix24
.github/workflows/attestation-gate.yml         @Vinix24
.vnx/governance_enforcement.yaml               @Vinix24
```

A PR cannot self-authorize a new signing key, disable the gate workflow, or loosen the local
detective enforcement config without a maintainer review. For the trust anchor specifically, this
CODEOWNERS review combines with the gate reading `allowed_signers` (and its verifier code) only
from the base branch (D3): a PR that edits its own trust anchor gets reviewed *and* is still gated
against the pre-PR version of that anchor. Note that `.vnx/governance_enforcement.yaml` is
CODEOWNERS-guarded but is the **separate local detective layer** — it is not read by the D3 CI
gate at all (`verify_pr.py` reads only `allowed_signers` from base).

---

## Local detective layer — `.vnx/governance_enforcement.yaml`

This is a **separate, earlier-and-softer** layer than the D3 CI gate — a local config the
governance CLI/daemon reads to decide whether to block a *dispatch* (not a PR merge) on things
like "codex gate must have run" or "no blocking open items." Four enforcement levels per check:

| Level | Name | Effect |
|---|---|---|
| 0 | `off` | check disabled |
| 1 | `advisory` | logs a warning, never blocks |
| 2 | `soft_mandatory` | blocks unless `VNX_OVERRIDE_<CHECK_NAME>` is set |
| 3 | `hard_mandatory` | always blocks; cannot be overridden |

Eleven checks are defined today, e.g. `ci_green_required` (level 3), `codex_gate_required` (level 2,
scoped to `scripts/`, `dashboard/`, `.github/`), `max_pr_lines` (level 1, threshold 300)
(`.vnx/governance_enforcement.yaml:11-56`). Four presets (`strict`, `standard`, `relaxed`, `off`)
let an operator dial the whole set up or down at once (`:58-98`); the active mode is set via
`mode:` at the top of the file (currently `standard`).

`.vnx/governance_profiles.yaml` layers folder-scoped profiles on top (default / light / minimal),
mapping glob patterns to `required_gates`, `max_pr_lines`, and `auto_merge` — first-match-wins
(`.vnx/governance_profiles.yaml:24-34`).

---

## Operator runbook

See [KEY_PROVISIONING.md](KEY_PROVISIONING.md) for the full one-time setup: generating an
ed25519(-sk) SSH key, registering it in `allowed_signers`, configuring `git` for SSH signing, and
the test-sign/verify round-trip. Key rotation: add the new key to `allowed_signers` (signed by an
existing trusted key), never let a new key self-authorize.

CLI surface (`vnx_cli/commands/attest.py`):

```
vnx attest write    --dispatch-id <id> --deliverable <D#> --track <track>  [--key <path>]
vnx attest verify   [--allowed-signers <path>]
vnx attest verify-pr --base-ref origin/main --head-ref HEAD   # used by the GitHub Action
vnx attest override --key <path> --reason "<text>"
```

`vnx attest write` without `--key` writes an unsigned record (advisory phase); with `--key`, it
attempts a `git commit -S` using the same key, falling back to a plain commit if SSH commit
signing is not configured (`vnx_cli/commands/attest.py:96-114`).

---

## Not yet built

These items are named in the plan (`claudedocs/2026-07-04-governance-attribution-enforce-PLAN.md`)
but have no shipped code:

- **D0 — Caller audit + key-custody design.** Deciding where the signing key actually lives
  (door-level signing service vs. operator keychain vs. machine key) was meant to gate the other
  deliverables; in practice D1-D5 shipped with the caller always supplying `key_path` explicitly,
  deferring the custody decision to the operator's local setup (`KEY_PROVISIONING.md` documents
  Option A / Option B, but no code enforces which one is in use).
- **D6 — Threat model + key-custody doc.** Not written as a standalone document; this document
  and `KEY_PROVISIONING.md` partially cover the same ground but were not scoped as the formal D6
  deliverable.
- **`vnx doctor --strict` attestation checks.** No check exists yet for key presence,
  `allowed_signers` well-formedness, or workflow presence (see D5 above).
- **Remote/door-level signing service.** Named in `KEY_PROVISIONING.md` as the fix for the
  single-machine residual; no implementation exists.
- **Evidence-bound gate.** Not a named feature in code — the closest existing thing is D2's
  diff-binding (the manifest embeds a diff hash). An "evidence-bound gate" as a distinct concept
  (binding to specific tracked claims, not just a diff hash) does not exist.
- **Signed delegation** (operator → door → worker chained authority). Not implemented; the
  current model is a single signer identity per manifest.
