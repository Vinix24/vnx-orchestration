# VNX Orchestration Roadmap

> Public roadmap. Detailed designs live in `docs/manifesto/ROADMAP.md`.

## Current: 1.0.0 (release candidate)

`VERSION` is `1.0.0` and the wheel builds from this tree. **It is not on PyPI yet — install from a checkout; the PyPI publish is the one remaining 1.0 ship gate (human-gated).** Production-validated on the author's own work (a multi-month receipt trail). Built across many waves since the first Wave 5 delivery in mid-May. Capability summary below.

### Always-on (default active)

- **5-provider dispatch**: Claude (subscription tmux lane + subprocess burst), Codex CLI, Gemini CLI, Kimi CLI (OAuth), LiteLLM bridge (DeepSeek V4-Pro/V4-Flash, GLM-5.1 via OpenRouter)
- **Governance receipts**: append-only NDJSON audit trail, uniform receipt + unified-report shape across all providers
- **Intelligence injection**: context bundles, ADR injection, repo-map enrichment for all providers (#712), kimi intelligence wiring (#701)
- **GOV-1 PreToolUse hook**: blocks raw worker spawns, enforces subprocess_dispatch path (#656)
- **Elastic worker pool**: `vnx pool` CLI, queue-aware + cost-aware scaling, per-worker worktree isolation
- **Central install**: from a checkout (`pip install -e .` or `./bin/vnx`), `vnx init`, `vnx doctor --strict`. PyPI publish (`pip install vnx-orchestration`) is the final 1.0 ship gate, not done yet.
- **Track layer**: schema + DAL + CLI + ADR-007 composite PK (FUT-1 + FUT-2, both done)
- **ADR intelligence**: FTS5 ADR index + injection in dispatch context (INT-1, INT-2)
- **Cost tracking**: universal cost tracking across all 5 providers (#684)

### Shipped opt-in (env-gated, not default)

- **Smart routing** (`VNX_AUTO_ROUTE=1`): cost-aware auto-route with constraint enforcement across all providers. Fully wired; default off because production routing mix still burns in.
- **Pool task consumer** (`VNX_POOL_TASK_CONSUMER=1`): N-1/2/3 foundation — atomic dispatch claim, pool_worker_runner, consumer wiring. Default off; single-worker path remains default.
- **Worktree isolation per dispatch** (`VNX_ISOLATED_WORKTREE=1`): per-dispatch git worktree with full provider isolation. Default off.

### Shipped dark (runnable, not user-facing)

- **Autopilot tick** (`RA-6`): `autopilot_tick` + scheduler wired; ships dark. Human-gate, step driver, and gate enforcement (RA-1..5, RA-3b) are active. Auto-advance requires explicit opt-in not yet exposed.
- **Auto-dream self-learning loop**: consolidator core (ADR-019), CLI, scheduler, and T0 review-gate all runnable. Nightly cron trigger and central-path unification are pending before routine activation.

## Wave History

All waves shipped and stable.

- **Wave 5** (2026-05-16): Control Centre, multi-project state aggregator, per-project T0 lifecycle
- **Wave 6** (2026-05-16): Elastic worker pool, `vnx pool` CLI, ADR-018
- **Wave 7** (2026-05-17): 5-provider production, benchmark suite, routing recommendations
- **Wave 8** (2026-05-17): Smart router, constraint enforcer, report schema guardrails, pipx wheel
- **Wave 4/central** (2026-05-17–25): Central install, `install-central.sh`, schema migrations 0017–0024, `vnx doctor --strict`
- **1.0 sprint** (2026-05-29): RA-1..6 (roadmap autopilot gate hardening), N-1/2/3 pool-task-consumer foundation, auto-dream runnable, vulture dead-code sweep, FUT-1+2 (track layer), GOV-1 hook, kimi+repo-map enrichment, packaging hardening

## Strategic Decisions (D-series, current)

- **D1** Hybrid-explicit positioning — tool-first, platform-availability. Wave 5/6 code is foundation for future scale; not in critical hot-path for current solopreneur workflow.
- **D2** Incremental centralization with per-project burn-in — complete.
- **D6** Retain own routing (no DSPy/smolagents/LangGraph swap) — ADR-003 + governance differentiator.
- **D11** Opus 4.7 on T0, Sonnet 4.6 on workers — measured on production data.
- **DeepSeek via Claude harness**: measured 30% more effective than bare DeepSeek API call (tool-use loops, smart-context, structured diff). Allowed with own DeepSeek API key + hardening (`ANTHROPIC_BASE_URL`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, MCP off). Never on production OAuth subscription.

## Near-Term Open Items

- Nightly cron trigger for auto-dream self-learning loop
- Central-path unification for dream receipt writes
- `VNX_AUTO_ROUTE` and `VNX_POOL_TASK_CONSUMER` burn-in and default-on graduation
- `VNX_ISOLATED_WORKTREE` default-on graduation
- RA-6 autopilot-tick user-facing exposure

## Future Horizons (post-1.0, non-binding)

- Business task benchmarks (B01-B08 orchestration tasks)
- Multi-operator federation (post-1.5)
- Performance optimisation for 100+ concurrent dispatches
- Domain expansion beyond coding (lead intake, blog, CRM)

---

Contributions welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md).

For release history: see [CHANGELOG.md](./CHANGELOG.md).

For architecture and milestone detail: see [docs/manifesto/ROADMAP.md](docs/manifesto/ROADMAP.md).
