# PR Queue - Feature: Review Contracts, Acceptance Idempotency, And Auto-Next Trials

## Progress Overview
Total: 7 PRs | Complete: 7 | Active: 0 | Queued: 0 | Blocked: 0
Progress: ██████████ 100%

## Governance Metadata
Risk-Class: high
Merge-Policy: human
Review-Stack: gemini_review,codex_gate,claude_github_optional

## Status

### ✅ Completed PRs
- PR-0: Dispatch Acceptance Idempotency Guard [risk=high, merge=human]
- PR-1: Review Contract Schema And Materializer [risk=medium, merge=human]
- PR-2: Gemini Review Prompt Renderer And Receipt Contract [risk=medium, merge=human]
- PR-3: Codex Final Gate Prompt Renderer And Headless Enforcement [risk=high, merge=human]
- PR-4: Claude GitHub Review Bridge And Evidence Linkage [risk=medium, merge=human]
- PR-5: Closure Verifier Contract Checks And Required Evidence Wiring [risk=high, merge=human]
- PR-6: Auto-Next Trial Harness And Controlled Certification [risk=high, merge=human]

## Dependency Flow
```
PR-0 (no dependencies)
PR-1 (no dependencies)
PR-1 → PR-2
PR-1 → PR-3
PR-1 → PR-4
PR-0, PR-2, PR-3, PR-4 → PR-5
PR-5 → PR-6
```
