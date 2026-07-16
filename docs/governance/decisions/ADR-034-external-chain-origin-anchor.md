# ADR-034 — External chain-origin anchor: a git-committed fingerprint that survives a local ledger-writer

**Status:** Proposed (design-only; precondition for `VNX_CHAIN_RECEIPTS` default-on — this ADR is not the flip)
**Date:** 2026-07-15 (revised 2026-07-16 — see Revision note below)
**Decided by:** Operator (Vincent van Deth). Design task only — no implementation, no tests in this PR; a follow-up dispatch implements it against this ADR.
**Revision (2026-07-16):** Two independent adversarial design reviews (Fable R1-R4, codex C1-C5) converged on the same architecture (external git anchor, fingerprint-only, this ADR's core §1 location decision — **not reopened**) with six findings, all addressed in this revision: **R1/C1** — added the reverse-direction fail-closed rule (anchor exists but ledger doesn't chain → `"broken"`; §2). **R2/C2** — corrected the §4 lock target, which named a lock nothing actually took; replaced with one public, shared lock primitive. **R3/C3** — removed an incorrect `git fsck`-detectability claim about forged local refs; moved on-host-verification-against-a-co-located-local-attacker to the residual section (§5); added an off-host cross-check output to `verify_chain` (§2, §7). **R4** — anchors are now per sealed epoch, not per ledger, closing a tail-truncation-via-new-epoch bypass (§1, §2, §7). **C4** — verified-active branch protection is now a hard activation precondition (§6 step 2b), not a follow-up; the anchor-immutability CI check is required from commit one, unlike the staged-advisory precedent it reuses the pattern from (§3). **C5** — the anchor's metadata exposure (ledger identity, line number, entry count) is explicitly accepted as non-sensitive, with reasoning, rather than left undecided (§1).
**Resolves / Cross-refs:** Closes the accepted residual ADR-029 left open ("full tamper-evidence against a local attacker needs an EXTERNAL append-only anchor... a future epoch-seal-anchor ADR" — `scripts/lib/ndjson_hash_chain.py:194-203`). Extends ADR-023 (receipt hash-chain) and ADR-029 (epoch rotation) without reopening either. Reuses ADR-027's merge-boundary trust model instead of inventing a new one. Preserves ADR-005 (NDJSON ledger is local and primary) and ADR-026 (per-project store canonical; only the governance/process signal class ever centralizes, never raw receipts). Supersedes the design direction of PR #1171 (closed) and PR #1086 (closed) — see Context.

## Context — three failed attempts, one root cause

Three PRs tried to close the same hole and failed the same class of codex review:

1. **#1085** — first hash-chain PR. `verify_chain`'s original binary "any mix ⇒ broken" state machine made the fleet-wide-RED footgun ADR-029 exists to fix, and codex separately flagged that a stripped prefix + re-chain still verified.
2. **#1086** — pinned the chain origin in `runtime_coordination.db` (a trusted SQLite store). HELD through four codex_gate rounds: mutable origin (`INSERT OR REPLACE`), a non-atomic ledger-write/origin-write pair, and fail-open on store errors.
3. **#1171** — pinned the origin in a sibling append-only NDJSON file (`<ledger>.origin_anchor.ndjson`) next to the ledger, written once at seal time. Closed 2026-07-15 on a codex FAIL, three holes tracked as **OI-628**:
   - **Deletion fail-open** — delete/truncate the sidecar → `read_origin_anchor` returns `None` → `verify_chain` falls back to the no-anchor path → the stripped+resealed ledger verifies again.
   - **Append-forgery** — `read_origin_anchor` took the **last** matching record; appending a forged record after re-chaining wins over the legitimate one.
   - **Seal race** — the ledger snapshot during seal was not flocked; a concurrent writer could get a forged origin pinned.

Each attempt anchored the origin somewhere the same actor who can edit the ledger can also edit: a DB row on the same disk, then a sidecar file on the same disk. **The root cause is invariant across all three: a purely-local anchor is deletable and forgeable by any actor with local filesystem write access to the ledger**, because a local ledger-writer and a local anchor-writer are the same trust boundary. No amount of flocking, write-once checks, or fail-closed-on-parse-error logic fixes that — the fix has to move the anchor's source of truth outside that boundary entirely.

## Hard constraint

Receipts stay **local and open-source** — NDJSON under `~/.vnx-data/<project_id>/`, never shipped to a cloud store. This ADR anchors a **fingerprint** (a hash + line number + entry count + ledger identity), never the receipts themselves. Nothing here introduces a cloud receipt store or changes what `~/.vnx-data/` contains.

## Decision

Anchor the origin fingerprint as a **git-committed record in the governed project's own source repository**, landed through the same PR-and-merge-gate pipeline that already governs every other change to that repo, and read back only from **git-committed history** — never from a plain working-tree file. `verify_chain` fails **closed**, not open, when a chaining-enabled ledger has no matching anchor.

### 1. Where the anchor lives

**Chosen: (a) — a tracked path in the project's own source repo.** Concretely: `governance/chain-origin.ndjson` at the repo root (a new top-level dir, sibling to `docs/`, `scripts/`, `.vnx/`), one NDJSON line per sealed **epoch** (revised from "per sealed ledger" — see the per-epoch anchoring note below).

Evaluated and rejected:

