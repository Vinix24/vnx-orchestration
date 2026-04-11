# Agent OS Lift-In Contract And Substrate Boundary

**Feature**: Feature 19 — Coding Substrate Generalization And Agent OS Lift-In
**Contract-ID**: agent-os-lift-in-v1
**Status**: Canonical
**Last Updated**: 2026-04-03

---

## 1. Purpose

This contract defines the boundary between VNX's coding-first core and the reusable
Agent OS substrate that future domain-specific agent families (business, regulated,
research) can build on. It locks what is shared, what remains coding-specific, and
what governance invariants every future domain must preserve.

The contract exists so that:

- Reusable substrate boundaries are explicit before extraction begins
- The coding core is preserved as authoritative — generalization does not dilute it
- Future domains have a locked capability profile model to build against
- Premature broad rollout is structurally blocked by anti-goals

**Relationship to existing documents**:
- `AGENT_OS_STRATEGY.md` defines the strategic vision. This contract makes it implementable.
- `AGENT_OS_REQUIREMENTS_AND_GUARDRAILS.md` defines hard requirements. This contract maps them to substrate boundaries.
- `RUNTIME_ADAPTER_CONTRACT.md` in this directory defines the runtime transport layer. This contract sits above it, defining the orchestration substrate.

---

## 2. Substrate Layer Model

### 2.1 Three Layers

VNX generalizes into three distinct layers. Each layer has clear ownership and
stability guarantees.

```
┌─────────────────────────────────────────────────┐
│         Domain Layer (coding, business, ...)     │
│  - Domain-specific skills, gates, scoping        │
│  - Domain governance profile                     │
│  - Domain-specific tooling (PR, worktree, ...)   │
├─────────────────────────────────────────────────┤
│         Substrate Layer (reusable)               │
│  - Manager/worker orchestration                  │
│  - Dispatch, lease, receipt pipeline             │
│  - Session lifecycle, event stream               │
│  - Open items, carry-forward, intelligence       │
│  - Capability profiles, governance enforcement   │
│  - Runtime adapter interface                     │
├─────────────────────────────────────────────────┤
│         Transport Layer                          │
│  - TmuxAdapter, HeadlessAdapter, LocalSession    │
│  - Process lifecycle, pane management            │
│  - Provider CLI integration                      │
└─────────────────────────────────────────────────┘
```

### 2.2 Layer Ownership Rules

| Layer | Owner | Stability | Modification Rule |
|-------|-------|-----------|-------------------|
| **Domain** | Domain team (e.g., coding team) | Evolves per domain | Domain-specific; no cross-domain coupling |
| **Substrate** | VNX core team | Stable — changes require cross-domain impact assessment | Must preserve all domain contracts |
| **Transport** | VNX core team | Stable — adapter interface locked | New adapters additive; existing adapters preserved |

---

## 3. Substrate Responsibilities

### 3.1 What The Substrate Provides (Reusable)

Every domain inherits these capabilities from the substrate without reimplementation:

| Capability | Implementation | Domain Role |
|------------|---------------|-------------|
| **Dispatch lifecycle** | dispatch_broker.py, runtime_coordination.py | Domain provides task content; substrate manages registration, claiming, delivery, completion |
| **Lease management** | lease_manager.py | Substrate manages terminal leases; domain does not interact with leases directly |
| **Receipt pipeline** | append_receipt.py, receipt_processor | Domain emits receipts; substrate ensures provenance chain |
| **Session lifecycle** | local_session_adapter.py, headless_event_stream.py | Substrate manages session/attempt tracking; domain consumes session state |
| **Open items** | open_items_manager.py | Substrate provides CRUD and carry-forward; domain defines severity semantics |
| **Intelligence & signals** | outcome_signals.py, governance_signal_extractor.py | Substrate collects and deduplicates signals; domain defines signal relevance |
| **Governance enforcement** | Quality gates, review gates | Substrate enforces gate outcomes; domain defines which gates are required |
| **Runtime adapter** | adapter_protocol.py, RuntimeAdapter | Substrate provides adapter interface; domain selects adapter type |
| **Capability profiles** | New (Section 5) | Substrate validates profiles; domain declares its profile |
| **Read model** | dashboard_read_model.py | Substrate provides projections; domain adds domain-specific views |

### 3.2 What The Substrate Does NOT Provide

