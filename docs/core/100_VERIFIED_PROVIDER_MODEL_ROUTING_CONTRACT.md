# Verified Provider And Model Routing Contract

**Status**: Canonical
**Feature**: Verified Provider and Model Routing Enforcement
**PR**: PR-0
**Gate**: `gate_pr0_routing_contract`
**Date**: 2026-04-01
**Author**: T3 (Track C Architecture)

This document is the single source of truth for provider and model routing in VNX dispatches. All downstream PRs (PR-1 through PR-4) implement against this contract. Any component that selects a provider, switches a model, or records runtime identity must conform to the rules defined here.

---

## 1. Why This Exists

### 1.1 The Problem

Provider and model routing in VNX is currently best-effort advice. Dispatches carry `Requires-Provider` and `Requires-Model` fields, but the system treats mismatches as warnings and model switches as fire-and-forget commands. This creates three concrete failures:

1. **Silent provider mismatch**: A dispatch requiring `codex_cli` lands on a `claude_code` terminal. The dispatcher logs a warning. Work proceeds on the wrong provider. The receipt does not record that the provider requirement was violated.

2. **Unverified model switching**: A dispatch requiring `opus` sends `/model default` to the terminal. The dispatcher waits 4 seconds and assumes success. If the switch failed (rate limit, unsupported, CLI error), work proceeds on the wrong model. The receipt does not record what model actually handled the work.

3. **Invisible pinned-terminal assumptions**: T1 and T2 are manually Sonnet-pinned. T0 and T3 run Opus. These are stated in CLAUDE.md files but not machine-checkable. No dispatch can prove its model requirement was satisfied by the terminal's pinned state.

### 1.2 The Fix

Move provider and model routing from:
- **Best-effort advice with warning-only mismatch** (current)

To:
- **Auditable execution intent with deterministic mismatch behavior and recorded runtime identity**

That means: dispatches declare what they need, the system enforces or explicitly degrades, and receipts prove what actually ran.

---

## 2. Routing Dimensions

A dispatch's routing requirements are expressed across four independent dimensions. Each dimension has its own enforcement semantics.

### 2.1 Dimension Definitions

| Dimension | Field | Purpose | Example Values |
|-----------|-------|---------|----------------|
| **Provider** | `Requires-Provider` | Which CLI tool must execute the work | `claude_code`, `codex_cli`, `gemini_cli` |
| **Model** | `Requires-Model` | Which LLM model must handle the work | `opus`, `sonnet`, `haiku`, `default` |
| **Capability** | Task class (from skill mapping) | What kind of work the dispatch carries | `coding_interactive`, `research_structured`, `docs_synthesis` |
| **Execution Mode** | `Mode` | What cognitive mode the CLI should operate in | `normal`, `thinking`, `planning` |

### 2.2 Dimension Independence

These dimensions are orthogonal. A dispatch may require:
- A specific provider without a specific model (use whatever model that provider is running)
- A specific model without a specific provider (any provider running that model satisfies)
- A capability without either (route by task class alone)
- An execution mode without any of the above (mode is a terminal configuration, not a routing constraint)

This independence is important: collapsing dimensions (e.g., treating `claude_code` as implying `opus`) creates invisible coupling that breaks when terminal configurations change.

---

## 3. Requirement Strength: Required vs Advisory

Every routing dimension supports two enforcement levels.

### 3.1 Required (Hard Blocker)

A **required** routing constraint means: if the system cannot satisfy this requirement, the dispatch MUST NOT proceed. The dispatcher must block delivery, record the reason, and requeue or fail the dispatch.

**When to use required**: When the work product depends on the specific provider or model. Examples:
- A Codex-specific code review that uses Codex-only features
- An Opus-grade architecture analysis where Sonnet would produce insufficient depth
- A Gemini review gate that must produce Gemini-sourced evidence

### 3.2 Advisory (Operator Warning)

An **advisory** routing constraint means: the dispatch prefers this provider or model, but work may proceed if the preference cannot be satisfied. The dispatcher logs a visible warning. The receipt records the mismatch.

**When to use advisory**: When the preference improves quality but the work is not invalidated by a different provider or model. Examples:
- Preferring Sonnet for routine coding tasks (cost optimization)
- Preferring a specific provider for consistency in a chain

### 3.3 Strength Declaration

