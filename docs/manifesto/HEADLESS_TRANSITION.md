# The Headless Transition: An Operator's Perspective

**Audience**: Operators evaluating VNX or tracing its architectural evolution  
**Scope**: The shift from interactive tmux-based execution to headless subprocess workers  
**Status**: Retrospective — written after the transition completed

---

## The "Before" Picture

When VNX started, the operator ran four tmux panes simultaneously:

```
┌─────────────────────┬─────────────────────┐
│  T0 — Orchestrator  │  T1 — Primary Work  │
│  (interactive)      │  (interactive)       │
├─────────────────────┼─────────────────────┤
│  T2 — Testing       │  T3 — Review        │
│  (interactive)      │  (interactive)       │
└─────────────────────┴─────────────────────┘
```

Each worker was a Claude session in an interactive tmux pane. The operator:

1. Read the dispatch markdown from `pending/`
2. Evaluated whether it was ready to promote
3. Ran a promotion command to deliver it via `tmux send-keys`
4. Watched the pane for output
5. Waited for the receipt to appear
6. Manually triggered the next step

The system worked, but it required continuous attention. A chain of 10 PRs meant 10 approval cycles, each requiring the operator to context-switch into the governance flow. Chains longer than 4-5 PRs were genuinely fatiguing.

**What was good about this**: Total visibility. The operator saw every decision, every output, every stall. Mistakes were caught immediately because the human was in the loop at every step.

**What was bad about this**: It didn't scale. The operator became the bottleneck. Multi-day chains required babysitting across sessions.

---

## Four Run Modes

VNX supports four adapter configurations. Each terminal (T0/T1/T2/T3) independently selects `tmux` (interactive) or `subprocess` (headless):

| Mode | T0 | Workers (T1/T2/T3) | When to use |
|------|----|--------------------|-------------|
| 1. All interactive | tmux | tmux | Live operator session, visible 2×2 grid |
| 2. Interactive T0 + headless workers | tmux | subprocess | Operator drives, workers run in background |
| 3. All headless | subprocess | subprocess | CI/cron, autonomous overnight chains |
| 4. Headless T0 + interactive workers | subprocess | tmux | Edge case — operator wants to inspect worker panes |

### Configuration

```bash
VNX_ADAPTER_T0=subprocess     # headless T0
VNX_ADAPTER_T1=subprocess     # headless T1
VNX_ADAPTER_T2=subprocess     # headless T2
VNX_ADAPTER_T3=subprocess     # headless T3
# Default (unset): tmux for all
```

**Mode 2 (recommended for solo dev):**
```bash
VNX_ADAPTER_T1=subprocess VNX_ADAPTER_T2=subprocess VNX_ADAPTER_T3=subprocess vnx start
```

**Mode 3 (fully headless):**
```bash
VNX_ADAPTER_T0=subprocess VNX_ADAPTER_T1=subprocess VNX_ADAPTER_T2=subprocess VNX_ADAPTER_T3=subprocess \
  python3 scripts/headless_orchestrator.py
```

### What stays identical across modes

- Receipt schema (append-only NDJSON)
- Quality gate enforcement (codex + gemini + CI)
- Provenance chain (instruction_sha256 → manifest → receipt → audit)
- Open items lifecycle
- Pattern intelligence DB

### What differs

| Aspect | Interactive (tmux) | Headless (subprocess) |
|--------|--------------------|-----------------------|
| Tmux pane | yes | no |
| Event stream | basic | full per-dispatch in `events/T<n>.ndjson` |
| Context rotation | Claude Code /clear | F43 explicit handover |
| Crash recovery | tmux session restore | `dispatcher_supervisor.sh` wrapper |
| Memory hooks | Claude Code native | Receipt-driven post-hooks (ARC-3) |

### When to choose which mode

