---
schema_version: 1
dispatch_id: plangate-p0-glm-harness
provider: glm-harness
sub_provider: zai
model: glm-5.2
terminal_id: T3
pool_id: headless
role: reviewer
task_class: implementation
pr_id: none
duration_seconds: 301.139
exit_code: 0
token_usage:
  input: 13953
  output: 5983
  cache_read: 0
cost_usd: 0.01985916
route_decision:
  strategy: default
  selected_provider: glm-harness
  selected_model: glm-5.2
---

# Dispatch plangate-p0-glm-harness

- Provider: glm-harness
- Terminal: T3
- Duration: 301.1s

## Instruction

PLAN-GATE review of a P0 bug-fix plan for vnx-orchestration. Verdict the DESIGN, not prose. Verified facts: migrate_future_system.py fails (unbalanced-parens in _matching_paren:211 + a headless_runs->dispatches FK-mismatch); vnx migrate (coordination_db.py:266-270) only applies the bootstrap ledger, not 0027/0028; sales-copilot store stuck at user_version=26 (no tracks.horizon). Assess: (1) is D1's sqlite3-introspection-instead-of-hand-parser the right call, or risks? (2) is folding 00NN into vnx migrate (D2) safe re: ordering/idempotency/ADR-007? (3) the FK-mismatch root-cause guess (ordering/foreign_keys pragma) — plausible? (4) any missing risk (data-loss, half-migrated states, the has_horizon silent-degrade)? End with exactly one line: VERDICT: PASS or VERDICT: REVISE + numbered findings.

PLAN:
# Horizon schema-migration fix — PLAN (P0)

**Track:** `horizon-schema-migration-fix` (now, P0). **Escalated by:** sales-copilot (finding-doc
+ repro). **Verified against code** 2026-07-05.

## Problem (two coupled, both confirmed)

**Bug A — two-ledger gap.** `vnx migrate` / `init_coordination_db` (`coordination_db.py:266-270`)
applies only the bootstrap ledger `runtime_coordination_v{2..10}.sql` (user_version-based). The
Horizon schema lives in a SEPARATE ledger: `schemas/migrations/0027_planning_horizon_and_deliverable_view.sql`
+ `0028_tracks_derived_status.sql`, applied by `scripts/migrate_future_system.py` — which the
fleet-sync never ran. Pre-Horizon stores (user_version<27) miss `tracks.horizon`; `vnx horizon list`
silently degrades to UNSCHEDULED (`has_horizon`-guard, `tracks.py:~219`) — hiding the gap.
`horizon add --horizon now` "succeeds" but the horizon is ignored. Verified: sales-copilot store
user_version=26, no `horizon` column; MC/seo/vnx-dev at 31 (fine) — sales-copilot is the stuck
half-cutover store.

**Bug B — `migrate_future_system.py` is broken** (the only fix for A). Fails two ways:
(1) `unbalanced parentheses in SQL` — `_matching_paren` (:211) via `_paren_group` (:260) chokes on
the nested `CASE WHEN SUM(CASE WHEN ... IN (...))` in the 0027 deliverables VIEW (and string-literals);
(2) a second run surfaced `foreign key mismatch - "headless_runs" referencing "dispatches"`. No
working migration path to the Horizon schema.

## Design

### D1 — fix `migrate_future_system.py` (Bug B, the blocker)
- Replace the hand-rolled paren-matcher (`_matching_paren`/`_paren_group`) with **sqlite3-based
  introspection**: don't parse SQL by hand — apply the migration to a throwaway/attached in-memory
  DB and read the resulting object definitions from `sqlite_master.sql`, OR use `sqlite3`'s own
  parser to extract the view/column spec. If a hand parser is kept, it MUST balance nested parens
  AND skip string-literals/`--` comments. Root-cause the `headless_runs→dispatches` FK-mismatch
  (likely an ordering / `PRAGMA foreign_keys` / missing-parent-table issue during the migration
  transaction) and fix it.
