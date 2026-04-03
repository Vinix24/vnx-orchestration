# Business-Light Governance Profile Contract

**Feature**: Feature 20 — Business-Light Governance Pilot And Folder-Scoped Orchestration
**Contract-ID**: business-light-governance-v1
**Status**: Canonical
**Last Updated**: 2026-04-03

---

## 1. Purpose

This contract defines the `business_light` governance profile — the first non-coding
governance mode in VNX. It establishes folder-scoped orchestration rules, review-by-exception
policy, authority boundaries relative to `coding_strict`, and explicit pilot limits with
rollback criteria.

The contract exists so that:

- Business-light governance is explicit before any implementation
- Folder-scoped work is formally bounded and cannot leak into coding worktrees
- `coding_strict` authority is structurally protected from business-light decisions
- The pilot is reversible and its success/failure criteria are measurable

**Relationship to existing contracts**:
- `AGENT_OS_LIFT_IN_CONTRACT.md` defines the substrate boundary and capability profiles. This contract fills in the `business_light` row.
- `GOVERNANCE_FEEDBACK_CONTRACT.md` defines how signals flow back. Business-light signals feed the same pipeline.
- Coding-specific contracts (runtime adapter, headless session) are unchanged by this profile.

---

## 2. Business-Light Governance Rules

### 2.1 Profile Summary

| Aspect | `coding_strict` | `business_light` |
|--------|-----------------|-------------------|
| Scope model | Git worktree | Folder |
| Review policy | Every PR reviewed by gate | Review-by-exception |
| Gate requirement | Codex + Gemini required | No required gates (opt-in) |
| Closure authority | Human only | Manager may close (with audit) |
| Policy mutation | Blocked | Blocked |
| Evidence retention | 30 days | 14 days |
| Audit trail | Full coordination events | Reduced (lifecycle events only) |
| Runtime adapter | tmux (primary) | headless (primary) |
| Worker model | Interactive (tmux panes) | Headless-by-default |

### 2.2 Review-By-Exception Policy

Business-light dispatches are **not** reviewed by quality gates by default. Instead:

1. **Default**: Dispatches complete when the manager (T0 equivalent) accepts the worker output. No gate blocks closure.
2. **Exception trigger**: A dispatch is flagged for review when:
   - Worker output contains error markers or failure signals
   - The dispatch touches a path listed in `review_required_paths` in the scope config
   - An operator manually requests review
   - A recurrence signal (from governance feedback loop) flags the pattern
3. **Exception review**: When triggered, the dispatch is held and routed to a gate (Codex or operator) before closure.

### 2.3 Closure Rules

| Rule | Description |
|------|-------------|
| **BL-C1** | Manager may close a dispatch without human approval if no exception is triggered |
| **BL-C2** | Closure must still emit a receipt to the receipt pipeline |
| **BL-C3** | Closed dispatches must record `closure_authority: "manager"` or `closure_authority: "operator"` in the receipt |
| **BL-C4** | If an exception is triggered post-closure, the dispatch must be reopenable by the operator |
| **BL-C5** | During the pilot phase, manager closure is enabled but all closures must be logged with `pilot_audit: true` in the receipt for post-hoc review (see Section 6) |

### 2.4 Open Item Handling

- Business-light open items use the same `open_items_manager.py` infrastructure
- Severity semantics differ: `blocker` in business_light means "needs operator attention" not "blocks merge"
- Business-light open items are namespaced by domain to prevent contamination: `OI-BL-NNN`
- Carry-forward between business-light features follows the same ledger pattern but with separate namespace

---

## 3. Folder-Scoped Orchestration

### 3.1 Scope Model

Business-light work is scoped to **folders** rather than git worktrees:

| Aspect | Detail |
|--------|--------|
| **Root** | A configured folder path (e.g., `~/Documents/Projects/ClientX/`) |
| **Isolation** | File-system directory boundaries. No git branch isolation. |
| **Discovery** | Folder contains a scope config file (`.vnx-scope.json`) |
| **Identity** | Folder path is the scope identifier. Not tied to git repo. |
| **Multi-scope** | Multiple folder scopes may be active simultaneously |

### 3.2 Scope Config

Each business-light folder contains:

```json
{
  "scope_type": "folder",
  "domain": "business",
  "governance_profile": "business_light",
  "root": "/absolute/path/to/folder",
  "review_required_paths": ["contracts/", "financials/"],
  "context_sources": ["*.md", "*.txt", "*.pdf"],
  "excluded_paths": [".git/", "node_modules/", ".vnx-data/"],
  "max_file_size_kb": 500,
  "created_at": "ISO8601"
}
```

