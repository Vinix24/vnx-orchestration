# ADR-034 — External chain-origin anchor: a git-committed fingerprint that survives a local ledger-writer

**Status:** Proposed (design-only; precondition for `VNX_CHAIN_RECEIPTS` default-on — this ADR is not the flip)
**Date:** 2026-07-15
**Decided by:** Operator (Vincent van Deth). Design task only — no implementation, no tests in this PR; a follow-up dispatch implements it against this ADR.
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

**Chosen: (a) — a tracked path in the project's own source repo.** Concretely: `governance/chain-origin.ndjson` at the repo root (a new top-level dir, sibling to `docs/`, `scripts/`, `.vnx/`), one NDJSON line per sealed ledger.

Evaluated and rejected:

- **(b) A dedicated anchor repo/remote.** Rejected: a new repo to create, own, and provision credentials for, with no benefit over (a) — and it centralizes a single point of failure across every project instead of keeping each project's anchor inside a boundary that project already trusts.
- **(c) The fabric repo, `ledger/chain-origins/<project_id>.ndjson`.** Rejected on a concrete, verified fact: `vnx-orchestration` is a **public** GitHub repo (`gh repo view Vinix24/vnx-orchestration` → `"visibility":"PUBLIC"`), while every consumer project checked — `mission-control`, `SEOCRAWLER_V2`, `sales-copilot` — is **private**. Anchoring a private project's fingerprint (existence, seal timestamps, receipt-count cadence) inside the public fabric repo leaks business-activity metadata about private client/product work into a public repository. That is a confidentiality break the receipts-stay-local constraint is explicitly trying to avoid one layer up. Each project's own repo already carries the correct privacy level for that project — anchoring there costs nothing extra and leaks nothing new.

This also matches an existing convention rather than adding one: `scripts/lib/project_root.py::resolve_project_id` already falls back to the project's **git remote `origin` URL** as the canonical source of a project's identity (ADR-007). The project's own repo is already treated as that project's root of trust elsewhere in VNX; this ADR just anchors the ledger fingerprint in the same place.

**`ledger_identity` is a stable logical key, not a local path.** The closed #1171 design used `str(ledger_path.resolve())` — an absolute local filesystem path — as the anchor key. That leaks local machine layout into a committed file and is not portable. This ADR uses `"{project_id}:{ledger_path relative to the project's central data dir}"`, e.g. `"vnx-dev:state/t0_receipts.ndjson"`, matching the ADR-007 `project_id` convention already used everywhere else in VNX.

**Creds / push-on-seal implications.** No new credentials: sealing pushes to the SAME repo and remote that every dispatch worker already pushes feature branches to and opens PRs against. There is no cross-repo auth to provision. Seals are rare (epoch rotations, not the per-receipt hot path) — a commit + PR + push per seal is cheap.

### 2. Fail CLOSED, not open

`verify_chain` changes its contract for any ledger where `saw_chain` is true (at least one `chain_epoch_start` marker or chained entry observed — i.e. chaining is enabled on that ledger, regardless of `VNX_CHAIN_RECEIPTS`):