| Responsibility | Owner | Reason |
|---------------|-------|--------|
| **Task content generation** | Domain | Prompts, instructions, skills are domain-specific |
| **Scope isolation model** | Domain | Coding uses worktrees; business may use folders; regulated may use sandboxes |
| **Review integration** | Domain | Coding integrates with GitHub PRs; other domains may use different review surfaces |
| **Domain-specific gates** | Domain | Coding requires Codex + Gemini; business may use lighter gates |
| **Merge/closure authority** | Domain governance profile | Different profiles have different closure rules |
| **Skill registry** | Domain | Skills are domain-specific (architect, test-engineer vs business-analyst) |

---

## 4. Coding-Specific vs Generalized Boundary

### 4.1 Coding-Specific (Stays In Domain Layer)

These capabilities are authoritative for the coding domain and must NOT be
pulled into the substrate:

| Component | Files | Reason |
|-----------|-------|--------|
| Worktree isolation | bin/vnx (new-worktree, finish-worktree, merge-preflight) | Coding-specific scoping model |
| PR/branch integration | GitHub PR creation, branch management | Coding-specific review surface |
| Code quality gates | codex_final_gate.py, quality_advisory.py | Coding-specific quality rules |
| File-level analysis | Function size, syntax checks, import validation | Coding-specific inspections |
| Git operations | Commit, push, merge-preflight | Coding-specific version control |
| Claude Code CLI integration | CLAUDE.md, terminal instructions | Coding-specific provider configuration |
| Test runner integration | pytest discovery, test evidence | Coding-specific validation |

### 4.2 Generalized (Moves To Substrate)

These capabilities are already domain-agnostic or can be made so:

| Component | Current Location | Generalization Required |
|-----------|-----------------|------------------------|
| Dispatch broker | dispatch_broker.py | None — already domain-agnostic |
| Lease manager | lease_manager.py | None — already domain-agnostic |
| Receipt pipeline | append_receipt.py | None — already domain-agnostic |
| Runtime coordination | runtime_coordination.py | None — already domain-agnostic |
| Session lifecycle | local_session_adapter.py | None — already domain-agnostic |
| Event stream | headless_event_stream.py | None — already domain-agnostic |
| Open items | open_items_manager.py | Minor — severity semantics should be configurable per profile |
| Signal extraction | governance_signal_extractor.py | Minor — signal sources should be pluggable per domain |
| Provider observability | provider_observability.py | None — already provider-agnostic |
| RuntimeAdapter | adapter_protocol.py | None — already transport-agnostic |
| Retrospective digest | retrospective_digest.py | None — already domain-agnostic |

### 4.3 Boundary Invariants

- **B-1**: No substrate change may break existing coding domain behavior. Coding domain tests are the regression gate.
- **B-2**: No domain-specific logic may be added to substrate modules. Domain logic must live in domain-layer files.
- **B-3**: The substrate must be usable by a new domain with zero coding-specific knowledge — only capability profile and governance profile required.
- **B-4**: Coding-specific components may import from the substrate. The substrate must never import from any domain layer.

---

## 5. Capability Profile Model

### 5.1 Purpose

A capability profile declares what a domain expects from its managers, workers,
sessions, runtime, and governance. Profiles are validated at initialization and
determine which substrate features are activated.

### 5.2 Profile Schema

```python
@dataclass
class CapabilityProfile:
    """Declares domain-level expectations for the substrate."""
    
    domain_id: str                    # "coding", "business", "regulated"
    governance_profile: str           # "coding_strict", "business_light", "regulated_strict"
    
    # Manager expectations
    manager_persistence: bool         # Stateful across dispatches?
    manager_judgment_authority: bool   # Can T0 make dispatch decisions?
    
    # Worker expectations  
    worker_headless_default: bool     # Workers are headless unless overridden?
    worker_disposable: bool           # Workers are stateless between dispatches?
    worker_scope_model: str           # "worktree", "folder", "sandbox", "none"
    
    # Session expectations
    session_attempt_tracking: bool    # Track attempts per session?
    session_evidence_required: bool   # Evidence completeness required for closure?
    session_event_stream: bool        # Structured event stream required?
    
    # Governance expectations
    gate_required: bool               # Quality gates mandatory?
    gate_types: List[str]             # Which gates: ["codex_gate", "gemini_review", ...]
    closure_requires_human: bool      # Silent autonomous closure blocked?
    policy_mutation_blocked: bool     # Automatic instruction edits blocked?
    audit_retention_days: int         # How long to keep audit trail
    
    # Runtime expectations
    runtime_adapter_type: str         # "tmux", "headless", "local_session"
    provider_types: List[str]         # ["claude_code", "codex_cli", "gemini"]
```

