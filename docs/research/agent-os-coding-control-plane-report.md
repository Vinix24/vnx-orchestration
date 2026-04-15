# Business OS / Agent OS Strategy Report

**Status**: Draft  
**Last Updated**: 2026-04-01  
**Owner**: T0 / Architecture Review  
**Purpose**: Define a stronger architectural direction for the next VNX major upgrade, with explicit distinctions between stateful orchestration, execution transport, governance profiles, and domain-specific rollout.

---

## 1. Executive Summary

The strongest next direction for VNX is **not** to become a generic multi-agent gateway or a thin command center. The real differentiator is already elsewhere:

- VNX preserves **open items** across runs and reviews;
- orchestrators are **stateful decision-makers**, not just message routers;
- quality checks and closure gates are part of the lifecycle;
- new findings can become new work automatically;
- the system can remain **local-first**, durable, and inspectable.

The right strategic framing is therefore:

> Build VNX as a **stateful orchestration and continuity system** with shared infrastructure for coding and business, but with **different governance profiles** per domain.

That yields these core choices:

1. Keep **one shared substrate** for task state, open items, history, preferences, sessions, and operator views.
2. Treat **coding**, **content/business**, and **regulated domains** as different governance profiles, not as different architectures.
3. Model the system around **persistent interactive managers** and **spawned headless workers**.
4. Keep **task/governance** distinct from **execution/runtime**.
5. Add an explicit **runtime session adapter layer** so the architecture does not remain coupled to tmux keystroke injection.
6. Use **folder-scoped ownership** for business/content and **worktree-scoped ownership** for coding.
7. Preserve VNX’s core strengths: open-item continuity, quality gating, and stateful orchestration memory.

This report therefore recommends a **shared Agent OS substrate**, with:

- `coding_strict` governance for software delivery;
- `business_light` governance for content and business pipelines;
- `regulated_strict` governance for sectors like healthcare, finance, and other high-audit workflows.

The transport layer and UI should support that model, not define it.

---

## 2. What VNX Actually Is

### 2.1 Wrong framing to avoid

VNX should **not** primarily be framed as:

- a remote-control tool;
- a chat-router for agents;
- a session gateway;
- a generic “agent team” console;
- a prettier wrapper around tmux.

Those may exist inside the system, but they are not the product core.

### 2.2 Correct framing

VNX is best understood as:

> a local-first orchestration system where managers retain judgment, unresolved work, quality findings, and operational memory across runs, while workers execute bounded tasks under explicit governance.

The distinctive capabilities are:

- open items survive across sessions;
- managers can create, track, escalate, and close work;
- execution results do not automatically equal acceptance;
- reviews, receipts, and evidence remain tied to work;
- preferences, mistakes, and lessons can be reused;
- the operator sees both **current work** and **historical continuity**.

This is stronger than routing-first systems because it emphasizes **continuity and governance**, not only communication.

---

## 3. Domain Model: One Substrate, Multiple Governance Profiles

### 3.1 Shared substrate

All domains should share the same substrate for:

- tasks;
- open items;
- orchestration decisions;
- execution attempts;
- session history;
- operator audit trail;
- preferences and lessons;
- manager and worker identity;
- read models and dashboards.

### 3.2 Governance profiles

The governance profile should vary by domain.

#### `coding_strict`

Used for software engineering work where correctness, merge safety, review quality, and traceability matter.

Typical characteristics:

- explicit dispatches;
- receipts and evidence;
- worktree isolation;
- review and validation steps;
- strict `done` gating;
- open items attached to findings and regressions;
- GitHub / PR / internal receipt integration.

#### `business_light`

Used for content and business workflows where throughput and consistency matter more than formal certification.

Typical characteristics:

- lighter task triage;
- review-by-exception;
- softer close gates;
- stronger emphasis on preferences, tone of voice, audience fit, and output history;
- persistent manager context;
- headless channel workers.

#### `regulated_strict`

Used for domains like healthcare, financial institutions, compliance-heavy operations, or any workflow where every step must be registered.

Typical characteristics:

- strict audit trail;
- approval checkpoints;
- policy constraints;
- stronger evidence retention;
- no silent auto-close;
- step-level traceability;
- explicit risk/open-item propagation.

This profile is architecturally closer to coding than to content.

### 3.3 Core conclusion

Coding agents and content pipelines are **different animals**, but they should still share the same substrate:

- same task/open-item/session model;
- same operator visibility;
- same manager/worker pattern;
- different governance intensity.

That is cleaner than building two systems.

---

## 4. Manager / Worker Operating Model

### 4.1 Primary pattern

The system should be built around:

- **persistent interactive managers**
- **spawned headless workers**

Managers keep durable context and judgment. Workers execute bounded tasks.

### 4.2 What managers do

Managers are not just “chat terminals.” They are stateful orchestration actors responsible for:

- planning and prioritization;
- intake and decomposition;
- preserving open items;
- applying lessons and preferences;
- reviewing or escalating outputs;
- deciding when work is actually done;
- maintaining continuity across sessions.

### 4.3 What workers do

Workers should be:

- bounded;
- disposable;
- task-focused;
- narrow in scope;
- easy to observe and re-run.

They may be:

- headless by default;
- interactive only when takeover or pairing is needed.

### 4.4 Why this matters

This solves a real weakness of long-lived multi-agent systems:

- if every worker is persistent, memory becomes fragmented;
- if every task starts fresh, continuity is lost;
- if managers are stateless routers, quality and open-item memory decay.

The durable state should live with **managers and substrate**, not with every worker.

---

## 5. Scope Model: Folder for Business, Worktree for Coding

### 5.1 Business/content scopes

Business work is naturally **folder-scoped**.

Example:

- `/Users/vincentvandeth/Desktop/BUSINESS/Content` = `content-manager`
- `/Users/vincentvandeth/Desktop/BUSINESS/Content/blog` = `blog` execution scope
- `/Users/vincentvandeth/Desktop/BUSINESS/Content/linkedin` = `linkedin` execution scope
- `/Users/vincentvandeth/Desktop/BUSINESS/Content/email` = `email` execution scope

Folder-local rules such as `CLAUDE.md`, templates, examples, and output history are valuable here and should remain first-class.

### 5.2 Coding scopes

Coding work is naturally **worktree-scoped**.

The unit of safe isolation is not “developer folder” or “reviewer folder,” but:

- feature worktree;
- branch/worktree identity;
- associated task set;
- associated receipts/open items/session history.

### 5.3 Do not confuse role with scope

These must remain separate:

- **runtime slot**: `T1`, `T2`, `T3`
- **profile/capability**: `developer`, `reviewer`, `debugger`, `qa-engineer`
- **scope**: worktree or folder

Replacing `T1/T2/T3` with role folders would collapse three distinct concepts into one and make dispatching weaker, not stronger.

### 5.4 Regulated domains

Regulated business work will often be folder- or case-scoped, but governance should be closer to `coding_strict`.

That means the architecture must support:

- folder scope with strict audit;
- human approvals;
- persisted step history;
- open-item escalation;
- evidence and sign-off.

---

## 6. Core Architectural Decisions

### 6.1 Task layer and execution layer stay separate

This remains the right split.

#### Task layer

This is the governance and continuity layer.

It contains:

- initiatives / projects / tasks;
- open items;
- acceptance contracts;
- priorities and dependencies;
- evidence links;
- preferences and lessons;
- closure gates.

#### Execution layer

This is the runtime layer.

It contains:

- worker sessions;
- execution attempts;
- provider/runtime selection;
- process state;
- PTY/log observation;
- retries/resumes;
- exit outcomes.

The rule remains:

- task objects describe **what, why, and under what governance**
- execution objects describe **how, where, and with what runtime outcome**

### 6.2 New required layer: runtime session adapter

The previous framing of `queue_dispatch` is not sufficient on its own. The architecture also needs an explicit **runtime session adapter layer**.

That layer exists to decouple VNX from tmux-specific delivery.

Responsibilities:

- spawn a worker session;
- attach to a session;
- observe PTY output;
- stop or reclaim a session;
- report health and capability metadata;
- support multiple transport/runtime adapters.

Initial adapters:

- `tmux_send_keys` adapter for backward compatibility
- `local_session_daemon` adapter for future session-based control

Potential future adapters:

- termlink-like Unix socket session transport
- background process launcher
- remote runner adapter

### 6.3 PTY is for observation and takeover, not primary dispatch

A PTY can technically inject input, including `Enter`, but raw PTY injection is still “typing into a UI.” It is not strong enough as the long-term orchestration primitive.

PTY should be used for:

- attach;
- mirror;
- observe output;
- manual takeover.

Primary dispatch should move toward:

- session spawn;
- structured launch spec;
- explicit acceptance and state reporting.

### 6.4 Transport abstraction before transport rewrite

The next step should **not** be a full tmux rewrite. It should be:

1. introduce a runtime/session abstraction;
2. keep tmux as one adapter;
3. prototype a better headless worker transport;
4. migrate gradually.

That preserves current momentum while unblocking the better architecture.

---

## 7. Recommended Data Model

### 7.1 Durable governance objects

- `initiative`
- `project`
- `task`
- `open_item`
- `manager_decision`
- `preference`
- `lesson`
- `task_dispatch`

### 7.2 Durable execution objects

- `worker_session`
- `execution_attempt`
- `handover`
- `receipt`
- `terminal_snapshot`
- `runtime_event`

### 7.3 Core identities

The architecture should treat these as first-class identifiers:

- `scope_id`
- `scope_path`
- `manager_id`
- `worker_profile`
- `session_id`
- `attempt_id`
- `terminal_id`
- `provider`
- `worktree_id` where applicable

### 7.4 Open items are first-class

Open items are not just annotations or report leftovers. They are core system objects and one of VNX’s main advantages.

Open items may originate from:

- manager review;
- worker result;
- QA/reviewer feedback;
- runtime failure;
- resume failure;
- repeated operator overrides;
- policy/compliance checks;
- learning loop pattern detection.

Open items must be attachable to:

- task;
- dispatch;
- execution attempt;
- session;
- scope.

### 7.5 Preferences and lessons are separate from receipts

Business/content workflows make this especially important.

You need durable objects for:

- tone of voice preferences;
- audience preferences;
- channel-specific style rules;
- rejected patterns;
- historical mistakes;
- approval notes;
- reusable judgments.

These should not be buried only inside receipts or transcripts.

---

## 8. Canonical Storage Direction

### 8.1 Recommendation for current VNX

For the next VNX major upgrade, the cleanest choice is:

- keep runtime and orchestration state under **`.vnx-data/`**
- introduce a dedicated subtree for the new control-plane artifacts

Recommended structure:

```text
.vnx-data/
  agent-os/
    initiatives/
    projects/
    tasks/
    open-items/
    decisions/
    preferences/
    lessons/
    dispatches/
      task/
      execution/
    sessions/
    runtime/
    views/
```

### 8.2 Why not create a second canonical root immediately

A separate repo root such as `.agent-os/` is attractive conceptually, but in current VNX it would create unnecessary split-brain risk.

The system already relies on:

- worktree-local `.vnx-data/`;
- file-based receipts;
- local runtime state;
- established operational conventions.

The least disruptive path is to evolve from there.

### 8.3 Future productization option

If VNX later becomes more externalized or productized, promoting the durable artifact model to `.agent-os/` can be reconsidered.

But for the next major internal step, `.vnx-data/agent-os/` is the better canonical choice.

---

## 9. Worker Launch Model

### 9.1 Worker launch spec

Every worker run should be expressed through a structured launch spec.

Minimum fields:

- `scope_type`
- `scope_path`
- `task_ref`
- `profile`
- `provider`
- `mode`
- `runtime_adapter`
- `acceptance_contract`
- `preference_refs`

Example:

```json
{
  "scope_type": "folder",
  "scope_path": "/Users/vincentvandeth/Desktop/BUSINESS/Content/blog",
  "task_ref": "task_blog_2026_041",
  "profile": "blog-writer",
  "provider": "claude-code-max",
  "mode": "headless",
  "runtime_adapter": "local_session_daemon",
  "acceptance_contract": "deliver draft + notes + open items",
  "preference_refs": [
    "pref_blog_tov_v1",
    "pref_mkb_audience"
  ]
}
```

### 9.2 Profiles vs scope

The execution identity of a worker is composed from:

- runtime slot or session;
- scope;
- profile.

Examples:

- coding worker:
  - scope = feature worktree
  - profile = `reviewer`
  - session = spawned headless Claude Code run

- business worker:
  - scope = `Content/blog`
  - profile = `blog-writer`
  - session = spawned headless Claude Code run

### 9.3 Current tmux model becomes legacy transport, not core identity

Today, VNX effectively routes work as “type this into terminal X.”  
The target model is “start or use session Y with launch spec Z.”

That is a major conceptual improvement even before the transport changes underneath.

---

## 10. Command Center / Operator UX

### 10.1 What the operator actually needs

