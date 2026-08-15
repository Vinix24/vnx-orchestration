# Blocked-track triage — deel a (40 tracks)

Meetopdracht, read-only. Baseline: `main`/`origin/main` op commit `45ace207` (dispatch-basis). Geen code gewijzigd, geen track gesloten, geen tests gedraaid.

## Methode

Per track: het goal gelezen via `vnx objective show <id>`, daarna bewijs gezocht in de code op `origin/main` (git log/grep/show, nooit `--branches`). Twee regels bewaakt:

- Meet het gedrag, niet het label. Een commit-titel die iets claimt is een titel; ik heb gekeken of de functie/vlag/tabel er staat.
- Lees de commit-historie, niet alleen de diff. Elke geciteerde sha is geverifieerd als ancestor van `origin/main`.

## Slottelling

| Uitkomst | Aantal |
|---|---|
| GESHIPT | 9 |
| DEELS | 6 |
| OPEN | 21 |
| ONTOETSBAAR | 4 |
| **Som** | **40** |

## Tabel

| track-id | uitkomst | bewijs in één regel |
|---|---|---|
| dispatch-lane-governance-gap | GESHIPT | #1416/#1478 (worktree), #1415/#1420 (fail-closed rapport), #1414 (conformity-matrix) |
| dispatch-lane-integrity-cluster | GESHIPT | #1419 (OI-1011 push), #1420 (OI-1017), #1416 (OI-1045), #1420+#1415+#1386 (OI-1048) |
| dream-liveness-watch | OPEN | geen check-artifact; DREAM-substrate bestaat (OI-895 #1277, OI-896 #1300) maar de terugkerende liveness-check niet; sluit alleen op operator-besluit |
| fabric-self-reporting-truthful | ONTOETSBAAR | één-regelsthema, geen OI/acceptance-criteria |
| gate-finding-disposition | OPEN | geen `disposition`-veld op gate-resultaten (grep = 0 treffers) |
| kimi-lane-usage-in-orchestrator-skill | ONTOETSBAAR | goal-veld is letterlijk `queued` (placeholder); titel-intent deels aanwezig (SKILL.md:28-34,51-52) |
| learning-loop-skill-consumers | DEELS | skill_refinement (#1008) aanwezig maar niet t0-orchestrator-gericht, geen `--apply`, geen persona-model |
| open-items-backlog-triage | OPEN | geen van de 4 bewegingen (upsert/vervaldatum/netto-stroom/lint-scheiding) geïmplementeerd |
| panel-distillate-per-seat | GESHIPT | `_SEAT_DISTILLATE_BUDGET` per seat (#1334) i.p.v. head-first knip |
| plan-gate-method-revision | GESHIPT | probe gerepareerd + seat-ledger (#1275), zwaarte via governance_variant (#1507), ijkmeting (#1504) |
| pre10-store-res-full | OPEN | zes resolvers naast elkaar; repo-local/central split staat er nog letterlijk |
| producer-freshness-monitor | GESHIPT | per-key freshness + exit-capture + PATH-parity + guard-fired + tripwire (#1267) |
| receipt-quality-b4-router-feedback | OPEN | geen propensity/candidate-score/shadow-alt; shadow_mode_runner is dry-run parity-comparator |
| retire-feature-plan-second-planning-layer | OPEN | `FEATURE_PLAN.md` nog in repo-root; generator + kickoff-gate nog actief |
| retire-redundant-architecture | OPEN | terminal-pinned structuur + legacy lanes staan er nog volledig |
| review-gate-kimi-codex-glm | DEELS | kimi_gate.py standalone aanwezig, maar gate_request_handler.py:94 nog `unknown_review_gate`, geen glm_gate |
| route-decision-ledger | GESHIPT | `_persist_route_decision` (dispatch_cli.py:1614, #1378) schrijft route_decisions.ndjson + per-dispatch JSON |
| router-availability-seam | GESHIPT | enum-gap dicht (#1484) + availability.py als runtime-signaal met cooldown |
| skill-role-reconcile | DEELS | router-naming reconciled (#1465/#1298), maar plan-reviewer geen skill, skill_name niet geretired |
| slop-refactor-panel-review | ONTOETSBAAR | proces-mandaat, geen code; geen slop-finding-tracks zichtbaar |
| smart-routing-cluster | GESHIPT | alle 10 punten in code (#1484-#1507, OI-1176..1188) |
| t0-daemon-driven-lifecycle | OPEN | alleen bouwplan (#1335); geen rotation_daemon.py |
| vnx-init-store-path-parity | DEELS | init/resolver-pariteit + test (#1437), maar migratiepad 4 XDG-projecten ontbreekt |
| worktree-preserve-release-path | GESHIPT | `vnx worktree-release` met dry-run (#1425/#1470), rescue-voor-unlock |
| usage-aware-routing | OPEN | geen collector/3-klassen-routing/burn-rate (grep = 0 treffers) |
| agent-skill-discovery-binding | OPEN | alleen per-project vnx_skills; geen per-provider symlink-materialisatie |
| business-panel-extract | DEELS | panel.py + deliberation_panel op main; skill alleen in ~/.claude/skills/, niet in repo |
| claude-p-lane-reinstate-probe | DEELS | lane open (#1455), maar role-orchestrator.md:81 zegt nog "NEVER claude -p" |
| fabric-fleet-pass-project-files-repo-only | ONTOETSBAAR | levering leeft in andere repos (MC/SEO/sales-copilot), niet toetsbaar hier |
| gated-climb-executor | OPEN | route_decisions `outcome` nog altijd `None` (smart_router.py:908); geen capability-map |
| gemma-4-12b-coder-local-eval | OPEN | geen model-entry/benchmark; wave7_models.yaml kent alleen gemma-4b-local |
| horizon-done-track-graduation | OPEN | geen graduate/archive-verb in horizon CLI |
| infra-connection-manager | OPEN | niets gebouwd (geen skill/agent/script) |
| json-report-contract | OPEN | worker-directive nog markdown-headings; geen JSON-emit |
| learning-capture-fragmentation | OPEN | geen routeringsregel/dedup/koppeling tussen de vier bakken |
| learning-d3-provider-filter-drop | OPEN | D3-filter skipt nog `provider=unknown` (learning_loop.py:449-452); test pint tegendeel |
| learning-signal-reattribution | OPEN | geen re-attributie/no-read-only-code |
| loop-maturity-score | OPEN | geen 0-100 score/dashboard-tegel |
| loop-pattern-catalog | OPEN | geen catalogusmodule; reconcile/learning-tick niet geherformuleerd |
| memory-curation-cadence | OPEN | geen memory-doctor/dedup/archief/orphan-logica |

---

## GESHIPT — bewijs per goal-eis

### dispatch-lane-governance-gap

Goal-eis (1) isolation=worktree gerespecteerd: `9f32ae84` (#1416, OI-1045) laat `run_envelope_headless_plan()` een dispatch-worktree aanmaken via `create_dispatch_worktree()` en hard-aborten bij mislukte creatie. Bevestigd: `scripts/lib/dispatch_envelope.py:803` draait `ClaudeSubprocessAdapter().run(enriched_spec, cwd=wt_path)`, en `:765`/`:770`/`:782` falen hard. Versterkt door `c36e7fad` (#1478, OI-1158) en `bc56078c`/`65c37756` (base_ref-threading).

Goal-eis (2) validate_body()-violatie krijgt geen success-receipt: `c28b7ac4` (#1420, OI-1017/OI-1048) plaatst `validate_body()` vóór `emit_dispatch_receipt()` en zet status op `contract_invalid`. Voorafgegaan door `db7a6046` (#1415, observe) en `c974dda9` (#1386, fail-closed terminal-success).

Goal-eis (3) per resterend mechanisme expliciet vastgesteld: `c0b4c2f7` (#1414) voegt `docs/lane-conformity-matrix.md` toe (3 lanes x 10 mechanismen, elke cel getraced naar een codepad). Bestand aanwezig op main.

### dispatch-lane-integrity-cluster

Vier gaten:

- OI-1011 (commit lokaal -> altijd push of zichtbaar falen): `69570995` (#1419) bindt `pr_enforcement.enforce_pr_exists` per state (pushed->PR, committed->push+PR), één bindingssite voor alle drie lanes; falende push/PR -> ok=False + corrective receipt. Plus `c974dda9` (#1386) origin-branch existence check (`git ls-remote --heads`).
- OI-1017 (rapport met prompt-echo/missende koppen nooit success): `c28b7ac4` (#1420) bindende `validate_body()`-override naar `contract_invalid`; een prompt-echo-rapport mist de vier verplichte koppen en faalt dezelfde check.
- OI-1045 (worktree-isolatie): `9f32ae84` (#1416), zie hierboven.
- OI-1048 (fail-closed rapportpoort bijt): `c28b7ac4` (#1420) + `db7a6046` (#1415) + `c974dda9` (#1386).

Noot: het `delivery`-veld van de track ("#1414 partial, #1415 partial") is stale; #1416/#1419/#1420 landden daarna en maken alle vier eisen compleet.

### route-decision-ledger

Goal-eis: elke door-deur-dispatch laat een joinbare routing-beslissing achter (model, lane, route_reason, fingerprint). Bewijs: `fe7f6248` (#1378, OI-849). `scripts/lib/dispatch_cli.py:1614` `_persist_route_decision()` schrijft (a) `state_dir/route_decisions.ndjson` (append-locked) en (b) `state_dir/route_decisions/<dispatch_id>.json` (atomair). `scripts/lib/dispatch_plan.py:135` `canonical_dict()` bevat `provider`/`model`/`lane`/`route_reason`. Callsite `dispatch_cli.py:1922` draait onvoorwaardelijk voor elke non-dry-run dispatch. Joinable via dispatch_id (`report_to_receipt_converter.py:438`).

### router-availability-seam

Goal-eis (1) enum-gap gedicht zodat tier-mid/high uitdrukbaar zijn: `2cb18258` (#1484) voegt de `claude`-mapping toe aan `_TIER_PROVIDER_TO_ENUM`; `cost_tier.py:15-17` definieert `TIER_LOW`/`TIER_MID`/`TIER_HIGH`.

Goal-eis (2) beschikbaarheid als runtime-signaal met afkoelperiode i.p.v. uitgecommentarieerde routes: `scripts/lib/providers/smart_router/availability.py` (`lane_available`/`record_lane_failure`/`lane_cooldown_remaining`/`cooldown_seconds`); decision-time gates = env vars + CLI-on-PATH + cooldown; `local-gemma` uit via inspectable `disabled_reason`, kimi terug als reguliere route.

### smart-routing-cluster

Tien punten, elk in code op main:

1. Deur geeft geen 'geen keuze' + aparte None-oorzaken: `door_routing.py:46-49` `DECLINE_T0_NEVER_ROUTES`/`DECLINE_EXPLICIT_PROVIDER`/`DECLINE_ROUTER_DISABLED`/`DECLINE_CLASSIFIER_ERROR`; keten eindigt altijd in een ungated lane. Noot: structureel gegarandeerd, geen letterlijke 100-sample-test aangetroffen.
2. Terugvalketen schakelt echt: `066882aa` (#1494) `tier_routing._walk_chain` skipt onbeschikbare lanes via één `lane_available`-gate (OI-1185); getest in `tests/smart_router/test_tier_routing.py`.
3. Afkoeling per faalklasse: `incident_taxonomy.py:394-458` RecoveryContracts — rate-limit 60s, quota-exhausted 3600s, auth-failure `halt_auto_recovery=True` (OI-1186).
4. Eén afkoelklok: `availability.py:19-20,130-146` `cooldown_seconds()` delegeert naar `incident_taxonomy.get_cooldown_seconds` (OI-1188).
5. Sonnet-terugval tier-bewust: `dispatch_cli.py:1095-1119` `_TIER_FALLBACK_MODEL = {"tier-high": "opus-5", "tier-mid": "sonnet-5"}` (OI-1187).
6. Elk model kan gate zijn: `observability_tier.py:29-51` `ADAPTER_DEFAULT_TIERS`/`ADAPTER_MINIMUM_TIERS` met kimi=1, glm=1, deepseek=1; `GOVERNANCE_MIN_TIERS["coding-strict"]=1` (2146f2cd, #1486).
7. Gate-zwaarte via governance_variant: `smart_router.py:527` `GOVERNANCE_VARIANT_GATE` + `derive_governance_variant` (`cb74dec1` #1495); panel-sizing per variant (`58d327ee` #1507).
8. deepseek-v4-pro op kwaliteitsas, geen hardgecodeerde tier-literal: `smart_router.py:245` `_compute_quality_tier` + `routing_recommendations.yaml`.
9. Staging op AUTO per tier + canary + nulmeting: `staging.py` (per-tier flags, SHA-256 canary) + `router_baseline.py` (`21471d85` #1500).
10. Modelveld canoniek uit wave7_models.yaml: `model_normalizer.py` + `governance_emit.py:215` (`38a30a36` #1491 OI-1184, `82729192` #1497 OI-1194).

### producer-freshness-monitor

`a717bcc7` (#1267) levert alle vier onderdelen plus zelf-monitoring:

1. Per-key freshness: `scripts/lib/producer_freshness.py` (per-key groepering, per-producer cadans-drempel, NDJSON `producer_freshness.ndjson`) + `configs/producer_freshness.yaml`.
2. Exit-status-capture: `scripts/lib/job_exit_capture.py` -> `job_exits.ndjson`.
3. PATH/interpreter-pariteit bij SessionStart: `scripts/lib/path_parity.py` + `scripts/hooks/path_parity_check.sh`.
4. Guard-fired-teller: `scripts/lib/guard_stats.py` -> `guard_evaluations.ndjson`, `suspect_silent`-flag.
5. Tripwire: `write_heartbeat` schrijft bij ELKE run een HealthBeacon (producer_freshness.py:496); `hooks/monitor_tripwire.sh` toetst alleen de heartbeat-leeftijd via `find -mmin` (geen gedeelde code/DB/interpreter).

### worktree-preserve-release-path

Goal-eis: `vnx worktree release` met dry-run, herclassificeert per bewaarde worktree, pusht committeerbaar werk naar een branch, en pas daarna unlock+remove. Bewijs: `d86f132d` (#1425) + fix `5c11901d` (#1470). `scripts/lib/worktree_release.py` + `scripts/commands/worktree_release.sh`; wired in `bin/vnx:329` en `:2493-2494`. Dry-run is default (`release_locked_worktrees(*, dry_run=True)`, `--apply` nodig). `list_locked_worktrees()` filtert locked/preserved records; `classify_for_release()` onderscheidt releasable/committable/unpushed_commits/both/detached/unreachable/error; `rescue_worktree()` pusht committable naar `vnx-release/<branch>`; unlock+remove volgt pas NA geslaagde rescue (falende rescue laat de worktree locked). Kanttekening: CLI-naam is `vnx worktree-release` (koppelteken), functioneel identiek aan het goal.

### panel-distillate-per-seat

Goal-eis: distilleer per seat met eigen budget i.p.v. head-first knip over de aaneengeplakte digest; verhoog de default. Bewijs: `7a28d575` (#1334, OI-820). `scripts/lib/deliberation_panel.py:213` `_SEAT_DISTILLATE_BUDGET = int(os.environ.get("VNX_PANEL_SEAT_DISTILLATE_BUDGET", "12000"))`, toegepast per seat in `_digest()` (`:216-235`), head+tail-cut, default verhoogd 6000->12000.

### plan-gate-method-revision

Vier werkstukken:

1. Probe gerepareerd: `scripts/lib/plan_gate_effectiveness_probe.py:196-206` `health()` heeft de `oi_plan_resolved > 0`-clausule niet meer; degraded bij all-attest (#1275).
2. Per-zetel-uitslagen gepersisteerd: `SEAT_LEDGER_RELPATH = ".vnx-attest/plan-gate-seats.ndjson"` in `plan_gate_effectiveness_probe.py:56` en `plan_gate_panel.py:76`, append via `append_chained_entry` (`plan_gate_panel.py:1159`) (#1275).
3. Licht versus zwaar: `plan_gate_enforcement.py:165` `plan_gate_scope()` + `:215` `complex_only_active()` (#1412); de deur size't het panel meetbaar via `derive_governance_variant` + `seat_labels_for_governance_variant` (minimal->0 seats, light->1 seat, `58d327ee` #1507).
4. Onderbouwd besluit: ijkmeting `80957141` (#1504, panel-van-vijf 89,4% gelijk aan eerste zetel) + proportioneel-panel-besluit embodied in #1507.

Restpunten (geen blocker voor GESHIPT, wel te wegen): er ligt geen formele ADR/`decision_ref` vast voor "blocker blijft", en de `VNX_PLAN_GATE_COMPLEX_ONLY`-read-site (#1412) is wees geworden omdat de echte skip via het governance_variant-pad (#1507) loopt.

---

## DEELS — wat nog ontbreekt (als opdracht)

- **review-gate-kimi-codex-glm**: `gate_request_handler.py:94` valt nog terug op `unknown_review_gate` en de default-stack in `review_gate_manager.py:29-35` is nog `["gemini_review","codex_gate","claude_github_optional"]`. Opdracht: wire kimi_gate en glm_gate in de handler en vervang de default-stack door kimi_gate + codex_gate met glm_gate als fallback (gemini eruit), zodat een gefaalde kimi/codex-zetel naar glm valt.
- **learning-loop-skill-consumers**: skill_refinement (#1008) genereert voorstellen voor generieke roles, niet voor de t0-orchestrator-skill, heeft geen `--apply`/accept-spoor, en er is geen Vincent-persona-model. Opdracht: richt refinement op de t0-orchestrator-skill, bouw accept/reject met spoor, en bouw het persona-model.
- **vnx-init-store-path-parity**: init/resolver-pariteit en de init-then-doctor-test zijn geshipt (#1437), maar het migratiepad voor de vier bestaande XDG-projecten (freshproj/gitproj/my-vnx-project/proj) ontbreekt. Opdracht: bouw de XDG->central migratie/backfill.
- **claude-p-lane-reinstate-probe**: de lane is open (#1455) en drie van de vier plekken zijn bijgewerkt, maar `.claude/terminals/T0/role-orchestrator.md:81` zegt nog "NEVER claude -p". Opdracht: update role-orchestrator.md zodat de vier plekken samen bewegen.
- **business-panel-extract**: `scripts/panel.py` + `deliberation_panel.py` staan op main, maar de standalone skill leeft alleen in `~/.claude/skills/business-panel/` en er is geen repo-getrackte kopie of losse spawn+poll-orchestrator. Opdracht: leg de geconvergeerde v1 (skill + thin-call-laag) vast waar hij vindbaar is, en verifieer de drie guards (timeout/atomic-done/unieke paden).
- **skill-role-reconcile**: router-naming is gereconciled (#1465/#1298), maar plan-reviewer heeft nog geen skill of expliciete no-skill-beslissing, `skill_name` is niet geretired (nog in `quality_intelligence.sql:531` en geschreven in `dispatch_create.sh:331`), en de 34% role-loze dispatches is niet onderzocht. Opdracht: geef plan-reviewer een skill of no-skill-beslissing, retire skill_name, onderzoek de role-loze 34%.

---

## OPEN — niets gebouwd of bewijs ontbreekt

Elk van deze 21 heeft geen van de goal-eisen aantoonbaar op main (zie tabelkolom voor de kern van het bewijs). Bijzonderheden:

- **dream-liveness-watch**: geen code-artifact voor de terugkerende check; het DREAM-substrate bestaat (OI-895 #1277, OI-896 #1300), maar dat is niet deze track's levering. Belangrijk: deze track sluit ALLEEN op expliciet operator-besluit. Niet auto-sluiten.
- **learning-d3-provider-filter-drop**: de bug staat er aantoonbaar nog, met een test die het tegendeel vastpint (`test_provider_unknown_filtered` assert `failures == []` voor provider="unknown"); de filter is `learning_loop.py:449-452`, de converter schrijft `provider="unknown"` (`report_to_receipt_converter.py:515-517,583`).
- **gated-climb-executor**: het "eerste PR" (route_decisions outcome back-fillen) is niet gedaan; `smart_router.py:908` staat nog `"outcome": None` en wordt nooit gevuld.
- **open-items-backlog-triage**: de parallelle #1519 is een dispositie-voorstel (docs-only), geen implementatie van de vier bewegingen.

---

## ONTOETSBAAR — goal niet toetsbaar

Vier tracks, apart geteld, bij naam:

1. **fabric-self-reporting-truthful** — één-regelsthema ("een faalmelding noemt de echte reden, een vrij slot leest niet als bezet, en een gate meet de PR in plaats van de werkkopie") zonder OI-refs, PR of acceptance-criteria. Geen unieke done-toestand.
2. **kimi-lane-usage-in-orchestrator-skill** — het goal-veld is letterlijk `queued` (placeholder), dus er is geen toetsbare eindtoestand. Titel-intent is deels aanwezig (kimi-k3 default build-worker en governed-door-not-raw in `t0-orchestrator/SKILL.md:28-34,51-52`), maar de goal zelf is nooit geschreven.
3. **fabric-fleet-pass-project-files-repo-only** — de levering leeft in andere repos (Mission Control, SEOcrawler, sales-copilot), niet toetsbaar tegen deze repo. De master-regel is hier wel gecodificeerd (#1056/#1061).
4. **slop-refactor-panel-review** — proces-mandaat (draai een multi-model review, findings -> tracks), geen code-levering. Of de review heeft gedraaid en findings-tracks zijn aangemaakt is niet uit origin/main-code af te leiden; het objective-store toont geen slop-finding-tracks.

---

## Methodologische noten

- Tijdens de meting schoof `origin/main` op van `45ace207` naar `081c4588` (#1514-#1519, parallelle dispatches incl. een sibling "deel b"-triage). Ik heb tegen de gepinde basis `45ace207` gemeten, en daarna geverifieerd dat alle 23 geciteerde shas nog ancestor zijn van de nieuwe `origin/main` en dat de negatieve signalen (unknown_review_gate, outcome=None, FEATURE_PLAN.md aanwezig) nog op de nieuwe tip staan. Geen revert, geen verdict-wijziging.
- Het `delivery`-veld van een track is geen bewijs; twee tracks hadden een stale `delivery`-veld dat de code tegensprak (dispatch-lane-integrity-cluster, smart-routing-cluster).
