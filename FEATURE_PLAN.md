# Feature: VNX Adoption, Packaging, Pythonization, And Public Onboarding

**Status**: Complete
**Priority**: P1
**Branch**: `feature/adoption-packaging-pythonization`
**Baseline**: FP-A through FP-D merged on `main`; control plane, recovery, execution modes, bounded intelligence, autonomy envelopes, and provenance controls are now available
**Runtime policy**: Preserve governance-first behavior while making VNX materially easier to install, understand, test, and market; replace bash with Python where stateful logic and packaging benefits justify it; keep tmux available for full operator mode and optional for starter/demo flows

This feature is the productization layer after the major FP-A through FP-D architecture upgrade. The system is now much stronger internally, but still too heavy, too shell-shaped, and too insider-dependent for broad adoption. This feature focuses on public onboarding, simpler execution modes, packaging, and selective Pythonization of the most failure-prone bash orchestration surfaces.

Primary objective:
Make VNX materially easier to adopt and explain without weakening governance, provenance, or runtime reliability.

Secondary objective:
Move the highest-value remaining bash-heavy logic into testable Python paths and add stricter QA/certification so adoption work does not regress the core runtime.

Estimated effort: ~16-24 engineering days across PR-0 through PR-8.

## Design Principles
- public adoption must not weaken governance
- starter mode must be simpler, not fake
- Python should replace bash where logic is branching, stateful, path-sensitive, or recovery-sensitive
- shell should remain only where it is the thinnest useful wrapper
- docs, install path, examples, and command surface must tell one coherent story
- this feature needs stricter certification than FP-A through FP-D because it touches product surface and operator ergonomics at the same time

## Governance Rules

| # | Rule | Rationale |
|---|------|-----------|
| G-R1 | **Adoption work must preserve FP-D governance controls** | Productization cannot undo safety |
| G-R2 | **Starter/demo flows must still emit receipts and traceable runtime state** | Simpler UX must not create opaque behavior |
| G-R3 | **Pythonization must be value-ranked** — migrate the most failure-prone bash logic first | Refactor effort must buy reliability |
| G-R4 | **Public docs must match actual runtime behavior** | Marketing and onboarding cannot drift from reality |
| G-R5 | **Every major UX simplification needs explicit QA evidence** | Avoid “looks easier” without operational proof |
| G-R6 | **No closure without push, PR, CI, and metadata consistency** | Lessons from FP-A through FP-D |
| G-R7 | **No fake commit-to-PR mapping in closure notes** | Closure evidence must stay truthful |
| G-R8 | **No claimed test totals without real repo test file verification** | Prevent invented or stale evidence |

## Closure And Evidence Rules

| # | Rule | Description |
|---|------|-------------|
| C-R1 | **No PR is "complete" while it exists only locally** | Push and PR state are part of closure evidence |
| C-R2 | **No PR is "merge-ready" while CI is red, unstable, or unknown** | Local green is not enough for closure |
| C-R3 | **`FEATURE_PLAN.md` and `PR_QUEUE.md` must match real execution state** | Metadata drift invalidates closure claims |
| C-R4 | **Closure notes must map commits to PRs truthfully** | Do not fake one-commit-per-PR neatness |
| C-R5 | **Claimed test suites must reference real test files and executable commands** | Prevent stale or invented test evidence |
| C-R6 | **Staging must be filtered to current-feature dispatches before promotion** | Old staged dispatches must not contaminate new feature execution |
| C-R7 | **QA/certification must include an independent adversarial review pass** | This feature touches public product surface and needs stronger skepticism |

## Architecture Rules

