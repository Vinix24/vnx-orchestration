# Known Limitations & Scope

## Tested
- 5 terminals: T0 (orchestrator) + Track A / Track B / Track C (workers) + T-MANAGER (system maintenance)
- Claude Code + Codex CLI + Gemini CLI + Kimi CLI (provider auto-detection via session_resolver)
- Single-repository workflows (one VNX instance per project)
- Deployed across 3 independent projects (SaaS SEO tool, marketing website, VNX itself) via `vnx update`
- Receipt-based cost observability (V4 — receipt_processor_v4 with git provenance per receipt)
- Graceful crash recovery via ledger replay
- T0 orchestrator: Claude Opus via Claude Code
- T0 write restrictions enforced via Claude Code hooks (T0 cannot write files directly)
- Dispatcher V8 with track-based routing (Track A/B/C) and MCP-aware dispatch
- Per-terminal MCP profiling (workers: github + sequential-thinking only; Track C: full 10-server stack)
- Remote distribution via `vnx update` (clone → install.sh → origin persistence)

## Multi-Model Dispatch Status

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
- Live provider switching mid-session (script to swap T1 provider without restart)
- Auto-detection of running CLI process (currently panes.json is authoritative source)
- T2/T3 swing capability (currently Claude-only)

## Not Yet Tested
- T0 orchestrator with other models (Codex, Gemini, Kimi may work as T0 but are untested in that role)
- Codex model switching (`/model gpt-5.x-codex`) — tested locally, not battle-tested in production
- Gemini/Kimi at scale (provider integration exists but less battle-tested than Claude/Codex)
- Multi-repository orchestration (single T0 coordinating workers across multiple repos)
- 10+ terminal scale
- Full public commit history from the earliest internal iterations (system originated inside a private product repo; history was later squashed when split out)

## By Design
- **File-based**: Uses the filesystem as a message bus. Not designed for distributed networks.
- **Local-first**: No cloud dependency for orchestration state.
- **Bash/Python prototype (~60/40 ratio)**: The codebase is roughly 60% bash, 40% Python. This ratio is a direct consequence of VNX's origin as simple tmux `send-keys` scripts — terminal orchestration is inherently shell-based. As governance logic grew more complex, Python was adopted for structured data handling (receipts, quality advisory, state reconciliation). Critical-path components are being progressively migrated from bash to Python to improve testability, error handling, and long-term maintainability. See `EVOLUTION_TIMELINE.md` for the full history.
- **Tmux Dependency**: Orchestration currently relies on tmux pane naming conventions.
- **Hook-agnostic**: Quality intelligence and usage tracking do not require provider hooks; hooks are optional enrichments.
- **T0 write isolation**: The orchestrator cannot write files directly; write restrictions are enforced through Claude Code hooks. This ensures T0 stays a coordinator, not an executor.
- **Checkpoint control**: Workers run provider REPLs; mid-session interruption is best-effort, so VNX prefers short dispatches and human review between checkpoints.
- **No inter-agent communication**: Workers cannot send-keys to each other's terminals. All coordination flows through T0 via dispatches and receipts. This is a deliberate governance choice — T0 is the single source of truth for task assignment, priority, and sequencing. Peer-to-peer agent communication would bypass quality gates, break audit trails, and make orchestration state unpredictable. Note: individual terminals *can* spawn subagents (via Claude Code's Task tool) for supporting work like researching subtopics or exploring code — but never for primary implementation or testing. This can be indicated in dispatches and specific skills that allow internal orchestration. The main task, its deliverables, and quality gates always remain with the dispatched terminal under T0 governance.