Requirement strength is declared per-dimension in the dispatch Manager Block:

```
Requires-Provider: claude_code          # Advisory (default)
Requires-Provider: claude_code required  # Hard blocker
Requires-Model: opus                    # Advisory (default)
Requires-Model: opus required           # Hard blocker
```

**Default strength is advisory.** This preserves backward compatibility with all existing dispatches. Required strength must be explicitly declared.

### 3.4 Enforcement Matrix

| Dimension | Strength | Match | Behavior |
|-----------|----------|-------|----------|
| Provider | Required | Match | Proceed |
| Provider | Required | Mismatch | **Block dispatch. Do not deliver.** Record reason. Requeue or fail. |
| Provider | Advisory | Match | Proceed |
| Provider | Advisory | Mismatch | **Warn and proceed.** Record mismatch in dispatch evidence. |
| Model | Required | Verified match | Proceed |
| Model | Required | Unverified | **Block dispatch.** Cannot prove model requirement is satisfied. |
| Model | Required | Verified mismatch | **Block dispatch.** Record reason. |
| Model | Advisory | Match or unverified | Proceed (best-effort) |
| Model | Advisory | Mismatch | **Warn and proceed.** Record mismatch. |
| Capability | Always hard | Match | Proceed |
| Capability | Always hard | Mismatch | **Block dispatch.** Target does not support task class. (Existing R-5 invariant.) |
| Execution Mode | Always advisory | Any | Best-effort mode activation. Mode failures do not block dispatch. |

---

## 4. Runtime Identity Recording

Every dispatch execution must record what actually ran, not just what was requested. This is the bridge between routing intent and audit evidence.

### 4.1 Runtime Identity Fields

After dispatch delivery, the following fields must be recorded in the dispatch evidence (coordination events, receipts, or both):

| Field | Source | Description |
|-------|--------|-------------|
| `requested_provider` | Dispatch `Requires-Provider` field | What provider the dispatch asked for |
| `actual_provider` | Terminal configuration (`VNX_Tn_PROVIDER` or registry) | What provider the terminal is running |
| `provider_match` | Comparison result | `match`, `mismatch_advisory`, `mismatch_blocked` |
| `requested_model` | Dispatch `Requires-Model` field | What model the dispatch asked for |
| `actual_model` | Post-switch verification or pinned assumption | What model handled the work |
| `model_switch_result` | Switch verification logic | `switched`, `already_active`, `unsupported`, `failed`, `unverified`, `not_requested` |
| `model_match` | Comparison result | `verified_match`, `assumed_match`, `mismatch_advisory`, `mismatch_blocked`, `unverified` |
| `execution_mode` | Mode activation result | `normal`, `thinking`, `planning`, `activation_failed` |
| `identity_recorded_at` | Timestamp | When identity was captured |

### 4.2 Recording Points

Runtime identity is recorded at three points in the dispatch lifecycle:

1. **Pre-delivery** (dispatcher `configure_terminal_mode`): Record requested vs terminal provider. Record model switch attempt and result.
2. **Post-delivery** (receipt generation): Record actual execution evidence. Include identity fields in the receipt.
3. **Post-completion** (report processing): Validate that recorded identity is consistent with the dispatch's routing requirements.

### 4.3 Recording By Execution Context

| Context | Provider Source | Model Source | Recording Mechanism |
|---------|---------------|-------------|-------------------|
| Interactive tmux | `VNX_Tn_PROVIDER` env var or `panes.json` | Pinned assumption or post-switch verification | Coordination event at delivery time |
| Headless CLI | Process invocation arguments | CLI `--model` flag or environment | Structured output capture |
| Channel adapter | Adapter configuration | Adapter configuration | Adapter completion callback |

---

## 5. Model Switch Verification

Model switching is the highest-risk routing operation because the current implementation has no feedback loop. This section defines the verification contract.

### 5.1 Switch Result States

Every model switch attempt produces exactly one of these result states:

| State | Meaning | Dispatch May Proceed (Required) | Dispatch May Proceed (Advisory) |
|-------|---------|-------------------------------|-------------------------------|
| `switched` | Model switch command sent and post-switch verification confirms the new model is active | **Yes** | Yes |
| `already_active` | Requested model is already the active model (no switch needed) | **Yes** | Yes |
| `unsupported` | Provider does not support runtime model switching (e.g., Gemini CLI) | **No** | Yes (with warning) |
| `failed` | Model switch command was sent but post-switch verification shows the old model is still active | **No** | Yes (with warning) |
| `unverified` | Model switch command was sent but verification is not available or not implemented | **No** | Yes (with warning) |
| `not_requested` | No model switch was requested | N/A | N/A |

