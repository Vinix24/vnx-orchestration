# besluitenregister

One append-only register of decisions per context folder (schema `besluit/1`, ADR-077). Standard library only, Python 3.11+, no VNX dependency. **Install:** `pip install packages/besluitenregister` (gives the `besluitenregister` command), or without installing `PYTHONPATH=packages/besluitenregister python3 -m besluitenregister --help`.

**A context folder** must already exist (`mkdir -p ~/work/acme/daily-log`); a missing folder is refused with exit 2. The first `add` creates `besluiten.ndjson` in it and needs `--slug` (e.g. `klant:acme`); later commands take the slug from the first line.

```sh
br() { python3 -m besluitenregister "$@" --context "$HOME/work/acme/daily-log"; }   # works in sh, bash and zsh
br add --besloten-op 2026-11-14 --vastgelegd-door operator --slug klant:acme --titel "Sync on Monday" --besluit "We meet on Mondays." --waarom "Fits both teams." --besloten-door klant --bron-soort mail --bron-ref "mail 14-11" --citaat "Monday works." --reikwijdte klant --vertrouwelijkheid klant --terugweg "geen: calendar only" --alternatief Friday "too late" --link OI-1 --tag planning --herzien-op 2027-01-01
br list --nu 2026-11-15
br show B-20261114-1a2b3c4d
br vervang B-20261114-1a2b3c4d --besloten-op 2026-11-14 --vastgelegd-door operator --reden "team grew" --titel "Sync on Tuesday" ...   # the same flags as add
br trek-in B-20261114-1a2b3c4d --besloten-op 2026-11-14 --vastgelegd-door operator --reden "not needed" --bron-soort meeting --bron-ref kickoff --citaat "Drop it."
br herzien B-20261114-1a2b3c4d --besloten-op 2026-11-14 --vastgelegd-door operator --waarom "still fits" --herzien-op geen
br bevestig B-20261114-1a2b3c4d --besloten-op 2026-11-14 --vastgelegd-door operator --bron-soort chat --bron-ref "chat 01-12" --citaat "Still Monday." --herzien-op 2027-06-01
br render    # writes BESLUITEN.md atomically, audience intern, same register gives the same bytes
br check     # every rule of ADR-077 § 3, plus BESLUITEN.md equal to what render writes
br export --voor klant:acme --van 2026-11-01 --tot 2026-12-31 --reikwijdte klant --uit acme.md
```

`export --voor` is required: `publiek` shows public decisions only, `klant:<slug>` adds client decisions of exactly that context, `intern` shows all. Exit codes: 0 ok, 1 check or read error, 2 usage, 3 refused by the core (secret, origin collision, transition, schema).
