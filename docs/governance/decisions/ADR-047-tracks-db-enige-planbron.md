# ADR-047 - De tracks-DB is de enige planbron

**Status:** Proposed (dispatch dlv-be010d69d2c2, track retire-feature-plan-second-planning-layer)
**Datum:** 2026-10-24
**Besloten door:** T0, onder het mandaat van de operator voor golf 1-3 van 24-10. De plan-gate van 24-10 gaf START met één vereiste wijziging: leg de autoriteitswijziging vast in een ADR die ook de vervangende semantiek benoemt, vóór de eerste implementatie. Dit is dat ADR. Alle metingen staan op main `a680d940`.
**Cross-refs:** plan `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/retire-feature-plan-second-planning-layer.md` (revisie 1); inventaris `20261024-inv-feature-plan`; ADR-005 (eerst schrijven, dan de toestand wijzigen); ADR-007 (`project_id` op elke tabel); ADR-030 (de plan-first-gate leest de track, geen bestand); ADR-035 (receipt v2); ADR-046 (de vorm van dit besluit).

## Context

VNX heeft twee planningslagen. De tracks-DB houdt tracks, deliverables, dispatches en hun afhankelijkheden. Daarnaast staan er drie bestanden die dezelfde waarheid nog een keer zeggen: `FEATURE_PLAN.md`, `PR_QUEUE.md` en `ROADMAP.yaml`.

De bestandlaag is al hol. Op main `a680d940` noemen 28 codebestanden `FEATURE_PLAN.md` (scope `scripts`, `vnx_cli`, `bin`, `hooks`, `.github`), 13 noemen `PR_QUEUE.md` en 18 noemen `ROADMAP.yaml`. De lezers verstaan alleen kopjes die de schrijver nooit schrijft.

- `_parse_feature_plan` (`scripts/closure_verifier.py:190`) leest `## PR-<n>:` en `### F<n>-PR<n>:`. Het levende `FEATURE_PLAN.md` heeft 0 van beide: de kopjes zijn `## Recently Merged`, `## Active features`, `## Completed` en `## Planned (from ROADMAP.yaml)`.
- `parse_feature_plan` (`scripts/lib/queue_reconciler.py:115`) leest dezelfde twee vormen. Elke dispatch krijgt daardoor de melding "not in FEATURE_PLAN.md" (`scripts/lib/queue_reconciler.py:371`).
- De per-PR-modus van de closure-verifier faalt altijd op `pr_exists_in_plan` (`scripts/closure_verifier.py:2340`), want de PR staat nooit in die lege lijst.
- `materialize_review_contract` gooit een `ValueError` zodra de PR-sectie ontbreekt (`scripts/lib/review_contract.py:352`). Op het levende bestand is dat altijd.
- `reconcile_queue_state.py` leest standaard `FEATURE_PLAN.md` uit de projectroot (`scripts/reconcile_queue_state.py:315`) en stopt met een fout als het bestand er niet is (`:318`). Het bestand is sinds #1630 untracked en gitignored, dus een verse worktree heeft het niet.

Wat overblijft is geen datamigratie maar verwijdering. De bestanden zijn gegenereerd, de DB is de bron. De tracks-DB heeft alles wat de semantiek nodig heeft: `tracks` (`schemas/migrations/0022_track_layer.sql:31`), `track_dependencies` (`:71`), `dispatches` met `pr_ref` (`:110`), de deliverables-view over `output_ref` (`schemas/migrations/0027_planning_horizon_and_deliverable_view.sql:65`) en `track_pr_delivery` (`schemas/migrations/0032_track_pr_delivery.sql:32`).

De spagaat kost vandaag bewijs. Een check die altijd faalt op een plan dat niets zegt, is geen check. Dit ADR haalt de tweede laag weg en legt vast wat elke plek daarna leest.

## Besluit

De tracks-DB is de enige planbron. `FEATURE_PLAN.md`, `PR_QUEUE.md` en `ROADMAP.md` gaan weg als input. `ROADMAP.yaml` blijft bestaan als voorbeeld, maar geen script leest nog een vast pad: `seed_tracks_from_roadmap.py` werkt alleen met een expliciete `--file` (C2).

Alles wat een planbestand las, leest daarna de DB. De zeven regels onder "Vervangende semantiek" zijn die vervanging. Elk noemt de regel, de bron per veld en de PR van het plan die hem uitvoert.

Een tweede principe: waar een veld niet af te leiden is, is de uitkomst hoog en niet medium. Een ontbrekend plan is geen vrijbrief.

