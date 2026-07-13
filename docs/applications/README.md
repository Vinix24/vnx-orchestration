# Applications

VNX is a governance layer for AI agents: every unit of work is authorized, reviewed by a human, and recorded as an append-only receipt. The mechanism is domain-agnostic. What makes it useful for coding agents — a tamper-evident trail of who did what, a gate before anything ships, and measurable process quality — is exactly what regulated domains have always demanded.

This folder documents where that governance layer fits.

| Application | What it covers |
|---|---|
| [Coding agents](coding-agents.md) | The proven, in-production application: governing multi-agent software work end to end. |
| [Finance & compliance](finance.md) | The same ledger discipline applied to finance processes — the origin the model was built from. |

The core primitive is the same everywhere: a **dispatch** (one authorized unit of work) produces a **receipt** (one append-only ledger line), and nothing advances without a **human gate**. The two documents above show what that looks like in two very different domains.

For the underlying mechanics, see [`docs/core/11_RECEIPT_FORMAT.md`](../core/11_RECEIPT_FORMAT.md) (the receipt/ledger spec) and [`docs/core/00_VNX_ARCHITECTURE.md`](../core/00_VNX_ARCHITECTURE.md).
