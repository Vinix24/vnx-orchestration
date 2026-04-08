# VNX Productization Contract

**Version**: 1.0
**Status**: Active
**PR**: PR-0 (feature/adoption-packaging-pythonization)
**Date**: 2026-03-29
**Authority**: This contract anchors all subsequent PRs in the adoption/packaging/Pythonization feature. Implementation PRs (PR-1 through PR-8) must conform to the mode definitions, command surface goals, migration priorities, and success criteria defined here.

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

VNX supports three user modes. All modes share the same canonical runtime model — they differ in surface complexity, not in underlying behavior. Receipts, provenance, and governance controls apply in all modes.

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

**Purpose**: Full multi-agent orchestration with tmux grid, multiple providers, and all governance controls.

| Property | Value |
|----------|-------|
| **tmux required** | Yes |
| **Terminals** | T0-T3 (4-terminal grid) |
| **Providers** | Multiple (profile-selectable) |
| **Dispatch model** | Parallel multi-track (A/B/C) |
| **Governance** | Full: receipts, provenance, gates, preflight |
| **Dashboard** | Available |
| **Worktrees** | Available |
| **Intelligence** | Full (cross-worktree merge, export/import) |

**Capabilities**: All 47 current commands.

**Boundaries**: None — this is the full system.

### 2.3 Demo Mode

**Purpose**: Showcase VNX capabilities without requiring a real project or persistent state. For marketing, talks, and evaluation.

| Property | Value |
|----------|-------|
| **tmux required** | No (optional for visual demo) |
| **Terminals** | Simulated or single |
| **Providers** | None required (uses dry-run / recorded flows) |
| **Dispatch model** | Replay or dry-run |
| **Governance** | Receipts emitted to temp directory |
| **Dashboard** | Available (read-only, with sample state) |
| **Worktrees** | Not available |
| **Intelligence** | Sample data only |

**Capabilities**:
- `vnx demo` — launch demo with sample dispatches and state
- `vnx demo --dashboard` — launch dashboard with sample data
- `vnx demo --replay <scenario>` — replay a recorded orchestration flow
- Dashboard visualization with pre-built state

**Boundaries**:
- No real dispatch execution
- No persistent state changes to user project
- Temp directory for all runtime artifacts (cleaned on exit)
- Cannot transition to starter/operator without `vnx init`

### 2.4 Mode Detection and Switching