- **(b) A dedicated anchor repo/remote.** Rejected: a new repo to create, own, and provision credentials for, with no benefit over (a) — and it centralizes a single point of failure across every project instead of keeping each project's anchor inside a boundary that project already trusts.
- **(c) The fabric repo, `ledger/chain-origins/<project_id>.ndjson`.** Rejected on a concrete, verified fact: `vnx-orchestration` is a **public** GitHub repo (`gh repo view Vinix24/vnx-orchestration` → `"visibility":"PUBLIC"`), while every consumer project checked — `mission-control`, `SEOCRAWLER_V2`, `sales-copilot` — is **private**. Anchoring a private project's fingerprint (existence, seal timestamps, receipt-count cadence) inside the public fabric repo leaks business-activity metadata about private client/product work into a public repository. That is a confidentiality break the receipts-stay-local constraint is explicitly trying to avoid one layer up. Each project's own repo already carries the correct privacy level for that project — anchoring there costs nothing extra and leaks nothing new.

This also matches an existing convention rather than adding one: `scripts/lib/project_root.py::resolve_project_id` already falls back to the project's **git remote `origin` URL** as the canonical source of a project's identity (ADR-007). The project's own repo is already treated as that project's root of trust elsewhere in VNX; this ADR just anchors the ledger fingerprint in the same place.

**`ledger_identity` is a stable logical key, not a local path.** The closed #1171 design used `str(ledger_path.resolve())` — an absolute local filesystem path — as the anchor key. That leaks local machine layout into a committed file and is not portable. This ADR uses `"{project_id}:{ledger_path relative to the project's central data dir}"`, e.g. `"vnx-dev:state/t0_receipts.ndjson"`, matching the ADR-007 `project_id` convention already used everywhere else in VNX.

**One anchor record per epoch, not one per ledger (design-review R4 fix).** The original design anchored a single record — the *origin*, i.e. the first-ever seal — per `ledger_identity`. That leaves every later epoch unpinned: `verified-segmented` (ADR-029) accepts any number of chained epochs after the origin, and only the origin was ever checked against git. An attacker can strip a ledger's tail *after* the anchored origin, append a fresh `chain_epoch_start` opening a new, higher epoch number, and re-chain the remainder — `verify_chain` sees an internally-consistent segmented chain, checks the (unaffected, still-matching) origin, and reports healthy while the tail is fabricated. This is the same class of hole as #1/#2, just scoped to epoch N instead of epoch 1, and the original per-ledger anchor design did not close it.

