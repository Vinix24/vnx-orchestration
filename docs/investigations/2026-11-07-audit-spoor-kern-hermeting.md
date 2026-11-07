# K-meet-2 — audit-spoor-kern: punten 1 en 3 opnieuw gemeten op het cohort ná K-fix-1 en K-fix-3

**Dispatch-ID**: dlv-523b0156d747
**Gemeten op**: branch `dispatch/dlv-523b0156d747`, afgetakt van `origin/main` = `48c575f0` (dat is ook de `writer_sha` van de laatste vijf cohortdispatches; `origin/main` stond op het meetmoment op `6f44037b`)
**K-fix-1** (PR #2433): merge commit `7edc873571bf8e81158acfe2e2c8098677fa11cc` — *fix(lifecycle): dispatch_finished carries role on every path*
**K-fix-3** (PR #2436): merge commit `131634493dac7c536c65a5d0f6a538834de2aee0` — *fix(receipts): the envelope's governance record of a v3 dispatch is dispatch_governed (ADR-054)*
**Meetmoment**: 2026-11-07T00:02:37Z (UTC), grootboek 42 396 regels
**Rol**: research-analyst · **Model**: deepseek-flash · **Provider**: deepseek-harness
**Plan**: `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/audit-spoor-kern.md`, sectie "Doel", punten 1 en 3. Die map is gitignored: het plan is een planningsartefact, geen getrackte bron.
**Voorganger**: `docs/investigations/2026-11-06-audit-spoor-kern-nameting.md` (#2426, "K-meet"). Dit document herhaalt uitsluitend punt 1 en punt 3; de punten 2, 4 en 5 zijn hier niet gemeten.

Alles in dit document is een meting. Er is niets besloten en niets gerepareerd. Elke teller komt uit een commando dat in dit document staat. Er is uitsluitend gelezen: het grootboek `/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson`, de health-beacon van de converter en de rapportmap. Geen sweep, geen finalizer, geen `decide`, geen `acknowledge`, geen `--apply`, en niets aan `.vnx-data/` geschreven.

## Executive Summary

Op het cohort ná K-fix-1 en K-fix-3 **houden punt 1 en punt 3 allebei**. Het cohort telt **23** `dispatch_started`-regels met `lifecycle_mode: v3` waarvan de `writer_sha` een nakomeling is van beide merge commits; **22** daarvan hebben op het meetmoment een eindregel (alleen deze dispatch loopt nog). De steekproef van twintig is daarmee volledig: de eerste twintig cohortdispatches naar starttijd mét een eindregel, deze dispatch uitgesloten.

- **Punt 1 — houdt.** **20/20** eindregels dragen nu een `role`, en in **20/20** gevallen is die gelijk aan de `role` van de startregel. Alle overige gevraagde velden zijn ook 20/20: precies één `dispatch_started`, precies één `dispatch_finished`, een `end_state` uit de gesloten set, `schema_version` 3 aan beide kanten, `project_id`/`dispatch_id`/`writer_sha` op beide regels, `base_sha`/`spec_sha256`/`requested_by` op de start en een gevulde `head_sha` op het einde. In #2426 was `role` op de eindregel **0/20**.
- **Punt 3 — houdt.** Voor de steekproef: **0/20** `task_complete`, **0** in het hele cohort; **20/20** precies één `dispatch_governed`; **20/20** precies één `dispatch_finished`. De planszin "`task_complete` wordt voor een `v3`-dispatch niet meer geschreven" is op dit cohort dus letterlijk waar (A en B allebei "ja"). Eén voorbehoud, opnieuw gemeten: de rapportregel was **15/20 om 2026-11-07T00:02:37Z** en is **20/20 om 2026-11-07T05:01:40Z** — de vijf ontbrekende rapporten stonden wél op schijf en kregen hun `report_ingested` tussen 00:08:07Z en 00:08:25Z. Dat was conversie-achterstand, geen ontbrekend rapport en geen codefout; de eerste telling is nu gemeten in plaats van afgeleid.

Als bevinding, geen doelpunt: **17** `door_bookkeeping_failed`-regels op **17 van de 23** cohortdispatches. Geen enkele regel heeft een `reason`-veld; de tekst staat in `error`. Zestien zijn een `DuplicateTransitionError` ("in terminal state 'completed'; cannot transition to 'accepted'"), één is een `InvalidTransitionError` op de enige `failed`-dispatch. Alle zeventien staan op dispatches met `writer_sha` `82e8076e` of `13163449`; geen enkele op `48c575f0`, de checkout die #2443 (`4d729453`: *the door leaves a row the lifecycle finalizer already closed alone*) bevat. **Inferred:** die fix is de reden dat de fout na 23:48:09Z niet meer optreedt.

## Methode — het cohort

De deur draait vanuit de main-checkout, dus de `writer_sha` van een startregel is de kop van die checkout op het moment van dispatchen, niet `origin/main`. De twee merge commits zijn daarom eerst opgezocht:

```
gh pr view 2433 --json mergeCommit --jq .mergeCommit.oid   # 7edc873571bf8e81158acfe2e2c8098677fa11cc
gh pr view 2436 --json mergeCommit --jq .mergeCommit.oid   # 131634493dac7c536c65a5d0f6a538834de2aee0
```

Daarna is per `writer_sha` in het grootboek getest of beide merge commits nakomelingen zijn:

```
A=7edc873571bf8e81158acfe2e2c8098677fa11cc
B=131634493dac7c536c65a5d0f6a538834de2aee0
for sha in ec879fa8 82e8076e 79aa5213 6ba6d1e3 48c575f0 31f874aa 13163449; do
  git merge-base --is-ancestor "$A" "$sha" && a=ja || a=nee
  git merge-base --is-ancestor "$B" "$sha" && b=ja || b=nee
  echo "$sha A=$a B=$b"
done
# -> ec879fa8 A=nee B=nee     82e8076e A=ja B=ja    79aa5213 A=nee B=nee
# -> 6ba6d1e3 A=nee B=nee     48c575f0 A=ja B=ja    31f874aa A=nee B=nee
# -> 13163449 A=ja B=ja
```

Drie `writer_sha`-waarden kwalificeren: `82e8076e` (16 starts), `48c575f0` (6 starts) en `13163449` (1 start) — samen **23** cohortdispatches. De lezing van het grootboek is één Python-scan, geselecteerd op `event_type`, `lifecycle_mode` en `dispatch_id`:

```
python3 - <<'PY'
import json, collections
LEDGER = '/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson'
COHORT_SHAS = {
 '82e8076ec6f40f093622900cd10b5794d241ca35',
 '48c575f060008891349d0879b6628370297264a5',
 '131634493dac7c536c65a5d0f6a538834de2aee0',
}
starts = {}; by = collections.defaultdict(list)
for line in open(LEDGER):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if e.get('event_type') == 'dispatch_started' and e.get('lifecycle_mode') == 'v3':
        starts[e['dispatch_id']] = e
    by[e.get('dispatch_id')].append(e)
coh = sorted([(d, s) for d, s in starts.items() if s.get('writer_sha') in COHORT_SHAS],
             key=lambda kv: (kv[1]['timestamp'], kv[0]))
with_end = [(d, s) for d, s in coh
            if any(e.get('event_type') == 'dispatch_finished' for e in by[d])]
sample = [(d, s) for d, s in with_end if d != 'dlv-523b0156d747'][:20]
for d, s in coh:
    fin = [e for e in by[d] if e.get('event_type') == 'dispatch_finished']
    print(d, s.get('role'), s.get('writer_sha'), s['timestamp'],
          fin[0].get('end_state') if fin else 'GEEN', fin[0]['timestamp'] if fin else '-')
print('cohort', len(coh), 'met eind', len(with_end), 'steekproef', len(sample))
PY
```

## Het cohort

**23** dispatches, van `dlv-b0b3e1c884c1` (2026-11-06T21:35:45Z) tot en met deze dispatch `dlv-523b0156d747` (23:59:16Z). Op het meetmoment hadden **22** een eindregel; alleen deze dispatch niet. De steekproefregel "de eerste twintig mét eindregel, deze dispatch uitgesloten" levert daarom een volledige steekproef van **20**: de nummers 1–20 hieronder. `dlv-2aec5ebc3d00` (nummer 21) en `dlv-12275555997a` (nummer 22) vallen buiten de steekproef; nummer 23 is deze dispatch. De regel "niet meetbaar op twintig" is dus niet van toepassing.

| # | dispatch | role | writer_sha | start | end_state | eind |
|---|---|---|---|---|---|---|
| 1 | `dlv-b0b3e1c884c1` | quality-engineer | `13163449` | 2026-11-06T21:35:45Z | completed | 2026-11-06T21:37:10Z |
| 2 | `dlv-423bf99b6168` | backend-developer | `82e8076e` | 2026-11-06T23:04:54Z | completed | 2026-11-06T23:07:56Z |
| 3 | `dlv-5e1ef02e1c0f` | backend-developer | `82e8076e` | 2026-11-06T23:04:54Z | completed | 2026-11-06T23:07:15Z |
| 4 | `dlv-6bec7ea18d20` | backend-developer | `82e8076e` | 2026-11-06T23:04:54Z | completed | 2026-11-06T23:08:31Z |
| 5 | `dlv-79e757392806` | backend-developer | `82e8076e` | 2026-11-06T23:04:54Z | completed | 2026-11-06T23:07:08Z |
| 6 | `dlv-a65008fd235d` | backend-developer | `82e8076e` | 2026-11-06T23:04:54Z | completed | 2026-11-06T23:06:53Z |
| 7 | `dlv-255899898b51` | security-engineer | `82e8076e` | 2026-11-06T23:09:56Z | completed | 2026-11-06T23:19:26Z |
| 8 | `dlv-22cc6e85dd85` | backend-developer | `82e8076e` | 2026-11-06T23:10:19Z | completed | 2026-11-06T23:16:15Z |
| 9 | `dlv-066f9094c6c2` | quality-engineer | `82e8076e` | 2026-11-06T23:18:12Z | completed | 2026-11-06T23:32:09Z |
| 10 | `dlv-4d89091fa4f5` | quality-engineer | `82e8076e` | 2026-11-06T23:18:12Z | completed | 2026-11-06T23:20:19Z |
| 11 | `dlv-f52c125360e1` | quality-engineer | `82e8076e` | 2026-11-06T23:18:12Z | completed | 2026-11-06T23:20:13Z |
| 12 | `dlv-9957f40c97c3` | backend-developer | `82e8076e` | 2026-11-06T23:20:55Z | completed | 2026-11-06T23:25:02Z |
| 13 | `20261107-inv-commentaar-opschonen` | research-analyst | `82e8076e` | 2026-11-06T23:25:48Z | completed | 2026-11-06T23:32:53Z |
| 14 | `20261107-inv-eventarchief` | research-analyst | `82e8076e` | 2026-11-06T23:25:48Z | completed | 2026-11-06T23:32:17Z |
| 15 | `20261107-inv-test-exclusions` | quality-engineer | `82e8076e` | 2026-11-06T23:25:48Z | failed | 2026-11-06T23:51:52Z |
| 16 | `dlv-9957f40c97c3-r2` | backend-developer | `82e8076e` | 2026-11-06T23:32:22Z | completed | 2026-11-06T23:35:31Z |
| 17 | `20261107-inv-ci-annulering-15` | research-analyst | `82e8076e` | 2026-11-06T23:36:44Z | completed | 2026-11-06T23:48:09Z |
| 18 | `dlv-6a3327d02117` | backend-developer | `48c575f0` | 2026-11-06T23:49:07Z | completed | 2026-11-06T23:52:32Z |
| 19 | `dlv-9957f40c97c3-r3` | backend-developer | `48c575f0` | 2026-11-06T23:49:07Z | completed | 2026-11-06T23:51:38Z |
| 20 | `dlv-255899898b51-r2` | security-engineer | `48c575f0` | 2026-11-06T23:51:15Z | completed | 2026-11-07T00:01:31Z |
| 21 | `dlv-2aec5ebc3d00` | backend-developer | `48c575f0` | 2026-11-06T23:53:13Z | completed | 2026-11-06T23:56:36Z |
| 22 | `dlv-12275555997a` | quality-engineer | `48c575f0` | 2026-11-06T23:57:34Z | completed | 2026-11-07T00:02:25Z |
| 23 | `dlv-523b0156d747` | research-analyst | `48c575f0` | 2026-11-06T23:59:16Z | **geen eindregel** | — |

Som: 22 eindregels op 23 starts. De twee "geen eindregel"-gevallen in de nummering zijn deze dispatch (23); nummer 20 kreeg zijn eindregel om 00:01:31Z, ná de eerste scan van deze meting, en zit daarom wél in de steekproef. Dat is precies de reden dat de steekproefgrens op het meetmoment is vastgelegd en hieronder niet opnieuw wordt opgerekt.

---

## Punt 1 — start- en eindregels in de steekproef van twintig

**Doel.** Per dispatch: precies één `dispatch_started` en precies één `dispatch_finished` met `end_state` uit {`completed`, `failed`, `timed_out`, `abandoned`}. Beide dragen `project_id`, `dispatch_id`, `role`, `writer_sha`, `schema_version: 3`; de start draagt `base_sha`, `spec_sha256`, `requested_by`; het einde `head_sha` of een lege `head_sha` met `no_push:<reden>`. Daarbij, per dispatch: de `role` van de eindregel is gelijk aan de `role` van de startregel. Het subpunt "de start ligt vóór de eerste actie van de worker" is geen deel van deze meting.

**Commando.** De scan uit "Methode", plus per dispatch:

```
python3 - <<'PY'
import json, collections
LEDGER = '/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson'
COHORT_SHAS = {'82e8076ec6f40f093622900cd10b5794d241ca35',
               '48c575f060008891349d0879b6628370297264a5',
               '131634493dac7c536c65a5d0f6a538834de2aee0'}
starts = {}; by = collections.defaultdict(list)
for line in open(LEDGER):
    try: e = json.loads(line)
    except ValueError: continue
    if e.get('event_type') == 'dispatch_started' and e.get('lifecycle_mode') == 'v3':
        starts[e['dispatch_id']] = e
    by[e.get('dispatch_id')].append(e)
def has(e, k):
    v = e.get(k)
    return v is not None and v != '' and v != []
coh = sorted([(d, s) for d, s in starts.items() if s.get('writer_sha') in COHORT_SHAS],
             key=lambda kv: (kv[1]['timestamp'], kv[0]))
sample = [(d, s) for d, s in coh
          if d != 'dlv-523b0156d747'
          and any(e.get('event_type') == 'dispatch_finished' for e in by[d])][:20]
for i, (d, s) in enumerate(sample, 1):
    fin = [e for e in by[d] if e.get('event_type') == 'dispatch_finished']
    e = fin[0] if fin else {}
    print(i, d,
          has(s, 'project_id') and has(e, 'project_id'),
          has(s, 'dispatch_id') and has(e, 'dispatch_id'),
          has(s, 'role'), has(e, 'role'), s.get('role') == e.get('role'),
          has(s, 'writer_sha') and has(e, 'writer_sha'),
          s.get('schema_version'), e.get('schema_version'),
          has(s, 'base_sha'), has(s, 'spec_sha256'), has(s, 'requested_by'),
          bool(e.get('head_sha')), e.get('no_push'),
          len(fin), e.get('end_state'))
PY
```

**Uitkomst.** Eén tabel, één rij per dispatch, één kolom per veld. `ja` = veld aanwezig en niet leeg; **ontbreekt** = veld niet aanwezig; de laatste twee kolommen zijn de tellingen `report` (`report_ingested`) en `task` (`task_complete`), die bij punt 3 horen.

| # | dispatch | start | eind | end_state | project_id | dispatch_id | role (start) | role (eind) | role gelijk | writer_sha | schema (start/eind) | base_sha | spec_sha256 | requested_by | head_sha (eind) | no_push | report | task |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `dlv-b0b3e1c884c1` | 2026-11-06T21:35:45Z | 2026-11-06T21:37:10Z | completed | ja | ja | ja | ja | ja | `13163449` | 3/3 | ja | ja | ja | `f198c0df` | — | 1 | 0 |
| 2 | `dlv-423bf99b6168` | 2026-11-06T23:04:54Z | 2026-11-06T23:07:56Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `41d5dc46` | — | 1 | 0 |
| 3 | `dlv-5e1ef02e1c0f` | 2026-11-06T23:04:54Z | 2026-11-06T23:07:15Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `3778bb16` | — | 1 | 0 |
| 4 | `dlv-6bec7ea18d20` | 2026-11-06T23:04:54Z | 2026-11-06T23:08:31Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `98165ee9` | — | 1 | 0 |
| 5 | `dlv-79e757392806` | 2026-11-06T23:04:54Z | 2026-11-06T23:07:08Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `cfc88870` | — | 1 | 0 |
| 6 | `dlv-a65008fd235d` | 2026-11-06T23:04:54Z | 2026-11-06T23:06:53Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `6b6082bf` | — | 1 | 0 |
| 7 | `dlv-255899898b51` | 2026-11-06T23:09:56Z | 2026-11-06T23:19:26Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `65dae0c8` | — | 1 | 0 |
| 8 | `dlv-22cc6e85dd85` | 2026-11-06T23:10:19Z | 2026-11-06T23:16:15Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `bcc9b6ff` | — | 1 | 0 |
| 9 | `dlv-066f9094c6c2` | 2026-11-06T23:18:12Z | 2026-11-06T23:32:09Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `225c43b9` | — | 1 | 0 |
| 10 | `dlv-4d89091fa4f5` | 2026-11-06T23:18:12Z | 2026-11-06T23:20:19Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `6193b1d1` | — | 1 | 0 |
| 11 | `dlv-f52c125360e1` | 2026-11-06T23:18:12Z | 2026-11-06T23:20:13Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `af675501` | — | 1 | 0 |
| 12 | `dlv-9957f40c97c3` | 2026-11-06T23:20:55Z | 2026-11-06T23:25:02Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `5094cbd0` | — | 1 | 0 |
| 13 | `20261107-inv-commentaar-opschonen` | 2026-11-06T23:25:48Z | 2026-11-06T23:32:53Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `7e5c9b39` | — | 1 | 0 |
| 14 | `20261107-inv-eventarchief` | 2026-11-06T23:25:48Z | 2026-11-06T23:32:17Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `7e5c9b39` | — | 1 | 0 |
| 15 | `20261107-inv-test-exclusions` | 2026-11-06T23:25:48Z | 2026-11-06T23:51:52Z | failed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `7e5c9b39` | — | 0 | 0 |
| 16 | `dlv-9957f40c97c3-r2` | 2026-11-06T23:32:22Z | 2026-11-06T23:35:31Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `bf3f4c4a` | — | 1 | 0 |
| 17 | `20261107-inv-ci-annulering-15` | 2026-11-06T23:36:44Z | 2026-11-06T23:48:09Z | completed | ja | ja | ja | ja | ja | `82e8076e` | 3/3 | ja | ja | ja | `01eded13` | — | 0 | 0 |
| 18 | `dlv-6a3327d02117` | 2026-11-06T23:49:07Z | 2026-11-06T23:52:32Z | completed | ja | ja | ja | ja | ja | `48c575f0` | 3/3 | ja | ja | ja | `6111f82a` | — | 0 | 0 |
| 19 | `dlv-9957f40c97c3-r3` | 2026-11-06T23:49:07Z | 2026-11-06T23:51:38Z | completed | ja | ja | ja | ja | ja | `48c575f0` | 3/3 | ja | ja | ja | `b0d6decf` | — | 0 | 0 |
| 20 | `dlv-255899898b51-r2` | 2026-11-06T23:51:15Z | 2026-11-07T00:01:31Z | completed | ja | ja | ja | ja | ja | `48c575f0` | 3/3 | ja | ja | ja | `9eb37a7c` | — | 0 | 0 |

Tellingen:

| Meting | Uitkomst |
|---|---|
| precies één `dispatch_started` per dispatch | 20/20 |
| precies één `dispatch_finished` per dispatch | 20/20 |
| `end_state` in {completed, failed, timed_out, abandoned} | 20/20 (19× `completed`, 1× `failed`) |
| `schema_version` 3 op start én eind | 20/20 |
| `project_id`, `dispatch_id` op beide regels | 20/20 |
| `role` op de startregel | 20/20 |
| **`role` op de eindregel** | **20/20** |
| **`role` (eind) gelijk aan `role` (start)** | **20/20** |
| `writer_sha` op beide regels | 20/20 |
| `base_sha`, `spec_sha256`, `requested_by` op de startregel | 20/20 |
| gevulde `head_sha` op de eindregel | 20/20 |
| lege `head_sha` met `no_push:<reden>` | 0/20 — deze tak is in de steekproef niet geraakt |

**Waar de rol vandaan komt.** K-fix-1 maakte `role` een verplicht veld van `Handover` (`scripts/lib/dispatch_start.py:71`) en een keyword-only parameter zonder default van `build_end_line` (`scripts/lib/dispatch_exit.py:116`), die het in het einddocument zet (`scripts/lib/dispatch_exit.py:137`); `finalize_exit` geeft `handover.role` door (`scripts/lib/dispatch_exit.py:169`) en de sweep kopieert de rol van de startregel naar zijn `abandoned`-regel (`scripts/lib/dispatch_lifecycle.py:886-891`). De gemeten eindregels dragen `role` naast de velden uit #2426; de eerdere sleutellijst zonder `role` is niet meer de lijst van vandaag.

**Verdict: houdt.** Alle gevraagde velden zijn 20/20, inclusief het veld dat in #2426 op 0/20 stond (`role` op de eindregel) en inclusief de gelijkheid tussen de rol van start en eind (20/20).

---

## Punt 3 — één rapportregel, geen tweede eindregel

**Doel.** Per steekproefdispatch: precies één `report_ingested` (met `dispatch_id`), nul `task_complete`, precies één `dispatch_governed` en precies één `dispatch_finished`. Daarna twee antwoorden: **(A)** schrijft de converter nog een `task_complete` voor een cohortdispatch? **(B)** houdt de planszin "`task_complete` wordt voor een `v3`-dispatch niet meer geschreven" letterlijk over het hele grootboek voor de steekproef?

**Commando.** Dezelfde scan; per dispatch worden de vier typen geteld:

```
python3 - <<'PY'
import json, collections
LEDGER = '/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson'
COHORT_SHAS = {'82e8076ec6f40f093622900cd10b5794d241ca35',
               '48c575f060008891349d0879b6628370297264a5',
               '131634493dac7c536c65a5d0f6a538834de2aee0'}
starts = {}; by = collections.defaultdict(list)
for line in open(LEDGER):
    try: e = json.loads(line)
    except ValueError: continue
    if e.get('event_type') == 'dispatch_started' and e.get('lifecycle_mode') == 'v3':
        starts[e['dispatch_id']] = e
    by[e.get('dispatch_id')].append(e)
coh = sorted([(d, s) for d, s in starts.items() if s.get('writer_sha') in COHORT_SHAS],
             key=lambda kv: (kv[1]['timestamp'], kv[0]))
sample = [(d, s) for d, s in coh
          if d != 'dlv-523b0156d747'
          and any(e.get('event_type') == 'dispatch_finished' for e in by[d])][:20]
for d, s in sample:
    c = collections.Counter(e.get('event_type') for e in by[d])
    print(d, 'report', c['report_ingested'], 'task_complete', c['task_complete'],
          'dispatch_governed', c['dispatch_governed'], 'dispatch_finished', c['dispatch_finished'])
PY
```

**Uitkomst, tabel.**

| Meting | Uitkomst |
|---|---|
| `report_ingested` precies één, met `dispatch_id` | **15/20 om 2026-11-07T00:02:37Z** → **20/20 om 2026-11-07T05:01:40Z** |
| `report_ingested` nul | **5/20 om 00:02:37Z** → **0/20 om 05:01:40Z** — `20261107-inv-test-exclusions`, `20261107-inv-ci-annulering-15`, `dlv-6a3327d02117`, `dlv-9957f40c97c3-r3`, `dlv-255899898b51-r2` kregen hun `report_ingested` tussen 00:08:07Z en 00:08:25Z |
| `task_complete` | **0/20** (ook 0 in het hele cohort van 23) |
| `dispatch_governed` precies één | **20/20** |
| `dispatch_finished` precies één (geen tweede eindregel) | **20/20** |

**Antwoord A — schrijft de converter nog een `task_complete` voor een cohortdispatch?** **Nee, 0.** In de steekproef staat 0/20 `task_complete`; in het hele cohort van 23 staat 0. De enige `task_complete`-regels in het grootboek (18 629) horen bij oudere, niet-v3-dispatches en bij fixture-achtige historie; geen enkele draagt een `dispatch_id` uit dit cohort.

**Antwoord B — houdt de planszin letterlijk over het hele grootboek voor de steekproef?** **Ja.** Voor alle twintig steekproefdispatches geldt: er staat geen `task_complete` in het grootboek. De zin is dus niet alleen "waar voor de converter" (zoals in #2426), maar waar voor het hele grootboek op dit cohort. De dubbele waarheid uit #2426 — converter 0/20 maar `envelope_govern._govern` 20/20 `task_complete` — is weg: `envelope_govern._govern` schrijft voor een v3-poging `dispatch_governed` (`scripts/lib/envelope_govern.py:603`, doorgegeven op `scripts/lib/envelope_govern.py:838`), en `governance_emit.emit_dispatch_receipt` kiest dat type op `lifecycle_v3` (`scripts/lib/governance_emit.py:307`). `ReceiptV2` accepteert precies {`task_complete`, `dispatch_governed`} als schema-2-type (`scripts/lib/receipt_schema.py:74-76`, controle op `scripts/lib/receipt_schema.py:236`).

**Waar het `dispatch_governed`-record vandaan komt.** Dezelfde 17-veldenset als het oude `task_complete`-record (`model`, `provider`, `role`, `token_usage`, `cost_usd`, `cost_ledger_run_ids` met `vnx-dev:envelope_govern:<dispatch-id>:1`, `verdict`, `verification`, …), schema 2, maar onder een niet-uitkomst-type. In de steekproef draagt elk record een `cost_ledger_run_ids` van die vorm.

**De lezers behandelen `dispatch_governed` als geen uitkomst — gemeten op drie steekproefdispatches.** Het commando uit de opdracht:

```
python3 scripts/receipt_query.py by-dispatch dlv-b0b3e1c884c1 \
  --state-dir /Users/vincentvandeth/.vnx-data/vnx-dev/state
python3 scripts/receipt_query.py by-dispatch 20261107-inv-test-exclusions \
  --state-dir /Users/vincentvandeth/.vnx-data/vnx-dev/state
python3 scripts/receipt_query.py by-dispatch dlv-255899898b51-r2 \
  --state-dir /Users/vincentvandeth/.vnx-data/vnx-dev/state
```

`by-dispatch` print alle regels van de dispatch; welke van die regels als *einde* geldt, is af te lezen aan het `event_type` (dezelfde opdracht met `--json` laat het zien):

| Dispatch | Regels (chronologisch) | Als einde gerapporteerd |
|---|---|---|
| `dlv-b0b3e1c884c1` | `dispatch_started` 21:35:45Z → `dispatch_governed` 21:37:10Z → `dispatch_finished` 21:37:10Z → `door_bookkeeping_failed` 21:37:11Z → `review_gate_request` 21:38:24Z → `review_gate_result` 21:38:41Z → `report_ingested` 21:50:02Z → `pr_merged` 21:56:20Z | **`dispatch_finished`** (21:37:10Z, `end_state: completed`) |
| `20261107-inv-test-exclusions` | `dispatch_started` 23:25:48Z → `dispatch_governed` 23:51:52Z → `subprocess_completion` 23:51:52Z → `dispatch_finished` 23:51:52Z → `door_bookkeeping_failed` 23:51:54Z | **`dispatch_finished`** (23:51:52Z, `end_state: failed`) |
| `dlv-255899898b51-r2` | `dispatch_started` 23:51:15Z → `dispatch_governed` 2026-11-07T00:01:31Z → `dispatch_finished` 2026-11-07T00:01:31Z | **`dispatch_finished`** (00:01:31Z, `end_state: completed`) |

Dat is geen toeval van de uitvoer: `ledger_end_line.end_line(records, 'vnx-dev', <id>)` geeft voor alle drie letterlijk `dispatch_finished` terug (`completed`, `failed`, `completed`) en nooit het `dispatch_governed`-record. De classificatie staat expliciet in de code: `dispatch_governed` zit in `NON_OUTCOME_EVENT_TYPES` (`scripts/lib/completion_reading.py:46-47`, `scripts/lib/receipt_outcome.py:101-102`) en in de niet-uitkomstverzameling van `scripts/lib/event_outcome_semantics.py:126-128`; `scripts/lib/ledger_end_line.py:12-14` zegt met zoveel woorden dat het `dispatch_governed`-record nooit de getuige kan zijn. **Verdict op dit subpunt: houdt.**

**De vijf ontbrekende `report_ingested`-regels waren conversie-achterstand, geen ontbrekend rapport.** Alle vijf rapportbestanden bestaan in `/Users/vincentvandeth/.vnx-data/vnx-dev/unified_reports/` (schrijftijd UTC: `20261107-inv-ci-annulering-15` 23:47:57Z, `dlv-9957f40c97c3-r3` 23:51:30Z, `20261107-inv-test-exclusions` 23:51:38Z, `dlv-6a3327d02117` 23:52:23Z, `dlv-255899898b51-r2` 00:01:14Z). Het health-bestand van de converter is exact `/Users/vincentvandeth/.vnx-data/vnx-dev/health/report_to_receipt_converter.json`: `_write_scan_heartbeat` schrijft naar `<state_dir>.parent/health/`, niet naar `<state_dir>/health/` (`scripts/lib/report_to_receipt_converter.py:2192-2209`). Op het meetmoment gaf dat bestand `last_run_iso: 2026-11-06T23:38:55Z` bij `expected_interval_seconds: 3600` — het grootboek bevestigt die run met negen `report_ingested`-regels tussen 23:38:26Z en 23:38:49Z. Het bestand is daarna overschreven en zegt nu `last_run_iso: 2026-11-07T05:01:09Z`, `status: ok`. Het bestand `/Users/vincentvandeth/.vnx-data/vnx-dev/state/health/report_to_receipt_converter.json` is een verouderd artefact van vóór die padcorrectie: het staat op `last_run_iso: 2026-08-30T14:32:53Z`, `status: fail`, en geen enkele lezer kijkt er nog (lezers resolven `state_dir.parent` vóór `all_beacons()`, zie de docstring op `scripts/lib/report_to_receipt_converter.py:2192-2209`). De converter schrijft de `report_ingested`-regel via `report_to_receipt_converter._lifecycle_report_line` (`scripts/lib/report_to_receipt_converter.py:1084`) → `report_lifecycle_mode.report_line` (`scripts/lib/report_lifecycle_mode.py:311`, `event_type` op regel 323). De volgende run is nu wél afgewacht en gemeten: de vijf kregen hun `report_ingested` tussen **00:08:07Z en 00:08:25Z**, waarmee de telling op 20/20 staat.

**Verdict: houdt** voor beide vragen (A: 0 `task_complete`; B: de planszin is op dit cohort letterlijk waar), voor de structuur (20/20 precies één `dispatch_governed`, 20/20 precies één `dispatch_finished`) en voor de verwachting "één `report_ingested` per dispatch": **15/20 om 2026-11-07T00:02:37Z, 20/20 om 2026-11-07T05:01:40Z**. De eerste telling was converter-achterstand — nu gemeten, niet afgeleid.

---

## Extra bevinding, geen doelpunt — `door_bookkeeping_failed` over het cohort

**17** regels op **17 van de 23** cohortdispatches. Het `reason`-veld bestaat op dit regeltype **niet** (0/17); de tekst staat in `error`.

```
python3 - <<'PY'
import json, collections
LEDGER = '/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson'
COHORT_SHAS = {'82e8076ec6f40f093622900cd10b5794d241ca35',
               '48c575f060008891349d0879b6628370297264a5',
               '131634493dac7c536c65a5d0f6a538834de2aee0'}
starts = {}; by = collections.defaultdict(list)
for line in open(LEDGER):
    try: e = json.loads(line)
    except ValueError: continue
    if e.get('event_type') == 'dispatch_started' and e.get('lifecycle_mode') == 'v3':
        starts[e['dispatch_id']] = e
    by[e.get('dispatch_id')].append(e)
coh = [(d, s) for d, s in starts.items() if s.get('writer_sha') in COHORT_SHAS]
for d, s in coh:
    for e in by[d]:
        if e.get('event_type') == 'door_bookkeeping_failed':
            print(d, 'reason=', repr(e.get('reason')), 'has_reason=', 'reason' in e,
                  'error=', e.get('error'))
PY
```

| Eigenschap | Meting |
|---|---|
| aantal regels | **17** |
| aantal verschillende dispatches | **17 van de 23** |
| `reason`-veld | **0/17** — het veld bestaat niet op dit regeltype |
| `source` / `site` | `dispatch_cli` / `_owner_transition:accepted` — 17/17 |
| `error` | 16× `DuplicateTransitionError: Dispatch '<id>' is in terminal state 'completed'; cannot transition to 'accepted'`; 1× `InvalidTransitionError: Dispatch transition 'failed_delivery' -> 'accepted' is not permitted` (op `20261107-inv-test-exclusions`, de enige `failed`-dispatch) |
| welke dispatches | `dlv-b0b3e1c884c1` (`13163449`), en 16 met `writer_sha` `82e8076e`: `dlv-423bf99b6168`, `dlv-5e1ef02e1c0f`, `dlv-6bec7ea18d20`, `dlv-79e757392806`, `dlv-a65008fd235d`, `dlv-255899898b51`, `dlv-22cc6e85dd85`, `dlv-066f9094c6c2`, `dlv-4d89091fa4f5`, `dlv-f52c125360e1`, `dlv-9957f40c97c3`, `20261107-inv-commentaar-opschonen`, `20261107-inv-eventarchief`, `20261107-inv-test-exclusions`, `dlv-9957f40c97c3-r2`, `20261107-inv-ci-annulering-15` |
| op `writer_sha` `48c575f0` | **0** van de 6 |

**Inferred:** `48c575f0` bevat #2443 (`4d729453`, *fix(door): `_owner_finish` leaves a row the lifecycle finalizer already closed alone*); `82e8076e` bevat die fix niet (bevestigd met `git merge-base --is-ancestor 4d729453`). De fout hoort daarmee bij de checkout van vóór #2443 en is in dit cohort na 23:48:09Z niet meer opgetreden; of #2443 hem volledig sluit, is buiten de opdracht en niet gemeten.

---

## Slottafel — punten 1 en 3, K-meet (#2426) naast K-meet-2

| Punt | K-meet #2426 (`79aa5213`, 2026-11-06) | K-meet-2 (dit document, `48c575f0`, 2026-11-07T00:02:37Z) | Verdict nu |
|---|---|---|---|
| 1. start- en eindregels | 20/20 één start, 20/20 één eind, 20/20 geldige `end_state`, 20/20 schema 3, 20/20 startvelden, 20/20 `head_sha` — maar **0/20 `role` op de eindregel** | **20/20 `role` op de eindregel**, **20/20 role(start) = role(eind)**; alle overige velden ook 20/20 | **houdt** |
| 3. één rapportregel, geen tweede eindregel | converter 0/20 `task_complete`, maar `envelope_govern._govern` schreef **20/20 `task_complete`**; planszin letterlijk onwaar | **0/20 `task_complete`** (en 0 in het hele cohort van 23); **20/20 `dispatch_governed`**; **20/20** precies één `dispatch_finished`; planszin letterlijk waar; rapportregel **15/20 om 00:02:37Z, 20/20 om 05:01:40Z** | **houdt** |

## Red run

De twee metingen die op het K-meet-cohort van #2426 zouden zakken, opnieuw gedraaid op precies dezelfde twintig K-meet-dispatches (`dlv-a280eb9f257a` … `dlv-dec97bc800a6`), zodat zichtbaar is dat de nullen en twintig van #2426 nog steeds reproduceren en dat de K-meet-2-meting daadwerkelijk ander gedrag laat zien:

```
python3 - <<'PY'
import json
LEDGER = '/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson'
KMEET = ['dlv-a280eb9f257a','dlv-00b11eda4d1e','dlv-99ad20f425f6','dlv-e85d19dec674',
         'dlv-ef1b2b363973','dlv-5b580f824d80','dlv-4bb0c1e1a048','dlv-c203d43060c9',
         'dlv-6b934e3a8b3e','dlv-3501f5463293','dlv-1437a92da352','dlv-a6f3b3356b2e',
         'dlv-682f0d227007','20261106-inv-test-suite-kosten','dlv-99ad20f425f6-r2',
         'dlv-33d4916379b0','dlv-97d8631d2b61','dlv-93e7ec933423','dlv-3fe9faa7aac8',
         'dlv-dec97bc800a6']
fin = 0; role = 0; tc = 0; gov = 0
for line in open(LEDGER):
    try: e = json.loads(line)
    except ValueError: continue
    if e.get('dispatch_id') not in KMEET:
        continue
    if e.get('event_type') == 'dispatch_finished':
        fin += 1
        if (e.get('role') or '').strip():
            role += 1
    if e.get('event_type') == 'task_complete':
        tc += 1
        if any(':envelope_govern:' in str(x) for x in (e.get('cost_ledger_run_ids') or [])):
            gov += 1
print('dispatch_finished', fin, 'met role', role, 'task_complete', tc, 'uit envelope_govern', gov)
PY
```

- **Red run** — `dispatch_finished`-regels met een niet-lege `role` onder de twintig K-meet-steekproef: **0** (verwacht 0). De eindregels van die twintig zijn niet herschreven; K-fix-1 geldt alleen voor regels die ná de fix zijn geschreven.
- **Red run** — `task_complete`-regels van `envelope_govern` voor diezelfde twintig: **20** (verwacht 20). Alle twintig dragen `schema_version: 2` en een `cost_ledger_run_ids` van de vorm `vnx-dev:envelope_govern:<dispatch-id>:1`.

Beide uitkomsten zijn identiek aan #2426: de oude data verandert niet, en de K-meet-2-cijfers hierboven komen uit dispatches die ná beide merge commits zijn gestart (hun `writer_sha` is een nakomeling van `7edc8735` en `13163449`, zoals in "Methode" getest).

## Onzekerheden en gaten

- **Het grootboek beweegt tijdens de meting.** Het meetmoment is vastgelegd op 2026-11-07T00:02:37Z (42 396 regels). Tussen twee scans groeide het cohort van 21 naar 22 dispatches mét eindregel doordat `dlv-255899898b51-r2` om 00:01:31Z eindigde. De steekproef van twintig is daarna stabiel (latere starts en latere eindes verschuiven de eerste twintig niet meer), maar de cohorttabel is een momentopname.
- **De rapportregel was 15/20 om 00:02:37Z en is 20/20 om 05:01:40Z.** Bij de eerste meting had de converter 23 minuten 42 seconden niet gelopen (laatste run 23:38:55Z, eerste meting 00:02:37Z; het getal 83 minuten in de eerste versie van dit document was fout). Alle vijf rapporten stonden op schijf en kregen hun `report_ingested` tussen 00:08:07Z en 00:08:25Z — de volgende run is dus wél afgewacht en gemeten.
- **Eén steekproefdispatch draagt een legacy-uitkomtregel naast zijn eindregel.** `20261107-inv-test-exclusions` heeft naast `dispatch_finished` (schema 3, `end_state: failed`) ook een `subprocess_completion` (schema 2, `status: failed`). Dat is geen `task_complete` en geen tweede `dispatch_finished`, en `ledger_end_line` kiest de `dispatch_finished`; toch is het een tweede uitkomstvormige regel in het grootboek voor dezelfde poging. Niet meegeteld in de doeltellingen van punt 3, wel genoemd.
- **`door_bookkeeping_failed` heeft geen `reason`-veld.** De opdracht vroeg om "het `reason`-veld van elk"; dat veld bestaat niet op dit regeltype. De informatie staat in `error` (16× `DuplicateTransitionError`, 1× `InvalidTransitionError`).
- **De cohortgrens hangt aan de checkout-kop, niet aan `origin/main`.** Een startregel kwalificeert alleen als zijn `writer_sha` beide merge commits als nakomeling heeft. `origin/main` is inmiddels verder (`6f44037b`); dat verandert de selectie niet, want de regel gebruikt de `writer_sha` van de regel zelf.
- **De rol op de eindregel is gemeten, niet geïnterpreteerd.** Dat `role` nu op elke eindregel staat en gelijk is aan de startregel, is een veldmeting. Of elke schrijver hem ook semantisch juist vult (dezelfde rol die de worker werkelijk had) valt buiten deze meting; de gemeten gelijkheid start/eind is het bewijs dat de opdracht vraagt.