- **Mode 1**: First-time setup, debugging, or when you want to watch every agent decision live.
- **Mode 2**: Daily development. You approve dispatches; workers run silently and produce receipts. Low cognitive overhead.
- **Mode 3**: Overnight chains, CI pipelines, or any context where no human is present between dispatch approval cycles.
- **Mode 4**: Rare. Useful when you want T0 to operate headlessly (e.g., driven by a webhook) while keeping worker panes open for manual inspection.

### Migration path from interactive-only

If you've been running Mode 1 and want to try Mode 2:

1. Set `VNX_ADAPTER_T1=subprocess` for one terminal first
2. Run a dispatch and verify the receipt appears identically
3. Gradually enable T2, then T3
4. Consider Mode 3 once comfortable with event-archive debugging

---

## The Transition: What Changed and When

### Step 1 — SubprocessAdapter (F32, March 2026)

The first headless workers. T1 and T2 became `claude -p --output-format stream-json` subprocesses instead of interactive tmux panes.

```
┌─────────────────────┬─────────────────────────────┐
│  T0 — Orchestrator  │  T1 — subprocess (headless) │
│  (interactive tmux) │  no pane needed              │
├─────────────────────┼─────────────────────────────┤
│  T2 — subprocess    │  T3 — Review (interactive)  │
│  (headless)         │  tmux pane                   │
└─────────────────────┴─────────────────────────────┘
```

Enabled via feature flags: `VNX_ADAPTER_T1=subprocess`, `VNX_ADAPTER_T2=subprocess`.

Workers wrote to per-terminal NDJSON ring buffers (`.vnx-data/events/T{n}.ndjson`), archived per-dispatch to `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson`. The operator watched the dashboard instead of the pane.

**What improved**: Two fewer panes to monitor. Workers ran silently and produced receipts without operator attention.

**What got harder**: Diagnosing stalls. When a headless worker hung, the operator had to read the event archive rather than the live pane. Silent failures became more common.

### Step 2 — Headless Review Gates (F39, April 2026)

Codex and Gemini review gates moved from manual operator review to headless subprocess execution. The operator no longer read gate output and made a pass/fail judgment — the gate runner did it autonomously, wrote a result JSON, and the closure verifier acted on it.

Triple-gate enforcement: codex pass + gemini pass + CI green = merge. All three conditions enforced in code, not memory.

**What improved**: Chains of 10+ PRs could land without the operator making 30+ individual gate decisions. The operator's role shifted from decision-maker to exception handler.

