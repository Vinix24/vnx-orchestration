# Claude Code Transcript Backup & Archive

**Status**: Active
**Last Updated**: 2026-06-13
**Scope**: Machine-level (Mac Mini, `Vincents-Mini`) — operator workstation, not the VNX runtime
**Owner**: Operator (Vincent)

---

## Why this exists

Claude Code stores every session transcript as `.jsonl` under `~/.claude/projects/<encoded-project>/`.
It also **deletes** transcripts older than `cleanupPeriodDays` on every startup. The default is
**30 days**.

On 2026-06-13 a calendar-sync design conversation (from before 14 May) turned out to be
unrecoverable: the 30-day cleanup had already wiped it, and no Time Machine backup existed.
Measured at that moment, 96% of all transcripts sat within the last 30 days and only ~23 MB
survived beyond it — confirming the cleanup runs aggressively.

This setup makes that loss impossible going forward: transcripts are retained longer in the
live directory **and** mirrored to a durable local copy that survives Claude Code's cleanup.

## Functional behaviour

- **Live retention extended to 365 days.** Claude Code now keeps a full year of transcripts
  directly resumable (`claude --resume`) and searchable in `~/.claude/projects/`.
- **Daily mirror.** Every night a copy of `~/.claude/projects/` is synced to
  `~/Backups/claude-transcripts/mirror/`. The sync never deletes, so a transcript that Claude
  Code later removes (after 365 days) **stays** in the mirror. Nothing is lost again.
- **Long-term compaction.** Transcripts older than 400 days — by then already gone from the
  live directory — are compressed per calendar month into `archive/<YYYY-MM>.tar.zst` and
  dropped from the uncompressed mirror. The mirror stays at roughly the last year (directly
  browsable); everything older lives compact in the archive (`.jsonl` compresses ~8–10× with zstd).
- **Local by design.** The backup is local only. Transcripts contain client data
  (SalesMinds clients etc.), so they are deliberately **not** synced to iCloud/Dropbox.
  Disk-failure protection is a separate layer (Time Machine — see Limitations).

## Technical components

| Component | Path | Role |
|---|---|---|
| Retention setting | `~/.claude/settings.json` → `"cleanupPeriodDays": 365` | Extends live retention from 30 → 365 days |
| Backup script | `~/scripts/claude-transcript-backup.py` | rsync mirror + monthly compaction |
| Scheduler | `~/Library/LaunchAgents/nl.vincentvandeth.claude-transcript-backup.plist` | launchd job, daily 03:30 local |
| Backup root | `~/Backups/claude-transcripts/` | `mirror/`, `archive/`, logs, `.lock` |

### Flow (per daily run)

1. **Mirror** — `rsync -a --partial` (no `--delete`) from `~/.claude/projects/` to `mirror/`.
   rsync exit code `24` ("files vanished during transfer", i.e. a live session was writing) is
   treated as benign.
2. **Compaction** — files in `mirror/` with `mtime` older than `ARCHIVE_AFTER_DAYS` (400) are
   grouped by month. For each month a tar stream is piped through `zstd -19` to
   `archive/<YYYY-MM>.tar.zst`. The archive is **verified** (`zstd -t` integrity + file-count
   match against the source list) **before** the originals are removed from the mirror.
   A month whose archive already exists but receives a late straggler is written to
   `archive/<YYYY-MM>.late-N.tar.zst` rather than overwriting.
3. **Log** — a line is appended to `backup.log` with mirror/archive sizes and what was archived.

`ARCHIVE_AFTER_DAYS = 400` sits comfortably past the 365-day live retention, so the live
directory has definitely purged those files and the mirror's `rsync` will not re-add them.

The compressor is `zstd` (resolved from `/opt/homebrew/bin/zstd`, then `/usr/local/bin/zstd`,
then `PATH`). `tar` on this macOS does not support `--zstd`, so the script streams `tar` into
`zstd` directly instead of relying on a tar flag.

### Backup layout

```
~/Backups/claude-transcripts/
├── mirror/                       # uncompressed, ~last 13 months, directly searchable
│   └── <encoded-project>/<uuid>.jsonl
├── archive/                      # compressed, everything older
│   └── 2025-07.tar.zst
├── backup.log                    # script's own run log
├── launchd.out.log               # launchd stdout
└── launchd.err.log               # launchd stderr
```

## Operations

### Verify it is registered and ran

```bash
launchctl list | grep claude-transcript          # label registered, last exit code
tail -n 5 ~/Backups/claude-transcripts/backup.log # last run summary
```

### Run on demand

```bash
# via launchd (proves the scheduled path works)
launchctl kickstart -k gui/$(id -u)/nl.vincentvandeth.claude-transcript-backup

# or directly
/usr/bin/python3 ~/scripts/claude-transcript-backup.py
```

### Restore a transcript

**Recent (still in the mirror):** copy it back into the live tree, then resume.

```bash
cp ~/Backups/claude-transcripts/mirror/<encoded-project>/<uuid>.jsonl \
   ~/.claude/projects/<encoded-project>/
# then, from the project's working dir:  claude --resume <uuid>
```

**Archived (compressed month):** list, then extract one file or the whole month.

```bash
MONTH=2025-07
zstd -dc ~/Backups/claude-transcripts/archive/$MONTH.tar.zst | tar -tf -        # list
zstd -dc ~/Backups/claude-transcripts/archive/$MONTH.tar.zst | tar -xf - -C /tmp/restore  # extract
```

`<encoded-project>` is Claude Code's path encoding: the absolute project path with `/`
replaced by `-` (e.g. `-Users-vincentvandeth-Development-vnx-roadmap-autopilot-wt`).

### Change retention or destination

- **Retention**: edit `cleanupPeriodDays` in `~/.claude/settings.json` (integer days; a large
  value such as `3650` effectively disables cleanup).
- **Destination**: edit `DEST` in `~/scripts/claude-transcript-backup.py`. To move the backup to
  an external disk, point `DEST` at the mounted volume (keep it off cloud-synced folders because
  of client data).

## Capacity

- Generation rate (peak, launch period): ~1.5 GB/month (~32 MB/day).
- Steady state: mirror holds ~last year uncompressed (~15–20 GB); archive holds older years
  compressed.
- 10-year ceiling raw ≈ 50–180 GB depending on activity; compressed archive of the same ≈ ~20 GB.

## Limitations & notes

- **Disk failure is out of scope.** This protects against Claude Code's cleanup, not a dead
  disk. Time Machine was **not configured** at setup time — enabling it (external disk) is the
  recommended second layer and would also make pre-cleanup transcripts recoverable.
- **Compaction is dormant until ~mid-2027.** Nothing on disk is older than 400 days yet, so the
  archive stays empty until then. The compaction path was validated end-to-end with a synthetic
  500-day-old file (archive built, verified, original removed, round-trip content identical).
- **mtime-based bucketing.** Months are grouped by file modification time; resumed sessions can
  bump an `mtime`, which only delays archiving slightly (harmless).
- The job runs as a user LaunchAgent: it fires at 03:30 when logged in, or shortly after the
  next wake if the machine was asleep. A missed day is harmless — the mirror is cumulative.

## Validation (2026-06-13)

- Initial mirror: 1.59 GB, 5598 `.jsonl` files — identical count to the live directory.
- Compaction branch: tested with a synthetic 500-day-old file; archive built, integrity +
  count verified, original deleted, empty dir pruned, zstd→tar round-trip byte-identical.
- launchd-triggered run confirmed (entry written to `launchd.out.log` by launchd itself).
