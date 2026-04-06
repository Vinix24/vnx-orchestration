# Feature: Smart Model Routing

**Feature-ID**: Feature 30 (redefined)
**Status**: Research Complete — Ready for Planning
**Priority**: P2
**Branch**: `feature/smart-model-routing`
**Risk-Class**: medium
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Date**: 2026-04-06
**Author**: T2 (Architect, Opus)

Previous F30 scope (full tmux elimination) is **abandoned**. F30 is redefined as:
intelligent task routing to the right model — from 3B local housekeeping to Opus architecture review.

---

## 1. Reference Project Analysis

### 1.1 Get Shit Done (GSD)

**Repository**: `gsd-build/gsd-2`

**Decomposition heuristic**: Three-level hierarchy: Milestone (shippable version, 4-10 slices) → Slice (one demoable vertical capability, 1-7 tasks) → Task (one context-window-sized unit). The iron rule: "a task must fit in one context window; if it can't, it's two tasks."

**Key patterns**:
- State-machine auto-mode driven by `.gsd/` files on disk
- Fresh context per task (no accumulated garbage) — pre-inlined relevant files + prior task summaries
- Git worktree isolation per milestone with squash-merge
- Stuck detection via sliding-window repeated-dispatch analysis
- Automatic roadmap reassessment after each slice

**Key insight for F30**: The context-window-as-hard-ceiling heuristic is directly applicable to local model routing. A task that fits in 4K context = local 14B candidate. A task needing 32K+ context = Sonnet/Opus.

### 1.2 Ralph Loops

**Repository**: `ghuntley/how-to-ralph-wiggum`, `vercel-labs/ralph-loop-agent`

**Decomposition heuristic**: Deliberately simple — run an AI agent in a bash `while` loop with a completion promise string. A parent agent owns the end-to-end plan (~1,500 lines), then shells out specific phases to sub-agents in their own loops. "One loop iteration = one coherent attempt." `--max-iterations` is the safety valve.

**Key patterns**:
- Ruthless context resets (each iteration starts fresh)
- Git worktree isolation per worker
- Fan-out via parallel workers with `event collect` fan-in
- Multi-step pipelines (pick issue → plan → implement → review) use chained loops where review failure jumps back to implementation

**Key insight for F30**: The simplicity principle applies to routing. Ralph loops prove that a 300-line loop + LLM tokens can orchestrate complex work. Our routing decision tree should stay deterministic and simple — no meta-LLM deciding which LLM to use.

### 1.3 DimitriGeelen/agentic-engineering-framework

**Repository**: `DimitriGeelen/agentic-engineering-framework`

**Key patterns**:
- **Task system**: Flat tasks (Markdown + YAML frontmatter in `.tasks/`) with workflow types (specification, design, build, test, refactor, decommission) that determine agent selection. "Nothing gets done without a task" enforced structurally via pre-operation hooks.
- **Four-tier enforcement**: Tier 0 (unconditional blocking for destructive ops), Tier 1 (strict default requiring task context), Tier 2 (human-authorized bypass).
- **Healing loop**: Failed tasks are classified, recovery suggested, patterns recorded for learning.
- **Three-layer memory**: Working, project, episodic. When a pattern repeats 3+ times, it's promoted from task-local to project-level.
- **Constitutional directives**: Antifragility, reliability, usability, portability as architectural north star.
- **545+ self-governed tasks** as proof of concept; 150+ audit checks.

**Key insight for F30**: The healing loop pattern is directly applicable to the housekeeping sidecar — stale OIs, broken leases, and orphaned dispatches are all "failures" that can be auto-corrected by a local model following deterministic rules. The task-type → agent-selection mapping validates our routing approach.

### 1.4 DimitriGeelen/termlink

**Repository**: `DimitriGeelen/termlink`

**Key patterns**:
- **Dual-plane design**: Control plane (JSON-RPC 2.0 for commands/queries/events) and data plane (binary frames for raw terminal I/O).
- **37 MCP tools** exposing all session operations to AI agents
- **Event bus** for inter-session signaling (emit/poll/watch/broadcast/collect/wait)
- **Hub supervision**: Multi-session coordinator with 30-second sweep cycle
- **Spawn backends**: Auto-detect platform (Terminal.app, tmux, background daemon)
- **`--isolate` flag**: Git worktree per worker with `--auto-merge`
- **Capability tokens**: HMAC-SHA256 for security

