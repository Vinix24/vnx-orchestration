# Open Method: Building VNX with AI, for AI

**Author**: Vincent (T-MANAGER)
**Date**: February 2026
**Status**: Transparency Report

---

## The "Vibecoding" Trap

Critics argue that "vibecoding"—letting AI generate code without discipline—leads to unmaintainable software. **I agree with them.**

That is exactly why I built VNX.

I am not a traditional systems engineer. I am a product architect who saw the potential of AI agents but was terrified by their lack of discipline. I didn't build VNX *with* AI to avoid coding; I built it *for* AI to enforce engineering discipline.

## My Methodology: Deterministic Engineering

### 1. Architect First, Prompt Second
I didn't just ask Claude to "make an orchestrator". I designed the **Architecture** first: the Receipt Schema (NDJSON) as the non-negotiable contract, and the Staging Lifecycle to mimic a human review process.

### 2. The "Glass Box" Principle
I refused to use "magic" abstractions. I chose readable Python/Bash and file-based state because I want to be able to audit every line the AI generates and every state transition the system makes.

### 3. Hook-Agnostic by Design
Quality intelligence, usage tracking, and governance run from receipts and watchers, not provider-specific hooks. Hooks can enrich metadata when available, but the system does not depend on them. This keeps VNX model-agnostic across Claude, Codex, Gemini, and future providers.

### 4. AI as the Junior Developer
I treat agents as Junior Developers. I write the spec, they write the implementation, I review the code (Governance Gate), and VNX runs the tests.

### 5. Tooling: Claude Code as the Primary Interface
About 80% of the workflow runs through **Claude Code**. It serves as both the development environment and the enforcement mechanism — T0's write restrictions are implemented through Claude Code hooks that prevent the orchestrator from modifying files directly. This isn't just tooling preference; it's a governance property. The orchestrator coordinates and dispatches, but all file modifications happen in worker terminals under the governance layer's oversight.

I started with Claude Opus as T0 and the hook-based write isolation kept it that way. Other models might work as orchestrator, but T0-as-Opus is the only tested configuration (as of this writing, T0 is pinned to Opus 5.5).

## Current State & The Road Ahead

*(Written February 2026 — describes the terminal-grid state at that point; the current default lane runs ephemeral per-dispatch workers instead, see `HEADLESS_TRANSITION.md`.)*

The current implementation runs in my daily workflow, orchestrating 3-4 terminals (Claude Code + Codex) with graceful recovery from crashes via ledger replay. It's not a "zero-downtime production" system; it's a working prototype that I trust enough to build real software with.

## Provenance Reality (Early History)

VNX started life inside a private product repository. When the architecture matured, I split it into its own Git repository. That means the **public history begins at the separation point**, and earlier work is represented as a **squashed baseline**. This is deliberate: it keeps the product repo private while still exposing the architecture and current implementation clearly.

For a reader-friendly chronology of the technical evolution, see `EVOLUTION_TIMELINE.md`.

For broader adoption, this architecture would benefit from a memory-safe implementation (Rust or Go). The current Python/Bash prototype proves the concepts; a production-grade CLI would require careful engineering.

**If you're a Rust/Go engineer or systems architect interested in governance tooling for AI workflows, I'd love to collaborate on what that might look like.**

---
*Let's stop "vibecoding" and start Engineering.*
