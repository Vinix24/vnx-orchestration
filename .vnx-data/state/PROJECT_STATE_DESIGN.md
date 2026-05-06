# PROJECT_STATE_DESIGN — Strategic State for VNX

**Date:** 2026-05-01
**Author:** T0 research synthesis (no code changes)
**Status:** Design proposal. No PRs proposed; no code modified.
**Companion docs:** `claudedocs/2026-04-30-single-vnx-migration-plan.md`, `claudedocs/2026-04-30-state-memory-audit.md`.

---

## 1. TL;DR

VNX has solid **runtime state** (`runtime_coordination.db`, `t0_state.json`, `t0_receipts.ndjson`, `dispatch_register.ndjson`) but no canonical home for **strategic state** — the durable answer to "what is this project trying to do, what did we decide, and where are we in the roadmap?" Today that information is scattered across `claudedocs/` (operator-rejected, gitignored), ad-hoc `STATE.md` / `PROJECT_STATUS.md` / `HANDOVER_*.md` files in `.vnx-data/state/` (inconsistently named, single-project-only), and operator memory.

Recommendation: a **hybrid model** — markdown-of-record in a uniform `strategy/` folder, with a thin **append-only NDJSON event log** as the system-of-record, and **derived SQLite tables** in `quality_intelligence.db` for cross-project queries once the central-VNX migration starts.

- **Folder name (per project, today):** `.vnx-data/strategy/` — sibling of `state/`, not nested. Sibling because runtime state churns every minute, strategic state changes a few times per session; they have different lifetimes and different audiences.
- **Centralized future:** `~/.vnx-data/strategy/<project_id>/` — same files, prefixed by `project_id`, plus DB tables with `project_id` columns matching the migration plan §6 Phase 0 schema.
- **Three core artifacts:** `roadmap.yaml` (the plan), `decisions.ndjson` (append-only operator + T0 decisions), `current_state.md` (auto-projected human-readable one-pager).
- **Update protocol:** event-sourced. Decisions and roadmap edits are appended to `decisions.ndjson`; `current_state.md` and `roadmap.yaml.lock` are projections rebuilt at SessionEnd hook + on every PR merge.
- **Loading protocol:** `build_t0_state.py` already runs at SessionStart — extend it to read `strategy/` and inject a `strategic_state` block into `t0_index.json`. T0 reads it the same way it already reads runtime state. No new boot mechanism.

---

## 2. What strategic state IS (vs runtime state)

**Definitional boundary:**

| Dimension | Runtime state | Strategic state |
|---|---|---|
| Lifetime | Seconds to minutes | Hours to months |
| Update rate | Hundreds of writes/hour (receipts) | 1-20 writes/session |
| Audience | Daemons, dispatchers, schedulers | Operator, T0, future-T0-after-/clear |
| Query pattern | "Is T1 idle right now?" | "What did we decide about R4?" |
| Source of truth | Live DB / NDJSON ring buffers | Append-only decision log + projections |
| Loss tolerance | Reconstructable from receipts | Lossy = re-asking the operator the same question twice |
| Rebuild cost | Cheap (re-derive from events) | Expensive (re-elicit human intent) |

**Examples — runtime state (already handled, do NOT touch):**
- `t0_state.json` / `t0_index.json` — terminal status, queue counts, active dispatches
- `runtime_coordination.db` — terminal_leases, gate_results, incident_log
- `t0_receipts.ndjson` / `dispatch_register.ndjson` — append-only event streams
- `progress_state.yaml` — per-track current gate
- `pr_queue_state.json` — projection of open PRs

**Examples — strategic state (the gap):**
- "We chose Model B over Model A for centralization." (decision)
- "R4 is shipped, R1 is next, R2 deferred until subprocess is hardened." (roadmap status)
- "OD-1 is closed; the operator approved gemini-fallback when codex rate-limits." (policy decision)
- "PRD-VNX-UH-001 is the active PRD; v1.2 is current; v1.0 and v1.1 are superseded." (PRD index)
- "This project's mission: VNX governance for multi-project orchestration." (project charter)
- "Last session we paused at the F46 cleanup chain; resume there." (session continuity)

**Heuristic:** if `git log` + receipts can rebuild it deterministically, it's runtime. If it requires asking the operator "what did you mean?", it's strategic.

---

## 3. Framework benchmark

