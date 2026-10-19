# VNX in 5 Minutes

This is the condensed pip-CLI happy path. For the full guide, including the
operator bash CLI (`./bin/vnx`), the two-binary split, and troubleshooting,
see [`docs/onboarding/ONBOARDING_GUIDE.md`](onboarding/ONBOARDING_GUIDE.md).

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
and `agents/`) and a resolved runtime state directory.

## Step 3: Prepare the Project and Dispatch

A dispatch needs two things `vnx init` does not create: a git repository, and a
GitHub-hosted `origin` remote with `gh` authenticated (the worker's worktree
branch is opened as a PR before the receipt can read `done`). It also needs an
authenticated `claude` CLI on your PATH. `vnx doctor` flags a missing worker CLI.

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
its receipt in `state/t0_receipts.ndjson`. `vnx doctor` prints this project's
store root on its `state:location` line, so derive `$DATA` from it and use that
path in every command you copy. Query the receipt instead of reading the file:

```bash
DATA=$(vnx doctor | sed -n 's/.*state:location.*: //p')
python3 <path-to-checkout>/scripts/receipt_query.py by-dispatch <dispatch-id> --state-dir $DATA/state
```

Run both from your project directory. `receipt_query.py` ships in the pip package
(`pyproject.toml` ships `scripts/**/*`) but has no console entry point, so run it
as a Python file; `<path-to-checkout>` above is one place it lives. The walkthrough
with real output is in the [README](../README.md#your-first-dispatch).

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
