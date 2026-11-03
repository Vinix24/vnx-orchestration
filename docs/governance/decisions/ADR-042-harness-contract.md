# ADR-042 - Het harness-contract

**Status:** Accepted (dispatch 20261020-g1-0-hermeting-r2, track golf-1-opruimen; uitvoering in golf-2-hardenen en golf-3-harness-agnostisch)
**Date:** 2026-10-22
**Decided by:** operator. Richting op 17-10 (feature-stop, harness-agnostisch als golf 3); het concept en punt 6 aangenomen op 22-10, samen met de keuze voor het `terminals`-blok.
**Cross-refs:** ADR-018 (elastische pool); ADR-024 (single-entry door als standaardlane, gedeeltelijk herzien in punt 6); ADR-036 (modelidentiteit uit de registry); ADR-041 (tiebreaker bij de review-cap); plan `claudedocs/specs/golf-1-opruimen/plan.md`; hermeting `docs/investigations/2026-10-20-golf-1-hermeting.md`.

## Context

Op 17 oktober besloot Vincent dat VNX moet werken met elke harness. Eerst betekent dat Claude Code, Codex en Kimi op hun abonnement, plus een open-source harness als bewijs, bijvoorbeeld OpenCode. Extern mag "werkt met elke harness" pas als golf 3 dat bewijst.

De fabric kent vandaag geen harness als begrip. Hij kent providers: claude, kimi, glm, deepseek, codex, gemini. Daarin lopen twee assen door elkaar:

- **De harness** is het programma dat de agent-lus draait: `claude`, `codex`, `kimi`, straks `opencode`.
- **Het model** komt van een provider: Anthropic, Moonshot, Zhipu, DeepSeek, OpenAI.

Voorbeeld van de vermenging: de deepseek- en glm-stoel draaien de `claude`-CLI met een ander endpoint (`ANTHROPIC_BASE_URL`). Kimi en codex draaien hun eigen CLI. In de deur heten alle vier "provider".

De Claude Code-specifieke bindingen van vandaag:

| Rol | Binding | Waar |
|---|---|---|
| Bouwer | `claude -p` via de envelope | `scripts/lib/dispatch_envelope.py` (lane `claude_headless`) |
| Bouwer, terminal-gebonden | `subprocess.Popen(["claude", ...])` per terminal | `scripts/lib/subprocess_dispatch.py`, opt-in `VNX_ADAPTER_T{n}=subprocess` |
| Bouwer, andere modellen | eigen CLI of de claude-CLI met een ander endpoint | `scripts/lib/provider_dispatch.py` |
| Reviewstoel | codex-CLI, kimi-CLI, claude-CLI met ander endpoint | `scripts/review_gate_manager.py` |
| T0 | interactieve Claude Code-sessie, rol via `CLAUDE.md`, state via de SessionStart-hook, rotatie via een tmux-spawner en `/kickoff` | `.claude/terminals/T0/`, `.claude/hooks/`, `scripts/t0_rotate_spawn.sh` |

`vnx role sync` schrijft de T0-rol al naar `CLAUDE.md`, `AGENTS.md` en `GEMINI.md`. Hooks, skills en de rotatie bestaan alleen voor Claude Code.

## Besluit

1. **Twee assen, apart benoemd.** Elke lane-keuze in de deur noemt de harness en het model. Het receipt draagt beide. Een deepseek-model via de claude-CLI is dan zichtbaar als harness `claude`, model `deepseek-...`.

2. **Wat een harness levert, per rol.** Een harness mag een rol vervullen als hij dit levert:

   | Eis | Bouwer | Reviewstoel en panelist | T0 |
   |---|---|---|---|
   | Niet-interactief draaien met een opdracht als tekst in een gegeven werkmap | ja | ja | nee |
   | Een exitcode en een eindantwoord dat de fabric leest | ja | ja | nee |
   | Schrijven en git binnen de werkmap | ja | nee, read-only | ja |
   | De permissiegrens van de fabric is af te dwingen (allowlist, sandbox of read-only) | ja | ja | ja |
   | Te stoppen bij een deadline, door het proces te beëindigen | ja | ja | nee |
   | Modelidentiteit vast te stellen uit configuratie of uitvoer, niet geraden | ja | ja | ja |
   | Een oordeelblok dat de runner parset | nee | ja | nee |
   | Een interactieve sessie die bij de start de canonieke rol laadt | nee | nee | ja |
   | Een startinjectie van de state (hook of gelijkwaardig) | nee | nee | ja |
   | Contextgebruik te meten of te begrenzen, zodat rotatie kan | nee | nee | ja |

3. **Wat van de fabric is en harness-onafhankelijk blijft:** de deur en de dispatch-spec, de worktree-isolatie, het rapportcontract, het grootboek en de receipt-pijplijn, de review-runner en de merge-deur, horizon en open items. Een harness raakt deze onderdelen alleen via zijn adapter.

