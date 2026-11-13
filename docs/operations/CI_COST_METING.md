# CI-kosten en merge-ritme: de meting (track ci-cost-and-merge-cadence, PR 1)

Gemeten op 13-11-2026 voor ADR-073. Elk getal staat met zijn commando, zijn venster en zijn n. Alles is alleen-lezen: `gh api`, `gh run view --log` en de grootboekbestanden. Er is geen run herhaald, geannuleerd of aangeraakt.

## 1. Punt 1 en 2: het profiel bestaat en kiest bij twijfel `heavy`

```
python3.12 -m pytest tests/test_ci_profile_classification.py -q -p no:cacheprovider
```

Resultaat op `origin/main` `723bc4496`: `22 passed`. Het profiel komt uit `scripts/ci/classify_ci_profile.py` (allowlist: `docs/**`, `claudedocs/**`, `*.md` in de root, changelog-fragmenten). De prelude drukt het af in de stap "Classify CI profile".

Een lichte en een zware run, de profielregel uit het run-log (`gh run view <id> --log | grep -m3 -E 'CI profile:|Classify CI profile'`):

| Run | Commit | Duur | Profielregel |
|---|---|---|---|
| 43241492133 (push, main) | `c4a5f9dce` | 2 min 28 s | `CI profile: light` |
| 43365651636 (push, main) | `723bc4496` | 17 min 30 s | `CI profile: heavy` |

## 2. Punt 3: docs-only voor en na

Het profiel van een run is geen veld op GitHub. Per run is dezelfde classifier gedraaid op dezelfde diffreeks als de prelude: `<sha>^...<sha>` voor een push, `origin/main...<sha>` voor een PR. Alleen runs met conclusie `success`. Een commit die niet lokaal staat is niet geclassificeerd en niet geteld.

```
gh api 'repos/Vinix24/vnx-orchestration/actions/workflows/vnx-ci.yml/runs?per_page=100&created=2026-11-09..2026-11-13&status=completed' --paginate \
  --jq '.workflow_runs[] | [.id,.event,.conclusion,.head_branch,.head_sha,.run_started_at,.updated_at] | @tsv' > /tmp/dlv36_runs.tsv
# idem met created=2026-08-01..2026-08-14 naar /tmp/dlv36_runs_before.tsv
python3 -I profiel.py /tmp/dlv36_runs.tsv        # profiel.py is het blok hieronder, vanuit de repo-root
```

```
import csv, subprocess, statistics, sys, datetime as dt
sys.path.insert(0, "scripts/ci")
from classify_ci_profile import classify_paths

def ts(s): return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
def paths(event, sha):
    rng = f"{sha}^...{sha}" if event == "push" else f"origin/main...{sha}"
    r = subprocess.run(["git", "diff", "--name-only", rng], capture_output=True, text=True)
    return r.stdout.split("\n")[:-1] if r.returncode == 0 else None
def q(v, f):
    v = sorted(v); return v[int(round(f * (len(v) - 1)))]
rows, skipped = [], 0
for run_id, event, concl, br, sha, start, end in csv.reader(open(sys.argv[1]), delimiter="\t"):
    if concl != "success": continue
    p = paths(event, sha)
    if p is None: skipped += 1; continue          # commit not local: not classified, not counted
    rows.append((event, classify_paths(p), (ts(end) - ts(start)).total_seconds() / 60))
print("event profile n median_min p75_min   (skipped, commit not local:", skipped, ")")
for ev in ("push", "pull_request"):
    for pr in ("light", "heavy"):
        d = [m for e, p, m in rows if e == ev and p == pr]
        if d: print(ev, pr, len(d), round(statistics.median(d), 1), round(q(d, .75), 1))
```

| Venster | Event | Profiel | n | Mediaan (min) | p75 (min) |
|---|---|---|---|---|---|
| na: 09-11 t/m 13-11 | push (main) | light | 15 | 2,5 | 2,7 |
| na | push (main) | heavy | 177 | 18,2 | 19,6 |
| na | pull_request | light | 23 | 2,5 | 2,7 |
| na | pull_request | heavy | 299 | 18,1 | 19,4 |
| voor: 01-08 t/m 14-08 | push (main) | light | 10 | 2,2 | 16,2 |
| voor | push (main) | heavy | 191 | 15,3 | 16,7 |
| voor | pull_request | light | 9 | 16,1 | 16,6 |
| voor | pull_request | heavy | 213 | 15,9 | 17,1 |