### 3.3 Context Sources

Business-light context is assembled from folder contents:

| Source | Priority | Description |
|--------|----------|-------------|
| `.vnx-scope.json` | P0 | Scope configuration |
| `*.md` files in root | P1 | Primary business context (project briefs, requirements) |
| Dispatch prompt | P2 | Current task instructions |
| Previous receipts | P3 | Prior outputs for this scope |
| Governance feedback signals | P7 | Recurring patterns from feedback loop |

Context budget follows the same bounded model as coding (P3-P7 overhead < 20% target, 25% hard limit) but with folder-based discovery instead of git-based discovery.

### 3.4 Folder Boundary Invariants

- **F-1**: A business-light dispatch cannot read or write files outside its folder scope root
- **F-2**: A business-light dispatch cannot access git operations (commit, push, branch)
- **F-3**: Folder scope paths must be absolute and must not overlap with any coding worktree path
- **F-4**: Folder scope discovery is explicit (`.vnx-scope.json` must exist) — no implicit scope inference

---

## 4. Authority Boundaries

### 4.1 Cross-Profile Isolation

`business_light` and `coding_strict` are structurally isolated:

| Boundary | Rule |
|----------|------|
| **AP-1** | Business-light dispatches cannot create, modify, or close coding-strict dispatches |
| **AP-2** | Business-light open items cannot escalate into coding-strict blocker items |
| **AP-3** | Business-light manager cannot acquire leases on coding-terminal slots (T1/T2/T3 when allocated to coding) |
| **AP-4** | Business-light signals feed the governance feedback loop but cannot trigger coding-strict policy changes |
| **AP-5** | Business-light capabilities are declared in the `business_light` CapabilityProfile — they do not inherit coding capabilities by default |
| **AP-6** | Coding-strict operations are unaware of business-light state. No coding module imports business-light modules. |

### 4.2 Shared Substrate Access

Both profiles share the substrate layer, but through domain-scoped namespaces:

| Substrate Component | Coding Namespace | Business Namespace | Shared? |
|--------------------|-----------------|--------------------|---------|
| Dispatch broker | `coding:` prefix | `business:` prefix | Same broker, scoped |
| Lease manager | T1/T2/T3 | BW1/BW2 (business workers) | Separate terminal pools |
| Receipt pipeline | Same NDJSON file | Same NDJSON file | Shared (receipts carry domain field) |
| Open items | `OI-NNN` | `OI-BL-NNN` | Same store, namespaced |
| Intelligence | Shared signal store | Shared signal store | Shared (signals carry domain field) |
| Coordination DB | Same DB | Same DB | Shared (domain column on all tables) |

### 4.3 Terminal Allocation

Business-light workers use a **separate terminal pool**:

- Coding terminals: T0 (orchestrator), T1, T2, T3
- Business terminals: BT0 (business manager), BW1, BW2 (business workers)
- No terminal can serve both domains simultaneously
- Terminal pool size is configurable (default: 1 manager + 2 workers for business)

---

## 5. Capability Profile Declaration

Per `AGENT_OS_LIFT_IN_CONTRACT.md` Section 5:

```python
business_light_profile = CapabilityProfile(
    domain_id="business",
    governance_profile="business_light",
    manager_persistence=True,
    manager_judgment_authority=True,
    worker_headless_default=True,
    worker_disposable=True,
    worker_scope_model="folder",
    session_attempt_tracking=True,
    session_evidence_required=False,
    session_event_stream=False,
    gate_required=False,
    gate_types=[],
    closure_requires_human=False,  # BL-C1: manager may close
    policy_mutation_blocked=True,
    audit_retention_days=14,
    runtime_adapter_type="headless",
    provider_types=["claude_code"],
)
```

---

## 6. Pilot Limits

### 6.1 Pilot Constraints

The business-light profile launches as a **constrained pilot**, not a production rollout:

| Constraint | Value | Rationale |
|------------|-------|-----------|
| Max concurrent business scopes | 2 | Limit blast radius |
| Max active business workers | 2 | Prevent resource contention with coding |
| Autonomous closure | Enabled with pilot audit logging (BL-C5) | Prove manager closure works; post-hoc review via `pilot_audit` receipts |
| Max dispatch age | 1 hour | Prevent long-running unmonitored work |
| Gate requirement | Optional but logged | Measure gate value before enforcing |
| Terminal pool | Separate from coding | Prevent cross-domain resource conflicts |

