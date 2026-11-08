# ADR-059 - The native materialisation of an agent's skill set (phase 2), and the evidence that proves it

**Status:** Accepted 08-11-2026 (T0, track `agent-skill-discovery-binding`, R1).
**Date:** 2026-11-08
**Decided by:** T0, on the plan `claudedocs/2026-11-07-naar-1-9/plannen/agent-skill-discovery-binding.md` (revision 2, decisions 1 to 22) and the plan-gate PASS of 08-11-2026. ADR-059 was free on `origin/main` (`ls docs/governance/decisions/` showed ADR-058 as the highest).
**Cross-refs:** ADR-005 (ledger first), ADR-007 (`project_id` on every central row), ADR-022 (skill delivery per provider), ADR-031 (agent folders, `agents/<role>/config.yaml` as SSOT), ADR-048 (skill delivery through the prompt), ADR-050 (agent layout phase 1), ADR-056 (autonomy measured), ADR-057 (provider-agnostic permission policy), ADR-058 (T0 playbook source).

The plan called this ADR 056 (its decision 22). 056 went to `zelfstandigheid-meten`, 057 to `provider-agnostic-permissions` and 058 to the skills-port SP-0 before this row fired, so by T0 decision of 08-11 it is 059. Gate-profile P9 moves to 060.

## Context

ADR-050 split the work in two. Phase 1 made `agents/<role>/config.yaml:skills` the declared set, materialises a shadow copy under `.vnx-agent-skills/` that no CLI discovers, and measures in the receipt. Phase 2 is the half that makes a provider ask for the declared set natively. ADR-050 section 4 folded the old track's DONE ("claude and codex discover the skills natively") into phase 2, and section 7 named what phase 2 contains: `on`, the discovered copy and the codex layout `.agents/skills`. This ADR is the contract that every later row (R2 to R8) builds against. It builds nothing itself.

Measured on `origin/main`, the problems this contract resolves:

- **The declared set has no reader.** `AgentConfig` parses `skills` (`scripts/lib/agent_resolver.py:254`), but no `agents/*/config.yaml` sets the key and no dispatch consumes it.
- **Discovery is cwd-dependent and provider-specific.** The worker's cwd is the isolation worktree. It carries the tracked `.claude/skills/` (27 skills) but `.agents/skills` has 0 entries. A codex dispatch therefore receives its skill only as prompt text (ADR-022), and no receipt says what the dispatch could see.
- **`on` does not exist.** `VNX_AGENT_LAYOUT=on` reads as `off` with one WARNING (`scripts/lib/agent_layout.py:41`).
- **The claude destination is not ignored.** `.claude/skills/<new>/SKILL.md` is not matched by any ignore rule (`git check-ignore` exit 1). Only `references/`, `scripts/`, `template.md`, `@*.md` and `skills.yaml` are ignored under `.claude/skills`. A `.git/info/exclude` route is out: a linked worktree shares the exclude of the main repository, so it is not per worktree.
- **A symlink does not work for claude.** `bin/vnx:1327-1328` states that Claude Code does not follow symlinks for skill discovery. The goal text of the old track ("materialise as symlinks") is refuted for claude; ADR-050 already rejected the alternative.
- **The codex routes have no common input.** `spawn_codex` (`scripts/lib/provider_spawns/codex_spawn.py:709-724`) takes `cwd`, `dispatch_id` and `terminal_id`, but no role, state directory or project. Its four callers each hold a different subset of those.

Everything in this ADR is deterministic: a declared list, a copy, `git check-ignore` as a gate, an ownership marker, a manifest of path, length and sha256, a closed set of statuses and reasons, an identity check and a fixed precedence. No model call sits in the dispatch path, so no model-specific safety net is needed. The only model-backed parts are the measurements (R7, R8): "the CLI saw X" is a property of the CLI, which only the CLI can prove. The T0 runs those, never a builder (ADR-050 section 8).

## Decision

### 1. Scope: phase-2 materialisation plus evidence; the door stays out (plan decision 1)

This track delivers the discovered copy and the proof of it. The binding (the set in the permit digest, a door refusal for a dispatch outside its set, deterministic role validation) belongs to `agents-als-map-fase-2` and to `deur-afgedwongen`. Reason: ADR-050 section 7 splits phase 2 in (a) the copy and (b) the binding, and the permit-check is a separate sprint decision. This ADR delivers the evidence both need. "Closed set" in this ADR therefore means "every declared skill is visible", never "nothing else is visible".

### 2. The SSOT stays `agents/<role>/config.yaml:skills` (decision 2)

