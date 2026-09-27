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

## Step 3: Run the Hello-World Demo

The `hello-world` example agent ships with VNX. No files to create:

```bash
vnx dispatch-agent --agent hello-world
```

## Step 4: Dispatch with a Custom Instruction

```bash
vnx dispatch-agent --agent hello-world --instruction "Write a greeting for a new VNX user"
```

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

See [Onboarding Guide, Part 2](onboarding/ONBOARDING_GUIDE.md#part-2-operator-path-repo-local-bash-cli)
and [Appendix A](onboarding/ONBOARDING_GUIDE.md#appendix-a-two-binaries-and-the-full-pip-cli-surface)
for the full operator workflow and command list.

## What's Next?

- Full onboarding walkthrough: [Onboarding Guide](onboarding/ONBOARDING_GUIDE.md)
- Create your own agent: [Agent Creation Guide](guides/AGENT_CREATION_GUIDE.md)
- Full documentation: [README](../README.md)
- Architecture: [docs/manifesto/ARCHITECTURE.md](manifesto/ARCHITECTURE.md)