| Framework | Where | Format | Project-scoping | Update mechanism | Strategic vs runtime separation |
|---|---|---|---|---|---|
| **LangGraph** ([docs](https://docs.langchain.com/oss/python/langgraph/add-memory)) | Pluggable Checkpointer (Memory/Postgres/Redis) + Store | Per-thread checkpoint + cross-thread Store | `thread_id` (runtime), `namespace` tuple (cross-thread) | Every node-step writes a checkpoint; Store writes are explicit | Yes — Checkpointer = thread/runtime, Store = long-term/strategic. Different APIs, different backends. |
| **CrewAI** ([docs](https://docs.crewai.com/en/concepts/memory)) | Unified `Memory` class (2025 rebuild) | SQLite + vector index | Hierarchical scope tree (`/project/alpha`, `/agent/researcher`) | Active cognitive ops: `encode`/`consolidate`/`recall`/`extract`/`forget`. LLM-driven importance scoring | No — single API; "scope path" is the only differentiator. |
| **Aider** ([docs](https://aider.chat/docs/config/aider_conf.html)) | Project root | `.aider.conf.yml` (config) + `.aider.chat.history.md` (transcript) + `.aider.input.history` | Per-repo file location (cwd-scoped) | Config: hand-edited. History: auto-appended each turn | Yes — config = strategic (hand-edited), history = runtime (auto). |
| **Cline (VSCode)** ([docs](https://docs.cline.bot/troubleshooting/task-history-recovery)) | OS user dir under VSCode globalStorage | `state/taskHistory.json` (index) + `tasks/<task_id>/{api_conversation_history,ui_messages,task_metadata}.json` + `checkpoints/<workspace_hash>/` (shadow git) | `workspace_hash` keyed (per-folder) | Index file rebuilt by scanning `tasks/`; per-task files written live | Yes — index (strategic) vs per-task transcripts (runtime); checkpoints separately git-shadow'd. |
| **Cursor** ([docs](https://docs.cursor.com/context/rules)) | `.cursor/rules/*.mdc` (in-repo, committed) + community `.memory/` (Memory Bank pattern) | Markdown with frontmatter | Per-repo (the rules are checked-in) | Rules: hand-edited. Memory Bank: convention for AI to update structured project-state .md files | Yes — rules = strategic governance, memory bank = strategic project state, both file-based, both committed. |
| **AutoGen v0.4** ([docs](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/state.html)) | Caller-managed (whatever the host writes to disk) | `team.save_state()` returns dict; `dump_component()` returns JSON config | Caller's choice (no built-in scoping) | Explicit API calls — no auto-persistence | Yes — `dump_component()` = config (strategic), `save_state()` = runtime state. Two distinct serialization APIs. |
| **OpenAI Agents SDK** ([docs](https://openai.github.io/openai-agents-python/sessions/)) | `Session` protocol; `AdvancedSQLiteSession` keeps message_structure + turn_usage tables | SQLite (default `:memory:`, configurable path) | `session_id` per session; no native multi-project layer above session | Auto-write per turn via Session protocol (`add_items`, `pop_item`) | No — Session is the unit. Project-level concept is left to the caller. |
| **Claude Code** ([docs](https://code.claude.com/docs/en/memory)) | `~/.claude/projects/<encoded_cwd>/sessions/*.jsonl` + `CLAUDE.md` (in-repo) + `~/.claude/projects/<encoded_cwd>/memory/MEMORY.md` (auto-memory) | JSONL append-only sessions; Markdown for memory + CLAUDE.md | Encoded `cwd` path → directory key | Sessions: append-only on every turn. CLAUDE.md: hand-edited. Auto-memory: model-written | **Yes — three-layer.** Sessions = runtime, CLAUDE.md = strategic config (committed), MEMORY.md = strategic auto-memory (per-user). |

**Patterns the field has converged on:**

1. **Two-layer separation is universal.** Every mature framework distinguishes "the trace of what happened" (runtime, append-only, often JSONL/SQLite) from "the durable instructions / decisions / project shape" (strategic, often Markdown, often committed to git). VNX has the runtime layer; it lacks the strategic layer.
2. **Markdown wins for strategic state.** Cursor `.mdc`, Aider `.aider.conf.yml`+history.md, Claude Code `CLAUDE.md`+`MEMORY.md`, Cursor Memory Bank — all converge on human-readable text-of-record that the agent reads and the human edits.
3. **Append-only event log + projection** is the dominant pattern for "decisions that accumulate." LangGraph thread checkpoints, Claude Code JSONL, Cline taskHistory all follow it. The materialized view (current_state.md) is rebuildable; the source of truth is the event log.
4. **Project-scoping is either path-based (Cline workspace_hash, Claude Code encoded_cwd) or namespace-based (LangGraph namespace tuples, CrewAI hierarchical scope).** VNX should use the path-based model today and migrate to a `project_id` column once the central VNX consolidation lands (matches the migration plan exactly).
5. **No framework relies on a daemon to maintain strategic state.** Updates are event-driven (a hook on session-end, a tool-call writing to memory, a manual edit). VNX should not introduce a strategic-state daemon either — let it ride existing hooks.

---

## 4. VNX-specific recommendation

### 4.1 Folder layout (today, per-project)

```
.vnx-data/
  state/                          # UNCHANGED — runtime state stays here
    runtime_coordination.db
    t0_state.json
    t0_receipts.ndjson
    ...
  strategy/                       # NEW — strategic state, uniform across all projects
    project_charter.md            # WHAT this project is. Hand-edited. Rare changes.
    roadmap.yaml                  # The plan. Source of truth for waves/features.
    roadmap.lock.json             # Projection — derived from roadmap.yaml + decisions.ndjson + register events.
    decisions.ndjson              # Append-only operator + T0 decisions. SYSTEM OF RECORD.
    current_state.md              # Projection — auto-rebuilt one-pager for cold-start T0.
    open_plans.json               # In-flight strategic initiatives with state machine.
    session_log.ndjson            # Append-only session boundaries (start, end, summary).
    prd_index.json                # PRD inventory: path, version, status (draft/active/superseded).
    adr_index.json                # ADR inventory (architectural decision records).
    archive/                      # Superseded handovers, old roadmap snapshots.
```

**Why a sibling `strategy/`, not nested under `state/`:**
- `state/` already churns every minute and contains 130+ files; nesting strategic content would bury it.
- Different lifetimes, different audiences, different backup/retention policies (operator may want to git-track `strategy/` even though `.vnx-data/` is gitignored — this design supports adding a `.gitignore` exception for `.vnx-data/strategy/` if the operator chooses).
- Sibling makes the "centralized model" path-rewrite trivial: `.vnx-data/strategy/` → `~/.vnx-data/strategy/<project_id>/`.

**Why the same name in every project:** so T0's loader code is `read_strategy(strategy_dir)` regardless of which project. The single-VNX migration plan can then rewrite one path resolution function and the strategic-state loader works unchanged.

### 4.2 The three core files (everything else is optional)

#### `decisions.ndjson` — system of record

Append-only. Every operator decision, every T0 strategic choice, every roadmap mutation lands here as a line. Schema:

```json
{
  "ts": "2026-05-01T10:32:18Z",
  "decision_id": "OD-2026-05-01-001",
  "kind": "operator_decision | t0_decision | roadmap_change | plan_state | charter_change",
  "actor": "operator | T0",
  "scope": "project | feature_R4 | OI-1199 | PRD-VNX-UH-001",
  "summary": "Approve Model B for central VNX migration; defer Model A.",
  "rationale": "Auth boundaries differ across projects; subtree split too costly.",
  "supersedes": ["OD-2026-04-28-003"],
  "linked_artifacts": ["claudedocs/2026-04-30-single-vnx-migration-plan.md"],
  "session_id": "T0-20260501-1015"
}
```

`decision_id` uses operator-decision (`OD-`) or T0-decision (`TD-`) prefixes for grep-ability and to reuse the existing OI/OD vocabulary.

#### `roadmap.yaml` — the plan

Hand-edited (mostly). Single source of truth for what waves/features exist, in what order, and what state. Schema:

```yaml
schema: roadmap/1.0
project_id: vnx-dev
mission: "VNX governance for multi-project orchestration."
current_focus: "VNX-R series + central migration Phase 0"
waves:
  - id: VNX-R
    title: "Convergence series"
    state: in_progress
    items:
      - id: R1
        title: "Profile contract unification"
        state: planned       # planned | in_progress | completed | deferred | cancelled
        effort: M
        loc_estimate: 150
      - id: R4
        title: "Canonical receipt append for subprocess"
        state: completed
        completed_at: 2026-04-28
        pr: 295
      - id: R4b
        title: "Subprocess intelligence injection"
        state: completed
        pr: 296
  - id: CENTRAL-MIGRATION
    title: "Single-VNX consolidation (4 → 1)"
    state: planned
    items:
      - id: PHASE-0
        title: "project_id columns on hot tables"
        state: planned
        loc_estimate: 120
deferred:
  - id: F60
    title: "Playwright suite"
    deferred_at: 2026-04-28
    reason: "Operator confirmed Opt A — observability first."
```

#### `current_state.md` — projection (auto-generated)

Rebuilt at SessionEnd + after every PR merge by a small projection function. Composes from `roadmap.yaml`, `decisions.ndjson` (last N), `t0_state.json`, recent receipts, open items digest. Replaces `STATE.md`, `PROJECT_STATUS.md`, and the bespoke `HANDOVER_*.md` pattern. Schema (markdown):

```markdown
# Project: vnx-dev — Current State

**Last rebuilt:** 2026-05-01T10:32:18Z (trigger: pr_merged #297)

## Mission
VNX governance for multi-project orchestration.

## Current focus
VNX-R series + central migration Phase 0.

## Roadmap snapshot
- Wave VNX-R: 6/10 complete (R1-R4b done; R5-R10 planned)
- Wave CENTRAL-MIGRATION: 0/6 (Phase 0 next)
- Deferred: F60 (Playwright)

## In flight
- PR #297: feat/pr-queue-state-json (codex round 2)
- T1: idle. T2: idle. T3: idle.
- Open blockers: 1 (OI-1199)

## Last 3 decisions
- 2026-05-01 OD-001: Approve Model B for central migration
- 2026-04-30 OD-014: Defer F60 in favor of observability tier
- 2026-04-28 TD-022: Merge with stalled gemini = OK when infra issue, not finding

## Resume hints
- Next dispatch: PHASE-0 schema columns (~120 LOC)
- Active worktree: vnx-roadmap-autopilot-wt
```

### 4.3 Optional supporting files

- `open_plans.json` — list of in-flight plans (multi-session initiatives) with state machine: `{plan_id, title, state: planning|active|paused|completed|cancelled, owner, created_at, last_advanced_at, next_step}`. Useful when several plans run concurrently (R-series + central migration + observability tier all simultaneous).
- `session_log.ndjson` — one line per session-start and session-end: `{ts, kind: session_start|session_end, session_id, summary?, decisions_made: [], prs_merged: [], ois_closed: []}`. Replaces the ad-hoc `HANDOVER_*.md` pattern.
- `prd_index.json` — `[{prd_id, path, version, status, supersedes}]`. The first user of this is the existing PRD-VNX-UH-001 file (currently in claudedocs).
- `adr_index.json` — same shape for ADRs. Empty today; the migration plan calls out ADR creation as a Phase 1 deliverable.
- `project_charter.md` — one-pager: mission, scope, non-goals, key contacts. Hand-edited, rare. The first time T0 boots in a new project, prompt the operator to fill it in (or copy from a template).

### 4.4 DB-backed component (centralized future)

Once `quality_intelligence.db` gains the `project_id` column (migration plan §6 Phase 0), add three small tables for cross-project strategic queries:

```sql
CREATE TABLE strategic_decisions (
  decision_id TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  ts          TEXT NOT NULL,
  kind        TEXT NOT NULL,
  actor       TEXT NOT NULL,
  scope       TEXT,
  summary     TEXT NOT NULL,
  rationale   TEXT,
  supersedes  TEXT,                 -- JSON array
  artifacts   TEXT,                 -- JSON array
  session_id  TEXT
);
CREATE INDEX idx_decisions_project_ts ON strategic_decisions (project_id, ts DESC);

CREATE TABLE roadmap_items (
  project_id  TEXT NOT NULL,
  wave_id     TEXT NOT NULL,
  item_id     TEXT NOT NULL,
  title       TEXT NOT NULL,
  state       TEXT NOT NULL,         -- planned|in_progress|completed|deferred|cancelled
  effort      TEXT,
  loc_estimate INTEGER,
  pr          INTEGER,
  completed_at TEXT,
  PRIMARY KEY (project_id, wave_id, item_id)
);

CREATE TABLE plan_state (
  plan_id     TEXT NOT NULL,
  project_id  TEXT NOT NULL,
  title       TEXT NOT NULL,
  state       TEXT NOT NULL,
  owner       TEXT,
  created_at  TEXT NOT NULL,
  last_advanced_at TEXT,
  next_step   TEXT,
  PRIMARY KEY (project_id, plan_id)
);
```

The DB is **derived** — every row is reproducible from `decisions.ndjson` + `roadmap.yaml` via a small projector script. This preserves the file-based source-of-truth (operator can grep, git-track, edit) while enabling SQL queries like "show me every cancelled plan across all 4 projects in the last 30 days."

**Why hybrid (file + DB), not DB-only:**
- Markdown/YAML survives `rm -rf .vnx-data/state/runtime_coordination.db` (a single bad migration); DB-only doesn't.
- Operator can edit `roadmap.yaml` directly in any editor without a CLI; DB-only requires a CLI.
- Git-trackable strategic state means PRD/decision history can be code-reviewed; DB-only doesn't fit code review.
- DB exists for query performance and cross-project rollups, not as authority.

### 4.5 Update protocol

| Event | What writes | Where |
|---|---|---|
| Operator types "approve Model B" in T0 chat | T0 calls `record_decision(kind="operator_decision", ...)` helper | Append to `decisions.ndjson` |
| T0 makes architectural decision (e.g. "merge with stalled gemini = OK") | T0 calls same helper with `actor=T0, kind=t0_decision` | Append to `decisions.ndjson` |
| PR merged | Existing post-merge hook | Trigger projection rebuild (`current_state.md` regen, DB sync) |
| `roadmap.yaml` hand-edited | Operator | Optional `record_decision(kind="roadmap_change")` to track *why* |
| Session start | SessionStart hook (already exists) | Read `current_state.md` + `decisions.ndjson` last 10 → inject into T0 boot |
| Session end | SessionEnd hook (new, ~30 LOC) | Append `session_log.ndjson` entry; rebuild `current_state.md` |

The pattern matches LangGraph's checkpoint-on-step + Claude Code's JSONL-on-turn: writes are tied to events, never to a polling daemon.

### 4.6 Loading protocol

Extend `scripts/build_t0_state.py` to add a new section. Add to `_DETAIL_SECTION_MAP`:

```python
_DETAIL_SECTION_MAP["strategic_state"] = "strategic_state"
```

And a builder:

```python
def _build_strategic_state(state_dir: Path) -> Dict[str, Any]:
    strategy_dir = state_dir.parent / "strategy"   # .vnx-data/strategy
    if not strategy_dir.is_dir():
        return {"available": False}
    return {
        "available": True,
        "current_state_md": (strategy_dir / "current_state.md").read_text(...),
        "roadmap": _safe_yaml(strategy_dir / "roadmap.yaml"),
        "recent_decisions": _tail_ndjson(strategy_dir / "decisions.ndjson", n=10),
        "open_plans": _safe_json(strategy_dir / "open_plans.json"),
    }
```

T0 reads `t0_index.json` at SessionStart (already does) → sees `strategic_state.available=true` → loads `t0_detail/strategic_state.json` if it needs the heavy version. No new boot mechanism, no new file T0 has to be told to read.

### 4.7 Centralized-future shape

When the migration plan §6 Phase 3 lands (receipt envelope adds `project_id`):

```
~/.vnx-data/
  strategy/
    vnx-dev/
      project_charter.md
      roadmap.yaml
      decisions.ndjson
      current_state.md
      ...
    mc/
      ...
    sales-copilot/
      ...
    seocrawler-v2/
      ...
  state/
    quality_intelligence.db    # strategic_decisions, roadmap_items, plan_state tables, project_id column
    runtime_coordination.db
    ...
```

Loader becomes `~/.vnx-data/strategy/<project_id>/` where `project_id` comes from the existing `vnx_project_id.py` helper from migration Phase 2. Project A reading project A's strategy is the only allowed mode — the loader does NOT support reading other projects' strategy directly (that's what the DB is for, with explicit cross-project queries).

---

## 5. Migration plan — what to do with existing files

### 5.1 In `.vnx-data/state/` (today)

| File | Classification | Action |
|---|---|---|
| `STATE.md` (Apr 24, 6 lines, stale) | Auto-generated runtime snapshot, mislabeled | Delete after `current_state.md` lands. The content is already covered by `t0_state.json` and `PROJECT_STATUS.md`. |
| `PROJECT_STATUS.md` (auto-generated by `build_project_status.py`) | Runtime snapshot | Keep as-is. Already auto-generated from canonical sources. Move authorship comment to point at `strategy/current_state.md` for the strategic view. |
| `HANDOVER_2026-04-28.md` | Strategic + session-log hybrid | Move to `strategy/archive/HANDOVER_2026-04-28.md`. Extract its decision log into `decisions.ndjson` (one-shot import script, ~40 LOC). |
| `HANDOVER_2026-04-28-evening.md` | Same | Same — archive + extract. |

### 5.2 In `claudedocs/` (today)

| File | Classification | Action |
|---|---|---|
| `PRD-VNX-UH-001-universal-headless-orchestration-harness.md` | Strategic — active PRD | Move to `docs/prds/PRD-VNX-UH-001.md` (proper docs location). Add to `strategy/prd_index.json` as `status: active, version: 1.0`. claudedocs is wrong; docs is right. |
| `2026-04-30-single-vnx-migration-plan.md` | Strategic — active plan | Move to `docs/plans/2026-04-30-single-vnx-migration-plan.md`. Add `open_plans.json` entry: `{plan_id: CENTRAL-MIGRATION, state: planning}`. Add to `strategy/decisions.ndjson` as the deciding entry that selected Model B. |
| `2026-04-30-state-memory-audit.md` | Research — supports strategy | Move to `docs/research/`. Reference from `roadmap.yaml` notes. |
| `2026-04-30-adaptive-receipt-classifier-research.md` | Research | Move to `docs/research/`. |
| `2026-05-01-multi-orchestrator-research.md`, `2026-05-01-universal-harness-research.md` | Research that produced the active PRD | Move to `docs/research/`. PRD references them via `linked_research`. |
| Other audits / synthesis docs (5+ files) | Historical research | Move to `docs/research/archive/` keyed by date. |
| `2026-05-01-deferred-pr-audit.md` | Operational | Move to `docs/audits/`. |
| `VNX_feature_breakdown.md` | Strategic — historical roadmap | Bootstrap `strategy/roadmap.yaml` from this. Then archive the .md to `docs/research/archive/`. |
| `research-self-learning-agentic-rag-memory.md` | Research | Move to `docs/research/`. |

**Rule applied:** strategic plans live in `docs/` (committed) for cross-session durability and code-review; their *state* lives in `strategy/` (per project_id in centralized world). PRDs/ADRs/plans are **artifacts**; `strategy/*_index.json` files are **catalogs of those artifacts**.

### 5.3 What stays in `claudedocs/`

After migration: only the truly ephemeral. The operator's preference (`claudedocs is wrong for state`) suggests deleting `claudedocs/` entirely once everything is moved. Recommendation: **keep `claudedocs/` for genuinely temporary scratch only** (single-session research that won't outlive the session), enforce via a stale-file sweep that warns on files >7 days old.

### 5.4 Migration sequence

1. **PR-1 (~80 LOC):** Create `.vnx-data/strategy/` + write `record_decision()` + `record_session()` helpers in `scripts/lib/strategy_log.py`. Add `roadmap.yaml.example` template.
2. **PR-2 (~120 LOC):** Bootstrap files for vnx-dev: hand-author `roadmap.yaml` from `VNX_feature_breakdown.md` + recent merged PRs; one-shot import existing decisions from HANDOVER files.
3. **PR-3 (~100 LOC):** SessionEnd hook + projection function (`build_current_state.py`).
4. **PR-4 (~80 LOC):** Extend `build_t0_state.py` to read `strategy/` and emit `t0_detail/strategic_state.json`.
5. **PR-5 (~60 LOC):** Move PRDs/research to `docs/`; populate `prd_index.json`. Delete `STATE.md`. Archive HANDOVER files.
6. **PR-6 (~150 LOC, deferred to migration Phase 4):** SQLite tables `strategic_decisions`/`roadmap_items`/`plan_state` + projector script that syncs file → DB on every change.
7. **PR-7 (~50 LOC, deferred to migration Phase 3):** Path-rewrite for centralized layout (`~/.vnx-data/strategy/<project_id>/`).

PRs 1-5 are independently shippable today, give the operator immediate value, and require no central-VNX work. PRs 6-7 ride the migration plan.

---

## 6. LOC estimate

| Component | LOC | Notes |
|---|---|---|
| `scripts/lib/strategy_log.py` (record_decision, record_session, append helpers) | ~120 | Append-only writers with file locking; reuse pattern from `dispatch_register.py`. |
| `scripts/build_current_state.py` (projection function) | ~180 | Reads roadmap.yaml + decisions.ndjson + t0_state.json + open_items_digest.json; emits markdown. |
| Extension to `scripts/build_t0_state.py` (`_build_strategic_state`) | ~50 | One new builder + one new entry in detail map. |
| SessionEnd hook script + `.claude/hooks/session_end.sh` | ~40 | Calls `build_current_state.py` + appends `session_log.ndjson`. |
| Bootstrap roadmap.yaml + import_handovers.py one-shot | ~100 | One-time import; deletable after run. |
| Schema migration (PR-6, optional / migration Phase 4) | ~120 | 3 CREATE TABLEs + projector loop + tests. |
| Reader helpers (`load_strategy()`, `query_decisions(project_id, scope)`) | ~80 | For T0 + future cross-project queries. |
| Tests (parsing, projection, idempotency, locking) | ~250 | ~70% coverage target. |
| Docs (`docs/architecture/STRATEGIC_STATE.md`) | ~150 | Reference doc explaining the model. |
| Path-rewrite for centralized future (PR-7) | ~60 | Single function; uses existing `vnx_project_id.py` helper. |
| **Total today (PRs 1-5):** | **~740 LOC** | |
| **Total inc. centralized + DB (PRs 1-7):** | **~1,150 LOC** | |

For comparison: migration plan estimates 1,950-2,400 LOC for the full single-VNX consolidation. Strategic state is ~30-40% of that scope.

---

## 7. Open questions for the operator (top 5)

1. **Git-track `strategy/` or not?** The folder lives under `.vnx-data/` (gitignored). Operator could opt to add a `.gitignore` exception for `strategy/` to commit `roadmap.yaml`/`current_state.md`/`decisions.ndjson` to the repo — gives audit trail in git, code-reviewable plan changes, but pollutes diffs. **Recommendation: yes, git-track.** Strategic state benefits from git history; runtime state does not.
2. **Markdown projection or YAML projection?** `current_state.md` is human-friendly but harder to consume programmatically. Alternative: emit `current_state.yaml` and a thin renderer for humans. **Recommendation: markdown.** The primary consumer is T0-after-/clear (a human + a model that reads markdown natively); YAML adds an extra parse step for zero gain.
3. **Decision-id format — global or per-project?** `OD-2026-05-01-001` is global by date. Centralized future could use `OD-vnx-dev-2026-05-01-001`. **Recommendation: ask the operator.** Global is simpler today but ambiguous when 4 projects all log decisions on the same day; per-project is clearer but longer.
4. **PRD/ADR location: `docs/prds/` (in repo) or `strategy/prds/` (in .vnx-data)?** Repo-tracked is reviewable; `.vnx-data/` is per-deployment. **Recommendation: `docs/prds/` in repo**, with `strategy/prd_index.json` as the catalog. Matches industry pattern (Cursor, Aider, Claude Code all put strategic docs in repo).
5. **One unified file or split files?** Could collapse `roadmap.yaml` + `decisions.ndjson` + `open_plans.json` into one big `strategy.yaml`. Simpler to read, harder to append-only-write. **Recommendation: split.** Append-only event log (`decisions.ndjson`) is the source-of-truth pattern; everything else is a projection. Collapsing breaks that contract.

---

## Sources

- [LangChain — LangGraph Memory docs](https://docs.langchain.com/oss/python/langgraph/add-memory)
- [CrewAI — Memory concepts](https://docs.crewai.com/en/concepts/memory)
- [CrewAI — Cognitive memory architecture blog](https://crewai.com/blog/how-we-built-cognitive-memory-for-agentic-systems)
- [Aider — YAML config docs](https://aider.chat/docs/config/aider_conf.html)
- [Cline — Task history recovery docs](https://docs.cline.bot/troubleshooting/task-history-recovery)
- [Cursor — Rules docs](https://docs.cursor.com/context/rules)
- [AutoGen v0.4 — Managing State tutorial](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/state.html)
- [OpenAI Agents SDK — Sessions overview](https://openai.github.io/openai-agents-python/sessions/)
- [OpenAI Agents SDK — Advanced SQLite sessions](https://openai.github.io/openai-agents-python/sessions/advanced_sqlite_session/)
- [Claude Code — Memory docs](https://code.claude.com/docs/en/memory)
- [Milvus blog — Claude Code local storage deep dive](https://milvus.io/blog/why-claude-code-feels-so-stable-a-developers-deep-dive-into-its-local-storage-design.md)
- Companion: `claudedocs/2026-04-30-single-vnx-migration-plan.md`
- Companion: `claudedocs/2026-04-30-state-memory-audit.md`

---

## Layer 2 — Learning + Memory Retrieval

**Date:** 2026-05-01 (extension)
**Author:** T0 research synthesis
**Status:** Design proposal. No code modified.
**Companion docs:** `claudedocs/2026-04-30-self-learning-loop-audit.md`, `claudedocs/2026-04-30-intelligence-system-audit.md`, `claudedocs/2026-04-30-state-memory-audit.md`, `claudedocs/2026-04-30-adaptive-receipt-classifier-research.md`, `claudedocs/PRD-VNX-UH-001-...md`.

Layer 1 (Sections 1-7 above) defined **strategic state** — the project's plan, decisions, and current snapshot. Layer 2 defines **learning state** — what we have learned from past artifacts and outcomes, and how that knowledge is surfaced at decision time. Layer 1 answers "where are we and what did we decide?" Layer 2 answers "have we tried this before, what worked, what didn't, what does the operator dislike, and what past artifact is most similar to the one I'm about to produce?"

### L2.1 The gap

VNX already has a substantial code-domain learning loop (full inventory in `2026-04-30-self-learning-loop-audit.md` and `2026-04-30-intelligence-system-audit.md`):

- `quality_intelligence.db` holds **187 success_patterns**, **750 antipatterns**, **28 prevention_rules**, **606 pattern_usage rows**, **1894 confidence_events**, **1069 dispatch_experiments** (F57 Karpathy loop), **71 164 code_snippets** in an FTS5 virtual table.
- `intelligence_selector.py` (FP-C contract, max 3 items / 2000 chars) injects proven_patterns + failure_preventions + recent_comparables into every dispatch prompt, gated by per-class confidence thresholds.
- `receipt_classifier.py` (ARC-3) provider-abstracts haiku/ollama/codex/gemini classifiers and writes structured outcomes back into `pattern_usage` + `confidence_events`.
- `f57_insights_reader.py` + `recommendation_aggregator.py` (PR #347) close the dispatch-parameter feedback loop.
- `project_scope.py` already provides `current_project_id()` + `scoped_query()` for multi-tenant filtering — Layer 2 does not need to invent project-scoping; it inherits it.

What is missing — the actual operator question:

1. **Semantic retrieval.** All retrieval today is keyword-based (FTS5 over `code_snippets`) or tag-based (`tag_combinations` join). There is no embedding store, no nearest-neighbour query. "Find me past dispatches similar in *meaning* to this one" is not implementable.
2. **Content artifacts.** Patterns and antipatterns are **about code work**. There is no schema, store, or retrieval path for marketing, sales, or ops artifacts. A blog draft, a sales email, a campaign brief have no home in `quality_intelligence.db` and no reason to be there — they aren't dispatch outcomes, they're produced content.
3. **Domain partitioning.** `pattern_type`, `category`, and `scope_tags` exist but every value in production is a code-domain value (`approach`, `architecture`, `crawler`, `storage`, `extraction`). Adding marketing entries to the same tables would silently mix them into the proven_pattern injection slot for code dispatches.
4. **Operator-level cross-domain preferences.** "Operator dislikes phrase Z" or "operator wants concise tone" should apply to every domain. Today these live as flat `feedback_*.md` files in Claude Code's auto-memory, not as queryable structured records.
5. **Retrieval at the right time.** Today injection happens at dispatch-create. The operator wants retrieval to happen *earlier* — when planning a new mission ("write a blog about X"), the system should surface "we wrote about X on date Y, the angle was Z, performance was W" *before* the dispatch is even drafted.

### L2.2 Framework benchmark (storage / embedding / domain handling / cost / local-first fit)

| Framework | Storage backend | Embedding default | Domain partitioning | Cost (self-hosted) | Local-first fit |
|---|---|---|---|---|---|
| **Mem0** ([docs](https://docs.mem0.ai/open-source/overview)) | 19 vector stores supported (Chroma, Qdrant, FAISS, sqlite-vec, Cassandra, Valkey, Neptune, Kuzu) | OpenAI text-embedding-3-small default; FastEmbed for local | Implicit via metadata filters (`user_id`, `agent_id`, `run_id`); no native hierarchy | Free OSS; Pro $19/mo for 50K memories, Graph $249/mo | Good — OSS install, Kuzu embedded graph, FastEmbed local. Ollama-friendly. |
| **Letta / MemGPT** ([docs](https://docs.letta.com/concepts/memgpt/)) | Postgres (default) or SQLite; Context Repositories with git-versioning (2026) | Configurable; OpenAI default | Three tiers (Core / Recall / Archival), agent-scoped; Context Repos git-branch per project | Self-hostable; LLM cost dominates | Medium — server-process oriented, fights local-first doctrine. |
| **CrewAI Memory** ([docs](https://docs.crewai.com/en/concepts/memory)) | SQLite + vector index (default Chroma) | Configurable | **Hierarchical scope tree** (`/project/alpha/agent/researcher`) — explicit native partition | Free OSS | Good — SQLite-first, no daemon. |
| **Zep / Graphiti** ([arxiv](https://arxiv.org/abs/2501.13956)) | Neo4j (graph) + Postgres | OpenAI default | Group_id filtering; temporal validity windows on every edge | Self-hosted requires Neo4j; cloud Pro tier | Poor — needs Neo4j daemon; ADR-001 violation. |
| **LangMem (LangChain)** ([blog](https://blog.langchain.com/langmem-sdk-launch/)) | Pluggable Store (Postgres / Redis / in-memory) | Configurable | Namespace tuples (`(user_id, "memories")`) | Free SDK | Medium — Store interface is the abstraction; needs a backend choice. |
| **LlamaIndex KG-RAG** ([docs](https://developers.llamaindex.ai/python/examples/query_engine/knowledge_graph_rag_query_engine/)) | Property graph + vector hybrid; pluggable | Configurable; HF embeddings local-friendly | Per-index isolation | Free OSS | Good — supports local embeddings + local LLMs. |
| **Cline (task history)** ([docs](https://docs.cline.bot/troubleshooting/task-history-recovery)) | Per-task JSON files + workspace_hash dir + shadow-git checkpoints | None — keyword search only | Workspace_hash (per-folder) | Free | Excellent for the task-history pattern; not a semantic store. |
| **Claude Code memory** ([docs](https://code.claude.com/docs/en/memory)) | JSONL sessions + Markdown CLAUDE.md/MEMORY.md | None — file-grep | Encoded-cwd directory key | Free | Excellent — already in use; is the *operator-preference* layer. |

**Convergence patterns.** (1) Two-layer separation — **episodic** (specific past events) vs **semantic** (extracted facts) — is the dominant 2026 vocabulary (LangMem, Zep, Letta all use it). (2) Vector embeddings are the default retrieval primitive; FTS is a complement, not a replacement. (3) Hierarchical scope paths (CrewAI `/project/x/agent/y`) win over implicit metadata-filter scoping (Mem0) for clarity but lose on flexibility. (4) Temporal validity windows (Zep, Letta Context Repos) are how the field handles "this fact was true then, isn't now" — superior to confidence-decay for content artifacts. (5) Local-first vector search has converged on **sqlite-vec** as the embedded option; Chroma as the standalone option; everything else needs a server.

### L2.3 Proposed VNX Layer-2 design

**Headline choice: hybrid file + sqlite-vec, single embedding model (`nomic-embed-text` via Ollama), hard domain partition with cross-domain promotion path.** Reuses the existing `quality_intelligence.db` host so we get one DB to back-up, one place to query, one process model.

#### L2.3.1 Storage model

Three tiers, each with a clear owner:

1. **Raw artifact store** — file-system, append-only, never mutated.
   - Path: `.vnx-data/memory/<domain>/<YYYY>/<YYYY-MM-DD>/<slug>.md` (with sibling `.meta.json`).
   - Contents: the actual blog markdown, sales email body, campaign brief, mission charter, ADR draft, codex-gate report.
   - Domains (initial): `code`, `marketing`, `sales`, `ops`, `research`, `operator`. Extensible — operator can add a new top-level dir and the indexer picks it up.
   - Centralized future: `~/.vnx-data/memory/<project_id>/<domain>/...`. Same shape, prefixed by `project_id`.

2. **Embedding + index layer** — `quality_intelligence.db` extended with `sqlite-vec` virtual tables.
   - One `vec0` table per domain (`vec_artifacts_code`, `vec_artifacts_marketing`, ...). Per-domain tables enforce the hard partition at query time — the wrong-domain rows are not even reachable from a code-domain selector.
   - Plus one shared `vec_operator_prefs` table for cross-domain operator-level signals.
   - Embedding dim: 768 (nomic-embed-text v1). Storage: ~3 KB/vec; 10K artifacts ≈ 30 MB. Comfortably inside SQLite's working-set sweet spot.

3. **Structured intelligence layer** — extends `quality_intelligence.db`'s existing tables with a `domain` column (default `'code'` for backfill compatibility) and one new `artifact_index` table joining the file path to its domain, vector rowid, tags, lifecycle state.

#### L2.3.2 Embedding model — recommendation

**`nomic-embed-text` via Ollama.** Reasoning:

- **Local-first compliant** (ADR-001) — runs as `ollama pull nomic-embed-text` then `ollama run`. No external API, no network egress.
- **Quality** — outperforms `text-embedding-3-small` and Ada-002 on MTEB short-context retrieval ([Nomic announcement](https://www.nomic.ai/news/nomic-embed-text-v1)); 8192-token context handles whole blog drafts in one shot.
- **Cost** — free. Embedding 1000 artifacts ≈ 3 minutes on M-series Mac, ~$0.
- **Footprint** — 0.55 GB model on disk; ~600 MB RAM at inference. Acceptable.
- **Failover** — If Ollama is unreachable, fall back to OpenAI `text-embedding-3-small` ($0.02/1M tokens; 1000 artifacts ≈ $0.02). Failover is opt-in via env var; default is local-only.

Rejected alternatives: `bge-small-en-v1.5` (sentence-transformers; equivalent quality but Python-process model, slower cold-start, harder to hot-reload than an Ollama daemon already running for other VNX uses). `text-embedding-3-small` as default (would be cheapest if API egress were free, but ADR-001 forbids).

#### L2.3.3 Indexing pipeline — where it hooks

Two write triggers, no daemon:

- **On receipt write** (extends `append_receipt.py:1112` post-hook, the same point ARC-3 uses today). When a worker writes a unified report, the post-hook (1) classifies the receipt (existing), (2) embeds the report body if it lives under a tracked artifact path, and (3) inserts/updates the `artifact_index` row. Cost-bounded by the same `VNX_RECEIPT_CLASSIFIER_DAILY_COST_USD` budget; nomic-via-Ollama spends $0 against that budget.
- **On manual artifact write** (operator drops a blog draft into `.vnx-data/memory/marketing/...`). A SessionStart sweep + an optional file-watcher (`fswatch`/`watchdog`, opt-in) finds new files and embeds them. The sweep is idempotent — existing rows skip.

No new long-running process. No new daemon. The pipeline rides existing hooks (matches Layer-1's "no daemon for strategic state" stance).

#### L2.3.4 Retrieval API

Three surfaces, in increasing automation:

1. **CLI** — `vnx memory search <query> [--domain marketing] [--top-k 5] [--since 2026-01-01]`. Returns ranked artifact paths + 200-char excerpts + last-used / confidence metadata. Wraps `bin/vnx` already at line 1868 pattern (sibling of `vnx suggest`).
2. **Programmatic** — `from vnx_memory import search; results = search(query=..., domain="marketing", top_k=5, project_id=None)`. Returns `list[ArtifactHit]` with `path`, `score`, `domain`, `tags`, `last_seen`, `lifecycle_state`. Used by `intelligence_selector.py` to extend its current pattern selection with semantic neighbours when no high-confidence proven_pattern exists.
3. **Auto-injection at planning time** — extends Layer-1's strategy-log writer. When the operator (or T0) drafts a new dispatch or mission, the planner runs `search()` over the dispatch instruction, prepends the top-k semantic neighbours to the dispatch context as "Related past work" (clearly labelled as semantic neighbours, not as proven patterns — different evidence class). Bounded by the existing FP-C 2000-char ceiling.

#### L2.3.5 Domain model — hard partition with cross-domain promotion

Hard partition is the default — code patterns never leak into marketing context and vice-versa. Implementation: per-domain `vec0` tables means a code-context selector cannot retrieve a marketing artifact even if it tries. This is the safer default for the operator's "tech antipattern shouldn't pollute marketing" requirement.

Cross-domain promotion is opt-in via two paths:
- **Operator-level signals** live in `vec_operator_prefs` (the shared table). Every domain's selector queries operator-prefs in addition to its own domain. Use case: "operator dislikes verbose tone."
- **Explicit cross-domain link** via `cross_domain_links` table (see schema below). When an artifact is genuinely useful across domains (rare; operator-flagged), a row in this table makes it discoverable from the linked domain's selector.

This mirrors CrewAI's hierarchical scope (operator-prefs ≈ root scope, per-domain ≈ branches) without requiring a tree-walker; sqlite UNIONs cover the same query shape with one more JOIN.

#### L2.3.6 Lifetime / decay

Three decay regimes, chosen per artifact at index time:

- **Pinned** — operator marks an artifact as "always remember" (e.g., the project mission). `lifecycle_state = 'pinned'`, never decays, never expires.
- **Confidence-decayed** — same regime as today's `success_patterns` / `antipatterns`. Each retrieval bumps `confidence`; non-retrieval over a window decays it. Drops below threshold → not retrieved by default but still in the store. Use case: dispatch-outcome-derived patterns.
- **TTL** — explicit `valid_until` timestamp. Hard expires from default queries after that date but still queryable with `--include-expired`. Use case: marketing campaigns ("Q2 2026 campaign assets — expire 2026-09-30").

Default per domain: `code` → confidence-decayed; `marketing` → TTL 90 days; `sales` → TTL 180 days; `ops` → confidence-decayed; `research` / `operator` → pinned-by-default.

#### L2.3.7 Multi-project + centralized VNX scoping

`project_scope.py` already provides `current_project_id()` and `scoped_query()`. Layer 2 inherits this:

- Every new table gets a `project_id TEXT NOT NULL DEFAULT 'vnx-dev'` column on creation, matching migration plan §6 Phase 0.
- Per-domain `vec0` virtual tables get a paired metadata table that holds `project_id`; queries JOIN through it.
- Centralized future: `~/.vnx-data/memory/<project_id>/<domain>/...`. The path-rewrite is the same single function the migration plan already specifies.

Operator-level prefs (`vec_operator_prefs`) intentionally have **no `project_id` filter** — operator preferences are global. This is the one allowed cross-project read.

### L2.4 Concrete schema additions to `quality_intelligence.db`

Five new tables. All take `project_id` per Phase 0 migration plan.

```sql
-- 1. The catalogue: one row per indexed artifact.
CREATE TABLE artifact_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    domain TEXT NOT NULL,                      -- code|marketing|sales|ops|research|operator
    artifact_path TEXT NOT NULL,               -- relative to .vnx-data/memory/
    artifact_hash TEXT NOT NULL,               -- SHA1(content); detects re-edits
    title TEXT,                                -- extracted from first H1 or front-matter
    summary TEXT,                              -- 200-char generated summary
    tags TEXT,                                 -- JSON array
    vec_rowid INTEGER,                         -- FK into the per-domain vec0 table
    lifecycle_state TEXT NOT NULL DEFAULT 'active',  -- pinned|active|decayed|expired|tombstoned
    confidence REAL DEFAULT 1.0,
    valid_from DATETIME DEFAULT CURRENT_TIMESTAMP,
    valid_until DATETIME,
    source_dispatch_id TEXT,                   -- if produced by a VNX dispatch
    source_decision_id TEXT,                   -- if linked to a Layer-1 OD-* decision
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_retrieved DATETIME,
    retrieval_count INTEGER DEFAULT 0,
    UNIQUE(project_id, domain, artifact_path)
);
CREATE INDEX idx_artifact_domain ON artifact_index (project_id, domain, lifecycle_state);
CREATE INDEX idx_artifact_hash ON artifact_index (artifact_hash);

-- 2. Per-domain vector tables (one each, created at schema apply time).
CREATE VIRTUAL TABLE vec_artifacts_code        USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE vec_artifacts_marketing   USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE vec_artifacts_sales       USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE vec_artifacts_ops         USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE vec_artifacts_research    USING vec0(embedding float[768]);
CREATE VIRTUAL TABLE vec_operator_prefs        USING vec0(embedding float[768]);

-- 3. Embedding cache — avoid re-embedding identical content.
CREATE TABLE embedding_cache (
    content_hash TEXT PRIMARY KEY,             -- SHA256 of content + model_id
    model_id TEXT NOT NULL,                    -- 'nomic-embed-text-v1' etc.
    embedding BLOB NOT NULL,                   -- raw float32 vector
    dim INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 4. Domain taxonomy — defines what domains exist + their decay regime.
CREATE TABLE domain_taxonomy (
    domain TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    default_lifecycle_state TEXT NOT NULL,     -- pinned|active|TTL-N
    default_ttl_days INTEGER,                  -- NULL if not TTL
    cross_domain_visible BOOLEAN DEFAULT 0,    -- 0 = hard partition default
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 5. Cross-domain bridge — opt-in, operator-flagged.
CREATE TABLE cross_domain_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    source_artifact_id INTEGER NOT NULL,       -- FK into artifact_index
    target_domain TEXT NOT NULL,
    rationale TEXT,                            -- why this cross-link exists
    created_by TEXT,                           -- operator|t0|auto
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_artifact_id) REFERENCES artifact_index(id)
);
CREATE INDEX idx_cross_target ON cross_domain_links (project_id, target_domain);
```

Plus an additive column on existing tables:

```sql
ALTER TABLE success_patterns ADD COLUMN domain TEXT NOT NULL DEFAULT 'code';
ALTER TABLE antipatterns     ADD COLUMN domain TEXT NOT NULL DEFAULT 'code';
ALTER TABLE prevention_rules ADD COLUMN domain TEXT NOT NULL DEFAULT 'code';
```

Backfill is trivial — every existing row is, by definition, code-domain.

### L2.5 Integration with PRD-VNX-UH-001 (multi-orchestrator architecture)

PRD-VNX-UH-001 §FR-7 (multi-tier orchestrator hierarchy) and §FR-12 (agent registry + dispatch routing) imply each future sub-orchestrator (`marketing-T0`, `sales-T0`, `tech-T0`) needs its own learning context but should share an operator-level layer. Layer-2's hard-partition-by-domain-with-shared-operator-prefs maps directly:

- `marketing-T0` runs with `VNX_PROJECT_ID=marketing-prod` and queries against `vec_artifacts_marketing` + `vec_operator_prefs`. It cannot accidentally retrieve a `tech` proven-pattern.
- `tech-T0` (the existing T0) runs with `VNX_PROJECT_ID=vnx-dev` (default) and queries `vec_artifacts_code` + `vec_operator_prefs`. Unchanged from today.
- The operator's "I dislike verbose tone" preference written once in `vec_operator_prefs` is visible to both.
- A genuinely cross-domain pattern (e.g., "we standardize on EU date format") gets a `cross_domain_links` row from `operator → marketing` and `operator → tech`.

PRD-VNX-UH-001 §FR-4 (folder-based agents) is a clean fit — each `.vnx/agents/<role>/` folder can carry an optional `memory_scope.yaml` declaring which domains it reads from and whether it can write to operator-level. Default-deny.

### L2.6 Migration from current `feedback_*.md` operator memory

Today's flat-file operator memory (Claude Code `~/.claude/projects/<slug>/memory/MEMORY.md` + `feedback_*.md`) is essentially a key-value store: each file is a lesson, the filename is the key, the content is the value. ~25 entries today. Recommendation:

**Keep Claude Code's auto-memory as-is** for the operator-edited surface (Claude Code already reads it natively at session start; reinventing this is pure cost). Add a **one-way sync** from `feedback_*.md` files into `vec_operator_prefs`:

- A `scripts/import_operator_memory.py` script (one-shot, then nightly) walks the auto-memory dir, embeds each `feedback_*.md` body, inserts into `vec_operator_prefs` with `lifecycle_state='pinned'` and `source='claude_auto_memory'`.
- This makes operator preferences semantically retrievable by every domain selector without forcing the operator to change their writing workflow.
- Dispatching agents that don't run inside Claude Code (e.g., a future `marketing-worker` headless subprocess) can still see operator preferences via the DB.
- New operator preferences captured *during* dispatch (e.g., T0 logs "operator just said 'never use the word leverage'") write to `vec_operator_prefs` directly; a sibling `feedback_*.md` is also written so Claude Code sees it too.

The auto-memory layer is the human-readable canon; `vec_operator_prefs` is the machine-queryable mirror. They never diverge by design — the script is one-way (auto-memory → DB) and the DB row carries `source_path` so the operator can always trace back.

### L2.7 LOC + dependency estimate

| Component | LOC | Notes |
|---|---|---|
| `scripts/lib/vnx_memory.py` (search, index, embed, lifecycle) | ~280 | Public API used by CLI + selector + planner. |
| `scripts/lib/embedding_provider.py` (Ollama + OpenAI failover) | ~120 | Mirrors `classifier_providers/` pattern. |
| `scripts/index_artifacts.py` (sweep + idempotent indexer) | ~150 | Run by SessionStart hook + manual CLI. |
| Schema migration `0017_layer2_memory.sql` | ~100 | 5 new tables + 3 ALTERs + sqlite-vec load. |
| `scripts/import_operator_memory.py` (one-shot + nightly) | ~80 | Walks `~/.claude/projects/.../memory/`, embeds, writes. |
| `bin/vnx` extension — `memory` subcommand | ~60 | search / show / pin / forget / reindex. |
| Hook into `append_receipt.py:1112` post-hook | ~40 | One additional call into `vnx_memory.index_if_artifact()`. |
| Hook into `intelligence_selector.py` (semantic-neighbour fallback) | ~50 | Augments existing FP-C selection when no high-confidence pattern exists. |
| Tests | ~280 | Round-trip embed / search / decay / partition isolation / cross-domain promotion. |
| Reference doc `docs/architecture/MEMORY_LAYER.md` | ~180 | Architecture + operator howto. |
| **Total** | **~1340 LOC** | |

**New dependencies:**

- `sqlite-vec` (C extension; `pip install sqlite-vec` ships pre-built wheels for macOS/Linux). ~500 KB install. No daemon.
- `ollama` already runs on the operator's machine (used by classifier providers); add `ollama pull nomic-embed-text` to install script (550 MB one-time).
- Optional: `openai` Python SDK if failover path enabled (not on critical path).

No new long-running process. No Redis (ADR-001 honored). No external API on the default path.

### L2.8 Top 3 open questions for the operator

1. **Hard partition vs soft partition on day one?** The proposal defaults to **hard** (per-domain `vec0` tables; cross-domain only via explicit `cross_domain_links`). Simpler, safer, but adds friction when the operator legitimately wants to cross-pollinate. Soft alternative: one shared `vec_artifacts` table with a `domain` column + `WHERE domain IN (...)` filter — easier promotion, easier accident. **Recommendation: hard.** Once an antipattern leaks across domains, trust in the system collapses. Cross-domain promotion is rare; explicit-and-flagged is the right cost.
2. **Embedding model — Ollama-only, or build the OpenAI failover from day one?** Ollama-only is simpler and free; failover adds resilience but also adds the ADR-001 footnote ("we permit this single egress for embedding cache misses"). **Recommendation: Ollama-only on day one**, ship failover as a follow-up PR if and when the operator hits a real outage. Ship the code path but keep it env-gated and disabled by default.
3. **Auto-injection at planning time — opt-in or default-on?** The semantic-neighbour injection at dispatch-create extends `intelligence_selector.py` and changes prompts every dispatch will see. Default-on means immediate value; opt-in (env var) means safer rollout. **Recommendation: opt-in for the first 2 weeks**, then flip default-on after the operator confirms the surfaced neighbours are signal not noise. Mirrors how the F32 subprocess adapter rolled out.

### L2.9 Sources (Layer 2)

- [Mem0 — Open Source Overview](https://docs.mem0.ai/open-source/overview)
- [Mem0 — State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [Letta — MemGPT concepts](https://docs.letta.com/concepts/memgpt/)
- [Letta — Memory Blocks blog](https://www.letta.com/blog/memory-blocks)
- [CrewAI — Memory concepts (hierarchical scope)](https://docs.crewai.com/en/concepts/memory)
- [Zep — Temporal Knowledge Graph (arxiv 2501.13956)](https://arxiv.org/abs/2501.13956)
- [LangChain — LangMem SDK launch](https://blog.langchain.com/langmem-sdk-launch/)
- [LangChain — LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)
- [LlamaIndex — Knowledge Graph RAG Query Engine](https://developers.llamaindex.ai/python/examples/query_engine/knowledge_graph_rag_query_engine/)
- [sqlite-vec — GitHub repository](https://github.com/asg017/sqlite-vec)
- [Nomic Embed v1 — announcement](https://www.nomic.ai/news/nomic-embed-text-v1)
- [BentoML — Best Open-Source Embedding Models 2026](https://www.bentoml.com/blog/a-guide-to-open-source-embedding-models)
- Companion: `claudedocs/2026-04-30-self-learning-loop-audit.md`
- Companion: `claudedocs/2026-04-30-intelligence-system-audit.md`
- Companion: `claudedocs/2026-04-30-state-memory-audit.md`
- Companion: `claudedocs/2026-04-30-adaptive-receipt-classifier-research.md`
- Companion: `claudedocs/PRD-VNX-UH-001-universal-headless-orchestration-harness.md`
