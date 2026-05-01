# PRD — VNX Universal Headless Orchestration Harness

**Document ID:** PRD-VNX-UH-001
**Version:** 1.1 (Draft)
**Date:** 2026-05-01
**Author:** T0 (Opus 4.7, 1M)
**Owner:** Vincent van Deth (operator)
**Status:** Draft — pending operator approval of open decisions

**Changelog:**
- v1.0 — initial draft synthesizing multi-orchestrator + universal-harness research
- v1.1 — adds §7.6 architecture mode matrix; adds §7.7 + FR-11 provider-failover at orchestrator level; updates FR-7 to note provider-interchangeability; updates OD-1 scope; adds R11 (cross-provider state continuity)

**Companion research:**
- `claudedocs/2026-05-01-multi-orchestrator-research.md` — multi-orchestrator hierarchy + session continuity
- `claudedocs/2026-05-01-universal-harness-research.md` — universal harness + observability + folder agents + capability tokens + workers=N
- `docs/governance/decisions/ADR-001-no-external-redis.md` — no Redis dependency
- `docs/governance/decisions/ADR-002-f43-context-rotation-packaging.md` — F43 revival + standalone PyPI package

---

## 1. Context & Problem Statement

### 1.1 Why now

VNX is at an inflection point. The 2026-05-01 sprint merged 30 PRs that consolidated the codebase (refactor of `subprocess_dispatch`, `append_receipt`, `receipt_processor_v4`, `gate_request_handler`, `dispatch_register`), shipped multi-tenant `project_id` Phase 0, closed cross-project contamination, and landed gate-receipt commit_sha + dispatch_id propagation. That work removed the technical debt that would have made the next architectural step impossible.

The next step is no longer "make VNX work better." It is **"make VNX a universal headless orchestration framework"** — Claude-agnostic, multi-provider, multi-orchestrator, folder-driven, with cryptographically-enforced approval inheritance and provider-parity observability.

### 1.2 What hurts today

1. **Observability is a half-truth.** VNX claims "fully observable" but that is true only for Claude workers. Codex workers' rich `--json` event stream (`thread.started`, `turn.started`, `item.*`, `turn.completed`) is buffered to completion in `codex_adapter.py:120-122` and dropped on the floor; `EventStore.append` is never called for non-Claude paths. Gemini-CLI v0.11+ now emits stream-json but VNX still uses `--output-format json` (single final blob). This is a concrete governance gap, not a theoretical one.

2. **Skills are injected, not folder-based.** `subprocess_dispatch_internals/skill_injection.py` concatenates skill prompt + dispatch instruction and pipes them to `claude -p` stdin. There is no per-agent folder with its own `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`, no per-agent `permissions.yaml`, no per-agent `hooks/`. Operator wants each agent to be a self-contained folder, mirroring Claude Code's native subagent pattern.

3. **Workers are hardcoded T0..T3.** Across 25+ files including `runtime_facade.py:49 (CANONICAL_TERMINALS)`, `decision_executor.py:31-33`, `vnx_doctor_checks.py:168,492`. Adding a fifth worker requires touching all of them. The operator wants `workers = N` driven by configuration, not by source-code constants.

4. **There is no multi-orchestrator hierarchy.** Today: one T0 → 3 workers. Operator's vision: top "Assistant" orchestrator → multiple sub-orchestrators (Tech Lead, Marketing Manager) → workers under each. Both headless and interactive sub-orchestrators must be supported. There is no current primitive for "delegate to a named sub-orchestrator." The Task-tool subagent pattern doesn't fit because Claude Code's subagents are "one fork can't fork further" by design.

5. **No trust chain.** Currently any process with disk access to `.vnx-data/dispatches/pending/` can drop a fake dispatch. The operator wants approval inheritance — `operator → main orchestrator → sub-orchestrator → workers` — with cryptographic enforcement so a compromised worker cannot forge an upstream-approved dispatch.

6. **Single-provider lock-in.** Adding Kimi 2.6, OpenAI Codex (full streaming), local Ollama, or any future CLI/API agent means writing yet another bespoke adapter. Industry has converged on a `Provider` interface (LiteLLM, OpenCode, smolagents, CrewAI). VNX has not.

### 1.3 The desired outcome

A universal headless framework where:
- **Any LLM (CLI or API) can be a worker or orchestrator** behind a uniform `WorkerProvider` interface, with full per-event observability normalized to a canonical event schema.
- **Sub-orchestrators are first-class.** A top "Assistant" orchestrator can dispatch to "Tech Lead" or "Marketing Manager"; each sub-orchestrator has its own folder, its own governance variant (coding-strict vs business-light), its own worker pool, and its own approval token signed by its parent.
- **Each agent is a folder.** `.claude/agents/orchestrators/<name>/` and `.claude/agents/workers/<role>/` carry the agent's prompts (tri-file CLAUDE/AGENTS/GEMINI), permissions, governance, hooks. No more injection.
- **Workers = N.** Worker identity is `(orchestrator_id, worker_id, role)`, not `T1/T2/T3`. Pool size is dynamic per orchestrator.
- **Approval inherits cryptographically.** Operator's signing key is the trust root; macaroon-style capability tokens narrow at every hop; workers verify offline.
- **Interactive escape hatch stays.** Operator can spin up an ad-hoc interactive session for one-off tasks without going through the dispatch flow.

---

## 2. Vision (the stip op de horizon)

> VNX 2.0 is the **universal headless orchestration framework** for multi-LLM, multi-orchestrator workflows with strict governance variants, full observability, folder-driven agents, and cryptographic approval inheritance. It runs locally (no Redis, no etcd, no daemons), supports any provider that emits a streaming protocol, and is portable enough that the context-rotation core, the streaming-drainer core, and the capability-token core can each ship as standalone PyPI packages for community adoption.

The product spans three audiences:

1. **Operator-as-CTO** — uses VNX to coordinate multiple domain orchestrators (engineering, marketing, ops) on real work.
2. **Operator-as-developer** — wants `pip install vnx` and a CLI that just works on their laptop.
3. **Community** — pulls extracted modules (`headless-context-rotation`, `streaming-drainer`, `cap-token-macaroons`) as standalone deps in their own headless-LLM stacks.

---

## 3. Goals & Non-Goals

### 3.1 Goals (in priority order)