**Key insight for F30**: VNX already has terminal specialization (T1=implement, T2=test, T3=review). The gap is model specialization within those terminals. termlink's MCP-tool-per-session pattern could inform future `/idea` integration — ideas captured via MCP tools rather than just CLI commands.

---

## 2. Local Model Benchmark Summary

### 2.1 Model Capabilities by Size

| Size | Representative Model | Realistic Tasks | Limitations |
|------|---------------------|----------------|-------------|
| **3B** | qwen2.5-coder:3b | Classification, tagging, JSON extraction, commit message drafting, simple regex, lint explanations | Cannot generate correct multi-file code; official HumanEval (84.1) contested — independent reproduction shows ~45; quantization degrades quality sharply |
| **7B** | qwen2.5-coder:7b | Formatting, linting fixes, single-function refactors, docstring generation, import sorting, type annotations | HumanEval 88.4 — strongest at this size, rivals 20B+ models; struggles with cross-file context |
| **14B** | qwen2.5-coder:14b | Small refactors (<50 lines), code review, test generation, bug fixes, PR descriptions, boilerplate | HumanEval ~89-90; surpasses CodeStral-22B; cannot maintain coherence across 3+ file changes |
| **32B** | qwen2.5-coder:32b | Complex single-file tasks, multi-function refactors | HumanEval 92.7, matches GPT-4o; only viable on Max chips (64GB+); ~15-20 tok/s |

### 2.2 Performance on Apple Silicon

Based on community benchmarks (Q4_K_M quantization, Ollama/llama.cpp):

| Model | M1 Pro (16 GPU) | M1/M2 Max (32+ GPU) | M4 Pro (20 GPU) | M4 Max (40 GPU) |
|-------|----------------|---------------------|----------------|----------------|
| 3B | ~60-70 tok/s | ~100+ tok/s | ~85 tok/s | ~140+ tok/s |
| 7B | ~36 tok/s | ~61-66 tok/s | ~51 tok/s | ~83 tok/s |
| 14B | ~18-22 tok/s | ~30-40 tok/s | ~28-32 tok/s | ~39-45 tok/s |
| 32B | too slow | ~15-20 tok/s | ~12-15 tok/s | ~20-25 tok/s |

**Note**: 7B numbers are measured; 3B/14B estimated proportionally. Direct datapoint: Qwen2.5-14B Q4 on M2 Max = ~39 tok/s. MLX backend runs 30-50% faster than Ollama GGUF on Apple Silicon. VNX already has `scripts/llm_benchmark_coding_v2.py` which can produce exact numbers for the operator's specific hardware. PR-1 should include a calibration step.

### 2.3 Code Quality Benchmarks

| Model | HumanEval (pass@1) | MBPP (pass@1) | Practical Assessment |
|-------|-------------------|---------------|---------------------|
| qwen2.5-coder:3b | ~45% (reproduced) | ~30% (reproduced) | Official claims 84.1/73.6 contested; useful only for classification/extraction |
| qwen2.5-coder:7b | **88.4%** | 83.5% | Strongest at size class; rivals 20B+ models |
| qwen2.5-coder:14b | ~89-90% | ~84%+ | Surpasses CodeStral-22B and DS-Coder-33B |
| qwen2.5-coder:32b | **92.7%** | ~87% | Matches GPT-4o; Aider score 73.7; needs 64GB+ RAM |
| qwen3.5:9b | ~70% | ~72% | Good reasoning, moderate code quality |
| devstral | ~72% | ~75% | Strong on code editing, weaker on generation |

**Key findings**:
- 7B is the surprise performer — 88.4% HumanEval beats many 20B+ models
- 14B is the practical sweet spot for local code tasks — fast enough for interactive use (~20-35 tok/s), accurate enough for scoped changes
- 3B official benchmarks are inflated; independent reproduction shows ~45% HumanEval. Reliable only for classification
- No SWE-bench results exist for sub-32B models — multi-file reasoning at these sizes is unreliable

