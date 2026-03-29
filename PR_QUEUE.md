# PR Queue - Feature: VNX Runtime Recovery, tmux Hardening, And Operability

## Progress Overview
Total: 6 PRs | Complete: 6 | Active: 0 | Queued: 0 | Blocked: 0
Progress: ██████████ 100%

## Status

### ✅ Completed PRs
- PR-0: Incident Taxonomy, Recovery Contracts, And Certification Matrix
- PR-1: Durable Incident Log, Retry Budgets, And Cooldown Shadow Path
- PR-2: Workflow Supervisor, Dead-Letter Routing, And Escalation Semantics
- PR-3: Declarative tmux Session Profiles, Remap, And Operator Shell Hardening
- PR-4: `vnx doctor` Hardening And Recovery Preflight
- PR-5: `vnx recover` Operator Flow, Cutover, And FP-B Certification

## Dependency Flow
```
PR-0 (no dependencies)
PR-0 → PR-1
PR-0 → PR-2
PR-0 → PR-3
PR-1, PR-2, PR-3 → PR-4
PR-2, PR-3, PR-4 → PR-5
```