## Vervangende semantiek

### 1. Autoriteit: de tracks-DB is de enige planbron

**Regel.** Een script leest een plan uit de DB. `FEATURE_PLAN.md`, `PR_QUEUE.md` en `ROADMAP.md` zijn geen input meer. `ROADMAP.yaml` is een voorbeeld: geen script leest een vast pad, en `seed_tracks_from_roadmap.py` werkt alleen met een expliciete `--file`. Zonder die flag stopt het script en schrijft het niets.

**Bron per veld.** Track: `tracks.track_id`, `tracks.title`, `tracks.phase`, `tracks.pr_ref`. Deliverable: de deliverables-view over `dispatches.output_ref`. PR: `dispatches.pr_ref`. Afhankelijkheid: `track_dependencies`. Levering per PR: `track_pr_delivery.delivery_kind`.

**Uitvoering.** C2 voor elke standaardlezer van `ROADMAP.yaml` en elke automatische aanroeper van de seeder. De lijst is hieronder compleet, gemeten met `git grep -n "ROADMAP.yaml\|seed_tracks_from_roadmap" -- scripts vnx_cli bin hooks .github`.

Standaardlezers (een pad dat zonder argument uit een vaste regel komt):

| Plek | Wat het vandaag leest | Wat C2 doet |
|---|---|---|
| `scripts/seed_tracks_from_roadmap.py:436` | `resolve_project_root(__file__) / "ROADMAP.yaml"` als `--roadmap` ontbreekt (`:420`) | `--roadmap` heet daarna `--file` en is verplicht; zonder flag stopt het script |
| `scripts/planning_cli.py:127` | `PROJECT_ROOT / "ROADMAP.yaml"` in `_resolve_roadmap_path` | de default gaat weg; `vnx objective sync` eist een pad |
| `scripts/planning_cli.py:2072` | `root / "ROADMAP.yaml"` in `_resolve_anchored_roadmap_path` | de auto-seed vervalt, zie de aanroepertabel |
| `scripts/planning_cli.py:296` | print een hint zonder pad (`seed_tracks_from_roadmap.py --apply`) | de hint noemt `--file` |
| `vnx_cli/commands/horizon.py:131` | default `--roadmap` = `project_dir / "ROADMAP.yaml"` | de default gaat weg; `--roadmap` wordt verplicht voor `sync` |
| `vnx_cli/main.py:679` | `--roadmap` met default `None`, die via `planning_cli` op de default valt | volgt `planning_cli`; dezelfde verplichting |
| `scripts/lib/track_reconciler.py:270` | `repo_root / "ROADMAP.yaml"` als bron 3 (`:330`), ook via `scripts/lib/objective_reconcile.py:874` | bron 3 gaat weg; de reconciler leest alleen DB, NDJSON en git |
| `scripts/build_strategy_projection.py:69` | `_REPO_ROOT / "ROADMAP.yaml"` | expliciet pad verplicht, anders stopt het script |
| `scripts/backfill_dispatch_track_linkage.py:780` | `project_root / "ROADMAP.yaml"` als `--roadmap` ontbreekt (`:749`) | `--roadmap` wordt verplicht |
| `scripts/build_pr_queue.py:27` | `_REPO_ROOT / "ROADMAP.yaml"` | gaat mee met B4, want dat bestand verdwijnt daar |
| `scripts/build_feature_plan.py:136` | `_REPO_ROOT / "ROADMAP.yaml"` als fallback | gaat mee met B3, want dat bestand verdwijnt daar |

Automatische aanroepers van de seeder (niemand typt het commando):

| Plek | Wat het doet | Wat C2 doet |
|---|---|---|
| `scripts/planning_cli.py:2092` (`maybe_auto_seed`), `:2132` | `seeder.seed(..., apply=True)` in de prelude-hook | de hook vervalt; er is geen seed meer zonder pad |
| `scripts/lib/dispatcher_supervisor_ticks.sh:54` (`_maybe_auto_seed_tracks`) | draait `planning_cli.py objective sync --apply` per tick bij `VNX_AUTO_SEED_TRACKS=1` | de tick vervalt |
| `scripts/dispatcher_minimal.sh:604` | roept `_maybe_auto_seed_tracks` aan in de prelude | de aanroep vervalt met de tick |
| `scripts/planning_cli.py:430` (`cmd_objective_sync`) | `seeder.seed(...)` voor het handmatige `vnx objective sync` | blijft, maar met een verplicht pad |