### 5.2 Verification Methods

Post-switch verification depends on the provider:

| Provider | Verification Method | Reliability |
|----------|-------------------|-------------|
| `claude_code` | Capture pane output after `/model` command; parse model confirmation message | Medium — depends on CLI output format stability |
| `codex_cli` | Capture pane output after `/model` command; parse confirmation | Medium |
| `gemini_cli` | Not applicable — does not support runtime model switching | N/A |

### 5.3 Pinned Terminal Model Assumptions

When runtime model switching is unavailable or unverified, the system falls back to **pinned terminal assumptions**: the operator-declared model configuration for each terminal.

Current pinned assumptions:
- T0: Opus (review/orchestration)
- T1: Sonnet (implementation)
- T2: Sonnet (testing/integration)
- T3: Opus (review/certification)

Pinned assumptions are declared in:
1. Terminal CLAUDE.md files (human-readable policy)
2. Terminal environment configuration in `bin/vnx` (machine-readable)
3. Execution target registry `model` field (runtime state)

### 5.4 Pinned Assumption Rules

| Rule | Description |
|------|-------------|
| PA-1 | A pinned assumption satisfies a **required** model constraint only when the assumption source is machine-verifiable (env var or registry, not just CLAUDE.md text). |
| PA-2 | A pinned assumption satisfies an **advisory** model constraint unconditionally (the assumption is logged but not verified). |
| PA-3 | When a dispatch requires a model that differs from the terminal's pinned assumption and runtime switching is `unsupported` or `unverified`, the dispatch is blocked (required) or warned (advisory). |
| PA-4 | Pinned assumptions must be refreshable: if an operator changes a terminal's model, the assumption source must be updatable without restarting the VNX session. |
| PA-5 | Pinned assumptions are recorded in runtime identity as `actual_model` with `model_match: assumed_match` to distinguish from verified switches. |

---

## 6. Provider Mismatch Behavior

### 6.1 Detection

Provider mismatch is detected by comparing the dispatch's `Requires-Provider` field against the terminal's configured provider (`VNX_Tn_PROVIDER` env var, falling back to `panes.json`, falling back to `claude_code` default).

### 6.2 Required Provider Mismatch (Fail-Closed)

When `Requires-Provider` is marked `required` and the terminal provider does not match:

1. The dispatcher MUST NOT deliver the dispatch to that terminal.
2. The dispatcher records a `provider_mismatch_blocked` coordination event with:
   - `requested_provider`
   - `actual_provider`
   - `terminal_id`
   - `dispatch_id`
   - `reason: "required provider mismatch"`
3. The dispatch is requeued for a terminal that matches, or failed if no matching terminal exists.
4. T0 receives explicit feedback: "Dispatch X requires provider Y but terminal Z runs provider W."

### 6.3 Advisory Provider Mismatch (Warn-Through)

When `Requires-Provider` is advisory (default) and the terminal provider does not match:

1. The dispatcher logs a structured warning (not just a log line — a coordination event).
2. The dispatch proceeds to delivery.
3. The receipt includes `provider_match: mismatch_advisory` in its identity fields.
4. T0 can see the mismatch in receipt review but is not blocked.

---

## 7. Capability Routing Integration

Capability routing via task classes is already defined in the FPC Execution Contracts (30_FPC_EXECUTION_CONTRACTS.md). This contract does not redefine capability routing. It clarifies the relationship:

### 7.1 Routing Order

When a dispatch has multiple routing dimensions specified, they are evaluated in this order:

1. **Capability** (task class → target type): Hard filter. Eliminates targets that cannot handle the task class. (Existing R-5.)
2. **Provider** (required or advisory): Filters or warns based on provider match.
3. **Model** (required or advisory): Filters or warns based on model match/verification.
4. **Execution Mode**: Applied after target selection. Does not affect target selection.

### 7.2 Combined Filtering