37 runs uit het voor-venster zijn niet geclassificeerd (commit niet lokaal). Een docs-only PR-run duurde voor de classifier (#1514, 15-08) mediaan 16,1 min en duurt nu 2,5 min (n=9 tegen n=23). De mediaan van 2,2 min voor de lichte pushes in het voor-venster, bij een p75 van 16,2, is met n=10 niet verklaard. Dat laat ik zo staan.

## 3. De nulmeting van 13-11

Overgenomen van de T0, niet door mij herberekend, bron `gh api repos/Vinix24/vnx-orchestration/actions/workflows/vnx-ci.yml/runs` en de receipts van vnx-dev:

- PR-CI mediaan 17 min, main-CI mediaan 19 min (venster 09-11 tot 13-11).
- Per 1000 regels in de klasse 300-450: 5,6 reviewrondes en 90 PR-CI-minuten.
- Een runner wacht mediaan 0,03 min over 440 jobs in 40 runs.

Mijn eigen telling over hetzelfde venster, alle conclusies, uit de lijst van sectie 2 (`run_started_at` tot `updated_at`):

```
python3 -I -c "
import csv,statistics,datetime as dt
t=lambda s: dt.datetime.strptime(s,'%Y-%m-%dT%H:%M:%SZ')
for ev in ('pull_request','push'):
    d=sorted((t(r[6])-t(r[5])).total_seconds()/60 for r in csv.reader(open('/tmp/dlv36_runs.tsv'),delimiter='\\t') if r[1]==ev)
    print(ev,len(d),round(statistics.median(d),1),round(d[int(round(.75*(len(d)-1)))],1))"
```

Uitkomst: PR-runs n=399, mediaan 16,8 min (p75 19,0). Main-pushruns n=197, mediaan 17,8 min (p75 19,5). Dat is 17 min voor PR-CI en 1,2 min onder de 19 min voor main. Het verschil komt uit de gekozen runs en de duurdefinitie en verandert de conclusie niet.

## 4. De wachttijd van de keten

Bron: `claudedocs/2026-10-24-nachtrun-golf-1-3/bin/merge-keten-20261111.log`. De wachttijd is de tijd van de regel `#<pr> MERGED` tot de regel `#<pr> main groen run` van dezelfde PR. Het log heeft alleen kloktijden: een negatief verschil is een middernachtovergang.

```
LOG=claudedocs/2026-10-24-nachtrun-golf-1-3/bin/merge-keten-20261111.log
grep -cE '^#[0-9]+ MERGED' $LOG          # 88
grep -cE '^#[0-9]+ main groen run' $LOG  # 87
python3 -I wacht.py $LOG                 # wacht.py is het blok hieronder
```

```
import re, sys, statistics
m, w = {}, []
def sec(t): h, mi, s = map(int, t[:-1].split(":")); return h * 3600 + mi * 60 + s
for line in open(sys.argv[1]):
    a = re.match(r"^#(\d+) MERGED \S+ (\d\d:\d\d:\d\dZ)", line)
    if a: m[a[1]] = sec(a[2]); continue
    b = re.match(r"^#(\d+) main groen run \d+ (\d\d:\d\d:\d\dZ)", line)
    if b and b[1] in m: w.append(((sec(b[2]) - m[b[1]]) % 86400) / 60)   # modulo: midnight crossing
w.sort()
print("n", len(w), "median", round(statistics.median(w), 1), "p75", round(w[int(round(.75 * (len(w) - 1)))], 1),
      "total_h", round(sum(w) / 60, 1), "max", round(w[-1], 1))
```

Resultaat: n=86 gekoppelde merges, mediaan **19,4 min**, p75 20,6 min, samen 24,9 uur, maximum 40,0 min. De T0 gaf 85 merges en 24,6 uur. Het verschil is één merge. Mediaan en p75 zijn gelijk. De 19,4 min is de duur van een main-run en dus een **bovengrens van de winst**.

## 5. Klaar-voor-merge tot merge

Venster: 11-11 00:00Z tot 13-11 06:00Z. De merge-tijd is de `timestamp` van het `pr_merged`-receipt in `~/.vnx-data/vnx-dev/state/t0_receipts.ndjson`. Klaar-voor-merge is de laatste van twee tijden:

- de eerste groene VNX CI PR-run op de gemergde kop (`gh api .../actions/workflows/vnx-ci.yml/runs?head_sha=<sha>`, hier uit de lijst van sectie 2);
- per stoel de laatste PASS voor die kop in `review_gates/rounds.ndjson`. De map `review_gates/results/` houdt per PR en stoel alleen het laatste resultaat en geen verdict per kop, dus het rondegrootboek is de bron voor de PASS-tijd.

De kop komt uit `tested_head` op het receipt. Dat veld staat pas sinds DR-7 (#2745) op receipts en ontbreekt op 88 van de 91. Daar is de kop gelezen uit de CI-poort van hetzelfde receipt (`preflight_gates[gate=ci].head_sha`, verdict GO): de kop die de deur als getest zag. Een PR waarvan een deel niet te lezen is, telt als onmeetbaar en staat apart. Nooit als 0 (ADR-063).

```
import csv, json, statistics, collections, datetime as dt, os
t = lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
S = os.path.expanduser("~/.vnx-data/vnx-dev/state")
# green PR-event CI completion per head: the first moment the head was green
green = {}
for rid, ev, concl, br, sha, st, end in csv.reader(open("/tmp/dlv36_runs.tsv"), delimiter="\t"):
    if ev == "pull_request" and concl == "success":
        green[sha] = min(green.get(sha, t(end)), t(end))
# latest PASS per (pr, head, seat) from the round ledger
rounds = collections.defaultdict(dict)
for l in open(f"{S}/review_gates/rounds.ndjson"):
    try: r = json.loads(l)
    except ValueError: continue
    if r.get("verdict") == "pass" and r.get("head_sha") and r.get("recorded_at"):
        k = (r["pr_number"], r["head_sha"]); s = r["seat"]
        rounds[k][s] = max(rounds[k].get(s, t(r["recorded_at"])), t(r["recorded_at"]))
vals, unm, src = [], collections.Counter(), collections.Counter()
for l in open(f"{S}/t0_receipts.ndjson"):
    if '"pr_merged"' not in l: continue
    try: r = json.loads(l)
    except ValueError: continue
    if r.get("event_type") != "pr_merged" or not ("2026-11-11T00:00:00Z" <= r["timestamp"] < "2026-11-13T06:00:00Z"): continue
    head, how = r.get("tested_head"), "tested_head"
    if not head:   # fallback: the head the ci gate of the door tested
        g = [x for x in r.get("preflight_gates", []) if x.get("gate") == "ci" and x.get("verdict") == "GO" and x.get("head_sha")]
        head, how = (g[0]["head_sha"], "ci_gate_head_sha") if g else (None, None)
    if not head: unm["geen tested head"] += 1; continue
    if head not in green: unm["geen groene CI-run op de kop in het venster"] += 1; continue
    seats = rounds.get((r["pr_number"], head))
    if not seats: unm["geen PASS-ronde op de kop"] += 1; continue
    ready = max([green[head], *seats.values()]); wait = (t(r["timestamp"]) - ready).total_seconds() / 60
    if wait < 0: unm["klaar na de merge (kop gewijzigd of ronde achteraf)"] += 1; continue
    vals.append(wait); src[how] += 1
vals.sort()
print("n_measured", len(vals), "median_min", round(statistics.median(vals), 1), "p75_min", round(vals[int(round(.75 * (len(vals) - 1)))], 1), "max", round(vals[-1], 1))
print("bron kop:", dict(src)); print("unmeasurable:", sum(unm.values()), dict(unm))
```

Resultaat: 91 `pr_merged`-receipts in het venster, **n gemeten 91**, mediaan **65,1 min**, p75 **159,1 min**, maximum 783,2 min. **Onmeetbaar: 0.** Kop uit `tested_head`: 3, uit de CI-poort: 88.

De 19,4 min van sectie 4 is een onderdeel van deze wachttijd. De mediaan van 65,1 min is groter omdat een klaarstaande PR ook wacht op de T0, op pauzes en op de merge-lus. De winst van het venster is dus hoogstens 19,4 min per merge, en alleen voor het deel van de wachttijd dat wachten op main is.

## 6. Wat nog volgt

De meting bij N=1 en bij N=3 volgt in PR 4b, met dezelfde commando's. Het besluit over N=3 is van de operator, met die meting erbij.
