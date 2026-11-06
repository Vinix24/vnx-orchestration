# Nameting — audit-spoor-kern: de vijf doelen gemeten op het cohort vanaf de activeringscommit

**Dispatch-ID**: dlv-656ec4f0ea1e
**Gemeten op**: `origin/main` = `79aa5213` (2026-11-06), branch `dispatch/dlv-656ec4f0ea1e`
**Activeringscommit (cohortgrens)**: `a7a2d7c4` — *feat(kact): the dispatch ledger lifecycle is on by default (replaces #2386)* (#2399), commit-tijd 2026-11-06T17:08:00Z
**Meetmoment**: 2026-11-06T19:52Z–19:56Z (UTC)
**Rol**: research-analyst · **Model**: deepseek-flash · **Provider**: deepseek-harness
**Plan**: `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/audit-spoor-kern.md`, sectie "Doel", punten 1–5, en de K-meet-regel onder "Leveringen". Die map is gitignored: het plan is een planningsartefact, geen getrackte bron.

Alles in dit document is een meting. Er is niets besloten en niets gerepareerd. Elke teller hieronder komt uit een commando dat in dit document staat, zodat de T0 het opnieuw kan draaien. Er is uitsluitend gelezen: het grootboek, de event-archieven, de database, de mappen en de health-beacons. Geen sweep, geen finalizer, geen `decide`, geen `acknowledge`, geen `--apply`. De enige schrijfactie is dit document.

## Executive Summary

Van de vijf doelen **houden er drie** (punt 2, 4 en 5), **houdt punt 1 niet** op één veld en **houdt punt 3 niet** zoals het plan het letterlijk opschrijft. Het cohort telt **30** `dispatch_started`-regels met `lifecycle_mode: v3` vanaf de activeringscommit (stand 19:51:42Z, inclusief deze dispatch); 29 daarvan hebben een eindregel, 1 niet (deze dispatch, nog lopend).

- **Punt 1 — houdt niet.** De steekproef van twintig is volledig: 20/20 precies één startregel, 20/20 precies één eindregel met een geldige `end_state`, 20/20 `schema_version` 3 aan beide kanten, 20/20 de gevraagde startvelden en 20/20 een gevulde `head_sha`. Maar **20/20 eindregels dragen geen `role`**. Het doel noemt `role` expliciet als veld dat *beide* regels dragen. Dat is één ontbrekend veld op elke eindregel, geen incident.
- **Punt 2 — houdt.** `20261106-kmeet-kill` kreeg om **19:51:10Z** een eindregel `end_state: abandoned`, `reason: deadline_passed_no_liveness` — 28 seconden na het eerste sweep-moment (19:50:42Z) en ruim binnen deadline+45 minuten. `open-outcomes` noemt hem; `dead_letter/` bevat hem niet.
- **Punt 3 — houdt niet letterlijk.** De converter schreef **0** `task_complete` voor een cohortdispatch (gemeten: 0/20), maar `task_complete` wordt voor een `v3`-dispatch wél nog geschreven: **20/20** hebben er precies één, van `envelope_govern._govern` via `governance_emit.emit_dispatch_receipt`. De zin "`task_complete` wordt voor een `v3`-dispatch niet meer geschreven" is dus onwaar als algemene uitspraak; de ADR-045-zin over *de converter* is waar.
- **Punt 4 — houdt.** Alle vier lezers (`check_active_drain`, `orphan_sweep`, `crash_recovery_sweep`, `open_outcomes`) stellen "loopt dit nog" via dezelfde leeshulp `attempt_verdict` → `liveness`, dus op grootboek plus liveness. De map is alleen de kandidatenlijst.
- **Punt 5 — houdt.** De telling op het cohort geeft **0 / 0 / 0** (starts zonder einde na de termijn, eindes zonder start, dubbele eindes). De hartslag staat in `t0_index.json` onder `lifecycle`, maar dat is een oude meting (18:09:38Z, 6 221 s oud op het meetmoment) en die telt het ruime `all_v3`-cohort met 3 eindes zonder start.

Daarnaast, als bevinding en niet als doelpunt: **28** `door_bookkeeping_failed`-regels over het cohort, op 28 van de 30 cohortdispatches, alle met `source: dispatch_cli` en `site: _owner_transition:accepted`, alle met een `DuplicateTransitionError` ("in terminal state 'completed'; cannot transition to 'accepted'"). Ze treden op na een normale fire, niet na een refire — en ze dragen geen `reason`-veld; de tekst staat in `error`.

## Methode

Het cohort is bepaald zoals het plan het voorschrijft (gat 6 in revisie 2): een `dispatch_started`-regel hoort erbij als `lifecycle_mode` gelijk is aan `v3` én `git merge-base --is-ancestor a7a2d7c4 <writer_sha>` waar is in deze checkout.

```
for sha in ec879fa877d79a54b4c5286a98106c17957100d6 \
           31f874aafc73cadb9293c188c384e3acf754729f \
           6ba6d1e3169be34173eb120990334142b70eddcd; do
  git merge-base --is-ancestor a7a2d7c4 "$sha" && echo "$sha ja" || echo "$sha nee"
done
```

Alle drie de `writer_sha`-waarden in het grootboek geven `ja`. Er zijn dus geen regels met een onbekende herkomst in dit cohort.

De lezing van het grootboek gebeurt met één Python-scan over `/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson` (42 061 regels op het meetmoment), geselecteerd op `event_type` en `dispatch_id`. De event-archieven worden geopend via de helpers uit `scripts/lib/event_store.py` (`find_archive_file`, `open_archive_text`), nooit met een `*.ndjson`-glob.

```
python3 - <<'PY'
import json, sys
sys.path.insert(0, 'scripts/lib')
from event_store import find_archive_file, open_archive_text
LEDGER = '/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson'
starts, ends, reps, tcs = {}, {}, {}, {}
for line in open(LEDGER):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    et, did = e.get('event_type'), e.get('dispatch_id')
    if et == 'dispatch_started' and e.get('lifecycle_mode') == 'v3':
        starts[did] = e
    elif et == 'dispatch_finished':
        ends[did] = e
    elif et == 'report_ingested':
        reps[did] = e
    elif et == 'task_complete':
        tcs[did] = e
order = sorted(starts.items(), key=lambda kv: kv[1]['timestamp'])
for did, s in order:
    e = ends.get(did)
    print(did, s['timestamp'], e.get('end_state') if e else '-',
          e['timestamp'] if e else '-', len([1 for x in (reps,) if did in x]),
          len([1 for x in (tcs,) if did in x]))
PY
```

## Het cohort

**30** dispatches, van `dlv-a280eb9f257a` (2026-11-06T17:53:42Z) tot en met deze dispatch `dlv-656ec4f0ea1e` (19:51:42Z). Dat reproduceert de tussenstand van de T0: **23** om 19:16:30Z, en **24** vanaf 19:18:07Z (de 24e is `dlv-e19b86a4ed31`). De T0 noemde "24 by 19:18Z"; die 24e start om 19:18:07Z, dus 24 is pas juist vanaf dat tijdstip.

| # | dispatch | role | writer_sha | start | end_state | eind |
|---|---|---|---|---|---|---|
| 1 | `dlv-a280eb9f257a` | security-engineer | `ec879fa8` | 2026-11-06T17:53:42Z | completed | 2026-11-06T17:59:40Z |
| 2 | `dlv-00b11eda4d1e` | backend-developer | `ec879fa8` | 2026-11-06T17:54:08Z | completed | 2026-11-06T17:57:16Z |
| 3 | `dlv-99ad20f425f6` | quality-engineer | `ec879fa8` | 2026-11-06T18:09:49Z | completed | 2026-11-06T18:16:46Z |
| 4 | `dlv-e85d19dec674` | backend-developer | `ec879fa8` | 2026-11-06T18:10:00Z | completed | 2026-11-06T18:14:05Z |
| 5 | `dlv-ef1b2b363973` | backend-developer | `ec879fa8` | 2026-11-06T18:10:15Z | completed | 2026-11-06T18:18:39Z |
| 6 | `dlv-5b580f824d80` | backend-developer | `ec879fa8` | 2026-11-06T18:10:35Z | completed | 2026-11-06T18:16:09Z |
| 7 | `dlv-4bb0c1e1a048` | backend-developer | `ec879fa8` | 2026-11-06T18:11:01Z | completed | 2026-11-06T18:18:04Z |
| 8 | `dlv-c203d43060c9` | backend-developer | `ec879fa8` | 2026-11-06T18:11:23Z | completed | 2026-11-06T18:17:05Z |
| 9 | `dlv-6b934e3a8b3e` | backend-developer | `ec879fa8` | 2026-11-06T18:14:49Z | completed | 2026-11-06T18:27:56Z |
| 10 | `dlv-3501f5463293` | backend-developer | `ec879fa8` | 2026-11-06T18:16:38Z | completed | 2026-11-06T18:20:03Z |
| 11 | `dlv-1437a92da352` | backend-developer | `ec879fa8` | 2026-11-06T18:17:24Z | completed | 2026-11-06T18:27:14Z |
| 12 | `dlv-a6f3b3356b2e` | backend-developer | `ec879fa8` | 2026-11-06T18:17:52Z | completed | 2026-11-06T18:25:49Z |
| 13 | `dlv-682f0d227007` | backend-developer | `ec879fa8` | 2026-11-06T18:25:23Z | completed | 2026-11-06T18:45:30Z |
| 14 | `20261106-inv-test-suite-kosten` | research-analyst | `ec879fa8` | 2026-11-06T18:25:47Z | completed | 2026-11-06T18:37:43Z |
| 15 | `dlv-99ad20f425f6-r2` | backend-developer | `ec879fa8` | 2026-11-06T18:39:10Z | completed | 2026-11-06T18:44:25Z |
| 16 | `dlv-33d4916379b0` | backend-developer | `ec879fa8` | 2026-11-06T18:42:21Z | completed | 2026-11-06T18:47:04Z |
| 17 | `dlv-97d8631d2b61` | backend-developer | `ec879fa8` | 2026-11-06T18:51:41Z | completed | 2026-11-06T18:57:31Z |
| 18 | `dlv-93e7ec933423` | backend-developer | `ec879fa8` | 2026-11-06T19:03:49Z | completed | 2026-11-06T19:15:15Z |
| 19 | `dlv-3fe9faa7aac8` | backend-developer | `ec879fa8` | 2026-11-06T19:06:28Z | completed | 2026-11-06T19:16:33Z |
| 20 | `dlv-dec97bc800a6` | backend-developer | `ec879fa8` | 2026-11-06T19:08:26Z | completed | 2026-11-06T19:13:44Z |
| 21 | `dlv-682f0d227007-r2` | backend-developer | `ec879fa8` | 2026-11-06T19:10:28Z | completed | 2026-11-06T19:19:51Z |
| 22 | `dlv-0790fe2fc8c3` | backend-developer | `31f874aa` | 2026-11-06T19:13:47Z | completed | 2026-11-06T19:19:10Z |
| 23 | `20261106-kmeet-kill` | research-analyst | `31f874aa` | 2026-11-06T19:15:42Z | abandoned | 2026-11-06T19:51:10Z |
| 24 | `dlv-e19b86a4ed31` | backend-developer | `31f874aa` | 2026-11-06T19:18:07Z | completed | 2026-11-06T19:23:12Z |
| 25 | `dlv-682f0d227007-r3` | backend-developer | `6ba6d1e3` | 2026-11-06T19:22:52Z | completed | 2026-11-06T19:25:35Z |
| 26 | `dlv-5828ae7b505d` | backend-developer | `6ba6d1e3` | 2026-11-06T19:25:15Z | completed | 2026-11-06T19:34:53Z |
| 27 | `dlv-00ba568c901a` | backend-developer | `6ba6d1e3` | 2026-11-06T19:32:43Z | completed | 2026-11-06T19:35:27Z |
| 28 | `dlv-f655edcbb026` | backend-developer | `6ba6d1e3` | 2026-11-06T19:37:42Z | completed | 2026-11-06T19:45:28Z |
| 29 | `dlv-53d4743d1c9a` | backend-developer | `6ba6d1e3` | 2026-11-06T19:45:31Z | completed | 2026-11-06T19:49:38Z |
| 30 | `dlv-656ec4f0ea1e` | research-analyst | `6ba6d1e3` | 2026-11-06T19:51:42Z | **geen eindregel** | — |

Som: 29 eindregels op 30 starts. Het grootboek bevat in totaal 32 `dispatch_finished`-regels; de drie extra zijn de legacy-testfixtures `d-k1`, `d-k3` en `d-k3b`, die geen `v3`-startregel hebben en dus als "einde zonder start" in de ruime telling vallen (zie punt 5).

---

## Punt 1 — start- en eindregels in de steekproef van twintig

**Doel.** Per dispatch: precies één `dispatch_started` met een tijdstip vóór de eerste actie van de worker, en precies één `dispatch_finished` met `end_state` uit {`completed`, `failed`, `timed_out`, `abandoned`}. Beide dragen `project_id`, `dispatch_id`, `role`, `writer_sha`, `schema_version: 3`; de start draagt `base_sha`, `spec_sha256`, `requested_by`; het einde `head_sha` of een lege `head_sha` met `no_push:<reden>`.

**Steekproef.** De eerste twintig cohortdispatches naar starttijd mét een eindregel, `20261106-kmeet-kill` uitgesloten (dat is punt 2). Dat zijn de nummers 1–20 uit de cohorttabel; alle twintig eindigden `completed`.

**Commando.** De scan uit "Methode", plus per dispatch:

```
python3 - <<'PY'
import sys
sys.path.insert(0, 'scripts/lib')
from event_store import find_archive_file, open_archive_text
from pathlib import Path
base = Path('/Users/vincentvandeth/.vnx-data/vnx-dev/events/archive')
did = 'dlv-dec97bc800a6'          # herhaal voor elke dispatch
for t in ('T1', 'T2', 'T3'):
    p = find_archive_file(base / t, did)
    if p:
        print(p)
        with open_archive_text(p) as fh:
            for line in fh:
                import json
                r = json.loads(line)
                if r.get('dispatch_id') == did:
                    print(r['timestamp'], 'sequence', r.get('sequence'), r.get('type'))
                    break
        break
PY
```

**Uitkomst.** Eén tabel, één rij per dispatch, één kolom per veld. `ja` = veld aanwezig en niet leeg; `**ontbreekt**` = veld niet aanwezig.

| # | dispatch | start | eind | end_state | project_id | dispatch_id (start) | role (start) | role (eind) | writer_sha | schema (start/eind) | base_sha | spec_sha256 | requested_by | head_sha (eind) | no_push | report | task |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `dlv-a280eb9f257a` | 2026-11-06T17:53:42Z | 2026-11-06T17:59:40Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `8bab1fbe` | — | 1 | 1 |
| 2 | `dlv-00b11eda4d1e` | 2026-11-06T17:54:08Z | 2026-11-06T17:57:16Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `54c7d518` | — | 1 | 1 |
| 3 | `dlv-99ad20f425f6` | 2026-11-06T18:09:49Z | 2026-11-06T18:16:46Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `5ef4b956` | — | 1 | 1 |
| 4 | `dlv-e85d19dec674` | 2026-11-06T18:10:00Z | 2026-11-06T18:14:05Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `c85e554c` | — | 1 | 1 |
| 5 | `dlv-ef1b2b363973` | 2026-11-06T18:10:15Z | 2026-11-06T18:18:39Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `3fe8abc7` | — | 1 | 1 |
| 6 | `dlv-5b580f824d80` | 2026-11-06T18:10:35Z | 2026-11-06T18:16:09Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `dbdf5e8b` | — | 1 | 1 |
| 7 | `dlv-4bb0c1e1a048` | 2026-11-06T18:11:01Z | 2026-11-06T18:18:04Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `c2660dce` | — | 1 | 1 |
| 8 | `dlv-c203d43060c9` | 2026-11-06T18:11:23Z | 2026-11-06T18:17:05Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `cda4a7bd` | — | 1 | 1 |
| 9 | `dlv-6b934e3a8b3e` | 2026-11-06T18:14:49Z | 2026-11-06T18:27:56Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `5d61f9fa` | — | 1 | 1 |
| 10 | `dlv-3501f5463293` | 2026-11-06T18:16:38Z | 2026-11-06T18:20:03Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `886eb54f` | — | 1 | 1 |
| 11 | `dlv-1437a92da352` | 2026-11-06T18:17:24Z | 2026-11-06T18:27:14Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `e0aea2d0` | — | 1 | 1 |
| 12 | `dlv-a6f3b3356b2e` | 2026-11-06T18:17:52Z | 2026-11-06T18:25:49Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `329989e1` | — | 1 | 1 |
| 13 | `dlv-682f0d227007` | 2026-11-06T18:25:23Z | 2026-11-06T18:45:30Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `b59641c1` | — | 1 | 1 |
| 14 | `20261106-inv-test-suite-kosten` | 2026-11-06T18:25:47Z | 2026-11-06T18:37:43Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `103f58d5` | — | 1 | 1 |
| 15 | `dlv-99ad20f425f6-r2` | 2026-11-06T18:39:10Z | 2026-11-06T18:44:25Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `e600807d` | — | 1 | 1 |
| 16 | `dlv-33d4916379b0` | 2026-11-06T18:42:21Z | 2026-11-06T18:47:04Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `48834b89` | — | 1 | 1 |
| 17 | `dlv-97d8631d2b61` | 2026-11-06T18:51:41Z | 2026-11-06T18:57:31Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `d01639c1` | — | 1 | 1 |
| 18 | `dlv-93e7ec933423` | 2026-11-06T19:03:49Z | 2026-11-06T19:15:15Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `a9ef74dc` | — | 1 | 1 |
| 19 | `dlv-3fe9faa7aac8` | 2026-11-06T19:06:28Z | 2026-11-06T19:16:33Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `8b048a5b` | — | 1 | 1 |
| 20 | `dlv-dec97bc800a6` | 2026-11-06T19:08:26Z | 2026-11-06T19:13:44Z | completed | ja | ja | ja | **ontbreekt** | `ec879fa8` | 3/3 | ja | ja | ja | `09ba19ae` | — | 1 | 1 |

Tellingen:

| Meting | Uitkomst |
|---|---|
| precies één `dispatch_started` per dispatch | 20/20 |
| precies één `dispatch_finished` per dispatch | 20/20 |
| `end_state` in {completed, failed, timed_out, abandoned} | 20/20 (`completed`) |
| `schema_version` 3 op start én eind | 20/20 |
| `project_id`, `dispatch_id` op beide regels | 20/20 |
| `role` op de startregel | 20/20 |
| **`role` op de eindregel** | **0/20** |
| `writer_sha` op beide regels | 20/20 |
| `base_sha`, `spec_sha256`, `requested_by` op de startregel | 20/20 |
| gevulde `head_sha` op de eindregel | 20/20 |
| lege `head_sha` met `no_push:<reden>` | 0/20 — deze tak is in de steekproef niet geraakt |

De eindregel draagt wel `attempt`, `session_id`, `spawned_at`, `status`, `task_class`, `tier_to` en `outcome_id`, maar geen `role` — geen enkele schrijver zet hem erop. De sleutels van de eindregel zijn, letterlijk, `attempt, dispatch_id, end_state, event_type, head_sha, ingested_at, open_items_created, outcome_id, project_id, schema_version, session_id, spawned_at, status, task_class, tier_to, timestamp, verdict, writer_sha`.

**Start vóór de eerste actie.** De eerste *eigen* regel in het archief van elke dispatch is later dan de startregel: 20/20. De tabel hieronder geeft het tijdstip van de eerste regel met hetzelfde `dispatch_id` in het archief, en het verschil met de startregel.

| # | dispatch | eerste eigen event | Δ t.o.v. start | `sequence` van dat event |
|---|---|---|---|---|
| 1 | `dlv-a280eb9f257a` | 17:57:28.655 | +226 s | 14205 |
| 2 | `dlv-00b11eda4d1e` | 17:54:10.450 | +2 s | 1 |
| 3 | `dlv-99ad20f425f6` | 18:11:04.189 | +75 s | 8229 |
| 4 | `dlv-e85d19dec674` | 18:10:42.523 | +42 s | 3805 |
| 5 | `dlv-ef1b2b363973` | 18:16:48.107 | +393 s | 15142 |
| 6 | `dlv-5b580f824d80` | 18:14:02.637 | +207 s | 9291 |
| 7 | `dlv-4bb0c1e1a048` | 18:17:56.704 | +415 s | 6204 |
| 8 | `dlv-c203d43060c9` | 18:16:44.993 | +321 s | 10142 |
| 9 | `dlv-6b934e3a8b3e` | 18:17:58.249 | +189 s | 14458 |
| 10 | `dlv-3501f5463293` | 18:17:27.161 | +49 s | 1882 |
| 11 | `dlv-1437a92da352` | 18:22:47.938 | +323 s | 1 |
| 12 | `dlv-a6f3b3356b2e` | 18:25:32.556 | +460 s | 24882 |
| 13 | `dlv-682f0d227007` | 18:37:49.864 | +746 s | 39690 |
| 14 | `20261106-inv-test-suite-kosten` | 18:25:51.388 | +4 s | 3 |
| 15 | `dlv-99ad20f425f6-r2` | 18:41:46.509 | +156 s | 1 |
| 16 | `dlv-33d4916379b0` | 18:45:31.795 | +190 s | 9077 |
| 17 | `dlv-97d8631d2b61` | 18:51:46.010 | +5 s | 1 |
| 18 | `dlv-93e7ec933423` | 19:13:57.857 | +608 s | 11360 |
| 19 | `dlv-3fe9faa7aac8` | 19:15:16.178 | +528 s | 19026 |
| 20 | `dlv-dec97bc800a6` | 19:08:28.959 | +2 s | 1 |

**Dit is een meetbeperking, geen volledig bewijs.** Slechts **5/20** archieven beginnen bij `sequence` 1 (2, 11, 15, 17, 20); de andere 15 beginnen midden in de stroom, met een `sequence` tussen 3 en 39 690. In **8/20** gevallen hoort zelfs de *eerste regel van het archiefbestand* bij een andere dispatch dan de naam van het bestand: het archief is dan een fragment van de gedeelde live-stroom van T1 in plaats van een schone per-dispatch-stroom. Een voorbeeld: het eerste record in `archive/T1/dlv-99ad20f425f6.ndjson` draagt `dispatch_id: dlv-5b580f824d80`, `sequence: 270`. De uitspraak "de startregel ligt vóór de eerste actie" is dus bewezen tegen de eerste *bewaarde* actie (20/20), en direct zichtbaar tegen de eerste actie in de 5 archieven die bij `sequence` 1 beginnen (Δ = 2, 323, 156, 5 en 2 seconden, allemaal positief).

**Verdict: houdt niet.** Het ontbrekende `role` op de eindregel is een letterlijk gemiste eis (0/20). De rest van punt 1 houdt (20/20), met de genoemde archiefbeperking op het subpunt "vóór de eerste actie".

---

## Punt 2 — de harde kill

**Doel.** Een lopende worker hard afbreken geeft zonder handwerk `abandoned` in het grootboek, uiterlijk op deadline plus 45 minuten. `abandoned` en `timed_out` staan daarna in `open_outcomes`, niet in `dead_letter/`.

**Commando.**

```
python3 - <<'PY'
import json
for line in open('/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson'):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if e.get('dispatch_id') == '20261106-kmeet-kill':
        print(e.get('event_type'), e.get('timestamp'), e.get('end_state'),
              e.get('reason'), e.get('no_push'), e.get('writer_sha'))
PY
python3 scripts/receipt_query.py open-outcomes \
  --state-dir /Users/vincentvandeth/.vnx-data/vnx-dev/state
ls /Users/vincentvandeth/.vnx-data/vnx-dev/dispatches/dead_letter/
```

**Uitkomst.**

| Vraag | Meting |
|---|---|
| startregel | `dispatch_started` 2026-11-06T19:15:42Z, `deadline_seconds` 300, `door_pid` 20167, `writer_sha` `31f874aa` |
| rapportregel | `report_ingested` 2026-11-06T19:20:42Z met `end_pending: true` (het rapport kwam vóór de eindregel) |
| eindregel | **één** `dispatch_finished` om **2026-11-06T19:51:10Z** |
| `end_state` | `abandoned` |
| `reason` | `deadline_passed_no_liveness` |
| `no_push` | `no_push:deadline_passed_no_liveness`, `head_sha` leeg |
| `status` | `timeout` |
| `writer_sha` op de eindregel | `6ba6d1e3` |
| `actor` / `source` op de eindregel | **niet aanwezig** — de regel noemt geen actor; de enige herkomst is `writer_sha` plus `reason` |
| deadline | 19:20:42Z (start + 300 s) |
| eerst mogelijke sweep | 19:50:42Z (deadline + `ABANDON_GRACE_SECONDS` = 30 min, `scripts/lib/dispatch_lifecycle.py:533`) |
| uiterste termijn uit het doel | 20:05:42Z (deadline + 45 min) |
| geboekt | 19:51:10Z — **28 s** na het eerste sweep-moment, **30 min 28 s** na de deadline, **14 min 32 s** vóór de uiterste termijn |
| handwerk nodig | nee |
| `open-outcomes` | bevat `20261106-kmeet-kill`: `abandoned … open point: end state 'abandoned' needs a human look (a T0 decides: receipt_query.py decide)` |
| `dead_letter/` | bevat geen `20261106-kmeet-kill` (0 treffers) |
| `dispatches/active/` | leeg (0 entries) |
| `dispatches/pending/` | bevat nog `20261106-kmeet-kill/` — de instructiebundel is niet opgeruimd |

**Verdict: houdt.** De boeking kwam zonder handwerk, ruim binnen de termijn, en de lezer plaatst hem in `open_outcomes` en niet in `dead_letter/`. Twee observaties horen bij de meting: de eindregel draagt geen `actor`/`source` (ADR-045 §4 schrijft `actor` voor op de *projectie-overgangen*, niet op de grootboekregel zelf), en de bundel blijft in `pending/` staan omdat er nooit een worker is gestart die hem opruimt.

---

## Punt 3 — één rapportregel, geen tweede eindregel

**Doel.** Het rapport wordt `report_ingested` met `dispatch_id` en er komt geen tweede eindregel: "`task_complete` wordt voor een `v3`-dispatch niet meer geschreven."

**Commando.** De scan uit "Methode" telt per dispatch `report_ingested` en `task_complete`; dezelfde scan leest `schema_version`, `timestamp` en `cost_ledger_run_ids` van elke `task_complete`.

**Uitkomst, twee antwoorden.**

*Antwoord A — heeft de converter een `task_complete` voor een cohortdispatch geschreven?* **Nee, 0.** Alle twintig steekproefdispatches hebben precies één `report_ingested` (20/20, elk met `dispatch_id`) en precies één `task_complete`. Elke `task_complete` draagt `schema_version: 2` en een `cost_ledger_run_ids` van de vorm `vnx-dev:envelope_govern:<dispatch-id>:1` — dat is de envelope-schrijver, niet de converter. In 19/20 gevallen valt de `task_complete`-tijdstempel exact samen met de `dispatch_finished`-tijdstempel; in 1/20 (`dlv-c203d43060c9`) ligt de `task_complete` één seconde eerder (18:17:04Z tegen 18:17:05Z).

| Meting | Uitkomst |
|---|---|
| `report_ingested` per steekproefdispatch | 20/20 precies één |
| `task_complete` per steekproefdispatch | 20/20 precies één |
| `task_complete` met `schema_version: 2` | 20/20 |
| `task_complete` met `cost_ledger_run_ids` uit `envelope_govern` | 20/20 |
| `task_complete`-tijdstempel gelijk aan `dispatch_finished` | 19/20 |
| `task_complete` geschreven door de converter | **0/20** |
| tweede eindregel (`dispatch_finished`) per dispatch | 0/20 |

*Antwoord B — houdt de planszin "`task_complete` wordt voor een `v3`-dispatch niet meer geschreven" letterlijk?* **Nee.** In 20/20 gevallen staat er voor een `v3`-dispatch wél een `task_complete` in het grootboek, één per dispatch. De zin is alleen waar als hij over *de converter* gaat; als algemene uitspraak over het grootboek is hij onwaar.

**Waar de twee regels vandaan komen.**

| Regel | Schrijfpad | Bewijsplaats |
|---|---|---|
| `task_complete` | `envelope_govern._govern` → `governance_emit.emit_dispatch_receipt` → `ReceiptV2(...)`; `ReceiptV2.__post_init__` forceert `event_type` naar de constante `RECEIPT_V2_EVENT_TYPE` | `scripts/lib/envelope_govern.py:413` (`_govern`), aanroep op `scripts/lib/envelope_govern.py:718`; `scripts/lib/governance_emit.py:78` (`emit_dispatch_receipt`), `ReceiptV2(...)` op `scripts/lib/governance_emit.py:298`; `scripts/lib/receipt_schema.py:70` (`RECEIPT_V2_EVENT_TYPE = "task_complete"`), geforceerd op `scripts/lib/receipt_schema.py:231` |
| `report_ingested` | `report_to_receipt_converter.build_receipt_from_report` → `_lifecycle_report_line` → `report_lifecycle_mode.report_line`; die zet `event_type` op `report_ingested` zodra `decide(...).is_v3` waar is | `scripts/lib/report_to_receipt_converter.py:1608` (`build_receipt_from_report`), `scripts/lib/report_to_receipt_converter.py:1084` (`_lifecycle_report_line`), aanroep op `scripts/lib/report_to_receipt_converter.py:1097`; `scripts/lib/report_lifecycle_mode.py:311` (`report_line`), `event_type` op `scripts/lib/report_lifecycle_mode.py:323` |

De oorzaak van de dubbele waarheid is dus precies zoals ADR-045 hem benoemt: de converter is omgezet (`scripts/lib/report_to_receipt_converter.py`, rond regel 1627: "a `v3` dispatch returns one `report_ingested` instead of a `task_complete`"), maar de envelope-GOVERN-seam is dat niet. Die schrijft zijn v2-receipt ongewijzigd, en de v2-vorm is per ADR-035 §3.2.1 ("never keyed on status") altijd `task_complete`.

**Verdict: houdt niet** als letterlijke eis ("niet meer geschreven": 20/20 tegendeel). De onderliggende bedoeling — de converter schrijft geen `task_complete` meer voor `v3`, en er is precies één eindregel — **houdt**: 0/20 van de converter, 20/20 precies één eindregel, 20/20 precies één rapportregel.

---

## Punt 4 — de vier lezers van `dispatches/active/`

**Doel.** De vier lezers (`check_active_drain`, `orphan_sweep`, `crash_recovery_sweep`, `open_outcomes`) beslissen "loopt dit nog" op grootboek plus liveness, niet op de map.

**Waar de beslissing valt.** Alle vier delegeren het antwoord aan dezelfde twee functies:

- `attempt_verdict` (`scripts/lib/ledger_attempt_state.py:95`) leest het grootboek: een eigen `dispatch_finished` voor `(project_id, dispatch_id, attempt)` maakt de poging `ended`; zonder eindregel vraagt hij `liveness`;
- `liveness` (`scripts/lib/dispatch_lifecycle.py:810`) meet de occupancy-flock, `door_pid` + `door_started_at` en de `child_pid` uit het spawnbestand, met "onmeetbaar" als `unknown` en "unknown nooit dood".

Daarmee is de map alleen de kandidatenlijst; de vraag "loopt dit nog" komt uit het grootboek plus een liveness-meting.

| Lezer | Waar hij "loopt dit nog" beslist | Leest hij het grootboek plus liveness, of de map? |
|---|---|---|
| `check_active_drain` | `scripts/check_active_drain.py:147` (`_destination`) → `scripts/lib/open_outcomes.py:451` (`active_destination`) → `scripts/lib/open_outcomes.py:428` (`_verdict_destination`), tak `KIND_LIVE` op `scripts/lib/open_outcomes.py:443-444` | grootboek + liveness (via `attempt_verdict`); de map levert alleen de `DispatchEntry` |
| `orphan_sweep` | `scripts/lib/orphan_sweep.py:191` (`_DispatchVerdicts.verdict`, grootboek één keer gelezen in `__init__`) en `scripts/lib/orphan_sweep.py:658-663` (`_dispatch_known_inflight`: `ended` → False, `live`/`unknown` → True) | grootboek + liveness; de map levert de kandidaten |
| `crash_recovery_sweep` | `scripts/lib/crash_recovery_sweep.py:264` (`_ledger_verdicts`) → `attempt_verdict`, en `scripts/lib/crash_recovery_sweep.py:280` (`_skip_not_recoverable`: elk niet-`legacy` verdict wordt nooit geborgen, alleen geteld in `v3_ended`/`v3_live`/`v3_unknown`/`v3_dead_no_end`) | grootboek + liveness; de map (`dispatches/active/<id>/manifest.json`) levert de kandidaten |
| `open_outcomes` | `scripts/lib/open_outcomes.py:859` (`open_outcome_items`) → `scripts/lib/open_outcomes.py:428` (`_verdict_destination`) | grootboek + liveness (via `attempt_verdict`) |

**Gemeten voorbeelden (alleen lezen).**

```
python3 scripts/check_active_drain.py --dry-run
# -> active/ is empty — nothing to drain.        (exit 0)

python3 - <<'PY'
import sys
sys.path.insert(0, 'scripts/lib')
import orphan_sweep
r = orphan_sweep.sweep(dry_run=True).to_dict()
print(r['dry_run'], r['active_scanned'], r['active_v3'])
PY
# -> True 0 {'v3_ended': [], 'v3_live': [], 'v3_unknown': [], 'v3_dead_no_end': []}

python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, 'scripts/lib')
import crash_recovery_sweep
r = crash_recovery_sweep.sweep(Path('/Users/vincentvandeth/.vnx-data/vnx-dev'), dry_run=True)
print(r.to_dict())
PY
# -> scanned 0; v3_ended/v3_live/v3_unknown/v3_dead_no_end alle leeg; ledger_error null

python3 scripts/receipt_query.py open-outcomes \
  --state-dir /Users/vincentvandeth/.vnx-data/vnx-dev/state
# -> abandoned 20261106-kmeet-kill  [receipt_outcome] open point: end state 'abandoned' …
```

De drie mappenlezers zien nu **nul** kandidaten, omdat `dispatches/active/` leeg is; hun v3-tellers zijn daarom leeg. Dat is een eerlijke nul, geen bewijs van werking.

De beslisfunctie zelf is wel meetbaar, los van de lege map:

```
python3 - <<'PY'
import sys
sys.path.insert(0, 'scripts/lib')
from ledger_attempt_state import attempt_verdict
for did in ('20261106-kmeet-kill', 'dlv-a280eb9f257a', 'dlv-656ec4f0ea1e'):
    v = attempt_verdict('/Users/vincentvandeth/.vnx-data/vnx-dev/state', 'vnx-dev', did)
    print(did, v.kind, v.liveness, getattr(v, 'end_state', None))
PY
# -> 20261106-kmeet-kill ended None abandoned
# -> dlv-a280eb9f257a   ended None completed
# -> dlv-656ec4f0ea1e   live  live None
```

**Verdict: houdt.** Vier van vier beslissen op grootboek plus liveness. Twee kanttekeningen bij de meting: (a) de mapkant is op het meetmoment leeg, dus de lezers zijn alleen op hun code en op de losse beslisfunctie gemeten, niet op een gevulde `active/`; (b) de wrapper-scripts `scripts/orphan_sweep.py` en `scripts/crash_recovery_sweep.py` zijn in deze omgeving niet als script te draaien: hun guard `if str(_LIB) not in sys.path` slaat de `sys.path`-insert over wanneer `scripts/lib` al via `PYTHONPATH` op de zoeklijst staat, waarna `from orphan_sweep import …` het script zelf importeert en met `ImportError: cannot import name 'DEFAULT_MAX_ORPHANS' from partially initialized module` stopt. Daarom is hier de lib-functie direct met `dry_run=True` aangeroepen. Dat is een startprobleem van de wrapper, niet van de lezer.

---

## Punt 5 — de dagelijkse telling in `ledger_health`

**Doel.** De telling meldt op nieuwe data nul starts zonder einde na de termijn, nul eindes zonder start en nul dubbele eindes, met een eigen hartslag in de SessionStart-briefing.

**Commando.**

```
python3 scripts/ledger_health.py --no-write --json \
  --state-dir /Users/vincentvandeth/.vnx-data/vnx-dev/state \
  --activation-commit a7a2d7c4
python3 -c "import json; d=json.load(open('/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_index.json')); print(d['lifecycle'])"
python3 -c "import json; d=json.load(open('/Users/vincentvandeth/.vnx-data/vnx-dev/health/lifecycle_count.json')); print(d['details']['counts'], d['details']['counted_at'])"
```

`--no-write` is gebruikt zodat de meting de beacon niet ververst: de hartslag die hieronder wordt gerapporteerd is de bestaande, niet een die dit commando zelf heeft gezet.

**Uitkomst — de drie getallen.**

| Cohorterm | `start_without_end` | `end_without_start` | `double_end` |
|---|---|---|---|
| `--activation-commit a7a2d7c4` (nieuwe data) | **0** | **0** | **0** |
| geen cohortgrens (`all_v3`, zoals de beacon hem schrijft) | 0 | **3** | 0 |

De drie "eindes zonder start" in de ruime telling zijn `d-k1`, `d-k3` en `d-k3b` — legacy-testfixtures met een `dispatch_finished` en zonder `v3`-startregel. Ze vallen buiten het cohort van dit plan. De ruime telling is dus niet fout, maar hij is ook niet het getal dat punt 5 vraagt; dat is de eerste rij.

Het volledige `lifecycle_count`-object van de meting op het cohort: `status: measured`, `cohort: a7a2d7c4`, `evaluated_at: 2026-11-06T19:53:19Z`, `projection_conflict: 0`, `provenance_unknown: 0`, `total: 0`, `problems: []`. De `overall_status` van `ledger_health` is `finding` en de exitcode 1, maar dat komt van `receipt_coverage` (5 ontbrekende receipts, 22 geacknowledgeerde gaten), niet van de levenscyclus-telling; de telling verandert de exitcode nooit (ADR-045 K-9).

**Uitkomst — de hartslag in de briefing.**

`t0_index.json` bevat de sleutel `lifecycle`:

```
{"line": "lifecycle: 0 start-without-end, 3 end-without-start, 0 double, 0 conflict,
          0 provenance-unknown; sweep 147s",
 "counts": {"double_end": 0, "end_without_start": 3, "projection_conflict": 0,
            "provenance_unknown": 0, "start_without_end": 0, "total": 3},
 "sweep_heartbeat_age_seconds": 147.965,
 "counted_at": "2026-11-06T18:09:38Z"}
```

Die regel komt uit `_lifecycle_count_index_summary` (`scripts/build_t0_state.py:2583`), dat de beacon `health/lifecycle_count.json` leest in plaats van opnieuw te tellen, en uit `briefing_line` (`scripts/lib/lifecycle_count.py:220`). Leeftijd op het meetmoment (19:53:19Z): **6 221 s = 1 u 43 min 41 s**. De `147s` in de regel is de leeftijd van de *sweep-hartslag op het moment van tellen* (dus om 18:09:38Z), niet de leeftijd van de telling zelf. De beacon `health/lifecycle_count.json` heeft `last_run_iso: 2026-11-06T18:09:40Z` bij `expected_interval_seconds: 86400`.

**Verdict: houdt.** De drie gevraagde getallen zijn op nieuwe data nul, en de hartslag staat met zijn leeftijd in de SessionStart-briefing. Twee nuances voor de T0: de briefing toont op dit moment de *ruime* `all_v3`-telling (3 eindes zonder start, alle drie legacy-fixtures), wat zonder context als gebrek kan lezen; en de getoonde telling is bij het meetmoment bijna twee uur oud omdat de beacon dagelijks draait.

---

## Extra bevinding, geen doelpunt — `door_bookkeeping_failed` over het cohort

De T0 zag één regel op `dlv-33d4916379b0` om 18:47:06Z. Over het hele cohort zijn het er **28**, op **28 van de 30** cohortdispatches.

```
python3 - <<'PY'
import json
starts = set()
rows = []
for line in open('/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson'):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if e.get('event_type') == 'dispatch_started' and e.get('lifecycle_mode') == 'v3':
        starts.add(e['dispatch_id'])
    elif e.get('event_type') == 'door_bookkeeping_failed' and e.get('timestamp', '') >= '2026-11-06T17:53':
        rows.append(e)
incohort = [e for e in rows if e.get('dispatch_id') in starts]
print(len(rows), len(incohort), len({e['dispatch_id'] for e in incohort}))
print({e.get('site') for e in incohort}, {e.get('source') for e in incohort})
print(sum(1 for e in incohort if 'reason' in e))
PY
# -> 28 28 28
# -> {'_owner_transition:accepted'} {'dispatch_cli'}
# -> 0
```

| Eigenschap | Meting |
|---|---|
| aantal regels | 28 |
| aantal verschillende dispatches | 28 van de 30 (alleen `20261106-kmeet-kill` en deze dispatch hebben er geen) |
| `source` | `dispatch_cli` — 28/28 |
| `site` | `_owner_transition:accepted` — 28/28 |
| `reason`-veld | **0/28** — het veld bestaat niet op dit regeltype; de tekst staat in `error` |
| `error` | 28/28 `DuplicateTransitionError: Dispatch '<id>' is in terminal state 'completed'; cannot transition to 'accepted'` |
| wanneer | 1–2 seconden ná de `dispatch_finished` van dezelfde dispatch |
| na een refire? | nee — het zijn normale fires |

Het patroon is eenduidig: de deur probeert na een geslaagde dispatch nog de overgang naar `accepted`, terwijl de finalizer de rij al op `completed` heeft gezet. Dat is een boekhoudfout in de deur, niet in de levenscyclus; de levenscyclus-regels zelf zijn correct. Omdat het op 28 van de 30 voorkomt, is dit geen incident maar het normale pad.

---

## Slottafel

| Punt | Commando (verkort) | Geteld resultaat | Verdict |
|---|---|---|---|
| 1. start- en eindregels | scan op `t0_receipts.ndjson` + `event_store`-helpers over de 20 archieven | 20/20 één start, 20/20 één eind, 20/20 geldige `end_state`, 20/20 `schema_version` 3, 20/20 startvelden, 20/20 `head_sha`; **0/20 `role` op de eindregel**; 20/20 start vóór het eerste bewaarde event (5/20 archieven vanaf `sequence` 1) | **houdt niet** (één veld) |
| 2. harde kill | ledger-scan op `20261106-kmeet-kill` + `receipt_query.py open-outcomes` + `ls dead_letter/` | eindregel 19:51:10Z, `abandoned`, `deadline_passed_no_liveness`; 28 s na het eerste sweep-moment, 14 min 32 s vóór de uiterste termijn; in `open_outcomes`; niet in `dead_letter/` | **houdt** |
| 3. één rapportregel | scan op `report_ingested` / `task_complete` per dispatch | 20/20 precies één `report_ingested`; 20/20 precies één `task_complete` (schema 2, uit `envelope_govern`); converter: **0**; planszin letterlijk: onwaar | **houdt niet** (letterlijk); bedoeling houdt |
| 4. vier lezers | `check_active_drain --dry-run`, `orphan_sweep` dry-run, `crash_recovery_sweep` dry-run, `receipt_query open-outcomes`, plus `attempt_verdict` per dispatch | alle vier via `attempt_verdict` → `liveness`; 0 kandidaten in de lege `active/`; `attempt_verdict` geeft `ended`/`ended`/`live` voor kmeet / `dlv-a280eb9f257a` / deze dispatch | **houdt** |
| 5. dagelijkse telling | `ledger_health.py --no-write --json --activation-commit a7a2d7c4` | 0 starts zonder einde, 0 eindes zonder start, 0 dubbele eindes; ruime `all_v3`-telling 0/3/0 (drie legacy-fixtures); hartslag in `t0_index.json` met leeftijd 6 221 s | **houdt** |

## Red run

De meting die vóór K-act zou zijn gezakt: het aantal `dispatch_started`-regels met `lifecycle_mode: v3` van vóór de activeringscommit.

```
python3 - <<'PY'
import json
n = 0
for line in open('/Users/vincentvandeth/.vnx-data/vnx-dev/state/t0_receipts.ndjson'):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if (e.get('event_type') == 'dispatch_started'
            and e.get('lifecycle_mode') == 'v3'
            and e.get('timestamp', '') < '2026-11-06T17:08:00Z'):
        n += 1
print(n)
PY
```

Uitkomst: **0**. De activeringscommit `a7a2d7c4` is van 2026-11-06T17:08:00Z; vóór dat moment bestaat er geen enkele `v3`-startregel. De volledige `dispatch_started`-teller met `lifecycle_mode: v3` is 30, en die begint bij 17:53:42Z — 45 minuten na de commit. Vóór K-act was dit getal 0 omdat de modus nog niet bestond, en de hele levenscyclus-telling zou op een leeg cohort nul hebben gemeld zonder ooit een regel te hebben gezien.

## Onzekerheden en gaten

- **De archieven zijn geen schone per-dispatch-stromen.** 15/20 archieven beginnen met een `sequence` boven 1 en 8/20 beginnen met een regel van een *andere* dispatch. "Start vóór de eerste actie" is daarmee alleen direct bewezen voor de 5 archieven die bij `sequence` 1 beginnen; voor de andere 15 is het bewezen tegen de eerste *bewaarde* actie. De echte eerste actie kan eerder zijn gevallen en niet bewaard zijn.
- **De eindregel draagt geen `role`.** Of dat een bewuste keuze is (de rol staat op de startregel en is per `(project_id, dispatch_id)` te koppelen) of een omissie, is uit deze meting niet te beslissen. Het plan noemt `role` wel expliciet als veld op beide regels; de meting laat 0/20 zien.
- **De eindregel draagt geen `actor`/`source`.** Bij de harde kill is de herkomst alleen af te leiden uit `writer_sha` (`6ba6d1e3`) en `reason`. Een lezer die "wie heeft dit geboekt" wil beantwoorden, moet dat uit de code halen, niet uit de regel.
- **De mapkant van punt 4 is op het meetmoment leeg.** De vier lezers zijn op hun code en op de losse beslisfunctie gemeten; geen van hen is op een gevulde `dispatches/active/` met een lopende `v3`-dispatch gemeten.
- **De briefing toont de ruime telling.** `t0_index.json` meldt 3 eindes zonder start uit het `all_v3`-cohort. Die drie zijn legacy-fixtures. Zonder de cohortgrens leest dat als een gebrek; met de grens is het 0.
- **De beacon is dagelijks.** Op het meetmoment was de getoonde telling 6 221 s oud. Een storing die na 18:09:38Z is ontstaan, staat nog niet in de briefing.
- **`door_bookkeeping_failed` heeft geen `reason`-veld.** De T0 vroeg om "het `reason`-veld van elk"; dat veld bestaat niet op dit regeltype. De informatie staat in `error`, en de waarde is op alle 28 regels dezelfde soort `DuplicateTransitionError`.
- **De wrapper-scripts van `orphan_sweep` en `crash_recovery_sweep` starten niet** onder een `PYTHONPATH` die `scripts/lib` al bevat (zelf-import, zie punt 4). De metingen hierboven gebruiken daarom de lib-functie met `dry_run=True`.
