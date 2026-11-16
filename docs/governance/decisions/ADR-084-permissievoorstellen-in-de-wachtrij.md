# ADR-084 - Permissievoorstellen in de wachtrij: amendement op ADR-072 besluit 10 en 14

**Status:** Voorgesteld 16-11-2026 (track `permissieblokkades-leren`, levering L5c, dispatch `dlv-a3ada95bb4e6-d`)
**Besloten door:** de plan-gate op plan v4 (`5ee12e546c02`, tiebreaker START, 16-11) met `bindingen-v4.md` punt 9 en 10.
**Cross-refs:** ADR-072 (besluit 9 de store, 10 de identiteit, 14 heropenen), ADR-065 (leren is voorstellen), ADR-075, ADR-082.

Dit ADR amendeert ADR-072 besluit 10 en 14 voor de drie permissiesoorten `permissie`, `bouwer_allowlist` en `bouwgewoonte`; het bestand van ADR-072 verandert niet. Elke andere `artefact_soort` houdt ADR-072 ongewijzigd.

## Context

ADR-072 besluit 14 heropent een afgewezen voorstel op elke prompt na de afwijzing. Voor een permissie is dat fout: tien nieuwe weigeringen van hetzelfde commando brengen een regel terug die Vincent al afwees. En een melding (`alleen_melding`, een harde stop) had geen plek in de store, dus een later gevonden harde stop kon een voorstel in de wachtrij niet tegenhouden.

## Besluit

1. **Identiteit.** Voor de drie soorten draagt `oorzaak` de volledige identiteit per vorm: `permission_form:<soort>:<account>:<bereik>:<sha256(vorm|origin|account|bereik|soort)[:16]>` (L5b). Een `permissie` draagt verplicht een `handelingsklasse` `K1`..`K8`; die staat op elke revisie.
2. **Heropenen alleen op een notitie of een andere klasse.** Nieuw bewijs heropent een afgewezen permissievoorstel nooit (`suppressed_rejected`). Het heropent alleen als de `handelingsklasse` afwijkt van die op de afgewezen revisie (beide bekend; `reason` noemt oud en nieuw), of via `reopen_with_note`: een notitie van de operator, nooit `system`.
3. **De melding leeft in de store.** Een `alleen_melding` is een voorstel met revisies dat `pending` begint. Het enige besluit is `acknowledged` (gezien); `approved`, `rejected` en `applied` worden geweigerd, en `acknowledged` voor elke andere soort ook. In de operator-wachtrij is het een punt om te zien, nooit om goed te keuren.
4. **Een harde stop onderdrukt duurzaam.** Een opgeslagen melding (elke status) onderdrukt elk permissievoorstel in hetzelfde project met dezelfde `oorzaak`: het wordt nooit toegevoegd, herzien of heropend (`suppressed_hard_stop`). Een bestaand `pending` of `approved` voorstel krijgt een record `suppressed` (`decided_by` `system`, `reason` de `proposal_id` van de melding) en de status `suppressed_hard_stop`; een goedkeuring is nog niet toegepast, dus intrekken is de veilige kant. `applied` blijft `applied` en wordt geteld als `applied_with_hard_stop`. De status volgt uit de records en houdt na het herschrijven van de projectie en na elke scan, ook als de gebeurtenis uit het venster van 7 dagen is.
5. **Volgorde.** `permission_proposals.persist` schrijft de meldingen vóór de voorstellen in één `reconcile`.

## Gevolgen

- Een afgewezen permissie komt alleen terug door een besluit van Vincent of een gewijzigde risicoklasse.
- Een harde stop wint altijd van een voorstel met dezelfde identiteit; terug kan alleen door de melding zelf te laten verdwijnen, en dat kan de store niet.
- `vnx learning decide-steering` kent nog geen `acknowledge`; de store-functie `decide(..., "acknowledged")` bestaat.