| # | Rule | Description |
|---|------|-------------|
| A-R1 | **Starter mode, operator mode, and demo mode share the same canonical runtime model** |
| A-R2 | **Install, bootstrap, doctor, and recover should converge on Python-led entrypoints where feasible** |
| A-R3 | **tmux remains first-class for operator mode, optional for starter/demo** |
| A-R4 | **Path resolution must stay deterministic across main repo and worktrees** |
| A-R5 | **Public CLI entrypoints must be explicit and documented** |
| A-R6 | **README and examples must reflect interactive plus headless reality post-FP-C** |
| A-R7 | **QA/certification must validate starter mode, operator mode, docs, CI, and install flows** |
| A-R8 | **Thin shell wrappers are acceptable; heavy orchestration logic should move to Python if it improves determinism** |

## Source Of Truth
- packaging and install surface: canonical public CLI entrypoints
- onboarding profiles: starter mode, operator mode, demo mode
- runtime state: existing FP-A through FP-D control plane and receipt model
- docs and examples: README, onboarding guides, comparison docs, example flows
- QA/certification evidence: test suites, smoke tests, CI checks, certification reports

## Known Failure Surface (Evidence / Problem Statement)
1. **VNX still feels too insider-only**: setup and usage assume operator familiarity with tmux and internal conventions
2. **Public onboarding is weaker than the runtime**: the system is better than the docs and install surface suggest
3. **Too much shell logic still owns important branching and path handling**: that increases fragility and makes testing harder
4. **README/product story still undersells the post-FP-D architecture**: governance and mixed execution are not explained cleanly
5. **Starter mode is missing or too implicit**: first-run experience is not good enough for broad adoption
6. **Closure discipline must improve**: FP-A through FP-D repeatedly showed that code can be good while governance metadata and closure evidence are still wrong

## What MUST NOT Be Done
1. Do NOT rip out tmux for operator mode
2. Do NOT collapse governance to make onboarding feel easier
3. Do NOT rewrite every shell script indiscriminately
4. Do NOT ship a public onboarding path that depends on undocumented tribal knowledge
5. Do NOT market VNX as a mass-market consumer product
6. Do NOT close PRs/features without push/PR/CI/metadata consistency
7. Do NOT accept self-reported completion without independent verification

## Dependency Flow
```text
PR-0 -> PR-1
PR-0 -> PR-2
PR-0 -> PR-5
PR-1, PR-2 -> PR-3
PR-2 -> PR-4
PR-3, PR-4, PR-5 -> PR-6
PR-6 -> PR-7
PR-7 -> PR-8
```

---

## PR-0: Productization Contract, User Modes, And Pythonization Matrix
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 1-2 days
**Dependencies**: []

### Description
Define what VNX is trying to become publicly, which user modes exist, and which bash-heavy areas should be migrated first.

### Scope
- Define `starter`, `operator`, and `demo` mode contracts
- Define public adoption success criteria
- Define bash-to-Python prioritization matrix ranked by reliability gain
- Identify critical path-sensitive and recovery-sensitive shell logic
- Define command-surface goals for public onboarding

### Success Criteria
- user modes are explicit and non-overlapping
- Pythonization targets are prioritized by operational value
- onboarding success criteria are measurable
- later PRs have one productization contract to implement against

### Quality Gate
`gate_pr0_productization_contract`:
- [ ] Starter, operator, and demo modes are defined clearly
- [ ] Bash-to-Python targets are prioritized by reliability value
- [ ] Public onboarding success criteria are measurable
- [ ] Public command-surface goals are explicit
- [ ] Contract is reviewable enough to anchor later QA and docs work

---

## PR-1: Python Init, Bootstrap, And Doctor Unification
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Unify bootstrap, init, and doctor under Python-led entrypoints to reduce scattered shell setup logic and improve deterministic validation.

### Scope
- Add Python entrypoints for init/bootstrap/doctor orchestration
- Migrate high-value branching from bash to Python
- Keep shell wrappers thin where needed
- Improve dependency and path validation output
- Preserve backward-compatible command names

### Success Criteria
- new users can initialize VNX through one clear flow
- doctor results are more actionable and less shell-fragile
- bootstrap logic becomes easier to test and reason about
- main repo and worktree path handling remain correct