---

## 3. Smart Routing Decision Tree

### 3.1 Input Signal Taxonomy

Every task entering the routing system must be classified on these axes:

```
TaskSignals:
  type:        implement | test | review | refactor | housekeeping | idea
  files:       int (number of affected files, 0 if unknown)
  risk:        low | medium | high | critical
  complexity:  S | M | L | XL
  new_file:    bool (creating new file vs editing existing)
  test_req:    bool (does the task require test coverage)
  context_kb:  int (estimated context window needed in KB)
```

### 3.2 Routing Algorithm

```python
def route_task(signals: TaskSignals) -> ModelTarget:
    """Deterministic model routing based on task signals.
    
    Returns one of: local-3b, local-7b, local-14b, sonnet, opus
    """
    
    # Critical risk always goes to Opus — no exceptions
    if signals.risk == "critical":
        return "opus"
    
    # Architecture and review tasks → Opus
    if signals.type in ("review",) and signals.risk in ("high", "critical"):
        return "opus"
    
    # Housekeeping tasks → local models
    if signals.type == "housekeeping":
        if signals.files == 0:
            return "local-3b"   # Classification, tagging, stale checks
        if signals.files <= 2:
            return "local-7b"   # Single-file cleanup, formatting
        return "local-14b"      # Multi-file housekeeping
    
    # Idea intake → local classification + cloud enrichment
    if signals.type == "idea":
        return "local-14b"      # Classification and outline; enrichment deferred
    
    # Implementation tasks — route by complexity and scope
    if signals.type == "implement":
        if signals.complexity == "S" and signals.files <= 1 and not signals.new_file:
            return "local-14b"  # Small, single-file edits
        if signals.complexity in ("S", "M") and signals.files <= 3:
            return "sonnet"     # Standard implementation work
        return "opus"           # Complex, multi-file, or new-file implementations
    
    # Test tasks
    if signals.type == "test":
        if signals.complexity == "S" and signals.files <= 2:
            return "local-14b"  # Simple test scaffolding
        return "sonnet"         # Test suites, integration tests
    
    # Refactoring
    if signals.type == "refactor":
        if signals.complexity == "S" and signals.files <= 1:
            return "local-14b"
        if signals.complexity in ("S", "M"):
            return "sonnet"
        return "opus"           # Large refactors need architectural judgment
    
    # Default: Sonnet (safe middle ground)
    return "sonnet"
```

### 3.3 Complexity Estimation Heuristic

Complexity is auto-estimated from the task description and file analysis:

| Complexity | Criteria |
|-----------|----------|
| **S** | ≤1 file, ≤30 lines changed, no new APIs, no schema changes |
| **M** | 2-3 files, ≤100 lines changed, simple API additions, no schema changes |
| **L** | 4-6 files, ≤300 lines changed, new APIs or schema, requires tests |
| **XL** | 7+ files, 300+ lines, new architecture, cross-cutting concerns |

### 3.4 Override Mechanism

Operators can force a specific model via dispatch metadata:

```yaml
# In dispatch YAML
model_override: opus    # Bypass routing, use this model
model_floor: sonnet     # Minimum model — routing can only go higher
```

This preserves operator control while allowing automatic optimization for routine work.

---

## 4. Housekeeping Sidecar Architecture

### 4.1 Overview

A lightweight Python daemon that runs a local model on a 5-minute poll cycle to perform automated governance housekeeping.

```
┌─────────────────────────────────────────────────┐
│  Housekeeping Sidecar (Python daemon)           │
│                                                 │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   │
│  │ OI Scan  │   │ Lease    │   │ Report   │   │
│  │ & Close  │   │ Cleanup  │   │ Tagger   │   │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘   │
│       │              │              │           │
│       ▼              ▼              ▼           │
│  ┌────────────────────────────────────────┐     │
│  │  Ollama HTTP API (localhost:11434)     │     │
│  │  Model: qwen2.5-coder:3b (classify)   │     │
│  │  Model: qwen2.5-coder:7b (generate)   │     │
│  └────────────────────────────────────────┘     │
│                                                 │
│  Poll: every 5 minutes                          │
│  Log: .vnx-data/logs/housekeeping.log           │
│  State: .vnx-data/state/housekeeping.json       │
└─────────────────────────────────────────────────┘
```

