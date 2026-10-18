# codex_gate — Headless Gate Report

**PR**: 2080
**Branch**: dispatch/20261016-cr-b1-state-inject
**Gate**: codex_gate
**Generated**: 2026-10-17T04:37:49Z

---

## Findings

Blocking count: 1
Advisory count: 4

### Blocking

- [BLOCKING] The new `os.rename(source, target)` follows symlinked parent directories. With project A's .claude/rotation pointing to project B's rotation directory, the real SessionStart hook automatically injected B's handover, renamed B's ready marker, and wrote into B's archive. The storage checks in trimming and cleanup do not protect claiming or delivery. Validate storage before reading markers and anchor mutations to verified directory descriptors. (original severity: error)

### Advisory

- The round-2 cleanup guard still permits data loss at `_unlink_logged(project_dir, marker["handover"])`. The Write tool does not acquire project_lock, so it can replace the handover after read_bytes returns but before this unlink. A targeted reproduction deleted the newer, unarchived handover. Retain the original or coordinate its writers with cleanup; comparing previously read bytes does not protect this deletion. (severity: warning)
- `entries = handover_injection.resume_entries(session.project_dir, session.session_id)` rebuilds the numbered selection instead of preserving the list shown to the user. Reproduced: list handover A as number 1, create newer handover B, then issue resume 1; B is injected and its original removed. Bind selections to stable handover identifiers or reject selections when the displayed list has changed. (severity: warning)
- `except LockTimeout as error:` does not catch filesystem errors from the project_lock newly added around cleanup. Making .lock unavailable after emission reproduced an escaping IsADirectoryError. hook_runtime then exits 1 despite having emitted the handover, violating cleanup's non-failing contract. Catch and report cleanup OSError failures locally while preserving the claim for recovery. (severity: warning)
- ADR-005: `os.replace(source, target)` commits the ready-to-outdated transition without first recording its decision in a canonical NDJSON ledger. This transition suppresses automatic injection, but injections.jsonl records only deliveries; an invalidated handover may never produce a ledger entry. Record invalidation decisions before changing their derived markers. (severity: warning)
