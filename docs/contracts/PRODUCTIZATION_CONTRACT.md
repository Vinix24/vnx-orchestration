# VNX Productization Contract

**Version**: 1.0
**Status**: Active
**PR**: PR-0 (feature/adoption-packaging-pythonization)
**Date**: 2026-03-29
**Authority**: This contract anchors all subsequent PRs in the adoption/packaging/Pythonization feature. Implementation PRs (PR-1 through PR-8) must conform to the mode definitions, command surface goals, migration priorities, and success criteria defined here.

**Namesake in the archive**: `docs/_archive/contracts/PRODUCTIZATION_CONTRACT.md` is an earlier, superseded draft of this same contract (a three-mode design that included a demo mode). It is not a duplicate to merge back in — this file is the one that shipped.

---

## 1. Product Identity

**VNX** is a governance-first, LLM-agnostic multi-agent orchestration system for software engineering teams that need traceable, auditable, human-gated AI coordination.

**Target audience** (in priority order):
1. Solo developers managing 2-4 AI agents across parallel tracks
2. Small engineering teams (2-5 people) coordinating AI-assisted feature work
3. Compliance-aware organizations needing provenance and audit trails for AI-generated code

**What VNX is NOT**:
- A consumer AI chat wrapper
- A replacement for CI/CD
- A mass-market no-code tool

---

## 2. User Modes

VNX supports two user modes. All modes share the same canonical runtime model — they differ in surface complexity, not in underlying behavior. Receipts, provenance, and governance controls apply in all modes.

### 2.1 Starter Mode

**Purpose**: First-run experience. Get VNX working in under 5 minutes with one AI provider.

| Property | Value |
|----------|-------|
| **tmux required** | No |
| **Terminals** | Single terminal (T0 only, or headless) |
| **Providers** | One (Claude Code by default) |
| **Dispatch model** | Sequential, single-track |
| **Governance** | Receipts emitted, provenance tracked |
| **Dashboard** | Not available (no multi-terminal state to project) |
| **Worktrees** | Not available |
| **Intelligence** | Available (single-DB, no cross-worktree merge) |

**Capabilities**:
- `vnx init --starter` — initialize minimal VNX project
- `vnx doctor` — validate installation health
- `vnx status` — show current state
- `vnx recover` — recover from failures
- Dispatch creation and execution (single-track)
- Receipt generation and audit trail

**Boundaries**:
- No multi-terminal orchestration
- No profile/preset selection (single provider)
- No tmux session management
- No worktree operations
- Cannot promote to operator mode without re-init

**Exit to operator mode**: `vnx init --operator` (re-initializes with full terminal grid)

### 2.2 Operator Mode

**Purpose**: Full multi-agent orchestration with multiple providers and all governance controls.

| Property | Value |
|----------|-------|
| **tmux required** | For T0 (interactive orchestrator) and any interactive non-Claude CLI panes; Claude build workers run headless (`claude_headless`, no tmux pane) |
| **Terminals** | T0-T3 (T0 orchestrates; T1-T3 dispatch as headless workers) |
| **Providers** | Multiple (profile-selectable) |
| **Dispatch model** | Parallel multi-track (A/B/C) |
| **Governance** | Full: receipts, provenance, gates, preflight |
| **Dashboard** | Available |
| **Worktrees** | Available |
| **Intelligence** | Full (cross-worktree merge, export/import) |

**Capabilities**: All 47 current commands.

**Boundaries**: None — this is the full system.

### 2.3 Mode Detection and Switching

```
vnx init --starter     → creates .vnx-data/mode.json {"mode": "starter"}
vnx init --operator    → creates .vnx-data/mode.json {"mode": "operator"}  (default if no flag)
vnx init               → interactive prompt: starter or operator
```

Mode is stored in `.vnx-data/mode.json` and checked at command dispatch time. Commands unavailable in the current mode return a clear error with upgrade instructions.

---

## 3. Command Surface Goals

### 3.1 Current State (47 commands)

All 47 commands are available in operator mode. The public command surface must be tiered by mode.