Fix: the anchor key becomes `f"{ledger_identity}#{epoch}"` — one committed record **per sealed epoch**, not one per ledger. `seal_and_commit_origin` (§4) runs at every epoch seal (it already does, via `chain_epoch_seal.py`'s idempotent `seal_ledger`), not only the first. `verify_chain` (§2) requires a matching anchor for **every** `chain_epoch_start` marker it observes during the walk, not only the earliest one — an epoch with no corresponding `{ledger_identity}#{epoch}` record in git is `"broken"`, exactly like a missing origin was before. Duplicate detection (§3) and the CI immutability check (§3) both operate on the composite key unchanged in mechanism: "first record per key wins, a second record for the same key is corrupt" and "only brand-new keys may be appended" now apply per-epoch instead of per-ledger. A forged higher-epoch record is still a brand-new key by the CI rule, so it is not rejected by novelty alone — it is rejected because producing it requires landing a PR through the same governed pipeline as any legitimate seal (§3 write side, §6 activation precondition), not because the key already exists.

**Metadata boundary — decided, not left open (design-review C5).** Each committed anchor record exposes `ledger_identity` (project id + ledger's relative path, e.g. `"vnx-dev:state/t0_receipts.ndjson"`), the epoch number, a line number, and an entry count to anyone with read access to the project's own repo. This ADR explicitly **accepts this as non-sensitive metadata**, not an oversight, for three reasons: (1) it is a coarse, seal-frequency-resolution signal — epoch seals are rare, operator-governed events, not a per-receipt cadence trace; (2) anyone with read access to the project's own repo already sees a strictly richer activity signal for free — commit frequency, PR cadence, file churn — the anchor's line-count/entry-count adds negligible marginal information on top of that; (3) `ledger_identity`'s ledger-path component (`state/t0_receipts.ndjson`) is a fixed, already-public VNX convention (open-source fabric), not a project-specific secret — only the `project_id` prefix is project-specific, and that project's identity is already visible from the fact that this is *that project's own repo*. The rejected alternative — replacing the record with a single opaque commitment hash over these fields — was considered and set aside: it would hide the fields from repo readers but not from anyone with local ledger access (the actual audience `verify_chain` protects against), and it adds a second hashing indirection to every read/write path for a threat class (a repo-read-only, non-ledger-access observer) this ADR does not otherwise treat as in-scope. If a future project's threat model changes this calculus, the commitment-hash alternative remains available without changing the anchor's location or write path.

**Creds / push-on-seal implications.** No new credentials: sealing pushes to the SAME repo and remote that every dispatch worker already pushes feature branches to and opens PRs against. There is no cross-repo auth to provision. Seals are rare (epoch rotations, not the per-receipt hot path) — a commit + PR + push per seal (now per epoch, not once per ledger — see above) is still cheap.

### 2. Fail CLOSED, not open

`verify_chain` changes its contract for any ledger where `saw_chain` is true (at least one `chain_epoch_start` marker or chained entry observed — i.e. chaining is enabled on that ledger, regardless of `VNX_CHAIN_RECEIPTS`):

- **No `project_root` supplied**, or the anchor cannot be resolved from git-committed history (see §3) in a way distinguishable from "genuinely never sealed" → **`"broken"`**. A chaining-enabled ledger with no verifiable anchor is now itself the violation — this directly replaces the ADR-029 "accepted residual" note, which is deleted by this ADR, not merely narrowed.
- **A git anchor exists for `{ledger_identity}#{epoch}` but the ledger itself is missing, empty, or unchained for that epoch** → `"broken"` — the *reverse* direction, and the one the original design missed (design-review R1/C1, both independent reviews). The forward-only contract above only fires when the ledger walk observes chained content; it says nothing about a ledger that has been reset to nothing. Verified against the tree: `verify_chain`'s pre-anchor short-circuit (`ndjson_hash_chain.py:211-212`) returns `(True, [], "unchained")` for any missing/zero-byte ledger before an anchor lookup is even attempted, and `fabric_audit.py` check C (~L292) counts `"unchained"` as an OK bucket. Together, `rm ledger.ndjson` (or an unmarked rewrite that drops every `prev_hash`) reads as "never adopted chaining" — a clean bill of health — even for a ledger whose epoch(s) were legitimately sealed and anchored in git. **Fix: `verify_chain` must resolve the git anchor for `ledger_identity` *before* it is permitted to return the empty/missing-ledger short-circuit.** If any `{ledger_identity}#{epoch}` record exists in git and the corresponding epoch is absent from the ledger walk (including "walk found nothing at all"), that is `"broken"`, unconditionally — existence of a committed anchor is itself a claim that the ledger must still be chained.
- **Anchor resolves but the ledger's own observed origin (hash / line number / epoch) doesn't match the anchored record** → `"broken"` (prefix-strip / moved-boundary — the exact bypass #1085/#1086/#1171 all targeted).
- **A `chain_epoch_start` marker is observed in the ledger walk with no matching `{ledger_identity}#{epoch}` anchor record in git** → `"broken"` (design-review R4 — see §1's per-epoch anchoring note). Closes the tail-truncation bypass: strip-after-the-anchored-origin-and-open-a-fresh-epoch no longer verifies, because every epoch, not only the first, now needs its own anchor.
- **Anchor resolves and matches, for every epoch observed in the walk** → `"verified"` / `"verified-segmented"`, unchanged from ADR-029 otherwise.
- Ledgers that have **never** enabled chaining (`saw_chain` is false, no marker anywhere, **and no anchor exists for this `ledger_identity` at any epoch**) are **unaffected** — `"unchained"` stays OK exactly as ADR-029 defined it. This ADR only tightens the case where chaining is, or was, already on.

The critical mechanism that makes this fail-closed rather than a repeat of hole #1: **the anchor is read from git-committed history, never from the working-tree copy of `governance/chain-origin.ndjson`.** A plain tracked file sitting on disk is exactly as locally-editable as the ledger itself — git tracking alone is not a defense; only the *committed, pushed* content is. Read order (§3 gives signatures):

1. `git fetch origin` + `git show origin/<protected-branch>:governance/chain-origin.ndjson` — the live remote, strongest guarantee against a remote-side attacker or a stale local anchor.
2. If the network is unavailable: `git show <local-remote-tracking-ref>:governance/chain-origin.ndjson` (e.g. `refs/remotes/origin/main` from the last successful fetch) — still resolved through git's content-addressed object store, not the working tree, so it is not vulnerable to hole #1's "edit the working-tree copy" bypass. **This is not a defense against a co-located local attacker specifically** — see §5, which corrects an overclaim from the initial design (a forged local ref is not the "categorically higher, fsck-detectable" bar this ADR originally claimed).
3. If neither resolves (fresh clone, never fetched, or `project_root` isn't a git repo at all) → fail closed, `"broken"`. "Can't check" is never treated as "assume fine."

**Off-host cross-check (design-review R3/C3).** Because on-host verification against a fully co-located local attacker has a residual gap (§5), `verify_chain`'s return value carries the resolved anchor commit SHA and the remote URL it fetched/read from (see §7's updated signature) whenever an anchor is consulted — regardless of the resulting status. This lets an operator, or CI running on a separate host with its own git config, independently confirm "the SHA `verify_chain` trusted really is `origin/main`'s tip on GitHub" rather than taking the local host's word for it. This is the primary defense for the local-attacker-controls-.git/config residual, not an incidental log field.

### 3. Canonical, not last-writer

Two independent fixes close the append-forgery hole (#1171 hole #2), one on the read side and one on the write side — defense in depth, not either/or:

- **Read side — first record, duplicates are a violation.** `read_git_anchor` parses every line and returns the **first** record matching a given `{ledger_identity}#{epoch}` key (§1). If a second record for the same key exists anywhere in the file, that is **not** "last write wins" — it is treated as `"corrupt"`, which `verify_chain` maps to `"broken"`. An appended forged record can no longer win by virtue of being appended later.
- **Write side — structural immutability, enforced in CI.** `governance/chain-origin.ndjson` gets a required GitHub Actions check, reusing the exact base-branch-trust pattern already shipped for ADR-027's D3 attestation gate (`.github/workflows/attestation-gate.yml`: the verifier is extracted from `origin/main`, never from the PR head, so a PR cannot neuter the check that reviews it). The new check diffs `governance/chain-origin.ndjson` between base and head: **any line whose `{ledger_identity}#{epoch}` key already existed in the base-branch copy but changed content, and any removed line, fails the check.** Only brand-new keys (an epoch's first-ever seal) may be added. This makes a forged-modification PR unmergeable by construction, not by convention.

**Unlike the pattern it reuses, this check is required from commit one — no staged-advisory period (design-review C4).** `attestation-gate.yml` — the workflow this design explicitly reuses the base-branch-trust pattern from — currently ships with `continue-on-error: true` (verified: `.github/workflows/attestation-gate.yml` job-level flag, header comment "STAGED ADVISORY (non-blocking)... becomes a REQUIRED check after the flip criterion is met: N=5 consecutive PRs..."). That staged rollout makes sense for D3, where the risk is ramping up *signature coverage* across a fleet that doesn't have keys provisioned yet. It does not make sense here: an anchor-immutability check that only reports and never blocks is equivalent, for this ADR's threat model, to having no write-side defense at all — the entire point of §3's write-side fix is that a forged-modification PR *cannot merge*, not that it merges and gets flagged afterward. The anchor-immutability check MUST be added to branch protection as a required status check, with `enforce_admins` on, from the same PR that introduces it — never `continue-on-error: true`, never a "promote it later" step. §6 makes verified-active branch protection a hard precondition of activation, not a follow-up.

**Placement detail that matters:** the attestation gate's exempt allowlist currently skips `docs/`, `tests/`, and `*.md` (`.github/workflows/attestation-gate.yml` header comment). That is exactly why `governance/chain-origin.ndjson` is **not** placed under `docs/governance/` — it would silently inherit the docs exemption and a forged anchor PR would sail through unattested. The chosen top-level `governance/` path falls outside all three exemptions by construction, so both the attestation gate and the new anchor-immutability check apply to it.

### 4. Atomic seal

**The original design named the wrong lock target — corrected here (design-review R2/C2, both independent reviews).** The initial draft claimed `seal_and_commit_origin` holds `fcntl.flock(ledger_path, LOCK_EX)` and called it "the same lock primitive `append_chained_entry` already takes on the hot receipt-append path." Verified against the tree, both halves of that claim are false:

1. `append_chained_entry` (`scripts/lib/ndjson_hash_chain.py`) takes **no lock at all** — it opens `path` in append mode and writes, unlocked. `import fcntl` does not even appear in that module.
2. The actual hot append path does not call `append_chained_entry` in the first place. `scripts/lib/append_receipt_internals/idempotency.py::_write_receipt_under_lock` **inlines** the read-tail-hash + stamp + write sequence (`_last_hash_under_lock` → `receipt_path.open("a")`) directly inside its own critical section, deliberately *not* calling `append_chained_entry` (the docstring says why: "so no second file handle is opened outside the VNX lock"). That critical section is guarded by `fcntl.flock` on a **separate lock file**, `receipts_path.parent / "append_receipt.lock"` (`_lock_file_for`, idempotency.py:81-82), not on `ledger_path` itself.

A seal that locks `ledger_path` and an appender that locks `append_receipt.lock` do not exclude each other — they are two different `flock()` calls on two different inodes. The "mutually exclusive by construction" claim in the original design was false; a receipt could append mid-seal, racing a forged/partial origin in exactly the way #1171 hole #3 was supposed to be closed.

**Fix: one public lock primitive, named and shared, not two independent ones.** `_lock_file_for` moves out of `append_receipt_internals/idempotency.py` (private, receipt-append-specific) and becomes a public function in `ndjson_hash_chain.py` — `ledger_lock_path(ledger_path: Path) -> Path`, returning `ledger_path.parent / "append_receipt.lock"` (byte-identical to today's private helper, so the existing lock file for `state/t0_receipts.ndjson` is unchanged and no migration is needed for the live hot path). Four call sites now take this same lock around their critical sections:

- `append_receipt_payload` (via `_write_receipt_under_lock`) — unchanged behavior, now sourcing the lock path from the public helper instead of a private duplicate.
- `append_chained_entry` — gains an internal `fcntl.flock(ledger_lock_path(path), LOCK_EX)` around its read-tail + write, closing the fact that today it has no lock of its own at all (relevant for callers other than the receipt hot path — see the sibling-ledger note below).
- `append_epoch_marker` — same: takes the lock around its append.
- `seal_and_commit_origin` — takes the lock via `ledger_lock_path(ledger_path)` (**not** a bespoke `fcntl.flock(ledger_path, ...)`) across the entire read-origin → compute → anchor sequence:
  1. Inspect `epoch_state(ledger_path)`; append a `chain_epoch_start` marker if none is open for the target epoch (existing ADR-029 `append_epoch_marker`, now itself lock-holding per above — `seal_and_commit_origin` must use a re-entrant/already-held acquisition here, not a second blocking `flock` call on the same fd from the same process).
  2. Compute the epoch fingerprint (§1) from the now-stable ledger tail.
  3. Commit + push the anchor record for `{ledger_identity}#{epoch}`.

Because all four call sites now serialize on the identical `append_receipt.lock` file for a given ledger, a concurrent receipt append and a seal are genuinely mutually exclusive — a writer appending mid-seal blocks on the real shared lock rather than racing past a differently-locked one. This is what actually closes #1171 hole #3; the original design only claimed to.

**Sibling ledgers are explicitly out of scope for anchoring until they adopt the same primitive.** `.vnx-attest/plan-gates.ndjson` (`scripts/lib/plan_gate_evidence.py`) and `.vnx-attest/mandates.ndjson` (`scripts/lib/delegation_mandate.py`) both call `append_chained_entry` directly today, unlocked (verified: neither module imports `fcntl` or takes any lock around its `append_chained_entry` call). Once `append_chained_entry` itself takes `ledger_lock_path`'s lock internally (above), these two call sites inherit correct locking for free and become anchor-eligible. Until the follow-up implementation lands that change, **this ADR anchors only `state/t0_receipts.ndjson`-class ledgers that go through the locked path** — `plan-gates.ndjson` and `mandates.ndjson` are not sealed or anchored in this phase, and `seal_and_commit_origin` must refuse (not silently skip) a request to seal a ledger whose writer path it cannot confirm is lock-guarded.

**Failure is naturally fail-closed, not a rollback problem.** If step 3 (push) fails after step 1 (marker append) already landed locally, the ledger now has an open epoch with no matching anchor yet — which §2's fail-closed rule already reports as `"broken"`. There is no window where a half-sealed ledger reads as healthy. Re-running `seal_and_commit_origin` is idempotent: step 1 no-ops (marker already open for that epoch), and only the missing anchor commit is completed.

### 5. Threat model — honest bar, honest residual

**New bar.** A local actor with write access to the ledger (the entire threat class #1085/#1086/#1171 were scoped to) can no longer strip-and-reseal, delete the anchor, or win by appending a forged record, and can no longer land a forged anchor PR without the anchor-immutability check catching the modification. Deletion/truncation of the *local working-tree copy* of the anchor file is a no-op against verification, because verification never trusts that copy.

**Residual — stated plainly, not overclaimed:**

- **A co-located local attacker who also controls `.git/config` is NOT defended against by the local-remote-tracking-ref fallback — corrected here (design-review R3/C3, both independent reviews).** The original design claimed forging §2's step-2 fallback (`git show refs/remotes/origin/main:...`) "would mean corrupting local git objects... a categorically higher bar, and one `git fsck` can detect." That is false: `git update-ref refs/remotes/origin/main <forged-commit-sha>` pointed at well-formed, locally-created git objects is a single, trivial command — no corruption occurs, and `git fsck` reports the repository clean, because the objects genuinely are structurally valid; fsck checks object-graph integrity, not provenance. Worse, this isn't limited to the offline fallback: `git fetch origin` (§2 step 1, the "strongest guarantee" path) itself resolves the `origin` remote's URL from `.git/config`, which the same local attacker can also edit — pointing `fetch` at a repo they control (or a stale fork) rather than the real GitHub remote. **On-host verification against an attacker who is co-located with the ledger AND has local git write access does not have a categorically stronger leg to stand on than the ledger itself does** — both are inside the same trust boundary once `.git/config` is attacker-writable. This is the honest bar, replacing the fsck overclaim: this ADR's on-host read order (§2) defends against an attacker who can edit the ledger and the working-tree anchor file but who does *not* also control this host's git configuration and remote-tracking refs; it does not defend beyond that against a fully co-located attacker. The mitigation for that narrower-but-real gap is the off-host cross-check (§2, §7): `verify_chain` surfaces the anchor commit SHA and resolved remote URL so an *independent* host (CI, or an operator's own separate verification run) can confirm the trusted SHA is really `origin/main`'s tip on GitHub, rather than accepting the ledger-writing host's own git config as ground truth.
- An actor with genuine **push access to the remote and a way past branch protection** (a leaked credential with admin rights, a misconfigured "allow force-push" setting, or the operator's own account being compromised) can still rewrite history and forge an anchor. This is the same residual ADR-027 already accepts for code attestation generally ("for an actor with full local write access, no client-side layer is un-bypassable... key custody" — ADR-027 Context) — this ADR does not claim to exceed that bar, only to reuse it.
- **A compromised or disabled branch-protection configuration is a genuine, unavoidable residual — but only *after* activation (design-review C4).** §6 makes verified-active branch protection (required-status-checks including the anchor-immutability check, `enforce_admins` on) a **hard precondition of this ADR's activation**, not a someday follow-up — see §6. What remains residual, and cannot be closed by any design-time check, is an admin *later* disabling that protection: no client-side or CI-side mechanism can prevent the repo owner from turning off their own repo's required-status-checks after the fact. That is the same class of residual as the push-access bullet above (an actor with admin control of the remote), not a gap in this ADR's design. `fabric-audit` growing a periodic runtime check that branch protection is *still* configured as required (catching post-activation drift, not gating activation itself) remains a legitimate follow-up.
- **Offline verification degrades, it does not silently pass.** If neither a live fetch nor a local remote-tracking ref resolves, verification fails closed (`"broken"`), which is safe but means a genuinely offline environment cannot distinguish "no network" from "tampered" without investigation. Accepted: fail-closed-but-noisy beats fail-open.
- This ADR does **not** defend against an attacker who controls both the ledger-writing machine **and** the git remote's admin settings simultaneously — that is a full compromise of the operator's own infrastructure, out of scope for a tamper-evidence design and consistent with every other ADR-027-adjacent guarantee in this system.

### 6. Migration + flag

`VNX_CHAIN_RECEIPTS` stays default-**OFF**. This ADR is the precondition for a future flip, not the flip itself — no behavior changes for any ledger until the follow-up implementation lands, and no ledger anywhere starts chaining because of this document.

**Verified fleet state (2026-07-15):** none of the three live project ledgers checked (`vnx-dev`, `mission-control`, `seocrawler-v2` — `state/t0_receipts.ndjson`) contain a single `chain_epoch_start` marker. The ADR-029 seal migration was designed but never actually run fleet-wide. **There is no existing sealed epoch anywhere that this ADR's fail-closed behavior would retroactively break.** This means the follow-up implementation can ship `verify_chain`'s new fail-closed anchor requirement directly — no backfill migration is required for current state.

For completeness (future-proofing, in case a ledger is manually seeded before the follow-up ships, or for any project onboarded later): `seal_and_commit_origin` **is** the backfill mechanism — it is idempotent and safe to run against a ledger that already has an open epoch but no anchor yet (step 1 no-ops, only the missing anchor commit is produced). No separate backfill script is needed.

**Adoption sequencing for the follow-up dispatch**, mirroring ADR-029's reversible-steps structure. Step 2b is a **hard activation precondition** (design-review C4), not a follow-up item — step 3 (the fail-closed flip) is blocked on it passing, not merely recommended after it:

1. Ship `compute_epoch_fingerprint`, `seal_and_commit_origin`, `read_git_anchor`, `ledger_lock_path` — additive, `verify_chain`'s read path unchanged, `fabric-audit` check C unaffected.
2. **(2a)** Add the `governance/chain-origin.ndjson` path + the anchor-immutability required check to each project repo's branch protection, required + `enforce_admins` from the same PR (§3 — no staged-advisory period). **(2b — hard gate)** Verify, per project, that branch protection is *actually* configured as claimed: required-status-checks includes the anchor-immutability check by name, `enforce_admins` is true, and force-push is disallowed on the anchor branch. This is a machine-checkable precondition (`gh api repos/:owner/:repo/branches/:branch/protection`), not an assumption — `seal_and_commit_origin` itself refuses to seal (raises, does not silently proceed) if it cannot confirm 2b for the target repo, and the follow-up dispatch's own DoD includes running this check against every project before step 3 begins.
3. Flip `verify_chain` to the new fail-closed contract (§2) + thread `project_root` through every call site (`fabric_audit.py` check C, `chain_epoch_seal.py`, `evidence_bound_gate.py`, `governance_effectiveness_probe.py`, `audit_chain.py` — six call sites, confirmed by `grep -rl "verify_chain(" scripts/`). **Gated on 2b passing for every project this flip applies to** — a project whose branch protection cannot be verified active does not get the fail-closed contract turned on for its ledgers yet. Since §6's fleet check found zero live markers, this step is otherwise a pure code change with no fleet-wide RED risk today.
4. Re-verify `fabric-audit` check C GREEN fleet-wide.
5. Only then does a future ADR/flip consider `VNX_CHAIN_RECEIPTS` default-on — unchanged from ADR-029, still gated on its own separate decision.

### 7. Interface sketch

No bodies — signatures only, for the follow-up implementation dispatch. All new code lives in `scripts/lib/chain_origin_anchor.py` (the module name `#1171` used; that PR was never merged, so there is no collision — this is a fresh implementation against this design, not a resurrection of the closed PR's code).

```python
# scripts/lib/chain_origin_anchor.py

ANCHOR_REL_PATH = Path("governance/chain-origin.ndjson")  # NOT under docs/ — see §3 placement note

def ledger_identity(project_id: str, ledger_path: Path, project_data_dir: Path) -> str:
    """Stable logical key: '{project_id}:{ledger_path relative to project_data_dir}'.
    Never an absolute local filesystem path (see §1). Combined with an epoch
    number (below) to form the actual anchor-file key — see anchor_key()."""

def anchor_key(identity: str, epoch: int) -> str:
    """'{identity}#{epoch}' — the actual key used in governance/chain-origin.ndjson
    and by read_git_anchor/duplicate-detection. One committed record per SEALED
    EPOCH, not one per ledger (§1 R4 fix) — closes the tail-truncation-via-new-
    epoch bypass a single per-ledger origin record could not."""

def ledger_lock_path(ledger_path: Path) -> Path:
    """'{ledger_path.parent}/append_receipt.lock' — the ONE public lock primitive
    for a given ledger (§4 R2/C2 fix). Byte-identical to the path the existing
    receipt hot path already locks (previously private as
    append_receipt_internals.idempotency._lock_file_for; now the single shared
    source of truth). append_receipt_payload, append_chained_entry,
    append_epoch_marker, and seal_and_commit_origin all take this SAME lock
    around their critical sections — not independent locks on different paths,
    which was the original design's bug (§4)."""

def compute_epoch_fingerprint(ledger_path: Path, epoch: int) -> OriginFingerprint:
    """Pure computation over the CURRENT tail of ledger_path for the given
    sealed epoch. Caller must hold ledger_lock_path(ledger_path)'s flock (see
    seal_and_commit_origin) — NOT a lock on ledger_path directly (§4). Returns
    {ledger_identity, epoch, origin_type, origin_hash, origin_line_number,
    entries_before_origin}. (Renamed from compute_ledger_origin: 'origin' now
    means 'this epoch's seal point', not necessarily the ledger's first epoch.)"""

def seal_and_commit_origin(
    ledger_path: Path,
    project_root: Path,
    *,
    project_id: str,
    project_data_dir: Path,
    branch: str = "main",
) -> SealResult:
    """Operator/T0-governed entrypoint — excluded from every autonomous WORKER
    role's tool set (scripts/lib/worker_permissions.py PermissionProfile); lands
    via a normal dispatch PR through the existing CI + codex-gate + attestation
    pipeline, never a direct unreviewed push. Holds
    fcntl.flock(ledger_lock_path(ledger_path), LOCK_EX) — the SAME public lock
    append_receipt_payload/append_chained_entry/append_epoch_marker take, not a
    lock on ledger_path itself (§4 correction) — across the full
    read-current-epoch -> compute -> anchor sequence. REFUSES (raises, no seal
    attempted) if it cannot verify branch protection is active on `branch` with
    the anchor-immutability check required (§6 step 2b — hard activation
    precondition, not best-effort). Idempotent per epoch: an anchor_key(identity,
    epoch) already present in the anchor file (read from the actor's own current
    HEAD, pre-push) is a no-op for that epoch; a NEW epoch since the last seal
    produces a new anchor record. Returns SealResult{action: "sealed"|"noop",
    epoch, origin, branch_name, pr_url|None}. Raises on git/push failure rather
    than swallowing it — see §4 for why a failed push still leaves verify_chain
    correctly fail-closed."""

def read_git_anchor(
    project_root: Path,
    identity: str,
    epoch: int,
    *,
    anchor_rel_path: Path = ANCHOR_REL_PATH,
    ref: str = "origin/main",
) -> tuple[AnchorRecord | Literal["corrupt"] | None, AnchorProvenance]:
    """Resolves anchor_key(identity, epoch)'s record from GIT-COMMITTED content
    only (§2): live fetch + `git show <ref>:...`, falling back to the local
    remote-tracking ref on network failure. NEVER reads the working-tree file.
    Returns the FIRST matching record in file order; "corrupt" if more than one
    record matches the same anchor_key (dupes=broken, §3). None only when ref
    resolves cleanly with genuinely no record for this key. ALSO returns
    AnchorProvenance{anchor_commit_sha, remote_url, ref} describing exactly what
    was resolved and from where (§2, §5 R3/C3 fix) — populated even when the
    record itself is None/"corrupt", so a caller can always show what was
    checked for the off-host cross-check, not just the verdict."""

def verify_chain(
    path: Path,
    *,
    project_root: Path | None = None,
    project_id: str | None = None,
    project_data_dir: Path | None = None,
    anchor_ref: str = "origin/main",
) -> tuple[bool, list[dict], str, AnchorProvenance | None]:
    """Existing ADR-029 epoch-aware walk, PLUS (§2):
    - Forward: whenever the walk observes a chain_epoch_start marker for epoch
      N, a matching git anchor for anchor_key(identity, N) is now REQUIRED —
      its absence, a resolution failure, or a "corrupt" (duplicate) anchor all
      report "broken". Checked for EVERY epoch observed, not only the first
      (§1/§2 R4 fix).
    - Reverse: resolved BEFORE the empty/missing-ledger short-circuit — if any
      anchor exists for this ledger_identity at any epoch and the ledger walk
      does not independently confirm that epoch is chained, that is "broken"
      too (§2 R1/C1 fix). A missing/empty ledger is only "unchained" once this
      check confirms no anchor exists for it anywhere.
    Unchained ledgers with no anchor anywhere are unaffected. The 4th return
    value carries the AnchorProvenance from the last anchor resolution
    attempted (or None if the walk never needed one, e.g. genuinely unchained
    with no anchor) for off-host cross-check (§2, §5). Six call sites to
    update, all now unpacking a 4-tuple: fabric_audit.py check C,
    chain_epoch_seal.py, evidence_bound_gate.py,
    governance_effectiveness_probe.py, audit_chain.py, and this module's own
    seal-verification self-check."""
```

```yaml
# .github/workflows/ (extend attestation-gate.yml or a sibling workflow) — §3 write-side fix
# Required check from the PR that introduces it — NOT continue-on-error: true,
# unlike attestation-gate.yml's own current staged-advisory rollout (§3 C4 fix).
# Diffs governance/chain-origin.ndjson base vs head, keyed on '{ledger_identity}#{epoch}'
# (§1 R4 fix). Reuses the base-branch-trust pattern already in attestation-gate.yml
# (verifier extracted from origin/main, never the PR head). Fails the PR if any
# line whose key existed in the base-branch copy changed content or was removed.
# Only new keys (an epoch's first-ever seal) may be added. Required in branch
# protection + enforce_admins from day one — verified per §6 step 2b before
# verify_chain's fail-closed contract (§2) is trusted for that project.
```

**Required test list for the implementation dispatch** (each maps to a named hole):

| # | Test | Hole |
|---|------|------|
| T1 | Chaining-enabled ledger, no anchor for its `ledger_identity` at any epoch anywhere in git history → `verify_chain` returns `"broken"` | #1 deletion fail-open |
| T2 | Same ledger, but the **local working-tree copy** of `governance/chain-origin.ndjson` is hand-edited/deleted while the git-committed ref is untouched → `verify_chain` result is unaffected by the working-tree edit (proves the read path ignores it) | #1 |
| T3 | `project_root=None` passed for a chaining-enabled ledger → `"broken"` (cannot silently skip the anchor check) | #1 |
| T4 | Unchained ledger (no marker anywhere), no anchor for this `ledger_identity` at any epoch → stays `"unchained"` (ADR-029 backward-compat regression guard) | regression |
| T5 | Two committed records share the same `{ledger_identity}#{epoch}` key (simulated forged append) → `read_git_anchor` returns `"corrupt"` → `verify_chain` `"broken"` | #2 append-forgery (read side) |
| T6 | A PR modifying an existing `{ledger_identity}#{epoch}` key's already-committed line in `governance/chain-origin.ndjson` is rejected by the anchor-immutability CI check | #2 (write side) |
| T7 | A PR appending a brand-new `{ledger_identity}#{epoch}` key (a different ledger's first seal, or the next epoch of an already-anchored ledger) is accepted and leaves every existing record byte-identical | #2 regression |
| T8 | Full attack repro: strip a ledger's prefix, insert a fresh `chain_epoch_start`, re-chain the remainder (the exact #1085/#1086/#1171 bypass) → the git-anchored `origin_hash`/`origin_line_number` no longer matches the ledger's observed origin → `"broken"` | the core bypass, all three holes together |
| T9 | Concurrent receipt append while `seal_and_commit_origin` holds `ledger_lock_path(ledger_path)`'s flock → the append blocks/serializes rather than interleaving; post-seal ledger + anchor are mutually consistent | #3 seal race |
| T10 | `seal_and_commit_origin` fails after marker-append but before anchor push (simulated) → `verify_chain` on the partially-sealed ledger reports `"broken"` (not falsely healthy), and re-running `seal_and_commit_origin` completes idempotently | #3 + fail-closed |
| T11 | Running `seal_and_commit_origin` twice on an already-sealed epoch is a no-op; a NEW epoch since the last seal produces a new anchor record rather than being skipped (existing ADR-029 idempotency guarantee preserved, generalized per-epoch) | regression |
| T12 | Full existing suite (`test_ndjson_hash_chain.py`, `test_chain_epoch_seal.py`, `test_fabric_audit.py`, `test_governance_effectiveness_probe.py`) stays green; `VNX_CHAIN_RECEIPTS` unset behavior is byte-for-byte unchanged | regression / flag discipline |
| T13 | Reverse-direction repro: seal + anchor a ledger normally, then `rm` (or truncate to zero bytes) the ledger file entirely while its git anchor stays committed → `verify_chain` returns `"broken"`, NOT `"unchained"` (proves the anchor-existence check runs before the empty-ledger short-circuit) | #1 reverse / deletion fail-open, R1/C1 |
| T14 | `seal_and_commit_origin` and a concurrent `append_receipt_payload` call target the SAME lock file (`ledger_lock_path(ledger_path) == append_receipt_internals path` for `state/t0_receipts.ndjson`) — assert identical resolved `Path`, not just "both take some lock" (proves the original wrong-lock-target bug (§4) can't silently regress) | #3 seal race, R2/C2 |
| T15 | Seal epoch 1 and anchor it; strip the ledger's tail after epoch 1, open a fresh epoch 2 with a `chain_epoch_start` marker, re-chain the remainder, but do NOT anchor epoch 2 in git → `verify_chain` returns `"broken"` even though epoch 1's anchor still matches perfectly (proves per-epoch anchoring, not just origin-checking, closes the bypass) | R4 tail-truncation-via-new-epoch |
| T16 | `seal_and_commit_origin` invoked against a repo whose branch protection does NOT have the anchor-immutability check in required-status-checks (simulated `gh api` response) → raises, no commit/push attempted, no partial anchor state left behind | C4 branch-protection hard precondition |

## Consequences

### Accepted
- `verify_chain` gains two new required parameters (`project_root`, plus project identity) and a 4th return value (`AnchorProvenance`, §7) for any caller that wants anchor-aware verification; the six existing call sites all need updating in the follow-up PR (Codex Defense Checklist: "all call sites use the helper" — flagged explicitly so the implementation doesn't ship an asymmetric fix on some callers only).
- A new top-level `governance/` directory (not `docs/governance/`) appears in every VNX-governed project's repo the first time that project's ledger is sealed.
- Sealing now requires a small PR + the anchor-immutability check to pass, not a single local script run — **and now one such PR per sealed epoch, not one ever per ledger** (§1 R4 fix). Given seals are rare (epoch rotations), this is accepted cost for closing a hole that already burned three PRs.
- `fabric-audit` check C's per-project loop gains a dependency on each project's repo being reachable locally (it already has each project's path via the project registry — `fabric_audit.py`'s `projects: list[tuple[str, str]]`).
- Activation now requires a verified branch-protection precondition (§6 step 2b) per project before that project's ledgers get the fail-closed contract — this is a real rollout dependency, not a formality: a project whose branch protection can't be confirmed active stays on the old (pre-ADR-034) `verify_chain` behavior until it is (§6 C4 fix).
- `append_chained_entry` and `append_epoch_marker` gain locking they did not have before (§4 R2/C2 fix) — a behavior change for their existing (unlocked) callers, most notably `plan_gate_evidence.py` and `delegation_mandate.py` (§4 sibling-ledger note), which is accepted as a correctness fix, not treated as a compatibility break.

### Rejected
- Anchoring in the fabric repo (option c) — public/private repo mismatch leaks private-project metadata (§1).
- A dedicated anchor repo (option b) — no benefit over the project's own repo, adds a new credential surface (§1).
- Trusting the working-tree copy of the anchor file at read time — reproduces hole #1 in a new location (§2).
- "Last write wins" anchor resolution — reproduces hole #2 (§3).
- A DB-backed origin store (PR #1086's approach) — already HELD through four codex rounds on mutability/atomicity grounds; not reconsidered here.
- One anchor record per ledger, checking only the origin epoch (the original §1 design) — reproduces R4's tail-truncation-via-new-epoch bypass; superseded by one record per sealed epoch.
- An opaque commitment hash replacing the anchor record's plaintext fields (§1 C5) — considered for privacy, set aside because it doesn't defend against the actual threat model's audience (an on-disk ledger reader, not a repo-read-only observer) and adds an indirection cost this ADR does not see a justified need for today.
- A staged-advisory rollout for the anchor-immutability CI check, mirroring `attestation-gate.yml`'s own `continue-on-error: true` precedent (§3 C4) — rejected because an advisory-only write-side check is equivalent to no write-side defense for this ADR's threat model.

## See also

- ADR-005 — Append-only NDJSON ledger (receipts stay local; this ADR anchors a fingerprint, not receipts)
- ADR-023 — Receipt hash-chain (experimental opt-in this ADR eventually hardens)
- ADR-026 — Per-project store canonical; only the governance/process signal class centralizes (the reasoning this ADR's §1 rejection of option (c) extends)
- ADR-027 — Signed-attestation enforcement at PR merge (the merge-boundary trust model and base-branch-trust CI pattern this ADR reuses rather than reinventing)
- ADR-029 — Hashchain epoch-rotation (the epoch/marker machinery this ADR builds on; the "accepted residual" note this ADR closes and removes)
- PR #1085, #1086, #1171 (all closed) — the three failed attempts; OI-628 — the tracked finding that names the three holes this ADR closes
- `scripts/lib/ndjson_hash_chain.py`, `scripts/chain_epoch_seal.py` — existing epoch machinery, unchanged by this ADR except for `verify_chain`'s new required-anchor contract
- `.github/workflows/attestation-gate.yml` — the base-branch-trust CI pattern this ADR's anchor-immutability check reuses