All hard filters must pass for a target to be eligible. If multiple targets pass all hard filters, the dispatcher selects based on:
1. Health state (healthy preferred over degraded)
2. Terminal affinity (track → terminal mapping: A→T1, B→T2, C→T3)
3. T0 override via `target_id` metadata (respects hard filters)

---

## 8. Dispatch Field Specification

### 8.1 Manager Block Fields

These fields appear in the dispatch Manager Block between `[[TARGET:X]]` and the dispatch body:

```
[[TARGET:B]]
Requires-Provider: claude_code [required]
Requires-Model: opus [required]
Mode: thinking
ClearContext: true
ForceNormalMode: false
```

### 8.2 Field Grammar

```
Requires-Provider: <provider_id> [required]
Requires-Model: <model_id> [required]
Mode: normal | thinking | planning | none
ClearContext: true | false
ForceNormalMode: true | false
```

- `<provider_id>`: One of `claude_code`, `codex_cli`, `gemini_cli`. Case-insensitive on parse, normalized to lowercase.
- `<model_id>`: One of `opus`, `sonnet`, `haiku`, `default`. Case-insensitive on parse, normalized to lowercase. `opus` normalizes to `default` for Claude Code (to select Opus 4.6 1M context, not 200K).
- `[required]`: Optional suffix. When present, the field is a hard blocker. When absent, the field is advisory.
- `Mode`, `ClearContext`, `ForceNormalMode`: Unchanged from current behavior. These are terminal configuration, not routing constraints.

### 8.3 Backward Compatibility

All existing dispatches that use `Requires-Provider` or `Requires-Model` without the `required` suffix continue to work as advisory preferences. No existing dispatch behavior changes.

---

## 9. Migration Path: Toward Provider-Agnostic Actor Routing

This contract is designed to support — but not implement — a future where dispatches route to actors rather than terminals.

### 9.1 Current State: Terminal-Pinned Routing

Today, routing is terminal-pinned:
- T0 = Opus orchestration
- T1 = Sonnet implementation (Track A)
- T2 = Sonnet testing (Track B)
- T3 = Opus review (Track C)

The routing dimensions (provider, model, capability, mode) are evaluated against terminal configurations. The terminal ID is the final routing target.

### 9.2 Future State: Actor-Based Routing

In a future iteration, the routing target could be an **actor** — a named execution context with declared capabilities, provider, model, and session state — decoupled from a fixed terminal slot.

This contract supports that migration because:

1. **Routing dimensions are evaluated against target properties, not terminal IDs.** The Execution Target Registry already stores provider type and model per target, not per terminal.
2. **Runtime identity recording is target-scoped.** The identity fields (`actual_provider`, `actual_model`) describe what ran, not where it ran.
3. **Pinned assumptions are a fallback, not the primary mechanism.** As runtime verification improves, pinned assumptions can be phased out without changing the contract.
4. **Requirement strength (required/advisory) is dispatch-scoped.** It does not encode terminal topology.

### 9.3 Non-Goals For This Feature

To keep scope bounded, the following are explicitly out of scope for the Verified Provider and Model Routing Enforcement feature:

| Non-Goal | Reason |
|----------|--------|
| Dynamic actor provisioning | Requires session management infrastructure not yet built |
| Cross-provider model equivalence mapping | "Opus-equivalent on Gemini" is undefined and subjective |
| Automatic terminal re-assignment on mismatch | Requires queue-aware rerouting; current requeue is sufficient |
| Provider-specific capability negotiation | Each provider's feature set is too different for a generic capability API |
| Runtime model switching for Gemini | Gemini CLI does not support it; forcing it would be provider-specific hacking |
| Terminal abstraction rewrite | This contract layers on existing terminal infrastructure, not replaces it |

---

## 10. Contract Invariants

These invariants are binding on all implementations. Violations are dispatch safety failures.