There is no `skills:` axis in `.vnx/worker_permissions.yaml`. Reason: ADR-050 section 3, and a second axis would be a second truth. This ADR extends ADR-050 and contradicts none of its sections. It supersedes exactly three phase-1 rules, each by section: section 1 (`on` reads as `off` with a WARNING) is superseded from R6 on, by decision 4 below; section 2 (why `on` is rejected) lapses with it; section 5, rule "Destination" (never in `.claude/skills/` or `.agents/skills/`) is superseded for the discovered copy only, by decisions 5 and 6 below. The shadow copy under `.vnx-agent-skills/` keeps every rule of ADR-050 section 5.

### 3. Copy, never a per-skill symlink (decision 3)

Each skill is copied as a regular file tree. Reason: `bin/vnx:1327-1328`, measured, and ADR-050's rejected alternative. The whole-directory `.claude` symlink of the T0 pane (`scripts/commands/start.sh`) stays; it is not a skill symlink. A test asserts `os.path.islink` is false on a materialised skill.

### 4. `on` becomes a working value only in R6 (decisions 4 and 20)

Until R6, `on` reads as `off` with one WARNING. That is the phase-1 behaviour, not a silent downgrade, and it promises nothing. R3 and R4 test the native branch with `layout_mode` patched to `on`. R6 activates `on` after claude (R3), codex (R4) and the receipt (R5) exist, and proves the chain from the environment variable to the receipt. From R6 the default stays `off` and `on` is set per project (ADR-050 section 1). Reason: the gate of 08-11 found that setting `on` before anything is wired lets it claim a capability nothing delivers.

### 5. The native destinations, inside the worktree (decision 5)

| Provider | Destination |
|---|---|
| claude | `<worktree>/.claude/skills/<slug>/` |
| codex | `<worktree>/.agents/skills/<slug>/` |

These are the paths the CLIs discover themselves (ADR-022). The worktree disappears with the dispatch, so nothing leaks to the main checkout, `~/.claude` or `~/.codex`. A destination table, not code: gemini (`.gemini/skills`) can be added without a contract change. The isolation gate is deterministic: `worktree.resolve()` must not equal the consumer project root (`dispatch_worktree_isolation.resolve_consumer_project_root()`) and must not lie under `~/.claude` or `~/.codex`; otherwise `skipped` with `path_outside_worktree`.

### 6. A destination is ignored before anything is written; the authored source moves (decisions 6 and 16)

R2 adds `.claude/skills/*/SKILL.md` to the tracked `.gitignore`. The consequences are fixed:

- The 27 tracked `SKILL.md` files stay tracked (an ignore rule does not touch tracked files). ADR-048 section 2, RS-5 (the two copies of a skill byte-equal), is unchanged for those 27.
- `skills/<slug>/SKILL.md` (25 tracked) is the authored source and is never ignored. It is the first source in the ADR-048 resolution order, so a shared skill is authored there and `git add` works.
- A new `.claude/skills/<slug>/SKILL.md` is ignored and needs `git add -f`. The 2 slugs that exist only under `.claude/skills/` (`control-centre`, `panel`) stay tracked; a new slug without a `skills/` counterpart is the exception that needs `git add -f`.
- This is a contract change. The comment above the install-artifact block in `.gitignore` (`.gitignore:129-131`) still names `.claude/skills/*/SKILL.md` as the authored source; it is older than `skills/` and R2 rewrites it.

Two gates stay on every write: if `<dest>/SKILL.md` already exists (tracked or not) the status is `already_native` and nothing is overwritten, and nothing under `.claude/skills` is ever deleted. In the fabric repo itself every declared skill is tracked, so `on` there reports `already_native` per slug and copies nothing; the gain there is the measurement. The real copy happens on a consumer or a central agent folder. `.agents/` is already ignored (`.gitignore:142`).

### 7. Codex deletes only what carries its own marker (decision 7)

The materialiser writes `.vnx-managed.json` in each skill folder it creates (`dispatch_id`, `role`, `slug`, `sha256`, `schema`). It may delete only a folder that carries that marker with the `dispatch_id` of this run. A folder without a marker is foreign: it stays, and the run ends `partial` with reason `not_owned:<slug>`. A fresh worktree (today: 0 entries) yields exactly the declared set; a polluted worktree never loses data and never reports a silent "cleaned up". Reason: `.gitignore:142` says git does not track the folder, not that the fabric owns it. A lost marker is the safe side: the folder stays and the run reports `partial`.

### 8. The evidence per provider is a measurement (decisions 8, 14 and 15)

