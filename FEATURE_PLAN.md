# F40 Feature Plan — Business Agent Integration

**Created**: 2026-04-11
**Status**: Completed
**Branch**: feat/f40-interactive (Track A) | feat/f40-headless (Track B)
**Goal**: Create reusable business agent templates with light governance profile

---

## PR-1: Agent Directory Structure + CLAUDE.md Templates
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~150
**Status**: Completed

Create agent directories with CLAUDE.md skill files:
- `agents/blog-writer/CLAUDE.md` — content creation agent that writes blog posts from topic/brief
- `agents/linkedin-writer/CLAUDE.md` — LinkedIn post agent with tone/format constraints
- `agents/research-analyst/CLAUDE.md` — research agent that gathers and summarizes information
- Each CLAUDE.md defines: role, capabilities, output format, quality criteria, report template
- Each agent dir gets a `config.yaml` with governance profile override (`light`)

**Success criteria**:
- [x] 3 agent directories exist under `agents/`
- [x] Each has a CLAUDE.md with clear role definition and constraints
- [x] Each has a config.yaml pointing to `light` governance profile
- [x] Subprocess adapter resolves agent dirs correctly (agents/{role}/ cwd)

## PR-2: Agent Dispatch Routing + Integration Tests
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~200
**Status**: Completed

Wire agent directories into the dispatch system:
- Update `subprocess_dispatch.py` to detect agent config.yaml and apply governance profile
- Add `--agent` flag to dispatch CLI: `vnx dispatch --agent blog-writer --instruction "Write about X"`
- Integration test: dispatch to blog-writer agent, verify it runs with light profile
- Integration test: dispatch to research-analyst, verify isolation (can't access coding files)
- Update `folder_scope.py` to enforce agent isolation via governance_profiles.yaml scopes

**Success criteria**:
- [x] `vnx dispatch --agent blog-writer` works end-to-end
- [x] Agent runs with light governance profile (exception-only review)
- [x] Agent output lands in correct unified_reports/ location
- [x] Folder scope isolation prevents cross-scope access
- [x] Integration tests pass

## PR-3: End-to-End Demonstration + Documentation
**Track**: A (T1 backend-developer)
**Estimated LOC**: ~100
**Status**: Completed

- Run each agent on a real task and capture the output
- Add `docs/guides/AGENT_CREATION_GUIDE.md` — how to create custom agents
- Update FEATURE_PLAN.md status to completed
- Verify all receipts, reports, and governance records are complete

**Success criteria**:
- [x] Agent creation guide exists (`docs/guides/AGENT_CREATION_GUIDE.md`)
- [x] All 3 agents verified: CLAUDE.md + config.yaml present
- [x] Governance profiles resolve correctly (light, exception_only, gates=[ci])
- [x] FEATURE_PLAN.md updated to completed
