# Blocked-track triage — deel b

Datum: 2026-08-15. Basis: `origin/main` = `45ace207`. Deze meting toetst 40 geblokkeerde
tracks op hun *goal* (niet de titel), met bewijs uit de code en de commit-historie, niet uit
de trackadministratie.

Uitkomsten: **GESHIPT** = elke goal-eis aantoonbaar aanwezig (met sha/bestand:regel/PR).
**DEELS** = een deel geshipt, een deel niet. **OPEN** = niets gebouwd of bewijs ontbreekt.
**ONTOETSBAAR** = het goal is zo vaag dat er niets aan te toetsen valt.

---

## Tabel

| track-id | uitkomst | bewijs in één regel |
|---|---|---|
| oi-id-project-namespace | OPEN | `open_items_manager.py:175` genereert nog kaal `OI-{next_id:03d}`, geen project-prefix |
| operator-prompt-ledger | OPEN | bestaande UserPromptSubmit-hooks doen intelligence-inject, geen append-only prompt-ledger |
| panel-seat-response-clean-capture | DEELS | `_strip_echo` vervangen door head+tail distillate (OI-820), maar clean-capture op dispatcher-laag ontbreekt |
| panel-standalone-plugin | OPEN | geen plugin-manifest, geen standalone subprocess-dispatcher, geen gevendorde kern |
| per-provider-concurrency-cap | OPEN | `dispatch_serialization.py` kent alleen `claude-tmux`; geen per-provider semaphore |
| post10-kimi-door-model | GESHIPT | registry default `kimi-k3` + bare-alias-normalisatie vóór de model-in-registry gate |
| post10-packaging-polish | OPEN | geen MANIFEST.in, deps hebben alleen ondergrenzen, geen otlp |
| post10-skill-doc-reconcile | DEELS | `docs/_archive` bestaat + `@pm`→`@horizon`, maar `planner/SKILL.md` stuurt nog FEATURE_PLAN.md |
| pre10-assembly-step4 | OPEN | install-template heeft geen door-on/SHARED_PREPARE-defaults; `VNX_SHARED_PREPARE` default "0" |
| pre10-gemini-complete | OPEN | `gemini_review` nog in roadmap_manager review_stack en chain_state_projection REQUIRED_GATES |
| provider-agnostic-permissions | OPEN | geen ADR; `worker_permissions.py` is per-terminal profiel, niet folder-addresseerbare Laag 0/1 |
| receipt-mailbox-generalization | OPEN | `receipt_pull.py` bestaat niet, geen mailbox-generalizatie |
| refactor-skill-en-levering | OPEN | geen refactor-skill in `skills/` (26 skills, geen refactor); alleen adjacent tooling |
| sc-central-cutover-hook-rewire | DEELS | hooks resolven via centrale symlink, maar `vnx doctor` meldt nog `install:dual` FAIL |
| self-learning-loop-reactivate | DEELS | loop gescheduled + leest contract_invalid, maar build-log proposal-scan-pad niet aangetoond |
| structured-json-reports | OPEN | `report_body_contract.py` enforceert nog markdown-headings; geen JSON-report met decision-fields |
| vincent-precedent-ledger | OPEN | geen precedentenregister/classificatie/escalatie-haak |
| vnx-fleet-pin-audit | OPEN | doctor kent alleen hook dead-pin check, geen fleet .vnx-system pin-audit |
| vnx-start-single-default | GESHIPT | `start.sh` default "T0 only", interactieve full-model opt-in via menu/`--preset` |
| window-receipt-failure-counter | GESHIPT | windowed contract_invalid counters + panel-*.md exemption in converter |
| aef-style-enrichment-layer | OPEN | geen discovery/enrichment-loop; dep `gemma-4-12b-integration` ongebouwd |
| decision-judge-binding-phase4 | GESHIPT | `decision_binding.py` `VNX_DECISION_JUDGE_ENABLED` + operator_approval (#1100) |
| decision-judge-fastpath-phase3 | GESHIPT | `decision_fast_path.py` `VNX_DECISION_FAST_PATH` deterministische classifier (#1099) |
| decision-judge-shadow-phase2 | GESHIPT | `decision_shadow.py` `VNX_DECISION_JUDGE_SHADOW` + advisory/divergence ledgers (#1096/#1098) |
| doc-relevant-intelligence-source | OPEN | geen doc_relevant-source in intelligence-DB (post-1.0) |
| dreaming-methodology | OPEN | auto-dream baseline (ADR-019) bestaat, maar Anthropic-style extensie niet researched/scoped |
| extractable-plugins-catalog | OPEN | geen catalog/design-deliverable (panel/research-taak) |
| fleet-addressable-agents-phase5 | OPEN | geen `VNX_AGENT_REGISTRY` flag, geen `vnx agent register`, geen agent-registry |
| gate-state-yaml-precedence | OPEN | trigger (cockpit #1145) gehaald, maar `gate_state()` leest alleen env, geen YAML-laag |
| glm-5-2-fleet-default | DEELS | constraints+registry+litellm op glm-5.2 allowlist, maar routing_recommendations heeft nog `glm-5` |
| governance-effective-level-resolver | OPEN | trigger gehaald, maar geen `effective_level()` (alleen governance_enforcer.py) |
| haiku-report-summarizer | OPEN | geen summarizer (post-1.0) |
| light-audit-intelligence-layer | DEELS | task_class + target_id functioneel; target_type/channel_origin/intelligence_payload niet bevestigd |
| migration-consolidation-and-tenancy-cut | OPEN | trigger gehaald, maar 44 migrations niet geconsolideerd, composite keys niet verwijderd |
| oss-harness-orchestrator-interchange | OPEN | geen research-deliverable |
| review-floor-enforcer | DEELS | SCOPE skip (`VNX_PLAN_GATE_COMPLEX_ONLY`) geshipt, floor-enforcement niet |
| scout-effectiveness-economy | OPEN | geen impact-meting |
| tag-curation | OPEN | geen tag-curatie (post-1.0 follow-up) |
| test-lock-probe | ONTOETSBAAR | goal is letterlijk "probe" |
| pm-gate-agent-automation | OPEN | deps (gemma-4-12b, oi-lifecycle-closure, planning-future-state-layer) ongebouwd, geen PM-gate automation |

---

## GESHIPT — bewijs per goal-eis

### post10-kimi-door-model
Goal: "The single-entry door accepts the current kimi model without a model-not-in-registry reject."
- **Huidige kimi-model staat in de registry** — `scripts/lib/providers/wave7_models.yaml:271` `default_model: kimi-k3`, `:277` `cli_model_arg: "kimi-code/k3"` (het model dat de kimi-cli daadwerkelijk draait).
- **Bare alias normaliseert vóór de gate** — `scripts/lib/providers/constraint_enforcer.py:226` `_kimi_bare_alias_registry_default()` mapt een kale `--model kimi` op de registry-default; `:557-562` roept die normalisatie aan vóórdat de model-in-registry gate draait, zodat een kale kimi-referentie niet wordt afgewezen.
- **Commits** — `586cfc15` "fix(kimi-lane): resolve registry-drift LLMNotSet, make K3 the explicit default"; `79cb9652` "fix(kimi): route kimi_exec through the model resolver" (#1460).

### vnx-start-single-default
Goal: default single (alleen T0), full T0-T3 interactive als opt-in, twee presets.
- **Default single** — `scripts/commands/start.sh:364` "T0-only layout (single window, one pane)"; `:263` "Startup creates T0 only; workers populate runtime state on demand"; `:406` "T0-only startup no longer creates fixed worker worktrees"; `:595` "Layout: T0 only".
- **Interactive opt-in** — `:148-149` `_interactive_startup_menu` (full T0-T3 via het menu bij een terminal-stdin); `:83-113` `--preset`-mechanisme.
- Noot: de opt-in loopt via het interactieve menu + generieke `--preset`, niet via een letterlijke `--interactive` flag of voorgebakken "single"/"interactive" presetbestanden.

### window-receipt-failure-counter
Goal: windowed (niet cumulatieve) receipt-failure counters + panelist-output vrijgesteld van het worker-report-contract.
- **Windowed counters** — `scripts/learning_loop.py:429-433`: `contract_invalid`/`report_contract_invalid` records lopen exclusief door de staleness-helper (`is_stale_contract_invalid`), andere failure-statuses door een generiek window-filter. Commits: `c45609a4`, `e0fb7c73` "window contract_invalid counters on processor ingest-time", `c18707e0`, `2e97af71` "exempt non-report dispatch classes + window contract_invalid counters", `632869c6`.
- **Panelist-exemption** — `scripts/lib/report_to_receipt_converter.py:143-158` `_NON_DISPATCH_REPORT_PREFIXES` bevat `"panel-"`; `:1177` een `panel-*.md` report wordt permanent als `skipped_non_dispatch` geclassificeerd en nooit een dispatch-receipt.

### decision-judge-binding-phase4
Goal: judge bindend voor routine (`VNX_DECISION_JUDGE_ENABLED`), T0 reviewt uitzonderingen, expliciete `operator_approval` receipt voor gevoelige acties.
- **Flag** — `scripts/lib/decision_binding.py:31` `_FLAG = "VNX_DECISION_JUDGE_ENABLED"`, `:43` default OFF.
- **operator_approval** — `decision_binding.py` `binding_verdict(...)` + `record_operator_approval(...)`; docstring: merge/close-track/override vereisen altijd een expliciet `operator_approval` receipt, onvoorwaardelijk.
- **Commit** — `7b2fbccf` "feat(adr-028): Phase 4 judge-binding policy (default-off, human-on-the-last-set)" (#1100).

### decision-judge-fastpath-phase3
Goal: deterministische classifier beslist triviale receipts zonder judge-spawn; alleen niet-triviaal naar een judge; `VNX_DECISION_FAST_PATH` flag.
- **Flag** — `scripts/lib/decision_fast_path.py:28` `fast_path_enabled()` leest `VNX_DECISION_FAST_PATH` (default OFF).
- **Deterministische classifier** — `decision_fast_path.py` `_CLEAN_STATUSES` (ok/done/passed/…) short-circuit triviale beslissingen; alles met nuance valt door naar de backend.
- **Commit** — `f31286a0` "feat(adr-028): Phase 3 judge fast-path (default-off, conservative)" (#1099).

### decision-judge-shadow-phase2
Goal: ephemere judge schrijft decision_advisory, T0 beslist zelf, comparator logt divergentie, `VNX_DECISION_JUDGE_SHADOW` flag, omkeerbaar.
- **Advisory ledger** — `scripts/lib/decision_shadow.py` `ADVISORY_LEDGER = "decision_advisory.ndjson"`.
- **Divergentie-comparator** — `decision_shadow.py` `DIVERGENCE_LEDGER = "decision_divergence.ndjson"`; docstring: comparator logt alleen, verandert nooit een beslissing.
- **Flag + omkeerbaar** — `decision_shadow.py:39` `_FLAG = "VNX_DECISION_JUDGE_SHADOW"`, default OFF; unset = inert.
- **Commits** — `86594327` (#1096) + `fc4b0cb8` "activate Phase 2 shadow — wire DecisionRouter.decide to the judge" (#1098).
- Noot (afwijking t.o.v. het goal): het goal noemt "spawnt per niet-triviale receipt (opus-tmux-spawn/codex/kimi, nooit claude -p)". De geleverde default-judge is een zero-cost rule-based judge; de optionele LLM-backend is `VNX_DECISION_JUDGE_BACKEND=ollama|claude-cli` (`scripts/lib/llm_decision_router.py:7,436`) — de `claude-cli`-optie is wél `claude -p`, in tegenspraak met "nooit claude -p".

---

## DEELS — wat er nog ontbreekt

- **panel-seat-response-clean-capture** — het goal vraagt "leg de zetel-RESPONSE schoon vast op de dispatcher/generatie-laag zodat er niks te strippen is"; wat geshipt is (OI-820, #1334, `7a28d575`) is een head+tail per-seat distillate-budget dat de echo alsnog *post-hoc* wegsnijdt in plaats van schoon te vangen.
- **post10-skill-doc-reconcile** — `docs/_archive` en de `@pm`→`@horizon`-rename zijn gedaan, maar `skills/planner/SKILL.md:15,64` genereert nog steeds FEATURE_PLAN.md in plaats van `objective-add`/tracks-DB te sturen.
- **self-learning-loop-reactivate** — de loop draait via de nightly pipeline (`com.vnx.nightly-intelligence-pipeline.plist` → `nightly_intelligence_pipeline.sh:202`) en leest contract_invalid windowed (`learning_loop.py:429-433`), maar het pad "contract-violations als operator-gated proposal via de build-log proposal-scan" is niet aangetoond; `learning_loop.py:156` noteert dat de proposal-tier nooit een `pending_rules.json` produceerde.
- **glm-5-2-fleet-default** — provider_constraints (#1513), wave7-registry en `glm_harness_litellm_proxy.yaml` zijn op glm-5.2-allowlist; `scripts/lib/providers/routing_recommendations.yaml:67,189,288,386` bevat nog `model_id: glm-5` (base) die niet naar 5.2 is geremapped.
- **light-audit-intelligence-layer** — `task_class` (`smart_router.py:188-228` `classify_task`) en `target_id`→terminal-routing zijn functioneel; de kolommen `target_type`, `channel_origin`, `intelligence_payload` zijn niet als gevuld aangetoond (alleen task_class/target_id).
- **review-floor-enforcer** — SCOPE skip is geshipt (`VNX_PLAN_GATE_COMPLEX_ONLY` read-site, `plan_gate_enforcement.py:240`, #1412); het eerste deel ("plan_gate_evidence.py ENFORCE de per-PR review floors, advisory→blocking") is niet gedaan — `plan_gate_evidence.py` recordt nog alleen `plan_gate_pass`, enforcement blijft advisory.
- **sc-central-cutover-hook-rewire** — de embedded install is vervangen door een centrale symlink (`.claude/vnx-system` → `/Users/vincentvandeth/.vnx-system/current`, hooks resolven naar central v1.4.7), maar `vnx doctor` op de SC-projectmap meldt nog `[FAIL] install:dual` (de symlink op `.claude/vnx-system` telt als "embedded"), dus "dual-FAIL wegnemen + doctor clean" is niet gehaald.

---

## ONTOETSBAAR

- **test-lock-probe** — het goal is letterlijk "probe"; er is geen eindtoestand om aan te toetsen. Apart geteld, niet OPEN.

---

## Slottelling

- **GESHIPT: 6**
- **DEELS: 7**
- **OPEN: 26**
- **ONTOETSBAAR: 1**

Som: 6 + 7 + 26 + 1 = **40**. Klopt.
