# ADR-091 - Eén bron voor provider en model van een bouwer

**Status:** Voorgesteld 18-11-2026 (track `agnostisch-standaard`, levering ADR-M, dispatch `dlv-74cc8f6dc0df`). Vervangt het besluit worker-provider-kimi-flip van 23-07-2026 (commit `a68b35beb`) via cross-ref; geen bestaande ADR verandert (ADR-079 § 83).
**Datum:** 2026-11-18
**Besloten door:** T0, op de plan-gate van 18-11 (ronde 5, tiebreaker STOP volgens de stop-rule, planversie 6).
**Cross-refs:** ADR-013 (werkers zijn configuratie, geen constante), ADR-018 (de elastische pool), ADR-036 § 2 (een onbekende provider of een onbekend model weigert luid), ADR-079 (§ 83, bestaande ADR's zijn bevroren), ADR-087 (één configuratie-oppervlak; de lezer van `VNX_T{n}_*` komt uit PR-3 van die track); plan `claudedocs/specs/agnostisch-standaard/plan.md` (gitignored, niet in de repo), V2.5, V3.2, V4.1, V4.2, V5.1, V5.3, V5.4, V6.1 tot en met V6.4; leveringen AG-2a1, AG-2a2, AG-2p, AG-2c, AG-2b, AG-2d.

## Context

Provider en model van een bouwer komen vandaag uit minstens zeven plekken in code. Gemeten op `3ce81bfa`:

| Vindplaats | Wat er staat |
|---|---|
| `scripts/lib/vnx_start_runtime.py:124-125` | T1 en T2: `os.environ.get("VNX_T{n}_PROVIDER", PROVIDER_KIMI)` en `os.environ.get("VNX_T{n}_MODEL", "kimi-k3")` |
| `scripts/lib/headless_dispatch_daemon.py:434` | `os.environ.get("VNX_DISPATCH_MODEL", "kimi-k3")`, commentaar op 431-433: "the two are decided separately" |
| `scripts/lib/pool_worker_runner.py:242` | `model or os.environ.get("VNX_DISPATCH_MODEL", "kimi-k3")` |
| `scripts/lib/pool_worker_runner.py:288` | `(tp.get("provider") or "kimi")`, met het commentaar worker-provider-kimi-flip op 285-287 |
| `scripts/lib/dispatch_prepare.py:59` | `prepare(..., model: str = "kimi-k3", ...)` |
| `scripts/lib/worker_registry.py:48-65` | `_HARDCODED_FALLBACK`, claude/sonnet voor T1 tot en met T3, gebruikt als geen registrybestand te vinden is (`:335-340`) |
| `scripts/lib/dispatch_cli.py:1029-1042` en `:1117` | `_DEFAULT_MODEL_PINS` (sonnet voor T1 tot en met T3) en de lader die op `workers-kimi-pinned` keyt; `_override_gate_and_effective_model` (`:3128`, aangeroepen op `:2974`) neemt het model uit de pin als de spec er geen noemt |
| `scripts/lib/dispatch_plan.py:385-401` | D4 via `_resolve_model` (`:510`): bij `pin_semantics: default` vult de pin het model als de spec er geen noemt |

De pin zelf staat in `scripts/lib/providers/provider_constraints.yaml:78-82` (id `workers-kimi-pinned`, `model: sonnet`). De regel op `:74-77` legt al uit dat de lader op die exacte string keyt.

De registry zegt iets anders dan de runtime. `.vnx/vnx_workers.default.yaml` noemt claude/sonnet voor T1 tot en met T3 en claude/opus voor T0 (rol `orchestrator`), alle vier in pool `default`. `worker_registry.py` vindt dat bestand via `Path(__file__).resolve().parents[2]` (`:223` en `:329`). In een geïnstalleerd pakket is dat site-packages, niet het project.

Daarnaast kiest de smart router in de deur bij `provider=AUTO` een paar vóór `validate()` (`dispatch_cli.py:4195-4248`, OI-962). Weigert of faalt de router, dan zet `_route_provider_pre_validate` vast provider CLAUDE met het model van `_tier_aware_fallback_model` (`:2161`, laatste redmiddel `"sonnet"`). De router staat per tier aan of uit via `VNX_SMART_ROUTER_TIER_ZERO`, `_LOW`, `_MID` en `_HIGH` (`config_registry.py:489-504`, standaard `"0"`).

**Waar de kimi-terugval vandaan komt.** Commit `a68b35beb` (23-07-2026, "feat(kimi-flip): default build-workers (T1/T2/T3) from sonnet to kimi-k3") zette alle bouwerstandaarden op kimi, met `or "kimi"` in `pool_worker_runner.py`. Commit `6a5bf036b` (26-09-2026, #1930, "sonnet is the default build worker") zette registry, pin en `_HARDCODED_FALLBACK` terug op sonnet. In de runtime veranderde alleen het commentaar: daar staat sindsdien dat de terugval kimi blijft en "apart besloten" is. Zo ontstonden twee standaarden die van elkaar weglopen. Het operatorbesluit van 23-09 zegt dat sonnet de standaard bouwer is. ADR-013 zegt dat werkers configuratie zijn, geen constante.

## Besluit

**1. Eén bron.** De werkerregistry is de ene bron voor provider en model van een bouwer: eerst `.vnx/vnx_workers.yaml` van het project, dan de meegeleverde `.vnx/vnx_workers.default.yaml`, gelezen via `worker_registry.py`. Dit vervangt uitdrukkelijk het besluit worker-provider-kimi-flip; de terugval in `pool_worker_runner.py:288` en de kimi-standaarden in de tabel hierboven vervallen. Reden: dat besluit was een tijdelijke omzetting tijdens een sonnet-quotumtekort, en het apart houden ervan liet de runtime stil op kimi draaien terwijl registry en pin sonnet zeiden.

**2. De volgorde van oplossen**, voor een terminal en voor de pool:

1. de spec (inclusief de router, besluit 8);
2. `VNX_T{n}_PROVIDER` en `VNX_T{n}_MODEL`, gelezen via de lezer van ADR-087 (config-een-oppervlak PR-3);
3. de registry van het project;
4. het meegeleverde standaardbestand.

De eerste bron die een paar levert wint als geheel. Reden: een vaste volgorde maakt elke uitkomst uit te leggen met één bronnaam.

**3. Het paarcontract.** Elke bron levert een heel paar (provider, model) of niets. Een lagere bron vult nooit een half paar van een hogere aan.

| Invoer van een bron | Paar dat die bron levert |
|---|---|
| model en provider | dat paar; past het model volgens de modelcatalogus niet bij de provider, dan weigeren met `worker_pair_conflict` |
| alleen model | de provider die de catalogus voor dat model noemt; staat het model niet in de catalogus, dan weigeren met `worker_model_unknown` |
| alleen provider | het standaardmodel dat de registry voor die provider noemt; noemt ze er geen, dan weigeren met `worker_model_unresolved` |
| niets | de bron slaat over |

Voorbeeld: `spec.model=kimi-k3` met `VNX_T1_PROVIDER=claude`. De spec is hoger en levert (kimi, kimi-k3); de env telt niet mee. De uitkomst draagt `overridden: [env]`. Reden: aanvullen over bronnen heen gaf in OI-1050 al eens claude met kimi-k3, een paar dat geen enkele bron bedoelde.

**4. Geen registry te lezen: weigeren.** Is ook het meegeleverde bestand niet te lezen, dan weigert het pad met het pad dat ontbreekt. Er is geen terugval in code; `_HARDCODED_FALLBACK` vervalt. Reden: een terugval in code is een derde standaard die stil van het bestand wegloopt.

**5. Vinden via project-root en pakketpad.** De registry van het project wordt gevonden via `scripts/lib/project_root.py`, nooit via `Path(__file__)`. Het standaardbestand reist mee als package-data en wordt gevonden via het pakketpad. Reden: in een wiel wijst `Path(__file__).parents[2]` naar site-packages, en dan weigert besluit 4 elke bouwerstart.

**6. De pool.** Een pool-aanvraag kiest alleen onder registry-regels met de `pool_id` van de bundel en een bouwerrol; `orchestrator` telt niet mee. Het poolschema krijgt een optioneel veld `default_pair: {provider, model}`, gecontroleerd tegen de modelcatalogus. Hebben de bouwers van een pool meer dan één paar en ontbreekt `default_pair`, dan weigert de aanvraag met `pool_default_ambiguous` en de pool-id. Reden: de meegeleverde pool `default` mengt T0 (claude/opus) met de bouwers; zonder de rolfilter weigert hij altijd, en een gemengde pool moet zijn keuze noemen in plaats van haar te raden.

**7. De deur schrijft het hele paar in elke bundel.** De deur lost op via `resolve_worker` en schrijft provider en model in elke bundel en elk plan dat zij of de broker maakt (`dispatch_broker.py:314` schrijft `target_profile` vandaag zoals het binnenkomt). De runtime neemt dat paar als spec-bron. Een bundel met maar één van de twee weigert met `bundle_pair_incomplete`. Reden: zo kan de runtime voor een dispatch niet meer afwijken van de deur, wat registry of pin ook zeggen.

**8. De AUTO-router is een spec-bron, geen tweede standaard.** Staat de router aan voor de tier en geeft hij een paar, dan telt dat paar als spec-bron met `source: router`. Staat hij uit of geeft hij niets, dan valt de dispatch door naar de gewone volgorde van besluit 2 (env, registry van het project, standaardbestand). De vaste CLAUDE-terugval in `_route_provider_pre_validate` en `_tier_aware_fallback_model` vervalt. Reden: die terugval is een standaard in code naast de registry, precies wat besluit 1 opheft.

*Grens, aanvaard:* de tier-bewuste keuze van OI-1187 (tier-high nooit stil een klasse lager) zit dan in de registry of in de router, niet meer in de deur. Een registry die voor tier-high een te licht model noemt, geeft dat model.

**9. De pin blijft een controle.** Het id `workers-kimi-pinned` blijft, want de lader keyt op die exacte string (`dispatch_cli.py:1117`); een andere naam laat de pin stil wegvallen. Wijkt het opgeloste model af van de pin, dan waarschuwt de deur zoals nu. De pin vult geen model meer in. De commentaarregel in `provider_constraints.yaml` (historische naam, waarde sonnet) hoort bij AG-2c, niet bij deze ADR.

**10. Het spoor.** Elke oplossing logt `worker_resolution: provider, model, source, overridden`. Reden: een vergeten env in een lange shell of een launchd-job is dan in één regel te zien.

**11. Een veilige overgang.** De volgorde is: eerst erbij (AG-2a1 de lader, AG-2a2 `resolve_worker`), dan leest de poolrunner het paar uit de bundel (AG-2p), dan de deur (AG-2c), dan de runtimepaden buiten de deur (AG-2b), dan de terugvallen weg (AG-2d). AG-2b, AG-2c en AG-2d mergen pas nadat `providers reset kimi --project vnx-dev` gemeten is. Vóór en na elke merge legt de T0 de opgeloste bouwerconfig vast (T1, T2, T3 en de pool, provider en model); elk verschil staat met reden in het rapport. AG-2a1 en AG-2a2 dragen dezelfde vergelijking en moeten nul verschil geven. Eén structurele toets, geparametriseerd over elk bouwerstartpad (`vnx_start_runtime`, `headless_dispatch_daemon`, `pool_worker_runner`, `dispatch_prepare` en de deur), eist dat elk pad weigert met de registry weg en een gestripte env. Die toets is de kwaliteitsvloer, geen grep. Reden: een grep kan `PROVIDER_KIMI` als standaard niet van een vergelijking onderscheiden; de toets wel.

*Grens, aanvaard:* tussen AG-2p en AG-2d bestaan oude en nieuwe paden naast elkaar. Een bundel zonder paar houdt tot AG-2b de oude terugval van de poolrunner, en tot AG-2d blijft `_HARDCODED_FALLBACK` staan. De structurele toets dekt pas na AG-2d elk pad.

## Gevolgen

**Wat deterministisch is, en wat niet.** Het hele contract is deterministisch: een vaste volgorde, een paartabel, een catalogusopzoeking en vaste weigercodes. Er zit geen model in de oplossing. Alleen de AUTO-router mag een model gebruiken om een tier te bepalen; zijn uitkomst gaat door dezelfde paartabel en dezelfde catalogus als elke andere spec.

**De leveringen bouwen hiertegen:**

- AG-2a1: de lader via project-root en pakketpad, het standaardbestand als package-data;
- AG-2a2: `resolve_worker` met de volgorde, het paarcontract, de poolkeuze met `default_pair`, de weigercodes en het spoor; geen aanroeper wijzigt;
- AG-2p: `pool_worker_runner` leest het paar uit de bundel, `bundle_pair_incomplete` bij een half paar;
- AG-2c: de deur via `resolve_worker`, het paar in elke bundel, de router als spec-bron, de pin als controle;
- AG-2b: de runtimepaden buiten de deur;
- AG-2d: `_HARDCODED_FALLBACK` en de oude oplospaden weg; de structurele toets volledig.

**Weigercodes:** `worker_pair_conflict`, `worker_model_unknown`, `worker_model_unresolved`, `pool_default_ambiguous`, `bundle_pair_incomplete`, plus de weigering met het ontbrekende registrypad.

**Wat deze ADR niet toestaat:**

- een standaardprovider of standaardmodel voor een bouwer in code, voor welke provider ook;
- een lagere bron die een half paar van een hogere aanvult;
- een tweede standaard naast de registry, ook niet in de pool, de router of de pin;
- de registry vinden via `Path(__file__)`.

**Vervangen:** het besluit worker-provider-kimi-flip (23-07-2026, `a68b35beb`) en de "apart besloten"-regel van `6a5bf036b` in `pool_worker_runner.py` en `headless_dispatch_daemon.py`. ADR-013 blijft ongewijzigd; deze ADR past het toe op provider en model.

## Alternatieven

- **De pin als bron.** Verworpen: `workers-kimi-pinned` is een governancecontrole in een constraintsbestand, geen configuratie van een project. Een ander project kan er zijn bouwers niet mee instellen zonder de controle te wijzigen.
- **De aparte kimi-terugval voor de pool houden.** Verworpen: twee standaarden lopen van elkaar weg. Dat is precies wat worker-provider-kimi-flip opleverde: registry en pin op sonnet, de pool stil op kimi.
- **Een vrije standaard per pad.** Verworpen: ADR-013. Werkers zijn configuratie, geen constante per bestand.