- Idempotent + safe: guard each migration by a marker (a `schema_migrations` row or a column-exists
  check) so re-running is a no-op; wrap in a transaction; back up before applying.

### D2 — `vnx migrate` applies the 00NN ledger (Bug A)
- After the bootstrap ledger, `init_coordination_db`/`vnx migrate` also drives the `migrate_future_system`
  path so ONE idempotent `vnx migrate` fully migrates a store (bootstrap + future-system). Order:
  bootstrap first (establishes the base tables the 00NN migrations depend on), then 00NN.
- `vnx update` / fleet-sync invokes `vnx migrate` per project-store so a future sync never leaves a
  store half-migrated again.

### D3 — migrate the stuck store + prove
- Run the fixed `vnx migrate` against sales-copilot's store (backup exists:
  `.bak-20260705-premigrate`) → user_version reaches 31, `tracks.horizon` present, `vnx horizon list`
  buckets its 8 tracks into now/next/later instead of UNSCHEDULED.

### D4 — tests
- A pre-27 fixture store: `vnx migrate` brings it to current with `tracks.horizon`; re-run is a no-op;
  the 0027 deliverables VIEW is created without the paren error; the FK-mismatch does not recur.
  `horizon add --horizon now` then lists under NOW, not UNSCHEDULED.

