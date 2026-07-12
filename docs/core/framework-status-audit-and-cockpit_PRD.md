# PRD — Framework status audit + subsystem cockpit (Optie A — descoped)

**Track:** `framework-status-audit-and-cockpit`
**Goal:** Turn the completed complexity-vs-value investigation into a sequence of small, independently deployable PRs that give VNX a single living **cockpit** for subsystem status (MAP + ON/OFF + HEALTH) and wire **read-only effectiveness probes** before any dormant loop is activated. This track **maps and measures** the framework; it does **not** build new enforcement.
**Authoring lens:** implementation feasibility and effort.
**Constraint discipline:** PRs ≤300 LOC, independently deployable, central-mode path correctness, no `anthropic-sdk`, ADR-007 for any new central table, tests per PR.

---

## 0. Descope decision (Optie A — 2026-07-12)

The v4 plan-gate scored **REVISE** for one honest reason: glm-5.2 verified every code-claim as true against the tree, but three seats (kimi/deepseek/codex) found the PRD **promised enforcement primitives that do not exist as code**:

1. `plan_gate_evidence.py` (the named "review-floor owner") only records `plan_gate_pass` — it does not enforce per-PR review floors.
2. `governance_enforcement.effective_level(check, project_id)` — the DB>env>YAML precedence resolver — does not exist (only `governance_enforcer.py`).
3. `gate_state()` reads env + DB, not YAML — the claimed YAML-precedence is not built.

**Decision (operator, Vincent, 2026-07-12): descope, do not build primitives inside the cockpit PRD.** The cockpit's value — a living MAP of every subsystem, its flag, its declared status, and a read-only HEALTH probe — does **not** depend on those primitives. This PRD keeps exactly the buildable, non-enforcing work and makes the enforcement gaps explicit and honest:

- **Review floors are ADVISORY-ONLY.** A PR records its `pr_type` and review floor in its body; the floor is surfaced, not merge-blocking. Making floors binding is deferred.
- **Governance PARK flags are DISPLAY-ONLY cockpit metadata.** `config_registry` registers them so the cockpit shows the governance subsystems as `PARK`; no read-site is wired to honor a level in this track. Enforcement wiring is deferred.
- The three missing primitives become **three separate later-horizon tracks** (created 2026-07-12), each un-parked when this cockpit track merges:
  - **`review-floor-enforcer`** — make `plan_gate_evidence.py` enforce floors (advisory→blocking) + the plan-gate SCOPE skip (former PR-16).
  - **`governance-effective-level-resolver`** — build `effective_level(check, project_id)` with DB>env>YAML precedence + wire the governance read-sites onto it (former PR-12/13/14).
  - **`gate-state-yaml-precedence`** — give `gate_state()` its YAML-precedence layer + evidence-bound-gate wiring (former PR-15).

**Net:** the enforcement teeth already live at the dispatch door (ADR-030, `#1111`, `vnx-dev` required). The cockpit does not add a second enforcement layer — it maps and measures. Nothing in this PRD drops a column, rebuilds a table, or changes an enforcement decision.

**In-scope PRs:** PR-1, PR-2, PR-3, PR-4, PR-5, PR-6, PR-7, PR-8, PR-11, PR-17, PR-18 (11 PRs).
**Removed from this PRD (→ follow-on tracks):** PR-9, PR-10 (→ `migration-consolidation-and-tenancy-cut`, parked earlier); PR-12, PR-13, PR-14, PR-15, PR-16 (→ the three primitive tracks above).

---

## 1. Background and non-negotiable decisions

A 6-agent code-grounded review + 5-provider deliberation panel concluded that VNX is:

- a solid **orchestration kernel** (~half the surface) that earns its keep, and
- an **experimental governance/intelligence lab** (~half the surface) that is built + tested but lives in an undeclared state — neither consciously ON nor consciously OFF.

The following decisions are **input, not up for debate** in this PRD:

| Subsystem | Decision | Rationale (summary) |
|-----------|----------|---------------------|
| Provider routing + constraints, git-grounded reconcile, `phantom_guard`, tmux operational scar-tissue, zero-LLM injection, dispatch-plan, test suite | **KEEP** untouched | Kernel earns its keep. |
| Migration mechanisms (schemas/migrations + appliers) | **PARK-with-trigger** | Real surface is 42 `schemas/migrations/*.sql` + 6 appliers; the collapse touches live stores. Moved to the verified track `migration-consolidation-and-tenancy-cut`. This PRD ships only the read-only inventory-lock (PR-8). |
| Within-DB ADR-007 multi-tenancy (`project_id` composite keys) | **PARK-with-trigger** | Dropping `project_id` interacts with the active central-store/dual-write + ADR-026 federation path. Moved to the same separate track. Not touched here. |
| Docs/marketing bloat | **CUT** | Inflates `docs/` count without operational value. Archived by rule (PR-11). |
| Governance-enforcement stack: receipt hash-chain, signed attestation, evidence-bound gate | **PARK-with-trigger, SURFACED (not wired)** | 0% adoption today. This PRD **surfaces** them as cockpit metadata + a read-only probe; the enforcement wiring is deferred to `governance-effective-level-resolver` + `gate-state-yaml-precedence`. |
| Intelligence/self-learning loop | **ACTIVATE-and-measure (gate built, default dormant)** | Currently dormant. This PRD builds the injection-effectiveness probe (PR-6) and the activation **gate** (PR-17); the loop stays off by default and only runs when the probe is healthy AND flags are on. |
| Plan-gate panel (`plan_gate_panel.py`) | **SCOPE to complex features only — DEFERRED** | The scope-skip enforcement (former PR-16) moves to `review-floor-enforcer`. This PRD registers the `VNX_PLAN_GATE_COMPLEX_ONLY` flag as cockpit metadata only. |

The **cockpit** is the unifying deliverable: a single living surface for MAP (what lives where), ON/OFF (config_registry flags), and HEALTH (read-only effectiveness probes).

---

## 2. Deliverable breakdown — concrete PRs

PRs are ordered by dependency. Each PR is 150–300 LOC, independently deployable, and includes tests. Titles are one-line; scope, acceptance criteria, and tests follow.

### 2.0 Per-PR authoring label + review floor (ADVISORY)

Every PR carries a `pr_type ∈ {docs, feat, chore, test, refactor}` author label in its body and a **review floor** (the minimum reviewer set expected to PASS). In this track the floors are **advisory**: they are recorded in the PR body and surfaced, and T0 honors them at review time, but they are **not** machine-enforced at merge. Making floors binding — wiring the read-site in `plan_gate_evidence.py` from advisory→blocking — is the `review-floor-enforcer` follow-on track, not this one.

Build lane is fixed by the provider constraint split (`~/.claude/rules/provider-constraints.md`): claude/Opus/Sonnet route the **tmux-subscription lane** (never `claude -p`, never the SDK); kimi/glm/deepseek route `provider_dispatch`.

