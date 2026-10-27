# Legacy XDG Store Migration (Operator Runbook)

**Status**: Active
**Owner**: operator (never automatic)
**Scope**: a project whose only VNX store still lives under
`${XDG_DATA_HOME:-~/.local/share}/vnx/<project_id>`

Before PR #1437 (2026-10-08) `vnx init` created a project's runtime store in the
pre-#1437 XDG data home. Since then the store is the central per-project
directory `~/.vnx-data/<project_id>` (ADR-026) and no resolver reads the XDG
location any more — so an upgraded operator can have an abandoned copy on disk
that every `vnx` command ignores. This runbook measures that copy, copies it into
the central store, checks the copy, and only then removes the old one.

`vnx migrate --relocate-legacy` **copies; it never deletes**. Removal is the last
step below and stays an operator decision.

---

## 1. Who needs this

A project needs this runbook when both are true:

- its store is at `${XDG_DATA_HOME:-~/.local/share}/vnx/<project_id>`, and
- `~/.vnx-data/<project_id>` does **not** exist (the central store every current
  resolver reads).

If the central store already exists, the XDG copy is inert history: `vnx doctor`
reports it as `PASS` and this runbook is not needed. If the directory under
`<xdg>/vnx/` exists but holds no `state/runtime_coordination.db`, it is only an
empty skeleton — there is nothing to move.

## 2. Measure it

### The doctor check

Run `vnx doctor` from a neutral shell (no ambient data-dir override, which would
point every check at another store):

```bash
unset VNX_DATA_DIR VNX_DATA_DIR_EXPLICIT VNX_STATE_DIR VNX_PROJECT_ID
vnx doctor --project-dir /path/to/project | grep 'store:legacy-xdg'
```

The check is `store:legacy-xdg`, defined by `_check_legacy_store`
(`vnx_cli/commands/doctor.py:374`). For a project whose only store is the legacy
one it prints a `WARN` with this exact text (paths substituted):

```
[WARN]  store:legacy-xdg  legacy XDG store <legacy-store> (holds state/runtime_coordination.db) is NOT used — this project's central store <central-store> does not exist, so the state there is invisible to every vnx command. Moving it is not wired yet: `vnx migrate --relocate-legacy` will be available in a later release
```

The final sentence predates the release that shipped the command: `vnx migrate
--relocate-legacy` is available, and the rest of this runbook is that path. The
wording is tracked as a follow-up; do not wait for it to change.

A plain `vnx doctor` exits non-zero only on `FAIL`; the `WARN` alone does not
fail it. `vnx doctor --strict` turns every warning into exit 1, so it is not the
check to run before a relocation.

### The inventory

The same fact, without doctor (a directory is a store only when it holds
`state/runtime_coordination.db`; this is the rule `legacy_store_path` applies at
`scripts/lib/legacy_store_path.py:47`):

```bash
XDG_VNX="${XDG_DATA_HOME:-$HOME/.local/share}/vnx"
ls -1 "$XDG_VNX" 2>/dev/null                       # one directory per project id
for d in "$XDG_VNX"/*/; do
  [ -f "$d/state/runtime_coordination.db" ] && du -sh "$d"
done
```

On the machine where this runbook was planned (2026-10-24) that was 36
directories, 28 of them stores, together about 30 MB — scale context, not a
target.

## 3. Preconditions

1. **Central store target absent.** `~/.vnx-data/<project_id>` must not exist in
   any form; the command refuses to overwrite one.
2. **No ambient data-dir override.** `VNX_DATA_DIR` and `VNX_STATE_DIR` must be
   unset: a relocation publishes into the *central* store, and an override would
   split the store. `VNX_PROJECT_ID`, when set, must equal `--project-id`.
3. **No repo-local shadow store.** The project directory must not have its own
   `.vnx-data/state` holding files; it would shadow the central target.
4. **Quiesce writers.** Stop anything that writes the project's store (dispatcher,
   receipt processor) before the copy; the copy is consistent, but a writer that
   keeps writing the *old* store afterwards is not migrated by it.
5. **Ownership is provable.** The source DBs carry `project_id` stamps that name
   `--project-id`; a schema-v14 store carries none and needs `--assume-owner`.

## 4. Copy it

Flags (from `vnx migrate --help`, help text abridged):

```
--project-dir DIR       project directory to resolve data root from
--relocate-legacy       copy a pre-#1437 XDG store into the central store (deletes nothing)
--legacy-path DIR       legacy store to relocate (required; no auto-discovery)
--project-id PROJECT_ID tenant of the relocated store (required)
--assume-owner REASON   record why an unstamped (schema-v14) store belongs to --project-id
```

```bash
vnx migrate --relocate-legacy \
  --project-dir /path/to/project \
  --legacy-path "${XDG_DATA_HOME:-$HOME/.local/share}/vnx/<project_id>" \
  --project-id <project_id> \
  --assume-owner "<why this store is <project_id>>"
```

Pass `--assume-owner` only for an unstamped store: the reason is written into the
migration record and the ownership receipt. A store whose databases carry the
`project_id` stamp needs no flag; one stamped with a different project is
refused.

The command:

1. refuses before writing anything when a precondition above fails;
2. copies through the SQLite backup API (committed WAL frames included) into
   `~/.vnx-data/.staging-<project_id>-<timestamp>/<project_id>`;
3. runs the full migration chain on the copy (base schema, numbered runners,
   future-system walk, tenant stamping) and verifies `PRAGMA integrity_check` and
   per-table row parity against the source — every step fatal;
4. publishes with one `rename` to `~/.vnx-data/<project_id>`;
5. writes `<target>/state/relocate-legacy.json` and books a `state_mutation`
   receipt (`trigger: migrate_relocate_legacy`) in `<target>/state/t0_receipts.ndjson`.

Expected lines at the end of a successful run:

```
Relocated into the central store: <central-store>
  source left untouched: <legacy-store>

Verify it:
  vnx doctor --project-dir /path/to/project

The legacy copy is NOT removed. Remove it yourself once the check passes:
  rm -rf -- <legacy-store>
```

The middle of the run prints the bootstrap and migration steps (staging path,
`[skip] migration ... already applied`, the W1 tenant-stamping summary,
`Migration complete.`). Those lines are expected, not a prompt.

### What it refuses, and why

| Refusal | Why |
|---|---|
| `--legacy-path` missing | There is no auto-discovery: a wrong guess would hand another project's state to this one. |
| `--project-id` missing or malformed | The tenant the store is stamped with must be explicit and match the project-id grammar. |
| `VNX_DATA_DIR` or `VNX_STATE_DIR` set | A relocation publishes into the central store; an ambient override would split or misdirect the store. |
| `VNX_PROJECT_ID` differs from `--project-id` | The fail-closed tenant resolver would refuse mid-run; refuse before any write instead. |
| Symlink anywhere in the source | `copytree` follows it while database detection does not, so a linked database would be copied raw (losing committed WAL frames) or twice. |
| No `state/runtime_coordination.db` in the source | An empty skeleton is not a store; there is nothing to move. |
| Project has its own `.vnx-data/state` with files | It would shadow the central target, so the migrated store would still be invisible. |
| `.vnx-project-id` marker empty/unreadable | The tenant the marker meant cannot be guessed. |
| `.vnx-project-id` names another project | Refuses to relabel another project's store. |
| Target `~/.vnx-data/<project_id>` exists | Never overwrites a store, in any form. |
| Source stamped with a foreign `project_id` | The store belongs to another tenant; handing it over would corrupt both. |
| Source carries no `project_id` stamp and no `--assume-owner` | A pre-ADR-007 (schema-v14) store: ownership must be asserted and recorded, not inferred. |

Exit codes: `2` for a usage error (missing flags, invalid `--project-id`, source
not a directory, a symlink in the source); `1` for every other refusal and for a
failed copy/upgrade/integrity/publish step.

**On any failure after the copy starts**, the staging directory is kept and its
path is printed; the central store is not created. Inspect or delete the staging
directory yourself, fix the cause, and re-run. A failure to book the ownership
receipt un-publishes the copy (it is renamed back into staging), so there is no
store without its receipt.

## 5. Check it

```bash
CENTRAL="$HOME/.vnx-data/<project_id>"
LEGACY="${XDG_DATA_HOME:-$HOME/.local/share}/vnx/<project_id>"

# 1. Doctor: the WARN is gone and the legacy copy is now inert history.
vnx doctor --project-dir /path/to/project | grep 'store:legacy-xdg'
#   [PASS]  store:legacy-xdg  a legacy XDG copy also exists at <legacy-store>
#           (not used; active central store: <central-store>)

# 2. The copy is a sound database at the current schema.
sqlite3 "$CENTRAL/state/runtime_coordination.db" 'PRAGMA integrity_check;'  # ok
sqlite3 "$CENTRAL/state/runtime_coordination.db" 'PRAGMA user_version;'    # current, not 14

# 3. The ownership decision is on record.
cat "$CENTRAL/state/relocate-legacy.json"
python3 -c "import json,sys; r=json.loads(open(sys.argv[1]).readline()); print(r['trigger'], r['status'])" \
  "$CENTRAL/state/t0_receipts.ndjson"

# 4. The source is untouched (same hashes before and after the copy).
shasum -a 256 "$LEGACY/state/runtime_coordination.db"   # sha256sum on Linux
```

`PRAGMA user_version` is the highest migration the release ships, not a fixed
number: compare it with the same pragma on a freshly `vnx init`-ed store, or with
the highest `NNNN_*` migration under `schemas/migrations/` in that release.

Only when all four checks pass should the old store be removed.

## 6. Remove the old store (operator step)

**Nothing in VNX will do this for you.** The command prints the exact removal
line; run it yourself after the checks above:

```bash
ls -la "$LEGACY"          # last look: path, size, timestamps
rm -rf -- "$LEGACY"
```

Removal is irreversible. If there is any doubt about the copy, keep the old store
(move it aside instead of deleting it) and remove it in a later session. Do not
remove the source when a check failed, and do not remove it while a writer still
points at it.

## Related

- `docs/governance/decisions/ADR-026-per-project-store-with-governance-federation.md`
  — the central per-project store.
- `docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md` — the
  `project_id` stamp ownership is read from.
- `docs/MIGRATION_GUIDE.md` §7 — the short pointer to this runbook.
