# codex_gate — Headless Gate Report

**PR**: 2089
**Branch**: dispatch/20261017-cr-e-export
**Gate**: codex_gate
**Generated**: 2026-10-17T10:04:10Z

---

## Findings

Blocking count: 1
Advisory count: 2

### Blocking

- [BLOCKING] The added `tgit checkout -q "$BRANCH" || fail "cannot check out $BRANCH in the target"` can overwrite ignored local files outside the owned paths when the export branch tracks them. Reproduced with private.txt: re-export returned 0, deleted the local file during restoration, and reported unchanged state. Use --no-overwrite-ignore for branch switches and restoration, or reject collisions before checkout. (original severity: error)

### Advisory

- The added `exit 0` returns before leak_scrub on unchanged exports. After committing a forbidden email into the target README, re-export returned 0 without reporting the leak. Run the scrub before the no-op success return, while the export branch is checked out. (severity: warning)
- The added `cp -R "$TMP/new/." "$TARGET/$OWNED_DST/" || fail "could not copy $OWNED_DST into the target"` retains destination permissions on existing files. Reproduced after a version bump: the source recorded hooks/run.sh as 100644, but the successful export still committed it as 100755. Preserve source executable bits when replacing existing files. (severity: warning)
