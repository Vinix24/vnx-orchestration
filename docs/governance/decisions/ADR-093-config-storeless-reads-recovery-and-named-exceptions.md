# ADR-093 - Config: reads without a store, recovery of the global file, and named exceptions

**Status:** Proposed 2026-11-20 (track `config-een-oppervlak`, dispatch `dlv-eda94b3d4f5c`). Amends ADR-087 decision 5 by cross-reference; ADR-087 stays unchanged (ADR-079 § 83).
**Date:** 2026-11-20
**Decided by:** T0, on the plan-gate of 2026-11-18 on plan version 5 (round 8: codex REVISE with five points, opus REVISE with three, tiebreaker deepseek-v4-pro START with one mandatory change). Plan version 6 carries out that change; this ADR records the contracts it adds.
**Cross-refs:** ADR-026 (one store per project), ADR-044 (the provider switch file, the write pattern of the global file), ADR-079 (§ 83, existing ADRs are frozen), ADR-087 (one configuration surface: the global layer, the five-layer chain, the enforcement profile); plan `claudedocs/specs/config-een-oppervlak/plan.md` (gitignored, not in the repo), § V3.1, V3.2, V5.3, V5.4, V5.7, V6.1, V6.2, V6.3, V6.6 and V6.7; deliverables PR-Q3a, PR-Q3c, PR-Q3d, G1b, N8, PR-9a and PR-9c.

All file and line citations below are measured on `origin/main` at `d08a051a2`. Each decision separates **today** (built, with a citation) from **decided** (not yet built, with its owning deliverable).

## Context

ADR-087 fixes the global layer, the precedence chain and the enforcement profile. Plan versions 5 and 6 added four binding contracts that fall outside it:

- **The bootstrap needs a profile before the store exists.** `project_root._run_explicit_data_dir_guard` runs `data_dir_guard` while paths are still being resolved (`scripts/lib/project_root.py:64-93`, called at `scripts/lib/project_root.py:110`). ADR-087 decision 5 lets that first phase abort on an `enforced` from the layers without a store. The round-8 tiebreaker rejected that: a project row ranks above `global_config`, `env` and `default`, and it has not been read yet, so an abort there lets a lower layer overrule a higher one.
- **`vnx migrate` reads policy on the store it is about to repair.** The FK preflight runs after the tenant gate and before the first write (`vnx_cli/commands/migrate.py:127-140`); its switch is still a bare env read (`scripts/lib/store_integrity.py:59-61`). Once that switch reads the profile through the full chain, an unreadable store reads as `enforced` (ADR-087 decision 7) and the migration aborts on exactly the store it must fix.
- **A hard stop can leave the global audit trail lying.** `config_global._write` appends the audit line with `fsync` and only then replaces the file (`scripts/lib/config_global.py:151-154`). A failed replace gets an `abort` line (`scripts/lib/config_global.py:155-159`), but a kill -9 or power loss between the two leaves an audit line for a change that never landed, with no `abort`. The first plan answer (V5.7) compared file values with `old` and `new`; codex showed that a later hand edit back to `old` then reads as "never landed".
- **One override name is not the override layer.** `VNX_OVERRIDE_<BARE>` is the per-process brake of every registry key (`scripts/lib/config_registry.py:1004-1006`). `VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET` looks like one, but it is a boolean bypass (`scripts/lib/paid_lane_budget.py:98`, `scripts/lib/paid_lane_budget.py:436-442`). Folding it into the chain would turn `=1` into a ceiling of $1 or make the bypass vanish silently.

## Decision

**1. One reader without a store, and two phases for `data_dir_guard`.**

*Reader.* `enforcement_profile.enforced_storeless(key)` is the only read of the profile without a store. It calls `config_registry.resolve_with_source(key, use_default_resolver=False, strict_resolver=True, layers=("override", "global_config", "env", "default"))` and returns `(value, source)`; an unreadable global file gives `global_config_error`.

- Today: built by PR-Q3a (`scripts/lib/enforcement_profile.py:19`, `scripts/lib/enforcement_profile.py:57-60`). The module has no module-level `config_runtime` import; only `read()` imports it inside the function (`scripts/lib/enforcement_profile.py:63-68`). The layer subset is validated against `CHAIN_LAYERS` (`scripts/lib/config_registry.py:947`, `scripts/lib/config_registry.py:957-966`).
- There are exactly two callers: phase 1 of `data_dir_guard` (PR-Q3c) and the migration path (decision 2, PR-Q3d). A third storeless reader is a defect.
- Decided, PR-9c: a test imports `enforcement_profile`, `config_registry` and `config_global` each in a clean subprocess and fails if `config_runtime` is in `sys.modules`.

