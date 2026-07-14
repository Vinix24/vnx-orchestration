# ADR-032 — Fabric artifacts in consumer repos: the A/B/C SSOT manifest

**Status:** Accepted (ratified by Vincent, 2026-07-14)
**Date:** 2026-07-14
**Decided by:** Operator (Vincent van Deth), by direct decision. The plan-gate panel was degraded (opus seat down, separate fix in flight) at ratification time, so this ADR was ratified by direct operator decision rather than gate certification.
**Resolves / Cross-refs:** `skills-manifest-and-update-flow` (#1070), `fleet-future-state-sync` (ops-attest 2026-07-05), `skill-role-reconcile` (queued), `post10-skill-doc-reconcile` (queued), role canonicalization 07-08/07-09 (`20260708-role-sync-provider-agnostic-spec.md`). Restates and brings current `claudedocs/2026-07-14-ADR-DRAFT-skills-in-consumers-ssot.md`.

## Context

Skills reach a consumer repo through `scripts/vnx_init.py::bootstrap_skills` (line 201): it rsync/copytree-copies the shipped fabric skills (`VNX_HOME/skills`, the SSOT) into `.claude/skills/` and the multi-provider dirs `.agents/skills` (codex) + `.gemini/skills` (gemini). Those consumer directories are generated install artifacts, not hand-authored source — with one exception uncovered during ratification (see below).

**The drift cause, verified (`vnx_init.py:214-215`):** if `.claude/skills` already exists as a directory, `bootstrap_skills` returns `SKIP` ("Keeping existing skills dir"). Once a consumer commits the copied skills, `vnx init` never refreshes them again — the tracked copy freezes at whatever version it was committed, and drifts from canon on every subsequent fabric change. This is not skills-only: `bootstrap_terminals` (line 294) and the `config` step (line 181) skip existing too, so the same freeze bug spans every bucket-A step (see §2).

This surfaced as a live incident: SEOcrawler_v2's T0 escalated a drift between `.agents/skills` (469 tracked files) and `.claude/skills` (543 tracked files) — the two copies had diverged from canon and from each other, and a skill README had regressed from "Evolution Library v1.0.0" to "Skills (Shipped)" (−122 lines). SEOcrawler held the drift pending this ruling.

**Reference model — roles:** roles are tracked in the consumer (as `CLAUDE.md`/`AGENTS.md`/`GEMINI.md`) and kept on canon by `vnx role sync --apply`. The fleet already has a tracked-plus-synced precedent for one class of fabric artifact — the question this ADR settles is which model applies to which artifact class.

## Decision

### 1. Skills stay Model A (generated install-artifact) — corrected for the mixed-dir case

Skills are treated as a dependency, not vendored source: generated, ~29 skills / ~500 files, pure fabric behavior, regenerated on every fabric change. Vendoring that as committed files that silently freeze on first commit is the `node_modules`-in-git antipattern — exactly what produced the SEOcrawler drift.

**Correction (SEO T0 verification, ratified same day):** the initial ratification assumed `.claude/skills` was pure fabric content. SEOcrawler T0's audit falsified that — of 94 skills present, 56 were genuine project-authored skills (SEO/marketing/CRO/dev toolset, actively loaded), 24 were canon, 14 were junk. Claude Code loads skills only from `.claude/skills` — a `.claude/skills-local/` is not wired into the loader, so relocating project skills there makes them dark. Wholesale gitignore of `.claude/skills` was therefore wrong: it would either drop the 56 project skills from git or clobber them on regenerate.

The corrected rule (the "A\* mixed-dir" case):

- `.claude/skills` stays a **mixed, tracked** directory. Project-authored skills live there and stay tracked — it is their only loaded home. There is no `skills-local`.
- A canon manifest (`scripts/lib/vnx_skills.py`, the #1070 skills-manifest, ~26 canon names) distinguishes canon entries from project entries.
- Canon skills are refreshed **per-skill** against that manifest — `bootstrap_skills` is fixed to per-skill overwrite, not dir-level skip-if-exists or wholesale delete. Only manifest-listed names are touched; the 56 project skills are never modified. Gitignore, if applied at all, is per-canon-entry, never the whole directory.
- Junk entries are pruned by the consumer directly (safe, not fabric-owned).

### 2. Roles stay Model B (tracked + `vnx role sync`)

Roles are small, human-read, and are the terminal's identity — they belong in the repo, synced to canon via `vnx role sync --apply` rather than materialized fresh. This is unchanged by this ADR; it is the reference precedent §1's per-entry correction generalizes from.

### 3. The A/B/C fabric-artifact taxonomy

Every fabric-managed path in a consumer classifies into one of three buckets, grounded in `vnx_init.py`. A canonical fabric-artifact manifest tags each path and drives both `.gitignore` generation and `vnx init`/`update` bootstrap in consumers.

| Path | Bucket | Source in `vnx_init.py` | Treatment |
|---|---|---|---|
| `.claude/skills`, `.agents/skills`, `.gemini/skills` | **A\* — mixed loaded dir** | `bootstrap_skills` | per-skill manifest refresh, never wholesale gitignore (§1) |
| `.claude/terminals/*` (T0-T3, T-MANAGER) | **A — generated** | `bootstrap_terminals` | gitignore + bootstrap; don't materialize unused lanes |
| `.claude/hooks/sessionstart.sh` | **A — generated** | `bootstrap_hooks` | gitignore + bootstrap |
| `AGENTS.md`, `GEMINI.md` | **A — generated mirror** | `generate_tri_files` | gitignore + regenerate (derivatives of `CLAUDE.md`, not tracked roles) |
| root `CLAUDE.md` | **B — project-authored source** | — (the source the tri-files mirror) | tracked |
| `ROADMAP.yaml` | **B — project-authored input** | — (no init step; tracks-DB is its projection) | tracked (project's own roadmap; remove if it is a leaked vnx-orch copy) |
| `plans/VNX_NEXT_LEVEL_PLAN.md` | **C — fabric-internal** | — | do not propagate to consumers; remove if present |

Bucket A and A\* share the skip-if-exists freeze bug (`vnx_init.py:214-215` for skills, line 294 for terminals, line 181 for config); the refresh fix applies to every bucket-A step, not just `bootstrap_skills`. Bucket A\* additionally requires the per-entry manifest discipline of §1 rather than a directory-level fix.

### 4. Consumer alignment, per bucket

- **Bucket A / A\*:** for pure-A paths, `git rm --cached` + gitignore, then re-materialize via `vnx init`/`vnx update` once the refresh fix lands. For A\* (skills), never wholesale-remove — apply the per-skill manifest refresh of §1 and leave project-authored entries untouched.
- **Bucket B:** keep tracked; if a file is actually a leaked fabric copy rather than project-authored, replace with the project's own or remove it.
- **Bucket C:** remove from the consumer; the fabric stops shipping it there.

### 5. Generalization

A bucket-A path that mixes fabric and project content in one loaded dir cannot be wholesale-gitignored — it needs per-entry manifest handling, as skills do. Every other bucket-A item (terminals, hooks, tri-files) must be verified pure rather than assumed pure; that unverified assumption is exactly what produced the §1 correction.

## Consequences

- **Positive:** one SSOT ruling for fabric-managed paths across the fleet; the skip-if-exists freeze fixed everywhere it recurs; a clear tracked-vs-generated line between roles (Model B), pure-generated artifacts (Model A), and mixed loaded dirs (Model A\*); SEOcrawler, Sales-Copilot, and Mission Control converge instead of each freezing its own copy.
- **Negative / cost:** Model A trades in-repo visibility for zero-drift on pure-generated buckets; Model A\* requires the fabric to maintain and consult a canon manifest (`vnx_skills.py`) rather than a simple directory operation; either requires a fabric-code change (`bootstrap_skills`, `bootstrap_terminals`, `config`) plus a per-consumer migration.
- **Governance clarity:** this ADR was ratified by direct operator decision because the plan-gate panel was degraded (opus seat down) at the time; the panel gate remains the default path for future ADRs once that seat is restored.

## Rollout

Follow-through, tracked under `skills-consumer-install-artifact` (broadened to the full A/B/C manifest):

1. Fix skip-if-exists across every bucket-A `bootstrap_*` step in `scripts/vnx_init.py`, and switch `bootstrap_skills` to the per-skill manifest refresh of §1 rather than a directory-level operation.
2. Generate each consumer's `.gitignore` and bootstrap behavior from the fabric-artifact manifest (buckets A/A\*/B/C), rather than hand-maintained per-consumer rules.
3. Stop propagating bucket-C paths to consumers.
4. SEOcrawler_v2 T0 aligns per §4: verify its 94 skills against the canon manifest (56 project-authored stay tracked, 24 canon entries sync per-skill, 14 junk entries are pruned), then clear the held drift.

## Related

- `claudedocs/2026-07-14-ADR-DRAFT-skills-in-consumers-ssot.md` — the original draft (Model A recommendation, then the A/B/C extension, then the §11 mixed-dir correction), restated and brought current here.
- `scripts/lib/vnx_skills.py` — the canon skills manifest (#1070) that A\* per-skill refresh reads.
- `scripts/vnx_init.py` — `bootstrap_skills` (skip-if-exists at line 214-215), `bootstrap_terminals` (line 294), `config` (line 181): the shared freeze bug this ADR requires fixed across all bucket-A steps.

## Open Items

- The skip-if-exists fix (bucket-A refresh + bucket-A\* per-skill manifest refresh) is not yet landed; tracked separately from this ADR under `skills-consumer-install-artifact`.
- SEOcrawler_v2's per-skill classification (56 project / 24 canon / 14 junk) is the audit result at ratification time; the canon manifest is the authority for any future reclassification, not this snapshot.
