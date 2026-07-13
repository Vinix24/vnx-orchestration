# Coding agents

The application VNX was built for: governing multi-agent software development so that every change is authorized, reviewed, and recorded — instead of trusting a black box.

## What it does

When an AI agent writes code, three questions usually go unanswered: what exactly did it do, who approved it, and was it any good? VNX answers all three by construction. Every piece of work runs as a **dispatch**, produces an append-only **receipt**, and passes a **human gate** before it ships. The result is a complete, tamper-evident audit trail of every agent action, plus process-quality metrics you can actually track.

## How it works

The unit of work is a dispatch: one scoped task, sent through a single entry door that decides how it runs.

1. **Authorize.** A dispatch is staged and only becomes runnable after a human promotes it. No agent starts work on its own.
2. **Isolate and build.** A worker agent runs in its own git worktree, on its own branch. It never touches the main line directly.
3. **Review — adversarial, not self-certifying.** The change goes through review gates: an independent model reviews the diff to *refute* it (find the bug, the security hole, the missed edge case), and deterministic CI runs the full test suite. Developers do not certify their own work.
4. **Human gate.** Nothing merges without a person authorizing the merge, and only when the gate and CI are green. This is the last set of hands, always human.
5. **Record — and link.** On completion, an append-only receipt is written to an NDJSON ledger: the task, the outcome, the evidence, the timing. Receipts are never edited — only appended. Crucially, the receipt links the exact **instruction** (the dispatch that was sent) to its **output** (the result, the log stream) and to the **commit** it produced. The chain runs both ways: from any receipt you can recover the instruction that caused it and the code change it produced, and from any commit you can find the receipt and the instruction behind it.

State lives per project, so one project's ledger can never bleed into another's.

Because instruction, outcome, and resulting change are linked — not just logged side by side — the whole run is **replayable as a picture, not only as scattered details.** You can reconstruct not just "what line changed" but "which instruction, judged how, produced which change, approved by whom." That is the difference between a log and a ledger.

## Why it is useful

- **A real audit trail.** Every agent action is a ledger line you can read back months later: what ran, who approved it, whether it passed the gate. Delete-and-pretend is not possible — the ledger only grows.
- **Bugs caught before they ship.** The adversarial review gate repeatedly catches issues the builder's own tests miss — fail-open security bypasses, off-by-one boundaries, wrong assumptions — because the reviewer's job is to break the change, not confirm it.
- **Process quality you can measure.** Because every dispatch is recorded, you get statistical-process-control metrics on the work itself: First-Pass Yield (how often work passes clean the first time) and rework rate (how often it comes back). Governance stops being a feeling and becomes a number.
- **Human in the loop where it counts.** The machine proposes and builds; a person decides. The gate is not a rubber stamp — a red gate or a red CI blocks the merge, no exceptions.

## In practice

VNX has run this way in production across roughly 900 sessions, producing on the order of 8,000 governed receipts (13,000+ including benchmark runs), with an 87% token reduction in the dispatch path versus a naive approach. It is open source: [github.com/Vinix24/vnx-orchestration](https://github.com/Vinix24/vnx-orchestration).

The point is not the numbers. The point is that a black box became a glass box: you can see, review, and audit every step an AI agent takes — and the next domain that needs exactly that discipline is [finance](finance.md).