Twee plekken blijven staan: `vnx_cli/commands/doctor.py:1110` en `scripts/check_no_file_derived_data_paths.py:89` controleren de pad-literaal, ze lezen het bestand niet.

Het kopje in het voorbeeld zelf liegt ook: `ROADMAP.yaml:2` noemt het bestand "de SINGLE SOURCE OF TRUTH" en noemt de twee gegenereerde views. C2 haalt dat kopje weg; het bestand blijft als voorbeeld van het formaat.

### 2. Per-PR-closure

**Regel.** Een PR is gesloten als drie dingen samen waar zijn, en geen van de drie is een blik in een planbestand:

1. er is een dispatch voor die PR in de store, met `dispatches.pr_ref` op dat nummer en een terminale receipt voor die dispatch in het receipt-grootboek;
2. de forge zegt dat de PR gemerged is voor dat nummer, en `scripts/pr_merge.py` schrijft daarbij het `pr_merged`-record;
3. het poortbewijs dat de merge-deur al eist op de kop staat er: de CI-run met conclusie success op precies de kop-SHA (`merge_preflight_ci_check.check_ci_run_for_head`), de vereiste reviewstoelen van de risicoklasse van de kop (`closure_verifier.check_review_gate_for_merge`, `scripts/closure_verifier.py:1240`) en geen `contract_invalid`-uitkomst. De deur zelf staat beschreven in `scripts/pr_merge.py:8`.

**Bron per veld.** PR-identiteit: `dispatches.pr_ref`. Terminale receipt: het receipt-grootboek op die dispatch-id. Gemerged: de forge plus het `pr_merged`-event. Poortbewijs: de poortrecords, niet het plan.

**Uitvoering.** A5. De check `pr_exists_in_plan` (`scripts/closure_verifier.py:2340`) verdwijnt; er komt geen vervanger, want de vraag "bestaat deze PR in het plan" is de vraag "bestaat deze dispatch".

### 3. Heel-feature-closure

**Regel.** Een feature is een track. Een track is klaar als elk deliverable op die track met bewijs is afgeboekt en elke PR die de track noemt gemerged is.

- Deliverable afgeboekt: `dispatches.state = 'completed'` op de deliverable-rij, geschreven door `vnx deliverable close --evidence <pr>` (`scripts/planning_cli.py:3072`). Nooit afgeleid uit een merge: `deliverable_close_candidates` (`scripts/lib/track_reconciler.py:883`) meldt alleen, het sluit niet (OI-1969).
- PR's gemerged: alle nummers in `tracks.pr_ref` zitten in de gemergede set, en er is geen leveringsblokkade. `_delivery_hold` (`scripts/lib/track_reconciler.py:71`) blokkeert zolang een gekoppelde PR een expliciete `track_pr_delivery.delivery_kind` anders dan `complete` heeft (`schemas/migrations/0032_track_pr_delivery.sql:32`).
- Die twee samen zijn precies waar `_compute_derived_status` (`scripts/lib/track_reconciler_status.py:22`) `done` voor teruggeeft, met de blokkades van open items en harde track-afhankelijkheden ervoor.

**Wat de drie bestandschecks vervangt.**

| Check van vandaag | Vervanging | Reden |
|---|---|---|
| `feature_plan_status` (`scripts/closure_verifier.py:2858`) | `tracks.derived_status = 'done'` van de track-reconciler | een bron, geschreven door `track_reconciler` |
| `pr_queue_complete` (`:2867`) | dezelfde afgeleide status plus de leveringsblokkade | de oude check vergeleek twee gegenereerde totalen; de blokkade noemt de PR die niet af is |
| `metadata_sync` (`:2875`) | niets, de check vervalt | hij vergeleek titel en dependency-flow tussen twee bestanden die beide weggaan; de afhankelijkheid die telde staat als edge in `track_dependencies` en `_compute_derived_status` geeft `blocked` op een open harde edge |

De derde regel is een verwijdering en geen verplaatsing. Een driftmelding tussen twee bestanden is niet te vervangen door een derde bestandsvergelijking. De check heette metadata-drift en de metadata krijgt één eigenaar.

**Bron per veld.** Trackstatus: `tracks.derived_status`, geschreven door `scripts/lib/track_reconciler.py`. Blokkades: `track_open_items` met `link_type = 'blocks'` en `resolved_at IS NULL`. Afhankelijkheid: `track_dependencies` tegen `tracks.phase`.

**Uitvoering.** A2 voor de lezer (`closure_verifier`), B2 voor de laatste aanroeper die hem twee planpaden geeft (`scripts/roadmap_manager.py:488`).