### 5.3 Known Profiles

| Field | `coding_strict` | `business_light` | `regulated_strict` |
|-------|-----------------|-------------------|---------------------|
| `manager_persistence` | True | True | True |
| `manager_judgment_authority` | True | True | True (with escalation) |
| `worker_headless_default` | False (tmux) | True | True |
| `worker_disposable` | True | True | True |
| `worker_scope_model` | `"worktree"` | `"folder"` | `"sandbox"` |
| `session_attempt_tracking` | True | True | True |
| `session_evidence_required` | True | False | True |
| `session_event_stream` | True | False | True |
| `gate_required` | True | False | True |
| `gate_types` | codex, gemini | (none required) | codex, gemini, audit |
| `closure_requires_human` | True | False | True |
| `policy_mutation_blocked` | True | True | True |
| `audit_retention_days` | 30 | 14 | 365 |
| `runtime_adapter_type` | `"tmux"` | `"headless"` | `"headless"` |

### 5.4 Profile Validation Rules

1. A domain must declare a capability profile before any dispatch can be created
2. The substrate validates that all required fields are present and within valid ranges
3. Profile changes require human review (no automatic profile mutation)
4. Unknown governance profiles are rejected at initialization
5. `policy_mutation_blocked` must be `True` for all profiles until explicitly relaxed by operator

### 5.5 Profile Registration

```python
register_domain(profile: CapabilityProfile) -> RegistrationResult
```

Registration validates the profile, creates the domain in the substrate, and
activates the appropriate governance enforcement. A domain cannot dispatch work
until registered.

---

## 6. Governance Authority Boundaries

### 6.1 Authority Matrix

| Authority | Substrate | Domain | Operator |
|-----------|-----------|--------|----------|
| Create dispatches | No | T0 only | Approve |
| Assign terminals | Substrate (lease manager) | No | Override |
| Enforce gates | Substrate (gate runner) | Define gates | Waive (with audit) |
| Close dispatches | No | T0 review | Final authority |
| Merge PRs | No | No | Human only |
| Mutate policy | No | No | Human only |
| Record signals | Substrate (signal extractor) | No | Review |
| Generate recommendations | Substrate (feedback loop) | No | Accept/dismiss |
| Modify capability profiles | No | No | Human only |

### 6.2 Governance Invariants

- **G-1**: The substrate enforces but does not decide. Gate results, signal thresholds, and recommendations are enforcement — the domain and operator retain decision authority.
- **G-2**: No governance profile may have `closure_requires_human=False` and `gate_required=True` simultaneously without explicit operator override. (Automated closure with mandatory gates creates deadlock risk.)
- **G-3**: Cross-domain contamination is forbidden. A coding dispatch cannot be processed by a business worker, and vice versa. Domain isolation is enforced at the dispatch level.
- **G-4**: All governance transitions (gate pass, gate fail, dispatch close, profile change) are recorded in the coordination events audit trail.
- **G-5**: `policy_mutation_blocked=True` is the default for all profiles. Relaxing this requires an explicit operator decision recorded in the audit trail.

---

## 7. Anti-Goals (Premature Rollout Blocks)

### 7.1 What Must NOT Happen During This Feature

| Anti-Goal | Reason | Enforcement |
|-----------|--------|-------------|
| **No business domain launch** | Business governance is projected, not proven. Launching it dilutes coding-first credibility. | Capability profile for `business_light` is defined but not activatable until a separate enablement feature. |
| **No regulated domain launch** | Regulated governance requires audit infrastructure not yet built. | Profile defined but blocked by missing `audit` gate type. |
| **No tmux removal** | tmux is still the active production adapter for coding. Removing it during substrate extraction breaks the working system. | B-1 invariant — coding tests gate regression. |
| **No forced production cutover** | New substrate abstractions must coexist with current paths until proven. | Feature flags for substrate routing (default: off). |
| **No autonomous policy mutation** | No governance profile allows automatic instruction edits. | `policy_mutation_blocked=True` enforced for all profiles. |
| **No cross-domain dispatch routing** | Domain isolation must be proven before allowing cross-domain work. | G-3 invariant enforced at dispatch level. |

### 7.2 Activation Requirements For Future Domains

Before a non-coding domain can be activated:

1. Its capability profile must be fully defined and validated
2. Its governance profile must have passing conformance tests
3. Its scope isolation model must be implemented and tested
4. Its domain-specific gates (if any) must be operational
5. An operator must explicitly enable the domain via registration
6. The activation decision must be recorded in the audit trail

---

## 8. Manager/Worker Substrate Responsibilities

### 8.1 Manager Substrate Contract

The substrate provides these guarantees to every manager (T0 equivalent):

| Responsibility | Substrate | Manager |
|---------------|-----------|---------|
| Terminal lease acquisition | Substrate owns lease lifecycle | Manager requests terminal via dispatch |
| Dispatch registration | Substrate ensures durable registration before delivery | Manager provides task content |
| Receipt processing | Substrate collects and links receipts | Manager reviews receipts for completion |
| Open item tracking | Substrate persists with dedup and carry-forward | Manager creates and resolves items |
| Intelligence injection | Substrate collects signals and injects P7 context | Manager consumes context |
| Recommendation surface | Substrate generates advisory recommendations | Manager decides whether to act |

### 8.2 Worker Substrate Contract

The substrate provides these guarantees to every worker (T1/T2/T3 equivalent):

| Responsibility | Substrate | Worker |
|---------------|-----------|--------|
| Execution surface | Substrate provides via RuntimeAdapter | Worker executes in provided surface |
| Dispatch delivery | Substrate delivers dispatch to worker terminal | Worker receives and activates |
| Session/attempt tracking | Substrate tracks lifecycle | Worker emits receipts at task boundaries |
| Event stream | Substrate persists structured events | Worker contributes events via adapter |
| Health monitoring | Substrate probes via adapter.health() | Worker is monitored (not self-reporting) |
| Failure classification | Substrate classifies exit codes and failures | Worker exits with honest codes |

---

## 9. Testing Contract

### 9.1 Substrate Extraction Tests

1. All substrate modules importable without any domain-specific imports (B-4)
2. Dispatch lifecycle works with a minimal capability profile (no coding assumptions)
3. Session lifecycle works without worktree or PR context
4. Open items work without GitHub integration

### 9.2 Coding Compatibility Tests

1. All existing coding domain tests continue to pass after substrate extraction (B-1)
2. Worktree operations unaffected by substrate changes
3. PR/review integration unaffected
4. Gate enforcement unaffected

### 9.3 Capability Profile Tests

1. Profile validation rejects missing required fields
2. Profile validation rejects unknown governance profiles
3. `policy_mutation_blocked=False` is rejected without operator override
4. Domain registration creates valid substrate state
5. Unregistered domains cannot create dispatches

### 9.4 Anti-Goal Tests

1. Business domain profile cannot be activated (feature-gated)
2. Regulated domain profile cannot be activated (missing audit gate)
3. Cross-domain dispatch routing is rejected (G-3)
4. Profile mutation without human review is rejected

---

## 10. Migration Path

### Phase 1: Contract Lock (This PR)
- Contract document is canonical
- No code changes

### Phase 2: Substrate Extraction (PR-1)
- Mark substrate modules with layer annotations
- Verify no domain imports in substrate layer
- Add import-boundary tests

### Phase 3: Capability Profiles (PR-2)
- Implement CapabilityProfile dataclass and validation
- Register coding_strict as the authoritative profile
- Define but do not activate business_light and regulated_strict

### Phase 4: Domain Plan Scaffolding (PR-3)
- Create planning templates for future domains
- Integrate guardrails into planning surfaces
- Add scaffolding validation

### Phase 5: Certification (PR-4)
- Prove substrate extraction preserves coding compatibility
- Prove capability profiles are honest
- Update planning docs

---

## 11. Open Questions (Resolved)

| Question | Resolution |
|----------|-----------|
| Should the substrate enforce domain isolation at the Python import level? | Yes for substrate -> domain direction (B-4). Domain -> substrate imports are allowed and expected. Enforced via import-boundary test. |
| Should capability profiles be stored in the database? | No. Profiles are code-defined constants validated at startup. Runtime state is in the coordination DB; profiles are static configuration. |
| Should business_light allow autonomous closure? | Yes, with `closure_requires_human=False`. But G-2 ensures this is only valid when `gate_required=False` simultaneously. |
| Should the substrate support multiple concurrent domains? | Yes, but domain isolation (G-3) prevents cross-domain routing. Each domain has its own dispatch namespace. |
| Should profile changes be versioned? | Not in this feature. Profile versioning is deferred to a future governance evolution feature. |