### 3.2 Tiered Command Surface

#### Tier 1: Universal (all modes)
| Command | Description |
|---------|-------------|
| `vnx init` | Initialize VNX project (with mode selection) |
| `vnx doctor` | Validate installation health |
| `vnx status` | Show current state |
| `vnx recover` | Recover from failures |
| `vnx help` | Show available commands for current mode |
| `vnx update` | Update VNX installation |

#### Tier 2: Starter + Operator
| Command | Description |
|---------|-------------|
| `vnx staging-list` | List pending dispatches |
| `vnx promote` | Promote a dispatch |
| `vnx queue-status` | Show PR queue status |
| `vnx gate-check` | Run quality gate check |
| `vnx suggest` | Get dispatch suggestions |
| `vnx cost-report` | Show session cost report |
| `vnx analyze-sessions` | Analyze session data |
| `vnx intelligence-export` | Export intelligence DB |
| `vnx intelligence-import` | Import intelligence DB |
| `vnx init-feature` | Initialize a new feature |
| `vnx bootstrap-*` | Bootstrap sub-commands |
| `vnx regen-settings` | Regenerate settings |
| `vnx patch-agent-files` | Patch CLAUDE.md / AGENTS.md |
| `vnx register` / `vnx list-projects` / `vnx unregister` | Project registry |
| `vnx install-git-hooks` / `vnx uninstall-git-hooks` | Git hook management |
| `vnx install-shell-helper` | Shell integration |

#### Tier 3: Operator Only
| Command | Description |
|---------|-------------|
| `vnx start` | Launch the T0 orchestration session |
| `vnx stop` | Stop tmux session |
| `vnx restart` | Restart session |
| `vnx jump` | Navigate to terminal |
| `vnx ps` | Show VNX processes |
| `vnx cleanup` | Clean up orphan processes |
| `vnx new-worktree` | Create git worktree |
| `vnx finish-worktree` | Finish and merge worktree |
| `vnx worktree-start` / `worktree-stop` / `worktree-refresh` / `worktree-status` | Worktree management |
| `vnx merge-preflight` | Pre-merge governance check |
| `vnx smoke` | Run smoke tests |
| `vnx package-check` | Package integrity check |
| `vnx init-db` | Initialize database |

### 3.3 Command Gating

When a user runs a Tier 3 command in starter mode:
```
$ vnx start
Error: 'vnx start' requires operator mode (current: starter).
Run 'vnx init --operator' to upgrade, or 'vnx help' for available commands.
```

---

## 4. Public Adoption Success Criteria

### 4.1 Onboarding Metrics (measurable)

| Criterion | Target | Measurement |
|-----------|--------|-------------|
| **Time to first working state** (starter mode) | < 5 minutes | From `git clone` to `vnx status` showing healthy state |
| **Time to first dispatch** (starter mode) | < 10 minutes | From init to first dispatch created and executed |
| **Time to operator mode** (from starter) | < 15 minutes | From `vnx init --operator` to a running operator-mode session |
| **Install commands required** | ≤ 3 | Clone, init, start (or clone, init for starter) |
| **Manual path edits required** | 0 | No user editing of PATH, config files, or env vars |
| **Doctor pass rate on clean install** | 100% | `vnx doctor` exits 0 on supported platforms |
| **README-to-working-state fidelity** | 100% | Every quickstart command in README works as documented |

### 4.2 Documentation Criteria

| Criterion | Target |
|-----------|--------|
| README explains both modes | Yes, with quickstart for each |
| Comparison vs raw Claude Code | Honest, differentiating |
| Comparison vs OpenClaw / similar | Honest, differentiating |
| Example flows cover coding + non-coding | At least 3 example flows |
| All public commands documented | `vnx help` output matches docs |

### 4.3 Packaging Criteria

| Criterion | Target |
|-----------|--------|
| Single install method works | `git clone` + `vnx init` |
| No hidden dependencies | `vnx doctor` catches all missing deps |
| Works in main repo and worktrees | Path resolution deterministic in both |
| CI validates install flow | Smoke test in CI |
| Version reporting | `vnx --version` returns meaningful version |

