# Feature: Phase 12 — Memory Layer And Cross-Domain Learning (Layer 2)

**Status**: Draft
**Priority**: P0
**Branch**: `feat/w-mem-memory-layer`
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional

Primary objective:
Add the Layer-2 memory subsystem: vector embeddings of artifacts (receipts, dispatches, feedback files, decisions), domain-partitioned retrieval, and auto-injection at dispatch. Enables cross-dispatch learning, operator-preference propagation, and feedback continuity across context rotation. Per `.vnx-data/state/PROJECT_STATE_DESIGN.md` §Layer-2: sqlite-vec for storage, nomic-embed-text via Ollama for embeddings, hard partition per domain with shared operator-prefs. Spans 4 waves: w-mem-1 (setup), w-mem-2 (indexer), w-mem-3 (retrieval API), w-mem-4 (auto-injection).

## Dependency Flow
```text
PR-0 (depends on W11 workers=N for worker_id partition keys)
PR-0 -> PR-1
PR-1 -> PR-2
PR-2 -> PR-3
PR-3 -> PR-4
PR-3, PR-4 -> PR-5
```

## PR-0: sqlite-vec + Ollama Setup, Schema Migration (W-MEM-1)
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 0.75 day
**Dependencies**: []

### Description
Wave w-mem-1. Install and pin sqlite-vec extension. Configure Ollama provider (already migrated in W9 PR-4) for nomic-embed-text. Add schema migration for `vec_artifacts_<domain>` tables (one per domain) plus `vec_operator_prefs` (shared cross-domain). Hard partition is enforced at table level so cross-domain leakage is structurally impossible.

### Scope
- Install sqlite-vec via vendored binary (no system-package dependency)
- Pin nomic-embed-text model in `OllamaProvider.embed()`
- Schema migration: `vec_artifacts_code`, `vec_artifacts_marketing` (initial domains), `vec_operator_prefs`
- Migration is idempotent
- Health check: verify Ollama daemon up and embedding model pulled before migration commits

### Success Criteria
- Migration applies cleanly on fresh DB
- Migration applies cleanly on existing DB (idempotent)
- Ollama embedding round-trip: text → 768-dim vector → text-similarity preserved
- Cross-domain physical separation: each domain table is independent

### Quality Gate
`gate_pr0_setup_w_mem_1`:
- [ ] Migration idempotent
- [ ] Ollama embedding produces 768-dim vectors
- [ ] sqlite-vec ANN query returns sane neighbors on test corpus
- [ ] Health-check refuses migration if Ollama daemon down (no half-migrated state)
- [ ] Cross-domain tables physically separate (verified by SQL)

## PR-1: Artifact Indexer And Embedding Pipeline (W-MEM-2)
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 1 day
**Dependencies**: [PR-0]

### Description
Wave w-mem-2. Background indexer that watches receipts, dispatches, feedback_*.md, and decisions, embeds them, and inserts into the appropriate domain table. Includes lifecycle states (active, fading, archived) and decay schedule. Idempotent: rerunning the indexer over the same artifact set yields the same index.

### Scope
- `scripts/lib/artifact_indexer.py` — watch + index
- Source readers for receipts, dispatches, feedback_*.md, decisions
- Domain detection from artifact metadata (receipts have agent_kind from W12; feedback files have explicit domain prefix)
- Lifecycle state field per row: `active | fading | archived`
- Decay schedule: artifact unaccessed for 30 days → fading; 90 days → archived
- Idempotency: re-index produces no new rows for same artifact

### Success Criteria
- Indexer ingests all four artifact types
- Domain detection correct on test corpus
- Re-index is no-op (idempotent)
- Lifecycle transitions on schedule
- Decay decreases retrieval weight without deleting rows

### Quality Gate
`gate_pr1_indexer_w_mem_2`:
- [ ] All four artifact types indexed
- [ ] Domain detection correct (no marketing artifact in code domain or vice versa)
- [ ] Re-index idempotent
- [ ] Lifecycle decay test: artifact fast-forward → fading → archived state transitions verified
- [ ] Decay reduces retrieval weight (not row deletion)

## PR-2: Domain Partitioning And Retrieval API (W-MEM-3)
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1.5 day
**Dependencies**: [PR-1]

### Description
Wave w-mem-3. Retrieval API (CLI + programmatic) with hard domain partition: a query in code-context cannot retrieve marketing artifacts, even with high semantic similarity. Operator-prefs are explicitly cross-domain (a single shared table). Hard partition is security-relevant: a cross-domain retrieval bug means marketing prompts could leak code secrets and vice versa, which is a trust collapse. Opus required.

