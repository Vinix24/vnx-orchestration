# Deliverables afgeleid voor de geblokkeerde tracks (punt 3, OI-1560-p3)

Dispatch-ID: 20260816-p3-blocked-deliverables

Planningswerk, geen code: dit doc verantwoordt een reeks `vnx deliverable add`/`vnx deliverable set`-
aanroepen tegen de central-DB (project `vnx-dev`). Geen dispatches gevuurd, geen plan-gate-panel
gedraaid, geen deliverable gepromoveerd — alles landt als `proposed` (of blijft op zijn bestaande
status voor de twee tags-only tracks).

## Aanpak

Per track uit de lijst van twaalf is `vnx objective show <track> --json` gelezen (`goal_state` +
het laatste `decision_ref` van de 2026-08-15-plan-gate-ronde, opus+kimi). Een deliverable is alleen
toegevoegd wanneer de tekst van het `goal_state`-veld zelf een concreet, onafhankelijk shipbaar stuk
werk aanwijst. Waar het `decision_ref` liet zien dat de KERN van het plan feitelijk onjuist of al
achterhaald is — niet "te dun", maar gebouwd op een aanname die niet klopt — is er niets toegevoegd:
een deliverable bovenop een foutieve aanname haalt de gate niet eerlijk, en levert bij implementatie
niets op. Die tracks staan onder "Goal moet eerst herzien".

Voor de twee tracks die al deliverables hadden (`fabric-self-reporting-truthful`,
`gate-finding-disposition`) is uitsluitend getagd via `vnx deliverable set` — er is niets
toegevoegd, zoals de dispatch voorschrijft.

## Deliverables toegevoegd

### doc-relevant-intelligence-source
`dlv-4f8768059dcf` — *doc_relevant intelligence-source: claudedocs/ en READMEs toevoegen aan de
geindexeerde dirs (pointer-only, D-A4 refs-pattern)* — `01_code_generation` / `sonnet`.

Het `goal_state`-veld noemt expliciet drie doelcategorieën: "docs/\*\* beyond ADRs, claudedocs/,
READMEs". De `doc_relevant`-source zelf en de indexering van `docs/**` bestaan al op main (bevestigd
door beide panelisten); de resterende, letterlijk in het goal genoemde scope is de uitbreiding naar
`claudedocs/` en READMEs. Geen nieuw ontwerp — alleen het restant van wat het goal al aanwijst.

### dream-liveness-watch
`dlv-b3c27e0e201d` — *dream-liveness-check script: launchctl load-status, laatste dream-event uit
events/dream/<date>.ndjson, skip-reason classificatie (probe_health), tot operator-sluiting* —
`01_code_generation` / `sonnet`.

Het goal noemt drie concrete bewijspunten (launchctl-status, laatste-run-bewijs, skip-reden). Het
panel wees erop dat de letterlijke bewijsbronnen in het goal niet bestaan (`dream_cycle_completed`
wordt nooit geschreven, "de ledger" is het verkeerde bestand, `produces_crap` is geen echte
reason-waarde — de echte waarde is `probe_not_ok` met detail in `probe_health`). De titel van dit
deliverable is aangepast naar de bestaande, juiste bronnen (`events/dream/<date>.ndjson`,
`probe_health`) — dat is een feitelijke correctie van waar het bewijs staat, geen nieuw ontwerp: de
drie te checken dingen blijven exact wat het goal vraagt.

### extractable-plugins-catalog
`dlv-d9bd3f06fa86` — *Framework-coupling meten plus extractie-ranking voor de 5 kandidaten:
CLI-launcher, panel, context-rotation, session-skills, phantom_guard* — `06_design` / `sonnet`,
output-kind `doc`.

Het goal is zelf al een "Panel/research task" met vijf met naam genoemde kandidaten en een
expliciet omschreven output (meten + rangschikken + catalogus). Dat is letterlijk dit ene
deliverable. De onbeantwoorde ontwerpvragen uit het panel (coupling-metriek, extractierichting,
licentie-/geschiedenis-beleid) horen bij de UITVOERING van dit deliverable, niet bij het besluit of
het deliverable bestaat.

### fleet-addressable-agents-phase5 (3 deliverables)
- `dlv-c7e266ff99c0` — *Centrale agent-registry (agent-registry/agents.json, append-only) plus vnx
  agent register CLI, per ADR-031 sectie 3* — `01_code_generation` / `sonnet`.
- `dlv-dbfd41d6507e` — *agent_resolver.py: cross-project dispatch-into-home via de centrale registry
  achter VNX_AGENT_REGISTRY_ENABLED (default off)* — `01_code_generation` / `sonnet`.
