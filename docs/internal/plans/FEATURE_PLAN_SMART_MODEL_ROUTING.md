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

**Repository**: Various implementations exist; the pattern is a CLI-driven task decomposer.

**Decomposition heuristic**: GSD-style tools typically use a 3-phase approach:
1. **Capture**: Free-form feature description input
2. **Decompose**: LLM breaks feature into atomic tasks with explicit acceptance criteria
3. **Sequence**: Tasks ordered by dependency graph, each tagged with estimated complexity

**Key insight for F30**: The decomposition prompt matters more than the decomposition algorithm. A well-crafted system prompt with examples of "good" task breakdowns produces consistent granularity. The heuristic is: each task should be completable in a single focused session (1-2 PRs).

### 1.2 Ralph Loops

**Pattern**: Iterative refinement loops for feature implementation.

**Decomposition heuristic**: Features are split using a "vertical slice" model:
1. Each slice delivers end-to-end functionality (not horizontal layers)
2. Complexity is estimated by counting: files touched, new APIs, new DB schema, new tests
3. If a slice exceeds 3 of these 4 dimensions, it gets further decomposed

**Key insight for F30**: The 4-dimension complexity estimator is directly applicable to VNX routing decisions. A task touching 1 file with no new APIs = local model candidate. A task touching 5+ files with new schema = Opus.

### 1.3 DimitriGeelen/agentic-engineering-framework

**Repository**: Agentic engineering framework with task management and healing loops.

**Key patterns**:
- **Task system**: Tasks have type (implement, test, review, refactor), complexity (S/M/L/XL), and risk (low/med/high). These three axes drive agent selection.
- **Healing loop**: Failed tasks are analyzed, the failure pattern is classified, and a corrective task is auto-generated. This runs until the task passes or a retry limit is hit.
- **Learning promotion**: When a pattern (failure or success) repeats 3+ times, it's promoted from task-local context to project-level memory. This prevents the same mistakes across sessions.

**Key insight for F30**: The healing loop pattern is directly applicable to the housekeeping sidecar — stale OIs, broken leases, and orphaned dispatches are all "failures" that can be auto-corrected by a local model following deterministic rules.

### 1.4 DimitriGeelen/termlink

**Repository**: Terminal session routing with MCP tools.

**Key patterns**:
- **Session routing**: Tasks are routed to terminals based on a capability matrix — each terminal declares what it can do (code, test, review), and the router matches tasks to capable terminals.
- **MCP integration**: Uses Model Context Protocol for tool access, allowing different models to share the same tool surface.

**Key insight for F30**: VNX already has terminal specialization (T1=implement, T2=test, T3=review). The gap is model specialization within those terminals. termlink's capability matrix can inform our routing decision tree.

---

## 2. Local Model Benchmark Summary

### 2.1 Model Capabilities by Size

| Size | Representative Model | Realistic Tasks | Limitations |
|------|---------------------|----------------|-------------|
| **3B** | qwen2.5-coder:3b | Classification, tagging, JSON extraction, text summarization, stale-check heuristics | Cannot generate correct multi-file code; hallucination rate >30% on novel tasks |
| **7B** | qwen2.5-coder:7b | Formatting, linting suggestions, single-function refactors, docstring generation, simple test scaffolds | Struggles with cross-file context; accuracy drops sharply beyond 2K token output |
| **14B** | qwen2.5-coder:14b | Small refactors (<50 lines), code review with structured output, test generation for isolated functions, bash script fixes | Cannot maintain coherence across 3+ file changes; misses edge cases in complex logic |

### 2.2 Performance on Apple Silicon

Based on community benchmarks and VNX's existing benchmark framework:

| Model | M1 Pro (16GB) | M2 Pro (32GB) | M3 Pro (36GB) | M4 Pro (48GB) |
|-------|--------------|---------------|---------------|---------------|
| 3B | ~60 tok/s | ~70 tok/s | ~80 tok/s | ~90 tok/s |
| 7B | ~30 tok/s | ~40 tok/s | ~50 tok/s | ~55 tok/s |
| 14B | ~15 tok/s | ~20 tok/s | ~25 tok/s | ~30 tok/s |
| 35B (MoE, 3B active) | ~10 tok/s | ~15 tok/s | ~20 tok/s | ~25 tok/s |

**Note**: These are approximate ranges from ollama community benchmarks. VNX already has `scripts/llm_benchmark_coding_v2.py` which can produce exact numbers for the operator's specific hardware. PR-1 should include a calibration step.

### 2.3 Code Quality Benchmarks

| Model | HumanEval (pass@1) | MBPP (pass@1) | Practical Assessment |
|-------|-------------------|---------------|---------------------|
| qwen2.5-coder:3b | ~45% | ~50% | Useful only for classification/extraction, not generation |
| qwen2.5-coder:7b | ~65% | ~68% | Can handle single-function tasks with clear specs |
| qwen2.5-coder:14b | ~78% | ~80% | Competitive with GPT-3.5; reliable for scoped tasks |
| qwen3.5:9b | ~70% | ~72% | Good reasoning, moderate code quality |
| devstral | ~72% | ~75% | Strong on code editing, weaker on generation |

**Key finding**: 14B models are the sweet spot for local code tasks — fast enough for interactive use (~20-30 tok/s on modern Macs), accurate enough for scoped single-file changes. Below 7B, code generation quality is too unreliable for production use; classification and extraction remain viable.

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

Reuse the existing Ollama HTTP API pattern from `scripts/conversation_analyzer.py`:

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