### 6.2 Pilot Success Criteria

The pilot is successful when all of these are met over a 7-day observation window:

| Criterion | Threshold | Evidence |
|-----------|-----------|---------|
| **PS-1** | 10+ dispatches complete without operator intervention | Receipt count with `closure_authority: "manager"` |
| **PS-2** | Zero cross-domain contamination incidents | No business dispatch touching coding state |
| **PS-3** | Review-by-exception triggers correctly | At least 1 exception-triggered review |
| **PS-4** | Folder scope boundaries hold | No file access outside scope root |
| **PS-5** | Open item namespace isolation works | No `OI-BL-*` items in coding lists |
| **PS-6** | Pilot can be disabled without affecting coding | `VNX_BUSINESS_LIGHT_ENABLED=0` stops all business dispatches |

### 6.3 Rollback Criteria

The pilot must be rolled back if any of these occur:

| Trigger | Action |
|---------|--------|
| Cross-domain contamination (AP-1..AP-6 violated) | Immediate disable, incident report |
| Coding-strict regression (any coding test fails due to business-light code) | Immediate revert of business-light changes |
| Resource starvation (coding terminals impacted by business workers) | Disable business workers, investigate |
| Autonomous closure produces incorrect output (when later enabled) | Revert to `closure_requires_human=True` |
| Operator reports business-light is reducing coding-first trust | Pause pilot, review scope |

### 6.4 Rollback Mechanism

```bash
# Immediate disable — all business dispatches stop, coding unaffected
export VNX_BUSINESS_LIGHT_ENABLED=0

# Full revert — remove business-light capability registration
vnx domain disable business
```

---

## 7. Testing Contract

### 7.1 Profile Tests

1. Business-light profile validates against substrate
2. Business-light profile is experimental maturity (not production-ready)
3. `policy_mutation_blocked` is True
4. `closure_requires_human` is False (manager closure enabled; pilot phase adds `pilot_audit` logging per BL-C5)

### 7.2 Isolation Tests

1. Business dispatch cannot access coding dispatch state (AP-1)
2. Business open items namespaced with `OI-BL-` prefix (AP-2)
3. Business terminals are separate pool (AP-3)
4. Folder scope boundary enforced (F-1, F-2, F-3)
5. Scope config required for folder discovery (F-4)

### 7.3 Review-By-Exception Tests

1. Default dispatch closes without gate
2. Error-marker dispatch triggers exception review
3. `review_required_paths` folder triggers exception
4. Operator manual review request works
5. Recurrence signal triggers exception

### 7.4 Pilot Limit Tests

1. Max concurrent scopes enforced
2. Max active workers enforced
3. Manager closure receipts include `pilot_audit: true` during pilot
4. Max dispatch age enforced
5. `VNX_BUSINESS_LIGHT_ENABLED=0` stops all business dispatches

---

## 8. Migration Path

### Phase 1: Contract Lock (This PR)
- Contract document is canonical
- No code changes

### Phase 2: Folder-Scoped Orchestration (PR-1)
- Implement folder scope config and discovery
- Implement scope boundary enforcement (F-1..F-4)
- Add folder-scoped context assembly

### Phase 3: Review-By-Exception And Closure Rules (PR-2)
- Implement review-by-exception policy
- Implement closure rules (BL-C1..BL-C5)
- Add exception trigger detection

### Phase 4: Pilot Limits And Domain Integration (PR-3)
- Implement pilot constraints
- Register business_light profile with substrate
- Add feature flag and rollback mechanism

### Phase 5: Certification (PR-4)
- Prove isolation (AP-1..AP-6)
- Prove folder scope boundaries (F-1..F-4)
- Prove pilot success criteria measurability
- Update planning docs

---

## 9. Open Questions (Resolved)

| Question | Resolution |
|----------|-----------|
| Should business-light use the same receipt pipeline? | Yes. Receipts carry a `domain` field for filtering. Separate pipelines would fragment audit. |
| Should business workers share T1/T2/T3? | No. Separate terminal pool (BW1/BW2) prevents resource contention and simplifies isolation. |
| Should business-light have its own coordination DB? | No. Same DB with domain column. Separate DBs would fragment substrate. |
| Can business-light graduate to production-ready? | Not in this feature. Maturity level is EXPERIMENTAL. Production graduation requires a separate enablement decision. |
| Should review-by-exception default to on or off during pilot? | Off (no gates by default). Exception triggers pull dispatches into review. This matches the business-light design intent. |
