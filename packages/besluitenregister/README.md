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

## Importing a daily-log

`add --from-daily-log <log.md>` writes every top-level bullet under a `## Beslissingen` or `### Beslissingen (...)` heading (level 2 or 3, any suffix; the section ends at the next heading of the same or a higher level) as one decision. A nested bullet belongs to its parent; empty bullets and `geen`, `-`, `n.v.t.` are skipped; no other section is read. `besloten_op` is the `date:` of the frontmatter, else the `YYYY-MM-DD` in the file name (neither: exit 2, nothing written). The source is `{"soort": "daily-log", "ref": "<file name>#<heading>/<n>"}`, where `<n>` is the bullet's ordinal under that heading; for this source `herkomst` keys on that position, not on the text, so a point edited in place is refused with exit 3 instead of becoming a second decision. Bullets inside a fenced block (``` or `~~~`) are no decisions. `vastgelegd_door` is `skill:daily-log` and `besloten_door` is `operator` (override with `--besloten-door`).

```sh
br add --from-daily-log "$HOME/work/acme/daily-log/2026-11-14.md" --slug klant:acme
```

Importing the same log again writes nothing and prints the existing ids. A bullet whose text changed is refused by the core (exit 3); the import stops there and names the bullet by its position (`<file name>#<heading>/<n>`) and the existing id, never by its text, plus how many were written before it. A secret is refused as a secret before any other check: the message names the pattern, never the value. A log without the section exits 0 with "0 besluiten geschreven".

## Activating the skills

The `daily-log` skill runs `python3 -m besluitenregister add --context <LOGDIR> --from-daily-log <log>` after it wrote the log and shows the count (a non-zero exit is shown, never swallowed). The `kickoff` skill runs `python3 -m besluitenregister list --context <LOGDIR>` and shows the recent decisions and those in state `te_herzien`. Both skip silently when `<LOGDIR>` has no `besluiten.ndjson` and the package is not installed. The skill text lives outside this repo, in `~/.claude/skills`, and is placed by the operator.
