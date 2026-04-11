# VNX in 5 Minutes

## Step 1: Install

```bash
git clone https://github.com/Vinix24/vnx-orchestration.git
cd vnx-orchestration
./install.sh /path/to/your/project
cd /path/to/your/project
```

## Step 2: Initialize

```bash
vnx init
# Creates .vnx/, agents/, .vnx-data/
```

## Step 3: Your First Agent

VNX ships with a hello-world example agent:

```bash
ls examples/hello-world/
# CLAUDE.md   config.yaml
```

## Step 4: Dispatch

```bash
vnx dispatch-agent --agent hello-world --instruction "Write a greeting for a new VNX user"
# Spawns agent headlessly, writes output to .vnx-data/unified_reports/
```

## Step 5: Check Results

```bash
cat .vnx-data/unified_reports/*.md
# Your agent's output report
```

## Step 6: Run Quality Gate

```bash
vnx gate-check --pr 1
# Codex + Gemini review your agent's work
# Gate result written to .vnx-data/state/review_gates/results/
```

## What's Next?

- Create your own agent: [Agent Creation Guide](guides/AGENT_CREATION_GUIDE.md)
- Full documentation: [README](../README.md)
- Architecture: [docs/manifesto/ARCHITECTURE.md](manifesto/ARCHITECTURE.md)