- `dlv-99b90d59beac` — *Audit-trail voor cross-project dispatch-into-home: receipt draagt source- en
  target-project_id* — `01_code_generation` / `sonnet`.

Het goal is een dichte opsomming van vier stukken (registry+CLI, dispatch-into-home, audit,
feature-flag) die samen "Phase 5" vormen — en Phase 5 is zelf al canoniek vastgelegd in
`docs/governance/decisions/ADR-031-orchestration-target-ratification.md` §3, inclusief het exacte
padformaat (`~/.vnx-data/agent-registry/agents.json`) en het commando (`vnx agent register`). Dat
lost precies het zwaarste punt op dat het panel als onbeslist markeerde: het opslagmedium. Omdat
ADR-031 al bestaat, is dit geen nieuw ontwerp maar het uitvoeren van een reeds geratificeerd besluit.
De feature-flag is bij deliverable 1 ondergebracht (poort voor de hele registry); audit is een eigen
PR omdat het een eigen acceptatiecriterium heeft. De resolutie-precedentie tegenover de bestaande
4-traps keten en de interactie met `data_dir_guard` — beide door het panel genoemd — zijn
implementatiedetails voor de bouwer, niet architectuurbeslissingen die hier al gemaakt moesten
worden.

### gate-state-yaml-precedence
`dlv-42eb25c767d8` — *gate_state() YAML-precedence layer: evidence_bound_gate key in
governance_enforcement.yaml, level 0-3 naar off/advisory/required, base-branch-read zonder
PR-tree bypass (mirrort attestation-gate.yml)* — `01_code_generation` / `sonnet`.

Het goal is ongewoon concreet: functienaam, het exacte read-site (`verify_pr.py:341`), en een
expliciete DONE-eis ("do NOT reinvent the state machine, add the YAML layer beneath it"). Het panel
vond wel een reële trust-boundary-fout in de kortste-weg-implementatie: een YAML gelezen uit de
PR-tree zou een PR toestaan zijn eigen gate uit te zetten. Die eis (lees van de base branch, niet van
de PR-tree) is geen verzonnen scope — het is het enige patroon waarmee "level en gate_state() zijn
het eens" ÜBERHAUPT veilig te bouwen is, en het patroon bestaat al in dit repo
(`attestation-gate.yml`). De titel benoemt dat zodat de bouwer het niet zelf hoeft te herontdekken.

### gated-climb-executor
`dlv-114daad94d85` — *route_decisions.ndjson outcome-veld populate-forward (was altijd None):
eval-loop dichten door gate-resultaat aan route-decision te koppelen op dispatch_id, zelfstandig van
de usage-aware-routing collector* — `01_code_generation` / `sonnet`.