### 4. Queue-reconciliatie

**Regel.** PR-identiteiten komen alleen uit de dispatch-toestand. Een PR staat in de queue als een dispatch hem noemt: `dispatches.pr_ref` (`schemas/migrations/0022_track_layer.sql:110`), met branch `dispatch/<dispatch_id>`. De melding "not in FEATURE_PLAN.md" (`scripts/lib/queue_reconciler.py:371`) verdwijnt: een dispatch waarvan de PR verder nergens bekend is, is een queue-regel en geen driftwaarschuwing. De projectie (`pr_queue_state.json`, `pr_queue_state.yaml`) blijft een cache. `PR_QUEUE.md` gaat weg in B4.

**Bron per veld.** `pr_id`: `dispatches.pr_ref`. Toestand: de dispatch-directory plus de terminale receipt (prioriteit 2 en 3 van het queue-contract, onveranderd). Afhankelijkheid: `track_dependencies`.

**Uitvoering.** A2. De lezer is `QueueReconciler.reconcile` (`scripts/lib/queue_reconciler.py:308`), de CLI is `reconcile_queue_state.py` (`scripts/reconcile_queue_state.py:315`).

### 5. Deliverable-naar-dispatch-aggregatie

**Regel.** Een deliverable is een rij in `dispatches` met `metadata_json.deliverable = true` en een `output_ref` van de vorm `<output_kind>:<dispatch_id>` (`scripts/planning_cli.py:2744`). De deliverables-view rolt de dispatches met dezelfde `output_ref` op (`schemas/migrations/0027_planning_horizon_and_deliverable_view.sql:65`). De `derived_status` van die view is berekend en niet gezaghebbend; de status van een deliverable is de `state` van de deliverable-rij zelf.

- **De PR van een deliverable** is de `pr_ref` van zijn dispatches. Alle rondes delen hem: een vervolgronde draagt het achtervoegsel `-r2` of `-r3`, hetzelfde `pr_id` en dezelfde `work_ref`. Gemeten op `dlv-209d2bcb21d2` en `dlv-209d2bcb21d2-r2`: beide `pr_id` 2176, `work_ref` `dispatch/dlv-209d2bcb21d2`. De aggregatie is dus `DISTINCT pr_ref` over de rijen met die `output_ref`, en rondes leveren nooit een tweede PR op.
- **De status** komt uit de deliverable-rij en niet uit de laatste receipt. Een receipt zegt dat een dispatch eindigde, niet dat het deliverable verscheen (OI-1969).
- **Afhankelijkheden** bestaan alleen tussen tracks: `track_dependencies(from_track_id, to_track_id, kind)` (`schemas/migrations/0022_track_layer.sql:71`). Een PR erft de afhankelijkheden van zijn track. Een afhankelijkheid per deliverable bestaat niet en deze track voegt er geen toe.
- **Volgorde binnen een track** is het werk van de T0 en wordt niet opgeslagen. De DB zegt welke tracks van welke afhangen, niet in welke orde de PR's van één track lopen.

**Bron per veld.** PR: `dispatches.pr_ref`. Status: `dispatches.state` van de deliverable-rij. Track: `dispatches.track`. Afhankelijkheid: `track_dependencies`. Levering: `track_pr_delivery.delivery_kind`. Titel: `metadata_json.title` van de deliverable-rij.

Een grens die dit ADR laat staan: een werkdispatch zonder eigen `output_ref` telt niet mee in de view-rollup (OI-1969). `vnx horizon queue` leest daarom de deliverable-rij en de `pr_ref` van de rijen met die `output_ref`, en toont `-` als er geen is.

**Uitvoering.** C1 (`vnx horizon queue`, tekst en `--json`). A2 levert de PR-identiteit waar C1 op joint.

### 6. Review-contract

**Regel.** Het review-contract wordt gebouwd uit de dispatch-toestand en de deliverable-rij. Er is geen plansectie meer, en een ontbrekende sectie is geen fout.

