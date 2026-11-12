# ADR-008 — Dual-LLM Adversarial Review (`codex_gate` + `gemini_review`) with `contract_hash` Binding

**Status:** Accepted, amended 2026-09-26 (second reviewer: kimi_gate), amended 2026-11-11 (second reviewer: deepseek_gate)
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Mandatory triple-gate policy (codex + gemini + CI green); review-gate evidence model

## Context

VNX dispatches code through one or more worker terminals (T1/T2/T3) and accumulates each PR through a series of gates before merge. Anthropic shipped native Code Review on 2026-03-09, and the strategic replan (`claudedocs/_archive/2026-05-20-pre-centralisatie/2026-05-09-vnx-strategic-replan-proposal.md` §1) names the question explicitly: "do we still need a custom review pipeline now that the platform vendor ships one?"

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
- `claudedocs/_archive/2026-05-20-pre-centralisatie/2026-05-09-vnx-strategic-replan-proposal.md` §4 moat M3, §5 (no replacement by Anthropic Code Review)
- T0 `.claude/terminals/T0/CLAUDE.md` §"Headless Review Enforcement", §"Operator Policies" A1/B1-B4
- `scripts/review_gate_manager.py`, `scripts/closure_verifier.py`, `scripts/lib/codex_parser.py`
- `.vnx-data/state/review_gates/{requests,results}/`, `$VNX_DATA_DIR/unified_reports/headless/`

## Amendment (2026-09-26): Gemini is no longer a reviewer

**Decided by:** Operator (Vincent van Deth), "Hele Gemini als reviewer eruit."

The decision above stands as history and is superseded on one point: the second reviewer is no longer `gemini_review`. Measured on 2026-09-26 over the review-gate records of the vnx-dev store: `gemini_review` delivered no verdict. Its binary was never on PATH and it left one record ever.

- **Reviewers.** `codex_gate` and `kimi_gate` on subscription. `glm_gate` and `deepseek_gate` are takeover fallback in `VNX_REVIEW_GATE_TAKEOVER_CHAIN`, never a default seat.
- **Selection.** `gemini_review` is not a registered gate name. It lives in `dispatch_spec.RETIRED_GATE_NAMES` only, so it is handled two ways depending on where it is named: a `VNX_DEFAULT_REVIEW_STACK` that still carries it is logged with a warning and the name dropped from the stack (`review_gate_manager._build_default_review_stack`); a `VNX_REVIEW_GATE_TAKEOVER_CHAIN` or a dispatch spec's declared `gate` that names it is refused outright, with the retired-gate reason in the error (`dispatch_spec.retired_gate_hint`, `gate_request_handler._parse_review_gate_takeover_chain`, `dispatch_bridge.py`).
- **History.** The closure verifier still interprets `gemini_review` result records, so old PRs do not read as a configuration gap. A retired gate is never a peer signer.
- **Unchanged.** The `contract_hash` binding, the three evidence surfaces per gate and the rule that a single reviewer is insufficient evidence. Gemini as a provider lane for non-review work is outside this amendment.

## Amendment (2026-10-23): a second reviewer only at high and critical

**Decided by:** Operator (Vincent van Deth). Decision of 2026-10-17: codex is the only required seat per round, and the second seat runs on the last head before the merge, only at high risk. The amendment was approved on 2026-10-23, and the receipt conversion was classed high on 2026-10-23.

