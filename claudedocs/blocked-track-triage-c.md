# Blocked-track triage — deel c

Datum: 2026-10-27. Basis: `origin/main` = `164d48ad` (de kop waarop deze meting is gedaan; er is
niets aan tracks, deliverables of open items gewijzigd). Deze meting toetst de 50 geblokkeerde
tracks die nog géén rij in deel a of deel b hadden, op hun *goal* (niet de titel), met bewijs uit
de code en de commit-historie, niet uit de trackadministratie.

Uitkomsten: **GESHIPT** = elke goal-eis aantoonbaar aanwezig (met sha/bestand:regel/PR).
**DEELS** = een deel geshipt, een deel niet. **OPEN** = niets gebouwd of bewijs ontbreekt.
**ONTOETSBAAR** = het goal is zo vaag dat er niets aan te toetsen valt.

Herkomst van de set: `vnx objective list --count-blocked --json` op `164d48ad` geeft
**104 geblokkeerd (93 plan-gate, 11 dependency, 0 overig)**. Deel a triageert 40 tracks, deel b 40,
samen 80 zonder overlap; 54 daarvan zijn op deze kop nog geblokkeerd, 26 niet meer. De resterende
50 zijn **exact dezelfde 50 ids** die de nameting (`unified_reports/dlv-27c55cde9acc.md`, PR #2283)
in Open Items 1 achterliet — set-vergelijking, geen verschil. Wat wél verschoof is de basis:
`d80d748f` → `164d48ad` (#2279, #2275, #2273), en daarmee het aantal a/b-tracks dat nog geblokkeerd
is.

Noot over de plannen: `claudedocs/` is gitignored (`.gitignore:121`, 14 getrackte bestanden), dus
`claudedocs/specs/` en `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/` bestaan alleen in de
primaire checkout en niet op main. "In uitvoering" is daarom getoetst aan wat op main gemerged is;
een plan op schijf is geen bewijs.

---

## Tabel

| track-id | uitkomst | bewijs in één regel | blokkade |
|---|---|---|---|
| agents-als-map-fase-1 | OPEN | geen `VNX_AGENT_LAYOUT` in de boom (grep = 0), geen `context_manifest`; skills worden niet als kopie gematerialiseerd | plan-gate OI-PLAN-agents-als-map-fase-1; dep refactor-skill-en-levering |
| agents-als-map-fase-2 | OPEN | geen `VNX_AGENT_LAYOUT`, geen permit-digest-veld voor rol/binding/account; deur kent geen L3-weigering | plan-gate OI-PLAN-agents-als-map-fase-2; dep agents-als-map-fase-1 |
| agents-als-map-fase-3 | OPEN | geen subset-validator op permissies, geen hook-manifest, geen canary per harness | plan-gate OI-PLAN-agents-als-map-fase-3; dep agents-als-map-fase-2 |
| agents-als-map-fase-4 | OPEN | geen L1-winkel per account, geen promotiecommando met herkomst en vervaldatum | plan-gate OI-PLAN-agents-als-map-fase-4; dep agents-als-map-fase-3 |
| archify-verified-architecture-maps | OPEN | geen archify-run of bron-controle in de repo (grep -ri archify = 0) | plan-gate OI-PLAN-archify-verified-architecture-maps |
| audit-spoor-kern | DEELS | in uitvoering: register schrijft `dispatch_started` en `dispatch_completed` (`register_emit.py:71,75`), maar geen `dispatch_finished` met `end_state`, geen abandoned-sweep, ADR-045 staat niet op main | dep golf-1-opruimen |
| audit-spoor-volledig | DEELS | in uitvoering: eventstroom per dispatch (#2086), één poortschrijver (#2232/#2262) en `commit_sha` op elk `review_gate_result` (#2196); de gesloten eindlijst, het identiteitsblok en de hash-keten ontbreken | dep audit-spoor-kern; dep golf-1-opruimen |
| bounded-acceptance-and-lane-state | DEELS | eindige lock-timeout (`dispatch_serialization.py:28,107`) en een dode lane blijft dicht (`dispatch_cli.py:389-440`, `provider_lane_exhausted`); het vereiste open item bij uitdroging wordt alleen een registerregel | plan-gate OI-PLAN-bounded-acceptance-and-lane-state |
| closed-evidence-chain | DEELS | `## Open Items` stroomt naar de tracker (`open_items_from_report.py:59`) en de receipt-processor is per project (#1830), maar een script-runner-poort wordt nog geweigerd (`gate_runner.py:738`) | plan-gate OI-PLAN-closed-evidence-chain |
| decision-note-per-change | OPEN | 47 ADRs in `docs/governance/decisions/`, geen beslissingsnotitie per wijziging, geen controle in `scripts/ci/` | plan-gate OI-PLAN-decision-note-per-change |
| deepseek-modellen-en-routes | OPEN | registry kent `deepseek-v4-pro`, `deepseek-v4-flash` en `deepseek-flash` (`wave7_models.yaml:341,361,457`), maar er is geen vijfarmige meting en geen deepseek-route via OpenRouter | plan-gate OI-PLAN-deepseek-modellen-en-routes |
| door-owns-dispatch-state | GESHIPT | hervuur-wachter (`dispatch_cli.py:3308`, gewired in de vuurweg), gesloten wachtrij op `runtime_coordination.db` die `dispatches/pending/` nooit scant (`:3201` + toelichting op `:3184-3199`), en de bestaande state machine bedraad (`:1440` `register_dispatch`, `:1457` claim, `:3351` `_owner_transition`) | plan-gate OI-PLAN-door-owns-dispatch-state |
| eval-suite-on-config-change | OPEN | nul workflows onder `.github/workflows/` noemen field-tests of benchmark; het harnas hangt nog alleen aan `monthly_runner.sh` | plan-gate OI-PLAN-eval-suite-on-config-change |
| expired-routing-state | DEELS | modelprijs draagt `price_checked_at` met fail-loud validatie (`provider_registry.py:159`) en de lane-cooldown is geclassificeerd (`availability.py:1-40`), maar geen enkele routeringstoestand verloopt luid | plan-gate OI-PLAN-expired-routing-state |
| fleet-release-and-rollout | DEELS | in uitvoering: `fleet_pin_audit.py:1` + `pin_drift.py:2` + lag-drempel uit config (`config_registry.py:359`, #2247); de uitrol van alle vier projecten is niet gemeten | dep vnx-init-store-path-parity |
| forge-poort-vloot | OPEN | de fabric-repo zelf is beschermd (`FORGE_GATE.md:14`, `scripts/forge/branch_protection.yaml`), maar van de drie consumer-repo's is geen check-run of `pr_merged`-receipt gemeten | plan-gate OI-PLAN-forge-poort-vloot |
| gate-receipts-volledig-en-sha-gebonden | DEELS | `commit_sha` staat op elk `review_gate_result` (`gate_recorder.py:576,623,781`) via één schrijver (#2232), maar de telling runs==receipts over een testdag is niet gedaan | plan-gate OI-PLAN-gate-receipts-volledig-en-sha-gebonden; dep audit-spoor-volledig |
| glm-53-evaluation | OPEN | 5.3 is naast 5.2 toegelaten om te kunnen meten (`provider_constraints.yaml:139-152`), maar er zijn geen 5 runs per versie en 5.2 blijft de default | plan-gate OI-PLAN-glm-53-evaluation |
| golf-1-opruimen | DEELS | in uitvoering: clusters c1-c5 gemerged (#2171, #2173, #2174, #2175, #2176, #2186, #2187, #2189), maar klaar-conditie (2) is niet gehaald: de retire-tracks staan nog op queued | dep deliverable-retarget-withdraw; dep retire-feature-plan-second-planning-layer; dep retire-redundant-architecture |
| golf-2-hardenen | OPEN | niet gestart als golf: geen plan in `claudedocs/specs/`, klaar-conditie (1) leunt op gate-engine-consolidation dat zelf nog loopt (GE-4a t/m GE-5b gemerged, GE-6 niet) | plan-gate OI-PLAN-golf-2-hardenen; 13 deps waaronder golf-1-opruimen |
| golf-3-harness-agnostisch | OPEN | geen aangenomen harness-contract-ADR en geen adapter per harness; hangt hard aan golf-2 | plan-gate OI-PLAN-golf-3-harness-agnostisch; 8 deps waaronder golf-2-hardenen |
| intelligence-corpus-quarantine | OPEN | gemeten op de store: 192.495 `code_snippets`-rijen over 875 paden, alle naar de opgeruimde worktree `vnx-roadmap-autopilot-wt`; geen quarantaine-pad, geen back-up | plan-gate OI-PLAN-intelligence-corpus-quarantine |
| intelligence-effect-measurable | DEELS | `pattern_injection_outcome` bestaat met `ab_arm` (`gather_intelligence.py:814`) en 441 rijen gemeten, maar `dispatcher_minimal.sh:487` roept gather nog met vier argumenten aan, dus zonder `dispatch_id` | plan-gate OI-PLAN-intelligence-effect-measurable |
| ledger-is-falsifiable | DEELS | `audit_chain.py` geeft exit 2 op `unchained` (`:26,44`), de converter-stderr wordt gevangen (`receipt_processor.sh:265,338`) en pytest-ruis wordt gefilterd (`receipt_outcome.py:133`); processor-liveness staat niet in t0_state | plan-gate OI-PLAN-ledger-is-falsifiable |
| ledger-schema-version | DEELS | (a) en (b) staan: `schema_version` op elk nieuw receipt (`payload.py:528`) en de drie leesrichtingen vastgelegd (`ledger_schema_version.py:47`, `tests/test_ledger_schema_version.py`); (c) is niet aangetoond — er is geen census die de 2217 receipts zonder status en de 19 statusvarianten als vorm-drift verklaart of repareert | plan-gate OI-PLAN-ledger-schema-version; dep audit-spoor-volledig |
| ledger-vocab-generated | DEELS | statusvocabulaire heeft één bron met CI-driftcontrole (`.github/workflows/vnx-ci.yml:239`, `event_outcome_semantics.py:212`); de event-type-lijst en de negeerbaar-markering ontbreken | plan-gate OI-PLAN-ledger-vocab-generated |
| one-worker-daemon | DEELS | per project draait een KeepAlive-launchd-job (`scripts/launchd/com.vnx.headless-trigger.plist`) en `daemon_liveness` zit in t0_state; er is geen daemon die met concurrency 1 uit de wachtrij claimt | plan-gate OI-PLAN-one-worker-daemon |
| opus-5-vs-5-5-vergelijking | OPEN | beide modellen staan in de registry (`wave7_models.yaml:170,202`) en er ligt een nulmeting, maar er is geen run en geen vooraf vastgelegde beslisregel | plan-gate OI-PLAN-opus-5-vs-5-5-vergelijking; dep golf-2-hardenen |
| orchestrator-skills-port-codex-kimi | OPEN | geen audit- of port-deliverable: de T0-rol noemt Codex/Kimi als harness, maar er is geen skill-inventaris of port | dep orchestrator-provider-agnostic-role; dep refactor-skill-en-levering |
| per-folder-agents | OPEN | een rol is `agents/<rol>/CLAUDE.md`, er is geen `.claude/agents/*.md` en geen map-adresseerbare rol die skill, profiel en model bindt | dep agents-als-map-fase-2 |
| plan-gate-seat-role-register | GESHIPT | `agents/plan-reviewer/CLAUDE.md:3` staat in het rolregister (b1eeeb4ef, #1571); gemeten op deze kop: `resolve_worker_profile('plan-reviewer')` resolvet zonder fallback met schrijfscope `['.vnx-data/unified_reports/**']` | plan-gate OI-PLAN-plan-gate-seat-role-register |
| planlaag-zegt-weer-iets | OPEN | 92 van de 103 geblokkeerde tracks hangen aan een plan-gate-open-item, er is geen phase-besluit en geen verklaring voor het gat tussen de tracks-DB en de standaardlijst | plan-gate OI-PLAN-planlaag-zegt-weer-iets |
| provider-agnostic-review-lane | DEELS | glm_gate en kimi_gate lopen nu door de governed lane (#2273), maar een script-runner-poort wordt nog geweigerd met `gate_not_subprocess_routable` (`gate_runner.py:738`) | dep gate-engine-consolidation; dep kosten-per-model-in-het-grootboek |
| receipt-uitkomst-attributie | DEELS | `receipt_outcome.py` vouwt de regels tot één uitkomst per dispatch (D3), maar de driestore-telling voor/na naar nul is niet gedaan | plan-gate OI-PLAN-receipt-uitkomst-attributie; dep audit-spoor-volledig |
| review-rondegrens-met-tiebreaker | DEELS | rondgrootboek, cap, tiebreaker en weigering aan de deur staan (`review_rounds.py:98`, `review_seat_evaluation.py:916,972`); de cap telt B3-blokkers, geen `review_round_cap` van 3 FAIL-rondes | plan-gate OI-PLAN-review-rondegrens-met-tiebreaker |
| review-router-escalatie-tiebreaker | DEELS | zetels komen uit config (`review_seats.py:1`, #2073) en de risicoklasse uit de diff (`review_risk_class.py:1`, #2191/#2193); escalatieorde per risicoklasse en de GitHub-check op die uitkomst zijn niet aangetoond | plan-gate OI-PLAN-review-router-escalatie-tiebreaker |
| role-register-single-resolution | OPEN | de deur scant nog één hardgecodeerde map: `dispatch_cli.py:2746` `_discover_valid_roles(_resolve_repo_root() / "agents")`, terwijl de docstring op `:1770` gelijkheid met het workerprofiel claimt | plan-gate OI-PLAN-role-register-single-resolution |
| scheduled-task-inventory | OPEN | geen inventarisatiemodule en geen `systemctl`-adapter (grep = 0); alleen losse launchd-scripts | plan-gate OI-PLAN-scheduled-task-inventory |
| sessie-wachtstatus | OPEN | `packages/claude-code-toolkit` bevat alleen context-meter en context-rotation; geen hook die de tmux-pane-status zet | plan-gate OI-PLAN-sessie-wachtstatus |
| spc-band-action-ladder | OPEN | lezers van `spc_control_limits` zijn de aggregator (`governance_aggregator.py:376`), `quality_db_init.py` en `intelligence_export.py`; geen enkele consument handelt | plan-gate OI-PLAN-spc-band-action-ladder |
| stop-conditions-are-code | DEELS | vier van de zes checks plus `halt.json` en een fail-closed deur (`stop_conditions.py:13`, `dispatch_cli.py:2338`), en een dagcap voor betaalde lanes (`paid_lane_budget.py:90`); geen daemon leest het halt en E2/E3 blijven proza | plan-gate OI-PLAN-stop-conditions-are-code |
| t0-acties-in-het-grootboek | OPEN | geen `t0_action`-receipttype: de enige treffers zijn disptach-footer-templates (`dispatch_footer.py:31-33`) | plan-gate OI-PLAN-t0-acties-in-het-grootboek; dep audit-spoor-volledig |
| task-complete-ontdubbelen | GESHIPT | de lane schrijft `subprocess_completion` (`subprocess_dispatch_internals/receipt_writer.py:316`, via `dispatch_govern.ensure_receipt`) en de rapport-converter `task_complete` (`report_to_receipt_converter.py:1294`); de lezer vouwt dat tot één uitkomst per dispatch (`event_outcome_semantics.py:107`) met de rode run in `tests/test_receipt_outcome_digest.py:1-8` | plan-gate OI-PLAN-task-complete-ontdubbelen; dep audit-spoor-volledig |
| terminal-binding-afbouwen | OPEN | `VNX_ADAPTER_T{n}` wordt nog gelezen (`dispatch_deliver.sh:577`, `append_receipt_internals/common.py:109`) en de pool start via `subprocess_dispatch`; plan ligt er, niets van gemerged | dep golf-1-opruimen |
| test-shared-datadir-isolation | GESHIPT | gemeten op deze kop: los 67 + 27 = 94, samen in één sessie 94 — de telling is niet meer volgorde-afhankelijk; track staat op `done` (PR #1077) | plan-gate OI-PLAN-test-shared-datadir-isolation |
| tier-ladder-evaluation-moment | DEELS | `escalate_tier` neemt een `failure_class` mee met een tabel per klasse en een herkansingsteller (`tier_routing.py:130,198`), met een productie-aanroeper in de deur (`dispatch_cli.py:3053`); de operator-melding met het kostenverschil en de instaptrede per task_class ontbreken | plan-gate OI-PLAN-tier-ladder-evaluation-moment |
| tmux-laag-opruimen | DEELS | P09 en P16 zijn weg (#2176, #2173) en `vnx permission` plus de escalatiewinkel zijn geretired (#2175, #2186, #2187, #2189); P01, P02, P13, P17 en P19 staan er nog | plan-gate OI-PLAN-tmux-laag-opruimen |
| vnx-2-0-verkenning | OPEN | de studie en de vier panelstukken staan alleen in de gitignorde `claudedocs/`; er is geen gecommit deliverable en geen richtingkeuze | plan-gate OI-PLAN-vnx-2-0-verkenning |
| worker-delivery-observability | DEELS | vier leveringstoestanden plus `delivery_state` op het receipt (`provider_dispatch.py:51-65,1279,2078`) met hergebruik van `classify_failure_safe` en een eigen test; de planpunten 4 en 5 (risico's, volgorde) hebben geen geaccepteerd plan | plan-gate OI-PLAN-worker-delivery-observability; dep audit-spoor-volledig |
| zelfverbeterende-lus | DEELS | voorstelgenerator en applier bestaan (`generate_suggested_edits.py`, `apply_suggested_edits.py`), maar er is geen lus-orchestrator, geen eval-voor-en-na, geen terugdraai en geen `vnx learning status` | plan-gate OI-PLAN-zelfverbeterende-lus |

## Join — elk geblokkeerd track heeft een triage-rij

```bash
python3 - <<'EOF'
import json, re, subprocess

blocked = {t["track_id"] for t in json.loads(subprocess.run(
    ["vnx", "objective", "list", "--count-blocked", "--json"],
    capture_output=True, text=True, check=True).stdout)["tracks"]}
triaged = set()
for f in ("claudedocs/blocked-track-triage-a.md",
          "claudedocs/blocked-track-triage-b.md",
          "claudedocs/blocked-track-triage-c.md"):
    for line in open(f):
        m = re.match(r"\| ([a-z0-9][a-z0-9-]+) \| (GESHIPT|DEELS|OPEN|ONTOETSBAAR) \|", line)
        if m:
            triaged.add(m.group(1))
missing = sorted(blocked - triaged)
print(f"geblokkeerd: {len(blocked)}; getriageerd: {len(blocked & triaged)}; "
      f"zonder triage: {len(missing)}")
print(missing or "geen")
EOF
```

```
geblokkeerd: 103; getriageerd: 103; zonder triage: 0
geen
```

Uitkomst: **0 geblokkeerde tracks zonder triage-rij.**

Noot bij de drift: tussen de eerste telling (104 = 93 plan-gate + 11 dependency) en deze
join-run zakte het totaal naar 103 (92 + 11). Precies één track raakte tijdens deze sessie
ontblokkeerd — `oss-harness-orchestrator-interchange`, een rij uit deel b, niet uit deze 50.
Alle 50 rijen in deze tabel zijn op het moment van de join nog steeds geblokkeerd. Het
commando hierboven is de herhaalbare meting; het getal beweegt alleen wanneer een blokkade
echt verandert, en dat is hier één keer gebeurd en benoemd.

---

## GESHIPT — bewijs per goal-eis

- **door-owns-dispatch-state** — goal: hervuur-wachter, gesloten cutover-wachtrij, de bestaande state
  machine bedraden, `dispatch_attempts` vullen; klaar wanneer een al gedraaide dispatch niet opnieuw
  kan vuren zonder reden en de rij zijn levensloop draagt.
  - Eis 1 (hervuur): `_refire_guard_verdict` (`scripts/lib/dispatch_cli.py:3308`) weigert op een
    terminaal receipt (`_find_terminal_receipt_status`), een bestaand routebesluit
    (`_route_decision_exists`) of een runtime-eindtoestand (`_REFIRE_RUNTIME_END_STATES`, `:3180`),
    behalve met `--refire` plus reden. Vastgelegd in `tests/test_dispatch_refire_guard.py:1-10`.
  - Eis 2 (gesloten wachtrij): de guard leest uitsluitend `runtime_coordination.db` op `dispatch_id`;
    `dispatch_cli.py:3184-3199` legt vast dat `dispatches/pending/` (2100+ mappen) nooit gescand
    wordt en dat een pre-cutover id als "geen eindtoestand" leest. Cutover: `2026-09-04`
    (`:3201`).
  - Eis 3 (state machine, geen tweede model): `_persist_dispatch_row` roept `register_dispatch`
    (`:1440`) en claimt direct (`:1457`), `_owner_transition` (`:3351`) drijft de rij via
    `transition_dispatch_idempotent` naar delivering/accepted/running/completed, `_owner_update_attempt`
    sluit de `dispatch_attempts`-rij.
  - Gemeten: `tests/test_dispatch_refire_guard.py` groen op deze kop (deel van 31 passed voor de
    drie GESHIPT-bewijstests).
- **plan-gate-seat-role-register** — goal: de rol `plan-reviewer` staat in een rolregister en de
  lane weigert hem niet meer fail-closed; klaar bij (1) lane-probe rc=0, (2) een plan-gate-run met
  twee leesbare verdicts, (3) `resolve_worker_profile('plan-reviewer')` zonder `UnknownRoleError`,
  schrijfscope beperkt tot `unified_reports`, zonder commit/push.
  - Register: `agents/plan-reviewer/CLAUDE.md:3` (b1eeeb4ef, #1571).
  - Eis 3 gemeten op deze kop: `resolve_worker_profile('plan-reviewer')` resolvet (geen fallback)
    met schrijfscope `['.vnx-data/unified_reports/**']`.
  - Eis 1 en 2 zijn op codeniveau vastgelegd: `tests/test_plan_reviewer_role.py:9-14` pint de
    resolutie, de schrijfscope en de #1563-fail-closed-garantie; `tests/test_plan_gate_panel.py:1407`
    pint dat de zetel werkelijk als `plan-reviewer` routeert. Een live lane-probe of plan-gate-run is
    in deze leesmeting niet gedaan (geen dispatch uitgevoerd) — dat is de enige eis die op
    observable-niveau open blijft.
- **task-complete-ontdubbelen** — goal: één `task_complete` per dispatch-afronding, óf een expliciet
  ander `event_type`, met tellingen die niet dubbel tellen en een test die op de oude code rood was.
  - De lane schrijft `subprocess_completion` (`scripts/lib/subprocess_dispatch_internals/receipt_writer.py:316`,
    via `dispatch_govern.ensure_receipt`), de rapport-converter `task_complete`
    (`scripts/lib/report_to_receipt_converter.py:1294`) — het tweede been van de klaar-conditie.
  - De lezer vouwt dat tot één uitkomst per dispatch: `COMPLETION_EVENT_TYPES`
    (`scripts/lib/event_outcome_semantics.py:107`) en `receipt_outcome.py`; de rode run staat in
    `tests/test_receipt_outcome_digest.py:1-8` ("on the pre-d3b digest and fail there on the counts").
  - Gemeten: `tests/test_receipt_outcome_digest.py` groen op deze kop.
- **test-shared-datadir-isolation** — goal: `test_objective_reconcile.py` verliest 36 tests naast
  `test_w1b_0031_reconcile.py` door een gedeelde `VNX_DATA_DIR`; de fixtures moeten de telling
  volgorde-onafhankelijk maken. Track staat op `done` (PR #1077).
  - Gemeten op deze kop: los 67 passed + 27 passed = 94, en samen in één sessie 94 passed. Geen
    verlies meer, dus de klaar-conditie is gehaald.

---

## DEELS — wat er nog ontbreekt

- **audit-spoor-kern** — register schrijft `dispatch_started` en `dispatch_completed`
  (`append_receipt_internals/register_emit.py:71,75`), maar er is geen `dispatch_finished` met
  `end_state`, geen abandoned-sweep en ADR-045 staat niet op main.
- **audit-spoor-volledig** — per-dispatch eventstroom (#2086), één poortschrijver (#2232/#2262) en
  `commit_sha` op elk `review_gate_result` (#2196) zijn er; de gesloten eindlijst, het
  identiteitsblok (rol/writer_sha) en de hash-keten ontbreken.
- **bounded-acceptance-and-lane-state** — eindige lock-timeout (`dispatch_serialization.py:28,107`) en
  een uitgedroogde lane blijft dicht (`dispatch_cli.py:389-440`); bij uitdroging wordt alleen een
  registerregel geschreven, geen operatorstop en geen open item.
- **closed-evidence-chain** — open items stromen uit het rapport (`open_items_from_report.py:59`) en
  de receipt-processor is per project (#1830), maar een script-runner-poort wordt nog geweigerd
  (`gate_runner.py:738`), dus die keten is niet gesloten.
- **expired-routing-state** — modelprijs draagt `price_checked_at` met fail-loud validatie
  (`provider_registry.py:159`) en de lane-cooldown is geclassificeerd (`availability.py:1-40`), maar
  geen enkele routeringstoestand verloopt daadwerkelijk luid (geen TTL-sweep, geen melding).
- **fleet-release-and-rollout** — pin-audit en driftdrempel staan (`fleet_pin_audit.py:1`,
  `pin_drift.py:2`, `config_registry.py:359`, #2247); de uitrol over alle vier projecten is niet
  gemeten (geen per-project pin-verificatie).
- **gate-receipts-volledig-en-sha-gebonden** — `commit_sha` staat op elk `review_gate_result`
  (`gate_recorder.py:576,623,781`) via één schrijver (#2232), maar de telling runs==receipts over een
  testdag is niet gedaan.
- **golf-1-opruimen** — in uitvoering: clusters c1-c5 zijn gemerged (#2171, #2173, #2174, #2175,
  #2176, #2186, #2187, #2189), maar klaar-conditie (2) is niet gehaald: de retire-tracks staan nog op
  `queued`.
- **intelligence-effect-measurable** — `pattern_injection_outcome` bestaat met `ab_arm`
  (`gather_intelligence.py:814`, 441 rijen in de store), maar `dispatcher_minimal.sh:487` roept
  gather nog met vier argumenten aan, dus zonder `dispatch_id`; het effect is niet per dispatch toe te
  schrijven.
- **ledger-is-falsifiable** — `audit_chain.py:26,44` exit 2 op `unchained`, de converter-stderr wordt
  gevangen (`receipt_processor.sh:265,338`) en pytest-ruis wordt gefilterd (`receipt_outcome.py:133`);
  processor-liveness staat niet in `t0_state`, dus "de ledger liegt niet" is niet continu falsifieerbaar.
- **ledger-schema-version** — (a) en (b) staan: `schema_version` op elk nieuw receipt
  (`payload.py:528`) en de drie leesrichtingen vastgelegd (`ledger_schema_version.py:47`,
  `tests/test_ledger_schema_version.py`); (c) is niet aangetoond — er is geen census die de 2217
  receipts zonder status en de 19 statusvarianten als vorm-drift verklaart of repareert.
- **ledger-vocab-generated** — het statusvocabulaire heeft één bron met CI-driftcontrole
  (`check_status_vocab_drift.py`, `.github/workflows/vnx-ci.yml:239`), maar de event-type-lijst en de
  negeerbaar-markering worden niet gegenereerd.
- **one-worker-daemon** — per project draait een KeepAlive-launchd-job en `daemon_liveness` zit in
  `t0_state`; er is geen daemon die met concurrency 1 uit de wachtrij claimt.
- **provider-agnostic-review-lane** — `glm_gate` en `kimi_gate` lopen nu door de governed lane
  (#2273), maar een script-runner-poort wordt nog geweigerd met `gate_not_subprocess_routable`
  (`gate_runner.py:738`).
- **receipt-uitkomst-attributie** — `receipt_outcome.py` vouwt de regels tot één uitkomst per dispatch
  (D3); de driestore-telling voor/na naar nul is niet gedaan.
- **review-rondegrens-met-tiebreaker** — rondgrootboek, cap, tiebreaker en weigering aan de deur staan
  (`review_rounds.py:98`, `review_seat_evaluation.py:916,972`); de cap telt B3-blokkers en niet de
  gevraagde 3 FAIL-rondes.
- **review-router-escalatie-tiebreaker** — zetels komen uit config (`review_seats.py:1`, #2073) en de
  risicoklasse uit de diff (`review_risk_class.py:1`, #2191/#2193); de escalatieorde per risicoklasse
  en de GitHub-check op die uitkomst zijn niet aangetoond.
- **stop-conditions-are-code** — vier van de zes checks plus `halt.json` en een fail-closed deur
  (`stop_conditions.py:13`, `dispatch_cli.py:2338`), en een dagcap voor betaalde lanes
  (`paid_lane_budget.py:90`); geen daemon leest het halt en E2/E3 blijven proza.
- **tier-ladder-evaluation-moment** — `escalate_tier` neemt een `failure_class` met tabel en
  herkansingsteller (`tier_routing.py:130,198`), met een productie-aanroeper in de deur
  (`dispatch_cli.py:3053`); de operator-melding met het kostenverschil en de instaptrede per
  `task_class` ontbreken.
- **tmux-laag-opruimen** — P09 en P16 zijn weg (#2176, #2173) en `vnx permission` plus de
  escalatiewinkel zijn geretired (#2175, #2186, #2187, #2189); P01, P02, P13, P17 en P19 staan er nog.
- **worker-delivery-observability** — vier leveringstoestanden plus `delivery_state` op het receipt
  (`provider_dispatch.py:51-65,1279,2078`) met eigen test; de planpunten 4 en 5 (risico's, volgorde)
  hebben geen geaccepteerd plan.
- **zelfverbeterende-lus** — voorstelgenerator en applier bestaan (`generate_suggested_edits.py`,
  `apply_suggested_edits.py`), maar er is geen lus-orchestrator, geen eval voor-en-na, geen
  terugdraai en geen `vnx learning status`.

---

## ONTOETSBAAR

Geen. Alle 50 goals hebben een toetsbare eindtoestand.

---

## Slottelling

- **GESHIPT: 4**
- **DEELS: 22**
- **OPEN: 24**
- **ONTOETSBAAR: 0**

Som: 4 + 22 + 24 + 0 = **50**. Klopt.
