# Agent Creation Guide

## What is a VNX Agent?

A VNX agent is a skill-scoped worker with its own `CLAUDE.md` role definition, a `config.yaml`
governance profile, and an isolated folder scope that prevents it from accessing unrelated parts of
the project. Agents are dispatched like any other VNX worker but run in a constrained context that
matches their domain — business agents use the `light` profile, coding agents use `default`.

---

## Quick Start

Follow these steps to add a new agent:

**1. Create the agent directory and CLAUDE.md**

```bash
mkdir -p agents/<name>
touch agents/<name>/CLAUDE.md
```

Write the role, capabilities, constraints, and output format in `CLAUDE.md`.
See the template section below for the required structure.

**2. Create config.yaml with a governance profile**

```bash
cat > agents/<name>/config.yaml <<EOF
governance_profile: light
isolation:
  scope_type: business_folder
  allowed_paths:
    - "agents/<name>/"
    - ".vnx-data/unified_reports/"
  denied_paths:
    - "scripts/"
    - "dashboard/"
    - ".claude/"
EOF
```

**3. Add a scope entry in `.vnx/governance_profiles.yaml`**

Under the `scopes:` block, add a line before the `"agents/*"` catch-all:

```yaml
scopes:
  "agents/<name>": light   # <- add this line
  "agents/*": light
  ...
```

**4. Test the agent**

```bash
bash scripts/commands/dispatch-agent.sh \
  --agent <name> \
  --instruction "test task"
```

Check `.vnx-data/unified_reports/` for the output.

---

## CLAUDE.md Template

```markdown
# <Agent Display Name>

You are a <one-line role description>. <one sentence on quality standard>.

## Role

<2-3 sentences: what this agent does, what "done" looks like, key outputs.>

## Input

You will receive:
- `field_name` — description of the input field
- `field_name` — description of the input field

## Output

<Describe the expected output format, structure, and length.>

## Constraints

- <Hard rule — no exceptions>
- <Style or quality rule>
- <Source / accuracy rule>

## Report Format

After completing the task, append a VNX unified report block:

​```
## VNX Report
- field: <value>
- quality_self_assessment: <one sentence>
- open_items: []
​```
```

---

## Governance Profiles

Profiles are defined in `.vnx/governance_profiles.yaml`. Three profiles ship by default:

| Profile   | Review mode      | Required gates            | Max PR lines | Auto-merge |
|-----------|------------------|---------------------------|--------------|------------|
| `default` | full             | codex_gate, gemini_review, ci | 300      | no         |
| `light`   | exception_only   | ci                        | 500          | no         |
| `minimal` | none             | _(none)_                  | 1000         | yes        |

**When to use each:**

- `default` — code changes, scripts, infrastructure. Full review gates enforced.
- `light` — business content agents (blog posts, LinkedIn, research summaries). CI only; no code review gates.
- `minimal` — internal scratch agents or dry-run tasks where output is ephemeral and not merged.

---

## Isolation

Each agent's `config.yaml` declares `allowed_paths` and `denied_paths`. The dispatch system
enforces these at scope-resolution time via `scripts/lib/governance_profiles.py`:

- `allowed_paths` — the only directories the agent may read from or write to.
- `denied_paths` — directories explicitly blocked even if they would otherwise match.

An agent scoped to `agents/blog-writer/` cannot read `scripts/`, `dashboard/`, or `.claude/`.
This prevents a content agent from accidentally modifying infrastructure or leaking credentials.

---

## Examples

Three built-in agents ship with this project:

| Agent               | Profile | Purpose                                      |
|---------------------|---------|----------------------------------------------|
| `blog-writer`       | light   | Writes 800-1500 word SEO-friendly blog posts |
| `linkedin-writer`   | light   | Writes 150-300 word LinkedIn posts with CTA  |
| `research-analyst`  | light   | Produces structured research summaries       |

Browse their definitions in `agents/blog-writer/`, `agents/linkedin-writer/`, and
`agents/research-analyst/`. Each directory contains a `CLAUDE.md` and a `config.yaml`.

To dispatch one:

```bash
bash scripts/commands/dispatch-agent.sh \
  --agent blog-writer \
  --instruction "Write a post about AI governance for startup founders"
```
