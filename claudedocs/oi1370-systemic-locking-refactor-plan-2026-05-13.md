# OI-1370 — Systemic Locking Refactor Plan

**Date:** 2026-05-13
**Author:** architect role (dispatch 20260513-oi1370-opus-architect-systemic-locking-plan)
**Status:** DESIGN — no code changes
**Scope:** all writers/migrators of `.vnx-data/state/*.ndjson` (envelope state)
**Background:** PRs #466 round-4 and #475 round-2 both passed scoped review but codex
re-opened OI-1370 each time by finding additional unprotected writer paths. Three
scope-reduced attempts have failed → this is a system-shape problem, not a single-file fix.

---

## 1. Audit — every writer/migrator touching envelope state

NDJSON files in scope: `dispatch_register.ndjson`, `t0_receipts.ndjson` (per-project and
central copies under `~/.vnx-data/<pid>/state/`).

### 1a. Writers that hold sentinel + file lock (CORRECT)
| Site | File | Lock(s) held | Notes |
|---|---|---|---|
| `scripts/lib/dispatch_register.py:205-214` `_write_event_locked` | `dispatch_register.ndjson` | `.state.lock` (sentinel, dir) + `LOCK_EX` on data fd | Canonical primary-path writer. Sentinel taken before open(data). |
| `scripts/lib/dual_writer.py:82-103` `_append_locked` | central NDJSON (any) | `<lock_filename>` sentinel + `LOCK_EX` on data fd | Shared by `append_receipt` central mirror + `dispatch_register` central mirror. Sentinel name is `.state.lock` by default; remapped to `append_receipt.lock` for `t0_receipts.ndjson` via `_LOCK_FILENAME_BY_TARGET`. |
| `scripts/migrate_phase3_envelope.py:117-157` `_restamp_ndjson_inplace` | both NDJSON | `.state.lock` sentinel + caller-supplied lock (`append_receipt.lock` for receipts, else data fd) | Migrator (re-stamper). Holds both locks through `os.replace`. |

### 1b. Writers that hold ONLY a single lock (PARTIAL — race vector)
| Site | File | Lock(s) held | Gap |
|---|---|---|---|
| `scripts/lib/gate_register_emit.py:71-73` `emit_codex_gate_to_register` | `dispatch_register.ndjson` | `LOCK_EX` on data fd only | **No sentinel.** Opens path → may resolve to soon-to-be-unlinked inode while migrator holds `.state.lock`. Once migrator releases data-fd lock and does `os.replace`, this writer appends to a dead inode. |
| `scripts/lib/cleanup_worker_exit.py:343-345` `_append_audit_event_step` | `dispatch_register.ndjson` | `LOCK_EX` on data fd only | Same gap. Direct write path used by both tmux and subprocess adapters at worker exit. |
| `scripts/lib/append_receipt_internals/idempotency.py:137-154` `_write_receipt_under_lock` | `t0_receipts.ndjson` | `append_receipt.lock` sentinel + open data fd (no `LOCK_EX` on data fd) | **Mitigated in practice** because the migrator for receipts uses the same `append_receipt.lock` as its sentinel — so primary writers and the migrator do serialize. Inconsistent though: dispatch_register canonical writer holds *two* locks; receipts holds *one*. |
| `scripts/lib/append_receipt_internals/payload.py:128-134` `_append_receipt_line_locked` | central `t0_receipts.ndjson` | `append_receipt.lock` sentinel only (no data-fd lock) | Same as above; mitigated for migrator-races but inconsistent. |

### 1c. Rewriters that take **NO lock at all** (HIGH-RISK — unblocked race)
| Site | File | Lock(s) held | Risk |
|---|---|---|---|
| `scripts/compact_state.py:215-279` `compact_receipts` | `t0_receipts.ndjson` | none — `read_text` + `_atomic_write_text` (tmp + `os.replace`) | Race with every `_write_receipt_under_lock` writer. Compactor reads file mid-append, drops the partially-written tail or the still-being-appended record. Same loss class as OI-1370 but for receipts. |
| `scripts/backfill_headless_receipts.py:332-372` `_update_ndjson` | `t0_receipts.ndjson` | none — read + write tmp + `tmp.replace(...)` | Same race shape as compact_receipts. Ad-hoc backfill tool; lower frequency, still capable of loss. |

