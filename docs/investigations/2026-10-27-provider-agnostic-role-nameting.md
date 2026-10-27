# orchestrator-provider-agnostic-role — nameting op main

**Dispatch-ID**: dlv-b589e464ecfb
**Measured**: `origin/main` at `9f42be777ddc40ad46db9e6fc9dd3fe6c80dd1fc` (the tip when the measurement ran; main has since advanced to `5573d363`, which touches neither `bin/vnx`, `scripts/fleet_role_drift.py` nor `scripts/commands/t0_role_audit.sh`)
**Date**: 2026-10-27
**Method**: throwaway projects under `mktemp -d` (`git init`, a minimal `.claude/terminals/T0/role-orchestrator.md`), never a real consumer repo. `VNX_HOME` = the fabric checkout; canon = `<VNX_HOME>/.claude/terminals/T0/role-orchestrator.md` (474 lines, 32706 bytes, sha256 `24f2825c48dca8e2…`). Nothing under `~/.kimi`, `~/.claude` or `~/.claude-salesminds` was read, written or tested.

## Measurement

| # | Goal point | Command | Counted output | Verdict |
|---|---|---|---|---|
| 1 | `vnx role sync` writes `role-orchestrator.md` to AGENTS.md (Codex) in the T0 folder | `bin/vnx role sync --apply --project-dir "$TMP"` (target resolution: `--project-dir`) | `[role-sync] created $TMP/.claude/terminals/T0/AGENTS.md`. Markers on line 1 (`<!-- VNX:BEGIN T0-ROLE -->`) and line 476 (`<!-- VNX:END T0-ROLE -->`); the payload between them is 32706 bytes, sha256 `24f2825c48dca8e2…`, byte-identical (`cmp`) to the canonical `role-orchestrator.md`. | **met** |
| 2 | The same role is written to GEMINI.md (Gemini, and the route the repo calls "Gemini/Kimi") | same invocation | `[role-sync] created $TMP/.claude/terminals/T0/GEMINI.md`, same markers (1 / 476), same 32706 bytes / `24f2825c48dca8e2…`, byte-identical to the same canonical role. | **met** |
| 3 | It reuses `patch-agent-files` (the marked block in CLAUDE.md/AGENTS.md), not a second writer | `grep -n vnx_upsert_marked_block bin/vnx scripts/lib/vnx_marked_blocks.sh` | 1 definition (`scripts/lib/vnx_marked_blocks.sh:18`) and 2 callers: `patch-agent-files` at `bin/vnx:1045` (targets CLAUDE.md/AGENTS.md/GEMINI.md, `bin/vnx:1091`) and role sync at `bin/vnx:1708` (its no-op probe `_role_sync_block_current` probes with the same helper, `bin/vnx:1489`). Role sync uses its own marker pair `<!-- VNX:BEGIN T0-ROLE -->` (`bin/vnx:1679`) so it cannot collide with the project-root `<!-- VNX:BEGIN BOOTSTRAP -->` block (`bin/vnx:207`). Zero other insert/replace implementations exist. | **met** |
| 4a | `fleet_role_drift.py` fails when AGENTS.md or GEMINI.md diverges from the canonical role | `python3 scripts/fleet_role_drift.py --project-dir "$TMP"` on the synced project; then after a one-line edit inside the AGENTS.md block; then again after a second `vnx role sync --apply` | Synced: exit 0, `content AGENTS.md current`, `content GEMINI.md current`, `fleet: 0 of 1 measured projects behind the canon`. One-line edit: exit 1, `content AGENTS.md STALE (2 diff lines)` plus the two diff lines (`-… DRIFTED LINE ONE-CHAR-EDIT.` / `+You are T0. You orchestrate work and governance. You do not implement code.`); the same edit inside GEMINI.md gives `content GEMINI.md STALE (2 diff lines)`. After the second sync: exit 0, both `current`. | **met** |
| 4b | `t0_role_audit.sh` fails when AGENTS.md or GEMINI.md diverges from the canonical role | `bash scripts/commands/t0_role_audit.sh --static "$TMP"` on the synced project and on the one-line-diverged project | On main the two runs print **byte-identical** output — 2 × `PLAYBOOK-MECHANISM-GAP`, exit 1 both times — so divergence is invisible: the static check only greps for the BEGIN marker (`scripts/commands/t0_role_audit.sh:291` on main) and never compares the payload. Fixed in this PR (below). | **not met on main → met in this PR** |
| 5 | Kimi's system-prompt surface | `git grep -n -i kimi -- bin/vnx scripts/fleet_role_drift.py scripts/commands/t0_role_audit.sh` | 6 hits, all comment/help text: 4 name the GEMINI.md route as "Gemini/Kimi" (`bin/vnx:1502`, `1674`, `1785`; `scripts/fleet_role_drift.py:21`, `298`) and 1 is the unrelated `vnx gate --only kimi` review gate (`bin/vnx:410`). No repo code writes a Kimi surface. | **operator decision, outside the repo** |

### The marked block it writes (points 1-3)

