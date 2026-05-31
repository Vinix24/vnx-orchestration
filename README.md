# VNX

VNX is a governance-first multi-agent orchestration framework for AI CLI workers. I built it to run on top of `claude`, `codex`, and `kimi` CLIs with no SDK imports, using tmux-based ephemeral worker dispatch and per-worker git worktree isolation.

VNX exists because Anthropic's June 15, 2026 billing change moves headless `claude -p` usage to API credits while interactive Claude Code stays on a subscription. The VNX leaseless tmux lane runs Claude workers as interactive tmux sessions, keeping the default Claude worker path on the subscription rather than the paid API lane. The tmux lane is the production default and is actively being hardened ahead of the June 15 OAuth rollout; it works today and the structural refactor targeting full maturity is part of the 1.0 scope.

The proof points are concrete: 2,000+ hours of Claude Code production use, append-only NDJSON receipts for audit-grade history, 1.450+ tests, and open source code on GitHub. The project is public at [github.com/Vinix24/vnx-orchestration](https://github.com/Vinix24/vnx-orchestration).

## Install

```bash
pip install vnx-orchestration
vnx init
vnx dispatch-agent --agent hello-world  # works via the examples/ fallback
```

There are two binaries on purpose during the 1.0 cutover. The pip-installed `vnx` covers the essentials, while checkout-only operator commands still live behind `./bin/vnx`, including `gate-check`, `new-worktree`, and `demo`.

## Multi-Provider Architecture

VNX is not a thin "supports many models" wrapper. The provider layer is governed by `scripts/lib/providers/provider_constraints.yaml`, a machine-readable source of truth for constraints such as `kimi-via-cli-only`, `no-anthropic-sdk`, and `deepseek-harness-subscription-blocked`.

Claude has two explicit paths. The default worker path is Claude on subscription through interactive tmux, implemented in `scripts/lib/tmux_interactive_dispatch.py`; the opt-in burst path is Claude on the paid API through subprocess `claude -p`. The receipt format and intelligence layer are uniform across all lanes today. Per-lane parity on the full PREPARE/GOVERN envelope is the dispatch-unification work targeted for the 1.x release.

Kimi runs through the Kimi CLI with OAuth. VNX does not call the Moonshot SDK directly for that lane, which keeps attribution and rate-limit behavior in one place.

OpenRouter is the gateway lane for GLM-5.1 from Zhipu today; arbitrary OpenAI-compatible models via the proxy lane are planned for a later release. Local Ollama is used for the resolver layer and privacy-sensitive work, including Gemma 4 E4B, where no data leaves the machine.

The non-obvious path is DeepSeek through the Claude harness. VNX can run DeepSeek with my own DeepSeek API key plus hardening: `ANTHROPIC_BASE_URL` redirect, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, telemetry and updater traffic disabled, and MCP off. Operator measurement on Claude Code 2.1.150 on 2026-05-26 showed this path is about 30% more effective on coding and tool tasks than a bare DeepSeek API call, because the harness adds tool-use loops, smart-context injection, and structured diff output that the raw API does not provide.

## Architecture

VNX uses a T0 orchestrator and ephemeral workers. The old fixed T1-T3 mental model is no longer the core model; workers spawn per dispatch and leave behind receipts, reports, and worktree state.

The leaseless single-shot tmux dispatch lane lives in `scripts/lib/tmux_interactive_dispatch.py`. Per-worker ephemeral git worktree isolation lives in `scripts/lib/tmux_worktree.py`, including teardown classification for clean, committed or pushed, and dirty worktrees.

Append-only NDJSON receipts are the PAST-forensics source of truth. `runtime_coordination.db` runs as SQLite WAL for real-time CURRENT state, and `quality_intelligence.db` stores patterns, antipatterns, and context injection data for the learning layer.

The closest spiritual cousin is [dmux](https://github.com/standardagents/dmux), which also pairs tmux with per-pane git worktrees. My choices differ on ephemeral-per-dispatch workers instead of long-lived panes, NDJSON receipts instead of interactive merge as the main record, and a teardown classifier that preserves dirty or pushed work instead of treating cleanup as one state.

## Status

I built this for my own work. It is open source because the architecture is portable. Use at your own discretion.

Public 1.0 status as of this README merge on 2026-05-28: the package is pip-installable, `VERSION` is `1.0.0`, and the operator binary is still required for the full command surface. Open governance and release items are tracked in [ROADMAP.md](ROADMAP.md), [FEATURE_PLAN.md](FEATURE_PLAN.md), and the open-items tooling under [scripts/open_items_manager.py](scripts/open_items_manager.py).

## Credits

Dimitri Geelen's Claude Code RFC on deterministic agent governance shaped the external governance framing: [RFC #45427](https://github.com/anthropics/claude-code/issues/45427).

My subagent observability work is currently represented by the [delivery substep observability contract](docs/core/150_DELIVERY_SUBSTEP_OBSERVABILITY_CONTRACT.md); I will link the public RFC when it is split out.

Anthropic Claude Code is the foundation. I add receipts, provider routing, tmux dispatch, and worktree isolation around it.