*Phase 1, during path resolution: record, never abort.* `data_dir_guard` reads the profile with `enforced_storeless` and records its finding. It never aborts, whatever the storeless layers say, because the project row, which ranks higher, is not read yet. **This replaces the sentence in ADR-087 decision 5 "Geven die `enforced`, dan breekt hij meteen af".**

*Phase 2, at the end of every `autowire`, successful or not: decide.* `config_runtime.autowire` calls `data_dir_guard.enforce_recorded(profile)` with the profile it determined itself:

- store wired: the full chain;
- no store (`store_missing`, `resolver_failed`, `no_project`): the layers and the ERROR or WARNING of ADR-087 decision 7.

`enforce_recorded` aborts on `enforced` together with a recorded finding. This replaces "na een geslaagde `autowire`" and "die het volledige profiel leest" in ADR-087 decision 5: phase 2 also runs after a failed `autowire`, and it receives the profile as an argument.

- `data_dir_guard.py` never imports `config_runtime`. The whole file is a bootstrap file, so PR-9c checks it per file (V6.6).
- An entry point that resolves paths without `autowire` never reaches phase 2. PR-Q3c lists every Python entry point that uses `project_root` or `vnx_paths` without `autowire`; each calls `enforce_recorded(enforced_storeless(...))` itself and gets a test (finding plus `env` `enforced` aborts).
- Precedence, tested in PR-Q3c: `env` `enforced` with project row `shadow` does not abort in phase 1 and follows the project row in phase 2; `env` `enforced` with no store aborts in phase 2; override `enforced` aborts in phase 2 even with project row `shadow`.
- Today: not built. `data_dir_guard.py` imports only `project_root` and `vnx_ids` (`scripts/lib/data_dir_guard.py:19-21`), still reads its own `VNX_DATA_DIR_GUARD` switch (`scripts/lib/data_dir_guard.py:28-32`) and raises in the path-resolution call when that switch is `enforce` (`scripts/lib/data_dir_guard.py:103-104`). `autowire` records its cause through `_failed` (`scripts/lib/config_runtime.py:53`, `scripts/lib/config_runtime.py:109-155`) but calls no guard. Owner: PR-Q3c.

Reason: the store needs the paths, so the paths cannot depend on the store; and a lower layer must never decide before a higher one has been read. Recording first and deciding once the full answer is known keeps both.

**2. The migration applies no stored profile row.** While `vnx migrate` runs, `VNX_MIGRATE_STRICT_FK` reads the profile only through `enforced_storeless`, the same reader as phase 1. It never reads the store it migrates.

- If a project row for the profile exists, the migration does not apply it. One WARNING per process names the key and the value used.
- `VNX_INJECTION_RECONSTRUCT_STRICT` runs after `autowire` and keeps the full chain of ADR-087 decision 7.
- Tests in PR-Q3d: an unreadable store and no strict storeless layer gives advice, no abort; `env` `enforced` aborts before the first change; a project row `enforced` in a readable store gives the WARNING and no abort.
- Today: not built. The switch is `env.get("VNX_MIGRATE_STRICT_FK") == "1"` (`scripts/lib/store_integrity.py:59-61`), called before any write (`vnx_cli/commands/migrate.py:136-138`); `VNX_INJECTION_RECONSTRUCT_STRICT` is a bare env read (`scripts/lib/final_prompt_integrity.py:219`). Owner: PR-Q3d, after PR-Q3c.

Reason: a migration exists to repair a store. A policy read from that store turns a broken store into a refusal to repair it.

*Boundary, accepted:* a stored `enforced` for the profile does not make one migration strict. Whoever wants a strict migration sets it in a storeless layer (override, global file or env).

**3. Recovery of the global file on a transaction id, and refusal after a hand edit.**

