# Contributing to VNX

VNX is MIT-licensed. See [LICENSE](./LICENSE).

## What we accept

- Bug fixes with a clear reproduction case
- Documentation and typo fixes
- New tests for existing behavior

For anything larger (new features, architecture changes, provider adapters): open an issue first. This keeps scope discussions out of code review.

## VNX coding standards

These patterns have caused real production incidents. CI enforces them.

### Atomic file writes

Write state files via a tmp-then-rename pattern, never directly:

```python
# Correct
with tempfile.NamedTemporaryFile("w", dir=target.parent, delete=False, suffix=".tmp") as f:
    json.dump(data, f)
    tmp = f.name
os.replace(tmp, target)

# Also correct — state_writer handles this
state_writer.append_locked(target, record)

# Wrong — data corruption on crash
with open(target, "w") as f:
    json.dump(data, f)
```

Reference: PR #483-486 (state_writer.append_locked), which fixed a race condition in production.

### No silent exceptions

Catching every exception and passing silently hides bugs:

```python
# Wrong — hides every error including programming mistakes
try:
    process(item)
except Exception:
    pass

# Correct — narrow the exception or log + re-raise
try:
    process(item)
except ValueError as e:
    logger.warning("Skipping invalid item: %s", e)
    raise
```

Reference: PR #479 (gate reviewer fail-loud).

To suppress CI on a specific line where silent handling is genuinely correct:

```python
except Exception:  # noqa: vnx-silent-except
    pass
```

### No Anthropic SDK imports

Claude is invoked via the CLI binary only:

```python
# Correct
subprocess.Popen(["claude", "-p", "--output-format", "stream-json"], ...)

# Wrong — violates ADR-003, risks account ban
import anthropic
client = anthropic.Anthropic()
```

Reference: ADR-003 in `docs/governance/decisions/ADR-003-oauth-only-claude-routing.md` and CI gate from PR #439.

### No TODO/FIXME in committed code

Either implement it or don't start it. Incomplete work stays on a branch.

## PR checklist

Before requesting review:

- [ ] Tests added or updated: `python3 -m pytest tests/<related>`
- [ ] `gh pr checks` shows VNX CI workflow conclusion = success
- [ ] No new TODO/FIXME in committed files
- [ ] No new `except Exception: pass` (or justified with `# noqa: vnx-silent-except`)
- [ ] No new direct `open(..., "w")` for state files without tmp-then-rename

## CI gates

Every PR runs two automated review gates in addition to the test suite:

- **codex_gate** — static analysis, may post blocking findings as PR comments
- **gemini_review** — adversarial review, may post blocking findings as PR comments

Respond to blocking findings by amending the PR. The gates re-run on each push.

**First-time contributors:** CI requires maintainer approval before running on external PRs. This is a GitHub security policy for public repos, not a manual delay.

## Development workflow

```bash
# Create a worktree for your branch
vnx new-worktree my-feature --branch feature/my-feature
cd ../your-project-wt-my-feature

# Start VNX session
vnx start

# All changes go through dispatches
# T0 creates dispatches, workers execute scoped tasks

# Pre-merge validation
vnx merge-preflight my-feature
vnx gate-check --pr PR-X

# Close worktree when done
vnx finish-worktree my-feature --delete-branch
```

All shell changes must pass `bash -n`. PRs should be 150-300 lines of diff.

## Where to ask

Open a GitHub issue or comment on an existing PR. Issues are the right place for design questions before writing code.
