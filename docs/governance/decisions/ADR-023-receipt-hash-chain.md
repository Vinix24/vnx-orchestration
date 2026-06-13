# ADR-023 — Receipt Hash-Chain for Tamper-Evidence

**Status:** Accepted
**Date:** 2026-06-13
**Decided by:** Operator (Vincent van Deth)
**References:** ADR-005 (NDJSON ledger-first), ADR-006 (staging→promote human gate), Task #17 PR-1 (hash-chain primitives), PR #840 (3-state verify model)

## Context

ADR-005 establishes the append-only NDJSON ledger as the canonical audit surface and argues it is "tamper-evident by construction" because append-only files have a one-way write semantic. That argument is correct for *accidental* mutation: a retroactive edit changes file mtime and size, and the operator's backup and forensic workflows lean on that monotonicity.

It is not sufficient for *deliberate* tampering. Append-only is a convention, not a cryptographic property. Nothing in the ledger format stops an actor with write access from rewriting a past line, deleting an entry, or splicing a fabricated one in the middle. For a system whose entire pitch is glass-box governance, "we promise we only append" is a weak claim. A reviewer cannot independently verify it from the file alone.

The codebase already ships the missing piece, but it was undocumented in any ADR or core doc:

- `scripts/lib/ndjson_hash_chain.py` — chain primitives (`compute_entry_hash`, `append_chained_entry`, `verify_chain`, `walk_chain`).
- `scripts/audit_chain.py` — the `verify` / `walk` CLI.
- `VNX_CHAIN_RECEIPTS=1` — the opt-in environment flag, wired into the receipt append path at `scripts/lib/append_receipt_internals/idempotency.py`.

This ADR codifies the hash-chain as the tamper-evidence implementation behind ADR-005's claim.

## Decision

**NDJSON ledger entries MAY carry a `prev_hash` field that references the SHA-256 of the prior entry's canonical-JSON body, forming a tamper-evident chain. Chaining is opt-in via `VNX_CHAIN_RECEIPTS=1` and is ADDITIVE metadata, not a replacement for the NDJSON-first principle of ADR-005.**

### Stored field: `prev_hash` only

Each chained entry carries exactly one chain field:

- `prev_hash` (string, 64 hex chars) — the entry hash of the immediately preceding entry in the file. The first entry in a chain uses the genesis sentinel `"0" * 64`.

There is no stored `entry_hash` field. An entry's own hash is *computed*, never persisted, by `compute_entry_hash()`: the SHA-256 of the entry's canonical JSON with `prev_hash` excluded (`sort_keys=True`, no whitespace). Excluding `prev_hash` from the hashed body is what lets the chain reference an entry without the reference altering the thing it points at. Verification recomputes each entry's hash on the fly and checks that the next entry's `prev_hash` matches.

### Opt-in via `VNX_CHAIN_RECEIPTS=1`

Default is OFF. Recognised truthy values: `1`, `true`, `yes`, `on` (case-insensitive). When OFF, the receipt append path is byte-for-byte unchanged — no `prev_hash`, no import-time dependency on the chain module. When ON, the receipt writer reads the file's tail hash and stamps `prev_hash` on the new entry *inside the same append lock* (`fcntl.flock(LOCK_EX)`) that serializes the write. This serialization is load-bearing: without it, two concurrent appends would read the same tail hash and fork the chain.

### Three-state verify model

`audit_chain.py verify <path>` (via `verify_chain()`) returns one of three states. This is not a boolean. A glass-box audit needs to distinguish "chaining was never enabled" from "the chain is broken," because those carry opposite trust signals.

| State | Meaning | Exit | `is_valid` |
|---|---|---|---|
| `unchained` | No entry in the file carries `prev_hash`. Chaining was never enabled (default). Integrity cannot be verified, and that is **not an error**. Output includes a hint to enable `VNX_CHAIN_RECEIPTS=1`. | 0 | True |
| `verified` | Every entry carries `prev_hash` and the chain links intact end to end. | 0 | True |
| `broken` | A chain is present but has an integrity violation: a tampered or spliced entry, an unparseable line, or a **partial** chain. | 1 | False |

Empty or missing file is `unchained` (exit 0) — a fresh ledger is honestly "not yet chained," not broken.

### Partial chain is `broken`, not `unchained`

A ledger must be either fully unchained or fully chained to be healthy. A ledger where some entries carry `prev_hash` and others do not is classified `broken`, even when every present `prev_hash` links correctly.

One edge case makes this guard load-bearing rather than cosmetic. The first entry in a chain is allowed to carry no `prev_hash` (or the genesis sentinel). If the first entry is unchained but every later entry hash-links correctly to its predecessor, the link-by-link comparison produces zero mismatches — the loop alone would pass it. Only the count check (`chained_count < total` → `broken`) catches that case. Without it, an attacker could strip the genesis anchor and present a chain that "verifies link by link" while having no fixed start. So: first-entry-unchained-but-rest-linked is explicitly `broken`.

### Event-type conventions for corrections

Append-only means past entries are never edited in place. Corrections are new entries that reference what they supersede. The chain primitives recognise:

