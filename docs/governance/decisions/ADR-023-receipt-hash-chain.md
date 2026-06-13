# ADR-023 — Receipt Hash-Chain (Experimental Opt-In)

**Status:** Proposed (1.0.1)
**Date:** 2026-06-13
**Decided by:** Operator (Vincent van Deth)
**References:** ADR-005 (NDJSON ledger-first), ADR-005 amendment 2026-06-13

VNX_CHAIN_RECEIPTS=1 enables an EXPERIMENTAL opt-in hash-chain on the append_receipt write path. scripts/audit_chain.py provides a 3-state verify (unchained / verified / broken). The governed emit_dispatch_receipt path does NOT yet chain; backfill/correction/redaction conventions are NOT yet reconciled with verify_chain (post-hoc appends verify as broken). Full per-append enforcement, cross-path chaining, and the tamper-evidence guarantees are DEFERRED to 1.0.1. This is tamper-EVIDENCE within a single write path, not authenticity/non-repudiation.

## See also

- ROADMAP — per-append chain enforcement items (1.0.1 scope)
- ADR-005 — Append-only NDJSON ledger (the tamper-evidence claim this ADR partially addresses)