| ID | Invariant | Type |
|----|-----------|------|
| VR-1 | A dispatch with `Requires-Provider: X required` MUST NOT be delivered to a terminal running provider Y where Y != X. | Hard |
| VR-2 | A dispatch with `Requires-Model: X required` MUST NOT proceed when model verification returns `unsupported`, `failed`, or `unverified`. | Hard |
| VR-3 | Every dispatch delivery MUST record `actual_provider` and `actual_model` in its runtime identity evidence. | Hard |
| VR-4 | A model switch result of `unsupported` or `failed` MUST be recorded as a coordination event, not silently absorbed. | Hard |
| VR-5 | Pinned terminal assumptions satisfy required model constraints only when the assumption source is machine-verifiable (PA-1). | Hard |
| VR-6 | Advisory routing mismatches MUST be recorded in dispatch evidence (coordination events and/or receipts). They MUST NOT be silently dropped. | Hard |
| VR-7 | Default requirement strength is advisory. Existing dispatches without `required` suffix retain their current behavior. | Compatibility |
| VR-8 | Routing dimension evaluation order is: capability → provider → model → execution mode. | Hard |
| VR-9 | Routing dimensions are independent. A provider requirement does not imply a model requirement or vice versa. | Structural |
| VR-10 | Runtime identity fields are target-scoped, not terminal-scoped, to support future actor-based routing. | Structural |

---

## 11. Implementation Guidance For Downstream PRs

This section maps contract rules to implementation work. It is guidance, not specification — downstream PRs own their implementation details.

### PR-1: Fail-Closed Provider Enforcement

- Parse `required` suffix from `Requires-Provider` field in `dispatch_metadata.sh`
- In `configure_terminal_mode`, promote provider mismatch from `log` to `return 1` when strength is required
- Emit `provider_mismatch_blocked` coordination event on block
- Preserve existing warning behavior for advisory (no `required` suffix)
- Add tests: required match, required mismatch (blocked), advisory match, advisory mismatch (warned)

### PR-2: Verified Model Switching

- Parse `required` suffix from `Requires-Model` field
- After `/model` command, capture pane output and parse for model confirmation
- Map result to switch states: `switched`, `already_active`, `unsupported`, `failed`, `unverified`
- Block delivery for required model when result is `unsupported`, `failed`, or `unverified`
- Record `requested_model`, `actual_model`, `model_switch_result`, `model_match` in coordination event
- Add tests for each switch result state

### PR-3: Preflight Provider Readiness

- At kickoff or promotion, read feature/review metadata for provider and model requirements
- Check terminal configuration against requirements before dispatching
- Report readiness gaps: `unsupported` (provider doesn't exist), `unavailable` (provider exists but terminal not configured), `misconfigured` (terminal configured but model doesn't match pinned assumption)
- Check pinned assumptions against env vars / registry, not just CLAUDE.md text

### PR-4: Certification

- Run mixed-provider scenario: dispatch requiring `codex_cli` on a `claude_code` chain
- Run mixed-model scenario: dispatch requiring `opus` on a Sonnet-pinned terminal
- Verify blocking, evidence recording, and receipt completeness
- Require Gemini review gate and Codex final gate on routing-core PRs

---

## Appendix A: Glossary

| Term | Definition |
|------|-----------|
| **Provider** | The CLI tool that executes dispatches: `claude_code`, `codex_cli`, `gemini_cli` |
| **Model** | The LLM model behind the provider: `opus`, `sonnet`, `haiku` |
| **Capability** | A task class describing the nature of work: `coding_interactive`, `research_structured`, etc. |
| **Execution Mode** | The cognitive mode of the CLI: `normal`, `thinking`, `planning` |
| **Pinned Terminal** | A terminal whose model is set by operator configuration, not runtime switching |
| **Runtime Identity** | The actual provider, model, and mode that handled a dispatch, recorded after execution |
| **Advisory** | A routing preference that warns on mismatch but does not block delivery |
| **Required** | A routing constraint that blocks delivery on mismatch |
| **Switch Verification** | Post-model-switch confirmation that the requested model is actually active |

## Appendix B: Relationship To Existing Contracts

| Contract | Relationship |
|----------|-------------|
| 30_FPC_EXECUTION_CONTRACTS | This contract extends FPC with provider and model routing. Capability routing (task class → target type) is unchanged. |
| 80_TERMINAL_EXCLUSIVITY_CONTRACT | Terminal exclusivity (one dispatch per terminal) is orthogonal to provider/model routing. Both must pass for delivery. |
| 90_DELIVERY_FAILURE_LEASE_CONTRACT | Delivery failures from provider/model mismatch interact with lease cleanup. A blocked dispatch releases its lease. |
| 42_FPD_PROVENANCE_CONTRACT | Runtime identity recording extends provenance. The `actual_provider` and `actual_model` fields become part of the provenance chain. |