The UI should be organized around operator questions, not implementation nouns.

Core questions:

- what managers are active right now?
- what workers are running right now?
- what work is blocked?
- what open items are unresolved?
- what preferences or lessons are affecting output?
- what failed recently and why?
- what sessions are safe to resume?
- what still requires human judgment?

### 10.2 Recommended v1 views

- `Overview`
  - active managers, active workers, blocked items, stale sessions
- `Scopes`
  - worktrees for coding, folders for business
- `Tasks`
  - tasks and task dispatches
- `Open Items`
  - blockers, findings, risks, recurring issues
- `Runtime`
  - sessions, attempts, health, headless runs, PTY attachability
- `Continuity`
  - preferences, lessons, handovers, resume readiness

### 10.3 Why not “Agents” as a primary tab

“Agents” is too generic. Operators need to distinguish:

- manager vs worker;
- scope;
- current work;
- quality status;
- unresolved items;
- resumability.

A flat “Agents” tab hides the real operating picture.

### 10.4 Read model first

This remains non-negotiable.

The UI must sit on stable read payloads, not on:

- ad hoc NDJSON reads;
- tmux pane assumptions;
- direct script output;
- raw SQLite or file layout coupling.

---

## 11. Framework Comparison

### 11.1 OpenClaw

What it is strong at:

- local-first gateway concept;
- session tooling;
- multi-channel surfaces;
- session-bound agents and sub-agents;
- routing and remote-control style capabilities.

What it does **not** define as a core product strength:

- durable open-item continuity;
- stateful manager judgment;
- quality-driven close gates;
- “new findings become new governed work” as the main operating model.

Usefulness to VNX:

- strong reference for runtime/session surfaces;
- useful for session identity and control concepts;
- useful for remote-surface design later.

What to avoid copying:

- letting routing become the product core;
- making channels or gateway features overshadow continuity and governance.

### 11.2 TermLink

What it is strong at:

- local Unix-socket session transport;
- structured session discovery;
- PTY attach and observation;
- event bus between sessions;
- a transport/control model much stronger than tmux keystrokes.

What it does **not** solve by itself:

- governance;
- quality gates;
- open-item lifecycle;
- manager judgment;
- domain-specific continuity memory.

Usefulness to VNX:

- very strong inspiration for a future runtime session adapter;
- validates the move away from tmux-specific transport.

What to avoid copying:

- mistaking transport for orchestration;
- assuming a better bus automatically solves work continuity.

### 11.3 Dimitri Geelen’s Agentic Engineering Framework

What it is strong at:

- task-first governance;
- approvals;
- traceability;
- board/task views;
- strong discipline that work should be tied to tasks.

What it does **not** center:

- multi-terminal runtime control;
- spawned headless worker orchestration;
- rich execution transport concerns;
- VNX-style open-item continuity across lanes and provider runs.

Usefulness to VNX:

- strong governance inspiration;
- confirms task-first discipline;
- helpful for operator board semantics.

What to avoid copying:

- collapsing governance and runtime into one repo-local task concept.

### 11.4 LangGraph

What it is strong at:

- durable execution;
- checkpoints;
- interrupts;
- resume primitives;
- human-in-the-loop transition points.

What it does **not** provide:

- VNX’s operator command center concept;
- worktree/folder operating model;
- open-item governance;
- stateful manager role with closure authority.

Usefulness to VNX:

- useful for resume/checkpoint patterns;
- useful for explicit interrupt/approval thinking.

What to avoid copying:

- over-graphing the runtime before the task/open-item model is stable.

### 11.5 Agentic Control Framework (ACF)

What it is strong at:

- file-first task artifacts;
- generated task views;
- readable local state;
- strong fit with local-first discipline.

What it does **not** fully solve:

- stateful orchestrator judgment;
- complex execution/runtime observability;
- quality-gated manager/worker lifecycles.

Usefulness to VNX:

- validates file-first artifacts and materialized overview files.

### 11.6 AutoGen Studio and similar consoles

What they are strong at:

- agent interaction visibility;
- team-console inspiration;
- prototyping interfaces.

What they usually underdeliver on for VNX’s needs:

- governance;
- open-item continuity;
- operator trust;
- closure rigor.

### 11.7 VNX’s actual comparative advantage

Compared with these frameworks, VNX’s distinctive value is:

