# Finance & compliance

The governance model in this repository was not designed for code first. It was designed by someone who spent a decade in finance — ISO, ISAE, audit trails, segregation of duties — and then applied that discipline to AI agents. This document runs the mapping in the other direction: how the same ledger discipline applies to finance processes.

If you come from finance, none of the ideas below are new. You have run controlled, auditable processes your whole career. What is new is that an AI agent can now run inside those same controls, instead of beside them as an unaccountable black box.

## The mapping

Every primitive in the framework has a direct finance equivalent.

| Framework primitive | Finance equivalent |
|---|---|
| Receipt per dispatch | Journal entry per transaction |
| Append-only NDJSON ledger | Immutable general ledger — you append, you never overwrite |
| Human gate / review gate | Four-eyes principle / segregation of duties |
| First-Pass Yield + rework rate | Statistical process control on the process itself |
| Hash-chained receipts | Tamper-evidence — a changed record breaks the chain |
| Dispatch → review → human merge | Authorization flow with recorded approval |
| Instruction ↔ output ↔ change, linked | Source document ↔ posted entry ↔ reported figure — full two-way traceability |
| Per-project state | Entity/ledger separation |

This is not an analogy stretched to fit. The append-only receipt *is* a journal entry. The reason it maps cleanly is that it was taken from finance in the first place.

## How you would apply it

Take any finance process you would consider handing partly to an AI agent — invoice processing, reconciliation, drafting journal entries, expense checks, first-line control testing. Instead of letting the model act and hoping, you run it inside the same four controls the framework already enforces:

1. **Every action becomes a journal entry.** The agent reads an invoice, proposes a booking, flags an exception — each step writes a receipt: what it did, on what input, with what confidence, when, and under whose authority. Nothing the agent does is off the record.
2. **The ledger is append-only and tamper-evident.** Corrections are new entries, never edits. The receipts are hash-chained, so any attempt to rewrite history breaks the chain and is detectable. This is the audit-trail integrity an external auditor asks for.
3. **A human authorizes the last set.** The agent proposes; a person approves the entry that actually posts. Four-eyes is built in, not bolted on — and the approval itself is recorded as part of the trail.
4. **You measure the process, not just the output.** First-Pass Yield tells you how often the agent's work is right the first time; rework rate tells you how often it comes back. You are running SPC on an AI-driven finance process, exactly as you would on any controlled process.

And because the instruction is linked to its output and to the resulting entry, the process is **replayable, not just inspectable.** You can take any reported figure and walk it all the way back — to the posting, to the agent's action, to the exact instruction and the input it ran on — and walk it forward again. That two-way reconstruction is precisely what an audit walkthrough is: not a sample of details, but the whole picture, re-derivable on demand.

The result is an AI-assisted finance workflow that an auditor can read end to end: every decision, every approval, every correction, in an immutable ledger — mapping naturally onto ISAE 3402 / internal-control language.

## Why it matters

The usual objection to AI in finance is not capability. It is accountability: you cannot audit a black box, and finance does not run on trust-me. That is the gap this closes. The same mechanism that makes AI coding work auditable — see [coding agents](coding-agents.md) — makes AI-driven finance processes as auditable as your general ledger.

## Scope

This is a conceptual document: how the discipline maps, and how you would apply it. It describes the governance model (open source at [github.com/Vinix24/vnx-orchestration](https://github.com/Vinix24/vnx-orchestration)), not a packaged finance product. If you want to explore what this would look like for a specific finance process, that mapping is the natural starting point — take one process, run it through the four controls above, and see what the ledger tells you.
