# ADR-033 — Hash-chain origin anchor: closing the prefix-strip / re-seal bypass

**Status:** Accepted (hardening, no behavior flip)
**Date:** 2026-07-15
**Decided by:** Worker dispatch `20260715-hashchain-anchor`, track `post10-hashchain-default-on` (deliverable `dlv-d2eac5fa32d1`). This is the "future epoch-seal-anchor ADR" that `ndjson_hash_chain.verify_chain`'s threat-model note (ADR-029) already referenced.
**Resolves / Cross-refs:** Extends ADR-023 (receipt hash-chain, default-off), ADR-029 (epoch rotation, the accepted residual this ADR closes), ADR-005 (append-only NDJSON ledger — the anchor store follows the same shape). Supersedes the design explored in PR #1086 (HELD, DB-backed origin store) and the finding it traces to, PR #1085 (closed).

## Context

ADR-029 made hash-chain adoption safe to flip on without a fleet-wide RED by introducing `chain_epoch_start` epoch markers: an unchained epoch-0 prefix (pre-adoption history) followed by one or more explicitly-sealed, chained epochs verifies as `verified-segmented` — healthy.

ADR-029's own "Threat-model note" documented an accepted residual, verbatim in the `verify_chain` docstring:

> A local actor with write access to the ledger can strip a prefix, insert a fresh `chain_epoch_start` marker, and re-chain the remainder — this verifies.

Concretely: `verify_chain` has no memory of *where* a ledger's trustworthy chain is supposed to begin. It only checks that whatever `chain_epoch_start` marker it finds anchors to `GENESIS_HASH` and that everything after it chains correctly. Any GENESIS-rooted marker satisfies that — including one an attacker planted after deleting the real history. A truncate-and-reseal (delete the true epoch-0 prefix and the true first marker, keep only later entries, re-chain them under a brand-new marker) is indistinguishable from a legitimate seal. This is exactly the finding that HELD PR #1085 (closed) and its would-be fix, PR #1086 (HELD through four `codex_gate` rounds).

### Why PR #1086's approach didn't land

PR #1086 pinned the origin in `runtime_coordination.db` (a "trusted" SQLite store) rather than a sidecar file, reasoning that a sidecar an attacker could rewrite defeats the point. Four gate rounds found, in order:

1. **Mutable origin.** `INSERT OR REPLACE INTO vnx_chain_origin` let an attacker strip the prefix, append a new first-chained entry, and have `_record_chain_origin` silently overwrite the pin with their own.
2. **Non-atomic append + anchor.** The ledger write and the origin-store write are two different storage engines (file vs. SQLite) — no shared transaction. A crash between them could leave a chained entry with no pinned origin.
3. **Fail-open on store errors.** `except sqlite3.OperationalError: return None` treated *any* DB error (locked, corrupt, not just "no such table") as "no origin recorded" — which downgrades straight to unauthenticated verification. An attacker who can induce an `OperationalError` defeats the pin without touching a single hash.
4. **No ADR-005 audit event** for the origin mutation — a trust anchor with no audit trail of its own is itself a gap.