### 4.2 Housekeeping Jobs

| Job | Model | Cycle | Description |
|-----|-------|-------|-------------|
| **OI Stale Scan** | 3B | 5 min | Read open items, check if referenced code has changed, classify as stale/active. Auto-close stale OIs with evidence. |
| **Lease Cleanup** | none (pure Python) | 5 min | Check `runtime_coordination.db` for expired leases, release them. No LLM needed. |
| **Report Tagger** | 3B | 15 min | Read unified reports, extract metadata (quality score, type, key findings), write tags to NDJSON sidecar file. |
| **Dispatch-Receipt Cross-ref** | 3B | 15 min | Compare dispatches in `completed/` with receipts. Flag orphaned dispatches (no receipt) or orphaned receipts (no dispatch). |
| **Quality Digest** | 7B | 60 min | Aggregate tagged reports and OI trends into a 1-paragraph quality summary. |

### 4.3 Implementation Pattern

Reuse the existing Ollama HTTP API pattern from `scripts/conversation_analyzer.py` (also in `scripts/llm_benchmark.py` which adds streaming, model management via `pull_ollama_model()`, and `list_ollama_models()`). The `retrospective_model_hook.py` adds a protocol-based design (`LocalModelHook` with `is_available()` and `analyze()`) that validates model output against an evidence pool — a pattern the housekeeping sidecar should adopt:

```python
def query_ollama(model: str, prompt: str, temperature: float = 0.3) -> str:
    """Reusable Ollama query — same pattern as conversation_analyzer._try_ollama()"""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": 2048},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        return body.get("response", "")
```

### 4.4 Failure Mode

If Ollama is unavailable, the sidecar degrades gracefully:
- Pure Python jobs (lease cleanup) continue running
- LLM-dependent jobs skip with a log entry
- No error propagation — housekeeping is advisory, not blocking

---

## 5. /idea Slash Command Design

### 5.1 User Flow

```
User types: /idea "Blog post over self-hosted governance vs OAuth platforms"

Step 1: Local classification (3B/7B, <2s)
  → type: blog
  → project: vincentvandeth.nl
  → complexity: M
  → tags: [governance, oauth, self-hosted, blog]

Step 2: Enrichment (14B, <10s)
  → outline: 5 sections with key points
  → audience: technical readers, DevOps engineers
  → estimated scope: 1500-2000 words

Step 3: Storage
  → Write to .vnx-data/ideas/<timestamp>-<slug>.yaml
  → Format:
    id: 20260406-blog-governance-vs-oauth
    type: blog
    project: vincentvandeth.nl
    status: idea
    title: "Self-hosted Governance vs OAuth Platforms"
    outline: [...]
    tags: [governance, oauth, self-hosted]
    created: 2026-04-06T16:00:00Z
    
Step 4: Kanban integration
  → Append to .vnx-data/kanban/ideas.ndjson
  → If Notion MCP is available: create Notion page in Ideas database
```

### 5.2 Skill Definition

```yaml
# .claude/skills/idea/SKILL.md
---
name: idea
description: Capture and classify ideas for any project using local models
allowed-tools: [Bash, Read, Write]
---
```

The skill invokes a Python script that:
1. Accepts the idea text as argument
2. Calls Ollama (3B) for classification
3. Calls Ollama (14B) for enrichment
4. Writes YAML to ideas directory
5. Optionally pushes to Notion via MCP

### 5.3 Classification Prompt Template

```
Classify this idea into a structured format.

IDEA: {user_input}

Respond in JSON:
{
  "type": "blog|feature|bug|research|experiment",
  "project": "project name or 'general'",
  "complexity": "S|M|L|XL",
  "tags": ["tag1", "tag2"],
  "title": "concise title",
  "summary": "one sentence summary"
}
```

### 5.4 Storage Format

Ideas are stored as individual YAML files for easy browsing and as NDJSON for streaming:

- **Individual**: `.vnx-data/ideas/<id>.yaml` — human-readable, editable
- **Stream**: `.vnx-data/kanban/ideas.ndjson` — append-only log for dashboard/kanban consumption
- **Notion sync**: Optional, via MCP when available

