# ADR-083 - De staart van het grootboek beslist over ketenen

**Status:** Voorgesteld 2026-11-16 (dispatch dlv-f4f302c32567, track receipt-hashketen-aan, levering H3).
**Datum:** 2026-11-16
**Besloten door:** de T0 op plan v1 van `receipt-hashketen-aan` en de bindingen van de plan-gate van 16-11-2026 (punten 4, 5 en 7).
**Amendeert:** ADR-023 (receipt-hashketen) en ADR-029 (tijdvakken en het zegel). Bestaande ADR-bestanden blijven bevroren (ADR-079).

## Context

`VNX_CHAIN_RECEIPTS` werd per proces uit `os.environ` gelezen, in het schrijfpad en in `ledger_health.py`. De receipt-processor, de deur, de merge-deur, de gate-runner en de shells van T0 en operator hebben elk een eigen omgeving. Eén proces zonder de variabele schreef een ongeketende regel in een geketend tijdvak, en vanaf die regel is het grootboek voorgoed `broken` (vooronderzoek H0, `dlv-93bbe96ec37a`). Daarnaast viel het schrijfpad na een afgescheurde staart terug op `GENESIS_HASH`, midden in een tijdvak.

## Besluit

1. **Voorrang.** Een append ketent als de laatste parseerbare regel van het grootboek een tijdvakmarker is of `prev_hash` draagt (`chaining`); anders beslist de variabele. Het zegel van ADR-029 is daarmee de schakelaar. De beslissing is één functie, `ndjson_hash_chain.append_chains`, en `ledger_health` gebruikt dezelfde.
2. **Uitzetten zet niet meer uit.** Na een zegel zet het weghalen of uitzetten van `VNX_CHAIN_RECEIPTS` het ketenen niet uit. Zonder marker of geketende staart blijft een append byte voor byte zoals voor dit ADR; de andere projecten van de vloot veranderen dus niet.
3. **De lezing.** `ndjson_hash_chain.read_ledger_tail` leest alleen vanaf het einde, onder `append_receipt.lock`, in dezelfde kritieke sectie als de schrijfactie, en vóór de sluiter en elk neveneffect. Lege regels en puin (onparseerbaar, geen object, de regel van de sluiter) worden overgeslagen tot de laatste parseerbare regel, met een budget van 64 KiB (`_TORN_TAIL_WINDOW`). Eén lange regel wordt heel gelezen tot zijn begin, zoals de sluiter doet: een record van 895 kB is geen puin. De nieuwe regel linkt aan de hash van die laatste parseerbare regel; `GENESIS_HASH` alleen voor een leeg grootboek (geen parseerbare regel, tot het begin gelezen).
4. **Dubbelzinnig weigert.** Is het budget op zonder parseerbare regel en zonder het begin van het bestand te halen (`ambiguous`), dan weigert de append met `receipt_tail_ambiguous`, met het grootboek en de reden in de melding. Geen gok op ongeketend, geen `GENESIS_HASH`, en het grootboek blijft byte voor byte gelijk.
5. **Geen terugweg in de code.** Terug naar ongeketend schrijven op een gezegeld grootboek kan alleen met een besluit buiten de code. Een nieuw tijdvak (`force_new_epoch`) zet het ketenen niet uit.
6. **Herstel is geen reparatie.** Een nieuw tijdvak hervat het ketenen, maar herstelt geen vastgelegde overtreding: het grootboek blijft `broken` voor die regel. Het herstelpad is een OI met de regel en de oorzaak, een nieuw tijdvak via operating, en een besluit van Vincent over hoe `verify` met die overtreding omgaat. Het puin van een gescheurde staart blijft zelf een overtreding in `verify_chain`.

## Gevolgen

- Een proces met code van vóór dit ADR schrijft na het zegel nog steeds ongeketend. Operatorstap 2 van het plan (elke schrijver op een release met dit ADR, vóór het zegel) blijft daarom nodig.
- Elke append leest de staart, ook met de variabele uit: één `pread` van hooguit 64 KiB bij een normale staart.
- Een geweigerde receipt is een uitzondering voor de aanroeper. Wat elke aanroeper ermee doet, staat in het rapport van dlv-f4f302c32567; een aanroeper die hem stil verliest, is daar een open punt.
- H2 hergebruikt `read_ledger_tail` voor de weigering van de vijf schrijvers die niet ketenen.