```
vnx init --starter     → creates .vnx-data/mode.json {"mode": "starter"}
vnx init --operator    → creates .vnx-data/mode.json {"mode": "operator"}  (default if no flag)
vnx init               → interactive prompt: starter or operator
vnx demo               → no init required; uses temp state
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
| `vnx start` | Launch tmux session with grid |
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

#### Tier 4: Demo Only
| Command | Description |
|---------|-------------|
| `vnx demo` | Launch demo mode |
| `vnx demo --dashboard` | Demo with dashboard |
| `vnx demo --replay <scenario>` | Replay recorded flow |

### 3.3 Command Gating

When a user runs a Tier 3 command in starter mode:
```
$ vnx start
Error: 'vnx start' requires operator mode (current: starter).
Run 'vnx init --operator' to upgrade, or 'vnx help' for available commands.
```

---

## 4. Bash-to-Python Prioritization Matrix

Scripts ranked by fragility score (methodology: weighted composite of path sensitivity, branching complexity, state management, error recovery, and testability). Higher score = migrate first.

### 4.1 Migration Priority Table

| Priority | Script | Lines | Fragility | Key Failure Modes | Migration Target |
|----------|--------|-------|-----------|-------------------|-----------------|
| **P1** | `start.sh` | 757 | 8.9 | Pane ID stability, race conditions in state writes, silent intelligence failures, no atomic JSON writes, broad pkill patterns | `scripts/lib/vnx_start.py` |
| **P2** | `recover.sh` | 350 | 8.6 | Lock age races, embedded Python one-liners, concurrent recovery corruption, no rollback | `scripts/lib/vnx_recover_legacy.py` (complement to existing `vnx_recover_runtime.py`) |
| **P3** | `new_worktree.sh` | 300 | 8.4 | Env variable save/restore bugs, orphaned worktrees on partial failure, no name validation, non-atomic bootstrap | `scripts/lib/vnx_worktree.py` |
| **P4** | `finish_worktree.sh` | 272 | 8.2 | Intelligence merged before removal confirmed, force mode discards without confirmation, no transaction | `scripts/lib/vnx_worktree.py` (same module) |
| **P5** | `merge_preflight.sh` | 318 | 8.0 | Silent JSON parsing failures, stale gate results accepted, incomplete blocker detection | `scripts/lib/vnx_preflight.py` |
| **P6** | `doctor.sh` | 340 | 7.8 | Fragile date parsing, external script validation gaps, worktree check incompleteness | `scripts/lib/vnx_doctor.py` |
| **P7** | `bin/vnx` (dispatcher) | 1800 | 7.5 | Path resolution order, env variable races, silent command load failures | `scripts/lib/vnx_cli.py` (Python CLI with thin shell entry) |
| **P8** | `jump.sh` | 170 | 7.2 | Stale pane IDs, reheal side effects, silent fallback to wrong terminal | `scripts/lib/vnx_jump.py` |
| **P9** | `regen_settings.sh` | 104 | 6.5 | Already delegates to Python; thin wrapper sufficient | Keep as shell wrapper |
| **P10** | `registry.sh` | 164 | 6.2 | Path resolution loose, no dedup | `scripts/lib/vnx_registry.py` |
| **P11** | `install_git_hooks.sh` | 103 | 5.8 | Symlink validation weak | Keep as shell wrapper |
| **P12** | `stop.sh` | 28 | 5.1 | Trivial | Keep as shell wrapper |

### 4.2 Migration Phases (maps to PRs)

| Phase | PRs | Scripts | Rationale |
|-------|-----|---------|-----------|
| **Phase 1** | PR-1 | `doctor.sh`, bootstrap/init logic from `bin/vnx` | Unify init/bootstrap/doctor under Python; immediate onboarding reliability |
| **Phase 2** | PR-3 | `start.sh`, `recover.sh`, worktree logic | Session lifecycle — highest fragility, most state management |
| **Phase 3** | PR-4 | `bin/vnx` dispatcher (partial), CLI entrypoints | Packaging and install surface |
| **Phase 4** | Future | `jump.sh`, `merge_preflight.sh`, remaining | Lower urgency; can follow adoption feature |

### 4.3 Migration Rules

1. **Shell wrapper pattern**: Every migrated command retains a thin bash wrapper that calls the Python entrypoint. Command names do not change.
2. **No big-bang rewrite**: Each script migrates independently. Mixed bash/Python is acceptable during transition.
3. **Test-before-migrate**: Each migration must include regression tests for the Python replacement before the shell version is demoted.
4. **Shared library**: Common utilities (path resolution, JSON state I/O, mode detection) go in `scripts/lib/vnx_common.py`.
5. **Atomic state writes**: All Python replacements must use temp-file-then-rename for JSON state files.

---

## 5. Public Adoption Success Criteria

### 5.1 Onboarding Metrics (measurable)

| Criterion | Target | Measurement |
|-----------|--------|-------------|
| **Time to first working state** (starter mode) | < 5 minutes | From `git clone` to `vnx status` showing healthy state |
| **Time to first dispatch** (starter mode) | < 10 minutes | From init to first dispatch created and executed |
| **Time to operator mode** (from starter) | < 15 minutes | From `vnx init --operator` to running tmux grid |
| **Install commands required** | ≤ 3 | Clone, init, start (or clone, init for starter) |
| **Manual path edits required** | 0 | No user editing of PATH, config files, or env vars |
| **Doctor pass rate on clean install** | 100% | `vnx doctor` exits 0 on supported platforms |
| **README-to-working-state fidelity** | 100% | Every quickstart command in README works as documented |

### 5.2 Documentation Criteria

| Criterion | Target |
|-----------|--------|
| README explains all three modes | Yes, with quickstart for each |
| Comparison vs raw Claude Code | Honest, differentiating |
| Comparison vs OpenClaw / similar | Honest, differentiating |
| Example flows cover coding + non-coding | At least 3 example flows |
| All public commands documented | `vnx help` output matches docs |

### 5.3 Packaging Criteria

| Criterion | Target |
|-----------|--------|
| Single install method works | `git clone` + `vnx init` |
| No hidden dependencies | `vnx doctor` catches all missing deps |
| Works in main repo and worktrees | Path resolution deterministic in both |
| CI validates install flow | Smoke test in CI |
| Version reporting | `vnx --version` returns meaningful version |

### 5.4 Governance Preservation Criteria

| Criterion | Target |
|-----------|--------|
| Starter mode emits receipts | Yes — verified in tests |
| Demo mode emits receipts (to temp) | Yes — verified in tests |
| Provenance tracking in all modes | Yes — no mode bypasses provenance |
| Mode cannot be silently changed | Mode stored in `.vnx-data/mode.json`, checked at dispatch |
| Audit trail covers mode transitions | Mode changes logged in receipt stream |

---

## 6. Path Resolution Contract

Path resolution is the single most fragile surface in VNX. This contract locks the rules.

### 6.1 Resolution Rules

1. **Script location is ground truth**: `PROJECT_ROOT` derives from `bin/vnx` location, never from environment.
2. **Worktree override**: If CWD is a git worktree of the same project, `PROJECT_ROOT` overrides to CWD and all data paths re-derive.
3. **Explicit env override**: If `VNX_DATA_DIR` is explicitly set and does not match the main repo default, it is preserved (worktree isolation).
4. **No relative paths**: All VNX paths are absolute after resolution.
5. **No inherited env**: `PROJECT_ROOT`, `VNX_HOME`, `VNX_DATA_DIR` are unset and recomputed on every CLI invocation.

### 6.2 Path Variables

| Variable | Derivation | Override allowed |
|----------|-----------|-----------------|
| `VNX_HOME` | `bin/vnx/../../` (VNX system dir) | No |
| `PROJECT_ROOT` | Parent of VNX_HOME, or CWD if worktree | Worktree auto-override only |
| `VNX_DATA_DIR` | `$PROJECT_ROOT/.vnx-data` | Yes (explicit env) |
| `VNX_STATE_DIR` | `$VNX_DATA_DIR/state` | No |
| `VNX_DISPATCH_DIR` | `$VNX_DATA_DIR/dispatches` | No |
| `VNX_INTELLIGENCE_DIR` | `$PROJECT_ROOT/.vnx-intelligence` | No |

### 6.3 Migration Impact

When path resolution moves to Python (`vnx_common.py`), these rules become enforced by a `VNXPaths` dataclass with validation. Shell wrappers call Python to resolve paths rather than reimplementing resolution.

---

## 7. Runtime Model Invariants

These invariants hold across all modes. No PR in this feature may violate them.

1. **Single state source**: `.vnx-data/state/` is canonical. Dashboard and CLI read from here.
2. **Receipt completeness**: Every dispatch execution produces a receipt, in every mode.
3. **Provenance chain**: Every code change traces to a dispatch, in every mode.
4. **Mode transparency**: The current mode is always queryable via `vnx status`.
5. **No silent degradation**: If a governance control cannot run (e.g., no tmux for preflight), the command fails explicitly rather than skipping the check.
6. **Atomic state transitions**: State files are written atomically (temp + rename) in Python paths.
7. **Idempotent init**: Running `vnx init` on an already-initialized project is safe and non-destructive.

---

## 8. Risk Register

| Risk | Severity | Mitigation |
|------|----------|------------|
| Starter mode feels too limited, users skip to operator before ready | Medium | Clear documentation of what starter enables; graduated command unlocking |
| Python migration introduces regressions in operator mode | High | Test-before-demote rule; shell originals kept as fallback during transition |
| Path resolution diverges between shell and Python during migration | High | Single `vnx_common.py` resolver; shell wrappers call Python for paths |
| Demo mode creates false expectations about system complexity | Medium | Demo clearly labeled; shows governance overhead, not just happy path |
| Mode detection adds latency to every command | Low | Mode file is a single JSON read; < 1ms |
| Packaging story (git clone) too primitive for enterprise adoption | Medium | Future: consider pip install / brew; out of scope for this feature |

---

## 9. Contract Boundary

This contract covers the productization, mode, command surface, and migration design. It does NOT cover:
- Specific Python implementation details (PR-1, PR-3, PR-4)
- README/positioning content (PR-5)
- Example flow content (PR-6)
- QA/certification methodology (PR-7)
- Release criteria (PR-8)

Those PRs implement against this contract. Changes to the contract require a dispatch and T0 review.