### Scope
- `scripts/lib/memory_retrieval.py` — `retrieve(query, domain, k=5)` returns top-k
- CLI: `python3 scripts/memory_cli.py retrieve --domain code --query "..."`
- Operator-prefs lookup is parallel and merged at result time (not by joining tables)
- Domain enforcement: caller must pass domain; default refused (no implicit domain)
- Re-ranking: combine semantic similarity with recency and lifecycle state

### Success Criteria
- Code query retrieves only code-domain artifacts plus operator-prefs
- Marketing query retrieves only marketing-domain artifacts plus operator-prefs
- Operator preference set in code domain is visible from marketing dispatch context
- Default-no-domain → refused with structured error (no silent fall-through)
- Re-ranking produces sensible top-5 on golden corpus

### Quality Gate
`gate_pr2_retrieval_w_mem_3`:
- [ ] **Hard partition test**: query with code-context cannot retrieve marketing artifacts even with semantic similarity
- [ ] **Operator pref cross-domain test**: operator preference set in code domain shows up in marketing dispatch context
- [ ] No-domain query refused with structured error
- [ ] Re-ranking produces deterministic order on golden corpus
- [ ] Performance: retrieval latency under 50ms for k=5 on 10k-row corpus
- [ ] CODEX GATE on this PR is mandatory wave-end gate (security-relevant)

## PR-3: Auto-Injection At Dispatch And Feedback Sync (W-MEM-4)
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @architect
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1.5 day
**Dependencies**: [PR-2]

### Description
Wave w-mem-4. The dispatch hot path is augmented to retrieve relevant memory and inject into the worker's context envelope. Feedback files (`feedback_*.md` in MEMORY.md) are bidirectionally synced: appended to the index after each dispatch with feedback-shape detection. Critical hot-path code: a bug here delays every dispatch or worse, injects wrong-domain content. Opus required.

### Scope
- Inject hook in `subprocess_dispatch.py` and tmux dispatch path
- Retrieval: based on dispatch's intended worker domain + agent_kind + recent task topic
- Injection format: appended to skill context, clearly delimited as memory excerpt
- Feedback sync: post-dispatch hook scans final receipt for feedback signals, embeds and indexes
- Performance budget: injection ≤ 200ms total (retrieval + serialization)
- Killswitch: `VNX_MEMORY_INJECT=off` disables auto-injection without breaking dispatcher

### Success Criteria
- Dispatch envelope contains injected memory excerpt with proper delimiter
- Memory is correct domain (verified against agent_kind)
- Feedback files synced bidirectionally
- Injection adds less than 200ms to dispatch latency
- Killswitch disables cleanly

### Quality Gate
`gate_pr3_inject_w_mem_4`:
- [ ] Dispatch envelope contains injected memory excerpt
- [ ] Memory injected is correct domain (cross-domain regression test)
- [ ] **Forget test**: operator deletes specific artifact → vanishes from index AND raw store; replay-cache notes the deletion
- [ ] Performance: injection ≤ 200ms on reference hardware
- [ ] Killswitch disables cleanly without dispatch breakage
- [ ] Feedback files bidirectionally synced
- [ ] CODEX GATE on this PR is mandatory wave-end gate
- [ ] CLAUDE_GITHUB_OPTIONAL on this PR is mandatory triple-gate (modifies dispatch hot path; an injection bug means every dispatch is corrupted)

## PR-4: Lifecycle Decay And Forget Implementation
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate
**Estimated Time**: 0.75 day
**Dependencies**: [PR-3]

### Description
Operator-facing lifecycle controls: `forget` command (delete artifact from index + raw store + record deletion in replay cache), `archive` command, periodic decay job. Surfaces in CLI for operator-driven memory hygiene.

### Scope
- `python3 scripts/memory_cli.py forget --artifact <id>` — fully removes from index, raw store, marks in replay cache
- `python3 scripts/memory_cli.py archive --artifact <id>` — moves to archived state, retains row but excludes from retrieval
- Periodic decay job: cron-driven (or VNX supervisor tick)
- Replay-cache prevents re-indexing of forgotten artifacts (defense against accidental re-ingestion)

### Success Criteria
- Forget removes from index and raw store
- Forget records deletion in replay cache; re-encountering artifact during indexing → ignore
- Archive transitions state without deletion
- Periodic decay runs deterministically

### Quality Gate
`gate_pr4_lifecycle`:
- [ ] Forget removes both index and raw store rows
- [ ] Forgotten artifact rediscovered → replay cache prevents re-indexing
- [ ] Archive preserves row but excludes from retrieval
- [ ] Periodic decay job deterministic and idempotent

## PR-5: End-To-End Integration Tests (Embedding Swap, Soak, Adversarial)
**Track**: B
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 1.25 day
**Dependencies**: [PR-3, PR-4]

