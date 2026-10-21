# Getting Started (VNX)

**Status**: Active
**Last Updated**: 2026-10-18
**Owner**: T-MANAGER
**Purpose**: Quick orientation and links to the current VNX "source of truth" docs.

---

## Start Here

New to VNX? Follow [`docs/onboarding/ONBOARDING_GUIDE.md`](../onboarding/ONBOARDING_GUIDE.md)
end to end: pip install, first dispatch, then the repo-local operator path
(`./bin/vnx`), feature worktrees, and troubleshooting.

For a five-minute version of the pip-only path, see [`docs/QUICKSTART.md`](../QUICKSTART.md).

## Current System Snapshot

- Architecture: `00_VNX_ARCHITECTURE.md`
- Dispatch workflow: `../DISPATCH_GUIDE.md`
- Dispatch rules + lanes: `DISPATCH_RULES.md`, `PROVIDER_LANES.md`
- Monitoring/ops: `../operations/CONTROL_CENTRE.md`
- Receipt pipeline: `../operations/RECEIPT_PIPELINE.md`
- T0 context rotation (guard and spawner): `../operations/T0_CONTEXT_ROTATION.md`
- Runtime rollback: `../operations/RUNTIME_CORE_ROLLBACK.md`
- Product modes: `../contracts/PRODUCTIZATION_CONTRACT.md`

For full navigation, start at `../DOCS_INDEX.md`.

---

## VNX CLI Quick Reference

```bash
# Initialize VNX in a new project with the pip CLI
vnx init

# Health check
vnx doctor

# Project status
vnx status

# Merge readiness of a PR: required contexts and gate evidence on its head
vnx pr-ready <PR>

# Model provider state; the write verbs (on/off/reset) refuse until every launch function calls provider_switch.require (see PROVIDER_LANES.md, "Provider switch")
vnx providers list
```

```bash
# Launch the T0 orchestrator in a tmux session, from a repo checkout
./bin/vnx start

# Stop all processes
./bin/vnx stop

# Token cost report
./bin/vnx cost-report

# Operator recovery
./bin/vnx recover
```

`./bin/vnx start` opens a tmux session with a single T0 orchestrator pane, not
a fixed T1-T3 grid. T1/T2/T3 workers run as ephemeral per-dispatch processes
through the single-entry door (`./bin/vnx dispatch`) unless a terminal opts
into a terminal-pinned worker with `VNX_ADAPTER_T{n}=subprocess`; see
`DISPATCH_RULES.md` §8.

The full pip-vs-bash command list and the two-binary split live in one place:
[Onboarding Guide, Appendix A](../onboarding/ONBOARDING_GUIDE.md#appendix-a-two-binaries-and-the-full-pip-cli-surface).

### T0 Context Rotation

A T0 session that crosses 500K tokens of context must rotate to a fresh session. Two parts
make that work: a guard hook that blocks new work, and a spawner that opens the successor.
Both shipped in a release (#1923). Two later changes are on main and not yet released: `vnx guard install|check` (#2078) and the wrap-proof check in the spawner (#2076). The full contract, with the bands and every exit code, is
[`../operations/T0_CONTEXT_ROTATION.md`](../operations/T0_CONTEXT_ROTATION.md).

The guard is `scripts/hooks/t0_context_guard.py`. Under the warn band it does nothing, and in the warn
band below 500K it only adds a reminder to the prompt. From 500K it denies starting new work (a new dispatch, a new
`/goal`) and holds the end of the turn once, so work already in flight can be finished and
nothing new starts. It guards a session only in a VNX project that has a T0, and never a
worker. It reads the user-level Claude settings of each account, so it is registered once per
account:

```bash
vnx guard install --dry-run   # list the accounts and print the diff per file, write nothing
vnx guard install             # register the guard in every Claude account
vnx guard check               # read-only: where the guard runs, per account and per project
```

`vnx guard check` exits 0 only when all of its checks hold, among them that each account carries
the three hook entries in the exact form and that the shim and the guard exist in the installed
VNX.

The spawner is `scripts/t0_rotate_spawn.sh`. It is called by the `rotate` skill after
`/build-log wrap` has written `daily-log/handoff.md`. It refuses to run outside tmux, on a
handoff older than 15 minutes, or without a wrap proof in the handoff. When it runs it opens a
new tmux window next to the current one, starts `claude --model opus` there with the kickoff
(`/kickoff rotatie <run_id>`) as its start argument. The kickoff runs on the handoff and takes up
its first numbered next step. A job on the tmux server checks that the first message arrived and
closes the old window only then, after resuming a running `/goal`. A rotation that cannot wait for a wrap uses `--skip-wrap-reason "<text>"`, which
leaves an audit receipt. `vnx handoff show` is a different handoff: it reads `handoff.md` from the rotation directory (`rotation_handovers/T0` under the project's runtime directory, or the directory given with `--logdir`) and prints "No handoff found" when that file is absent. It does not read `daily-log/handoff.md`.

### Key Bindings (in tmux)
- `Ctrl+G` — Open dispatch queue popup
- `Ctrl+B D` — Detach (keeps running)
- Mouse — Click to switch panes

---

## Feature Development Workflow

Feature worktrees, the gate workflow, and daily operator commands are covered
in [Onboarding Guide, Part 2](../onboarding/ONBOARDING_GUIDE.md#part-2-operator-path-repo-local-bash-cli).

### Shell Helper

For global `vnx` access from any project directory:

```bash
./bin/vnx install-shell-helper   # Adds vnx() to ~/.zshrc or ~/.bashrc
```

The helper walks up from CWD to find the project-local `.vnx/bin/vnx` (primary, created by `vnx_install.py` in a consumer project) or `.claude/vnx-system/bin/vnx` (legacy layout). Neither exists in the vnx-orchestration repo itself — its own entrypoint is `./bin/vnx` at the repo root; this helper is for projects where VNX has been installed as a consumer.

---
