# VNX in 5 Minutes

This is the condensed pip-CLI happy path. For the full guide, including the
operator bash CLI (`./bin/vnx`), the two-binary split, and troubleshooting,
see [`docs/onboarding/ONBOARDING_GUIDE.md`](onboarding/ONBOARDING_GUIDE.md).

## Prerequisites

VNX does not run models itself. It drives existing coding CLIs as subprocesses
and governs the result. You need Python 3.11 to 3.13. The default dispatch lane
needs an installed and authenticated `claude` CLI on your PATH (other lanes:
`codex`, `gemini`, `kimi`), and using it incurs that provider's subscription or
credit usage. `vnx dispatch-agent` fails at spawn if no worker CLI is present.
`vnx doctor` flags this with a `tool:worker-cli` warning. A zero-key exploration
of the governance flow is not shipped, and the old replay demo was retired.

## Two binaries

There are two binaries on purpose. The pip `vnx` covers the essentials: `init`,
`migrate`, `doctor`, `status`, `dispatch-agent`, `track`, `pool`, `dream`,
`gate-check` (the same deterministic pre-merge GO/HOLD check the fabric's own CI
runs, usable without a checkout), `horizon` and `objective`, `deliverable`,
`attest`, `handoff`, `role`, `update`, `release`, `learning`, `fabric-audit`,
`subsystems`, `version`, `pr-ready`, `worktree-release`, `providers`, `guard` and
`preflight`. Checkout-only operator commands live behind `./bin/vnx`:
`new-worktree` and the rest of the operator surface (worktree lifecycle,
snapshot and restore, staging). For those, clone the repository and run
`pip install -e .` from the checkout.

For a shared central install, measure a rollout before it happens. The
[install-central runbook](operations/install-central-runbook.md), section "Before
a rollout", covers the read-only preflight and the four classes it blocks on,
release verification, the `release:main-drift` doctor check, and the three steps
that stay manual: the release itself, the `current` flip and reloading the
launchd jobs.

## Step 1: Install

```bash
pip install vnx-orchestration
vnx version
```

## Step 2: Initialize

```bash
mkdir -p my-vnx-project
cd my-vnx-project
vnx init
vnx doctor
```

`vnx init` creates the tracked project scaffold (`.vnx/`, `.vnx-project-id`,
and `agents/`) and a resolved runtime state directory. Both pip and source
init reuse an existing project marker, otherwise slugify the directory basename
(or use a deterministic `vnx-<8 hex>` path hash if no valid slug exists). Source
init anchors `<data-root>/.vnx-project-id` before initializing its store; it needs
no pre-created marker or `VNX_PROJECT_ID`. An existing data marker wins over a
renamed directory. Conflicting project/data markers or store layouts refuse.
Direct store initialization still requires a project id from the database layout, a `.vnx-project-id` marker, or
`VNX_PROJECT_ID`; present sources must agree. Without a source it refuses and
asks you to run `vnx init` in the project. A failed QI tenant reconciliation
also fails initialization; correct the reported error before retrying.

## Step 3: Prepare the Project and Dispatch

A dispatch needs two things `vnx init` does not create. A fresh project trips
over both.

**A git repository.** `vnx init` scaffolds config, databases and `agents/`, but
it does not run `git init`. Dispatching before you do fails at the
worktree-creation step. The error is unhelpful: a permission error against the
read-only central install path, not a message that says "run git init".

**A GitHub-hosted `origin` remote, with `gh` authenticated.** Worktree isolation
is unconditional on the default lane, and that path calls `gh pr create` before
it records the dispatch as done. A local-only remote is not enough. The worker
still does real work, but the receipt reads `failed`.

It also needs an authenticated `claude` CLI on your PATH. `vnx doctor` flags a
missing worker CLI.

```bash
git init -q -b main && git add -A && git commit -q -m "initial scaffold"
git remote add origin git@github.com:<you>/<repo>.git && git push -u origin main
vnx migrate
```

Then dispatch one of the built-in roles. The single-entry door checks the role
against the `agents/` registry, so use a role name from it (`research-analyst`
here), not `examples/hello-world`:

```bash
vnx dispatch-agent --agent research-analyst \
  --instruction "Quick depth research: what does 'orchestration' mean in software systems? 3 bullet points."
```

## Step 4: Read the Result

The worker's report lands in the store's `unified_reports/<dispatch-id>.md` and
its receipt in `state/t0_receipts.ndjson`, one JSON line per dispatch. `vnx doctor`
prints this project's store root on its `state:location` line, so derive `$DATA`
from it and use that path in every command you copy.

No pip `vnx` command reads a receipt, so this step needs a checkout. Clone
`vnx-orchestration` and run its query script as a Python file from your project
directory. Query the receipt instead of reading the file:

```bash
DATA=$(vnx doctor | sed -n 's/.*state:location.*: //p')
python3 <path-to-checkout>/scripts/receipt_query.py by-dispatch <dispatch-id> --state-dir $DATA/state
```

Without a checkout you can still read the report file and `tail -n 1` the
receipt file by hand.

A receipt that reads `[failed]` can be correct. If `origin` is a local bare
repository and not a real GitHub remote, `gh pr create` has nothing to talk to
and the receipt records exactly that. The report and the commit are real, and the
receipt still refuses to call the dispatch done without a PR in front of it.
Point `origin` at a real GitHub remote and the same sequence ends with
`status: done`.

After a dispatch the orchestrator still records whether the outcome is accepted.
`receipt_query.py open-outcomes` lists the dispatches whose outcome still needs a
decision, and `receipt_query.py decide <dispatch-id> accept|reject --reason ...
--state-dir <state-dir>` records it. Before merging, `vnx pr-ready <PR>` shows what
the merge door will check on the PR head, and the merge itself goes through
`scripts/pr_merge.py` ([DISPATCH_RULES.md](core/DISPATCH_RULES.md) §2.1 and §13).

## Step 5: Check Status

```bash
vnx status
```

Use `vnx status --json` when you need machine-readable project state.

## Step 6: Operator Gate Check

Quality gates and the rest of the operator surface run through the
repo-local `./bin/vnx` bash CLI, from a cloned `vnx-orchestration` checkout:

```bash
./bin/vnx gate-check --pr 1
```

Before a merge, `vnx pr-ready <PR>` (also in the pip CLI) lists the required
contexts and the gate evidence on the PR's current head.

See [Onboarding Guide, Part 2](onboarding/ONBOARDING_GUIDE.md#part-2-operator-path-repo-local-bash-cli)
and [Appendix A](onboarding/ONBOARDING_GUIDE.md#appendix-a-two-binaries-and-the-full-pip-cli-surface)
for the full operator workflow and command list.

## What's Next?

- Full onboarding walkthrough: [Onboarding Guide](onboarding/ONBOARDING_GUIDE.md)
- Create your own agent: [Agent Creation Guide](guides/AGENT_CREATION_GUIDE.md)
- Full documentation: [README](../README.md)
- Architecture: [docs/manifesto/ARCHITECTURE.md](manifesto/ARCHITECTURE.md)
