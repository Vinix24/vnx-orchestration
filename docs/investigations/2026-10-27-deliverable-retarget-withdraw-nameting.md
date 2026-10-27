# Nameting — deliverable-retarget-withdraw: de drie doelpunten, en de neerwaartse weg zoals hij gebouwd is

**Dispatch-ID**: dlv-d609b5af323b
**Measured on**: `origin/main` = `ef1a5f11` (na #2255 en #2257), branch `dispatch/dlv-d609b5af323b`
**Date**: 2026-10-27
**Role**: research-analyst · **Model**: deepseek-flash · **Provider**: deepseek-harness
**Store**: `vnx-dev`, `runtime_coordination.db`, `user_version` 36 (read-only)
**Track goal**: `deliverable-retarget-withdraw` — PR-0 (ADR-049, #2235), PR-1 (`vnx deliverable retarget`, #2241), PR-2 (`withdrawn`, migratie 0036, `vnx deliverable withdraw`, #2255)

Dit document wijzigt geen code en geen store. Het meet elk van de drie doelpunten van de track opnieuw, noemt per punt het commando en de uitkomst, en geeft een verdict. Daarna volgen de aanbeveling per open levering (P09, P21) en de stand van de twee open items die #2255 achterliet.

**Methode.** De ruwe lezingen op de levende store gaan via `sqlite3 "file:…?mode=ro&immutable=1"`. De CLI-commando's (`vnx deliverable list`, `vnx objective show`, `vnx objective reconcile`) openen de store via `_db_conn`, dat onvoorwaardelijk `PRAGMA journal_mode = WAL` zet (`scripts/planning_cli.py:2277`) en dus schrijft; ze draaiden daarom op een byte-identieke kopie in `/tmp` (`shasum -a 256` gelijk, `577e2c8e…981`) met dezelfde `--state-dir`-inhoud. Geen schrijfcommando tegen enige store. De "voor"-meting komt uit de store-backup van vóór 0036, `runtime_coordination.pre-0036-20261027-1046Z.db` (`user_version` 35, 10:46Z — vóór de herindeling van 11:01Z).

## Tabel — de drie doelpunten

| # | Doelpunt | Commando | Uitkomst | Exit | Verdict |
|---|---|---|---|---|---|
| 1 | `vnx deliverable retarget` en `vnx deliverable withdraw` staan op main, met een rode run op de oude kop | `pytest tests/test_deliverable_retarget.py tests/test_deliverable_withdraw.py tests/test_migration_0036_deliverable_withdrawn.py` | 137 passed, 0 failed; rode runs op `1ce78b68`, `a86defd7`, `5bb1eb5c` (#2241) en `be4860d2`, `b3fce3e6` (#2255) — zie §1 | 0 | **met** |
| 2 | Een ingetrokken levering houdt geen track open en telt niet als geleverd en niet als mislukt | `vnx deliverable list … --json [--all]`, `vnx objective show`, `deliverables`-view, `vnx objective reconcile` zonder `--apply` | 2 `withdrawn`-rijen: `completed_count` 0, `in_flight_count` 0, `planned_count` 0, `withdrawn_count` 1, `derived_status` `withdrawn`; de track leidt `queued`, niet `done`/`failed`; open blijven P09 en P21 — zie §2 | 0 | **met** |
| 3 | De herindeling van `retire-redundant-architecture` (OI-2325) is met deze commando's uitgevoerd en gemeten | 16 + 1 × `vnx deliverable retarget`, 2 × `vnx deliverable withdraw`, daarna de view, de events en het grootboek | voor 26 (5 `done`, 21 `proposed`) → na 9 (5 `done`, 2 `proposed`, 2 `withdrawn`); 17 `deliverable_retargeted` + 2 `deliverable_withdrawn`; 17 + 2 `state_mutation`-regels in `t0_receipts.ndjson`, elk met een reden — zie §3 | 0 | **met** |

Geen van de drie punten is `partly met` of `not met`; §1–§3 geven per punt het bewijs, §4 de aanbeveling voor P09 en P21, §5 de twee open items.

## 1. Punt 1 — de twee verben op main, met een rode run op de oude kop

**Vandaag, op deze branch.** Eén proces, één suite:

```
python3 -m pytest tests/test_deliverable_retarget.py tests/test_deliverable_withdraw.py \
    tests/test_migration_0036_deliverable_withdrawn.py -q -p no:cacheprovider \
    --junitxml=$VNX_DATA_DIR/unified_reports/dlv-d609b5af323b.junit.xml \
    -o junit_suite_name=dlv-d609b5af323b
→ 137 passed in 4.11s, exit 0 (junit: tests=137, failures=0, errors=0, skipped=0)
```

**De rode run van #2241** (`dlv-ef18a14528ac*.md`, PR-1, branch `dispatch/dlv-ef18a14528ac`):

| Ronde | Kop van de rode run | Commando | Uitkomst |
|---|---|---|---|
| 1 | `1ce78b68` (main vóór de PR) | `pytest tests/test_deliverable_retarget.py -q` | **41 failed, 0 passed**; 37 op `invalid choice: 'retarget'` (argparse exit 2), 4 op de ontbrekende `_retarget_check` |
| 2 | `a86defd7` (ronde-1-kop, vóór de ledgerregel) | `pytest tests/test_deliverable_retarget.py -q` | **15 failed, 38 passed**; de eerste nieuwe test faalt op `assert len(lines) == 1` met `AssertionError: []` (geen ledgerregel) |
| 3 | `5bb1eb5c` (na de merge van main, vóór de allowlist-fix) | `pytest` op de receipt-guard | **1 failed** — `test_receipt_writer_allowlist_enforced`, `missing=[] extra=['planning_cli.py']`; reproduceert CI-run 40174728232 |

De kop die mergeerde is `df767412` (PR #2241, merge `be4860d2`, 2026-10-27T07:13:20Z). De drie rode runs zijn de before-state van de drie rondes; de repo bevat alle drie de commits (`git rev-parse --verify <sha>^{commit}` slaagt voor elk).

**De rode run van #2255** (`dlv-c61cf36113cc*.md`, PR-2, branch `dispatch/dlv-c61cf36113cc`):

| Ronde | Kop van de rode run | Commando | Uitkomst |
|---|---|---|---|
| 1 | `be4860d2` (de merge-kop van PR-1) | `pytest tests/test_deliverable_withdraw.py` + `tests/test_migration_0036_deliverable_withdrawn.py` | **43 failed, 6 passed** op de withdraw-tests; de migratietest geeft een **collection error** (geen `apply_0036` op die kop). Getoond: `assert 'queued' == 'done'`, `assert 'proposed' == 'withdrawn'`, `assert 2 == 1` (de ingetrokken rij was een stub en werd afgeboekt) |
| 2 | `b3fce3e6` (ronde-1-kop, vóór de ledger-first downweg en de track-swap) | `pytest tests/test_deliverable_withdraw.py tests/test_migration_0036_deliverable_withdrawn.py -q` | **15 failed, 68 passed**; alle 15 zijn nieuwe tests, o.a. `test_down_books_one_intent_line_naming_both_rows_before_they_change` met `assert [] == [('migration_0036_revert', …)]` (de oude revert boekt geen receipt) |

De kop die mergeerde is `8f0e69af` (PR #2255, merge `8e2f6c6f`, 2026-10-27T10:49:31Z).

**Verdict: met.** Beide verben staan op main; elke PR heeft een rode run op een benoemde oude kop, en de suite is vandaag groen.

## 2. Punt 2 — een ingetrokken levering telt niet mee en houdt de track niet open

**De view, op de levende store.** `deliverables` voor `retire-redundant-architecture` heeft 9 rijen:

| `derived_status` | aantal | rijen |
|---|---|---|
| `done` | 5 | `doc:dlv-a57d7f6f9940`, `doc:dlv-dc316f60611b`, `pr:dlv-053278c5f8c3`, `pr:dlv-410316123422`, `pr:dlv-608186504eea` |
| `proposed` | 2 | `pr:dlv-9f0a3b126914` (P21), `pr:dlv-c3ab567a26b0` (P09) |
| `withdrawn` | 2 | `pr:dlv-6761827dd422` (P22), `pr:dlv-6c6a4b6e6eca` (P23) |

De twee ingetrokken rijen dragen `completed_count` 0, `in_flight_count` 0, `planned_count` 0, `withdrawn_count` 1 en `derived_status` `withdrawn` (`_DELIVERABLES_VIEW_V36`, `scripts/lib/schema_manifest.py:345-381`). `withdrawn` heeft een eigen tak vóór de faal-tak (`:366-367`), telt in geen van de drie andere tellingen, en de faal-tak sluit `withdrawn` juist uit (`:368-372`). Ze lezen dus niet als geleverd en niet als mislukt.

**De CLI.**

```
vnx deliverable list --objective retire-redundant-architecture --json --all
→ 9 rijen: done 5, proposed 2, withdrawn 2
vnx deliverable list --objective retire-redundant-architecture --json
→ 7 rijen (de 2 withdrawn verborgen); tekstmodus zegt: "(2 withdrawn hidden — --all to show)"
vnx objective show retire-redundant-architecture
→ phase queued, derived_status queued, delivery #1905…#2143 partial, open items (none)
```

**Wat de reconciler afleidt, zonder `--apply`.**

```
VNX_RECONCILE_GIT=0 vnx objective reconcile --max-gh-calls 0 --json
→ mode check; counts tracks=359, nominated=9, confirmed=0, closed=0, delivery_hold=16
→ per_track[retire-redundant-architecture] = {
      "verdict": "delivery_hold",
      "reason": "delivery hold: 6 of 6 linked PRs not marked 'complete' (6 explicit 'partial')" }
→ derived_status na de run: queued (phase queued, drifted false)
```

De afleiding zelf laat ingetrokken rijen weg (`_compute_derived_status`, `scripts/lib/track_reconciler_status.py:97`: `… AND state IS NOT 'withdrawn'`), en `withdrawn` zit in de canonieke terminale verzameling (`scripts/lib/coordination_db.py:48`), zodat een ingetrokken rij nooit een open stub is. De track leidt `queued`, niet `done` en niet `failed`.

**Wat de track open houdt.** Op de leveringsvlakte precies twee rijen: **P09 `dlv-c3ab567a26b0`** en **P21 `dlv-9f0a3b126914`**, beide `proposed`. Op de ruwe dispatchvlakte staan daarnaast drie rijen `failed_delivery` zonder `output_ref` (`20260924-tmux-inventaris-a-lib`, `20261020-g1-0-hermeting`, `20261021-skills-consumer-fixes`); ze zijn geen levering en tellen niet in de view. De twee ingetrokken rijen staan in geen van beide groepen.

**Verdict: met.** De twee ingetrokken leveringen houden de track niet open en tellen als geen van beide; P09 en P21 zijn wat open blijft.

## 3. Punt 3 — de herindeling is uitgevoerd en meetbaar

**Voor** (store-backup vóór 0036, 10:46Z; `deliverables` op `user_version` 35):

```
retire-redundant-architecture  done      5
retire-redundant-architecture  proposed 21   → samen 26
terminal-binding-afbouwen      (geen rijen) → 0
retire-feature-plan-second-planning-layer  done 11, proposed 4 → 15
```

Dat is gelijk aan de meting die de T0 vóór de herindeling deed (`vnx deliverable list --objective retire-redundant-architecture --json`: 26, waarvan 5 `done` en 21 `proposed`).

**De uitgevoerde commando's.** Zestien `vnx deliverable retarget <id> --objective terminal-binding-afbouwen`, één `vnx deliverable retarget dlv-46f7f927d1a1 --objective retire-feature-plan-second-planning-layer`, en twee `vnx deliverable withdraw` (P22, P23), elk met een reden die OI-2325 en rapport `dlv-a57d7f6f9940` noemt. Alle negentien exit 0.

**Na** (levende store, `user_version` 36):

```
retire-redundant-architecture              done 5, proposed 2, withdrawn 2 → 9
terminal-binding-afbouwen                  proposed 16                    → 16
retire-feature-plan-second-planning-layer  done 11, proposed 5            → 16
```

De zestien op `terminal-binding-afbouwen` zijn P01–P07, P10, P13–P20 (`dlv-10ce3200707e`, `dlv-9d9f900f9b64`, `dlv-915675bef7de`, `dlv-de79e5903da2`, `dlv-729397a63308`, `dlv-d8c303d77c53`, `dlv-51efd7b8bb10`, `dlv-cac1ef8e3fd3`, `dlv-22390c03ba5a`, `dlv-696b8fc4ddce`, `dlv-fd2f251033a3`, `dlv-1cb50247f59d`, `dlv-91bf6ef1097d`, `dlv-69201f02f9c2`, `dlv-093c62dd74a9`, `dlv-fa8064ffd4cf`); `retire-feature-plan-second-planning-layer` kreeg `dlv-46f7f927d1a1`.

**Het spoor.** In `coordination_events`, voor precies deze 19 ids: **17** `deliverable_retargeted` (17 verschillende `entity_id`) en **2** `deliverable_withdrawn` (2 verschillende). Elke `deliverable_retargeted` draagt `from_track`, `to_track`, `prior_state`, `actor=operator` en een reden; elke `deliverable_withdrawn` draagt `track`, `prior_state`, `to_state=withdrawn` en een reden. In `t0_receipts.ndjson` staan voor dezelfde 19 ids **17** regels met `trigger=deliverable_retarget` en **2** met `trigger=deliverable_withdraw`, elk met een niet-lege `reason` en een `status` van de vorm `<trigger>:<uuid>` (het `retarget_id`/`withdraw_id`). Er staan twee extra `deliverable_withdraw`-regels in het grootboek (11:06Z, `dlv-f7b4e67b910e` en `dlv-b8c68b8e049e`); die horen niet bij deze 19 ids.

**Verdict: met.** De uitkomst is 26 → 9 op de bron, 16 + 16 op de doelen, en de verwachte 17 + 2 events en 17 + 2 grootboekregels, elk met een reden.

## 4. P09 en P21 — de aanbeveling per levering

C2 is #2176 (`dispatch/dlv-209d2bcb21d2`, **MERGED** 2026-10-24T22:38:04Z, merge `9362bf58`). De vraag per levering is of #2176 precies dat werk opleverde.

### P09 `dlv-c3ab567a26b0` — aanbeveling: **sluiten met #2176 als bewijs**

- De drie hookbestanden en hun test staan niet meer op main: `git cat-file -e origin/main:scripts/hooks/tmux_signal_prompt_received.sh` (en `…_session_ready.sh`, `…_stop_receipt.sh`, `tests/test_tmux_lane_signals.py`) faalt voor alle vier.
- Het retired-paths-fragment noemt precies die vier paden: `scripts/ci/retired_paths.d/dlv-209d2bcb21d2.txt`.
- `git grep tmux_signal origin/main` vindt geen levend bestand meer: alleen historie (`claudedocs/`, `docs/_archive/`, de changelog-fragmenten van #2176) en het fragment zelf.
- #2176 verwijderde 89 + 51 + 123 regels hookcode en 443 regels test, plus de dode staarten in de twee `pretooluse_block_*`-hooks en de guards in de twee `session_reconcile_*`-hooks.

#2176 leverde exact het golf-1-deel van P09. De levering kan met #2176 als bewijs dicht.

### P21 `dlv-9f0a3b126914` — aanbeveling: **sluiten met #2176 als bewijs voor de C2-docregels; de P21-rest is golf 2 en heeft nog geen levering**

Het rapport `dlv-a57d7f6f9940` noemde drie open C2-plekken: `docs/core/00_VNX_ARCHITECTURE.md:621-623`, `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md:90-96` en `scripts/panel.py:135`. #2176 raakte alle drie:

```
gh pr view 2176 --json files
→ docs/core/00_VNX_ARCHITECTURE.md                    +3/-3
→ docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md +4/-3
→ scripts/panel.py                                    +1/-1
```

Vandaag op main: de gegenereerde hooks-lijst in `00_VNX_ARCHITECTURE.md` noemt geen `tmux_signal_*` meer; `DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` zegt dat de hooks en de Stop-hook "are gone (golf 1, cluster C2)"; `panel.py` zegt "the claude lane" in plaats van "the claude/tmux lane". Geen levende doc noemt de verwijderde hooks nog.

Wat #2176 **niet** dekte, en wat het rapport expliciet golf 2 noemt: `CLAUDE.md`, `docs/operations/UNIFIED_SUPERVISOR.md`, `scripts/vnx_doctor.sh`, `vnx_init.py`, `vnx_setup.py`, de plist en de (b)-alinea van `DISPATCH_RULES.md`. Dat is de "P21-rest" en die heeft volgens hetzelfde rapport nog geen levering; hij hoort onder `terminal-binding-afbouwen`. Sluiten met #2176 dekt de gemeten C2-scope; de rest moet als golf-2-levering worden aangewezen in plaats van stil te verdwijnen.

De T0 beslist en voert de sluiting uit; dit document draait geen schrijfcommando.

## 5. De twee open items — nog open en nog waar op main

| OI | Status in de store | Nog waar op main? | Bewijs |
|---|---|---|---|
| **OI-2378** — een store van `vnx init` draagt de 0022-CHECK zonder `withdrawn`; `withdraw` weigert hem (exit 2) | `open`, severity `warn`, aangemaakt 2026-10-27T08:51:38Z | **ja** | `schemas/migrations/0022_track_layer.sql:115-121` somt `withdrawn` niet op; `_withdraw_check` weigert bij een CHECK zonder `'withdrawn'` (`scripts/planning_cli.py:4035-4036`); 0036 herbouwt `dispatches` niet (`apply_0036._migrate` maakt alleen de view). Gemeten op een verse store via `_bootstrap_runtime_dbs` in `/tmp`: `user_version` 36, de v36-view mét `withdrawn`, en de CHECK nog zonder `withdrawn` (`ADMITS_WITHDRAWN False`) |
| **OI-2379** — `dispatch_outcome_classifier` kent geen uitkomst voor een ingetrokken levering; `withdrawn` eindigt na het venster als `abandoned` | `open`, severity `info`, aangemaakt 2026-10-27T08:51:38Z | **ja** | `classify_outcome` (`scripts/lib/dispatch_outcome_classifier.py:226-278`) heeft geen `withdrawn`-tak en `_EXPIRED_STATE`/`_DEAD_LETTER_STATE` zijn `expired`/`dead_letter` (`:139-140`). Gemeten: een `withdrawn`-dispatch met 48 uur leeftijd en zonder receipt/PR/parent/branch classificeert als `abandoned`; op 1 uur als `None` (binnen het venster) |

Beide staan in `~/.vnx-data/vnx-dev/state/open_items.json` op `status: open` en zijn op main nog steeds gedrag van de code, niet alleen een notitie.

## 6. Wat dit document niet doet

- Het draait geen `retarget`, `withdraw`, `close` of `reconcile --apply` en schrijft geen store.
- Het beoordeelt #2176 niet als geheel; het meet alleen de bestanden die P09 en P21 noemen.
- Het telt de twee extra `deliverable_withdraw`-regels van 11:06Z niet mee bij deze 19 ids; ze horen bij andere leveringen.
