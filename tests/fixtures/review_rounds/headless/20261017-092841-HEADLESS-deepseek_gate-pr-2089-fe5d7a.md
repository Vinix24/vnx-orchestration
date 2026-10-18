# deepseek_gate — Headless Gate Report

**PR**: 2089
**Branch**: dispatch/20261017-cr-e-export
**Gate**: deepseek_gate
**Generated**: 2026-10-17T09:35:09Z

---

## Findings

Blocking count: 1
Advisory count: 5

### Blocking

- [BLOCKING] `git commit -F` has no pathspec, so it commits the whole index: a change the operator already staged in the target clone outside context-rotation/, .claude-plugin/marketplace.json and README.md is swept into the export commit and published to the public toolkit (reproduced: a staged docs/other.md landed in the export commit while the dirty check passed clean). This breaks the 'one reviewable commit' contract and silently consumes the operator's staged work. Commit with a pathspec (git commit -- "$OWNED_DST" "$MARKET_REL" "$README_REL") or refuse when the index is dirty outside the owned paths. (original severity: error)

### Advisory

- `reset -q --keep "$TIP_BEFORE" ... || true` can be refused when the working tree has local changes to files the export commit touched. Combined with the staged-change case above plus a leak hit, the tool prints 'the export commit was removed' while the leak commit and the run-created branch both remain in the target (reproduced). The cleanup claim is not verified before it is printed. (severity: warning)
- undo_export restores the previously checked-out ref only when this run created the branch. If the export branch already existed and the clone was on another branch, a failed run (or the 'nothing to export' path) leaves the target checked out on the export branch, contradicting 'Leave the target as it was found' (reproduced). (severity: warning)
- The marketplace assertion ends in `or [p["name"] for p in doc["plugins"]] == ["context-rotation"]`, so it still passes when the entry's version/description are wrong as long as the name matches. (severity: info)
- The exported hook is asserted with os.R_OK only; the executable bit is what matters for hooks. (Verified manually that cp -R preserves it, so no defect today — the test would not catch a regression.) (severity: info)
- leak_scrub scans the entire pre-existing toolkit README.md and marketplace.json, so any pre-existing email, /Users/ path or 'vnx' mention in those files blocks every export with a generic 'leak scrub failed' that does not say the content predates the export. (severity: info)