- **claude:** the session listing (`context_manifest.extract_skills`) must contain the declared set ("declared is a subset of measured"). The rest of the listing is the preserved catalogue and is counted per origin in the manifest (`repo` 27, `user` 25, `synced`, `namespaced`, `other`). "Only the declared set" is the door (phase 2 binding) and is not claimed here.
- **codex:** two cells, both decisive. C3a is deterministic: in a fresh worktree `ls .agents/skills` is exactly the declared set. C3b asks the CLI: it names the skill, with a negative control on a skill that is not in the tree. C3a is necessary and not sufficient, because a directory listing proves our placement and not the discovery.

### 9. No new receipt field; `context_manifest` gets an additive `source` (decision 9)

The receipt pointer stays `{schema, status, sha256}` (ADR-050 section 6). Only the manifest object gains `source` (`session` | `materialized`); absent reads as `session`. Reason: `context_manifest` is the field ADR-050 already reserves, a second field would be double evidence, and `receipt_schema.py` is busy with other tracks. This track does not touch `receipt_schema.py`.

### 10. The fail-closed precedence in the receipt (decision 10)

A session capture with `status` in (`ok`, `partial`) wins (`source: session`). Otherwise a valid materialisation record wins (`source: materialized`). Otherwise `unavailable` with the reason. A missing, `None`, unparseable or unknown record never reads as `ok`, and the receipt never names a hash that is not in the source. The record test is decision 13's record contract.

### 11. The closed status and reason sets (decision 11)

`status` is one of `ok`, `partial`, `error`, `skipped`. `reason` is one of: `no_role`, `no_declared_set`, `no_cwd`, `no_state_dir`, `no_dispatch_id`, `no_project_id`, `not_isolated`, `not_ignored`, `already_native`, `source_missing`, `copy_failed:<Class>`, `not_owned:<slug>`, `path_outside_worktree`. An open string cannot be tested, and an unknown value stays non-`ok`.

### 12. Explicitly out of scope (decision 12)

- Kimi: no fabric code uses `--skills-dir`; delivery stays prompt injection (open item after 1.9).
- Gemini: not in the DONE.
- The harness lanes that start `claude --bare` (`scripts/lib/provider_spawns/harness_config_dir.py:63`). Whether `--bare` disables skill discovery is unmeasured; cell C2 measures it. If it does, `on` is inert on those lanes and the DONE is measured on the non-`--bare` claude lane. `--bare` itself is not touched.
- The `bin/vnx` bootstrap parity (the provider sync skipped at `bin/vnx:1315-1317`) and the stale comment at `bin/vnx:1327-1330`: owned by the skills-port track (SP-6).
- The door and the permit digest (decision 1).
- Native materialisation does not replace the ADR-022/048 prompt seam. The two mechanisms run side by side: the prompt carries one bound slug (DISPATCH_RULES section 15), the materialisation carries the role's declared set. The rollback is `VNX_AGENT_LAYOUT=off`, which leaves the receipt byte-equal to today's.

### 13. The public interface and the input contract per caller (decisions 13 and 17)

One function:

`materialize_skill_set(role, *, worktree, provider, mode, dispatch_id, state_dir, project_id=None) -> dict`

It reads the set through `agent_resolver.resolve_agent`, copies by decisions 5 to 7, writes the record by decision 19, and always fails open: one WARNING, `status: error|skipped` with a reason, and the dispatch continues. It never reads the environment; everything comes from the caller. `spawn_codex` gains three keyword-only parameters (`role`, `state_dir`, `project_id`, default `None`) that all four callers pass, so the spawner never reads ambient env and the outcome does not differ per route. A route that lacks a required field writes a `skipped` record with the reason and does nothing.

| Caller | cwd / worktree | role | state_dir | dispatch_id | project_id | On missing field |
|---|---|---|---|---|---|---|
| `ClaudeSubprocessAdapter.run` (R3) | the `cwd` argument | `spec.role` | `spec.state_dir` | `spec.dispatch_id` | `project_scope.resolve_stamp_project_id()` | `skipped` + `no_cwd`/`no_role`/`no_state_dir`/`no_project_id` |
| `CodexAdapter.run` (`scripts/lib/envelope_adapters_claude.py:52`, R4) | the `cwd` argument | `spec.role` | `spec.state_dir` | `spec.dispatch_id` | same | same |
| `_run_codex` (`scripts/lib/envelope_adapters_provider.py:238`, R4) | the `cwd` argument | `plan.role` (`scripts/lib/dispatch_plan.py:170`) | `provider_dispatch._resolve_state_dir()` (the plan has none) | `plan.dispatch_id` | `plan.project_id` (`scripts/lib/dispatch_plan.py:149`) | `skipped` + `no_state_dir` |
| `_dispatch_codex` (`scripts/lib/provider_dispatch.py:2962`, R4) | `worker_cwd`, the isolation worktree | `args.role` | `_resolve_state_dir()` | `args.dispatch_id` | `resolve_stamp_project_id(db_path=...)` | `skipped` + `not_isolated` when `worker_cwd` is the project root |
| `CodexAdapter.complete` (`scripts/lib/adapters/codex_adapter.py:125`, R4) | none | `context["role"]` | none | `dispatch_id` | none | `skipped` + `no_cwd`; no materialisation until a later track gives this route a worktree |