### 1d. Read-only consumers (not writers but listed for completeness)
| Site | Lock |
|---|---|
| `scripts/lib/dispatch_register.py:367-374` `_read_register_locked_per_project` | `LOCK_SH` on data fd. Compatible with the `LOCK_EX` writers in 1a / 1b (file-fd lock). NOT compatible with 1c (no-lock) — reader may observe a truncated tail. |
| All `build_t0_state.py` / `build_feature_plan.py` / `build_project_status.py` consumers | read-text only; rely on writer atomicity guarantees that 1c violates. |

### 1e. Bash-side
`scripts/lib/receipt_processor/rp_lock.sh:7-21` introduces a **third lock primitive**:
`receipt_write.lock` used by `scripts/receipt_processor_v4.sh` for its own processing
flow. This file is NOT shared with the Python writers and does NOT protect
`t0_receipts.ndjson` itself — it gates the receipt-processor pipeline. Documented
to prevent future confusion: `.state.lock`, `append_receipt.lock`, and
`receipt_write.lock` are three different things.

---

## 2. Lock-discipline analysis — the gap

Three independent lock conventions for two data files:

| File | Primary path writer convention | Central mirror convention | Migrator/Rewriter convention | Outlier writers |
|---|---|---|---|---|
| `dispatch_register.ndjson` | `.state.lock` + data-fd `LOCK_EX` | same (`dual_writer`) | `.state.lock` + data-fd `LOCK_EX` (`migrate_phase3_envelope`) | `gate_register_emit`, `cleanup_worker_exit._append_audit_event_step` — **data-fd only** |
| `t0_receipts.ndjson` | `append_receipt.lock` + data fd unlocked | same | `.state.lock` AND `append_receipt.lock` AND data-fd `LOCK_EX` (`migrate_phase3_envelope`) | `compact_state.compact_receipts`, `backfill_headless_receipts._update_ndjson` — **no lock** |

Two structural problems:
1. **Sentinel discipline is asymmetric.** `dispatch_register` standardized on a directory
   sentinel (`.state.lock`) so that any writer + the migrator share a stable lock file
   that survives `os.replace` on the data file. But two writer call sites (`gate_register_emit`,
   `cleanup_worker_exit`) bypass the sentinel — they take only `LOCK_EX` on the data fd,
   which is on the inode the migrator is about to unlink. Every PR-466/-475 round of codex
   findings was a different facet of this same omission.
2. **Receipts has no migrator-safe writer convention codified.** The migrator added the
   sentinel for receipts; primary writers (`_write_receipt_under_lock`,
   `_append_receipt_line_locked`) did not — they only ever take `append_receipt.lock`.
   This *happens* to work because the migrator also takes `append_receipt.lock`, but it
   means `compact_receipts` and `_update_ndjson` (which take neither) silently race the
   primary writers. Same OI-1370 shape, different filename.

---

## 3. Target architecture — choose ONE

### Option A — single sentinel-lock per file (`.<filename>.sentinel.lock`)
Every writer + every migrator takes the per-file sentinel BEFORE opening the data fd.
- **Pros:** localized; preserves per-file lock granularity; minimal change to existing
  callers (just add sentinel hold around the existing data-fd open); cleanly unifies
  the convention.
- **Cons:** still two locks per write (sentinel + data fd for reader/writer
  exclusion); adds a per-file dotfile; needs every call site touched.

### Option B — directory-level lock (`.state-dir.lock`)
Every writer of any `.vnx-data/state/*` file takes the same dir-level sentinel.
- **Pros:** one global truth; one lock file to audit; the migrator vs compactor vs
  writer fight is settled with one primitive; trivial mental model.
