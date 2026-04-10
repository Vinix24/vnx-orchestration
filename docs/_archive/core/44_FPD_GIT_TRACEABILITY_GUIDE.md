# FP-D Git Traceability Guide — Operator And Worker Expectations

**Feature**: FP-D — Safe Autonomy, Governance Envelopes, And End-To-End Provenance
**PR**: PR-3
**Status**: Canonical
**Purpose**: Documents how Git-native traceability works, what operators and workers need to do, and how enforcement is configured. CLI-agnostic by design.

---

## 1. Overview

VNX enforces Git traceability through three independent layers:

| Layer | Purpose | Depends On CLI? | Bypassable? |
|---|---|---|---|
| **Local git hooks** | Assist: auto-inject and warn | No — reads `$VNX_CURRENT_DISPATCH_ID` env var | Yes (`--no-verify`) |
| **CI validation** | Durable backstop: scan PR commits | No — runs `git log` | No (requires repo admin) |
| **Receipt validation** | Runtime: cross-check receipt ↔ commit | No — reads receipt fields | No |

No single layer is primary. Defense in depth ensures traceability survives tool changes (G-R8, A-R6).

---

## 2. For Workers (T1, T2, T3)

### 2.1 Automatic Flow (Recommended)

If VNX git hooks are installed and `VNX_CURRENT_DISPATCH_ID` is set:

1. Write your code
2. `git commit` — the `prepare-commit-msg` hook auto-appends `Dispatch-ID: <id>`
3. The `commit-msg` hook validates the token is present
4. Done — no manual action needed

### 2.2 Manual Flow

If hooks are not installed or dispatch context is not in the environment:

1. Add `Dispatch-ID: <dispatch-id>` on its own line in the **commit message body**
2. The dispatch ID is in your dispatch file or `$VNX_CURRENT_DISPATCH_ID`

Example:
```
feat(governance): implement policy evaluator

Add runtime policy evaluation against the canonical matrix.

Dispatch-ID: 20260329-180606-governance-evaluation-engine-a-B
```

### 2.3 What If I Forget?

- **Shadow mode** (default): You get a warning but the commit proceeds
- **Enforcement mode**: The commit is blocked. Add the token and retry
- **CI catch**: Even if local hooks are bypassed, CI will flag missing tokens

### 2.4 Legacy Formats

During transition, these are accepted:
- `dispatch:<id>` inline in the message
- `PR-N` in the subject line (e.g., `feat(scope): PR-3 description`)
- `FP-X` in the subject line (e.g., `fix(scope): close FP-D gaps`)

New commits should use `Dispatch-ID:` format. Legacy acceptance will be sunset after FP-D stabilization.

---

## 3. For Operators (T0)

### 3.1 Installing Hooks

```bash
vnx install-git-hooks     # Install hooks as symlinks
vnx uninstall-git-hooks   # Remove VNX hooks, restore backups
```

Hooks are symlinked from `$VNX_HOME/hooks/git/` into `.git/hooks/`. Updates to VNX propagate automatically.

### 3.2 Enforcement Configuration

| Variable | Values | Default | Effect |
|---|---|---|---|
| `VNX_PROVENANCE_ENFORCEMENT` | `0` / `1` | `0` | `0` = warn only, `1` = block commits |
| `VNX_PROVENANCE_LEGACY_ACCEPTED` | `0` / `1` | `1` | `0` = preferred format only |
| `VNX_CURRENT_DISPATCH_ID` | dispatch ID | unset | Auto-injected by `prepare-commit-msg` |

### 3.3 Rollout Phases

| Phase | `VNX_PROVENANCE_ENFORCEMENT` | Behavior |
|---|---|---|
| **Shadow** (current) | `0` | Hooks warn, CI reports, commits not blocked |
| **Enforcement** | `1` | Hooks block, CI fails on missing tokens |
| **Rollback** | `0` | Return to shadow mode |

### 3.4 Bypass Handling

`--no-verify` bypasses local hooks. This is an **explicit governance event**, not a silent skip:
- The bypass is detectable by CI (commit will lack a trace token)
- Receipt validation can flag the gap
- CI logs the missing token for audit

### 3.5 CI Check

The CI trace token check runs automatically on PRs. It scans all new commits for trace tokens and reports:
- Per-commit status (valid/legacy/missing)
- Summary counts
- Failed commit SHAs for remediation

---

## 4. Hook Architecture

```
hooks/git/
├── prepare-commit-msg    # Auto-inject Dispatch-ID from env
└── commit-msg            # Validate trace token presence

scripts/lib/
└── trace_token_validator.py   # Shared validation library

scripts/
└── ci_trace_token_check.sh    # CI validation script
```

### 4.1 prepare-commit-msg

- Reads `VNX_CURRENT_DISPATCH_ID` environment variable
- If set, appends `Dispatch-ID: <id>` to commit message
- If token already present, does nothing
- Skips merge and squash commits
- Falls back to inline append if Python validator not found

### 4.2 commit-msg

- Validates trace token presence using the shared validator
- Shadow mode: emits warning, allows commit
- Enforcement mode: blocks commit on missing token
- Falls back to inline regex checks if Python validator not found
- Never blocks due to tooling errors (validator failure = pass-through)

### 4.3 CI Script

- Runs `git log` to find commits between base and HEAD
- Validates each commit message for trace tokens
- Reports per-commit and summary results
- Respects enforcement mode for exit code

---

## 5. Troubleshooting

### Hook not firing
- Check: `ls -la .git/hooks/commit-msg` — should be a symlink to VNX
- Run: `vnx install-git-hooks` to reinstall

### Token not auto-injected
- Check: `echo $VNX_CURRENT_DISPATCH_ID` — must be set
- VNX sets this when a dispatch is active

### CI failing on old commits
- Legacy commits before FP-D cutover are exempt
- CI only checks commits after the merge base with the target branch

### Want to bypass for a specific commit
- Use `--no-verify` — this is an accepted override mechanism
- The bypass will be caught by CI and flagged in the trace token report