| PR | pr_type | task_class (staging) | Build lane | Review floor (advisory) |
|----|---------|----------------------|-----------|--------------------------|
| PR-1 SUBSYSTEMS.md + registry | `docs` | `coding_interactive` | claude-sonnet tmux | codex diff-mode |
| PR-2 config_registry flags | `feat` | `coding_interactive` | claude-sonnet tmux | codex + kimi |
| PR-3 `vnx subsystems` CLI | `feat` | `coding_interactive` | claude-sonnet tmux | codex + kimi |
| PR-4 dashboard cockpit tile | `feat` | `coding_interactive` | claude-sonnet tmux | codex |
| PR-5 probe registry + base | `feat` | `coding_interactive` | claude-sonnet tmux | codex + kimi |
| PR-6 injection-effectiveness probe | `feat` | `coding_interactive` | claude-sonnet tmux | codex + kimi + deepseek |
| PR-7 governance/plan-gate/migration probes | `feat` | `coding_interactive` | claude-sonnet tmux | codex + kimi |
| PR-8 migration inventory (read-only) | `chore` | `coding_interactive` | claude-sonnet tmux | codex diff-mode |
| PR-11 docs bloat cleanup | `docs` | `coding_interactive` | claude-sonnet tmux | codex diff-mode |
| PR-17 intelligence loop activation gate | `feat` | `coding_interactive` | claude-sonnet tmux | codex + kimi + deepseek |
| PR-18 dashboard effectiveness wiring | `feat` | `coding_interactive` | claude-sonnet tmux | codex |

`pr_type` selects the reviewer set only; it is **not** `dispatches.task_class` (the FP-C routing vocab). The 5-model plan-gate panel applies to this whole track (it is a complex feature); the trivial-skip is deferred to `review-floor-enforcer`.

**Routing `task_class` vs `pr_type` (panel-recurring finding, resolved here).** The FP-C routing `task_class` (`coding_interactive`/`research_structured`/`docs_synthesis`/…) is set by **T0 at dispatch staging**, not in the PR body. All 11 in-scope PRs stage as `coding_interactive` on the single `claude-sonnet` tmux-subscription lane, because every one is a code/docs edit on this repo with no cross-provider routing decision to make — there is no cheaper correct lane for a Sonnet worker editing VNX. The per-deliverable **quality floor** is therefore the review floor in the table above (codex ± kimi ± deepseek), applied at review time; a uniform build lane is correct precisely because the work is uniform. A follow-on track that needs a non-Sonnet lane (e.g. a kimi synthesis pass) sets a different `task_class` at staging.

### 2.1 Governance PARK subsystems are SURFACED, not wired (descope)

The governance-enforcement stack (hash-chain, signed attestation, evidence-bound gate) is surfaced in the cockpit as `PARK`:

- **PR-2 registers `config_registry` metadata flags** (`VNX_GOVERNANCE_ENFORCED`, `VNX_HASH_CHAIN_REQUIRED`, `VNX_ATTESTATION_REQUIRED`) with `status=PARK` so the cockpit MAP shows them. These are **display metadata**; no read-site is wired to a level in this track. **To avoid cockpit-vs-reality drift** (deepseek finding — a static `off` bool could read `off` while `governance_enforcement.yaml` enforces at advisory): the governance row's DISPLAYED effective value is read read-only from the actual `.vnx/governance_enforcement.yaml` level, not from the static bool. The bool is a registry entry for completeness; the value the cockpit shows tracks the real YAML level. Building the write-side resolver that makes a flag CHANGE that level is still deferred to `governance-effective-level-resolver`.
- **PR-7 measures them read-only:** the governance effectiveness probe reads `.vnx-attest/plan-gates.ndjson` (the real hash-chained attestation file — `governed.ndjson` does NOT exist), verifies the chain via `scripts/lib/ndjson_hash_chain.py:verify_chain(path)` (read-only), and reports `produces_crap` only when a chained file is *broken* (tamper), not merely *unchained* (see PR-7). It **reports**; it does not enforce.

Everything that would make a flag *change enforcement behavior* — the `effective_level(check, project_id)` DB>env>YAML resolver, the read-site wiring in hash-chain/attestation/evidence-gate, and the `gate_state()` YAML-precedence layer — is **explicitly out of scope** and lives in `governance-effective-level-resolver` + `gate-state-yaml-precedence`. The existing `.vnx/governance_enforcement.yaml` level system and the existing `gate_state()` (`off|advisory|required`, default advisory) are **untouched** by this PRD.

---

### PR-1: SUBSYSTEMS.md skeleton + registry subsystem metadata
**Title:** `docs(core): add SUBSYSTEMS.md SSOT and link config_registry entries to subsystems`

**Scope (≈250 LOC):**
- Create `docs/core/SUBSYSTEMS.md` with the status-ledger table (§5). The `health` column is a **hand-seeded initial snapshot**, not a generated value; the header states "seeded — regenerated from probes once `vnx subsystems --md` lands," so the SSOT claim is not asserted before the generator exists.
- Extend `scripts/lib/config_registry.py`: add optional `subsystem: Optional[str]` and `status: Optional[str]` fields to `ConfigEntry` (values: `LIVE`, `PARK`, `CUT`, `ACTIVATE`, `SCOPE`, `COCKPIT`).
- **Backfill every existing registry entry** with a `subsystem` + `status` (the PR-1 test asserts completeness). Entries with no dedicated flag get their subsystem from a `CONFIG_REGISTRY_SUBSYSTEMS` mapping.
- Add `all_effective()` output keys: `subsystem`, `status`.
- **Also git-add the descoped PRD** `docs/core/framework-status-audit-and-cockpit_PRD.md` (currently untracked) as part of this PR, so the planning artifact enters git alongside the ledger.
- No runtime behaviour change; defaults unchanged.

**Acceptance criteria:**
- `SUBSYSTEMS.md` renders correctly in GitHub preview.
- `config_registry.all_effective()` returns `subsystem` and `status` for every entry.
- No flag default changes.

**Tests:**
- `tests/test_config_registry.py`: assert every registry entry has a `subsystem` and a `status`.
- `tests/test_subsystems_md_exists.py`: assert `docs/core/SUBSYSTEMS.md` exists and contains the header `| subsystem | what | flag | status | health |`.

---

### PR-2: config_registry completion — subsystem flags
**Title:** `feat(config): add missing subsystem flags for governance, intelligence, and migration`

**Scope (≈280 LOC):**
- **Already-registered flags — do NOT re-add** (kimi finding): `VNX_EVIDENCE_BOUND_GATE` and `VNX_PLAN_GATE_ENFORCE` already exist in `config_registry`; the §5 seed references them. PR-2 only relies on PR-1's backfill for their `subsystem`+`status` and does **not** re-register them. The list below is the **net-new** flags this PR adds.
- Add flags to `scripts/lib/config_registry.py`:
  - `VNX_GOVERNANCE_ENFORCED` (bool, default `"0"`, category `gate`, approval=True, status `PARK`, subsystem `governance-enforcement-stack`) — **display metadata only** (§2.1); no read-site wired in this track.
  - `VNX_LEARNING_LOOP_ENABLED` (bool, default `"0"`, category `intelligence`, status `ACTIVATE`, subsystem `intelligence-self-learning-loop`).
  - `VNX_DREAM_SCHEDULER_ENABLED` (bool, default `"0"`, category `intelligence`, status `ACTIVATE`, subsystem `dream-consolidation`).
  - `VNX_INJECTION_FEEDBACK_ENABLED` (bool, default `"0"`, category `intelligence`, status `ACTIVATE`, subsystem `injection-effectiveness-eval-loop`).
  - `VNX_PLAN_GATE_COMPLEX_ONLY` (bool, default `"0"`, category `gate`, status `SCOPE`, subsystem `plan-gate-panel`) — **display metadata only**; the scope-skip read-site is deferred to `review-floor-enforcer`.
  - `VNX_HASH_CHAIN_REQUIRED` (bool, default `"0"`, category `gate`, approval=True, status `PARK`, subsystem `receipt-hash-chain`) — display metadata only.
  - `VNX_ATTESTATION_REQUIRED` (bool, default `"0"`, category `gate`, approval=True, status `PARK`, subsystem `signed-attestation`) — display metadata only.
  - `VNX_MIGRATION_SYSTEM` (enum `"manifest"`, category `dispatch`, writable=False, status `PARK`, subsystem `migration-mechanisms`) — a **pinned selector** recording which migration mechanism is active; parked pending the `migration-consolidation-and-tenancy-cut` trigger.
