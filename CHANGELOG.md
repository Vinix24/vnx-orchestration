# Changelog

All notable changes to VNX Orchestration are documented here.

Format: [keep-a-changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [semver](https://semver.org/).

## [Unreleased]

### Refactored
- refactor(dispatch): extract `_apply_runtime_overrides` from delivery.py + recovery.py to shared `runtime_overrides.py` module (Kimi audit duplication finding). Eliminates copy-paste drift.

### Added
- feat(install): `install-central.sh` for centralized VNX install (apart van embedded `install.sh`) — clones to `~/.vnx-system/versions/<v>/`, atomic symlink-swap, project-pin via `.vnx-version`. Pre-centralization must-have #7.
- feat(intelligence): A/B random-skip framework (V5 per intelligence-injection-quality-research). Adds `ab_arm` column to intelligence_injections + 10% control-arm skip via `VNX_INTEL_AB_TEST=1` env flag + weekly lift report (`scripts/intelligence_ab_report.py`). Enables verifiable +30pp lift measurement. Audit BLOCKER #2 closes-the-loop PR-IH-3.
- feat(intelligence): fine-grained task_class subclassing (coding_sql/runtime/intelligence/test/ui) + active scope_tags matching via VNX_INTEL_STRICT_SCOPE env-flag. Selector kan SQL-werk SQL-kennis geven ipv generieke pool. (Audit BLOCKER #2 follow-up PR-IH-2)
- feat(cli): `vnx version` subcommand prints VERSION, commit, VNX_HOME, pin, Python+platform (pre-central-install support)
- feat(cli): `vnx update --to <ver> --keep-last N --dry-run --rollback` subcommand for future central install version-flip (pre-central-install scaffolding)
- feat(vnx_paths): override-resolver `_resolve_overrides_dir()` + `get_skill_path()` — per-project `.vnx-overrides/skills,schemas,configs/` directory takes precedence over central VNX_HOME. Enables mission-control-style custom assets without central pollution. (Pre-centralization must-have #5)
- feat(pyproject): `vnx` console_script entry-point + requires-python `>=3.11,<3.14`. Pipx-installable. (Pre-centralization must-have #6)
- feat(schema): migration 0021 `central_install_pins` + `central_install_events` tables met `scripts/lib/central_install_db.py` helper. Bookkeeping voor project pin tracking + install/update/rollback event history. Pre-centralization must-have #10.

### Fixed
- fix(governance): `_emit_governance` no longer raises `SystemExit(1)` on transient receipt write failure (Kimi audit). 3 retries with exponential backoff (0.5/1.0/1.5 s), then raises to caller. Transient FS races (rename collision, brief lock) no longer kill workers.
- fix(cli-update): path-traversal validation + ADR-005 audit events for symlink-flip + prune + git FileNotFoundError handling (codex blocker + 3 advisories)
- fix(pyproject): build-backend `setuptools.backends._legacy` → `setuptools.build_meta`. Wheel build now succeeds via `python -m build`.
- fix(vnx_paths): validate skill_name + resolved-path confinement check in get_skill_path (codex path-traversal blocker)
- fix(pool): add real-subprocess integration tests for pool spawn; confirms `_spawn_via_provider_dispatch` uses real `subprocess.Popen` with live PID verification — guards against regression to pre-PR-6.5a stub (Sonnet audit BLOCKER #1)
- fix(intelligence): catalogus-hygiene — filter governance-event success_patterns (81.6% noise) + memory_consolidation antipatterns (26% noise) at source; recency-decay (0.95^weeks) op confidence; migration invalidates existing noise. Addresses Sonnet audit BLOCKER #2 (intelligence +30pp claim artefact). PR-IH-1 per intelligence-injection-quality-research.
- fix(intelligence-hygiene): replace ALTER TABLE IF NOT EXISTS (invalid SQLite < 3.37) with Python idempotent column-add via PRAGMA table_info check in quality_db_init.py (codex_gate blocker audit-ih-1-fixforward)
- fix(intelligence-hygiene): parse ISO timestamps with timezone via fromisoformat + astimezone(UTC) — raw[:26] truncation was dropping tz offsets, skipping recency decay for those rows

### Changed
- chore: sync VERSION + pyproject.toml to 1.0.0-rc2 (was 1.0.0-rc1 / 0.9.0 mismatch); single-source version for pipx wheel + central install pin

### Added
- feat(skill-coverage): `scripts/check_skill_coverage.py` pre-flight scanner — finds project skill-refs vs central+overrides availability. Prevents mission-control style product-skill loss on central rollout. Pre-centralization must-have #9.
- fix(skill-coverage): replace silent `except Exception: pass` in `_scan_local_skills_dir` + `_list_skills_in_dir` with narrow `(yaml.YAMLError, OSError)` + `logger.warning` + skipped report surfaced in `--json` output (codex 2 blockers)
- fix(skill-coverage): narrow `_read_text` exception (OSError, UnicodeDecodeError) + logger.warning + surface to skipped list (codex round-2 blocker)

### Planned (Wave 8)
- Smart provider router with task-class-aware routing (`scripts/lib/smart_router.py`)
- Unified report schema enforced via YAML frontmatter + shell-script guardrails (model-agnostic)
- Hard constraint enforcement at code level (`HardConstraintViolation` raise; T0=Opus, Kimi=CLI-only, no Anthropic SDK)
- Self-learning loop: `route_decisions_watcher.py` auto-adjusts `routing_recommendations.yaml` on production failure patterns
- Multi-project centralization via pipx wheel (D2 burn-in path: mission-control → sales-copilot → SEOcrawler)
- Module-size CI gate (prevents new 2500-LOC monoliths from landing)
- Hard-task benchmark suite (T08-T11) to discriminate Pro vs Flash on complex coding tasks

### Open horizons (post-1.0)
- Business-task benchmark suite (`scripts/benchmark_business/`) for non-coding orchestration
- Multi-operator federation with isolated state
- 100+ concurrent dispatch scale tuning

## [1.0.0-rc1+wave7] - 2026-05-17

Multi-provider milestone. 5 providers in production with provider-agnostic governance, intelligence injection, and end-to-end token + cost tracking. Reproducible 49-dispatch benchmark suite ships with routing recommendations.

### Added — Wave 7: Multi-Provider via LiteLLM (PR #515-#520, #531, #536, #545, #550-#552)

- **PR-7.0 (#515)** ADR-015 LiteLLM Path B for DeepSeek/Kimi/GLM integration freeze
- **PR-7.1 (#516)** DeepSeek V4 lane via LiteLLM subprocess bridge (V4-Pro + V4-Flash)
- **PR-7.2 (#517)** Kimi K2.6 + K2-0905 lane via LiteLLM Moonshot endpoint
- **PR-7.3 (#518)** GLM-5.1 lane via OpenRouter (z.AI direct deferred)
- **PR-7.4 (#519)** Cost-routing policy engine (feature-flag gated)
- **PR-7.5 (#520)** Provider behavior contracts (capabilities + tool-shape + cache-control)
- **PR-7.6 (#536)** Provider governance unification — uniform receipt + unified report shape for all 5 providers (claude/codex/gemini/litellm/kimi)
- **PR-7.7 (#550)** Kimi CLI as 5th provider — OAuth via `kimi login`, no API key required (Anthropic-compatible stream-json output)
- **PR #531** `vnx.env` loader + DeepSeek V4-Pro/V4-Flash model registry
- **PR #545** OI cleanup group 2 — LiteLLM usage stream + unified report `.md` suffix
- **PR #551 (P0-A)** Intelligence injection unification — codex/gemini/litellm equal first-class with claude
- **PR #552 (P0-B)** Token usage + cost tracking end-to-end for all 5 providers; OI-1489 streaming drainer accepts `usage_complete` event

### Added — Wave 6: Workers=N Elastic Pool (PR #534-#544, #546)

- **PR-6.0 (#534)** ADR-018 elastic worker pool design freeze
- **PR-6.1 (#535)** `vnx_workers.yaml` + `WORKER_REGISTRY` (ADR-013 implementation)
- **PR-6.2 (#538)** Schema v14 elastic worker pool tables + migration scripts
- **PR-6.3 (#539)** `PoolManager` core (decision engine + state repo + manager)
- **PR-6.4 (#540)** Pluggable scaling policies (`queue_depth_v1` + `cost_aware_v1`)
- **PR-6.5 (#541)** Provider-mix per pool with lowest-share-first allocation
- **PR-6.6 (#542)** Health monitoring + dead-worker reap (tick cycle: reap → decide → execute)
- **PR-6.7 (#543)** `vnx pool` CLI (`status`/`scale`/`config`/`reap` subcommands)
- **PR-6.8 (#544)** Control Centre pool integration (cross-project pool view + supervisor)
- **PR #546** OI cleanup group 1 — idempotency + regex + ledger + audit fixes
- **PR #537** OI-1479 — token_usage extraction + cost_usd computation per provider

### Added — Wave 5: Control Centre + Multi-Project (PR #521-#532)

- **PR-5.0 (#521)** ADR-017 Control Centre product-shape architecture
- **PR-5.1 (#522)** Multi-project state aggregator write-pad
- **PR-5.2 (#525)** Per-project T0 lifecycle management (spawn/heartbeat/kill/reap)
- **PR-5.3 (#523)** Multi-tenant lease isolation (schema v12)
- **PR-5.4 (#524)** Cross-project intelligence aggregator (global + per-project facets)
- **PR-5.5 (#528)** Control Centre CLI shell skill + operator commands
- **PR-5.6 (#530)** Hybrid dispatch routing with receipt-tail lifecycle tracker
- **PR-5.7 (#532)** Operator demo runbook + Control Centre docs + completion report
- **PR #533** OI-1476 — align `project_id` regex + YAML placeholder substitution

### Added — Benchmark Infrastructure (PR #547, #548)

- **PR #547** Benchmark suite infrastructure — 9 models × 7 task-classes orchestrator + judge + analyzer (`scripts/benchmark/`)
- **PR #548** 56-dispatch model comparison results + routing recommendations (`scripts/lib/providers/routing_recommendations.yaml`)

Result summary (49 valid dispatches):
- DeepSeek V4-Flash: $0.0006/dispatch, 7.3/10 — cost+speed winner (198× cheaper than Opus 4.6)
- Kimi K2.6: 8.1/10 — top-tier quality, 21× cheaper than Opus
- GLM-5.1: 8.0/10 — top-tier quality, 24× cheaper, fastest top-tier (100s vs Kimi's 215s)
- Opus 4.6: 8.2/10 — highest cost, marginal quality lead

### Added — Wave 4.6: Provider Dispatch Generalization (PR #488, #490, #510-#513)

- **PR-4.6.1 (#488)** `scripts/lib/provider_dispatch.py` — provider-agnostic dispatch entry-point (`--provider {claude,codex,gemini,litellm:<model>}`)
- **PR-4.6.2 (#490)** `claude_spawn` extracted from `subprocess_dispatch` (byte-identical)
- **PR-4.6.3 (#511)** `codex_spawn` handler extracted from `codex_adapter`
- **PR-4.6.4 (#510)** `gemini_spawn` handler extracted from `gemini_adapter`
- **PR-4.6.5 (#512)** `litellm_spawn` handler extracted from `litellm_adapter`
- **PR-4.6.6 (#513)** Unified event shape via `CanonicalEvent` + `EventStore` enforcement

### Refactored

- **intelligence_selector.py** (2026-05-17, in flight) — 2511 LOC monolith split into `intelligence_sources/` package (9 modules, target ~321 LOC main + sources)
- **conversation_analyzer (#504)** — modularized into package; closes OI-1438/1439/1440/1441/1442
- **replay_harness (#506)** — modularized into package; closes OI-1443/1444/1445/1446/1447
- **cleanup_worker_exit (#507)** — decompose 104-line function; closes OI-1448

### Hardened — Silent-except narrowing (OI-1437, PR #491-#500, #508, #509)

- 14 PRs converting bare `except:` and overly broad `except Exception:` patterns to specific exception types with `logger.warning` across hot files (build_t0_state, intelligence_selector, gather_intelligence, learning_loop, api_intelligence, dispatch_register, append_receipt payload, api_operator, session_resolver, conversation_analyzer, replay_harness, cleanup_worker_exit, plus 13 singleton files, plus 7 hot files). Total ~120 silent-except sites converted to instrumented warnings.

### Fixed

- **OI-1489** (in flight) — Streaming drainer drops `usage_complete` event; 1-line fix re-enables provider-agnostic token telemetry end-to-end
- **OI-1450/1451/1452 (#503)** — Receipt processor bootstrap audit ordering + test infra hardening
- **dispatcher (#502)** — log stderr, fix `script_dir` leak, receipt processor bootstrap-mode
- **ADR-003 (#505)** — clarify API-key + CLI permitted; SDK still banned

### Added — CONTRIBUTING (#489)

- `CONTRIBUTING.md` + CI lint gate enforcing atomic-write and silent-except policies

## [1.0.0-rc1] - 2026-05-09

Architectural stabilization milestone. 14 ADRs locked. Central VNX state proven on real production data (855k snippets across 4 projects, 0 verifier discrepancies). CI gate enforces OAuth-only Claude routing. Smart context injection validated at +30 percentage-point dispatch quality lift on 658 outcome-tagged dispatches.

From this release forward, dispatch envelope, receipt schema, NDJSON ledger format, and ADR-locked invariants are backwards-compatibility-honoring.

### Added — ADR backfill (10 new ADRs, 003-014)

- ADR-003 OAuth-only Claude routing via `claude -p` subprocess (no SDK, no API key)
- ADR-004 VNX positioning: self-hosted alternative to Anthropic Managed Agents
- ADR-005 Append-only NDJSON audit ledger as primary orchestration substrate
- ADR-006 Mandatory staging→promote with human approval gate
- ADR-007 Multi-tenant `project_id` stamping with composite UNIQUE rebuilds
- ADR-008 Dual-LLM adversarial review (codex_gate + gemini_review) with `contract_hash` evidence binding
- ADR-009 Schema-first migrations via PRAGMA introspection
- ADR-010 Subprocess adapter (`claude -p`) as canonical Claude routing
- ADR-011 Manager+worker hierarchy with explicit depth>1
- ADR-012 Hybrid interactive+headless (no retire-interactive)
- ADR-013 Worker pool size as configuration (workers = N)
- ADR-014 Autonomous mode = pre-approved chain dispatch with SHA-256 chain-spec hash as consent token

### Added — Structural enforcement

- CI gate `ADR-003: No Anthropic SDK Imports` blocks any `import anthropic` / `from anthropic` / `import claude_agent_sdk` in `scripts/`, `dashboard/`, `tests/`

### Added — Wave 1 shadow-mode read cutover (PR #450-#454)

- `shadow_verifier.py` — independent comparator with 6 zero-tolerance divergence metrics
- `shadow_logger.py` NDJSON writer + CLI + flock-rotation
- T0 state-builder + IntelligenceSelector + DispatchRegister + Dashboard shadow wiring across 13 read sites
- Canary divergence test pack (14+ fixtures) + operator-readable rollback procedure

### Added — Wave 5 smart-context injection (PR #455-#461)

- Prior-round-findings injection (W5.0)
- ADR injection by file-touch (W5.1)
- Code anchor injection (W5.2)
- Operator memory injection (W5.3)
- Schema introspection injection (W5.4)
- Production plumbing for P0-P4 context-bundle classes (W5.5)

### Added — Wave 4 OTel observability foundation (PR #468)

- Opt-in OpenTelemetry export wired into `subprocess_dispatch` completion. Emits `dispatch_completion_count` metric + spans. Env-gated via `OTEL_EXPORTER_OTLP_ENDPOINT`; no-op when unset.

### Added — Wave 4.5 provider parity (PR #471, #472, #477, #479)

- `PromptAssembler` provider-agnostic methods (claude/codex/gemini/litellm)
- Codex + Gemini adapters use `PromptAssembler`; `AGENTS.md` + `GEMINI.md` tri-file activated by `vnx init` bootstrap
- Gate reviewer prompts use `gh pr diff` authoritative source
- Intelligence injection per-provider with empty-`dispatch_id` guard (audit-safe)

### Added — Wave 2 package extraction foundation (PR #469, #478)

- `pyproject.toml` + `vnx_core` + `vnx_cli` package skeleton with smoke tests
- First module migration: `function_size_gate.py` → `vnx_core` with `sys.path`-fallback shim

### Fixed — OI-1370 systemic locking refactor (PR #482-#486)

- Original `migrate_phase3_envelope` race (writer pre-rename appends to unlinked inode) required system-wide locking refactor across all writer paths
- `scripts/lib/state_writer.append_locked()` helper with sentinel registry; 100-thread × 100-write concurrency test passes
- 4-PR migration of all envelope/state writers to helper
- All four implementation PRs (#483-#486) implemented by **Codex CLI workers** — first production codex-worker dispatches in this codebase

### Fixed — Security + governance

- **OI-1369 (#465)** Path traversal in `vnx_paths.resolve_central_data_dir` — strict regex `^[a-z][a-z0-9-]{1,31}$`
- **OI-1294 (#467)** `compact_open_items_digest` function-size 76→34 via mechanical helper extraction
- **OI-1415 (#462)** `review_contract.content_hash` backward-compat for empty `deleted_files`

### Added — Repo hygiene (OI-1373 cleanup)

- 5-tier OI-1373 cleanup: 49 strategic/business docs moved from public `roadmap/`+`docs/internal/` to gitignored `claudedocs/`
- Pattern: filesystem `mv` + `git add -u` (NOT `git mv`) — preserves files locally on disk while removing from git tracking

## [0.10.0] - 2026-04-30

Chain summary: 27 PRs landed across governance hardening, headless audit parity, supervisor pack, CFX thematic refactors, P0 intelligence loop fixes.

### Added — State self-maintenance

- `compact_state.py` + `install_nightly_crons.sh` (#299, #313): auto-rotate intelligence_archive (7d), receipts cap (10k), open_items_digest (>30d evict)

### Added — Headless audit parity (40% → 90%)

- `instruction_sha256` in manifest + receipt (#309): cryptographic reproducibility
- `WorkerHealthMonitor` STUCK → EventStore + receipt `stuck_event_count` (#310)
- Codex+Gemini token tracking via `adapter.get_token_usage()` (#307)
- Canonical gate result schema with `gate_status.is_pass()` (#322)

### Added — Real-time observability

- `/api/register-stream` SSE endpoint (#304): dispatch lifecycle stream

### Added — Supervisor pack (auto-respawn)

- `cleanup_worker_exit` single-owner exit cleanup (#315)
- `receipt_processor_supervisor.sh` wrapper-respawn (#319)
- `lease_sweep` + dispatcher prelude tick (#316)
- `runtime_supervise` + 60s tick (#317)
- Operator guide `docs/operations/UNIFIED_SUPERVISOR.md` (#318)

### Added — Frontend regression protection

- Playwright visual regression suite (#312)
- `tsc` strict + `npm typecheck` (#306)
- Playwright network failure scenarios (#308)
- Console error detection per route (#305)

### Improved — Codex review intelligence

- Severity prompt tightening (#323, #324): `error` reserved for data loss / false closure / security; ~75% reduction in blocking findings noise

## [0.9.0] - 2026-04-11

Streaming + autonomous loop + A/B test milestone.

### Added

- **F42 PR-1** Restore EventStore from git history + dashboard archive endpoints for historical dispatch event retrieval
- **F42 PR-2** Headless T0 decision loop — decision parser extracted from replay harness, decision executor with 5 decision types and loop guards, trigger wiring for closed autonomous loop
- **A/B Test** First systematic comparison of interactive vs headless execution across F40 (moderate) and F42 (complex). Finding: headless produces functionally equivalent output with ~4% less LOC and ~18% fewer tests. Conclusion: execution mode does not determine quality — instruction quality does.

## [0.8.0] - 2026-04-11

Headless intelligence + governance profiles milestone.

### Added

- **F39** Headless T0 benchmark — decision framework with deterministic pre-filter (Level-1: 100%, Level-2: 73-87%, Level-3: 67-78%), context assembler, replay harness, file-based gate locks (#204)
- **F41** Intelligence pipeline activation — governance aggregator backfill (722 metrics, 58 SPC control limits), nightly pipeline scheduling via launchd, quality digest with real SPC data (#206)
- **F41** 3-layer headless trigger system — file watcher on unified_reports, silence watchdog (10-min stale lease/dispatch detection), optional haiku LLM triage (#206)
- Headless dispatch writer — programmatic dispatch creation for autonomous T0 orchestration (#207)
- Governance profiles — config-driven review profiles (default/light/minimal) replacing hardcoded business/coding split, configurable via `.vnx/governance_profiles.yaml` (#207)

## [0.5.0] - 2026-03-30

Governance Runtime Upgrade. Largest upgrade since initial public preview. One-command worktree lifecycle with deterministic gates, governance-aware finish flow, hardened dispatcher/tmux delivery, intelligence export/import + self-learning loop, token/model tracking in receipts, dashboard attention model + event timeline, Codex CLI + multi-model orchestration improvements, configurable per-terminal models, Opus 4.6 1M default.

## [0.1.0] - 2026-02-22

Initial public preview release of VNX.