| Veld | Bron van nu af | Wat verdwijnt |
|---|---|---|
| `closure_stage` | de dispatch-toestand: `merged` bij een merge op de forge of een `pr_merged`-event op de dispatch, `in_review` bij een reviewresultaat op de kop, anders `open` | `_parse_pr_queue_status` (`scripts/lib/review_contract.py:286`) |
| `pr_title` | `metadata_json.title` van de deliverable-rij; `track` en `feature_title` uit `dispatches.track` en `tracks.title` | `_parse_pr_section` (`:215`) |
| `dependencies` | de harde edges van de track van de dispatch in `track_dependencies` | `_parse_pr_section` |
| `risk_class`, `merge_policy`, `review_stack` | de risicoklasse van de kop (`review_risk_class.merge_head_risk_class`, `scripts/lib/review_risk_class.py:534`) en de stack van het project; is de klasse niet af te leiden, dan `high`, nooit `medium` | `_parse_pr_section` |
| `deliverables`, `non_goals`, `scope_files`, `estimated_time` | de metadata van de deliverable-rij en `dispatch_paths` van de dispatch; leeg is toegestaan en is geen fout | `_parse_pr_section` |

De twee inhoudsparameters (`feature_plan_content`, `pr_queue_content`, `scripts/lib/review_contract.py:334` en `:335`) gaan weg, net als de `raise` op een ontbrekende sectie (`:352`) en `pr_queue_state_snapshot` (`scripts/lib/pr_queue_state_snapshot.py`). De docstring bovenaan (`scripts/lib/review_contract.py:7`) noemt daarna de DB.

De terugval op `high` is bewust. De merge-deur doet hetzelfde wanneer de klasse niet af te leiden is, en een contract dat `medium` zegt waar de deur `high` eist, maakt van een onbekende klasse een lagere vloer.

**Uitvoering.** B4, risicoklasse high, vloer opus. Een review-contractwijziging krijgt geen deepseek-flash-vloer (gat 4 van de plan-gate). A4 haalt de materializer-CLI al weg; B4 doet de semantiek.

### 7. Volgorde

**Regel.** Twee afhankelijkheden in het plan stonden omgekeerd. Ze staan hieronder recht.

**A6 wacht tot B2 de laatste aanroeper van de interface heeft weggehaald.** A6 haalt de planparser uit `pr_queue_manager` (`load_feature_plan`, `scripts/pr_queue_manager.py:1672`). De aanroepers zijn `scripts/roadmap_manager.py:206` (`load_feature_plan`) en `:706` (`create_dispatch_from_pr`), en `roadmap_manager` is B2's bestand. Zonder deze volgorde verdwijnt de interface terwijl er nog een aanroeper staat. De derde aanroeper, `scripts/generate_t0_recommendations.py:640` (`check_pr_dependencies`, `scripts/pr_queue_manager.py:459`), gaat binnen A6 zelf mee.

**B4 wacht tot de laatste `PR_QUEUE.md`-schrijver weg is.** `git grep -n "PR_QUEUE.md" -- scripts` geeft vier schrijvers:

| Schrijver | Regel | Eigenaar |
|---|---|---|
| `scripts/build_pr_queue.py` | `:105` | B4, de generator zelf |
| `scripts/pr_queue_manager.py` (`queue_file`, `update_markdown`) | `:115`, `:315` | A6, samen met de planparser |
| `scripts/reconcile_queue_state.py` (de `PR_QUEUE.md`-render) | `:256` | A2, samen met de nieuwe PR-bron |
| `scripts/commands/new_worktree.sh` (scaffold) | `:223` | B5, de laatste |

De laatste schrijver is de scaffold in `scripts/commands/new_worktree.sh:223`. Het plan zet B5 ná B4, dus B4 zou het bestand weghalen terwijl een nieuwe worktree het nog schrijft. B5 splitst daarom: **B5a** haalt de schrijver in `new_worktree.sh` weg en loopt vóór B4; **B5b** haalt de drie sjablonen, `ROADMAP.md`, de `FEATURE_PLAN.md`-scaffold in `vnx init` en de `reconcile_queue_state.py --repair`-regel uit de canonieke T0-rol weg en loopt erna.

**A4 haalt ook de CI-aanroeper weg.** Het plan noemde `verify_closure.py` dood omdat het geen importeur heeft. De Profile C-job van de CI roept het aan: stap `Run closure verification` (`.github/workflows/vnx-ci.yml:843`) draait `scripts/verify_closure.py` (`:848`). A4 haalt die stap weg. Een wijziging in `.github/workflows/` maakt A4 risicoklasse high met vloer opus, geen medium met deepseek-flash.

**De volgorde die overblijft.** A1, A2, A4 en C1 eerst. A3 en A5 na A2. B1 na A1. B2 na A5. A6 na B2. B3 na A1, A4 en B2. B5a vóór B4. B4 na A4, A6 en B5a. B5b na B4. C2 na B5b. C-last als laatste. Twee bouwers nooit op hetzelfde bestand: `closure_verifier.py` (A2, A5), `pr_queue_manager.py` (A6), `review_contract.py` (A4, B4), `.github/workflows/vnx-ci.yml` (A4).