- Update `dashboard/token-dashboard/app/operator/config/page.tsx` to render the new categories and `PARK`/`ACTIVATE`/`SCOPE` badges.
- Update `dashboard/api_config.py` if needed to pass the new metadata (should be transparent).

**Acceptance criteria:**
- `vnx config` (or dashboard config API) lists the new flags.
- New flags default to off and require approval where marked.
- `VNX_MIGRATION_SYSTEM` is read-only and defaults to `manifest`.
- No read-site behavior changes: registering these flags does not alter any gate/enforcement decision (they are metadata).

**Tests:**
- `tests/test_config_registry.py`: assert new keys exist, defaults correct, approval flags require approval.
- `tests/test_api_config.py`: assert `/api/operator/config` returns the new flags.

---

### PR-3: `vnx subsystems` CLI command
**Title:** `feat(cli): add vnx subsystems command rendering live SSOT`

**Scope (≈260 LOC):**
- Add `vnx_cli/commands/subsystems.py` implementing `vnx subsystems [--json] [--md] [--project-id ID]`.
- Reads the **union of three sources** (the ledger is NOT generable from `config_registry` alone: rows like `provider-routing`, `phantom_guard` have no flag): (a) `config_registry.CONFIG_REGISTRY` + `all_effective()` for flag-backed subsystems, (b) the `CONFIG_REGISTRY_SUBSYSTEMS` map (PR-1) for flag-less subsystems, (c) health beacons via `scripts/lib/health_beacon.py:all_beacons(state_dir)` — **the real signature requires `state_dir: Path`, and the beacon root is `VNX_DATA_DIR` (health_beacon writes/reads under `VNX_DATA_DIR/health`), NOT `VNX_STATE_DIR`** (kimi finding — resolve the data dir via the same `_resolve_data_dir` helper the door uses; do NOT call `all_beacons()` argless). Rowset = (a) ∪ (b); health = (c) with the committed seed as fallback.
- Output columns: subsystem, status, flag, effective value, provenance, health, last signal.
- `--md` emits the exact `SUBSYSTEMS.md` status-ledger table (the CI-regeneration source of truth).
- Register command in `vnx_cli/main.py` (`_register_subsystems_subparser` + `_dispatch_command`).
- Add a thin bash wrapper in `scripts/commands/subsystems.sh` and wire it in `bin/vnx` for repo-local parity (dual-CLI: new commands go in BOTH).
- **Own the drift-check:** add `.github/workflows/subsystems-drift.yml` (and a `make subsystems-check` target it calls) that runs `vnx subsystems --md` and diffs the **deterministic columns** (subsystem/flag/status/effective-value) against committed `docs/core/SUBSYSTEMS.md`, failing on any difference. **The inherently-dynamic `health` column is excluded from the diff** (kimi finding — otherwise live probe health would trip the check on every run and force constant ledger recommits); no `SUBSYSTEMS.md` regeneration target is needed for health because health is not diffed.
- **Health column reproduces the committed seed until probes exist** (otherwise the drift-check fails on every commit between PR-3 and PR-7): `--md` reads each subsystem's `health` from a live beacon when one exists, else falls back to the value committed in `SUBSYSTEMS.md`. **The seed-parse mechanic is in scope** (glm finding): `--md` parses the `health` cell out of the committed `docs/core/SUBSYSTEMS.md` table (split each data row on `|`, take the health column) and uses it as the fallback, so the round-trip is byte-identical before probes exist and the drift-check passes. Before PR-5–7 land, `--md` reproduces the seed exactly; once probes emit beacons, `health` goes live. The deterministic columns (subsystem/flag/status/effective-value) are always generated and always checked, and match the PR-2 registry state (the seed lists the same flags PR-2 registers, so the deterministic round-trip holds after PR-2).
- **This PR owns the `vnx subsystems --probe` flag surface** (deepseek finding: PR-3 and PR-5 are parallel DAG siblings, so PR-5 must not edit this PR's `subsystems.py`). `--probe` calls `subsystem_health.aggregate()` behind a **guarded import** that returns `unknown` / `"no probe registered"` when the module is absent. PR-5 supplies that module, so `--probe` goes live on PR-5's merge with no edit back to this file and no added DAG dependency.

**Acceptance criteria:**
- `vnx subsystems` prints a human-readable table.
- `vnx subsystems --json` emits machine-readable JSON.
- `vnx subsystems --md` emits the ledger table verbatim; the drift-check (added by this PR) asserts `SUBSYSTEMS.md` matches and fails CI otherwise.
- Health column falls back to the committed seed / `unknown` when no beacon exists.
- `all_beacons` is called with a resolved `state_dir` (regression guard against the argless-call bug).

**Tests:**
- `tests/vnx_cli/test_subsystems.py`: assert JSON output contains expected subsystems and health keys.
- `tests/scripts/test_subsystems_sh.py`: assert the bash wrapper invokes the Python CLI and returns 0.

---

### PR-4: Dashboard subsystem cockpit tile
**Title:** `feat(dashboard): subsystem cockpit tile on operator/observability page`

**Scope (≈240 LOC):**
- Add `dashboard/api_subsystems.py` with `GET /api/operator/subsystems` returning `{project_id, subsystems: [...]}`.
- Wire the endpoint in `dashboard/serve_dashboard.py`.
- **Extend the existing `operator/observability/page.tsx` with a subsystem cockpit tile** (one deliverable, not a new route): the tile shows the status ledger (subsystem, flag, status badge, health badge, effective value).
- Reuse existing SWR hook pattern; add `useSubsystems()` in `dashboard/token-dashboard/lib/hooks.ts`.
- Add `postConfigSet` integration so status badges update after toggles (without page reload): after a successful `postConfigSet`, call SWR `mutate()` on the `useSubsystems()` key to revalidate the tile immediately (kimi finding — specify the revalidation mechanism, don't leave it vague).

**Acceptance criteria:**
- Dashboard renders every subsystem from `SUBSYSTEMS.md`.
- Toggling a flag in `/operator/config` updates the cockpit tile within one SWR refresh.
- `unknown` health is visually distinct.

**Tests:**
- `tests/dashboard/test_api_subsystems.py`: assert endpoint returns JSON with ≥10 subsystems.
- `tests/dashboard/test_page_subsystems.py` (React Testing Library): assert cockpit tile renders `governance-enforcement-stack` row.

---

### PR-5: Effectiveness probe registry + base class
**Title:** `feat(health): add effectiveness probe registry and base class`

**Scope (≈230 LOC):**
- Create `scripts/lib/effectiveness_probe.py` with:
  - `EffectivenessProbe` abstract base class (`probe()`, `signal()`, `health()`).
  - `ProbeResult` dataclass: `status ∈ {ok, degraded, produces_crap, unknown}`, `signal` (one-line), `detail` dict.
  - `EFFECTIVENESS_PROBES` registry mapping subsystem → probe class.
  - `PROBE_TO_BEACON` mapping from the probe vocabulary to `HealthBeacon`'s classification: `ok→ok`, `degraded→stale`, `produces_crap→fail`, `unknown→` (no beacon / `stale` with signal `"no probe"`). **A tampered/broken hash-chain maps to `fail`, NOT `corrupt`** (kimi/deepseek finding): `health_beacon.py` derives `corrupt` only from unreadable JSON and `all_beacons()` has no `status=='corrupt'→health` branch, so routing a probe tamper-signal to `corrupt` would fall through to staleness logic and misclassify. The governance probe emits `fail` for a broken chain and records `"tamper"` in the beacon `detail`; `corrupt` stays owned by the beacon layer for unparseable files.
- Add `scripts/lib/subsystem_health.py` aggregator that runs all registered probes and emits a beacon via `health_beacon.py` for each subsystem, translating through `PROBE_TO_BEACON`.
- **Supply the module behind `vnx subsystems --probe`** (the flag surface is owned by PR-3; deepseek finding — PR-3/PR-5 are parallel siblings, so PR-5 does NOT edit PR-3's `subsystems.py`). PR-3's `--probe` calls `subsystem_health.aggregate()` behind a guarded import; this PR provides `subsystem_health.py`, so `--probe` goes live on this PR's merge with no cross-file edit and no added DAG dependency. Before this lands, `--probe` returns `unknown` / `"no probe registered"`.
- **ADR-007 scope statement:** probes (PR-5/6/7) are **read-only over existing stores + file-based beacons** written under `.vnx-data/health/` via `health_beacon.py`. They create **no new central-DB table**, so ADR-007 composite-`project_id`-key does not attach. If any probe ever needs central-DB persistence, that becomes its own PR carrying a composite `(project_id, …)` UNIQUE/PK per ADR-007. `health_beacon.all_beacons(state_dir: Path)` and `verify_chain(path)` are read-only.

**Acceptance criteria:**
- `subsystem_health.aggregate()` returns a dict keyed by subsystem.
- Each result has `status` and `signal`.
- Unknown subsystems report `unknown` with signal `"no probe registered"`.
- `vnx subsystems --probe` runs the aggregator and merges probe health; with no probe registered it reports `unknown`.

**Tests:**
- `tests/test_effectiveness_probe.py`: register a dummy probe, run it, assert result shape.
- `tests/test_subsystem_health.py`: assert aggregator emits beacons under `.vnx-data/health/`.

---

### PR-6: Injection-effectiveness eval-loop probe
**Title:** `feat(intelligence): build injection-effectiveness probe before activating learning loop`

**Scope (≈290 LOC):**
- Add `scripts/lib/injection_effectiveness_probe.py` implementing `EffectivenessProbe` for the intelligence self-learning loop.
- Signal sources (all PERSISTED, verified to exist): the persisted **`pattern_usage` table** used/ignored counts (kimi finding — read the persisted table, NOT the in-memory `PatternUsageMetric` object, which is per-process and empty in a probe run), `pending_rules.json`/`pending_skill_refinements.json` counts, the `dream_cycles` table count. **No "injection outcome receipts" source** (that artifact does not exist): `ignore_rate` is computed from the `pattern_usage` used/ignored totals alone.
- Health rules — classification is a **total function of `r` alone** (`r` = ignore_rate ∈ [0,1]); the reason-signal is DETAIL in the beacon, never a branch condition. Checked top-down, first match wins:
  - `unknown`: no data (zero used AND zero ignored) — checked first so an empty counter never divides by zero.
  - `produces_crap`: `r ≥ 0.90`.
  - `degraded`: `0.50 ≤ r < 0.90`, OR proposals stalled > 7 days.
  - `ok`: `r < 0.50` and proposals flowing.
  - Every `r ∈ [0,1]` matches exactly one branch; endpoints `0.90`/`0.50` owned by `produces_crap`/`degraded`. A recorded reason-signal is added to `detail` (explains WHY, does not change status).
- Write a beacon with details: `ignore_rate`, `used_count`, `ignored_count`, `pending_proposals`, `last_dream_cycle_iso`.

**Acceptance criteria:**
- `vnx subsystems --probe` shows `intelligence-self-learning-loop` health driven by real counters.
- The probe does **not** activate the loop; it only measures.
- `VNX_INJECTION_FEEDBACK_ENABLED` remains off.

**Tests:**
- `tests/test_injection_effectiveness_probe.py`: mock high ignore rate → `produces_crap`; mock balanced → `ok`.
- `tests/test_subsystem_health.py`: assert the probe is registered under `intelligence-self-learning-loop`.

---

### PR-7: Probe wiring for governance, plan-gate, and migration
**Title:** `feat(health): add effectiveness probes for governance stack, plan-gate, and migration`

**Scope (≈280 LOC):**
- Add probes (all read-only):
  - `scripts/lib/governance_effectiveness_probe.py`: reads `.vnx-attest/plan-gates.ndjson` — **the real hash-chained attestation file; `.vnx-attest/governed.ndjson` does NOT exist** (kimi finding, verified) — verifies the chain via `scripts/lib/ndjson_hash_chain.py:verify_chain(path)`, counts attestations. **`unchained` vs `broken` (glm/deepseek finding):** entries with NO `prev_hash` are *unchained* — the expected PARK state (hash-chain off), reported `ok`/PARK, **not** `produces_crap`. Entries that DO carry `prev_hash` but fail `verify_chain()` are *broken* (tamper) — reported `produces_crap` (beacon `fail`, with `"tamper"` in `detail`; **not** `corrupt`, which the beacon layer reserves for unreadable JSON — kimi/deepseek). **Known detection limitation:** this read-only probe inherits `verify_chain`'s open **#1086 prefix-strip weakness** — it cannot detect a prefix-strip forgery until #1086's origin-pinning lands; the probe records this caveat in its signal `detail`.
  - `scripts/lib/plan_gate_effectiveness_probe.py`: reads plan-gate state from `.vnx-attest/plan-gates.ndjson` (the panel attestation records) plus the OI-PLAN blocker state under `VNX_DATA_DIR` (kimi finding — name the source), reports `ok` when recent panels converge and `degraded` when panel verdicts disagree. It does **not** check "complex tracks skip the panel" (kimi finding) — the scope-skip read-site is deferred to `review-floor-enforcer`, so that signal does not exist yet; that check is added when the skip lands.
  - `scripts/lib/migration_effectiveness_probe.py`: reads `schema_manifest.py` invariant manifest vs. actual `PRAGMA user_version` and reports `ok`/`degraded`/`produces_crap`.
- Register all three in `EFFECTIVENESS_PROBES`.

**Acceptance criteria:**
- `vnx subsystems --probe` returns health for `governance-enforcement-stack`, `plan-gate-panel`, and `migration-mechanisms`.
- Hash-chain broken (e.g., tampered receipt) is detected even in PARK mode (measured, not enforced).

**Tests:**
- Unit tests for each probe with mocked inputs.
- Integration test: corrupt a temp hash-chain file, assert probe reports `produces_crap`.

---

### PR-8: Migration surface inventory-lock (read-only, no removal)
**Title:** `chore(migrations): lock the full migration-surface inventory (evidence for the parked consolidation track)`

**Scope (≈220 LOC):** This PR is **read-only cataloguing**. It does NOT remove or deprecate anything — the consolidation + ADR-007-tenancy CUT are PARKed to `migration-consolidation-and-tenancy-cut`, and this PR produces the locked inventory that is that track's un-park trigger.
- Add `scripts/lib/migration_inventory.py` that enumerates the actual surface (verified 2026-07-11):
  1. `schemas/migrations/*.sql` — 42 files (all `NNNN_`-prefixed; 7 also `2026_`-date-prefixed).
  2. `scripts/lib/migrations/apply_00NN.py` — the per-version python applier series.
  3. `scripts/lib/schema_manifest.py` — invariant manifest / `user_version` reconcile.
  4. `scripts/lib/schema_migration.py` and `scripts/lib/migrations/auto_apply.py`.
  5. `scripts/lib/project_id_migration.py` — the ADR-007 tenancy-stamping migration.
  6. `vnx_cli/commands/migrate.py` — the CLI entrypoint + ADR-007 fail-closed tenant reconciliation.
- For each surface, record: path(s), file count, tables touched, and central-DB (shared) vs per-project classification.
- **Completeness oracle** (`verify_complete()`), a hard test-gate — **both universes rooted at the SAME migrations dir** (codex finding: the oracle and its test must not compare different file universes):
  1. the filesystem-glob enumeration of `<root>/schemas/migrations/*.sql` MUST match `git ls-files '<root>/schemas/migrations/*.sql'` **exactly** — a file on disk but untracked, or tracked but missing on disk, is itself a FAIL. Catching disk↔git divergence IS the oracle's job, so the two sets are compared over one root, not a count against a different universe.
  2. every `scripts/lib/migrations/apply_*.py` module on disk MUST appear in the applier-series surface.
  3. every enumerated SQL file MUST resolve to ≥1 touched table with a non-`unknown` `central_db` classification.
  The migrations root is a parameter (defaults to the repo) so the completeness test can run against a **monkeypatched temp root**, never the real repo dir.
- Update the `migration-mechanisms` row in `docs/core/SUBSYSTEMS.md` health to `degraded` with signal `"42 SQL files + 6 appliers; consolidation PARKed pending inventory-lock"`.

**Acceptance criteria:**
- `migration_inventory.py` returns all six surfaces with real file paths and counts (≥42 SQL files under surface 1).
- Each surface carries a `central_db: bool` classification per touched table.
- No file is deleted, archived, or deprecated by this PR.

**Tests:**
- `tests/test_migration_inventory.py`: assert ≥42 SQL files enumerated and all six surfaces present.
- `tests/test_migration_inventory_classification.py`: assert every touched table has a `central_db` classification (no `unknown`).
- `tests/test_migration_inventory_completeness.py`: assert `verify_complete()` passes on the real tree AND, using a **monkeypatched temp migrations root** (not the real repo dir, no pollution), fails when a synthetic untracked `*.sql` is added to that temp root — proving the oracle detects a disk↔git-universe divergence over one shared root.

---

### PR-9 + PR-10 — PARKED to `migration-consolidation-and-tenancy-cut`

The original PR-9 (collapse migrations) and PR-10 (remove within-DB ADR-007 tenancy) are in the separate parked track (own plan-gate, own PRD). This PRD keeps only the read-only inventory (PR-8). The cockpit does not depend on either CUT.

---

### PR-11: Docs bloat cleanup
**Title:** `docs: archive stale comparisons and marketing content`

**Scope (≈200 LOC):**
- Move to `docs/_archive/` by a **named, correctness-defined rule** (not a volume target):
  - `docs/comparisons/*.md` (enumerated list committed in the PR).
  - `docs/archive/*.md` (already-archived content, consolidated into `_archive/`).
  - Every `docs/**/*.md` that is BOTH (a) unreachable from `docs/DOCS_INDEX.md` AND `README.md`, AND (b) untouched in git for > 12 months — the exact path list is generated by the archiver script and committed as `docs/_archive/ARCHIVED_MANIFEST.md`.
- Delete empty directories left behind.
- Update `docs/DOCS_INDEX.md` to remove links to archived content.
- Add `docs/_archive/README.md` explaining the archive policy.
- No code changes.

**Acceptance criteria:**
- Every archived file satisfies the rule (unreachable AND >12mo stale) — asserted per-file against `ARCHIVED_MANIFEST.md`.
- No file reachable from `DOCS_INDEX.md`/`README.md` is archived.
- All remaining top-level docs entries still have a link from `DOCS_INDEX.md` or `README.md`.
- `docs/_archive/` is excluded from docs-site generation.

**Tests:**
- `tests/test_docs_index.py`: assert every non-archive `docs/**/*.md` is reachable from `DOCS_INDEX.md`.
- `tests/test_docs_archive.py`: assert archived files are under `docs/_archive/` only.

---

### PR-12 / PR-13 / PR-14 / PR-15 / PR-16 — REMOVED (→ follow-on primitive tracks)

The governance-enforcement wiring and the plan-gate scope-skip are the enforcement primitives the v4 gate flagged as not-yet-built. They are removed from this PRD and split into three later-horizon tracks (created 2026-07-12), each un-parked when this cockpit track merges:

- **`review-floor-enforcer`** (former PR-16): make `plan_gate_evidence.py` enforce the §2.0 review floors (advisory→blocking) and add the plan-gate SCOPE skip (`VNX_PLAN_GATE_COMPLEX_ONLY` read-site in `plan_gate_enforcement.py`/`dispatch_cli.py`), fail-closed on unknown/missing `plan_gate_scope`.
- **`governance-effective-level-resolver`** (former PR-12/13/14): build `governance_enforcement.effective_level(check, project_id)` with precedence `project_config (DB) > VNX_OVERRIDE_<CHECK> (env) > governance_enforcement.yaml (fleet base)`, the un-park audit-row writer via `set_config()`, and wire the hash-chain + attestation read-sites onto it.
- **`gate-state-yaml-precedence`** (former PR-15): give `evidence_bound_gate.gate_state()` its YAML-precedence layer so the `evidence_bound_gate` YAML level and the env override agree, plus the CI read-site.

Each track carries its own PRD, its own plan-gate, and (for hash-chain) the `#1086` origin-pinning merge as a hard prerequisite before any mandatory level.

---

### PR-17: Intelligence loop activation gate (default dormant)
**Title:** `feat(intelligence): activate learning loop only when effectiveness probe is healthy`

**Scope (≈260 LOC):** Builds the **gate**, not an always-on loop. Default stays dormant.

**The activation gate is the PROBE HEALTH (from PR-6), not a flag** (glm/codex finding — clarify the gate). The two flags are the enable-switch: `VNX_LEARNING_LOOP_ENABLED` arms the loop, and `VNX_INJECTION_FEEDBACK_ENABLED` must also be on because the injection-effectiveness probe IS the gate — arming the loop without the probe that gates it (path 2) is the misconfiguration hard-error. Probe health then decides run (only `ok`) vs skip (anything else).

- Modify `scripts/learning_loop.py` — four explicit, non-overlapping paths:
  1. `VNX_LEARNING_LOOP_ENABLED=0` → dormant no-op (default; not an error, no beacon).
  2. `VNX_LEARNING_LOOP_ENABLED=1` AND `VNX_INJECTION_FEEDBACK_ENABLED=0` → **hard error** (cannot activate learning without the effectiveness probe that gates it).
  3. Both enabled, probe health is **anything other than `ok`** (i.e. `unknown` no-baseline, `degraded`, OR `produces_crap`) → **exit early with a `degraded` beacon, do not update patterns**. `degraded` (50–90% ignore) is NOT healthy enough to feed generation, so it is gated exactly like `unknown`/`produces_crap` — this closes the "degraded activates the loop" safety gap (codex finding).
  4. Both enabled AND probe health `ok` (clean) → run the cycle. Only a clean `ok` activates.
- Modify the **actual dream-cycle executor** `scripts/dream/consolidator.py:run_dream_cycle()` similarly — gate on `VNX_DREAM_SCHEDULER_ENABLED` + the **injection-effectiveness probe health (PR-6, `scripts/lib/injection_effectiveness_probe.py`)**, the same probe named as the learning-loop gate (kimi finding: name the probe). **Not** `scripts/dream/scheduler.py` (it only installs the LaunchAgent/crontab). The gate sits in the executor so it holds for both scheduled (`vnx dream run` → `dream.py` → `run_dream_cycle`) and manual paths.
- Update `dashboard/api_intelligence.py` to expose the probe signal (`ignore_rate`, `pending_proposals`).

**Acceptance criteria:**
- Loop stays dormant by default (`VNX_LEARNING_LOOP_ENABLED=0`) — path 1, no error.
- Enabling the loop without the injection probe flag → hard error — path 2.
- Probe `unknown` (no data), `degraded`, OR `produces_crap` → no pattern updates + degraded beacon — path 3.
- Probe `ok` (clean) → cycle runs — path 4. `degraded` does NOT activate.

**Tests:**
- `tests/test_learning_loop_gate.py`: enabled without feedback flag → error; enabled + `ok` probe → cycle runs.
- `tests/test_learning_loop_crap_signal.py`: probe `produces_crap` → no updates, degraded beacon.
- `tests/test_learning_loop_unknown_probe.py`: probe `unknown` → no updates, degraded beacon (does NOT activate blind).
- `tests/test_learning_loop_degraded_no_activate.py`: probe `degraded` → no updates, degraded beacon (does NOT activate — the safety-gap regression guard).

---

### PR-18: Dashboard effectiveness health wiring
**Title:** `feat(dashboard): surface effectiveness-probe signals on health and intelligence pages`

**Scope (≈250 LOC):**
- Extend `dashboard/api_health.py` to include subsystem effectiveness summary.
- Extend `dashboard/api_intelligence.py` with `/api/operator/intelligence/effectiveness` endpoint.
- Update `dashboard/token-dashboard/app/operator/health/page.tsx` to show subsystem health cards.
- Update `dashboard/token-dashboard/app/operator/intelligence/page.tsx` to show a **point-in-time** injection-effectiveness gauge: the current `ignore_rate` + `pending_proposals` from the latest beacon. **Not a time-series** (deepseek finding: no ignore_rate history store exists) — a sparkline is only added later if beacon history is persisted.
- Reuse existing SWR hooks.

**Acceptance criteria:**
- Health page shows ≥10 subsystem cards with health badges.
- Intelligence page shows `ignore_rate` and `pending_proposals` counts.
- `unknown` health prompts the user to add/improve a probe.

**Tests:**
- `tests/dashboard/test_api_health_subsystems.py`: assert health API includes subsystem effectiveness.
- `tests/dashboard/test_intelligence_effectiveness.py`: assert effectiveness endpoint returns expected shape.

---

## 3. Sequencing + dependency DAG

```
Phase 0 — Cockpit foundation
  PR-1  SUBSYSTEMS.md + registry metadata (+ commits the descoped PRD)
    │
    ▼
  PR-2  config_registry completion (metadata flags only)
    │
    ├──► PR-3  vnx subsystems CLI
    │
    ├──► PR-4  Dashboard cockpit tile
    │
    └──► PR-5  Effectiveness probe registry
           │
           ├──► PR-6  Injection-effectiveness probe
           │
           └──► PR-7  Governance/plan-gate/migration probes

Phase 1 — Safe read-only work (no live-store changes)
  PR-8  Migration surface inventory-LOCK (read-only)
  PR-11 Docs bloat cleanup (no code changes)

Phase 2 — ACTIVATE-and-measure (gate built, default dormant)
  PR-17 Intelligence loop activation gate (depends on PR-6 + PR-2)
    │
    ▼
  PR-18 Dashboard effectiveness wiring (depends on PR-7 + PR-4)
```

**Hard dependencies:**
- PR-1 before everything (adds the `subsystem`/`status` fields + the ledger).
- PR-2 before PR-3, PR-4, PR-17.
- PR-5 before PR-6 and PR-7.
- PR-6 before PR-17.
- PR-7 + PR-4 before PR-18.
- PR-1 before PR-8 and PR-11 (SUBSYSTEMS.md must exist first).

**Soft ordering:**
- PR-3, PR-4, PR-5, PR-8, PR-11 can run in parallel after PR-1+PR-2.
- PR-6, PR-7 after PR-5.
- PR-17, PR-18 last.

---

## 4. Risk / rollback per deliverable

| PR | Primary risk | Mitigation | Rollback |
|----|--------------|------------|----------|
| PR-1 | SUBSYSTEMS.md becomes stale | Seed header states "regenerated from probes"; PR-3 adds CI drift-check | Revert markdown + registry metadata fields |
| PR-2 | New flags accidentally change defaults/behavior | All defaults off; **no read-sites wired** (metadata only) | Revert registry entries; UI falls back to not rendering unknown keys |
| PR-3 | CLI output format breaks scripts | `--json` is the stable contract | Revert command module; bash wrapper stays compatible |
| PR-4 | Dashboard performance with many subsystems | Server-side aggregation; SWR caching | Revert page component; API remains |
| PR-5 | Probe abstraction too heavy | One class + one registry | Delete module; no runtime callers yet |
| PR-6 | Counter signal inaccurate | Total-function classification; reason-signal in detail | Disable probe registration |
| PR-7 | Hash-chain probe false positives on legacy receipts | ADR-029 epoch marker awareness; read-only | Tune probe thresholds |
| PR-8 | Inventory misses a surface | Read-only; completeness oracle asserts no gap; nothing deleted | Revert the module (no store touched) |
| PR-11 | Broken external links to docs | Archive README keeps original paths | Restore files from git history |
| PR-17 | Loop activates despite crap/unknown signal | Hard gate on probe status; dormant default | Set `VNX_LEARNING_LOOP_ENABLED=0` |
| PR-18 | Dashboard shows misleading health | Probe signals reviewed in PR-6/PR-7 | Hide tile via feature flag |

**Live-store mutations are OUT OF SCOPE for this PRD.** No PR here drops a column, rebuilds a table, merges migration files, or changes an enforcement decision. The only migration-related PR (PR-8) is read-only. Enforcement changes live in the three follow-on primitive tracks.

---

## 5. Initial `docs/core/SUBSYSTEMS.md` status-ledger table

The table below is the content to commit in PR-1. It is the seed for the SSOT and is regenerated from `config_registry` + probes in later PRs.

```markdown
# VNX Subsystem Status Ledger

This document is the single source of truth for which VNX subsystems are live, parked, cut, or scoped. **Bootstrap note (PR-1):** the table below is a hand-seeded initial snapshot; the `health` column is a seed, not yet a probe reading. Once `vnx subsystems --md` (PR-3) and the effectiveness probes (PR-5–7) land, the ledger is regenerated from `scripts/lib/config_registry.py` plus health probes and the CI drift-check (`.github/workflows/subsystems-drift.yml`, PR-3) forbids hand-edits. Until then, treat `health` values as seeds.

| subsystem | what | flag | status | health |
|-----------|------|------|--------|--------|
| provider-routing | Model/provider selection, constraint solving, fallback order. | — | LIVE | works — dispatch outcomes routed correctly |
| git-grounded-reconcile | Per-project canonical stores, git-provenance linking, no shared-state fork. | — | LIVE | works — `vnx fabric-audit` passes |
| phantom_guard | Receipt deduplication and replay protection. | — | LIVE | works — zero duplicate dispatches in test suite |
| tmux-operational-scar | Terminal/session lifecycle, session handover, F1.1 safe linkage. | — | LIVE | works — `vnx doctor` tmux checks pass |
| zero-llm-injection | No prompt injection via environment or receipts; strict input boundaries. | — | LIVE | works — red-team tests pass |
| dispatch-plan | Single-entry dispatch door, dispatch-plan reconciliation. | — | LIVE | works — dispatch tests pass |
| test-suite | Pytest + integration coverage for kernel and cockpit. | — | LIVE | works — CI green |
| migration-mechanisms | Schema-evolution surfaces (42 SQL files + 6 appliers). Consolidation PARKed pending inventory-lock. | `VNX_MIGRATION_SYSTEM` | PARK-with-trigger | degraded — 42 SQL files + 6 appliers; collapse deferred to a verified track |
| within-db-tenancy | Composite `(project_id, id)` keys inside per-project DBs. Removal PARKed pending per-table central-DB safety proof. | — | PARK-with-trigger | degraded — keys present; drop deferred (central-store/dual-write/ADR-026 interaction) |
| docs-bloat | Comparisons, stale archive, marketing docs inflating `docs/` count. | — | CUT | degraded — ~288 markdown files, large `_archive/` |
| governance-enforcement-stack | Receipt hash-chain + signed attestation + evidence-bound merge gate. SURFACED here; enforcement wiring deferred. | `VNX_GOVERNANCE_ENFORCED` | PARK-with-trigger | produces-crap — 15,577 receipts, 0 `prev_hash` |
| receipt-hash-chain | Tamper-evident NDJSON hash-chain (ADR-029). | `VNX_HASH_CHAIN_REQUIRED` | PARK-with-trigger | produces-crap — unchained receipts |
| signed-attestation | SSH-signed PR attestation manifests (ADR-027). | `VNX_ATTESTATION_REQUIRED` | PARK-with-trigger | produces-crap — 0 signed attestations in active use |
| evidence-bound-gate | D3 evidence-bound merge gate. | `VNX_EVIDENCE_BOUND_GATE` | PARK-with-trigger | produces-crap — advisory only, enforces nothing |
| intelligence-self-learning-loop | Daily pattern learning, skill refinements, confidence updates. | `VNX_LEARNING_LOOP_ENABLED` | ACTIVATE-and-measure | produces-crap — 98% injection ignore rate, 0 dream cycles |
| dream-consolidation | Nightly memory consolidation + pending review dispatch. | `VNX_DREAM_SCHEDULER_ENABLED` | ACTIVATE-and-measure | unknown — no cycles run |
| injection-effectiveness-eval-loop | Instrument WHY patterns are ignored before tuning generation. | `VNX_INJECTION_FEEDBACK_ENABLED` | ACTIVATE-and-measure | unknown — probe not built yet |
| plan-gate-panel | 5-model deliberation panel for plan-first enforcement. | `VNX_PLAN_GATE_ENFORCE` | SCOPE | works — panel runs, verdicts recorded |
| plan-gate-task-class-scope | Restrict panel to complex features; skip trivial tracks. Enforcement deferred to review-floor-enforcer. | `VNX_PLAN_GATE_COMPLEX_ONLY` | SCOPE | unknown — read-site deferred |
| subsystem-cockpit | SUBSYSTEMS.md + config_registry + `vnx subsystems` + dashboard tile. | — | COCKPIT | degraded — SSOT exists, probes partial |
| effectiveness-probe-framework | Generic "does it produce crap?" probes per subsystem. | — | COCKPIT | unknown — framework not built yet |
```

**Legend:**
- `LIVE` — running and expected to stay on.
- `PARK-with-trigger` — built + tested, currently off, with a documented un-park trigger.
- `CUT` — being removed; no future need.
- `ACTIVATE-and-measure` — dormant, gated on a probe/loop, then turned on with measurement.
- `SCOPE` — stays on, but restricted to a narrower class of work.
- `COCKPIT` — meta-subsystems that implement this very audit surface.

---

## 6. Definition of done for the umbrella track

The `framework-status-audit-and-cockpit` umbrella track is done when:

1. `docs/core/SUBSYSTEMS.md` exists, is auto-refreshable from `config_registry` + probes (PR-3 `--md`), and accurately reflects every subsystem decision.
2. `config_registry.py` contains a flag (metadata) for every toggleable subsystem; no subsystem is implicitly on/off in the MAP.
3. `vnx subsystems` and `/operator/observability` render the ledger live, including effective values and health.
4. Every `health: unknown` row has either (a) a merged read-only effectiveness probe, or (b) a documented reason a probe is infeasible.
5. The migration surface is **locked** (PR-8): `migration_inventory.py` enumerates all 42 SQL files + 6 appliers with per-table central-DB classification. The actual collapse is PARKed to `migration-consolidation-and-tenancy-cut` and is NOT a done-criterion here.
6. ADR-007 within-DB composite tenant keys are untouched; the drop is PARKed with its trigger.
7. Docs/marketing bloat is archived by rule; remaining docs are linked from `DOCS_INDEX.md`.
8. The governance-enforcement stack is **surfaced** in the cockpit as `PARK` (metadata + read-only probe). Its enforcement wiring is explicitly deferred to `governance-effective-level-resolver` + `gate-state-yaml-precedence`; **this PRD changes no enforcement decision**.
9. The plan-gate SCOPE flag is surfaced as metadata; the scope-skip enforcement is deferred to `review-floor-enforcer`.
10. The intelligence self-learning loop remains dormant by default; the activation **gate** (PR-17) is built so it can only run when the injection-effectiveness probe is healthy AND the flags are on; activation stays approval-gated and measured.
11. Review floors are recorded per PR and honored at review time as **advisory**; making them binding is `review-floor-enforcer`, not this track.
12. All 11 in-scope PRs are merged (PR-1..PR-8, PR-11, PR-17, PR-18), each ≤300 LOC, each with tests, a `pr_type` tag, CI green on every step.
13. A post-track `vnx doctor` + `vnx fabric-audit` + `vnx subsystems --probe` run shows: every `LIVE` subsystem is either `works`/`ok` from a probe OR carries a documented "no probe — asserted live via `<existing signal>`" note (provider-routing via dispatch tests, git-grounded-reconcile via `vnx fabric-audit`, phantom_guard/dispatch-plan/zero-llm-injection via their test suites, test-suite via CI-green, tmux via `vnx doctor`). All `PARK` subsystems report `PARK`, all `CUT` removed/archived, all `ACTIVATE` gated by a green probe. No `LIVE` row is left as bare `unknown`.

---

## 7. Provenance + revision log

**The "five overlapping migration mechanisms" premise was falsified** (plan-gate panel 2026-07-11, 4/5 seats) — the real surface is 42 SQL files + 6 appliers. Both the collapse and the ADR-007 tenancy drop were PARKed to `migration-consolidation-and-tenancy-cut` (operator option B); this PRD keeps only the read-only inventory-lock (PR-8).

**v2 (2026-07-11):** addressed the converged panel findings — per-PR routing floor + tags, PR-9/PR-10 parked, PR-8 rescoped to the real inventory, probe/beacon vocab mapping, #1086 pinned as an un-park blocker for the hash-chain flag.

**v3 (2026-07-11):** re-verified every code claim against the tree (an Explore verify pass) — two glm findings were themselves falsified (the `verify_chain` prefix-strip fix `d8f4dce0` is NOT on main; it lives on the open PR #1086). Split `pr_type` (author label) from `plan_gate_scope` (panel-skip tag).

**v4 (2026-07-11):** REVISE (glm: all load-bearing code claims hold; 3 seats: the PRD promises enforcement primitives that don't exist as code).

**v5 → Optie A descope (2026-07-12, operator Vincent):** rather than keep grinding the plan, the enforcement primitives the v4 gate flagged (`plan_gate_evidence` floor enforcement, `effective_level()` resolver, `gate_state()` YAML precedence) are **removed from this PRD** and split into three later-horizon tracks. This PRD keeps exactly the buildable cockpit-MAP + read-only probes (PR-1..8, PR-11, PR-17, PR-18); review floors are advisory; governance flags are display-only metadata. The gate did its job — the facts are correct — so the response is to build the working subset now and sequence the enforcement primitives behind their own gates, not to write a v5 plan. Enforcement teeth already exist at the dispatch door (ADR-030, #1111).

**v6 (2026-07-12) — clean-panel Optie-A REVISE addressed (one fix round).** The descoped plan went through a full-seat panel (kimi/glm-5.2-harness/deepseek/codex all scored, no lane flakes; opus abstained via the known `staging_validator` bug). All four confirmed the descope is **factually correct and the approach sound** (glm: "all load-bearing code claims verified true"; deepseek: "architecture sound"; codex: "salvageable"). `block_count=0`; the REVISE findings were concrete, fixable second-order gaps, all addressed here:
- **PR-7 governance probe retargeted** from the non-existent `.vnx-attest/governed.ndjson` to the real `.vnx-attest/plan-gates.ndjson` (kimi, verified on disk); `unchained` (PARK-expected) vs `broken` (tamper) defined; the open #1086 prefix-strip detection limitation named.
- **PR-6** dropped the non-existent "injection outcome receipts" signal (kimi) — `ignore_rate` is from `PatternUsageMetric` alone (verified in `learning_loop.py`).
- **PR-17 safety fix:** `degraded` probe health no longer activates the loop — only a clean `ok` runs; `degraded` is gated with `unknown`/`produces_crap` (codex); the activation gate (probe health) vs the enable-flags is clarified (glm).
- **`--probe` CLI flag** assigned to PR-5 as its named owner (glm/codex — no PR had scoped it); PR-3's seed-health-parse mechanic made explicit so the drift-check round-trips.
- **PR-8** oracle + synthetic-file test now compare ONE shared migrations root (git↔disk divergence is the oracle's job), test runs on a monkeypatched temp root (codex/deepseek).
- **PR-18** de-scoped to a point-in-time gauge (no ignore_rate time-series store exists — deepseek).
- **PR-2** distinguishes already-registered flags (`VNX_EVIDENCE_BOUND_GATE`, `VNX_PLAN_GATE_ENFORCE`) from net-new (kimi); plan-gate probe drops the "complex tracks skip" check whose read-site is deferred to `review-floor-enforcer` (kimi).
- **`task_class` routing** (the panel-recurring finding): named explicitly — set by T0 at staging, uniform `coding_interactive` on the single Sonnet lane because the work is uniform; the quality floor is the §2.0 review floor.

**v7 (2026-07-12) — second full-seat panel + second fix round (convergence, then operator attest).** The v6 doc went through another full-seat panel: **glm-5.2 PASSED**, kimi + deepseek REVISE, opus abstained (staging_validator bug), codex abstained (parse-error lane flake). `block_count=0`, revise dropped 4→2, one seat flipped to PASS — healthy convergence, not the v4-class non-convergence. The two REVISE seats' findings were addressed here (build-breakers first):
- **`corrupt` beacon collision** (kimi/deepseek — real build-breaker): a tampered chain now maps to beacon `fail` (with `"tamper"` in detail), not `corrupt`; `health_beacon.py` reserves `corrupt` for unreadable JSON and `all_beacons()` has no `corrupt`-from-status branch, so the old mapping would have misclassified.
- **PR-3/PR-5 `--probe` sibling conflict** (deepseek — real build-breaker): the `--probe` flag surface is owned by PR-3 (guarded import returns `unknown` until PR-5 supplies `subsystem_health.py`); no cross-file edit between DAG siblings, no added dependency.
- **PR-6 persisted `pattern_usage` table** not the in-memory `PatternUsageMetric` (kimi); **PR-17** names its gate probe (PR-6).
- **Governance cockpit-vs-reality drift** (deepseek): the governance row's displayed effective value reads the real `governance_enforcement.yaml` level read-only, not the static bool.
- **`task_class` column** added to the §2.0 table (all `coding_interactive`); **beacon root** pinned to `VNX_DATA_DIR` (kimi); **plan-gate probe source** named (`.vnx-attest/plan-gates.ndjson` + OI-PLAN state); **PR-4 SWR** revalidation via `mutate()` after `postConfigSet`; **drift-check** excludes the dynamic health column so live probe health doesn't churn CI.

After two full-seat panels confirming the descope is factually correct (block_count=0 both, glm PASS on the second) and one applied fix round per B3, the operator (Vincent, directive "descope Optie A en ga … tot klaar is en getest en werkt") attests the plan gate. The residual seat disagreement is second-order and converging; further re-gating is the non-convergence pattern the plan-first discipline explicitly avoids. Enforcement teeth already live at the dispatch door (ADR-030, #1111); this plan builds only the read-only cockpit + probes.
