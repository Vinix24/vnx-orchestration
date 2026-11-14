# besluitenregister

One append-only register of decisions per context folder (schema `besluit/1`, ADR-077). Standard library only, Python 3.11+, no VNX dependency.

## Install

```sh
pip install packages/besluitenregister          # gives the `besluitenregister` command
# or, without installing:
PYTHONPATH=packages/besluitenregister python3 -m besluitenregister --help
```

## A context folder

The folder must already exist; the CLI refuses a missing folder with exit 2. The first `add` creates `besluiten.ndjson` in it and needs `--slug` (the context slug, e.g. `klant:acme`); later commands take the slug from the first line.

```sh
mkdir -p ~/work/acme/daily-log
```

## Commands

```sh
R="python3 -m besluitenregister"; C="--context ~/work/acme/daily-log"
$R add $C --slug klant:acme --besloten-op 2026-11-14 --vastgelegd-door operator \
  --titel "Weekly sync on Monday" --besluit "We meet every Monday." --waarom "Fits both teams." \
  --besloten-door klant --bron-soort mail --bron-ref "mail 14-11" --citaat "Monday works for us." \
  --reikwijdte klant --vertrouwelijkheid klant --terugweg "geen: calendar only" \
  --alternatief Friday "too late in the week" --link OI-1 --tag planning --herzien-op 2027-01-01
$R list $C --nu 2026-11-15
$R show B-20261114-1a2b3c4d $C
$R vervang B-20261114-1a2b3c4d $C --besloten-op 2026-11-20 --vastgelegd-door operator --reden "team grew" \
  --titel "Weekly sync on Tuesday" ...same flags as add...
$R trek-in B-20261114-1a2b3c4d $C --besloten-op 2026-11-20 --vastgelegd-door t0 --reden "no longer needed" \
  --bron-soort meeting --bron-ref "kickoff" --citaat "Drop it."
$R herzien B-20261114-1a2b3c4d $C --besloten-op 2027-01-01 --vastgelegd-door t0 --waarom "still fits" --herzien-op geen
$R bevestig B-20261114-1a2b3c4d $C --besloten-op 2026-12-01 --vastgelegd-door operator \
  --bron-soort chat --bron-ref "chat 01-12" --citaat "Still Monday." --herzien-op 2027-06-01
$R render $C            # writes BESLUITEN.md (audience intern, deterministic)
$R check $C             # every rule of ADR-077 § 3, plus BESLUITEN.md equal to render
$R export $C --voor klant:acme --van 2026-11-01 --tot 2026-12-31 --reikwijdte klant --uit acme.md
```

`export` requires `--voor intern|publiek|klant:<slug>`: `publiek` shows only public decisions, `klant:<slug>` adds client decisions of exactly that context, never internal ones.

Exit codes: 0 ok, 1 check or read error, 2 usage, 3 refused by the core (secret, origin collision, transition, schema).