4. **Een adapter per harness.** Spawnen, permissies zetten, de modelidentiteit lezen en het eindantwoord lezen gebeurt in een adapter per harness. Buiten de adapter staat geen harness-specifieke code. Golf 3 meet dat met een grep-check in CI.

5. **tmux blijft, als transport.** tmux is het injectiepad naar interactieve CLI's en de route voor de T0-rotatie (besluit 24 september). Het is geen lane voor bouwers.

6. **De terminal-gebonden kiezer gaat weg, de bouwstenen blijven (herziet ADR-024 gedeeltelijk).** De kiezer `VNX_ADAPTER_T{n}=subprocess` kiest de lane per terminal, buiten de deur om, en kan alleen `claude` starten. Dat botst met punt 3 en 4, en de terminals T1 tot T3 zijn sinds 8 oktober legacy. `subprocess_dispatch.py` zelf is geen losse lane: de pool start hem (`pool_manager.py:122`), en dertien modules importeren hem of `subprocess_dispatch_internals`, waaronder `provider_dispatch.py`, `crash_recovery_sweep.py` en de PreToolUse-hook. Daarom:
   - de kiezer en het terminal-gebonden entrypoint gaan weg in golf 2 (track `terminal-binding-afbouwen`), na het audit-spoor;
   - `subprocess_dispatch_internals` blijft als gedeelde bouwsteen tot golf 3 hem onder de claude-adapter brengt;
   - de pool krijgt in golf 2 een besluit: start via de deur, of blijft op de interne bouwsteen;
   - wat de lane bood (eerdere bevindingen meegeven, lange runs) wordt niet in golf 1 of 2 nagebouwd: dat is nieuw gedrag en valt onder de feature-stop. Het staat als eis voor de adapters in golf 3.
   - de terugvalluiken van de deur, `VNX_DISPATCH_LEGACY=1` en `VNX_SINGLE_ENTRY_DISPATCH=0`, blijven tot golf 2. Ze openen het legacy-pad (ADR-024 punt 3) en zijn het rollback-net van ADR-028 fase 0 ("P6, not before"). Weghalen herziet beide ADR's en gebeurt in `terminal-binding-afbouwen`, met een eigen akkoord.
   De operator aanvaardde dit punt op 22-10; het herziet ADR-024 gedeeltelijk.

### Amendement 2026-10-27 — de pool start zijn worker via de deur (T-D0)

Bouwt besluit 6 uit op het punt dat daar nog open stond ("de pool krijgt in golf 2 een besluit"). Track `terminal-binding-afbouwen`, taak T-D0, op het operator-akkoord van 27-10 (V1 en V2 van het plan).

- **De poolroute is de deur.** `scripts/lib/pool_manager.py::_pool_worker_command` start standaard `scripts.lib.pool_worker_runner`. Die levert via `dispatch_bridge.deliver_via_door` zolang `dispatch_flags.single_entry_enabled()` waar is — en dat is de standaard. De pool zet daarmee geen tweede leveringspad meer naast de deur.
- **Het terugdraaien is `VNX_SINGLE_ENTRY_DISPATCH=0`.** Dan (of met `VNX_DISPATCH_LEGACY=1`, dat altijd wint) start de pool weer `-m scripts.lib.subprocess_dispatch`, ongewijzigd en bereikbaar. Dat is het hard geparkeerde rollback-net van besluit 6 en ADR-028 fase 0 ("P6, not before"); het blijft staan tot een eigen akkoord het weghaalt.
- **`VNX_POOL_TASK_CONSUMER` houdt zijn betekenis.** `=1` betekent nog steeds "start de single-claim runner" en doet dat ook onder het rollback-net. `=0` of ongezet kiest geen route meer: de deurvlag beslist. Wie de oude route terugwil, gebruikt de deurvlag, niet deze vlag.
- **`terminal_leases` blijft een pooltabel.** Geen migratie en geen kolom weg. De tabel draagt `project_id` al in zijn sleutel — `schemas/migrations/0031_runtime_tenant_fk_repair.sql:146` (`UNIQUE(terminal_id, project_id)`) — zoals ADR-007 eist voor elke centrale tabel.
- **De eigenaar van de lease-vernieuwing is de pool-worker.** De poolmanager schrijft de rij direct na de spawn (`PoolStateRepository.add_or_refresh_pool_lease`); daarna houdt de worker hem levend: `pool_worker_runner._PoolLease` ververst `last_heartbeat_at` zolang hij loopt en geeft de rij vrij aan zijn einde. `subprocess_dispatch._pool_heartbeat_loop` blijft voorlopig staan als vernieuwer van de rollback-route; T-G1 haalt hem weg zodra de test op deze route op main groen is.
- **De lezers van `dispatches/active/` blijven ongewijzigd.** Dit amendement verandert geen lezer; die afstemming is T-G5, na K-7 van `audit-spoor-kern`.

