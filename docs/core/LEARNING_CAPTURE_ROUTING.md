# Learning capture routing

Working copy of ADR-067 (`docs/governance/decisions/ADR-067-learning-capture-routing.md`). The ADR decides; this page is what you open while capturing a lesson. When the two differ, the ADR wins.

## Welke bak waarvoor

| Recordtype | Home | Sleutel | Wanneer |
|---|---|---|---|
| Open item | `state/open_items.json` | `OI-<n>` | Een probleem dat de sessie overleeft en een status nodig heeft |
| Memory-notitie | `~/.claude/projects/<slug>/memory/<naam>.md` | bestandsnaam zonder `.md` | Een duurzame les die toekomstig gedrag verandert |
| Build-log-nugget | `build-log-bucket/YYYY-MM.md` | locator-hash van de kop | Een publiceerbare sessie-observatie |
| Leerlus | `state/quality_intelligence.db` | geen | Een uit bewijs afgeleid patroon: alleen voorstel, nooit een bron |

Bij een gelijke vondst in twee bakken wint de volgorde open item, dan notitie, dan nugget. Eén record heeft precies één home. De andere bakken verwijzen alleen.

## Verwijzen

- Naar een open item: `OI-<n>`, gelezen met `OI-[1-9][0-9]*`.
- Naar een notitie: `[[naam]]`, zonder alias-deel.
- Een nugget is nooit een doel. Een nugget mag wel verwijzen.

Een verwijzing die niet oplost leest als `onbekend (<reden>)`, nooit als `open`.

## Wanneer een paar telt

De audit vergelijkt per paar twee records van verschillende bakken: `oi_mem`, `oi_nug` of `mem_nug`. Een paar telt voor een venster `(project_id, since, until)` wanneer minstens één van de twee records nieuw is in dat venster. De partner mag elke leeftijd hebben. Het venster komt uit `explicit` (de Done-meting), `chain` (de nachtelijke run) of `first24h` (de eerste run). Een meting zonder nieuwe records (`anchors_total == 0`) is `null` en geen nul.

## De drie standen van een paar

| Stand | Betekenis |
|---|---|
| `linked` | Het niet-home record draagt de sleutel van het home record. De audit ziet dat zelf. |
| `dismissed` | De operator zegt dat het geen duplicaat is, met een reden in `capture_dispositions.ndjson`. |
| `open` | Geen van beide. Alleen deze stand telt in `open_pairs`. |

## Adoptievolgorde

1. De nachtelijke run schrijft `state/capture_audit.json` en zet `duplicate_capture_open` en `capture_audit_bins` in de beacon.
2. Draai `python3 scripts/capture_audit.py report` (met `--show` om namen lokaal op te lossen in de terminal).
3. Per open paar: voeg `OI-<n>` of `[[naam]]` toe aan het niet-home record, of draai `python3 scripts/capture_audit.py dismiss --pair <id> --reason <tekst>`. Een lege reden wordt geweigerd.
4. De volgende run toont het paar als `linked` of `dismissed`.

`report` leest het geschreven `capture_audit.json` en toont per open paar de id, soort en anker. Is het bestand weg of van een ander schema, dan stopt het met exit 2. Bestaat `capture_dispositions.ndjson` maar is hij onleesbaar, dan zegt `report` dat op een eigen regel: het leesmodel telt dan niets als afgehandeld en de open paren zijn een overschatting. `--show` zet namen alleen in de terminal en kan niet samen met `--json`.

`dismiss` schrijft een regel `{pair_id, reason, at, by}` onder een bestandsslot. Hij past dezelfde regel toe als de lezer (`valid_dismissal`): een paar-id van 16 hexadecimale tekens en een niet-lege reden, tijd en `by`. Wat de lezer zou afwijzen wordt geweigerd met exit 2 en er wordt niets geschreven.

De nachtelijke beacon draait de audit na de leerlus, ook als de lus faalde. Faalt de audit zelf, dan staat de fout in `capture_audit_error`, is `duplicate_capture_open` `null` en verandert de status van de lus niet. Een `null` is geen nul.

## Bronnen en binding

De audit leest alleen via `state/capture_sources.json`, geschreven door `python3 scripts/capture_audit.py bind`. Het bevat het `project_id`, de geheugenmap (onder `~/.claude/projects/`), de bucketmap en de bucketslug. Zonder dat bestand, of bij een afwijkend `project_id`, is de bak `unbound` en is `open_pairs` `null`. Entries van een andere slug worden geteld en niet gelezen.

## De leerlus als bron

De lus schrijft voorstellen en nooit records. Standaard is hij een schaduwrun. `VNX_LEARNING_LOOP_QUEUE=1` (uit) zet voorstellen in `pending_rules.json`. `VNX_LEARNING_LOOP_PERSIST=1` (uit) laat hem patronen schrijven, elk onder een operatorbesluit en een goedkeuringsrecord. `vnx learning decide-rule` legt het besluit vast als één bundel. Goedkeuren via `ingest_approved_rules` zet een preventieregel in dispatches, en daarom gaat een les nooit rechtstreeks naar deze bak. Modi, vlaggen en de poort: [SELF_LEARNING_LOOP.md](../intelligence/SELF_LEARNING_LOOP.md).

## Waarom

- **Open item**: alleen daar staat een status die sluit, en een les zonder status leest als actueel terwijl het probleem dicht kan zijn.
- **Memory-notitie**: alleen een notitie wordt bij een volgende dispatch teruggezet, dus een les die gedrag moet veranderen hoort hier.
- **Build-log-nugget**: een observatie voor publicatie heeft geen status en geen gedrag nodig, en wint daarom de laagste voorrang bij een dubbele vondst.
- **Leerlus**: een afgeleid patroon is geen bron, en goedkeuren zou via `ingest_approved_rules` een preventieregel in dispatches zetten.