- **The rule.** Two independent reviewers at `high` and `critical`. One reviewer at `low` and `medium`: the first seat of the review stack, in stack order (`codex_gate` in the fabric's own stack). One function decides it: `review_seat_evaluation.seats_for_risk`. The merge door, `vnx-gate/review`, `vnx pr-ready` and track closure all read it.
- **The class.** It is derived from the PR diff against its merge base (R3a, `scripts/lib/review_risk_class.py`). Every path in a category of `smart_router._PLAN_SCOPE_PREFIXES` makes it `high`: schema, security, gate or merge logic, store resolution, the receipt conversion, a path outside the repository. A dispatch spec with `irreversible: true` makes it `high` too. Only the caller (`--risk-class`) or that spec flag can raise it, and nothing lowers it. A diff that cannot be fetched or read fails closed to `high`. The door fetches the PR head and the base branch before it reads the class (`review_risk_class.merge_head_risk_class`). A false positive is repaired by changing the path table in its own reviewed PR, never by a flag.
- **What it supersedes.** From decision 1, "Two reviewers, not one", and the sentence "Either gate alone is insufficient evidence": these now hold for `high` and `critical` only. From the 2026-09-26 amendment, "the rule that a single reviewer is insufficient evidence": this too holds for `high` and `critical` only. At `low` and `medium` the first seat's PASS on the merged head is sufficient evidence.
- **What stays.** The `contract_hash` binding. The three evidence surfaces per gate. One reviewer per seat, and the takeover chain that fills a seat. A reviewer never runs on the builder's vendor. At `high` and `critical`, the second seat's PASS must be on exactly the head being merged.
- **Older merges.** A merge record under the older rule (`every_seat_distinct_reviewer`) is judged by that rule at track closure. A merge since this amendment records `first_seat_or_every_seat_by_risk` with the class, its reason and source, the stack and the required seats.

## Amendment (2026-10-30): the second seat fires on a signal

**Decided by:** Operator (Vincent van Deth). Decision of 2026-10-30. Track `tweede-stoel-op-signaal`. Measurement: `claudedocs/2026-10-30-tweede-stoel-op-waarschuwing.md`.

This amendment is written before the rule is active. The key `VNX_REVIEW_SEAT_RULE` stays unset, so the rule of the 2026-10-23 amendment, `first_seat_or_every_seat_by_risk`, still decides every merge. One key switches between the two rules: unset keeps the old rule, and the signal rule goes on only when PR 5 of the track sets the key after the shadow test below. Unset is also the safe default across projects: no other project changes rule silently.

- **The measurement.** On 165 heads with a codex PASS and a second-seat verdict, 16 were real faults. With a real codex warning the second seat returned FAIL on 12 of 72 heads; without one on 4 of 93. Per seat: kimi via its own CLI 4 of 26 against 0 of 37, deepseek 8 of 45 against 4 of 54. Twelve of the sixteen came from the DeepSeek seat, which is no longer the default seat; on kimi the sample is 4 FAIL in 63.
- **The rule.** After a codex PASS on a head the door asks the second seat only when at least one signal holds:
  1. **Real warning.** The first seat reports at least one warning that is not a pre-scan notice.
  2. **Security path.** `smart_router.security_path_kind`, or the category `security`.
  3. **Schema.** The category `schema`.
  4. **Irreversible.** `irreversible` in the spec is true.
  5. **Sample.** `int(sha256(head_sha), 16) % 5 == 0`. Deterministic, so a dry run and the merge see the same head the same way; it keeps the miss ratio measurable.
- **A merge path is not a signal.** A head on a `gate-merge` path does not by itself require the second seat. That is the operator decision of 2026-10-30 and it deliberately costs risk: #2184 sat on such a path without a warning.
- **Fail-closed.** Every seat stays required when the first seat has no verdict or returned FAIL, when the number of warnings is unknown (an old ledger line, an unreadable report), when the first seat was filled by a takeover, when the risk class is unknown, or when the spec cannot be read. Every input field has the value `unknown`, and any `unknown` gives every seat. A FAIL of the second seat blocks as it does today. A caller cannot turn a signal off.
- **What it supersedes.** From the 2026-10-23 amendment, "Two independent reviewers at `high` and `critical`. One reviewer at `low` and `medium`": when the signal rule is active this seat selection is replaced by the first seat plus the second seat when a signal holds. The `contract_hash` binding, the three evidence surfaces per gate, the rule that a reviewer never runs on the builder's vendor, and the requirement that a second-seat PASS sits on exactly the merged head all stay.
- **The trail.** The dry run and the `pr_merged` receipt carry the rule name, the signals found and the `head_sha`. Receipts written before activation stay judged under their own rule, as `SEAT_RULE` and `RISK_SEAT_RULE` records already are.
- **Transition of obligations.** A new head asks for the second seat only after the first seat is booked on that head and the rule names a signal. An open second-seat obligation from before activation stays and is executed. A `pending` obligation without a signal is not erased: it gets the terminal status `retired` with reason `no_signal` and an audit line, and only the door or the runner may do that. A second-seat FAIL on the head always blocks, also when the signal is gone. A new head starts clean: no verdict and no signal is inherited.
- **Shadow test before activation.** The rule goes on only after at least 20 merges in shadow, with every receipt carrying the signals and the rule name, zero times fewer seats at an `unknown`, and the T0 having read every difference from the old rule.
- **Rollback criterion.** The key goes back to `first_seat_or_every_seat_by_risk` as soon as, on the sample of clean heads, 2 or more FAILs that the T0 judges real fall in the first 10, or more than 10 percent after at least 20. The sample runs about 1 in 5, so 20 heads asks about 100 clean heads. The repeat measurement after 50 merges also counts what codex-only merges received as fix-forward afterwards.
- **Accepted cost.** The rule misses 3 of the 16 faults on the measurement set, and #2184 sat on a `gate-merge` path without a warning. The sample and the repeat measurement keep the miss ratio visible; a miss ratio above 10 percent of the sample sends the key back.

### Activering 2026-11-09

**Status:** uitgevoerd (dispatch dlv-375fc3998a77, track tweede-stoel-op-signaal, PR 5). Bron: `claudedocs/2026-11-07-naar-1-9/metingen/schaduwtoets-tweede-stoel-2026-11-09.md`.

De schaduwtoets uit dit amendement is gehaald. De T0 zet vnx-dev daarna op de signaalregel met een `project_config`-rij (`VNX_REVIEW_SEAT_RULE` = `first_seat_or_every_seat_on_signal`) die hij met een approval-id schrijft; die rij is geen deel van PR 5. Elk ander project heeft geen rij en houdt `first_seat_or_every_seat_by_risk`.

**De vier eisen en hun meting.**

| Eis | Gemeten | Uitkomst |
|---|---|---|
| Minstens 20 merges in schaduw | 213 merges, van 03-11 12:22Z tot 09-11 15:24Z | gehaald |
| Elk receipt draagt regelnaam en signalen | 0 van 213 zonder `rule` of `signals` | gehaald |
| Nul keer minder stoelen bij `unknown` | 6 merges met een `unknown`-veld, alle 6 met de hele stapel | gehaald |
| Elk verschil nagelezen | 77 verschillen, hieronder per soort | gehaald, met één bevinding |

**De twee soorten verschil.** 136 merges kregen onder beide regels dezelfde stoelen. 40 keer vroeg de schaduw meer stoelen (stoel 2 bij een medium-PR), door de signalen `sample` en `real_warning`, soms samen. 37 keer vroeg de schaduw minder stoelen (alleen codex waar de risicoregel er twee eiste): 34 keer was de enige reden voor `high` een pad in de categorie `gate-merge`, 3 keer (#2433, #2497, #2554) hoogde de T0 zelf een afgeleide `medium` op met `--risk-class high`. Alle 37 hebben stoel-2-regels in `rounds.ndjson`, en geen enkele is een FAIL: op deze steekproef had de smallere regel geen blokkerende bevinding gemist.

**De bevinding (OI-2697).** `seats_for_signals` kent een vrijwillige ophoging door de aanroeper niet als signaal, dus een plan dat bewust twee stoelen vraagt zonder dat de diff of de spec dat afdwingt krijgt na activering één stoel. Tijdelijke regel tot OI-2697 dicht is: een vrijwillig verhoogde levering draagt `irreversible: true` in haar spec, want dat is wel een signaal.

**Verwachte belasting van stoel 2.** Onder de risicoregel vroegen 37 van de 213 merges stoel 2 (17 procent); onder de signaalregel zijn dat er 40 (19 procent), op andere PR's. De belasting blijft dus gelijk en verschuift van gate-merge-paden naar PR's met een echte waarschuwing of een steekproef.

**Een overname die stoel 1 volledig vult (OI-2701, operatorbesluit 09-11, optie a).** De sleutel `VNX_REVIEW_TAKEOVER_FULL_FILL` noemt per project de reviewers waarvan een overname van stoel 1 als stoel 1 telt, als komma-lijst van `gate:model`-paren (bijvoorbeeld `deepseek_gate:deepseek-v4-pro`). De sleutel is standaard leeg en keurt dan niemand goed; een item dat geen `gate:model` is wordt gelogd en keurt ook niemand goed. Een goedgekeurde overname met PASS, nul blokkerende bevindingen en beide tellingen bekend op exact de kop wordt beoordeeld als een directe PASS van stoel 1: haar eigen tellingen, uit haar eigen rapport, en elk ander signaal beslissen. Een overname buiten de lijst houdt elke stoel, zoals hierboven. Het besluit noemt het paar dat telde (`takeover_full_fill`).

**Terugdraaien en criterium.** Zet de rij terug op `first_seat_or_every_seat_by_risk` (of gebruik `VNX_OVERRIDE_REVIEW_SEAT_RULE` als de onmiddellijke noodrem) zodra het terugdraaicriterium van dit amendement geldt: een misratio boven 10 procent van de steekproef; de herhaalmeting na 50 merges telt ook mee wat codex-alleen-merges daarna als fix-forward kregen.

## Amendement 4 (08-11-2026): poortprofiel en veiligheidsondergrens

**Status:** Accepted (dispatch dlv-eec805d0fb3e, track gate-profiel-per-project, levering P0)
**Decided by:** T0, onder het mandaat van de operator voor golf 1-9. Het plan (`claudedocs/2026-11-07-naar-1-9/plannen/gate-profiel-per-project.md`, revisie 2 van 08-11) passeerde de plan-gate van 08-11 (codex-seat REVISE, tiebreaker deepseek-v4-pro START) op één bindende voorwaarde: een onbekende of malformed profielwaarde faalt dicht. Dit amendement is rij P0 van dat plan en legt het ondergrenscontract en de `gate-merge`-uitzondering vast vóór de eerste `light`-activatie in P5.
**Gemeten op:** `origin/main` `e747573ac` (2026-11-08); elk regelnummer hieronder is op die commit gemeten.
**Wijzigt:** niets aan de besluiten hierboven. Het legt een laag bovenop de zetelregels van de amendementen van 2026-09-26, 2026-10-23 en 2026-10-30, met één ondergrens die geen profiel mag zakken.

### Het poortprofiel: drie namen, vier waarden

Eén sleutel, `VNX_GOVERNANCE_PROFILE`, zet in één beweging de zwaarte van de reviewdeur en de plan-gate. De sleutel bestaat op deze commit nog niet: hij staat niet in `CONFIG_REGISTRY` (`scripts/lib/config_registry.py:83`). P1 van de track voegt hem toe als `enum` met default `standard` en categorie `gate`. Tot die rij bestaat, is elk profiel `standard` en verandert dit amendement geen gedrag. De leesfunctie wordt één: `gate_profile.resolve()` valideert de ruwe waarde en geeft `values = {review_stack, round_cap, takeover_chain, plan_gate_seats}` terug, met per waarde de herkomst; `None` betekent "geen bijdrage".

| Waarde (veld) | Registry-key / bron | `light` | `standard` (= vandaag) | `heavy` |
|---|---|---|---|---|
| `review_stack` (wat de deur vraagt) | `VNX_DEFAULT_REVIEW_STACK` (`scripts/lib/config_registry.py:116-117`) | `codex_gate` (+ `ci_gate` als `VNX_CI_GATE_REQUIRED`) | `codex_gate,kimi_gate` (+ `ci_gate`) | `codex_gate,kimi_gate,claude_gate` (+ `ci_gate`) |
| `round_cap` | `VNX_REVIEW_ROUND_CAP` (`scripts/lib/config_registry.py:190-191`) | `2` | `3` | `5` |
| `takeover_chain` | `VNX_REVIEW_GATE_TAKEOVER_CHAIN` (`scripts/lib/config_registry.py:141-143`) | `codex_gate,kimi_gate,claude_gate` | `codex_gate,kimi_gate,claude_gate,deepseek_gate` | `codex_gate,kimi_gate,claude_gate,deepseek_gate` |
| `plan_gate_seats` (vanaf de start) | `configs/plan_gate_panel.yaml:47-48` | `1` | `1` | `2` |
| `required_stack` (wat `vnx gate` verifieert) | enforcement-laag op `review_stack` | uit `review_stack` + enforcement | idem | idem |
| zetelregel (`VNX_REVIEW_SEAT_RULE`) | `scripts/lib/review_seat_evaluation.py:112` | niet gezet | `first_seat_or_every_seat_by_risk` voor elk project zonder rij; vnx-dev: `first_seat_or_every_seat_on_signal` sinds 2026-11-09, een `project_config`-rij met een approval-id | niet gezet |

`standard` draagt niets bij: elke lezer valt door naar de keten van vandaag. De gemeten "vandaag"-kolom is de registry-default — `codex_gate,kimi_gate` (`scripts/lib/config_registry.py:116-117`), cap `3` (`:190-191`), vier ketennamen (`:141-143`) — zodat "standaard is het gedrag van vandaag" een constructie-eigenschap is en geen belofte. `ci_gate` is orthogonaal: de bestaande keten voegt hem toe wanneer `VNX_CI_GATE_REQUIRED` waar is, nooit het profiel.

Eén functie is de bron, elke bestaande lezer houdt zijn eigen terugvalketen. De mapping van veld naar lezer, gemeten op deze commit:

| Veld | Lezer(s) |
|---|---|
| `review_stack` | `review_seats.resolve_review_seats` (`scripts/lib/review_seats.py:193`) |
| `round_cap` | `review_rounds.round_cap` (`scripts/lib/review_rounds.py:773`) |
| `takeover_chain` | `gate_request_handler._build_review_gate_takeover_chain` (`scripts/lib/gate_request_handler.py:141`), `providers_cli._review_chain_consequence` (`scripts/providers_cli.py:223`) |
| `plan_gate_seats` | `plan_gate_panel.compose_panel` (`scripts/lib/plan_gate_panel.py:632`), via `planning_cli._derive_panel_weight` (`scripts/planning_cli.py:4774`) |
| `required_stack` | `scripts/commands/gate.sh::_g_required_gates` (`:33`), via `gate_profile_cli.py` |

Alles aan dit profiel is deterministisch: de invoer is een gesloten enum van drie namen, de uitvoer een gesloten dict van vier waarden, en de regels zijn een tabel, een precedentieketen, een validatie en een weigering. Er is geen onderdeel waarvoor een model nodig is, en er komt er ook geen bij: de enige "model-achtige" beslissing is *welke* waarden `light` en `heavy` krijgen, en die staat als genummerd besluit in het plan, niet in code die een oordeel vraagt.

### De veiligheidsondergrens

De ondergrens staat onder elk profiel en is dezelfde in de reviewdeur en de plan-gate.

- **Reviews.** Op een kop van klasse `high` of `critical` is elke stoel van de aanvraagstapel vereist: `seats_for_risk` (`scripts/lib/review_seat_evaluation.py:343`) geeft alleen bij `low` en `medium` één stoel (`_FIRST_SEAT_CLASSES`, `scripts/lib/review_seat_evaluation.py:130-132`); elke andere, onbekende of ontbrekende klasse geeft elke stoel. Elke `plan_scope`-categorie is `high` (`scripts/lib/review_risk_class.py:15-22`). `light` heeft een basisstapel van één stoel en **weigert** daarom op zo'n kop, behalve wanneer de beslissende categorie **uitsluitend** `gate-merge` is. De weigering staat in `RequiredSeats.error` (`scripts/lib/review_seat_evaluation.py:322`) en noemt het profiel en het pad. Een expliciete stack met twee stoelen blijft werken: expliciet wint.
- **Plan-gate.** `second_seat_from_start` (`scripts/lib/plan_gate_panel.py:2360`) is een vloer onder elk profiel: schema, security, outside-repo en elke irreversible categorie geven de tweede zetel vanaf de start. `compose_panel` (`scripts/lib/plan_gate_panel.py:632`) gebruikt `max(profile_seats, vloer)`. `gate-merge` alleen geeft **geen** tweede zetel.
- **Fail-closed.** Kan de overnameketen een vereiste stoel niet vullen, dan blokkeert de deur, met de reden in `RequiredSeats.error` (`scripts/lib/review_seat_evaluation.py:322`). Een `unknown`-uitkomst is nooit een pass.
- **Eén signaalset.** De vloer en de plan-gate lezen dezelfde categorieën uit `scripts/lib/smart_router.py`: de constanten (`:965-969`, met `gate-merge` op `:967`), de prefixlijst (`_PLAN_SCOPE_PREFIXES`, `:995-1051`) en de onomkeerbare categorieën (`_IRREVERSIBLE_PREFIXES`, `:909-920`). Twee lezers, één tabel, dus de twee kunnen niet driften.

`light` wordt op een signaalpad geweigerd en niet stil opgetrokken. Optrekken zou het profiel laten liegen: het project koos `light` en kreeg `standard`, en niemand ziet dat de keuze niet werkte. Een weigering met profiel en pad in de fout is zichtbaar, en de ontsnapping blijft expliciet — een twee-stoels stack in de aanvraag, geen codewijziging.

### De `gate-merge`-uitzondering

De uitzondering is het operatorbesluit van 30-10, woordelijk uit het amendement van 2026-10-30 hierboven: **"A head on a `gate-merge` path does not by itself require the second seat."**

- **Wat het zegt.** Een pad dat alleen `gate-merge` is, is geen signaal en geen vloer: de reviewdeur vraagt bij `light` één stoel, de plan-gate houdt één zetel. Dat is een bewuste prijs: #2184 stond op zo'n pad zonder waarschuwing.
- **Wat het niet zegt.** Elke andere categorie of elk signaal blijft werken. Een echte waarschuwing, een security-pad, een schema-pad of `irreversible` heffen de stapel naar elke stoel en geven de plan-gate haar tweede zetel, ongeacht het profiel. Een pad dat `gate-merge` is én een signaal draagt, is niet "alleen `gate-merge`".
- **Wat het niet is.** Geen algemene ontsnapping aan de ondergrens: alleen een expliciete twee-stoels stack mag `light` op een signaalpad laten werken, en die keuze staat in de aanvraag, niet in het profiel.

### Fail-closed bij een onbekende of malformed profielwaarde

Dit is de bindende voorwaarde van de tiebreaker, en zij staat hier letterlijk als regel. `gate_profile.resolve()` leest de ruwe waarde en valideert **vóór** de tabelmapping:

- afwezig, `None`, leeg of alleen witruimte → "niet gezet" → `standard`, bron `default` of `unset`;
- na één `strip()` exact in `{light, standard, heavy}` → geldig;
- al het andere (`zwaar`, `Heavy`, `high`, `standard heavy`, een tikfout) → `GateProfileConfigError` met de **ruwe** waarde, de **bron** en de reden.

Geen enkele lezer vangt die fout weg: de deur weigert (blokkerend), `vnx gate` stopt met de fout, de plan-gate weigert, en `vnx gate-profile show` print de fout en exit 2. Het record draagt `profile_raw`, `profile_source` en `profile_error`; het schrijft **nooit** `profile: "standard"` op de fout. Een terugval op `standard` verzwakt de governance stil, en dat is precies wat deze regel uitsluit. De normalisatie is bewust minimaal — één `strip()`, case-sensitive: `" heavy "` is geldig, `"Heavy"` niet.

### Het profielwijzigingscontract

Een profielwijziging is een gewone configwijziging, maar de gevolgen liggen vast.

- **Opgeslagen stapels.** Elk aanvraagrecord en elk resultaat-receipt draagt `profile`, `profile_source`, `stack` en `stack_source` van het moment van de aanvraag. Een latere profielwijziging herschrijft geen bestaand record. `SeatEvaluation.to_record()` (`scripts/lib/review_seat_evaluation.py:1554`) draagt die sleutels vandaag niet; P6 voegt ze toe, additief en zonder versiebump.
- **Open verplichtingen.** Een open verplichting behoudt de eis waaronder hij is geboekt en wordt nooit stilzwijgend ingetrokken of vervuld. Een nog niet gestarte verplichting die een lichter profiel niet meer nodig heeft, krijgt de terminale status `retired` met een reden en een auditregel; alleen de deur of de runner mag dat doen — het spiegelbeeld van "Transition of obligations" in het amendement van 2026-10-30.
- **Herkomst.** `profile_source` ∈ {`unset`, `override`, `project_config`, `env`, `default`}; `stack_source` ∈ {`profile`, `enforcement`, `override`, `project_config`, `env`, `default`}.
- **Rollback.** De sleutel weghalen of op `standard` zetten herstelt de oude keten voor **toekomstige** aanvragen; open verplichtingen blijven staan. Geen migratie, geen dataverlies.
- **Midden in de vlucht.** De merge-deur toetst tegen het **huidige** effectieve profiel; is dat strenger, dan blokkeert hij. Hij merget nooit op de lichtere eis van een ouder profiel.

### Wat dit amendement niet doet

Het activeert `light` niet (dat is P5), het zet `VNX_REVIEW_SEAT_RULE` niet (de sleutel blijft onbezet; de regel van 2026-10-30 beslist elke merge tot de schaduwtest haar aanzet), en het verandert geen code. De precedentie blijft die van `_resolve` (`scripts/lib/config_registry.py:648`): override > project_config-rij > enforcement > profiel > env > default.

### Cross-references

- ADR-030, amendement 2026-10-25 (de tweede plan-gate-zetel vanaf de start bij schema, security, outside-repo en irreversible)
- ADR-041 (de tiebreaker bij de review-cap; de plan-gate van 08-11 en haar bindende voorwaarde)
- `claudedocs/2026-11-07-naar-1-9/plannen/gate-profiel-per-project.md` (revisie 2; de profieltabel, B5, B13 en B14)

## Amendment (2026-11-11): seat 2 is DeepSeek v4-pro, "subscription first" holds for seat 1 only

**Decided by:** Operator (Vincent van Deth). Decision of 2026-11-11, about 16:50Z: the Kimi subscription is cancelled and not renewed; it runs to the end of the invoice period. Measurement: `claudedocs/2026-11-10-kimi-vs-deepseek-oordeel.md`.

- **What changed.** The default review stack is `codex_gate,deepseek_gate` (`scripts/lib/config_registry.py:117-118`). The default takeover chain is `codex_gate,deepseek_gate,claude_gate` (`:141-143`, equal to `gate_request_handler._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN`). `deepseek_gate` runs `deepseek-v4-pro` by default. The `light`, `standard` and `heavy` profiles name no `kimi_gate` (`scripts/lib/gate_profile.py:66-84`). The project dial for seat 2 is `deepseek_gate_required` (`.vnx/governance_enforcement.yaml:20`).
- **Why.** One review costs $0.17 on Kimi and $0.16 on DeepSeek v4-pro, so the subscription bought no saving at seat 2. Kimi also stops at its weekly limit, and a stopped seat 2 stops the merge door. With the subscription gone, the reason for "subscription first" at seat 2 is gone too.
- **What it supersedes.** From the 2026-09-26 amendment: the bullet "`codex_gate` and `kimi_gate` on subscription" and the sentence that `deepseek_gate` is never a default seat. "Subscription first" now holds for seat 1 only: `tests/test_review_stack_subscription_first.py` fails when seat 1 of a default stack is not a subscription gate, and when a default stack, chain or profile names `kimi_gate`. Seat 2 and the chain may be metered.
- **What it gives up.** Seat 2 runs on API credit. A `deepseek-flash` build is reviewed by the same vendor on a different model (R7b, `deepseek-v4-pro`). A `deepseek-v4-pro` build passes `deepseek_gate` over, and seat 2 goes to `claude_gate`, which failed its calibration run and is therefore the last link of the chain.
- **What stays.** `kimi_gate` is a registered gate and the kimi lane stays: an explicit stack, chain or build choice can still name them. A `project_config` row beats the new default, so a project that pinned `kimi_gate` keeps it until an operator changes the row. The `contract_hash` binding, the three evidence surfaces per gate and the signal rule of 2026-10-30 are unchanged.
- **Reading older amendments.** The defaults in the 2026-10-23 and 2026-10-30 amendments and in the profile table of amendment 4 (2026-11-08) are the defaults of their date. Read them with this amendment.
