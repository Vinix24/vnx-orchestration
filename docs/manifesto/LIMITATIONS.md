# Known Limitations & Scope

> **Note on the model below (2026-07):** the fixed T0 + Track A/B/C + T-MANAGER terminal layout described in this "Tested" section is the **opt-in subprocess lane** (`VNX_ADAPTER_T{n}=subprocess`). The **default** since the door-flip is the single-entry dispatch door with **ephemeral per-dispatch workers** (spawned per task, leaving receipts + worktree state behind), not fixed terminals. See the README and `docs/core/DISPATCH_RULES.md` for the current lanes; live per-feature status lives in `ROADMAP.yaml`.

## Tested

**Current default lane** (single-entry dispatch door, ephemeral per-dispatch workers):
- The dispatch door (`vnx dispatch`) as the single entry point for every dispatch (ADR-024)
- `claude_headless` (`claude -p` via envelope) as the only Claude worker lane, subscription-preserving
- Review seats: the default stack `codex_gate,kimi_gate`, one distinct reviewer per seat, a rejection from any reviewer blocks the merge (ADR-008, ADR-037). Takeover chain `codex_gate,kimi_gate,deepseek_gate`; `glm_gate` is outside the default chain. On main, not yet released
- Horizon (`vnx horizon`) as the roadmap, tracks and deliverables planning layer, with the plan-first gate: one seat, a second only on BLOCK (`docs/core/HORIZON_LIFECYCLE.md`)
- Open outcomes: a dispatch is finished only with an accept or reject from a T0 (`docs/core/DISPATCH_RULES.md` section 13)
- Receipt-based cost observability (git provenance per receipt)
- Graceful crash recovery via ledger replay
- T0 context guard and rotation (`docs/operations/T0_CONTEXT_ROTATION.md`) and the context-rotation plugin (`packages/claude-code-toolkit/context-rotation/`, its README names its own limits)

**Opt-in subprocess lane** (`VNX_ADAPTER_T{n}=subprocess`, terminal-pinned, the historical default layout):
- T0 (orchestrator) plus Track A / Track B / Track C workers and T-MANAGER (system maintenance)
- Claude Code, Codex CLI, Gemini CLI and Kimi CLI (provider auto-detection via session_resolver)
- Single-repository workflows (one VNX instance per project)
- T0 runs Claude Opus via Claude Code; a hook blocks T0 from spawning subagents
- Terminal-pinned dispatch with track-based routing (Track A/B/C)
- Remote distribution via `vnx update` (clone, install.sh, origin persistence)

## Multi-Model Dispatch Status

The section below describes CLI-level capability per provider on the opt-in subprocess
lane. On the current default lane, provider choice is a pinned default rather than a
capability question: T0 runs Opus 5.5, build-workers default to Sonnet, and `codex`/`kimi`
are the subscription review gates, with `deepseek` as API-credit fallback and `glm` outside the default chain.

`vnx providers` has one switch per provider family and `vnx providers list` shows its state. It cannot switch yet: every write verb refuses while a launch function does not call `provider_switch.require`, and `list` prints the count of those functions. The content boundary (`content_class`) is read by the conversation analyzer and the dream consolidation only, not by the receipt classifier, the tagger or the scout (OI-2172).

VNX was built and battle-tested with Claude Code as the primary provider. As Codex CLI
results have become increasingly clear, multi-model dispatch support has been added:

- **Claude Code**: Production-ready. All features fully supported and tested.
- **Codex CLI**: Skills, context reset (/new), and planning mode (/plan) work.
  Model switching (/model gpt-5.3-codex etc.) has been quickly tested but is not yet
  taken into production (not battle-tested).
- **Gemini CLI**: Skills and context reset (/clear) work. No runtime model switching.
  No mode toggles (planning/thinking). Less battle-tested than Claude/Codex.

### Nice-to-have (not in production)
- Codex `/model` runtime switching (works in testing, needs production validation)
- Enforced provider switching (`vnx providers`; see above)
- Auto-detection of running CLI process (currently panes.json is authoritative source)

## Not Yet Tested
- T0 orchestrator with other models (Codex, Gemini, Kimi may work as T0 but are untested in that role)
- Codex model switching (`/model gpt-5.x-codex`) — tested locally, not battle-tested in production
- Gemini/Kimi at scale (provider integration exists but less battle-tested than Claude/Codex)
- Multi-repository orchestration (single T0 coordinating workers across multiple repos)
- Parallel multi-track execution (Tier 3, designed and not shipped, ADR-020)
- Full public commit history from the earliest internal iterations (system originated inside a private product repo; history was later squashed when split out)
- A tamper-evident ledger: the receipt hash chain is available per project and off by default (ADR-023)

## By Design
- **File-based**: Uses the filesystem as a message bus. Not designed for distributed networks.
- **Local-first**: No cloud dependency for orchestration state.
- **Bash/Python prototype (about 8/92 ratio)**: The codebase is about 8% bash and 92% Python (measured 2026-10-18 by line count of tracked `.py` and `.sh` files under `scripts/` and `bin/`). This ratio reflects origin, not design preference. VNX started as tmux `send-keys` scripts and grew organically. Bash handles tmux orchestration, file-bus operations, and process supervision; Python handles intelligence, receipt processing, and anything requiring structured data or testability. New components are written in Python by default; existing bash is migrated when it needs significant changes. See [EVOLUTION_TIMELINE.md, Language Evolution](EVOLUTION_TIMELINE.md#language-evolution-why-python-now-dominates) for full context.
- **Tmux Dependency**: Orchestration currently relies on tmux pane naming conventions.
- **Hook-agnostic**: Quality intelligence and usage tracking do not require provider hooks; hooks are optional enrichments.
- **T0 is a coordinator**: The orchestrator role is read-only and never writes code; the work goes to workers through dispatches. A Claude Code hook blocks T0 from spawning subagents (`scripts/hooks/pretooluse_t0_no_subagents.py`).
- **Checkpoint control**: Workers run provider REPLs; mid-session interruption is best-effort, so VNX prefers short dispatches and human review between checkpoints.
- **No inter-agent communication**: Workers cannot send-keys to each other's terminals. All coordination flows through T0 via dispatches and receipts. This is a deliberate governance choice — T0 is the single source of truth for task assignment, priority, and sequencing. Peer-to-peer agent communication would bypass quality gates, break audit trails, and make orchestration state unpredictable. Note: individual terminals *can* spawn subagents (via Claude Code's Task tool) for supporting work like researching subtopics or exploring code — but never for primary implementation or testing. This can be indicated in dispatches and specific skills that allow internal orchestration. The main task, its deliverables, and quality gates always remain with the dispatched terminal under T0 governance.