- **Cons:** serialises all state writers including unrelated ones (e.g. a `t0_receipts`
  appender now waits on a `dispatch_register` event write); on a busy multi-terminal
  system that's measurable contention. Coarsest possible.

### Option C — lock-free append + tombstone (logical rewrite)
Writers continue to append. Migrator/compactor never `os.replace` — instead writes a
sidecar "tombstone" file listing original line offsets to skip, and an "envelope-stamp"
file listing per-offset overrides. Readers merge live + tombstone + stamps.
- **Pros:** no writer blocks ever; structurally race-free for the canonical OI-1370
  scenario (no inode replace); aligns with the central-DB endgame where appends
  become INSERTs anyway.
- **Cons:** read path complexity explodes (every reader must understand three files
  per logical NDJSON); compactor cost shifts to readers; needs careful tombstone GC;
  invalidates assumptions of every existing reader (~20+ files in `scripts/lib`);
  bigger blast radius than the bug it fixes.

### Recommendation — **Option A**
Pick **per-file sentinel discipline** plus a single shared helper that all writers
and the migrator route through. Reasoning:
- **Correctness:** structurally identical to the proven `dispatch_register._write_event_locked`
  + `dual_writer._append_locked` pattern. We are extending the convention that already
  works, not inventing a new one.
- **Simplicity:** the rule fits in one sentence — "every writer of an NDJSON state
  file calls `state_writer.append_locked(path, record)`". Codex can audit by grepping
  for direct `path.open("a")` on these files; any hit is a finding.
- **Performance:** preserves per-file lock granularity. Option B's dir-wide lock
  contention is a regression for a system that already has high write throughput
  (every dispatch event + every receipt). No measured benefit over Option A.
- **Testability:** parity tests can hammer one file at a time; we don't need to model
  cross-file orderings.
- **Migration safety:** purely additive. Each existing writer that gains the sentinel
  becomes *more* race-safe; nobody becomes less safe at any intermediate step.

Option C is the right *long-term* direction once the central SQLite DB lands (Wave 1
shadow → Wave N cutover already in flight), but it cannot ship before central-DB
because half the readers need rewriting. Option B trades correctness for unnecessary
contention. Option A is the smallest correct fix and is forward-compatible with
Option C (the helper can later swap append-under-sentinel for INSERT into SQLite).

---

## 4. Migration phasing — 4 PRs, each independently mergeable

The phasing order minimizes the window during which the system is partially-protected.
A writer that gains the sentinel is strictly safer than before; a migrator change is the
*last* step because it depends on every writer being safe first.

### PR N1 — introduce `scripts/lib/state_writer.py` helper (no behavior change)
- New module exporting `append_locked(path: Path, record: dict) -> None`.
- Implementation mirrors `dispatch_register._write_event_locked` (sentinel sibling
  `.<filename>.sentinel.lock`, then `LOCK_EX` on the data fd) — but **takes the
  file path so it can be reused across files**.
- Helper consults a small registry mapping `filename -> sentinel_filename` so that
  the historical names (`.state.lock`, `append_receipt.lock`) keep working
  (preserves coexistence with not-yet-migrated callers).
- Zero existing call sites touched. Pure addition. Unit-test the helper directly:
  100 threads × 100 writes each, verify line count == 10000 and every JSON parses.
- **Mergeable:** yes — no caller change.

### PR N2 — migrate `gate_register_emit` and `cleanup_worker_exit` to helper
- Replace the inline open+flock blocks at `gate_register_emit.py:69-73` and
  `cleanup_worker_exit.py:341-345` with a single call into `state_writer.append_locked`.
- Adds a parity test: spawn the canonical migrator + N writers via these two paths
  concurrently, assert no inode-loss.
- These two are the codex round-2 findings — fixing them closes the originally-flagged
  hole without changing the migrator at all.
- **Mergeable:** yes — both paths become *strictly* safer; no other consumer affected.