## Gevolgen

- **Geen migratie en geen dataverlies.** De bestanden waren gegenereerd. `FEATURE_PLAN.md` is sinds #1630 untracked en gitignored, dus het verdwijnt met de schrijver. `PR_QUEUE.md` is tracked en gaat in B4 weg. Terugdraaien is `git revert` per PR, met het retired-paths-fragment in dezelfde revert.
- **Elk verwijderd pad krijgt een fragment onder `scripts/ci/retired_paths.d/`.** `scripts/ci/check_retired_paths.py` faalt wanneer de oude kop terugkomt. Dat is de wachter die deze uitfasering vasthoudt.
- **Het canonieke contract krijgt een statusblok in deze PR.** `docs/core/70_QUEUE_TRUTH_CONTRACT.md` noemt `FEATURE_PLAN.md` nu prioriteit 1 (`docs/core/70_QUEUE_TRUTH_CONTRACT.md:57`, regel P-1 op `:65`). Het statusblok zegt per bestand welke PR de laatste lezer weghaalt. De rest van het contract wordt in B4 herschreven, wanneer die lezer weg is.
- **Elke PR toetst gedrag, niet een ImportError.** Een lezer die vandaag een lege lijst krijgt, krijgt straks een gevulde lijst. A2 en A5 toetsen op een dispatch die vandaag op het plan faalt.
- **Consumers merken het pas na een release.** Twee wijzigingen raken de vloot pas dan en staan in de release-notitie: de regel uit de canonieke T0-rol (`vnx role sync` draagt hem uit) en `vnx init` zonder planbestanden.
- **Een gedeclareerd done track blijft done.** `_compute_derived_status` houdt de gedeclareerde fase als laatste woord wanneer het bewijs onvolledig is. Dit ADR verandert dat niet.
- **Een ontbrekende risicoklasse wordt `high`.** Dat is een gedragswijziging in het review-contract en ze is bedoeld.
- **De consistentietest tussen de twee lagen valt weg.** `tests/test_roadmap_consistency.py` vergelijkt `ROADMAP.yaml` met de twee gegenereerde bestanden, met de twee generatoren uit zijn eigen docstring. Zonder die generatoren is er niets meer te vergelijken. De test gaat mee met de eerste generator die verdwijnt (B3) en zijn regel in `scripts/ci/test_exclusions.txt:186` gaat in dezelfde PR weg. De regel in B4 ("de uitsluiting weg, de test is vandaag groen") kan niet: groen is hij alleen zolang de lagen bestaan.

## Wat dit ADR niet verandert

- **De merge-deur en de stoelregel.** Welke stoelen de deur eist blijft `scripts/lib/review_seat_evaluation.py` en de risicoklasse van de kop.
- **De plan-first-gate.** Die leest de track en zijn `OI-PLAN`-blokkade (ADR-030), geen bestand. Dit ADR raakt hem niet.
- **De deliverables-view.** Die blijft een afgeleide rollup met een berekende status.
- **Afhankelijkheden per deliverable.** Ze komen er niet. Alleen tracks hangen van tracks af.

## Alternatieven die zijn afgewezen

**(a) De lezers de kopjes van het levende bestand laten verstaan.**
Afgewezen. Dan moet de generator die kopjes ook schrijven, en dan is er weer een tweede laag die kan driften. De leegte is het symptoom, niet de oorzaak: 28 codebestanden houden een waarheid bij die de DB al heeft.

**(b) De generators bijwerken zodat het bestand weer klopt.**
Afgewezen. Dat maakt de tweede laag duur en houdt hem. Elke dispatch zou dan twee schrijfacties en een reconciliatiemoment krijgen, voor een bestand dat niemand als bron nodig heeft.

**(c) `PR_QUEUE.md` houden als leesbaar overzicht.**
Afgewezen. Een overzicht is een commando (C1, `vnx horizon queue`), geen bestand dat naast de DB kan staan. Een bestand met een eigen `derived_status` is precies de drift die dit ADR wegneemt.

**(d) Een deliverable-afhankelijkheid toevoegen om de planvolgorde in de DB te zetten.**
Afgewezen. Volgorde binnen een track is een planningsbeslissing van de T0, geen feit over de code. De DB kent alleen de harde afhankelijkheid tussen tracks, met een `derivation_source` die zegt waar hij vandaan komt.
