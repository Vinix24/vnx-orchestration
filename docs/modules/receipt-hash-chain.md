# receipt-hash-chain

Appends NDJSON entries with a previous-entry hash under the ledger lock and verifies continuity across explicit epochs. Receipt appends continue an already-chained tail; otherwise the current implementation consults `VNX_CHAIN_RECEIPTS`.

Background: [epoch rotation](../governance/decisions/ADR-029-hashchain-epoch-rotation.md), [external origin anchor](../governance/decisions/ADR-034-external-chain-origin-anchor.md).

## Entry points

[`scripts/lib/ndjson_hash_chain.py::append_chained_entry`](../../scripts/lib/ndjson_hash_chain.py), [`scripts/lib/ndjson_hash_chain.py::verify_chain`](../../scripts/lib/ndjson_hash_chain.py), [`scripts/lib/ndjson_hash_chain.py::append_chains`](../../scripts/lib/ndjson_hash_chain.py).

## Tests

[tests/test_ndjson_hash_chain.py](../../tests/test_ndjson_hash_chain.py).

## Decisions

[ADR-023](../governance/decisions/ADR-023-receipt-hash-chain.md), [ADR-029](../governance/decisions/ADR-029-hashchain-epoch-rotation.md), [ADR-034](../governance/decisions/ADR-034-external-chain-origin-anchor.md), [ADR-083](../governance/decisions/ADR-083-de-staart-beslist-over-ketenen.md), [ADR-087](../governance/decisions/ADR-087-een-configuratie-oppervlak.md).

## Flag

`VNX_HASH_CHAIN_REQUIRED` — registry metadata; the implemented append selector still reads `VNX_CHAIN_RECEIPTS`.