**What got harder**: Gate noise. When codex flagged everything as `error`, the operator had to manually retune prompts or override results. This was addressed in v0.10.0 with severity prompt tightening (#323, #324), reducing blocking rates ~75%.

### Step 3 — Supervisor Pack (v0.10.0, April 2026)

Daemons became self-healing:

- `receipt_processor_supervisor.sh`: auto-respawns receipt processor on crash
- `dispatcher_supervisor.sh`: auto-respawns dispatcher with exponential backoff
- `lease_sweep`: runs every 30s to release stale leases before they block the queue
- `compact_state.py` + nightly cron: state directories auto-rotate, preventing unbounded growth

**What improved**: Multi-day chains became operationally viable without babysitting. The operator no longer needed to check "is the receipt processor still running?" at session start.

**What got harder**: Debugging. Diagnosing why a supervisor restarted requires reading supervisor logs, not just the primary daemon log. The operational surface area is wider even though the operator burden is lower.

---

## The "After" Picture

```
┌─────────────────────────────────────────────────────┐
│  T0 — Orchestrator (interactive tmux, or headless)  │
│  Reads: t0_state.json, open dispatches, receipts    │
│  Writes: dispatch files to pending/                  │
└────────────────────────┬────────────────────────────┘
                         │ file-bus (pending/ → active/)
         ┌───────────────┴───────────────┐
         │                               │
┌────────▼────────┐             ┌────────▼────────┐
│  T1 subprocess  │             │  T2 subprocess  │
│  (headless)     │             │  (headless)     │
│  event archive  │             │  event archive  │
└────────┬────────┘             └────────┬────────┘
         │ receipt                       │ receipt
         └───────────────┬───────────────┘
                         │
              ┌──────────▼──────────┐
              │  Receipt Processor  │
              │  (supervised)       │
              │  → gate triggers    │
              │  → audit trail      │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │  Review Gates       │
              │  Codex (headless)   │
              │  Gemini (headless)  │
              │  CI green           │
              └──────────┬──────────┘
                         │ gate result JSON
              ┌──────────▼──────────┐
              │  closure_verifier   │
              │  → merge or block   │
              └─────────────────────┘
```

The operator today:
1. Reviews the dispatch draft (still human-authored or T0-drafted)
2. Promotes it from `pending/` to active (approval gate — intentionally human)
3. Monitors the dashboard for receipts and gate results
4. Intervenes only on failures, ambiguous gate results, or escalations

A 27-PR chain (like the v0.10.0 chain) landed over two days with the operator making approximately 27 approval decisions — one per dispatch — rather than 27 × N gate decisions per chain.

---

## What's Still Imperfect

**Intelligence loop newly closed, not proven.** The selector-learner open-circuit was fixed in v0.10.0 (#326–#328), but whether the closed loop produces measurable quality improvement hasn't been evaluated yet. The learning signal is now reaching the right places; whether it meaningfully improves future dispatch quality is an open question.

**T0 still interactive by default.** T0 runs in a tmux pane. `VNX_ADAPTER_T0=subprocess` exists but is not the default. The autonomous T0 decision loop (F42) can run headlessly, but the production default is still interactive. This is intentional — full headless T0 in production requires the benchmark to hold above 90% on Level-2 scenarios before the operator considers it safe.

**Debugging headless failures requires archive diving.** When a headless worker produces unexpected output, the operator reads `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson`. This is unambiguous but inconvenient. The dispatch-viewer UI (F59-PR4) adds a scrubbable event replay timeline to the dashboard, which helps, but it's an additional tool to learn.

**Gate noise still occurs on first-run codex.** Severity prompt tightening reduced blocking rates ~75%, but novel code patterns can still trigger false positives. The operator occasionally needs to manually inspect the codex report and decide whether to override.

**Audit parity at 90%, not 100%.** The remaining 10% gap includes edge cases in partial-failure scenarios and some legacy receipt formats that predate headless instrumentation. Closing the gap requires either backfill migration or new receipt schema enforcement.

---

## Key Lessons

**"Invisible" is not the same as "autonomous".** Headless workers don't require a visible pane, but they still require human approval at the dispatch gate. The operator burden shifted from *watching* to *approving* — which is a meaningful reduction in attention cost, but not full automation.

**Silent failure surfaces increased with headless execution.** Every capability moved to subprocess created a new failure mode that doesn't produce visible tmux output. The event archive, supervisor respawn logs, and dashboard health indicators are the compensating controls.

**Gate noise is an operator tax, not a worker tax.** When codex blocks 100% of chain PRs as `error`, it's the operator who has to manually evaluate and override each one. Severity calibration (#323, #324) was necessary for sustainable headless gate operation — it's not a shortcut, it's a prerequisite.

**The approval gate is intentional governance, not a scaling bottleneck.** The operator still approves each dispatch before it goes active. This is not a bug. The approval gate is the system's primary human checkpoint — the point where an operator's judgment replaces automated logic. Removing it would make the system fully autonomous, which requires a different trust model.

---

## See Also

- [docs/comparisons/headless_vs_interactive.md](../comparisons/headless_vs_interactive.md) — detailed side-by-side comparison
- [docs/operations/SUBPROCESS_ADAPTER_FEATURE_FLAG.md](../operations/SUBPROCESS_ADAPTER_FEATURE_FLAG.md) — env var reference for all terminals
- [docs/operations/EVENT_STREAMS.md](../operations/EVENT_STREAMS.md) — per-terminal NDJSON structure
- [README.md §"Adapter mode matrix"](../../README.md) — quick-start matrix in the project overview
