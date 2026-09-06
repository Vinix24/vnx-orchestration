# ADR-038 — Een uitkomstidentiteit voor beide boekingsroutes

**Status:** Accepted (dispatch 20260906-f14-uitkomstidentiteit)
**Date:** 2026-09-06
**Decided by:** T0, op basis van een meting op het centrale grootboek (`~/.vnx-data/vnx-dev/state/t0_receipts.ndjson`).
**Resolves / Cross-refs:** OI-1639-klasse dubbele boekingen (twee routes, één uitkomst). Bouwt voort op ADR-005 (NDJSON-grootboek is canoniek, correcties zijn nieuwe regels) en de `commit_sha`/`status`-uitbreiding van `IDEMPOTENCY_FIELDS` (`idempotency.py:24-57`, agent-dispatch 2026-09-04, OI tegen PR #1762's glm_gate/kimi_gate-boekingsvolgorde).

## Context

Twee boekingsroutes leveren completion-receipts voor dezelfde dispatch-uitkomst: de lane zelf (`governance_emit` / `envelope_govern` / `dispatch_govern` / `provider_dispatch`) direct na afloop, en `report_to_receipt_converter` wanneer die later een rapport in `unified_reports/` alsnog omzet. Beide roepen `append_receipt_payload` aan, en dus dezelfde idempotentie-laag (`append_receipt_internals/idempotency.py`).

De bestaande idempotentiesleutel (`IDEMPOTENCY_FIELDS`) bevat naast identiteitsvelden ook route-velden: `source`, `report_path`, `file`, `trigger`, `section`, `terminal`. Die velden verschillen tussen de twee routes VOOR DEZELFDE uitkomst per constructie — de lane stempelt `source="governance_emit"` met een leeg `report_path`, de converter stempelt `source="report_to_receipt_converter"` met het pad naar het rapport. Twee routes, identieke uitkomst, twee verschillende sleutels, twee regels in het grootboek.

De dedup zelf is bovendien een tijdvenster (`cache_window_seconds`, default 300s, `idempotency.py:216-246`): zelfs bij een toevallig gelijke sleutel ziet een herverwerking na een uur niets van een eerdere boeking.

**Meting vooraf (06-09, op het huidige grootboek):** groepering van completion-receipts op `(dispatch_id, status)` gaf 106 groepen met meer dan één distincte `source`, waarvan 104 een boeking `source=None` naast `source=subprocess` combineren. Deze meting was een proxy: hij telt alleen kruis-route-duplicaten op een grovere sleutel dan de uiteindelijke oplossing gebruikt.

**Meting na de fix (droog, tegen dezelfde ledger, zonder te schrijven — `--rebuild-outcome-index --dry-run`):** van 29166 regels dragen 27023 een `dispatch_id`. Daarvan zou de uitkomst-index **13547 regels** als duplicaat hebben geweigerd en **173** als correctie hebben geboekt. Opsplitsing van die 13547: 7696 zijn `source=pytest` (bekende testlek in het centrale grootboek, gedocumenteerd bij `payload.resolve_central_data_dir` — "the test suite's pinned appends leaked 684+ lines into the real central ledger"), 3349 zijn overige ontwikkel-/smoke-fixtures (niet-datum-voorvoegsel `dispatch_id`, bv. `DISP-PANES-CODEX-001` tientallen keren herhaald), en **2502 regels over 86 distincte, echte (datum-voorvoegsel) productie-dispatch_id's** zijn de daadwerkelijke dubbele productieboekingen die dit ADR voorkomt. De 106/104-vooraf-meting was dus een sterke onderschatting van het werkelijke aantal dubbele regels in het grootboek — hij mat alleen het geval waarin de twee kopieën een verschillende `source` droegen, niet het bredere geval van een identieke uitkomst die twee keer (ongeacht bron) geboekt is.

## Beslissing

**Elke receipt krijgt een `outcome_id`: een deterministische hash over precies de velden die de UITKOMST identificeren, nooit de velden die de boekings-ROUTE identificeren.** Een index op die identiteit dekt het hele grootboek, niet een tijdvenster.

### De twee identiteiten, expliciet gescheiden

- **`outcome_id`** — hash over `dispatch_id, event_type, status, commit_sha, gate, pr_number`. Dit IS de uitkomst: welke dispatch, welke gebeurtenis, welk resultaat, op welke commit, voor welke poort/PR.
- **correctie-sleutel** — dezelfde velden MINUS `status`: de familie van uitkomsten voor één dispatch/event/gate/pr/commit. Een nieuwe `status` binnen dezelfde familie is een correctie (ADR-005: correcties zijn nieuwe regels die de gecorrigeerde gebeurtenis bij ID noemen, nooit een in-place edit), geen duplicaat.

Bewust NIET in `outcome_id`: `source`, `report_path`, `file`, `trigger`, `section`, `timestamp`, `terminal`, `task_id`. Dat zijn precies de velden die tussen de twee boekingsroutes verschillen voor dezelfde uitkomst — ze in de identiteit opnemen zou het probleem dat dit ADR oplost reproduceren.

### Scope: alleen receipts met een echte `dispatch_id`

De index handhaaft alleen wanneer `dispatch_id` aanwezig is. Zonder die beperking zou een receipt zonder `dispatch_id` (sommige `state_mutation`/test-events) samenvallen met ELKE andere `dispatch_id`-loze receipt — alle zes velden leeg geeft dezelfde hash — en zo losstaande, legitieme gebeurtenissen stil laten verdwijnen. `outcome_id` wordt wél op elke receipt gestempeld (vereiste 1); alleen de handhaving (duplicaat/correctie-detectie) is tot dispatch-uitkomsten beperkt.

### Twee lagen, snel-naar-langzaam

1. **Het bestaande tijdvenster** (`recent_keys`, `IDEMPOTENCY_FIELDS`-sleutel) blijft de eerste, goedkope voorfilter — ongewijzigd.
2. **De duurzame index** (`state/receipt_outcome_index.json`, sibling van het grootboekbestand) is de tweede laag: hij dekt de volledige geschiedenis, niet vijf minuten. Alleen geraadpleegd wanneer de eerste laag GEEN duplicaat meldt (anders is de dure laag overbodig) én de receipt een `dispatch_id` draagt.

Structuur van de index:

```json
{
  "version": 1,
  "outcomes": {"<outcome_id>": <epoch-seconde-van-boeking>, ...},
  "correction_index": {"<correctie_sleutel>": "<laatst-geboekte-outcome_id>", ...}
}
```

`correction_index` bewaart alleen de LAATST geboekte `outcome_id` per familie — voldoende om een `supersedes`-pointer te zetten; de volledige keten van correcties is af te leiden uit het grootboek zelf (elke regel draagt zijn eigen `outcome_id` en, indien van toepassing, zijn eigen `supersedes`).

### Correcties blijven zichtbaar, nooit stil vervangen

Een receipt met dezelfde correctie-sleutel maar een andere `outcome_id` (want andere `status`) wordt **geboekt**, met een `supersedes: <vorige outcome_id>`-veld. Dit is de directe voortzetting van de OI-1639/2026-09-04-fix (status in `IDEMPOTENCY_FIELDS`): een `unavailable`-boeking mag nooit een latere echte `pass`/`fail` blokkeren. `outcome_id` maakt dat onderscheid nu ook op de lange termijn (buiten het tijdvenster) waterdicht, in plaats van alleen binnen 300 seconden.

### Rebuildbaar uit het grootboek (ADR-005)

`rebuild_outcome_index(receipts_path, write=...)` (`append_receipt_internals/outcome_identity.py`) speelt het grootboek in volgorde af en herleidt de index helemaal opnieuw — een projectie, nooit een tweede bron van waarheid, conform ADR-005's regel dat een SQLite/JSON-projectie altijd uit het grootboek herbouwbaar moet zijn. Aangeroepen via `scripts/append_receipt.py --rebuild-outcome-index` (optioneel `--dry-run`: rapporteert tellingen zonder de indexfile te schrijven — dit is ook het gereedschap waarmee de "meting na de fix"-cijfers hierboven zijn genomen, droog tegen het echte grootboek).

### Fail-open, nooit fail-closed, op de index zelf

Een onleesbare of onschrijfbare index-file blokkeert de receipt-schrijving nooit. Zowel de lees- als de schrijf-kant van de index volgen dezelfde postuur als de bestaande idempotentie-cache (`_write_cache`'s bestaande "Audit #3"-commentaar): de receipt is op het moment van de indexcheck al duurzaam weggeschreven of staat op het punt dat te worden; in het slechtste geval ontstaat een zeldzame toekomstige duplicaat, nooit een verloren receipt. Een kapotte index is bovendien altijd herstelbaar via `--rebuild-outcome-index`.

### Hash-chain blijft orthogonaal

`VNX_CHAIN_RECEIPTS` blijft default uit. Met de vlag uit is het appendpad byte-gelijk aan vóór dit ADR, BEHALVE het nieuwe `outcome_id`-veld (en, waar van toepassing, `supersedes`) — die twee velden zijn de enige bewust toegevoegde velden, ongeacht de vlagstand. `compute_entry_hash` sluit alleen `prev_hash` uit van zijn hash-berekening (`ndjson_hash_chain.canonical_json`), dus de toevoeging van `outcome_id`/`supersedes` aan een regel is voor de keten-verificatie transparant: elke regel hasht over zijn eigen, volledige inhoud zoals geschreven.

## Consequenties

### Geaccepteerd

- Elke receipt draagt voortaan `outcome_id`. Dit is een additief, nooit een verwijderd of hernoemd veld (in lijn met ADR-035 §9's additive-only-discipline voor de v2-vormgeving).
- `state/receipt_outcome_index.json` is runtime state (net als `t0_receipts.ndjson` zelf) — niet gecommit, wél onderdeel van het herstel-verhaal (`--rebuild-outcome-index` na een corruptie of migratie).
- De 2502-regel/86-dispatch_id meting hierboven is de daadwerkelijke productie-impact; de 7696+3349 overige gevallen zijn test-/fixture-ruis die apart moet worden opgeschoond (zie Open Items in het dispatchrapport) — dit ADR lost de identiteits- en dedup-mechaniek op, niet de historische ledger-vervuiling.

### Afgewezen

- **`outcome_id` zonder de `dispatch_id`-scope-beperking** — afgewezen: zou `state_mutation`/test-events zonder `dispatch_id` op elkaar laten samenvallen en legitieme herhalingen stil laten verdwijnen.
- **Alleen het tijdvenster vergroten** (bv. naar 24 uur) — afgewezen: lost het kruis-route-probleem niet op (de sleutel zelf verschilt nog steeds tussen routes) en verschuift het "te laat"-probleem alleen in de tijd.
- **In-place corrigeren van de eerdere regel bij een status-wijziging** — afgewezen: in strijd met ADR-005's append-only-regel. Een correctie is een nieuwe regel met `supersedes`, nooit een edit.
- **SQLite-index in plaats van JSON** — afgewezen voor deze iteratie: het append-pad heeft nog geen SQLite-afhankelijkheid voor dit doel; JSON met atomic tmp+`os.replace` (zelfde patroon als de bestaande idempotentie-cache) is voldoende en voegt geen nieuwe storage-laag toe. Een migratie naar SQLite bij significante schaalgroei is een latere, aparte beslissing.

## See also

- ADR-005 — NDJSON-grootboek is canoniek; correcties zijn nieuwe regels, projecties zijn herbouwbaar
- ADR-023 / `VNX_CHAIN_RECEIPTS` — hash-chain, orthogonaal aan dit ADR
- `scripts/lib/append_receipt_internals/idempotency.py` — het bestaande tijdvenster (laag 1) en `IDEMPOTENCY_FIELDS`
- `scripts/lib/append_receipt_internals/outcome_identity.py` — implementatie van `outcome_id`, de correctie-sleutel, de index en `rebuild_outcome_index`
- `scripts/append_receipt.py --rebuild-outcome-index [--dry-run]` — CLI voor herbouw en droge meting