- stateful orchestration authority;
- quality-aware completion gating;
- unresolved-work continuity;
- ability to turn findings into tracked follow-up work;
- local-first inspectability;
- ability to operate both lighter business flows and heavy governed flows on the same substrate.

That is the core to preserve.

---

## 12. Implications for the Current VNX Setup

### 12.1 What should not change immediately

- do not delete tmux support;
- do not freeze current feature work while inventing a new daemon;
- do not rewrite the full dashboard first;
- do not collapse T0/T1/T2/T3 into role folders.

### 12.2 What should change conceptually now

- stop treating terminals as fixed worker identities;
- start treating them as runtime slots;
- introduce explicit worker profiles;
- introduce explicit scope-based launches;
- make runtime transport an adapter, not an assumption.

### 12.3 What the next migration should be

The next step should be a **runtime abstraction**, not a full transport cutover.

Recommended sequence:

1. Define `WorkerLaunchSpec`
2. Define `RuntimeAdapter`
3. Keep `TmuxAdapter` for compatibility
4. Prototype `LocalSessionAdapter` for headless workers
5. Keep interactive managers human-facing until the headless path is proven

This lets VNX improve without stalling active plans.

---

## 13. Phased Rollout Proposal

### Phase 0: Thesis Lock

Before implementation, explicitly lock:

- VNX is a stateful orchestration and continuity system;
- one substrate, multiple governance profiles;
- manager/worker model is primary;
- transport is not the product core.

### Phase 1: Runtime Abstraction

- define `WorkerLaunchSpec`;
- define runtime/session adapter boundary;
- wrap current tmux delivery in an adapter;
- stop spreading dispatch logic directly across terminal scripts.

### Phase 2: Canonical Artifact Model

- implement `.vnx-data/agent-os/` structure;
- define durable task, open-item, preference, lesson, session, attempt objects;
- define lifecycle invariants.

### Phase 3: Headless Worker Prototype

- launch bounded local Claude Code worker sessions via new adapter;
- start with coding or content workers, not interactive managers;
- prove reliable spawn, tracking, attach, and result capture.

### Phase 4: Read Model

- build stable projections over tasks, open items, runtime sessions, preferences, and lessons;
- explicitly classify live-runtime vs durable history.

### Phase 5: First Operator Surface

- build views around managers, scopes, tasks, open items, runtime, continuity;
- keep actions limited at first.

### Phase 6: Governance Profiles

- formalize `coding_strict`;
- formalize `business_light`;
- formalize `regulated_strict`.

### Phase 7: Broader Domain Lift-In

- add business folder managers/workers more systematically;
- add stricter regulated-domain workflows;
- revisit remote control and channels only after local session architecture is stable.

---

## 14. Non-Goals for the Next Major Step

- not a full remote-control stack first;
- not a messaging-first architecture;
- not a wholesale tmux deletion on day one;
- not a role-folder replacement for coding;
- not “all domains equal” governance;
- not a premature cloud or API-first runner layer if Max-plan local CLI remains the operational constraint.

---

## 15. Open Questions

1. Should the first `LocalSessionAdapter` target headless coding workers or headless content workers?
2. For business/content, do you want one top-level manager per domain root, or layered managers when a subdomain becomes complex enough?
3. For coding, do you want managers only at orchestration level, or later specialized persistent managers such as `dev-manager`, `qa-manager`, and `review-manager`?
4. How strict should the first `business_light` close gate be?
5. Which preferences deserve first-class storage first: tone of voice, audience, approval notes, or “rejected patterns”?
6. For regulated domains, what evidence bundle should be mandatory at close?

---

## 16. Canonicalization Recommendation

This document should remain in `docs/research/` only while it is under active revision.

If adopted, it should be:

- promoted into `docs/architecture/`, or
- merged into the canonical architecture stack around [00_VNX_ARCHITECTURE.md](/Users/vincentvandeth/Development/vnx-roadmap-autopilot-wt/docs/core/00_VNX_ARCHITECTURE.md).

It should not become a long-lived “canonical strategy” while remaining an unindexed research note.

---

## 17. Final Recommendation

Adopt this direction **only after revision lock on the core decisions above**.

The strongest next VNX direction is:

> one shared substrate for stateful orchestration, continuity, open-item tracking, and operator visibility, with persistent managers, spawned workers, explicit runtime adapters, and domain-specific governance profiles.

That direction preserves what is actually unique in VNX while giving it a much stronger execution model than the current tmux-centric setup.
