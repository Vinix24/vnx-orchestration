# VNX in 5 Minutes

## Step 1: Install

```bash
git clone https://github.com/Vinix24/vnx-orchestration.git
cd vnx-orchestration
pip install -e .
# or install VNX into an existing project:
# ./install.sh /path/to/project
```

## Step 2: Initialize Starter Mode

```bash
vnx init --starter
# Creates .vnx-data/ starter runtime state inside your project
```

## Step 3: Check Health

```bash
vnx doctor
# Confirms the install and starter runtime are healthy
```

## Step 4: Confirm Status

```bash
vnx status
# Shows starter mode, terminal state, queue counts, and receipts
```

## Step 5: Inspect Machine-Readable State

```bash
vnx status --json | python3 -m json.tool
# Useful for scripts and sanity checks
```

## Step 6: Grow Into Operator Mode

```bash
vnx init --operator
# Upgrade when you want the full tmux grid and multi-track orchestration
```

## What's Next?

- Create your own agent: [Agent Creation Guide](guides/AGENT_CREATION_GUIDE.md)
- Full documentation: [README](../README.md)
- Architecture: [docs/manifesto/ARCHITECTURE.md](manifesto/ARCHITECTURE.md)
