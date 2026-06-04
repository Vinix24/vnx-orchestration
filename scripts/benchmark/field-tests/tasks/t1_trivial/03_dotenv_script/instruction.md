# Task 03 — Add load_dotenv() to a backfill script

Source-inspiratie: SEOcrawler PR #119 (`scripts/backfill_parity_check.py` missing dotenv load). Tier: T1 trivial. Deadline: 5 minutes wallclock.

## Context

The seed includes `backfill_check.py`, a CLI script that reads two environment variables and prints a summary. Running it currently fails when the operator's `.env` file isn't sourced because `load_dotenv()` is missing — the script assumes the variables are already exported in the shell.

```
$ python3 backfill_check.py
RuntimeError: missing env var SUPABASE_URL (.env not loaded?)
```

This is the exact pattern fixed in SEOcrawler PR #119: a one-line fix that brings the script in line with the rest of the codebase (which uses `python-dotenv`).

## Required changes

1. Add `from dotenv import load_dotenv` at the top of `backfill_check.py`
2. Call `load_dotenv()` near the top of the script (after imports, before reading env vars)
3. Optionally accept an explicit `.env` path via `load_dotenv(<path>)` — not required

After your edit, running the script with the seed `.env` file present must:
- Successfully load `SUPABASE_URL` and `SUPABASE_KEY` from `.env`
- Print: `OK: connected to <url> (key prefix: <first 8 chars>)`
- Exit 0

## Files you may modify

- `backfill_check.py` (modify — add 2 lines, that's it)

Do NOT modify `.env`, `requirements.txt`, or any other file.

## Definition of done

- `backfill_check.py` calls `load_dotenv()` at module level
- Running `python3 backfill_check.py` with the seed `.env` present prints "OK: connected to..." and exits 0
- No other changes