| # | Goal | Success looks like |
|---|------|-------------------|
| G1 | **Provider-parity observability** | Every provider's per-event stream lands in `EventStore` and `events/T{n}.ndjson` archive. No silent buffering. |
| G2 | **Folder-based agents** | Each agent is a self-contained folder; `.claude/skills/` injection becomes legacy fallback. |
| G3 | **Universal `WorkerProvider` interface** | Adding Kimi/Ollama/LiteLLM-bridge/local-llama is ~80-150 LOC each, not a from-scratch adapter. |
| G4 | **Multi-tier orchestrator hierarchy** | Top Assistant orchestrator → ≥1 named sub-orchestrators → workers, with full audit trail per level. |
| G5 | **Cryptographic approval inheritance** | Workers verify a chain-of-trust token offline; forgery and scope-escalation are impossible without operator's private key. |
| G6 | **Workers = N** | Worker count is configuration, not source code. Adding a 5th worker is a YAML edit. |
| G7 | **Governance variants** | `coding-strict` vs `business-light` declared per orchestrator folder; gate stack and approval policy follow automatically. |
| G8 | **Interactive escape hatch preserved** | Operator can spin up tmux+claude on demand for one-off tasks; doesn't need to be inside the dispatch flow. |
| G9 | **Local-first, zero-daemon** | No Redis, no etcd, no Consul. SQLite + files only. (Per ADR-001.) |
| G10 | **Backwards-compatible migration** | Existing T0..T3 dispatches keep working during the transition; no big-bang cutover. |

### 3.2 Non-Goals (explicitly out of scope for v1)

