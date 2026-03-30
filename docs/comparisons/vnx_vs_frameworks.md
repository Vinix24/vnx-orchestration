# VNX vs Multi-Agent Frameworks

An honest comparison with OpenClaw, CrewAI, LangGraph, AutoGen, and similar multi-agent orchestration frameworks.

## Different Categories

VNX and multi-agent frameworks solve related but distinct problems:

- **Frameworks** (CrewAI, LangGraph, AutoGen, OpenClaw) are libraries you import into your code to build agent workflows programmatically. You define agents, tools, and orchestration logic in Python/TypeScript.
- **VNX** is a runtime orchestration system that coordinates existing AI CLI tools (Claude Code, Codex CLI, Gemini CLI) across terminals with governance controls. You don't write agent code — you dispatch tasks to CLI agents that already exist.

This isn't better or worse. It's a different approach for different workflows.

## Feature Comparison

| Capability | VNX | CrewAI | LangGraph | AutoGen | OpenClaw |
|-----------|-----|--------|-----------|---------|----------|
| **Approach** | Runtime orchestration of CLI agents | Python framework for agent teams | Graph-based agent workflows | Multi-agent conversation framework | CLI orchestration toolkit |
| **Setup** | `git clone` + `vnx init` | `pip install` + write agent code | `pip install` + define graph | `pip install` + define agents | `pip install` + configure |
| **Agent definition** | None — uses existing CLIs as-is | Python classes with role/goal/backstory | Nodes in a state graph | Python agent definitions | CLI tool configuration |
| **LLM-agnostic** | Yes (any CLI tool) | Yes (via LiteLLM) | Yes (via LangChain) | Yes (multiple providers) | Varies |
| **Audit trail** | Append-only NDJSON ledger | Custom logging required | LangSmith (cloud) or custom | Custom logging required | Varies |
| **Quality gates** | Built-in deterministic gates | Custom implementation | Custom implementation | Custom implementation | Varies |
| **Human-in-the-loop** | Mandatory — every dispatch | Optional interrupt points | Optional breakpoints | Optional human proxy | Configurable |
| **Context rotation** | Automatic handover + resume | Not typically handled | Checkpointing available | Not typically handled | Varies |
| **State management** | File-based (greppable, diffable) | In-memory / custom persistence | Graph state with checkpoints | Conversation history | Varies |
| **Provenance tracking** | Built-in (dispatch → code → receipt) | Not built-in | Via LangSmith | Not built-in | Varies |

## Where VNX Is Stronger

### 1. Governance is architectural, not optional

In most frameworks, human approval and quality gates are features you can enable. In VNX, they're mandatory — every dispatch requires human promotion, every output passes through deterministic gates. You can't accidentally ship ungoverned agent output.

### 2. No agent code to write

Frameworks require you to define agents programmatically — roles, tools, orchestration logic, error handling. VNX dispatches tasks to existing CLI agents (Claude Code, Codex CLI) that already know how to code. You define *what to do*, not *how the agent works*.

### 3. File-based state you can inspect

VNX state is NDJSON files and JSON documents on disk. You can `grep`, `jq`, `diff`, and version-control everything. No database migrations, no cloud dashboards, no opaque state stores.

### 4. Context window management

Long-running coding tasks fill context windows. VNX detects this and handles rotation automatically — structured handover, session clear, resume with context. Most frameworks don't address this because they use API calls (not interactive CLIs), but for coding workflows where agents run interactively, this matters.

### 5. Real CLI tools, not wrapped APIs

VNX agents are full Claude Code / Codex CLI / Gemini CLI instances with all their native capabilities — file editing, terminal access, tool use, MCP servers. Frameworks typically wrap API calls, which means agents can't use provider-native features like Claude Code's hooks or Codex's sandbox.

## Where Frameworks Are Stronger

### 1. Programmatic orchestration

If you need custom agent workflows — conditional routing, retry logic, dynamic tool selection, multi-step reasoning chains — frameworks give you full programmatic control. VNX's dispatch model is simpler: assign scoped tasks to terminals.

### 2. Integration into applications

Frameworks are libraries you import. You can embed agent workflows into web apps, APIs, pipelines, and services. VNX is a standalone system — it orchestrates development work, not application logic.

### 3. Cloud-native deployment

Frameworks deploy to cloud infrastructure. VNX is local-first and file-based. If you need distributed agents across machines, frameworks are the better fit today.

### 4. Ecosystem and community

CrewAI and LangGraph have large communities, extensive documentation, and marketplace ecosystems. VNX is a younger project with a smaller community. Frameworks have more examples, tutorials, and third-party integrations.

### 5. Non-coding use cases

Frameworks handle arbitrary agent tasks — research, data analysis, content generation. VNX is purpose-built for software engineering workflows: code changes, testing, reviews, and deployment governance.

## When to Choose VNX

- You already use AI coding CLIs and want governance around them
- You need audit trails and provenance for AI-generated code
- You want quality gates that are deterministic, not LLM-based
- You prefer file-based state over databases and cloud services
- Your workflow is software engineering, not general-purpose agent tasks
- You want multi-model support without writing adapter code

## When to Choose a Framework

- You're building agent logic into an application or service
- You need programmatic control over agent routing and decision-making
- Your use case is research, data analysis, or content — not primarily coding
- You need cloud-native deployment and distributed execution
- You want a large ecosystem with pre-built tools and integrations

## Can You Use Both?

Yes. VNX orchestrates CLI agents at the terminal level. Those agents can internally use framework features (Claude Code's Task tool, MCP servers, etc.). VNX governs the outer loop — what gets dispatched, what gets approved, what passes quality gates. The inner loop is whatever the CLI agent does natively.