### 14. Which row is which severity (decision 18)

The codex row (R4) is classed `high`, gets every review seat and the second seat from the start. `scripts/lib/provider_spawns/` is a security prefix and the row deletes files; the review weight follows the behaviour, not the line count. R5 (receipt format) is `high` as well, and R2 (the ignore rule travels with every release) is raised to `high` in its PR body.

### 15. The record contract (decision 19)

- **Path.** One file per (dispatch, provider, attempt): `<state_dir>/agent_skill_materializations/<dispatch_id>/<provider>.attempt-<n>.json`, with `n` chosen through `O_CREAT|O_EXCL` (never overwrite; a second attempt writes a new file). `state_dir` is the value the receipt writer already receives; the reader never reads the environment.
- **Identity.** The record carries `schema`, `status`, `reason`, `dispatch_id`, `project_id`, `role`, `provider`, `worktree`, `session_id` (may be `null`), `attempt`, `captured_at`, `skills[{slug, dest, sha256, bytes, ownership}]` and `manifest_sha256`. It carries paths, lengths and hashes only: never content, never an e-mail address.
- **Refusals, never `ok`.** Missing file (`no_record`), unparseable (`record_malformed`), `dispatch_id` differs from the receipt (`identity_mismatch`), `project_id` missing or different (`project_mismatch`), `session_id` neither `null` nor equal (`session_mismatch`), unknown `status` (`unknown_status`).
- **Attempt safety.** The reader picks the highest `attempt` that passes the identity check. A tie cannot occur (`O_EXCL`). A record of another dispatch is never read, by path and by field.
- **Status mapping.** `ok` to `ok`, `partial` to `partial`, `skipped` to `unavailable` plus the reason, `error` to `error` plus the reason; anything else to `unavailable` plus the reason.
- **Cleanup.** The record is small and grows per attempt; its cleanup belongs to `worktree-opruiming-structureel`.

### 16. The probe and the measurement document are two PRs (decision 21)

R7 is the probe (`scripts/probe_agent_skill_discovery.sh`): four cells (C1 claude `on`, C2 the same under `--bare`, C3a codex placement, C3b codex discovery), each with three outcomes (yes, no, not measured with the reason) and a sha256 of the output. R8 is the document that commits the T0's post-merge outcomes, per cell, with command, output, outcome and sha256. Reason: executable code and documentation in one PR blurs the review and leaves the measurement undocumented until someone updates it by hand. A decisive cell left on "not measured" keeps the track open.

## Consequences

- **With the flag off nothing changes.** No copy, no record, no receipt field; a test per row proves it.
- **Two new surfaces get their rules.** The native destinations (`.claude/skills`, `.agents/skills`) and the record directory `agent_skill_materializations/`. A change that loosens a rule here amends this ADR.
- **The authored-source rule changes.** From R2 a new shared skill is authored under `skills/<slug>/SKILL.md`; a new `.claude/skills/<slug>/SKILL.md` needs `git add -f`.
- **Between R3 and R6 the wiring is dead but tested.** Nobody can switch it on until the chain exists.
- **Residual risk.** `--bare` may make `on` inert on the harness lanes (cell C2). A `.claude/skills` copy is invisible to git status by design, so a skill that exists only as an ignored copy never reaches a commit; that is the intent for a generated map.
- **What this ADR does not do.** It builds nothing: no code, config or hook changes. R2 to R8 do that, each with a test that fails on the old code on behaviour (R1 and R8 are docs-only and say so).

## Rejected alternatives

- **Symlinks per skill.** Rejected: Claude Code does not follow them (`bin/vnx:1327-1328`).
- **`on` activated in R2.** Rejected: it would claim a capability before the wiring and the receipt exist (decision 4).
- **A `.git/info/exclude` entry.** Rejected: shared between a linked worktree and the main repository.
- **Overwriting or deleting under `.claude/skills`.** Rejected: it dirties the worktree and is destructive; `already_native` is the gate.
- **Codex deletion by set equality.** Rejected: ownership is not implied by an ignore rule; the marker is.
- **A new receipt field.** Rejected: double evidence; `context_manifest` gets `source` instead.
- **Exact-set evidence for claude.** Rejected: it contradicts the preserved catalogue (27 repo, 25 user); that claim is the door's.
- **The door and the permit digest in this track.** Rejected: they belong to `agents-als-map-fase-2` and `deur-afgedwongen`.