### Amendement 2026-10-28 — een receipt draagt geen `terminal_snapshot` meer (T-B2, dispatch `dlv-9cae7e00a9c2`)

Bouwt de "Golf 2"-regel over het `terminals`-blok uit, met de beslissing die de plan-gate van 27-10 als punt 4 eiste: `terminal_snapshot` verdwijnt uit het receipt en wordt **niet** vervangen door een kopie van `live_work`. De identiteit van een receipt komt uit de dispatch-rij (audit-spoor groep 2), en "wat loopt er" is `live_work` in `t0_state.json`, op één plek gelezen in plaats van op elke receiptregel gekopieerd. Daarmee gaan in dezelfde golf `_enrich_terminal_snapshot` uit `append_receipt_internals/enrichment.py`, `is_headless_t0()` (de altijd-onware voorwaarde achter `VNX_ADAPTER_T0`), het `terminals`-blok van `t0_state.json` met zijn schrijver (`build_t0_state._build_terminals`) en zijn lezers, en de re-export van `collect_terminal_snapshot` uit `append_receipt.py`; `terminal_snapshot.py` zelf blijft, want `canonical_state_views.build_terminal_snapshot` en `terminal_state.json` zijn buiten deze taak.

## Gevolgen

### Golf 1

**Blijft staan**, omdat het contract of de fabric-kern het nodig heeft:
- de deur (`bin/vnx dispatch`, `scripts/lib/dispatch_plan.py`, `scripts/lib/dispatch_cli.py`), `dispatch_envelope.py` met `claude_headless`, `provider_dispatch.py`;
- `review_gate_manager.py`, de stoelen en `plan_gate_panel.py`;
- het rapportcontract en de receipt-pijplijn;
- `vnx role sync` met de drie rolbestanden;
- tmux als injectiepad en de rotatie-spawner;
- de Claude Code-hooks en -skills. In golf 3 worden ze de claude-adapter van de T0-rol. Ze verdwijnen niet.

**Mag weg in golf 1, zodra de hermeting aantoont dat niets het nog aanroept:**
- de plakken uit het tmux-opruimplan van 24 september (#1909) die geen grootboek, geen terminalbinding en geen terugvalluik van de deur raken. De hermeting van PR #2143 (`docs/investigations/2026-10-20-golf-1-hermeting.md`) zet P08 (deels), P09 (deels), P11, P12 en de afgesplitste delen van P15 en P16 in golf 1, als clusters C1 tot C5; P05, P06, P07, P10, P13, P14, P15 en P16 (de rest) gaan naar golf 2;
- `FEATURE_PLAN.md` als tweede planningslaag (track `retire-feature-plan-second-planning-layer`, eigen plan).

### Golf 2

**Gaat naar golf 2 (track `terminal-binding-afbouwen`, na het audit-spoor (track `audit-spoor-volledig`)):**
- de rolmappen `.claude/terminals/T1` tot `T3`, de pane-daemons en `vnx start` (besluit 8 oktober: schrappen na het audit-spoor);
- `scripts/lib/headless_dispatch_daemon.py` met `headless_orchestrator.py`: de daemon schrijft `dispatches/active/` en leest `terminals.lease_state`;
- de legacy `.md`-stroom (ADR-025) en de terugvalluiken van de deur (besluit 6);
- de terminal-gebonden kiezer en het entrypoint van `subprocess_dispatch.py` (besluit 6);
- het `terminals`-blok in `t0_state.json` en zijn lezers. In golf 2 wordt het vervangen door `live_work`, en de lezers (`feature_state_machine.py:286-296`, `append_receipt_internals/enrichment.py:166`, `commands/dispatch.sh:79`, `commands/status.sh:104`) gaan mee naar `live_work`;
- `terminal_leases` in `runtime_coordination.db`, met ADR-007;
- de lezers van `dispatches/active/` en `crash_recovery_sweep`, voor zover het audit-spoor (track `audit-spoor-volledig`) ze niet al vervangt.

## Verworpen alternatieven

- **Alleen model-agnostisch, zonder harness-as.** Dan blijft een deepseek-model via de claude-CLI onzichtbaar, en golf 3 kan niet meten welke harness wat deed.
- **Een universele harness voor alles, bijvoorbeeld OpenCode.** Dat kost de abonnementen. Draaien op meerdere CLI's met hun eigen abonnement is het tweede onderscheidende punt van VNX (besluit 20 september).
- **In golf 1 alles weghalen wat Claude Code-specifiek is.** Dan breekt de enige werkende T0 voordat golf 3 een tweede heeft.