Het goal noemt deze stap letterlijk als "EERSTE PR ... ZELFSTANDIG, geen collector-dependency,
veilige eerste stap" — een bewust geïsoleerd deliverable binnen een verder nog onaf ontwerp. Het
panel bevestigt de onafhankelijkheid met een code-citaat (`smart_router.py:909` schrijft
`outcome=None`) en corrigeert alleen de naam: "back-fill" wordt "populate-forward", want er is geen
historische outcome-data om terug te vullen. De titel volgt die correctie. Fases 2-4 (capability-map,
shadow-mode, gated escalatie) blijven bewust ONGEDECOMPONEERD: het panel telt daar tien onbesliste
ontwerpvragen (opslagvorm, verval-model, shadow-flip-criterium, risico's zonder mitigatie) die een
eigen ontwerp-ronde nodig hebben — dat is geen dispatch-taak.

## Alleen getagd (geen nieuwe deliverables)

### fabric-self-reporting-truthful
Drie van de vier bestaande deliverables misten `task_class`/`routing_floor`; nu allemaal getagd:

| dispatch_id | titel (verkort) | task_class | routing_floor |
|---|---|---|---|
| `dlv-e40643ae34c9` | plan_gate_panel op canonieke project_root-resolver + lint-regel | `03_refactoring` *(al aanwezig)* | `sonnet` *(al aanwezig)* |
| `dlv-b20c856a0868` | fabric_drift-veld op elk receipt via de doctor-checks | `01_code_generation` | `sonnet` |
| `dlv-a5d440b83b0f` | Peer-berichten spiegelen naar het grootboek | `01_code_generation` | `sonnet` |
| `dlv-5948154786c6` | pre_merge_gate: niet-kunnen-controleren -> HOLD/SKIPPED_UNVERIFIED (OI-1140) | `05_debugging` | `sonnet` |

`dlv-b20c856a0868` en `dlv-a5d440b83b0f` voegen elk een nieuwe capaciteit toe (drift-veld op
receipts, resp. peer-messages als grootboek-event) → `01_code_generation`. `dlv-5948154786c6`
repareert een verkeerde GO-uitkomst op een OI-nummer → `05_debugging`.

### gate-finding-disposition
Eén bestaand deliverable getagd:

| dispatch_id | titel (verkort) | task_class | routing_floor |
|---|---|---|---|
| `dlv-7254430a7a88` | Provider-fouten (403/429/auth) -> unavailable i.p.v. fail (OI-1142) | `05_debugging` | `sonnet` |

Bugfix op een foutieve statusclassificatie → `05_debugging`.

`routing_floor: sonnet` is overal consistent gehouden met de al bestaande tag op
`fabric-self-reporting-truthful` en met de vloot-brede workers-pin (momenteel sonnet, zie
`~/.claude/rules/provider-constraints.md`).

## Goal moet eerst herzien (geen deliverables toegevoegd)

Vier van de twaalf tracks kregen NIETS: niet omdat het goal te kort is, maar omdat het `decision_ref`
van de 2026-08-15-ronde (opus + kimi, onafhankelijk) een kern-aanname van het goal met een concreet
bewijs weerlegt. Een deliverable erbovenop zetten zou diezelfde onjuiste aanname naar de volgende
gate-ronde doorschuiven.

- **business-panel-extract** — het gedragen mechanisme (host-Claude draait zelf als panelist "in
  eigen window") is volgens beide panelisten niet uitvoerbaar tegen de synchrone
  `DispatcherFn`-callback (`deliberation_panel.py:31`) die het plan zelf als bewijs van
  provider-agnosticisme aanhaalt; host-Claude zou tegelijk orchestrator, diverge-seat én synthesiser
  zijn (zelfbevestiging + single point of failure). Regeltelling in het goal klopt bovendien niet
  (299 vs. werkelijk 547 regels; echt extractie-oppervlak ~970 regels inclusief de dispatcher-cluster
  die als "INTERIM — PR-12 consolidation target" gemarkeerd staat). Nodig: een design-pass die eerst
  vaststelt WIE de seats draait voordat er een PR-opsplitsing zinvol is.

- **claude-p-lane-reinstate-probe** — het goal vraagt: meet of `claude -p` op de subscription
  draait, en heropen de lane als dat zo is. Die meting en dat besluit zijn al genomen: de
  headless-lane staat sinds 2026-08-11 open (`routing_policy.yaml` `headless_block.enabled=false`,
  #1455), vastgelegd in dit repo's eigen root-CLAUDE.md ("Headless is opened as of 2026-08-11...
  runs on the Max subscription... measured 2026-08-11"). Eén panelist gaf hier zelfs een expliciete
  BLOCK. Het echte resterende werk zijn de twee daar al genoemde governance-gaten
  (`isolation=worktree` wordt door de headless-lane genegeerd; het fail-closed report-gate heeft een
  rapport met alle vier verplichte kopjes gemist) — maar dat staat niet in het huidige goal-veld, dus
  is dat geen "afleiden", dat zou een nieuw goal schrijven zijn.

- **dreaming-methodology** — het goal is één zin, byte-identiek aan wat het track-titel al zegt: er
  is niets in de tekst te knippen. Eén panelist (opus) gaf hier expliciet BLOCK, niet REVISE: het
  fundament waar dit op zou bouwen (dream-cycli) faalt vandaag op elke run met `probe_not_ok`
  (dezelfde OI-894 die `dream-liveness-watch` bewaakt), en letterlijk uitgevoerd zou het synthetische
  data naast het audit-spoor zetten in tabellen zonder `project_id`-sleutel. Bovendien hangt de track
  af van twee P2-tracks (`learning-d3-provider-filter-drop`, `learning-signal-reattribution`) die het
  goal-veld niet noemt als dependency.

- **fabric-fleet-pass-project-files-repo-only** — het goal citeert
  `claudedocs/20260708-fabric-hygiene-consolidation-initiative.md` (WP1) als bron; dat bestand
  bestaat niet. Het document dat wél bestaat over dit onderwerp
  (`20260708-fleet-role-migration-synthesis.md`) zegt het TEGENOVERGESTELDE van wat het goal
  voorstelt: de drie genoemde MC-restanten (gate-invocation, Chain-transitie, pr-spec-expander) zijn
  volgens dat document geen te strippen canon-duplicaat maar bewust te bewaren runtime-divergentie.
  Een deliverable "strip deze blokken" zou het echte besluitdocument tegenspreken. Nodig: het goal
  herschrijven tegen de juiste bron, niet decomponeren tegen de huidige.

## Verification

**Blocked tracks, voor en na:**
```
$ vnx objective list | grep -c blocked
69   # voor
69   # na — verwacht: deliverables toevoegen verandert derived_status niet,
     # dat gebeurt pas als de plan-gate opnieuw draait en PASSt (niet gedraaid, zie hieronder)
```
(De dispatch-instructie noemde 62 als uitgangspunt, gemeten op een eerder moment op 2026-08-16; de
telling hierboven is vers gedraaid bij aanvang van deze dispatch.)

**Deliverable-listing per aangeraakte track** (acht `vnx deliverable list --objective <track>`-runs,
plus de twee tags-only tracks) — alle 8 nieuwe deliverables staan op `proposed`, alle 5 getagde
bestaande deliverables (4 op `fabric-self-reporting-truthful`, 1 op `gate-finding-disposition`) staan
nog op hun bestaande `ready`-status, ongewijzigd op het punt van status, wel voorzien van
`task_class`/`routing_floor`:

```
[doc-relevant-intelligence-source]      dlv-4f8768059dcf  pr   proposed  01_code_generation / sonnet
[dream-liveness-watch]                  dlv-b3c27e0e201d  pr   proposed  01_code_generation / sonnet
[extractable-plugins-catalog]           dlv-d9bd3f06fa86  doc  proposed  06_design / sonnet
[fleet-addressable-agents-phase5]       dlv-c7e266ff99c0  pr   proposed  01_code_generation / sonnet
[fleet-addressable-agents-phase5]       dlv-dbfd41d6507e  pr   proposed  01_code_generation / sonnet
[fleet-addressable-agents-phase5]       dlv-99b90d59beac  pr   proposed  01_code_generation / sonnet
[gate-state-yaml-precedence]            dlv-42eb25c767d8  pr   proposed  01_code_generation / sonnet
[gated-climb-executor]                  dlv-114daad94d85  pr   proposed  01_code_generation / sonnet

[fabric-self-reporting-truthful]        dlv-e40643ae34c9  pr   ready     03_refactoring / sonnet  (al getagd)
[fabric-self-reporting-truthful]        dlv-b20c856a0868  pr   ready     01_code_generation / sonnet  (nieuw getagd)
[fabric-self-reporting-truthful]        dlv-a5d440b83b0f  pr   ready     01_code_generation / sonnet  (nieuw getagd)
[fabric-self-reporting-truthful]        dlv-5948154786c6  pr   ready     05_debugging / sonnet  (nieuw getagd)
[gate-finding-disposition]              dlv-7254430a7a88  pr   ready     05_debugging / sonnet  (nieuw getagd)
```

**Samengestelde plantekst** (`gated-climb-executor`, via `resolve_plan_source()` uit
`scripts/planning_cli.py` — read-only aanroep van bestaande code, geen panel gedraaid):

```
[... goal_state van de track ...]

----- DELIVERABLES (1) -----
- id: pr:dlv-114daad94d85
  output_kind: pr
  title: route_decisions.ndjson outcome-veld populate-forward (was altijd None): eval-loop dichten
    door gate-resultaat aan route-decision te koppelen op dispatch_id, zelfstandig van de
    usage-aware-routing collector
  status: proposed
  task_class: 01_code_generation
  routing_floor: sonnet
----- END DELIVERABLES -----
```

Bevestigt dat het deliverable, inclusief `task_class` en `routing_floor`, bij een panelist aankomt
zoals de plan-gate op axis 3 en 5 hem leest — zonder dat de panel daadwerkelijk gedraaid is.

## Open Items

- Vier tracks (`business-panel-extract`, `claude-p-lane-reinstate-probe`, `dreaming-methodology`,
  `fabric-fleet-pass-project-files-repo-only`) hebben een goal-herschrijving nodig voordat er
  verantwoord deliverables aan te hangen zijn — zie "Goal moet eerst herzien" hierboven voor de
  reden per track. Operator-actie: `vnx objective set-goal <track>` met het bijgewerkte goal, dan
  pas opnieuw voor deliverables in aanmerking.
- De plan-gate-panel is bewust NIET gedraaid op deze acht tracks (kost zetels, volgende stap).
  Verificatie hierboven bewijst alleen dat de deliverables correct samengesteld worden voor de panel
  — niet dat ze een PASS zouden opleveren.
- 62 andere geblokkeerde tracks (69 totaal minus de 12 uit deze dispatch minus 5 die al eerder
  deliverables hadden = niet 1-op-1 uit te rekenen zonder overlap-check) zijn niet aangeraakt; dit
  was punt 3 van de opschaling, gericht op de twaalf tracks die op 2026-08-16 al door de plan-gate
  zijn geweest.