The conclusion on record (PR #1086, round 4): *"iterative patching will not resolve this... tamper-evident chaining needs a holistic security design."* That holistic design is this ADR.

Note the original PR #1085 finding actually offered **both** options — "a trusted sidecar **or** the coordination DB" — so a sidecar file was never rejected on principle; PR #1086 simply chose the DB, and that specific implementation accumulated the four defects above. This ADR chooses the sidecar and shows why, done carefully, it avoids all four.

## Decision

Pin the chain origin in a **sibling, append-only NDJSON anchor file** next to the ledger it protects (`scripts/lib/chain_origin_anchor.py`), written **only** by the one-time `chain_epoch_seal.seal_ledger` migration — never on the hot per-receipt append path. `ndjson_hash_chain.verify_chain` enforces the anchor when one exists; with no anchor present, verification is byte-for-byte identical to pre-ADR-033 behavior.

### 1. Anchor record

For a ledger at `<dir>/<name>.ndjson`, the anchor lives at `<dir>/<name>.ndjson.origin_anchor.ndjson` — a plain NDJSON file, one record per ledger it has ever anchored (in practice: at most one, this ledger only):

```json
{
  "ledger_identity": "/abs/path/to/t0_receipts.ndjson",
  "origin_type": "epoch_marker",
  "origin_hash": "<sha256 of the earliest chain_epoch_start marker's canonical JSON>",
  "origin_line_number": 4,
  "origin_epoch": 1,
  "entries_before_origin": 3,
  "sealed_at": "2026-07-15T12:00:00+00:00"
}
```

`origin_type` is `"epoch_marker"` (the ledger's earliest `chain_epoch_start` marker — the normal case, produced by `chain_epoch_seal`) or `"genesis_entry"` (a marker-less ledger chained directly from GENESIS at line 1 — the `verify_chain` `"verified"` status, reachable if chaining is ever enabled on a fresh ledger without going through the seal script).

### 2. Write-once, by construction

`write_origin_anchor_if_absent(ledger_path, origin)` opens the anchor file `a+` and takes an exclusive `flock` for the entire read-check-then-append: it scans existing records for this ledger's identity; if one exists, it returns that record **unchanged** — the caller's `origin` argument is discarded, even if it differs. There is no UPDATE/REPLACE code path at all, so PR #1086 defect #1 (mutable origin) has no equivalent here to regress into.

### 3. No per-append atomicity problem to solve

The anchor is written by `chain_epoch_seal.seal_ledger`, a one-time (idempotent) migration — not paired with every receipt append the way PR #1086's design was. This sidesteps defect #2 structurally rather than by careful sequencing: if `seal_ledger` crashes between appending the marker and writing the anchor, the ledger is left with a valid, unanchored marker; re-running `seal_ledger` (already required to be idempotent by ADR-029) sees `chaining_active=True`, takes the no-op action branch, and completes the anchor write. No recovery/reconciliation design is needed beyond "the seal script is already idempotent and always tries to anchor."

### 4. Fail CLOSED on anchor-store errors

`read_origin_anchor` distinguishes "no anchor file, or an anchor file with no record for this ledger and nothing unparseable in it" (→ `None`, legitimately never sealed) from "the anchor file exists and contains at least one line that fails to parse, with no valid record found for this ledger" (→ `{"_corrupt": True}`). `verify_chain` treats the latter as `broken` immediately. There is no `except: return None` anywhere in the read path that could turn a real error into a silent pass — this directly closes PR #1086 defect #3.

### 5. The anchor file is its own audit trail

Per ADR-005, the anchor file is itself append-only NDJSON with a `sealed_at` timestamp — `tail -f`-able, `git diff`-able, no separate event stream needed. This closes PR #1086's advisory finding #4 (no audit event for the origin mutation) as a side effect of the storage choice, not a bolt-on.

### 6. `verify_chain` enforcement

`verify_chain` (`scripts/lib/ndjson_hash_chain.py`) now:

1. Runs the existing ADR-029 epoch-aware walk unchanged (factored out as `_verify_chain_unanchored`).
2. Looks up the ledger's anchor. **No anchor → returns the unanchored result verbatim** (identical `(is_valid, violations, status)` tuple as before this ADR).
3. With an anchor present:
   - Corrupt anchor → `broken`.
   - Unanchored ledger status is already `broken` → unchanged.
   - Unanchored ledger status is `unchained` (all chain activity gone even though an anchor was previously recorded) → `broken` — history was erased outright.
   - Otherwise (`verified` / `verified-segmented`), re-derives the ledger's **current** origin — its earliest `chain_epoch_start` marker, or its first entry for a marker-less genesis chain — and compares `origin_hash` **and** `origin_line_number` against the pinned anchor. A mismatch on either (moved boundary, stripped prefix content, wrong marker) → `broken`, with a violation note naming the anchor mismatch.

Pinning **both** the hash and the line number matters: ADR-005 append-only guarantees nothing is ever removed *before* the true origin in legitimate operation, so the origin's line number is exactly as immutable as its content. An attacker who deletes an early prefix line without touching the marker's own bytes changes nothing about the marker's hash but does shift its line number — caught by the position check even when the content check alone would not catch it.

### 7. Flag stays default-off

`VNX_CHAIN_RECEIPTS` is not touched by this change. This is a precondition for eventually flipping it, not the flip itself. The append path (`append_chained_entry`, `append_epoch_marker`) is unmodified; only `verify_chain` (read path) and `chain_epoch_seal.seal_ledger` (the one-time migration, which now also calls the new `seal_chain_origin`) change.

## Threat model

**Closed by this ADR:** a local actor with write access to the ledger who strips an early prefix (including a legitimately-sealed epoch marker) and re-chains the remainder under a fresh GENESIS-rooted marker. Once a ledger has been sealed (and therefore anchored), this now verifies `broken`, not `verified-segmented`.

**Not closed (explicitly out of scope, same residual ADR-029 already accepted):** a root-equivalent attacker who edits **both** the ledger and its sibling anchor file in the same operation. A locally-stored anchor — whether a sidecar file (this ADR) or a DB row (PR #1086's attempt) — is exactly as defeatable by an attacker who controls both stores; neither approach changes that fact, which is why PR #1086's round-4 conclusion listed "external append-only / signed anchor" as a separate, later scope item. Full defense against a local root attacker requires an anchor that lives outside the machine the attacker controls (a remote append-only log, a signed/timestamped external attestation) — deferred.

**Also not covered:** legitimate flag-flapping mid-ledger-life (chaining enabled, then disabled, then re-enabled) still produces a real `broken` finding for the unchained gap in between, by the pre-existing ADR-029 state machine — this ADR does not change that, and it is a correct result (an integrity gap genuinely exists there), not a false positive introduced by the anchor.

## Consequences

### Accepted

- `verify_chain` gains an opt-in-by-construction hardening: it only activates once a ledger has been sealed via `chain_epoch_seal.seal_ledger` (or anything else that calls the new `seal_chain_origin`). Ledgers that have never been sealed are completely unaffected — same `unchained`/`verified`/`verified-segmented`/`broken` behavior as before.
- All existing `verify_chain` callers (`fabric_audit.py` check C, `scripts/lib/evidence_bound_gate.py`, `scripts/lib/attest_override.py`, `scripts/lib/governance_effectiveness_probe.py`, `scripts/audit_chain.py`) get this protection automatically once their target ledger is sealed, with no call-site changes — the enforcement lives in the shared `verify_chain` entry point precisely so there is nothing to forget to wire up per caller.
- PR #1086 should be closed in favor of this design once this lands (left to the operator/T0 to action — not done by this dispatch).

### Rejected

- Rewriting the anchor on every re-seal / new epoch. The anchor pins the ledger's **earliest** trustworthy point, not its latest; a legitimate second or third epoch marker must never move it (covered by test: a later, legitimately-chained epoch leaves the original anchor byte-identical).
- Coupling the anchor write to the hot per-receipt append path (what PR #1086 did). Decoupling it — anchor only at seal time — is what removes the atomicity problem structurally instead of requiring a recovery/reconciliation protocol.
- An external/remote/signed anchor in this pass. Real, but a materially larger scope (network dependency, signing key management) than "close the local truncate-and-reseal bypass" — tracked as a future item, not silently dropped.

## Test plan

`tests/test_chain_origin_anchor.py`:

- The bypass is closed: seal a ledger, pin its anchor, truncate-and-reseal it (strip the true prefix + true marker, re-chain the same tail bodies under a fresh GENESIS marker) → `broken` (fails against pre-ADR-033 `verify_chain`, passes after).
- A moved epoch boundary (one prefix line removed; the marker's own bytes are untouched but its line number shifts) → `broken`, caught by the line-number pin even though the hash alone would not catch it.
- Legitimate seal + matching anchor → stays `verified-segmented`.
- A legitimate second epoch (no unchained gap) leaves the original anchor untouched and the ledger stays healthy.
- A marker-less genesis chain (`verified`) anchors to its first entry; the same truncate-and-reanchor attack on it is also caught.
- No anchor present → `verify_chain` output is identical, entry-for-entry, to the pre-anchor `_verify_chain_unanchored` core (regression guard for the default/no-anchor path), across both a healthy and a tampered ledger.
- First-seal bootstrap writes the anchor exactly once; a second `seal_ledger` call is a no-op — the anchor file's bytes do not change.
- `write_origin_anchor_if_absent` called directly with two different origins for the same ledger: the second call is discarded, first one wins, file has exactly one record (append-only, write-once as a primitive, independent of the seal script).
- A corrupt anchor file fails closed (`broken`), never silently treated as "no anchor".
- An anchor pinned on a ledger later wiped back to fully unchained → `broken` (history erased, not read as "chaining never enabled").
- `seal_chain_origin` is a no-op on an unchained, empty, or absent ledger (nothing to anchor yet).

## See also

- ADR-023 — Receipt hash-chain (experimental opt-in)
- ADR-029 — Epoch rotation (the accepted residual this ADR closes)
- ADR-005 — Append-only NDJSON ledger (the anchor file follows the same append-only shape)
- PR #1085 (closed) — original prefix-strip finding
- PR #1086 (HELD) — DB-backed origin-pin attempt; four gate rounds' findings directly shaped this design
- `scripts/lib/chain_origin_anchor.py`, `scripts/lib/ndjson_hash_chain.py` (`verify_chain`, `seal_chain_origin`, `_compute_current_origin`), `scripts/chain_epoch_seal.py`