- Every write gets a `txn` (uuid4). Its audit line carries `txn` and `new_sha256`, the sha256 of the whole document about to be written. The document carries `last_txn`.
- A successful write appends a `commit` line with its `txn` after the replace.
- Recovery runs under the lock, before the document is read, on the last audit line with a `txn` and no later `commit` or `abort`:
  - `last_txn` in the file is that `txn` and the file's sha256 is `new_sha256`: the change landed; write `commit` for that `txn`;
  - `last_txn` is the `txn` of the line before it (or absent, on the first write): the change never landed; write `abort` with `error: "crash_recovery: replace never landed"`;
  - any other case: the file was changed outside `config_global`. Every write refuses with an error that names the file, the `txn` and the audit line, until the operator resolves it through `vnx config` (PR-10b names the command). Reads continue.
- Recovery is idempotent: a second round appends nothing.
- Tests in G1b: a crash after the audit line and before the replace gives `abort`; a crash after the replace and before `commit` gives `commit`; a hand edit back to the old value gives the refusal and no `abort`; a second recovery round appends nothing.
- Today: not built. Operations are `set`, `unset` and `abort` (`scripts/lib/config_global.py:30`); the audit line carries `old` and `new` but no `txn` or hash (`scripts/lib/config_global.py:151-152`); there is no `commit` line and no recovery. Owner: G1b, after this ADR.

Reason: equal values do not prove where a value came from. A transaction id and a document hash do: they tell "my write landed", "my write never landed" and "someone else wrote" apart. Refusing on the third case keeps the audit trail of a `requires_approval` key (an enforcement path) truthful instead of guessing.

*Boundary, accepted:* a hand edit blocks every global write until the operator resolves it. That is loud by design.

**4. `VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET` is a named exception to `VNX_OVERRIDE_<BARE>`.** It is a boolean bypass, not a registry key and not the override layer of one.

- `VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET=1` lets an exhausted ceiling through with one WARNING naming both amounts. Any other value does nothing.
- `VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET_USD=<amount>` is the override layer of the key `VNX_PAID_LANE_DAILY_BUDGET_USD` once that key is registered and read through the chain: it sets the ceiling.
- The bypass stays an env read in `paid_lane_budget.py`. It goes on the allowlist of PR-9a with the reason "operator bypass, one window", in the same class as `VNX_OVERRIDE_WORKER_CLAUDE`.
- No prefix rule may treat a `VNX_OVERRIDE_*` name as the override layer without a registry key behind it. N8 merges nothing and updates `docs/core/LOCKS_AND_RELEASES.md` with both names, their meaning and an example.
- Tests in N8: ceiling exhausted with bypass `1` gives no error and one WARNING; bypass `true` or `0` gives `PaidLaneBudgetExceededError`; `..._USD=1` without bypass gives a $1 ceiling and a $2 spend fails.
- Today: the bypass is built (`scripts/lib/paid_lane_budget.py:98`, `scripts/lib/paid_lane_budget.py:436-442`). `VNX_PAID_LANE_DAILY_BUDGET_USD` is **not** a registry key on main: `get_daily_budget_usd` reads it straight from env (`scripts/lib/paid_lane_budget.py:97`, `scripts/lib/paid_lane_budget.py:182-189`), so `VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET_USD` has no effect today. Plan V5.3 cites `config_registry.py:881` for the key; that line is the generic override read, not a row for it. Registration and the switch to the chain belong to the config track, not to this ADR.

Reason: one prefix with two meanings is a trap. A named exception keeps the bypass exactly as it works today and keeps the amount where every other key's override lives.

## Consequences

- PR-Q3c, PR-Q3d and G1b build against this ADR and cite it; each must come after it (V6.7).
- ADR-087 decision 5 is amended as stated in decision 1; its other decisions are unchanged.
- The bootstrap list of PR-9c holds `scripts/lib/data_dir_guard.py` as a whole file.
- A storeless policy read now has one name. A new caller of `resolve_with_source(..., layers=STORELESS_LAYERS)` outside `enforcement_profile` is a review finding.
- Global writes gain one `commit` line each and two fields per audit line.

## Rejected alternatives

- **Phase 1 aborts on a storeless `enforced` (ADR-087 decision 5 as written).** Lets `env` overrule a project row `shadow`.
- **`data_dir_guard` reads the profile through `config_runtime`.** Creates the loop `autowire` → `vnx_paths` → `project_root` → `data_dir_guard` → `config_runtime`.
- **Recovery by comparing values with `old` and `new` (plan V5.7).** Marks a successful change followed by a hand edit back to `old` as never landed.
- **Merge the budget bypass into `..._USD`.** Turns `=1` into a $1 ceiling, or removes the bypass silently.
