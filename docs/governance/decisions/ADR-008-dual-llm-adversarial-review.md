# ADR-008 — Dual-LLM Adversarial Review (`codex_gate` + `gemini_review`) with `contract_hash` Binding

**Status:** Accepted
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Mandatory triple-gate policy (codex + gemini + CI green); review-gate evidence model

## Context

VNX dispatches code through one or more worker terminals (T1/T2/T3) and accumulates each PR through a series of gates before merge. Anthropic shipped native Code Review on 2026-03-09, and the strategic replan (`claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §1) names the question explicitly: "do we still need a custom review pipeline now that the platform vendor ships one?"

The answer is yes — but the reasoning is structural, not feature-driven. Anthropic Code Review reviews **Claude work using Claude.** The reviewer and the author share the same model family, the same training cutoff, and the same blind spots. Empirically through the spring 2026 PR chains:

- `codex_gate` (OpenAI Codex CLI) caught migration-list omissions in PR #432 that `gemini_review` missed (specifically, migration 0016 missing from the apply list — round-3 finding).
- `gemini_review` caught dashboard SSE regressions in PR #411 series that `codex_gate` did not flag (specifically, the streaming-drainer reuse-after-close pattern).
- A single-vendor reviewer would have missed at least one blocking finding per PR in the W7 chain.

The cross-vendor review is the moat, and the strategic replan §4 codifies it as moat M3: "Custom dual-LLM gate (codex + gemini reviewing Claude work) — Anthropic will never ship 'use Codex to gate Claude'; structural conflict."

VNX has shipped this pattern operationally for months, but it has not been written down as a binding architectural rule. This ADR codifies it.

## Decision

**Every PR with non-trivial scope passes through TWO independent LLM reviewers — `codex_gate` (OpenAI Codex CLI) and `gemini_review` (Google Gemini CLI) — orchestrated through `scripts/review_gate_manager.py`. Each review produces a structured result bound to the active review contract via a non-empty `contract_hash`. T0 cannot complete a PR without three evidence surfaces per gate (request, result, normalized markdown report). Disagreement between gates triggers operator review.**

Concretely:

1. **Two reviewers, not one.** The default required gates are `codex_gate` and `gemini_review`. Either gate alone is insufficient evidence. Anthropic's native Code Review MAY supplement but does NOT replace either gate.

2. **Three evidence surfaces per gate.**
   - **Request:** `.vnx-data/state/review_gates/requests/<gate>_<dispatch>.json` — proves the gate was triggered.
   - **Result:** `.vnx-data/state/review_gates/results/<gate>_<dispatch>.json` — proves the gate ran and produced a verdict.
   - **Normalized report:** `$VNX_DATA_DIR/unified_reports/headless/<gate>_<dispatch>.md` — operator-readable record of findings.

3. **`contract_hash` binding.** Every result records the SHA-256 hash of the active review contract (the YAML/JSON describing what the gate checks for). A result with empty `contract_hash` is incomplete evidence. A result whose `contract_hash` does not match the active contract version is a stale result and does not satisfy the gate.

4. **Closure verifier.** `scripts/closure_verifier.py` blocks PR completion when any of the three surfaces is missing, when `contract_hash` is empty, or when `report_path` is empty. T0's CLAUDE.md ("Headless Review Enforcement" §3-9) restates the same rule operationally.

5. **Disagreement handling.** When both gates run successfully but reach contradicting verdicts (e.g., codex blocking, gemini pass), T0 routes to operator review. T0 does not silently prefer one verdict over the other.

6. **`queued` is not evidence.** A request record alone — without a matching result and report — is not closure evidence. T0's CLAUDE.md §"Doubt Escalation Policy" enforces: "either start execution, dispatch execution, or classify the missing runner path as a blocker."

## Reasoning

1. **Same-vendor reviewers share blind spots.** A reviewer trained on a similar corpus to the author will rationalize the same patterns. Empirically, the ~200-PR spring 2026 chain shows that codex and gemini surface non-overlapping sets of findings: codex is stronger at SQL semantics, schema drift, and migration list completeness; gemini is stronger at frontend behavioral regressions, SSE lifecycle, and TypeScript type narrowing. Either alone would have missed multiple production-blocking issues.

2. **Anthropic cannot ship the moat.** Strategic replan §4 moat M3 states the structural argument: Anthropic's Code Review uses Claude to review Claude. They will not ship "use Codex to gate Claude" because it requires depending on a competitor's CLI. Therefore the dual-vendor adversarial property is a feature only an operator-controlled system can offer — it is the moat that survives any platform-vendor's vertical integration.

3. **`contract_hash` makes evidence falsifiable.** Without contract binding, a result file is a verdict with no provenance. With `contract_hash`, a verdict is bound to a specific set of checks; if the contract changes (new finding category added, severity threshold tightened), old results are automatically invalidated. This is the same pattern as git's commit hashes — content-addressed evidence rather than mutable label-addressed.

4. **Three surfaces, not one, because each surface answers a different question.** Request answers "was the gate triggered?" Result answers "what did the gate decide?" Report answers "what did the gate find?" Operator review needs all three: an empty report with a pass result is suspicious; a result with no request is unprovenanced; a request without a result is incomplete. T0's CLAUDE.md "Headless Review Enforcement" enumerates this explicitly because every surface combination has been observed as a failure mode in the field.

5. **Operator memory `feedback_mandatory_triple_gate` is the operational form of this ADR.** The memory entry says: "Every PR: codex gate → gemini review → CI green → merge. No exceptions, no skipping." This ADR is the architectural justification for that rule. Without the ADR, the rule is policy that drifts; with the ADR, the rule has structural reasoning behind it.

6. **F28 gate-skip incident is the prior failure.** Operator memory `feedback_gate_enforcement_failure_f28`: "T0 skipped all 5 F28 gates before merge; operator flagged as unacceptable; NEVER skip gates." That incident demonstrated that without enforced evidence surfaces, T0 can rationalize bypassing gates under time pressure. The closure_verifier + three-surface rule is the structural defense against the rationalization.

7. **Disagreement is a feature.** Two reviewers reaching different verdicts is not a bug — it is the signal the operator wants. A pass-pass result is consensus; a pass-fail result is exactly the case where human review adds the most value. The system explicitly does not auto-resolve disagreement because auto-resolution would discard the moat.

8. **Codex CLI rate limits are a known risk, not a reason to skip.** Operator policy A1 (T0 CLAUDE.md): "Codex CLI rate-limited → wait for reset (default 5h+, max 5d acceptable). NEVER fall back unless explicitly authorized as Option B." When Option B is invoked (gemini-only merge), an open item is filed for codex re-audit. The dual-gate rule is preserved over time even when one gate is temporarily unavailable.

## Consequences

### Accepted

- Every PR-promotion path enforces two-gate evidence. `closure_verifier.py` blocks closure if any of (request, result, normalized report) is missing for either gate.
- `contract_hash` is recorded on every result and verified at closure time. Empty or stale `contract_hash` blocks closure.
- Operator policy A1 / B1 / B2 / B3 in T0 CLAUDE.md govern temporary unavailability and finding-handling without weakening the dual-gate baseline.
- Anthropic Code Review MAY be added as a supplementary signal but does NOT replace either required gate. If adopted, it appears as a third optional surface, not a substitute.
- New review-gate categories (e.g., security_review, performance_gate) extend the rule rather than dilute it: each new category has its own request/result/report contract.
- The structural test in `scripts/review_gate_manager.py` and `tests/test_review_gates.py` enforces evidence-surface presence at CI time.

### Rejected

- **Single-LLM review.** Either gate alone (codex-only or gemini-only) is insufficient.
- **"Anthropic Code Review is enough now."** Single-vendor; same blind spots as the author. Does not satisfy moat M3.
- **Silent gate skipping.** F28 incident is the cited precedent. Skipping requires operator override and an open item per the codex-unavailable template.
- **Auto-resolution of gate disagreement.** Preferring one gate's verdict over another by configured priority discards the moat.
- **`queued` or `requested` as terminal evidence.** A pending request without a result is not closure evidence — T0 CLAUDE.md §"Headless Review Enforcement" rule 9.
- **Result without `contract_hash`.** Untraceable verdicts are blocking findings, not soft warnings.
- **Result without normalized report.** Structured JSON alone is operator-unreadable; the markdown report is required.

## Cross-references

- ADR-005 (NDJSON ledger — the audit substrate that records gate execution events)
- ADR-006 (human gate — adversarial review feeds into staging→promote, not around it)
- Operator memory `feedback_mandatory_triple_gate` (operational policy)
- Operator memory `feedback_gate_enforcement_failure_f28` (prior incident)
- Operator memory `feedback_codex_findings_must_be_parsed`, `feedback_codex_5_2_structured_findings` (codex_gate parsing details)
- Operator memory `feedback_review_gate_full_lifecycle` (request→execute→report→record→verify)
- `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4 moat M3, §5 (no replacement by Anthropic Code Review)
- T0 `.claude/terminals/T0/CLAUDE.md` §"Headless Review Enforcement", §"Operator Policies" A1/B1-B4
- `scripts/review_gate_manager.py`, `scripts/closure_verifier.py`, `scripts/lib/codex_parser.py`
- `.vnx-data/state/review_gates/{requests,results}/`, `$VNX_DATA_DIR/unified_reports/headless/`