---

## 6. Integration with SubprocessAdapter (F28)

### 6.1 Current State

F28 implements `SubprocessAdapter` which spawns:
```python
subprocess.Popen(["claude", "-p", "--output-format", "stream-json", "--model", model])
```

The adapter speaks the `RuntimeAdapter` protocol (`spawn`, `poll`, `read_output`, `terminate`).

### 6.2 Extension Strategy: Ollama as Second Backend

The `SubprocessAdapter` can be extended to support Ollama-routed tasks. Two options:

**Option A: Ollama HTTP API (recommended)**

Use `urllib.request` to call `localhost:11434/api/generate` directly. This is already proven in the codebase (`conversation_analyzer.py`, `llm_benchmark_coding_v2.py`).

Advantages:
- No new subprocess management complexity
- Streaming via `"stream": True` returns NDJSON lines (same pattern as Claude stream-json)
- Model loading/unloading managed by Ollama server
- Health checks via `GET /api/tags`

**Option B: Ollama CLI subprocess**

```python
subprocess.Popen(["ollama", "run", "qwen2.5-coder:14b", prompt])
```

Disadvantages:
- No structured output format (raw text only)
- No streaming events
- Process management more complex (Ollama CLI is interactive by default)

**Recommendation**: Option A (HTTP API). Implement a new `OllamaAdapter` that implements the same `RuntimeAdapter` protocol but routes through the Ollama HTTP API instead of the Claude CLI.

### 6.3 Adapter Selection Extension

Current routing (F28):
```bash
VNX_ADAPTER_T1=subprocess   # → SubprocessAdapter → claude CLI
VNX_ADAPTER_T2=subprocess   # → SubprocessAdapter → claude CLI
```

Extended routing (F30):
```bash
VNX_ADAPTER_T1=subprocess   # → SubprocessAdapter → claude CLI (Sonnet/Opus)
VNX_ADAPTER_T2=subprocess   # → SubprocessAdapter → claude CLI (Sonnet/Opus)
VNX_LOCAL_MODEL=qwen2.5-coder:14b   # Default local model for housekeeping
VNX_ROUTING=smart           # Enable smart routing (default: off)
```

When `VNX_ROUTING=smart` is set, the dispatcher:
1. Classifies the task using the routing decision tree (§3.2)
2. If the result is `local-*`, routes to `OllamaAdapter`
3. If the result is `sonnet` or `opus`, routes to `SubprocessAdapter` with `--model` flag

### 6.4 Architecture Diagram

```
Dispatch
  │
  ▼
TaskClassifier (Python, deterministic)
  │
  ├── local-3b/7b/14b ──→ OllamaAdapter ──→ Ollama HTTP API
  │                         (new in F30)      localhost:11434
  │
  ├── sonnet ────────────→ SubprocessAdapter ──→ claude -p --model sonnet
  │                         (F28, existing)
  │
  └── opus ──────────────→ SubprocessAdapter ──→ claude -p --model opus
                            (F28, existing)
```

### 6.5 Billing Safety

The billing safety invariant holds:
- Claude tasks: routed through `claude` CLI binary (SubprocessAdapter) — covered by Pro/Max subscription
- Local tasks: routed through Ollama HTTP API (OllamaAdapter) — zero external cost
- **No Anthropic SDK usage** in either path

---

## 7. PR Breakdown

### PR-1: Task Classifier & Routing Engine (~100 lines)

**Scope**: Implement `TaskClassifier` with the deterministic routing algorithm from §3.2.

| File | Action |
|------|--------|
| `scripts/lib/task_classifier.py` | NEW — Routing decision tree, signal extraction |
| `tests/test_task_classifier.py` | NEW — Unit tests for all routing paths |

**Dependencies**: None (pure Python, no external deps)

### PR-2: OllamaAdapter (~120 lines)

**Scope**: Implement `OllamaAdapter` following `RuntimeAdapter` protocol. Reuse Ollama HTTP API pattern from `conversation_analyzer.py`.

