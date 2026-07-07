# Changelog

All notable changes to VNX Orchestration are documented here.

Format: [keep-a-changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [semver](https://semver.org/).

## [1.0.1] — Unreleased

Future-state reconciliation batch (`adr007-composite-keys-batch` / future-state milestone). It makes the track ↔ dispatch ↔ open-item future state reflect reality *automatically* and brings the `dispatches` table into ADR-007 composite-key tenancy. `VERSION` is still `1.0.0`: these changes are on `main` but not yet cut as a tagged release. Driven by `claudedocs/PRD-future-state-reconciliation-v1.1.md` (database-engineer skill under T0 governance). The 1.0 launch does not depend on this batch. Cite ADR-007.

### Fabric + quality hardening (2026-07-08)

Phase-0 fabric hardening (ADR-028) and a code-health gate, from the autonomous horizon run.

- **fabric-audit `-wal`/`-shm` awareness (#1047)** — `fabric_audit.py` check A read only `.db` mtimes, so a Jun-20 `.db` with a same-day `.db-wal` reported as a safe 17-day stale relic while a connection had just opened the store. mtime alone cannot tell a leftover sidecar from a live handle, so a fresh sidecar now drives the active/stale decision and escalates to RED with a "verify with `lsof` before retiring" note. Proven empirically during the store retirement below (the audit gave a false-clean signal).
- **Repo-local state-pin gate (#1047)** — a standalone `state-pin-gate` CI job bans a repo-local `VNX_STATE_DIR=.vnx-data` pin across every shipped surface (templates, skills, docs, the T0 role — not just `scripts/`, which the pre-existing "Legacy path gate" covered). Durability guard for the #1043 footgun.
- **CI apt-flake fix (#1047)** — strip the flaky `packages.microsoft.com` apt source before `apt-get update` in every ripgrep-install step; it broke the install ~4×/night with a NOSPLIT/hash-sum mismatch.
- **Legacy shared-store retirement** — the orphaned `~/.vnx-data/state/` (last real write 2026-06-20) was moved to `~/.vnx-data/state.pre-retirement` (reversible; ADR-028 Phase-0 30-day hold) after confirming no live writer via `lsof`. `vnx fabric-audit` → **GREEN**.
- **File-size gate escalates to BLOCKING (#1048)** — `quality_advisory.py` had a "blocking" file-size threshold that only ever emitted `severity="warning"`, so monoliths (up to 3357 lines) grew unchecked. The Python hard ceiling is now 1200 (warn stays 500) and over it emits `severity="blocking"` (HOLDs `pre_merge_gate`). A `FILE_SIZE_ALLOWLIST` grandfathers every current over-ceiling source file (surfaced as a standing advisory, not a block); test files are exempt. A genuinely new monolith blocks.

### Pre-ship hardening sprint (2026-06-26)

A pre-PyPI-ship pass: five real production bugs and the future-state drift, then a stale-test and docs sweep. None change the 1.0 feature surface; they make it tip-top before publish.

- **exit_classifier audit-trail restore (#913)** — a dead-code purge had gutted `_STDERR_PATTERNS`, broken the decision-tree order, and inverted `_RETRYABLE` for INTERRUPTED/UNKNOWN, corrupting the governed coordination-events trail + retry behavior. Restored to HEADLESS_RUN_CONTRACT §4 (+ context-limit → non-retryable, auth 401/403 → non-retryable to save tokens).
- **future-state git-grounded reconcile (#914)** — the track reconciler never advanced a track to `done` from a merged PR (it read three dead-for-central-store sources). Added a 4th source (`gh pr list --state merged`, opt-in `VNX_RECONCILE_GIT`, cache-first, silent-on-failure) + multi-PR `pr_ref` parsing.
- **self-learning proposal tier revived (#915)** — `learning_loop.extract_failure_patterns` scanned a directory that never existed under the central store; pointed it at the real `t0_receipts.ndjson` so the operator-gated proposal tier (`pending_rules.json`) finally runs.
- **schema SSOT for `dispatches.output_ref`/`output_kind` (#916)** — columns referenced by code but declared in no `.sql`; declared in the canonical table (closes the schema-drift guard).
- **`report_findings` self-heal (#917)** — `ALTER ADD COLUMN` for missing columns before the `CREATE INDEX (extracted_at DESC)` so a drifted table self-heals instead of crashing.
- **`vnx_doctor` partial-setup tolerance (#918)** — `.get()` fallback for `VNX_INTELLIGENCE_DIR` so the doctor reports failures instead of `KeyError`-ing on a bare project.
- **code-anchor injection as pointers (#919)** — code anchors were silently evicted whole (item + suppression list exceeded the payload budget); now inject compact `file:line` pointers, not full bodies — cheaper, richer, and the item survives the budget. `MAX_PAYLOAD_CHARS` unchanged.
- **stale-test clusters (#920)** — test-only: aligned fixtures/expectations to current production (project_id-scoped fixtures, F54 temporal columns, the cb174793 CLI rename, dynamic ADR count).

### Added

- **ADR-007 composite-key `dispatches` rebuild (PR-A1, #859)** — schema-preserving in-place repair of the `dispatches` table to `UNIQUE(dispatch_id, project_id)`, removing every uniqueness keyed solely on `dispatch_id` (inline column, table-level, standalone index, partial index, and `lower(dispatch_id)` expression index). Canonical 12-step crash-safe rebuild: capture/restore `PRAGMA foreign_keys`, `BEGIN IMMEDIATE` with bounded retry on `SQLITE_BUSY/LOCKED`, drop+recreate dependent views/triggers verbatim, preserve the `sqlite_sequence` high-water mark, and run `foreign_key_check` + `integrity_check` before commit (abort/rollback on any violation). Tenant `project_id` is resolved **fail-closed** from a precedence chain (resolved DB path → `.vnx-project-id` marker → `VNX_PROJECT_ID`); conflicting or unknown sources abort, and existing NULL/empty/conflicting `project_id` values abort before any mutation. Never a silent `vnx-dev` default. (`scripts/migrate_future_system.py`)
- **Version reconciliation via a declarative invariant manifest (PR-A2, #861)** — a per-version (v22–v30) invariant manifest (tables; columns with type + nullability; PK ordinals; FK actions; index definitions; views) in `scripts/lib/schema_manifest.py`. A DB whose claimed `user_version` fails its invariant is downgraded to the highest version whose invariants actually hold and the missing migrations re-run; on no safe target it raises rather than guess (ADR-009).
- **Tenant-scoped canonical tracks in `build_t0_state` (PR-B, #863, R3.2)** — the canonical-track and `track_open_items` reads always carry a `WHERE project_id = ?` predicate. On unavailable tenant identity the builder returns a documented degraded fallback (`available: false`, `tenant_unavailable: true`, empty `tracks`/`open_items`) and never returns cross-tenant rows.
- **Open-item → track bridge through `tracks.py` (PR-C, #862, R4.1–R4.4)** — `scripts/import_open_items_to_tracks.py`, a thin orchestrator over the single-writer primitives `tracks.link_open_item` / `tracks.unlink_open_item` (no second SQL writer; decision D1). One run-level `BEGIN IMMEDIATE` transaction makes the read-then-write window serialized (TOCTOU closed) and the run atomic. It fails loud on an absent/unreadable/wrong-shape source (never coerced to an empty store that would close every active link), requires the migration 0030 resolution schema (`resolved_at` / `resolution_reason`) and fails closed on a pre-0030 DB, and is idempotent (`INSERT OR REPLACE` upsert — re-running yields identical rows). **D3 event semantics, documented honestly:** the DB is authoritative and the ADR-005 ledger events are emitted *after* a successful commit (at-most-once, never orphaned). A post-commit emit failure is logged loudly and is non-fatal — the DB mutation persists and the reconciler re-derives status — surfacing as CLI exit 4. Exactly-once via a transactional outbox is deferred to 1.x (#867).
- **Bridge + reconcile wired into the autopilot loop (PR-D, #871, R5/D2/D4)** — `RoadmapManager.autopilot_tick()` runs the open-item → track bridge and then `reconcile_tracks()` synchronously, under the `VNX_ROADMAP_AUTOPILOT=1` gate, before any feature-step dispatch or advance. If the track sync fails the tick returns `status: degraded` (`reason: track_sync_failed`) and refuses to advance on stale state — the downstream advance is gated on a clean sync. (`scripts/roadmap_manager.py`)
- **Bridge CLI exit codes** in `docs/EXIT_CODES.md`: `3` source missing/malformed, `4` ledger-emit failure (DB already committed), `5` resolution-schema (0030) precondition, `6` DB error.
- **Operator runtime-migration runbook** in `docs/MIGRATION_GUIDE.md` (PRD §7.2, human-gated): quiesce → WAL-safe verified backup → preflight + dry-run → migrate → backfill linkage → bridge-import → reconcile → row/schema/checksum/`integrity_check` postflight; restore-from-verified-backup and re-run on any phase failure (each phase idempotent).

### Changed

- **Test-isolation guard enforced (PR-0, #857)** — migration test modules pin `VNX_DATA_DIR` to a tmp dir; a guard refuses to open the canonical `$HOME/.vnx-data` DB in test mode, and a CI canary asserts the live DB file hash is unchanged after the full suite.
- **Kanban / state-builder honesty (PR-E, #858)** — `build_t0_state` catches only enumerated pre-migration missing-table/column cases; any other `OperationalError` (locked/malformed) sets a `health=degraded|failed` field and a non-zero exit instead of a silent legacy fallback. Artifact-read failures are recorded with the dispatch id (work is not dropped) and flag the build degraded; the active-dispatch count is de-duplicated across dir and `.md` forms.
- Kimi default per-chunk stall threshold raised 300s → 600s (#860).
- Worker-role skills pre-approved in settings so detached lanes don't stall on skill-permission prompts (#872).
- Roadmap updated with the local-model PM-gate-automation plan and an honest future-state batch status (#873).

### Known issues / roadmap (1.x)

Filed and tracked in `ROADMAP.yaml`; not part of this batch:

- **#864** — broader ADR-007 composite-key batch across the SPC / intelligence tables (separate from this dispatches/tracks migration).
- **#866** — event-stream-primary measurement (normalized NDJSON of all tool-calls as the primary measurement substrate).
- **#867** — open-item bridge exactly-once via a transactional outbox (supersedes the current at-most-once post-commit events).
- **#868** — governance observability.
- **#869** — operator-runbook automation (automating the §7.2 runtime-migration runbook).
- Additional follow-ups **#865**, **#870**, **#874** are filed against the 1.x line.

## [1.0.0] — 2026-07-02

The 1.0.0 release, published to PyPI (`pip install vnx-orchestration`) and tagged
`v1.0.0`. Everything below is the rc9 → 1.0.0 delta; the rc-series entries that
follow document the road there.

### Added

- **Realistic benchmark methodology (repo-only)** — field-tests harness with production-derived tasks, programmatic verification per task, LLM-judge fallback, and cost per quality-point; codex lane added and provider-agnostic skill injection verified end-to-end on all 6 lanes (#828, #830, #831). Lives under `scripts/benchmark/field-tests/`; deliberately **excluded from the wheel** (#832) — task seeds are repo-specific, a generalised bring-your-own-tasks version is planned for 1.1 (OI-225).
- **Smart Lanes foundation** — local Gemma e4b via MLX with package structure (`[local-gemma]` extra), Smart Router cost-tier classifier (flag-gated, default-off) (#813), `quality_tier` discriminator with per-task min/max gates (#822).
- **Planning / future-state layer (ships dark)** — tracks seeder + horizon views + `vnx objective list` (#787), deliverable plane with proposed→ready human gate (#790), planning kanban in the dashboard (#791), advisory rollup reconciler that never auto-writes ROADMAP (#793), dispatch→track linkage backfill (#801), human-gated objective sync (#800), `track_type` + `next_action_owner` discriminator (#803).
- **Governance hardening** — `/pending` dispatch-path enforcement closes the T0 direct-call bypass (#811), profile-gate resolver active in `request_reviews()` (#804), worker-permission relay with operator auto-accept window + catastrophic hard-list (#799), OI bulk pattern subcommands + 1.0 closing sprint (96→48 open items) (#812).
- **Digest architecture V2** — `atomic_io.py` + ADR-021 exception discipline (#816), progress-table + minimal digest skeleton (#817).
- **OI-lifecycle closure** — `vnx track done`, `vnx oi-close`, dispatch-to-track linkage backfill, and `vnx status --tracks` added; coordinates track completion with open-item closure in a single governed action (#849).
- **dispatch_metadata backfill tool** — `vnx dispatch_metadata` subcommand backfills `outcome`, `model`, `provider`, `tokens`, and `cost_usd` from receipts into the dispatch register; `contract_invalid` vocabulary synced at gate-F2 (#847).

### Fixed

- **Bench seed decontamination** (#831) — task seeds no longer contain solutions and the scorer no longer reads repo-root state; `tests/test_bench_seed_integrity.py` guards that every verifier fails on the bare repo.
- **Wheel hygiene** (#832) — benchmark dev-tooling (incl. a planted-flaw `sk-live` fixture string and a binary DB fixture) excluded from the artifact: 0 benchmark files, 2.3 MB, fresh-venv install verified.
- Receipt dedup per dispatch_id keeps best status (#808); dispatcher survives scans that reject all dispatches (`set -e` leak) with observable rejection (#806); self-learning loop controls for task difficulty in model inference (#805); claude-spawn captures `completion_text` from stream-json (#821); smart-router null-cost sort collapse (#818); `_dispatch_gemini` respects `--model` (OI-155, #823); uniform central-path resolution (OI-126, #819); four regressed nightly intelligence phases repaired (OI-2331, #792); hook-driven version-agnostic tmux lane signals (#798); reconciler derives done from `track.pr_ref` instead of the legacy A/B/C join (#802).
- **Audit-chain verify** now correctly distinguishes an unchained (virgin) ledger from a broken/corrupt one; previously both returned the same error code (LB-5, #840).
- **Schema-init view-ordering** on legacy DBs unblocked v22/v23 migration failures — SQLite view dependency order now enforced during schema bootstrap (nightly phase-0 failure mode 2, #842).
- **Observability path resolution** unified across `state/` and `events/`; `events_path` field added to receipt pointer so consumers locate the correct per-dispatch event archive (H2, #843).
- **Kimi lane/constraint conflict** resolved — constraint file no longer marks the kimi CLI lane as violating; raw-spawn guard generalized to protect against uncontrolled provider CLI spawns on all non-claude lanes (#844).
- **tmux-lane receipts** now emit truthful completion status and timestamps; extra-flags argument handling rewritten with `shlex.split` to eliminate quoting edge cases (H3/H5, #845).
- **Dispatch broker atomicity** — orphan dispatch window and `claim_next` TOCTOU races closed; adapter pipe hygiene ensures `SIGPIPE` does not silently swallow worker output (H1/H6, #848).
- **Self-learning duplicate-dominance** — injection history now suppresses patterns that dominated past injections even when their raw score is high; root cause of 93% duplicate injection rate resolved (#850).

### Changed

- tmux-spawn documented as the default dispatch lane for parallel independent work; subprocess-dispatch reserved for terminal-pinned work (#824, #825).
- README benchmark claim rewritten from package feature to repo methodology (#832); roadmap privacy trim moved operational detail to private state (#827).
- **Docs truth-pass for 1.0 launch** — version labels corrected, ADR provenance added, surface sync between README/ROADMAP/CHANGELOG and shipped code completed (#846).

## [1.0.0-rc9] — 2026-05-26

### Added

- **feat(governance) GOV-3 (#655)** `scripts/traceability_audit.py` — re-runnable observability tool that cross-references PRs/commits/dispatches/receipts and reports traceability gaps. Four gap categories (A–D): dispatches without completion receipt, receipts with unresolvable dispatch_id, merged PRs without receipt cross-reference, completion receipts missing both pr_id and dispatch_id link. Supports `--since`/`--until` date range, `--repo PATH` override, atomic markdown output. 44 unit tests. First run against vnx-dev (2026-01-01 → 2026-05-26): Category C gaps at 6.0% (most PRs linked via branch-slug heuristic); Categories A/B/D reflect tmux-era receipt schema predating current linkage fields.
- **feat(governance) GOV-2 (#654)** `scripts/pr_merge.py` canonical T0 merge path: instead of raw `gh pr merge`, T0 calls this script which merges the PR and atomically emits a `pr_merged` receipt (pr_number, dispatch_id, conclusion, merge_method) to `t0_receipts.ndjson` + `dispatch_register.ndjson`. `scripts/backfill_pr_merged_receipts.py` reconciles already-merged PRs against existing receipts; idempotent, supports `--dry-run` / `--limit` / `--since`. Fixes FPY/history gap: previously merged PRs left no governance trail.

### Fixed

- **fix(provider-dispatch) CL1 (#644)** Non-claude provider dispatch (kimi/codex) now captures correct `status`, `output`, and `tokens` in completion receipts. Receipt fields were being dropped for non-claude cheap lanes.

### Changed

- **feat(subprocess-dispatch) CL2 (#652)** Cheap-lane execution routes through `provider_dispatch` instead of Claude fallback. Subprocess dispatch now uses the provider-agnostic entry-point for cheap-lane tasks — removes the hardcoded Claude-only path and enables kimi/codex/gemini as cheap-lane workers.
- **refactor(providers) CL3 (#643)** Constraint renamed: `deepseek-path-d-blocked` → `deepseek-harness-subscription-blocked`. Semantics expanded: own-key + hardening path (`ANTHROPIC_API_KEY` + `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` + MCP off) is now explicitly **allowed**; subscription-redirect (no own key, rides OAuth subscription) remains blocked. Measured on claude v2.1.150: 0 calls to `api.anthropic.com` with own-key + hardening.

### Refactored

- **refactor(quality-db) OI #645** `bootstrap_qi_db` extracts migration registry (OI-1542/1544/1541) — migration blocks now indexed, removes inline migration sprawl.
- **refactor(benchmark) OI #646** Extract source-info + report-writers from benchmark main (OI-1510) — cuts benchmark script below size threshold.
- **refactor(dispatcher) OI #647** Extract stuck-cleanup python + supervisor-ticks from dispatcher (OI-1521/1523).
- **refactor(receipt-proc) OI #648** Extract mtime-calc python from bootstrap-protection (OI-1525/1524).
- **refactor(doctor) OI #649** Extract worktree + settings checks from `cmd_doctor` (OI-1573).
- **refactor(install-central) OI #650** Move shim content to template file (OI-1562) — shim generator no longer embeds multi-line heredoc inline.
- **refactor(migrate-central) OI #653** Extract `migrate_import` module from `migrate_to_central_vnx.py` (OI-1537/1539, part 1/3).
- **refactor(migrate-central) OI #657** Extract `migrate_schema` module from `migrate_to_central_vnx.py` (OI-1536/1533, part 2/3).

## [1.0.0-rc3] — 2026-05-20

### Added
- chore: bump version to 1.0.0-rc3; Wave 2a centralisation milestone
- feat(env): Wave 2a feature flag block in `vnx.env.example` (VNX_USE_CENTRAL_DB, VNX_RUNTIME_PRIMARY, VNX_CANONICAL_LEASE_ACTIVE, dormant Wave 5/6 flags)
- docs: dry-run manifest + rapport voor 4-project centralisatie (`claudedocs/wave2a-dag1-dry-run-2026-05-20.md`); 1,891,733 rijen gescand, 0 read errors, risico-classificatie per project

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