## Guards / non-goals
- ADR-007: migrations stay project_id-composite-key safe.
- Do NOT unify the two ledgers in this PR (sales' option 3) — that's a larger refactor; keep this a
  targeted fix. Note it as a follow-up.
- Never destroy data: back up the store before migrating; the migration is additive.

## Acceptance
sales-copilot store migrates to 31 + horizon-usable (its 8 tracks schedulable); a pre-27 fixture
migrates clean + idempotent; no paren/FK error.

## Response

Ik heb de code gelezen. Hier is mijn beoordeling van het ontwerp.

## Analyse

**Bug B(1) root-cause is verkeerd gediagnosticeerd in het PLAN.** De 0027-migratie wordt toegepast via `schema_migration.apply_script_if_below` (`migrate_future_system.py:1799`), die SQL splitst met `_split_sql_statements` — een puntkomma-splitser die strings en `--`/`/* */`-comments afhandelt (`schema_migration.py:89`). Het roept NOOIT `_matching_paren`/`_paren_group` aan. De `_matching_paren` wordt alleen gebruikt voor het parsen van de CREATE TABLE/INDEX-tekst van `sqlite_master`: de `dispatches`-rebuild op regel 604 en index-SQL op 260/485/495. De geneste `CASE WHEN SUM(CASE WHEN ... IN (...))` in de deliverables VIEW is dus niet de offender. De werkelijke errorbron is `_matching_paren` op de `dispatches` CREATE TABLE-tekst (of een index-SQL) waarbij `_mask_quoted_sql` (regel 171) strings en identifiers maskeert, maar `--`- en `/* */`-comments NIET. Een `--`-comment met een haakje in de opgeslagen CREATE-tekst gooit de teller om. D1's "vervang hand-parser door sqlite3-introspectie" is gebaseerd op een verkeerde diagnose en overschrijft het probleem.

**D1 is deels onuitvoerbaar zoals geformuleerd.** Voor index-solo-detectie (260/485/495) kan introspectie werken — `PRAGMA index_xinfo` wordt daar al gebruikt; `_paren_group` is alleen een fallback voor expressie-indexen. Maar voor de `dispatches`-rebuild op regel 604 moet de CREATE TABLE-tekst worden *herschreven* (solo-UNIQUE laten vallen, project_id + composite toevoegen). Introspectie geeft kolommen, maar kan de volledige CREATE-tekst met constraints/triggers niet regenereren. De minimale, correcte fix is `_mask_quoted_sql` uitbreiden met commentaar-masking (≈5 regels), niet een introspectie-herschrijving.

**Bug B(2) FK-mismatch — de gok is plausibel maar niet afgelijnd.** `0031_runtime_tenant_fk_repair.sql:60` declareert `FOREIGN KEY (dispatch_id, project_id) REFERENCES dispatches(dispatch_id, project_id)`. Met `PRAGMA foreign_keys = ON` (gezet in `run()`, regel ~2990) vereist SQLite dat de parent-tabel een UNIQUE/PK heeft op die exacte kolom-combinatie. Als de ADR-007-repair (stap A) de `dispatches` niet composite heeft gemaakt, faalt het aanmaken/inserten van de FK met "foreign key mismatch - headless_runs referencing dispatches". Dus: ontbrekende parent-UNIQUE is de echte oorzaak — geen pragma-volgorde. De plandocumentatie moet dit vastpinnen: 0031 vereist dat de ADR-007-repair volledig is uitgevoerd op dezelfde verbinding vóór 0031. De huidige `run()`-volgorde (A vóór C) suggereert dat dit zo is, maar de repair is een no-op wanneer `_dispatches_needs_adr007_repair` false retourneert — een store die composite mist maar waar de detector een false negative heeft, valt erdoorheen. De detector (`_dispatches_needs_adr007_repair`) moet worden geverifieerd voor de sales-copilot-vorm.

**D2-volgorde heeft een gat.** `init_coordination_db` stamt tot `runtime_coordination_v10.sql` → user_version=10. De 00NN-walk begint bij 0022 met een preflight die `dispatches` v22-schema eist. Er is geen 11→21-brug in `vnx migrate`. Sales-copilot (user_version=26) springt schoon over het gat heen (≥22), maar een verse `vnx init` + `vnx migrate` op een lege store komt vast te zitten op de 0022-preflight. D2 moet dit expliciteren: óf de bootstrap stamt al naar ≥22 (verifieer), óf de walk heeft een 11-21-bridge. Het plan zwijgt hierover.

**D2 — QI/RC coupled migration ontbreekt.** `run()` sluit af met `_run_w1_coupled_migration(db_path)` (stap E) die de Quality-Intelligence-DB samen met RC migreert. D2 laat dit weg. Een `vnx migrate` die alleen de RC-walk draait, laat de QI-DB achter. Acceptatiecriteria dekken dit niet.

**D2 — fleet-sync auto-migrate is een nieuw risico.** D2 laat `vnx update`/fleet-sync per store `vnx migrate` draaien. De ADR-007-rebuild doet `DROP TABLE dispatches` + `RENAME` met `foreign_keys=OFF` op elke store die nog niet composite is. Idempotent (no-op) wanneer al composite — maar voor elke store die niet-composite is, draait een tabelrebuild tijdens een routinematige fleet-sync zonder per-store backup. D3 heeft een `.bak` voor sales-copilot; D2 zou `vnx migrate` moeten back-uppen of de no-op-voorwaarde hard moeten stellen vóór de brede uitrol.

**`has_horizon` silent-degrade — niet gefixeerd, alleen gemigreerd.** `tracks.py:219` valt terug naar een INSERT zonder `horizon`-kolom wanneer `has_horizon` false is. Op een pre-27 store "slagt" `horizon add --horizon now` maar dropt de waarde stilletjes — intentie-dataloss. D3 migreert sales-copilot, maar andere fleet-stores die pre-27 staan (en er zijn er mogelijk meer) blijven stilletjes degraderen. Acceptantie zou moeten eisen: `vnx horizon add` faalt luid of waarschuwt wanneer `has_horizon` false is, in plaats van stilletjes de kolom weg te laten. Het plan noemt het symptoom maar heeft geen guard.

**Half-migrated-staat bij 0031.** 0031 doet `DROP TABLE headless_runs; ALTER TABLE headless_runs_new RENAME TO headless_runs`. Als de FK-mismatch mid-0031 optreedt, rolt `apply_script_if_below`'s SAVEPOINT terug — maar verifieer dat het adaptive 0031-pad (regel 2861+) niet zelf transacties beheert buiten die SAVEPOINT. Het plan claimt "wrap in a transaction" als een D1-eis, maar controleert niet of 0031 al buiten de SAVEPOINT om commit/rollback doet. Een crash tussen DROP en RENAME laat de store zonder `headless_runs`. Acceptatie zou een 0031-crash-test moeten bevatten.

**0031 vs ADR-007 R3.1-spanning.** `0031`-SQL hardcodeert `project_id TEXT NOT NULL DEFAULT 'vnx-dev'` voor `headless_runs_new` en `dispatch_attempts_new`. De R3.1-resolver in `migrate_future_system` ("NEVER default to 'vnx-dev'") is in tegenspraak met de SQL-standaardwaarde. Voor bestaande rijen is de INSERT-stamp OK (resolver leidt af uit DB-pad), maar toekomstige directe inserts die weglaten, krijgen de sentinel. De plan-guard "migrations stay project_id-composite-key safe" dekt dit niet. Volg-up, niet blocker, maar noteer het.

## Conclusie

De roadmap voor het oplossen van het blokkeringsprobleem is richtinggevend juist (los de parser op, vouw 00NN in `vnx migrate`, migreer de vastgelopen store), maar D1 richt zich op de verkeerde codepath en de diagnose van Bug B(1) klopt niet met de daadwerkelijke aanroeppaden. D2 mist de QI-coupled-migratie en de 11-21-bridge. De `has_horizon`-silent-degrade blijft bestaan voor de rest van de vloot. REVISE.

VERDICT: REVISE
1. Bug B(1)-diagnose is fout: 0027 loopt via `_split_sql_statements`, niet via `_matching_paren`; de haakjeserror komt van `dispatches`/index-CREATE-tekst. Herformuleer D1: uitbreiden van `_mask_quoted_sql` met `--`/`/* */`-commentaar-masking op de regel 604/260/485/495 call-sites, niet een sqlite3-introspectie-herschrijving (die de CREATE-tekst niet kan regenereren).
2. D1 "sqlite3-introspectie" is onuitvoerbaar voor de `dispatches`-rebuild op regel 604 ( tekst moet worden herschreven, niet alleen geïnspecteerd); reserveer introspectie voor de index-detectie-fallback.
3. Bug B(2) oorzaak vastpinnen: 0031 vereist `dispatches` composite UNIQUE(dispatch_id, project_id) op dezelfde verbinding vóór 0031; verifieer `_dispatches_needs_adr007_repair` heeft geen false negative voor de sales-copilot-vorm, anders faalt 0031 FK met "mismatch". Het is geen pragma-volgordeprobleem.
4. D2: verifieer de 11→21 user_version-bridge — `init_coordination_db` stamt slechts tot v10; 00NN-preflight eist v22. Expliciteer of de bootstrap naar ≥22 stamt of voeg een bridge toe; anders loopt een verse `vnx init` + `migrate` vast op de 0022-preflight.
5. D2: voeg `_run_w1_coupled_migration` (QI-DB) toe aan de `vnx migrate`-pad; acceptatie dekt nu alleen RC, waardoor QI-DB ongemigreerd achterblijft.
6. D2: back-up of harde no-op-voorwaarde vóór fleet-sync auto-migrate; de ADR-007-rebuild doet DROP/RENAME met `foreign_keys=OFF` op elke niet-composite store tijdens routinematige sync.
7. `has_horizon` silent-degrade fixen in `vnx horizon add` (faalt luid/waarschuwt wanneer `has_horizon` false), niet alleen sales-copilot migreren; andere pre-27 stores in de vloot degraderen anders stilletjes door.
8. D4: voeg een 0031 crash/half-migrated-test toe — verifieer dat het adaptive 0031-pad binnen `apply_script_if_below`'s SAVEPOINT blijft (geen eigen commit/rollback buiten de savepoint), anders kan een crash tussen DROP en RENAME een store zonder `headless_runs` achterlaten.
9. Volg-up noteren: 0031's `DEFAULT 'vnx-dev'` voor `headless_runs_new`/`dispatch_attempts_new` staat in spanning met ADR-007 R3.1 ("never default to vnx-dev"); bestaande-rij-stamp is OK, toekomstige directe inserts krijgen de sentinel.

## Findings

None
