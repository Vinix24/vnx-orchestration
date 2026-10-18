# codex_gate — Headless Gate Report

**PR**: 2089
**Branch**: dispatch/20261017-cr-e-export
**Gate**: codex_gate
**Generated**: 2026-10-17T17:44:15Z

---

## Findings

Blocking count: 1
Advisory count: 3

### Blocking

- [BLOCKING] The update-ref call follows symbolic refs. With the export ref pointing to refs/heads/main, export exits successfully after moving checked-out main, leaving its index inconsistent with HEAD. Reject symbolic export refs and prevent dereferencing during publication and rollback. This also reproduces in the initial feature commit, so it is not a fix-round regression. (original severity: error)

### Advisory

- `NEW=$CANDIDATE` establishes rollback ownership before compare-and-swap succeeds. If another export publishes the same candidate first, the CAS fails, but undo_export deletes or resets that competing export's branch. Reproduced deletion after a 'reference already exists' failure. An explicit CAS failure must not trigger branch rollback. (severity: warning)
- `old = open(market_path, encoding="utf-8").read()` translates CRLF to LF. Exporting a CRLF marketplace consequently rewrites unrelated entries, violating byte preservation. The README reader has the same problem. Preserve original newline bytes when reading and merging these files. (severity: warning)
- `return tuple(int(n) for n in found.groups()) if found else (0, 0, 0)` discards prerelease identifiers. Consequently, exporting 0.1.1 after releasing 0.1.1-rc.1 incorrectly fails the version-bump check. Compare complete semantic-version precedence. (severity: warning)