### 4.4 Governance Preservation Criteria

| Criterion | Target |
|-----------|--------|
| Starter mode emits receipts | Yes — verified in tests |
| Provenance tracking in all modes | Yes — no mode bypasses provenance |
| Mode cannot be silently changed | Mode stored in `.vnx-data/mode.json`, checked at dispatch |
| Audit trail covers mode transitions | Mode changes logged in receipt stream |

---

## 5. Path Resolution Contract

Path resolution is the single most fragile surface in VNX. This contract locks the rules.

### 5.1 Resolution Rules

1. **Script location is ground truth**: `PROJECT_ROOT` derives from `bin/vnx` location, never from environment.
2. **Worktree override**: If CWD is a git worktree of the same project, `PROJECT_ROOT` overrides to CWD and all data paths re-derive.
3. **Explicit env override**: If `VNX_DATA_DIR` is explicitly set and does not match the main repo default, it is preserved (worktree isolation).
4. **No relative paths**: All VNX paths are absolute after resolution.
5. **No inherited env**: `PROJECT_ROOT`, `VNX_HOME`, `VNX_DATA_DIR` are unset and recomputed on every CLI invocation.

### 5.2 Path Variables

| Variable | Derivation | Override allowed |
|----------|-----------|-----------------|
| `VNX_HOME` | `bin/vnx/../../` (VNX system dir) | No |
| `PROJECT_ROOT` | Parent of VNX_HOME, or CWD if worktree | Worktree auto-override only |
| `VNX_DATA_DIR` | `$PROJECT_ROOT/.vnx-data` | Yes (explicit env) |
| `VNX_STATE_DIR` | `$VNX_DATA_DIR/state` | No |
| `VNX_DISPATCH_DIR` | `$VNX_DATA_DIR/dispatches` | No |
| `VNX_INTELLIGENCE_DIR` | `$PROJECT_ROOT/.vnx-intelligence` | No |

### 5.3 Migration Impact

When path resolution moves to Python (`vnx_common.py`), these rules become enforced by a `VNXPaths` dataclass with validation. Shell wrappers call Python to resolve paths rather than reimplementing resolution.

---

## 6. Runtime Model Invariants

These invariants hold across all modes. No PR in this feature may violate them.

1. **Single state source**: `.vnx-data/state/` is canonical. Dashboard and CLI read from here.
2. **Receipt completeness**: Every dispatch execution produces a receipt, in every mode.
3. **Provenance chain**: Every code change traces to a dispatch, in every mode.
4. **Mode transparency**: The current mode is always queryable via `vnx status`.
5. **No silent degradation**: If a governance control cannot run (e.g., no tmux for preflight), the command fails explicitly rather than skipping the check.
6. **Atomic state transitions**: State files are written atomically (temp + rename) in Python paths.
7. **Idempotent init**: Running `vnx init` on an already-initialized project is safe and non-destructive.

---

## 7. Risk Register

| Risk | Severity | Mitigation |
|------|----------|------------|
| Starter mode feels too limited, users skip to operator before ready | Medium | Clear documentation of what starter enables; graduated command unlocking |
| Python migration introduces regressions in operator mode | High | Test-before-demote rule; shell originals kept as fallback during transition |
| Path resolution diverges between shell and Python during migration | High | Single `vnx_common.py` resolver; shell wrappers call Python for paths |
| Mode detection adds latency to every command | Low | Mode file is a single JSON read; < 1ms |
| Packaging story (git clone) too primitive for enterprise adoption | Medium | Future: consider pip install / brew; out of scope for this feature |

---

## 8. Contract Boundary

This contract covers the productization, mode, command surface, and migration design. It does NOT cover:
- Specific Python implementation details (PR-1, PR-3, PR-4)
- README/positioning content (PR-5)
- Example flow content (PR-6)
- QA/certification methodology (PR-7)
- Release criteria (PR-8)

Those PRs implement against this contract. Changes to the contract require a dispatch and T0 review.