### PR N3 — migrate `compact_state.compact_receipts` and `backfill_headless_receipts._update_ndjson`
- Both are *rewriters* (read-modify-replace), not pure appenders. They need a
  rewrite primitive: `state_writer.rewrite_locked(path: Path, transform: Callable[[str], str]) -> None`
  added in this PR to the helper. Sentinel + `LOCK_EX` held through the read,
  write-tmp, `os.replace` cycle.
- Replaces the unprotected `read_text → _atomic_write_text / tmp.replace()` pairs.
- Parity test: a tight loop of `_write_receipt_under_lock` writes against a thread
  running `compact_receipts` — pre-fix loses records, post-fix doesn't.
- **Mergeable:** yes — only `compact_state` and `backfill_headless_receipts` change.

### PR N4 — migrate `migrate_phase3_envelope` to helper + retire bespoke sentinel
- `_restamp_ndjson_inplace` calls `state_writer.rewrite_locked` instead of rolling
  its own sentinel+lock code. Removes ~40 lines of duplication. Same lock file
  used (registry maps `dispatch_register.ndjson` → `.state.lock`, `t0_receipts.ndjson`
  → `append_receipt.lock` for backward-compat).
- Optionally normalise the dispatch_register sentinel from `.state.lock` to
  `.dispatch_register.ndjson.sentinel.lock` — but only if grepping confirms no
  out-of-tree shell script grabs `.state.lock` by name. **Defer this rename.**
- Parity test: rerun the OI-1370 concurrency test (canonical `append_event` + migrator)
  with > 1000 events; assert zero loss.
- **Mergeable:** yes — pure refactor, behavior identical, sentinel filename preserved.

### Optional PR N5 — codex enforcement
Add a codex-gate check: any new diff that contains `dispatch_register.ndjson` or
`t0_receipts.ndjson` and a `.open("a"` or `os.replace(` near it but does NOT import
`state_writer` is a blocking finding. Prevents regression by future PRs.

---

## 5. Testing strategy — what makes a concurrency test trustable

### Why earlier tests passed but the bug shipped
PR #466 round-1 had `test_directory_lock_serializes_with_migration` that used
`concurrent.futures.ThreadPoolExecutor` but never called `.result()` on the
returned futures. Exceptions inside threads were swallowed; the test asserted on
the *final* file content while race-induced losses simply produced a smaller
final-line-count without raising. Combined with a deterministic-seeming sleep,
this produced false-green runs.

### Required patterns
1. **Always call `future.result()`** on every submitted job — surfaces exceptions
   that would otherwise be silently lost. (Fixed in commit `2e7c613` for the
   specific test; the rule must be a project convention.)
2. **Assert on a stable invariant, not a timing window.** Correct invariant:
   `total_lines_written_by_workers == lines_present_in_file_after_migrator_finishes`.
   The migrator may rewrite to add envelope fields but must preserve every line.
3. **Use a record-count + content-hash assertion**, not just a line count. A
   torn-write that loses one record but adds an empty line still satisfies a
   line-count test.
4. **Process-isolated workers via `multiprocessing.Process`** for cross-process
   `flock` semantics. `fcntl.flock` on the *same* process treats locks as
   reentrant; only separate processes exercise the real OS-level mutex. Threads
   alone cannot reproduce the OI-1370 race. (This is the root cause of why
   single-process pytest unit tests have repeatedly missed the issue.)
5. **Inject a "slow read" hook** into the migrator under test so the race window
   is wide. The migrator's read-tmp-rename is sub-millisecond in normal operation;
   without a sleep injection, a writer rarely loses the race even when broken.
   Use a monkeypatched `time.sleep` *inside the lock-held region* to make races
   reproducible. (This is how `tests/concurrency/test_oi1370_envelope_migration.py`
   ought to be structured.)
6. **Cover all writer paths in one test matrix.** Parametrise by writer:
   `(canonical_append_event, gate_register_emit, cleanup_worker_exit_audit,
   compact_receipts, backfill_update_ndjson)`. Earlier rounds tested only one
   path and codex kept finding the next.