- **No `project_root` supplied**, or the anchor cannot be resolved from git-committed history (see §3) in a way distinguishable from "genuinely never sealed" → **`"broken"`**. A chaining-enabled ledger with no verifiable anchor is now itself the violation — this directly replaces the ADR-029 "accepted residual" note, which is deleted by this ADR, not merely narrowed.
- **Anchor resolves but the ledger's own observed origin (hash / line number / epoch) doesn't match the anchored record** → `"broken"` (prefix-strip / moved-boundary — the exact bypass #1085/#1086/#1171 all targeted).
- **Anchor resolves and matches** → `"verified"` / `"verified-segmented"`, unchanged from ADR-029.
- Ledgers that have **never** enabled chaining (`saw_chain` is false, no marker anywhere) are **unaffected** — `"unchained"` stays OK exactly as ADR-029 defined it. This ADR only tightens the case where chaining is already on.

The critical mechanism that makes this fail-closed rather than a repeat of hole #1: **the anchor is read from git-committed history, never from the working-tree copy of `governance/chain-origin.ndjson`.** A plain tracked file sitting on disk is exactly as locally-editable as the ledger itself — git tracking alone is not a defense; only the *committed, pushed* content is. Read order (§3 gives signatures):

1. `git fetch origin` + `git show origin/<protected-branch>:governance/chain-origin.ndjson` — the live remote, strongest guarantee.
2. If the network is unavailable: `git show <local-remote-tracking-ref>:governance/chain-origin.ndjson` (e.g. `refs/remotes/origin/main` from the last successful fetch) — still resolved through git's content-addressed object store, not the working tree. Forging this would mean corrupting local git objects, not editing a file — a categorically higher bar, and one `git fsck` can detect.
3. If neither resolves (fresh clone, never fetched, or `project_root` isn't a git repo at all) → fail closed, `"broken"`. "Can't check" is never treated as "assume fine."

### 3. Canonical, not last-writer

Two independent fixes close the append-forgery hole (#1171 hole #2), one on the read side and one on the write side — defense in depth, not either/or:

- **Read side — first record, duplicates are a violation.** `read_git_anchor` parses every line and returns the **first** record matching a given `ledger_identity`. If a second record for the same `ledger_identity` exists anywhere in the file, that is **not** "last write wins" — it is treated as `"corrupt"`, which `verify_chain` maps to `"broken"`. An appended forged record can no longer win by virtue of being appended later.
- **Write side — structural immutability, enforced in CI.** `governance/chain-origin.ndjson` gets a required GitHub Actions check, reusing the exact base-branch-trust pattern already shipped for ADR-027's D3 attestation gate (`.github/workflows/attestation-gate.yml`: the verifier is extracted from `origin/main`, never from the PR head, so a PR cannot neuter the check that reviews it). The new check diffs `governance/chain-origin.ndjson` between base and head: **any line whose `ledger_identity` already existed in the base-branch copy but changed content, and any removed line, fails the check.** Only brand-new `ledger_identity` lines (a ledger's first-ever seal) may be added. This makes a forged-modification PR unmergeable by construction, not by convention.

**Placement detail that matters:** the attestation gate's exempt allowlist currently skips `docs/`, `tests/`, and `*.md` (`.github/workflows/attestation-gate.yml` header comment). That is exactly why `governance/chain-origin.ndjson` is **not** placed under `docs/governance/` — it would silently inherit the docs exemption and a forged anchor PR would sail through unattested. The chosen top-level `governance/` path falls outside all three exemptions by construction, so both the attestation gate and the new anchor-immutability check apply to it.

### 4. Atomic seal

`seal_and_commit_origin` (§7) holds a single `fcntl.flock(ledger_path, LOCK_EX)` across the **entire** read-origin → compute → anchor sequence:

1. Inspect `epoch_state(ledger_path)`; append a `chain_epoch_start` marker if none is open (existing ADR-029 `append_epoch_marker`, unchanged).
2. Compute the origin fingerprint from the now-stable ledger tail.
3. Commit + push the anchor record.

This is the **same** lock primitive `append_chained_entry` already takes on the hot receipt-append path (`ndjson_hash_chain.py`), so a concurrent receipt append and a seal are mutually exclusive by construction — a writer appending mid-seal blocks on the lock rather than racing a forged/partial origin in. This closes #1171 hole #3 directly.

**Failure is naturally fail-closed, not a rollback problem.** If step 3 (push) fails after step 1 (marker append) already landed locally, the ledger now has an open epoch with no matching anchor yet — which §2's fail-closed rule already reports as `"broken"`. There is no window where a half-sealed ledger reads as healthy. Re-running `seal_and_commit_origin` is idempotent: step 1 no-ops (marker already open), and only the missing anchor commit is completed.

### 5. Threat model — honest bar, honest residual

**New bar.** A local actor with write access to the ledger (the entire threat class #1085/#1086/#1171 were scoped to) can no longer strip-and-reseal, delete the anchor, or win by appending a forged record, and can no longer land a forged anchor PR without the anchor-immutability check catching the modification. Deletion/truncation of the *local working-tree copy* of the anchor file is a no-op against verification, because verification never trusts that copy.

**Residual — stated plainly, not overclaimed:**

- An actor with genuine **push access to the remote and a way past branch protection** (a leaked credential with admin rights, a misconfigured "allow force-push" setting, or the operator's own account being compromised) can still rewrite history and forge an anchor. This is the same residual ADR-027 already accepts for code attestation generally ("for an actor with full local write access, no client-side layer is un-bypassable... key custody" — ADR-027 Context) — this ADR does not claim to exceed that bar, only to reuse it.
- A **compromised or disabled branch-protection configuration** on the project repo (required-status-checks turned off, `enforce_admins` disabled) removes the anchor-immutability guarantee. This is an operational/config risk, not a design gap — `vnx doctor`/`fabric-audit` should grow a check that branch protection is actually configured as required (tracked as a follow-up, not this ADR).
- **Offline verification degrades, it does not silently pass.** If neither a live fetch nor a local remote-tracking ref resolves, verification fails closed (`"broken"`), which is safe but means a genuinely offline environment cannot distinguish "no network" from "tampered" without investigation. Accepted: fail-closed-but-noisy beats fail-open.
- This ADR does **not** defend against an attacker who controls both the ledger-writing machine **and** the git remote's admin settings simultaneously — that is a full compromise of the operator's own infrastructure, out of scope for a tamper-evidence design and consistent with every other ADR-027-adjacent guarantee in this system.

### 6. Migration + flag

`VNX_CHAIN_RECEIPTS` stays default-**OFF**. This ADR is the precondition for a future flip, not the flip itself — no behavior changes for any ledger until the follow-up implementation lands, and no ledger anywhere starts chaining because of this document.

**Verified fleet state (2026-07-15):** none of the three live project ledgers checked (`vnx-dev`, `mission-control`, `seocrawler-v2` — `state/t0_receipts.ndjson`) contain a single `chain_epoch_start` marker. The ADR-029 seal migration was designed but never actually run fleet-wide. **There is no existing sealed epoch anywhere that this ADR's fail-closed behavior would retroactively break.** This means the follow-up implementation can ship `verify_chain`'s new fail-closed anchor requirement directly — no backfill migration is required for current state.

For completeness (future-proofing, in case a ledger is manually seeded before the follow-up ships, or for any project onboarded later): `seal_and_commit_origin` **is** the backfill mechanism — it is idempotent and safe to run against a ledger that already has an open epoch but no anchor yet (step 1 no-ops, only the missing anchor commit is produced). No separate backfill script is needed.

**Adoption sequencing for the follow-up dispatch**, mirroring ADR-029's reversible-steps structure:
1. Ship `compute_ledger_origin`, `seal_and_commit_origin`, `read_git_anchor` — additive, `verify_chain`'s read path unchanged, `fabric-audit` check C unaffected.
2. Add the `governance/chain-origin.ndjson` path + the anchor-immutability required check to each project repo's branch protection.
3. Flip `verify_chain` to the new fail-closed contract (§2) + thread `project_root` through every call site (`fabric_audit.py` check C, `chain_epoch_seal.py`, `evidence_bound_gate.py`, `governance_effectiveness_probe.py`, `audit_chain.py` — six call sites, confirmed by `grep -rl "verify_chain(" scripts/`). Since §6's fleet check found zero live markers, this step is a pure code change with no fleet-wide RED risk today.
4. Re-verify `fabric-audit` check C GREEN fleet-wide.
5. Only then does a future ADR/flip consider `VNX_CHAIN_RECEIPTS` default-on — unchanged from ADR-029, still gated on its own separate decision.

### 7. Interface sketch

No bodies — signatures only, for the follow-up implementation dispatch. All new code lives in `scripts/lib/chain_origin_anchor.py` (the module name `#1171` used; that PR was never merged, so there is no collision — this is a fresh implementation against this design, not a resurrection of the closed PR's code).

```python
# scripts/lib/chain_origin_anchor.py

ANCHOR_REL_PATH = Path("governance/chain-origin.ndjson")  # NOT under docs/ — see §3 placement note

def ledger_identity(project_id: str, ledger_path: Path, project_data_dir: Path) -> str:
    """Stable logical key: '{project_id}:{ledger_path relative to project_data_dir}'.
    Never an absolute local filesystem path (see §1)."""

def compute_ledger_origin(ledger_path: Path) -> OriginFingerprint:
    """Pure computation over the CURRENT tail of ledger_path. Caller must hold
    ledger_path's flock (see seal_and_commit_origin). Returns
    {ledger_identity, origin_type, origin_hash, origin_line_number, origin_epoch,
    entries_before_origin}."""

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
    pipeline, never a direct unreviewed push. Holds fcntl.flock(ledger_path,
    LOCK_EX) across the full read-origin -> compute -> anchor sequence (§4).
    Idempotent: a ledger_identity already present in the anchor file (read from
    the actor's own current HEAD, pre-push) is a no-op. Returns
    SealResult{action: "sealed"|"noop", origin, branch_name, pr_url|None}.
    Raises on git/push failure rather than swallowing it — see §4 for why a
    failed push still leaves verify_chain correctly fail-closed."""

def read_git_anchor(
    project_root: Path,
    ledger_identity: str,
    *,
    anchor_rel_path: Path = ANCHOR_REL_PATH,
    ref: str = "origin/main",
) -> AnchorRecord | Literal["corrupt"] | None:
    """Resolves ledger_identity's origin from GIT-COMMITTED content only (§2):
    live fetch + `git show <ref>:...`, falling back to the local
    remote-tracking ref on network failure. NEVER reads the working-tree file.
    Returns the FIRST matching record in file order; "corrupt" if more than one
    record matches the same ledger_identity (dupes=broken, §3). None only when
    ref resolves cleanly with genuinely no record for this ledger_identity."""

def verify_chain(
    path: Path,
    *,
    project_root: Path | None = None,
    project_id: str | None = None,
    project_data_dir: Path | None = None,
    anchor_ref: str = "origin/main",
) -> tuple[bool, list[dict], str]:
    """Existing ADR-029 epoch-aware walk, PLUS (§2): whenever the walk observes
    any chained content, a matching git anchor is now REQUIRED — its absence,
    a resolution failure, or a "corrupt" (duplicate) anchor all report
    "broken". Unchained ledgers are unaffected. Six call sites to update:
    fabric_audit.py check C, chain_epoch_seal.py, evidence_bound_gate.py,
    governance_effectiveness_probe.py, audit_chain.py, and this module's own
    seal-verification self-check."""
```

```yaml
# .github/workflows/ (extend attestation-gate.yml or a sibling workflow) — §3 write-side fix
# Required check (not advisory): diffs governance/chain-origin.ndjson base vs head.
# Reuses the base-branch-trust pattern already in attestation-gate.yml (verifier
# extracted from origin/main, never the PR head). Fails the PR if any line whose
# ledger_identity existed in the base-branch copy changed content or was removed.
# Only new ledger_identity lines (first-ever seal) may be added.
```

**Required test list for the implementation dispatch** (each maps to a named hole):

| # | Test | Hole |
|---|------|------|
| T1 | Chaining-enabled ledger, no anchor for its `ledger_identity` anywhere in git history → `verify_chain` returns `"broken"` | #1 deletion fail-open |
| T2 | Same ledger, but the **local working-tree copy** of `governance/chain-origin.ndjson` is hand-edited/deleted while the git-committed ref is untouched → `verify_chain` result is unaffected by the working-tree edit (proves the read path ignores it) | #1 |
| T3 | `project_root=None` passed for a chaining-enabled ledger → `"broken"` (cannot silently skip the anchor check) | #1 |
| T4 | Unchained ledger (no marker anywhere), no anchor → stays `"unchained"` (ADR-029 backward-compat regression guard) | regression |
| T5 | Two committed records share the same `ledger_identity` (simulated forged append) → `read_git_anchor` returns `"corrupt"` → `verify_chain` `"broken"` | #2 append-forgery (read side) |
| T6 | A PR modifying an existing `ledger_identity`'s already-committed line in `governance/chain-origin.ndjson` is rejected by the anchor-immutability CI check | #2 (write side) |
| T7 | A PR appending a brand-new `ledger_identity` (a different ledger's first seal) is accepted and leaves the existing record byte-identical | #2 regression |
| T8 | Full attack repro: strip a ledger's prefix, insert a fresh `chain_epoch_start`, re-chain the remainder (the exact #1085/#1086/#1171 bypass) → the git-anchored `origin_hash`/`origin_line_number` no longer matches the ledger's observed origin → `"broken"` | the core bypass, all three holes together |
| T9 | Concurrent receipt append while `seal_and_commit_origin` holds the ledger flock → the append blocks/serializes rather than interleaving; post-seal ledger + anchor are mutually consistent | #3 seal race |
| T10 | `seal_and_commit_origin` fails after marker-append but before anchor push (simulated) → `verify_chain` on the partially-sealed ledger reports `"broken"` (not falsely healthy), and re-running `seal_and_commit_origin` completes idempotently | #3 + fail-closed |
| T11 | Running `seal_and_commit_origin` twice on an already-sealed ledger is a no-op (existing ADR-029 idempotency guarantee preserved) | regression |
| T12 | Full existing suite (`test_ndjson_hash_chain.py`, `test_chain_epoch_seal.py`, `test_fabric_audit.py`, `test_governance_effectiveness_probe.py`) stays green; `VNX_CHAIN_RECEIPTS` unset behavior is byte-for-byte unchanged | regression / flag discipline |

## Consequences

### Accepted
- `verify_chain` gains two new required parameters (`project_root`, plus project identity) for any caller that wants anchor-aware verification; the six existing call sites all need updating in the follow-up PR (Codex Defense Checklist: "all call sites use the helper" — flagged explicitly so the implementation doesn't ship an asymmetric fix on some callers only).
- A new top-level `governance/` directory (not `docs/governance/`) appears in every VNX-governed project's repo the first time that project's ledger is sealed.
- Sealing now requires a small PR + the anchor-immutability check to pass, not a single local script run. Given seals are rare (epoch rotations), this is accepted cost for closing a hole that already burned three PRs.
- `fabric-audit` check C's per-project loop gains a dependency on each project's repo being reachable locally (it already has each project's path via the project registry — `fabric_audit.py`'s `projects: list[tuple[str, str]]`).

### Rejected
- Anchoring in the fabric repo (option c) — public/private repo mismatch leaks private-project metadata (§1).
- A dedicated anchor repo (option b) — no benefit over the project's own repo, adds a new credential surface (§1).
- Trusting the working-tree copy of the anchor file at read time — reproduces hole #1 in a new location (§2).
- "Last write wins" anchor resolution — reproduces hole #2 (§3).
- A DB-backed origin store (PR #1086's approach) — already HELD through four codex rounds on mutability/atomicity grounds; not reconsidered here.

## See also

- ADR-005 — Append-only NDJSON ledger (receipts stay local; this ADR anchors a fingerprint, not receipts)
- ADR-023 — Receipt hash-chain (experimental opt-in this ADR eventually hardens)
- ADR-026 — Per-project store canonical; only the governance/process signal class centralizes (the reasoning this ADR's §1 rejection of option (c) extends)
- ADR-027 — Signed-attestation enforcement at PR merge (the merge-boundary trust model and base-branch-trust CI pattern this ADR reuses rather than reinventing)
- ADR-029 — Hashchain epoch-rotation (the epoch/marker machinery this ADR builds on; the "accepted residual" note this ADR closes and removes)
- PR #1085, #1086, #1171 (all closed) — the three failed attempts; OI-628 — the tracked finding that names the three holes this ADR closes
- `scripts/lib/ndjson_hash_chain.py`, `scripts/chain_epoch_seal.py` — existing epoch machinery, unchanged by this ADR except for `verify_chain`'s new required-anchor contract
- `.github/workflows/attestation-gate.yml` — the base-branch-trust CI pattern this ADR's anchor-immutability check reuses
