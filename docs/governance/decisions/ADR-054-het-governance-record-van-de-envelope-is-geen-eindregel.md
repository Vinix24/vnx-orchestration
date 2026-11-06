# ADR-054 - Het governance-record van de envelope is geen eindregel

**Status:** Aangenomen 06-11-2026 (T0, track `audit-spoor-kern`, K-fix-3).
**Datum:** 2026-11-06
**Besloten door:** T0, naar aanleiding van de K-meet (`docs/investigations/2026-11-06-audit-spoor-kern-nameting.md`) en de inventaris `20261106-inv-kmeet-gaten.md` §B.
**Cross-refs:** ADR-005 (eerst het grootboek), ADR-035 (receipt-v2), ADR-038 (uitkomst-identiteit), ADR-043 (kostengrootboek), ADR-045 (drie levenscyclusregels, één eindregel per poging), ADR-048 (skill-levering).

## Context

ADR-045:31 noemt twee schrijvers van een dispatch-einde: de converter en
`scripts/report_parser.py`. De K-meet vond een derde: `envelope_govern._govern`
schreef voor **elke** v3-dispatch een `task_complete` (schema 2, 32/32 gemeten)
naast de ene `dispatch_finished`. `task_complete` is een completion-type, dus die
regel las als een tweede einde.

Die regel is niet weg te gooien. De 17 velden die alleen daar staan zijn het
enige bewijs dat de rol en de skill de prompt hebben gehaald (`role_applied`,
`role_tier`, `role_source_path`, `skill_applied`, `skill_source_path`) en de enige
kostenattributie per dispatch op de regel zelf (`cost_usd`, `token_usage`,
`duration_seconds`, `cost_source`, `cost_ledger_run_ids`, ...). Zonder die regel
verliest `scout_effectiveness` zijn enige bruikbare regel en `paid_lane_budget`
zijn terugvalroute.

## Besluit

**Voor een v3-dispatch schrijft de envelope hetzelfde record onder de nieuwe,
niet-voltooiing-`event_type` `dispatch_governed`. Voor een legacy-dispatch
verandert niets: die houdt `task_complete`. `dispatch_governed` is geen
uitkomst, geen rapport-type en geen einde.**

- De vocabulaire staat in `receipt_schema.py`: `DISPATCH_GOVERNED_EVENT_TYPE` en
  de gesloten set `RECEIPT_V2_EVENT_TYPES`. `ReceiptV2` accepteert precies
  `{task_complete, dispatch_governed}`; elk ander type werpt bij constructie.
  `schema_version` blijft 2 — het is hetzelfde record, niet een nieuw schema.
- `event_outcome_semantics`, `receipt_outcome` en `completion_reading`
  classificeren `dispatch_governed` expliciet als geen uitkomst;
  `ledger_end_line` kan het type niet als einde kiezen, want het staat niet in
  `COMPLETION_EVENT_TYPES`.
- De lane-exit leest `Handover.is_v3` en geeft dat als `lifecycle_v3` door
  `_govern` heen aan `governance_emit.emit_dispatch_receipt`. Een pad zonder
  `Handover` blijft legacy (default `False`).
- De dedupe (`_is_lane_completion_receipt`) toetst veldvorm, niet event-type,
  dus een herrun vindt het bestaande record van beide typen en schrijft geen
  tweede. De append-laag accepteert de regel; haar `status` wordt niet als
  uitkomstclaim gevalideerd (`_STATUS_BEARING_EVENT_TYPES` blijft ongewijzigd).

## Gevolgen

- Een v3-dispatch heeft weer precies één einde: zijn `dispatch_finished`. De
  lezers van de 17 velden (`scout_effectiveness.correlate_records`,
  `paid_lane_budget._spent_from_receipts`) filteren niet op event-type en zien
  het record onder beide typen. Een nieuwe lezer moet dat ook doen en mag
  `dispatch_governed` nooit als einde of als uitkomst tellen.
- Wie een `task_complete` van de envelope verwacht, moet vanaf nu
  `dispatch_governed` meelezen. De analytische lezers van de eindregel
  (`cost_tracker`, dashboard, `cqs_calculator`) boeken de v3-poging al op haar
  `dispatch_finished`; die getallen verschuiven niet.
- `schema_version` 2 kent nu twee event-typen. Een toekomstige lezer die op
  schema 2 = "altijd task_complete" vertrouwt, is fout.
- Legt een latere track de 17 velden op het `dispatch_finished` zelf, dan kan
  `dispatch_governed` vervallen; tot dan is dit de kleinste wijziging die het
  bewijs houdt en het tweede einde wegneemt.