### Quality Gate
`gate_pr1_python_bootstrap_doctor`:
- [ ] Python-led init/bootstrap/doctor entrypoints work end-to-end
- [ ] Shell wrappers remain thin and non-authoritative
- [ ] Dependency and path failures are explicit and actionable
- [ ] Main repo and worktree contexts are regression-tested
- [ ] Tests cover init and doctor flows

---

## PR-2: Starter Mode And No-tmux Demo Path
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Create a materially simpler first-run experience that does not require full tmux operator mode on day one.

### Scope
- Add starter mode profile
- Add no-tmux demo or dry-run path
- Preserve receipts and canonical runtime state
- Define exactly what starter mode can and cannot do
- Add feature flags and rollback controls for simplified modes

### Success Criteria
- first-time users can see VNX work without full operator-grid setup
- starter/demo flows remain governance-compatible
- demo path improves learnability and marketing value
- simplified modes do not fork the runtime model

### Quality Gate
`gate_pr2_starter_mode`:
- [ ] Starter mode works without full operator-grid setup
- [ ] No-tmux demo path remains traceable and receipt-producing
- [ ] Limits of starter/demo mode are explicitly documented
- [ ] Rollback controls exist for simplified-mode rollout
- [ ] Tests cover starter-mode setup and basic execution

---

## PR-3: Pythonization Of Start, Recover, And Worktree-Sensitive Logic
**Track**: B
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-1, PR-2]

### Description
Move the most failure-prone orchestration shell flows into Python where stateful logic, path resolution, and recovery semantics are easier to test and maintain.

### Scope
- Migrate critical parts of `start`, `recover`, and worktree-sensitive path logic into Python
- Reduce ambiguous env/path resolution logic
- Preserve existing command names through thin shell wrappers where needed
- Add regression tests for path and mode handling

### Success Criteria
- fewer shell-driven path and state bugs
- more deterministic start/recover behavior
- improved testability of key orchestration flows
- starter and operator modes share the same runtime truth

### Quality Gate
`gate_pr3_pythonize_critical_shell_paths`:
- [ ] Start and recover critical logic moves into Python modules
- [ ] Worktree path handling is deterministic and test-covered
- [ ] Starter and operator modes share canonical runtime expectations
- [ ] Shell wrappers stay thin
- [ ] Regressions are covered for shared path/state logic

---

## PR-4: Packaging, Install Surface, And Public CLI Entry Points
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-2]

### Description
Make VNX easier to install and invoke like a real product instead of a manually wired internal tool.

### Scope
- Standardize public CLI entrypoint strategy
- Improve install surface for clean project setup
- Reduce manual path assumptions
- Document supported install and invocation modes
- Tighten install-time validation and failure messaging

### Success Criteria
- install path is clearer and less intimidating
- public command surface is easier to explain
- fewer manual path edits are needed
- packaging story matches the actual runtime

### Quality Gate
`gate_pr4_packaging_surface`:
- [ ] Public CLI entrypoint strategy is coherent
- [ ] Install flow is clearer and less path-fragile
- [ ] Supported install and invocation modes are documented
- [ ] Install-time failures are explicit and actionable
- [ ] Tests cover public entrypoint and install assumptions

---

## PR-5: README, Positioning, And Public Comparison Rewrite
**Track**: C
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @architect
**Requires-Model**: opus
**Estimated Time**: 2-3 days
**Dependencies**: [PR-0]

### Description
Rewrite the public-facing explanation of VNX so it reflects the post-FP-D system and its real market position.

### Scope
- Rewrite README positioning and quickstart
- Add comparison pages versus OpenClaw and Claude Code
- Add target audience and use-case docs
- Align messaging with starter mode and operator mode
- Remove outdated framing that undersells the runtime

### Success Criteria
- README reflects the real post-FP-D system
- quickstart is materially clearer
- positioning is sharper for the intended audience
- comparisons are honest and differentiating

