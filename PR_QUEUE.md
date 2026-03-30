# PR Queue - Feature: Review Contracts, Acceptance Idempotency, And Auto-Next Trials

## Progress Overview
Total: 7 PRs | Complete: 0 | Active: 0 | Queued: 7 | Blocked: 0
Progress: ░░░░░░░░░░ 0%

## Governance Metadata
Risk-Class: high
Merge-Policy: human
Review-Stack: gemini_review,codex_gate,claude_github_optional

## Status

### ⏳ Queued PRs
- PR-0: Dispatch Acceptance Idempotency Guard (dependencies: none) [risk=high, merge=human, review=gemini_review,codex_gate,claude_github_optional]
- PR-1: Review Contract Schema And Materializer (dependencies: none) [risk=medium, merge=human, review=gemini_review,codex_gate,claude_github_optional]
- PR-2: Gemini Review Prompt Renderer And Receipt Contract (dependencies: PR-1) [risk=medium, merge=human, review=gemini_review,codex_gate,claude_github_optional]
- PR-3: Codex Final Gate Prompt Renderer And Headless Enforcement (dependencies: PR-1) [risk=high, merge=human, review=gemini_review,codex_gate,claude_github_optional]
- PR-4: Claude GitHub Review Bridge And Evidence Linkage (dependencies: PR-1) [risk=medium, merge=human, review=gemini_review,codex_gate,claude_github_optional]
- PR-5: Closure Verifier Contract Checks And Required Evidence Wiring (dependencies: PR-0, PR-2, PR-3, PR-4) [risk=high, merge=human, review=gemini_review,codex_gate,claude_github_optional]
- PR-6: Auto-Next Trial Harness And Controlled Certification (dependencies: PR-5) [risk=high, merge=human, review=gemini_review,codex_gate,claude_github_optional]

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