### Pytest skeleton (one file per PR)
```
tests/concurrency/test_state_writer_<writer>.py
  - fixture: temp state_dir, sentinel filenames patched to point inside it
  - n_procs = 8, n_iters = 200
  - launch n_procs subprocesses each calling the writer-under-test
  - launch 1 subprocess running the migrator on a 50ms loop
  - join all, assert: len(unique records) == n_procs * n_iters
  - assert: every line in final file parses as JSON
  - assert: no orphaned tmp files in the dir
```

Threading vs multiprocessing: use **multiprocessing for fcntl correctness, threading
acceptable only for a smoke layer**. External `subprocess.Popen` of a tiny worker
script is the most realistic — that's what production looks like (dispatcher,
worker, supervisor are all separate processes).

---

## 6. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Sentinel filename collision with operator tooling (e.g. `.state.lock` already used by some shell script) | low | medium | Audit before PR N4. The current code already uses `.state.lock` and `append_receipt.lock`; we are not renaming, only formalising. |
| Helper introduces a deadlock if a caller forgets to release | very low | high | `with`-statement only API; no explicit acquire/release. Pattern is `with sentinel.open(): flock(); ...; auto-release`. Same as today. |
| Performance regression from added lock on previously-unlocked paths (`compact_receipts`, `backfill`) | low | low | Both are batch jobs run at low frequency. Receipt-write hot path gains no new lock — it already takes `append_receipt.lock`. |
| Cross-process flock on macOS APFS edge cases | low | medium | Existing code already relies on `fcntl.flock` cross-process; behavior unchanged. Add a CI run on macOS in addition to Linux for the new concurrency suite. |
| Reader holding `LOCK_SH` during migrator `os.replace` sees zero-byte file briefly | low | low | `os.replace` is atomic at the directory entry level; readers either see old or new inode, never empty. Confirmed by existing operation. |
| Bash shell scripts (`dispatch_lifecycle.sh`, `queue_auto_accept.sh`) call `dispatch_register.py append ...` — already goes through canonical writer | n/a | n/a | Already safe via subprocess → `append_event` → `_write_event_locked`. No change needed. |
| Operator runs migrator manually while workers active | medium | medium | Post-fix this is no longer dangerous (race closed). Still document as best practice to quiesce before re-stamping. |

---

## 7. Recommendation — ship as pre-rc2 fix (NOT in v1.0.0 final)

### v1.0.0 final readiness
Operationally, OI-1370 is mitigable for the v1.0.0 final cut by **not running
`migrate_phase3_envelope` while dispatches are active**. That is:
- The migrator is a *one-shot* envelope re-stamper used at Phase 3 cutover. In normal
  steady-state operation it is not invoked.
- The race only fires when a writer is in flight at the moment the migrator does
  `os.replace`. Quiescent state → no race.
- v1.0.0 final does not require the migrator to run during operator load. Document
  the constraint in `RELEASE_NOTES.md` and ship.

### Why not in v1.0.0
- The refactor touches 4 writer modules + 1 migrator + 1 helper across 4 PRs.
- Wave 5 / wave 2 work in flight (PRs #478-#481) already crowds the merge train.
- Each PR needs a real concurrency test with multiprocessing — these are slow and
  flake-prone; landing them under v1.0.0 deadline pressure is how round-3 happened.

### When to ship: post-v1.0.0, pre-rc2
- Wave 6 doc-hygiene and v1.0.0 cut go first.
- Open OI-1370 sub-tickets for each of the 4 PRs immediately after v1.0.0 tag.
- Land the helper (PR N1) week 1, the four writer migrations (PR N2, N3, N4) over
  the following 2-3 weeks, with a real concurrency suite gating each.
- Target rc2 as the first build that closes OI-1370 structurally.

### What we owe v1.0.0 in the meantime
- Add a one-line note to `RELEASE_NOTES.md`: "OI-1370 mitigated operationally —
  do not run `migrate_phase3_envelope` while workers are active. Structural fix
  scheduled for rc2."
- Open OI-1370-A1 through OI-1370-A4 (one per planned PR) so the queue is visible.
- Keep the existing 3-attempt history in `claudedocs/` for context.