- **Forking sessions** (Claude Agent SDK's `fork`). Defer until two operators ask for it.
- **Cross-domain peer messaging** (Claude Code Agent Teams' `TeammateTool`). Strict tree first; revisit if workflow demands it.
- **Vertex AI streaming-via-REST** (`streamGenerateContent`). The CLI path is enough for v1.
- **Distributed multi-host orchestration.** Single-host only, per the local-first constraint.
- **GUI / web dashboard for orchestrator hierarchy.** CLI + existing token dashboard is enough; revisit later.
- **Auto-spawning sub-orchestrators based on task content.** Operator (or top orchestrator under operator instruction) dispatches sub-orchestrators explicitly.
- **Replacement of the existing receipt processor or NDJSON ledger.** Those are stable and additive-friendly.

---

## 4. Personas & Use Cases

### 4.1 Personas

| Persona | Role | Primary use |
|---------|------|-------------|
| **Operator (Vincent)** | Owner of trust root; signs operator key; defines orchestrator folders and governance variants. | Dispatches the main orchestrator; approves missions; reviews receipts. |
| **Main Orchestrator (Assistant)** | Top-level coordinator. Always Opus. Interactive (current T0) or headless. | Reads missions; decomposes into per-domain dispatches; signs sub-orchestrator capability tokens. |
| **Sub-Orchestrator (Tech Lead, Marketing Manager, ...)** | Domain coordinator. Opus default. | Receives dispatch from main; plans + dispatches workers in its pool; signs worker capability tokens. |
| **Worker (backend-developer, code-reviewer, copy-writer, ...)** | Executor. Sonnet/Haiku/Codex/Gemini/Kimi/local. | Receives dispatch; executes within capability-token scope; emits receipt. |
| **Community user** | Pulls extracted modules. | `pip install headless-context-rotation` etc. |

### 4.2 Headline use cases

- **UC-1 — Two-domain feature delivery.** Operator dispatches "ship feature F50 with launch announcement." Main → Tech Lead (plans + 4 code workers) + Marketing Manager (plans + 2 copy workers). Both run in parallel with separate audit trails. Operator reviews both.
- **UC-2 — Multi-provider review fan-out.** A PR is gated by Claude code-reviewer + Codex blocking gate + Gemini design review. Three providers, three live event streams, one consolidated receipt.
- **UC-3 — Local-only worker.** Operator wants a privacy-sensitive task done on local Ollama. Same dispatch flow; the worker is just `provider: ollama, model: llama-3.1-70b`.
- **UC-4 — Mission resume after context limit.** Tech Lead's planning session hits 65% context (F43). Auto-handover dispatch fires: a fresh Tech Lead session with summary + `resume_of: <prior_session_id>`. Receipt chain is unbroken.
- **UC-5 — Interactive one-off.** Operator wants to ask Claude one quick question without going through the dispatch flow. Spawns an interactive tmux session in `.claude/agents/workers/backend-developer/` cwd; no token, no receipt, just a session log.
- **UC-6 — Compromised worker is contained.** A worker subprocess is somehow hijacked. It tries to drop a fake "main approved this" dispatch. Verifier rejects: no signing chain to operator key. Threat surface: zero.

---

## 5. Functional Requirements

### FR-1 — Universal `WorkerProvider` interface

`scripts/lib/provider_adapter.py` is upgraded to declare a `WorkerProvider` Protocol with:

```python
class WorkerProvider(Protocol):
    def name(self) -> str: ...
    def capabilities(self) -> set[Capability]: ...   # CODE, REVIEW, DECISION, DIGEST, ORCHESTRATE
    def is_available(self) -> bool: ...
    def models(self) -> list[ModelInfo]: ...
    def spawn(self, worker_id: str, config: dict) -> SpawnResult: ...
    def stop(self, worker_id: str) -> StopResult: ...
    def deliver(self, worker_id, dispatch_id, instruction, *, model, cwd, capability_token, resume_session) -> DeliveryResult: ...
    def stream_events(self, worker_id, chunk_timeout, total_deadline) -> Iterator[StreamEvent]: ...   # MUST be live
    def session_id(self, worker_id) -> str | None: ...
    def token_usage(self, worker_id) -> TokenUsage | None: ...
```

All providers (Claude, Codex, Gemini-CLI, Ollama, future Kimi/LiteLLM bridge) implement this Protocol. `OrchestratorProvider` is the same Protocol with `ORCHESTRATE` capability declared.

### FR-2 — Streaming drainer parity

`scripts/lib/adapters/_streaming_drainer.py` (new, ~120 LOC) is a mixin that all subprocess-based adapters compose. It reads stdout line-by-line, normalizes via a per-adapter `event_normalizer`, calls `EventStore.append()` per event, and yields `StreamEvent` live (not post-hoc). Codex adapter and Gemini-CLI adapter migrate to use it. After migration, every provider's per-event stream lands in `events/T{n}.ndjson` and `events/archive/{worker_id}/{dispatch_id}.ndjson`.

### FR-3 — Canonical event schema with observability tier

```python
@dataclass
class CanonicalEvent:
    timestamp: str        # ISO-8601 UTC
    dispatch_id: str
    worker_id: str        # "<orchestrator_id>/<worker_id>"
    sequence: int
    type: Literal["start","thinking","tool_use","tool_result","text","token_count","completion","error"]
    provider: str
    raw: dict             # provider-native event; for forensics
    normalized: dict      # provider-independent fields
    observability_tier: Literal[1, 2, 3]
```

Tier 1 = full streaming (Claude, Codex, Gemini-new); Tier 2 = text-only streaming (Kimi, Ollama legacy); Tier 3 = final-result-only (batch APIs). Receipt records the tier; governance variant declares minimum tier required.

### FR-4 — Folder-based agents (single-source SKILL.md + provider symlinks)

#### 4.1 Layout

```
.claude/agents/
├── orchestrators/
│   ├── tech-lead/
│   │   ├── SKILL.md             ← single source of truth (operator edits this)
│   │   ├── CLAUDE.md → SKILL.md ← symlink (Claude CLI auto-loads)
│   │   ├── AGENTS.md → SKILL.md ← symlink (Codex CLI auto-loads)
│   │   ├── GEMINI.md → SKILL.md ← symlink (Gemini CLI auto-loads)
│   │   ├── permissions.yaml
│   │   ├── governance.yaml      ← variant: coding-strict OR business-light
│   │   ├── guardrails.yaml      ← model whitelist, max risk class, gate stack
│   │   ├── runtime.yaml         ← provider chain (FR-11)
│   │   ├── hooks/
│   │   └── workers.yaml         ← which worker pool this orch owns
│   └── marketing-lead/
│       └── ...
└── workers/
    ├── backend-developer/
    │   ├── SKILL.md
    │   ├── CLAUDE.md → SKILL.md
    │   ├── AGENTS.md → SKILL.md
    │   ├── GEMINI.md → SKILL.md
    │   ├── permissions.yaml
    │   ├── runtime.yaml
    │   └── tools.yaml
    └── ...
```

**Single source of truth: `SKILL.md`.** The three provider-named files (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) are symlinks pointing back at `SKILL.md`. Drift between providers is impossible because all three reference the *same inode* on disk. Operator edits one file; all providers see identical content.

#### 4.2 Why symlinks (not tri-file, not converter)

Three options were considered:

| Option | LOC | Drift risk | Provider-specific divergence supported |
|--------|-----|------------|----------------------------------------|
| Tri-file manual (3 separate `.md` files) | 0 | High — operator must sync | Yes |
| Single `SKILL.md` + auto-converter | ~300 | Low — generated on save | Yes (via `<provider:claude>...</provider>` tags) |
| **Single `SKILL.md` + symlinks** (chosen) | ~10 | Zero — same file | No (acceptable trade-off) |

Provider-specific divergence in the prompt content turns out to be **rare in practice**: VNX's permission profile (allowed-tools list) is already provider-specific via `permissions.yaml`, and tool-naming differences (e.g. Claude's `Edit` vs Codex's `update_file`) are handled by the **adapter layer**, not by the skill prompt. The skill prompt itself can be provider-neutral.

If divergence ever becomes necessary later, upgrade to the converter (Option B) is a one-time migration, not blocking work for v1.

#### 4.3 Loading semantics

| Mode | What VNX does |
|------|---------------|
| **Headless (default)** | VNX dispatcher reads `SKILL.md` from the agent folder, builds the prompt, pipes it to the provider CLI via `--instruction` or stdin. The provider CLI's auto-loading conventions are bypassed (we drive the prompt entirely). The 3 symlinks exist but are not read in this mode — they are insurance for the interactive escape hatch. |
| **Interactive (escape hatch)** | Operator runs `vnx interactive --agent <role>` which `cd`s into the folder and spawns `claude` (or `codex` / `gemini`) without `-p`. The provider CLI auto-loads its provider-specific filename — which resolves through the symlink to `SKILL.md`. Operator gets the same skill content the headless flow would have used. |

**This means the skill folder is genuinely unified across modes.** No separate paths, no special-casing, no "headless gets injection / interactive gets folder-load." Same file system → same content → same behavior.

#### 4.4 Boot-time symlink creation

A boot helper in `scripts/lib/agent_folder_loader.py` (~30 LOC of the W8 estimate) ensures the three provider-named symlinks exist for every agent folder containing a `SKILL.md`. Idempotent: if symlinks already exist and point to `SKILL.md`, no-op. If they point elsewhere or are stale, repair.

Operator can also commit the symlinks to git directly — git tracks symlinks as "links to a path." Either way, after first boot every folder is consistent.

#### 4.5 Migration path

- Existing `.claude/skills/<role>/CLAUDE.md` → rename to `.claude/agents/workers/<role>/SKILL.md`, create the three symlinks.
- Existing `_inject_skill_context()` in `subprocess_dispatch_internals/skill_injection.py:228-247` keeps working as legacy fallback behind `VNX_FOLDER_AGENTS=0` for backward-compat during migration.
- Once `VNX_FOLDER_AGENTS=1` is the default and all dispatches use it, injection path is removed in W14.

### FR-5 — Capability token trust chain

Macaroon-style ed25519-signed JSON tokens. Operator generates root key once via `python3 scripts/vnx_trust_init.py` → writes `~/.vnx/keys/operator_ed25519.priv` (chmod 600); public key registered in `.vnx-data/state/trust_anchors.json`.

Token shape:
```json
{
  "iss": "operator:vincentvd",
  "sub": "orchestrator:tech-lead",
  "iat": 1714560000,
  "exp": 1714563600,
  "scope": {
    "providers": ["claude", "codex"],
    "models": ["sonnet", "opus"],
    "max_risk_class": "medium",
    "allowed_tools": ["Read", "Edit", "Bash"],
    "max_dispatches": 50,
    "min_observability_tier": 1
  },
  "caveats": [],
  "delegation_chain": [
    {"signer": "operator:vincentvd", "sig": "..."},
    {"signer": "orchestrator:tech-lead", "sig": "..."}
  ]
}
```

Verifier (`scripts/lib/cap_token.py`, ~250 LOC + tests) checks signature chain → trust anchor, all caveats hold, replay-cache lookup. Sub-orchestrators can attenuate tokens (narrow scope, add caveats, sign with own key) but cannot expand.

### FR-6 — Governance variants

Each orchestrator folder's `governance.yaml` declares one of:

```yaml
variant: coding-strict    # OR business-light

coding-strict:
  required_gates: [codex, gemini, ci_green]
  auto_merge: false
  max_risk_class: medium
  pr_size_limit_lines: 300
  required_reviewers: 1
  blocking_findings_close_pr: true
  min_observability_tier: 1

business-light:
  required_gates: [content_review]
  auto_merge: true
  max_risk_class: low
  pr_size_limit_lines: null
  required_reviewers: 0
  blocking_findings_close_pr: false
  min_observability_tier: 2
```

`scripts/lib/gate_stack_resolver.py` (new, ~150 LOC) reads the variant per dispatch and resolves the gate stack accordingly.

### FR-7 — Multi-tier orchestrator hierarchy (provider-interchangeable)

**An orchestrator at any tier is provider-interchangeable.** Main, sub-orchestrators, and workers are not Claude-locked: each runs on whatever provider its folder declares as primary, with declared fallbacks for resilience (see FR-11). The default orchestrator stack is **Opus → Codex → Gemini** but the operator can override per orchestrator folder.

```
Operator
  ↓ (signs root token)
Main Orchestrator (Assistant)
  ↓ (signs sub-orch tokens, narrowed)
Sub-Orchestrator (Tech Lead, Marketing Lead, ...)
  ↓ (signs worker tokens, narrowed)
Workers
```

Concretely:
- `agent_kind` field on dispatches: `worker` (current) or `sub_orchestrator` (new). Drives skill selection and model default (Opus for sub-orchestrators).
- `parent_dispatch_id` field on dispatches and `dispatch_register.ndjson` rows. Renders dispatches as a forest in `t0_state.json`.
- `OrchestratorAdapter` class (~150 LOC) subclasses `SubprocessAdapter`: refuses to spawn unless `agent_kind == "sub_orchestrator"`, defaults to Opus, injects orchestrator skill (per-domain variant from folder).
- `.vnx-data/missions/<id>.json` describes a top-level mission; main orchestrator reads this the way T0 reads dispatches today. Mission states: `planning | active | review | done | aborted`. Owned by main orchestrator only.

### FR-8 — Session continuity (fresh by default, opt-in resume)

Two new dispatch fields:
- `agent_kind` (see FR-7).
- `resume_of: <prior_dispatch_id>` (optional).

When `resume_of` is set, dispatcher looks up the prior `session_id` from `_session_ids` map and appends `--resume <session_id>` to the `claude -p` invocation. Otherwise fresh session.

Default policy: **fresh per task.** Resume only when (a) dispatch carries `resume_of`, OR (b) parent orchestrator's instruction includes a "continue mission" marker. Operator's "continue this thread" UI flag sets the latter.

### FR-9 — Workers = N

Worker identity becomes `(orchestrator_id, worker_id, role)`:
- `orchestrator_id`: `"main"` or `"tech-lead"` or `"marketing-lead"`.
- `worker_id`: short stable name (`"be-dev-1"`, `"reviewer-7"`). Operator-assigned at pool boot or auto-incremented from role.
- `role`: matches folder under `.claude/agents/workers/<role>/`.

Canonical string for logging: `"<orchestrator_id>/<worker_id>"`.

`runtime_coordination.db.terminal_leases` already keys on text `terminal_id` — schema doesn't change; only validation logic relaxes (drop assertion that `terminal_id ∈ {T0..T3}` in `vnx_doctor_checks.py:492` etc).

New sidecar table: `worker_registry(worker_id, orchestrator_id, role, governance_variant, created_at)`.

Backward-compat: `T0..T3` continue to work as aliases that resolve to `main/T1` etc, for 6 months minimum.

### FR-10 — Interactive escape hatch

Operator can spawn an ad-hoc interactive session in any agent folder:
```bash
vnx interactive --agent backend-developer
# spawns: cd .claude/agents/workers/backend-developer && claude
```

Interactive sessions carry **no capability token** (operator is physically driving). When the session ends, a `session_summary` event is appended to `events/T{n}.ndjson` so observability surface stays uniform. Driver = "human" instead of "orchestrator."

This preserves the standing memory note `feedback_hybrid_interactive_headless.md`: tmux paths are permanent.

### FR-11 — Provider failover at orchestrator level

**Every orchestrator (main, sub) declares a primary provider plus an ordered list of fallbacks.** On primary unavailability, the orchestrator restarts on the next available fallback with state recovered from a summary checkpoint. Workers also support failover, but with a more conservative policy (because mid-task switching is risky).

#### 11.1 Per-orchestrator runtime config

```yaml
# .claude/agents/orchestrators/main/runtime.yaml
provider_chain:
  primary: { provider: claude, model: opus }
  fallbacks:
    - { provider: codex,  model: gpt-5.3-codex }
    - { provider: gemini, model: gemini-2.5-pro }

failover:
  trigger: on_unavailable     # OR on_error_5xx, on_timeout, manual
  health_check_interval: 60s
  consecutive_failures_before_flip: 3
  cooldown_before_reflip: 300s
  state_handoff: summary_checkpoint   # OR fresh (lossy), warn_operator (block)
```

Same schema applies to sub-orchestrators (in their own folder) and workers (in `.claude/agents/workers/<role>/runtime.yaml`).

#### 11.2 Health-check probe

A lightweight per-provider probe runs every `health_check_interval` (default 60s):

| Provider | Probe |
|----------|-------|
| Claude   | `claude --version` + `claude -p --dry-run "ping"` (no API call if dry-run honored) |
| Codex    | `codex --version` + cached `codex auth status` |
| Gemini   | `gemini --version` + `gemini --dry-run` if available |
| Ollama   | `curl -fsS http://localhost:11434/api/tags` |
| LiteLLM bridge | `litellm --health-check` |

Probe results land in `.vnx-data/state/provider_health.ndjson` (NDJSON ring buffer + archive, mirroring existing `events/` pattern).

#### 11.3 Failover state-handoff strategies

| Strategy | What happens at failover | When to use |
|----------|--------------------------|-------------|
| **`summary_checkpoint`** (recommended) | Orchestrator periodically writes a `.vnx-data/missions/<mission_id>/checkpoints/<timestamp>.md` summary. On failover, new provider starts fresh with `--instruction "$(cat latest_checkpoint.md)"`. | Default for orchestrators. Loses live context but preserves intent. |
| **`fresh`** | Drop the in-flight dispatch. Operator restarts manually. | Workers, where mid-task switching corrupts the artifact. |
| **`warn_operator`** | Block until operator acknowledges. Mission state goes to `paused`. | High-stakes coding-strict missions where any state loss must be human-reviewed. |

#### 11.4 Provider-agnostic trust chain

Capability tokens are **signed by the orchestrator's ed25519 key, not by a provider session**. Therefore:

- Main on Opus signs token → Tech Lead receives token → Opus goes down → Tech Lead's *Codex* fallback can dispatch workers using the *same* token chain (signature still verifies; trust anchor unchanged).
- A worker dispatched by orch-on-Opus is verifiable by the worker even if that orch later runs on Codex (same signing key, no provider re-handshake needed).

This is a key property of the cap-token design and is **only true if** the orchestrator's signing key persists across provider switches — which it does, because the key lives in `.vnx-data/state/keys/orchestrator_<id>_ed25519.priv`, not in any provider's session state.

#### 11.5 Cross-provider session continuity (limits)

| What survives failover | What does not |
|-----------------------|---------------|
| Capability token chain (key-based) | Provider's internal `session_id` (Opus session ≠ Codex session) |
| Mission state in `.vnx-data/missions/<id>.json` | Live conversation context inside the LLM |
| Receipts and dispatch register | Tool-call history within the prior provider's session |
| Orchestrator's signing key | F43 context-rotation handover summaries (need to be re-built per provider) |

The pragmatic answer: failover is **always lossy at the conversation level**, but **never lossy at the trust level**. The summary-checkpoint pattern minimizes conversation loss to ≤1 checkpoint interval (default: every 10 turns or 5 minutes, whichever first).

#### 11.6 Provider-pair compatibility matrix (orchestrator role)

| Primary → Fallback | Skill file pair | Tool-use semantics | Compatibility | Notes |
|-------------------|----------------|---------------------|---------------|-------|
| Opus → Codex      | CLAUDE.md → AGENTS.md | both support tool_use natively | ✅ Full | Default recommended pair |
| Opus → Gemini     | CLAUDE.md → GEMINI.md | both support function-calling | ✅ Full | Gemini-CLI v0.11+ required for streaming parity |
| Codex → Gemini    | AGENTS.md → GEMINI.md | both function-calling | ✅ Full | Operator-validated provider-pair |
| Opus → Ollama (Llama) | CLAUDE.md → CLAUDE.md (Llama uses Claude-style prompt OK) | Llama tool-use is text-pattern-based | ⚠️ Tier-2 observability | Acceptable for low-risk orchestrators |
| Any → Kimi 2.6    | CLAUDE.md → AGENTS.md or KIMI.md | OpenAI-compatible | ✅ Full | Future, requires KimiAdapter |

#### 11.7 Failover events in receipt

Receipt envelope gets two new optional fields:

```json
{
  "...existing...":,
  "provider_chain_at_dispatch": ["claude/opus", "codex/gpt-5.3-codex"],
  "active_provider_at_completion": "claude/opus",
  "failover_events": []
}
```

If a failover happened during the dispatch:

```json
{
  "failover_events": [
    {
      "ts": "2026-05-01T12:34:56Z",
      "from": "claude/opus",
      "to": "codex/gpt-5.3-codex",
      "trigger": "on_unavailable",
      "checkpoint_path": ".vnx-data/missions/M-001/checkpoints/20260501-123450.md"
    }
  ]
}
```

#### 11.8 LOC estimate

- `scripts/lib/provider_health.py` — health-check probe + ring buffer (~120 LOC)
- `scripts/lib/provider_chain.py` — chain resolver + failover decision (~100 LOC)
- `scripts/lib/checkpoint_writer.py` — summary checkpoint writer for orchestrators (~80 LOC)
- Receipt schema additions (`provider_chain_at_dispatch`, `failover_events`) (~30 LOC)
- Tests + docs (~150 LOC)
- **Total: ~480 LOC. New wave: W7.5 (between streaming drainer and folder agents).**

---

## 6. Non-Functional Requirements

| ID | NFR | Target |
|----|-----|--------|
| NFR-1 | **Local-first** | Zero external network daemons. SQLite + files only. (ADR-001.) |
| NFR-2 | **Backward compatibility** | All existing T0..T3 dispatches keep working during migration (≥6 months alias support). |
| NFR-3 | **Observability latency** | Per-event archival lag from provider emit to disk: <100ms p95. |
| NFR-4 | **Token verification cost** | Capability-token verify path: <5ms per dispatch (allowing 200 dispatches/sec headroom). |
| NFR-5 | **Storage** | `.vnx-data/` growth bounded by per-archive ring-buffer rotation already implemented. |
| NFR-6 | **macOS + Linux parity** | Bash 3.2 compat preserved. No new Linux-only syscalls. |
| NFR-7 | **No new external deps for core** | Capability tokens use stdlib `cryptography` (already a transitive dep) or pure-Python ed25519. No new wheels. |
| NFR-8 | **Test coverage** | ≥80% coverage on `cap_token.py`, `streaming_drainer.py`, `worker_provider.py`. ≥60% elsewhere. |
| NFR-9 | **Receipt schema additivity** | All new fields are optional; old readers tolerate missing fields. |
| NFR-10 | **Audit trail completeness** | Every dispatch (including sub-orchestrator dispatches) writes a receipt; no orphan dispatches. |

---

## 7. Architecture & Design

### 7.1 High-level architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  OPERATOR                                                         │
│  signs operator_ed25519.priv → root token                         │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ signs
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│  Main Orchestrator (Assistant) — Opus, interactive OR headless   │
│  reads missions/, dispatches sub-orchestrators                    │
└─────────┬────────────────────────────┬───────────────────────────┘
          │ signs token                │ signs token
          ▼                            ▼
   ┌──────────────┐             ┌──────────────┐
   │  Tech Lead   │             │ Marketing Mgr│
   │  Orchestrator│             │ Orchestrator │
   │  (own folder)│             │ (own folder) │
   └──────┬───────┘             └──────┬───────┘
          │ signs token                │ signs token
   ┌──────┼──────┬────┐         ┌──────┼──────┐
   ▼      ▼      ▼    ▼         ▼      ▼      ▼
  be-1  fe-1   rev-1 ...      copy-1 seo-1 analyt-1
 (claude)(claude)(codex)     (claude)(claude)(gemini)
```

Each box is a `WorkerProvider` instance. Each arrow is a dispatch carrying a capability token. Each session has its own `session_id` captured in the provider's `_session_ids` map.

### 7.2 Provider layer

```
WorkerProvider (Protocol)
├── ClaudeProvider      ← scripts/lib/adapters/claude_adapter.py (existing)
├── CodexProvider       ← scripts/lib/adapters/codex_adapter.py (refactored)
├── GeminiProvider      ← scripts/lib/adapters/gemini_adapter.py (refactored)
├── OllamaProvider      ← scripts/lib/adapters/ollama_adapter.py (audit + refactor)
├── LiteLLMProvider     ← scripts/lib/adapters/litellm_adapter.py (NEW)
└── KimiProvider        ← scripts/lib/adapters/kimi_adapter.py (future)
```

All compose `_streaming_drainer.py` mixin. Each declares `capabilities()`, `models()`, and registers in a `provider_registry.yaml`.

### 7.3 Folder-based loading

Replaces `_inject_skill_context()`:
- **Boot:** orchestrator reads `.claude/agents/orchestrators/<self>/{CLAUDE,AGENTS,GEMINI}.md` based on its provider runtime.
- **Dispatch:** dispatcher reads `.claude/agents/workers/<role>/{CLAUDE,AGENTS,GEMINI}.md`, attaches `permissions.yaml` (current `_inject_permission_profile`), signs cap token, passes folder path via `--cwd` + `VNX_AGENT_DIR`.

### 7.4 Capability token verification flow

```
worker subprocess starts
  ↓
reads VNX_CAP_TOKEN env var
  ↓
calls cap_token.verify(token, trust_anchors_path)
  ↓
checks: signature chain → trust anchor; all caveats; not replayed
  ↓
[OK] proceeds with dispatch
[FAIL] exits 1 with structured error in receipt
```

### 7.5 Data model deltas

#### dispatch.json (additions)

```json
{
  "dispatch_id": "...",
  "worker_ref": "tech-lead/be-dev-1",
  "agent_kind": "worker",
  "parent_dispatch_id": "20260501-tech-lead-plan-f50",
  "resume_of": null,
  "capability_token": "<base64>",
  "...": "existing fields"
}
```

#### dispatch_register.ndjson (additions)

```json
{
  "ts": "...",
  "event": "dispatch_emitted",
  "dispatch_id": "...",
  "parent_dispatch_id": "...",
  "agent_kind": "...",
  "worker_ref": "...",
  "...": "existing fields"
}
```

#### Receipt envelope (additions)

```json
{
  "...": "existing receipt fields",
  "observability_tier": 1,
  "capability_token_chain_depth": 2,
  "agent_folder": ".claude/agents/workers/backend-developer"
}
```

#### New: `.vnx-data/missions/<id>.json`

```json
{
  "mission_id": "M-2026-05-01-001",
  "title": "Ship F50 + launch announcement",
  "state": "active",
  "created_at": "...",
  "owner_orchestrator": "main",
  "child_dispatches": ["dispatch_id_1", "dispatch_id_2"],
  "summary": "..."
}
```

#### New: `worker_registry` SQLite table

```sql
CREATE TABLE worker_registry (
  worker_id TEXT NOT NULL,
  orchestrator_id TEXT NOT NULL,
  role TEXT NOT NULL,
  governance_variant TEXT NOT NULL,
  provider TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (orchestrator_id, worker_id)
);
```

### 7.6 Architecture mode matrix (interactive ⇄ headless at every level)

The dual architecture (interactive + headless) is preserved at **every tier** of the hierarchy. Every node in the tree below can run in either mode, independently of its parent or children.

|                         | Interactive (tmux pane / TTY)                        | Headless (subprocess only)                      |
|-------------------------|------------------------------------------------------|-------------------------------------------------|
| **Operator**            | always interactive (it's a human)                    | n/a                                              |
| **Main Orchestrator**   | current T0 — operator drives directly                | dispatched via cron / mission-trigger / webhook |
| **Sub-Orchestrator**    | opt-in via `vnx attach <orchestrator_id>` (own pane) | default — subprocess in pool, observable via events archive |
| **Worker**              | `vnx interactive --agent <role>` (operator drives, no token) | default — dispatched by (sub-)orchestrator, full token chain |

**Properties of this matrix:**

1. **Mode is a per-node attribute, not a tree-wide attribute.** Main can be interactive while sub-orchestrators are headless and workers are headless. Or main can be headless (cron-triggered) while one sub-orchestrator is interactive for operator inspection.
2. **Cap-token chain is mode-agnostic.** Headless and interactive nodes both verify against the same trust root. Interactive sessions just don't *consume* a token (operator is the driver).
3. **Observability is mode-uniform.** Every node — interactive or headless — emits events to its own archive. Interactive sessions emit `session_summary` on close.
4. **The 4-mode matrix from `docs/manifesto/HEADLESS_TRANSITION.md` generalizes:** what was "T0 interactive + workers headless" etc becomes "any-tier interactive + any-tier headless" with the per-node opt-in flag.

This matrix is the unified design that obsoletes the static T0/T1/T2/T3 model — any node, any mode, any provider (per FR-11).

### 7.7 Provider-failover summary diagram

```
   ┌────────────────────────────────────────────────────────┐
   │ orchestrator boot                                      │
   │   reads runtime.yaml: primary=opus, fallbacks=[codex,..]│
   │   spawns provider probe loop (60s)                     │
   └────────────────────────────────────────────────────────┘
                         │
                         ▼
   ┌────────────────────────────────────────────────────────┐
   │ normal operation on primary (opus)                     │
   │   periodically writes summary checkpoint to            │
   │   .vnx-data/missions/<id>/checkpoints/<ts>.md          │
   └─────────┬──────────────────────────────────────────────┘
             │ probe detects 3 consecutive failures
             ▼
   ┌────────────────────────────────────────────────────────┐
   │ failover decision                                       │
   │   - log to provider_health.ndjson                      │
   │   - emit failover_event                                │
   │   - cap-token chain UNCHANGED (same orch key)          │
   └─────────┬──────────────────────────────────────────────┘
             │
             ▼
   ┌────────────────────────────────────────────────────────┐
   │ restart on next available fallback (codex)             │
   │   instruction = latest checkpoint summary              │
   │   workers continue under same token chain              │
   └────────────────────────────────────────────────────────┘
```

The trust chain survives the provider switch because cap-tokens are signed by the orchestrator's persistent ed25519 key, not by any provider's session.

---

## 8. Migration Strategy & Phasing

### 8.1 Sequencing

| Wave | Scope | LOC | Weeks | Prerequisite | Why now |
|------|-------|-----|-------|-------------|---------|
| **W6/F43** | F43 context rotation revival + single-system migration P1-P6 | ~1500 | 2-3 | (none) | State must consolidate before sub-orchestrators |
| **W7** | Streaming drainer + Canonical event schema + Tier-1/2/3 labels | ~290 + ~200 tests | 1 | W6 | Cheapest win; closes governance hole; reused everywhere below |
| **W7.5** | Provider-failover (FR-11): health probe + provider chain + summary checkpoints | ~480 | 1-2 | W7 | Without this, "Opus down" = full halt. Mandatory before sub-orchestrators land. |
| **W8** | Folder-based agents Phase A+B (single SKILL.md + 3 symlinks per folder) | ~900 | 3 | W7.5 | Enables N workers without renaming; dispatcher learns folders; symlinks created at boot |
| **W9** | Universal `WorkerProvider` interface | ~400 | 2 | W8 | Refactor `ProviderAdapter` → `WorkerProvider`; add `models()`, lifecycle |
| **W10** | Capability tokens + governance variants | ~600 | 2-3 | W9 | Trust chain MUST land before multi-orchestrator |
| **W11** | Workers=N rename + lease polymorphism | ~600 | 2 | W10 | Now scope is signed and folders carry config; drop T0..T3 |
| **W12** | Sub-orchestrator pools + missions + Assistant orchestrator | ~800 | 3 | W11 | Tier-2 orchestrators with own worker pools |
| **W13** | LiteLLM bridge + Ollama parity + first community module (`headless-context-rotation`) PyPI | ~400 | 1 | W12 | Reach Bedrock/Vertex/Mistral for free |
| **W14** | Folder-based agents Phase C cutover (remove legacy injection) | ~200 | 1 | W13 | Burn the bridge once new path is proven |
| **W15** | Roadmap-autopilot integration over universal harness | (existing 4 PRs) | 2 | W14 | Now operates universally |

**Total new code: ~3690 LOC over ~17-21 weeks** (with parallelism, ~14 calendar weeks).

### 8.2 What happens in W7 (the next concrete step)

1. **W7-A** — `CanonicalEvent` schema + `EventStore` API (~80 LOC). Receipts learn `observability_tier` field.
2. **W7-B** — `_streaming_drainer.py` mixin (~120 LOC). All adapters compose it.
3. **W7-C** — Codex adapter migrates to streaming (~115 LOC). Gap closes.
4. **W7-D** — Gemini-CLI adapter migrates (~55 LOC). Gap closes (gated behind `VNX_GEMINI_STREAM=1` until v0.11+ proven).
5. **W7-E** — `LiteLLMAdapter` proof-of-concept (~150 LOC + tests). Bedrock/Mistral reachable.
6. **W7-F** — `OllamaAdapter` audit + streaming refactor (~70 LOC).
7. **W7-G** — Tier-1/2/3 labeling in receipt + governance-variant gating logic (~50 LOC).

W7 is one PR per sub-step, mergeable independently. Estimated total ~640 LOC of source + tests.

### 8.3 Backward compatibility commitments

- T0..T3 aliases: **6 months minimum** post W11 cutover.
- Existing `worker_permissions.yaml`: kept as a fallback for 3 months post W8.
- `.claude/skills/` symlink: kept for 3 months post W14.
- All receipt fields added are optional; old readers do not break.

---

## 9. Risk Register

| ID | Risk | Likelihood | Impact | Mitigation |
|----|------|-----------|--------|-----------|
| R1 | Capability-token signing key compromise | Low | Catastrophic | Out of scope (root trust). Recommendation: hardware-backed (YubiKey) for v2. |
| R2 | Folder-based skill loading breaks an in-flight dispatch during cutover | Medium | High | Phase A keeps both paths; flag-controlled rollout per dispatch. |
| R3 | Codex `--json` event schema bumps in a future codex CLI version | Low | Medium | Adapter versions pinned in `provider_capabilities.yaml`; `raw` field preserves provider-native event for forensics. |
| R4 | Sub-orchestrator approval inheritance lets a buggy sub-orch fan-out 1000 dispatches | Medium | High | Cap-token `max_dispatches` caveat enforced by verifier. Operator's root token sets ceiling. |
| R5 | Schema migration drops a row during workers=N cutover | Low | High | All migrations are additive. `worker_registry` is sidecar, not replacement. Validation relaxation only, no data movement. |
| R6 | Gemini-CLI v0.11 streaming flag changes name in a later release | Medium | Low | Feature-flagged behind `VNX_GEMINI_STREAM=1`. |
| R7 | Tri-file authoring burden (CLAUDE.md + AGENTS.md + GEMINI.md per agent) | Medium | Medium | Open Decision OD-3: single-source `worker.md` + auto-converter. |
| R8 | Interactive sessions emit no token; operator forgets they're "off-system" and treats output as audited | Low | Medium | `session_summary` event explicitly tagged `driver=human`; receipt processor can flag. |
| R9 | LiteLLM bridge introduces dep that pulls 50 transitive packages | High | Medium | Wrap LiteLLM behind a minimal `litellm-cli` shim subprocess; main VNX dep tree stays clean. |
| R10 | ed25519 signature verification overhead on hot dispatch path | Low | Low | Benchmarked at <1ms; well within NFR-4. |
| R11 | Cross-provider state continuity at failover loses live conversation context | Medium | Medium | Summary-checkpoint pattern (FR-11.3) bounds loss to ≤1 checkpoint interval. Cap-token chain survives unchanged so trust isn't lost. Document for operator: failover is conversation-lossy by design; mission state survives. |
| R12 | Symlink-based SKILL.md breaks on filesystems that don't support symlinks (Windows w/o developer mode, some FUSE mounts) | Low | Medium | Boot helper detects symlink support; falls back to copying SKILL.md content into 3 sibling files (with a "DO NOT EDIT" header) if symlinks unavailable. Preserves headless path; interactive may show drift warning. |

---

## 10. Open Decisions Required from Operator (BLOCKING)

These decisions block the start of W7. Please answer in the same order:

### OD-1 — Codex (and Gemini) scope (REVISED v1.1)

> Are Codex and Gemini going to be *workers only*, or also *fallback orchestrators*?

The v1.1 vision (per FR-11) makes Codex and Gemini **first-class fallback orchestrators**. This means they need full per-event observability AND the ability to load orchestrator skills and sign capability tokens — same as Opus.

| Scope | What it implies |
|-------|-----------------|
| Worker only | W7 streaming-drainer fix sufficient. Skill folders only need worker-role markdown. |
| Worker + fallback orchestrator (recommended) | Same W7 streaming fix + Codex/Gemini get orchestrator skills + `runtime.yaml` provider chain enforced |

**Recommendation:** Worker + fallback orchestrator. Aligns with the "providers down" resilience requirement; tri-symlinked SKILL.md works for both worker and orchestrator roles uniformly.

### OD-2 — Capability-token signing key location

> Where does the operator's ed25519 private key live?

| Option | Pros | Cons |
|--------|------|------|
| Laptop file (`~/.vnx/keys/operator_ed25519.priv`, chmod 600) | Simple; works headless. | Compromised on laptop loss/theft. |
| macOS Keychain | OS-protected; Touch ID prompt on use. | Headless dispatch flow needs Keychain unlock. |
| YubiKey / hardware token | Strongest. | Requires physical presence per signing. |

**Recommendation:** laptop file for v1; hardware-backed in v2 if/when operator runs sensitive customer code.

### OD-3 — Skill file strategy (REVISED v1.1)

> How do we keep the skill prompt unified across providers?

**Decision: single `SKILL.md` + 3 provider-named symlinks (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) per agent folder.** See FR-4.2 for the trade-off table.

| Option | LOC | Drift risk | Status |
|--------|-----|------------|--------|
| Tri-file manual | 0 | High | **Rejected** — drift unavoidable |
| Single `SKILL.md` + auto-converter | ~300 | Low | Deferred — only needed if provider-specific prompt divergence becomes a real requirement |
| **Single `SKILL.md` + symlinks** | ~10 | Zero | **Accepted** — simplest, drift-impossible, headless-and-interactive unified |

This decision **closes** OD-3 in v1.1 — operator no longer needs to choose; the symlink approach gives single-source-of-truth without the converter LOC. If provider divergence is ever needed later, upgrade to converter is a one-time migration.

### OD-4 — Sub-orchestrator approval gates

> When the Assistant dispatches Tech Lead "plan and ship F50," do Tech Lead's *own* dispatches to its workers still need human promotion-from-pending?

| Option | Pros | Cons |
|--------|------|------|
| Strict (every dispatch hits `pending/`) | Maximum safety; per-step human checkpoints. | Slow; defeats the point of sub-orchestration. |
| Inherit (sub-orch dispatches auto-approved within token scope) | Fast; trust chain enforces scope. | Loses per-step human oversight. |

**Recommendation:** Inherit — but only when capability token's `max_dispatches` and `max_risk_class` are set conservatively. Operator can revoke at any time by re-signing the parent token with `max_dispatches: 0`.

### OD-5 — Physical vs virtual sub-orchestrators

> Tech Lead and Marketing Manager: each gets a tmux pane, or purely subprocess?

| Option | Pros | Cons |
|--------|------|------|
| Physical (own pane) | Operator can attach + watch; familiar UX. | 4-pane layout already crowded. |
| Virtual (subprocess only) | Clean; many parallel sub-orchs possible. | Not directly inspectable; rely on event stream. |

**Recommendation:** Virtual by default; opt-in pane attachment via `vnx attach <orchestrator_id>` (mirrors existing `VNX_ADAPTER_T*` flag).

### OD-6 — Worker pool size limit

> Hard ceiling per orchestrator (e.g. 16 workers) or fully dynamic?

**Recommendation:** Configurable per orchestrator in `workers.yaml` with default of 8. Hard ceiling at 32 (operator can override). Lease sweep depends on bounded set.

---

## 11. Success Metrics

| Metric | Target | How measured |
|--------|--------|--------------|
| **Provider parity (observability)** | 100% of merged dispatches have per-event archive | Count of dispatches with `events/archive/<id>.ndjson` / total |
| **Folder agent adoption** | ≥90% of dispatches use `VNX_FOLDER_AGENTS=1` path 1 month post W14 | `agent_folder` field present in receipt |
| **Cap-token coverage** | 100% of post-W10 dispatches carry verified token | `capability_token_chain_depth` field present |
| **Sub-orchestrator missions delivered** | ≥3 successful end-to-end missions in first month post W12 | Count of `missions/*.json` with `state=done` |
| **Time-to-add-provider** | New provider integration ≤2 days for one developer | Onboarding measure on next provider added (Kimi target) |
| **Dispatch latency** | p95 from operator request to first event <2s | EventStore timestamp delta |
| **Backward compat** | 0 receipt-processing failures attributable to new fields | CI integration test on legacy receipts |
| **Community pull** | First standalone module (`headless-context-rotation`) gets ≥100 PyPI downloads in first month | PyPI stats |

---

## 12. References

### Research basis (this PRD's sources)
- `claudedocs/2026-05-01-multi-orchestrator-research.md` — sections 2-6 of this PRD draw on it
- `claudedocs/2026-05-01-universal-harness-research.md` — sections 5, 7, 8 of this PRD draw on it

### Architectural decisions
- `docs/governance/decisions/ADR-001-no-external-redis.md` — informs NFR-1, NFR-7
- `docs/governance/decisions/ADR-002-f43-context-rotation-packaging.md` — informs UC-4, W6/F43, success metric

### Related operator memory (standing preferences)
- `feedback_hybrid_interactive_headless.md` — informs FR-10
- `project_model_agnostic_flow.md` — informs OD-3
- `feedback_mandatory_triple_gate.md` — informs FR-6

### Standing roadmap (overlap)
- `ROADMAP.yaml` — `roadmap-autopilot` feature (PR-0..PR-3) is W15 in this PRD's sequencing
- `claudedocs/2026-04-30-single-vnx-migration-plan.md` — W6 in this PRD's sequencing

### Critical files this PRD touches (in implementation phases)

**FR-1 / FR-2 / W7 (~640 LOC):**
- `scripts/lib/provider_adapter.py` — refactor to `WorkerProvider` Protocol
- `scripts/lib/adapters/_streaming_drainer.py` — NEW
- `scripts/lib/adapters/codex_adapter.py:120-122,169-184` — gap closure
- `scripts/lib/adapters/gemini_adapter.py:70,130-148` — gap closure
- `scripts/lib/event_store.py` — add `observability_tier` field
- `scripts/lib/canonical_event.py` — NEW

**FR-4 / W8 (~900 LOC):**
- `scripts/lib/subprocess_dispatch_internals/skill_injection.py:228-247` — fold into folder loader
- `scripts/lib/agent_folder_loader.py` — NEW
- `.claude/agents/` — NEW directory tree

**FR-5 / FR-6 / W10 (~600 LOC):**
- `scripts/lib/cap_token.py` — NEW
- `scripts/lib/gate_stack_resolver.py` — NEW
- `scripts/vnx_trust_init.py` — NEW
- `scripts/lib/dispatch_deliver.sh` — token verification before deliver

**FR-7 / FR-8 / W12 (~800 LOC):**
- `scripts/lib/orchestrator_adapter.py` — NEW
- `scripts/build_t0_state.py` — dispatch tree forest rendering
- `scripts/lib/mission_manager.py` — NEW

**FR-9 / W11 (~600 LOC):**
- `scripts/lib/runtime_facade.py:49` — drop `CANONICAL_TERMINALS`
- `scripts/lib/decision_executor.py:31-33,142` — polymorphic worker_id
- `scripts/lib/vnx_doctor_checks.py:168,492` — drop assertion
- `scripts/lib/vnx_start_runtime.py:84,121-123,277-320` — N-worker bootstrap
- `runtime_coordination.db` — `worker_registry` table migration

---

## 13. Verification Plan (end-to-end)

After each wave merges:

### W7 verification
```bash
# Dispatch via codex; confirm per-event archive lands
python3 scripts/lib/subprocess_dispatch.py --terminal-id T1 --dispatch-id smoke-w7-codex \
  --provider codex --model gpt-5.3-codex --instruction "echo hello"
# Expect: .vnx-data/events/T1.ndjson grows live during run; archive at end
ls .vnx-data/events/archive/T1/smoke-w7-codex.ndjson
# Expect: ≥3 events (init, item.completed, turn.completed)
wc -l .vnx-data/events/archive/T1/smoke-w7-codex.ndjson
```

### W8 verification
```bash
# Boot orchestrator with VNX_FOLDER_AGENTS=1
VNX_FOLDER_AGENTS=1 python3 scripts/lib/subprocess_dispatch.py --role backend-developer \
  --instruction "create test file"
# Expect: receipt has agent_folder=".claude/agents/workers/backend-developer"
grep agent_folder .vnx-data/state/t0_receipts.ndjson | tail -1
```

### W10 verification
```bash
# Bootstrap operator key
python3 scripts/vnx_trust_init.py
# Dispatch with cap token
python3 scripts/lib/subprocess_dispatch.py --role backend-developer --instruction "noop" \
  --capability-token "$(python3 scripts/sign_test_token.py)"
# Expect: receipt has capability_token_chain_depth=1
# Forge attempt:
python3 scripts/lib/subprocess_dispatch.py --capability-token "FAKE"
# Expect: exit 1, receipt has error="cap_token_verify_failed"
```

### W12 verification
```bash
# Dispatch a sub-orchestrator
vnx mission create "test multi-tier" --orchestrator tech-lead
# Expect: missions/M-*.json created with state=active
# Tech Lead spawns 1+ workers
# Expect: dispatch tree visible in t0_state.json
python3 -c "import json; print(json.load(open('.vnx-data/state/t0_state.json'))['dispatch_tree'])"
```

### Full-stack smoke (post W15)
- Operator dispatches mission "implement F50 + write launch post."
- Main → Tech Lead (4 workers) + Marketing Lead (2 workers).
- All 6 workers complete with archived events.
- Cap-token chain depth = 3 in every worker receipt.
- Single mission file shows state transitions: planning → active → review → done.
- ≥1 worker uses Codex (not Claude), ≥1 worker uses Gemini.
- All gates run per governance variant (coding-strict for Tech Lead, business-light for Marketing).

---

*End of PRD. Awaiting operator decision on OD-1 through OD-6 before W7 implementation begins.*