| File | Action |
|------|--------|
| `scripts/lib/ollama_adapter.py` | NEW — RuntimeAdapter impl for Ollama HTTP API |
| `scripts/lib/runtime_facade.py` | MODIFY — Add ollama adapter selection |
| `tests/test_ollama_adapter.py` | NEW — Unit tests + integration test with live Ollama |

**Dependencies**: F28 merged (RuntimeAdapter protocol available)

### PR-3: Smart Routing Integration (~80 lines)

**Scope**: Wire `TaskClassifier` into dispatcher. Add `VNX_ROUTING=smart` feature flag.

| File | Action |
|------|--------|
| `scripts/lib/subprocess_dispatch.py` | MODIFY — Add classifier call before adapter selection |
| `scripts/dispatcher_v8_minimal.sh` | MODIFY — Pass routing flag to Python dispatch layer |
| `tests/test_smart_routing_integration.py` | NEW — End-to-end routing tests |

**Dependencies**: PR-1, PR-2

### PR-4: Housekeeping Sidecar (~150 lines)

**Scope**: Implement the housekeeping daemon from §4.

| File | Action |
|------|--------|
| `scripts/housekeeping_sidecar.py` | NEW — Daemon with 5-min poll cycle |
| `scripts/lib/ollama_client.py` | NEW — Shared Ollama HTTP client (extracted from conversation_analyzer) |
| `bin/vnx` | MODIFY — Add `vnx housekeeping start/stop/status` commands |
| `tests/test_housekeeping_sidecar.py` | NEW — Unit tests for each job |

**Dependencies**: PR-2 (OllamaAdapter for shared client)

### PR-5: /idea Slash Command (~100 lines)

**Scope**: Implement the `/idea` skill from §5.

| File | Action |
|------|--------|
| `.claude/skills/idea/SKILL.md` | NEW — Skill definition |
| `scripts/idea_intake.py` | NEW — Classification + enrichment + storage |
| `.vnx-data/ideas/` | NEW dir — Idea YAML storage |
| `tests/test_idea_intake.py` | NEW — Unit tests for classification and storage |

**Dependencies**: PR-4 (shared Ollama client)

---

## 8. Dependencies and Sequencing

### Feature Dependencies

```
F28 (SubprocessAdapter)
  │
  ├──→ F29 (Agent Stream / Dashboard)
  │
  └──→ F30 (Smart Model Routing)
        │
        ├── PR-1: TaskClassifier (no deps)
        ├── PR-2: OllamaAdapter (needs F28 RuntimeAdapter protocol)
        ├── PR-3: Smart Routing (needs PR-1 + PR-2)
        ├── PR-4: Housekeeping Sidecar (needs PR-2)
        └── PR-5: /idea Command (needs PR-4)
```

### Prerequisites

| Prerequisite | Status | Impact |
|-------------|--------|--------|
| F28 merged (SubprocessAdapter + RuntimeAdapter protocol) | In progress (PRs 1-4 merged) | PR-2 and PR-3 blocked until merged |
| Ollama installed on operator machine | Assumed | PR-2 must degrade gracefully if absent |
| At least one local model pulled (`qwen2.5-coder:14b`) | Assumed | Calibration step in PR-1 verifies |

### Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Local model quality insufficient for code tasks | High | Routing defaults to Sonnet; local only for housekeeping initially |
| Ollama unavailable or crashed | Medium | Graceful degradation — all tasks fall back to SubprocessAdapter |
| Routing misclassification sends complex task to 3B | High | `model_floor` override; initial deployment with conservative thresholds |
| Housekeeping sidecar interferes with active dispatches | Medium | Sidecar is read-only for dispatch/receipt data; only modifies OIs and leases |

---

## 9. Open Questions

1. **Model calibration**: Should PR-1 include a mandatory `vnx calibrate` step that benchmarks local models on the operator's hardware and stores performance profiles?
2. **Idea → Dispatch pipeline**: Should `/idea` with type=feature automatically create a draft dispatch, or should it remain in the kanban/ideas queue until manually promoted?
3. **Cost tracking**: Should routing decisions be logged with estimated cost savings (local = $0 vs Sonnet/Opus token cost)?
4. **MoE models**: qwen3.5:35b-a3b (35B params, 3B active) may offer better quality than 14B at similar speed. Should this be the default local model instead of 14B?
