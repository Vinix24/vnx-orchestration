# Multi-Model Guide

> **Note**: Multi-model dispatch routing is documented inline in the CLAUDE.md files
> for each terminal (`/.claude/terminals/T0-T3/CLAUDE.md`) and in the top-level
> `CLAUDE.md` under "Subprocess Adapter Feature Flag".
>
> For provider-specific adapter configuration see:
> - `docs/core/PROVIDER_LANES.md` — the full per-provider lane map (claude tmux/headless, codex, gemini, kimi, deepseek-harness, ollama) and the single-entry door
> - `docs/core/DISPATCH_RULES.md` — provider-string routing cheat-sheet and lane selection
> - `docs/operations/SUBPROCESS_ADAPTER_FEATURE_FLAG.md` — per-terminal adapter env vars
> - `docs/operations/RECEIPT_PIPELINE.md` — multi-provider receipt handling
>
> A full multi-model dispatch converter (CLAUDE.md → GEMINI.md / AGENTS.md) is a
> planned feature; see `docs/research/HIERARCHICAL_MANAGER_ARCHITECTURE.md` for
> the design proposal.
