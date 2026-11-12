# ADR-066 - De dream-liveness-watch: één contract `dream_liveness/1`, een nacht als eenheid van bewijs, en 30 dagen als nieuwe beleidsdrempel

**Status:** Aangenomen 10-11-2026 (T0, track `dream-liveness-watch`, rij PR-1; PR #2601 is op 10-11-2026 gemerged).
**Datum:** 2026-11-10
**Besloten door:** het plan `claudedocs/2026-11-07-naar-1-9/plannen/dream-liveness-watch.md` (revisie 2, 08-11-2026), inclusief de acht bevindingen van de plan-gate van 08-11. Het nummer 066 is door de T0 toegewezen: het plan noemt 056, maar dat is bezet (ADR-064 en ADR-065 zijn door andere tracks geclaimd).
**Cross-refs:** ADR-005 (NDJSON-grootboek vóór elke databaseschrijfactie), ADR-007 (samengestelde sleutel over `project_id`; hier niet van toepassing, zie §8), ADR-019 (auto-dream-consolidatie; hier gecorrigeerd), ADR-051 (een geplande taak volgt de pin op runtime), ADR-055 (versheid nooit uit een mtime), ADR-063 (de dagelijkse doorloopmeting: hetzelfde contract-denken voor een andere meting).

Dit ADR legt vast waartegen de rijen PR-1 tot en met PR-7 bouwen: het document `dream_liveness/1`, de betekenis van een nacht, de voortgang over zeven gesloten nachten en de leeftijd van een wachtend voorstel. Het bevat geen code; alles erin is een regel over data. Het hele leespad is **deterministisch** — NDJSON parsen, een plist lezen, `launchctl`-uitvoer lezen, een SQLite-bestand read-only openen, logregels op `cycle_id` correleren, een vast venster rekenen, een gesloten classificatietabel toepassen. Er zit in dit pad **geen model**: de enige modelaanroep in de dream-keten is de consolidatie zelf (ADR-019), en die wordt hier gelezen en geclassificeerd, niet aangeroepen. Een model zou de meting juist daar niet-reproduceerbaar maken waar reproduceerbaarheid het product is.

## Context

De dream-cyclus kan nachten stil blijven en de doctor zegt nog steeds PASS. De enige controle, `scripts/vnx_doctor.py::check_dream_cycle` (`:823-906`, geregistreerd op `:1248`), leest alleen het nieuwste event met een tijdstempel en geeft PASS bij `scheduler_disabled` (`:878-882`), PASS bij een ontbrekende events-map (`:836-838`) en PASS bij een lege map (`:864-866`). Vandaag is de uitkomst letterlijk PASS ("deliberately disabled") over honderd dagen zonder uitvoer.

De gemeten feiten op main `6cdd75531` (08-11-2026):

- **Soms draait de job helemaal niet.** `events/dream/launchd.out.log` heeft 98 `dream run:`-regels, maar op 2026-10-26, 2026-10-30 en 2026-11-06 staat er geen regel en is er ook geen eventbestand: 3 van de laatste 14 nachten zonder enige poging. `StartCalendarInterval` haalt een gemiste nacht niet in.
- **Voltooien zonder uitvoer leest als succes.** In 120 eventbestanden staan 134 events: 124 skips (`probe_not_ok` 60, `scheduler_disabled` 40, `incomplete_data` 20, `insufficient_data` 4), 7 starts, 2 voltooiingen, 1 fout. Beide voltooiingen hebben `input_count: 187` en `merged/dropped/archived/flagged = 0/0/0/0`; er is in de hele geschiedenis dus nul nachten met uitvoer.
- **De schrijver laat de velden weg die de lezer nodig heeft.** `dream_cycle_completed` (`scripts/dream/consolidator.py:686-701`) en `dream_cycle_failed` (`:654-662`) dragen geen `timestamp`; 3 van de 134 gemeten events missen het. `dream_cycle_timeout` heeft er wel een.
- **De wachtrij is 43,33 dagen oud en niemand leest hem.** `oldest_age_seconds: 3.743.426`; de leeftijd komt uit **mtime** (`scripts/dream/review_gate.py:85-95`) omdat het review-JSON geen `created_at` draagt (`consolidator.py:665-679`), en een onleesbaar reviewbestand wordt **stil overgeslagen** (`review_gate.py:113-114`) — een leesfout ziet er dus uit als een lege wachtrij.

Het gat in één zin: het alarm bestaat en wordt genegeerd omdat het niet kan zeggen *wat* er aan de hand is. Wat ontbreekt is een poging-bewijs uit log én event, een geclassificeerde reden inclusief "geen poging", een voltooiing *met uitvoer*, en een reviewleeftijd uit inhoud in plaats van mtime.

## Besluit

### 1. Eén contract, één bestand, één eigenaar

`dream_liveness/1` wordt alleen door `scripts/lib/dream_liveness.py` gebouwd en alleen door `scripts/dream_liveness_probe.py --json` geprint. De repo-doctor (`check_dream_cycle`, dezelfde naam en hetzelfde `CheckResult`-contract) en de `HealthBeacon` zijn projecties van hetzelfde document; er is geen tweede JSON-vorm. `vnx dream watch` vervalt: het zou `vnx_cli/commands/dream.py` en `vnx_cli/main.py` kosten en is pas bruikbaar na de 1.9-release.

### 2. Fail-closed op bewijs, ook als dat "onbekend" heet

Drie toestanden: `ok`, `deviation`, `unmeasured`, met de rangorde `deviation` > `unmeasured` > `ok`. `unmeasured` is **niet** passing. Een ontbrekende bron levert een **benoemde** bevinding met `measured: false` — nooit een PASS. Een event zonder `timestamp` is `undated_event` (telt niet mee voor enige leeftijd, wordt wel geteld). Een onbekende `event_type` of `reason` is een bevinding (`unknown_event_type` / `unknown_skip_reason`), nooit een pass. `reason` is de sleutel, niet `outcome`: in alle 124 gemeten skips is `outcome` afwezig.

### 3. De eenheid van bewijs is een nacht, niet een event

Zie `## nachten` en `## voortgang`. Een voltooiing telt alleen als voortgang als er uitvoer was; "completed" alleen zou de 42-daagse stilte gezond hebben gemarkeerd zodra de scheduler weer aanschiet met een lege consolidatie.

### 4. De autoritatieve scope is de geïnstalleerde plist, niet de cwd

Bronnen: `Label`, `ProgramArguments` (`--project-id`), `StandardOutPath`/`StandardErrorPath`, `StartCalendarInterval`, `WorkingDirectory` en de env-waarde `VNX_DREAM_SCHEDULER_ENABLED` uit die plist. De dataroot komt uit dat `project_id` via `resolve_central_data_dir(project_id)` — dezelfde functie die de installer gebruikt (`scripts/dream/scheduler.py:41-47`) — en nooit uit de projectmap van de aanroeper.

### 5. Read-only, met een met naam bekende schrijfoppervlakte

De leesmodule schrijft niets. De probe schrijft met `--append` twee dingen: één NDJSON-regel in `<data_root>/state/dream_liveness.ndjson` en de beacon in `<state_dir>/health/dream_liveness.json`. De doctor schrijft niets. Dat is de hele schrijfoppervlakte van deze track.

### 6. De 30-dagendrempel is nieuw beleid, en ADR-019 zet hem niet

Zie `## reviewleeftijd`. De acceptatie-politiek verandert **niet**: geen auto-apply, ooit, in deze track. Besluit 2 van de operator van 07-11-2026 is hier letterlijk van kracht: "zelfstandigheid-meten: T0 promoveert leveringen van een cleared plan; **leren en dreaming blijven voorstellen**."

### 7. Geen schemawijziging

Er is geen migratie, geen nieuwe tabel, geen prune en geen `PRAGMA`. De leesmodule opent `state/quality_intelligence.db` met een read-only URI. ADR-007 (samengestelde sleutel over `project_id`) is dus niet van toepassing; wie later een centrale tabel toevoegt, citeert ADR-007 eerst en doet dat in een eigen rij.

## schema

Het document `dream_liveness/1`, precies één vorm:

```json
{
  "schema": "dream_liveness/1",
  "project_id": "vnx-dev",
  "generated_at": "2026-11-08T10:00:00+00:00",
  "state": "ok | deviation | unmeasured",
  "sources": {"events": "ok", "plist": "ok", "log": "ok", "db": "ok", "reviews": "ok"},
  "job": {"plist_path": "...", "label": "com.vnx.auto-dream", "loaded": true, "last_exit": 0,
          "scheduled": {"hour": 3, "minute": 0, "timezone": "Europe/Amsterdam", "grace_hours": 3},
          "arm_switch": "0", "template_arm_switch": "1", "drift": "arm_switch"},
  "attempts": {"newest": {"cycle_id": "dream-20261108-020005-2b63e9ba",
                          "at": "2026-11-08T02:00:05+00:00", "local_night": "2026-11-08",
                          "log": "launchd.out.log:688", "in_window": true},
               "age_hours": 8.1, "bound_hours": 26},
  "outcome": {"event_type": "dream_cycle_skipped", "reason": "scheduler_disabled", "class": "blocked"},
  "nights": [{"night": "2026-11-07", "attempt": true, "terminal": "skipped",
              "reason": "scheduler_disabled", "class": "blocked", "progress": false}],
  "progress_window": {"nights": 7, "completed_with_output": 0, "allowed_skip_streak_max": 0,
                      "nights_without_attempt": 0, "undated_events": 3},
  "in_flight": [],
  "history": {"completed": 2, "completed_with_output": 0, "failed": 1, "timeout": 0},
  "reviews": {"available": true, "pending_count": 2, "oldest_age_seconds": 3743426,
              "created_at_source": "mtime", "unreadable_count": 0, "overdue": true},
  "findings": [{"id": "arm_switch_off", "severity": "warn", "measured": true,
                "detail": "VNX_DREAM_SCHEDULER_ENABLED=0 in de geinstalleerde plist"}]
}
```

- **`state`** is de samenvatting van de bevindingen volgens de rangorde uit §2: `deviation` bij minstens één `severity: error`, `unmeasured` als een bron ontbreekt of onleesbaar is zonder dat er een harde afwijking is, anders `ok`.
- **`sources`** heeft vijf sleutels (`events`, `plist`, `log`, `db`, `reviews`). Elke sleutel is `ok` of onbreekt met een eigen bevinding (`events_missing`, `plist_missing`/`plist_unreadable`, `log_missing`/`log_unreadable`, `db_unavailable`, `reviews_unreadable`). Een sleutel die geen `ok` is, is nooit passing.
- **`job`** komt uit de geïnstalleerde plist. `last_exit` is de exit-status uit `launchctl list`; `drift` noemt het verschil tussen de geïnstalleerde waarde en het sjabloon (`arm_switch` vandaag: sjabloon `1`, geïnstalleerd `0`), of is `null`.
- **`attempts.newest`** is de nieuwste poging volgens `## nachten`; `log` is `bestand:regel` uit `StandardOutPath`. `bound_hours` is `26`.
- **`outcome.class`** is `allowed_skip` of `blocked`; `unknown_skip_reason` is een bevinding, geen klasse.
- **`history`** telt de hele geschiedenis: voltooiingen, waarvan met uitvoer, fouten en timeouts. Het is context, geen bevinding.
- **`findings`** gebruikt een **gesloten** verzameling ids, elk met `severity`, `measured` en een `detail`: `plist_missing`, `plist_unreadable`, `job_not_loaded`, `arm_switch_off`, `template_drift`, `events_missing`, `log_missing`, `log_unreadable`, `db_unavailable`, `reviews_unreadable`, `no_attempt_26h`, `no_scheduled_attempt`, `started_without_end`, `undated_event`, `unknown_event_type`, `unknown_skip_reason`, `blocked_skip`, `allowed_skip_streak`, `no_output`, `review_overdue`, `watch_stale`. Een id buiten deze lijst is een fout in de lezer, geen nieuwe bevinding.

## nachten

Een **nacht** is de lokale datum van `StartCalendarInterval` uit de geïnstalleerde plist. Het venster is `[HH:MM, HH:MM + 3 u]` op die lokale datum, met `GRACE_HOURS = 3`. Drie uur omdat een cyclus begrensd is door `_DEFAULT_KIMI_TIMEOUT = 180` s per chunk (`scripts/dream/consolidator.py:43`) plus één promptchunk bij 187 inputs; drie uur dekt ook een slapende machine die laat ontwaakt, en DST verschuift hooguit één uur mee.

- **Een poging wordt alleen bewezen door log én event met dezelfde `cycle_id`.** De logregel is `dream run: cycle=<id> project=<pid>` (`consolidator.py:521`); het event draagt dezelfde `cycle_id`. Beide moeten er zijn, en de logregel moet uit het `StandardOutPath` van de **geïnstalleerde** plist komen. Reden: 98 logregels tegen 120 eventbestanden, en 3 nachten met geen van beide — één bron alleen is aantoonbaar onbetrouwbaar.
- **Een `started` zonder terminal event** is *in vlucht* (`in_flight`) zolang het venster open is (geen bevinding) en `started_without_end` zodra het venster gesloten is.
- **Fouten en timeouts buiten het venster** gaan naar `history` en zijn nooit een bevinding; binnen het venster zijn ze `blocked`.
- **Een event zonder `timestamp`** is `undated_event`: het telt niet mee voor enige leeftijd of poging, wordt wel geteld in `progress_window.undated_events`, en is een bevinding.
- **Review-events hebben een eigen bucket.** `dream_cycle_approved` en `dream_cycle_rejected` (`scripts/dream/review_gate.py:138-146`, `:187-196`) zijn review-events: ze verfrissen nooit een poging, nooit een leeftijd en tellen nooit als voortgang.
- **Tijd komt nooit uit een bestandsnaam.** Events zijn UTC en worden met `astimezone()` omgezet; de bestandsnaamdatum is UTC (`consolidator.py:170`), de nacht is lokaal.

## voortgang

De "reset de teller" wordt een **schuivend venster van zeven gesloten nachten**. Er is geen muteerbare teller, dus er is ook geen reset-semantiek nodig — en de watch blijft read-only.

- Een nacht is `progress` als er in het venster een `dream_cycle_completed` ligt met minstens één van `merged/dropped/archived/flagged > 0`. Gemeten staan beide historische voltooiingen op 0/0/0/0 bij `input_count: 187`; die tellen dus niet. `flagged` telt bewust mee: een flag is een menselijke actie.
- Een **toegestane skip** is maximaal twee nachten achter elkaar; een reeks van drie of meer is een bevinding (`allowed_skip_streak`).
- `progress_window` draagt `nights: 7`, `completed_with_output`, `allowed_skip_streak_max`, `nights_without_attempt` en `undated_events`. De done-meting vraagt `completed_with_output >= 5`, `allowed_skip_streak_max <= 2` en `nights_without_attempt == 0` over de laatste zeven gesloten nachten.
- **De 26-uurgrens gaat over de poging, 5-van-7 over de voortgang.** Ze blijven gescheiden: een toegestane skip schrijft zelf een event en houdt `attempts.age_hours <= 26` groen; `## voortgang` mag daar niet groener van worden. Een poging ouder dan 26 uur is `no_attempt_26h`.
- **De classificatie van skip-redenen is een gesloten verzameling; onbekend is `blocked`.**

| reden (bron) | klasse | grond |
|---|---|---|
| `insufficient_data` (`consolidator.py:286-303`) | `allowed_skip` | geen core-patronen om te consolideren; ontwerp, geen defect |
| `incomplete_data` met "stale" in detail (`:101-158`) | `allowed_skip` | de drempel is "een receipt in de laatste 48 uur" (`:102`); stale betekent letterlijk geen nieuwe invoer |
| `incomplete_data` met "absent" (`:123`) of "empty" (`:148`) | `blocked` | het grootboek is weg of bevat geen parseerbare receipt; een defect elders |
| `content_restricted_project`, `content_restricted_all_rows` (`:250-252`, `:264-269`) | `allowed_skip` | bewuste inhoudsgrens |
| `scheduler_disabled` (`:538-555`) | `blocked` | de arm-switch uit betekent geen bewijs |
| `probe_not_ok` (`:563-580`) | `blocked` | zolang OI-894 open is: verwacht, maar wel een open item |
| `provider_switched_off` (`scripts/dream/consolidator.py:304-308`, constante op `scripts/lib/provider_switch.py:409`) | `blocked` | kimi staat uit; niets consolideert |
| onbekend of leeg | `blocked` | fail-closed |

## reviewleeftijd

De leeftijd van een wachtend voorstel komt uit **inhoud**, met een benoemde bron. De ketting is: `created_at` in het review-JSON (na PR-3) → `timestamp` van het bijbehorende `dream_cycle_completed`-event → mtime. Het veld `created_at_source` zegt welke tree gold: `content`, `event` of `mtime`. ADR-055 (`:16`, `:70`) legt vast dat versheid nooit uit een mtime komt; deze ketting respecteert dat door inhoud eerst te lezen en de mtime alleen als **benoemde** laatste terugval zichtbaar te maken, zodat de meting liegt noch zwijgt.

Een onleesbaar reviewbestand wordt **geteld**, niet overgeslagen: `unreadable_count` en `unreadable_paths` (alleen namen, nooit inhoud). `pending_count == 0` geldt alleen als de map leesbaar was **én** `unreadable_count == 0`. Vandaag slaat `list_pending_reviews` een kapot bestand stil over (`scripts/dream/review_gate.py:113-114`) en komt de leeftijd uit mtime (`:85-95`).

**De 30-dagendrempel is nieuw beleid dat hier wordt ingevoerd.** `docs/governance/decisions/ADR-019-auto-dream-memory-consolidation.md` zegt op `:18` en `:28` dat in de eerste 30 dagen na aanzetten een T0-review-gate verplicht is, en op `:29` wat er daarna mag (auto-apply alleen voor exacte dedup en het archiveren van ongebruikte entries ouder dan 30 dagen). ADR-019 **legt geen maximumleeftijd** voor een wachtend voorstel vast. Wie beweert dat de 30-dagendrempel al in ADR-019 staat, leest een burn-in-venster als een deadline. Deze ADR voert de drempel daarom als nieuw beleid in, met de twee levende voorstellen als motivering: `pending_count: 2`, `oldest_age_seconds: 3.743.426` (43,33 dagen).

- `reviews.overdue` is `true` zodra `oldest_age_seconds > 2.592.000` (30 dagen) en `pending_count > 0`; de bijbehorende bevinding is `review_overdue` met `severity: warn`.
- Het is een **WARN**, geen FAIL van de hele check: een wachtrij mag er liggen, maar niet ongemerkt oud worden.
- **De acceptatie-politiek verandert niet.** Er komt geen auto-apply, ook niet na 30 dagen. De check rapporteert leeftijd en een `overdue`-vlag en muteert niets. Besluit 2 van de operator van 07-11-2026 ("leren en dreaming blijven voorstellen") blijft van kracht.

## Consequenties

- **Positief:** één contract voor zeven rijen. De invariant zit in het document (`state` en `measured`) in plaats van in een renderer, zodat een stille bron luid wordt en geen stille nul.
- **Positief:** een poging is uit twee onafhankelijke bronnen bewezen (log én event), en een voltooiing telt alleen met uitvoer. "Geladen" en "gedraaid" zijn niet langer hetzelfde.
- **Positief:** de reviewleeftijd komt uit inhoud en een onleesbaar bestand telt mee; een leesfout ziet er niet langer uit als een lege wachtrij.
- **Positief:** de 30-dagendrempel staat eindelijk op de juiste plaats, met de correctie op ADR-019 in dezelfde tekst.
- **Negatief, en aanvaard:** een legitieme "niets samen te voegen"-nacht telt niet als voortgang. Dat is de prijs van een falsifieerbare definitie; `input_count > 0` alleen zou de 42-daagse stilte hebben goedgekeurd.
- **Negatief, en aanvaard:** `scheduler_disabled` blijft `blocked` tot de operator de arm-switch aanzet (operatorstap 1). De check leidt de intentie nooit zelf af.
- **Negatief, en aanvaard:** de corpus-quarantaine (besluit 3 van 07-11) maakt de `db`-bron `unavailable`; de check meldt `db_unavailable` met `measured: false` — correct, maar permanent tot de paden meeverhuizen.
- **Negatief, en aanvaard:** de watch is zelf een job. Sterft hij, dan maakt `beacon_register.expected_component_names()` hem zichtbaar als `absent`; de bestaande zesuurlijkse `dream_cycles`-producent blijft als onafhankelijke tweede loper staan.
- **Volgorde:** niets van dit alles is actief voordat PR-2 tot en met PR-7 gemerged zijn. De operator armt dreaming, beslist de twee voorstellen en installeert `com.vnx.dream-watch`.

## Wat dit ADR niet doet

Het lost de consolidatie zelf niet op en roept geen model aan; het levert geen code en geen schemawijziging; het wijzigt niets in `hooks/`, `configs/producer_freshness.yaml` of `scripts/launchd/launchd_project_scope.py`; het voert geen auto-apply in, ook niet na 30 dagen; en het beslist niet of dreaming voor de 1.9-release aan moet — dat is een operatorbesluit, geen eigenschap van de meting. De zeven rijen van het plan bouwen tegen dit contract.