```
$TMP/.claude/terminals/T0/AGENTS.md   line 1: <!-- VNX:BEGIN T0-ROLE -->   line 476: <!-- VNX:END T0-ROLE -->
$TMP/.claude/terminals/T0/GEMINI.md   line 1: <!-- VNX:BEGIN T0-ROLE -->   line 476: <!-- VNX:END T0-ROLE -->
payload (both files, and role-orchestrator.md): 32706 bytes, sha256 24f2825c48dca8e2…, `cmp` identical
```

A second `vnx role sync --apply` is a no-op on all three files (`already current`), so the write is idempotent; an out-of-date block is reported as `updated` and a pre-existing file is backed up (`AGENTS.md.bak.<timestamp>`) first.

## Point 4b: what was fixed here

`t0_role_audit.sh --static` checked marker PRESENCE, not currency. A hand-edited block carried the same markers as a current one, so it audited exactly like a synced mirror. This PR adds a content axis:

- `PROVIDER-ROLE-DRIFT` — the block in AGENTS.md/GEMINI.md is not the text of the project's `role-orchestrator.md`, or the block cannot be delimited (BEGIN without END), or there is no role file to compare against. All three stay blocking: an unmeasurable mirror is never "current".
- No block at all stays unflagged: AGENTS.md/GEMINI.md are gitignored artefacts `vnx role sync` generates, and a project that does not use that route legitimately has none. Absence is `fleet_role_drift.py`'s coverage warning, not local drift.
- Reference is the project's own `role-orchestrator.md`, not the shipped canon: a role file that is itself behind the canon is `fleet_role_drift.py`'s content axis. Keeping the two tools on their own axis avoids a second canon lookup in the audit.

Measured end to end on the temp project (branch code): one-line edit inside AGENTS.md → `PROVIDER-ROLE-DRIFT: …/.claude/terminals/T0/AGENTS.md carries a canonical-role block that is not the text of …/role-orchestrator.md — the codex/gemini role surface has diverged from the canonical role (run 'vnx role sync --apply')`, exit 1; after `vnx role sync --apply` → 0 `PROVIDER-ROLE-DRIFT` lines. The audit's exit code stays 1 in both cases because a provider mirror always triggers the tracked `PLAYBOOK-MECHANISM-GAP` finding (codex/gemini have no SessionStart-hook equivalent); the discriminating signal is the named finding, and the exit code becomes the discriminator once that tracked gap closes.

Size: 34 added production lines (non-comment, non-blank) in `scripts/commands/t0_role_audit.sh`. Test: `TestProviderRoleContentDrift` in `tests/test_t0_role_audit_static.py`, 5 of its 7 cases red on `9f42be77` (5 failed, 2 passed) and all green after the fix (90 passed in the file).

## Kimi (point 5)

The repo has no `.kimi` path and no Kimi role surface to write: the only Kimi configuration is `~/.kimi/config.toml`, which lives outside the repo in an operator-only area (hard-parked — nothing under `~/.kimi` was read, written or tested here). The evidence that no repo code writes a Kimi surface is the grep in the table: all six `kimi` hits in `bin/vnx`, `scripts/fleet_role_drift.py` and `scripts/commands/t0_role_audit.sh` are prose naming GEMINI.md as the "Gemini/Kimi" route, plus the `vnx gate --only kimi` review gate — the role-sync writer touches exactly two provider files, AGENTS.md and GEMINI.md (`bin/vnx:1684`). Repo-wide, the only `~/.kimi` interaction is the kimi CLI spawn path reading/reaping CLI session dirs (`scripts/lib/provider_spawns/kimi_spawn.py:541`, `scripts/lib/append_receipt_internals/session_resolver.py:27`); a grep for writes to a `.kimi` path returns nothing. Whether a Kimi system prompt exists at all is therefore an operator decision outside this repository, not a delivery gap in the track.

## Which PRs delivered points 1-4

| Point | Delivered by |
|---|---|
| 1, 2, 3 | **#1057** (`5039b503`, 2026-07-08) — `cmd_role_sync` mirrors the canonical role into `<!-- VNX:BEGIN T0-ROLE -->` blocks in AGENTS.md and GEMINI.md through the shared `vnx_upsert_marked_block` helper, with `--project-dir` target resolution and backup-before-write. |
| 4 (fleet axis) | **#1704** (`35bfd2ee1`, 2026-08-28) — `scripts/fleet_role_drift.py`, content axis over `role-orchestrator.md`, AGENTS.md and GEMINI.md, exit 1 on drift, exit 2 when it cannot measure. |
| 4 (audit axis, presence) | **#1174** (`744564bab`, 2026-07-16) — `t0_role_audit.sh --static` reports `PLAYBOOK-MECHANISM-GAP` for the AGENTS.md/GEMINI.md surfaces it previously did not look at. |
| 4 (audit axis, content) | **this PR** — `PROVIDER-ROLE-DRIFT`, the missing half from 4b. |

## What is left

The delivered role's Mandatory Startup step still has no delivery mechanism on the codex/gemini surfaces — that is the tracked `PLAYBOOK-MECHANISM-GAP`, reported by the audit, not closed by the track's goal points. Nothing else in this track is open.
