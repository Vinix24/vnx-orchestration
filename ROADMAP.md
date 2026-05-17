# VNX Orchestration Roadmap

> Public roadmap. Detailed designs live in the operator's private working notes.

## Current: 1.0.0-rc1+wave7 (2026-05-17)

Wave 7 complete. Multi-provider end-to-end. 5 providers in production:

- **Claude** (Opus 4.6/4.7, Sonnet 4.5/4.6, Haiku 4.5) via `claude -p` subprocess
- **Codex** (GPT-5.2-codex) via `codex --dangerously-bypass-approvals-and-sandbox`
- **Gemini** (2.5 Pro / 2.5 Flash) via `gemini` CLI
- **Kimi CLI** (Kimi K2.6, K2-0905) via `kimi` CLI with OAuth (no API key)
- **LiteLLM bridge** for DeepSeek V4-Pro/V4-Flash, Moonshot endpoints, and OpenRouter (GLM-5.1)

Provider-agnostic governance:
- Uniform receipt + unified-report shape across all 5 providers
- Intelligence injection equal first-class
- End-to-end token usage + cost tracking
- Reproducible 9-model × 7-task benchmark suite (`scripts/benchmark/`)
- Routing recommendations published at `scripts/lib/providers/routing_recommendations.yaml`

## Wave 8: Smart Routing + Schema Enforcement + Self-Learning

In design (operator-approved 2026-05-17). Five layers:

### Layer 1 — Routing Decision (`scripts/lib/smart_router.py`)
Per task-class auto-route with operator override. Classifier cascade: explicit `--task-class` tag → regex + role mapping → Haiku 4.5 classifier (only on confidence < 0.6). Pure-function `resolve_route()` returns `RouteDecision` with primary, fallbacks, applied constraints, and cost estimate.

### Layer 2 — Uniform Reports
YAML frontmatter enforced on `unified_reports/*.md`. Required fields: `dispatch_id`, `provider`, `model`, `task_class`, `cost_usd`, `tokens_in`, `tokens_out`. `route_decisions.ndjson` per decision. `t0_receipts.ndjson` with filled-in fields.

### Layer 3 — Guardrails (model-agnostic, shell-script enforced)
- Pre-dispatch: `scripts/guardrails/check_route.sh`
- Post-dispatch: `scripts/guardrails/verify_report_schema.sh`
- CI workflow: `.github/workflows/schema_guard.yml`
- Runtime: `provider_dispatch.py` raises `HardConstraintViolation` on policy breach (T0 must be Opus, Kimi must be CLI-only, no Anthropic SDK, etc.)

### Layer 4 — Analysis Cadence
- Weekly drift sample (1 task per model, 7 dispatches)
- Monthly full benchmark (9 models × 7 tasks)
- Triggered re-bench on new model addition or quality-drop alert

### Layer 5 — Self-Learning Loop
`route_decisions_watcher.py` reads NDJSON, detects production failure patterns per (model, task-class), auto-adjusts `routing_recommendations.yaml` via confidence-decay algorithm. Strong degradation flags a triggered re-bench.

Estimated effort: 3 weeks solo work, 16 PRs, ~3490 LOC total.

## Sneak Previews (in development)

### Smart Dispatch Router
Benchmark-data-driven model selection per task class. Operator-configurable cost caps per provider via `vnx.env`. Hard constraints enforced in code (`HardConstraintViolation`). Auto-fallback chain to guaranteed-quality model on primary failure. Telemetry written to `route_decisions.ndjson` for self-learning loop consumption.

### VNX Centralization (pipx wheel)
Single source of truth for VNX runtime across projects. `pipx install vnx-orchestration`. Per-project state isolation via `.vnx-data/` but shared core code. Burn-in path: mission-control (24-48h) → sales-copilot (48h) → SEOcrawler (maintenance window with rollback archive). Removes the 16-30 day drift between projects observed pre-Wave-8.

### Self-Learning Routing
`route_decisions.ndjson` watcher auto-adjusts `routing_recommendations.yaml` based on production success/fail patterns. Confidence-decay algorithm. Triggered re-bench when degradation detected. Closes the gap between static recommendations (PR #548) and runtime adaptation.

### Module-Size CI Gate
Prevents new 2500-LOC monoliths from landing. Configurable threshold per file pattern, explicit waiver mechanism for legitimate exceptions. Direct response to the `intelligence_selector.py` refactor of 2026-05-17 (2511 LOC → 9-module package).

## Strategic Decisions (D-series, 2026-05)

Public-facing summary of operator decisions driving Wave 8 sequencing:

- **D1 ✅** Hybrid-explicit positioning (tool-first, platform-availability). Wave 5/6 code remains as foundation for future scale; not in critical hot-path for current solopreneur workflow.
- **D2 ✅** Incremental centralization with per-project burn-in (mission-control → sales-copilot → SEOcrawler).
- **D3 ✅** Conservative LOC reduction this week (2-3k verified-dead-code: chain_recovery, FPC cluster, old SQL snapshots, `vnx demo` retirement).
- **D4 ✅** Re-activate conversation analyzer + launchd plist + 5-week backfill (`quality_intelligence.db` was 5 weeks stale, breaking smart-context freshness).
- **D5 🚧** Critical gaps follow-up — `T2.ndjson` ring-buffer truncate (P0) + cross-project intelligence aggregator wire-in (P1).
- **D6 ✅** Retain own routing (no swap to DSPy/smolagents/LangGraph) — ADR-003 spirit + governance differentiator.
- **D7 🚧** Multi-project sync via pipx (blocked on D2 wheel release).
- **D8 ✅** Kimi/GLM/DeepSeek benchmark complete + blog drafts ready (NL + EN).
- **D9 🚧** Wave 8 public release — gated on mission-control validation via D2.
- **D10 🚧** Blog publication strategy (LinkedIn → NL blog → EN blog).
- **D11 ✅** Opus 4.6 → 4.7 — 4.7 on T0 (2.5× better on T0-orchestrator role per production data), 4.6 retained option for other terminals pending further analysis.

## Future Horizons (post-1.0, non-binding)

### Business Task Benchmarks (Wave 9)
Current benchmark suite is coding-tasks only (7 task-classes). Add B01-B08 business orchestration benchmarks (lead classification, email drafting, blog drafting, CRM enrichment, etc.). Separate suite at `scripts/benchmark_business/`. Estimated 1-2 weeks.

### Multi-Operator Federation
Multiple operators sharing infrastructure with isolated state. Target post-1.5.

### Performance & Scale
Optimize for 100+ concurrent dispatches. Pool autoscaling refinements. SQLite concurrency tuning (per Gemini external review, May 2026).

### Integration Breadth
Direct integrations with more LLM providers as Anthropic-compatible endpoints proliferate. Path D (Claude-harness for non-Claude models) remains blocked pending telemetry-leak resolution (`claude` v2.1.136 hits `api.anthropic.com` 8× even with `BASE_URL` redirect, verified 2026-05-10).

### Domain Expansion
Business agent skills beyond coding (lead intake, blog drafting, CRM enrichment). Builds on uniform-schema enforcement from Wave 8.

---

Contributions welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md).

Most valuable contributions: test coverage, failure-mode hardening, provider adapters, docs clarity.

For release history: see [CHANGELOG.md](./CHANGELOG.md).
