# receipt-hash-chain

Appends NDJSON entries with a previous-entry hash under the ledger lock and verifies continuity across explicit epochs. Receipt appends continue an already-chained tail; otherwise `config_runtime` resolves `VNX_HASH_CHAIN_REQUIRED` (default off).

Background: [epoch rotation](../governance/decisions/ADR-029-hashchain-epoch-rotation.md), [external origin anchor](../governance/decisions/ADR-034-external-chain-origin-anchor.md).

## Entry points

[`scripts/lib/ndjson_hash_chain.py::append_chained_entry`](../../scripts/lib/ndjson_hash_chain.py), [`scripts/lib/ndjson_hash_chain.py::verify_chain`](../../scripts/lib/ndjson_hash_chain.py), [`scripts/lib/ndjson_hash_chain.py::append_chains`](../../scripts/lib/ndjson_hash_chain.py).

## Tests

[tests/test_ndjson_hash_chain.py](../../tests/test_ndjson_hash_chain.py).

## Decisions

[ADR-023](../governance/decisions/ADR-023-receipt-hash-chain.md), [ADR-029](../governance/decisions/ADR-029-hashchain-epoch-rotation.md), [ADR-034](../governance/decisions/ADR-034-external-chain-origin-anchor.md), [ADR-083](../governance/decisions/ADR-083-de-staart-beslist-over-ketenen.md), [ADR-087](../governance/decisions/ADR-087-een-configuratie-oppervlak.md).

## Flag

`VNX_HASH_CHAIN_REQUIRED` — active append selector: override > project row > global config > env > default. The retired receipt-chain env flag has no effect (ADR-087 decision 9).

Enable it with `vnx config set VNX_HASH_CHAIN_REQUIRED 1 --approval-id <approval-id>`
for the ledger's project, or add `--global` for the global value. Seal an existing
unchained ledger first (ADR-029); changing the switch alone does not seal its history.

The append writer and ledger-health reader derive the project from the explicit
ledger directory, without git discovery. That identity wins over `VNX_PROJECT_ID`.
If no project can be derived, a WARNING names the key and directory, and resolution
excludes every project row (including a previously wired ambient store).
Here "global and default layers only" excludes project rows and preserves existing
override and env precedence: override > global config > env > default.

Explicit project-store reads are strict. A corrupt or unreadable database produces
a WARNING naming the key, state directory and underlying error before falling back
to the remaining config layers. The receipt writer emits warnings as `receipt_config`
JSON lines and still appends the receipt; with the default off it stays unchained.
An already chained tail continues chaining regardless of the switch (ADR-083).