### Description
Comprehensive end-to-end memory layer test suite. Includes embedding-model swap (without index corruption), soak test (10k artifacts indexed and retrieved), adversarial cross-domain probes, and operator-pref cross-domain validation.

### Scope
- Embedding-model swap test (nomic-embed-text → bge-small)
- Soak test (10k artifacts)
- Cross-domain adversarial probes
- Operator-pref cross-domain validation
- Lifecycle full-cycle test
- Forget round-trip test
- Performance regression suite

### Success Criteria
- Embedding swap works without index corruption (re-embedding pipeline triggers, old vectors marked stale, new vectors generated, retrieval continues)
- 10k-artifact corpus retrieval under 100ms p95
- Cross-domain adversarial probes all rejected
- Operator-pref cross-domain still works
- Lifecycle decay full-cycle exercised
- Forget round-trip clean

### Quality Gate
`gate_pr5_memory_e2e`:
- [ ] **Embedding model swap test**: change from nomic-embed-text to bge-small → re-embedding works without index corruption
- [ ] 10k-artifact soak: p95 retrieval ≤ 100ms
- [ ] Adversarial cross-domain: 1000 high-similarity false-positive marketing→code probes all rejected
- [ ] Operator-pref cross-domain still works after swap
- [ ] Lifecycle decay full-cycle exercised end-to-end
- [ ] Forget round-trip clean (no resurrection on subsequent indexer pass)
- [ ] CODEX GATE on this PR is mandatory feature-end gate
- [ ] CLAUDE_GITHUB_OPTIONAL on this PR is mandatory triple-gate (memory layer is dispatch-hot-path and security-relevant for hard partition)

## Test Plan (Phase-Level — Security And Correctness Critical)

### Setup / Schema (W-MEM-1)
- Migration idempotent (apply twice → no diff)
- sqlite-vec ANN query: known nearest neighbor returned for known query in test corpus
- Ollama embedding determinism: same input → same vector (within float tolerance)
- Cross-domain physical separation: SQL inspection confirms separate tables, no shared rows

### Indexer (W-MEM-2)
- All artifact types ingested: receipts, dispatches, feedback_*.md, decisions
- Domain detection: 100-artifact mixed corpus, 100% correct domain assignment
- Idempotency: re-index → row count unchanged
- **Lifecycle decay test**: artifact ages, confidence decays, eventually graduates to archived state — fast-forwarded for test (mock time)

### Retrieval (W-MEM-3 — security-critical)
- **Hard partition test**: query with code-context cannot retrieve marketing artifacts even with semantic similarity. Construct adversarial corpus where a marketing artifact is highly semantically similar to a code query → retrieval still rejects (table-level separation)
- **Operator pref cross-domain test**: operator preference set in code domain shows up in marketing dispatch context, by being merged in retrieval API not by table-join
- Default-no-domain query → structured rejection
- Re-ranking deterministic: same query, same corpus → same top-5 order
- Performance: retrieval p95 < 50ms on 10k-row corpus

### Auto-Injection (W-MEM-4 — hot-path-critical)
- Inject envelope correctly delimited
- Domain regression: marketing-bound dispatch never receives code-domain memory
- Performance budget: injection latency p95 < 200ms
- Killswitch: `VNX_MEMORY_INJECT=off` disables, dispatch behaves identically to pre-feature
- Feedback sync: feedback in receipt → indexed → retrievable on next dispatch

### Forget / Lifecycle
- **Forget test**: operator deletes specific artifact → vanishes from index AND raw store; replay-cache notes the deletion. Re-running indexer with same source → forgotten artifact NOT re-ingested
- Archive: row preserved, retrieval excluded
- Periodic decay: idempotent

### Embedding Model Swap
- **Embedding model swap test**: change from nomic-embed-text to bge-small → re-embedding pipeline triggers, old 768-dim vectors marked stale, new vectors (different dim if applicable) generated, retrieval continues without corruption. Sentinel artifact pre-swap is retrievable post-swap

### Soak / Performance
- 10k-artifact corpus: index, retrieve, lifecycle decay, all under SLO
- Concurrent retrieval: 4 workers querying simultaneously → no lock contention
- Indexer + retrieval simultaneous → no read/write conflicts

### Adversarial Tests
- Construct marketing artifact whose embedding is highly similar to code query → still excluded by hard partition
- Construct fake operator preference (issued by sub-orchestrator without operator key) → rejected (cap-token check on pref insertion)
- Indexer fed corrupted artifact → rejected with structured error, no partial index state

### Operator UX
- CLI commands: `retrieve`, `forget`, `archive`, `inspect`
- All produce machine-parseable output (JSON option)
- Dry-run mode for `forget`