- `backfill` — historical entry added retroactively; `prev_hash` set to genesis.
- `correction` — supersedes a prior entry; must carry `corrects_hash`.
- `redaction` — removes content from a prior entry; must carry `redacts_hash`; body is tombstoned, chain preserved.
- `tombstone` — marks an entry withdrawn; must carry `tombstones_hash`; entry-id preserved for chain continuity.

These keep the chain continuous through real-world edits without ever mutating a hashed body.

## Reasoning

1. **Upgrades ADR-005's claim from convention to proof.** ADR-005 reason #2 ("tamper-evident by construction") rests on append-only file semantics and mtime/size monotonicity — sufficient against accident, not against intent. The hash-chain makes the claim independently verifiable: a reviewer runs `audit_chain.py verify` and gets a cryptographic yes/no, no trust in the operator required.

2. **Opt-in keeps the default path untouched.** Most deployments do not need cryptographic chaining and should not pay its cost or complexity. Gating on `VNX_CHAIN_RECEIPTS` means the OFF path is byte-identical to the pre-chain writer, so enabling it is a pure add and disabling it never corrupts an existing ledger. High-assurance deployments (compliance, customer audits) flip the flag.

3. **Three states beat a boolean.** A two-state valid/invalid model would force a choice between calling a normal unchained ledger "invalid" (false alarm — chaining is off by default) or calling it "valid" (false confidence — nothing was actually verified). The `unchained` state names the honest third answer: "I cannot verify this, and here is how to enable verification." That distinction is the whole point of a glass-box audit.

4. **Partial-chain-is-broken closes a real splice attack.** Allowing a partial chain would let an attacker chain only the half of the ledger they fabricated and leave the rest "unchained," or strip the genesis anchor to float the chain's start. Demanding all-or-nothing removes that seam.

5. **Corrections stay append-only.** The `correction`/`redaction`/`tombstone` conventions let the ledger evolve (fix a wrong entry, redact secrets) without ever editing a hashed body — which would otherwise break every downstream link. The audit trail of the correction is itself a chained entry.

## Consequences

### Accepted

- The receipt schema gains one optional field, `prev_hash`, present only when `VNX_CHAIN_RECEIPTS=1`. Documented in `docs/core/11_RECEIPT_FORMAT.md`.
- `audit_chain.py verify` is the canonical integrity check. CI and pre-launch audits run it against `.vnx-data/state/t0_receipts.ndjson`; exit 1 (`broken`) fails the check, exit 0 (`unchained` or `verified`) passes, with `unchained` surfacing the enable-hint.
- High-assurance deployments enable `VNX_CHAIN_RECEIPTS=1` per-environment. The flag is read at receipt-append time, so it can be turned on going forward without rewriting history. Enabling mid-ledger transitions the file from fully-unchained to a chained tail; by the partial-chain rule that reads as `broken` until the unchained prefix is rotated out. Operators enabling chaining on an existing ledger should rotate (archive + truncate) the ledger file first so the new chain starts clean.
- `entry_hash` is never a stored field. Any tool or doc referencing a persisted `entry_hash` is wrong; the hash is always recomputed from canonical JSON.

### Rejected

- **Storing `entry_hash` on each entry.** Redundant (recomputable) and a tamper vector (an attacker could edit a body and update its stored hash). The hash is derived at verify time precisely so it cannot be forged in place.
- **Making chaining the default.** Rejected for 1.0. The OFF default keeps the common path simple and byte-stable; chaining is a deliberate opt-in for deployments that need it. Revisit for a future major version once enable-mid-ledger ergonomics (ledger rotation on enable) are smoother.
- **A two-state valid/invalid verify.** Rejected — collapses `unchained` into either a false alarm or false confidence. See Reasoning #3.
- **Allowing partial chains.** Rejected — see Reasoning #4. All-or-nothing is the only model that closes the splice and floating-start seams.

## Implementation note

- Primitives: `scripts/lib/ndjson_hash_chain.py`. Append-path integration: `scripts/lib/append_receipt_internals/idempotency.py` (`_chain_receipts_enabled`, tail-hash read under lock, `prev_hash` stamp before write). CLI: `scripts/audit_chain.py`.
- The tail-hash read and `prev_hash` stamp MUST happen inside the existing append `LOCK_EX` critical section. The hash-chain module exposes `append_chained_entry()` for standalone use, but the receipt path deliberately inlines the tail read so it never opens a second file handle outside the lock (fork-the-chain risk under concurrency).
- `audit_chain.py walk <path>` emits `(line_number, short_hash, event_type)` per entry for forensic inspection of a specific chain.

## See also

- ADR-005 — Append-only NDJSON ledger (this ADR is the tamper-evidence implementation behind ADR-005 reason #2; ADR-005 carries an amendment cross-referencing this ADR)
- ADR-006 — Staging→promote human gate (the gate's evidence trail is the chained ledger)
- ADR-021 — Exception discipline (receipt/append writers raise on OSError, never silently drop a chained write)
- `docs/core/11_RECEIPT_FORMAT.md` — receipt schema including the optional `prev_hash` field
- `docs/operations/RECEIPT_PIPELINE.md` — where the chained append sits in the receipt flow
- `scripts/lib/ndjson_hash_chain.py`, `scripts/audit_chain.py`, `scripts/lib/append_receipt_internals/idempotency.py` — implementation surfaces
