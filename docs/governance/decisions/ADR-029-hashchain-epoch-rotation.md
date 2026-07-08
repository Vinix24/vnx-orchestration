# ADR-029 — Receipt hash-chain: epoch-rotation to enable default-on without fleet-wide RED

**Status:** Accepted (default-on flip approved by operator; rollout gated on the epoch-migration landing + `fabric-audit` check C verified GREEN fleet-wide — see Rollout)
**Date:** 2026-07-08
**Decided by:** Operator (Vincent van Deth), in an autonomous session under an explicit overnight mandate. Design grounded in the current `ndjson_hash_chain.verify_chain` state machine and `fabric_audit.py` check C.
**Resolves / Cross-refs:** Extends ADR-023 (receipt hash-chain, currently PARTIAL — default OFF). Preserves ADR-005 (append-only NDJSON ledger, immutable past). Feeds `fabric-audit` check C (the integrity gate that goes RED on a broken chain). Prerequisite for the ADR-028 target architecture's "tamper-evident AUDIT is VNX's differentiator" claim.

## Context

The receipt hash-chain (ADR-023) is **off by default** (`VNX_CHAIN_RECEIPTS` unset). Each project's `state/t0_receipts.ndjson` is therefore **fully unchained**: no entry carries a `prev_hash`. `verify_chain()` classifies this as `"unchained"` — reported OK by `fabric-audit` check C, because ADR-023 is PARTIAL by design.

The goal is to enable chaining by default so integrity is verifiable going forward. The blocker is a **blast-radius footgun**: flipping `VNX_CHAIN_RECEIPTS` on naively makes every *new* receipt carry a `prev_hash` while all *historical* entries do not. `verify_chain()` treats a ledger where "some entries carry `prev_hash` while others do not" as **`"broken"`**, not `"unchained"` (`scripts/lib/ndjson_hash_chain.py:119-125`). So the first receipt written after a naive flip flips `fabric-audit` check C from OK → **RED on every project ledger in the fleet at once**.

We cannot fix this by back-filling `prev_hash` across history: rewriting historical receipts violates ADR-005 append-only immutability. The past must stay exactly as it was recorded.

## Decision

Introduce **epoch rotation**. Chaining does not retroactively cover history; it starts a **new epoch** at the adoption point. The immutable pre-adoption entries are epoch 0 (legitimately unchained); every entry from the flip forward chains within epoch 1+. `verify_chain()` and check C become epoch-aware so a segmented ledger (unchained prefix + chained suffix) verifies as healthy.

### 1. Epoch-start marker

A chain epoch begins with a dedicated marker entry appended to the ledger:

```json
{"type": "chain_epoch_start", "epoch": 1, "epoch_ts": "<utc>", "prev_hash": "<64-zero GENESIS>"}
```

The marker's `prev_hash` is `GENESIS_HASH` (`"0"*64`) — it resets the chain. Every subsequent entry in that epoch chains normally (`prev_hash` = hash of the prior entry, marker included). `epoch` is monotonically increasing; a future re-key or ledger rotation opens epoch 2, etc.

### 2. `verify_chain()` becomes epoch-aware

New state machine (replaces the current binary "any mix ⇒ broken"):

- Walk entries in file order. Maintain `expected_prev`, initialised to `GENESIS_HASH`.
- A **leading run of unchained entries** (no `prev_hash`, before the first `chain_epoch_start`) is the **epoch-0 prefix** — legitimate, not a violation.
- On a `chain_epoch_start` marker: assert its `prev_hash == GENESIS_HASH`, reset `expected_prev = GENESIS_HASH`, then continue chained verification within the epoch.
- **Within an epoch** (after a marker): every entry MUST carry `prev_hash == expected_prev`; a missing or mismatched `prev_hash` is a real integrity break.
- New status **`"verified-segmented"`** (`is_valid=True`): the ledger has an epoch-0 unchained prefix plus one or more intact chained epochs.
- `"verified"` (unchanged): every entry chained from a single GENESIS, no unchained prefix.
- `"unchained"` (unchanged): no `prev_hash` anywhere, no marker.
- `"broken"` (narrowed): a within-epoch `prev_hash` mismatch, an unchained entry *after* an epoch marker, a marker whose `prev_hash ≠ GENESIS`, or unparseable lines. This is the only RED-worthy state.

The key change: an unchained entry is only a violation when it appears **inside** an open epoch. Before the first marker, it is epoch-0 history.

### 3. `fabric-audit` check C update

Check C (`scripts/fabric_audit.py`) maps `verify_chain` status → finding:
- `unchained` → OK (chaining not yet enabled — unchanged)
- `verified` → GREEN (unchanged)
- **`verified-segmented` → GREEN** (new — a sealed ledger mid-adoption is healthy)
- `broken` → RED (unchanged — genuine tamper / within-epoch gap)

### 4. The flip migration — epoch-seal

A one-time, idempotent migration `scripts/chain_epoch_seal.py` runs per project ledger **before** the default-on flip:
- If the ledger's latest entry is already inside an open epoch (a `chain_epoch_start` exists with no later unchained entry) → no-op.
- Otherwise append a `chain_epoch_start` marker (next `epoch` number) as the epoch boundary.
- Append-only: it never rewrites a historical line (ADR-005 preserved).

After the seal, `VNX_CHAIN_RECEIPTS=1` chains all new receipts within the sealed epoch. `verify_chain` returns `verified-segmented`; check C stays GREEN.

## Rollout (each step reversible; fleet-wide flip is the last)

1. Land `verify_chain` epoch-awareness + `verified-segmented` status (with unit tests).
2. Land `fabric-audit` check C acceptance of `verified-segmented`.
3. Land the `chain_epoch_seal.py` migration (idempotent, append-only).
4. Run the migration per project ledger; verify each returns `verified-segmented` / stays check-C GREEN.
5. Flip `VNX_CHAIN_RECEIPTS` default-on. Re-run `vnx fabric-audit` fleet-wide; confirm no check-C RED.

Steps 1–3 are inert until the flag flips (they only *accept* more states). Step 5 is a one-line default change, revertible by unsetting the flag; the sealed epochs remain valid either way.

## Consequences

- **Positive.** Integrity becomes verifiable from the adoption point forward without violating append-only history. The fleet-wide RED footgun is closed structurally, not by convention. Delivers the tamper-evidence ADR-028 leans on.
- **Negative / accepted.** Pre-adoption history (epoch 0) remains unverifiable — by design; we do not claim retroactive integrity we cannot honestly provide. A ledger is only as trustworthy as its first sealed epoch onward.
- **Backward-compat.** Ledgers that never adopt chaining stay `unchained` → OK. Nothing forces the flip.

## Test plan

- `verify_chain`: (a) unchained prefix + one intact epoch ⇒ `verified-segmented`; (b) within-epoch tamper ⇒ `broken`; (c) unchained entry after a marker ⇒ `broken`; (d) mixed chained/unchained with NO marker (naive-flip simulation) ⇒ still `broken` (guards against skipping the seal); (e) marker with non-GENESIS `prev_hash` ⇒ `broken`.
- `chain_epoch_seal.py`: idempotency (second run is a no-op), append-only (historical lines byte-identical), correct next-epoch numbering.
- `fabric-audit` check C: GREEN on a `verified-segmented` ledger; RED on a naive-flip (unsealed) ledger.
