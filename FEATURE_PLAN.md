# Feature: Verified Provider And Model Routing Enforcement

## PR-0: Routing Contract And Capability Boundaries
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @architect
**Estimated Time**: 2-3 hours
**Dependencies**: []

### Description
Define the canonical routing contract for provider and model requirements so dispatch metadata stops behaving as best-effort advice and becomes auditable execution intent.

### Scope
- Define the difference between:
  - provider selection
  - model selection
  - capability selection
  - execution mode
- Define which requirements are hard blockers vs operator warnings
- Define how interactive terminals, headless gates, and future provider-agnostic sessions should report actual runtime identity
- Define how pinned-terminal assumptions are represented when runtime model switching is unavailable or unverified
- Lock non-goals so this does not become a full terminal abstraction rewrite

### Success Criteria
- Routing rules are explicit rather than implicit
- Provider mismatch behavior is deterministic
- Model mismatch behavior is deterministic
- Pinned-terminal operation remains auditable while runtime switching is unreliable
- The contract supports future actor-identity work without being tied forever to T1/T2/T3

### Quality Gate
`gate_pr0_routing_contract`:
- [ ] Contract defines hard vs advisory routing requirements for provider, model, capability, and execution mode
- [ ] Contract defines how actual runtime identity is recorded after dispatch
- [ ] Contract blocks silent provider mismatch for required provider-routed work
- [ ] Contract defines how pinned terminals satisfy model requirements when switching is unavailable
- [ ] Contract preserves a migration path toward provider-agnostic actor routing

---

## PR-1: Fail-Closed Provider Enforcement At Dispatch Time
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0]

### Description
Make required provider routing fail closed so dispatches do not silently land on a terminal that cannot satisfy the requested provider.

### Scope
- Promote `Requires-Provider` from warning-only to enforceable routing rule where policy marks it as required
- Block dispatch when terminal provider does not match required provider
- Preserve explicit advisory mode for cases where provider preference is informational only
- Add tests for required vs optional provider routing

### Success Criteria
- Required provider mismatches no longer dispatch silently
- Optional provider preferences remain visible without overblocking
- Dispatcher reports explicit reasons for provider-route rejection
- Existing terminal start configuration remains compatible

### Quality Gate
`gate_pr1_provider_enforcement`:
- [ ] All provider-routing enforcement tests pass
- [ ] Required provider mismatch blocks dispatch with explicit reason
- [ ] Optional provider preference remains advisory and auditable
- [ ] Provider routing logic does not silently downgrade required work to the wrong terminal

---

## PR-2: Verified Model Switching And Post-Switch Runtime Validation
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-0, PR-1]

### Description
Turn model routing from best-effort command injection into a verified state transition so dispatches can prove which model actually handled the work.

### Scope
- Add explicit model-switch result states:
  - switched
  - already_active
  - unsupported
  - failed
  - unverified
- Verify post-switch runtime state where provider supports it
- Block required model-routed dispatches when the switch cannot be verified
- Preserve explicit unsupported behavior for providers that do not support runtime model switching

### Success Criteria
- Required model changes are not assumed successful without verification
- Dispatch receipts can show the requested and actual runtime model
- Unsupported runtime switching is explicit rather than silently ignored
- Model-routing failures do not continue to delivery as if nothing happened

### Quality Gate
`gate_pr2_verified_model_switching`:
- [ ] All model-routing verification tests pass
- [ ] Required model-routed dispatches fail when post-switch state cannot be verified
- [ ] Requested and actual runtime model are recorded in dispatch evidence
- [ ] Unsupported runtime switching paths are explicit and do not masquerade as success

---

## PR-3: Kickoff, Preset, And Preflight Provider Readiness
**Track**: C
**Priority**: P2
**Complexity**: Medium
**Risk**: Medium
**Skill**: @quality-engineer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-1, PR-2]

### Description
Move provider and model readiness checks earlier into kickoff and runtime preflight so T0 does not discover missing routing capabilities only after work is already underway.

### Scope
- Add preflight validation for required provider and model capabilities from feature/review metadata
- Surface missing provider capability at kickoff or promotion time
- Verify startup presets and env profiles can express required routing capabilities
- Verify pinned terminal assumptions for the current chain:
  - T1 = Sonnet
  - T2 = Sonnet
  - T0 and T3 = stronger review/orchestration model
- Add operator-readable diagnostics for why a requested provider/model is unavailable

### Success Criteria
- Required provider/model capability gaps are caught before real execution
- T0 receives deterministic readiness feedback instead of post-failure guesswork
- Presets and env profiles can intentionally provision routing requirements
- Pinned-terminal assumptions can be checked before the chain starts
- Autonomous chain features can declare provider expectations in a machine-usable way

### Quality Gate
`gate_pr3_routing_preflight_readiness`:
- [ ] All preflight readiness tests pass
- [ ] Kickoff or promotion blocks when required provider or model capability is unavailable
- [ ] Startup preset and env diagnostics explain missing routing capability clearly
- [ ] Pinned terminal assumptions are checked explicitly before the chain starts
- [ ] T0 can distinguish unsupported, unavailable, and misconfigured routing states

---

## PR-4: Certification With Real Mixed-Provider And Mixed-Model Dispatches
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @quality-engineer
**Estimated Time**: 2-3 hours
**Dependencies**: [PR-2, PR-3]

### Description
Certify routing enforcement using realistic mixed-provider and mixed-model dispatches so the next autonomous feature sequence can trust provider/model requirements.

### Scope
- Run at least one mixed-provider scenario and one mixed-model scenario
- Prove required provider mismatch blocks before delivery
- Prove verified model switching records requested vs actual runtime identity
- Require Gemini review and Codex final gate on certification and routing-core PRs

### Success Criteria
- Required provider and model routing works deterministically under real dispatch flow
- No dispatch silently stays on the old model when a required verified switch is requested
- Gemini review evidence exists and blocking findings are resolved
- Codex final gate evidence exists and passes for routing-core changes

### Quality Gate
`gate_pr4_routing_certification`:
- [ ] All routing certification tests pass for mixed-provider and mixed-model scenarios
- [ ] Required provider mismatch blocks before delivery and produces explicit evidence
- [ ] Requested and actual runtime model are both present in certification evidence
- [ ] Gemini review receipt exists and all blocking findings are closed
- [ ] Codex final gate receipt exists and all required checks pass