### Quality Gate
`gate_pr5_readme_and_positioning`:
- [ ] README reflects the current architecture and intended audience
- [ ] Quickstart supports a realistic first-run path
- [ ] Comparison language is clear and non-confused
- [ ] Messaging aligns with starter mode and operator mode
- [ ] Outdated architectural claims are removed

---

## PR-6: Public Example Flows And Operator Onboarding Docs
**Track**: B
**Priority**: P1
**Complexity**: Medium
**Risk**: Medium
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Estimated Time**: 2-3 days
**Dependencies**: [PR-3, PR-4, PR-5]

### Description
Ship example flows and onboarding docs that map VNX to real use cases instead of internal abstractions only.

### Scope
- Add example flow for coding orchestration
- Add example flow for structured research/headless work
- Add example flow for content or non-coding orchestration
- Add operator onboarding docs for starter and operator modes
- Ensure examples stay governance-compatible

### Success Criteria
- users can map VNX to real use cases quickly
- examples reinforce the mixed execution model
- onboarding docs reduce dependence on tribal knowledge
- examples and docs support the rewritten README

### Quality Gate
`gate_pr6_examples_and_onboarding`:
- [ ] Coding example flow is realistic and current
- [ ] Headless structured-task example is realistic and current
- [ ] Content/non-coding example shows the right orchestration model
- [ ] Operator onboarding docs cover starter and operator modes clearly
- [ ] Examples remain governance-compatible

---

## PR-7: QA, Review, And Certification Hardening
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @quality-engineer
**Requires-Model**: opus
**Estimated Time**: 2-3 days
**Dependencies**: [PR-6]

### Description
Add stricter QA and review gates than FP-A through FP-D had, specifically because this feature touches runtime behavior, install UX, docs, and public expectations at once.

### Scope
- Add stronger CI/smoke checks for starter mode, operator mode, and install flows
- Add review/certification checklist for docs correctness and command correctness
- Add path-resolution regression tests
- Add public quickstart validation
- Add feature certification report for adoption readiness
- Add an independent closure-verification pass that checks push/PR/CI/metadata consistency before acceptance

### Success Criteria
- adoption feature has stronger evidence than earlier feature bundles
- install and onboarding regressions are caught in CI
- docs and commands are checked against each other
- review/QA path is explicit instead of ad hoc

### Quality Gate
`gate_pr7_qa_and_certification`:
- [ ] CI covers starter mode, operator mode, and install/quickstart smoke paths
- [ ] Docs and public command examples are validated against actual behavior
- [ ] Path-resolution regressions are covered
- [ ] Certification report summarizes adoption readiness and residual risks
- [ ] Review and QA evidence is strong enough for public-facing rollout
- [ ] Independent closure verification catches no push/PR/CI/metadata inconsistencies

---

## PR-8: Adoption Cutover And Release Readiness
**Track**: C
**Priority**: P1
**Complexity**: High
**Risk**: High
**Skill**: @t0-orchestrator
**Requires-Model**: opus
**Estimated Time**: 1-2 days
**Dependencies**: [PR-7]

### Description
Certify that VNX is materially easier to adopt and safer to market publicly without weakening the architectural guarantees gained in FP-A through FP-D.

### Scope
- Certify starter mode, operator mode, and demo mode
- Certify Pythonized command surface and install path
- Certify docs/readme/example consistency
- Document residual adoption risks
- Confirm that governance and provenance guarantees remain intact

### Success Criteria
- VNX is materially easier to install, understand, and demonstrate
- governance and provenance guarantees remain intact
- docs, examples, and install path tell one coherent story
- release readiness is explicitly documented

### Quality Gate
`gate_pr8_adoption_release_readiness`:
- [ ] Starter, operator, and demo modes all pass certification
- [ ] Install and command surface are materially simpler than before
- [ ] Governance and provenance guarantees remain intact
- [ ] Docs, examples, and onboarding path are coherent and current
- [ ] Release-readiness report is explicit about residual risks and next steps
- [ ] Final closure evidence includes push, PR, CI, metadata, and truthful commit mapping
